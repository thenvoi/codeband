# Security Practices

Baseline security expectations for any code a Codeband agent writes or reviews.
Language-agnostic — apply the principle using your stack's safe primitives. The Code
Reviewer treats violations here as blocking, but only when there is a concrete,
demonstrable path to exploitation — not a theoretical one.

## Trust nothing from outside the process

Anything that crosses a boundary into your code is untrusted until validated: network
requests, user input, file contents, environment variables, message-queue payloads,
and responses from external services. Validate shape and bounds at the boundary;
reject what doesn't conform rather than coercing it.

## Injection: never build a command/query by string concatenation

- **SQL:** use parameterized queries / prepared statements / the ORM's safe binding.
  Never interpolate user input into a query string.
- **Shell/OS commands:** avoid shelling out with interpolated input. If you must,
  pass arguments as a list to the exec API (no shell), never a concatenated string
  through a shell.
- **NoSQL / LDAP / template engines:** the same rule — use the parameterized/escaped
  API, not string building.
- **HTML/JS output:** escape/encode on output to prevent XSS; use the framework's
  auto-escaping rather than hand-built markup with raw input.

## No hardcoded secrets

- No API keys, passwords, tokens, private keys, or connection strings in source code,
  tests, fixtures, or committed config.
- Read secrets from environment variables or the project's secret manager.
- Don't log secrets, tokens, full auth headers, or PII. Redact before logging.
- If you encounter an existing hardcoded secret, flag it — don't copy the pattern.

## Authentication and authorization

- **Authentication** (who you are) and **authorization** (what you may do) are
  separate checks — doing one doesn't give you the other.
- Check authorization on **every** protected operation, server-side, on the actual
  resource being accessed — not just at the UI or route layer. Don't trust an ID from
  the client to be one the caller is allowed to touch (broken object-level
  authorization is the most common real-world hole).
- Fail closed: if you can't determine permission, deny.

## Path and request safety

- **Path traversal:** never build filesystem paths from untrusted input without
  normalizing and confining to an allowed base directory. Reject `..` segments.
- **SSRF:** don't fetch URLs supplied by users without validating the destination
  against an allowlist; internal metadata endpoints and private ranges are the
  classic targets.
- **Open redirects:** validate redirect targets against an allowlist.

## Sensitive data handling

- Transmit secrets and PII over TLS only.
- Hash passwords with a slow, salted algorithm (bcrypt/scrypt/argon2) — never plain,
  never fast unsalted hashes.
- Store only what you need; minimize the blast radius of a breach.
- Be careful what ends up in error messages returned to clients — don't leak stack
  traces, internal paths, or query details to the outside.

## Dependency security

- Prefer well-maintained, widely-used libraries over obscure ones for security-
  sensitive work (crypto, auth, parsing).
- Don't roll your own crypto. Use vetted library primitives.
- When adding a dependency, prefer the official/most-common package and pin it the way
  the repo pins its others.

## Reviewer note: demonstrate the exploit

When flagging a security finding, show the concrete path: which untrusted input
reaches which sink, and what an attacker achieves. "This could theoretically be
unsafe" without a demonstrable path is a suggestion, not a blocker.
