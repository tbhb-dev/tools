"""Pure, default-deny REST policy. No policy check performs I/O."""

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
OWNER = "tbhb-dev"
PART = r"[a-z0-9_.-]+"
REPO = rf"/repos/{PART}/{PART}"
ORG = rf"/orgs/{PART}"
OWNED = re.compile(rf"/(?:repos|orgs)/({PART})(?:/|$)")
REVIEW = re.compile(rf"{REPO}/pulls/[0-9]+/reviews(?:/|$)")
MERGE = re.compile(rf"{REPO}/pulls/[0-9]+/merge")
ENVIRONMENT = re.compile(rf"{REPO}/environments/{PART}")
SHARED = re.compile(rf"{ORG}/actions/(?:secrets|variables)/{PART}")
# The mutant that swaps a-z for A-Z survives: IGNORECASE makes them one set.
ROUTABLE = re.compile(r"/[a-z0-9_./-]+", re.IGNORECASE | re.ASCII)  # pragma: no mutate
PAID = frozenset(
    {"billing", "copilot", "codespaces", "hosted-runners", "larger-runners"}
)
MEMBERSHIP = frozenset(
    {
        "members",
        "memberships",
        "outside_collaborators",
        "collaborators",
        "invitations",
        "teams",
        "role",
        "roles",
        "organization-roles",
        "repository-roles",
        "custom-repository-roles",
        "custom-roles",
    }
)
SETTINGS = frozenset(
    {
        "name",
        "description",
        "homepage",
        "private",
        "visibility",
        "has_issues",
        "has_projects",
        "has_wiki",
        "is_template",
        "default_branch",
        "allow_squash_merge",
        "allow_merge_commit",
        "allow_rebase_merge",
        "allow_auto_merge",
        "delete_branch_on_merge",
        "archived",
        "web_commit_signoff_required",
    }
)
# Every expression is anchored by fullmatch. Unknown descendants stay denied.
FAMILIES = (
    (rf"{REPO}", "administration"),
    (rf"{ORG}", "organization_administration"),
    (rf"{ORG}/repos", "administration"),
    (rf"{REPO}/rulesets(?:/[0-9]+)?", "administration"),
    (rf"{ORG}/rulesets(?:/[0-9]+)?", "organization_administration"),
    (rf"{REPO}/branches/{PART}/protection(?:/{PART})?", "administration"),
    (rf"{REPO}/actions/secrets(?:/{PART})?(?:/public-key)?", "secrets"),
    (
        rf"{ORG}/actions/secrets(?:/{PART})?(?:/public-key|/repositories(?:/[0-9]+)?)?",
        "organization_secrets",
    ),
    (
        rf"{ORG}/actions/variables(?:/{PART})?(?:/repositories(?:/[0-9]+)?)?",
        "organization_variables",
    ),
    (rf"{REPO}/actions/variables(?:/{PART})?", "variables"),
    (
        rf"{REPO}/environments(?:/{PART})?(?:/(?:secrets|variables)(?:/{PART})?)?",
        "environments",
    ),
    (
        rf"{REPO}/actions/runners(?:/[0-9]+)?",
        "administration",
    ),
    (
        rf"{ORG}/actions/runners(?:/[0-9]+)?",
        "organization_self_hosted_runners",
    ),
    (
        rf"{ORG}/actions/runner-groups(?:/[0-9]+)?(?:/(?:repositories|runners)(?:/[0-9]+)?)?",
        "organization_self_hosted_runners",
    ),
    (rf"{REPO}/hooks(?:/[0-9]+)?(?:/(?:pings|tests))?", "repository_hooks"),
    (rf"{ORG}/hooks(?:/[0-9]+)?(?:/pings)?", "organization_hooks"),
    (rf"{REPO}/properties/values", "custom_properties"),
    (
        rf"{ORG}/properties/(?:schema(?:/{PART})?|values)",
        "organization_custom_properties",
    ),
    (rf"{REPO}/contents(?:/{PART})*", "contents"),
    (
        rf"{REPO}/pulls(?:/[0-9]+)?(?:/(?:reviews|comments|requested_reviewers)(?:/[0-9]+)?)?",
        "pull_requests",
    ),
)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    path: str
    permission: str = ""


def normalize(  # Reject each ambiguous routing form.
    path: str, *, fold: bool = True
) -> str:
    """Canonical ASCII path, lowercase unless ``fold`` is false.

    A query string is dropped. Reject encodings that can change routing.
    """
    try:
        parsed = urlsplit(path)
    except ValueError:
        return ""
    if path != path.strip():
        return ""
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return ""
    raw = parsed.path.removesuffix("/")
    decoded = unquote(raw)
    if not ROUTABLE.fullmatch(decoded):
        return ""
    if raw.count("/") != decoded.count("/") or any(
        part in {".", "..", ""} for part in decoded.split("/")[1:]
    ):
        return ""
    return decoded.lower() if fold else decoded


