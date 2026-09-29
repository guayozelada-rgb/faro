// Evita la consola extra en Windows en builds de release. NO QUITAR.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if let Err(err) = faro_lib::run() {
        // Si el registro ya estaba iniciado, queda en faro.log; si no, al menos sale por stderr.
        tracing::error!(error = %err, "la aplicación terminó con un error");
        eprintln!("Faro terminó con un error: {err}");
        std::process::exit(1);
    }
}
