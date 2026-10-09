//! Pruebas de la cola de auditoría del núcleo: forma de los eventos, búfer acotado con
//! descarte por prioridad, `audit.dropped` y agregación de rechazos por grupo.

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
        (Action::AuditDropped, "audit.dropped"),
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
    let lines = read_lines(&mut reader, AUDIT_BUFFER + 1).await;
    // Primero, el aviso sintético con los descartados.
    assert_eq!(lines[0]["action"], "audit.dropped");
    assert_eq!(lines[0]["result"], "error");
    assert_eq!(
        lines[0]["details"],
        json!({"reason": "buffer_full", "count": "2"})
    );
    assert_eq!(lines[1]["details"]["reason"], "n2");
    assert_eq!(
        lines[AUDIT_BUFFER]["details"]["reason"],
        format!("n{}", AUDIT_BUFFER + 1)
    );
    assert_eq!(queue.dropped(), 2);
    // Ya informado: no se repite.
    queue.record(event(9));
    assert_eq!(queue.sync().await, 0);
    assert_eq!(
        read_lines(&mut reader, 1).await[0]["details"]["reason"],
        "n9"
    );
    let _ = logs;
}

fn denied(n: usize) -> AuditEvent {
    AuditEvent::new(Actor::System, Action::Denied, Outcome::Denied)
        .detail(DetailKey::Reason, &format!("r{n}"))
}

/// Motor desenganchado (p. ej. `database_error`): una inundación de rechazos no expulsa
/// los `ok` anteriores, y al reengancharse llega `audit.dropped` antes que nada.
#[tokio::test]
async fn con_el_bufer_lleno_se_descartan_primero_los_rechazos() {
    let (logs, _guard) = crate::test_logs::capture();
    let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
    queue.detach();
    for n in 0..5 {
        queue.record(event(n));
    }
    // Rechazos de grupos distintos (no se agregan) y agregables con `run_id` distintos.
    for n in 0..AUDIT_BUFFER + 100 {
        queue.record(denied(n));
        queue.record(
            malformed(None)
                .run_id(&format!("0192f0a0-0000-7abc-8def-{n:012}"))
                .aggregated(),
        );
    }
    assert_eq!(queue.sync().await, AUDIT_BUFFER);
    let kept = queue.snapshot();
    let reasons: Vec<&str> = kept
        .iter()
        .take(5)
        .map(|e| e.details[&DetailKey::Reason].as_str())
        .collect();
    assert_eq!(reasons, ["n0", "n1", "n2", "n3", "n4"], "los ok siguen");
    let dropped = queue.dropped();
    assert!(dropped >= 100, "{dropped}");

    // Búfer lleno solo de `ok`: un rechazo nuevo se descarta él mismo.
    let full = AuditQueue::spawn(&tokio::runtime::Handle::current());
    for n in 0..AUDIT_BUFFER {
        full.record(event(n));
    }
    full.record(denied(0));
    assert_eq!(full.sync().await, AUDIT_BUFFER);
    assert!(full.snapshot().iter().all(|e| e.result == Outcome::Ok));
    assert_eq!(full.dropped(), 1);

    let (core, engine) = tokio::io::duplex(1024 * 1024);
    queue.attach(StdinWriter::spawn(Box::new(core)));
    assert_eq!(queue.sync().await, 0);
    let mut reader = BufReader::new(engine);
    let first = read_lines(&mut reader, 6).await;
    assert_eq!(first[0]["action"], "audit.dropped");
    assert_eq!(first[0]["details"]["count"], dropped.to_string());
    for (n, line) in first[1..].iter().enumerate() {
        assert_eq!(line["details"]["reason"], format!("n{n}"));
    }
    assert!(logs.text().contains("búfer de auditoría lleno"));
}

#[tokio::test]
async fn un_audit_dropped_que_no_se_puede_enviar_se_vuelve_a_contar() {
    let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
    for n in 0..AUDIT_BUFFER + 3 {
        queue.record(event(n));
    }
    assert_eq!(queue.sync().await, AUDIT_BUFFER);
    let (core, engine) = tokio::io::duplex(64);
    drop(engine);
    queue.attach(StdinWriter::spawn(Box::new(core)));
    assert_eq!(queue.sync().await, AUDIT_BUFFER, "nada se pierde");
    let (core, engine) = tokio::io::duplex(1024 * 1024);
    queue.attach(StdinWriter::spawn(Box::new(core)));
    assert_eq!(queue.sync().await, 0);
    let first = read_lines(&mut BufReader::new(engine), 1).await;
    assert_eq!(first[0]["details"]["count"], "3");
}

