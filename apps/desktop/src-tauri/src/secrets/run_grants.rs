//! Concesiones por ejecución de los agentes (ADR 0014 §1, condiciones 3, 4, 5 y 7 de la
//! revisión de T3).
//!
//! ```text
//! motor → {"event":"run_grant_request","id":"<uuid>","run_id":"<uuid>","agent":"<kind>",
//!          "site_id":"<uuid>"|null,"provider":"anthropic"|"openai"|"gemini"|null,
//!          "trigger":"user"|"schedule"|"catch_up"}
//! núcleo → {"event":"run_grant_response","id":"<uuid>","ok":true,"expires_in_seconds":900}
//!        | {"event":"run_grant_response","id":"<uuid>","error":"agents.paused"|"agent.grant_denied"}
//! motor → {"event":"run_grant_release","run_id":"<uuid>",
//!          "status":"succeeded"|"failed"|"cancelled"|"waiting_approval"|"paused"}
//! ```
//!
//! Comprobaciones, en este orden (el primer fallo decide el motivo):
//!
//! 1. agentes no pausados → `agents.paused`;
//! 2. `agent` en la tabla incrustada ([`crate::agents::manifest`]);
//! 3. `run_id` UUID canónico y sin otra concesión activa (las caducadas no cuentan: así se
//!    renueva una concesión vencida del mismo `run_id`);
//! 4. si el agente `requires_site`, `site_id` presente y en el índice de sitios del perfil
//!    activo; si no lo requiere, el sitio se ignora (nunca se concede `wp/*`);
//! 5. `provider` presente, de la lista y con `llm/<provider>/default` en la tabla del
//!    agente (`provider: null` se deniega; un agente sin plantillas `llm/*` nunca obtiene
//!    concesión);
//! 6. como mucho [`MAX_ACTIVE_RUN_GRANTS`] concesiones de ejecución activas.
//!
//! El sitio se comprueba fuera del candado (lee el índice del disco); por eso, antes de
//! guardar la concesión, se repiten bajo el candado la pausa, el `run_id`, la generación,
//! el perfil y el máximo. La concesión lleva solo `llm/<provider>/default: get` y, si el
//! agente declara `wp/{site_id}/token`, `wp/<site_id>/token: get`; caduca a
//! `min(max_grant_seconds, 900)` s y queda atada a la generación del motor y al perfil.
//!
//! Todo rechazo responde `agent.grant_denied` (o `agents.paused`) **sin motivo**: el motivo
//! solo va al log y a la auditoría. `run_grant_release` solo borra la concesión de
//! ejecución de su `run_id` (nunca una de operación) y no tiene respuesta.
//!
//! Lo que un motor puede repetir sin límite (solicitudes malformadas o sin `id`,
//! liberaciones malformadas o de concesiones que no existen) se registra muestreado (el
//! primero y uno de cada 100) y se audita agregado (`details.count`); la liberación de
//! una concesión que no existe se audita como `denied` (`not_active`).

use serde::Deserialize;

use super::*;
use crate::agents::manifest::AgentSpec;
use crate::vault::Provider;

/// Concesiones de ejecución activas como máximo (ADR 0014 §1).
pub const MAX_ACTIVE_RUN_GRANTS: usize = 4;
/// Respuesta con los agentes en pausa.
pub const AGENTS_PAUSED: &str = "agents.paused";
/// Cualquier otro rechazo.
pub const GRANT_DENIED: &str = "agent.grant_denied";

/// Qué disparó la ejecución (solo se valida; no cambia la decisión).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Trigger {
    User,
    Schedule,
    CatchUp,
}

impl Trigger {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::User => "user",
            Self::Schedule => "schedule",
            Self::CatchUp => "catch_up",
        }
    }
}

/// Estado con el que termina una ejecución (`run_grant_release`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReleaseStatus {
    Succeeded,
    Failed,
    Cancelled,
    WaitingApproval,
    Paused,
}

