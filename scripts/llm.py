#!/usr/bin/env python3
"""
llm.py — Runtime dispatcher for LLM calls.

Two backends:
  - api:  uses the anthropic Python SDK (requires ANTHROPIC_API_KEY)
  - cli:  shells out to `claude -p` (uses your existing Claude Code auth)

Selection order:
  1. config.json `runtime` field if explicitly set to "api" or "cli"
  2. Otherwise "auto": api if ANTHROPIC_API_KEY is set, else cli

The dream-cycle constraint (no input during sleep) is enforced two ways:
  - api: no tools attached to the request
  - cli: --max-turns 1 forces a single response, no tool calls possible
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_CWD = SKILL_ROOT / ".runtime-cwd"


class LLMClient:
    def __init__(self, model: str, max_tokens: int, runtime: str = "auto"):
        self.model = model
        self.max_tokens = max_tokens

        if runtime == "auto":
            runtime = "api" if os.environ.get("ANTHROPIC_API_KEY") else "cli"
        if runtime not in ("api", "cli"):
            sys.stderr.write(f"ERROR: unknown runtime '{runtime}' (expected api|cli|auto)\n")
            sys.exit(2)
        self.runtime = runtime

        if self.runtime == "api":
            try:
                from anthropic import Anthropic
            except ImportError:
                sys.stderr.write("ERROR: anthropic package not installed. Run: pip3 install anthropic\n")
                sys.exit(2)
            if not os.environ.get("ANTHROPIC_API_KEY"):
                sys.stderr.write("ERROR: runtime=api but ANTHROPIC_API_KEY is not set\n")
                sys.exit(2)
            self._client = Anthropic()
        else:
            if not shutil.which("claude"):
                sys.stderr.write(
                    "ERROR: runtime=cli but 'claude' is not in PATH. "
                    "Install Claude Code or set ANTHROPIC_API_KEY for api runtime.\n"
                )
                sys.exit(2)
            # Sentinel cwd isolates our internal claude -p calls into a known
            # project dir under ~/.claude/projects/, so sleep.py can filter
            # them out by directory name regardless of prompt content.
            RUNTIME_CWD.mkdir(parents=True, exist_ok=True)

    def call(self, system: str, user_msg: str) -> str:
        if self.runtime == "api":
            return self._call_api(system, user_msg)
        return self._call_cli(system, user_msg)

    # -- backends --------------------------------------------------------

    def _call_api(self, system: str, user_msg: str) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def _call_cli(self, system: str, user_msg: str) -> str:
        # Combine system + user into one prompt for portability across CLI flag versions.
        combined = (
            "=== SYSTEM INSTRUCTIONS ===\n"
            f"{system}\n\n"
            "=== INPUT ===\n"
            f"{user_msg}"
        )
        cmd = [
            "claude",
            "-p", combined,
            "--model", self.model,
            "--max-turns", "1",
        ]
        last_err: Exception | None = None
        for attempt in range(3):
            if attempt:
                time.sleep(10 * attempt)
                sys.stderr.write(f"[llm] retry {attempt}/2 after CLI failure\n")
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=900,
                    cwd=str(RUNTIME_CWD),
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError("claude CLI timed out after 15 minutes")
            if result.returncode == 0:
                return result.stdout.strip()
            last_err = RuntimeError(
                f"claude CLI failed (rc={result.returncode}):\n"
                f"stderr: {result.stderr.strip()[:500]}"
            )
        raise last_err  # type: ignore[misc]
