# Remote access over HTTP

`pdf-mcp-http` authenticates with a single shared bearer token, `PDF_MCP_AUTH_TOKEN`, verified by FastMCP's `StaticTokenVerifier`. One token, one tenant, no per-user identity. That is the whole model, and this page explains what it protects, what it does not, and how to decide whether the trade is right for your deployment.

![Request path and trust boundary for pdf-mcp over HTTP: an MCP client sends a bearer token over HTTPS, a proxy terminates TLS and forwards over loopback to the container, and one tenant boundary encloses the shared cache, warmed corpus, and paths allow list](images/remote-access-architecture.svg)

## What it is

`pdf-mcp-http` runs the same tools as the stdio entry point, served over HTTP at `/mcp` by default. Every request to that endpoint carries an `Authorization: Bearer` header, and the token in it is compared against the one value in `PDF_MCP_AUTH_TOKEN`; anything else is rejected. One route is deliberately outside auth: `GET /health`, which returns `status` and `version` and nothing else, so a load balancer or an uptime probe can confirm the process is alive without holding a credential. There are no sessions, no per-user identity, and no scopes. The verifier is configured with a single `client_id` of `pdf-mcp` and an empty scope list.

## When to use it, and when not

Use it when one person, or one agent acting for one person, needs pdf-mcp over the network. A home server you reach from a laptop, a VPS you own, a container behind your own reverse proxy: all fine. The transport exists because process-per-conversation stdio cannot span machines.

Do not use it to serve a team from one endpoint. The reason is structural, not a policy preference. Every caller of a single process shares one SQLite cache, one warmed corpus, and one global `[paths]` allow list. Nothing in the request is scoped to the caller, because nothing in the request identifies the caller. A second person on the same endpoint is not a second tenant; they are the same tenant with a second keyboard, reading the same documents through the same cache.

## Threat model versus stdio

Three differences matter when you move from stdio to HTTP.

1. The boundary becomes a bearer token in a header, so it is only as strong as the client config file that holds it. Stdio inherits the operating system's user boundary instead: if you can spawn the process, you were already that user.
2. The process is long-lived, so a warmed corpus and its cache outlive any single session. A stdio process dies with the conversation, taking its in-memory state with it; an HTTP deployment accumulates extracted text on disk across weeks.
3. The `[paths]` allow list becomes the only thing standing between a caller and the filesystem. That is why its absence is a startup failure rather than a warning.

For the in-scope and out-of-scope lists that govern security reports, see [`SECURITY.md`](../SECURITY.md).

## The single-tenant contract

Single tenancy here is a contract, not a gap waiting to be filled. It is worth stating why, because a reader may arrive from a project that does have per-user tokens.

Issuing a token per user would change nothing about what those users can reach. pdf-mcp has no per-token path scoping, so per-user tokens would authenticate callers while authorizing them identically. That is worse than one honest shared token, because a list of named credentials implies an isolation boundary that does not exist, and operators reasonably act on what the credential model implies. A shared token at least tells the truth about the blast radius.

Real isolation comes from running separate processes with separate cache volumes, which is exactly how stdio gets it from process-per-conversation. A reader who needs two users runs two containers, each with its own token, its own named cache volume, and its own allow list. That costs a little memory and buys an actual boundary.

## Configuration

Generate the token with `openssl rand -hex 32`. `deploy/bootstrap.sh` does this for you, writes it into `.env`, and sets that file to mode 600, so the secret is readable only by the account that created it. The Compose stack publishes to loopback only: the host mapping is `127.0.0.1:${PDF_MCP_HOST_PORT:-8802}` onto the container's internal port 8000, so nothing is reachable from off the box until you put something in front of it. TLS is that something, and it belongs to a reverse proxy in front of the process rather than to the app. See `deploy/Caddyfile.example` for a starting point.

For the full environment-variable table, the volume layout, and the rest of the Docker mechanics, see [`configuration.md`](configuration.md).

## Client configuration

An MCP client points at the endpoint URL and sends the token as a bearer header on every request:

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

Once you do that, the token lives in that client's config file in plaintext and inherits exactly that file's protection, no more. Treat it the way you would treat an SSH private key sitting in the same directory.

## Revoking a token

1. Generate a replacement token: `openssl rand -hex 32`.
2. Rewrite `PDF_MCP_AUTH_TOKEN` in `.env`.
3. Restart the stack: `docker compose up -d`.
4. Update every client that held the old token.

Revocation stops future requests and does nothing about past ones. It does not invalidate anything already read, and the cache retains whatever was extracted while the old token was valid. So treat a leaked token as disclosure of every PDF reachable under `[paths]`, not as a window that closes when you rotate. If the leak matters, rotate the token and then decide separately whether the documents themselves need to move.

## Startup guards

`pdf-mcp-http` fails closed in two places, both before it binds a port.

It refuses to start when `PDF_MCP_AUTH_TOKEN` is unset or empty, exiting with a message telling you to set it to a long random secret. There is no unauthenticated mode to fall back to, by design: an HTTP transport that starts without a credential is an open document server, and the failure would be silent.

It also refuses to start when no non-empty `[paths]` allow list is configured, and the error names the config file it looked in. This guard exists because `PDFConfig` enforces path checks only when the allow list is non-empty. An absent allow list therefore leaves the server permissive, which is the correct default for a local stdio install (you already have the filesystem) and unsafe for a remote endpoint, where it would let any holder of the token read any path the process can reach. In the Docker image, `deploy/config.docker.toml` supplies that allow list and is mounted read-only.

`PDF_MCP_ALLOW_ANY_PATH=1` overrides the second guard. It is a deliberate escape hatch, not a convenience: setting it means the token alone grants read access to every file the server process can open, so use it only where that is genuinely what you want, and never on a host holding anything you would not hand to the token's holder.
