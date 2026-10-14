"""Hook de PyInstaller para LiteLLM (spec F1b T2 y T11; condición 12 del informe de T2).

LiteLLM carga muchos módulos de forma perezosa por nombre (`importlib`, `_lazy_imports`),
que el análisis estático de PyInstaller no ve. En lugar de añadir todos sus submódulos
(`collect_submodules("litellm")`, unos 1 800), se añade una **lista permitida**:

- **Núcleo:** todos los paquetes de `litellm` salvo los de proveedores (`litellm.llms`) y
  lo que Faro no usa: el proxy (`litellm.proxy`, `litellm.proxy_auth`), el cliente MCP
  experimental y el protocolo A2A (ADR 0015 §7, "nada del proxy de LiteLLM").
- **Proveedores** (`litellm.llms.<x>`): solo los de Faro y la infraestructura común
  (`LLMS_ALLOWED`). Gemini de AI Studio está implementado en `litellm/llms/vertex_ai/gemini`,
  por eso entra `vertex_ai` (Faro nunca llama a Vertex: `api_base` fijo, ADR 0015 §1).

Lo que LiteLLM importa con un `import` normal entra igual por el análisis estático: la lista
solo limita lo que se añade a mano. **Medido en T11 (Linux, LiteLLM 1.104.0):** el análisis
estático ya arrastra casi todo `litellm.llms` (1 227 módulos con esta lista frente a 1 231
con todos los submódulos) y 524 módulos de `litellm.proxy` (que `import litellm` importa),
con el mismo tamaño del paquete. Es decir, esta lista no reduce de forma apreciable lo que
entra; reducirlo de verdad exigiría excluir paquetes que LiteLLM importa al cargarse
(pendiente, ver el informe de T11). La prueba de humo `adapter` (`scripts/bundle_smoke.py`)
llama a los seis modelos del catálogo y a los errores de cada proveedor en el ejecutable: si
falta un módulo, falla. Al subir LiteLLM, hay que volver a lanzar `engine-bundle-smoke`.

Datos: el mapa de precios local (`LITELLM_LOCAL_MODEL_COST_MAP=True`), las cabeceras beta
de Anthropic locales, los tokenizadores y los JSON de configuración. Nada de la interfaz web
del proxy ni documentación.

El puente nativo en Rust (`litellm.rust_bridge._native`, ~45 MB) no se excluye aquí: lo
decide `bundle_smoke_build.py` con `--exclude-module` para poder medir ambas opciones.
"""

from __future__ import annotations

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Paquetes de primer nivel que no entran (Faro no los usa).
EXCLUDED_TOP: frozenset[str] = frozenset(
    {"proxy", "proxy_auth", "experimental_mcp_client", "a2a_protocol"}
)
# `litellm.llms.<x>` que sí entran.
LLMS_ALLOWED: frozenset[str] = frozenset(
    {
        # Proveedores de Faro (Gemini de AI Studio vive en `vertex_ai/gemini`).
        "openai",
        "anthropic",
        "gemini",
        "vertex_ai",
        # Infraestructura común de todos los proveedores.
        "base",
        "base_llm",
        "custom_httpx",
        "custom_llm",
    }
)


def allowed(name: str) -> bool:
    """¿Entra `name` (un submódulo de `litellm`) en la lista permitida?"""
    parts = name.split(".")
    if len(parts) < 2:  # `litellm` solo
        return True
    if parts[1] in EXCLUDED_TOP:
        return False
    if parts[1] == "llms" and len(parts) > 2:  # `litellm.llms.<x>`
        return parts[2] in LLMS_ALLOWED
    return True


hiddenimports = collect_submodules("litellm", filter=allowed, on_error="ignore")

datas = collect_data_files(
    "litellm",
    excludes=["proxy/**", "proxy_auth/**", "**/*.md", "**/README*", "**/*.pyi"],
)
