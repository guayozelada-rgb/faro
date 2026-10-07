"""Export del registro de agentes (fuente de `agent-grants.json` en `npm run contracts`)."""

from __future__ import annotations

import io
import json
import subprocess
import sys

from faro_engine.agents.registry import agent_grants_table
from faro_engine.export_agents import main, render_agents


def test_export_writes_the_registry_table() -> None:
    out = io.BytesIO()
    assert main(out) == 0
    assert json.loads(out.getvalue()) == agent_grants_table()


def test_export_is_deterministic_and_canonical() -> None:
    text = render_agents()
    assert text == render_agents()
    data = json.loads(text)
    expected = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    assert text == expected.encode("utf-8")


def test_export_module_runs_as_script() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "faro_engine.export_agents"],
        capture_output=True,
        check=True,
        timeout=60,
    )
    assert json.loads(result.stdout) == agent_grants_table()
    assert not result.stdout.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in result.stdout
