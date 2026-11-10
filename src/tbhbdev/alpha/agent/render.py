"""The brief a harness receives, from literal text and run kind."""

from tbhbdev.alpha.agent.model import RunKind

FINAL_ONLY = (
    "Report results only in your final answer. "
    "Never message Claude sessions, session sockets or other agents directly."
)


def brief(kind: RunKind, text: str) -> str:
    return f"{FINAL_ONLY}\n{text}" if kind is RunKind.EXEC else text
