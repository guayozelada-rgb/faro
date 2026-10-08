//! Secretos que pide el motor y concesiones por operación (ADR 0010 §2–4, spec F1a §4.3).
//!
//! El motor nunca toca el llavero: cuando una operación necesita un secreto escribe un
//! `secret_request` por stdout y [`SecretBroker`] decide, en este orden:
//!
//! 1. gramática de la referencia → `vault.invalid_ref`;
//! 2. `run_id` de una concesión activa (creada por `engine_call`, caduca con la llamada
//!    o a los `timeout + 5 s`, y todas desaparecen al reiniciar el motor) →
//!    `vault.secret_not_allowed`;
//! 3. referencia y operación concedidas → `vault.secret_not_allowed`. `{new}` permite
//!    **una sola** `create` por concesión, de una referencia que no exista
//!    (`vault.already_exists`), y el `delete` solo de la referencia que esa misma
//!    concesión intentó crear. El `delete` se acepta aunque la `create` siga en cola o
//!    haya fallado (el motor deshace al agotar su espera): solo borra del llavero si la
//!    `create` llegó a escribir; si no, responde `ok` sin tocarlo y cancela la `create`
//!    pendiente, que después se rechaza sin escribir ni el secreto ni el índice;
//! 4. `db/*` **nunca** → `vault.secret_not_allowed`;
//! 5. para `get`, que exista → `vault.not_found`;
//! 6. para `create`/`set`, la forma del valor (`wp/*/token`: ADR 0011 §3) →
//!    `vault.invalid_input`.
//!
//! Fallo del llavero → `vault.keyring_unavailable`. Las operaciones del llavero van en
//! `spawn_blocking` y en serie. Cada solicitud, aceptada o no, deja un evento de
//! auditoría sin el valor ([`audit`]).
//!
//! `wp/{site_id}/token` solo se concede si el sitio es del perfil activo
//! ([`sites::SiteRegistry`]). El valor de un secreto solo existe en `SecretString` o
//! `Zeroizing` y nunca aparece en `Debug`, logs, eventos ni valores de retorno.
//!
//! **Dos tipos de concesión** en el mismo mapa (ADR 0014 §1): las de operación, que crea
//! `engine_call` y viven lo que dura la llamada, y las de ejecución de un agente, que pide
//! el motor con `run_grant_request` ([`run_grants`]). Las dos quedan atadas a generación
//! y perfil y se revalidan tras el candado del llavero; las de ejecución solo llevan
//! `get` y desaparecen al pausar los agentes.

pub mod audit;
pub mod operations;
pub mod refs;
pub mod request;
pub mod run_grants;
pub mod sites;

#[cfg(test)]
mod tests;

use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use secrecy::{ExposeSecret, SecretString};
use tokio::sync::mpsc;
use tokio::time::Instant;
use zeroize::Zeroizing;

use crate::agents::manifest::{self, AgentTable};
use crate::engine::protocol::{parse_stdout_line, StdoutLine};
use crate::engine::supervisor::StdinWriter;
use crate::error::AppError;
use crate::logging::sample::LogSampler;
use crate::profile::{new_uuid_v7, NIL_PROFILE_ID};
use crate::secrets::audit::{Action, Actor, AuditEvent, AuditQueue, DetailKey, Outcome};
use crate::secrets::operations::{OperationSpec, TemplateKind};
use crate::secrets::refs::{is_canonical_uuid, parse_ref, wp_ref, RefKind};
use crate::secrets::request::{
    error_line, is_valid_value, is_valid_wp_token_value, ok_line, parse_request, value_line, Op,
    ParsedRequest, SecretRequest,
};
use crate::secrets::sites::SiteRegistry;
use crate::vault::store::SecretStore;

/// Margen de la concesión sobre el tiempo máximo de la operación (ADR 0010 §3).
pub const GRANT_MARGIN: Duration = Duration::from_secs(5);
/// Solicitudes pendientes por motor; si el motor manda más, se descartan (y el motor
/// agota su espera de 10 s).
pub const PENDING_REQUESTS: usize = 32;

const INVALID_REF: &str = "vault.invalid_ref";
const NOT_ALLOWED: &str = "vault.secret_not_allowed";
const NOT_FOUND: &str = "vault.not_found";
const ALREADY_EXISTS: &str = "vault.already_exists";
const INVALID_INPUT: &str = "vault.invalid_input";
const KEYRING_UNAVAILABLE: &str = "vault.keyring_unavailable";

