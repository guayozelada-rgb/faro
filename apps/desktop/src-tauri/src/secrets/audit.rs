//! Eventos de auditoría del núcleo (ADR 0010 §4): Bóveda y canal de secretos.
//!
//! Se envían al motor por stdin, una línea por evento:
//!
//! ```text
//! {"event":"audit","occurred_at":"…Z","actor":"user|system|agent","action":"secret.…",
//!  "secret_ref":"…"|null,"run_id":"…"|null,"result":"ok|denied|error","details":{…}}
//! ```
//!
//! [`AuditQueue`] guarda los eventos en un búfer **acotado** de [`AUDIT_BUFFER`] eventos
//! (revisión de seguridad de T5) y una tarea los envía en orden cuando el motor está listo
//! con su base disponible. `record` nunca bloquea ni hace crecer la memoria. Si el búfer
//! está lleno (motor no listo, o un motor que no lee su stdin), descarta **primero** el
//! rechazo o evento agregable más viejo y, solo si no queda ninguno, el más viejo de los
//! demás (un rechazo nuevo con el búfer lleno de `ok` se descarta él mismo). Cada descarte
//! se cuenta ([`AuditQueue::dropped`]) y se registra muestreado; al volver a enviar al
//! motor, antes que nada va un evento sintético `audit.dropped` con `details.count` (los
//! descartados desde el último aviso), que vive fuera del búfer y nunca se descarta. En
//! modo de desarrollo externo (sin stdin) los eventos solo van al log.
//!
//! **Rechazos que el motor puede provocar** (segunda revisión de seguridad de T5): todo
//! `secret.denied`, `agent.grant_denied` y `agent.grant_released` con resultado `denied`
//! se marca con [`AuditEvent::aggregated`] y se agrupa por (acción, actor, resultado,
//! `reason`, `error_code`). Cada grupo tiene un cubo de [`AGGREGATE_BURST`] fichas que
//! recupera una cada [`AGGREGATE_WINDOW`]:
//!
//! - con fichas, el rechazo se registra en el log y se audita al momento;
//! - sin fichas, se suma a un evento pendiente del grupo, que la tarea vuelca (una línea
//!   de log y un evento con `details.count`) cada [`AGGREGATE_WINDOW`] y al engancharse
//!   un motor.
//!
//! Al sumar se conservan solo los campos iguales en todos (referencia, `run_id` y
//! `details`). Así, una inundación con `run_id` o referencias distintas deja como mucho
//! unas pocas líneas por grupo y minuto, en el log y en la auditoría. Sin motor
//! enganchado, los agregables del mismo grupo también se suman en el búfer.
//!
//! El canal de control (motor listo, detenido, modo externo) no tiene límite: solo lo usa
//! el supervisor en cada cambio de estado del motor, nunca una línea del motor.
//!
//! Nunca llevan el valor de un secreto ni `last4`: solo acción, resultado, referencia,
//! `run_id` y `details` con claves y valores cortos y validados.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use serde::Serialize;
#[cfg(test)]
use tokio::sync::oneshot;
use tokio::sync::{mpsc, Notify};
use zeroize::Zeroizing;

use crate::engine::supervisor::StdinWriter;
use crate::logging::sample::LogSampler;
use crate::secrets::refs::{is_canonical_uuid, parse_ref};

/// Eventos que se guardan como máximo mientras el motor no puede recibirlos.
pub const AUDIT_BUFFER: usize = 500;
/// Máximo de `details.count` (el motor acepta de 1 a 18 dígitos).
pub const MAX_COUNT: u64 = 999_999_999_999_999_999;
/// Rechazos de un grupo que se registran uno a uno antes de agregar.
pub const AGGREGATE_BURST: u32 = 10;
/// Cada cuánto se recupera una ficha y se vuelcan los agregados pendientes.
pub const AGGREGATE_WINDOW: Duration = Duration::from_secs(60);
/// Grupos de agregación como máximo (los motivos son una lista cerrada: es una defensa).
pub const MAX_AGGREGATE_KEYS: usize = 128;

