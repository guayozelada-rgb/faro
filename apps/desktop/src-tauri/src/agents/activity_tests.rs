//! Pruebas de `agent_activity`: esquema cerrado, sin texto libre y límite de 20/s.

use std::sync::Mutex;

use serde_json::{json, Value};

use super::*;

const RUN: &str = "0192f0a0-0000-7abc-8def-000000000001";
const SITE: &str = "0192f0a0-0001-7abc-8def-0123456789ab";

fn valid() -> Value {
    json!({
        "event": "agent_activity",
        "run_id": RUN,
        "seq": 12,
        "occurred_at": "2026-10-08T12:00:00.123Z",
        "kind": "step_finished",
        "agent": "site_summary",
        "site_id": SITE,
        "status": "running",
        "step": "read_site",
        "step_cost_micros": 0,
        "run_cost_micros": 5678,
        "run_tokens": 4321,
        "error_code": null
    })
}

fn with(key: &str, value: Value) -> String {
    let mut v = valid();
    v[key] = value;
    v.to_string()
}

fn recording() -> (ActivityRelay, Arc<Mutex<Vec<AgentActivity>>>) {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let out = Arc::clone(&seen);
    let sink: ActivitySink = Arc::new(move |a: &AgentActivity| out.lock().unwrap().push(a.clone()));
    let table = crate::agents::manifest::parse_agent_grants(
        r#"[{"kind":"site_summary","requires_site":true,"max_grant_seconds":900,"secrets":[
            {"ref":"llm/openai/default","access":["get"]}]}]"#,
    )
    .unwrap();
    (ActivityRelay::new(sink, Arc::new(table)), seen)
}

#[test]
fn un_agente_fuera_de_la_tabla_se_descarta() {
    let (logs, _guard) = crate::test_logs::capture();
    let (relay, seen) = recording();
    // Forma válida, pero el tipo no está en `agent-grants.json`.
    assert!(parse_activity(&with("agent", json!("agente_inventado"))).is_ok());
    assert_eq!(
        relay.relay(&with("agent", json!("agente_inventado"))),
        Relayed::Invalid
    );
    assert!(seen.lock().unwrap().is_empty());
    let text = logs.text();
    assert!(text.contains("field=\"agent\""), "{text}");
    assert!(!text.contains("agente_inventado"), "{text}");
    // Con la tabla incrustada (vacía hasta T9) no se emite nada.
    let empty = ActivityRelay::new(
        Arc::new(|_: &AgentActivity| panic!("no debe emitirse")),
        crate::agents::manifest::embedded(),
    );
    if crate::agents::manifest::embedded()
        .find("site_summary")
        .is_none()
    {
        assert_eq!(empty.relay(&valid().to_string()), Relayed::Invalid);
    }
}

#[test]
fn las_lineas_invalidas_se_registran_muestreadas() {
    let (logs, _guard) = crate::test_logs::capture();
    let (relay, _seen) = recording();
    for _ in 0..250 {
        assert_eq!(relay.relay("no es json"), Relayed::Invalid);
    }
    assert_eq!(relay.dropped(), (250, 0));
    // La primera, la 100 y la 200.
    assert_eq!(
        logs.text().matches("actividad de agente inválida").count(),
        3
    );
    assert!(logs.text().contains("descartadas=200"), "{}", logs.text());
}

#[test]
fn una_linea_valida_se_emite_sin_event() {
    let activity = parse_activity(&valid().to_string()).unwrap();
    let payload = serde_json::to_value(&activity).unwrap();
    let mut expected = valid();
    expected.as_object_mut().unwrap().remove("event");
    assert_eq!(payload, expected);
    // Con nulos y en los otros tipos.
    for kind in ["run_status", "step_started", "approval_requested"] {
        let mut v = valid();
        v["kind"] = json!(kind);
        v["site_id"] = Value::Null;
        v["step"] = Value::Null;
        v["error_code"] = json!("llm.rate_limited");
        v["occurred_at"] = json!("2026-10-08T12:00:00Z");
        assert!(parse_activity(&v.to_string()).is_ok(), "{kind}");
    }
    // Límites de los enteros.
    assert!(parse_activity(&with("run_tokens", json!(MAX_COUNTER))).is_ok());
}

