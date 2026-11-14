"""The refusal table is the administrative boundary, including routing tricks."""

# cspell:words ulesets nvitations idget illing fwidget

import pytest

from tbhbdev.admin import policy

# Rule, method, endpoint, body. Every rule includes an encoded/case variant.
REFUSALS: list[tuple[str, str, str, object]] = [
    (
        "approval_or_merge",
        "POST",
        "/repos/tbhb-dev/widget/pulls/7/reviews",
        {"event": "APPROVE"},
    ),
    ("approval_or_merge", "POST", "/repos/tbhb-dev/widget/pulls/7/reviews/", None),
    (
        "approval_or_merge",
        "POST",
        "/REPOS/TBHB-DEV/WIDGET/PULLS/7/%52EVIEWS/9/events",
        {"event": "APPROVE"},
    ),
    ("approval_or_merge", "PUT", "/repos/tbhb-dev/widget/pulls/7/merge", {}),
    ("approval_or_merge", "GET", "/repos/tbhb-dev/widget/pulls/7/merge", None),
    ("approval_or_merge", "PUT", "/REPOS/TBHB-DEV/WIDGET/PULLS/7/MERG%45/", None),
    ("foreign_owner", "GET", "/repos/other-org/widget", None),
    ("foreign_owner", "PATCH", "/orgs/other-org", {"description": "x"}),
    ("foreign_owner", "GET", "/orgs/other-org/actions/runners", None),
    ("foreign_owner", "GET", "/REPOS/TBHB-DEV-X/WIDGET", None),
    ("foreign_owner", "POST", "/repos/other-org/widget/rulesets", {}),
    ("protection_weakening", "DELETE", "/repos/tbhb-dev/widget/rulesets/7", None),
    (
        "protection_weakening",
        "PUT",
        "/repos/tbhb-dev/widget/branches/main/protection",
        {"required_status_checks": None},
    ),
    (
        "protection_weakening",
        "PATCH",
        "/orgs/tbhb-dev/rulesets/7",
        {"enforcement": "evaluate"},
    ),
    (
        "protection_weakening",
        "PUT",
        "/REPOS/TBHB-DEV/WIDGET/%72ulesets/7/",
        {"bypass_actors": []},
    ),
    (
        "protection_weakening",
        "PATCH",
        "/repos/tbhb-dev/widget/rulesets/7",
        {"enforcement": "disabled"},
    ),
    (
        "protection_weakening",
        "PUT",
        "/repos/tbhb-dev/widget/branches/main/protection/required_pull_request_reviews",
        {"required_approving_review_count": 0},
    ),
    (
        "protection_weakening",
        "PATCH",
        "/repos/tbhb-dev/widget/rulesets/7",
        {"unknown": True},
    ),
    ("membership_change", "PUT", "/orgs/tbhb-dev/memberships/user", {"role": "admin"}),
    ("membership_change", "DELETE", "/orgs/tbhb-dev/outside_collaborators/user", None),
    ("membership_change", "PUT", "/orgs/tbhb-dev/teams/group/memberships/user", None),
    (
        "membership_change",
        "POST",
        "/ORGS/TBHB-DEV/%69nvitations/",
        {"email": "user@example.com"},
    ),
    ("membership_change", "PUT", "/repos/tbhb-dev/widget/collaborators/user", None),
    ("membership_change", "PATCH", "/orgs/tbhb-dev/organization-roles/7", None),
    ("repository_removal", "DELETE", "/repos/tbhb-dev/widget", None),
    (
        "repository_removal",
        "POST",
        "/repos/tbhb-dev/widget/transfer",
        {"new_owner": "other"},
    ),
    ("repository_removal", "PATCH", "/repos/tbhb-dev/widget", {"archived": True}),
    ("repository_removal", "DELETE", "/REPOS/TBHB-DEV/%77idget/?x=1", None),
    (
        "paid_feature",
        "PATCH",
        "/repos/tbhb-dev/widget",
        {"security_and_analysis": {"advanced_security": {"status": "enabled"}}},
    ),
    (
        "paid_feature",
        "PATCH",
        "/repos/tbhb-dev/widget",
        {"security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
    ),
    ("paid_feature", "POST", "/orgs/tbhb-dev/actions/hosted-runners", {}),
    ("paid_feature", "POST", "/orgs/tbhb-dev/codespaces", {}),
    ("paid_feature", "POST", "/orgs/tbhb-dev/copilot/billing/selected_users", {}),
    ("paid_feature", "PATCH", "/ORGS/TBHB-DEV/%62illing/spending-limit/", {}),
    ("paid_feature", "PATCH", "/orgs/tbhb-dev", {"actions_spending_limit": 100}),
    ("public_visibility", "PATCH", "/repos/tbhb-dev/widget", {"visibility": "public"}),
    ("public_visibility", "POST", "/orgs/tbhb-dev/repos", {"visibility": "public"}),
    ("public_visibility", "PATCH", "/repos/tbhb-dev/other", {"private": False}),
    (
        "public_visibility",
        "PATCH",
        "/REPOS/TBHB-DEV/%77idget/",
        {"visibility": "public"},
    ),
]


@pytest.mark.parametrize(("code", "method", "path", "body"), REFUSALS)
def test_refusal_table(code: str, method: str, path: str, body: object) -> None:
    decision = policy.check(method, path, body)
    assert decision == policy.Decision(
        allowed=False, code=code, path=policy.normalize(path)
    )


@pytest.mark.parametrize(
    "path",
    [
        "https://api.github.com/repos/tbhb-dev/widget",
        "//api.github.com/repos/tbhb-dev/widget",
        "/repos/tbhb-dev/widget#fragment",
        "/repos//tbhb-dev/widget",
        "/repos/tbhb-dev/../widget",
        "/repos/tbhb-dev/%2e%2e/widget",
        "/repos/tbhb-dev%2fwidget",
        "/repos/tbhb-dev/%252fwidget",
        "/repos/tbhb-dev/wi\\dget",
        "repos/tbhb-dev/widget",
        "/repos/tbhb-dev/wid%get",
        "/",
        "/repos/tbhb-dev/./widget",
        " /repos/tbhb-dev/widget",
        "//[invalid",
        "/repos/tbhb-dev/wid get",
    ],
)
def test_ambiguous_paths(path: str) -> None:
    assert policy.check("GET", path) == policy.Decision(
        allowed=False, code="ambiguous_path", path=""
    )


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "CONNECT", "TRACE", "get", ""])
def test_invalid_methods(method: str) -> None:
    assert policy.check(method, "/repos/tbhb-dev/widget") == policy.Decision(
        allowed=False, code="invalid_method", path="/repos/tbhb-dev/widget"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/graphql",
        "/user",
        "/repos/tbhb-dev/widget/issues",
        "/orgs/tbhb-dev/actions/runners/registration-token",
    ],
)
def test_default_deny(path: str) -> None:
    assert policy.check("POST", path) == policy.Decision(
        allowed=False, code="endpoint_denied", path=path
    )


