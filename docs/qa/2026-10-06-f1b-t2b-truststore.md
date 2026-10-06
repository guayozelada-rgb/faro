# F1b T2b — HTTPS del motor con el almacén de certificados del sistema

- **Spec:** [F1b](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md), tarea T2b.
- **Decisión:** [ADR 0012, actualización 2026-10-06](../adr/0012-red-saliente-del-motor.md).
- **Origen:** informe de T2, [§8, hallazgo 7 de §10 y §12](2026-10-05-f1b-t2-dependencias.md).
- **Estado:** implementado. Revisión de `revisor-seguridad`: APROBADO CON CAMBIOS; los cambios están hechos (§6) y falta que el revisor los confirme. Pendiente: la prueba manual del usuario con un WordPress real y la CI en Windows, macOS y Linux.

## 1. Qué cambia

| Archivo | Cambio |
| --- | --- |
| `apps/engine/faro_engine/net/tls.py` (nuevo) | `tls_context()`: un único `truststore.SSLContext(PROTOCOL_TLS_CLIENT)` por proceso, protegido con un cerrojo, con `CERT_REQUIRED`, `check_hostname=True`, TLS 1.2 como mínimo y sin raíces añadidas. Si `truststore` falla (`ImportError`, `OSError` o `NotImplementedError`), el respaldo es `ssl.create_default_context(cafile=certifi.where())` con las mismas condiciones. Registra `net.tls_context_ready` con `store` y la versión de `truststore`; en el respaldo, también `net.tls_store_fallback` con el nombre de la clase de la excepción. `tls_store()` devuelve `system` o `certifi`. |
| `apps/engine/faro_engine/net/client.py` | `default_transport()` usa `verify=tls_context()`. Siguen igual `trust_env=False`, `http2=False`, la fijación de IP con `sni_hostname`, la guardia SSRF y los errores (`site.tls_error`). Docstrings actualizados. |
| `apps/engine/faro_engine/__main__.py` | Quita `SSLKEYLOGFILE`, `SSL_CERT_FILE` y `SSL_CERT_DIR` (`TLS_ENV_REMOVED`) antes de cualquier otro import. |
| `apps/engine/pyproject.toml`, `uv.lock` | `certifi>=2026.7.22` pasa a dependencia directa (respaldo). `trustme==1.2.1` entra en el grupo `dev` (arrastra `cryptography`, `cffi` y `pycparser`, solo para pruebas). `net/tls.py` entra en `strict_modules`. |
| `apps/engine/scripts/bundle_smoke.py` | La comprobación `tls` (prueba 6) es obligatoria también en la variante `base`. Dentro del ejecutable comprueba lo siguiente: `tls_context()` es un `truststore.SSLContext` compartido, con `store=system`, `CERT_REQUIRED`, `check_hostname`, TLS 1.2 como mínimo y sin raíces; `default_transport()` lo usa sin HTTP/2; el módulo de plataforma de `truststore` (`_windows`, `_macos` u `_openssl`) está cargado. Si LiteLLM está incluido, comprueba además su contexto, como antes. También quita las tres variables TLS al arrancar, igual que `__main__`. |
| `apps/engine/scripts/bundle_smoke_build.py` | La variante `base` ya no excluye `truststore`, que ahora es del núcleo de red. |
| `apps/engine/scripts/manual_tls_check.py` (nuevo) | Comprobación manual (§4). |
| `apps/engine/README.md` | Uso de `manual_tls_check.py`, nota sobre certificados y antivirus. |

Sin cambios en los endpoints ni en el OpenAPI.

## 2. Pruebas (`apps/engine/tests/net/`)

Montaje en `tls_helpers.py`:

- certificados de `trustme` con validez corta (macOS rechaza los de más de 825 días);
- un servidor HTTPS en `127.0.0.1` que anota, por conexión, el SNI y los bytes descifrados que recibe;
- `LoopbackTransport`: envuelve el `default_transport()` real y, después de comprobar que la petición ya va a la IP fijada por la guardia, cambia **solo** el destino a `127.0.0.1:<puerto>`;
- `fetch_through_engine`: `SafeHttpClient` completo (URL, guardia con resolución falsa a una IP pública, fijación, SNI y mapeo de errores).

La CA de prueba nunca entra en el contexto de producción. Las pruebas que necesitan aceptar un certificado construyen otro contexto con `tls._build_tls_context()`, sin caché, le añaden la CA con `load_verify_locations` y lo inyectan en `faro_engine.net.client` con `monkeypatch`. El código de producción no ofrece ninguna forma de añadir raíces.

