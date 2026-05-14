# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅        |
| < 1.0   | ❌        |

Only the latest minor release on the 1.x line receives security updates. Older patches may be cherry-picked into a back-port at the maintainer's discretion if the issue is severe and the gap is small.

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

## Threat Model

pdf-mcp routinely processes attacker-controllable input — PDF byte streams (local or fetched via URL), PDF metadata, OCR output, and embedded section text. The following are **in scope** for security reports:

- **SSRF via URL fetch.** Includes local-network access (RFC 1918, link-local, loopback, IPv6 ULA, AWS IMDS), DNS rebinding (TOCTOU between resolution and connect), and content smuggling via `Content-Type` misrepresentation.
- **Prompt injection via PDF-derived content.** Extracted text, OCR output, metadata fields, table contents, and section titles are all attacker-controllable. Each content-returning MCP tool's `description` restates the untrusted-content contract for the consuming LLM, but final responsibility for honoring it lies with the agent runtime.
- **Resource exhaustion.** Multi-thousand-page documents, pathologically large titles, or oversized URL responses that bypass the configured caps.
- **Multi-user info leak.** Cache directory permissions, shared filesystem locations, or any path where a cached PDF could be read by another local user.
- **Path traversal or symlink escape** through path-resolution logic.

**Out of scope:**

- Vulnerabilities in third-party dependencies (PyMuPDF, FastMCP, fastembed, httpx, etc.) — please report those to their upstream maintainers. We will pick up their fixes via dependency bumps.
- Resource exhaustion from PDFs the user explicitly requested at full size (e.g. legitimately reading a 3000-page document via `pdf_read_pages`). The configured `[limits].max_response_bytes` is the knob; complaints about legitimate large-document use go to the maintainer as feature requests, not security.
- Prompt-injection attacks that succeed despite the LLM agent ignoring the documented untrusted-content contract. The contract is restated in every tool description; downstream non-compliance is a client-side issue.
- Vulnerabilities requiring an attacker to already have write access to the user's local filesystem (e.g. malicious symlinks pre-planted in the cache directory).

## Current Hardening Posture

See `docs/tool-reference.md` § *Security & Hardening* for the runtime contracts users can rely on. The CHANGELOG `### Security` blocks document each release's specific changes. The most recent batch of security work (v1.13.0) covers URL-fetcher hardening (early content-type rejection, expanded IPv6 deny list, IPv4-mapped unwrap, per-hop IP pinning to defeat DNS rebinding) plus tool-description prompt-injection hardening and cache-directory permission tightening.