/// `{new}` de una concesión: una sola `create` y el `delete` de esa misma referencia.
///
/// Venga en el orden que venga (`create` y luego `delete`, o un `delete` con la `create`
/// todavía esperando el llavero), al final no queda secreto en el llavero.
#[derive(Debug)]
struct NewSlot {
    allow_delete: bool,
    /// Referencia de la única `create` de la concesión, haya terminado bien o no.
    attempted: Option<String>,
    /// Se fija solo cuando la `create` escribió en el llavero.
    created: Option<String>,
    /// Llegó un `delete` antes de que la `create` escribiera: si todavía no se ejecutó,
    /// ya no escribirá.
    cancelled: bool,
}

/// Para quién es una concesión: el texto de `details.operation` en la auditoría y, si es
/// de ejecución, el tipo de agente.
#[derive(Debug, Clone)]
struct Origin {
    /// `operationId` (concesión de operación) o `agent:<kind>` (de ejecución).
    label: Arc<str>,
    /// Tipo de agente: solo en las concesiones de ejecución.
    agent: Option<Arc<str>>,
}

impl Origin {
    fn operation(operation_id: &str) -> Self {
        Self {
            label: Arc::from(operation_id),
            agent: None,
        }
    }

    fn run(agent: &str) -> Self {
        Self {
            label: Arc::from(format!("agent:{agent}")),
            agent: Some(Arc::from(agent)),
        }
    }

    fn is_run(&self) -> bool {
        self.agent.is_some()
    }
}

#[derive(Debug)]
struct Grant {
    /// Número de serie único (nunca se repite en la vida de la app): `prepare` exige que
    /// la concesión tras el candado sea la misma que se comprobó, aunque otra con el mismo
    /// `run_id` la haya reemplazado (p. ej. un motor nuevo; revisión de seguridad de T5).
    serial: u64,
    origin: Origin,
    expires_at: Instant,
    /// Motor y perfil para los que se concedió: la ejecución usa estos, no los actuales.
    generation: u64,
    profile_id: Option<String>,
    refs: Vec<(String, Vec<Op>)>,
    new_slot: Option<NewSlot>,
}

#[derive(Debug)]
struct BrokerState {
    generation: u64,
    running: bool,
    profile_id: Option<String>,
    grants: HashMap<String, Grant>,
    /// Pausa global de los agentes (ADR 0014 §2). Empieza en `true` (falla cerrado) hasta
    /// que `AgentsControl` aplica el estado guardado.
    agents_paused: bool,
    /// Último número de serie de concesión entregado.
    last_serial: u64,
}

impl Default for BrokerState {
    fn default() -> Self {
        Self {
            generation: 0,
            running: false,
            profile_id: None,
            grants: HashMap::new(),
            agents_paused: true,
            last_serial: 0,
        }
    }
}

impl Grant {
    /// ¿Incluye la referencia con esa operación (permiso simple)?
    fn allows(&self, secret_ref: &str, op: Op) -> bool {
        self.refs
            .iter()
            .any(|(r, ops)| r == secret_ref && ops.contains(&op))
    }
}

impl BrokerState {
    /// Número de serie para una concesión nueva.
    fn next_serial(&mut self) -> u64 {
        self.last_serial = self.last_serial.saturating_add(1);
        self.last_serial
    }

    /// Quita las concesiones caducadas.
    fn prune(&mut self, now: Instant) {
        self.grants.retain(|_, grant| grant.expires_at > now);
    }
}

/// Qué permite la concesión para esta solicitud.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Permit {
    Plain,
    NewCreate,
    NewDelete,
}

/// Rechazo con su código (respuesta) y motivo (auditoría y log).
#[derive(Debug, Clone, Copy)]
struct Denial {
    code: &'static str,
    reason: &'static str,
}

const fn deny(code: &'static str, reason: &'static str) -> Denial {
    Denial { code, reason }
}

/// Concesión que permitió una solicitud: qué permite, su origen y su número de serie.
#[derive(Debug, Clone)]
struct Checked {
    permit: Permit,
    origin: Origin,
    serial: u64,
}

/// Permiso o rechazo de una solicitud, con el origen de la concesión (si se encontró).
type GrantCheck = Result<Checked, (Denial, Option<Origin>)>;

/// Resultado de ejecutar una operación en el llavero.
enum Done {
    Value(SecretString),
    Ok(Action),
    /// Bien sin tocar el llavero (nada que hacer), con el motivo para la auditoría.
    Skipped(Action, &'static str),
    Failed(Action, &'static str, Option<&'static str>),
}

/// Qué hacer tras el candado del llavero, según la concesión en ese momento.
enum Step {
    /// Ejecutar en el llavero con el perfil de la concesión.
    Run(Option<String>),
    /// `delete` de `{new}` sin nada creado: responder bien sin tocar el llavero.
    Skip,
}

/// Concesión de una llamada en curso. Al soltarse, la concesión desaparece.
pub struct GrantGuard {
    broker: Arc<SecretBroker>,
    run_id: String,
}

impl GrantGuard {
    /// Valor de la cabecera `X-Faro-Run-Id`.
    pub fn run_id(&self) -> &str {
        &self.run_id
    }
}

impl fmt::Debug for GrantGuard {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("GrantGuard")
            .field("run_id", &self.run_id)
            .finish()
    }
}

