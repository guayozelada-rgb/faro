//! Ritmo de las decisiones que el motor provoca con solicitudes **aceptadas**
//! (condición 7 de `docs/qa/2026-10-08-f1b-t5-condiciones.md`, revisión de seguridad de T13).
//!
//! Los rechazos ya se agregan en [`super::audit`]; aquí se acota lo que el motor consigue
//! con solicitudes válidas, que antes dejaba una línea de decisión ([`DECISION_TARGET`])
//! cada vez y podía agotar la reserva diaria del log (`logging/capped.rs`):
//!
//! 1. **Emisión de concesiones de ejecución**: un cubo por tipo de agente de
//!    [`RUN_GRANT_BURST`] fichas que recupera una cada [`RUN_GRANT_REFILL`]. Sin fichas, la
//!    solicitud se deniega con `agents.grant_rate_limited` (motivo `rate_limited`, que
//!    entra en el agregado de rechazos); el motor devuelve la tarea a la cola y espera. Solo gasta ficha una concesión emitida. El cubo no se vacía
//!    al reiniciar el motor ni al pausar.
//! 2. **Usos de secretos** (`secret.used` y las demás operaciones del llavero): por
//!    concesión (número de serie), referencia y operación, solo los
//!    [`USES_LOGGED_PER_REF`] primeros van al log uno a uno; el resto se cuenta y, al
//!    terminar la concesión (liberada, caducada, pausada, fin de la llamada o motor
//!    detenido), sale **una** línea `usos de secreto resumidos` con `count` y `errors`.
//! 3. **Tope común** de todas esas líneas (concesión emitida y liberada, usos y sus
//!    resúmenes): un cubo de [`DECISION_LINE_BURST`] líneas que recupera una cada
//!    [`DECISION_LINE_REFILL`]. Sin fichas, la decisión no se escribe y se suma a un
//!    contador; con la siguiente ficha sale antes una línea `decisiones del motor sin
//!    registrar` con `count`, y al detenerse el motor se escribe siempre.
//!
//! Cota diaria de las líneas provocadas por el motor: `DECISION_LINE_BURST + 2 ×
//! 86 400 s / DECISION_LINE_REFILL` (cada ficha puede llevar delante un resumen) más un
//! resumen por parada del motor: ≈ 11 700 líneas, unos 3 MiB con las líneas de ~250
//! bytes que mide la prueba de inundación (menos de 6 MiB aunque ocuparan el doble), de
//! los 16 MiB de la reserva. El resto queda para pausa, reanudación, estado, reinicios y
//! apagado, que solo provoca el usuario o el supervisor.
//!
//! La auditoría (`audit_log`, en la base del motor) no cambia: cada uso y cada concesión
//! siguen dejando su evento.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use tokio::time::Instant;

use crate::logging::DECISION_TARGET;
use crate::secrets::request::Op;

/// Concesiones de ejecución seguidas por tipo de agente antes de limitar el ritmo.
pub const RUN_GRANT_BURST: u32 = 10;
/// Cada cuánto se recupera una concesión de ejecución por tipo de agente.
pub const RUN_GRANT_REFILL: Duration = Duration::from_secs(60);
/// Usos por (concesión, referencia, operación) que van al log uno a uno.
pub const USES_LOGGED_PER_REF: u32 = 3;
/// Líneas de decisión provocadas por el motor seguidas antes de limitar el ritmo.
pub const DECISION_LINE_BURST: u32 = 200;
/// Cada cuánto se recupera una línea de decisión provocada por el motor.
pub const DECISION_LINE_REFILL: Duration = Duration::from_secs(15);

/// Límites de este módulo (otros en pruebas).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DecisionLimits {
    pub run_grant_burst: u32,
    pub run_grant_refill: Duration,
    pub uses_logged_per_ref: u32,
    pub line_burst: u32,
    pub line_refill: Duration,
}

impl DecisionLimits {
    /// Los de la app.
    pub const DEFAULT: Self = Self {
        run_grant_burst: RUN_GRANT_BURST,
        run_grant_refill: RUN_GRANT_REFILL,
        uses_logged_per_ref: USES_LOGGED_PER_REF,
        line_burst: DECISION_LINE_BURST,
        line_refill: DECISION_LINE_REFILL,
    };
}

/// Cubo de fichas: `burst` seguidas y una más cada `every`.
#[derive(Debug)]
struct RateBucket {
    tokens: u32,
    burst: u32,
    every: Duration,
    refilled: Instant,
}

impl RateBucket {
    fn full(burst: u32, every: Duration, now: Instant) -> Self {
        Self {
            tokens: burst,
            burst,
            every,
            refilled: now,
        }
    }

