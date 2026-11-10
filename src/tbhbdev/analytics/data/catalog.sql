WITH schemas AS (
  SELECT oid, nspname FROM pg_namespace
  WHERE nspname IN ('raw', 'staging', 'model', 'metric', 'mart', 'meta')
), relations AS (
  SELECT n.nspname || '.' || c.relname AS name, c.relkind AS kind,
    (SELECT jsonb_agg(jsonb_build_object('name', a.attname, 'type', format_type(a.atttypid, a.atttypmod)) ORDER BY a.attnum)
     FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped) AS columns
  FROM pg_class c JOIN schemas n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'v', 'm', 'p')
), functions AS (
  SELECT n.nspname || '.' || p.proname AS name, pg_get_function_identity_arguments(p.oid) AS arguments,
    pg_get_function_result(p.oid) AS returns
  FROM pg_proc p JOIN schemas n ON n.oid = p.pronamespace
)
SELECT jsonb_build_object(
  'schemas', (SELECT coalesce(jsonb_agg(nspname ORDER BY nspname), '[]') FROM schemas),
  'relations', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY name), '[]') FROM relations r),
  'functions', (SELECT coalesce(jsonb_agg(to_jsonb(f) ORDER BY name), '[]') FROM functions f));
