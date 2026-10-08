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
//! con su base disponible. `record` nunca bloquea ni hace crecer la memoria: si el búfer
//! está lleno (motor no listo, o un motor que no lee su stdin), descarta el evento más
//! viejo, lo cuenta ([`AuditQueue::dropped`]) y lo registra muestreado (el primero y uno
//! de cada 100). En modo de desarrollo externo (sin stdin) los eventos solo van al log.
//!
//! Los eventos marcados con [`AuditEvent::aggregated`] (solicitudes malformadas y
//! liberaciones de concesiones inexistentes, que un motor puede repetir sin límite) se
//! **agregan** mientras esperan en el búfer: uno igual (misma acción, actor, resultado,
//! referencia y `details`) suma 1 a `details.count` del que ya estaba en lugar de ocupar
//! otro sitio. Si los `run_id` difieren, el agregado queda sin `run_id`.
//!
//! El canal de control (motor listo, detenido, modo externo) no tiene límite: solo lo usa
//! el supervisor en cada cambio de estado del motor, nunca una línea del motor.
//!
//! Nunca llevan el valor de un secreto ni `last4`: solo acción, resultado, referencia,
//! `run_id` y `details` con claves y valores cortos y validados.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

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

/// Quién hizo la acción.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Actor {
    User,
    System,
    Agent,
}

/// Acciones del núcleo (lista cerrada del motor: `CORE_ACTIONS` de `audit.py`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
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
        }
    }
}

/// Resultado.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
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

/// Claves que admite `details` (`DETAIL_KEYS` de `audit.py`).
///
/// Solo las comunes: nunca las propias de una acción del motor (`approval_id`, `decision`,
/// `level`; `ACTION_DETAIL_KEYS`), que el motor rechazaría en un evento del núcleo
/// (condición 6 de la revisión de T4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
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
    /// Veces que se repitió un evento agregado (2 o más; revisión de seguridad de T5).
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
    pub details: std::collections::BTreeMap<DetailKey, String>,
    /// Se agrega con los iguales mientras espera en el búfer.
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
            details: std::collections::BTreeMap::new(),
            aggregate: false,
            count: 1,
        }
    }

    /// Marca el evento como agregable (ver el módulo).
    #[must_use]
    pub fn aggregated(mut self) -> Self {
        self.aggregate = true;
        self
    }

    /// Veces que representa este evento (1 si no se agregó).
    pub fn count(&self) -> u64 {
        self.count
    }

    /// ¿Se puede sumar `other` a este evento?
    fn absorbs(&self, other: &AuditEvent) -> bool {
        self.aggregate
            && other.aggregate
            && self.actor == other.actor
            && self.action == other.action
            && self.result == other.result
            && self.secret_ref == other.secret_ref
            && self
                .details
                .iter()
                .filter(|(k, _)| **k != DetailKey::Count)
                .eq(other
                    .details
                    .iter()
                    .filter(|(k, _)| **k != DetailKey::Count))
    }

    /// Suma `other` (ya comprobado con [`absorbs`](Self::absorbs)). El contador se queda
    /// en [`MAX_COUNT`] (18 dígitos, la forma que acepta el motor).
    fn absorb(&mut self, other: &AuditEvent) {
        self.count = self.count.saturating_add(other.count).min(MAX_COUNT);
        if self.run_id != other.run_id {
            self.run_id = None;
        }
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

    /// Línea JSON con `\n` para el stdin del motor.
    pub fn to_line(&self) -> Zeroizing<Vec<u8>> {
        let mut bytes = serde_json::to_vec(self).unwrap_or_default();
        bytes.push(b'\n');
        Zeroizing::new(bytes)
    }
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
    dropped_log: LogSampler,
}

