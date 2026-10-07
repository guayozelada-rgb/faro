"""`core/store/autonomy_rules.py`: regla efectiva (sitio → general → nivel 1)."""

from __future__ import annotations

import pytest

from faro_engine.core.db.connection import Connection
from faro_engine.core.store import autonomy_rules as rules
from tests.store.conftest import OTHER_SITE, SITE, T0, T1


def test_default_level_is_one_without_rules(conn: Connection) -> None:
    assert rules.DEFAULT_AUTONOMY_LEVEL == 1
    assert rules.effective_level(conn, "site_summary", SITE) == 1
    assert rules.effective_level(conn, "site_summary", None) == 1
    assert rules.effective_rule(conn, "site_summary", SITE) is None
    assert rules.list_rules(conn) == []


def test_site_rule_overrides_general_rule(conn: Connection) -> None:
    general = rules.set_rule(
        conn, rule_id="r-gen", agent_kind="site_summary", site_id=None, level=2, now=T0
    )
    assert general.previous_level == 1
    assert general.rule.limits == "{}"
    assert rules.effective_level(conn, "site_summary", SITE) == 2  # hereda la general

    site = rules.set_rule(
        conn,
        rule_id="r-site",
        agent_kind="site_summary",
        site_id=SITE,
        level=0,
        now=T0,
        limits={"per_day": 3},
    )
    assert site.previous_level == 2
    assert site.rule.limits == '{"per_day":3}'
    assert rules.effective_level(conn, "site_summary", SITE) == 0
    assert rules.effective_level(conn, "site_summary", OTHER_SITE) == 2
    assert rules.effective_level(conn, "otro", SITE) == 1
    assert [r.id for r in rules.list_rules(conn)] == ["r-gen", "r-site"]


def test_set_rule_updates_in_place(conn: Connection) -> None:
    rules.set_rule(conn, rule_id="r-1", agent_kind="site_summary", site_id=SITE, level=1, now=T0)
    change = rules.set_rule(
        conn, rule_id="r-ignorado", agent_kind="site_summary", site_id=SITE, level=3, now=T1
    )
    assert change.previous_level == 1
    assert (change.rule.id, change.rule.level, change.rule.created_at, change.rule.updated_at) == (
        "r-1",
        3,
        T0,
        T1,
    )
    assert rules.get_rule(conn, "r-ignorado") is None
    assert len(rules.list_rules(conn)) == 1


@pytest.mark.parametrize("level", [-1, 4])
def test_level_out_of_range(conn: Connection, level: int) -> None:
    with pytest.raises(ValueError, match="nivel"):
        rules.set_rule(
            conn, rule_id="r", agent_kind="site_summary", site_id=None, level=level, now=T0
        )


def test_delete_only_site_rules(conn: Connection) -> None:
    rules.set_rule(conn, rule_id="r-gen", agent_kind="site_summary", site_id=None, level=2, now=T0)
    rules.set_rule(conn, rule_id="r-site", agent_kind="site_summary", site_id=SITE, level=0, now=T0)
    assert rules.delete_site_rule(conn, "r-gen") == "default_rule"
    assert rules.delete_site_rule(conn, "no-existe") == "not_found"
    assert rules.delete_site_rule(conn, "r-site") == "deleted"
    assert rules.delete_site_rule(conn, "r-site") == "not_found"
    assert rules.effective_level(conn, "site_summary", SITE) == 2
