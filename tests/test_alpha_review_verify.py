import json
from functools import partial
from subprocess import CompletedProcess

import pytest

from tbhbdev.alpha import pull_request, review
from tbhbdev.alpha.review_gate import APP

HEAD = "a" * 40
BASE = "b" * 40
ROOT = "repos/o/r"
PULL = f"{ROOT}/pulls/7"
POLICY = f"{ROOT}/contents/.github/review-tier-2-paths?ref={BASE}"
CHECKS = f"{ROOT}/commits/{HEAD}/check-runs"
RUNS = f"{ROOT}/actions/runs?head_sha={HEAD}&per_page=100"
JOBS = f"{ROOT}/actions/runs/9/jobs?filter=all&per_page=100"


def run(
    mode: str, meta: list[object], responses: dict[str, object], argv: list[str]
) -> CompletedProcess[str]:
    assert argv[:4] == ["gh", "api", "--hostname", "github.com"]
    path = argv[4]
    if path == POLICY:
        if mode in {"missing", "api error"}:
            error = "gh: Not Found (HTTP 404)" if mode == "missing" else "forbidden"
            return CompletedProcess(argv, 1, "", error)
        return CompletedProcess(argv, 0, "guard.py\n", "")
    if path in {RUNS, JOBS}:
        assert mode in {"private", "skipped job"}
    done = CompletedProcess(argv, 0, json.dumps(responses[path]), "")
    if path == PULL and mode == "changed":
        meta[0] = BASE
    return done


@pytest.mark.parametrize(
    ("mode", "result", "output"),
    [
        ("public", 0, f"reviews-verified: pass {HEAD}"),
        (
            "no builder trailer",
            1,
            "unknown builder needs approvals at the head from 2 providers (found 1)",
        ),
        ("private", 1, "job lint ran on runner group Default"),
        ("skipped job", 0, f"reviews-verified: pass {HEAD}"),
        ("rerun", 0, f"reviews-verified: pass {HEAD}"),
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
    visibility = "private" if mode in {"private", "skipped job"} else "public"
    meta: list[object] = [HEAD, BASE, "builder", "open", False, visibility, 2, 1]
    files = [{"filename": "doc.md"}, {"filename": "renamed.py"}]
    if mode == "rename":
        files[1]["previous_filename"] = "guard.py"
    approval = {
        "id": 1,
        "state": "APPROVED",
        "commit_id": HEAD,
        "user": {"login": APP},
        "body": f"Run r, claude m, judged head {HEAD}, variant a.",
    }
    message = "fix: x"
    if mode != "no builder trailer":
        message += "\n\nGenerated-by: LLM harness=codex;model=m\n"
    commit = {"sha": HEAD, "commit": {"message": message}}
    check = {"name": "ci", "status": "completed", "conclusion": "success"}
    ghost = {**approval, "id": 2, "user": None, "body": None}
    job = {"name": "lint", "conclusion": "success", "runner_group_name": "Default"}
    if mode == "skipped job":
        job = {"name": "lint", "conclusion": "skipped"}
    responses: dict[str, object] = {
        PULL: meta,
        f"{PULL}/files?per_page=100": [[files[0]], [files[1]]],
        f"{PULL}/commits?per_page=100": [[commit]],
        f"{PULL}/reviews?per_page=100": [[approval], [ghost]],
        f"{CHECKS}?filter=latest&per_page=100": [{"check_runs": [check]}],
        RUNS: [{"workflow_runs": [{"id": 9}]}],
        JOBS: [{"jobs": [job]}],
        f"{ROOT}/git/commits/{BASE}": {},
    }
    if mode == "rerun":
        responses[f"{CHECKS}?filter=all&per_page=100"] = [
            {"check_runs": [check, {**check, "conclusion": "failure"}]}
        ]
    if mode == "bad sha":
        meta[0] = "abc"
    if mode == "bad count":
        meta[-2] = 3

    monkeypatch.setattr(pull_request, "origin_repository", lambda: "o/r")

    monkeypatch.setattr(pull_request, "find_tool", str)
    monkeypatch.setattr(pull_request, "run", partial(run, mode, meta, responses))
    options: list[str] = (
        []
        if mode == "public"
        else ["--repo", "invalid" if mode == "invalid repo" else "o/r"]
    )
    assert review.main(["verify", "7", *options], "review") == result
    assert capsys.readouterr().out.splitlines() == [output]


def test_verify_rejects_zero_pr_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="2"):
        review.main(["verify", "0"], "review")
    assert "PR number must be at least 1" in capsys.readouterr().err
