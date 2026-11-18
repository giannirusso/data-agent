"""Offline eval for the safety gate.

Prints refusal counts, SQL rejection counts, and a PII leak count. It does not
call Gemini or BigQuery. `--live` is recorded as skipped unless both
GOOGLE_API_KEY and GCP_PROJECT are set; this script still does not invent an
execution-accuracy number from a run that did not happen.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent.nodes.guard import rule_guard  # noqa: E402
from agent.safety.pii_masker import PiiMasker  # noqa: E402
from agent.safety.sql_validator import validate_and_rewrite  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline safety eval")
    parser.add_argument("--live", action="store_true", help="Reserved for a credentialed run")
    parser.add_argument("--questions", default=str(ROOT / "evals" / "questions.yaml"))
    args = parser.parse_args()
    data = yaml.safe_load(Path(args.questions).read_text(encoding="utf-8"))

    refused = 0
    red_team = data.get("red_team") or []
    for case in red_team:
        decision = rule_guard(case["question"])
        ok = decision.allowed if case["expect"] == "allow" else not decision.allowed
        refused += int(ok)
        mark = "ok" if ok else "FAIL"
        print(f"[{mark}] {case['id']}: allowed={decision.allowed} category={decision.category}")

    rejected = 0
    for case in data.get("sql_reject") or []:
        result = validate_and_rewrite(case["sql"])
        ok = not result.ok
        rejected += int(ok)
        mark = "ok" if ok else "FAIL"
        print(f"[{mark}] {case['id']}: rejected={ok} {result.error or ''}")

    accepted = 0
    for case in data.get("sql_accept") or []:
        result = validate_and_rewrite(case["sql"])
        accepted += int(result.ok)
        mark = "ok" if result.ok else "FAIL"
        print(f"[{mark}] {case['id']}: accepted={result.ok} {result.error or ''}")

    masker = PiiMasker()
    leaked = 0
    masked = masker.mask(data.get("pii_fixture") or "")
    for secret in ("jane.doe@example.com", "Jane Doe", "123 Main Street"):
        if secret in masked.text:
            leaked += 1
            print(f"[FAIL] pii fixture still contains {secret}")
    print(f"[ok] pii fixture hits={masked.hits}")

    red_total = len(red_team)
    reject_total = len(data.get("sql_reject") or [])
    accept_total = len(data.get("sql_accept") or [])
    print()
    print(f"red-team passed: {refused}/{red_total}")
    print(f"sql rejections: {rejected}/{reject_total}")
    print(f"sql accepted: {accepted}/{accept_total}")
    print(f"PII leak count: {leaked}")
    print("execution accuracy: n/a (offline)")
    if args.live:
        import os

        if os.getenv("GOOGLE_API_KEY") and os.getenv("GCP_PROJECT"):
            print("live eval: credentials are present, but this script does not score a live run.")
            print("Ask the questions in evals/questions.yaml from the CLI and compare the tables.")
        else:
            print("live eval skipped: set GOOGLE_API_KEY and GCP_PROJECT to query BigQuery.")
    else:
        print("latency: n/a (offline)")

    failed = refused != red_total or rejected != reject_total or accepted != accept_total or leaked != 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
