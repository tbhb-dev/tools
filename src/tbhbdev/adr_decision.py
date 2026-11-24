"""Read the Decision issue contract from a GitHub issue response."""

import re
from dataclasses import dataclass
from typing import Self, cast

from tbhbdev.content import Json, Refused

ADR_LABEL = "adr"
DECISION_TYPE = "Decision"
# Organization fields exist outside the issue response and supply no record text.
DECIDER_FIELD = "Decider"
REVERSIBILITY_FIELD = "Reversibility"
ADR_LINK_KEY = "adrLink"
QUESTION = "Question"
OPTIONS = "Options"
RECOMMENDATION = "Recommendation"
UNDO = "Undo"

_PULL_URL = r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/pull/[1-9][0-9]*"
_LINK_LINE = re.compile(rf"^{ADR_LINK_KEY}:[ \t]*({_PULL_URL})[ \t]*$", re.MULTILINE)
_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_OPTION = re.compile(r"^[ \t]*([1-9][0-9]*)\.[ \t]+(.+? - .+)$", re.MULTILINE)


def _required(data: Json, name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        message = f"Decision issue is missing required text: {name}"
        raise Refused(message)
    return value


@dataclass(frozen=True)
class Decision:
    title: str
    question: str
    choice: str
    undo: str

    @classmethod
    def parse(cls, issue: Json) -> Self:
        kind = issue.get("type")
        if not isinstance(kind, dict) or kind.get("name") != DECISION_TYPE:
            message = f"issue is not a {DECISION_TYPE}"
            raise Refused(message)
        labels = issue.get("labels")
        if not isinstance(labels, list) or not any(
            isinstance(label, dict) and label.get("name") == ADR_LABEL
            for label in cast("list[object]", labels)  # pragma: no mutate -- Type only.
        ):
            message = f"Decision issue lacks the {ADR_LABEL} label"
            raise Refused(message)
        if _required(issue, "state") == "closed":
            message = "Decision issue is closed"
            raise Refused(message)
        body = _required(issue, "body").replace("\r\n", "\n")
        if _LINK_LINE.search(f"{ADR_LINK_KEY}: {issue.get(ADR_LINK_KEY)}\n{body}"):
            message = "Decision issue already links to a record pull request"
            raise Refused(message)
        parts = _HEADING.split(body)
        sections: Json = dict(zip(parts[1::2], parts[2::2], strict=True))
        options = {
            option[1]: option[2]
            for option in _OPTION.finditer(_required(sections, OPTIONS))
        }
        selected = _required(sections, RECOMMENDATION).strip()
        if selected not in options:
            message = f"{RECOMMENDATION} must name a numbered option"
            raise Refused(message)
        return cls(
            " ".join(_required(issue, "title").split()),
            " ".join(_required(sections, QUESTION).split()),
            " ".join(options[selected].split()),
            " ".join(_required(sections, UNDO).split()),
        )
