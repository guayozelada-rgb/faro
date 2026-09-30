"""Tablas `sites` y `site_connections` (spec F1a §6). Funciones síncronas que se ejecutan
con `Database.run` (un hilo y el candado de la conexión).

En la base solo quedan la URL, el `connection_id` remoto, `sha256(token)` y metadatos: el
token y el secreto HMAC viven solo en el llavero (`secret_ref`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from faro_engine.core.db.connection import Connection, transaction

KIND_WP_PLUGIN: Final = "wp_plugin"

# Columnas de `site_connections` que se pueden actualizar (lista cerrada: se interpolan
# en el SQL como nombres, nunca como valores).
UPDATABLE_COLUMNS: Final = frozenset(
    {
        "api_root",
        "remote_connection_id",
        "token_sha256",
        "status",
        "last_error_code",
        "plugin_version",
        "wp_version",
        "woocommerce_active",
        "woocommerce_version",
        "hpos_enabled",
        "seo_plugin",
        "pages_count",
        "posts_count",
        "products_count",
        "connected_at",
        "last_checked_at",
        "revoked_at",
    },
)

_CONNECTION_COLUMNS: Final = (
    "id",
    "api_root",
    "remote_connection_id",
    "token_sha256",
    "secret_ref",
    "status",
    "last_error_code",
    "plugin_version",
    "wp_version",
    "woocommerce_active",
    "woocommerce_version",
    "hpos_enabled",
    "seo_plugin",
    "pages_count",
    "posts_count",
    "products_count",
    "connected_at",
    "last_checked_at",
    "revoked_at",
)
# Solo nombres de columna fijos (sin valores).
_SELECT: Final = (
    "SELECT s.id, s.url, s.name, s.created_at, "  # noqa: S608
    + ", ".join(f"c.{column}" for column in _CONNECTION_COLUMNS)
    + " FROM sites s LEFT JOIN site_connections c ON c.site_id = s.id AND c.kind = ?"
)


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    id: str
    api_root: str
    remote_connection_id: str
    token_sha256: str
    secret_ref: str
    status: str
    last_error_code: str | None = None
    plugin_version: str | None = None
    wp_version: str | None = None
    woocommerce_active: int | None = None
    woocommerce_version: str | None = None
    hpos_enabled: int | None = None
    seo_plugin: str | None = None
    pages_count: int | None = None
    posts_count: int | None = None
    products_count: int | None = None
    connected_at: str = ""
    last_checked_at: str | None = None
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True)
class SiteRecord:
    id: str
    url: str
    name: str | None
    created_at: str
    connection: ConnectionRecord | None


def _record(row: Sequence[Any]) -> SiteRecord:
    site_id, url, name, created_at, *rest = row
    connection = None if rest[0] is None else ConnectionRecord(*rest)
    return SiteRecord(id=site_id, url=url, name=name, created_at=created_at, connection=connection)


def list_sites(conn: Connection) -> list[SiteRecord]:
    """Todos los sitios, por fecha de conexión (o de alta si no tienen conexión)."""
    rows = conn.execute(
        _SELECT + " ORDER BY COALESCE(c.connected_at, s.created_at), s.id", (KIND_WP_PLUGIN,)
    ).fetchall()
    return [_record(row) for row in rows]


def get_site(conn: Connection, site_id: str) -> SiteRecord | None:
    row = conn.execute(_SELECT + " WHERE s.id = ?", (KIND_WP_PLUGIN, site_id)).fetchone()
    return None if row is None else _record(row)


def find_site_id_by_url(conn: Connection, url: str) -> str | None:
    row = conn.execute("SELECT id FROM sites WHERE url = ?", (url,)).fetchone()
    return None if row is None else str(row[0])


def insert_site(conn: Connection, site: SiteRecord, updated_at: str) -> None:
    """Inserta el sitio y su conexión en una sola transacción."""
    connection = site.connection
    if connection is None:
        raise ValueError("un sitio de F1a siempre tiene conexión")
    columns = ("site_id", "kind", *_CONNECTION_COLUMNS)
    values = (site.id, KIND_WP_PLUGIN, *(getattr(connection, c) for c in _CONNECTION_COLUMNS))
    with transaction(conn):
        conn.execute(
            "INSERT INTO sites (id, url, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (site.id, site.url, site.name, site.created_at, updated_at),
        )
        conn.execute(
            f"INSERT INTO site_connections ({', '.join(columns)}) "  # noqa: S608 - nombres fijos
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )


def update_site(
    conn: Connection,
    site_id: str,
    fields: Mapping[str, object],
    *,
    updated_at: str,
    name: str | None = None,
    set_name: bool = False,
) -> None:
    """Actualiza columnas de la conexión (y el nombre del sitio) en una transacción."""
    unknown = set(fields) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"columnas no actualizables: {sorted(unknown)}")
    with transaction(conn):
        if fields:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            conn.execute(
                f"UPDATE site_connections SET {assignments} "  # noqa: S608 - lista cerrada
                "WHERE site_id = ? AND kind = ?",
                (*fields.values(), site_id, KIND_WP_PLUGIN),
            )
        if set_name:
            conn.execute(
                "UPDATE sites SET name = ?, updated_at = ? WHERE id = ?",
                (name, updated_at, site_id),
            )
        else:
            conn.execute("UPDATE sites SET updated_at = ? WHERE id = ?", (updated_at, site_id))


def delete_site(conn: Connection, site_id: str) -> bool:
    """Borra el sitio (y su conexión, en cascada). `False` si no existía."""
    with transaction(conn):
        cursor = conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    return int(cursor.rowcount) > 0
