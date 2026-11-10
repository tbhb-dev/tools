-- All timestamps and names are placeholders. Conflict updates make this reusable.
INSERT INTO meta.repo VALUES ('fixture-a', 'release'), ('fixture-b', 'apply')
ON CONFLICT (repo) DO UPDATE SET kind = excluded.kind;
INSERT INTO raw.git_commit
SELECT 'fixture-a', 'sha-' || n, '2026-10-01Z'::timestamptz + (n - 1) * interval '1 day',
       'fixture commit', CASE WHEN n = 0 THEN ARRAY[]::text[] ELSE ARRAY['sha-' || (n - 1)] END,
       NULL, meta.as_of()
FROM generate_series(0, 12) n
ON CONFLICT (repo, sha) DO UPDATE SET parents = excluded.parents;
INSERT INTO raw.git_tag
SELECT 'fixture-a', 'v' || n, 'sha-' || n,
       CASE WHEN n = 0 THEN '2026-09-24Z'::timestamptz
            WHEN n = 12 THEN '2026-10-22Z'::timestamptz
            ELSE '2026-10-01Z'::timestamptz + (n - 1) * interval '1 day' END,
       meta.as_of()
FROM generate_series(0, 12) n
ON CONFLICT (repo, tag, sha) DO NOTHING;
INSERT INTO raw.git_tag VALUES ('fixture-a', 'future', 'future', '2026-10-22T00:00:01Z', meta.as_of())
ON CONFLICT DO NOTHING;
INSERT INTO raw.git_commit VALUES
  ('fixture-a', 'revert', '2026-10-20Z', 'Revert sha-2', ARRAY['sha-12'], NULL, meta.as_of())
ON CONFLICT DO NOTHING;
INSERT INTO raw.event
SELECT 'apply-' || n, 'afakt.delivery.deploy', '2026-10-01Z'::timestamptz + n * interval '1 day',
       'fixture-host', NULL,
       jsonb_build_object('repo', 'fixture-b', 'ref', 'apply-' || n, 'outcome', 'success'), meta.as_of()
FROM generate_series(1, 6) n
ON CONFLICT (event_id) DO NOTHING;
INSERT INTO raw.event VALUES
  ('incident-a', 'afakt.incident.event', '2026-10-20Z', 'fixture-host', NULL,
   '{"repo":"fixture-a","deployment":"v1"}', meta.as_of()),
  ('incident-a-duplicate', 'afakt.incident.event', '2026-10-21Z', 'fixture-host', NULL,
   '{"repo":"fixture-a","deployment":"v1"}', meta.as_of()),
  ('rollback-a', 'afakt.delivery.deploy', '2026-10-20Z', 'fixture-host', NULL,
   '{"repo":"fixture-a","ref":"v3","outcome":"rolled_back"}', meta.as_of()),
  ('incident-b', 'afakt.incident.event', '2026-10-20Z', 'fixture-host', NULL,
   '{"repo":"fixture-b","deployment":"apply-1"}', meta.as_of())
ON CONFLICT (event_id) DO NOTHING;
INSERT INTO meta.load VALUES ('fixture', 'fixture-host', 'smoke.sql', 1, meta.as_of())
ON CONFLICT (source, host, file) DO UPDATE SET loaded_at = excluded.loaded_at;
REFRESH MATERIALIZED VIEW model.deployment;
