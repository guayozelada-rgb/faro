//! Actividad de los agentes en vivo (ADR 0014 §3, spec F1b §4.5 y §5.3).
//!
//! El motor publica por stdout líneas `agent_activity` de esquema **cerrado**:
//!
//! ```text
//! {"event":"agent_activity","run_id":"<uuid>","seq":12,"occurred_at":"…Z",
//!  "kind":"run_status"|"step_started"|"step_finished"|"approval_requested",
//!  "agent":"site_summary","site_id":"<uuid>"|null,"status":"running","step":"read_site"|null,
//!  "step_cost_micros":0,"run_cost_micros":5678,"run_tokens":4321,"error_code":"llm.rate_limited"|null}
//! ```
//!
//! - Solo esas claves (`deny_unknown_fields`; una clave repetida también es un error).
//! - Identificadores (`agent`, `status`, `step`, `error_code`) con forma
//!   `^[a-z][a-z0-9_.]{0,47}$`; `kind` de la lista; UUID canónicos; enteros de 0 a 10^12;
//!   `occurred_at` RFC 3339 en UTC (`Z`); línea de 4 KB como mucho.
//! - **Nunca texto libre**: una línea que no cumpla se descarta con un aviso que dice qué
//!   campo falló, nunca su contenido.
//! - Cubo de fichas de [`RATE_PER_SECOND`] eventos por segundo: el exceso se descarta (la
//!   interfaz lo nota por el salto de `seq` y vuelve a pedir el estado).
//!
//! Las válidas se emiten a la ventana `main` como `engine://agents` con la misma carga sin
//! `event` ([`ActivitySink`]). La interfaz no puede emitir ese evento (sin
//! `core:event:allow-emit`; prueba en `tests/acl.rs`).

use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tokio::time::Instant;

use crate::secrets::refs::is_canonical_uuid;

/// Evento Tauri hacia la ventana `main`.
pub const ACTIVITY_EVENT: &str = "engine://agents";
/// Ventana destino.
pub const ACTIVITY_WINDOW: &str = "main";
/// Tamaño máximo de una línea `agent_activity`.
pub const MAX_ACTIVITY_LINE_BYTES: usize = 4096;
/// Eventos por segundo (y tamaño del cubo).
pub const RATE_PER_SECOND: u32 = 20;
/// Máximo de los contadores (`seq`, costos, tokens).
pub const MAX_COUNTER: u64 = 1_000_000_000_000;
/// Longitud máxima de un identificador.
const MAX_IDENTIFIER_LEN: usize = 48;

/// Tipo de evento (lista cerrada).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActivityKind {
    RunStatus,
    StepStarted,
    StepFinished,
    ApprovalRequested,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawActivity {
    event: String,
    run_id: String,
    seq: u64,
    occurred_at: String,
    kind: ActivityKind,
    agent: String,
    site_id: Option<String>,
    status: String,
    step: Option<String>,
    step_cost_micros: u64,
    run_cost_micros: u64,
    run_tokens: u64,
    error_code: Option<String>,
}

/// Carga de `engine://agents`: la línea validada, sin `event`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AgentActivity {
    pub run_id: String,
    pub seq: u64,
    pub occurred_at: String,
    pub kind: ActivityKind,
    pub agent: String,
    pub site_id: Option<String>,
    pub status: String,
    pub step: Option<String>,
    pub step_cost_micros: u64,
    pub run_cost_micros: u64,
    pub run_tokens: u64,
    pub error_code: Option<String>,
}

/// Por qué se descartó una línea (nombre del campo o de la regla, nunca contenido).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidActivity(pub &'static str);

/// `^[a-z][a-z0-9_.]{0,47}$`.
pub fn is_identifier(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=MAX_IDENTIFIER_LEN).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'_' | b'.'))
}

/// RFC 3339 en UTC con `Z` (`2026-10-08T12:00:00Z` o con fracción de 1 a 9 dígitos).
pub fn is_utc_timestamp(value: &str) -> bool {
    let Some(rest) = value.strip_suffix('Z') else {
        return false;
    };
    let shape_ok = rest.len() >= 19 && rest.is_ascii() && {
        let (base, fraction) = rest.split_at(19);
        let b = base.as_bytes();
        let digits = |range: std::ops::Range<usize>| b[range].iter().all(u8::is_ascii_digit);
        digits(0..4)
            && b[4] == b'-'
            && digits(5..7)
            && b[7] == b'-'
            && digits(8..10)
            && b[10] == b'T'
            && digits(11..13)
            && b[13] == b':'
            && digits(14..16)
            && b[16] == b':'
            && digits(17..19)
            && (fraction.is_empty()
                || fraction.strip_prefix('.').is_some_and(|f| {
                    (1..=9).contains(&f.len()) && f.bytes().all(|c| c.is_ascii_digit())
                }))
    };
    shape_ok && chrono::DateTime::parse_from_rfc3339(value).is_ok()
}

fn counter(value: u64, field: &'static str) -> Result<u64, InvalidActivity> {
    if value <= MAX_COUNTER {
        Ok(value)
    } else {
        Err(InvalidActivity(field))
    }
}