#[tokio::test(flavor = "current_thread")]
async fn en_modo_externo_audit_dropped_va_al_log() {
    let (logs, _guard) = crate::test_logs::capture();
    let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
    for n in 0..AUDIT_BUFFER + 1 {
        queue.record(event(n));
    }
    queue.log_only();
    assert_eq!(queue.sync().await, 0);
    assert!(logs.text().contains("audit.dropped"), "{}", logs.text());
}

#[test]
fn audit_dropped_tiene_la_forma_del_motor() {
    let v: Value = serde_json::from_slice(&AuditEvent::dropped(0).to_line()).unwrap();
    assert_eq!(v["action"], "audit.dropped");
    assert_eq!(v["actor"], "system");
    assert_eq!(v["details"], json!({"reason": "buffer_full", "count": "1"}));
    let big = AuditEvent::dropped(u64::MAX);
    assert_eq!(big.count(), MAX_COUNT);
}

#[test]
fn solo_los_rechazos_de_las_acciones_con_count_se_agregan() {
    assert!(malformed(None).is_aggregated());
    for action in [Action::Denied, Action::GrantDenied, Action::GrantReleased] {
        assert!(AuditEvent::new(Actor::System, action, Outcome::Denied)
            .aggregated()
            .is_aggregated());
        assert!(!AuditEvent::new(Actor::System, action, Outcome::Ok)
            .aggregated()
            .is_aggregated());
    }
    for action in [
        Action::Used,
        Action::Added,
        Action::GrantIssued,
        Action::AgentsPaused,
    ] {
        assert!(!action.takes_count());
        assert!(!AuditEvent::new(Actor::System, action, Outcome::Denied)
            .aggregated()
            .is_aggregated());
    }
    assert!(Action::AuditDropped.takes_count());
}

#[test]
fn al_agregar_solo_quedan_los_campos_iguales() {
    let site = "0192f0a0-1234-7abc-8def-0123456789ab";
    let mut first = AuditEvent::new(Actor::System, Action::Denied, Outcome::Denied)
        .secret_ref(&format!("wp/{site}/token"))
        .run_id(RUN)
        .detail(DetailKey::Reason, "run_inactive")
        .detail(DetailKey::ErrorCode, "vault.secret_not_allowed")
        .detail(DetailKey::Op, "get")
        .detail(DetailKey::SiteId, site)
        .aggregated();
    let other = AuditEvent::new(Actor::System, Action::Denied, Outcome::Denied)
        .secret_ref("llm/openai/default")
        .run_id(RUN)
        .detail(DetailKey::Reason, "run_inactive")
        .detail(DetailKey::ErrorCode, "vault.secret_not_allowed")
        .detail(DetailKey::Op, "get")
        .aggregated();
    assert!(first.absorbs(&other));
    first.absorb(&other);
    assert_eq!(first.secret_ref, None);
    assert_eq!(first.run_id.as_deref(), Some(RUN));
    let keys: Vec<DetailKey> = first.details.keys().copied().collect();
    assert_eq!(
        keys,
        [
            DetailKey::Op,
            DetailKey::Reason,
            DetailKey::ErrorCode,
            DetailKey::Count
        ]
    );
    // Otro motivo es otro grupo.
    let not_granted = AuditEvent::new(Actor::System, Action::Denied, Outcome::Denied)
        .detail(DetailKey::Reason, "not_granted")
        .detail(DetailKey::ErrorCode, "vault.secret_not_allowed")
        .aggregated();
    assert!(!first.absorbs(&not_granted));
}

#[test]
fn el_cubo_da_diez_seguidos_y_recupera_uno_por_ventana() {
    let aggregator = Aggregator::new(Duration::from_secs(60));
    let start = Instant::now();
    let flood = |n: usize| {
        malformed(None)
            .run_id(&format!("0192f0a0-0000-7abc-8def-{n:012}"))
            .aggregated()
    };
    let now: Vec<bool> = (0..25)
        .map(|n| matches!(aggregator.admit(flood(n), start), Admit::Now(..)))
        .collect();
    assert_eq!(now.iter().filter(|n| **n).count(), 10);
    assert!(now[..10].iter().all(|n| *n));
    let drained = aggregator.drain(start);
    assert_eq!(drained.len(), 1);
    assert_eq!(drained[0].0.count(), 15);
    assert_eq!(drained[0].1, 25, "total del grupo");
    // Sin fichas ni ventana: se sigue agregando.
    assert!(matches!(aggregator.admit(flood(0), start), Admit::Held));
    // Una ventana después hay una ficha, pero el pendiente va primero.
    let later = start + Duration::from_secs(61);
    assert!(matches!(aggregator.admit(flood(1), later), Admit::Held));
    assert_eq!(aggregator.drain(later)[0].0.count(), 2);
    assert!(matches!(
        aggregator.admit(flood(2), later),
        Admit::Now(_, 28)
    ));
    // Grupo sin pendiente y con el cubo lleno: se olvida.
    let much_later = later + Duration::from_secs(3600);
    assert!(aggregator.drain(much_later).is_empty());
    assert!(aggregator.lock().is_empty());
}