impl Shared {
    fn lock(&self) -> MutexGuard<'_, VecDeque<AuditEvent>> {
        match self.buffer.lock() {
            Ok(buffer) => buffer,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    /// Agrega o guarda al final; si no cabe, descarta el más viejo.
    fn push_back(&self, event: AuditEvent) {
        let mut buffer = self.lock();
        if event.aggregate {
            if let Some(existing) = buffer.iter_mut().rev().find(|e| e.absorbs(&event)) {
                existing.absorb(&event);
                return;
            }
        }
        if buffer.len() >= AUDIT_BUFFER {
            buffer.pop_front();
            drop(buffer);
            self.count_drop();
            buffer = self.lock();
        }
        buffer.push_back(event);
    }

    /// Devuelve al principio un evento que no se pudo enviar (si no cabe, se descarta).
    fn push_front(&self, event: AuditEvent) {
        let mut buffer = self.lock();
        if buffer.len() >= AUDIT_BUFFER {
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
        if self.dropped_log.hit() {
            tracing::warn!(
                limit = AUDIT_BUFFER,
                descartados = total,
                "búfer de auditoría lleno: se descarta el evento más viejo"
            );
        }
    }
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
        let (tx, rx) = mpsc::unbounded_channel();
        let shared = Arc::new(Shared {
            buffer: Mutex::new(VecDeque::new()),
            notify: Notify::new(),
            dropped: AtomicU64::new(0),
            dropped_log: LogSampler::new(),
        });
        runtime.spawn(forward(Arc::clone(&shared), rx));
        Self { shared, tx }
    }

    /// Registra un evento (se envía o se guarda según el estado del motor). Nunca
    /// bloquea: si el búfer está lleno, se descarta el más viejo.
    pub fn record(&self, event: AuditEvent) {
        self.shared.push_back(event);
        self.shared.notify.notify_one();
    }

    /// Eventos descartados por el búfer lleno desde que se creó la cola.
    pub fn dropped(&self) -> u64 {
        self.shared.dropped.load(Ordering::Relaxed)
    }

    /// El motor está listo y su base disponible: se envía lo guardado y lo siguiente.
    pub(crate) fn attach(&self, writer: StdinWriter) {
        let _ = self.tx.send(AuditMsg::Attach(writer));
    }

    /// Modo externo: los eventos solo se registran en el log.
    pub fn log_only(&self) {
        let _ = self.tx.send(AuditMsg::LogOnly);
    }

    /// El motor dejó de estar listo (o su base no está disponible): se vuelve a guardar.
    pub fn detach(&self) {
        let _ = self.tx.send(AuditMsg::Detach);
    }

    /// Espera a que la tarea procese todo lo anterior. Devuelve cuántos eventos guarda.
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

/// Envía (o registra) lo que haya en el búfer según el modo.
async fn flush(shared: &Shared, mode: &mut Mode) {
    match mode {
        Mode::Buffer => {}
        Mode::LogOnly => {
            let drained: Vec<AuditEvent> = shared.lock().drain(..).collect();
            for event in &drained {
                log_event(event);
            }
        }
        Mode::Engine(writer) => {
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

async fn forward(shared: Arc<Shared>, mut rx: mpsc::UnboundedReceiver<AuditMsg>) {
    let mut mode = Mode::Buffer;
    loop {
        tokio::select! {
            biased;
            msg = rx.recv() => match msg {
                None => break,
                Some(AuditMsg::Attach(writer)) => mode = Mode::Engine(writer),
                Some(AuditMsg::LogOnly) => mode = Mode::LogOnly,
                Some(AuditMsg::Detach) => mode = Mode::Buffer,
                #[cfg(test)]
                Some(AuditMsg::Sync(reply)) => {
                    flush(&shared, &mut mode).await;
                    let _ = reply.send(shared.lock().len());
                    continue;
                }
            },
            () = shared.notify.notified() => {}
        }
        flush(&shared, &mut mode).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    use tokio::io::{AsyncBufReadExt, BufReader};

    const RUN: &str = "0192f0a0-0000-7abc-8def-000000000002";

    fn event(n: usize) -> AuditEvent {
        AuditEvent::new(Actor::User, Action::Tested, Outcome::Ok)
            .secret_ref("llm/openai/default")
            .detail(DetailKey::Reason, &format!("n{n}"))
    }

    #[test]
    fn serializa_con_la_forma_del_motor() {
        let e = AuditEvent::new(Actor::System, Action::Denied, Outcome::Denied)
            .secret_ref("wp/0192f0a0-1234-7abc-8def-0123456789ab/token")
            .run_id(RUN)
            .detail(DetailKey::Op, "get")
            .detail(DetailKey::ErrorCode, "vault.secret_not_allowed")
            .detail(DetailKey::SiteId, "0192f0a0-1234-7abc-8def-0123456789ab")
            .detail(DetailKey::Operation, "checkSiteConnection");
        let line = e.to_line();
        assert_eq!(line.last(), Some(&b'\n'));
        let v: Value = serde_json::from_slice(&line).unwrap();
        let occurred = v["occurred_at"].as_str().unwrap().to_owned();
        assert!(
            occurred.ends_with('Z') && occurred.len() == 24,
            "{occurred}"
        );
        assert_eq!(
            v,
            json!({
                "event": "audit",
                "occurred_at": occurred,
                "actor": "system",
                "action": "secret.denied",
                "secret_ref": "wp/0192f0a0-1234-7abc-8def-0123456789ab/token",
                "run_id": RUN,
                "result": "denied",
                "details": {
                    "error_code": "vault.secret_not_allowed",
                    "op": "get",
                    "operation": "checkSiteConnection",
                    "site_id": "0192f0a0-1234-7abc-8def-0123456789ab"
                }
            })
        );
    }

    #[test]
    fn campos_invalidos_no_se_guardan() {
        let e = AuditEvent::new(Actor::Agent, Action::Used, Outcome::Error)
            .secret_ref("db/../key")
            .run_id("no-es-uuid")
            .detail(DetailKey::Reason, "con espacio")
            .detail(DetailKey::Reason, "")
            .detail(DetailKey::Provider, &"a".repeat(65));
        assert_eq!(e.secret_ref, None);
        assert_eq!(e.run_id, None);
        assert!(e.details.is_empty());
        let v: Value = serde_json::from_slice(&e.to_line()).unwrap();
        assert_eq!(v["secret_ref"], Value::Null);
        assert_eq!(v["actor"], "agent");
        for (action, text) in [
            (Action::Added, "secret.added"),
            (Action::Replaced, "secret.replaced"),
            (Action::Tested, "secret.tested"),
            (Action::Used, "secret.used"),
            (Action::Denied, "secret.denied"),
            (Action::Deleted, "secret.deleted"),
            (Action::AgentsPaused, "agents.paused"),
            (Action::AgentsResumed, "agents.resumed"),
            (Action::GrantIssued, "agent.grant_issued"),
            (Action::GrantDenied, "agent.grant_denied"),
            (Action::GrantReleased, "agent.grant_released"),
        ] {
            assert_eq!(action.as_str(), text);
            assert_eq!(serde_json::to_value(action).unwrap(), json!(text));
        }
        for (outcome, text) in [
            (Outcome::Ok, "ok"),
            (Outcome::Denied, "denied"),
            (Outcome::Error, "error"),
        ] {
            assert_eq!(outcome.as_str(), text);
        }
    }

    async fn read_lines(reader: &mut BufReader<tokio::io::DuplexStream>, n: usize) -> Vec<Value> {
        let mut out = Vec::new();
        for _ in 0..n {
            let mut line = String::new();
            reader.read_line(&mut line).await.unwrap();
            out.push(serde_json::from_str(&line).unwrap());
        }
        out
    }

    #[test]
    fn las_claves_de_details_son_solo_las_comunes_del_motor() {
        // `DETAIL_KEYS` de `core/audit.py`; nunca `approval_id`, `decision` ni `level`.
        let keys = [
            DetailKey::SiteId,
            DetailKey::Operation,
            DetailKey::Provider,
            DetailKey::Op,
            DetailKey::Reason,
            DetailKey::ErrorCode,
            DetailKey::AgentKind,
            DetailKey::Count,
        ]
        .map(|k| serde_json::to_value(k).unwrap());
        assert_eq!(
            keys.to_vec(),
            [
                "site_id",
                "operation",
                "provider",
                "op",
                "reason",
                "error_code",
                "agent_kind",
                "count"
            ]
            .map(Value::from)
            .to_vec()
        );
        let source = include_str!("audit.rs");
        let enum_body = source
            .split("pub enum DetailKey {")
            .nth(1)
            .and_then(|rest| rest.split('}').next())
            .unwrap();
        for forbidden in ["ApprovalId", "Decision", "Level"] {
            assert!(!enum_body.contains(forbidden), "{forbidden}");
        }
    }

    #[tokio::test]
    async fn guarda_mientras_no_hay_motor_y_envia_en_orden_al_quedar_listo() {
        let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
        for n in 0..3 {
            queue.record(event(n));
        }
        assert_eq!(queue.sync().await, 3);
        let (core, engine) = tokio::io::duplex(64 * 1024);
        queue.attach(StdinWriter::spawn(Box::new(core)));
        assert_eq!(queue.sync().await, 0);
        let mut reader = BufReader::new(engine);
        let lines = read_lines(&mut reader, 3).await;
        let reasons: Vec<&str> = lines
            .iter()
            .map(|l| l["details"]["reason"].as_str().unwrap())
            .collect();
        assert_eq!(reasons, ["n0", "n1", "n2"]);
        // Con el motor listo se envía al momento.
        queue.record(event(3));
        assert_eq!(queue.sync().await, 0);
        assert_eq!(
            read_lines(&mut reader, 1).await[0]["details"]["reason"],
            "n3"
        );
        // Tras `detach` se vuelve a guardar.
        queue.detach();
        queue.record(event(4));
        assert_eq!(queue.sync().await, 1);
    }

    #[tokio::test]
    async fn con_mas_de_500_descarta_los_mas_viejos() {
        let (logs, _guard) = crate::test_logs::capture();
        let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
        for n in 0..AUDIT_BUFFER + 2 {
            queue.record(event(n));
        }
        assert_eq!(queue.sync().await, AUDIT_BUFFER);
        let (core, engine) = tokio::io::duplex(1024 * 1024);
        queue.attach(StdinWriter::spawn(Box::new(core)));
        assert_eq!(queue.sync().await, 0);
        let mut reader = BufReader::new(engine);
        let lines = read_lines(&mut reader, AUDIT_BUFFER).await;
        assert_eq!(lines[0]["details"]["reason"], "n2");
        assert_eq!(
            lines[AUDIT_BUFFER - 1]["details"]["reason"],
            format!("n{}", AUDIT_BUFFER + 1)
        );
        let _ = logs;
    }

    #[tokio::test]
    async fn escritura_fallida_vuelve_a_guardar() {
        let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
        let (core, engine) = tokio::io::duplex(64);
        drop(engine);
        queue.attach(StdinWriter::spawn(Box::new(core)));
        queue.record(event(0));
        assert_eq!(queue.sync().await, 1, "el evento no se pierde");
        queue.record(event(1));
        assert_eq!(queue.sync().await, 2);
    }

    fn malformed(run_id: Option<&str>) -> AuditEvent {
        let mut event = AuditEvent::new(Actor::System, Action::GrantReleased, Outcome::Denied)
            .detail(DetailKey::Reason, "not_active")
            .aggregated();
        if let Some(run_id) = run_id {
            event = event.run_id(run_id);
        }
        event
    }

    #[tokio::test]
    async fn los_repetidos_agregables_se_suman_en_un_evento() {
        let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
        let other = "0192f0a0-0000-7abc-8def-000000000003";
        queue.record(malformed(Some(RUN)));
        queue.record(event(0));
        queue.record(malformed(Some(RUN)));
        assert_eq!(queue.sync().await, 2);
        assert_eq!(
            queue.snapshot()[0].run_id.as_deref(),
            Some(RUN),
            "mismo run_id"
        );
        queue.record(malformed(Some(other)));
        queue.record(malformed(None));
        // Uno distinto (otro motivo) no se suma; uno sin marcar tampoco.
        queue.record(
            AuditEvent::new(Actor::System, Action::GrantReleased, Outcome::Denied)
                .detail(DetailKey::Reason, "malformed")
                .aggregated(),
        );
        queue.record(event(0));
        assert_eq!(queue.sync().await, 4);
        let events = queue.snapshot();
        assert_eq!(events[0].count(), 4);
        assert_eq!(
            events[0].run_id, None,
            "run_id distintos: el agregado no lleva"
        );
        assert_eq!(events[1].count(), 1);
        assert_eq!(events[2].count(), 1);
        assert_eq!(events[3].count(), 1);

        let (core, engine) = tokio::io::duplex(64 * 1024);
        queue.attach(StdinWriter::spawn(Box::new(core)));
        assert_eq!(queue.sync().await, 0);
        let lines = read_lines(&mut BufReader::new(engine), 4).await;
        assert_eq!(
            lines[0]["details"],
            json!({"reason": "not_active", "count": "4"})
        );
        assert_eq!(lines[0]["run_id"], Value::Null);
        assert_eq!(lines[0]["result"], "denied");
        assert_eq!(lines[1]["details"]["reason"], "n0");
        assert_eq!(lines[2]["details"], json!({"reason": "malformed"}));
    }

    #[test]
    fn el_contador_agregado_no_pasa_del_maximo() {
        let mut first = malformed(None);
        first.count = MAX_COUNT - 1;
        first.absorb(&malformed(None));
        first.absorb(&malformed(None));
        assert_eq!(first.count(), MAX_COUNT);
        assert_eq!(first.details[&DetailKey::Count].len(), 18);
    }

    /// Un motor que no lee stdin: `record` no bloquea y la memoria queda acotada al búfer.
    #[tokio::test]
    async fn motor_que_no_lee_stdin_no_hace_crecer_la_cola() {
        let (logs, _guard) = crate::test_logs::capture();
        let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
        // Tubería de 256 bytes que nadie lee; plazo de atasco largo para que la tarea
        // quede esperando mientras llegan eventos.
        let (core, _engine) = tokio::io::duplex(256);
        queue.attach(StdinWriter::spawn_with_stall(
            Box::new(core),
            std::time::Duration::from_secs(60),
        ));
        let started = std::time::Instant::now();
        for n in 0..20_000 {
            queue.record(event(n));
            queue.record(malformed(Some(RUN)));
            if n % 1000 == 0 {
                tokio::task::yield_now().await;
                assert!(queue.buffered() <= AUDIT_BUFFER);
            }
        }
        assert!(
            started.elapsed() < std::time::Duration::from_secs(10),
            "record no bloquea"
        );
        assert!(queue.buffered() <= AUDIT_BUFFER, "{}", queue.buffered());
        assert!(queue.dropped() > 19_000, "{}", queue.dropped());
        // Muestreado: el aviso no se repite por cada descarte.
        let warnings = logs.text().matches("búfer de auditoría lleno").count();
        assert!((1..=400).contains(&warnings), "{warnings}");
        assert!(format!("{queue:?}").contains("dropped"));
    }

    #[tokio::test]
    async fn el_escritor_de_stdin_avisa_del_atasco_y_no_espera_sin_plazo() {
        let (logs, _guard) = crate::test_logs::capture();
        let (core, _engine) = tokio::io::duplex(16);
        let writer =
            StdinWriter::spawn_with_stall(Box::new(core), std::time::Duration::from_millis(100));
        let line = || Zeroizing::new(vec![b'x'; 64]);
        // La primera no cabe: tras el plazo falla y el escritor queda atascado.
        let (first, second) = tokio::join!(writer.send(line()), writer.send(line()));
        assert!(!first && !second);
        tokio::time::timeout(std::time::Duration::from_secs(5), writer.stalled())
            .await
            .expect("no avisó del atasco");
        assert!(!writer.send(line()).await, "ya no acepta líneas");
        assert!(logs
            .text()
            .contains(crate::engine::supervisor::LOG_STDIN_STALLED));
        // Si la tubería se cierra (no atasco), `stalled` no vuelve.
        let (core, engine) = tokio::io::duplex(16);
        drop(engine);
        let closed = StdinWriter::spawn(Box::new(core));
        assert!(!closed.send(line()).await);
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(100), closed.stalled())
                .await
                .is_err()
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn modo_externo_solo_registra_en_el_log() {
        let (logs, _guard) = crate::test_logs::capture();
        let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
        queue.record(event(0));
        queue.log_only();
        queue.record(event(1));
        assert_eq!(queue.sync().await, 0);
        let text = logs.text();
        assert_eq!(text.matches("auditoría (sin motor gestionado)").count(), 2);
        assert!(text.contains("secret.tested"));
    }
}
