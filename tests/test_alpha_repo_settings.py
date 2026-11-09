# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the pure functions behind tbhbdev alpha repo bootstrap."""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from repo_settings_example import DEFAULTS, EXAMPLE

from tbhbdev.alpha import repo_settings as rs

# Each allowed key with the kind of value it takes, in the order the commands list them.
KINDS = {
    "allow_squash_merge": "bool",
    "allow_merge_commit": "bool",
    "allow_rebase_merge": "bool",
    "squash_merge_commit_title": "enum",
    "squash_merge_commit_message": "enum",
    "delete_branch_on_merge": "bool",
    "has_issues": "bool",
    "has_wiki": "bool",
    "has_projects": "bool",
    "allow_auto_merge": "bool",
    "allow_update_branch": "bool",
    "web_commit_signoff_required": "bool",
}
ALLOWED = tuple(KINDS)
F = "repo-settings.toml"
LIST = ", ".join(ALLOWED)
ALLOWED_ARE = f"the allowed ones are {LIST}"
TITLE_ENUM = (
    f"{F}: [defaults] squash_merge_commit_title must be one of "
    "PR_TITLE, COMMIT_OR_PR_TITLE"
)
MESSAGE_ENUM = (
    f"{F}: [defaults] squash_merge_commit_message must be one of "
    "PR_BODY, COMMIT_MESSAGES, BLANK"
)


def test_the_allowlist_is_exactly_these_keys_in_this_order() -> None:
    assert rs.KEYS == ALLOWED


def test_the_enum_values_are_the_ones_github_documents() -> None:
    assert rs.ENUMS == {
        "squash_merge_commit_title": ("PR_TITLE", "COMMIT_OR_PR_TITLE"),
        "squash_merge_commit_message": ("PR_BODY", "COMMIT_MESSAGES", "BLANK"),
    }


def test_the_config_file_name() -> None:
    assert rs.CONFIG_FILE == "repo-settings.toml"


def test_the_example_config_parses() -> None:
    config = rs.parse_config(EXAMPLE)
    assert config.defaults == DEFAULTS
    assert config.repos == {"example-org/example-repo": {"has_issues": True}}


def test_repos_is_optional() -> None:
    config = rs.parse_config("[defaults]\nhas_wiki = false\n")
    assert config == rs.Config({"has_wiki": False}, {})


@pytest.mark.parametrize("key", ALLOWED)
def test_every_allowed_key_takes_its_type(key: str) -> None:
    if KINDS[key] == "enum":
        for value in rs.ENUMS[key]:
            config = rs.parse_config(f'[defaults]\n{key} = "{value}"\n')
            assert config.defaults == {key: value}
    else:
        for value in (True, False):
            text = f"[defaults]\n{key} = {str(value).lower()}\n"
            assert rs.parse_config(text).defaults == {key: value}


