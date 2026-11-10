"""The run request: a table over literal fields, and the JSON round trip."""

from __future__ import annotations

import dataclasses
import json

import pytest

from tbhbdev.alpha.agent import model
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.model import Invalid

type Data = dict[str, object]

MISSING = object()
COMMIT = "0123456789abcdef0123456789abcdef01234567"
COMMIT_256 = COMMIT + "89abcdef0123456789abcdef"
NAME_RULE = "[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
REASON = "must be one printable line of up to 200 characters"

BUILD: Data = {
    "v": 2,
    "kind": "exec",
    "repo": "example-org/example-repo",
    "commit": None,
    "harness": "codex",
    "model": "example-model-1",
    "effort": "high",
    "profile": "build",
    "claude_profile": None,
    "reason": None,
    "account": "agent",
    "timeout": None,
    "class": None,
    "variant": None,
    "role": None,
    "unsandboxed": False,
    "tainted_host": False,
    "authorized_by": None,
    "startup_deadline_seconds": 120,
}
REVIEW: Data = {**BUILD, "profile": "review", "commit": COMMIT}
CLAUDE: Data = {
    **BUILD,
    "harness": "claude",
    "claude_profile": "primary",
    "reason": "swift-macos",
}
AGY: Data = {**BUILD, "harness": "agy", "timeout": 900}
FULL: Data = {
    **REVIEW,
    "kind": "start",
    "repo": "git.example.com/example-org/example-repo",
    "harness": "claude",
    "claude_profile": "Primary",
    "reason": "sysctl",
    "timeout": 3600,
    "class": "review",
    "variant": "v2",
    "role": "reviewer",
}
UNSANDBOXED: Data = {
    **BUILD,
    "unsandboxed": True,
    "authorized_by": "example-org/example-repo#12",
}


def made(base: Data, **changes: object) -> Data:
    data = dict(base)
    for key, value in changes.items():
        if value is MISSING:
            del data[key]
        else:
            data[key] = value
    return data


def refused(data: Data, *, host: bool = True) -> str:
    with pytest.raises(Invalid) as caught:
        request.parse(data, host_allows_unsandboxed=host)
    return str(caught.value)


@pytest.mark.parametrize(
    "data",
    [BUILD, REVIEW, CLAUDE, AGY, FULL, UNSANDBOXED],
    ids=lambda d: str(d["kind"]),
)
def test_round_trips_through_json(data: Data) -> None:
    parsed = request.parse(data, host_allows_unsandboxed=True)
    wire = json.loads(json.dumps(request.to_dict(parsed)))
    assert wire == data
    assert request.parse(wire, host_allows_unsandboxed=True) == parsed


def test_parse_builds_every_field() -> None:
    assert request.parse(FULL, host_allows_unsandboxed=False) == model.RunRequest(
        kind=model.RunKind.START,
        repo="git.example.com/example-org/example-repo",
        commit=COMMIT,
        harness=model.Harness.CLAUDE,
        model="example-model-1",
        effort="high",
        profile=model.Profile.REVIEW,
        claude_profile="Primary",
        reason="sysctl",
        account="agent",
        timeout=3600,
        class_="review",
        variant="v2",
        role="reviewer",
        unsandboxed=False,
        authorized_by=None,
        startup_deadline_seconds=120,
    )


def test_to_dict_holds_the_version_and_every_field() -> None:
    parsed = request.parse(BUILD, host_allows_unsandboxed=False)
    assert request.to_dict(parsed) == BUILD
    assert model.REQUEST_VERSION == 2


def test_to_dict_does_not_validate() -> None:
    parsed = dataclasses.replace(
        request.parse(BUILD, host_allows_unsandboxed=False), repo="../x"
    )
    assert request.to_dict(parsed)["repo"] == "../x"


@pytest.mark.parametrize("version", [1, 0, -1, "2", True, None, 2.0, [2], 11])
def test_unknown_versions_are_refused_before_anything_else(version: object) -> None:
    expected = (
        f"request v {version!r} is not a supported request version; this build reads 2"
    )
    assert refused({"v": version}) == expected
    assert refused(made(BUILD, v=version, repo="../x", extra=1)) == expected


def test_a_missing_version_is_refused() -> None:
    assert refused({}) == "request v is required"
    assert refused(made(BUILD, v=MISSING)) == "request v is required"


def test_missing_fields_are_named() -> None:
    assert refused(made(BUILD, repo=MISSING)) == "request is missing fields: repo"
    assert refused({"v": 2}).startswith("request is missing fields: account, ")
    assert refused(made(BUILD, repo=MISSING, kind=MISSING, extra=1)) == (
        "request is missing fields: kind, repo"
    )


