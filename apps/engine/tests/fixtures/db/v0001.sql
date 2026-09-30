-- Datos de ejemplo del esquema v0001 (spec F1a §6). Sin datos personales ni secretos:
-- dominios .test, hash de relleno y referencias con UUID de prueba. Sirve para probar la
-- migración 0002 sobre una base con datos cuando exista.

INSERT INTO sites (id, url, name, created_at, updated_at) VALUES (
  '01920000-0000-7000-8000-00000000a001', 'https://tienda-ejemplo.test', 'Tienda de ejemplo',
  '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z'
);

INSERT INTO site_connections (
  id, site_id, kind, api_root, remote_connection_id, token_sha256, secret_ref, status,
  last_error_code, plugin_version, wp_version, woocommerce_active, woocommerce_version,
  hpos_enabled, seo_plugin, pages_count, posts_count, products_count, connected_at,
  last_checked_at, revoked_at
) VALUES (
  '01920000-0000-7000-8000-00000000b001', '01920000-0000-7000-8000-00000000a001',
  'wp_plugin', 'https://tienda-ejemplo.test/wp-json/', 'remote-ejemplo-1',
  'test-hash-de-relleno', 'wp/01920000-0000-7000-8000-00000000a001/token', 'active',
  NULL, '0.1.0', '6.6', 1, '9.3.0', 1, 'none', 4, 10, 25, '2026-09-30T12:00:00Z',
  '2026-09-30T12:05:00Z', NULL
);

INSERT INTO audit_log (id, occurred_at, actor, action, secret_ref, run_id, result, details)
VALUES (
  '01920000-0000-7000-8000-00000000c001', '2026-09-30T12:00:00Z', 'system', 'secret.added',
  'wp/01920000-0000-7000-8000-00000000a001/token', '01920000-0000-7000-8000-00000000d001',
  'ok', '{"site_id":"01920000-0000-7000-8000-00000000a001"}'
);

INSERT INTO audit_log (id, occurred_at, actor, action, secret_ref, run_id, result)
VALUES (
  '01920000-0000-7000-8000-00000000c002', '2026-09-30T12:00:01Z', 'user', 'site.connected',
  NULL, NULL, 'ok'
);
