-- Datos de ejemplo del esquema v0002 (spec F1b §6). Se carga después de `v0001.sql` (usa
-- su sitio). Sin datos personales ni secretos: dominios .test, UUID de prueba, solo
-- referencias `llm/<proveedor>/default` y montos en micros. Sirve para probar la
-- migración 0003 sobre una base con datos cuando exista.

INSERT INTO schedules (
  id, agent_kind, site_id, cadence, weekday, time_local, timezone, enabled, next_run_at,
  last_run_at, last_run_id, created_at, updated_at
) VALUES (
  '01920000-0000-7000-8000-00000000e001', 'site_summary',
  '01920000-0000-7000-8000-00000000a001', 'weekly', 0, '09:00', 'America/Lima', 1,
  '2026-10-12T14:00:00Z', '2026-10-05T14:00:00Z', '01920000-0000-7000-8000-00000000f002',
  '2026-10-01T12:00:00Z', '2026-10-05T14:00:00Z'
);

-- Tarea del usuario terminada, con su resultado.
INSERT INTO agent_runs (
  id, parent_run_id, agent_kind, agent_version, objective, site_id, trigger, schedule_id,
  status, status_reason, error_code, priority, provider, current_step, token_budget,
  tokens_in, tokens_out, cost_micros, estimated_cost_micros, max_cost_micros, currency,
  result, activity_seq, notice_ack_at, created_at, started_at, finished_at, updated_at
) VALUES (
  '01920000-0000-7000-8000-00000000f001', NULL, 'site_summary', 1, 'site_summary.run',
  '01920000-0000-7000-8000-00000000a001', 'user', NULL, 'succeeded', NULL, NULL, 0,
  'anthropic', 'save_summary', 12000, 3100, 900, 7600, 7000, 24000, 'USD',
  '{"kind":"site_summary","suggestion_only":false,"summary":{"audience":"Personas que compran en línea","headline":"Tienda de ejemplo","language":"es","offerings":["Productos de ejemplo"],"summary":"Resumen de prueba.","version":1}}',
  9, NULL, '2026-10-02T12:00:00Z', '2026-10-02T12:00:01Z', '2026-10-02T12:01:00Z',
  '2026-10-02T12:01:00Z'
);

-- Tarea programada que espera una decisión en la Bandeja.
INSERT INTO agent_runs (
  id, agent_kind, agent_version, objective, site_id, trigger, schedule_id, status, priority,
  provider, current_step, token_budget, tokens_in, tokens_out, cost_micros,
  estimated_cost_micros, max_cost_micros, activity_seq, created_at, started_at, updated_at
) VALUES (
  '01920000-0000-7000-8000-00000000f002', 'site_summary', 1, 'site_summary.run',
  '01920000-0000-7000-8000-00000000a001', 'schedule', '01920000-0000-7000-8000-00000000e001',
  'waiting_approval', 2, 'openai', 'propose_save', 12000, 2800, 850, 6100, 7000, 24000, 6,
  '2026-10-05T14:00:00Z', '2026-10-05T14:00:02Z', '2026-10-05T14:01:00Z'
);

-- Tarea recuperada al abrir (`catch_up`) en cola, con el aviso sin confirmar.
INSERT INTO agent_runs (
  id, agent_kind, agent_version, objective, site_id, trigger, status, status_reason,
  priority, token_budget, max_cost_micros, created_at, updated_at
) VALUES (
  '01920000-0000-7000-8000-00000000f003', 'site_summary', 1, 'site_summary.run', NULL,
  'catch_up', 'queued', 'daily_limit', 2, 12000, 24000, '2026-10-06T08:00:00Z',
  '2026-10-06T08:00:00Z'
);

INSERT INTO agent_steps (
  id, run_id, seq, node, kind, status, idempotency_key, autonomy_decision, provider, model,
  tier, secret_ref, prompt_id, prompt_version, attempts, tokens_in, tokens_out, cost_micros,
  cost_estimated, currency, error_code, started_at, finished_at
) VALUES (
  '01920000-0000-7000-8000-000000001001', '01920000-0000-7000-8000-00000000f001', 1,
  'classify_content', 'llm_call', 'succeeded', '01920000-0000-7000-8000-000000002001', NULL,
  'anthropic', 'modelo-economico-de-prueba', 'economy', 'llm/anthropic/default',
  'site_summary.classify', 1, 1, 2000, 400, 4000, 0, 'USD', NULL, '2026-10-02T12:00:10Z',
  '2026-10-02T12:00:20Z'
);

INSERT INTO agent_steps (
  id, run_id, seq, node, kind, status, idempotency_key, autonomy_decision, started_at,
  finished_at
) VALUES (
  '01920000-0000-7000-8000-000000001002', '01920000-0000-7000-8000-00000000f002', 1,
  'propose_save', 'approval', 'succeeded', '01920000-0000-7000-8000-000000002002', 'propose',
  '2026-10-05T14:00:50Z', '2026-10-05T14:01:00Z'
);

