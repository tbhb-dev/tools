"""Read and check staged issue blocks."""

import re
from dataclasses import dataclass

CRITERIA = {
    "Initiative": (1, 6),
    "Epic": (1, 6),
    "Feature": (2, 5),
    "Defect": (1, 3),
    "Chore": (1, 3),
    "Spike": (1, 3),
}
TYPES = CRITERIA.keys() | {"Task", "Incident", "Problem", "Request", "Change"}
FIELDS = {
    "type",
    "labels",
    "title",
    "criteria",
    "verification",
    "owner",
    "repo",
    "evidence",
    "parent",
    "blocked-by",
    "duplicate-of",
}
ITEM_FIELDS = {"criteria", "verification"}
RELATIONS = ("parent", "blocked-by", "duplicate-of")
PARENT_TYPES = {"Epic": "Initiative", "Feature": "Epic"}
MAX_VERIFICATION = 3
MIN_TITLE_CHARS = 15
MAX_TITLE_CHARS = 70
MIN_TITLE_WORDS = 3
TITLE_WORDS = {
    "Initiative": 12,
    "Epic": 12,
    "Feature": 14,
    "Task": 12,
    "Chore": 12,
    "Defect": 16,
    "Spike": 15,
}
TITLE_SHAPES = {
    "Epic": r"^(?!.* can )",
    "Feature": r"^[A-Z][^ ]*( [^ ]+){0,3} can [a-z]",
    "Task": r"^[A-Z][a-z]+ ",
    "Chore": r"^[A-Z][a-z]+ ",
    "Defect": r" when ",
    "Spike": r"\?$",
}


@dataclass(frozen=True)
class Block:
    id: str
    type: str
    title: str
    labels: tuple[str, ...]
    criteria: tuple[str, ...]
    verification: tuple[str, ...]
    owner: str
    repo: str
    evidence: str
    parent: str
    blocked_by: tuple[str, ...]
    duplicate_of: str

    def targets(self, relation: str) -> tuple[str, ...]:
        return {
            "parent": (self.parent,) if self.parent else (),
            "blocked-by": self.blocked_by,
            "duplicate-of": (self.duplicate_of,) if self.duplicate_of else (),
        }[relation]


@dataclass(frozen=True)
class Finding:
    id: str
    rule: str
    count: int = 1

    def __str__(self) -> str:
        return f"{self.id}: {self.rule} (count {self.count})"