impl ReleaseStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::WaitingApproval => "waiting_approval",
            Self::Paused => "paused",
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRequest {
    event: String,
    id: String,
    run_id: String,
    agent: String,
    site_id: Option<String>,
    provider: Option<String>,
    trigger: Trigger,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRelease {
    event: String,
    run_id: String,
    status: ReleaseStatus,
}

#[derive(Deserialize)]
struct IdOnly {
    id: Option<serde_json::Value>,
}

/// Solicitud con la forma correcta (los valores se comprueban después, en orden).
#[derive(Debug)]
struct RunGrantRequest {
    id: String,
    run_id: String,
    agent: String,
    site_id: Option<String>,
    provider: Option<String>,
    trigger: Trigger,
}

enum Parsed {
    Request(RunGrantRequest),
    /// Forma incorrecta con `id` válido: se responde `agent.grant_denied`.
    Malformed {
        id: String,
    },
    /// Sin `id` válido: no se puede responder (el motor agota su espera).
    Unanswerable,
}

fn parse_request(line: &str) -> Parsed {
    match serde_json::from_str::<RawRequest>(line.trim()) {
        Ok(raw) if raw.event == "run_grant_request" && is_canonical_uuid(&raw.id) => {
            Parsed::Request(RunGrantRequest {
                id: raw.id,
                run_id: raw.run_id,
                agent: raw.agent,
                site_id: raw.site_id,
                provider: raw.provider,
                trigger: raw.trigger,
            })
        }
        Ok(_) => Parsed::Unanswerable,
        Err(_) => match serde_json::from_str::<IdOnly>(line.trim()) {
            Ok(IdOnly {
                id: Some(serde_json::Value::String(id)),
            }) if is_canonical_uuid(&id) => Parsed::Malformed { id },
            _ => Parsed::Unanswerable,
        },
    }
}

/// `{"event":"run_grant_response","id":…,"ok":true,"expires_in_seconds":N}` + `\n`.
fn ok_response(id: &str, seconds: u64) -> Zeroizing<String> {
    Zeroizing::new(format!(
        "{}\n",
        serde_json::json!({
            "event": "run_grant_response",
            "id": id,
            "ok": true,
            "expires_in_seconds": seconds,
        })
    ))
}

/// `{"event":"run_grant_response","id":…,"error":"<code>"}` + `\n`.
fn error_response(id: &str, code: &str) -> Zeroizing<String> {
    Zeroizing::new(format!(
        "{}\n",
        serde_json::json!({"event": "run_grant_response", "id": id, "error": code})
    ))
}

/// Concesión emitida (para la auditoría).
struct Issued {
    seconds: u64,
    provider: Provider,
    /// Sitio cuyo `wp/<site_id>/token` se concedió.
    site_id: Option<String>,
}

/// Datos validados de la solicitud que se pueden auditar (nunca texto arbitrario).
struct Facts<'a> {
    run_id: Option<&'a str>,
    /// Solo si el tipo está en la tabla.
    agent: Option<&'a str>,
    provider: Option<Provider>,
    site_id: Option<&'a str>,
    trigger: Option<Trigger>,
}

impl SecretBroker {
    /// Atiende una línea `run_grant_request` del motor de la generación `generation`.
    /// Devuelve la respuesta, o `None` si no tiene un `id` válido.
    pub(crate) fn handle_run_grant_line(
        &self,
        line: &str,
        generation: u64,
    ) -> Option<Zeroizing<String>> {
        let request = match parse_request(line) {
            Parsed::Request(request) => request,
            Parsed::Malformed { id } => {
                self.record_run_denial(&Facts::none(), deny(GRANT_DENIED, "malformed"));
                return Some(error_response(&id, GRANT_DENIED));
            }
            Parsed::Unanswerable => {
                if self.samples.grant_unanswerable.hit() {
                    tracing::warn!(
                        total = self.samples.grant_unanswerable.total(),
                        "solicitud de concesión de agente sin id válido: se ignora"
                    );
                }
                self.record_run_denial(&Facts::none(), deny(GRANT_DENIED, "malformed"));
                return None;
            }
        };
        let facts = Facts {
            run_id: Some(request.run_id.as_str()).filter(|r| is_canonical_uuid(r)),
            agent: self
                .agents
                .find(&request.agent)
                .map(|spec| spec.kind.as_str()),
            provider: request.provider.as_deref().and_then(Provider::parse),
            site_id: request.site_id.as_deref().filter(|s| is_canonical_uuid(s)),
            trigger: Some(request.trigger),
        };
        match self.decide_run_grant(&request, generation) {
            Ok(issued) => {
                self.record_issued(&facts, &issued);
                Some(ok_response(&request.id, issued.seconds))
            }
            Err(denial) => {
                self.record_run_denial(&facts, denial);
                Some(error_response(&request.id, denial.code))
            }
        }
    }