| Prueba del ADR | Pruebas |
| --- | --- |
| 1. Propiedades | `test_tls_context_es_unico_de_truststore_y_endurecido`, `test_tls_context_se_construye_una_sola_vez`, `test_tls_context_registra_el_almacen_sin_datos_de_certificados`, `test_default_transport_usa_el_contexto_compartido` (los argumentos `verify`, `trust_env` y `http2`, y el contexto del pool de httpcore) |
| 2. `SSL_CERT_FILE` | `test_ssl_cert_file_no_anade_raices_al_motor` (proceso aparte, `tests/net/tls_probe.py`). Control: un `ssl.create_default_context()` creado antes de importar `__main__` acepta el certificado, en memoria y por red. Después, el motor da `site.tls_error` y el servidor no recibe ningún byte. `test_sin_limpieza_el_almacen_del_sistema_tampoco_usa_ssl_cert_file`: variante sin limpieza, solo en Windows y macOS |
| 3. Nombre | `test_nombre_que_no_coincide_se_rechaza`: el mismo certificado de `otro.test` da `site.tls_error` con `sitio.test` (cero bytes) y 200 con `otro.test`. `test_certificado_valido_con_la_ca_inyectada_da_200`: el servidor recibe SNI `sitio.test` y `Host: sitio.test`. `test_en_memoria_el_nombre_es_lo_unico_que_cambia` |
| 4. Caducado y CA desconocida | `test_certificado_caducado_se_rechaza`, `test_certificado_de_otra_ca_se_rechaza`, `test_contexto_de_produccion_rechaza_una_ca_que_no_esta_en_el_sistema`, `test_en_memoria_caducado_y_ca_desconocida_se_rechazan`, `test_en_memoria_el_contexto_compartido_no_acepta_la_ca_de_prueba` |
| 5. Estática | `test_solo_net_tls_crea_contextos_tls`, con un análisis AST de todo `faro_engine/` salvo `net/tls.py`. Busca `SSLContext(`, `create_default_context`, `_create_unverified_context`, `create_ssl_context`, `load_verify_locations`, `load_default_certs`, `set_default_verify_paths`, `inject_into_ssl` y `_build_tls_context`; cualquier `verify=` que no sea `tls_context()`; `CERT_NONE` y `CERT_OPTIONAL`; asignaciones a `check_hostname` y `verify_mode`. `test_la_comprobacion_estatica_detecta_cada_forma` (14 casos) demuestra que detecta cada forma, y `test_la_comprobacion_estatica_admite_el_contexto_compartido` que ignora los textos |
| 6. Ejecutable | `scripts/bundle_smoke.py`, comprobación `tls` (§1). Sin empaquetar, en este equipo: `ok`, con `engine_store=system`, `engine_platform_module=truststore._windows` y LiteLLM en `truststore._api.SSLContext` |
| Respaldo | `test_respaldo_a_certifi_si_truststore_no_se_puede_importar` (con `truststore` simulado como no disponible: mismo objeto, condiciones 2 a 4 y solo las raíces de `certifi`), `test_respaldo_a_certifi_si_truststore_falla_al_construir` (`OSError` y `NotImplementedError`), `test_respaldo_rechaza_la_ca_de_ssl_cert_file` (con la variable puesta, el respaldo no la lee) y `test_otros_errores_de_truststore_no_activan_el_respaldo` |
| Script manual | `test_manual_tls_check.py`: muestra el almacén y el código, no sigue redirecciones, no reintenta, trae los códigos de error y la configuración de producción, y quita las variables al importarse |

Las pruebas "en memoria" hacen el handshake completo con `wrap_bio`, el mismo camino que usa asyncio, sin sockets. Ningún interceptor de red puede meterse ahí, así que comprueban la verificación del sistema (CryptoAPI, Security.framework u OpenSSL) también en un equipo con antivirus.

**Resultado local (Windows 11 con Norton):** `uv run pytest` da 978 pruebas superadas y 2 omitidas, con un 100 % de cobertura (`net/tls.py` al 100 %). `ruff format --check`, `ruff check` y `mypy faro_engine tests scripts` pasan.

## 3. Hallazgos

