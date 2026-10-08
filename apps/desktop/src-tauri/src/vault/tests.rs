//! Pruebas de la Bóveda con `MemoryStore` y servidor HTTP simulado (spec F0 §10.2).
//! Sin claves reales ni proveedores reales.

use std::sync::Arc;
use std::time::Duration;

use secrecy::{ExposeSecret, SecretString};
use serde_json::json;

use super::fake::{FakeProviders, Reply};
use super::providers::{BaseUrls, HttpProviderChecker, ANTHROPIC_VERSION};
use super::secret::AddKeyInput;
use super::store::{MemoryStore, SecretStore};
use super::{KeyStatus, KeySummary, Provider, VaultService};

const SECRET: &str = "test-key-000000000000000000001a2B"; // gitleaks:allow
const SECRET_2: &str = "test-key-000000000000000000009z8Y"; // gitleaks:allow
const NOW: &str = "2026-09-29T12:00:00Z";
const TIMEOUT: Duration = Duration::from_millis(500);

struct Env {
    vault: Arc<VaultService>,
    store: Arc<MemoryStore>,
    server: FakeProviders,
}

async fn env() -> Env {
    let server = FakeProviders::start().await;
    let env = env_with_bases(server.bases());
    Env {
        vault: env.0,
        store: env.1,
        server,
    }
}

fn env_with_bases(bases: BaseUrls) -> (Arc<VaultService>, Arc<MemoryStore>) {
    let store = Arc::new(MemoryStore::new());
    let checker = Arc::new(HttpProviderChecker::for_tests(bases, TIMEOUT).unwrap());
    let vault = Arc::new(VaultService::new(
        store.clone(),
        checker,
        Arc::new(|| NOW.to_owned()),
    ));
    (vault, store)
}

fn add_input(provider: Provider, secret: &str, replace: bool) -> AddKeyInput {
    AddKeyInput {
        provider,
        secret: SecretString::from(secret.to_owned()),
        replace,
    }
}

fn stored(store: &MemoryStore, provider: Provider) -> Option<String> {
    store
        .get(provider.secret_ref())
        .unwrap()
        .map(|s| s.expose_secret().to_owned())
}

fn preload(store: &MemoryStore, provider: Provider, secret: &str) {
    store
        .set(
            provider.secret_ref(),
            &SecretString::from(secret.to_owned()),
        )
        .unwrap();
}

fn assert_no_secret(text: &str) {
    for s in [SECRET, SECRET_2, "000000000000000000001a2B"] {
        assert!(!text.contains(s), "el secreto apareció en: {text}");
    }
}

// ---------- agregar ----------

#[tokio::test]
async fn add_valida_guarda_y_devuelve_resumen_sin_secreto() {
    let env = env().await;
    for provider in Provider::ALL {
        let summary = env
            .vault
            .add(add_input(provider, SECRET, false))
            .await
            .unwrap();
        assert_eq!(
            summary,
            KeySummary {
                provider,
                secret_ref: format!("llm/{}/default", provider.as_str()),
                last4: "1a2B".to_owned(),
                status: KeyStatus::Valid,
                last_tested_at: Some(NOW.to_owned()),
                last_error_code: None,
            }
        );
        assert_eq!(stored(&env.store, provider).as_deref(), Some(SECRET));
        let serialized = serde_json::to_string(&summary).unwrap();
        assert_no_secret(&serialized);
        assert_no_secret(&format!("{summary:?}"));
    }
}

#[tokio::test]
async fn add_recorta_espacios_antes_de_probar_y_guardar() {
    let env = env().await;
    env.vault
        .add(add_input(Provider::Openai, &format!("  {SECRET}\n"), false))
        .await
        .unwrap();
    assert_eq!(
        stored(&env.store, Provider::Openai).as_deref(),
        Some(SECRET)
    );
    let req = &env.server.requests()[0];
    assert_eq!(
        req.header("authorization"),
        Some(format!("Bearer {SECRET}").as_str())
    );
}

