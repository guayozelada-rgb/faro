-- 0001: tablas iniciales de F1a (spec F1a §6, ADR 0009).
-- Nunca edites esta migración una vez publicada: crea una nueva.
-- `schema_migrations` y el piso `PRAGMA user_version` los gestiona el ejecutor.

CREATE TABLE IF NOT EXISTS sites (
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL,               -- normalizada (ADR 0012), sin barra final
  name TEXT,                       -- nombre del sitio desde /status
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS sites_url_uq ON sites(url);

CREATE TABLE IF NOT EXISTS site_connections (
  id TEXT PRIMARY KEY,             -- se envía como app_instance_id
  site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('wp_plugin')),
  api_root TEXT NOT NULL,          -- https://…/wp-json/ o https://…/?rest_route=
  remote_connection_id TEXT NOT NULL,
  token_sha256 TEXT NOT NULL,      -- hex; el token vive solo en el llavero
  secret_ref TEXT NOT NULL,        -- wp/<site_id>/token
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
  last_error_code TEXT,
  plugin_version TEXT,
  wp_version TEXT,
  woocommerce_active INTEGER CHECK (woocommerce_active IN (0, 1)),
  woocommerce_version TEXT,
  hpos_enabled INTEGER CHECK (hpos_enabled IN (0, 1)),
  seo_plugin TEXT CHECK (seo_plugin IN ('yoast', 'rank_math', 'none')),
  pages_count INTEGER,
  posts_count INTEGER,
  products_count INTEGER,
  connected_at TEXT NOT NULL,
  last_checked_at TEXT,
  revoked_at TEXT
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS site_connections_site_kind_uq ON site_connections(site_id, kind);

-- Solo se inserta; sin clave foránea a `sites` para que sobreviva a quitar un sitio.
-- Nunca guarda valores de secretos ni `last4`.
CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY,
  occurred_at TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('user', 'agent', 'system')),
  action TEXT NOT NULL,
  secret_ref TEXT,
  run_id TEXT,
  result TEXT NOT NULL CHECK (result IN ('ok', 'denied', 'error')),
  details TEXT NOT NULL DEFAULT '{}'
) STRICT;
CREATE INDEX IF NOT EXISTS audit_log_occurred_at_idx ON audit_log(occurred_at);
