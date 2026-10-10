//! Cómo se lanza el proceso del motor (ADR 0004).
//!
//! - [`DevVenvLauncher`] (solo `debug_assertions`): Python de `apps/engine/.venv`.
//! - [`UnavailableLauncher`]: builds sin sidecar (release en F0) → `engine.start_failed`.
//! - Lanzador falso en pruebas (`engine::fake`).
//!
//! El lanzador de sidecar PyInstaller de la fase de release implementará el mismo trait.
//!
//! **Procesos huérfanos en Windows.** El `python.exe` de un `.venv` creado por uv es un
//! lanzador que crea el intérprete real como proceso hijo. Matar solo el PID lanzado
//! dejaría vivo al intérprete. Por eso cada proceso del motor se asigna a un Job Object
//! con `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: al cerrar el handle del job (al matar, al
//! terminar o si el propio núcleo muere) Windows termina todo el árbol.

use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncWrite};

use crate::error::AppError;

/// Futuro en caja, `Send`, para métodos de traits usados como objetos.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Proceso del motor recién lanzado.
pub struct EngineProcess {
    /// Entrada estándar del motor (token y eventos del protocolo).
    pub stdin: Box<dyn AsyncWrite + Send + Unpin>,
    /// Salida estándar del motor (solo protocolo).
    pub stdout: Box<dyn AsyncRead + Send + Unpin>,
    /// Salida de errores del motor (logs JSON).
    pub stderr: Box<dyn AsyncRead + Send + Unpin>,
    /// Control del ciclo de vida.
    pub control: Box<dyn ProcessControl>,
}

/// Control del proceso del motor y de todos sus descendientes.
pub trait ProcessControl: Send {
    /// Espera a que el proceso termine y devuelve el código de salida, si lo hay.
    /// Debe poder cancelarse (se usa dentro de `tokio::select!`).
    fn wait(&mut self) -> BoxFuture<'_, Option<i32>>;
    /// Mata el proceso **y todo su árbol**. Idempotente.
    fn kill(&mut self);
    /// PID del proceso lanzado (en Windows con uv, el lanzador del `.venv`).
    fn pid(&self) -> Option<u32>;
    /// PIDs vivos de todo el árbol (Job Object en Windows). Diagnóstico y pruebas.
    fn tree_pids(&self) -> Vec<u32> {
        Vec::new()
    }
}

/// Fábrica de procesos del motor.
pub trait EngineLauncher: Send + Sync {
    /// Lanza un proceso nuevo. Errores: `engine.start_failed` con
    /// `details.reason` = `spawn` | `dev_env_missing`.
    fn launch(&self) -> Result<EngineProcess, AppError>;
}

/// Lanzador para builds sin motor (release antes de la fase de release, spec F0 §4.3).
#[derive(Debug, Default, Clone, Copy)]
pub struct UnavailableLauncher;

impl EngineLauncher for UnavailableLauncher {
    fn launch(&self) -> Result<EngineProcess, AppError> {
        tracing::warn!("este build no incluye el motor");
        Err(AppError::engine_start_failed("spawn"))
    }
}

/// Proceso hijo real (tokio) + Job Object en Windows.
pub struct ChildProcess {
    child: tokio::process::Child,
    pid: Option<u32>,
    #[cfg(windows)]
    job: Option<win32job::Job>,
}

impl ChildProcess {
    /// Lanza `command` con stdin/stdout/stderr en tubería y lo encierra en un job
    /// (Windows). Si no se puede asignar el job, mata el proceso y falla: preferimos
    /// no arrancar antes que arriesgar procesos huérfanos.
    pub fn spawn(mut command: tokio::process::Command) -> Result<EngineProcess, AppError> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        #[cfg(windows)]
        {
            // Sin ventana de consola propia; el motor no la necesita.
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = command.spawn().map_err(|err| {
            tracing::warn!(kind = ?err.kind(), "no se pudo lanzar el proceso del motor");
            AppError::engine_start_failed("spawn")
        })?;
        let pid = child.id();

        #[cfg(windows)]
        let job = match assign_kill_on_close_job(&child) {
            Ok(job) => Some(job),
            Err(()) => {
                let _ = child.start_kill();
                return Err(AppError::engine_start_failed("spawn"));
            }
        };

        let (Some(stdin), Some(stdout), Some(stderr)) =
            (child.stdin.take(), child.stdout.take(), child.stderr.take())
        else {
            let _ = child.start_kill();
            tracing::error!("el proceso del motor no tiene tuberías estándar");
            return Err(AppError::engine_start_failed("spawn"));
        };

        let control = ChildProcess {
            child,
            pid,
            #[cfg(windows)]
            job,
        };
        Ok(EngineProcess {
            stdin: Box::new(stdin),
            stdout: Box::new(stdout),
            stderr: Box::new(stderr),
            control: Box::new(control),
        })
    }
}