/// Quién hizo la acción.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Actor {
    User,
    System,
    Agent,
}

/// Acciones del núcleo (lista cerrada del motor: `CORE_ACTIONS` de `audit.py`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub enum Action {
    #[serde(rename = "secret.added")]
    Added,
    #[serde(rename = "secret.replaced")]
    Replaced,
    #[serde(rename = "secret.tested")]
    Tested,
    #[serde(rename = "secret.used")]
    Used,
    #[serde(rename = "secret.denied")]
    Denied,
    #[serde(rename = "secret.deleted")]
    Deleted,
    /// Pausa global de los agentes (ADR 0014 §2), actor `user`.
    #[serde(rename = "agents.paused")]
    AgentsPaused,
    #[serde(rename = "agents.resumed")]
    AgentsResumed,
    /// Concesiones por ejecución (ADR 0014 §1), actor `system`.
    #[serde(rename = "agent.grant_issued")]
    GrantIssued,
    #[serde(rename = "agent.grant_denied")]
    GrantDenied,
    #[serde(rename = "agent.grant_released")]
    GrantReleased,
    /// Eventos que el búfer del núcleo tuvo que descartar (`details.count`).
    #[serde(rename = "audit.dropped")]
    AuditDropped,
}

impl Action {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Added => "secret.added",
            Self::Replaced => "secret.replaced",
            Self::Tested => "secret.tested",
            Self::Used => "secret.used",
            Self::Denied => "secret.denied",
            Self::Deleted => "secret.deleted",
            Self::AgentsPaused => "agents.paused",
            Self::AgentsResumed => "agents.resumed",
            Self::GrantIssued => "agent.grant_issued",
            Self::GrantDenied => "agent.grant_denied",
            Self::GrantReleased => "agent.grant_released",
            Self::AuditDropped => "audit.dropped",
        }
    }

    /// Acciones que pueden llevar `details.count` (`CORE_ACTION_DETAIL_KEYS` del motor).
    pub fn takes_count(self) -> bool {
        matches!(
            self,
            Self::Denied | Self::GrantDenied | Self::GrantReleased | Self::AuditDropped
        )
    }
}

/// Resultado.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Ok,
    Denied,
    Error,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Denied => "denied",
            Self::Error => "error",
        }
    }
}

/// Claves que admite `details` (`DETAIL_KEYS` de `audit.py`, más `count`).
///
/// Las comunes, más `count`, que el motor solo acepta en las acciones agregables del
/// núcleo ([`Action::takes_count`]; `CORE_ACTION_DETAIL_KEYS`). Nunca las propias de una
/// acción del motor (`approval_id`, `decision`, `level`; `ACTION_DETAIL_KEYS`), que el
/// motor rechazaría en un evento del núcleo (condición 6 de la revisión de T4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DetailKey {
    SiteId,
    Operation,
    Provider,
    Op,
    Reason,
    ErrorCode,
    /// Tipo de agente de `agent.grant_*` (F1b).
    AgentKind,
    /// Repeticiones de un evento agregado (2 o más) o eventos descartados
    /// (`audit.dropped`, 1 o más). Solo en las acciones de [`Action::takes_count`].
    Count,
}

/// Un evento de auditoría. Solo datos no sensibles.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AuditEvent {
    event: &'static str,
    pub occurred_at: String,
    pub actor: Actor,
    pub action: Action,
    pub secret_ref: Option<String>,
    pub run_id: Option<String>,
    pub result: Outcome,
    pub details: BTreeMap<DetailKey, String>,
    /// Se agrega con los de su grupo.
    #[serde(skip)]
    aggregate: bool,
    /// Repeticiones agregadas (1 = solo este).
    #[serde(skip)]
    count: u64,
}

