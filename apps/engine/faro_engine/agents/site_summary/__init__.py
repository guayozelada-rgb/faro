"""Agente de demostración «Resumen del sitio» (`site_summary`, spec F1b §4.4).

Lee el sitio conectado en vivo, clasifica su contenido con el modelo económico, redacta un
resumen con el potente y propone guardarlo en Faro (acción `site_summary.save`,
`internal`). No publica ni gasta nada fuera de la llamada al LLM.

| Módulo | Qué tiene |
| --- | --- |
| `spec.py` | `SPEC` (`AgentSpec`) que registra `agents/registry.py` |
| `graph.py` | nodos y aristas |
| `state.py` | `SiteSummaryState` (solo tipos JSON) |
| `schemas.py` | `ContentClassificationV1`, `SiteSummaryV1`, payload de la acción |
| `tools.py` | herramienta de lectura `read_site_content` (la llama el nodo, no el modelo) |
| `actions.py` | acción `site_summary.save` |
| `estimate.py` | estimado, máximo garantizado y presupuesto de tokens |
| `limits.py` | tamaños de los datos (compartidos por los nodos y el estimado) |
| `fake.py` | respuestas del modo `--fake-llm` (solo desarrollo) |
| `prompts/` | `classify_content.v1.md`, `write_summary.v1.md` |

Este `__init__` no importa nada: `core/schemas` usa `schemas.py` sin cargar el agente.
"""
