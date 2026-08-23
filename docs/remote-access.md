# Remote access over HTTP

`pdf-mcp-http` authenticates with a single shared bearer token, `PDF_MCP_AUTH_TOKEN`, verified by FastMCP's `StaticTokenVerifier`. One token, one tenant, no per-user identity. That is the whole model.

This document covers the trust boundary: what the token protects, what it costs you, and how it fails. For setting the transport up, configuring a client, and rotating the token, see [HTTP transport setup](configuration.md#http-transport-setup).

![Request path and trust boundary for pdf-mcp over HTTP: an MCP client sends a bearer token over HTTPS, a proxy terminates TLS and forwards over loopback to the container, and one tenant boundary encloses the shared cache, warmed corpus, and paths allow list](images/remote-access-architecture.svg)

## What it is

`pdf-mcp-http` runs the same tools as the stdio entry point, served over HTTP at `/mcp` by default. Every request to that endpoint carries an `Authorization: Bearer` header, and the token in it is compared against the one value in `PDF_MCP_AUTH_TOKEN`. Anything else is rejected. There are no sessions and no scopes.

One route is deliberately outside auth: `GET /health`, which returns `status` and `version` and nothing else, so an uptime probe can confirm liveness without holding a credential. It is written for a probe on the same box; publishing it through a proxy has a cost, covered under Configuration.

## When to use it, and when not

Use it when one person, or one agent acting for one person, needs pdf-mcp over the network: a home server you reach from a laptop, a VPS you own, a container behind your own proxy. The transport exists because process-per-conversation stdio cannot span machines.

Three situations make it worth the operational cost, and in none of them does an agent hand the server a file.

| situation | what HTTP gives you | on stdio |
|---|---|---|
| **The client cannot spawn a subprocess.** The Anthropic API MCP connector, claude.ai custom connectors, web and mobile hosts. | The only way these clients can reach pdf-mcp. The URL path below suits them well, since a linked paper or filing needs no upload at all. | Not possible: the client has to launch and hold a local process. |
| **A warm corpus is shared by several clients.** A laptop, a phone, and a scheduled agent querying one collection. | One long-lived process, so all of them hit the same warmed SQLite. | Each spawned server has its own cache, so the minutes of extraction and embedding are paid again per client. |
| **You would rather not install the dependencies.** | The Docker image bakes in Tesseract, the embedding model, and the column-aware extractor, so every tool works on the first request. | Tesseract and the embedding model have to be installed on the machine running the server. |

It is not for ad hoc work on documents that live on your own machine. That is stdio's job, it is the default, and it stays simpler for that case: you configure no token, run no proxy, and copy nothing to a server first.

Do not use it to serve a team from one endpoint. Every caller of a process shares one SQLite cache, one warmed corpus, and one global `[paths]` allow list, and nothing in a request identifies the caller, so nothing can be scoped to them. A second person on the same endpoint is not a second tenant; they are the same tenant with a second keyboard.

## Getting documents to the server

Over HTTP every path argument resolves on the server, not on the machine the agent is running on. Two things make a PDF readable.

The first is a file under an allow-listed root. Put it there with whatever you already use: `cp` into the Compose stack's `./documents` folder, which the container sees as `/data/pdfs`, or `rsync`, `scp`, a sync client, or a scheduled job. Use this for a corpus you curate and keep, since the corpus tools take a directory, reject URLs, and benefit from staying warm between sessions.

The second is an `https://` URL the server fetches. Pass the URL as the `path` argument and the server downloads and caches it. This is the right answer for an application that receives documents from its users: put the bytes in object storage and pass a presigned link. Presigned S3 and GCS URLs work even though they usually serve `application/octet-stream`, because the fetcher validates the payload by its `%PDF` magic bytes rather than its content type. Only `https://` is accepted, and SSRF protection rejects loopback, private, link-local, and IMDS addresses, so a link to the agent's own machine or your LAN will not work. Set `[urls].allow` to bound what else it can reach.

What an agent cannot do is hand a remote server a file that exists only on the caller's machine. MCP has no client-to-server file transfer, and a tool that takes a path is not an exception, whichever server offers it: the server process opens the path, so the bytes never travel from the caller. Passing the file inline as base64 is not a way around that, for reasons set out in [`investigated-rejected.md`](investigated-rejected.md). [SEP-2631](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2631) would change this by negotiating an out-of-band HTTPS transfer, and even then the client application performs the upload, not the model.

A connecting agent does not have to be told a path. `server_info` returns a `documents` block naming the roots this server will open:

```json
"documents": {
  "access_mode": "allowlist",
  "roots": ["/data/pdfs"],
  "allow_patterns": ["/data/pdfs/**"],
  "deny_patterns": []
}
```

A value in `roots` can be passed straight to `pdf_corpus_overview` or `pdf_corpus_warm`. When `access_mode` is `unrestricted` (no `[paths]` allow list, which a remote deployment refuses to start without) `roots` is empty, and that means "any path the process can read", not "no documents".

## Threat model versus stdio

| | stdio | HTTP |
|---|---|---|
| **What the boundary is** | The operating system's user boundary. If you can spawn the process, you were already that user. | A bearer token in a header, only as strong as the client config file that holds it. |
| **How long state lives** | `PDFCache._init_db` ends by calling `clear_expired()`, so every conversation that spawns the server sweeps the TTL. | The process is long-lived, so a warmed corpus and its cache outlive any single session. The sweep runs at startup, and the process may then run for weeks without another. |
| **What bounds local reads** | The user's own filesystem permissions. | The `[paths]` allow list, and nothing else. That is why its absence is a startup failure rather than a warning. |
| **What the caller can fetch** | Same URL-fetching branch, but reachable only by someone who is already that user. | The token buys outbound HTTPS fetching too. `_resolve_path` treats an `https://` value as a download, governed by `[urls]`, which is empty (unrestricted) by default. |

SSRF hardening still applies to that outbound fetch. What is left is ordinary public-internet egress originating from your server, and disk filling with whatever the caller fetched. Set `[urls].allow` if either matters to you. For the full URL-fetching surface see [`tool-reference.md`](tool-reference.md#url-fetching-ssrf), and for the in-scope and out-of-scope lists that govern security reports see [`SECURITY.md`](../SECURITY.md).

[Rotating the token](configuration.md#rotating-the-token) stops future requests and does nothing about past ones. The cache retains whatever was extracted while the old token was valid. Treat a leak as disclosure of every PDF reachable under `[paths]`, and as outbound HTTPS fetching from your server for as long as the token was valid.

## The single-tenant contract

Single tenancy here is a contract, not a gap waiting to be filled. Issuing a token per user would change nothing about what those users can reach: pdf-mcp has no per-token path scoping, so per-user tokens would authenticate callers while authorizing them identically. That is worse than one honest shared token, because a list of named credentials implies an isolation boundary that does not exist, and operators act on what the credential model implies.

Isolation comes from separate processes with separate cache volumes, exactly as stdio gets it from process-per-conversation. Someone who needs two users runs two containers, each with its own token, cache volume, and allow list. The shipped `docker-compose.yml` is written for one instance: it hardcodes `container_name: pdf-mcp` and declares a single named volume, `pdf-mcp-cache`, and `COMPOSE_PROJECT_NAME` does not override `container_name`. A second instance means changing both, plus the published host port. The `pdf-mcp-cache` volume holds the whole cache directory, so the SQLite `-wal` and `-shm` sidecar files travel with it automatically; nothing extra is needed to mount or back them up alongside `cache.db`.

## Startup guards

`pdf-mcp-http` fails closed before it binds a port, on a missing `PDF_MCP_AUTH_TOKEN` and on a missing or empty `[paths]` allow list. There is no unauthenticated mode to fall back to, by design: an HTTP transport that starts without a credential is an open document server, and the failure would be silent.

The allow-list guard exists because `PDFConfig.check_path` enforces an allow list only when one is non-empty. Deny rules always apply, but deny is subtractive and cannot bound what it was never told to exclude, so an absent allow list leaves the server permissive. That is the correct default for a local stdio install (you already have the filesystem) and unsafe for a remote endpoint. For where the allow list comes from in each kind of install, see [`configuration.md`](configuration.md).

`PDF_MCP_ALLOW_ANY_PATH=1` overrides that second guard, and only that guard. It skips the startup check; it does not touch `check_path` and cannot relax an allow list you have configured, so if your config has one, setting the variable changes nothing. It matters only in the case the guard fires on, no allow list at all, and there its effect is total: the token alone grants read access to every file the process can open. Never set it on a host holding anything you would not hand to the token's holder.