1. **Norton intercepta TLS también en loopback.** A `127.0.0.1`, `127.0.0.2` y `::1`, con y sin SNI, presenta un certificado propio. Cuando el certificado del servidor no es de confianza, firma con su raíz "Norton Web/Mail Shield **Untrusted** Root", que no está en el almacén, así que el rechazo se mantiene. Consecuencias:
   - en este equipo no se puede comprobar por red que un certificado de prueba se acepta. `test_certificado_valido_con_la_ca_inyectada_da_200` y `test_nombre_que_no_coincide_se_rechaza` se omiten con el motivo "un programa del equipo intercepta TLS en loopback; se ejecuta en la CI". La detección (`loopback_tls_intercepted()`) compara el certificado que llega con el que presentó el servidor;
   - en la prueba 2, la parte de control por red solo se exige sin interceptor; la de memoria se exige siempre;
   - las pruebas de rechazo por red sí se ejecutan aquí, pero en este equipo el rechazo lo provoca la raíz "Untrusted" de Norton. Lo que demuestran el rechazo por la causa correcta son las pruebas en memoria (siempre) y la CI (sin interceptor).
2. **`truststore.SSLContext.get_ca_certs()` lanza `NotImplementedError`** (0.10.4). El ADR pide `get_ca_certs() == []`. La prueba lo comprueba sobre el contexto interno (`context._ctx`), que es el que usa `truststore` para validar (en Windows y macOS, las raíces cargadas ahí se suman a las del sistema). En el humo del ejecutable se hace igual.
3. **(Resuelto tras la revisión, §6.1) `truststore` cambia el contexto interno durante cada `wrap_bio` y `wrap_socket`.** En Windows y macOS pone `check_hostname=False` y `CERT_NONE` mientras dura la llamada y luego los restaura. La verificación con el sistema, que ocurre después del handshake, lee `verify_mode` y `check_hostname` de ese mismo contexto interno. `wrap_socket` toma un cerrojo, pero la verificación queda fuera de él, y `wrap_bio`, que es lo que usa asyncio, no toma ninguno. Si **otro hilo** abre una conexión con el mismo objeto justo cuando termina la verificación de una conexión, esa verificación puede ejecutarse con `CERT_NONE` y aceptar cualquier certificado.
   - En el motor hoy no ocurre: todo el HTTPS es asíncrono y va en el hilo del bucle de eventos, y entre el cambio y la restauración no hay `await`.
   - Queda documentado en el docstring de `net/tls.py`: solo para transportes asíncronos en el bucle del motor.
   - Si en el futuro algún cliente síncrono en otro hilo, como un `httpx.Client` dentro de `run_in_threadpool`, usara `tls_context()`, haría falta un contexto propio por hilo. Esto afecta a la condición 9 (contexto inmutable) y a T6, por los transportes de LiteLLM.
4. **`pypi.org/simple/` pasa del límite de 5 MB** del cliente del motor (es el índice completo de PyPI). El script usa por defecto `https://pypi.org/simple/truststore/`: mismo dominio, que Norton intercepta, y pocos KB.
5. Con Norton activo, `uv add` y `uv lock` fallan con `UnknownIssuer` si no se usa `--system-certs` (anotado en el README).

## 4. Comprobación manual (obligatoria, en el equipo del usuario)

Desde `apps/engine`, sin claves:

```
uv run python scripts/manual_tls_check.py
```

Resultado esperado con Norton: `store=system HTTP 200` y código de salida 0. Antes de T2b, con `certifi`, este dominio daba `CERTIFICATE_VERIFY_FAILED` (`site.tls_error`), según el informe de T2, §8. Con otra URL: `uv run python scripts/manual_tls_check.py https://<tu-sitio>/wp-json/`. Muestra solo el almacén y el código HTTP, o el código de error; no sigue redirecciones y no reintenta.

| Comprobación | Resultado | Quién | Fecha |
| --- | --- | --- | --- |
| `manual_tls_check.py` (pypi.org) | `store=system HTTP 200`, código 0 | agente `motor-python`, en este mismo equipo, durante la implementación | 2026-10-06 |
| `manual_tls_check.py` (pypi.org) | ⏳ | usuario | |
| WordPress real por HTTPS desde la app (flujo de F1a) | ⏳ | usuario | |

## 5. Comportamiento por sistema

| Sistema | Verificación | Notas |
| --- | --- | --- |
| Windows | CryptoAPI (`CertGetCertificateChain` + política SSL con el nombre del SNI) | Comprobado aquí: pruebas en memoria, rechazo por red y `manual_tls_check.py` con 200 a través de Norton. |
| macOS | Security.framework (`SecTrust` con política SSL y nombre) | Solo en la CI (`macos-latest`). Falta comprobarlo en el ejecutable firmado con hardened runtime antes de la primera versión para macOS (ADR). |
| Linux (solo CI y desarrollo) | OpenSSL con las rutas por defecto de la distribución | `truststore` llama a `set_default_verify_paths()` en cada conexión, así que `SSL_CERT_FILE` y `SSL_CERT_DIR` sí cambiarían las raíces; las quita `__main__` (prueba 2). La variante "sin limpieza" se omite en Linux a propósito. |