@pytest.mark.parametrize("body", [[], [1], "text", 7, False])
def test_body_must_be_object(body: object) -> None:
    assert policy.check("PATCH", "/repos/tbhb-dev/widget", body) == policy.Decision(
        allowed=False, code="invalid_body", path="/repos/tbhb-dev/widget"
    )


@pytest.mark.parametrize("path", ["/repos/tbhb-dev/widget", "/orgs/tbhb-dev"])
def test_unknown_settings(path: str) -> None:
    assert policy.check("PATCH", path, {"unknown": True}) == policy.Decision(
        allowed=False, code="unknown_setting", path=path
    )
    assert policy.check("PATCH", path, {"description": "example"}).allowed


@pytest.mark.parametrize(
    ("method", "path", "body", "permission"),
    [
        (
            "PUT",
            "/repos/tbhb-dev/widget/rulesets/7",
            {"enforcement": "active"},
            "administration",
        ),
        (
            "PATCH",
            "/orgs/tbhb-dev/rulesets/7",
            {"enforcement": "active"},
            "organization_administration",
        ),
        (
            "GET",
            "/repos/tbhb-dev/widget/branches/main/protection",
            None,
            "administration",
        ),
        (
            "POST",
            "/repos/tbhb-dev/widget/branches/main/protection/required_signatures",
            {},
            "administration",
        ),
        (
            "POST",
            "/repos/tbhb-dev/widget/branches/main/protection/enforce_admins",
            None,
            "administration",
        ),
        (
            "PUT",
            "/orgs/tbhb-dev/actions/secrets/build/repositories/7",
            None,
            "organization_secrets",
        ),
        ("PUT", "/repos/tbhb-dev/widget/environments/test", {}, "environments"),
        (
            "PATCH",
            "/repos/tbhb-dev/widget",
            {"archived": False, "visibility": "private"},
            "administration",
        ),
        ("GET", "/orgs/tbhb-dev", None, "organization_administration"),
        (
            "POST",
            "/orgs/tbhb-dev/repos",
            {"name": "widget", "private": True},
            "administration",
        ),
        ("PUT", "/repos/tbhb-dev/widget/actions/secrets/build", {}, "secrets"),
        ("PATCH", "/repos/tbhb-dev/widget/actions/variables/name", {}, "variables"),
        (
            "PUT",
            "/orgs/tbhb-dev/actions/variables/name/repositories/7",
            None,
            "organization_variables",
        ),
        ("GET", "/repos/tbhb-dev/widget/actions/runners", None, "administration"),
        (
            "GET",
            "/orgs/tbhb-dev/actions/runners",
            None,
            "organization_self_hosted_runners",
        ),
        (
            "PUT",
            "/orgs/tbhb-dev/actions/runner-groups/7/repositories/8",
            None,
            "organization_self_hosted_runners",
        ),
        ("POST", "/repos/tbhb-dev/widget/hooks", {}, "repository_hooks"),
        ("POST", "/orgs/tbhb-dev/hooks", {}, "organization_hooks"),
        ("PATCH", "/repos/tbhb-dev/widget/properties/values", {}, "custom_properties"),
        (
            "PUT",
            "/orgs/tbhb-dev/properties/schema/name",
            {},
            "organization_custom_properties",
        ),
        ("PUT", "/repos/tbhb-dev/widget/contents/docs/file.md", {}, "contents"),
        ("GET", "/repos/tbhb-dev/widget/pulls/7/reviews", None, "pull_requests"),
        ("POST", "/repos/tbhb-dev/widget/pulls/7/comments", {}, "pull_requests"),
    ],
)
def test_allowed_neighbors(
    method: str, path: str, body: object, permission: str
) -> None:
    assert policy.check(method, path, body) == policy.Decision(
        allowed=True, code="allowed", path=path, permission=permission
    )


