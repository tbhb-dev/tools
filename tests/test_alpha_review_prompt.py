"""Check packaged review prompts and their schema 2 draft skeletons."""

from __future__ import annotations

import re
import shutil
import zipfile
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from tbhbdev import content, cue_runtime
from tbhbdev.alpha import review_doc, review_prompt
from tbhbdev.alpha.review_doc import Finding, ReviewDoc, ReviewError, Run
from tbhbdev.alpha.review_doc import _metadata as review_metadata

if TYPE_CHECKING:
    from tbhbdev.alpha.review_doc import Tier, Variant

HEAD = "192837465a" * 4
BASE_REVIEWED = "b52a076891" * 4
FIELDS: dict[str, str | int] = {
    "run": "review-storage",
    "provider": "openai",
    "model": "gpt-6",
    "host": "reviewer.example.org",
    "head": HEAD,
    "repo": "tbhb-dev/tools",
    "pr": 385,
}
LENSES = {
    "correctness": "C",
    "security": "S",
    "tests-and-coverage": "T",
    "docs": "D",
    "scope": "P",
}
BLOCK = Finding("BLOCK", "src/storage.py", 17, "Preserve the last saved record.")
VERIFICATION = "Read the storage migration and traced the failure path."


@pytest.fixture
def review_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bazel supplies the pinned tool on PATH in its test sandbox.
    if executable := shutil.which("cue"):
        monkeypatch.setattr(cue_runtime, "resolve", lambda: Path(executable))


def _resource(name: str) -> str:
    return (
        files("tbhbdev.alpha")
        .joinpath("review_prompt_data", f"{name}.txt")
        .read_text(encoding="utf-8")
    )


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    ("tier", "delta", "variant", "depth"),
    [
        (1, False, "a", "tier-1"),
        (1, False, "b", "tier-1"),
        (2, False, "a", "tier-2-a"),
        (2, False, "b", "tier-2-b"),
        (2, True, "a", "tier-2-a"),
        (1, True, "b", "tier-1"),
    ],
)
@pytest.mark.parametrize("blocked", [False, True], ids=["go", "no-go"])
def test_render_and_validate(
    tier: Tier, delta: bool, variant: Variant, depth: str, *, blocked: bool
) -> None:
    fields = FIELDS | {"base_reviewed": BASE_REVIEWED} if delta else FIELDS
    prompt = review_prompt.render(tier, delta, variant, fields)
    skeleton = review_prompt.skeleton(tier, delta, variant, fields)
    assert prompt.endswith(skeleton)
    assert _resource(depth) in prompt
    lens_text = "\n".join(_resource(lens) for lens in LENSES)
    assert f"\n{lens_text}\n" in prompt
    assert ("The security lens is mandatory" in prompt) == (tier == 2)
    assert ("FE escalation rules in review_data/" in prompt) == (tier == 2)
    if delta:
        assert prompt.startswith(f"Base-reviewed: {BASE_REVIEWED}\n")
        assert "Judge only the delta" in prompt
        assert "Keep earlier unresolved findings by reference" in prompt
    else:
        assert prompt.startswith(_resource(depth))
        assert "Base-reviewed:" not in prompt
    assert not re.search(r"[{}$]", prompt)

    verdict = "NO-GO" if blocked else "GO"
    finding_line = f"\n- BLOCK `{BLOCK.path}:{BLOCK.line}` {BLOCK.summary}\n"
    draft = (
        skeleton.replace("## Verdict\n", f"## Verdict\n\n{verdict}\n")
        .replace("## Findings\n", "## Findings\n" + (finding_line if blocked else ""))
        .replace("## Verification\n", f"## Verification\n\n{VERIFICATION}\n")
    )
    doc = ReviewDoc(
        "tbhb-dev/tools",
        385,
        HEAD,
        tier,
        verdict,
        Run("review-storage", "openai", "gpt-6", variant, "reviewer.example.org"),
        (BLOCK,) if blocked else (),
        VERIFICATION,
    )
    assert review_doc.parse(draft) == doc
    assert review_doc.validate(doc) == []
    _, frontmatter, body = draft.split("---\n", 2)
    assert frontmatter.splitlines() == [
        "schema: 2",
        'repo: "tbhb-dev/tools"',
        "pr: 385",
    ]
    assert content.check_review(body, metadata=review_metadata(doc)) == []


