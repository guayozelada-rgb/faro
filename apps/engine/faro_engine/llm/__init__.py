"""Capa de IA del motor (spec F1b §4.1, ADR 0015 §1, skill `capa-llm`).

Todo pasa por `LlmService.call` (`service.py`). Solo `litellm_client.py` importa LiteLLM, y se
carga de forma perezosa en la primera llamada real (nunca antes de `ready`).
"""
