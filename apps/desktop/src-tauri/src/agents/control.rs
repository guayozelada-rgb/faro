//! Pausa global de los agentes en el núcleo (ADR 0014 §2, spec F1b §4.5 y §5.3).
//!
//! - Estado en `<app_data_dir>/agents-control.json` =
//!   `{"version":1,"paused":true,"changed_at":"…Z"}`, escrito de forma atómica (temporal
//!   + `sync_all` + renombrar). No es secreto.
//! - Archivo ausente = no pausado. Ilegible o con otra forma = **pausado** (falla cerrado),
//!   con aviso en el log; no se sobrescribe hasta que el usuario pausa o reanuda.
//! - Pausar, en este orden: [`SecretBroker::pause_agents`] (borra todas las concesiones de
//!   ejecución y rechaza las nuevas con `agents.paused`) → avisa al motor → guarda el
//!   archivo → audita `agents.paused` (actor `user`). La revocación en memoria va primero
//!   (revisión de seguridad de T5): nunca espera al disco. Si el archivo no se pudo
//!   guardar, la pausa en memoria ya está aplicada y el comando devuelve
//!   `agents.control_unavailable`.
//! - Reanudar: guarda el archivo; si falla, no reanuda (`agents.control_unavailable`).
//! - El motor recibe `{"event":"agents_control","paused":…,"llm_providers":[…]}` justo
//!   después de `ready` (cada arranque) y en cada cambio: pausa, reanudación y alta o baja
//!   de una clave en la Bóveda. `llm_providers` son solo los nombres de los proveedores
//!   con clave. Una tarea propia lo envía por el escritor de stdin único, en orden: cada
//!   envío lleva el estado vigente en ese momento.

use std::fmt;
use std::fs;
use std::io::{self, Write as _};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;
#[cfg(test)]
use tokio::sync::oneshot;
use zeroize::Zeroizing;

use crate::engine::supervisor::StdinWriter;
use crate::error::AppError;
use crate::secrets::audit::{now_utc, Action, Actor, AuditEvent, AuditQueue, DetailKey, Outcome};
use crate::secrets::SecretBroker;
use crate::vault::Provider;

/// Archivo del estado, dentro de `<app_data_dir>`.
pub const CONTROL_FILE: &str = "agents-control.json";
/// Versión del formato.
pub const CONTROL_VERSION: u32 = 1;

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ControlFile {
    version: u32,
    paused: bool,
    changed_at: String,
}

/// Estado que devuelven los comandos `agents_*`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ControlState {
    pub paused: bool,
    /// `null` si nunca se cambió (archivo ausente) o si el archivo es ilegible.
    pub changed_at: Option<String>,
}

/// Proveedores con clave en la Bóveda (bloqueante: lee el llavero).
pub type ProviderSource = Arc<dyn Fn() -> Vec<Provider> + Send + Sync>;

enum ControlMsg {
    Attach(StdinWriter),
    Detach,
    Changed,
    ProvidersChanged,
    #[cfg(test)]
    Sync(oneshot::Sender<()>),
}

/// Lee el estado guardado. Ausente → activo; ilegible → pausado.
fn read_state(path: &Path) -> ControlState {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == io::ErrorKind::NotFound => {
            return ControlState {
                paused: false,
                changed_at: None,
            }
        }
        Err(err) => {
            tracing::warn!(kind = ?err.kind(), "no se pudo leer agents-control.json: agentes en pausa");
            return paused_unknown();
        }
    };
    match serde_json::from_slice::<ControlFile>(&bytes) {
        Ok(file)
            if file.version == CONTROL_VERSION
                && crate::agents::activity::is_utc_timestamp(&file.changed_at) =>
        {
            ControlState {
                paused: file.paused,
                changed_at: Some(file.changed_at),
            }
        }
        _ => {
            tracing::warn!("agents-control.json ilegible o con otra forma: agentes en pausa");
            paused_unknown()
        }
    }
}

fn paused_unknown() -> ControlState {
    ControlState {
        paused: true,
        changed_at: None,
    }
}