    fn decide_run_grant(
        &self,
        request: &RunGrantRequest,
        generation: u64,
    ) -> Result<Issued, Denial> {
        // 1–3 bajo el candado.
        let (spec, profile_id) = {
            let mut state = self.lock();
            state.prune(Instant::now());
            if state.agents_paused {
                return Err(deny(AGENTS_PAUSED, "paused"));
            }
            let spec = self
                .agents
                .find(&request.agent)
                .ok_or(deny(GRANT_DENIED, "unknown_agent"))?;
            if !is_canonical_uuid(&request.run_id) {
                return Err(deny(GRANT_DENIED, "invalid_run_id"));
            }
            if state.grants.contains_key(&request.run_id) {
                return Err(deny(GRANT_DENIED, "run_active"));
            }
            if !state.running || state.generation != generation {
                return Err(deny(GRANT_DENIED, "engine_inactive"));
            }
            (spec.clone(), state.profile_id.clone())
        };
        // 4. Sitio del perfil activo (lee el índice del disco, fuera del candado).
        let site_id = self.check_site(&spec, request.site_id.as_deref(), profile_id.as_deref())?;
        // 5. Proveedor declarado por el agente.
        let provider = request
            .provider
            .as_deref()
            .ok_or(deny(GRANT_DENIED, "provider_missing"))?;
        let provider = Provider::parse(provider).ok_or(deny(GRANT_DENIED, "provider_unknown"))?;
        if !spec.declares_provider(provider) {
            return Err(deny(GRANT_DENIED, "provider_not_declared"));
        }
        // 6. Se repite lo que pudo cambiar mientras se leía el índice, y el máximo.
        let mut state = self.lock();
        state.prune(Instant::now());
        if state.agents_paused {
            return Err(deny(AGENTS_PAUSED, "paused"));
        }
        if state.grants.contains_key(&request.run_id) {
            return Err(deny(GRANT_DENIED, "run_active"));
        }
        if !state.running || state.generation != generation {
            return Err(deny(GRANT_DENIED, "engine_inactive"));
        }
        if state.profile_id != profile_id {
            return Err(deny(GRANT_DENIED, "profile_changed"));
        }
        let active = state.grants.values().filter(|g| g.origin.is_run()).count();
        if active >= MAX_ACTIVE_RUN_GRANTS {
            return Err(deny(GRANT_DENIED, "too_many_grants"));
        }
        // La concesión sale solo de la tabla y de la petición ya validada.
        let mut refs = vec![(provider.secret_ref().to_owned(), vec![Op::Get])];
        let granted_site = site_id.filter(|_| spec.declares_site_token());
        if let Some(site) = &granted_site {
            refs.push((wp_ref(site), vec![Op::Get]));
        }
        let seconds = spec.grant_seconds();
        let serial = state.next_serial();
        state.grants.insert(
            request.run_id.clone(),
            Grant {
                serial,
                origin: Origin::run(&spec.kind),
                expires_at: Instant::now() + Duration::from_secs(seconds),
                generation,
                profile_id,
                refs,
                new_slot: None,
            },
        );
        Ok(Issued {
            seconds,
            provider,
            site_id: granted_site,
        })
    }

    /// Comprobación 4. Devuelve el sitio validado si el agente lo requiere.
    fn check_site(
        &self,
        spec: &AgentSpec,
        site_id: Option<&str>,
        profile_id: Option<&str>,
    ) -> Result<Option<String>, Denial> {
        if let Some(site) = site_id {
            if !is_canonical_uuid(site) {
                return Err(deny(GRANT_DENIED, "invalid_site"));
            }
        }
        if !spec.requires_site {
            // Con `site_id` presente y `requires_site: false` nunca se concede `wp/*`.
            return Ok(None);
        }
        let site = site_id.ok_or(deny(GRANT_DENIED, "site_required"))?;
        let owned = profile_id.is_some_and(|profile| self.sites.contains(profile, site));
        if !owned {
            return Err(deny(GRANT_DENIED, "site_not_in_profile"));
        }
        Ok(Some(site.to_owned()))
    }

