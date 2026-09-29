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
    get_ipc_response(
        window,
        InvokeRequest {
            cmd: cmd.into(),
            callback: CallbackFn(0),
            error: CallbackFn(1),
            url: url.parse().unwrap(),
            body: InvokeBody::default(),
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
