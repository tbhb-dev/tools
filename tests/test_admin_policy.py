"""The refusal table is the administrative boundary, including routing tricks."""

# cspell:words ulesets nvitations idget illing fwidget

import pytest

from tbhbdev.admin import policy

# Rule, method, endpoint, body. Every rule includes an encoded/case variant.
REFUSALS: list[tuple[str, str, str, object]] = [
    ("protection_weakening", "DELETE", "/repos/acme/widget/rulesets/7", None),
    (
        "protection_weakening",
        "PUT",
        "/repos/acme/widget/branches/main/protection",
        {"required_status_checks": None},
    ),
    (
        "protection_weakening",
        "PATCH",
        "/orgs/acme/rulesets/7",
        {"enforcement": "evaluate"},
    ),
    (
        "protection_weakening",
        "PUT",
        "/REPOS/ACME/WIDGET/%72ulesets/7/",
        {"bypass_actors": []},
    ),
    (
        "protection_weakening",
        "PATCH",
        "/repos/acme/widget/rulesets/7",
        {"enforcement": "disabled"},
    ),
    (
        "protection_weakening",
        "PUT",
        "/repos/acme/widget/branches/main/protection/required_pull_request_reviews",
        {"required_approving_review_count": 0},
    ),
    (
        "protection_weakening",
        "PATCH",
        "/repos/acme/widget/rulesets/7",
        {"unknown": True},
    ),
    ("membership_change", "PUT", "/orgs/acme/memberships/user", {"role": "admin"}),
    ("membership_change", "DELETE", "/orgs/acme/outside_collaborators/user", None),
    ("membership_change", "PUT", "/orgs/acme/teams/group/memberships/user", None),
    (
        "membership_change",
        "POST",
        "/ORGS/ACME/%69nvitations/",
        {"email": "user@example.com"},
    ),
    ("membership_change", "PUT", "/repos/acme/widget/collaborators/user", None),
    ("membership_change", "PATCH", "/orgs/acme/organization-roles/7", None),
    ("repository_removal", "DELETE", "/repos/acme/widget", None),
    (
        "repository_removal",
        "POST",
        "/repos/acme/widget/transfer",
        {"new_owner": "other"},
    ),
    ("repository_removal", "PATCH", "/repos/acme/widget", {"archived": True}),
    ("repository_removal", "DELETE", "/REPOS/ACME/%77idget/?x=1", None),
    (
        "paid_feature",
        "PATCH",
        "/repos/acme/widget",
        {"security_and_analysis": {"advanced_security": {"status": "enabled"}}},
    ),
    (
        "paid_feature",
        "PATCH",
        "/repos/acme/widget",
        {"security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
    ),
    ("paid_feature", "POST", "/orgs/acme/actions/hosted-runners", {}),
    ("paid_feature", "POST", "/orgs/acme/codespaces", {}),
    ("paid_feature", "POST", "/orgs/acme/copilot/billing/selected_users", {}),
    ("paid_feature", "PATCH", "/ORGS/ACME/%62illing/spending-limit/", {}),
    ("paid_feature", "PATCH", "/orgs/acme", {"actions_spending_limit": 100}),
    ("public_visibility", "PATCH", "/repos/acme/widget", {"visibility": "public"}),
    ("public_visibility", "POST", "/orgs/acme/repos", {"visibility": "public"}),
    ("public_visibility", "PATCH", "/repos/acme/other", {"private": False}),
    ("public_visibility", "PATCH", "/REPOS/ACME/%77idget/", {"visibility": "public"}),
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
        "https://api.github.com/repos/acme/widget",
        "//api.github.com/repos/acme/widget",
        "/repos/acme/widget#fragment",
        "/repos//acme/widget",
        "/repos/acme/../widget",
        "/repos/acme/%2e%2e/widget",
        "/repos/acme%2fwidget",
        "/repos/acme/%252fwidget",
        "/repos/acme/wi\\dget",
        "repos/acme/widget",
        "/repos/acme/wid%get",
        "/",
        "/repos/acme/./widget",
        " /repos/acme/widget",
        "//[invalid",
        "/repos/acme/wid get",
    ],
)
def test_ambiguous_paths(path: str) -> None:
    assert policy.check("GET", path) == policy.Decision(
        allowed=False, code="ambiguous_path", path=""
    )


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "CONNECT", "TRACE", "get", ""])
def test_invalid_methods(method: str) -> None:
    assert policy.check(method, "/repos/acme/widget") == policy.Decision(
        allowed=False, code="invalid_method", path="/repos/acme/widget"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/graphql",
        "/user",
        "/repos/acme/widget/issues",
        "/orgs/acme/actions/runners/registration-token",
    ],
)
def test_default_deny(path: str) -> None:
    assert policy.check("POST", path) == policy.Decision(
        allowed=False, code="endpoint_denied", path=path
    )


@pytest.mark.parametrize("body", [[], [1], "text", 7, False])
def test_body_must_be_object(body: object) -> None:
    assert policy.check("PATCH", "/repos/acme/widget", body) == policy.Decision(
        allowed=False, code="invalid_body", path="/repos/acme/widget"
    )


