# Remote access over HTTP

`pdf-mcp-http` authenticates with a single shared bearer token, `PDF_MCP_AUTH_TOKEN`, verified by FastMCP's `StaticTokenVerifier`. One token, one tenant, no per-user identity. That is the whole model.

![Request path and trust boundary for pdf-mcp over HTTP: an MCP client sends a bearer token over HTTPS, a proxy terminates TLS and forwards over loopback to the container, and one tenant boundary encloses the shared cache, warmed corpus, and paths allow list](images/remote-access-architecture.svg)

## What it is

`pdf-mcp-http` runs the same tools as the stdio entry point, served over HTTP at `/mcp` by default. Every request to that endpoint carries an `Authorization: Bearer` header, and the token in it is compared against the one value in `PDF_MCP_AUTH_TOKEN`. Anything else is rejected. One route is deliberately outside auth: `GET /health`, which returns `status` and `version` and nothing else, so an uptime probe can confirm liveness without holding a credential. It is written for a probe on the same box; publishing it through a proxy has a cost, covered under Configuration. There are no sessions, no per-user identity, and no scopes; the verifier holds a single `client_id` of `pdf-mcp` and an empty scope list.

## When to use it, and when not

Use it when one person, or one agent acting for one person, needs pdf-mcp over the network: a home server you reach from a laptop, a VPS you own, a container behind your own proxy. The transport exists because process-per-conversation stdio cannot span machines.

Three situations make it worth the operational cost, and in none of them does an agent hand the server a file.

| situation | what HTTP gives you | on stdio |
|---|---|---|
| **The client cannot spawn a subprocess.** The Anthropic API MCP connector, claude.ai custom connectors, web and mobile hosts. | The only way these clients can reach pdf-mcp. The URL path below suits them well, since a linked paper or filing needs no upload at all. | Not possible: the client has to launch and hold a local process. |
| **A warm corpus is shared by several clients.** A laptop, a phone, and a scheduled agent querying one collection. | One long-lived process, so all of them hit the same warmed SQLite. | Each spawned server has its own cache, so the minutes of extraction and embedding are paid again per client. |
| **You would rather not install the dependencies.** | The Docker image bakes in Tesseract, the embedding model, and the column-aware extractor, so every tool works on the first request. | Tesseract and the embedding model have to be installed on the machine running the server. |

It is not for ad hoc work on documents that live on your own machine. That is stdio's job, it is the default, and it stays simpler for that case: you configure no token, run no proxy, and copy nothing to a server first.

Do not use it to serve a team from one endpoint. The reason is structural, not a policy preference. Every caller of a process shares one SQLite cache, one warmed corpus, and one global `[paths]` allow list, and nothing in the request is scoped to the caller because nothing in it identifies the caller. A second person on the same endpoint is not a second tenant; they are the same tenant with a second keyboard.

## Getting documents to the server

Under stdio, the agent and the server share a filesystem, so any path the agent can name, the server can open. Over HTTP they do not, and every path argument resolves on the server. Two things make a PDF readable, and an agent cannot arrange either one itself.

The first is a file under an allow-listed root. Put it there with whatever you already use: `cp` into the Compose stack's `./documents` folder, which the container sees as `/data/pdfs`, or `rsync`, `scp`, a sync client, or a scheduled job that writes into that directory. Use this for a corpus you curate and keep. It is what the corpus tools want, since they take a directory and reject URLs, and it is what benefits from staying warm between sessions.

The second is an `https://` URL the server fetches. Pass the URL as the `path` argument and the server downloads and caches it. This is the right answer for an application that receives documents from its users: put the bytes in object storage and pass a presigned link. Presigned S3 and GCS URLs work even though they usually serve `application/octet-stream`, because the fetcher accepts that content type and validates the payload by its `%PDF` magic bytes. The constraints are HTTPS only, and SSRF protection rejects loopback, private, link-local, and IMDS addresses, so a link to the agent's own machine or your LAN will not work. The threat model below describes the same capability from the risk side, in the "what the caller can fetch" row; set `[urls].allow` to bound it.

