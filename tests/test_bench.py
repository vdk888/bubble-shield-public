"""Regression gate: the bench must keep 100% recall on the fixtures."""
from pathlib import Path

from bench.run_bench import run, DEFAULT_FIXTURES


def test_bench_passes_recall_gate():
    # run() returns 0 when overall recall >= TARGET_RECALL.
    assert run(DEFAULT_FIXTURES) == 0


def test_fixtures_file_present():
    assert Path(DEFAULT_FIXTURES).exists()
