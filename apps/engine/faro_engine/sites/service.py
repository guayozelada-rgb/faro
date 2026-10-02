"""Casos de uso de los sitios conectados (spec F1a §4.2, ADR 0010 y 0011).

| Caso | Operación | Secretos (concesión del núcleo) |
| --- | --- | --- |
| `list_sites` | `listSites` | ninguno |
| `connect` | `connectSite` | `wp/{new}/token`: `create` (+ `delete` si queda a medias) |
| `reconnect` | `reconnectSite` | `wp/{site_id}/token`: `set` |
| `check` | `checkSiteConnection` | `wp/{site_id}/token`: `get` |
| `list_content` | `listSiteContent` | `wp/{site_id}/token`: `get` |
| `remove` | `removeSite` | `wp/{site_id}/token`: `get` y `delete` |

Reglas:
- El token y el secreto HMAC solo viven en el llavero y, durante la operación, en
  `bytearray` que se sobrescriben al terminar (`SiteCredentials`). En la base solo queda
  `sha256(token)`; antes de usar el secreto se comprueba que coincide.
- `check` devuelve el sitio con `status = revoked` cuando el sitio da un veredicto
  (`site.revoked`, `site.connection_broken`, `site.auth_failed`) o falta el secreto
  (`site.secret_missing`). Si no se pudo comprobar (red, tiempo, 5xx…), devuelve el error
  y no cambia nada.
- Nunca se registra ni se devuelve el código de vinculación.
- Plazos (T13 B2): cada `secret_request` espera como mucho lo que le queda al plazo de la
  operación. `connect` trabaja hasta `UNDO_RESERVE_SECONDS` antes del final y guarda ese
  tiempo para deshacer: primero el `delete` del secreto (si no llega antes de que el
  núcleo cierre la concesión, el secreto queda huérfano) y después el `revoke` remoto, sin
  reintentos y con `UNDO_REVOKE_SECONDS` como mucho. Sin esa reserva no empieza `create`.
  `remove` reserva lo mismo para el `delete` final tras el `revoke`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, TypeVar, cast
from urllib.parse import urlsplit

import structlog

from faro_engine.core.audit import AuditLog
from faro_engine.core.config import UNDO_RESERVE_SECONDS, UNDO_REVOKE_SECONDS
from faro_engine.core.db.connection import Connection, DatabaseError, DbUnavailableError
from faro_engine.core.db.database import UNAVAILABLE_STATUS, Database
from faro_engine.core.errors import (
    SITE_ALREADY_CONNECTED,
    SITE_INVALID_CODE_FORMAT,
    SITE_MOVED,
    SITE_NOT_FOUND,
    SITE_SECRET_MISSING,
    SITE_TIMEOUT,
    VAULT_NOT_FOUND,
    FaroError,
    site_error,
)
from faro_engine.core.ids import new_id
from faro_engine.core.schemas.sites import (
    ContentKind,
    RemoveSiteOut,
    SeoPlugin,
    SiteConnectionOut,
    SiteContentItem,
    SiteContentPage,
    SiteCountsOut,
    SiteOut,
    WooCommerceOut,
)
from faro_engine.core.secrets import SecretBroker, SecretError
from faro_engine.net.client import Deadline, NetSettings, SafeHttpClient
from faro_engine.net.urls import normalize_site_url
from faro_engine.sites import repository
from faro_engine.sites.repository import ConnectionRecord, SiteRecord
from faro_engine.wordpress.client import RemoteStatus, WordPressClient
from faro_engine.wordpress.errors import VERDICT_CODES
from faro_engine.wordpress.signing import SiteCredentials, parse_secret_value

log = structlog.get_logger(__name__)

T = TypeVar("T")

PAIRING_CODE_RE: Final = re.compile(r"[0-9]{6}")
STATUS_ACTIVE: Final = "active"
STATUS_REVOKED: Final = "revoked"


def secret_ref_for(site_id: str) -> str:
    return f"wp/{site_id}/token"


def normalize_pairing_code(raw: str) -> str:
    """6 números; se aceptan espacios ("482 913"). Si no, `site.invalid_code_format`."""
    code = "".join(raw.split())
    if PAIRING_CODE_RE.fullmatch(code) is None:
        raise site_error(SITE_INVALID_CODE_FORMAT)
    return code


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def site_out(record: SiteRecord) -> SiteOut:
    connection = record.connection
    return SiteOut(
        id=record.id,
        url=record.url,
        name=record.name,
        created_at=record.created_at,
        connection=None if connection is None else _connection_out(connection),
    )


def _connection_out(c: ConnectionRecord) -> SiteConnectionOut:
    woocommerce = (
        None
        if c.woocommerce_active is None
        else WooCommerceOut(
            active=bool(c.woocommerce_active),
            version=c.woocommerce_version,
            hpos_enabled=_bool(c.hpos_enabled),
        )
    )
    counts = (
        None
        if c.pages_count is None or c.posts_count is None
        else SiteCountsOut(pages=c.pages_count, posts=c.posts_count, products=c.products_count)
    )
    return SiteConnectionOut(
        status="revoked" if c.status == STATUS_REVOKED else "active",
        last_error_code=c.last_error_code,
        connected_at=c.connected_at,
        last_checked_at=c.last_checked_at,
        revoked_at=c.revoked_at,
        plugin_version=c.plugin_version,
        wp_version=c.wp_version,
        woocommerce=woocommerce,
        seo_plugin=cast(SeoPlugin | None, c.seo_plugin),  # CHECK de la tabla
        counts=counts,
    )


def status_fields(remote: RemoteStatus) -> dict[str, object]:
    """Metadatos y conteos de `/status` para `site_connections`."""
    woo = remote.woocommerce
    return {
        "plugin_version": remote.plugin_version,
        "wp_version": remote.wp_version,
        "woocommerce_active": int(woo.active),
        "woocommerce_version": woo.version,
        "hpos_enabled": None if woo.hpos_enabled is None else int(woo.hpos_enabled),
        "seo_plugin": remote.seo_plugin,
        "pages_count": remote.counts.pages,
        "posts_count": remote.counts.posts,
        "products_count": remote.counts.products,
    }


def _site_name(name: str) -> str | None:
    clean = name.strip()
    return clean or None


def same_site(typed: str, final: str) -> bool:
    """Mismo esquema, puerto y host, admitiendo solo añadir o quitar `www.`."""
    a, b = urlsplit(typed), urlsplit(final)
    if a.scheme != b.scheme or a.port != b.port:
        return False
    host_a, host_b = (a.hostname or "").lower(), (b.hostname or "").lower()
    return host_a != "" and host_a.removeprefix("www.") == host_b.removeprefix("www.")


@dataclass(slots=True)
class SitesContext:
    """Dependencias de los casos de uso (una por app)."""

    database: Database
    secrets: SecretBroker
    audit: AuditLog
    net: NetSettings
    app_version: str
    now: Callable[[], datetime] = _utc_now
    wordpress_factory: Callable[[SafeHttpClient], WordPressClient] = WordPressClient


class SitesService:
    """Casos de uso de una operación con su plazo total (`deadline`)."""

    def __init__(self, context: SitesContext, deadline: Deadline) -> None:
        self._ctx = context
        self._deadline = deadline

    # --- Utilidades ----------------------------------------------------------------------

    async def _db(self, fn: Callable[[Connection], T]) -> T:
        try:
            return await self._ctx.database.run(fn)
        except DbUnavailableError as exc:
            raise FaroError.of(exc.code, UNAVAILABLE_STATUS) from None

    @contextlib.asynccontextmanager
    async def _wordpress(self, deadline: Deadline | None = None) -> AsyncIterator[WordPressClient]:
        async with SafeHttpClient(self._ctx.net, deadline or self._deadline) as http:
            yield self._ctx.wordpress_factory(http)

    def _now(self) -> str:
        return iso_utc(self._ctx.now())

    async def _load(self, site_id: str) -> tuple[SiteRecord, ConnectionRecord]:
        record = await self._db(lambda conn: repository.get_site(conn, site_id))
        if record is None or record.connection is None:
            raise site_error(SITE_NOT_FOUND)
        return record, record.connection

    async def _reload(self, site_id: str) -> SiteOut:
        record = await self._db(lambda conn: repository.get_site(conn, site_id))
        if record is None:  # pragma: no cover - lo acaban de actualizar en esta operación
            raise site_error(SITE_NOT_FOUND)
        return site_out(record)

    async def _update(
        self,
        site_id: str,
        fields: dict[str, object],
        *,
        name: str | None = None,
        set_name: bool = False,
    ) -> None:
        updated_at = self._now()
        await self._db(
            lambda conn: repository.update_site(
                conn, site_id, fields, updated_at=updated_at, name=name, set_name=set_name
            )
        )

    async def _credentials(self, record: SiteRecord, c: ConnectionRecord) -> SiteCredentials | None:
        """Credenciales del llavero si existen y coinciden con `token_sha256`; si no, `None`.

        Otros fallos del canal (llavero caído, tiempo agotado) se propagan.
        """
        try:
            secret = await self._ctx.secrets.get(c.secret_ref, max_wait=self._deadline.remaining())
        except SecretError as exc:
            if exc.code != VAULT_NOT_FOUND:
                raise
            log.warning("sites.secret_missing", site_id=record.id, reason="not_found")
            return None
        with secret:
            credentials = parse_secret_value(c.remote_connection_id, secret.buffer)
        if credentials is None:
            log.warning("sites.secret_missing", site_id=record.id, reason="shape")
            return None
        if not hmac.compare_digest(credentials.token_sha256(), c.token_sha256):
            credentials.wipe()
            log.warning("sites.secret_missing", site_id=record.id, reason="hash_mismatch")
            return None
        return credentials

    async def _mark_revoked(self, record: SiteRecord, c: ConnectionRecord, code: str) -> None:
        now = self._now()
        await self._update(
            record.id,
            {
                "status": STATUS_REVOKED,
                "last_error_code": code,
                "revoked_at": c.revoked_at if c.status == STATUS_REVOKED and c.revoked_at else now,
            },
        )
        if c.status != STATUS_REVOKED or c.last_error_code != code:
            log.info("sites.revoked_detected", site_id=record.id, error_code=code)
            await self._ctx.audit.record(
                action="site.revoked_detected",
                result="ok",
                actor="system",
                secret_ref=c.secret_ref,
                details={"site_id": record.id, "error_code": code},
            )

    async def _ensure_new_url(self, url: str) -> None:
        existing = await self._db(lambda conn: repository.find_site_id_by_url(conn, url))
        if existing is not None:
            raise site_error(SITE_ALREADY_CONNECTED, {"site_id": existing})

    # --- Casos de uso --------------------------------------------------------------------

    async def list_sites(self) -> list[SiteOut]:
        records = await self._db(repository.list_sites)
        return [site_out(record) for record in records]

    async def connect(self, url: str, pairing_code: str) -> SiteOut:
        code = normalize_pairing_code(pairing_code)
        site_url = normalize_site_url(url, self._ctx.net.policy)
        await self._ensure_new_url(site_url)
        # Los últimos `UNDO_RESERVE_SECONDS` del plazo quedan para deshacer (T13 B2).
        work = self._deadline.ending_before(UNDO_RESERVE_SECONDS)
        async with SafeHttpClient(self._ctx.net, work) as http:
            wp = self._ctx.wordpress_factory(http)
            found = await wp.discover(site_url)
            if found.site_url != site_url:
                # El código de vinculación solo va al dominio que escribió el usuario (o a
                # su variante con o sin `www.`), nunca al destino de una redirección ajena.
                if not same_site(site_url, found.site_url):
                    log.warning("site.discovery_other_host")
                    raise site_error(SITE_MOVED)
                await self._ensure_new_url(found.site_url)
            connection_id = new_id()
            site_id = new_id()
            ref = secret_ref_for(site_id)
            pairing = await wp.pair(
                found.api_root,
                code=code,
                app_instance_id=connection_id,
                app_version=self._ctx.app_version,
            )
            with pairing.credentials as credentials:
                create_sent = False
                try:
                    # Sin tiempo para deshacer no se crea nada; solo se revoca el `pair`.
                    work.check()
                    # Desde aquí el núcleo puede crearlo aunque la respuesta no llegue.
                    create_sent = True
                    value = credentials.secret_value()
                    try:
                        await self._ctx.secrets.create(ref, value, max_wait=work.remaining())
                    finally:
                        value[:] = bytes(len(value))
                    remote = await wp.status(found.api_root, credentials)
                    now = self._now()
                    record = SiteRecord(
                        id=site_id,
                        url=found.site_url,
                        name=_site_name(remote.site_name),
                        created_at=now,
                        connection=ConnectionRecord(
                            id=connection_id,
                            api_root=found.api_root,
                            remote_connection_id=credentials.connection_id,
                            token_sha256=credentials.token_sha256(),
                            secret_ref=ref,
                            status=STATUS_ACTIVE,
                            connected_at=now,
                            last_checked_at=now,
                            **status_fields(remote),  # type: ignore[arg-type]
                        ),
                    )
                    # La base tampoco puede comerse la reserva del deshacer.
                    work.check()
                    try:
                        async with asyncio.timeout(work.remaining()):
                            await self._insert(record, now)
                    except TimeoutError:
                        raise site_error(SITE_TIMEOUT) from None
                except BaseException:
                    await self._undo_connect(
                        http, wp, found.api_root, credentials, ref if create_sent else None
                    )
                    raise
        log.info("sites.connected", site_id=site_id)
        await self._ctx.audit.record(
            action="site.connected",
            result="ok",
            secret_ref=ref,
            details={"site_id": site_id},
        )
        return site_out(record)

    async def _insert(self, record: SiteRecord, now: str) -> None:
        try:
            await self._db(lambda conn: repository.insert_site(conn, record, now))
        except DatabaseError:
            # Casi siempre la URL única: otro `connectSite` del mismo sitio ganó la carrera.
            existing = await self._db(lambda conn: repository.find_site_id_by_url(conn, record.url))
            if existing is not None:
                raise site_error(SITE_ALREADY_CONNECTED, {"site_id": existing}) from None
            raise

    async def _undo_connect(
        self,
        http: SafeHttpClient,
        wp: WordPressClient,
        api_root: str,
        credentials: SiteCredentials,
        ref: str | None,
    ) -> None:
        """Vinculación a medias, con su propio plazo (`UNDO_RESERVE_SECONDS`, sin pasar
        del final de la operación): borra el secreto si se pidió crearlo (`ref`; si no
        llegó a crearse, el núcleo lo rechaza sin más) y después revoca la conexión en el
        sitio, sin reintentos y con `UNDO_REVOKE_SECONDS` como mucho. Las dos de buena fe.
        """
        log.warning("sites.connect_rolled_back")
        undo = self._deadline.capped(UNDO_RESERVE_SECONDS)
        if ref is not None:
            # Primero lo crítico: un secreto huérfano en el llavero no lo ve nadie.
            try:
                await self._ctx.secrets.delete(ref, max_wait=undo.remaining())
            except FaroError as exc:
                log.error("sites.rollback_delete_failed", error_code=exc.code)
        try:
            with http.limited_to(undo.capped(UNDO_REVOKE_SECONDS)):
                await wp.revoke(api_root, credentials, retries=0)
        except FaroError as exc:
            log.warning("sites.rollback_revoke_failed", error_code=exc.code)

    async def reconnect(self, site_id: str, pairing_code: str) -> SiteOut:
        code = normalize_pairing_code(pairing_code)
        record, c = await self._load(site_id)
        async with self._wordpress() as wp:
            found = await wp.discover(record.url)
            if found.site_url != record.url:
                raise site_error(SITE_MOVED)
            pairing = await wp.pair(
                found.api_root,
                code=code,
                app_instance_id=c.id,
                app_version=self._ctx.app_version,
            )
            with pairing.credentials as credentials:
                value = credentials.secret_value()
                try:
                    await self._ctx.secrets.set(
                        c.secret_ref, value, max_wait=self._deadline.remaining()
                    )
                finally:
                    value[:] = bytes(len(value))
                now = self._now()
                # El llavero ya tiene el token nuevo: la base lo sigue pase lo que pase.
                fields: dict[str, object] = {
                    "api_root": found.api_root,
                    "remote_connection_id": credentials.connection_id,
                    "token_sha256": credentials.token_sha256(),
                    "status": STATUS_ACTIVE,
                    "last_error_code": None,
                    "connected_at": now,
                    "revoked_at": None,
                }
                try:
                    remote = await wp.status(found.api_root, credentials)
                except FaroError as exc:
                    await self._update(site_id, fields)
                    if exc.code in VERDICT_CODES:
                        updated = replace(
                            c, status=STATUS_ACTIVE, last_error_code=None, revoked_at=None
                        )
                        await self._mark_revoked(record, updated, exc.code)
                    raise
        fields.update(status_fields(remote), last_checked_at=now)
        await self._update(site_id, fields, name=_site_name(remote.site_name), set_name=True)
        log.info("sites.reconnected", site_id=site_id)
        await self._ctx.audit.record(
            action="site.reconnected",
            result="ok",
            secret_ref=c.secret_ref,
            details={"site_id": site_id},
        )
        return await self._reload(site_id)

    async def check(self, site_id: str) -> SiteOut:
        record, c = await self._load(site_id)
        credentials = await self._credentials(record, c)
        if credentials is None:
            await self._mark_revoked(record, c, SITE_SECRET_MISSING)
            return await self._reload(site_id)
        with credentials:
            async with self._wordpress() as wp:
                try:
                    remote = await wp.status(c.api_root, credentials)
                except FaroError as exc:
                    if exc.code not in VERDICT_CODES:
                        raise
                    await self._mark_revoked(record, c, exc.code)
                    return await self._reload(site_id)
        fields = status_fields(remote)
        fields.update(
            status=STATUS_ACTIVE, last_error_code=None, last_checked_at=self._now(), revoked_at=None
        )
        await self._update(site_id, fields, name=_site_name(remote.site_name), set_name=True)
        return await self._reload(site_id)

    async def list_content(
        self, site_id: str, kind: ContentKind, page: int, per_page: int
    ) -> SiteContentPage:
        record, c = await self._load(site_id)
        credentials = await self._credentials(record, c)
        if credentials is None:
            await self._mark_revoked(record, c, SITE_SECRET_MISSING)
            raise site_error(SITE_SECRET_MISSING)
        with credentials:
            async with self._wordpress() as wp:
                try:
                    remote = await wp.list_content(
                        c.api_root, credentials, kind, page=page, per_page=per_page
                    )
                except FaroError as exc:
                    if exc.code in VERDICT_CODES:
                        await self._mark_revoked(record, c, exc.code)
                    raise
        return SiteContentPage(
            items=[
                SiteContentItem(
                    remote_id=item.id,
                    kind=kind,
                    title=item.title,
                    url=item.url,
                    slug=item.slug,
                    modified_at=item.modified_at,
                )
                for item in remote.items
            ],
            next_cursor=str(page + 1) if page < remote.total_pages else None,
            total=remote.total,
            total_pages=remote.total_pages,
            woocommerce_active=remote.woocommerce_active,
        )

    async def remove(self, site_id: str) -> RemoveSiteOut:
        record, c = await self._load(site_id)
        remote_revoked = False
        credentials = await self._credentials(record, c)
        if credentials is not None:
            with credentials:
                # El `revoke` deja `UNDO_RESERVE_SECONDS` para el `delete` de después.
                revoke = self._deadline.ending_before(UNDO_RESERVE_SECONDS)
                async with self._wordpress(revoke) as wp:
                    try:
                        remote_revoked = await wp.revoke(c.api_root, credentials, retries=1)
                    except FaroError as exc:
                        log.warning("sites.remote_revoke_failed", error_code=exc.code)
        # Si el llavero falla, el sitio se queda: sin su secreto huérfano en el llavero.
        await self._ctx.secrets.delete(c.secret_ref, max_wait=self._deadline.remaining())
        await self._db(lambda conn: repository.delete_site(conn, site_id))
        log.info("sites.removed", site_id=site_id, remote_revoked=remote_revoked)
        await self._ctx.audit.record(
            action="site.removed",
            result="ok",
            secret_ref=c.secret_ref,
            details={"site_id": site_id},
        )
        return RemoveSiteOut(remote_revoked=remote_revoked)