/// Hora actual en UTC con milisegundos (`2026-09-30T12:00:00.123Z`).
pub fn now_utc() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

/// Valor de `details` aceptable: 1–64 caracteres `[A-Za-z0-9._:/-]` (mismo filtro que el
/// motor). Un valor con otra forma no se añade.
fn is_detail_value(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'/' | b'-'))
}

impl AuditEvent {
    pub fn new(actor: Actor, action: Action, result: Outcome) -> Self {
        Self {
            event: "audit",
            occurred_at: now_utc(),
            actor,
            action,
            secret_ref: None,
            run_id: None,
            result,
            details: BTreeMap::new(),
            aggregate: false,
            count: 1,
        }
    }

    /// Marca el evento como agregable (ver el módulo). Solo surte efecto en los `denied`
    /// de las acciones que admiten `details.count`.
    #[must_use]
    pub fn aggregated(mut self) -> Self {
        self.aggregate = self.action.takes_count() && self.result == Outcome::Denied;
        self
    }

    /// ¿Está marcado como agregable?
    pub fn is_aggregated(&self) -> bool {
        self.aggregate
    }

    /// Evento sintético con los descartados por el búfer lleno (nunca se descarta).
    pub fn dropped(count: u64) -> Self {
        let count = count.clamp(1, MAX_COUNT);
        let mut event = Self::new(Actor::System, Action::AuditDropped, Outcome::Error)
            .detail(DetailKey::Reason, "buffer_full");
        event.count = count;
        event.details.insert(DetailKey::Count, count.to_string());
        event
    }

    /// Veces que representa este evento (1 si no se agregó).
    pub fn count(&self) -> u64 {
        self.count
    }

    /// ¿Se descarta antes que los demás si el búfer se llena?
    fn expendable(&self) -> bool {
        self.aggregate || self.result == Outcome::Denied
    }

    /// ¿Se puede sumar `other` a este evento? (los dos agregables y del mismo grupo).
    fn absorbs(&self, other: &AuditEvent) -> bool {
        self.aggregate && other.aggregate && AggKey::of(self) == AggKey::of(other)
    }

    /// Suma `other` (ya comprobado con [`absorbs`](Self::absorbs)): solo quedan la
    /// referencia, el `run_id` y los `details` iguales en los dos. El contador se queda
    /// en [`MAX_COUNT`] (18 dígitos, la forma que acepta el motor).
    fn absorb(&mut self, other: &AuditEvent) {
        self.count = self.count.saturating_add(other.count).min(MAX_COUNT);
        if self.secret_ref != other.secret_ref {
            self.secret_ref = None;
        }
        if self.run_id != other.run_id {
            self.run_id = None;
        }
        self.details
            .retain(|key, value| *key == DetailKey::Count || other.details.get(key) == Some(value));
        self.details
            .insert(DetailKey::Count, self.count.to_string());
    }

    /// Solo se guarda si cumple la gramática del llavero.
    #[must_use]
    pub fn secret_ref(mut self, secret_ref: &str) -> Self {
        self.secret_ref = parse_ref(secret_ref).map(|_| secret_ref.to_owned());
        self
    }

    /// Solo se guarda si es un UUID canónico.
    #[must_use]
    pub fn run_id(mut self, run_id: &str) -> Self {
        self.run_id = is_canonical_uuid(run_id).then(|| run_id.to_owned());
        self
    }

    /// Añade un dato si su valor tiene la forma permitida.
    #[must_use]
    pub fn detail(mut self, key: DetailKey, value: &str) -> Self {
        if is_detail_value(value) {
            self.details.insert(key, value.to_owned());
        }
        self
    }

    fn get(&self, key: DetailKey) -> Option<&str> {
        self.details.get(&key).map(String::as_str)
    }

    /// Línea JSON con `\n` para el stdin del motor.
    pub fn to_line(&self) -> Zeroizing<Vec<u8>> {
        let mut bytes = serde_json::to_vec(self).unwrap_or_default();
        bytes.push(b'\n');
        Zeroizing::new(bytes)
    }
}