    /// Atiende una línea `run_grant_release`. No responde.
    pub(crate) fn handle_run_release_line(&self, line: &str, generation: u64) {
        let release = match serde_json::from_str::<RawRelease>(line.trim()) {
            Ok(raw) if raw.event == "run_grant_release" && is_canonical_uuid(&raw.run_id) => raw,
            _ => {
                if self.samples.release_malformed.hit() {
                    tracing::warn!(
                        total = self.samples.release_malformed.total(),
                        "liberación de concesión de agente malformada: se ignora"
                    );
                }
                return;
            }
        };
        let removed = {
            let mut state = self.lock();
            let current = state.running && state.generation == generation;
            let is_run = state
                .grants
                .get(&release.run_id)
                .map(|grant| grant.origin.is_run());
            match (current, is_run) {
                (true, Some(true)) => state
                    .grants
                    .remove(&release.run_id)
                    .map(|grant| grant.origin),
                (true, Some(false)) => {
                    // Una concesión de operación nunca se libera desde el motor.
                    drop(state);
                    tracing::warn!("el motor intentó liberar una concesión de operación");
                    let event =
                        AuditEvent::new(Actor::System, Action::GrantReleased, Outcome::Denied)
                            .run_id(&release.run_id)
                            .detail(DetailKey::Reason, "not_run_grant");
                    self.audit.record(event);
                    return;
                }
                _ => None,
            }
        };
        let status = release.status.as_str();
        let event = match &removed {
            Some(origin) => {
                // Cada una corresponde a una concesión emitida: no hace falta muestrear.
                tracing::info!(
                    run_id = release.run_id.as_str(),
                    status,
                    "concesión de agente liberada"
                );
                let event = AuditEvent::new(Actor::System, Action::GrantReleased, Outcome::Ok)
                    .run_id(&release.run_id);
                with_origin(event, origin).detail(DetailKey::Reason, status)
            }
            None => {
                // Caducada, ya liberada o inventada: el motor puede repetirla sin límite.
                if self.samples.release_not_active.hit() {
                    tracing::info!(
                        run_id = release.run_id.as_str(),
                        status,
                        total = self.samples.release_not_active.total(),
                        "liberación de una concesión de agente que no existe"
                    );
                }
                AuditEvent::new(Actor::System, Action::GrantReleased, Outcome::Denied)
                    .run_id(&release.run_id)
                    .detail(DetailKey::Reason, "not_active")
                    .aggregated()
            }
        };
        self.audit.record(event);
    }

    fn record_issued(&self, facts: &Facts<'_>, issued: &Issued) {
        tracing::info!(
            run_id = facts.run_id,
            agent = facts.agent,
            trigger = facts.trigger.map(Trigger::as_str),
            provider = issued.provider.as_str(),
            site_id = issued.site_id.as_deref(),
            expires_in_seconds = issued.seconds,
            "concesión de agente emitida"
        );
        let mut event = AuditEvent::new(Actor::System, Action::GrantIssued, Outcome::Ok)
            .detail(DetailKey::Provider, issued.provider.as_str());
        event = facts.apply(event);
        if let Some(site) = &issued.site_id {
            event = event.detail(DetailKey::SiteId, site);
        }
        self.audit.record(event);
    }

    /// Log y auditoría de un rechazo: el motivo solo aquí, nunca en la respuesta.
    fn record_run_denial(&self, facts: &Facts<'_>, denial: Denial) {
        let malformed = denial.reason == "malformed";
        if !malformed || self.samples.grant_malformed.hit() {
            tracing::warn!(
                run_id = facts.run_id,
                agent = facts.agent,
                trigger = facts.trigger.map(Trigger::as_str),
                error_code = denial.code,
                reason = denial.reason,
                total = malformed.then(|| self.samples.grant_malformed.total()),
                "concesión de agente denegada"
            );
        }
        let mut event = AuditEvent::new(Actor::System, Action::GrantDenied, Outcome::Denied)
            .detail(DetailKey::ErrorCode, denial.code)
            .detail(DetailKey::Reason, denial.reason);
        if malformed {
            event = event.aggregated();
        }
        event = facts.apply(event);
        if let Some(provider) = facts.provider {
            event = event.detail(DetailKey::Provider, provider.as_str());
        }
        if let Some(site) = facts.site_id {
            event = event.detail(DetailKey::SiteId, site);
        }
        self.audit.record(event);
    }
}

impl Facts<'_> {
    fn none() -> Self {
        Self {
            run_id: None,
            agent: None,
            provider: None,
            site_id: None,
            trigger: None,
        }
    }

    /// `run_id` y, si el agente es de la tabla, `operation = agent:<kind>` y `agent_kind`.
    fn apply(&self, mut event: AuditEvent) -> AuditEvent {
        if let Some(run_id) = self.run_id {
            event = event.run_id(run_id);
        }
        if let Some(agent) = self.agent {
            event = with_origin(event, &Origin::run(agent));
        }
        event
    }
}

#[cfg(test)]
#[path = "run_grants_tests.rs"]
mod tests;
