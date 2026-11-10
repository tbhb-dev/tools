CREATE SCHEMA raw;
CREATE SCHEMA staging;
CREATE SCHEMA model;
CREATE SCHEMA metric;
CREATE SCHEMA mart;

CREATE TABLE meta.repo (
  repo text PRIMARY KEY,
  kind text CHECK (kind IN ('release', 'apply', 'content'))
);
CREATE TABLE meta.definition (
  metric text,
  definition_version text,
  changed_at timestamptz,
  PRIMARY KEY (metric, definition_version)
);
CREATE TABLE meta.load (
  source text,
  host text,
  file text,
  byte_offset bigint,
  loaded_at timestamptz,
  PRIMARY KEY (source, host, file)
);
CREATE FUNCTION meta.as_of() RETURNS timestamptz
LANGUAGE sql STABLE AS $$
  SELECT coalesce(nullif(current_setting('afakt.as_of', true), '')::timestamptz, now())
$$;

CREATE TABLE raw.event (
  event_id text PRIMARY KEY,
  event_name text,
  time timestamptz,
  host text,
  item_ref text,
  attrs jsonb,
  loaded_at timestamptz
);
CREATE TABLE raw.git_commit (
  repo text,
  sha text,
  committed_at timestamptz,
  subject text,
  parents text[],
  pr integer,
  loaded_at timestamptz,
  PRIMARY KEY (repo, sha)
);
CREATE TABLE raw.git_tag (
  repo text,
  tag text,
  sha text,
  tagged_at timestamptz,
  loaded_at timestamptz,
  PRIMARY KEY (repo, tag, sha)
);
CREATE TABLE raw.pr_head (
  repo text,
  pr integer,
  head_sha text,
  first_commit_at timestamptz,
  commit_count integer,
  loaded_at timestamptz,
  PRIMARY KEY (repo, pr, head_sha)
);

-- The materialized store does not depend on a connection's clock. Views
-- evaluate windows and failures in the querying session.
-- Event attributes use repo/ref for deploys and repo/deployment for
-- incidents. Revert subjects use "Revert <sha>". A release contains the
-- tagged commit's ancestors not already present in an earlier release.
CREATE MATERIALIZED VIEW model.deployment AS
WITH RECURSIVE
releases AS (
  SELECT t.repo, t.tag AS ref, t.sha, t.tagged_at AS deployed_at
  FROM raw.git_tag t JOIN meta.repo r USING (repo) WHERE r.kind = 'release'
),
ancestors AS (
  SELECT repo, ref, sha, deployed_at FROM releases
  UNION
  SELECT a.repo, a.ref, p.sha, a.deployed_at
  FROM ancestors a JOIN raw.git_commit c ON c.repo = a.repo AND c.sha = a.sha
  CROSS JOIN LATERAL unnest(c.parents) p(sha)
),
release_commits AS (
  SELECT a.repo, a.ref, a.deployed_at, array_agg(DISTINCT a.sha ORDER BY a.sha) AS commits
  FROM ancestors a
  WHERE NOT EXISTS (
    SELECT FROM ancestors old WHERE old.repo = a.repo AND old.sha = a.sha
      AND old.deployed_at < a.deployed_at
  )
  GROUP BY a.repo, a.ref, a.deployed_at
)
SELECT t.repo, t.ref AS deployment_ref, t.deployed_at,
       coalesce(c.commits, ARRAY[]::text[]) AS commits
FROM releases t LEFT JOIN release_commits c USING (repo, ref, deployed_at)
UNION ALL
SELECT c.repo, c.sha, c.committed_at, ARRAY[c.sha]
FROM raw.git_commit c JOIN meta.repo r USING (repo)
WHERE r.kind = 'content' AND c.pr IS NOT NULL
UNION ALL
SELECT e.attrs->>'repo', e.attrs->>'ref', e.time, ARRAY[e.attrs->>'sha']
FROM raw.event e JOIN meta.repo r ON r.repo = e.attrs->>'repo'
WHERE r.kind = 'apply' AND e.event_name = 'afakt.delivery.deploy'
  AND e.attrs->>'outcome' = 'success';
CREATE UNIQUE INDEX deployment_identity ON model.deployment (repo, deployment_ref, deployed_at);

CREATE VIEW metric.deployments AS
SELECT r.repo, w.label AS "window", meta.as_of() AS window_end,
       count(d.repo)::integer AS deployments
FROM meta.repo r CROSS JOIN (VALUES ('7d', interval '7 days'), ('28d', interval '28 days')) w(label, duration)
LEFT JOIN model.deployment d ON d.repo = r.repo
  AND d.deployed_at > meta.as_of() - w.duration AND d.deployed_at <= meta.as_of()
GROUP BY r.repo, w.label;

CREATE VIEW metric.change_failure AS
SELECT r.repo, w.label AS "window", meta.as_of() AS window_end,
       count(d.repo)::integer AS deployments,
       count(d.repo) FILTER (WHERE
         EXISTS (SELECT FROM raw.event e
           WHERE e.attrs->>'repo' = d.repo AND e.time >= d.deployed_at AND e.time <= meta.as_of()
             AND ((e.event_name = 'afakt.incident.event' AND e.attrs->>'deployment' = d.deployment_ref)
               OR (e.event_name = 'afakt.delivery.deploy' AND e.attrs->>'outcome' = 'rolled_back'
                 AND e.attrs->>'ref' = d.deployment_ref)))
         OR EXISTS (SELECT FROM raw.git_commit c
           WHERE c.repo = d.repo AND c.committed_at > d.deployed_at AND c.committed_at <= meta.as_of()
             AND c.subject = ANY (SELECT 'Revert ' || unnest(d.commits)))
       )::integer AS failures
FROM meta.repo r CROSS JOIN (VALUES ('7d', interval '7 days'), ('28d', interval '28 days')) w(label, duration)
LEFT JOIN model.deployment d ON d.repo = r.repo
  AND d.deployed_at > meta.as_of() - w.duration AND d.deployed_at <= meta.as_of()
GROUP BY r.repo, w.label;

CREATE VIEW mart.dora_by_repo AS
SELECT f.repo, f."window", f.window_end, f.deployments, f.failures,
       CASE WHEN f.deployments >= 10 THEN f.failures::numeric / f.deployments END AS change_failure_rate,
       f.deployments >= 10 AS min_sample_met,
       (SELECT definition_version FROM meta.definition
        WHERE metric = 'change_failure_rate' AND changed_at <= meta.as_of()
        ORDER BY changed_at DESC, definition_version DESC LIMIT 1) AS definition_version
FROM metric.change_failure f;
CREATE VIEW mart.definition_changes AS
SELECT metric, definition_version, changed_at FROM meta.definition;
CREATE VIEW mart.store_status AS
SELECT source, max(loaded_at) AS last_load FROM meta.load GROUP BY source;

INSERT INTO meta.definition VALUES
  ('change_failure_rate', '1', '2026-01-01T00:00:00Z'),
  ('change_failure_rate', '2', '2026-10-01T00:00:00Z');