def test_owner_check_ignores_paths_outside_repos_and_orgs() -> None:
    assert policy.check("GET", "/user").code == "endpoint_denied"
    assert policy.check("GET", "/repos/tbhb-dev").code == "endpoint_denied"
    assert policy.check("GET", "/orgs/tbhb-dev").allowed is True


def test_a_repository_named_merge_or_reviews_is_not_a_refusal() -> None:
    assert policy.check("GET", "/repos/tbhb-dev/merge").allowed is True
    assert policy.check("PATCH", "/repos/tbhb-dev/reviews", {}).allowed is True


def test_normalization_and_nested_fields() -> None:
    assert (
        policy.normalize("/REPOS/TBHB-DEV/%77idget/?page=2") == "/repos/tbhb-dev/widget"
    )
    assert policy.fields({"Outer": [{"Role": "admin"}], "empty": []}) == {
        "outer",
        "role",
        "empty",
    }
    assert not policy.protection(
        "PUT", "/repos/tbhb-dev/widget/branches/main/protection", {}
    )
    assert not policy.protection(
        "POST", "/repos/tbhb-dev/widget/rulesets", {"enforcement": "active"}
    )
    assert not policy.protection(
        "POST",
        "/repos/tbhb-dev/widget/branches/main/protection/required_signatures",
        {"unknown": True},
    )


@pytest.mark.parametrize("field", ["role", "permission", "permissions", "team_id"])
def test_role_fields_without_membership_path(field: str) -> None:
    assert policy.check(
        "POST", "/orgs/tbhb-dev/hooks", {"config": {field: "admin"}}
    ) == policy.Decision(
        allowed=False, code="membership_change", path="/orgs/tbhb-dev/hooks"
    )


@pytest.mark.parametrize("field", ["spending_limit", "use_static_ip"])
def test_paid_fields(field: str) -> None:
    assert policy.check("PATCH", "/orgs/tbhb-dev", {field: 100}) == policy.Decision(
        allowed=False, code="paid_feature", path="/orgs/tbhb-dev"
    )


@pytest.mark.parametrize("path", ["/repos/tbhb-dev/widget", "/orgs/tbhb-dev"])
def test_get_settings_has_no_write_field_restriction(path: str) -> None:
    assert policy.check("GET", path, {"unknown": True}).allowed is True


def test_complete_setting_lists() -> None:
    repository: dict[str, object] = dict.fromkeys(policy.SETTINGS, True)
    repository.update(archived=False, private=True, visibility="private")
    assert policy.check("PATCH", "/repos/tbhb-dev/widget", repository).allowed is True
    organization = dict.fromkeys(
        ["name", "description", "company", "blog", "email", "location"], "example"
    )
    assert policy.check("PATCH", "/orgs/tbhb-dev", organization).allowed is True


@pytest.mark.parametrize(
    "path", ["/./repos/tbhb-dev/widget", "/repos/tbhb-dev/widget//"]
)
def test_first_and_last_empty_or_dot_segments(path: str) -> None:
    assert policy.normalize(path) == ""