#[test]
fn claves_de_mas_o_de_menos_se_descartan() {
    let mut extra = valid();
    extra["summary"] = json!("texto libre del resumen");
    assert_eq!(
        parse_activity(&extra.to_string()),
        Err(InvalidActivity("schema"))
    );
    let mut missing = valid();
    missing.as_object_mut().unwrap().remove("run_tokens");
    assert_eq!(
        parse_activity(&missing.to_string()),
        Err(InvalidActivity("schema"))
    );
    // Clave repetida.
    let text = valid().to_string();
    let duplicated = text.replacen("\"seq\":12", "\"seq\":12,\"seq\":13", 1);
    assert_eq!(parse_activity(&duplicated), Err(InvalidActivity("schema")));
    assert_eq!(parse_activity("no es json"), Err(InvalidActivity("schema")));
    assert_eq!(
        parse_activity(&with("event", json!("otro"))),
        Err(InvalidActivity("event"))
    );
}

#[test]
fn texto_libre_e_identificadores_invalidos_se_descartan() {
    let cases = [
        ("agent", json!("Site Summary"), "agent"),
        ("agent", json!(""), "agent"),
        ("agent", json!("x".repeat(49)), "agent"),
        ("status", json!("terminó bien"), "status"),
        ("status", json!("<b>ok</b>"), "status"),
        ("step", json!("lee el sitio https://ejemplo.com"), "step"),
        ("error_code", json!("Error: clave sk-ant-123"), "error_code"),
        ("kind", json!("summary_text"), "schema"),
        ("kind", json!("Run_Status"), "schema"),
        ("run_id", json!("no-uuid"), "run_id"),
        ("run_id", json!(RUN.to_uppercase()), "run_id"),
        ("site_id", json!("sitio"), "site_id"),
        ("occurred_at", json!("ayer"), "occurred_at"),
        (
            "occurred_at",
            json!("2026-10-08T12:00:00+02:00"),
            "occurred_at",
        ),
        ("occurred_at", json!("2026-10-08 12:00:00Z"), "occurred_at"),
        ("occurred_at", json!("2026-13-40T12:00:00Z"), "occurred_at"),
        (
            "occurred_at",
            json!("2026-10-08T12:00:00.1234567890Z"),
            "occurred_at",
        ),
        ("occurred_at", json!("2026-10-08T12:00:00.Z"), "occurred_at"),
        ("seq", json!(-1), "schema"),
        ("seq", json!(1.5), "schema"),
        ("seq", json!("1"), "schema"),
        ("seq", json!(MAX_COUNTER + 1), "seq"),
        (
            "step_cost_micros",
            json!(MAX_COUNTER + 1),
            "step_cost_micros",
        ),
        ("run_cost_micros", json!(MAX_COUNTER + 1), "run_cost_micros"),
        ("run_tokens", json!(MAX_COUNTER + 1), "run_tokens"),
        ("status", json!(7), "schema"),
    ];
    for (key, value, field) in cases {
        assert_eq!(
            parse_activity(&with(key, value.clone())),
            Err(InvalidActivity(field)),
            "{key} = {value}"
        );
    }
}

#[test]
fn linea_de_mas_de_4_kb_se_descarta() {
    let mut line = valid().to_string();
    line.push_str(&" ".repeat(MAX_ACTIVITY_LINE_BYTES - line.len()));
    assert!(parse_activity(&line).is_ok(), "justo 4 KB");
    line.push(' ');
    assert_eq!(parse_activity(&line), Err(InvalidActivity("size")));
}

