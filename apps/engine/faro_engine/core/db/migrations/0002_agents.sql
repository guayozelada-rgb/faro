-- 0002: tareas de agentes, aprobaciones, autonomía, programaciones, uso y límites de las
-- claves de IA, ajustes, resúmenes del sitio y checkpoints de LangGraph (spec F1b §6,
-- ADR 0014, 0015 y 0016).
-- Nunca edites esta migración una vez publicada: crea una nueva.
-- Solo agrega tablas: el piso de compatibilidad sigue en 1 (`COMPATIBILITY_FLOORS`), así
-- una app de F1a abre la base migrada sin tocarla (`newer_schema`).
-- Ninguna tabla guarda valores de secretos, prompts completos ni respuestas crudas del
-- proveedor: solo referencias (`secret_ref`), identificadores de prompt, conteos de tokens
-- y montos en micros.
-- Excepción consciente a "clave primaria `id`": las dos tablas de checkpoints usan la
-- clave compuesta que espera LangGraph.

CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY,
  agent_kind TEXT NOT NULL,
  site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  cadence TEXT NOT NULL CHECK (cadence IN ('daily', 'weekly')),
  weekday INTEGER CHECK (weekday BETWEEN 0 AND 6),           -- 0 = lunes
  time_local TEXT NOT NULL,                                   -- 'HH:MM'
  timezone TEXT NOT NULL,                                     -- IANA
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  next_run_at TEXT NOT NULL,
  last_run_at TEXT,
  last_run_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK ((cadence = 'weekly') = (weekday IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS schedules_agent_site_uq ON schedules(agent_kind, site_id);

CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,                    -- también thread_id de LangGraph y run_id de la concesión
  parent_run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
  agent_kind TEXT NOT NULL,
  agent_version INTEGER NOT NULL,
  objective TEXT NOT NULL,
  site_id TEXT REFERENCES sites(id) ON DELETE SET NULL,
  trigger TEXT NOT NULL CHECK (trigger IN ('user', 'schedule', 'catch_up')),
  schedule_id TEXT REFERENCES schedules(id) ON DELETE SET NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'waiting_approval', 'paused',
                                         'succeeded', 'failed', 'cancelled')),
  status_reason TEXT,                     -- agents_paused, interrupted, daily_limit, user_cancelled…
  error_code TEXT,
  priority INTEGER NOT NULL DEFAULT 2,
  provider TEXT CHECK (provider IN ('anthropic', 'openai', 'gemini')),
  current_step TEXT,
  token_budget INTEGER NOT NULL CHECK (token_budget > 0),
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_micros INTEGER NOT NULL DEFAULT 0,
  estimated_cost_micros INTEGER,
  max_cost_micros INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
  result TEXT,                            -- JSON del esquema del agente; nunca secretos
  activity_seq INTEGER NOT NULL DEFAULT 0,
  notice_ack_at TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS agent_runs_queue_idx ON agent_runs(status, priority, created_at);
CREATE INDEX IF NOT EXISTS agent_runs_created_idx ON agent_runs(created_at);