## 6. Revisión de seguridad (APROBADO CON CAMBIOS) y correcciones

### 6.1 Carrera de hilos en `truststore` (medio; crítico si ocurre)

`truststore` (`_api.py`, `wrap_bio` sin cerrojo; `_windows.py` y `_macos.py`, `_configure_context`) guarda `verify_mode` y `check_hostname`, pone `CERT_NONE` y `check_hostname=False` y al salir restaura lo guardado. Con dos hilos cruzados, el contexto compartido queda **para siempre** en `CERT_NONE`; el revisor lo reprodujo 3 de 3. El hallazgo 3 de §3 lo anticipaba como "no ocurre hoy", pero nada lo impedía.

Corrección en `faro_engine/net/tls.py`:

- `_ThreadOwnedContext`, subclase de `truststore.SSLContext` creada por `_thread_owned_class()`. El primer hilo que llama a `wrap_bio` o `wrap_socket` pasa a ser su dueño. Se guarda el objeto `Thread`, no `get_ident()`, que se reutiliza. Cualquier otro hilo recibe `WrongThreadError` (subclase de `ssl.SSLError`) **antes** de tocar el contexto, y se registra `net.tls_wrong_thread`.
- El cliente lo traduce a `site.tls_error`: httpcore lo envuelve en `ConnectError` y `_is_tls_error` encuentra el `SSLError` en la cadena. Comprobado en una prueba, sin que salga el ClientHello.
- **Respaldo con `certifi`, sin protección, y por qué:** es un `ssl.SSLContext` de la biblioteca estándar, cuyo `wrap_bio`/`wrap_socket` no cambia el contexto (OpenSSL lee una configuración que nadie modifica, condición 9 del ADR). Una prueba con cuatro hilos y 1200 conexiones lo comprueba.
- `scripts/bundle_smoke.py` (comprobación `tls`) exige además que el contexto sea un `_ThreadOwnedContext` dentro del ejecutable.

Pruebas nuevas en `tests/net/test_tls.py`:

- `test_wrap_bio_desde_otro_hilo_falla_sin_tocar_el_contexto`: `SSLError`, contexto interno en `CERT_REQUIRED` y `check_hostname=True`; el dueño sigue aceptando un certificado válido y rechazando uno con otro nombre.
- `test_el_primer_hilo_que_conecta_es_el_dueno`.
- `test_wrap_socket_desde_otro_hilo_falla_sin_tocar_el_contexto`.
- `test_hilos_ajenos_en_paralelo_no_degradan_el_contexto`: la reproducción del revisor (cuatro hilos contra el dueño). Las 1200 llamadas ajenas fallan y el contexto queda intacto.
- `test_en_el_bucle_un_hilo_de_trabajo_no_puede_usar_el_contexto`: con `asyncio.to_thread`.
- `test_el_cliente_traduce_el_hilo_ajeno_a_tls_error`.
- `test_el_bucle_sigue_conectando_tras_un_intento_desde_otro_hilo`: 200, intento ajeno y 200 otra vez. Necesita loopback directo: se omite en este equipo y se ejecuta en la CI.
- `test_el_respaldo_con_certifi_no_cambia_al_conectar_desde_varios_hilos`.

Los dos casos de respaldo por error de construcción parchean ahora `truststore.SSLContext.__init__`, porque el motor construye la subclase.

### 6.2 Comprobación estática incompleta (bajo)

`tls_violations` detecta ahora todas las formas de la condición 14 del ADR:

- alias de import de `ssl`, `truststore`, `_ssl`, `httpx` y `httpcore`, incluido `from ssl import create_default_context as mk`;
- `getattr`, `setattr`, `delattr` y `hasattr` con nombres TLS, y `getattr` sobre `ssl` o `truststore`;
- `**kwargs`, `*args`, posicionales a transportes y `mounts=` en `httpx` y `httpcore`;
- `ssl=`, `ssl_context=`, `context=`, `cafile=`, `capath=` y `cadata=`, y `verify` dentro de `**{...}` o `**dict(...)`;
- `httpx.Client` y `httpx.AsyncClient` sin `transport=`; `httpx.get`, `post`, `request`, `stream` y similares;
- `urlopen`, `build_opener`, `HTTPSHandler` y `HTTPSConnection`;
- asignaciones a `verify_mode`, `check_hostname`, `minimum_version`, `maximum_version`, `options`, `verify_flags`, `keylog_filename`, `hostname_checks_common_name`, `post_handshake_auth`, `sslobject_class` y `sslsocket_class`;
- cualquier referencia a `_create_unverified_context` o `_create_default_https_context`.

