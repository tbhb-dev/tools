import json
from functools import partial
from pathlib import Path
from subprocess import CompletedProcess
from typing import cast

import pytest

from tbhbdev.alpha import pull_request, review

HEAD = "a" * 40
BASE = "b" * 40
ROOT = "repos/o/r"
PULL = f"{ROOT}/pulls/7"
POLICY = f"{ROOT}/contents/.github/review-tier-2-paths?ref={BASE}"
JOBS = f"{ROOT}/actions/runs/9/jobs?filter=all&per_page=100"
FIXTURE = Path(__file__).with_name("testdata") / "alpha" / "review-verify.json"


def api(
    mode: str, responses: dict[str, object], argv: list[str]
) -> CompletedProcess[str]:
    assert argv[:4] == ["gh", "api", "--hostname", "github.com"]
    path = argv[4]
    if path == POLICY:
        error = {"missing": "gh: Not Found (HTTP 404)", "api error": "forbidden"}.get(
            mode
        )
        policy = "guard.py" if mode == "rename" else "other.py"
        return CompletedProcess(argv, int(bool(error)), policy, error or "")
    done = CompletedProcess(argv, 0, json.dumps(responses[path]), "")
    if path == PULL and mode == "changed":
        cast("list[object]", responses[PULL])[0] = BASE
    return done


@pytest.mark.parametrize(
    ("mode", "result", "output"),
    [
        ("public", 0, f"reviews-verified: pass {HEAD}"),
        ("private", 1, "job lint ran on runner group Default"),
        ("skipped job", 0, f"reviews-verified: pass {HEAD}"),
        ("rename", 1, "1 independent approval variants at the head, need 2"),
        ("missing", 0, f"reviews-verified: pass {HEAD}"),
        ("api error", 1, f"gh could not read {POLICY}: forbidden"),
        ("bad sha", 1, "GitHub returned an invalid head or base SHA"),
        (
            "bad count",
            1,
            "GitHub returned an incomplete file or commit list for the head",
        ),
        ("changed", 1, "the pull request changed during verification, run again"),
        ("invalid repo", 1, "repository must be OWNER/NAME"),
    ],
)
def test_verify(
    mode: str,
    result: int,
    output: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responses = json.loads(FIXTURE.read_text(encoding="utf-8"))
    meta = responses[PULL]
    if mode in {"private", "skipped job"}:
        meta[5] = "private"
    if mode == "skipped job":
        responses[JOBS][0]["jobs"][0] = {"name": "lint", "conclusion": "skipped"}
    if mode == "bad sha":
        meta[0] = "abc"
    if mode == "bad count":
        meta[-2] = 3

    monkeypatch.setattr(pull_request, "origin_repository", lambda: "o/r")
    monkeypatch.setattr(pull_request, "find_tool", str)
    monkeypatch.setattr(pull_request, "run", partial(api, mode, responses))
    repo = "invalid" if mode == "invalid repo" else "o/r"
    options = ["--repo", repo] if mode != "public" else []
    assert review.main(["verify", "7", *options], "review") == result
    assert capsys.readouterr().out.splitlines() == [output]
