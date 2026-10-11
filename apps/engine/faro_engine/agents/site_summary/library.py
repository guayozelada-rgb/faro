"""Prompts versionados de `site_summary` (`prompts/<nombre>.v<N>.md`, skill
`prompts-y-evals`). Se cargan y validan al importar: si un prompt no cumple las reglas del
cargador, el motor no arranca."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from faro_engine.agents.framework.prompts import PromptLibrary
from faro_engine.agents.site_summary.schemas import SCHEMAS

AGENT: Final = "site_summary"
PROMPTS_DIR: Final = Path(__file__).with_name("prompts")
PROMPTS: Final = PromptLibrary(PROMPTS_DIR, agent=AGENT, schemas=SCHEMAS)
