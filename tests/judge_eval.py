#!/usr/bin/env python3
"""TESTING.md Layer 2: a repeatability/consistency check for the judge
prompt, not a pass/fail test — the judge is non-deterministic, so this is
about spotting drift, not asserting a single "correct" answer.

Run by hand after any JUDGE_SYSTEM_PROMPT change:

    python tests/judge_eval.py

Runs every fixture in tests/fixtures/ through judge.call_judge() 3 times
each, and appends a dated block to tests/judge_eval_log.md with each run's
score and interruption count, so you can see whether a prompt change made
consistency better or worse over time. Makes real LLM Gateway calls — has a
real cost and takes a while, which is why it's a standalone script, not a
pytest test.
"""

import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import judge  # noqa: E402
import lib  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures"
LOG_FILE = ROOT / "tests" / "judge_eval_log.md"
RUNS_PER_FIXTURE = 3
PAUSE_BETWEEN_CALLS = 2.0  # seconds — the gateway rate-limits bursts
# Rough heuristic, not a hard spec: a bigger swing than this between runs on
# the same fixture is worth reading the log for, not proof of a real bug.
INCONSISTENT_SPREAD = 20


def run_fixture(path: Path) -> list[dict]:
    timeline = json.loads(path.read_text(encoding="utf-8"))
    turns = judge.parse_timeline(timeline)
    results = []
    for i in range(RUNS_PER_FIXTURE):
        try:
            result = judge.call_judge(turns)
            results.append({
                "ok": True,
                "overall_score": result["overall_score"],
                "interruptions": len(result["interruptions"]),
            })
        except Exception as err:  # keep going across fixtures even if one run fails
            results.append({"ok": False, "error": str(err)})
        if i < RUNS_PER_FIXTURE - 1:
            time.sleep(PAUSE_BETWEEN_CALLS)
    return results


def summarize(results: list[dict]) -> str:
    failures = [r for r in results if not r["ok"]]
    if failures:
        return f"{len(failures)}/{len(results)} run(s) failed — see details below"
    scores = [r["overall_score"] for r in results]
    spread = max(scores) - min(scores)
    verdict = "consistent" if spread <= INCONSISTENT_SPREAD else f"INCONSISTENT (spread {spread})"
    return f"scores: {scores} — {verdict}"


def format_block(fixture_name: str, results: list[dict]) -> str:
    lines = [
        f"## {date.today().isoformat()} — {fixture_name}",
        "",
        f"Model: `{judge.JUDGE_MODEL}`",
        "",
        summarize(results),
        "",
    ]
    for i, r in enumerate(results, 1):
        if r["ok"]:
            lines.append(f"- run {i}: overall_score={r['overall_score']}, interruptions={r['interruptions']}")
        else:
            lines.append(f"- run {i}: FAILED — {r['error']}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    lib.load_env()
    fixtures = sorted(FIXTURES_DIR.glob("*.json"))
    if not fixtures:
        sys.exit(f"No fixtures found in {FIXTURES_DIR}")

    blocks = []
    for path in fixtures:
        print(f"Running {path.name} x{RUNS_PER_FIXTURE}...")
        results = run_fixture(path)
        block = format_block(path.name, results)
        print(block)
        blocks.append(block)

    existing = LOG_FILE.read_text(encoding="utf-8") if LOG_FILE.exists() else "# Judge Eval Log\n"
    LOG_FILE.write_text(existing.rstrip("\n") + "\n\n" + "\n".join(blocks), encoding="utf-8")
    print(f"Appended to {LOG_FILE}")


if __name__ == "__main__":
    main()
