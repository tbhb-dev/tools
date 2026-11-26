"""Read GitHub data and report the result of reviews-verified."""

import json
from typing import cast

from tbhbdev.alpha import pull_request as pr
from tbhbdev.alpha import review_gate as gate
from tbhbdev.alpha.review_doc import ReviewError

TIER_PATH = ".github/review-tier-2-paths"
PR_FIELDS = (
    "[.head.sha,.base.sha,.user.login,.state,.draft,"
    ".base.repo.visibility,.changed_files,.commits]"
)


type Snapshot = tuple[str, str, str, str, bool, str, int, int]


def request(path: str, *options: str, optional: bool = False) -> str:
    """Read one endpoint, allowing a missing tier policy."""
    done = pr.run(
        [pr.find_tool(pr.GH), "api", "--hostname", "github.com", path, *options]
    )
    if done.returncode:
        why = " ".join(done.stderr.split())
        if optional and why.endswith("(HTTP 404)"):
            return ""
        msg = f"gh could not read {path}: {why}"
        raise ReviewError(msg)
    return done.stdout


def rows(path: str, key: str = "") -> list[dict[str, object]]:
    """Read rows from every page of a GitHub list."""
    pages = json.loads(request(path, "--paginate", "--slurp"))
    return [item for page in pages for item in (page[key] if key else page)]


def snapshot(path: str) -> Snapshot:
    """Read the PR metadata that must stay fixed across the other reads."""
    return cast("Snapshot", tuple(json.loads(request(path, "--jq", PR_FIELDS))))


def job_groups(root: str, head: str) -> list[tuple[str, str | None]]:
    """Read runner group names for non-skipped job attempts at the head."""
    runs = rows(f"{root}/actions/runs?head_sha={head}&per_page=100", "workflow_runs")
    return [
        (str(job["name"]), cast("str | None", job.get("runner_group_name")))
        for run in runs
        for job in rows(
            f"{root}/actions/runs/{run['id']}/jobs?filter=all&per_page=100", "jobs"
        )
        if job.get("conclusion") != "skipped"
    ]


def inputs(repo: str, number: int) -> gate.Pull:
    """Read checks, reviews, commit trailers, tier policy and private runner groups."""
    root = f"repos/{repo}"
    path = f"{root}/pulls/{number}"
    info = snapshot(path)
    head, base, author, state, draft, visibility, file_count, commit_count = info
    if not (gate.HEAD_SHA.fullmatch(head) and gate.HEAD_SHA.fullmatch(base)):
        msg = "GitHub returned an invalid head or base SHA"
        raise ReviewError(msg)
    files = rows(f"{path}/files?per_page=100")
    commits = rows(f"{path}/commits?per_page=100")
    if (len(files), len(commits)) != (file_count, commit_count) or (
        not commits or commits[-1]["sha"] != head
    ):
        msg = "GitHub returned an incomplete file or commit list for the head"
        raise ReviewError(msg)
    paths = [str(row["filename"]) for row in files]
    paths.extend(
        str(row["previous_filename"]) for row in files if "previous_filename" in row
    )
    policy = request(
        f"{root}/contents/{TIER_PATH}?ref={base}",
        "-H",
        "Accept: application/vnd.github.raw+json",
        optional=True,
    )
    if not policy:
        # Confirm contents access before treating an absent policy as tier 1.
        request(f"{root}/git/commits/{base}")
    reviews = [
        gate.Review(
            id=cast("int", row["id"]),
            login=cast("dict[str, str]", row["user"] or {}).get("login", ""),
            state=cast("str", row["state"]),
            commit_id=cast("str", row["commit_id"]),
            body=cast("str", row["body"] or ""),
        )
        for row in rows(f"{path}/reviews?per_page=100")
    ]
    endpoint = f"{root}/commits/{head}/check-runs?filter=latest&per_page=100"
    checks = cast("list[gate.Check]", rows(endpoint, "check_runs"))
    jobs = job_groups(root, head) if visibility == "private" else ()
    if snapshot(path) != info:
        msg = "the pull request changed during verification, run again"
        raise ReviewError(msg)
    return gate.Pull(
        head,
        reviews,
        checks,
        [cast("dict[str, str]", row["commit"])["message"] for row in commits],
        state=state,
        draft=draft,
        author=author,
        tier=gate.tier_for(paths, policy),
        jobs=jobs,
    )


def verify(number: int, repo: str | None) -> int:
    """Print one line per unmet condition or a success line with the head SHA."""
    try:
        target = pr.origin_repository() if repo is None else repo
        if not pr.NAME_WITH_OWNER.fullmatch(target):
            msg = "repository must be OWNER/NAME"
            raise ReviewError(msg)
        pull = inputs(target, number)
        reasons = gate.evaluate(gate.APP, pull)
    except (OSError, SystemExit, ValueError, KeyError, TypeError) as exc:
        print(" ".join(str(exc).splitlines()))
        return 1
    lines = reasons or [f"{gate.CONTEXT}: pass {pull.head}"]
    print("\n".join(" ".join(line.splitlines()) for line in lines))
    return int(bool(reasons))