impl Drop for GrantGuard {
    fn drop(&mut self) {
        self.broker.lock().grants.remove(&self.run_id);
    }
}

/// Avisos del log que un motor puede repetir sin límite: se muestrean (el primero y uno
/// de cada 100; revisión de seguridad de T5).
#[derive(Debug, Default)]
struct Samples {
    /// `secret_request` sin `id` válido.
    secret_unanswerable: LogSampler,
    /// `secret_request` malformada (con o sin `id`).
    secret_malformed: LogSampler,
    /// `run_grant_request` sin `id` válido.
    grant_unanswerable: LogSampler,
    /// `run_grant_request` malformada (con o sin `id`).
    grant_malformed: LogSampler,
    /// `run_grant_release` malformada.
    release_malformed: LogSampler,
    /// `run_grant_release` de una concesión que no existe.
    release_not_active: LogSampler,
}

/// Canal de secretos del núcleo. Uno por app (`AppState`), compartido con el supervisor.
pub struct SecretBroker {
    store: Arc<dyn SecretStore>,
    sites: SiteRegistry,
    audit: AuditQueue,
    /// Tabla de concesiones de los agentes (`agent-grants.json` incrustado).
    agents: Arc<AgentTable>,
    state: Mutex<BrokerState>,
    /// Serializa las operaciones del llavero de todas las solicitudes.
    keyring: tokio::sync::Mutex<()>,
    samples: Samples,
}

impl fmt::Debug for SecretBroker {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SecretBroker").finish_non_exhaustive()
    }
}

impl SecretBroker {
    pub fn new(store: Arc<dyn SecretStore>, app_data_dir: &Path, audit: AuditQueue) -> Self {
        Self {
            store,
            sites: SiteRegistry::new(app_data_dir),
            audit,
            agents: manifest::embedded(),
            state: Mutex::new(BrokerState::default()),
            keyring: tokio::sync::Mutex::new(()),
            samples: Samples::default(),
        }
    }

    /// Solo pruebas: otra tabla de agentes (la incrustada está vacía hasta T9).
    #[cfg(test)]
    #[must_use]
    pub(crate) fn with_agent_table(mut self, table: AgentTable) -> Self {
        self.agents = Arc::new(table);
        self
    }

    /// Tabla de agentes (también la usa el relevo de actividad).
    pub fn agent_table(&self) -> Arc<AgentTable> {
        Arc::clone(&self.agents)
    }

    /// Cola de auditoría (también la usa la Bóveda).
    pub fn audit(&self) -> &AuditQueue {
        &self.audit
    }