/// Grupo de agregación: acción, actor, resultado, `reason` y `error_code`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct AggKey {
    action: Action,
    actor: Actor,
    result: Outcome,
    reason: Option<String>,
    error_code: Option<String>,
}

impl AggKey {
    fn of(event: &AuditEvent) -> Self {
        Self {
            action: event.action,
            actor: event.actor,
            result: event.result,
            reason: event.get(DetailKey::Reason).map(str::to_owned),
            error_code: event.get(DetailKey::ErrorCode).map(str::to_owned),
        }
    }
}

/// Cubo de fichas y agregado pendiente de un grupo.
#[derive(Debug)]
struct Bucket {
    tokens: u32,
    refilled: Instant,
    pending: Option<AuditEvent>,
    /// Rechazos del grupo desde que arrancó la app (para el log).
    total: u64,
}

impl Bucket {
    fn refill(&mut self, now: Instant, window: Duration) {
        let windows = now.duration_since(self.refilled).as_millis() / window.as_millis().max(1);
        if windows > 0 {
            let add = u32::try_from(windows).unwrap_or(u32::MAX);
            self.tokens = self.tokens.saturating_add(add).min(AGGREGATE_BURST);
            let step = window.saturating_mul(add);
            self.refilled = self.refilled.checked_add(step).unwrap_or(now);
        }
    }
}

/// Qué hacer con un rechazo agregable.
#[derive(Debug)]
enum Admit {
    /// Se registra y audita ya (con el total del grupo).
    Now(AuditEvent, u64),
    /// Se sumó al pendiente del grupo.
    Held,
}

/// Agregación por grupo (ver el módulo).
#[derive(Debug)]
struct Aggregator {
    window: Duration,
    buckets: Mutex<HashMap<AggKey, Bucket>>,
}

impl Aggregator {
    fn new(window: Duration) -> Self {
        Self {
            window,
            buckets: Mutex::new(HashMap::new()),
        }
    }

