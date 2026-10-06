"""
Merge labels from the edited dataset.csv back into dataset.jsonl.

    python tests/router_eval/import_csv.py [path/to/edited.csv]

Reads columns id, expected_action, expected_args, notes (and language if you corrected it).
Rows with a blank expected_action are left unlabelled. Unknown actions abort the import.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.router_eval.common import ACTIONS, CSV_PATH, load_dataset, parse_expected_args, save_dataset  # noqa: E402


def merge(rows: list[dict], csv_rows: list[dict]) -> tuple[int, list[str]]:
    """Apply CSV labels onto dataset rows in place; return (labelled_count, error messages)."""
    by_id = {r["id"]: r for r in rows}
    errors, labelled = [], 0
    for cr in csv_rows:
        row = by_id.get(cr["id"].strip())
        if row is None:
            errors.append(f"{cr['id']}: id not in dataset.jsonl")
            continue
        action = (cr.get("expected_action") or "").strip().lower()
        if not action:
            continue
        if action not in ACTIONS:
            errors.append(f"{cr['id']}: unknown action '{action}' (allowed: {', '.join(ACTIONS)})")
            continue
        try:
            row["expected_args"] = parse_expected_args(cr.get("expected_args", ""), action)
        except ValueError as exc:
            errors.append(f"{cr['id']}: bad expected_args ({exc})")
            continue
        row["expected_action"] = action
        row["notes"] = (cr.get("notes") or "").strip()
        if (cr.get("language") or "").strip():
            row["language"] = cr["language"].strip()
        labelled += 1
    return labelled, errors


def main() -> None:
    """CLI entry: merge the CSV, report counts, write dataset.jsonl only if there were no errors."""
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else CSV_PATH
    with path.open(newline="", encoding="utf-8-sig") as f:
        csv_rows = list(csv.DictReader(f))
    rows = load_dataset()
    labelled, errors = merge(rows, csv_rows)
    if errors:
        print("\n".join(errors))
        sys.exit("Import aborted — fix the rows above and retry (dataset.jsonl unchanged).")
    save_dataset(rows)
    print(f"labelled {labelled}/{len(rows)} rows ({len(rows) - labelled} still blank)")


if __name__ == "__main__":
    main()
