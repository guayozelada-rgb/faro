# ADR 0012 — Red saliente del motor: SSRF, tiempos, reintentos y sitios locales en desarrollo

- **Fecha:** 2026-09-29
- **Estado:** aceptado
- **Spec:** [F1a — Conexión con WordPress](../specs/2026-09-29-f1a-conexion-wordpress.md)
- **Skill:** `revision-seguridad` §5

## Contexto

En F1a el motor hace por primera vez peticiones a una URL que **escribe el usuario** (su sitio WordPress). En F2 el rastreador (crawler) hará lo mismo a escala. Sin protección, la URL puede apuntar a `localhost` (incluido el propio motor), a la red local del usuario o a direcciones de metadatos de nube (SSRF). Además, las pruebas con wp-env necesitan justo lo contrario: conectar con `http://localhost:8888`.

## Decisión

Un módulo común `faro_engine/net/` (guardia de red + cliente `httpx`) que usan todas las peticiones salientes a URLs no fijas en código. Reglas:

1. **URL**: se recortan espacios; sin esquema → `https://`; solo `https` (salvo el modo de desarrollo, abajo); sin usuario ni contraseña en la URL; host en minúsculas e IDN en punycode; sin puerto o puerto 443; se quitan consulta y fragmento; se conserva la ruta (WordPress en subcarpeta) sin barra final; máximo 2048 caracteres.
2. **Direcciones prohibidas**: se resuelve el nombre y se rechaza si **alguna** dirección es de loopback, privada (RFC 1918), enlace local (`169.254.0.0/16`, `fe80::/10`), CGNAT (`100.64.0.0/10`), ULA (`fc00::/7`), multicast, reservada, no especificada, o IPv4 mapeada en IPv6 a cualquiera de ellas.
3. **Sin segunda resolución** (DNS rebinding): la conexión se hace a la IP ya validada, con SNI y `Host` del nombre original.
4. **Redirecciones**: no se siguen de forma automática. Solo el descubrimiento inicial del sitio sigue hasta 3, validando cada salto con las reglas 1–3. Las peticiones firmadas nunca siguen redirecciones (una redirección → `site.moved`).
5. **TLS siempre verificado**; sin proxies del sistema (`trust_env=False`); `User-Agent: Faro/<versión>`.
6. **Tiempos**: conexión 5 s, lectura 15 s, 20 s por petición; cada operación del motor tiene además un plazo total (5 s menos que el `x-faro-timeout-seconds` que aplica el núcleo).
7. **Tamaño**: respuestas de más de 5 MB se cortan (`site.response_too_large`).
8. **Reintentos**: solo peticiones idempotentes (GET, DELETE): hasta 2 reintentos ante error de red, 502, 503 o 504, con espera de 1 s y 3 s (±20 % aleatorio); ante 429 un reintento respetando `Retry-After` si es ≤ 10 s. Cada reintento firmado lleva nonce y hora nuevos. **Nunca** se reintenta `POST /pair` (un código ya usado no sirve dos veces).
9. **Concurrencia**: como máximo 4 peticiones simultáneas en total y 1 por sitio.

**Modo sitios locales (solo desarrollo)**: permite `http` y hosts de loopback (`localhost`, `127.0.0.1`, `::1`) con cualquier puerto, **excepto el puerto del propio motor**. Se activa solo si:
- el núcleo es un build de depuración (`cfg!(debug_assertions)`) y `.env.local` tiene `FARO_ALLOW_LOCAL_SITES=1`, en cuyo caso lanza el motor con `--allow-local-sites`; o el motor corre con `--dev` y el mismo valor está en `.env.local`;
- y el motor **no** está empaquetado (`--allow-local-sites` con `sys.frozen` → sale con código 2).
Las redes privadas siguen prohibidas también en este modo.

## Consecuencias

- El rastreador de F2 reutiliza `faro_engine/net/` (añadirá robots.txt y límites por dominio).
- Sitios que solo funcionan por `http`, en un puerto distinto de 443, detrás de un proxy corporativo obligatorio o en la red local del usuario no se pueden conectar en F1a. Se acepta: el usuario objetivo tiene su tienda publicada con HTTPS.
- Cobertura del 95 % exigida en el módulo de guardia de red.