#[cfg(windows)]
fn assign_kill_on_close_job(child: &tokio::process::Child) -> Result<win32job::Job, ()> {
    let mut info = win32job::ExtendedLimitInfo::new();
    info.limit_kill_on_job_close();
    let job = win32job::Job::create_with_limit_info(&info).map_err(|err| {
        tracing::error!(error = %err, "no se pudo crear el Job Object del motor");
    })?;
    let Some(handle) = child.raw_handle() else {
        tracing::error!("el proceso del motor terminó antes de asignarlo al job");
        return Err(());
    };
    job.assign_process(handle as isize).map_err(|err| {
        tracing::error!(error = %err, "no se pudo asignar el motor al Job Object");
    })?;
    Ok(job)
}

impl ProcessControl for ChildProcess {
    fn wait(&mut self) -> BoxFuture<'_, Option<i32>> {
        Box::pin(async move {
            match self.child.wait().await {
                Ok(status) => status.code(),
                Err(err) => {
                    tracing::warn!(kind = ?err.kind(), "no se pudo esperar al proceso del motor");
                    // Evita un bucle caliente en select!: si no se puede esperar, no termina nunca.
                    std::future::pending::<()>().await;
                    None
                }
            }
        })
    }

    fn kill(&mut self) {
        let _ = self.child.start_kill();
        // Cerrar el handle del job termina todo el árbol (KILL_ON_JOB_CLOSE).
        #[cfg(windows)]
        drop(self.job.take());
    }

    fn pid(&self) -> Option<u32> {
        self.pid
    }

    #[cfg(windows)]
    fn tree_pids(&self) -> Vec<u32> {
        self.job
            .as_ref()
            .and_then(|job| job.query_process_id_list().ok())
            .map(|list| {
                list.into_iter()
                    .filter_map(|p| u32::try_from(p).ok())
                    .collect()
            })
            .unwrap_or_default()
    }
}

impl Drop for ChildProcess {
    fn drop(&mut self) {
        self.kill();
    }
}

/// Carpeta `apps/engine` calculada desde `CARGO_MANIFEST_DIR` (`apps/desktop/src-tauri`).
pub fn dev_engine_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("engine")
}

/// Intérprete del `.venv` del motor.
pub fn venv_python(engine_dir: &Path) -> PathBuf {
    if cfg!(windows) {
        engine_dir.join(".venv").join("Scripts").join("python.exe")
    } else {
        engine_dir.join(".venv").join("bin").join("python")
    }
}

/// Variables del entorno del núcleo que hereda el motor (condición 11 de la revisión de
/// T2): lo mínimo para que el intérprete arranque y encuentre sus carpetas. Todo lo demás
/// se quita con `env_clear()`, entre otras: `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`,
/// proxies (`HTTP(S)_PROXY`, `ALL_PROXY`, `NO_PROXY`), `SSL_*`, `SSLKEYLOGFILE`,
/// `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `LITELLM_*`, `OPENAI_*`, `ANTHROPIC_*`,
/// `GEMINI_*`, `GOOGLE_API_KEY`, `LANGSMITH_*` y `LANGCHAIN_*`. En Windows los nombres no
/// distinguen mayúsculas.
pub const ENGINE_ENV_ALLOWLIST: &[&str] = &[
    // Windows
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PROGRAMDATA",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    // Ambos
    "PATH",
    "TEMP",
    "TMP",
    // macOS y Linux
    "HOME",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
];

/// Filtra el entorno con [`ENGINE_ENV_ALLOWLIST`] (sin distinguir mayúsculas).
pub fn engine_env(
    vars: impl IntoIterator<Item = (std::ffi::OsString, std::ffi::OsString)>,
) -> Vec<(std::ffi::OsString, std::ffi::OsString)> {
    vars.into_iter()
        .filter(|(name, _)| {
            name.to_str().is_some_and(|name| {
                ENGINE_ENV_ALLOWLIST
                    .iter()
                    .any(|allowed| allowed.eq_ignore_ascii_case(name))
            })
        })
        .collect()
}

/// Comando del motor con el entorno limpio (`env_clear()`), solo las variables de
/// [`ENGINE_ENV_ALLOWLIST`] del núcleo y las fijas del intérprete. Todo lanzador del motor
/// (también el de release, T11) debe partir de aquí; la prueba lanza un proceso real con
/// este mismo comando para comprobar que no hereda nada más.
pub fn engine_command(program: &Path) -> tokio::process::Command {
    let mut command = tokio::process::Command::new(program);
    command
        .env_clear()
        .envs(engine_env(std::env::vars_os()))
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONIOENCODING", "utf-8");
    command
}

