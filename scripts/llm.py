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

# Substrings that mark a failure as transient (retry-worthy) rather than a
# genuinely broken/invalid model configuration.
_TRANSIENT_MARKERS = (
    "rate limit",
    "rate_limit",
    "overloaded",
    "529",
    "503",
    "usage limit",
    "too many requests",
    "timeout",
    "timed out",
)


def _is_transient(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(marker in s for marker in _TRANSIENT_MARKERS)


# Default model fallback chain, used when the configured model is rejected
# (e.g. a deprecated alias). Overridable via config.json "model_fallbacks".
DEFAULT_FALLBACKS = ["sonnet", "opus", "haiku"]


class LLMClient:
    def __init__(
        self,
        model: str,
        max_tokens: int,
        runtime: str = "auto",
        fallbacks: list[str] | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks if fallbacks is not None else DEFAULT_FALLBACKS

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
            self._preflight_cli()

    def _preflight_cli(self) -> None:
        """Validate the configured model with a cheap call; fall back loudly.

        A pinned-but-deprecated model alias was the single point of failure that
        silently killed this pipeline for ~60 days. This pre-flight ensures a
        model deprecation degrades to a working fallback (logged loudly) instead
        of crashing every run. Transient blips (rate limits) are retried before
        we conclude a model is actually dead.
        """
        candidates = [self.model] + [m for m in self.fallbacks if m != self.model]
        for candidate in candidates:
            ok, transient = self._probe_model(candidate)
            if ok:
                if candidate != self.model:
                    sys.stderr.write(
                        f"[llm] WARNING: configured model '{self.model}' is unusable; "
                        f"falling back to '{candidate}'. Update config.json.\n"
                    )
                    self.model = candidate
                return
            if transient:
                # Could not validate due to a transient error; don't burn the
                # whole fallback chain on a rate limit. Trust the configured
                # model and let the per-call retry loop handle it.
                sys.stderr.write(
                    f"[llm] pre-flight for '{candidate}' inconclusive (transient error); "
                    f"proceeding with configured model.\n"
                )
                return
        sys.stderr.write(
            "[llm] ERROR: no usable model found in pre-flight "
            f"(tried: {', '.join(candidates)}).\n"
        )
        sys.exit(2)

    def _probe_model(self, model: str) -> tuple[bool, bool]:
        """Return (ok, transient). ok=True if the model answered a tiny prompt.
        transient=True when failure looks like a rate limit / overload rather
        than a genuinely invalid model."""
        cmd = ["claude", "-p", "ok", "--model", model, "--max-turns", "1"]
        for attempt in range(2):
            if attempt:
                time.sleep(10)
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120, cwd=str(RUNTIME_CWD)
                )
            except subprocess.TimeoutExpired:
                continue  # treat as transient, retry once
            if result.returncode == 0:
                return True, False
            if _is_transient(result.stderr):
                return False, True
        return False, False

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
        # Exponential backoff. Bursts of large consolidation calls can trip
        # rate limits, which surface as rc=1 (often with empty stderr); short
        # backoff loses whole days, so we retry longer before giving up.
        backoffs = [0, 15, 45, 90, 180]
        last_err: Exception | None = None
        for attempt, wait in enumerate(backoffs):
            if wait:
                time.sleep(wait)
                sys.stderr.write(
                    f"[llm] retry {attempt}/{len(backoffs) - 1} after CLI failure "
                    f"(waited {wait}s)\n"
                )
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
                f"stderr: {result.stderr.strip()[:500] or '(empty)'}"
            )
        raise last_err  # type: ignore[misc]