def fields(body: object) -> set[str]:
    """All nested field names, for rules that apply even inside objects."""
    if isinstance(body, dict):
        mapping: dict[str, object] = body
        return {k.lower() for k in mapping} | set().union(
            *(fields(v) for v in mapping.values())
        )
    if isinstance(body, list):
        items: list[object] = body
        return set().union(*(fields(v) for v in items))
    return set()


def protection(method: str, path: str, body: dict[str, object]) -> bool:
    """Only operations that can be classified without previous state pass."""
    if "/rulesets" in path:
        return method in {"PUT", "PATCH"} and body == {"enforcement": "active"}
    return (
        method == "POST"
        and path.endswith(("/required_signatures", "/enforce_admins"))
        and not body
    )


def write_refusal(method: str, path: str, body: dict[str, object]) -> str:
    if set(path.split("/")) & MEMBERSHIP or fields(body) & {
        "role",
        "permission",
        "permissions",
        "team_id",
    }:
        return "membership_change"
    if re.fullmatch(REPO, path) and (
        method == "DELETE" or body.get("archived") is True
    ):
        return "repository_removal"
    if re.fullmatch(rf"{REPO}/transfer", path):
        return "repository_removal"
    if body.get("visibility") == "public" or body.get("private") is False:
        return "public_visibility"
    if ("/rulesets" in path or "/protection" in path) and not protection(
        method, path, body
    ):
        return "protection_weakening"
    return ""


def deployment_refusal(method: str, path: str, body: dict[str, object]) -> str:
    """Refuse environment changes that drop protection and broad org sharing."""
    if ENVIRONMENT.fullmatch(path) and (
        method == "DELETE"
        or ("reviewers" in body and not body["reviewers"])
        or ("wait_timer" in body and not body["wait_timer"])
        or (
            "deployment_branch_policy" in body
            and body["deployment_branch_policy"] is None
        )
        or body.get("prevent_self_review") is False
    ):
        return "protection_weakening"
    # A default of "SELECTED" survives: .lower() makes its case moot.
    visibility = str(body.get("visibility", "selected")).lower()  # pragma: no mutate
    if SHARED.fullmatch(path) and visibility != "selected":
        return "broad_visibility"
    return ""


def explicit_refusal(method: str, path: str, body: dict[str, object]) -> str:
    if set(path.split("/")) & PAID or "security_and_analysis" in fields(body):
        return "paid_feature"
    if fields(body) & {"actions_spending_limit", "spending_limit", "use_static_ip"}:
        return "paid_feature"
    if MERGE.fullmatch(path) or (method == "POST" and REVIEW.match(path)):
        return "approval_or_merge"
    if method == "GET":
        return ""
    return write_refusal(method, path, body) or deployment_refusal(method, path, body)


def permission_for(path: str) -> str:
    for pattern, permission in FAMILIES:
        if re.fullmatch(pattern, path):
            return permission
    return ""


def check(  # noqa: PLR0911  # Ordered default-deny decisions.
    method: str, path: str, body: object = None
) -> Decision:
    """Return a stable decision before any credentials or network are used."""
    canonical = normalize(path)
    if not canonical:
        return Decision(allowed=False, code="ambiguous_path", path="")
    if method not in METHODS:
        return Decision(allowed=False, code="invalid_method", path=canonical)
    if body is not None and not isinstance(body, dict):
        return Decision(allowed=False, code="invalid_body", path=canonical)
    owner = OWNED.match(canonical)
    if owner and owner[1] != OWNER:
        return Decision(allowed=False, code="foreign_owner", path=canonical)
    payload: dict[str, object] = body or {}
    code = explicit_refusal(method, canonical, payload)
    if code:
        return Decision(allowed=False, code=code, path=canonical)
    permission = permission_for(canonical)
    if not permission:
        return Decision(allowed=False, code="endpoint_denied", path=canonical)
    if (
        method != "GET"
        and re.fullmatch(REPO, canonical)
        and not payload.keys() <= SETTINGS
    ):
        return Decision(allowed=False, code="unknown_setting", path=canonical)
    if (
        method != "GET"
        and re.fullmatch(ORG, canonical)
        and not payload.keys()
        <= {"name", "description", "company", "blog", "email", "location"}
    ):
        return Decision(allowed=False, code="unknown_setting", path=canonical)
    return Decision(allowed=True, code="allowed", path=canonical, permission=permission)