@pytest.mark.parametrize("key", [k for k in BUILD if k != "v"])
def test_each_field_is_required(key: str) -> None:
    assert refused(made(BUILD, **{key: MISSING})) == f"request is missing fields: {key}"


def test_unknown_fields_are_named() -> None:
    assert refused(made(BUILD, zeta=1, alpha=2)) == (
        "request has unknown fields: alpha, zeta"
    )
    assert refused(made(BUILD, token="x")) == "request has unknown fields: token"  # noqa: S106


@pytest.mark.parametrize("key", ["repo", "model", "effort", "account"])
@pytest.mark.parametrize("bad", [5, None, True, ["x"]])
def test_text_fields_must_be_strings(key: str, bad: object) -> None:
    assert refused(made(BUILD, **{key: bad})) == f"request {key} must be a string"


@pytest.mark.parametrize(
    "key", ["claude_profile", "reason", "class", "variant", "role", "authorized_by"]
)
def test_optional_text_fields_must_be_strings_or_null(key: str) -> None:
    assert refused(made(BUILD, **{key: 5})) == f"request {key} must be a string"


def test_commit_must_be_a_string_or_null() -> None:
    assert refused(made(REVIEW, commit=5)) == "request commit must be a string"


@pytest.mark.parametrize("key", ["timeout", "startup_deadline_seconds"])
@pytest.mark.parametrize("bad", ["5", True, 1.5, [5]])
def test_numbers_must_be_integers(key: str, bad: object) -> None:
    assert refused(made(AGY, **{key: bad})) == f"request {key} must be an integer"


def test_the_startup_deadline_is_not_optional() -> None:
    assert refused(made(BUILD, startup_deadline_seconds=None)) == (
        "request startup_deadline_seconds must be an integer"
    )


@pytest.mark.parametrize("bad", ["yes", 1, 0, None, "true"])
def test_unsandboxed_must_be_a_boolean(bad: object) -> None:
    assert refused(made(BUILD, unsandboxed=bad)) == (
        "request unsandboxed must be true or false"
    )


@pytest.mark.parametrize(
    ("key", "names"),
    [
        ("kind", "exec, start"),
        ("harness", "codex, claude, agy"),
        ("profile", "build, review"),
    ],
)
@pytest.mark.parametrize("bad", ["bad", "EXEC", "", 5, None, ["exec"]])
def test_choices_are_refused(key: str, names: str, bad: object) -> None:
    assert refused(made(BUILD, **{key: bad})) == f"request {key} must be one of {names}"


@pytest.mark.parametrize("repo", ["../x", "/o/r", "r", "o/r/x/y", "o/r s"])
def test_repo_uses_the_repo_rules(repo: str) -> None:
    with pytest.raises(Invalid) as caught:
        request.parse(made(BUILD, repo=repo), host_allows_unsandboxed=False)
    assert str(caught.value).startswith(f"repo {repo!r} ")


@pytest.mark.parametrize(
    "repo", ["example-org/example-repo", "git.example.com/o/r", "o/.github"]
)
def test_repo_may_use_either_form(repo: str) -> None:
    assert (
        request.parse(made(BUILD, repo=repo), host_allows_unsandboxed=False).repo
        == repo
    )


@pytest.mark.parametrize("commit", [COMMIT, COMMIT_256])
def test_commit_may_be_sha1_or_sha256(commit: str) -> None:
    assert (
        request.parse(made(REVIEW, commit=commit), host_allows_unsandboxed=False).commit
        == commit
    )


@pytest.mark.parametrize(
    "commit",
    [
        "",
        "abc",
        "A" * 40,
        "g" * 40,
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
        COMMIT + "\n",
        " " + COMMIT,
    ],
)
def test_commit_format(commit: str) -> None:
    assert refused(made(REVIEW, commit=commit)) == (
        "request commit must match [0-9a-f]{40}|[0-9a-f]{64}"
    )


def test_a_review_run_needs_a_commit_and_a_build_run_has_none() -> None:
    assert (
        refused(made(REVIEW, commit=None))
        == "request commit is required for a review run"
    )
    assert (
        refused(made(BUILD, commit=COMMIT))
        == "request commit applies only to a review run"
    )


