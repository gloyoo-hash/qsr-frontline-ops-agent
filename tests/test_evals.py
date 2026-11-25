"""Integration test executing the Golden Dataset Evaluation Suite."""

from __future__ import annotations

from evals.run_evals import run_golden_evals


def test_golden_evaluation_suite_passes_100_percent() -> None:
    report = run_golden_evals()
    assert report["total"] >= 11
    failed = [r for r in report["results"] if not r["passed"]]
    assert not failed, f"Golden eval failures: {failed}"
    assert report["pass_rate_pct"] == 100.0