/// Modo "gestionado" de desarrollo: lanza el Python de `apps/engine/.venv` (ADR 0004).
#[cfg(debug_assertions)]
#[derive(Debug, Clone)]
pub struct DevVenvLauncher {
    engine_dir: PathBuf,
    python: PathBuf,
    data_dir: PathBuf,
    allow_local_sites: bool,
    fake_llm: bool,
}

#[cfg(debug_assertions)]
impl DevVenvLauncher {
    /// `data_dir` se pasa como `--data-dir` (carpeta de datos de la app).
    pub fn new(data_dir: PathBuf) -> Self {
        let engine_dir = dev_engine_dir();
        let python = venv_python(&engine_dir);
        Self {
            engine_dir,
            python,
            data_dir,
            allow_local_sites: false,
            fake_llm: false,
        }
    }

    /// Lanza el motor con `--allow-local-sites` (ADR 0012). Quien lo llama decide con
    /// [`crate::engine::local_sites_allowed`]; este lanzador solo existe en debug.
    #[must_use]
    pub fn with_allow_local_sites(mut self, allow: bool) -> Self {
        self.allow_local_sites = allow;
        self
    }

    /// Lanza el motor con `--fake-llm` (spec F1b §4.1). Quien lo llama decide con
    /// [`crate::engine::fake_llm_allowed`]; este lanzador solo existe en debug.
    #[must_use]
    pub fn with_fake_llm(mut self, fake: bool) -> Self {
        self.fake_llm = fake;
        self
    }

    /// Argumentos del motor tras el intérprete.
    pub fn args(&self) -> Vec<std::ffi::OsString> {
        let mut args: Vec<std::ffi::OsString> = [
            "-m",
            "faro_engine",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--data-dir",
        ]
        .into_iter()
        .map(Into::into)
        .collect();
        args.push(self.data_dir.clone().into_os_string());
        if self.allow_local_sites {
            args.push("--allow-local-sites".into());
        }
        if self.fake_llm {
            args.push("--fake-llm".into());
        }
        args
    }
}

#[cfg(debug_assertions)]
impl EngineLauncher for DevVenvLauncher {
    fn launch(&self) -> Result<EngineProcess, AppError> {
        if !self.python.is_file() {
            tracing::warn!("no existe apps/engine/.venv; ejecuta `npm run setup`");
            return Err(AppError::engine_start_failed("dev_env_missing"));
        }
        if self.allow_local_sites {
            // Solo desarrollo (ADR 0012): `http` y loopback para wp-env.
            tracing::warn!("motor lanzado con --allow-local-sites (solo desarrollo)");
        }
        if self.fake_llm {
            // Solo desarrollo (spec F1b §4.1): IA simulada, sin claves ni llamadas reales.
            tracing::warn!("motor lanzado con --fake-llm (solo desarrollo)");
        }
        let mut command = engine_command(&self.python);
        command.args(self.args()).current_dir(&self.engine_dir);
        ChildProcess::spawn(command)
    }
}

/// Tiempo máximo para recoger un proceso ya matado.
pub(crate) const REAP_TIMEOUT: Duration = Duration::from_secs(5);

#[cfg(all(test, debug_assertions))]
mod tests {
    use super::*;

    #[test]
    fn dev_venv_sin_sitios_locales_por_defecto() {
        let launcher = DevVenvLauncher::new(PathBuf::from("datos"));
        let args = launcher.args();
        assert!(!args.iter().any(|a| a == "--allow-local-sites"));
        assert_eq!(
            args,
            [
                "-m",
                "faro_engine",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--data-dir",
                "datos"
            ]
            .map(std::ffi::OsString::from)
        );
    }

    #[test]
    fn dev_venv_con_sitios_locales_anade_el_argumento_al_final() {
        let launcher = DevVenvLauncher::new(PathBuf::from("datos")).with_allow_local_sites(true);
        let args = launcher.args();
        assert_eq!(
            args.last().map(|a| a.as_os_str()),
            Some("--allow-local-sites".as_ref())
        );
        assert_eq!(
            args.iter().filter(|a| *a == "--allow-local-sites").count(),
            1
        );
        let off = DevVenvLauncher::new(PathBuf::from("datos"))
            .with_allow_local_sites(true)
            .with_allow_local_sites(false);
        assert!(!off.args().iter().any(|a| a == "--allow-local-sites"));
    }

