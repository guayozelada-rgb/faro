"""Tabla `autonomy_rules` (ADR 0016 §2, spec F1b §6).

Regla efectiva = la del agente **y** sitio si existe; si no, la general del agente
(`site_id IS NULL`); si no, `DEFAULT_AUTONOMY_LEVEL` (nivel 1, "Preparar y pedirte OK").
Los guardarraíles (tope por clase de efecto, pausa global) los aplica
`agents/framework/autonomy.py`; aquí solo se guarda y se lee la regla.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from faro_engine.core.db.connection import Connection
from faro_engine.core.store.common import atomic, to_json

DEFAULT_AUTONOMY_LEVEL: Final = 1
MIN_LEVEL: Final = 0
MAX_LEVEL: Final = 3

DeleteOutcome = Literal["deleted", "not_found", "default_rule"]

_COLUMNS: Final = ("id", "agent_kind", "site_id", "level", "limits", "created_at", "updated_at")
_SELECT: Final = f"SELECT {', '.join(_COLUMNS)} FROM autonomy_rules"  # noqa: S608


@dataclass(frozen=True, slots=True)
class AutonomyRuleRecord:
    id: str
    agent_kind: str
    site_id: str | None
    level: int
    limits: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RuleChange:
    rule: AutonomyRuleRecord
    previous_level: int  # el nivel efectivo antes del cambio (1 si no había regla)


def check_level(level: int) -> int:
    if not MIN_LEVEL <= level <= MAX_LEVEL:
        raise ValueError("nivel de autonomía fuera de rango")
    return level


def _find(conn: Connection, agent_kind: str, site_id: str | None) -> AutonomyRuleRecord | None:
    row = conn.execute(
        _SELECT + " WHERE agent_kind = ? AND site_id IS ?", (agent_kind, site_id)
    ).fetchone()
    return None if row is None else AutonomyRuleRecord(*row)


def effective_rule(
    conn: Connection, agent_kind: str, site_id: str | None
) -> AutonomyRuleRecord | None:
    """Regla del sitio, si no la general; `None` = sin regla (nivel predeterminado)."""
    if site_id is not None and (rule := _find(conn, agent_kind, site_id)) is not None:
        return rule
    return _find(conn, agent_kind, None)


def effective_level(conn: Connection, agent_kind: str, site_id: str | None) -> int:
    rule = effective_rule(conn, agent_kind, site_id)
    return DEFAULT_AUTONOMY_LEVEL if rule is None else rule.level


def get_rule(conn: Connection, rule_id: str) -> AutonomyRuleRecord | None:
    row = conn.execute(_SELECT + " WHERE id = ?", (rule_id,)).fetchone()
    return None if row is None else AutonomyRuleRecord(*row)


def list_rules(conn: Connection) -> list[AutonomyRuleRecord]:
    """Generales primero y después por sitio, ordenadas por agente."""
    rows = conn.execute(
        _SELECT + " ORDER BY agent_kind, site_id IS NOT NULL, site_id, id"
    ).fetchall()
    return [AutonomyRuleRecord(*row) for row in rows]


def set_rule(
    conn: Connection,
    *,
    rule_id: str,
    agent_kind: str,
    site_id: str | None,
    level: int,
    now: str,
    limits: Mapping[str, Any] | None = None,
) -> RuleChange:
    """Crea o cambia la regla de (agente, sitio). `rule_id` solo se usa si es nueva."""
    check_level(level)
    limits_json = to_json(limits or {})
    with atomic(conn):
        previous_level = effective_level(conn, agent_kind, site_id)
        existing = _find(conn, agent_kind, site_id)
        if existing is None:
            conn.execute(
                "INSERT INTO autonomy_rules (id, agent_kind, site_id, level, limits, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rule_id, agent_kind, site_id, level, limits_json, now, now),
            )
            target = rule_id
        else:
            conn.execute(
                "UPDATE autonomy_rules SET level = ?, limits = ?, updated_at = ? WHERE id = ?",
                (level, limits_json, now, existing.id),
            )
            target = existing.id
        rule = get_rule(conn, target)
    assert rule is not None  # noqa: S101 - recién escrita en la misma transacción
    return RuleChange(rule, previous_level)


def delete_site_rule(conn: Connection, rule_id: str) -> DeleteOutcome:
    """Quita una regla de sitio. La general no se borra (`autonomy.default_rule`)."""
    with atomic(conn):
        rule = get_rule(conn, rule_id)
        if rule is None:
            return "not_found"
        if rule.site_id is None:
            return "default_rule"
        conn.execute("DELETE FROM autonomy_rules WHERE id = ?", (rule_id,))
    return "deleted"
