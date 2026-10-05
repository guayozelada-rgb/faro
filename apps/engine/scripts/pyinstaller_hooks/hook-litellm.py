"""Hook de PyInstaller para LiteLLM (spec F1b T2, ADR 0015 §6 criterios 6 y 7).

LiteLLM carga muchos módulos de forma perezosa por nombre (`importlib`), que el análisis
estático de PyInstaller no ve: se incluyen todos sus submódulos salvo el proxy, que Faro no
usa (ADR 0015 §7, "nada del proxy de LiteLLM"). `import litellm` sí importa algunos módulos
de `litellm.proxy`; esos entran igual por el análisis estático.

Datos: el mapa de precios local (`LITELLM_LOCAL_MODEL_COST_MAP=True`), los tokenizadores y
los JSON de configuración. Nada de la interfaz web del proxy ni documentación.

El puente nativo en Rust (`litellm.rust_bridge._native`, ~45 MB) no se excluye aquí: lo
decide `bundle_smoke_build.py` con `--exclude-module` para poder medir ambas opciones.
"""

from __future__ import annotations

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules(
    "litellm",
    filter=lambda name: not name.startswith("litellm.proxy"),
    on_error="ignore",
)

datas = collect_data_files(
    "litellm",
    excludes=["proxy/**", "**/*.md", "**/README*", "**/*.pyi"],
)
