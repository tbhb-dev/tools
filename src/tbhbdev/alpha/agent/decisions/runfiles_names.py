"""The coordinator's file contract with run-host, relative to the run directory."""

DIRECTORY = "tbhbdev/agent-runs"
REQUEST = ".request.json"
PROMPT = ".prompt"
SCHEMA = ".schema.json"
PGID = ".pgid"
LAUNCHED = ".launched"
PHASE = ".phase"
OUT = ".out"
FINAL = ".out.final"
LOG = ".log"
BRANCH = ".branch"
PR = ".pr"
HEAD = ".head"
RC = ".rc"
KEEP = ".keep"
RC_PENDING = ".rc.pending"
RUN_LOCK = ".run.lock"
FALLBACK = ".rc.fallback"
OWNER = ".owner"
AUDIT = ".audit.json"
WOULD_REFUSE = ".would_refuse"
INDEX = "index.jsonl"
SUFFIXES = (
    REQUEST,
    PROMPT,
    SCHEMA,
    PGID,
    LAUNCHED,
    PHASE,
    OUT,
    FINAL,
    LOG,
    BRANCH,
    PR,
    HEAD,
    KEEP,
    RC_PENDING,
    RUN_LOCK,
    FALLBACK,
    RC,
    AUDIT,
    WOULD_REFUSE,
    OWNER,  # Keep ownership until every other file is archived.
)