CREATE TABLE IF NOT EXISTS agent_steps (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  node TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('llm_call', 'tool_call', 'approval', 'control')),
  status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'skipped', 'cancelled')),
  idempotency_key TEXT NOT NULL,
  autonomy_decision TEXT CHECK (autonomy_decision IN ('suggest', 'propose', 'execute')),
  provider TEXT CHECK (provider IN ('anthropic', 'openai', 'gemini')),
  model TEXT,
  tier TEXT CHECK (tier IN ('economy', 'premium')),
  secret_ref TEXT,                        -- llm/<proveedor>/default; nunca el valor
  prompt_id TEXT,
  prompt_version INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_micros INTEGER NOT NULL DEFAULT 0,
  cost_estimated INTEGER NOT NULL DEFAULT 0 CHECK (cost_estimated IN (0, 1)),
  currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
  error_code TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS agent_steps_run_seq_uq ON agent_steps(run_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS agent_steps_idempotency_uq ON agent_steps(idempotency_key);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  step_id TEXT REFERENCES agent_steps(id) ON DELETE SET NULL,
  site_id TEXT REFERENCES sites(id) ON DELETE SET NULL,
  agent_kind TEXT NOT NULL,
  action_kind TEXT NOT NULL,
  side_effect TEXT NOT NULL CHECK (side_effect IN ('internal', 'publish', 'spend')),
  autonomy_level INTEGER NOT NULL CHECK (autonomy_level BETWEEN 0 AND 3),
  status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired',
                                         'cancelled', 'executed', 'failed')),
  decided_by TEXT CHECK (decided_by IN ('user', 'rule')),
  payload TEXT NOT NULL,                  -- JSON validado por el esquema de action_kind
  evidence TEXT NOT NULL DEFAULT '{}',
  estimated_cost_micros INTEGER,
  currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
  idempotency_key TEXT NOT NULL,
  previous_value TEXT,                    -- para deshacer (F4)
  error_code TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  decided_at TEXT,
  executed_at TEXT,
  updated_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS approvals_idempotency_uq ON approvals(idempotency_key);
CREATE INDEX IF NOT EXISTS approvals_status_idx ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS autonomy_rules (
  id TEXT PRIMARY KEY,
  agent_kind TEXT NOT NULL,
  site_id TEXT REFERENCES sites(id) ON DELETE CASCADE,   -- NULL = todos los sitios
  level INTEGER NOT NULL CHECK (level BETWEEN 0 AND 3),
  limits TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS autonomy_rules_default_uq ON autonomy_rules(agent_kind) WHERE site_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS autonomy_rules_site_uq ON autonomy_rules(agent_kind, site_id) WHERE site_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS credential_usage (
  id TEXT PRIMARY KEY,
  secret_ref TEXT NOT NULL,               -- llm/<proveedor>/default
  provider TEXT NOT NULL CHECK (provider IN ('anthropic', 'openai', 'gemini')),
  usage_date TEXT NOT NULL,               -- día local 'AAAA-MM-DD'
  requests INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_micros INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
  updated_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS credential_usage_ref_date_uq ON credential_usage(secret_ref, usage_date);

CREATE TABLE IF NOT EXISTS credential_limits (
  id TEXT PRIMARY KEY,
  secret_ref TEXT NOT NULL,
  daily_limit_micros INTEGER NOT NULL CHECK (daily_limit_micros BETWEEN 500000 AND 500000000),
  currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
  updated_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS credential_limits_ref_uq ON credential_limits(secret_ref);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,                   -- lista cerrada en código (llm.preferred_provider)
  value TEXT NOT NULL,                    -- JSON
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS site_summaries (
  id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
  approval_id TEXT REFERENCES approvals(id) ON DELETE SET NULL,
  content TEXT NOT NULL,                  -- JSON SiteSummaryV1
  ai_generated INTEGER NOT NULL DEFAULT 1 CHECK (ai_generated IN (0, 1)),
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS site_summaries_site_idx ON site_summaries(site_id, created_at);

CREATE TABLE IF NOT EXISTS agent_checkpoints (
  thread_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  checkpoint_ns TEXT NOT NULL DEFAULT '',
  checkpoint_id TEXT NOT NULL,
  parent_checkpoint_id TEXT,
  type TEXT NOT NULL,
  checkpoint BLOB NOT NULL,
  metadata BLOB NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
) STRICT;

CREATE TABLE IF NOT EXISTS agent_checkpoint_writes (
  thread_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  checkpoint_ns TEXT NOT NULL DEFAULT '',
  checkpoint_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  task_path TEXT NOT NULL DEFAULT '',
  idx INTEGER NOT NULL,
  channel TEXT NOT NULL,
  type TEXT NOT NULL,
  value BLOB NOT NULL,
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
) STRICT;
