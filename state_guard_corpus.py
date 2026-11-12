"""Count state-guard outcomes without disclosing private corpus sentences."""

import argparse
import json
from collections import Counter
from pathlib import Path

from tbhbdev.alpha import state_guard


def main() -> None:
    """Measure a labeled corpus; print counts and optionally row identifiers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--ids", action="store_true")
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in (args.corpus / "window.jsonl").read_text().splitlines()
    ]
    labels = {
        row["id"]: row
        for row in (
            json.loads(line)
            for line in (args.corpus / "labels.jsonl").read_text().splitlines()
        )
    }
    anchors = json.loads((args.corpus / "anchors.json").read_text())
    blocked = {
        row["id"]: bool(state_guard.unsupported_claims(row["off"])) for row in rows
    }
    report_counts(labels, blocked)
    a_anchors = [anchor for anchor in anchors if anchor["verdict"] == "A"]
    kept = sum(all(blocked[row_id] for row_id in anchor["ids"]) for anchor in a_anchors)
    print(f"A anchors: {kept}/{len(a_anchors)}")  # noqa: T201
    if args.ids:
        report_ids(labels, blocked)


def report_counts(labels: dict[int, dict[str, str]], blocked: dict[int, bool]) -> None:
    """Print blocked and total counts for every verdict and shape."""
    for field in ("verdict", "shape"):
        total = Counter(label[field] for label in labels.values())
        counts = Counter(
            labels[row_id][field] for row_id, bad in blocked.items() if bad
        )
        for name in sorted(total):
            print(f"{field} {name}: {counts[name]}/{total[name]}")  # noqa: T201


def report_ids(labels: dict[int, dict[str, str]], blocked: dict[int, bool]) -> None:
    """Print identifiers of missed claims and remaining false positives."""
    for verdict in ("A", "B", "C"):
        ids = sorted(
            row_id
            for row_id, bad in blocked.items()
            if labels[row_id]["verdict"] == verdict and bad != (verdict == "A")
        )
        print(f"{verdict} {'pass' if verdict == 'A' else 'block'} ids: {ids}")  # noqa: T201


if __name__ == "__main__":
    main()