    fn lock(&self) -> MutexGuard<'_, HashMap<AggKey, Bucket>> {
        match self.buckets.lock() {
            Ok(buckets) => buckets,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    fn admit(&self, event: AuditEvent, now: Instant) -> Admit {
        let key = AggKey::of(&event);
        let mut buckets = self.lock();
        if !buckets.contains_key(&key) && buckets.len() >= MAX_AGGREGATE_KEYS {
            // Inalcanzable con los motivos actuales: se trata como el primero.
            return Admit::Now(event, 1);
        }
        let bucket = buckets.entry(key).or_insert(Bucket {
            tokens: AGGREGATE_BURST,
            refilled: now,
            pending: None,
            total: 0,
        });
        bucket.refill(now, self.window);
        bucket.total = bucket.total.saturating_add(event.count);
        if bucket.tokens > 0 && bucket.pending.is_none() {
            bucket.tokens -= 1;
            return Admit::Now(event, bucket.total);
        }
        match bucket.pending.as_mut() {
            Some(pending) => pending.absorb(&event),
            None => bucket.pending = Some(event),
        }
        Admit::Held
    }

    /// Saca los agregados pendientes (con el total de su grupo) y olvida los grupos sin
    /// nada pendiente y con el cubo lleno.
    fn drain(&self, now: Instant) -> Vec<(AuditEvent, u64)> {
        let mut buckets = self.lock();
        let mut out = Vec::new();
        buckets.retain(|_, bucket| {
            bucket.refill(now, self.window);
            match bucket.pending.take() {
                Some(pending) => {
                    out.push((pending, bucket.total));
                    true
                }
                None => bucket.tokens < AGGREGATE_BURST,
            }
        });
        out
    }
}

/// Log de un rechazo agregable (uno suelto o un agregado), sin ningún valor.
fn log_denial(event: &AuditEvent, total: u64) {
    let message = match event.action {
        Action::Denied => "solicitud de secreto rechazada",
        Action::GrantDenied => "concesión de agente denegada",
        _ => "liberación de concesión de agente rechazada",
    };
    tracing::warn!(
        action = event.action.as_str(),
        reason = event.get(DetailKey::Reason),
        error_code = event.get(DetailKey::ErrorCode),
        op = event.get(DetailKey::Op),
        secret_ref = event.secret_ref.as_deref(),
        operation = event.get(DetailKey::Operation),
        agent_kind = event.get(DetailKey::AgentKind),
        run_id = event.run_id.as_deref(),
        count = event.count,
        total,
        "{message}"
    );
}

/// Cambios de estado del motor (solo los manda el supervisor) y sincronía de pruebas.
enum AuditMsg {
    Attach(StdinWriter),
    LogOnly,
    Detach,
    #[cfg(test)]
    Sync(oneshot::Sender<usize>),
}

enum Mode {
    /// Motor no listo o base no disponible: se guardan.
    Buffer,
    /// Motor listo con base disponible: se envían por stdin.
    Engine(StdinWriter),
    /// Modo de desarrollo externo: solo al log (ADR 0010 §5).
    LogOnly,
}

/// Búfer acotado compartido entre `record` y la tarea que envía.
struct Shared {
    buffer: Mutex<VecDeque<AuditEvent>>,
    /// Hay eventos nuevos.
    notify: Notify,
    dropped: AtomicU64,
    /// Descartados desde el último `audit.dropped` enviado.
    dropped_unreported: AtomicU64,
    dropped_log: LogSampler,
    aggregator: Aggregator,
    /// Hay un motor enganchado (lo fijan `attach`, `detach` y `log_only` al momento).
    /// Mientras lo hay, los rechazos que se auditan al momento no se suman en el búfer
    /// (cada uno llega con su detalle); sin motor sí, para que el búfer no se llene.
    attached: AtomicBool,
}

impl Shared {
    fn lock(&self) -> MutexGuard<'_, VecDeque<AuditEvent>> {
        match self.buffer.lock() {
            Ok(buffer) => buffer,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    /// Agrega (si `merge`) o guarda al final; si no cabe, hace sitio por prioridad.
    fn push_back(&self, event: AuditEvent, merge: bool) {
        let mut buffer = self.lock();
        if merge && event.aggregate {
            if let Some(existing) = buffer.iter_mut().rev().find(|e| e.absorbs(&event)) {
                existing.absorb(&event);
                return;
            }
        }
        if buffer.len() >= AUDIT_BUFFER {
            let fits = make_room(&mut buffer, &event);
            if fits {
                buffer.push_back(event);
            }
            drop(buffer);
            self.count_drop();
            return;
        }
        buffer.push_back(event);
    }

    /// Devuelve al principio un evento que no se pudo enviar (si no cabe, por prioridad).
    fn push_front(&self, event: AuditEvent) {
        let mut buffer = self.lock();
        if buffer.len() >= AUDIT_BUFFER {
            let fits = make_room(&mut buffer, &event);
            if fits {
                buffer.push_front(event);
            }
            drop(buffer);
            self.count_drop();
            return;
        }
        buffer.push_front(event);
    }

    fn pop_front(&self) -> Option<AuditEvent> {
        self.lock().pop_front()
    }

    fn count_drop(&self) {
        let total = self.dropped.fetch_add(1, Ordering::Relaxed) + 1;
        self.dropped_unreported.fetch_add(1, Ordering::Relaxed);
        if self.dropped_log.hit() {
            tracing::warn!(
                limit = AUDIT_BUFFER,
                descartados = total,
                "búfer de auditoría lleno: se descarta un evento (primero los rechazos)"
            );
        }
    }

    /// Vuelca al búfer los agregados pendientes (con su línea de log).
    fn drain_aggregates(&self) {
        for (event, total) in self.aggregator.drain(Instant::now()) {
            log_denial(&event, total);
            let merge = !self.attached.load(Ordering::Relaxed);
            self.push_back(event, merge);
        }
    }
}

/// Hace sitio en un búfer lleno para `incoming`: quita el primer evento prescindible y,
/// si no hay, el más viejo, salvo que `incoming` sea prescindible (entonces no cabe).
fn make_room(buffer: &mut VecDeque<AuditEvent>, incoming: &AuditEvent) -> bool {
    if let Some(position) = buffer.iter().position(AuditEvent::expendable) {
        buffer.remove(position);
        return true;
    }
    if incoming.expendable() {
        return false;
    }
    buffer.pop_front();
    true
}

/// Cola de auditoría del núcleo. Clonable; `record` nunca bloquea.
#[derive(Clone)]
pub struct AuditQueue {
    shared: Arc<Shared>,
    tx: mpsc::UnboundedSender<AuditMsg>,
}

impl std::fmt::Debug for AuditQueue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AuditQueue")
            .field("dropped", &self.dropped())
            .finish_non_exhaustive()
    }
}

