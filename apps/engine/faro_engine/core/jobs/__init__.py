"""Tareas de agentes en el motor (spec F1b §4.3, ADR 0014 y 0015 §3).

- `control`: estado de `agents_control` (pausado hasta recibirlo) y proveedores con clave.
- `grants`: cliente de `run_grant_request` / `run_grant_release` (futuros por `id`, 10 s).
- `activity`: emisor de `agent_activity` (`seq` por tarea, sin texto libre).
- `runner`: contrato entre el trabajador y los agentes (`AgentDefinition`, `RunContext`,
  `StopSignal`, `RunStopped`); lo implementa `AgentSpec` con el marco de
  `agents/framework` (LangGraph).
- `queue`: cola sobre `agent_runs` (deduplicación, prioridades, planes con dependencias,
  toma condicional, transiciones y actividad con su `seq`).
- Crear tareas (estimado, `startAgentRun`, disparos del programador) es del orquestador
  del marco (`agents/framework/orchestrator.py`), que sustituye al `submit.py` de T7.
- `worker`: un trabajador; concesión, `run_id`, estados finales, tope diario, sitio
  quitado y reintento con espera ante fallos fuera del agente.
- `scheduler`: programaciones con APScheduler desde la tabla `schedules`; `catch_up`.
- `recovery`: al arrancar, `running` → `paused` (`interrupted`) y propuestas caducadas.
- `runtime`: arranque y apagado de todo lo anterior alrededor de `server.serve()`.
"""