    fn refill(&mut self, now: Instant) {
        if self.tokens >= self.burst {
            // Lleno: el tiempo sin usarlo no da fichas de más.
            self.refilled = now;
            return;
        }
        let every = self.every.as_nanos().max(1);
        let windows = now.saturating_duration_since(self.refilled).as_nanos() / every;
        if windows > 0 {
            let add = u32::try_from(windows).unwrap_or(u32::MAX);
            self.tokens = self.tokens.saturating_add(add).min(self.burst);
            self.refilled = self
                .refilled
                .checked_add(self.every.saturating_mul(add))
                .unwrap_or(now);
        }
    }

    /// Gasta una ficha si la hay.
    fn take(&mut self, now: Instant) -> bool {
        self.refill(now);
        if self.tokens == 0 {
            return false;
        }
        self.tokens -= 1;
        true
    }
}

/// Usos de una referencia con una operación dentro de una concesión.
#[derive(Debug)]
struct UseCount {
    secret_ref: String,
    op: Op,
    logged: u32,
    /// Usos que no fueron al log uno a uno (salen en el resumen).
    unlogged: u64,
    /// De esos, cuántos terminaron con error.
    errors: u64,
}

/// Usos de secretos de una concesión. Pocas entradas: solo referencias y operaciones
/// que la concesión permite (`check_grant` ya pasó).
#[derive(Debug, Default)]
pub(crate) struct UseTally(Vec<UseCount>);

impl UseTally {
    fn entry(&mut self, secret_ref: &str, op: Op) -> &mut UseCount {
        let position = self
            .0
            .iter()
            .position(|u| u.op == op && u.secret_ref == secret_ref);
        let index = match position {
            Some(index) => index,
            None => {
                self.0.push(UseCount {
                    secret_ref: secret_ref.to_owned(),
                    op,
                    logged: 0,
                    unlogged: 0,
                    errors: 0,
                });
                self.0.len() - 1
            }
        };
        &mut self.0[index]
    }
}

/// Por qué termina una concesión (campo `reason` del resumen de usos).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum GrantEnd {
    /// Terminó la llamada de `engine_call`.
    Finished,
    /// `run_grant_release` del motor.
    Released,
    Expired,
    Paused,
    /// Motor detenido o reiniciado.
    EngineStopped,
}

impl GrantEnd {
    fn as_str(self) -> &'static str {
        match self {
            Self::Finished => "finished",
            Self::Released => "released",
            Self::Expired => "expired",
            Self::Paused => "paused",
            Self::EngineStopped => "engine_stopped",
        }
    }
}

/// Ritmo de emisión por agente y tope común de las líneas de decisión que provoca el
/// motor (ver el módulo). Vive en el estado del `SecretBroker`, bajo su candado.
#[derive(Debug)]
pub(crate) struct DecisionLog {
    limits: DecisionLimits,
    lines: RateBucket,
    /// Decisiones sin escribir desde la última línea `decisiones del motor sin registrar`.
    unlogged: u64,
    /// Cubos por tipo de agente (solo tipos de la tabla incrustada: conjunto cerrado).
    agents: HashMap<Arc<str>, RateBucket>,
}

impl DecisionLog {
    pub(crate) fn new(limits: DecisionLimits) -> Self {
        Self {
            limits,
            lines: RateBucket::full(limits.line_burst, limits.line_refill, Instant::now()),
            unlogged: 0,
            agents: HashMap::new(),
        }
    }

    /// ¿Se puede emitir otra concesión de ejecución a `agent`? Gasta una ficha si sí.
    pub(crate) fn allow_run_grant(&mut self, agent: &str, now: Instant) -> bool {
        let limits = self.limits;
        let bucket = match self.agents.get_mut(agent) {
            Some(bucket) => bucket,
            None => self.agents.entry(Arc::from(agent)).or_insert_with(|| {
                RateBucket::full(limits.run_grant_burst, limits.run_grant_refill, now)
            }),
        };
        bucket.take(now)
    }

    /// ¿Se escribe ahora una línea de decisión provocada por el motor que representa
    /// `weight` decisiones? Si no hay ficha, se cuentan y la línea no se escribe. Si hay
    /// ficha y quedaban decisiones sin registrar, antes sale su resumen.
    pub(crate) fn admit_line(&mut self, now: Instant, weight: u64) -> bool {
        if self.take_line(now) {
            return true;
        }
        self.unlogged = self.unlogged.saturating_add(weight);
        false
    }

    /// Gasta una ficha (y escribe antes el resumen pendiente) sin contar nada si no hay.
    fn take_line(&mut self, now: Instant) -> bool {
        if !self.lines.take(now) {
            return false;
        }
        self.flush_unlogged();
        true
    }