impl AuditQueue {
    /// Crea la cola y su tarea en `runtime`.
    pub fn spawn(runtime: &tokio::runtime::Handle) -> Self {
        Self::spawn_with_window(runtime, AGGREGATE_WINDOW)
    }

    /// Como [`spawn`](Self::spawn), con otra ventana de agregación (pruebas).
    pub fn spawn_with_window(runtime: &tokio::runtime::Handle, window: Duration) -> Self {
        let (tx, rx) = mpsc::unbounded_channel();
        let shared = Arc::new(Shared {
            buffer: Mutex::new(VecDeque::new()),
            notify: Notify::new(),
            dropped: AtomicU64::new(0),
            dropped_unreported: AtomicU64::new(0),
            dropped_log: LogSampler::new(),
            aggregator: Aggregator::new(window),
            attached: AtomicBool::new(false),
        });
        runtime.spawn(forward(Arc::clone(&shared), rx, window));
        Self { shared, tx }
    }

    /// Registra un evento (se envía o se guarda según el estado del motor). Nunca
    /// bloquea: si el búfer está lleno, se hace sitio por prioridad. Un rechazo agregable
    /// se registra en el log aquí mismo o se suma a su grupo (ver el módulo).
    pub fn record(&self, event: AuditEvent) {
        let event = if event.aggregate {
            match self.shared.aggregator.admit(event, Instant::now()) {
                Admit::Now(event, total) => {
                    log_denial(&event, total);
                    event
                }
                Admit::Held => return,
            }
        } else {
            event
        };
        let merge = !self.shared.attached.load(Ordering::Relaxed);
        self.shared.push_back(event, merge);
        self.shared.notify.notify_one();
    }

    /// Eventos descartados por el búfer lleno desde que se creó la cola.
    pub fn dropped(&self) -> u64 {
        self.shared.dropped.load(Ordering::Relaxed)
    }

    /// El motor está listo y su base disponible: se envía lo guardado (con los agregados
    /// pendientes) y lo siguiente.
    pub(crate) fn attach(&self, writer: StdinWriter) {
        self.shared.attached.store(true, Ordering::Relaxed);
        let _ = self.tx.send(AuditMsg::Attach(writer));
    }

    /// Modo externo: los eventos solo se registran en el log.
    pub fn log_only(&self) {
        self.shared.attached.store(false, Ordering::Relaxed);
        let _ = self.tx.send(AuditMsg::LogOnly);
    }

    /// El motor dejó de estar listo (o su base no está disponible): se vuelve a guardar.
    pub fn detach(&self) {
        self.shared.attached.store(false, Ordering::Relaxed);
        let _ = self.tx.send(AuditMsg::Detach);
    }

