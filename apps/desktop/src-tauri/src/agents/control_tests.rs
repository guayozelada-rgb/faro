//! Pruebas de la pausa global: archivo ausente, válido e ilegible, escritura atómica,
//! fallo al guardar, aviso al motor y auditoría. Solo carpetas temporales y `MemoryStore`.

use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader, DuplexStream};
use tokio::time::{timeout, Duration};

use super::*;

const WAIT: Duration = Duration::from_secs(5);

struct Setup {
    dir: tempfile::TempDir,
    broker: Arc<SecretBroker>,
    audit_lines: BufReader<DuplexStream>,
    providers: Arc<Mutex<Vec<Provider>>>,
    provider_reads: Arc<AtomicUsize>,
}

fn setup_dir() -> tempfile::TempDir {
    tempfile::tempdir().unwrap()
}

fn setup_with(dir: tempfile::TempDir) -> (Setup, Arc<AgentsControl>) {
    let audit = AuditQueue::spawn(&tokio::runtime::Handle::current());
    let (core, engine) = tokio::io::duplex(256 * 1024);
    audit.attach(StdinWriter::spawn(Box::new(core)));
    let broker = Arc::new(SecretBroker::new(
        Arc::new(crate::vault::store::MemoryStore::new()),
        dir.path(),
        audit,
    ));
    let providers = Arc::new(Mutex::new(vec![Provider::Anthropic]));
    let provider_reads = Arc::new(AtomicUsize::new(0));
    let (list, reads) = (Arc::clone(&providers), Arc::clone(&provider_reads));
    let source: ProviderSource = Arc::new(move || {
        reads.fetch_add(1, Ordering::SeqCst);
        list.lock().unwrap().clone()
    });
    let control = AgentsControl::load(
        &tokio::runtime::Handle::current(),
        dir.path(),
        Arc::clone(&broker),
        source,
    );
    (
        Setup {
            dir,
            broker,
            audit_lines: BufReader::new(engine),
            providers,
            provider_reads,
        },
        control,
    )
}

impl Setup {
    fn file(&self) -> PathBuf {
        self.dir.path().join(CONTROL_FILE)
    }

    fn read_file(&self) -> Value {
        serde_json::from_slice(&fs::read(self.file()).unwrap()).unwrap()
    }

    async fn audit_of(&mut self, action: &str) -> Value {
        loop {
            let mut line = String::new();
            timeout(WAIT, self.audit_lines.read_line(&mut line))
                .await
                .expect("no llegó la auditoría")
                .unwrap();
            let event: Value = serde_json::from_str(&line).unwrap();
            if event["action"] == action {
                return event;
            }
        }
    }
}

/// Motor falso: lo que el núcleo escribe en su stdin.
fn engine_pipe() -> (StdinWriter, BufReader<DuplexStream>) {
    let (core, engine) = tokio::io::duplex(64 * 1024);
    (StdinWriter::spawn(Box::new(core)), BufReader::new(engine))
}

async fn next_line(reader: &mut BufReader<DuplexStream>) -> Value {
    let mut line = String::new();
    timeout(WAIT, reader.read_line(&mut line))
        .await
        .expect("no llegó agents_control")
        .unwrap();
    assert!(line.ends_with('\n'));
    serde_json::from_str(&line).unwrap()
}

// ---------- Estado guardado ----------

#[tokio::test]
async fn archivo_ausente_es_activo() {
    let (s, control) = setup_with(setup_dir());
    assert_eq!(
        control.state(),
        ControlState {
            paused: false,
            changed_at: None,
            saved: true,
        }
    );
    assert!(!s.broker.agents_paused());
    assert!(!s.file().exists(), "no se crea el archivo hasta que cambia");
    assert_eq!(
        serde_json::to_value(control.state()).unwrap(),
        json!({"paused": false, "changed_at": null, "saved": true})
    );
}