    fn lock(&self) -> MutexGuard<'_, BrokerState> {
        match self.state.lock() {
            Ok(state) => state,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    /// Un motor nuevo (`generation`) arrancó con el perfil activo `profile_id` (si se
    /// conoce). Borra todas las concesiones anteriores.
    pub fn engine_started(&self, generation: u64, profile_id: Option<&str>) {
        let mut state = self.lock();
        state.generation = generation;
        state.running = true;
        state.profile_id = profile_id
            .filter(|p| is_canonical_uuid(p) && *p != NIL_PROFILE_ID)
            .map(str::to_owned);
        state.grants.clear();
    }

    /// El motor se detuvo o se va a reiniciar: todas las concesiones desaparecen.
    pub fn engine_stopped(&self) {
        let mut state = self.lock();
        state.running = false;
        state.grants.clear();
    }

    /// Concesiones activas (diagnóstico y pruebas).
    pub fn active_grants(&self) -> usize {
        self.lock().grants.len()
    }

    /// Concesiones de ejecución activas (sin contar las caducadas).
    pub fn active_run_grants(&self) -> usize {
        let mut state = self.lock();
        state.prune(Instant::now());
        state.grants.values().filter(|g| g.origin.is_run()).count()
    }

    /// Pausa los agentes: borra **todas** las concesiones de ejecución y, desde ahora,
    /// rechaza las nuevas con `agents.paused`, en un solo paso bajo el candado. Las de
    /// operación (`engine_call`, que inicia el usuario) no se tocan. Devuelve cuántas
    /// se revocaron.
    pub fn pause_agents(&self) -> usize {
        let mut state = self.lock();
        state.agents_paused = true;
        let before = state.grants.len();
        state.grants.retain(|_, grant| !grant.origin.is_run());
        before - state.grants.len()
    }

    /// Reanuda: vuelven a concederse (con todas las comprobaciones).
    pub fn resume_agents(&self) {
        self.lock().agents_paused = false;
    }

    /// ¿Están pausados los agentes?
    pub fn agents_paused(&self) -> bool {
        self.lock().agents_paused
    }

    /// Crea la concesión de una llamada a `operation` del motor `generation`.
    ///
    /// `None` si la operación no pide secretos. `path` son los parámetros de ruta ya
    /// validados por `engine_call`: el mismo valor que va en la URL es el que se sustituye
    /// en la referencia. `wp/{param}/token` se omite (con aviso) si el sitio no es del
    /// perfil activo; `{new}` se omite si no hay perfil activo.
    pub fn grant(
        self: &Arc<Self>,
        operation: &'static OperationSpec,
        path: &BTreeMap<String, String>,
        generation: u64,
    ) -> Result<Option<GrantGuard>, AppError> {
        self.grant_with_ttl(
            operation,
            path,
            generation,
            Duration::from_secs(operation.timeout_seconds) + GRANT_MARGIN,
        )
    }

    fn grant_with_ttl(
        self: &Arc<Self>,
        operation: &'static OperationSpec,
        path: &BTreeMap<String, String>,
        generation: u64,
        ttl: Duration,
    ) -> Result<Option<GrantGuard>, AppError> {
        if operation.secrets.is_empty() {
            return Ok(None);
        }
        let profile_id = {
            let state = self.lock();
            if !state.running || state.generation != generation {
                return Err(AppError::engine_not_ready());
            }
            state.profile_id.clone()
        };
        let mut refs = Vec::new();
        let mut new_slot = None;
        for spec in &operation.secrets {
            let ops: Vec<Op> = spec.access.iter().filter_map(|a| Op::parse(a)).collect();
            match &spec.kind {
                TemplateKind::Llm => refs.push((spec.template.clone(), ops)),
                TemplateKind::WpParam(name) => {
                    let site_id = path.get(name).map(String::as_str).unwrap_or_default();
                    if !is_canonical_uuid(site_id) {
                        return Err(AppError::engine_invalid_request("path"));
                    }
                    let owned = profile_id
                        .as_deref()
                        .is_some_and(|profile| self.sites.contains(profile, site_id));
                    if owned {
                        refs.push((wp_ref(site_id), ops));
                    } else {
                        tracing::warn!(
                            operation = operation.operation_id.as_str(),
                            "el sitio no es del perfil activo: no se concede su secreto"
                        );
                    }
                }
                TemplateKind::WpNew => {
                    if profile_id.is_some() {
                        new_slot = Some(NewSlot {
                            allow_delete: ops.contains(&Op::Delete),
                            attempted: None,
                            created: None,
                            cancelled: false,
                        });
                    } else {
                        tracing::warn!(
                            operation = operation.operation_id.as_str(),
                            "sin perfil activo: no se concede crear secretos"
                        );
                    }
                }
            }
        }
        let run_id = new_uuid_v7().map_err(|_| {
            tracing::error!("el generador aleatorio del sistema no está disponible");
            AppError::internal_unexpected()
        })?;
        let mut state = self.lock();
        if !state.running || state.generation != generation {
            return Err(AppError::engine_not_ready());
        }
        let serial = state.next_serial();
        state.grants.insert(
            run_id.clone(),
            Grant {
                serial,
                origin: Origin::operation(operation.operation_id.as_str()),
                expires_at: Instant::now() + ttl,
                generation,
                profile_id,
                refs,
                new_slot,
            },
        );
        drop(state);
        Ok(Some(GrantGuard {
            broker: Arc::clone(self),
            run_id,
        }))
    }

    /// Solo pruebas: concesión con referencias arbitrarias (p. ej. `db/*`, que ninguna
    /// operación válida puede declarar) para probar las defensas posteriores.
    #[cfg(test)]
    pub(crate) fn insert_test_grant(&self, run_id: &str, refs: Vec<(String, Vec<Op>)>) {
        let mut state = self.lock();
        let generation = state.generation;
        let profile_id = state.profile_id.clone();
        let serial = state.next_serial();
        state.grants.insert(
            run_id.to_owned(),
            Grant {
                serial,
                origin: Origin::operation("pruebaInterna"),
                expires_at: Instant::now() + Duration::from_secs(60),
                generation,
                profile_id,
                refs,
                new_slot: None,
            },
        );
    }

    /// Solo pruebas: concesión de ejecución de `agent` con referencias arbitrarias.
    #[cfg(test)]
    pub(crate) fn insert_test_run_grant(
        &self,
        run_id: &str,
        agent: &str,
        refs: Vec<(String, Vec<Op>)>,
    ) {
        let mut state = self.lock();
        let generation = state.generation;
        let profile_id = state.profile_id.clone();
        let serial = state.next_serial();
        state.grants.insert(
            run_id.to_owned(),
            Grant {
                serial,
                origin: Origin::run(agent),
                expires_at: Instant::now() + Duration::from_secs(60),
                generation,
                profile_id,
                refs,
                new_slot: None,
            },
        );
    }

    /// Solo pruebas: la concesión `run_id` caduca ya (sin esperar su plazo).
    #[cfg(test)]
    pub(crate) fn expire_grant_now(&self, run_id: &str) {
        if let Some(grant) = self.lock().grants.get_mut(run_id) {
            grant.expires_at = Instant::now();
        }
    }

    /// Solo pruebas: concesión con caducidad corta.
    #[cfg(test)]
    pub(crate) fn grant_expiring(
        self: &Arc<Self>,
        operation: &'static OperationSpec,
        path: &BTreeMap<String, String>,
        generation: u64,
        ttl: Duration,
    ) -> Result<Option<GrantGuard>, AppError> {
        self.grant_with_ttl(operation, path, generation, ttl)
    }

    /// Tarea que atiende en orden las líneas del motor de la generación `generation`
    /// (`secret_request`, `run_grant_request` y `run_grant_release`) y responde por su
    /// stdin. Termina cuando se suelta el `Sender` (el proceso terminó).
    pub(crate) fn spawn_worker(
        self: &Arc<Self>,
        writer: StdinWriter,
        generation: u64,
    ) -> mpsc::Sender<Zeroizing<String>> {
        let (tx, mut rx) = mpsc::channel::<Zeroizing<String>>(PENDING_REQUESTS);
        let broker = Arc::clone(self);
        tokio::spawn(async move {
            while let Some(line) = rx.recv().await {
                let response = match parse_stdout_line(&line) {
                    StdoutLine::RunGrantRequest => broker.handle_run_grant_line(&line, generation),
                    StdoutLine::RunGrantRelease => {
                        broker.handle_run_release_line(&line, generation);
                        None
                    }
                    _ => broker.handle_line(&line).await,
                };
                let Some(response) = response else {
                    continue;
                };
                drop(line);
                if !writer.send(into_bytes(response)).await {
                    tracing::warn!("no se pudo responder al motor la solicitud de secreto");
                }
            }
        });
        tx
    }

    /// Atiende una línea `secret_request`. Devuelve la respuesta (en memoria que se borra
    /// al soltarse) o `None` si no se puede responder (sin `id` válido).
    pub async fn handle_line(&self, line: &str) -> Option<Zeroizing<String>> {
        let request = match parse_request(line) {
            ParsedRequest::Request(request) => request,
            ParsedRequest::Malformed { id } => {
                self.record_denial(None, None, None, None, deny(NOT_ALLOWED, "malformed"));
                return Some(error_line(&id, NOT_ALLOWED));
            }
            ParsedRequest::Unanswerable => {
                if self.samples.secret_unanswerable.hit() {
                    tracing::warn!(
                        total = self.samples.secret_unanswerable.total(),
                        "solicitud de secreto sin id válido: se ignora"
                    );
                }
                self.record_denial(None, None, None, None, deny(NOT_ALLOWED, "malformed"));
                return None;
            }
        };
        Some(self.handle(request).await)
    }

    async fn handle(&self, request: SecretRequest) -> Zeroizing<String> {
        let SecretRequest {
            id,
            run_id,
            op,
            secret_ref,
            value,
        } = request;
        let op = Op::parse(&op);
        let op_text = op.map(Op::as_str);

        // 1. Gramática.
        let Some(kind) = parse_ref(&secret_ref) else {
            self.record_denial(
                None,
                Some(&run_id),
                op_text,
                None,
                deny(INVALID_REF, "invalid_ref"),
            );
            return error_line(&id, INVALID_REF);
        };
        let Some(op) = op else {
            self.record_denial(
                Some(&secret_ref),
                Some(&run_id),
                None,
                None,
                deny(NOT_ALLOWED, "invalid_op"),
            );
            return error_line(&id, NOT_ALLOWED);
        };
        // 2–3. Concesión activa con esa referencia y operación.
        let checked = match self.check_grant(&run_id, &secret_ref, kind, op) {
            Ok(found) => found,
            Err((denial, origin)) => {
                self.record_denial(
                    Some(&secret_ref),
                    Some(&run_id),
                    Some(op.as_str()),
                    origin.as_ref(),
                    denial,
                );
                return error_line(&id, denial.code);
            }
        };
        let Checked {
            permit,
            origin,
            serial,
        } = checked;
        let context = Context {
            secret_ref: &secret_ref,
            run_id: &run_id,
            op,
            serial,
            origin: Some(origin),
            site_id: match kind {
                RefKind::Wp { site_id } => Some(site_id),
                _ => None,
            },
        };
        // 4. `db/*` nunca (ninguna concesión la incluye; defensa explícita).
        if matches!(kind, RefKind::Db) {
            return self.denied(&id, &context, deny(NOT_ALLOWED, "db_ref"));
        }
        // 6. Forma del valor (solo `create`/`set` llevan valor).
        let value_ok = match (&value, op.takes_value()) {
            (Some(value), true) => match kind {
                RefKind::Wp { .. } => is_valid_wp_token_value(value),
                _ => is_valid_value(value),
            },
            (None, false) => true,
            _ => false,
        };
        if !value_ok {
            return self.denied(&id, &context, deny(INVALID_INPUT, "invalid_value"));
        }
        // Una copia exacta (sin realojar) en `SecretString`; `value` se borra al soltarse.
        let secret = value.map(|v| SecretString::from(v.as_str().to_owned()));

        // 5 y ejecución en el llavero.
        let done = match self.execute(&context, permit, secret).await {
            Ok(done) => done,
            Err(denial) => return self.denied(&id, &context, denial),
        };
        self.finish(&id, &context, done)
    }

    fn check_grant(&self, run_id: &str, secret_ref: &str, kind: RefKind<'_>, op: Op) -> GrantCheck {
        let mut state = self.lock();
        state.prune(Instant::now());
        let Some(grant) = state.grants.get_mut(run_id) else {
            return Err((deny(NOT_ALLOWED, "run_inactive"), None));
        };
        let origin = grant.origin.clone();
        let serial = grant.serial;
        let checked = |permit| {
            Ok(Checked {
                permit,
                origin: origin.clone(),
                serial,
            })
        };
        if grant.allows(secret_ref, op) {
            return checked(Permit::Plain);
        }
        if let (Some(slot), RefKind::Wp { .. }) = (grant.new_slot.as_mut(), kind) {
            if op == Op::Create {
                if slot.attempted.is_some() {
                    return Err((deny(NOT_ALLOWED, "new_already_used"), Some(origin)));
                }
                slot.attempted = Some(secret_ref.to_owned());
                return checked(Permit::NewCreate);
            }
            if op == Op::Delete
                && slot.allow_delete
                && slot.attempted.as_deref() == Some(secret_ref)
            {
                if slot.created.is_none() {
                    // La `create` sigue en cola o falló: si todavía no escribió, ya no lo
                    // hará. Se marca aquí (no tras el candado) para que gane aunque la
                    // `create` esté antes en la cola del llavero.
                    slot.cancelled = true;
                }
                return checked(Permit::NewDelete);
            }
        }
        Err((deny(NOT_ALLOWED, "not_granted"), Some(origin)))
    }

    /// Ejecuta la operación en el llavero (en serie y en `spawn_blocking`).
    async fn execute(
        &self,
        context: &Context<'_>,
        permit: Permit,
        secret: Option<SecretString>,
    ) -> Result<Done, Denial> {
        let _serial = self.keyring.lock().await;
        // La espera del candado puede cruzarse con el fin de la llamada, un reinicio del
        // motor o un `delete` que cancela la `create`: se vuelve a comprobar la concesión
        // y se usa su perfil, no el actual.
        let profile_id = match self.prepare(context, permit)? {
            Step::Run(profile_id) => profile_id,
            Step::Skip => return Ok(Done::Skipped(Action::Deleted, "not_created")),
        };
        let store = Arc::clone(&self.store);
        let sites = self.sites.clone();
        let secret_ref = context.secret_ref.to_owned();
        let site_id = context.site_id.map(str::to_owned);
        let op = context.op;
        let dispatch = tracing::dispatcher::get_default(Clone::clone);
        let task = move || {
            tracing::dispatcher::with_default(&dispatch, || {
                run_in_keyring(
                    store.as_ref(),
                    &sites,
                    &secret_ref,
                    op,
                    permit,
                    secret,
                    profile_id.as_deref(),
                    site_id.as_deref(),
                )
            })
        };
        let done = match tokio::task::spawn_blocking(task).await {
            Ok(done) => done,
            Err(_) => {
                tracing::error!("falló la tarea del llavero");
                Done::Failed(action_for(op), KEYRING_UNAVAILABLE, None)
            }
        };
        if permit == Permit::NewCreate && matches!(done, Done::Ok(_)) {
            // Se recuerda lo creado para permitir su `delete` en esta misma concesión.
            if let Some(grant) = self.lock().grants.get_mut(context.run_id) {
                if let Some(slot) = grant.new_slot.as_mut() {
                    slot.created = Some(context.secret_ref.to_owned());
                }
            }
        }
        Ok(done)
    }

    /// Ya con el candado del llavero (ninguna otra solicitud ejecuta): la concesión debe
    /// ser **la misma** que se comprobó (mismo número de serie: otra con el mismo `run_id`,
    /// p. ej. de un motor nuevo, no vale), seguir viva (no caducó y el motor que la recibió
    /// sigue en marcha) y, si es un permiso simple, seguir incluyendo la referencia y la
    /// operación. Para `{new}`:
    ///
    /// - `create` cancelada por un `delete` → se rechaza sin tocar el llavero ni el índice;
    /// - `delete` sin nada creado en esta concesión → bien sin tocar el llavero (y la
    ///   `create`, si llega después, queda cancelada).
    fn prepare(&self, context: &Context<'_>, permit: Permit) -> Result<Step, Denial> {
        let mut state = self.lock();
        let (running, generation) = (state.running, state.generation);
        let grant = match state.grants.get_mut(context.run_id) {
            Some(grant)
                if grant.serial == context.serial
                    && grant.expires_at > Instant::now()
                    && running
                    && grant.generation == generation =>
            {
                grant
            }
            _ => return Err(deny(NOT_ALLOWED, "run_inactive")),
        };
        if permit == Permit::Plain && !grant.allows(context.secret_ref, context.op) {
            return Err(deny(NOT_ALLOWED, "not_granted"));
        }
        match permit {
            Permit::NewCreate if grant.new_slot.as_ref().is_some_and(|slot| slot.cancelled) => {
                Err(deny(NOT_ALLOWED, "new_cancelled"))
            }
            Permit::NewDelete => {
                let created = grant
                    .new_slot
                    .as_ref()
                    .and_then(|slot| slot.created.as_deref())
                    == Some(context.secret_ref);
                if created {
                    return Ok(Step::Run(grant.profile_id.clone()));
                }
                if let Some(slot) = grant.new_slot.as_mut() {
                    slot.cancelled = true;
                }
                Ok(Step::Skip)
            }
            _ => Ok(Step::Run(grant.profile_id.clone())),
        }
    }

    fn finish(&self, id: &str, context: &Context<'_>, done: Done) -> Zeroizing<String> {
        let (line, action, outcome, code, reason) = match done {
            Done::Value(secret) => (
                value_line(id, secret.expose_secret()),
                Action::Used,
                Outcome::Ok,
                None,
                None,
            ),
            Done::Ok(action) => (ok_line(id), action, Outcome::Ok, None, None),
            Done::Skipped(action, reason) => (ok_line(id), action, Outcome::Ok, None, Some(reason)),
            Done::Failed(action, code, reason) => (
                error_line(id, code),
                action,
                Outcome::Error,
                Some(code),
                reason,
            ),
        };
        tracing::info!(
            op = context.op.as_str(),
            secret_ref = context.secret_ref,
            operation = context.origin.as_ref().map(|o| &*o.label),
            result = outcome.as_str(),
            error_code = code,
            "solicitud de secreto del motor"
        );
        let mut event = self.event(context, action, outcome);
        if let Some(code) = code {
            event = event.detail(DetailKey::ErrorCode, code);
        }
        if let Some(reason) = reason {
            event = event.detail(DetailKey::Reason, reason);
        }
        self.audit.record(event);
        line
    }

    fn event(&self, context: &Context<'_>, action: Action, outcome: Outcome) -> AuditEvent {
        let mut event = AuditEvent::new(Actor::System, action, outcome)
            .secret_ref(context.secret_ref)
            .run_id(context.run_id)
            .detail(DetailKey::Op, context.op.as_str());
        if let Some(origin) = &context.origin {
            event = with_origin(event, origin);
        }
        if let Some(site_id) = context.site_id {
            event = event.detail(DetailKey::SiteId, site_id);
        }
        event
    }

    fn denied(&self, id: &str, context: &Context<'_>, denial: Denial) -> Zeroizing<String> {
        self.record_denial(
            Some(context.secret_ref),
            Some(context.run_id),
            Some(context.op.as_str()),
            context.origin.as_ref(),
            denial,
        );
        error_line(id, denial.code)
    }

    /// Registra (log y auditoría) una solicitud rechazada, sin ningún valor.
    fn record_denial(
        &self,
        secret_ref: Option<&str>,
        run_id: Option<&str>,
        op: Option<&str>,
        origin: Option<&Origin>,
        denial: Denial,
    ) {
        // Solo se registra la referencia si cumple la gramática (nunca texto arbitrario).
        let valid_ref = secret_ref.filter(|r| parse_ref(r).is_some());
        // Las malformadas, que el motor puede repetir sin límite, se muestrean en el log y
        // se agregan en la auditoría.
        let malformed = denial.reason == "malformed";
        if !malformed || self.samples.secret_malformed.hit() {
            tracing::warn!(
                op,
                secret_ref = valid_ref,
                operation = origin.map(|o| &*o.label),
                error_code = denial.code,
                reason = denial.reason,
                total = malformed.then(|| self.samples.secret_malformed.total()),
                "solicitud de secreto rechazada"
            );
        }
        let mut event = AuditEvent::new(Actor::System, Action::Denied, Outcome::Denied)
            .detail(DetailKey::ErrorCode, denial.code)
            .detail(DetailKey::Reason, denial.reason);
        if malformed {
            event = event.aggregated();
        }
        if let Some(secret_ref) = valid_ref {
            event = event.secret_ref(secret_ref);
            if let Some(RefKind::Wp { site_id }) = parse_ref(secret_ref) {
                event = event.detail(DetailKey::SiteId, site_id);
            }
        }
        if let Some(run_id) = run_id {
            event = event.run_id(run_id);
        }
        if let Some(op) = op {
            event = event.detail(DetailKey::Op, op);
        }
        if let Some(origin) = origin {
            event = with_origin(event, origin);
        }
        self.audit.record(event);
    }
}

/// `details.operation` y, en las concesiones de ejecución, `details.agent_kind`.
fn with_origin(event: AuditEvent, origin: &Origin) -> AuditEvent {
    let event = event.detail(DetailKey::Operation, &origin.label);
    match &origin.agent {
        Some(agent) => event.detail(DetailKey::AgentKind, agent),
        None => event,
    }
}

/// Datos de la solicitud para auditoría y log (nunca el valor).
struct Context<'a> {
    secret_ref: &'a str,
    run_id: &'a str,
    op: Op,
    /// Número de serie de la concesión comprobada en `check_grant`.
    serial: u64,
    origin: Option<Origin>,
    site_id: Option<&'a str>,
}

