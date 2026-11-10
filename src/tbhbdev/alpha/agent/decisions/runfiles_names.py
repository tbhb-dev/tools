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
    RC,
    KEEP,
    RC_PENDING,
)