#[tokio::test]
async fn archivo_valido_se_respeta() {
    for paused in [true, false] {
        let dir = setup_dir();
        fs::write(
            dir.path().join(CONTROL_FILE),
            json!({"version": 1, "paused": paused, "changed_at": "2026-10-08T12:00:00.000Z"})
                .to_string(),
        )
        .unwrap();
        let (s, control) = setup_with(dir);
        assert_eq!(
            control.state(),
            ControlState {
                paused,
                changed_at: Some("2026-10-08T12:00:00.000Z".into()),
                saved: true,
            }
        );
        assert_eq!(s.broker.agents_paused(), paused);
    }
}

#[tokio::test]
async fn archivo_ilegible_cuenta_como_pausado_y_no_se_sobrescribe() {
    let (logs, _guard) = crate::test_logs::capture();
    let bad_files = [
        "no es json".to_owned(),
        "{}".to_owned(),
        json!({"version": 2, "paused": false, "changed_at": "2026-10-08T12:00:00Z"}).to_string(),
        json!({"version": 1, "paused": "no", "changed_at": "2026-10-08T12:00:00Z"}).to_string(),
        json!({"version": 1, "paused": false, "changed_at": "ayer"}).to_string(),
        json!({"version": 1, "paused": false, "changed_at": "2026-10-08T12:00:00Z", "x": 1})
            .to_string(),
        r#"{"version":1,"paused":false,"paused":true,"changed_at":"2026-10-08T12:00:00Z"}"#
            .to_owned(),
    ];
    for bad in bad_files {
        let dir = setup_dir();
        fs::write(dir.path().join(CONTROL_FILE), &bad).unwrap();
        let (s, control) = setup_with(dir);
        assert_eq!(
            control.state(),
            ControlState {
                paused: true,
                changed_at: None,
                // Al volver a abrir seguirá en pausa: nada que avisar.
                saved: true,
            },
            "{bad}"
        );
        assert!(s.broker.agents_paused(), "{bad}");
        assert_eq!(fs::read_to_string(s.file()).unwrap(), bad);
    }
    // El archivo es una carpeta: no se puede leer → pausado.
    let dir = setup_dir();
    fs::create_dir(dir.path().join(CONTROL_FILE)).unwrap();
    let (s, control) = setup_with(dir);
    assert!(control.state().paused);
    assert!(s.broker.agents_paused());
    assert!(logs.text().contains("agentes en pausa"), "{}", logs.text());
}

// ---------- Pausar y reanudar ----------

#[tokio::test]
async fn pausar_guarda_revoca_avisa_y_audita() {
    let (mut s, control) = setup_with(setup_dir());
    let (writer, mut engine) = engine_pipe();
    control.attach(writer);
    // Tras `ready` el motor recibe el estado actual.
    assert_eq!(
        next_line(&mut engine).await,
        json!({"event": "agents_control", "paused": false, "llm_providers": ["anthropic"]})
    );

    let state = control.pause().await.unwrap();
    assert!(state.paused);
    assert!(state.saved);
    assert_eq!(control.state(), state);
    let changed_at = state.changed_at.clone().unwrap();
    assert!(crate::agents::activity::is_utc_timestamp(&changed_at));
    // 1. Guardado de forma atómica (sin temporal).
    assert_eq!(
        s.read_file(),
        json!({"version": 1, "paused": true, "changed_at": changed_at})
    );
    assert!(!s.dir.path().join("agents-control.json.tmp").exists());
    // 2–3. Concesiones de ejecución rechazadas.
    assert!(s.broker.agents_paused());
    // 4. Aviso al motor.
    assert_eq!(
        next_line(&mut engine).await,
        json!({"event": "agents_control", "paused": true, "llm_providers": ["anthropic"]})
    );
    // 5. Auditoría con actor `user`.
    let event = s.audit_of("agents.paused").await;
    assert_eq!(event["actor"], "user");
    assert_eq!(event["result"], "ok");
    assert_eq!(event["details"], json!({}));

    // Pausar otra vez conserva cuándo empezó.
    assert_eq!(control.pause().await.unwrap().changed_at, Some(changed_at));
    next_line(&mut engine).await;

    let resumed = control.resume().await.unwrap();
    assert!(!resumed.paused);
    assert!(resumed.saved);
    assert!(!s.broker.agents_paused());
    assert_eq!(s.read_file()["paused"], false);
    assert_eq!(
        next_line(&mut engine).await,
        json!({"event": "agents_control", "paused": false, "llm_providers": ["anthropic"]})
    );
    let event = s.audit_of("agents.resumed").await;
    assert_eq!(event["actor"], "user");
    assert_eq!(event["result"], "ok");
    assert_eq!(control.state(), resumed);
}