@pytest.mark.parametrize("path", ["/repos/acme/widget", "/orgs/acme"])
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
            "/repos/acme/widget/rulesets/7",
            {"enforcement": "active"},
            "administration",
        ),
        (
            "PATCH",
            "/orgs/acme/rulesets/7",
            {"enforcement": "active"},
            "organization_administration",
        ),
        ("GET", "/repos/acme/widget/branches/main/protection", None, "administration"),
        (
            "POST",
            "/repos/acme/widget/branches/main/protection/required_signatures",
            {},
            "administration",
        ),
        (
            "POST",
            "/repos/acme/widget/branches/main/protection/enforce_admins",
            None,
            "administration",
        ),
        (
            "PUT",
            "/orgs/acme/actions/secrets/build/repositories/7",
            None,
            "organization_secrets",
        ),
        ("PUT", "/repos/acme/widget/environments/test", {}, "environments"),
        (
            "PATCH",
            "/repos/acme/widget",
            {"archived": False, "visibility": "private"},
            "administration",
        ),
        ("GET", "/orgs/acme", None, "organization_administration"),
        (
            "POST",
            "/orgs/acme/repos",
            {"name": "widget", "private": True},
            "administration",
        ),
        ("PUT", "/repos/acme/widget/actions/secrets/build", {}, "secrets"),
        ("PATCH", "/repos/acme/widget/actions/variables/name", {}, "variables"),
        (
            "PUT",
            "/orgs/acme/actions/variables/name/repositories/7",
            None,
            "organization_variables",
        ),
        ("GET", "/repos/acme/widget/actions/runners", None, "administration"),
        ("GET", "/orgs/acme/actions/runners", None, "organization_self_hosted_runners"),
        (
            "PUT",
            "/orgs/acme/actions/runner-groups/7/repositories/8",
            None,
            "organization_self_hosted_runners",
        ),
        ("POST", "/repos/acme/widget/hooks", {}, "repository_hooks"),
        ("POST", "/orgs/acme/hooks", {}, "organization_hooks"),
        ("PATCH", "/repos/acme/widget/properties/values", {}, "custom_properties"),
        (
            "PUT",
            "/orgs/acme/properties/schema/name",
            {},
            "organization_custom_properties",
        ),
        ("PUT", "/repos/acme/widget/contents/docs/file.md", {}, "contents"),
        ("POST", "/repos/acme/widget/pulls/7/reviews", {}, "pull_requests"),
    ],
)
def test_allowed_neighbors(
    method: str, path: str, body: object, permission: str
) -> None:
    assert policy.check(method, path, body) == policy.Decision(
        allowed=True, code="allowed", path=path, permission=permission
    )


def test_normalization_and_nested_fields() -> None:
    assert policy.normalize("/REPOS/ACME/%77idget/?page=2") == "/repos/acme/widget"
    assert policy.fields({"Outer": [{"Role": "admin"}], "empty": []}) == {
        "outer",
        "role",
        "empty",
    }
    assert not policy.protection(
        "PUT", "/repos/acme/widget/branches/main/protection", {}
    )
    assert not policy.protection(
        "POST", "/repos/acme/widget/rulesets", {"enforcement": "active"}
    )
    assert not policy.protection(
        "POST",
        "/repos/acme/widget/branches/main/protection/required_signatures",
        {"unknown": True},
    )


@pytest.mark.parametrize("field", ["role", "permission", "permissions", "team_id"])
def test_role_fields_without_membership_path(field: str) -> None:
    assert policy.check(
        "POST", "/orgs/acme/hooks", {"config": {field: "admin"}}
    ) == policy.Decision(
        allowed=False, code="membership_change", path="/orgs/acme/hooks"
    )


@pytest.mark.parametrize("field", ["spending_limit", "use_static_ip"])
def test_paid_fields(field: str) -> None:
    assert policy.check("PATCH", "/orgs/acme", {field: 100}) == policy.Decision(
        allowed=False, code="paid_feature", path="/orgs/acme"
    )


@pytest.mark.parametrize("path", ["/repos/acme/widget", "/orgs/acme"])
def test_get_settings_has_no_write_field_restriction(path: str) -> None:
    assert policy.check("GET", path, {"unknown": True}).allowed is True


def test_complete_setting_lists() -> None:
    repository: dict[str, object] = dict.fromkeys(policy.SETTINGS, True)
    repository.update(archived=False, private=True, visibility="private")
    assert policy.check("PATCH", "/repos/acme/widget", repository).allowed is True
    organization = dict.fromkeys(
        ["name", "description", "company", "blog", "email", "location"], "example"
    )
    assert policy.check("PATCH", "/orgs/acme", organization).allowed is True


@pytest.mark.parametrize("path", ["/./repos/acme/widget", "/repos/acme/widget//"])
def test_first_and_last_empty_or_dot_segments(path: str) -> None:
    assert policy.normalize(path) == ""
