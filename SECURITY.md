# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.x     | ✅        |
| 1.x     | ❌        |
| < 1.0   | ❌        |

Only the latest minor release on the 2.x line receives security updates. Fixes may be back-ported to the final 1.x release at the maintainer's discretion if the issue is severe and the gap is small.

## Reporting a Vulnerability

Please report security issues **privately** via **GitHub Security Advisories**: repository **Security** tab → **Report a vulnerability**. This creates a private discussion between you and the maintainer and gives both sides a clean audit trail.

**Do not** open a public GitHub issue, pull request, or discussion thread for an unpatched security report.

When reporting, please include:

- A description of the issue and the affected version(s).
- Steps to reproduce, or a minimal proof-of-concept.
- Your assessment of the impact and any suggested mitigation.

### Expected response

- Initial acknowledgment within **7 days**.
- Fix or mitigation plan within **30 days** for HIGH severity (remote SSRF bypass, sandbox escape, privilege escalation). Longer for MEDIUM/LOW.
- Public disclosure coordinated with the reporter once a fix is released. CVE filing is at the maintainer's discretion based on severity and impact.

## Deployment Model

pdf-mcp supports two deployment shapes, and the threat model below is scoped to both.

1. **Stdio, single user.** The typical install: an MCP stdio server spawned by an MCP client (Claude Desktop, Claude Code, VS Code) on the user's own machine, one process per conversation. Isolation comes from the operating system's user boundary.
2. **HTTP, single tenant.** One `pdf-mcp-http` process per tenant, with its own cache volume, published to loopback with TLS terminated by a reverse proxy in front of it, and both startup guards armed (an auth token and a `[paths]` allow list are required, or the process refuses to start). See [docs/remote-access.md](docs/remote-access.md) for the trust boundary and the operator runbook.

The following remain **unsupported** configurations: multi-tenant deployments, one token shared among several people, shared-host installs where another local user can read the cache, and binding the HTTP transport directly to a public interface with no proxy in front of it.

## Threat Model

pdf-mcp routinely processes attacker-controllable input — PDF byte streams (local or fetched via URL), PDF metadata, OCR output, and embedded section text. Even in single-user deployments, the attacker controls the PDF content the user opens, so the following are **in scope** for security reports:

- **SSRF via URL fetch.** A prompt-injected PDF can instruct the agent to fetch attacker-chosen URLs (`http://169.254.169.254/...`, DNS-rebinding hosts, IPv6 link-local, etc.). In scope: local-network access (RFC 1918, link-local, loopback, IPv6 ULA, AWS IMDS), DNS rebinding (TOCTOU between resolution and connect), content smuggling via `Content-Type` misrepresentation.
- **Prompt injection via PDF-derived content.** Extracted text, OCR output, metadata fields, table contents, and section titles are all attacker-controllable. Each content-returning MCP tool's `description` restates the untrusted-content contract for the consuming LLM, but final responsibility for honoring it lies with the agent runtime.
- **Resource exhaustion.** Multi-thousand-page documents, pathologically large titles, or oversized URL responses that bypass the configured caps.
- **Path traversal or symlink escape** through path-resolution logic.
- **Auth or path-boundary escape in single-tenant HTTP deployments.** Bypassing the bearer-token check on the MCP endpoint, escaping the `[paths]` allow list, or `/health` disclosing more than the status and version string are all in scope. Deployments matching shape 2 above are supported and hardened; multi-tenant ones are not (see below).

**Out of scope:**

- Vulnerabilities in third-party dependencies (PyMuPDF, FastMCP, fastembed, httpx, etc.) — please report those to their upstream maintainers. We will pick up their fixes via dependency bumps.
- Resource exhaustion from PDFs the user explicitly requested at full size (e.g. legitimately reading a 3000-page document via `pdf_read_pages`). The configured `[limits].max_response_bytes` is the knob; complaints about legitimate large-document use go to the maintainer as feature requests, not security.
- Prompt-injection attacks that succeed despite the LLM agent ignoring the documented untrusted-content contract. The contract is restated in every tool description; downstream non-compliance is a client-side issue.
- Vulnerabilities requiring an attacker to already have write access to the user's local filesystem (e.g. malicious symlinks pre-planted in the cache directory).
- **Multi-user info leak via cache permissions.** Single-user deployment is the supported configuration; the cache directory is `chmod 0o700` as defense-in-depth, but reports framed around "another local user on a shared box can read my cache" are out of scope because shared-host deployment is unsupported.
- Vulnerabilities in **multi-tenant** deployments of pdf-mcp, including one token shared among several people. Such deployments are unsupported: the file-system access patterns, cache invalidation logic, and per-process global state assume one trusted caller, and a single process has no per-user partitioning of the cache, the warmed corpus, or the `[paths]` allow list.

## Current Hardening Posture

See `docs/tool-reference.md` § *Security & Hardening* for the runtime contracts users can rely on. The CHANGELOG `### Security` blocks document each release's specific changes. The most recent substantive hardening batch (v1.13.0) covers URL-fetcher hardening (early content-type rejection, expanded IPv6 deny list, IPv4-mapped unwrap, per-hop IP pinning to defeat DNS rebinding) and tool-description prompt-injection hardening. The cache-directory `chmod 0o700` shipped in the same release is defense-in-depth only — it does not address an in-scope threat under the single-user deployment model.