@pytest.mark.parametrize(
    ("key", "good", "bad", "rule"),
    [
        ("model", "a" * 128, "a" * 129, "[A-Za-z0-9][A-Za-z0-9._-]{0,127}"),
        ("model", "gpt-6.1_x", "-x", "[A-Za-z0-9][A-Za-z0-9._-]{0,127}"),
        ("model", "x", "", "[A-Za-z0-9][A-Za-z0-9._-]{0,127}"),
        ("model", "x", "a b", "[A-Za-z0-9][A-Za-z0-9._-]{0,127}"),
        ("model", "x", "a\n", "[A-Za-z0-9][A-Za-z0-9._-]{0,127}"),
        ("model", "x", ".x", "[A-Za-z0-9][A-Za-z0-9._-]{0,127}"),
        ("effort", "a" * 32, "a" * 33, "[A-Za-z0-9][A-Za-z0-9_-]{0,31}"),
        ("effort", "x-y_z", "x.y", "[A-Za-z0-9][A-Za-z0-9_-]{0,31}"),
        ("effort", "x", "", "[A-Za-z0-9][A-Za-z0-9_-]{0,31}"),
        ("effort", "x", "x y", "[A-Za-z0-9][A-Za-z0-9_-]{0,31}"),
        ("account", "a" * 64, "a" * 65, NAME_RULE),
        ("account", "ops_1-x", "-x", NAME_RULE),
        ("account", "x", "", NAME_RULE),
        ("account", "x", "a b", NAME_RULE),
        ("account", "x", "a.b", NAME_RULE),
        ("account", "x", "é", NAME_RULE),
        ("class", "a" * 64, "a" * 65, NAME_RULE),
        ("class", "x", "-x", NAME_RULE),
        ("variant", "a" * 64, "a" * 65, NAME_RULE),
        ("variant", "x", "", NAME_RULE),
        ("role", "a" * 64, "a" * 65, NAME_RULE),
        ("role", "x", "a b", NAME_RULE),
    ],
)
def test_field_formats(key: str, good: str, bad: str, rule: str) -> None:
    key_name = key
    parsed = request.parse(made(BUILD, **{key: good}), host_allows_unsandboxed=False)
    assert request.to_dict(parsed)[key_name] == good
    assert refused(made(BUILD, **{key: bad})) == f"request {key} must match {rule}"


def test_claude_profile_format() -> None:
    assert refused(made(CLAUDE, claude_profile="a b")) == (
        f"request claude_profile must match {NAME_RULE}"
    )
    assert refused(made(CLAUDE, claude_profile="")) == (
        f"request claude_profile must match {NAME_RULE}"
    )
    parsed = request.parse(
        made(CLAUDE, claude_profile="a" * 64), host_allows_unsandboxed=False
    )
    assert parsed.claude_profile == "a" * 64


@pytest.mark.parametrize("reason", ["x", "swift-macos", "a b, c.", "é", "a" * 200])
def test_reason_is_one_printable_line(reason: str) -> None:
    assert (
        request.parse(made(CLAUDE, reason=reason), host_allows_unsandboxed=False).reason
        == reason
    )


@pytest.mark.parametrize("reason", ["", " ", "\t", "a\nb", "a\x00", "a" * 201, "a\x7f"])
def test_bad_reasons(reason: str) -> None:
    assert refused(made(CLAUDE, reason=reason)) == f"request reason {REASON}"
    assert refused(made(BUILD, reason=reason)) == f"request reason {REASON}"


@pytest.mark.parametrize("key", ["timeout", "startup_deadline_seconds"])
@pytest.mark.parametrize("bad", [0, -1])
def test_durations_must_be_positive(key: str, bad: int) -> None:
    assert refused(made(AGY, **{key: bad})) == (
        f"request {key} must be a positive number of seconds"
    )


def test_one_second_is_a_valid_duration() -> None:
    parsed = request.parse(
        made(AGY, timeout=1, startup_deadline_seconds=1), host_allows_unsandboxed=False
    )
    assert (parsed.timeout, parsed.startup_deadline_seconds) == (1, 1)


def test_a_claude_run_needs_a_reason_and_a_profile() -> None:
    assert (
        refused(made(CLAUDE, reason=None))
        == "request reason is required for a claude run"
    )
    assert refused(made(CLAUDE, claude_profile=None)) == (
        "request claude_profile is required for a claude run"
    )
    assert request.parse(made(CLAUDE, kind="start"), host_allows_unsandboxed=False)


@pytest.mark.parametrize("harness", ["codex", "agy"])
def test_only_a_claude_run_has_a_claude_profile(harness: str) -> None:
    data = made(AGY, harness=harness, claude_profile="primary")
    assert refused(data) == "request claude_profile applies only to a claude run"


def test_other_harnesses_need_no_reason() -> None:
    assert request.parse(BUILD, host_allows_unsandboxed=False).reason is None
    assert (
        request.parse(made(AGY, reason="why"), host_allows_unsandboxed=False).reason
        == "why"
    )


def test_an_agy_exec_run_needs_a_timeout() -> None:
    assert (
        refused(made(AGY, timeout=None))
        == "request timeout is required for an agy exec run"
    )
    assert request.parse(
        made(AGY, kind="start", timeout=None), host_allows_unsandboxed=False
    )
    assert request.parse(made(BUILD, timeout=None), host_allows_unsandboxed=False)
    assert request.parse(made(CLAUDE, timeout=None), host_allows_unsandboxed=False)