def test_installed_lens_ids() -> None:
    seen = set()
    for name, prefix in LENSES.items():
        heading, *items = _resource(name).splitlines()
        assert heading == f"### {name.replace('-', ' ').capitalize()}"
        assert 4 <= len(items) <= 8
        for index, item in enumerate(items, start=1):
            match = re.fullmatch(rf"- ({prefix}[1-8]) [^?]+\?", item)
            assert match is not None
            item_id = match[1]
            assert item_id == f"{prefix}{index}"
            assert item_id not in seen
            seen.add(item_id)


def test_archive_preserves_resource_names_and_encoding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = files("tbhbdev.alpha")
    archive_path = tmp_path / "resources.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for item in package.joinpath("review_prompt_data").iterdir():
            archive.writestr(f"review_prompt_data/{item.name}", item.read_bytes())

    with zipfile.ZipFile(archive_path) as archive:

        def package_files(package_name: str) -> zipfile.Path:
            assert package_name == "tbhbdev.alpha"
            return zipfile.Path(archive)

        read_text = zipfile.Path.read_text

        def read_resource(
            resource: zipfile.Path, *, encoding: str | None = None
        ) -> str:
            assert encoding is not None
            return read_text(resource, encoding=encoding)

        monkeypatch.setattr(review_prompt, "files", package_files)
        monkeypatch.setattr(zipfile.Path, "read_text", read_resource)
        fields = FIELDS | {"base_reviewed": BASE_REVIEWED}
        prompt = review_prompt.render(2, delta=True, variant="b", fields=fields)
        assert prompt.startswith(f"Base-reviewed: {BASE_REVIEWED}\n")


def test_missing_packaged_lens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    resources = tmp_path / "review_prompt_data"
    resources.mkdir()
    for name in ("common", "tier-1", "correctness"):
        (resources / f"{name}.txt").write_text(_resource(name), encoding="utf-8")

    def package_files(_package: str) -> Path:
        return tmp_path

    monkeypatch.setattr(review_prompt, "files", package_files)
    with pytest.raises(
        ReviewError, match=r"^Missing review prompt resource: security\.txt$"
    ):
        review_prompt.render(1, delta=False, variant="a", fields=FIELDS)


@pytest.mark.parametrize("name", FIELDS)
def test_missing_field(name: str) -> None:
    fields = FIELDS.copy()
    del fields[name]
    with pytest.raises(ReviewError, match=rf"^Missing review placeholder: {name}$"):
        review_prompt.skeleton(1, delta=False, variant="a", fields=fields)


def test_delta_requires_base_reviewed() -> None:
    for renderer in (review_prompt.render, review_prompt.skeleton):
        with pytest.raises(ReviewError, match=r"^Missing review field: base_reviewed$"):
            renderer(2, delta=True, variant="b", fields=FIELDS)


def test_unknown_fields() -> None:
    fields = FIELDS | {"tier": 2, "extra": "input"}
    with pytest.raises(ReviewError, match=r"^Unknown review fields: extra, tier$"):
        review_prompt.render(1, delta=False, variant="a", fields=fields)


@pytest.mark.parametrize(("tier", "variant"), [(0, "a"), (2, cast("Variant", "c"))])
def test_unsupported_selection(tier: Tier, variant: Variant) -> None:
    message = rf"^Unsupported review tier or variant: {tier}, {variant}$"
    with pytest.raises(ReviewError, match=message):
        review_prompt.skeleton(tier, delta=False, variant=variant, fields=FIELDS)


@pytest.mark.parametrize(
    ("placeholder", "message"),
    [
        ("${unexpected}", "Unknown review placeholder: unexpected"),
        ("${unclosed", "Invalid review placeholder syntax"),
        ("{run}", "Unresolved placeholder in review text"),
        ("$$run", "Unresolved placeholder in review text"),
    ],
)
def test_invalid_template_placeholder(
    monkeypatch: pytest.MonkeyPatch, placeholder: str, message: str
) -> None:
    common = _resource("common").replace("${provider}", placeholder)

    def load(_name: str) -> str:
        return common

    monkeypatch.setattr(review_prompt, "_load", load)
    with pytest.raises(ReviewError, match=rf"^{message}$"):
        review_prompt.skeleton(1, delta=False, variant="a", fields=FIELDS)


@pytest.mark.parametrize("value", ["{provider}", "$provider"])
def test_field_cannot_leave_placeholder(value: str) -> None:
    with pytest.raises(ReviewError, match=r"^Unresolved placeholder in review text$"):
        review_prompt.render(
            1, delta=False, variant="a", fields=FIELDS | {"provider": value}
        )