@pytest.mark.parametrize(
    "key",
    [
        "visibility",
        "private",
        "archived",
        "default_branch",
        "name",
        "security_and_analysis",
        "has_discussions",
        "description",
    ],
)
@pytest.mark.parametrize("where", ["defaults", 'repos."example-org/example-repo"'])
def test_a_key_outside_the_allowlist_is_refused_by_name(key: str, where: str) -> None:
    text = f'[defaults]\n{key} = "public"\n'
    if where != "defaults":
        text = f'[defaults]\nhas_wiki = false\n[{where}]\n{key} = "public"\n'
    message = (
        f"{F}: [{where}] {key} is not an allowed setting; the allowed ones are {LIST}"
    )
    with pytest.raises(rs.ConfigError) as caught:
        rs.parse_config(text)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[other]\n", f"{F}: other is not a table this command reads"),
        ("", f"{F}: [defaults] is missing"),
        ("defaults = 1\n", f"{F}: [defaults] must be a table"),
        (
            "[defaults]\nhas_issues = 1\n",
            f"{F}: [defaults] has_issues must be true or false",
        ),
        (
            '[defaults]\nhas_wiki = "no"\n',
            f"{F}: [defaults] has_wiki must be true or false",
        ),
        (
            '[defaults]\nsquash_merge_commit_title = "NOPE"\n',
            TITLE_ENUM,
        ),
        (
            "[defaults]\nsquash_merge_commit_message = 1\n",
            MESSAGE_ENUM,
        ),
        (
            '[defaults]\nsquash_merge_commit_message = "PR_TITLE"\n',
            MESSAGE_ENUM,
        ),
        (
            "[defaults]\nrepos = 1\n",
            f"{F}: [defaults] repos is not an allowed setting; {ALLOWED_ARE}",
        ),
        ("repos = 1\n[defaults]\n", f"{F}: [repos] must be a table"),
        (
            "[defaults]\n[repos.nope]\n",
            f'{F}: [repos."nope"] must be named owner/name',
        ),
        (
            '[defaults]\n[repos."a/b/c"]\n',
            f'{F}: [repos."a/b/c"] must be named owner/name',
        ),
        (
            '[defaults]\n[repos]\n"a/b" = 1\n',
            f'{F}: [repos."a/b"] must be a table',
        ),
        (
            '[defaults]\n[repos."A/b"]\n[repos."a/B"]\n',
            f'{F}: [repos."a/B"] repeats a repository',
        ),
    ],
)
def test_invalid_config(text: str, message: str) -> None:
    with pytest.raises(rs.ConfigError) as caught:
        rs.parse_config(text)
    assert str(caught.value) == message


def test_an_empty_repos_table_is_fine() -> None:
    assert rs.parse_config("[defaults]\n[repos]\n").repos == {}


def test_text_that_is_not_toml_names_the_file() -> None:
    with pytest.raises(rs.ConfigError, match=r"^repo-settings\.toml: "):
        rs.parse_config("[defaults\n")


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("example-org/example-repo", True),
        ("a/b", True),
        ("Example_Org/repo.name-1", True),
        ("example-org", False),
        ("example-org/", False),
        ("/repo", False),
        ("a/b/c", False),
        ("a/.b", True),
        ("example-org/.github", True),
        ("a/..b", True),
        ("a/b.", True),
        ("a/-", True),
        ("a/.", False),
        ("a/..", False),
        (".a/b", False),
        ("..a/b", False),
        ("./b", False),
        ("a /b", False),
        ("a/b\n", False),
        ("a/.\n", False),
        ("a/..\n", False),
        ("a\n/b", False),
        ("a/b c", False),
        ("../x", False),
        ("", False),
        *[
            (name, False)
            for char in "?#% \n\t:@\\*"
            for name in (f"a/b{char}", f"a/b{char}c", f"a{char}/b", f"a/{char}")
        ],
        ("a/b?x=1", False),
        ("a/b#frag", False),
        ("a/b%2e", False),
        ("a/%2e", False),
        ("a/b/", False),
    ],
)
def test_valid_repo(name: str, ok: bool) -> None:
    assert rs.valid_repo(name) is ok


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("example-org/.github", True),
        ("Example-Org/.GitHub", True),
        ("a/.", False),
        ("a/..", False),
        (".a/b", False),
        ("a/b?", False),
        ("a/b#", False),
        ("a/b%", False),
        ("a/b c", False),
        ("a/b\n", False),
        ("a?/b", False),
        ("a#/b", False),
        ("a%/b", False),
        ("a b/b", False),
        ("a\n/b", False),
    ],
)
def test_a_config_key_follows_the_same_name_rule(name: str, ok: bool) -> None:
    text = f"[defaults]\n[repos.{json.dumps(name)}]\nhas_wiki = true\n"
    if ok:
        assert rs.parse_config(text).repos == {name.lower(): {"has_wiki": True}}
    else:
        with pytest.raises(rs.ConfigError) as caught:
            rs.parse_config(text)
        assert str(caught.value) == f'{F}: [repos."{name}"] must be named owner/name'


def test_desired_puts_the_repos_table_over_the_defaults() -> None:
    config = rs.parse_config(EXAMPLE)
    want = rs.desired(config, "example-org/example-repo")
    assert want == {**DEFAULTS, "has_issues": True}