What an agent cannot do is upload a local file, and that is not a gap we can close. MCP has no client-to-server file transfer: Resources and `BlobResourceContents` flow server to client, `roots` names paths without moving bytes, and the only channel into a tool call is its arguments, which the model generates token by token. A tool taking base64 would therefore require the model to emit the whole file. A 5 MB PDF is roughly 1.7 M tokens, past the output limit of a single response and more expensive than reading the document, which defeats the purpose of running this server. The model would also have to transcribe several million characters of binary without one error. The MCP project reached the same conclusion. [SEP-2356](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2356), which passed file content inline as data URIs, was closed in favor of [SEP-2631](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2631), which keeps bytes out of JSON-RPC by negotiating an out-of-band HTTPS transfer instead. Once that is accepted and the SDK supports it, pdf-mcp can implement it. Even then the client application performs the upload, not the model.

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

Four differences matter when moving from stdio to HTTP.

| | stdio | HTTP |
|---|---|---|
| **What the boundary is** | The operating system's user boundary. If you can spawn the process, you were already that user. | A bearer token in a header, only as strong as the client config file that holds it. |
| **How long state lives** | `PDFCache._init_db` ends by calling `clear_expired()`, so every conversation that spawns the server sweeps the TTL. | The process is long-lived, so a warmed corpus and its cache outlive any single session. The sweep runs at startup, and the process may then run for weeks without another. |
| **What bounds local reads** | The user's own filesystem permissions. | The `[paths]` allow list, and nothing else. That is why its absence is a startup failure rather than a warning. |
| **What the caller can fetch** | Same URL-fetching branch, but reachable only by someone who is already that user. | The token buys outbound HTTPS fetching too. `_resolve_path` treats an `https://` value as a download, governed by `[urls]`, which is empty (unrestricted) by default. |

