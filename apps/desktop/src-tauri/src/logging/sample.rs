//! Muestreo de avisos que un motor puede provocar sin límite (revisión de seguridad de T5).
//!
//! Igual que el aviso de actividad descartada por el límite: se registra la primera vez y
//! luego una de cada [`SAMPLE_EVERY`], con el total en el propio aviso. Así un motor que
//! inunda stdout no llena el log del núcleo (que además tiene un tope diario, ver
//! [`super::capped`]) ni oculta los avisos de otras causas.

use std::sync::atomic::{AtomicU64, Ordering};

/// Se registra uno de cada tantos casos (más el primero).
pub const SAMPLE_EVERY: u64 = 100;

/// Contador de casos de un aviso muestreado.
#[derive(Debug, Default)]
pub struct LogSampler(AtomicU64);

impl LogSampler {
    pub const fn new() -> Self {
        Self(AtomicU64::new(0))
    }

    /// Cuenta un caso; `true` si toca registrarlo (el primero y uno de cada 100).
    pub fn hit(&self) -> bool {
        let total = self.0.fetch_add(1, Ordering::Relaxed) + 1;
        should_log(total)
    }

    /// Casos contados hasta ahora.
    pub fn total(&self) -> u64 {
        self.0.load(Ordering::Relaxed)
    }
}

/// ¿Se registra el caso número `total` (desde 1)?
pub fn should_log(total: u64) -> bool {
    total == 1 || total.is_multiple_of(SAMPLE_EVERY)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn el_primero_y_uno_de_cada_cien() {
        let sampler = LogSampler::new();
        let logged: Vec<u64> = (1..=350).filter(|_| sampler.hit()).collect::<Vec<_>>();
        assert_eq!(logged.len(), 4);
        assert_eq!(sampler.total(), 350);
        assert!(should_log(1) && should_log(100) && should_log(300));
        assert!(!should_log(2) && !should_log(99) && !should_log(101));
    }
}
