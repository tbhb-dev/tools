"""Render a review prompt or draft skeleton from packaged text resources."""

from __future__ import annotations

import re
from importlib.resources import files
from string import Template
from typing import TYPE_CHECKING

from tbhbdev.alpha.review_doc import ReviewError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tbhbdev.alpha.review_doc import Tier, Variant

_PROMPTS = {
    (1, "a"): "tier-1.txt",
    (1, "b"): "tier-1.txt",
    (2, "a"): "tier-2-a.txt",
    (2, "b"): "tier-2-b.txt",
}
# These lens IDs are separate from the review floor's R and FE IDs.
_LENSES = ("correctness", "security", "tests-and-coverage", "docs", "scope")
_FIELDS = frozenset(
    {"run", "provider", "model", "host", "head", "repo", "pr", "base_reviewed"}
)
_TEMPLATE_FIELDS = _FIELDS | {"tier", "variant"}
_UNFILLED = re.compile(r"\{[^{}\n]+\}|\$(?:\{|[A-Za-z_])")


def _load(name: str) -> str:
    try:
        return (
            files("tbhbdev.alpha")
            .joinpath("review_prompt_data", name)
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        message = f"Missing review prompt resource: {name}"
        raise ReviewError(message) from exc


def _common() -> tuple[str, str]:
    instructions, draft = _load("common.txt").split("\n---\n", maxsplit=1)
    return instructions, f"---\n{draft}"


def _values(
    tier: Tier, variant: Variant, fields: Mapping[str, str | int], *, delta: bool
) -> dict[str, str | int]:
    if (tier, variant) not in _PROMPTS:
        message = f"Unsupported review tier or variant: {tier}, {variant}"
        raise ReviewError(message)
    if unknown := fields.keys() - _FIELDS:
        message = f"Unknown review fields: {', '.join(sorted(unknown))}"
        raise ReviewError(message)
    if delta and "base_reviewed" not in fields:
        message = "Missing review field: base_reviewed"
        raise ReviewError(message)
    return {**fields, "tier": tier, "variant": variant}


def _fill(text: str, values: Mapping[str, str | int]) -> str:
    try:
        filled = Template(text).substitute(values)
    except KeyError as exc:
        name = exc.args[0]
        issue = "Missing" if name in _TEMPLATE_FIELDS else "Unknown"
        message = f"{issue} review placeholder: {name}"
        raise ReviewError(message) from exc
    except ValueError as exc:
        message = "Invalid review placeholder syntax"
        raise ReviewError(message) from exc
    if _UNFILLED.search(filled):
        message = "Unresolved placeholder in review text"
        raise ReviewError(message)
    return filled


def render(
    tier: Tier,
    delta: bool,  # noqa: FBT001 -- Shared selection arguments for both renderers.
    variant: Variant,
    fields: Mapping[str, str | int],
) -> str:
    """Render reviewer instructions, the five lenses, and the draft skeleton."""
    values = _values(tier, variant, fields, delta=delta)
    instructions, draft = _common()
    depth = _load(_PROMPTS[tier, variant])
    scope = _load("delta.txt") if delta else ""
    lenses = "\n".join(_load(f"{lens}.txt") for lens in _LENSES)
    return _fill(f"{scope}{depth}\n{lenses}\n{instructions}\n{draft}", values)


def skeleton(
    tier: Tier,
    delta: bool,  # noqa: FBT001 -- Shared selection arguments for both renderers.
    variant: Variant,
    fields: Mapping[str, str | int],
) -> str:
    """Render schema 2 draft frontmatter and the unfilled review sections."""
    values = _values(tier, variant, fields, delta=delta)
    _, draft = _common()
    return _fill(draft, values)
