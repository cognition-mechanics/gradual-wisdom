# Contributing

Contributions are welcome. This is an open research project — if you run it, find something unexpected, or want to extend it, open a PR.

## Branch naming

All work happens on branches, never directly on `master`.

| Type | Prefix | Example |
|---|---|---|
| New feature or capability | `feature/` | `feature/embedding-drift-metrics` |
| Bug fix | `bug/` | `bug/probe-keychain-auth` |

## Workflow

1. Fork the repository (external contributors) or create a branch (collaborators)
2. Name your branch following the convention above
3. Make your changes — keep commits focused and descriptive
4. Open a pull request against `master`
5. A maintainer will review and merge

Direct pushes to `master` are blocked. All changes require a pull request with at least one approving review.

## What's welcome

- New probe categories or individual probes — extend `prompts/probes.json` (see versioning note below)
- Consolidation signal improvements — `prompts/consolidate.md` and `prompts/merge.md`
- Alternative runtimes beyond Anthropic API and Claude CLI
- `metrics.py` extensions, especially embedding-based semantic drift and opinion stability scoring
- Scheduler improvements — macOS launchd plist, Linux systemd service, Windows Task Scheduler
- Documentation fixes and clarity improvements

## Open an issue first for

- Changes to `wisdom.json` schema (requires a migration plan)
- Changes to the probe set version (breaks longitudinal comparability — existing data must remain interpretable)
- New categories in the knowledge base

## Code style

- Python 3.10+
- No external dependencies beyond `anthropic` (keep the install surface small)
- Follow the patterns already in `scripts/` — no framework, no magic
- Type hints encouraged, not required

## Privacy rule

`data/` is gitignored for a reason. Never commit wisdom files, session transcripts, probe results, or anything derived from real usage. If you add test fixtures, use obviously synthetic data.

## Reporting bugs

Use the bug issue template. Include your OS, Python version, Claude Code version, and the relevant excerpt from `data/sleep.log` or `data/probe.log`.