Outbound fetching needs more detail than a table cell holds. SSRF hardening still applies to the fetch: it cannot reach loopback, RFC 1918, link-local, or IMDS addresses, and only `https://` is accepted. What is left is ordinary public-internet egress originating from your server, and disk filling with whatever the caller fetched. Set `[urls].allow` if either matters to you. For the full URL-fetching surface, see [`tool-reference.md`](tool-reference.md#url-fetching-ssrf).

For the in-scope and out-of-scope lists that govern security reports, see [`SECURITY.md`](../SECURITY.md).

## The single-tenant contract

Single tenancy here is a contract, not a gap waiting to be filled. It is worth saying why, since a reader may arrive from a project that does have per-user tokens.

Issuing a token per user would change nothing about what those users can reach. pdf-mcp has no per-token path scoping, so per-user tokens would authenticate callers while authorizing them identically. That is worse than one honest shared token: a list of named credentials implies an isolation boundary that does not exist, and operators act on what the credential model implies.

Isolation comes from separate processes with separate cache volumes, exactly as stdio gets it from process-per-conversation. Someone who needs two users runs two containers, each with its own token, cache volume, and allow list. The shipped `docker-compose.yml` is written for one instance: it hardcodes `container_name: pdf-mcp` and declares a single named volume, `pdf-mcp-cache`, and `COMPOSE_PROJECT_NAME` does not override `container_name`. A second instance means changing both, plus the published host port.

## Configuration

Generate the token with `openssl rand -hex 32`. `deploy/bootstrap.sh` does this, writes it into `.env`, and sets that file to mode 600, readable by its owner and root and nobody else. That `chmod` runs only when the script creates the file, and an existing `.env` is left untouched, so after hand-editing the file to rotate the token, re-check the mode: some editors rewrite a file with fresh default permissions. The Compose stack publishes to loopback only, mapping `127.0.0.1:${PDF_MCP_HOST_PORT:-8802}` onto the container's internal port 8000, so nothing is reachable off the box until you put a TLS proxy in front of it. TLS belongs to that proxy, not to the app. See `deploy/Caddyfile.example` for a starting point.

Decide on `[urls]` while you are writing the config. It is the second half of what the token grants (see the "what the caller can fetch" row of the threat model above) and, unlike `[paths]`, no startup guard asks you about it: left empty it permits fetching from any public HTTPS host. `deploy/config.toml.example` ships a commented `[urls]` block next to the `[paths]` one.

Decide on `/health` too. `deploy/Caddyfile.example` proxies the whole host to the backend, so following it publishes the one credential-free route, and with it the exact running version, to anyone who asks. That is the precondition for matching a published CVE against your deployment. [`SECURITY.md`](../SECURITY.md) accepts version disclosure on `/health` as in bounds, so this is a deliberate default rather than an oversight, but it is being chosen for you. If you would rather it stayed internal, block the path at the proxy and probe over loopback instead. In Caddy that is a path matcher placed above `reverse_proxy`:

```caddyfile
@health path /health
respond @health 404
```

For the environment-variable table, the volume layout, and the Docker mechanics, see [`configuration.md`](configuration.md).

## Client configuration

A client points at the endpoint URL and sends the token as a bearer header on every request. The block below is the Claude-family client config format (Claude Desktop, Claude Code); other MCP clients spell the same two things, the URL and the header, differently:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "type": "http",
      "url": "https://pdf.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

The token then lives in that client's config file in plaintext and inherits exactly that file's protection, no more. Treat it as you would an SSH private key.

## Revoking a token

1. Generate a replacement token: `openssl rand -hex 32`.
2. Rewrite `PDF_MCP_AUTH_TOKEN` in `.env`.
3. Restart the stack: `docker compose up -d --force-recreate`.
4. Update every client that held the old token.
5. Verify the rotation took. Two checks, and you need both. First, confirm the service actually came back, using the credential-free probe:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8802/health
```

Then send the *old* token and expect a rejection:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer <OLD-token>" \
  http://127.0.0.1:8802/mcp
```

Step 3 uses `--force-recreate` on purpose. A plain `up -d` usually recreates the container when a value in `.env` changes, but that depends on how your Compose version hashes the service config, and the failure is silent: the container keeps running with the old token loaded while you believe it is dead.

Step 5 is what catches that. A healthy rotation reads `200` from `/health` and `401` from the old token. Read the two results together, because they fail in different directions:

- `200` then `401`: done.
- `200` then anything else, `200` included: the old credential still validates, so the restart did not pick up the new value. Go back to step 3.
- `000` from either call: that is what `%{http_code}` prints when the connection was refused, so nothing is listening and the service did not come back at all. Repeating step 3 will not help. Run `docker compose logs pdf-mcp` and read the last lines. The usual cause is an `.env` edit that left `PDF_MCP_AUTH_TOKEN` empty, which trips the fail-closed startup guard and exits before the port is bound.

Substitute your own port if you changed `PDF_MCP_HOST_PORT` from its 8802 default.

Revocation stops future requests and does nothing about past ones. It does not invalidate anything already read, and the cache retains whatever was extracted while the old token was valid. Treat a leak as disclosure of every PDF reachable under `[paths]`, and as outbound HTTPS fetching from your server for as long as the token was valid.

## Startup guards

`pdf-mcp-http` fails closed in two places, both before it binds a port.

It refuses to start when `PDF_MCP_AUTH_TOKEN` is unset or empty, telling you to set it to a long random secret. There is no unauthenticated mode to fall back to, by design: an HTTP transport that starts without a credential is an open document server, and the failure would be silent.

It also refuses to start when no non-empty `[paths]` allow list is configured, and the error names the config file it looked in. `PDFConfig.check_path` enforces an allow list only when one is non-empty; deny rules always apply, but deny is subtractive and cannot bound what it was never told to exclude. An absent allow list therefore leaves the server permissive, the correct default for a local stdio install (you already have the filesystem) and unsafe for a remote endpoint. In the Docker image, `deploy/config.docker.toml` supplies that allow list, mounted read-only; for a local or bare-metal `pdf-mcp-http` install, `deploy/config.toml.example` is the template to copy to `~/.config/pdf-mcp/config.toml` and edit.

`PDF_MCP_ALLOW_ANY_PATH=1` overrides that second guard, and only that guard. It skips the startup check; it does not touch `check_path` and cannot relax an allow list you have configured, so if your config has one, setting the variable changes nothing. It matters only in the case the guard fires on, no allow list at all, and there its effect is total: the token alone grants read access to every file the process can open. Never set it on a host holding anything you would not hand to the token's holder.