/// Escritura atómica: temporal en la misma carpeta, `sync_all` y renombrar.
fn write_state(path: &Path, paused: bool, changed_at: &str) -> io::Result<()> {
    let dir = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(dir)?;
    let tmp = path.with_extension("json.tmp");
    let body = serde_json::to_vec_pretty(&ControlFile {
        version: CONTROL_VERSION,
        paused,
        changed_at: changed_at.to_owned(),
    })
    .map_err(io::Error::other)?;
    let result = (|| {
        let mut out = fs::File::create(&tmp)?;
        out.write_all(&body)?;
        out.write_all(b"\n")?;
        out.sync_all()?;
        drop(out);
        fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

/// Línea `agents_control` para el stdin del motor.
fn control_line(paused: bool, providers: &[Provider]) -> Zeroizing<Vec<u8>> {
    let names: Vec<&str> = providers.iter().map(|p| p.as_str()).collect();
    let mut bytes = serde_json::to_vec(&serde_json::json!({
        "event": "agents_control",
        "paused": paused,
        "llm_providers": names,
    }))
    .unwrap_or_default();
    bytes.push(b'\n');
    Zeroizing::new(bytes)
}

/// Pausa global de los agentes. Una por app (`AppState`), compartida con el supervisor.
pub struct AgentsControl {
    path: PathBuf,
    state: Arc<Mutex<ControlState>>,
    broker: Arc<SecretBroker>,
    audit: AuditQueue,
    tx: mpsc::UnboundedSender<ControlMsg>,
    /// Serializa pausar y reanudar.
    ops: tokio::sync::Mutex<()>,
    /// Solo pruebas: se llama justo antes de guardar el archivo.
    #[cfg(test)]
    save_hook: Mutex<Option<Arc<dyn Fn() + Send + Sync>>>,
}

impl fmt::Debug for AgentsControl {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("AgentsControl")
            .field("state", &self.state())
            .finish_non_exhaustive()
    }
}

impl AgentsControl {
    /// Lee el estado guardado, lo aplica al canal de secretos y arranca la tarea que avisa
    /// al motor.
    pub fn load(
        runtime: &tokio::runtime::Handle,
        app_data_dir: &Path,
        broker: Arc<SecretBroker>,
        providers: ProviderSource,
    ) -> Arc<Self> {
        let path = app_data_dir.join(CONTROL_FILE);
        let initial = read_state(&path);
        if initial.paused {
            broker.pause_agents();
            tracing::info!("agentes en pausa al arrancar");
        } else {
            broker.resume_agents();
        }
        let audit = broker.audit().clone();
        let state = Arc::new(Mutex::new(initial));
        let (tx, rx) = mpsc::unbounded_channel();
        runtime.spawn(notify_engine(rx, Arc::clone(&state), providers));
        Arc::new(Self {
            path,
            state,
            broker,
            audit,
            tx,
            ops: tokio::sync::Mutex::new(()),
            #[cfg(test)]
            save_hook: Mutex::new(None),
        })
    }

    fn lock(&self) -> MutexGuard<'_, ControlState> {
        lock(&self.state)
    }

    /// Estado actual (comando `agents_control_state`).
    pub fn state(&self) -> ControlState {
        self.lock().clone()
    }

    /// Pausa todos los agentes (comando `agents_pause_all`).
    pub async fn pause(&self) -> Result<ControlState, AppError> {
        let _serial = self.ops.lock().await;
        let changed_at = {
            let current = self.lock();
            match (&current.changed_at, current.paused) {
                // Ya en pausa y guardada: se conserva cuándo empezó.
                (Some(at), true) => at.clone(),
                _ => now_utc(),
            }
        };
        // 1. Revocar y rechazar nuevas, en memoria y antes que nada.
        let state = ControlState {
            paused: true,
            changed_at: Some(changed_at.clone()),
        };
        *self.lock() = state.clone();
        let revoked = self.broker.pause_agents();
        // 2. Avisar al motor.
        let _ = self.tx.send(ControlMsg::Changed);
        // 3. Guardar (si falla, la pausa ya está aplicada).
        let saved = self.save(true, &changed_at).await;
        // 4. Auditar.
        let mut event = AuditEvent::new(Actor::User, Action::AgentsPaused, Outcome::Ok);
        if !saved {
            event = event
                .detail(DetailKey::Reason, "not_saved")
                .detail(DetailKey::ErrorCode, "agents.control_unavailable");
        }
        self.audit.record(event);
        tracing::info!(revoked, saved, "agentes en pausa");
        if saved {
            Ok(state)
        } else {
            Err(AppError::agents_control_unavailable())
        }
    }

    /// Reanuda los agentes (comando `agents_resume_all`). Si no se puede guardar, no
    /// reanuda.
    pub async fn resume(&self) -> Result<ControlState, AppError> {
        let _serial = self.ops.lock().await;
        let changed_at = now_utc();
        if !self.save(false, &changed_at).await {
            self.audit.record(
                AuditEvent::new(Actor::User, Action::AgentsResumed, Outcome::Error)
                    .detail(DetailKey::ErrorCode, "agents.control_unavailable"),
            );
            return Err(AppError::agents_control_unavailable());
        }
        let state = ControlState {
            paused: false,
            changed_at: Some(changed_at),
        };
        *self.lock() = state.clone();
        self.broker.resume_agents();
        let _ = self.tx.send(ControlMsg::Changed);
        self.audit.record(AuditEvent::new(
            Actor::User,
            Action::AgentsResumed,
            Outcome::Ok,
        ));
        tracing::info!("agentes reanudados");
        Ok(state)
    }

    /// La Bóveda agregó o borró una clave: se vuelve a avisar al motor.
    pub fn providers_changed(&self) {
        let _ = self.tx.send(ControlMsg::ProvidersChanged);
    }

    /// Motor recién `ready`: se le envía el estado ahora y en cada cambio.
    pub(crate) fn attach(&self, writer: StdinWriter) {
        let _ = self.tx.send(ControlMsg::Attach(writer));
    }

    /// El motor dejó de estar disponible.
    pub fn detach(&self) {
        let _ = self.tx.send(ControlMsg::Detach);
    }

    /// Espera a que la tarea procese todo lo anterior.
    #[cfg(test)]
    pub async fn sync(&self) {
        let (tx, rx) = oneshot::channel();
        let _ = self.tx.send(ControlMsg::Sync(tx));
        let _ = rx.await;
    }

    /// Solo pruebas: `hook` se llama justo antes de cada guardado.
    #[cfg(test)]
    pub(crate) fn set_save_hook(&self, hook: Arc<dyn Fn() + Send + Sync>) {
        *lock_any(&self.save_hook) = Some(hook);
    }

    async fn save(&self, paused: bool, changed_at: &str) -> bool {
        #[cfg(test)]
        if let Some(hook) = lock_any(&self.save_hook).clone() {
            hook();
        }
        let path = self.path.clone();
        let changed_at = changed_at.to_owned();
        match tokio::task::spawn_blocking(move || write_state(&path, paused, &changed_at)).await {
            Ok(Ok(())) => true,
            Ok(Err(err)) => {
                tracing::warn!(kind = ?err.kind(), "no se pudo guardar agents-control.json");
                false
            }
            Err(_) => {
                tracing::error!("falló la tarea que guarda agents-control.json");
                false
            }
        }
    }
}

fn lock(state: &Mutex<ControlState>) -> MutexGuard<'_, ControlState> {
    lock_any(state)
}

