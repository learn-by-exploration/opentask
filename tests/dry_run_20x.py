#!/usr/bin/env python3
"""
20-round stress test for TaskPilot.

Runs the full test suite 20 times via pytest, collecting pass/fail counts
per round. Reports aggregate results and any intermittent failures.

Usage:
    python tests/dry_run_20x.py [--rounds N]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# Force test env
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dry-run-token")
os.environ.setdefault("ALLOWED_USER_IDS", "12345")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TEST_FILES = sorted(
    str(p.relative_to(PROJECT_ROOT))
    for p in (PROJECT_ROOT / "tests").glob("test_*.py")
)

# Files known to be slow or to need extra timeout
SLOW_FILES = {
    "tests/test_full_coverage.py",
    "tests/test_coverage_gaps.py",
    "tests/test_openclaw_api.py",
    "tests/test_qa_security.py",
    "tests/test_recipes.py",
    "tests/test_runner_extra.py",
    "tests/test_security_session21.py",
}
DEFAULT_TIMEOUT = 120
SLOW_TIMEOUT = 240
SLOW_TIMEOUT = 120


def run_one_file(filepath: str, timeout: int) -> dict:
    """Run pytest on a single file, return {passed, failed, errors, time}."""
    cmd = [
        sys.executable, "-m", "pytest", filepath, "-q", "--tb=line", "--no-header",
    ]
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )
        elapsed = time.monotonic() - start
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return {
            "passed": 0, "failed": 0, "errors": 0,
            "timeout": True, "time": elapsed, "output": f"TIMEOUT after {timeout}s",
        }

    # Parse pytest summary line: "N passed, M failed, K errors in Xs"
    passed = failed = errors = 0
    for line in output.strip().splitlines():
        line = line.strip()
        if "passed" in line or "failed" in line or "error" in line:
            import re
            m_pass = re.search(r"(\d+) passed", line)
            m_fail = re.search(r"(\d+) failed", line)
            m_err = re.search(r"(\d+) error", line)
            if m_pass:
                passed = int(m_pass.group(1))
            if m_fail:
                failed = int(m_fail.group(1))
            if m_err:
                errors = int(m_err.group(1))

    # Extract failure details
    fail_details = []
    if failed > 0 or errors > 0:
        for line in output.splitlines():
            if line.startswith("FAILED") or "ERROR" in line:
                fail_details.append(line.strip())

    return {
        "passed": passed, "failed": failed, "errors": errors,
        "timeout": False, "time": elapsed,
        "output": "\n".join(fail_details) if fail_details else "",
    }


def run_round(round_num: int, total: int) -> dict:
    """Run all test files once, return aggregate stats."""
    print(f"\n{'='*60}")
    print(f"  ROUND {round_num}/{total}")
    print(f"{'='*60}")

    round_passed = 0
    round_failed = 0
    round_errors = 0
    round_timeouts = 0
    failures = []

    for filepath in TEST_FILES:
        timeout = SLOW_TIMEOUT if filepath in SLOW_FILES else DEFAULT_TIMEOUT
        result = run_one_file(filepath, timeout)

        symbol = "✓" if result["failed"] == 0 and result["errors"] == 0 and not result["timeout"] else "✗"
        suffix = ""
        if result["timeout"]:
            suffix = " [TIMEOUT]"
            round_timeouts += 1
        elif result["failed"] > 0 or result["errors"] > 0:
            suffix = f" [{result['failed']}F/{result['errors']}E]"

        print(f"  {symbol} {filepath:<45s} {result['passed']:>4d} passed  {result['time']:5.1f}s{suffix}")

        round_passed += result["passed"]
        round_failed += result["failed"]
        round_errors += result["errors"]

        if result["failed"] > 0 or result["errors"] > 0 or result["timeout"]:
            failures.append({
                "file": filepath,
                "failed": result["failed"],
                "errors": result["errors"],
                "timeout": result["timeout"],
                "details": result["output"],
            })

    return {
        "round": round_num,
        "passed": round_passed,
        "failed": round_failed,
        "errors": round_errors,
        "timeouts": round_timeouts,
        "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description="20-round TaskPilot stress test")
    parser.add_argument("--rounds", type=int, default=20, help="Number of rounds (default: 20)")
    args = parser.parse_args()

    num_rounds = args.rounds
    print(f"TaskPilot 20x Stress Test")
    print(f"Rounds: {num_rounds}")
    print(f"Test files: {len(TEST_FILES)}")
    print(f"Files: {', '.join(Path(f).stem for f in TEST_FILES)}")

    all_results = []
    all_failures: dict[str, int] = defaultdict(int)
    total_start = time.monotonic()

    for i in range(1, num_rounds + 1):
        result = run_round(i, num_rounds)
        all_results.append(result)

        for fail in result["failures"]:
            key = fail["file"]
            if fail["timeout"]:
                key += " [TIMEOUT]"
            all_failures[key] += 1

        # Progress summary after each round
        total_p = sum(r["passed"] for r in all_results)
        total_f = sum(r["failed"] for r in all_results)
        total_e = sum(r["errors"] for r in all_results)
        elapsed = time.monotonic() - total_start
        print(f"\n  Round {i} summary: {result['passed']} passed, {result['failed']} failed, {result['errors']} errors")
        print(f"  Cumulative: {total_p} passed, {total_f} failed, {total_e} errors ({elapsed:.0f}s elapsed)")

    # Final report
    total_elapsed = time.monotonic() - total_start
    total_passed = sum(r["passed"] for r in all_results)
    total_failed = sum(r["failed"] for r in all_results)
    total_errors = sum(r["errors"] for r in all_results)
    total_timeouts = sum(r["timeouts"] for r in all_results)

    print(f"\n{'='*60}")
    print(f"  FINAL REPORT — {num_rounds} ROUNDS")
    print(f"{'='*60}")
    print(f"  Total tests executed: {total_passed + total_failed + total_errors}")
    print(f"  Total passed:         {total_passed}")
    print(f"  Total failed:         {total_failed}")
    print(f"  Total errors:         {total_errors}")
    print(f"  Total timeouts:       {total_timeouts}")
    print(f"  Total time:           {total_elapsed:.1f}s ({total_elapsed/60:.1f}m)")
    print(f"  Avg per round:        {total_elapsed/num_rounds:.1f}s")

    if all_failures:
        print(f"\n  Intermittent failures (file → count across rounds):")
        for key, count in sorted(all_failures.items(), key=lambda x: -x[1]):
            print(f"    {key}: {count}/{num_rounds} rounds")

        # Show unique failure details from last occurrence
        print(f"\n  Last failure details:")
        for result in reversed(all_results):
            for fail in result["failures"]:
                if fail["details"]:
                    print(f"    [{fail['file']}]")
                    for line in fail["details"].splitlines()[:5]:
                        print(f"      {line}")
    else:
        print(f"\n  ✓ ALL {total_passed} TESTS PASSED IN ALL {num_rounds} ROUNDS — NO FAILURES")

    # Exit code
    if total_failed > 0 or total_errors > 0:
        print(f"\n  EXIT: FAIL")
        sys.exit(1)
    else:
        print(f"\n  EXIT: PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
