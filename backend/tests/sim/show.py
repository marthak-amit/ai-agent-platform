"""Print simulated transcripts from tests/sim/out (usage: python -m tests.sim.show <text in code/detail> [n])."""
import json
import sys
from pathlib import Path

OUT = Path(__file__).parent / "out"


def main() -> None:
    """Show up to n transcripts of conversations that have a finding whose code/detail contains the query."""
    query = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    report = json.loads((OUT / "report.json").read_text())
    transcripts = json.loads((OUT / "transcripts.json").read_text())
    seen = set()
    for group in report["groups"]:
        if query not in group["code"] + " " + group["detail"]:
            continue
        for example in group["examples"]:
            phone = example.get("phone")
            if phone in seen or phone not in transcripts:
                continue
            seen.add(phone)
            print(f"=== {phone} {example['flow']} lang={example['lang']} shop={example['shop']} [{group['code']}] {group['detail'][:120]}")
            for turn in transcripts[phone]:
                print(f"  C[{turn['lang']}]: {turn['c']}")
                for kind, text, buttons, rows in turn["b"]:
                    extra = (f" btn={buttons}" if buttons else "") + (f" rows={[r[:2] for r in rows]}" if rows else "")
                    print(f"      B({kind}): {text[:320]!r}{extra}")
            if len(seen) >= limit:
                return


if __name__ == "__main__":
    main()
