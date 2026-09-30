//! Control de acceso (ACL) de los comandos propios con el contexto REAL de la app
//! (`tauri.conf.json`, `capabilities/main.json`, `permissions/*.toml` y el manifiesto
//! `__app-acl__` que genera `build.rs`), sobre el runtime simulado de Tauri.
//!
//! Solo se comprueba la decisión del ACL: sin `AppState` los comandos permitidos fallan
//! después del ACL (estado no registrado), nunca tocan el motor ni el llavero.
// Archivo solo de pruebas: los ayudantes fuera de `#[test]` también pueden fallar con panic.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use tauri::ipc::{CallbackFn, InvokeBody};
use tauri::test::{get_ipc_response, mock_builder, MockRuntime, INVOKE_KEY};
use tauri::webview::InvokeRequest;
use tauri::{App, Manager, WebviewWindow, WebviewWindowBuilder};

const COMMANDS: [&str; 6] = [
    "engine_status",
    "engine_restart",
    "vault_list_keys",
    "vault_add_key",
    "vault_test_key",
    "vault_delete_key",
];

/// Origen local en desarrollo (`build.devUrl`); las pruebas no activan `custom-protocol`.
const LOCAL_URL: &str = "http://localhost:1420/";

/// Comando de prueba registrado en el handler pero sin permiso en ninguna capability.
#[tauri::command]
fn faro_prueba_sin_permiso() -> &'static str {
    "no debería ejecutarse"
}

fn real_app() -> App<MockRuntime> {
    faro_lib::register_commands(mock_builder())
        .build(tauri::generate_context!())
        .expect("app simulada")
}

fn window(app: &App<MockRuntime>, label: &str) -> WebviewWindow<MockRuntime> {
    match app.get_webview_window(label) {
        Some(window) => window,
        None => WebviewWindowBuilder::new(app, label, Default::default())
            .build()
            .expect("ventana simulada"),
    }
}

fn invoke(
    window: &WebviewWindow<MockRuntime>,
    cmd: &str,
    url: &str,
) -> Result<serde_json::Value, serde_json::Value> {
    invoke_with(window, cmd, url, serde_json::Value::Null)
}

fn invoke_with(
    window: &WebviewWindow<MockRuntime>,
    cmd: &str,
    url: &str,
    body: serde_json::Value,
) -> Result<serde_json::Value, serde_json::Value> {
    get_ipc_response(
        window,
        InvokeRequest {
            cmd: cmd.into(),
            callback: CallbackFn(0),
            error: CallbackFn(1),
            url: url.parse().unwrap(),
            body: if body.is_null() {
                InvokeBody::default()
            } else {
                InvokeBody::Json(body)
            },
            headers: Default::default(),
            invoke_key: INVOKE_KEY.to_string(),
        },
    )
    .map(|body| body.deserialize::<serde_json::Value>().unwrap())
}

fn rejected_by_acl(result: &Result<serde_json::Value, serde_json::Value>) -> bool {
    match result {
        Ok(_) => false,
        Err(error) => error.to_string().contains("not allowed"),
    }
}

#[test]
fn comandos_propios_pasan_el_acl_desde_main() {
    let app = real_app();
    let main = window(&app, "main");
    for cmd in COMMANDS {
        let result = invoke(&main, cmd, LOCAL_URL);
        assert!(
            !rejected_by_acl(&result),
            "`{cmd}` rechazado por el ACL: {result:?}"
        );
        // Pasó el ACL y falló después, por falta de estado: nunca se ejecutó de verdad.
        assert!(
            result.is_err(),
            "`{cmd}` no debería ejecutarse sin AppState"
        );
    }
}

#[test]
fn comando_sin_permiso_es_rechazado() {
    let app = mock_builder()
        .invoke_handler(tauri::generate_handler![faro_prueba_sin_permiso])
        .build(tauri::generate_context!())
        .expect("app simulada");
    let main = window(&app, "main");
    let result = invoke(&main, "faro_prueba_sin_permiso", LOCAL_URL);
    assert!(rejected_by_acl(&result), "{result:?}");
}

