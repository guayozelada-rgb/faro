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

/// Las mismas comprobaciones que `build.rs`, ejecutadas en cada `cargo test` aunque
/// cargo no vuelva a correr el script de compilación.
mod config {
    #![allow(dead_code)]
    include!("../acl_checks.rs");

    use std::path::PathBuf;

    fn manifest_dir() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
    }

    fn copy_dir(from: &Path, to: &Path) {
        fs::create_dir_all(to).unwrap();
        for entry in fs::read_dir(from).unwrap().flatten() {
            let path = entry.path();
            let target = to.join(entry.file_name());
            if path.is_dir() {
                copy_dir(&path, &target);
            } else {
                fs::copy(&path, &target).unwrap();
            }
        }
    }

    /// Copia de lo que lee `check_acl` en una carpeta temporal.
    fn copy_of_real_config() -> tempfile::TempDir {
        let tmp = tempfile::tempdir().unwrap();
        let root = manifest_dir();
        copy_dir(&root.join("src"), &tmp.path().join("src"));
        copy_dir(&root.join("capabilities"), &tmp.path().join("capabilities"));
        fs::create_dir_all(tmp.path().join("permissions")).unwrap();
        for entry in fs::read_dir(root.join("permissions")).unwrap().flatten() {
            if entry.path().is_file() {
                fs::copy(
                    entry.path(),
                    tmp.path().join("permissions").join(entry.file_name()),
                )
                .unwrap();
            }
        }
        fs::copy(
            root.join("tauri.conf.json"),
            tmp.path().join("tauri.conf.json"),
        )
        .unwrap();
        tmp
    }

    fn edit_capability(root: &Path, edit: impl FnOnce(&mut serde_json::Value)) {
        let path = root.join("capabilities").join(CAPABILITY_FILE);
        let mut value: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        edit(&mut value);
        fs::write(&path, serde_json::to_string_pretty(&value).unwrap()).unwrap();
    }

    #[test]
    fn la_configuracion_real_pasa_las_comprobaciones() {
        let problems = check_acl(&manifest_dir());
        assert!(problems.is_empty(), "{problems:#?}");
        let copy = copy_of_real_config();
        let problems = check_acl(copy.path());
        assert!(problems.is_empty(), "{problems:#?}");
    }

    #[test]
    fn configuraciones_de_tauri_por_plataforma_o_alternativas_fallan() {
        for name in [
            "tauri.windows.conf.json",
            "tauri.macos.conf.json5",
            "tauri.conf.json5",
            "Tauri.toml",
            "Tauri.linux.toml",
        ] {
            let copy = copy_of_real_config();
            fs::write(copy.path().join(name), "{}").unwrap();
            let problems = check_acl(copy.path());
            assert!(
                problems.iter().any(|p| p.contains(name)),
                "{name}: {problems:#?}"
            );
        }
    }

    #[test]
    fn capability_con_claves_o_ventanas_no_permitidas_falla() {
        type Edit = fn(&mut serde_json::Value);
        let cases: [(&str, Edit); 7] = [
            ("remote", |v| {
                v["remote"] = serde_json::json!({ "urls": ["https://ejemplo.com"] })
            }),
            ("webviews", |v| v["webviews"] = serde_json::json!(["main"])),
            ("platforms", |v| {
                v["platforms"] = serde_json::json!(["windows"])
            }),
            ("local", |v| v["local"] = serde_json::json!(false)),
            ("identifier", |v| {
                v["identifier"] = serde_json::json!("otra")
            }),
            ("windows", |v| {
                v["windows"] = serde_json::json!(["main", "otra"])
            }),
            ("windows", |v| v["windows"] = serde_json::json!(["*"])),
        ];
        for (key, edit) in cases {
            let copy = copy_of_real_config();
            edit_capability(copy.path(), edit);
            let problems = check_acl(copy.path());
            assert!(
                problems.iter().any(|p| p.contains(&format!("`{key}`"))),
                "{key}: {problems:#?}"
            );
        }
    }

    #[test]
    fn core_default_y_capabilities_extra_fallan() {
        let copy = copy_of_real_config();
        edit_capability(copy.path(), |v| {
            v["permissions"]
                .as_array_mut()
                .unwrap()
                .push(serde_json::json!("core:default"))
        });
        let problems = check_acl(copy.path());
        assert!(
            problems.iter().any(|p| p.contains("core:default")),
            "{problems:#?}"
        );

        let copy = copy_of_real_config();
        fs::write(copy.path().join("capabilities").join("extra.json"), "{}").unwrap();
        let problems = check_acl(copy.path());
        assert!(
            problems.iter().any(|p| p.contains("extra.json")),
            "{problems:#?}"
        );

        let copy = copy_of_real_config();
        let conf = copy.path().join("tauri.conf.json");
        let text = fs::read_to_string(&conf).unwrap().replace(
            r#""capabilities": ["main"]"#,
            r#""capabilities": ["main", "otra"]"#,
        );
        fs::write(&conf, text).unwrap();
        let problems = check_acl(copy.path());
        assert!(
            problems
                .iter()
                .any(|p| p.contains("app.security.capabilities")),
            "{problems:#?}"
        );
    }
}
