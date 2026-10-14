"""Lanza `tests.deps.offline_probe` en un proceso aparte con un entorno controlado."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from tests.conftest import ENGINE_DIR

PROBE_TIMEOUT_S = 180
# Variables que podrían cambiar el resultado y que el motor no hereda (informe de T2).
_SCRUBBED_PREFIXES = ("LANGSMITH_", "LANGCHAIN_", "LITELLM_", "CUSTOM_TIKTOKEN", "TIKTOKEN_")
_SCRUBBED = frozenset({"SSL_VERIFY", "SSL_CERT_FILE", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"})
# Proxies del entorno (en cualquier combinación de mayúsculas): la sonda debe ver la red como
# en la CI. Con un proxy en loopback (el contenedor de los agentes), LangSmith y la descarga de
# `cl100k_base` salían a internet a través de él (T12; `tests/network_guard.py`).
_PROXIES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})


@dataclass(frozen=True, slots=True)
class ProbeResult:
    report: dict[str, Any]
    stdout: str
    stderr: str


def run_probe(*args: str, env: dict[str, str] | None = None) -> ProbeResult:
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_SCRUBBED_PREFIXES) and k not in _SCRUBBED and k.upper() not in _PROXIES
    }
    base["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    base.update(env or {})
    completed = subprocess.run(
        [sys.executable, "-m", "tests.deps.offline_probe", *args],
        cwd=ENGINE_DIR,
        env=base,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROBE_TIMEOUT_S,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("FARO_PROBE ")]
    assert completed.returncode == 0, completed.stderr[-3000:]
    assert len(lines) == 1, completed.stdout[-3000:]
    report: dict[str, Any] = json.loads(lines[0].removeprefix("FARO_PROBE "))
    return ProbeResult(report=report, stdout=completed.stdout, stderr=completed.stderr)