def test_desired_for_an_unlisted_repo_is_the_defaults() -> None:
    config = rs.parse_config(EXAMPLE)
    assert rs.desired(config, "example-org/other") == DEFAULTS


def test_desired_ignores_the_case_of_the_name() -> None:
    config = rs.parse_config(EXAMPLE)
    assert rs.desired(config, "Example-Org/Example-Repo")["has_issues"] is True


def test_desired_does_not_change_the_config() -> None:
    config = rs.parse_config(EXAMPLE)
    rs.desired(config, "example-org/example-repo")["has_wiki"] = True
    assert config.defaults == DEFAULTS


def test_current_keeps_only_the_allowlisted_keys() -> None:
    payload = {
        "name": "example-repo",
        "private": True,
        "visibility": "private",
        "has_issues": True,
        "allow_squash_merge": False,
        "squash_merge_commit_title": "PR_TITLE",
    }
    assert rs.current(payload) == {
        "allow_squash_merge": False,
        "squash_merge_commit_title": "PR_TITLE",
        "has_issues": True,
    }
    assert list(rs.current(payload)) == [
        "allow_squash_merge",
        "squash_merge_commit_title",
        "has_issues",
    ]


@pytest.mark.parametrize(
    ("have", "want", "expected"),
    [
        ({}, {}, {}),
        ({"has_wiki": True}, {"has_wiki": True}, {}),
        ({"has_wiki": True}, {"has_wiki": False}, {"has_wiki": (True, False)}),
        ({}, {"has_wiki": False}, {"has_wiki": (None, False)}),
        ({"has_wiki": False}, {}, {}),
        (
            {"has_issues": True, "has_wiki": True, "allow_squash_merge": True},
            {"has_issues": False, "has_wiki": True, "allow_squash_merge": False},
            {"allow_squash_merge": (True, False), "has_issues": (True, False)},
        ),
    ],
)
def test_diff(
    have: dict[str, object],
    want: dict[str, object],
    expected: dict[str, tuple[object, object]],
) -> None:
    assert rs.diff(have, want) == expected


def test_diff_lists_keys_in_allowlist_order() -> None:
    want = {"has_projects": True, "allow_squash_merge": True, "has_wiki": True}
    assert list(rs.diff({}, want)) == ["allow_squash_merge", "has_wiki", "has_projects"]


def test_diff_ignores_a_key_outside_the_allowlist() -> None:
    assert rs.diff({}, {"visibility": "public"}) == {}


PAIR_HAVE = {
    "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
    "squash_merge_commit_message": "COMMIT_MESSAGES",
}


