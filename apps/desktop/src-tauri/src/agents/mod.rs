//! Agentes en el núcleo (ADR 0014, spec F1b §4.5).
//!
//! - [`manifest`]: tabla de concesiones `agent-grants.json` incrustada y validada con tipos
//!   cerrados (falla cerrado).
//! - [`control`]: pausa global persistente (`agents-control.json`) y aviso
//!   `agents_control` al motor.
//! - [`activity`]: validación de `agent_activity`, límite de 20 eventos/s y relevo como
//!   `engine://agents`.
//!
//! Las concesiones por ejecución viven en `secrets::run_grants` (mismo mapa que las de
//! operación).

pub mod activity;
pub mod control;
pub mod manifest;

use std::sync::Arc;

/// Lo que el supervisor necesita de los agentes: avisar al motor y retransmitir su
/// actividad.
#[derive(Debug, Clone)]
pub struct AgentsLink {
    pub control: Arc<control::AgentsControl>,
    pub activity: Arc<activity::ActivityRelay>,
}
