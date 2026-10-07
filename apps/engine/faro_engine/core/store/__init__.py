"""Repositorios de las tablas de la migración 0002 (spec F1b §6).

Funciones síncronas que se ejecutan con `Database.run` (un hilo y el candado de la
conexión), igual que `faro_engine/sites/repository.py`. Reglas comunes:

- Las fechas llegan ya formateadas por quien llama (`format_utc`, reloj inyectable):
  UTC ISO-8601 con `Z` y precisión de segundos, para que las comparaciones de texto
  (`expires_at <= ?`, `next_run_at < ?`) sean correctas.
- Las transiciones de estado son `UPDATE … WHERE status = ?` y devuelven `bool`: `False`
  significa que otro camino ya cambió la fila (o no existe); quien llama decide el error.
- Las funciones de varias sentencias usan `atomic`: abren su transacción o se suman a la
  de quien llama, así se pueden componer (p. ej. paso + tarea + uso de la clave).
- Ningún repositorio guarda valores de secretos: solo `secret_ref` de `llm/*`, conteos de
  tokens y montos en micros.
"""