fn lock_any<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    match mutex.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    }
}

/// Lee los proveedores con clave fuera del runtime (el llavero es bloqueante).
async fn load_providers(source: &ProviderSource) -> Vec<Provider> {
    let source = Arc::clone(source);
    let dispatch = tracing::dispatcher::get_default(Clone::clone);
    let task = move || tracing::dispatcher::with_default(&dispatch, || source());
    match tokio::task::spawn_blocking(task).await {
        Ok(providers) => providers,
        Err(_) => {
            tracing::error!("falló la lectura de los proveedores con clave");
            Vec::new()
        }
    }
}

/// Tarea única que envía `agents_control` al motor conectado, en orden.
async fn notify_engine(
    mut rx: mpsc::UnboundedReceiver<ControlMsg>,
    state: Arc<Mutex<ControlState>>,
    source: ProviderSource,
) {
    let mut writer: Option<StdinWriter> = None;
    let mut providers: Vec<Provider> = Vec::new();
    while let Some(msg) = rx.recv().await {
        match msg {
            ControlMsg::Attach(new_writer) => {
                providers = load_providers(&source).await;
                writer = Some(new_writer);
            }
            ControlMsg::Detach => {
                writer = None;
                continue;
            }
            ControlMsg::Changed => {}
            ControlMsg::ProvidersChanged => providers = load_providers(&source).await,
            #[cfg(test)]
            ControlMsg::Sync(reply) => {
                let _ = reply.send(());
                continue;
            }
        }
        let Some(current) = &writer else {
            continue;
        };
        let paused = lock(&state).paused;
        if current.send(control_line(paused, &providers)).await {
            tracing::info!(
                paused,
                providers = providers.len(),
                "estado de los agentes enviado al motor"
            );
        } else {
            tracing::warn!("no se pudo enviar el estado de los agentes al motor");
            writer = None;
        }
    }
}

#[cfg(test)]
#[path = "control_tests.rs"]
mod tests;
