"""
Pre-flight must treat timeouts as transient, not as an invalid model.

Run: cd <repo-root> && python3 -m pytest scripts/test_llm_preflight.py -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402


def test_probe_timeouts_are_transient(monkeypatch):
    def always_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120)

    monkeypatch.setattr(llm.subprocess, "run", always_timeout)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    client = object.__new__(llm.LLMClient)
    ok, transient = client._probe_model("sonnet")
    assert (ok, transient) == (False, True)


def test_probe_hard_failure_is_not_transient(monkeypatch):
    def invalid_model(*args, **kwargs):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="model not found")

    monkeypatch.setattr(llm.subprocess, "run", invalid_model)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    client = object.__new__(llm.LLMClient)
    assert client._probe_model("nope") == (False, False)
