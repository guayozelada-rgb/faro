//! Comandos Tauri de Faro, un módulo por dominio.
//!
//! Cada comando nuevo:
//! 1. vive en `commands/<dominio>.rs` y devuelve `Result<T, crate::error::AppError>`;
//! 2. se registra en `register_commands` (`lib.rs`) y en `COMMANDS` de `build.rs`;
//! 3. tiene su permiso en `permissions/<dominio>.toml` y se agrega a `capabilities/main.json`.
//!
//! `build.rs` hace fallar la compilación si los pasos 2 y 3 no coinciden.

pub mod agents;
pub mod engine;
pub mod vault;
pub mod wp_plugin;