#[tokio::test]
async fn la_pausa_sobrevive_a_un_reinicio_de_la_app() {
    let (s, control) = setup_with(setup_dir());
    let paused = control.pause().await.unwrap();
    // Otra instancia (app reiniciada) sobre la misma carpeta, con un canal nuevo.
    let broker = Arc::new(SecretBroker::new(
        Arc::new(crate::vault::store::MemoryStore::new()),
        s.dir.path(),
        AuditQueue::spawn(&tokio::runtime::Handle::current()),
    ));
    let source: ProviderSource = Arc::new(Vec::new);
    let reopened = AgentsControl::load(
        &tokio::runtime::Handle::current(),
        s.dir.path(),
        Arc::clone(&broker),
        source,
    );
    assert_eq!(reopened.state(), paused);
    assert!(broker.agents_paused());
}

#[tokio::test]
async fn fallo_al_guardar_la_pausa_se_aplica_igual_pero_reanudar_no() {
    let dir = setup_dir();
    let (mut s, control) = setup_with(dir);
    // `agents-control.json` es una carpeta con contenido: no se puede reemplazar.
    fs::create_dir(s.file()).unwrap();
    fs::write(s.file().join("x"), b"x").unwrap();
    let (writer, mut engine) = engine_pipe();
    control.attach(writer);
    next_line(&mut engine).await;

    let err = control.pause().await.unwrap_err();
    assert_eq!(err.code, "agents.control_unavailable");
    assert!(control.state().paused, "la pausa en memoria se aplica");
    assert!(!control.state().saved, "pero no sobrevive a cerrar Faro");
    assert_eq!(
        serde_json::to_value(control.state()).unwrap()["saved"],
        json!(false)
    );
    assert!(s.broker.agents_paused());
    assert_eq!(next_line(&mut engine).await["paused"], true);
    let event = s.audit_of("agents.paused").await;
    assert_eq!(event["result"], "ok");
    assert_eq!(event["details"]["reason"], "not_saved");
    assert_eq!(event["details"]["error_code"], "agents.control_unavailable");

    let err = control.resume().await.unwrap_err();
    assert_eq!(err.code, "agents.control_unavailable");
    assert!(control.state().paused, "no reanuda");
    assert!(!control.state().saved, "sigue sin guardar");
    assert!(s.broker.agents_paused());
    let event = s.audit_of("agents.resumed").await;
    assert_eq!(event["result"], "error");
    assert!(!s.dir.path().join("agents-control.json.tmp").exists());
}