#[test]
fn identificadores_y_fechas() {
    assert!(is_identifier("a"));
    assert!(is_identifier("llm.rate_limited"));
    assert!(is_identifier(&format!("a{}", "b".repeat(47))));
    for bad in [
        "",
        "1a",
        ".a",
        "A",
        "a-b",
        "a b",
        "ñ",
        &format!("a{}", "b".repeat(48)),
    ] {
        assert!(!is_identifier(bad), "{bad:?}");
    }
    assert!(is_utc_timestamp("2026-10-08T12:00:00Z"));
    assert!(is_utc_timestamp("2026-10-08T12:00:00.123456789Z"));
    for bad in [
        "",
        "Z",
        "2026-10-08T12:00:00",
        "2026-10-08T12:00:00z",
        "2026-10-08t12:00:00Z",
        "2026/10/08T12:00:00Z",
        "2026-10-08T12-00-00Z",
        "2026-02-30T12:00:00Z",
        "2026-10-08T12:00:00,1Z",
        "２026-10-08T12:00:00Z",
    ] {
        assert!(!is_utc_timestamp(bad), "{bad:?}");
    }
}

#[test]
fn el_relevo_emite_las_validas_y_cuenta_descartes_sin_registrar_contenido() {
    let (logs, _guard) = crate::test_logs::capture();
    let (relay, seen) = recording();
    assert_eq!(relay.relay(&valid().to_string()), Relayed::Emitted);
    let secret_text = "resumen con sk-ant-api03-texto-del-modelo";
    assert_eq!(
        relay.relay(&with("status", json!(secret_text))),
        Relayed::Invalid
    );
    let mut extra = valid();
    extra["title"] = json!("Título del sitio privado");
    assert_eq!(relay.relay(&extra.to_string()), Relayed::Invalid);
    assert_eq!(seen.lock().unwrap().len(), 1);
    assert_eq!(relay.dropped(), (2, 0));
    let text = logs.text();
    assert!(text.contains("actividad de agente inválida"), "{text}");
    assert!(text.contains("status"), "{text}");
    assert!(
        !text.contains(secret_text) && !text.contains("Título"),
        "{text}"
    );
    assert!(format!("{relay:?}").contains("invalid: 2"));
}

#[test]
fn limite_de_20_eventos_por_segundo() {
    let (logs, _guard) = crate::test_logs::capture();
    let (relay, seen) = recording();
    let start = Instant::now();
    let line = valid().to_string();
    let results: Vec<Relayed> = (0..25).map(|_| relay.relay_at(&line, start)).collect();
    assert_eq!(
        results.iter().filter(|r| **r == Relayed::Emitted).count(),
        20
    );
    assert_eq!(
        results
            .iter()
            .filter(|r| **r == Relayed::RateLimited)
            .count(),
        5
    );
    // A los 100 ms se repusieron 2 fichas.
    let later = start + Duration::from_millis(100);
    let more: Vec<Relayed> = (0..3).map(|_| relay.relay_at(&line, later)).collect();
    assert_eq!(
        more,
        [Relayed::Emitted, Relayed::Emitted, Relayed::RateLimited]
    );
    // Tras un segundo entero el cubo está lleno otra vez (pero nunca más de 20).
    let refill = later + RATE_WINDOW * 5;
    let burst: Vec<Relayed> = (0..21).map(|_| relay.relay_at(&line, refill)).collect();
    assert_eq!(burst.iter().filter(|r| **r == Relayed::Emitted).count(), 20);
    assert_eq!(seen.lock().unwrap().len(), 42);
    assert_eq!(relay.dropped(), (0, 7));
    // Un solo aviso (el primero), no uno por línea.
    assert_eq!(
        logs.text()
            .matches("demasiada actividad de agentes")
            .count(),
        1
    );
    // Cada 100 descartes, otro aviso.
    for _ in 0..93 {
        relay.relay_at(&line, refill);
    }
    assert_eq!(relay.dropped(), (0, 100));
    assert_eq!(
        logs.text()
            .matches("demasiada actividad de agentes")
            .count(),
        2
    );
}

#[test]
fn constantes_del_evento() {
    assert_eq!(ACTIVITY_EVENT, "engine://agents");
    assert_eq!(ACTIVITY_WINDOW, "main");
    assert_eq!(RATE_PER_SECOND, 20);
}
