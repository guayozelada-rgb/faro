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

/// Modo "gestionado" de desarrollo: lanza el Python de `apps/engine/.venv` (ADR 0004).
#[cfg(debug_assertions)]
#[derive(Debug, Clone)]
pub struct DevVenvLauncher {
    engine_dir: PathBuf,
    python: PathBuf,
    data_dir: PathBuf,
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
        }
    }
}

#[cfg(debug_assertions)]
impl EngineLauncher for DevVenvLauncher {
    fn launch(&self) -> Result<EngineProcess, AppError> {
        if !self.python.is_file() {
            tracing::warn!("no existe apps/engine/.venv; ejecuta `npm run setup`");
            return Err(AppError::engine_start_failed("dev_env_missing"));
        }
        let mut command = tokio::process::Command::new(&self.python);
        command
            .args(["-m", "faro_engine", "--host", "127.0.0.1", "--port", "0"])
            .arg("--data-dir")
            .arg(&self.data_dir)
            .current_dir(&self.engine_dir)
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8");
        ChildProcess::spawn(command)
    }
}

/// Tiempo máximo para recoger un proceso ya matado.
pub(crate) const REAP_TIMEOUT: Duration = Duration::from_secs(5);
