"""Hook de PyInstaller para tiktoken (spec F1b T2).

tiktoken descubre sus codificaciones como plugins del espacio de nombres `tiktoken_ext`
(`pkgutil.iter_modules`), que el análisis estático no ve: sin esto, el motor empaquetado
falla con "Unknown encoding cl100k_base. Plugins found: []" en la primera llamada de
Anthropic o Gemini (comprobado en T2).
"""

from __future__ import annotations

hiddenimports = ["tiktoken_ext", "tiktoken_ext.openai_public"]