    /// Vuelca los agregados pendientes y espera a que la tarea procese todo lo anterior.
    /// Devuelve cuántos eventos guarda.
    #[cfg(test)]
    pub async fn sync(&self) -> usize {
        let (tx, rx) = oneshot::channel();
        let _ = self.tx.send(AuditMsg::Sync(tx));
        rx.await.unwrap_or(usize::MAX)
    }

    /// Eventos en el búfer ahora mismo, sin esperar a la tarea (pruebas).
    #[cfg(test)]
    pub fn buffered(&self) -> usize {
        self.shared.lock().len()
    }

    /// Eventos en el búfer ahora mismo (pruebas): copia para inspeccionarlos.
    #[cfg(test)]
    pub fn snapshot(&self) -> Vec<AuditEvent> {
        self.shared.lock().iter().cloned().collect()
    }
}

fn log_event(event: &AuditEvent) {
    tracing::info!(
        action = event.action.as_str(),
        result = event.result.as_str(),
        secret_ref = event.secret_ref.as_deref(),
        run_id = event.run_id.as_deref(),
        count = event.count,
        "auditoría (sin motor gestionado)"
    );
}

/// Envía (o registra) lo que haya en el búfer según el modo; antes, `audit.dropped` si
/// hubo descartes.
async fn flush(shared: &Shared, mode: &mut Mode) {
    match mode {
        Mode::Buffer => {}
        Mode::LogOnly => {
            let unreported = shared.dropped_unreported.swap(0, Ordering::Relaxed);
            if unreported > 0 {
                log_event(&AuditEvent::dropped(unreported));
            }
            let drained: Vec<AuditEvent> = shared.lock().drain(..).collect();
            // Los agregables ya quedaron en el log al registrarse o al volcarse.
            for event in drained.iter().filter(|e| !e.aggregate) {
                log_event(event);
            }
        }
        Mode::Engine(writer) => {
            let unreported = shared.dropped_unreported.swap(0, Ordering::Relaxed);
            if unreported > 0 && !writer.send(AuditEvent::dropped(unreported).to_line()).await {
                shared
                    .dropped_unreported
                    .fetch_add(unreported, Ordering::Relaxed);
                tracing::warn!("no se pudo enviar la auditoría al motor; se guarda");
                *mode = Mode::Buffer;
                return;
            }
            // En orden; ante un fallo (tubería cerrada o motor atascado, con plazo en
            // `StdinWriter::send`) el evento vuelve al búfer hasta el siguiente motor.
            while let Some(event) = shared.pop_front() {
                if !writer.send(event.to_line()).await {
                    shared.push_front(event);
                    tracing::warn!("no se pudo enviar la auditoría al motor; se guarda");
                    *mode = Mode::Buffer;
                    break;
                }
            }
        }
    }
}

async fn forward(shared: Arc<Shared>, mut rx: mpsc::UnboundedReceiver<AuditMsg>, window: Duration) {
    let mut mode = Mode::Buffer;
    let mut ticker = tokio::time::interval_at(tokio::time::Instant::now() + window, window);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            biased;
            msg = rx.recv() => match msg {
                None => break,
                Some(AuditMsg::Attach(writer)) => {
                    shared.drain_aggregates();
                    mode = Mode::Engine(writer);
                }
                Some(AuditMsg::LogOnly) => mode = Mode::LogOnly,
                Some(AuditMsg::Detach) => mode = Mode::Buffer,
                #[cfg(test)]
                Some(AuditMsg::Sync(reply)) => {
                    shared.drain_aggregates();
                    flush(&shared, &mut mode).await;
                    let _ = reply.send(shared.lock().len());
                    continue;
                }
            },
            _ = ticker.tick() => shared.drain_aggregates(),
            () = shared.notify.notified() => {}
        }
        flush(&shared, &mut mode).await;
    }
}

#[cfg(test)]
#[path = "audit_tests.rs"]
mod tests;