#[test]
fn comando_desconocido_es_rechazado() {
    let app = real_app();
    let main = window(&app, "main");
    let result = invoke(&main, "vault_get_secret", LOCAL_URL);
    assert!(rejected_by_acl(&result), "{result:?}");
}

#[test]
fn otra_ventana_no_tiene_permisos() {
    let app = real_app();
    let other = window(&app, "otra");
    for cmd in COMMANDS {
        let result = invoke(&other, cmd, LOCAL_URL);
        assert!(rejected_by_acl(&result), "`{cmd}`: {result:?}");
    }
}

#[test]
fn origen_remoto_es_rechazado() {
    let app = real_app();
    let main = window(&app, "main");
    for cmd in COMMANDS {
        let result = invoke(&main, cmd, "https://ejemplo-malicioso.com/");
        assert!(rejected_by_acl(&result), "`{cmd}`: {result:?}");
    }
}

/// Lo que hace `listen()` de `@tauri-apps/api/event` al escuchar `engine://status`.
fn listen_body() -> serde_json::Value {
    serde_json::json!({
        "event": "engine://status",
        "target": { "kind": "Any" },
        "handler": 7
    })
}

#[test]
fn listen_y_unlisten_de_eventos_funcionan_desde_main() {
    let app = real_app();
    let main = window(&app, "main");
    let listened = invoke_with(&main, "plugin:event|listen", LOCAL_URL, listen_body());
    let event_id = listened
        .as_ref()
        .ok()
        .and_then(serde_json::Value::as_u64)
        .unwrap_or_else(|| panic!("listen debería funcionar: {listened:?}"));
    let unlistened = invoke_with(
        &main,
        "plugin:event|unlisten",
        LOCAL_URL,
        serde_json::json!({ "event": "engine://status", "eventId": event_id }),
    );
    assert!(
        unlistened.is_ok(),
        "unlisten debería funcionar: {unlistened:?}"
    );
}

#[test]
fn listen_desde_otra_ventana_u_origen_remoto_es_rechazado() {
    let app = real_app();
    let other = window(&app, "otra");
    let result = invoke_with(&other, "plugin:event|listen", LOCAL_URL, listen_body());
    assert!(rejected_by_acl(&result), "{result:?}");
    let main = window(&app, "main");
    let result = invoke_with(
        &main,
        "plugin:event|listen",
        "https://ejemplo-malicioso.com/",
        listen_body(),
    );
    assert!(rejected_by_acl(&result), "{result:?}");
}

/// Sin `core:default`: los permisos de core que la interfaz no usa se rechazan.
#[test]
fn permisos_de_core_no_concedidos_son_rechazados() {
    let app = real_app();
    let main = window(&app, "main");
    let cases = [
        // La interfaz no emite eventos: solo el núcleo emite `engine://status`.
        (
            "plugin:event|emit",
            serde_json::json!({ "event": "engine://status", "payload": { "state": "ready" } }),
        ),
        (
            "plugin:window|title",
            serde_json::json!({ "label": "main" }),
        ),
        (
            "plugin:window|close",
            serde_json::json!({ "label": "main" }),
        ),
        (
            "plugin:webview|print",
            serde_json::json!({ "label": "main" }),
        ),
        (
            "plugin:image|new",
            serde_json::json!({ "rgba": [0, 0, 0, 0], "width": 1, "height": 1 }),
        ),
        ("plugin:app|version", serde_json::Value::Null),
        (
            "plugin:path|resolve_directory",
            serde_json::json!({ "directory": 1 }),
        ),
        ("plugin:menu|new", serde_json::json!({ "kind": "Menu" })),
        ("plugin:tray|new", serde_json::json!({ "options": {} })),
        ("plugin:resources|close", serde_json::json!({ "rid": 1 })),
    ];
    for (cmd, body) in cases {
        let result = invoke_with(&main, cmd, LOCAL_URL, body);
        assert!(
            rejected_by_acl(&result),
            "`{cmd}` no debería pasar el ACL: {result:?}"
        );
    }
}
