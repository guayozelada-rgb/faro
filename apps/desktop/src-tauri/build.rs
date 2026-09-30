//! Script de compilación del núcleo.
//!
//! Declara explícitamente los comandos propios en el manifiesto de la app (`__app-acl__`).
//! Así Tauri aplica SIEMPRE el control de acceso a los comandos propios, aunque
//! desaparezcan los `.toml` de `permissions/`: sin manifiesto de app, Tauri deja pasar
//! cualquier comando de `generate_handler!` desde un origen local sin mirar capabilities.
//!
//! Además comprueba, antes de compilar, que la lista de comandos, `src/lib.rs`, los
//! permisos escritos a mano y la capability `main` coinciden, que `main` es la única
//! capability (carpeta y `tauri.conf.json`) y que no concede permisos ajenos fuera de
//! `ALLOWED_FOREIGN_PERMISSIONS`. Si no, la compilación falla.
//!
//! También revalida `packages/shared/engine-operations.json` (lista permitida de
//! `engine_call` y concesiones de secretos, ADR 0010 §3) con las mismas reglas que
//! `ACCESS_BY_KIND` del motor: un archivo inválido no llega a incrustarse en el núcleo.

include!("acl_checks.rs");
include!("operations_checks.rs");

/// Lista permitida generada por `npm run contracts` (se incrusta en `src/secrets/operations.rs`).
const ENGINE_OPERATIONS: &str = "../../../packages/shared/engine-operations.json";

fn main() {
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=permissions");
    println!("cargo:rerun-if-changed=capabilities");
    println!("cargo:rerun-if-changed=tauri.conf.json");
    println!("cargo:rerun-if-changed=acl_checks.rs");
    println!("cargo:rerun-if-changed=operations_checks.rs");
    println!("cargo:rerun-if-changed={ENGINE_OPERATIONS}");

    let problems = check_acl(Path::new("."));
    if !problems.is_empty() {
        panic!(
            "El ACL de los comandos propios no es consistente:\n  - {}",
            problems.join("\n  - ")
        );
    }

    let operations = match std::fs::read_to_string(ENGINE_OPERATIONS) {
        Ok(text) => text,
        Err(error) => panic!("no se pudo leer {ENGINE_OPERATIONS}: {error}"),
    };
    if let Err(problems) = parse_operations(&operations) {
        panic!(
            "engine-operations.json no cumple las reglas de concesiones (ADR 0010 §3):
  - {}",
            problems.join(
                "
  - "
            )
        );
    }

    // tauri-build solo incrusta el manifiesto de Windows (Common Controls v6) en los
    // binarios. Las pruebas que construyen una app simulada (`tests/acl.rs`) lo
    // necesitan también o no arrancan (STATUS_ENTRYPOINT_NOT_FOUND).
    let target_os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let target_env = std::env::var("CARGO_CFG_TARGET_ENV").unwrap_or_default();
    if target_os == "windows" && target_env == "msvc" {
        println!("cargo:rustc-link-arg-tests=/MANIFEST:EMBED");
        println!(
            "cargo:rustc-link-arg-tests=/MANIFESTDEPENDENCY:type='win32' name='Microsoft.Windows.Common-Controls' version='6.0.0.0' processorArchitecture='*' publicKeyToken='6595b64144ccf1df' language='*'"
        );
    }

    let attributes = tauri_build::Attributes::new()
        .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS));
    if let Err(error) = tauri_build::try_build(attributes) {
        panic!("tauri-build falló: {error:#}");
    }
}