/// Un motor que sí lee stdin e inunda con rechazos de `run_id` y referencias distintas:
/// la auditoría y el log quedan acotados por grupo.
#[tokio::test]
async fn una_inundacion_de_rechazos_queda_acotada_en_log_y_auditoria() {
    let (logs, _guard) = crate::test_logs::capture();
    let queue = AuditQueue::spawn(&tokio::runtime::Handle::current());
    let (core, engine) = tokio::io::duplex(1024 * 1024);
    queue.attach(StdinWriter::spawn(Box::new(core)));
    let reader = tokio::spawn(async move {
        let mut reader = BufReader::new(engine);
        let mut lines = Vec::new();
        let mut line = String::new();
        while reader.read_line(&mut line).await.unwrap_or(0) > 0 {
            lines.push(serde_json::from_str::<Value>(&line).unwrap());
            line.clear();
        }
        lines
    });
    const FLOOD: usize = 30_000;
    for n in 0..FLOOD {
        let run = format!("0192f0a0-0000-7abc-8def-{n:012}");
        let site = format!("0192f0a0-{:04x}-7abc-8def-0123456789ab", n % 65_536);
        for (action, reason) in [
            (Action::Denied, "run_inactive"),
            (Action::GrantDenied, "unknown_agent"),
            (Action::GrantReleased, "not_active"),
        ] {
            queue.record(
                AuditEvent::new(Actor::System, action, Outcome::Denied)
                    .secret_ref(&format!("wp/{site}/token"))
                    .run_id(&run)
                    .detail(DetailKey::Reason, reason)
                    .detail(DetailKey::SiteId, &site)
                    .aggregated(),
            );
        }
    }
    assert_eq!(queue.sync().await, 0);
    drop(queue);
    let lines = tokio::time::timeout(Duration::from_secs(10), reader)
        .await
        .unwrap()
        .unwrap();
    let burst = usize::try_from(AGGREGATE_BURST).unwrap();
    assert_eq!(lines.len(), 3 * (burst + 1), "{}", lines.len());
    let mut represented = 0;
    for line in &lines {
        represented += line["details"]["count"]
            .as_str()
            .map_or(1, |c| c.parse::<usize>().unwrap());
    }
    assert_eq!(
        represented,
        3 * FLOOD,
        "ningún rechazo se pierde en la cuenta"
    );
    let aggregated: Vec<&Value> = lines
        .iter()
        .filter(|l| l["details"]["count"].is_string())
        .collect();
    assert_eq!(aggregated.len(), 3);
    for line in aggregated {
        assert_eq!(line["run_id"], Value::Null);
        assert_eq!(line["secret_ref"], Value::Null);
        assert!(line["details"].get("site_id").is_none(), "{line}");
    }
    let text = logs.text();
    for message in [
        "solicitud de secreto rechazada",
        "concesión de agente denegada",
        "liberación de concesión de agente rechazada",
    ] {
        assert_eq!(text.matches(message).count(), burst + 1, "{message}");
    }
    assert!(
        text.contains(&format!("total={FLOOD}")),
        "el agregado lleva el total"
    );
}

/// Sin `sync`: la tarea vuelca los agregados al cumplirse la ventana.
#[tokio::test]
async fn los_agregados_se_vuelcan_solos_cada_ventana() {
    let queue = AuditQueue::spawn_with_window(
        &tokio::runtime::Handle::current(),
        Duration::from_millis(50),
    );
    let (core, engine) = tokio::io::duplex(1024 * 1024);
    queue.attach(StdinWriter::spawn(Box::new(core)));
    for _ in 0..AGGREGATE_BURST + 5 {
        queue.record(malformed(Some(RUN)));
    }
    let burst = usize::try_from(AGGREGATE_BURST).unwrap();
    let mut reader = BufReader::new(engine);
    let lines = tokio::time::timeout(Duration::from_secs(5), read_lines(&mut reader, burst + 1))
        .await
        .expect("no se volcó el agregado");
    assert_eq!(lines[burst]["details"]["count"], "5");
    assert_eq!(lines[burst]["run_id"], RUN, "mismo run_id en todos");
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