fn action_for(op: Op) -> Action {
    match op {
        Op::Get => Action::Used,
        Op::Create => Action::Added,
        Op::Set => Action::Replaced,
        Op::Delete => Action::Deleted,
    }
}

/// Operación síncrona en el llavero. Errores del llavero → `vault.keyring_unavailable`.
#[allow(clippy::too_many_arguments)]
fn run_in_keyring(
    store: &dyn SecretStore,
    sites: &SiteRegistry,
    secret_ref: &str,
    op: Op,
    permit: Permit,
    secret: Option<SecretString>,
    profile_id: Option<&str>,
    site_id: Option<&str>,
) -> Done {
    let unavailable = |action| Done::Failed(action, KEYRING_UNAVAILABLE, None);
    match (op, permit, secret) {
        (Op::Get, Permit::Plain, None) => match store.get(secret_ref) {
            Ok(Some(secret)) => Done::Value(secret),
            Ok(None) => Done::Failed(Action::Used, NOT_FOUND, None),
            Err(_) => unavailable(Action::Used),
        },
        (Op::Create, Permit::NewCreate, Some(secret)) => {
            let (Some(profile_id), Some(site_id)) = (profile_id, site_id) else {
                return Done::Failed(Action::Added, NOT_ALLOWED, Some("no_profile"));
            };
            match store.get(secret_ref) {
                Ok(Some(_)) => return Done::Failed(Action::Added, ALREADY_EXISTS, None),
                Ok(None) => {}
                Err(_) => return unavailable(Action::Added),
            }
            // Primero el índice del perfil: un secreto de sitio nunca queda sin dueño.
            if let Err(err) = sites.add(profile_id, site_id) {
                tracing::warn!(kind = ?err.kind(), "no se pudo guardar el sitio en el índice del perfil");
                return Done::Failed(Action::Added, KEYRING_UNAVAILABLE, Some("site_index"));
            }
            match store.set(secret_ref, &secret) {
                Ok(()) => Done::Ok(Action::Added),
                Err(_) => unavailable(Action::Added),
            }
        }
        (Op::Set, Permit::Plain, Some(secret)) => {
            let existed = match store.get(secret_ref) {
                Ok(current) => current.is_some(),
                Err(_) => return unavailable(Action::Replaced),
            };
            let action = if existed {
                Action::Replaced
            } else {
                Action::Added
            };
            match store.set(secret_ref, &secret) {
                Ok(()) => Done::Ok(action),
                Err(_) => unavailable(action),
            }
        }
        (Op::Delete, Permit::Plain | Permit::NewDelete, None) => match store.delete(secret_ref) {
            Ok(()) => Done::Ok(Action::Deleted),
            Err(_) => unavailable(Action::Deleted),
        },
        // Combinaciones que las comprobaciones anteriores no dejan pasar.
        (op, _, _) => Done::Failed(action_for(op), NOT_ALLOWED, Some("not_granted")),
    }
}

/// Mueve el texto a bytes sin copiarlo (el búfer sigue bajo `Zeroizing`).
fn into_bytes(mut line: Zeroizing<String>) -> Zeroizing<Vec<u8>> {
    Zeroizing::new(std::mem::take(&mut *line).into_bytes())
}