/// `saved` dice si la pausa sobrevive a cerrar Faro: es lo que la interfaz muestra tras
/// recargar la ventana (revisión de seguridad de T10, hallazgo 1). Se comprueba contra lo que
/// leería Faro al volver a abrirse.
#[tokio::test]
async fn saved_sigue_al_ultimo_guardado_con_exito() {
    let (s, control) = setup_with(setup_dir());
    let tmp = s.dir.path().join("agents-control.json.tmp");
    // El temporal es una carpeta con contenido: no se puede escribir, y el archivo que ya
    // hubiera se queda como estaba (la escritura es atómica).
    let block = || {
        fs::create_dir(&tmp).unwrap();
        fs::write(tmp.join("x"), b"x").unwrap();
    };
    let unblock = || fs::remove_dir_all(&tmp).unwrap();
    let reopened = |s: &Setup| {
        let runtime = tokio::runtime::Handle::current();
        let broker = Arc::new(SecretBroker::new(
            Arc::new(crate::vault::store::MemoryStore::new()),
            s.dir.path(),
            AuditQueue::spawn(&runtime),
        ));
        AgentsControl::load(&runtime, s.dir.path(), broker, Arc::new(Vec::new)).state()
    };

    // Activo y sin archivo → pausa que no se guarda: `saved` es falso, porque Faro
    // reabierto no estaría en pausa.
    block();
    control.pause().await.unwrap_err();
    let failed = control.state();
    assert!(failed.paused && !failed.saved);
    assert!(!reopened(&s).paused);
    unblock();

    // Pausar otra vez con éxito: guardada y conserva cuándo empezó.
    let ok = control.pause().await.unwrap();
    assert!(ok.paused && ok.saved);
    assert_eq!(ok.changed_at, failed.changed_at);
    assert_eq!(control.state(), ok);
    assert_eq!(reopened(&s), ok);

    // Ya en pausa y guardada: si guardar falla otra vez, el archivo sigue diciendo
    // "pausado" y `saved` sigue verdadero.
    block();
    control.pause().await.unwrap_err();
    assert_eq!(control.state(), ok);
    assert_eq!(reopened(&s), ok);

    // Reanudar falla: sigue en pausa y guardada.
    control.resume().await.unwrap_err();
    assert_eq!(control.state(), ok);
    unblock();

    // Reanudar con éxito: guardado. Una pausa nueva que no se guarda vuelve a ser `false`:
    // el archivo dice "activo".
    let resumed = control.resume().await.unwrap();
    assert!(!resumed.paused && resumed.saved);
    assert_eq!(reopened(&s), resumed);
    block();
    control.pause().await.unwrap_err();
    assert!(control.state().paused && !control.state().saved);
    assert!(!reopened(&s).paused);
}

/// Mientras se guarda una pausa nueva, el estado no dice "guardado" antes de tiempo.
#[tokio::test]
async fn saved_es_falso_mientras_se_guarda_una_pausa_nueva() {
    let (_s, control) = setup_with(setup_dir());
    let seen: Arc<Mutex<Vec<(bool, bool)>>> = Arc::new(Mutex::new(Vec::new()));
    let out = Arc::clone(&seen);
    let state = Arc::clone(&control.state);
    control.set_save_hook(Arc::new(move || {
        let current = lock(&state).clone();
        out.lock().unwrap().push((current.paused, current.saved));
    }));
    assert!(control.pause().await.unwrap().saved);
    // Pausar otra vez, ya guardada: durante el guardado sigue guardada.
    assert!(control.pause().await.unwrap().saved);
    assert_eq!(*seen.lock().unwrap(), [(true, false), (true, true)]);
}