@pytest.mark.parametrize(
    "authorization",
    [
        "example-org/example-repo#12",
        "o/r#1",
        "a-b/r.x_y#9007",
        "operator-chat-2026-11-09",
        "operator-chat-2024-02-29",
        "operator-chat-2026-12-31",
        "operator-chat-2026-01-01",
    ],
)
def test_unsandboxed_authorization_forms(authorization: str) -> None:
    parsed = request.parse(
        made(UNSANDBOXED, authorized_by=authorization), host_allows_unsandboxed=True
    )
    assert parsed.unsandboxed
    assert parsed.authorized_by == authorization


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "operator",
        "o/r",
        "o/r#",
        "o/r#0",
        "o/r#01",
        "o/r#5 ",
        "o/r#\uff15",
        "#5",
        "-o/r#1",
        "o/r s#1",
        "o/r#1\n",
        "operator-chat-2026-02-29",
        "operator-chat-2026-13-01",
        "operator-chat-2026-00-10",
        "operator-chat-2026-11-00",
        "operator-chat-2026-11-31",
        "operator-chat-0000-01-01",
        "operator-chat-2026-1-1",
        "operator-chat-2026-11-09x",
        "operator-chat-\uff12\uff10\uff12\uff16-11-09",
        "operator-chat-",
        "Operator-chat-2026-11-09",
    ],
)
def test_unsandboxed_refuses_other_authorizations(authorization: str) -> None:
    assert refused(made(UNSANDBOXED, authorized_by=authorization)) == (
        "request authorized_by must be owner/repo#N or operator-chat-YYYY-MM-DD"
    )


def test_unsandboxed_rule() -> None:
    assert request.parse(UNSANDBOXED, host_allows_unsandboxed=True).unsandboxed
    assert (
        refused(UNSANDBOXED, host=False)
        == "request unsandboxed isn't enabled on this host"
    )
    assert refused(made(UNSANDBOXED, authorized_by=None)) == (
        "request unsandboxed requires authorized_by"
    )
    assert refused(made(BUILD, authorized_by="o/r#1")) == (
        "request authorized_by requires unsandboxed"
    )
    claude = made(CLAUDE, unsandboxed=True, authorized_by="o/r#1")
    assert refused(claude) == "request unsandboxed applies only to a codex run"
    agy = made(AGY, unsandboxed=True, authorized_by="o/r#1")
    assert refused(agy) == "request unsandboxed applies only to a codex run"
    review = made(REVIEW, unsandboxed=True, authorized_by="o/r#1")
    assert refused(review) == "request unsandboxed applies only to a build run"


def test_the_host_flag_does_not_matter_without_unsandboxed() -> None:
    assert request.parse(BUILD, host_allows_unsandboxed=True)
    assert request.parse(BUILD, host_allows_unsandboxed=False)


def test_validate_checks_a_built_request() -> None:
    built = request.parse(BUILD, host_allows_unsandboxed=False)
    request.validate(built, host_allows_unsandboxed=False)
    with pytest.raises(Invalid, match="request model must match"):
        request.validate(
            dataclasses.replace(built, model="a b"), host_allows_unsandboxed=False
        )
    unsandboxed = dataclasses.replace(built, unsandboxed=True)
    with pytest.raises(Invalid, match="requires authorized_by"):
        request.validate(unsandboxed, host_allows_unsandboxed=True)


def test_the_request_holds_no_credential_field() -> None:
    names = {field.name for field in dataclasses.fields(model.RunRequest)}
    assert not {n for n in names if "token" in n or "secret" in n or "key" in n}
    assert set(
        request.to_dict(request.parse(BUILD, host_allows_unsandboxed=False))
    ) == (set(BUILD))


@pytest.mark.parametrize("tainted", [False, True])
def test_request_reads_driver_host_taint(tainted: bool) -> None:
    parsed = request.parse(
        BUILD | {"tainted_host": tainted}, host_allows_unsandboxed=True
    )
    assert parsed.tainted_host is tainted
    assert request.to_dict(parsed)["tainted_host"] is tainted


def test_request_host_taint_is_required() -> None:
    assert refused(made(BUILD, tainted_host=MISSING)) == (
        "request is missing fields: tainted_host"
    )


@pytest.mark.parametrize("bad", [None, 0, 1, "false"])
def test_request_host_taint_requires_boolean(bad: object) -> None:
    assert (
        refused(BUILD | {"tainted_host": bad})
        == "request tainted_host must be true or false"
    )