def _comma_list(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _fields(lines: list[str]) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    field = ""
    for raw in lines:
        line = raw.strip()
        key, colon, value = line.partition(":")
        if colon and key in FIELDS:
            if key in fields:
                message = f"repeated field {key}"
                raise ValueError(message)
            field = key
            fields[field] = [value.strip()]
        elif field == "evidence":
            fields[field].append(raw)
        elif field in ITEM_FIELDS and line.startswith("- "):
            fields[field].append(line[2:].strip())
        elif line:
            message = "expected a field or a list item"
            raise ValueError(message)
    return fields


def _block(id_: str, lines: list[str]) -> Block:
    fields = _fields(lines)

    def get(field: str) -> str:
        return fields.get(field, [""])[0]

    labels = get("labels")
    if "labels" in fields and not (labels.startswith("[") and labels.endswith("]")):
        message = "labels must be in brackets"
        raise ValueError(message)
    if any(get(field) for field in ITEM_FIELDS):
        message = "criteria and verification must use list items"
        raise ValueError(message)
    return Block(
        id_,
        get("type"),
        get("title"),
        _comma_list(labels[1:-1]),
        tuple(fields.get("criteria", [])[1:]),
        tuple(fields.get("verification", [])[1:]),
        get("owner"),
        get("repo"),
        "\n".join(fields.get("evidence", [])).strip(),
        get("parent"),
        _comma_list(get("blocked-by")),
        get("duplicate-of"),
    )


def read(source: str) -> tuple[dict[str, Block], list[Finding]]:
    preamble, *sections = re.split(r"^###(?:[ \t]+|$)", source, flags=re.MULTILINE)
    blocks: dict[str, Block] = {}
    findings: list[Finding] = []
    if preamble.strip() or not sections:
        findings.append(Finding("<file>", "expected issue blocks starting with ###"))
    seen: set[str] = set()
    for section in sections:
        heading, _, body = section.partition("\n")
        id_ = heading.strip()
        if not id_ or id_ in seen:
            findings.append(Finding(id_ or "<missing id>", "missing or repeated id"))
            continue
        seen.add(id_)
        try:
            blocks[id_] = _block(id_, body.splitlines())
        except ValueError as exc:
            findings.append(Finding(id_, str(exc)))
    return blocks, findings


def _counts(block: Block) -> list[Finding]:
    findings: list[Finding] = []
    limits = CRITERIA.get(block.type)
    count = len(block.criteria)
    if limits and not limits[0] <= count <= limits[1]:
        findings.append(
            Finding(block.id, f"criteria must be {limits[0]} to {limits[1]}", count)
        )
    if block.type == "Task":
        if count:
            findings.append(Finding(block.id, "Task must have no criteria", count))
        if not 1 <= len(block.verification) <= MAX_VERIFICATION:
            findings.append(
                Finding(
                    block.id,
                    "Task verification must be 1 to 3",
                    len(block.verification),
                )
            )
    return findings


def _check_block(block: Block, blocks: dict[str, Block]) -> list[Finding]:
    findings = _counts(block)
    if block.type not in TYPES:
        findings.append(Finding(block.id, "unknown type"))
    parent = blocks.get(block.parent)
    parent_type = parent.type if parent else ""
    if parent_type == "Task":
        findings.append(Finding(block.id, "parent is a Task"))
    required = PARENT_TYPES.get(block.type)
    if required and parent_type != required:
        findings.append(
            Finding(block.id, f"{block.type} requires a parent of type {required}")
        )
    if block.type == "Task" and not block.parent:
        findings.append(Finding(block.id, "Task requires a parent", 0))
    return findings


def in_cycle(start: str, edges: dict[str, tuple[str, ...]]) -> bool:
    pending = list(edges[start])
    seen: set[str] = set()
    while pending:
        target = pending.pop()
        if target == start:
            return True
        if target not in seen:
            seen.add(target)
            pending.extend(edges.get(target, ()))
    return False


def _check_edges(relation: str, edges: dict[str, tuple[str, ...]]) -> list[Finding]:
    findings: list[Finding] = []
    for id_, targets in edges.items():
        missing = sum(target not in edges for target in targets)
        if missing:
            findings.append(Finding(id_, f"unknown {relation} target", missing))
        if in_cycle(id_, edges):
            findings.append(Finding(id_, f"{relation} cycle"))
    return findings


def check(blocks: dict[str, Block]) -> list[Finding]:
    findings: list[Finding] = []
    for block in blocks.values():
        findings.extend(_check_block(block, blocks))
    for relation in RELATIONS:
        edges = {id_: block.targets(relation) for id_, block in blocks.items()}
        findings.extend(_check_edges(relation, edges))
    return findings


def title_warning(block: Block) -> bool:
    title = block.title
    return (
        not MIN_TITLE_CHARS <= len(title) <= MAX_TITLE_CHARS
        or not MIN_TITLE_WORDS
        <= len(title.split())
        <= TITLE_WORDS.get(block.type, MAX_TITLE_CHARS)
        or title.endswith((".", "!", ":"))
        or re.search(
            r"^(\[|\(|[A-Za-z]+-[0-9]+)|\b(?i:initiative|epic|feature|story|task|chore|defect|spike)\b",
            title,
        )
        is not None
        or re.search(TITLE_SHAPES.get(block.type, ""), title) is None
    )
