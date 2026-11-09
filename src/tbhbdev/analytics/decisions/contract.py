"""Catalog equality and preservation of published objects and column types."""

from tbhbdev.analytics.model import Catalog, Relation, Verdict


def preserves(old: Catalog, new: Catalog) -> Verdict:
    violations = [
        f"missing schema: {name}" for name in old.schemas if name not in new.schemas
    ]
    relations = {relation.name: relation for relation in new.relations}
    for relation in old.relations:
        candidate = relations.get(relation.name)
        if candidate is None:
            violations.append(f"missing relation: {relation.name}")
            continue
        violations.extend(_relation_violations(relation, candidate))
    violations.extend(
        f"missing function: {name}({arguments}) -> {returns}"
        for name, arguments, returns in old.functions
        if (name, arguments, returns) not in new.functions
    )
    return Verdict(violations=tuple(violations))


def matches(catalog: Catalog, contract: Catalog) -> Verdict:
    if catalog == contract:
        return Verdict()
    violations = (
        preserves(contract, catalog).violations
        + preserves(catalog, contract).violations
    )
    return Verdict(violations=(*violations, "catalog differs from contract"))


def _relation_violations(relation: Relation, candidate: Relation) -> list[str]:
    violations: list[str] = []
    if candidate.kind != relation.kind:
        violations.append(f"relation kind changed: {relation.name}")
    columns = dict(candidate.columns)
    for name, kind in relation.columns:
        if name not in columns:
            violations.append(f"missing column: {relation.name}.{name}")
        elif columns[name] != kind:
            violations.append(f"column type changed: {relation.name}.{name}")
    return violations