INSERT INTO approvals (
  id, run_id, step_id, site_id, agent_kind, action_kind, side_effect, autonomy_level,
  status, decided_by, payload, evidence, estimated_cost_micros, currency, idempotency_key,
  previous_value, error_code, created_at, expires_at, decided_at, executed_at, updated_at
) VALUES (
  '01920000-0000-7000-8000-000000003001', '01920000-0000-7000-8000-00000000f001', NULL,
  '01920000-0000-7000-8000-00000000a001', 'site_summary', 'site_summary.save', 'internal', 1,
  'executed', 'user',
  '{"kind":"site_summary.save","summary":{"audience":"Personas que compran en línea","headline":"Tienda de ejemplo","language":"es","offerings":["Productos de ejemplo"],"summary":"Resumen de prueba.","version":1}}',
  '{"reason_key":"site_summary.new"}', NULL, 'USD', '01920000-0000-7000-8000-000000004001',
  NULL, NULL, '2026-10-02T12:00:50Z', '2026-10-16T12:00:50Z', '2026-10-02T12:00:55Z',
  '2026-10-02T12:01:00Z', '2026-10-02T12:01:00Z'
);

INSERT INTO approvals (
  id, run_id, step_id, site_id, agent_kind, action_kind, side_effect, autonomy_level,
  status, payload, idempotency_key, created_at, expires_at, updated_at
) VALUES (
  '01920000-0000-7000-8000-000000003002', '01920000-0000-7000-8000-00000000f002',
  '01920000-0000-7000-8000-000000001002', '01920000-0000-7000-8000-00000000a001',
  'site_summary', 'site_summary.save', 'internal', 1, 'pending',
  '{"kind":"site_summary.save","summary":{"audience":"Público de prueba","headline":"Tienda de ejemplo","language":"es","offerings":[],"summary":"Otro resumen de prueba.","version":1}}',
  '01920000-0000-7000-8000-000000004002', '2026-10-05T14:01:00Z', '2026-10-19T14:01:00Z',
  '2026-10-05T14:01:00Z'
);

INSERT INTO autonomy_rules (id, agent_kind, site_id, level, limits, created_at, updated_at)
VALUES (
  '01920000-0000-7000-8000-000000005001', 'site_summary', NULL, 1, '{}',
  '2026-10-01T12:00:00Z', '2026-10-01T12:00:00Z'
);

INSERT INTO autonomy_rules (id, agent_kind, site_id, level, created_at, updated_at)
VALUES (
  '01920000-0000-7000-8000-000000005002', 'site_summary',
  '01920000-0000-7000-8000-00000000a001', 0, '2026-10-01T12:05:00Z', '2026-10-01T12:05:00Z'
);

INSERT INTO credential_usage (
  id, secret_ref, provider, usage_date, requests, tokens_in, tokens_out, cost_micros,
  currency, updated_at
) VALUES (
  '01920000-0000-7000-8000-000000006001', 'llm/anthropic/default', 'anthropic', '2026-10-02',
  2, 3100, 900, 7600, 'USD', '2026-10-02T12:01:00Z'
);

INSERT INTO credential_limits (id, secret_ref, daily_limit_micros, currency, updated_at)
VALUES (
  '01920000-0000-7000-8000-000000007001', 'llm/openai/default', 10000000, 'USD',
  '2026-10-01T12:00:00Z'
);

INSERT INTO settings (key, value, updated_at)
VALUES ('llm.preferred_provider', '"anthropic"', '2026-10-01T12:00:00Z');

INSERT INTO settings (key, value, updated_at)
VALUES ('approvals.expiry_days', '14', '2026-10-01T12:00:00Z');

INSERT INTO site_summaries (id, site_id, run_id, approval_id, content, ai_generated, created_at)
VALUES (
  '01920000-0000-7000-8000-000000008001', '01920000-0000-7000-8000-00000000a001',
  '01920000-0000-7000-8000-00000000f001', '01920000-0000-7000-8000-000000003001',
  '{"audience":"Personas que compran en línea","headline":"Tienda de ejemplo","language":"es","offerings":["Productos de ejemplo"],"summary":"Resumen de prueba.","version":1}',
  1, '2026-10-02T12:01:00Z'
);

-- Checkpoint de la tarea en espera (bytes JSON de relleno: `{"v":1}`).
INSERT INTO agent_checkpoints (
  thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata,
  created_at
) VALUES (
  '01920000-0000-7000-8000-00000000f002', '', '1f0a0000-0000-6000-8000-000000000001', NULL,
  'json', X'7B2276223A317D', X'7B7D', '2026-10-05T14:01:00Z'
);

INSERT INTO agent_checkpoint_writes (
  thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, idx, channel, type, value
) VALUES (
  '01920000-0000-7000-8000-00000000f002', '', '1f0a0000-0000-6000-8000-000000000001',
  'tarea-de-prueba', '', 0, '__interrupt__', 'json', X'7B7D'
);
