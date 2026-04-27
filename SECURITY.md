# Security Policy

## Reporting a Vulnerability

Please do not open a public issue for security vulnerabilities.

Email security reports to security@band.ai with:

- A short description of the issue
- Steps to reproduce
- Affected versions or commits, if known
- Any relevant logs, screenshots, or proof of concept

We will acknowledge receipt as quickly as possible and coordinate disclosure once a fix is available.

## Scope

Security-sensitive areas include:

- Credential handling for Band.ai, Claude, Codex, OpenAI, Anthropic, and GitHub
- Docker environment and volume handling
- Git checkout, branch, merge, and PR automation
- Agent prompt or tool behavior that could expose secrets or execute unintended commands

Please redact secrets from all reports.