    /// Escribe (siempre) el resumen de las decisiones sin registrar, si las hay. Solo lo
    /// llaman una ficha nueva y la parada del motor, que no decide el motor.
    pub(crate) fn flush_unlogged(&mut self) {
        if self.unlogged == 0 {
            return;
        }
        tracing::info!(
            target: DECISION_TARGET,
            count = self.unlogged,
            "decisiones del motor sin registrar"
        );
        self.unlogged = 0;
    }

    /// Cuenta un uso de `secret_ref` con `op` en la concesión `tally`. `true` si va al
    /// log uno a uno (dentro de los primeros por referencia y con ficha).
    pub(crate) fn note_use(
        &mut self,
        tally: &mut UseTally,
        secret_ref: &str,
        op: Op,
        failed: bool,
        now: Instant,
    ) -> bool {
        let entry = tally.entry(secret_ref, op);
        // Sin ficha, el uso queda en la cuenta de la concesión (no en la común): cada
        // decisión se cuenta una sola vez.
        if entry.logged < self.limits.uses_logged_per_ref && self.take_line(now) {
            entry.logged += 1;
            return true;
        }
        entry.unlogged = entry.unlogged.saturating_add(1);
        if failed {
            entry.errors = entry.errors.saturating_add(1);
        }
        false
    }

    /// Resumen de los usos sin registrar de una concesión que termina (una línea por
    /// referencia y operación, dentro del tope común).
    pub(crate) fn retire(
        &mut self,
        run_id: &str,
        operation: &str,
        tally: &UseTally,
        end: GrantEnd,
        now: Instant,
    ) {
        for entry in tally.0.iter().filter(|u| u.unlogged > 0) {
            if self.admit_line(now, entry.unlogged) {
                tracing::info!(
                    target: DECISION_TARGET,
                    run_id,
                    operation,
                    secret_ref = entry.secret_ref.as_str(),
                    op = entry.op.as_str(),
                    count = entry.unlogged,
                    errors = entry.errors,
                    reason = end.as_str(),
                    "usos de secreto resumidos"
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn el_cubo_recupera_una_ficha_por_intervalo_sin_pasar_del_maximo() {
        let start = Instant::now();
        let mut bucket = RateBucket::full(3, Duration::from_secs(60), start);
        assert!((0..3).all(|_| bucket.take(start)));
        assert!(!bucket.take(start));
        assert!(!bucket.take(start + Duration::from_secs(59)));
        assert!(bucket.take(start + Duration::from_secs(60)));
        assert!(!bucket.take(start + Duration::from_secs(61)));
        // Mucho tiempo después: como mucho `burst`.
        let later = start + Duration::from_secs(3600);
        assert!((0..3).all(|_| bucket.take(later)));
        assert!(!bucket.take(later));
    }

    #[test]
    fn los_usos_pasan_al_resumen_tras_los_primeros() {
        let now = Instant::now();
        let mut log = DecisionLog::new(DecisionLimits::DEFAULT);
        let mut tally = UseTally::default();
        let logged = (0..10)
            .filter(|n| log.note_use(&mut tally, "llm/openai/default", Op::Get, *n == 9, now))
            .count();
        assert_eq!(logged, 3);
        // Otra referencia tiene sus propios primeros.
        assert!(log.note_use(&mut tally, "llm/gemini/default", Op::Get, false, now));
        let entry = tally.entry("llm/openai/default", Op::Get);
        assert_eq!((entry.logged, entry.unlogged, entry.errors), (3, 7, 1));
    }

    #[test]
    fn sin_fichas_se_cuenta_y_se_resume_con_la_siguiente() {
        let limits = DecisionLimits {
            line_burst: 2,
            line_refill: Duration::from_secs(10),
            ..DecisionLimits::DEFAULT
        };
        let start = Instant::now();
        let mut log = DecisionLog::new(limits);
        assert!(log.admit_line(start, 1));
        assert!(log.admit_line(start, 1));
        assert!(!log.admit_line(start, 1));
        assert!(!log.admit_line(start, 5));
        assert_eq!(log.unlogged, 6);
        assert!(log.admit_line(start + Duration::from_secs(10), 1));
        assert_eq!(log.unlogged, 0);
    }

    #[test]
    fn el_ritmo_de_concesiones_es_por_agente() {
        let limits = DecisionLimits {
            run_grant_burst: 2,
            ..DecisionLimits::DEFAULT
        };
        let now = Instant::now();
        let mut log = DecisionLog::new(limits);
        assert!(log.allow_run_grant("a", now));
        assert!(log.allow_run_grant("a", now));
        assert!(!log.allow_run_grant("a", now));
        assert!(log.allow_run_grant("b", now));
        assert!(log.allow_run_grant("a", now + RUN_GRANT_REFILL));
    }
}
