"""Select a preceding release from inspected and freshly fetched git facts."""

from dataclasses import dataclass

from tbhbdev.analytics.model import Verdict


@dataclass(frozen=True)
class Facts:
    shallow: bool | None = False
    fetched: bool = True
    main_has_contract: bool | None = False
    staged: bool = False
    unstaged: bool = False
    head_tag: str | None = None
    parent_tag: str | None = None
    baseline_has_contract: bool = False


def choose(facts: Facts) -> Verdict:
    if facts.shallow is None:
        failure = "could not inspect history"
    elif facts.shallow:
        failure = "complete history is required"
    elif not facts.fetched:
        failure = "could not fetch release tags and main history"
    elif facts.main_has_contract is None:
        failure = "could not inspect main history"
    else:
        return _release(facts)
    return _failure(failure)


def _failure(message: str) -> Verdict:
    return Verdict(violations=(f"analytics baseline: {message}",))


def _release(facts: Facts) -> Verdict:
    tag = facts.head_tag if facts.staged or facts.unstaged else facts.parent_tag
    if tag is None:
        if facts.main_has_contract:
            return _failure("analytics already shipped but no release tag is reachable")
        return Verdict(message="first release: no earlier analytics tree")
    if facts.main_has_contract and not facts.baseline_has_contract:
        return _failure(
            "analytics already shipped but the baseline has no analytics tree"
        )
    return Verdict(value=tag, message=f"baseline: {tag}")