@pytest.mark.parametrize(
    ("changes", "want", "have", "body"),
    [
        ({}, {}, {}, {}),
        (
            {"has_wiki": (True, False)},
            {"has_wiki": False},
            {"has_wiki": True},
            {"has_wiki": False},
        ),
        # Both wanted: both come from want.
        (
            {"squash_merge_commit_message": ("PR_BODY", "BLANK")},
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "BLANK",
            },
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "PR_BODY",
            },
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "BLANK",
            },
        ),
        (
            {"squash_merge_commit_title": ("COMMIT_OR_PR_TITLE", "PR_TITLE")},
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "BLANK",
            },
            {
                "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
                "squash_merge_commit_message": "BLANK",
            },
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "BLANK",
            },
        ),
        # Want wins over have for a key in both.
        (
            {"squash_merge_commit_message": ("PR_BODY", "BLANK")},
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "BLANK",
            },
            PAIR_HAVE,
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "BLANK",
            },
        ),
        # Only the message is wanted: the title is the repository's.
        (
            {"squash_merge_commit_message": ("COMMIT_MESSAGES", "BLANK")},
            {"squash_merge_commit_message": "BLANK"},
            PAIR_HAVE,
            {
                "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
                "squash_merge_commit_message": "BLANK",
            },
        ),
        # Only the title is wanted: the message is the repository's.
        (
            {"squash_merge_commit_title": ("COMMIT_OR_PR_TITLE", "PR_TITLE")},
            {"squash_merge_commit_title": "PR_TITLE"},
            PAIR_HAVE,
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "COMMIT_MESSAGES",
            },
        ),
        # Only the message is wanted and the reply lacks the title.
        (
            {"squash_merge_commit_message": (None, "BLANK")},
            {"squash_merge_commit_message": "BLANK"},
            {},
            {"squash_merge_commit_message": "BLANK"},
        ),
        # Only the title is wanted and the reply lacks the message.
        (
            {"squash_merge_commit_title": (None, "PR_TITLE")},
            {"squash_merge_commit_title": "PR_TITLE"},
            {"has_wiki": True},
            {"squash_merge_commit_title": "PR_TITLE"},
        ),
        # Nothing of the pair changes: its current values aren't sent.
        (
            {"has_wiki": (True, False)},
            {"has_wiki": False, "squash_merge_commit_title": "PR_TITLE"},
            {**PAIR_HAVE, "has_wiki": True},
            {"has_wiki": False},
        ),
        (
            {"has_wiki": (True, False)},
            {"has_wiki": False},
            {**PAIR_HAVE, "has_wiki": True},
            {"has_wiki": False},
        ),
        # Other current keys never join the body.
        (
            {"squash_merge_commit_title": ("COMMIT_OR_PR_TITLE", "PR_TITLE")},
            {"squash_merge_commit_title": "PR_TITLE"},
            {**PAIR_HAVE, "has_wiki": True, "has_issues": True},
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "COMMIT_MESSAGES",
            },
        ),
    ],
)
def test_patch_body_sends_the_changed_keys_and_the_pair_together(
    changes: dict[str, tuple[object, object]],
    want: dict[str, object],
    have: dict[str, object],
    body: dict[str, object],
) -> None:
    assert rs.patch_body(changes, want, have) == body


@pytest.mark.parametrize(
    ("value", "text"),
    [(True, "true"), (False, "false"), ("PR_TITLE", '"PR_TITLE"'), (None, "null")],
)
def test_show(value: object, text: str) -> None:
    assert rs.show(value) == text


def test_render_changes_lists_each_key_then_the_count() -> None:
    changes: rs.Changes = {
        "allow_squash_merge": (None, True),
        "has_wiki": (True, False),
    }
    assert rs.render_changes(changes) == (
        "allow_squash_merge: null -> true\nhas_wiki: true -> false\n2 changes"
    )


def test_render_changes_one_change() -> None:
    assert rs.render_changes({"has_wiki": (True, False)}) == (
        "has_wiki: true -> false\n1 change"
    )


def test_render_changes_none() -> None:
    assert rs.render_changes({}) == "0 changes"


def test_render_create() -> None:
    want = {"has_wiki": False, "allow_squash_merge": True}
    assert rs.render_create("example-org/example-repo", "private", want) == (
        "would create example-org/example-repo (private)\n"
        "settings it would get:\n"
        "allow_squash_merge: true\n"
        "has_wiki: false"
    )


def test_timestamp_is_utc_rfc_3339() -> None:
    zone = timezone(timedelta(hours=-5))
    moment = datetime(2026, 11, 9, 8, 30, 5, 123456, tzinfo=zone)
    assert rs.timestamp(moment) == "2026-11-09T13:30:05Z"


def test_log_entry_has_exactly_the_documented_fields() -> None:
    moment = datetime(2026, 11, 9, 13, 30, 5, tzinfo=UTC)
    entry = rs.log_entry(
        "example-org/example-repo",
        {"has_wiki": (True, False), "has_issues": (None, True)},
        moment,
        created=True,
        verified=False,
    )
    assert entry == {
        "v": 1,
        "ts": "2026-11-09T13:30:05Z",
        "repo": "example-org/example-repo",
        "created": True,
        "changed": {"has_wiki": [True, False], "has_issues": [None, True]},
        "verified": False,
    }
    assert list(entry) == ["v", "ts", "repo", "created", "changed", "verified"]
