# Security Policy

## Scope

gradual-wisdom is a local tool. It runs on your machine, reads your Claude Code session transcripts from `~/.claude/projects/`, and calls the Anthropic API. It does not expose any network service, handle authentication, or store data outside your local filesystem.

## Sensitive data handling

- **API keys** — `ANTHROPIC_API_KEY` is read from environment. Never hardcode it. The cron entries in `install.sh` show the correct pattern.
- **Session transcripts** — processed locally by `sleep.py`. Not transmitted anywhere except to the Anthropic API for consolidation.
- **`data/wisdom.json`** — lives only on your machine. The `.gitignore` excludes the entire `data/` directory. Never commit it.

## Reporting a vulnerability

If you find a security issue — for example, a path that could cause the tool to leak data or a script injection vector — please report it privately:

**Email:** ali.pirouzi@symanto.com

Do not open a public issue for security vulnerabilities. Include a description of the issue, steps to reproduce, and your assessment of impact. You'll receive a response within 5 business days.