    #[test]
    fn dev_venv_sin_ia_simulada_por_defecto_y_con_ella_al_final() {
        let off = DevVenvLauncher::new(PathBuf::from("datos"));
        assert!(!off.args().iter().any(|a| a == "--fake-llm"));
        let on = DevVenvLauncher::new(PathBuf::from("datos"))
            .with_allow_local_sites(true)
            .with_fake_llm(true);
        let args = on.args();
        assert_eq!(
            args.last().map(|a| a.as_os_str()),
            Some("--fake-llm".as_ref())
        );
        assert_eq!(args.iter().filter(|a| *a == "--fake-llm").count(), 1);
        assert!(args.iter().any(|a| a == "--allow-local-sites"));
        let again_off = on.with_fake_llm(false);
        assert!(!again_off.args().iter().any(|a| a == "--fake-llm"));
    }

    #[test]
    fn el_motor_solo_hereda_las_variables_permitidas() {
        use std::ffi::OsString;
        let vars = [
            ("SystemRoot", "C:\\Windows"),
            ("PATH", "C:\\bin"),
            ("TEMP", "C:\\tmp"),
            ("HOME", "/home/ana"),
            ("PYTHONPATH", "C:\\malicioso"),
            ("PYTHONHOME", "C:\\malicioso"),
            ("PYTHONSTARTUP", "C:\\malicioso\\x.py"),
            ("HTTPS_PROXY", "http://proxy"),
            ("http_proxy", "http://proxy"),
            ("SSL_CERT_FILE", "C:\\ca.pem"),
            ("SSLKEYLOGFILE", "C:\\keys.log"),
            ("REQUESTS_CA_BUNDLE", "C:\\ca.pem"),
            ("OPENAI_API_KEY", "sk-test-ficticia"),
            ("ANTHROPIC_BASE_URL", "http://otro"),
            ("LITELLM_LOG", "DEBUG"),
            ("LANGSMITH_TRACING", "true"),
            ("FARO_ENGINE_DEV_TOKEN", "x"),
        ]
        .map(|(k, v)| (OsString::from(k), OsString::from(v)));
        let kept: Vec<String> = engine_env(vars)
            .into_iter()
            .map(|(k, _)| k.into_string().unwrap())
            .collect();
        assert_eq!(kept, ["SystemRoot", "PATH", "TEMP", "HOME"]);
    }

    /// Revisión de seguridad de T5: el proceso lanzado **no** hereda el entorno del núcleo
    /// (`env_clear()`), no solo que `engine_env` filtre. Un proceso real imprime su
    /// entorno; una variable del padre fuera de la lista (la trampa) no debe aparecer.
    #[tokio::test]
    async fn el_proceso_lanzado_no_hereda_el_entorno_del_nucleo() {
        let trap = std::env::vars_os()
            .filter_map(|(name, _)| name.into_string().ok())
            .find(|name| {
                !name.is_empty()
                    && !name.starts_with('=')
                    && !ENGINE_ENV_ALLOWLIST
                        .iter()
                        .any(|allowed| allowed.eq_ignore_ascii_case(name))
            })
            .expect("el entorno de la prueba tiene alguna variable fuera de la lista");
        let mut command = if cfg!(windows) {
            let shell = std::env::var_os("COMSPEC").unwrap_or_else(|| "cmd.exe".into());
            let mut command = engine_command(Path::new(&shell));
            command.args(["/d", "/c", "set"]);
            command
        } else {
            engine_command(Path::new("/usr/bin/env"))
        };
        #[cfg(windows)]
        command.creation_flags(0x0800_0000);
        let output = command.output().await.unwrap();
        assert!(output.status.success());
        let text = String::from_utf8_lossy(&output.stdout);
        let names: Vec<String> = text
            .lines()
            .filter_map(|line| line.split_once('='))
            .map(|(name, _)| name.to_ascii_uppercase())
            .collect();
        assert!(
            !names.contains(&trap.to_ascii_uppercase()),
            "{trap} se heredó: {names:?}"
        );
        assert!(names.contains(&"PATH".to_owned()), "{names:?}");
        assert!(names.contains(&"PYTHONUNBUFFERED".to_owned()), "{names:?}");
        for name in &names {
            assert!(
                ENGINE_ENV_ALLOWLIST
                    .iter()
                    .any(|allowed| allowed.eq_ignore_ascii_case(name))
                    || ["PYTHONUNBUFFERED", "PYTHONIOENCODING", "PROMPT"].contains(&name.as_str()),
                "variable inesperada en el motor: {name}"
            );
        }
    }

    #[test]
    fn dev_venv_sin_venv_falla_antes_de_lanzar_con_sitios_locales() {
        let (logs, _guard) = crate::test_logs::capture();
        let dir = tempfile::tempdir().unwrap();
        let mut launcher =
            DevVenvLauncher::new(dir.path().to_path_buf()).with_allow_local_sites(true);
        launcher.python = dir.path().join("no-existe").join("python.exe");
        let err = launcher.launch().err().unwrap();
        assert_eq!(err.code, "engine.start_failed");
        assert!(!logs.text().contains("--allow-local-sites"));
    }
}
