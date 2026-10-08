"""Tareas de agentes en el motor (spec F1b §4.3, ADR 0014).

- `control`: estado de `agents_control` (pausado hasta recibirlo) y proveedores con clave.
- `grants`: cliente de `run_grant_request` / `run_grant_release` (futuros por `id`, 10 s).
- `activity`: emisor de `agent_activity` (`seq` por tarea, sin texto libre).

La cola, el trabajador, el programador y la recuperación llegan en T7.
"""