/// Revisión de seguridad de T5: la revocación en memoria no espera al disco. Cuando se
/// guarda el archivo, las concesiones de ejecución ya no existen y las nuevas se rechazan.
#[tokio::test]
async fn pausar_revoca_en_memoria_antes_de_guardar() {
    let (s, control) = setup_with(setup_dir());
    s.broker.engine_started(1, None);
    s.broker.insert_test_run_grant(
        "0192f0a0-5555-7abc-8def-000000000001",
        "site_summary",
        vec![(
            "llm/anthropic/default".to_owned(),
            vec![crate::secrets::request::Op::Get],
        )],
    );
    assert_eq!(s.broker.active_run_grants(), 1);
    let seen: Arc<Mutex<Vec<(bool, usize, bool)>>> = Arc::new(Mutex::new(Vec::new()));
    let (broker, out, file) = (Arc::clone(&s.broker), Arc::clone(&seen), s.file());
    control.set_save_hook(Arc::new(move || {
        out.lock().unwrap().push((
            broker.agents_paused(),
            broker.active_run_grants(),
            file.exists(),
        ));
    }));
    control.pause().await.unwrap();
    assert_eq!(
        *seen.lock().unwrap(),
        [(true, 0, false)],
        "al guardar ya estaba en pausa, sin concesiones y aún sin archivo"
    );
    assert!(s.read_file()["paused"].as_bool().unwrap());

    // Si el guardado falla, la pausa en memoria se mantiene y el error es el mismo.
    control.resume().await.unwrap();
    fs::remove_file(s.file()).unwrap();
    fs::create_dir(s.file()).unwrap();
    fs::write(s.file().join("x"), b"x").unwrap();
    s.broker.insert_test_run_grant(
        "0192f0a0-5555-7abc-8def-000000000002",
        "site_summary",
        vec![(
            "llm/anthropic/default".to_owned(),
            vec![crate::secrets::request::Op::Get],
        )],
    );
    let err = control.pause().await.unwrap_err();
    assert_eq!(err.code, "agents.control_unavailable");
    assert_eq!(seen.lock().unwrap().last(), Some(&(true, 0, true)));
    assert!(control.state().paused);
    assert_eq!(s.broker.active_run_grants(), 0);
}

// ---------- Aviso al motor ----------

#[tokio::test]
async fn agents_control_al_conectar_y_al_cambiar_la_boveda() {
    let (s, control) = setup_with(setup_dir());
    // Sin motor conectado, los cambios no se envían a ninguna parte.
    control.providers_changed();
    control.pause().await.unwrap();
    control.sync().await;

    let (writer, mut engine) = engine_pipe();
    control.attach(writer);
    assert_eq!(
        next_line(&mut engine).await,
        json!({"event": "agents_control", "paused": true, "llm_providers": ["anthropic"]})
    );
    *s.providers.lock().unwrap() = vec![Provider::Anthropic, Provider::Openai, Provider::Gemini];
    control.providers_changed();
    assert_eq!(
        next_line(&mut engine).await["llm_providers"],
        json!(["anthropic", "openai", "gemini"])
    );
    s.providers.lock().unwrap().clear();
    control.providers_changed();
    assert_eq!(next_line(&mut engine).await["llm_providers"], json!([]));

    // Tras `detach`, nada más llega a ese motor.
    control.detach();
    control.providers_changed();
    control.sync().await;
    let reads = s.provider_reads.load(Ordering::SeqCst);
    // Un motor nuevo recibe el estado al conectarse (y se vuelven a leer los proveedores).
    let (writer, mut second) = engine_pipe();
    control.attach(writer);
    assert_eq!(next_line(&mut second).await["paused"], true);
    assert!(s.provider_reads.load(Ordering::SeqCst) > reads);
    drop(engine);
}

#[tokio::test]
async fn escritura_fallida_suelta_el_motor() {
    let (logs, _guard) = crate::test_logs::capture();
    let (_s, control) = setup_with(setup_dir());
    let (core, engine) = tokio::io::duplex(64);
    drop(engine);
    control.attach(StdinWriter::spawn(Box::new(core)));
    control.sync().await;
    control.pause().await.unwrap();
    control.sync().await;
    assert!(
        logs.text()
            .contains("no se pudo enviar el estado de los agentes"),
        "{}",
        logs.text()
    );
    assert!(format!("{control:?}").contains("paused: true"));
}

#[test]
fn linea_agents_control() {
    let line = control_line(true, &[Provider::Openai, Provider::Gemini]);
    assert_eq!(line.last(), Some(&b'\n'));
    let value: Value = serde_json::from_slice(&line).unwrap();
    assert_eq!(
        value,
        json!({"event": "agents_control", "paused": true, "llm_providers": ["openai", "gemini"]})
    );
}