/// Valida una línea `agent_activity`.
pub fn parse_activity(line: &str) -> Result<AgentActivity, InvalidActivity> {
    if line.len() > MAX_ACTIVITY_LINE_BYTES {
        return Err(InvalidActivity("size"));
    }
    let raw: RawActivity =
        serde_json::from_str(line.trim()).map_err(|_| InvalidActivity("schema"))?;
    if raw.event != "agent_activity" {
        return Err(InvalidActivity("event"));
    }
    if !is_canonical_uuid(&raw.run_id) {
        return Err(InvalidActivity("run_id"));
    }
    if !is_utc_timestamp(&raw.occurred_at) {
        return Err(InvalidActivity("occurred_at"));
    }
    if !is_identifier(&raw.agent) {
        return Err(InvalidActivity("agent"));
    }
    if raw
        .site_id
        .as_deref()
        .is_some_and(|s| !is_canonical_uuid(s))
    {
        return Err(InvalidActivity("site_id"));
    }
    if !is_identifier(&raw.status) {
        return Err(InvalidActivity("status"));
    }
    if raw.step.as_deref().is_some_and(|s| !is_identifier(s)) {
        return Err(InvalidActivity("step"));
    }
    if raw.error_code.as_deref().is_some_and(|s| !is_identifier(s)) {
        return Err(InvalidActivity("error_code"));
    }
    Ok(AgentActivity {
        run_id: raw.run_id,
        seq: counter(raw.seq, "seq")?,
        occurred_at: raw.occurred_at,
        kind: raw.kind,
        agent: raw.agent,
        site_id: raw.site_id,
        status: raw.status,
        step: raw.step,
        step_cost_micros: counter(raw.step_cost_micros, "step_cost_micros")?,
        run_cost_micros: counter(raw.run_cost_micros, "run_cost_micros")?,
        run_tokens: counter(raw.run_tokens, "run_tokens")?,
        error_code: raw.error_code,
    })
}

/// Cubo de fichas: `capacity` fichas que se reponen a `capacity` por segundo.
#[derive(Debug)]
struct Bucket {
    capacity: f64,
    tokens: f64,
    last: Instant,
}

impl Bucket {
    fn new(capacity: u32, now: Instant) -> Self {
        Self {
            capacity: f64::from(capacity),
            tokens: f64::from(capacity),
            last: now,
        }
    }

    fn take(&mut self, now: Instant) -> bool {
        let elapsed = now.saturating_duration_since(self.last).as_secs_f64();
        self.last = now;
        self.tokens = (self.tokens + elapsed * self.capacity).min(self.capacity);
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

/// Qué pasó con una línea.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Relayed {
    Emitted,
    Invalid,
    RateLimited,
}

/// Destino de los eventos válidos (en la app: `emit_to("main", "engine://agents", …)`).
pub type ActivitySink = Arc<dyn Fn(&AgentActivity) + Send + Sync>;

/// Relevo de `agent_activity` hacia la interfaz.
pub struct ActivityRelay {
    sink: ActivitySink,
    bucket: Mutex<Bucket>,
    invalid: AtomicU64,
    rate_limited: AtomicU64,
}

impl fmt::Debug for ActivityRelay {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ActivityRelay")
            .field("invalid", &self.invalid.load(Ordering::Relaxed))
            .field("rate_limited", &self.rate_limited.load(Ordering::Relaxed))
            .finish_non_exhaustive()
    }
}

impl ActivityRelay {
    pub fn new(sink: ActivitySink) -> Self {
        Self {
            sink,
            bucket: Mutex::new(Bucket::new(RATE_PER_SECOND, Instant::now())),
            invalid: AtomicU64::new(0),
            rate_limited: AtomicU64::new(0),
        }
    }

    /// Valida, limita y emite una línea. Nunca registra su contenido.
    pub fn relay(&self, line: &str) -> Relayed {
        self.relay_at(line, Instant::now())
    }

    fn relay_at(&self, line: &str, now: Instant) -> Relayed {
        let activity = match parse_activity(line) {
            Ok(activity) => activity,
            Err(InvalidActivity(field)) => {
                let total = self.invalid.fetch_add(1, Ordering::Relaxed) + 1;
                tracing::warn!(
                    field,
                    descartadas = total,
                    "actividad de agente inválida: se descarta"
                );
                return Relayed::Invalid;
            }
        };
        let allowed = match self.bucket.lock() {
            Ok(mut bucket) => bucket.take(now),
            Err(poisoned) => poisoned.into_inner().take(now),
        };
        if !allowed {
            let total = self.rate_limited.fetch_add(1, Ordering::Relaxed) + 1;
            // Un aviso al primer descarte y luego uno cada 100, para no llenar el log.
            if total == 1 || total.is_multiple_of(100) {
                tracing::warn!(
                    limite_por_segundo = RATE_PER_SECOND,
                    descartadas = total,
                    "demasiada actividad de agentes: se descarta"
                );
            }
            return Relayed::RateLimited;
        }
        (self.sink)(&activity);
        Relayed::Emitted
    }

    /// Líneas descartadas por inválidas y por el límite (diagnóstico y pruebas).
    pub fn dropped(&self) -> (u64, u64) {
        (
            self.invalid.load(Ordering::Relaxed),
            self.rate_limited.load(Ordering::Relaxed),
        )
    }
}

/// Un segundo (reposición completa del cubo).
pub const RATE_WINDOW: Duration = Duration::from_secs(1);

#[cfg(test)]
#[path = "activity_tests.rs"]
mod tests;
