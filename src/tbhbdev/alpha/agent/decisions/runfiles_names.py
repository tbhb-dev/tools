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
RC = ".rc"
KEEP = ".keep"
RC_PENDING = ".rc.pending"
RUN_LOCK = ".run.lock"
FALLBACK = ".rc.fallback"
OWNER = ".owner"
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
    KEEP,
    RC_PENDING,
    RUN_LOCK,
    FALLBACK,
    RC,
    OWNER,
)