`test_la_comprobacion_estatica_detecta_cada_forma` pasa de 14 a 71 casos, que cubren los 12 de la reproducción del revisor. `test_la_comprobacion_estatica_admite_el_contexto_compartido` tiene 12 casos que no deben dar aviso (`ssl.SSLError`, `getattr(socket, …)`, `Model(**fields)`, lecturas de `.options`…). El recorrido de `faro_engine/` sigue sin violaciones.

### 6.3 Salvaguarda en la CI (bajo)

`loopback_is_direct()`: si `CI` o `GITHUB_ACTIONS` están definidas y se detecta un interceptor en loopback, `pytest.fail` en vez de `pytest.skip`. Lo usan el fixture `direct_loopback` y la parte de control por red de la prueba 2. La prueba es `test_en_la_ci_un_interceptor_hace_fallar_en_vez_de_omitir`, con cinco casos.

### 6.4 `inject_into_ssl` (bajo)

Nueva `install_system_trust_for_libraries()` en `net/tls.py`:

- construye primero el contexto del motor y, solo con `store=system`, llama a `truststore.inject_into_ssl()`; con el respaldo no inyecta;
- es idempotente y registra `net.tls_libraries_trust`;
- T6 la llamará antes de importar LiteLLM; `__main__` todavía no la llama;
- `scripts/bundle_smoke.py` ya la usa en lugar de `truststore.inject_into_ssl()`.

Pruebas: `test_install_system_trust_construye_el_contexto_y_luego_inyecta` y `test_install_system_trust_no_inyecta_con_el_respaldo`. Las dos deshacen la inyección al terminar. La skill `capa-llm`, el ADR (condición 13) y §12 del informe de T2 apuntan a esta función.

### 6.5 IP literal (bajo)

Pruebas en memoria, que se ejecutan en los tres sistemas de la CI y también con Norton:

- `test_en_memoria_ip_literal_correcta_se_acepta_y_otra_se_rechaza` (`203.0.113.10` frente a `.11`);
- `test_en_memoria_certificado_dns_pedido_por_ip_se_rechaza`;
- `test_en_memoria_el_comodin_cubre_un_solo_nivel` (`*.sitio.test` acepta `a.sitio.test` y rechaza `a.b.sitio.test`).

### 6.6 Documentación

- **ADR 0012:**
  - condiciones 12 (un hilo por contexto), 13 (inyección solo con la función) y 14 (comprobación estática ampliada);
  - el riesgo conocido de AIA en Windows (medio) en el modelo de amenaza;
  - la condición para F2 y las condiciones para T6 en Consecuencias;
  - las pruebas de IP literal y la salvaguarda de la CI en "Pruebas exigidas".
- **Riesgo conocido de AIA en Windows (medio).** La verificación llama a `CertGetCertificateChain` con `chain_flags=0` dentro de `do_handshake`, de forma síncrona en el hilo del bucle. Un certificado sin intermedio, con una AIA que no responde, bloquea el motor unos 15 s por conexión; el revisor lo reprodujo. Se acepta por ahora. La mitigación es la condición de F2.
- **Condición para F2 (rastreador):** los handshakes contra hosts no confiables no se hacen en el bucle principal. Van en un proceso aparte o en hilos de trabajo, y cada hilo usa **su propio** contexto de `net/tls.py` (`threading.local`, con las mismas condiciones), nunca compartido.
- **Condiciones para T6** (también en la skill `capa-llm` y en §12 del informe de T2, condiciones 18 a 21):
  - solo `acompletion` con el cliente asíncrono propio de cada llamada;
  - prohibidos `completion` síncrono, `litellm.ssl_verify`, `aclient_session` y cualquier cliente que LiteLLM construya por su cuenta;
  - una prueba que espíe `wrap_bio` y `wrap_socket` y demuestre que, durante `acompletion` con los tres proveedores, (a) todas las llamadas sobre `tls_context()` ocurren en el hilo del bucle, y (b) no se usa ningún contexto de `_ssl_context_cache` de LiteLLM.

### 6.7 Resultado

En este equipo (Windows 11 con Norton):

- `uv run pytest`: 1063 pruebas superadas y 3 omitidas, con un 100 % de cobertura. Se omiten tres pruebas por el interceptor en loopback; en la CI fallarían en vez de omitirse.
- `ruff format --check`, `ruff check` y `mypy faro_engine tests scripts` pasan.
- `scripts/bundle_smoke.py` sin empaquetar, comprobación `tls`: `ok` (`engine_store=system`, `truststore._windows`, LiteLLM en `truststore._api.SSLContext`).