#[tokio::test]
async fn add_no_guarda_si_la_prueba_falla_y_mapea_cada_respuesta() {
    let cases: [(Provider, Reply, &str); 11] = [
        (
            Provider::Anthropic,
            Reply::Status(401, "{}"),
            "vault.invalid_key",
        ),
        (
            Provider::Openai,
            Reply::Status(401, "{}"),
            "vault.invalid_key",
        ),
        (
            Provider::Gemini,
            Reply::Status(401, "{}"),
            "vault.invalid_key",
        ),
        (
            Provider::Gemini,
            Reply::Status(
                400,
                r#"{"error":{"code":400,"status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}"#,
            ),
            "vault.invalid_key",
        ),
        (
            Provider::Gemini,
            Reply::Status(400, r#"{"error":{}}"#),
            "vault.provider_error",
        ),
        (
            Provider::Anthropic,
            Reply::Status(403, "{}"),
            "vault.key_restricted",
        ),
        (
            Provider::Openai,
            Reply::Status(429, "{}"),
            "vault.provider_rate_limited",
        ),
        (
            Provider::Anthropic,
            Reply::Status(500, "{}"),
            "vault.provider_error",
        ),
        (
            Provider::Openai,
            Reply::Status(503, "{}"),
            "vault.provider_error",
        ),
        (
            Provider::Gemini,
            Reply::Status(302, "{}"),
            "vault.provider_error",
        ),
        (
            Provider::Anthropic,
            Reply::Hang,
            "vault.provider_unreachable",
        ),
    ];
    for (provider, reply, code) in cases {
        let env = env().await;
        env.server.reply(provider, reply.clone());
        let err = env
            .vault
            .add(add_input(provider, SECRET, false))
            .await
            .unwrap_err();
        assert_eq!(err.code, code, "{provider:?} {reply:?}");
        assert_eq!(err.details, json!({}));
        assert_no_secret(&serde_json::to_string(&err).unwrap());
        assert!(
            stored(&env.store, provider).is_none(),
            "{provider:?} {reply:?}"
        );
        assert!(env.vault.list().await.unwrap().is_empty());
    }
}

#[tokio::test]
async fn add_sin_conexion_es_provider_unreachable() {
    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = closed.local_addr().unwrap().port();
    drop(closed);
    let root = format!("http://127.0.0.1:{port}");
    let (vault, store) = env_with_bases(BaseUrls {
        anthropic: root.clone(),
        openai: root.clone(),
        gemini: root,
    });
    let err = vault
        .add(add_input(Provider::Gemini, SECRET, false))
        .await
        .unwrap_err();
    assert_eq!(err.code, "vault.provider_unreachable");
    assert!(stored(&store, Provider::Gemini).is_none());
}

#[tokio::test]
async fn add_existente_sin_replace_es_already_exists_y_no_prueba() {
    let env = env().await;
    preload(&env.store, Provider::Anthropic, SECRET);
    let err = env
        .vault
        .add(add_input(Provider::Anthropic, SECRET_2, false))
        .await
        .unwrap_err();
    assert_eq!(err.code, "vault.already_exists");
    assert!(env.server.requests().is_empty());
    assert_eq!(
        stored(&env.store, Provider::Anthropic).as_deref(),
        Some(SECRET)
    );
}

#[tokio::test]
async fn add_con_replace_reemplaza_solo_si_la_nueva_es_valida() {
    let env = env().await;
    preload(&env.store, Provider::Openai, SECRET);

    env.server.reply(Provider::Openai, Reply::Status(401, "{}"));
    let err = env
        .vault
        .add(add_input(Provider::Openai, SECRET_2, true))
        .await
        .unwrap_err();
    assert_eq!(err.code, "vault.invalid_key");
    assert_eq!(
        stored(&env.store, Provider::Openai).as_deref(),
        Some(SECRET)
    );

    env.server.reply(Provider::Openai, Reply::Status(200, "{}"));
    let summary = env
        .vault
        .add(add_input(Provider::Openai, SECRET_2, true))
        .await
        .unwrap();
    assert_eq!(summary.last4, "9z8Y");
    assert_eq!(summary.status, KeyStatus::Valid);
    assert_eq!(
        stored(&env.store, Provider::Openai).as_deref(),
        Some(SECRET_2)
    );
}

#[tokio::test]
async fn add_con_formato_invalido_no_prueba_ni_guarda() {
    let env = env().await;
    for bad in [
        "",
        "   ",
        "corta-123",
        "test-key-0000000000 000000001a2B",
        "test-key-000000000000000000001a2Bñ",
    ] {
        let err = env
            .vault
            .add(add_input(Provider::Anthropic, bad, false))
            .await
            .unwrap_err();
        assert_eq!(err.code, "vault.invalid_input", "{bad:?}");
    }
    let long = "a".repeat(513);
    let err = env
        .vault
        .add(add_input(Provider::Anthropic, &long, false))
        .await
        .unwrap_err();
    assert_eq!(err.code, "vault.invalid_input");
    assert!(env.server.requests().is_empty());
    assert!(env.store.refs().is_empty());
}

// ---------- peticiones por proveedor ----------

#[tokio::test]
async fn peticiones_con_cabeceras_correctas_por_proveedor() {
    let env = env().await;
    for provider in Provider::ALL {
        env.vault
            .add(add_input(provider, SECRET, false))
            .await
            .unwrap();
    }
    let requests = env.server.requests();
    assert_eq!(requests.len(), 3);
    let ua = concat!("Faro/", env!("CARGO_PKG_VERSION"));

    let anthropic = &requests[0];
    assert_eq!(anthropic.method, "GET");
    assert_eq!(anthropic.target, "/anthropic/v1/models?limit=1");
    assert_eq!(anthropic.header("x-api-key"), Some(SECRET));
    assert_eq!(
        anthropic.header("anthropic-version"),
        Some(ANTHROPIC_VERSION)
    );
    assert_eq!(anthropic.header("authorization"), None);
    assert_eq!(anthropic.header("user-agent"), Some(ua));

    let openai = &requests[1];
    assert_eq!(openai.method, "GET");
    assert_eq!(openai.target, "/openai/v1/models");
    assert_eq!(
        openai.header("authorization"),
        Some(format!("Bearer {SECRET}").as_str())
    );
    assert_eq!(openai.header("x-api-key"), None);
    assert_eq!(openai.header("user-agent"), Some(ua));

    let gemini = &requests[2];
    assert_eq!(gemini.method, "GET");
    assert_eq!(gemini.target, "/gemini/v1beta/models?pageSize=1");
    assert_eq!(gemini.header("x-goog-api-key"), Some(SECRET));
    assert_eq!(gemini.header("authorization"), None);
    assert_eq!(gemini.header("user-agent"), Some(ua));
}

#[tokio::test]
async fn la_peticion_a_gemini_no_lleva_la_clave_en_la_url() {
    let env = env().await;
    env.vault
        .add(add_input(Provider::Gemini, SECRET, false))
        .await
        .unwrap();
    let req = &env.server.requests()[0];
    assert!(!req.target.contains(SECRET));
    assert!(!req.target.contains("key="));
    let request_line = req.raw.lines().next().unwrap();
    assert!(!request_line.contains(SECRET));
}

// ---------- probar ----------

#[tokio::test]
async fn test_key_con_veredicto_actualiza_el_estado() {
    let env = env().await;
    preload(&env.store, Provider::Anthropic, SECRET);
    preload(&env.store, Provider::Openai, SECRET);
    preload(&env.store, Provider::Gemini, SECRET);

    env.server.reply(Provider::Openai, Reply::Status(401, "{}"));
    env.server.reply(Provider::Gemini, Reply::Status(403, "{}"));

    let a = env.vault.test(Provider::Anthropic).await.unwrap();
    assert_eq!(a.status, KeyStatus::Valid);
    assert_eq!(a.last_error_code, None);
    assert_eq!(a.last_tested_at.as_deref(), Some(NOW));

    let o = env.vault.test(Provider::Openai).await.unwrap();
    assert_eq!(o.status, KeyStatus::Invalid);
    assert_eq!(o.last_error_code, Some("vault.invalid_key"));

    let g = env.vault.test(Provider::Gemini).await.unwrap();
    assert_eq!(g.status, KeyStatus::Invalid);
    assert_eq!(g.last_error_code, Some("vault.key_restricted"));

    let list = env.vault.list().await.unwrap();
    assert_eq!(list, vec![a, o, g]);
    // Una clave rechazada no se borra al probarla.
    assert_eq!(env.store.refs().len(), 3);
}

#[tokio::test]
async fn test_key_gemini_400_api_key_invalid_es_invalid() {
    let env = env().await;
    preload(&env.store, Provider::Gemini, SECRET);
    env.server.reply(
        Provider::Gemini,
        Reply::Status(
            400,
            r#"{"error":{"details":[{"reason":"API_KEY_INVALID"}]}}"#,
        ),
    );
    let g = env.vault.test(Provider::Gemini).await.unwrap();
    assert_eq!(g.status, KeyStatus::Invalid);
    assert_eq!(g.last_error_code, Some("vault.invalid_key"));
}

#[tokio::test]
async fn test_key_sin_veredicto_no_cambia_el_estado() {
    let cases = [
        (Reply::Status(429, "{}"), "vault.provider_rate_limited"),
        (Reply::Status(500, "{}"), "vault.provider_error"),
        (Reply::Status(502, "{}"), "vault.provider_error"),
        (Reply::Hang, "vault.provider_unreachable"),
    ];
    for (reply, code) in cases {
        let env = env().await;
        // `untested` sigue `untested`.
        preload(&env.store, Provider::Anthropic, SECRET);
        // `valid` sigue `valid`.
        env.vault
            .add(add_input(Provider::Openai, SECRET, false))
            .await
            .unwrap();
        let before = env.vault.list().await.unwrap();
        assert_eq!(before[0].status, KeyStatus::Untested);
        assert_eq!(before[1].status, KeyStatus::Valid);

        env.server.reply(Provider::Anthropic, reply.clone());
        env.server.reply(Provider::Openai, reply.clone());
        let err = env.vault.test(Provider::Anthropic).await.unwrap_err();
        assert_eq!(err.code, code, "{reply:?}");
        let err = env.vault.test(Provider::Openai).await.unwrap_err();
        assert_eq!(err.code, code, "{reply:?}");

        assert_eq!(env.vault.list().await.unwrap(), before, "{reply:?}");
    }
}

#[tokio::test]
async fn test_key_invalid_sigue_invalid_si_despues_no_se_puede_probar() {
    let env = env().await;
    preload(&env.store, Provider::Openai, SECRET);
    env.server.reply(Provider::Openai, Reply::Status(401, "{}"));
    env.vault.test(Provider::Openai).await.unwrap();
    env.server.reply(Provider::Openai, Reply::Status(429, "{}"));
    env.vault.test(Provider::Openai).await.unwrap_err();
    let list = env.vault.list().await.unwrap();
    assert_eq!(list[0].status, KeyStatus::Invalid);
    assert_eq!(list[0].last_error_code, Some("vault.invalid_key"));
}

#[tokio::test]
async fn test_key_sin_clave_es_not_found() {
    let env = env().await;
    let err = env.vault.test(Provider::Gemini).await.unwrap_err();
    assert_eq!(err.code, "vault.not_found");
    assert!(env.server.requests().is_empty());
}

#[tokio::test]
async fn tres_pruebas_simultaneas_no_mezclan_estados_y_se_serializan() {
    let env = env().await;
    for p in Provider::ALL {
        preload(&env.store, p, SECRET);
    }
    let delay = Duration::from_millis(50);
    env.server
        .reply(Provider::Anthropic, Reply::Delayed(200, delay));
    env.server
        .reply(Provider::Openai, Reply::Delayed(401, delay));
    env.server
        .reply(Provider::Gemini, Reply::Delayed(403, delay));

    let tasks: Vec<_> = Provider::ALL
        .into_iter()
        .map(|p| {
            let vault = env.vault.clone();
            tokio::spawn(async move { vault.test(p).await })
        })
        .collect();
    let mut results = Vec::new();
    for task in tasks {
        results.push(task.await.unwrap().unwrap());
    }
    assert_eq!(results[0].provider, Provider::Anthropic);
    assert_eq!(results[0].status, KeyStatus::Valid);
    assert_eq!(results[1].provider, Provider::Openai);
    assert_eq!(results[1].status, KeyStatus::Invalid);
    assert_eq!(results[1].last_error_code, Some("vault.invalid_key"));
    assert_eq!(results[2].provider, Provider::Gemini);
    assert_eq!(results[2].status, KeyStatus::Invalid);
    assert_eq!(results[2].last_error_code, Some("vault.key_restricted"));

    assert_eq!(env.server.requests().len(), 3);
    assert_eq!(env.server.max_active(), 1, "las pruebas deben serializarse");
    assert_eq!(env.vault.list().await.unwrap(), results);
}

// ---------- listar y borrar ----------

#[tokio::test]
async fn list_solo_proveedores_con_clave_en_orden_y_untested_al_iniciar() {
    let env = env().await;
    assert!(env.vault.list().await.unwrap().is_empty());
    preload(&env.store, Provider::Gemini, SECRET_2);
    preload(&env.store, Provider::Anthropic, SECRET);
    let list = env.vault.list().await.unwrap();
    assert_eq!(list.len(), 2);
    assert_eq!(list[0].provider, Provider::Anthropic);
    assert_eq!(list[0].last4, "1a2B");
    assert_eq!(list[1].provider, Provider::Gemini);
    assert_eq!(list[1].last4, "9z8Y");
    for s in &list {
        assert_eq!(s.status, KeyStatus::Untested);
        assert_eq!(s.last_tested_at, None);
        assert_eq!(s.last_error_code, None);
    }
    assert!(env.server.requests().is_empty(), "listar no prueba claves");
}

#[tokio::test]
async fn delete_es_idempotente_y_limpia_el_estado() {
    let env = env().await;
    env.vault.delete(Provider::Openai).await.unwrap();

    env.vault
        .add(add_input(Provider::Openai, SECRET, false))
        .await
        .unwrap();
    env.vault.delete(Provider::Openai).await.unwrap();
    assert!(stored(&env.store, Provider::Openai).is_none());
    assert!(env.vault.list().await.unwrap().is_empty());
    env.vault.delete(Provider::Openai).await.unwrap();

    // Si la clave vuelve a aparecer, ya no arrastra el estado anterior.
    preload(&env.store, Provider::Openai, SECRET);
    let list = env.vault.list().await.unwrap();
    assert_eq!(list[0].status, KeyStatus::Untested);
    assert_eq!(list[0].last_tested_at, None);
}

#[tokio::test]
async fn llavero_no_disponible_en_cada_operacion() {
    let env = env().await;
    env.store.set_unavailable(true);
    assert_eq!(
        env.vault.list().await.unwrap_err().code,
        "vault.keyring_unavailable"
    );
    assert_eq!(
        env.vault
            .add(add_input(Provider::Anthropic, SECRET, false))
            .await
            .unwrap_err()
            .code,
        "vault.keyring_unavailable"
    );
    assert_eq!(
        env.vault.test(Provider::Anthropic).await.unwrap_err().code,
        "vault.keyring_unavailable"
    );
    assert_eq!(
        env.vault
            .delete(Provider::Anthropic)
            .await
            .unwrap_err()
            .code,
        "vault.keyring_unavailable"
    );
    assert!(env.server.requests().is_empty());
}

// ---------- forma serde y secretos ----------

#[tokio::test]
async fn key_summary_serializa_como_el_contrato_ts() {
    let env = env().await;
    preload(&env.store, Provider::Openai, SECRET);
    env.server.reply(Provider::Openai, Reply::Status(403, "{}"));
    let summary = env.vault.test(Provider::Openai).await.unwrap();
    assert_eq!(
        serde_json::to_value(&summary).unwrap(),
        json!({
            "provider": "openai",
            "secret_ref": "llm/openai/default",
            "last4": "1a2B",
            "status": "invalid",
            "last_tested_at": NOW,
            "last_error_code": "vault.key_restricted",
        })
    );
    let list = env.vault.list().await.unwrap();
    assert_eq!(serde_json::to_value(&list).unwrap()[0]["status"], "invalid");

    env.vault.delete(Provider::Openai).await.unwrap();
    preload(&env.store, Provider::Gemini, SECRET);
    let list = serde_json::to_value(env.vault.list().await.unwrap()).unwrap();
    assert_eq!(
        list,
        json!([{
            "provider": "gemini",
            "secret_ref": "llm/gemini/default",
            "last4": "1a2B",
            "status": "untested",
            "last_tested_at": null,
            "last_error_code": null,
        }])
    );
}

#[test]
fn provider_serde_y_referencias() {
    for (p, s) in [
        (Provider::Anthropic, "anthropic"),
        (Provider::Openai, "openai"),
        (Provider::Gemini, "gemini"),
    ] {
        assert_eq!(serde_json::to_value(p).unwrap(), json!(s));
        assert_eq!(serde_json::from_value::<Provider>(json!(s)).unwrap(), p);
        assert_eq!(p.as_str(), s);
        assert_eq!(p.secret_ref(), format!("llm/{s}/default"));
    }
    assert!(serde_json::from_value::<Provider>(json!("open_ai")).is_err());
}

#[test]
fn vault_service_debug_no_expone_nada() {
    let store = Arc::new(MemoryStore::new());
    preload(&store, Provider::Anthropic, SECRET);
    let checker =
        Arc::new(HttpProviderChecker::for_tests(BaseUrls::production(), TIMEOUT).unwrap());
    let vault = VaultService::new(store, checker, Arc::new(|| NOW.to_owned()));
    assert_no_secret(&format!("{vault:?}"));
}

#[tokio::test]
async fn test_key_con_caracteres_no_validos_en_cabecera_es_invalid_sin_peticion() {
    // Solo puede pasar con una clave guardada fuera de Faro: un salto de línea no cabe
    // en una cabecera HTTP, así que no puede ser válida y no se envía nada.
    let env = env().await;
    for p in Provider::ALL {
        preload(
            &env.store,
            p,
            "test-key-con-salto
de-linea-0001",
        );
        let summary = env.vault.test(p).await.unwrap();
        assert_eq!(summary.status, KeyStatus::Invalid, "{p:?}");
        assert_eq!(summary.last_error_code, Some("vault.invalid_key"), "{p:?}");
    }
    assert!(env.server.requests().is_empty());
}

/// Clase del error de red en el log (`kind`), sin URL ni detalles de `reqwest`.
async fn unreachable_kind(bases: BaseUrls) -> String {
    let (vault, store) = env_with_bases(bases);
    let (buffer, _guard) = crate::test_logs::capture();
    let err = vault
        .add(add_input(Provider::Openai, SECRET, false))
        .await
        .unwrap_err();
    assert_eq!(err.code, "vault.provider_unreachable");
    assert!(stored(&store, Provider::Openai).is_none());
    let all = buffer.text();
    assert_no_secret(&all);
    // Solo los registros de Faro: la captura incluye también las trazas de hyper.
    let logs: Vec<&str> = all.lines().filter(|l| l.contains("faro_lib::")).collect();
    let line = logs
        .iter()
        .find(|l| l.contains("sin conexión con el proveedor"))
        .unwrap_or_else(|| panic!("falta el aviso: {all}"));
    for l in &logs {
        assert!(!l.contains("127.0.0.1"), "el log no lleva la URL: {l}");
    }
    // `… kind="connect"` → `connect`.
    let start = line.find("kind=\"").expect("falta kind") + "kind=\"".len();
    line[start..]
        .split('"')
        .next()
        .unwrap_or_default()
        .to_owned()
}

#[tokio::test]
async fn sin_conexion_registra_la_clase_connect() {
    // El puerto 0 no admite conexiones: el SO rechaza la conexión al instante.
    let root = "http://127.0.0.1:0".to_owned();
    let kind = unreachable_kind(BaseUrls {
        anthropic: root.clone(),
        openai: root.clone(),
        gemini: root,
    })
    .await;
    assert_eq!(kind, "connect");
}

#[tokio::test]
async fn conexion_cerrada_sin_respuesta_registra_la_clase_other() {
    let server = FakeProviders::start().await;
    server.reply(Provider::Openai, Reply::Close);
    let kind = unreachable_kind(server.bases()).await;
    assert_eq!(kind, "other");
    assert_eq!(server.requests().len(), 1);
}

#[test]
fn system_clock_da_utc_en_rfc3339_con_segundos() {
    let now = super::system_clock()();
    assert!(now.ends_with('Z'), "{now}");
    let parsed = chrono::DateTime::parse_from_rfc3339(&now).unwrap();
    assert_eq!(parsed.timestamp_subsec_nanos(), 0);
}

#[test]
fn vault_service_system_se_construye_sin_usar_el_llavero() {
    // Crear el servicio no crea entradas ni llama al llavero: solo construye
    // `KeyringStore` y el cliente HTTP. Aquí no se ejecuta ninguna operación.
    let vault = VaultService::system().unwrap();
    assert!(!format!("{vault:?}").is_empty());
}

// ---------- logs ----------

#[tokio::test]
async fn ningun_log_contiene_la_clave() {
    let (buffer, _guard) = crate::test_logs::capture();

    let env = env().await;
    for p in Provider::ALL {
        env.vault.add(add_input(p, SECRET, false)).await.unwrap();
    }
    env.server.reply(Provider::Openai, Reply::Status(401, "{}"));
    env.vault.test(Provider::Openai).await.unwrap();
    env.server
        .reply(Provider::Openai, Reply::Status(500, "secreto del cuerpo"));
    env.vault.test(Provider::Openai).await.unwrap_err();
    env.server.reply(Provider::Gemini, Reply::Hang);
    env.vault.test(Provider::Gemini).await.unwrap_err();
    env.vault
        .add(add_input(
            Provider::Anthropic,
            "clave con espacios y más texto",
            true,
        ))
        .await
        .unwrap_err();
    env.vault.list().await.unwrap();
    env.vault.delete(Provider::Anthropic).await.unwrap();

    let logs = buffer.text();
    assert!(logs.contains("clave probada"), "se esperaban logs: {logs}");
    assert_no_secret(&logs);
    assert!(!logs.contains("secreto del cuerpo"));
    assert!(!logs.contains("clave con espacios"));
    assert!(!logs.to_ascii_lowercase().contains("bearer"));
}

// ---------- auditoría de la Bóveda (F1a T8, ADR 0010 §4) ----------

#[tokio::test]
async fn cada_operacion_deja_un_evento_de_auditoria_sin_la_clave() {
    use crate::engine::supervisor::StdinWriter;
    use crate::secrets::audit::AuditQueue;
    use tokio::io::AsyncBufReadExt;

    let server = FakeProviders::start().await;
    let store = Arc::new(MemoryStore::new());
    let checker = Arc::new(HttpProviderChecker::for_tests(server.bases(), TIMEOUT).unwrap());
    let audit = AuditQueue::spawn(&tokio::runtime::Handle::current());
    let (core, engine) = tokio::io::duplex(1024 * 1024);
    audit.attach(StdinWriter::spawn(Box::new(core)));
    let vault = VaultService::new(store.clone(), checker, Arc::new(|| NOW.to_owned()))
        .with_audit(audit.clone());

    let p = Provider::Openai;
    vault.add(add_input(p, SECRET, false)).await.unwrap();
    vault.add(add_input(p, SECRET_2, true)).await.unwrap();
    vault.add(add_input(p, SECRET, false)).await.unwrap_err();
    vault.add(add_input(p, "corta", true)).await.unwrap_err();
    vault.test(p).await.unwrap();
    server.reply(p, Reply::Status(401, "{}"));
    vault.test(p).await.unwrap();
    vault.delete(p).await.unwrap();
    vault.test(p).await.unwrap_err();
    store.set_unavailable(true);
    vault.delete(p).await.unwrap_err();

    assert_eq!(audit.sync().await, 0);
    let expected = [
        ("secret.added", "ok", None),
        ("secret.replaced", "ok", None),
        ("secret.added", "error", Some("vault.already_exists")),
        ("secret.replaced", "error", Some("vault.invalid_input")),
        ("secret.tested", "ok", None),
        ("secret.tested", "error", Some("vault.invalid_key")),
        ("secret.deleted", "ok", None),
        ("secret.tested", "error", Some("vault.not_found")),
        ("secret.deleted", "error", Some("vault.keyring_unavailable")),
    ];
    let mut reader = tokio::io::BufReader::new(engine);
    for (action, result, code) in expected {
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        assert_no_secret(&line);
        assert!(!line.contains("last4") && !line.contains("1a2B") && !line.contains("9z8Y"));
        let event: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(event["event"], "audit");
        assert_eq!(event["actor"], "user");
        assert_eq!(event["action"], action, "{line}");
        assert_eq!(event["result"], result, "{line}");
        assert_eq!(event["secret_ref"], "llm/openai/default");
        assert_eq!(event["run_id"], serde_json::Value::Null);
        assert_eq!(event["details"]["provider"], "openai");
        match code {
            Some(code) => assert_eq!(event["details"]["error_code"], code, "{line}"),
            None => assert!(event["details"].get("error_code").is_none(), "{line}"),
        }
    }
}

/// Revisión de seguridad de T5: saber qué proveedores tienen clave no lee su valor.
#[test]
fn providers_with_key_no_lee_el_valor_de_las_claves() {
    use crate::error::AppError;

    struct NoRead(MemoryStore);
    impl SecretStore for NoRead {
        fn get(&self, _: &str) -> Result<Option<SecretString>, AppError> {
            panic!("providers_with_key no debe leer el valor");
        }
        fn set(&self, secret_ref: &str, secret: &SecretString) -> Result<(), AppError> {
            self.0.set(secret_ref, secret)
        }
        fn delete(&self, secret_ref: &str) -> Result<(), AppError> {
            self.0.delete(secret_ref)
        }
        fn exists(&self, secret_ref: &str) -> Result<bool, AppError> {
            self.0.exists(secret_ref)
        }
    }
    let store = NoRead(MemoryStore::new());
    assert!(VaultService::providers_with_key(&store).is_empty());
    for provider in [Provider::Openai, Provider::Gemini] {
        store
            .set(
                provider.secret_ref(),
                &SecretString::from("test-clave-ficticia-0000000000".to_owned()),
            )
            .unwrap();
    }
    assert_eq!(
        VaultService::providers_with_key(&store),
        [Provider::Openai, Provider::Gemini]
    );
    // Un llavero caído cuenta como sin clave.
    store.0.set_unavailable(true);
    assert!(VaultService::providers_with_key(&store).is_empty());
}
