"""Tareas de agentes en el motor (spec F1b §4.3, ADR 0014 y 0015 §3).

- `control`: estado de `agents_control` (pausado hasta recibirlo) y proveedores con clave.
- `grants`: cliente de `run_grant_request` / `run_grant_release` (futuros por `id`, 10 s).
- `activity`: emisor de `agent_activity` (`seq` por tarea, sin texto libre).
- `runner`: contrato entre el trabajador y los agentes (`AgentDefinition`, `RunContext`,
  `StopSignal`, `RunStopped`); T8 lo implementa con LangGraph.
- `queue`: cola sobre `agent_runs` (deduplicación, prioridades, toma condicional,
  transiciones con su evento).
- `submit`: crear tareas (estimado, `startAgentRun`, disparos del programador).
- `worker`: un trabajador; concesión, `run_id`, estados finales, tope diario.
- `scheduler`: programaciones con APScheduler desde la tabla `schedules`; `catch_up`.
- `recovery`: al arrancar, `running` → `paused` (`interrupted`) y propuestas caducadas.
- `runtime`: arranque y apagado de todo lo anterior alrededor de `server.serve()`.
"""
