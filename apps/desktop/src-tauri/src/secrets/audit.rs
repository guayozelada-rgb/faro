//! Eventos de auditoría del núcleo (ADR 0010 §4): Bóveda y canal de secretos.
//!
//! Se envían al motor por stdin, una línea por evento:
//!
//! ```text
//! {"event":"audit","occurred_at":"…Z","actor":"user|system|agent","action":"secret.…",
//!  "secret_ref":"…"|null,"run_id":"…"|null,"result":"ok|denied|error","details":{…}}
//! ```
//!
//! [`AuditQueue`] es una tarea con un búfer de [`AUDIT_BUFFER`] eventos: mientras el motor
//! no está listo o su base no está disponible, guarda los eventos (si se llena, descarta
//! los más viejos y lo registra); al quedar listo, los envía en orden. En modo de
//! desarrollo externo (sin stdin) los eventos solo van al log.
//!
//! Nunca llevan el valor de un secreto ni `last4`: solo acción, resultado, referencia,
//! `run_id` y `details` con claves y valores cortos y validados.

use std::collections::VecDeque;

use serde::Serialize;
use tokio::sync::mpsc;
#[cfg(test)]
use tokio::sync::oneshot;
use zeroize::Zeroizing;

use crate::engine::supervisor::StdinWriter;
use crate::secrets::refs::{is_canonical_uuid, parse_ref};

/// Eventos que se guardan como máximo mientras el motor no puede recibirlos.
pub const AUDIT_BUFFER: usize = 500;

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
        }
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

enum AuditMsg {
    Event(AuditEvent),
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

/// Cola de auditoría del núcleo. Clonable; `record` nunca bloquea.
#[derive(Clone)]
pub struct AuditQueue {
    tx: mpsc::UnboundedSender<AuditMsg>,
}

impl std::fmt::Debug for AuditQueue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AuditQueue").finish_non_exhaustive()
    }
}

impl AuditQueue {
    /// Crea la cola y su tarea en `runtime`.
    pub fn spawn(runtime: &tokio::runtime::Handle) -> Self {
        let (tx, rx) = mpsc::unbounded_channel();
        runtime.spawn(forward(rx));
        Self { tx }
    }

    /// Registra un evento (se envía o se guarda según el estado del motor).
    pub fn record(&self, event: AuditEvent) {
        if self.tx.send(AuditMsg::Event(event)).is_err() {
            tracing::warn!("la cola de auditoría no está disponible");
        }
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
}

fn push(buffer: &mut VecDeque<AuditEvent>, event: AuditEvent) {
    if buffer.len() >= AUDIT_BUFFER {
        buffer.pop_front();
        tracing::warn!(
            limit = AUDIT_BUFFER,
            "búfer de auditoría lleno: se descarta el evento más viejo"
        );
    }
    buffer.push_back(event);
}

fn log_event(event: &AuditEvent) {
    tracing::info!(
        action = event.action.as_str(),
        result = event.result.as_str(),
        secret_ref = event.secret_ref.as_deref(),
        run_id = event.run_id.as_deref(),
        "auditoría (sin motor gestionado)"
    );
}

async fn forward(mut rx: mpsc::UnboundedReceiver<AuditMsg>) {
    let mut buffer: VecDeque<AuditEvent> = VecDeque::new();
    let mut mode = Mode::Buffer;
    while let Some(msg) = rx.recv().await {
        match msg {
            AuditMsg::Event(event) => match &mode {
                Mode::LogOnly => log_event(&event),
                Mode::Buffer | Mode::Engine(_) => push(&mut buffer, event),
            },
            AuditMsg::Attach(writer) => mode = Mode::Engine(writer),
            AuditMsg::LogOnly => {
                for event in buffer.drain(..) {
                    log_event(&event);
                }
                mode = Mode::LogOnly;
            }
            AuditMsg::Detach => mode = Mode::Buffer,
            #[cfg(test)]
            AuditMsg::Sync(reply) => {
                let _ = reply.send(buffer.len());
                continue;
            }
        }
        // Envía en orden; ante un fallo de escritura, el evento se conserva y se vuelve
        // a guardar hasta el siguiente motor listo.
        if let Mode::Engine(writer) = &mode {
            while let Some(event) = buffer.front() {
                if writer.send(event.to_line()).await {
                    buffer.pop_front();
                } else {
                    tracing::warn!("no se pudo enviar la auditoría al motor; se guarda");
                    mode = Mode::Buffer;
                    break;
                }
            }
        }
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
                "agent_kind"
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
