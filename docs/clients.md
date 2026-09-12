# MCP client setup

Configuration for each MCP client pdf-mcp is known to work with. Install
the server first:

```bash
pip install pdf-mcp
```

<details open>
<summary><strong>Claude Code</strong></summary>

```bash
claude mcp add pdf-mcp -- pdf-mcp
```

Or add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "pdf-mcp"
    }
  }
}
```

</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

**One-click install (nothing to install first):**

1. Download `pdf-mcp-<version>.mcpb` from the
   [latest release](https://github.com/jztan/pdf-mcp/releases/latest).
2. In Claude Desktop open **Settings > Extensions** and drag the file onto
   that page (double-clicking the file also works on some computers). Click
   **Install**.
3. The first start downloads pdf-mcp's components (about 250 MB) and needs an
   internet connection; it can take a few minutes. Claude sees the tools
   right away and they start working once setup finishes. Later starts take
   seconds.
4. Use it in a **Chat**: give Claude the file's location, for example
   "Use pdf-mcp to summarize C:\Users\me\Downloads\report.pdf", or a folder
   of PDFs. A PDF attached to the chat is read by Claude directly and does
   not go through pdf-mcp.

Needs Windows 10 or later, macOS 13 or later on an Intel Mac, or macOS 14 or
later on an Apple Silicon Mac (the oldest versions pdf-mcp's search engine,
onnxruntime, is built for).

Works in Claude Desktop's **Chat**. Cowork and Claude Code inside Claude
Desktop start extensions differently and need Node.js installed on the
computer; if you use those, install with pip below.

**Updating:** download the newer `.mcpb` and drag it onto Settings >
Extensions the same way; it replaces the installed version in place. With
"Check for updates" left on, Claude tells you when a new version is out.

**Uninstalling:** remove pdf-mcp in Claude Desktop's extension settings, then
delete the folder `.cache\pdf-mcp` in your user folder
(`%USERPROFILE%\.cache\pdf-mcp` on Windows, `~/.cache/pdf-mcp` on macOS and
Linux). It holds pdf-mcp's downloaded components and its PDF cache; Claude
Desktop does not remove it.

**Or configure it by hand** (needs `pip install pdf-mcp` first). Add to your
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "pdf-mcp"
    }
  }
}
```

Config file location:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Restart Claude Desktop after updating the config.

</details>

<details>
<summary><strong>Visual Studio Code</strong></summary>

Requires VS Code 1.101+ with GitHub Copilot.

**CLI:**
```bash
code --add-mcp '{"name":"pdf-mcp","command":"pdf-mcp"}'
```

**Command Palette:**
1. Open Command Palette (`Cmd/Ctrl+Shift+P`)
2. Run `MCP: Open User Configuration` (global) or `MCP: Open Workspace Folder Configuration` (project-specific)
3. Add the configuration:
   ```json
   {
     "servers": {
       "pdf-mcp": {
         "command": "pdf-mcp"
       }
     }
   }
   ```
4. Save. VS Code will automatically load the server.

**Manual:** Create `.vscode/mcp.json` in your workspace:
```json
{
  "servers": {
    "pdf-mcp": {
      "command": "pdf-mcp"
    }
  }
}
```

</details>

<details>
<summary><strong>Codex CLI</strong></summary>

```bash
codex mcp add pdf-mcp -- pdf-mcp
```

Or configure manually in `~/.codex/config.toml`:

```toml
[mcp_servers.pdf-mcp]
command = "pdf-mcp"
```

</details>

<details>
<summary><strong>Kiro</strong></summary>

Create or edit `.kiro/settings/mcp.json` in your workspace:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "pdf-mcp",
      "args": [],
      "disabled": false
    }
  }
}
```

Save and restart Kiro.

</details>

<details>
<summary><strong>Other MCP Clients</strong></summary>

Most MCP clients use a standard configuration format:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "pdf-mcp"
    }
  }
}
```

With `uvx` (for isolated environments):

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "uvx",
      "args": ["pdf-mcp"]
    }
  }
}
```

</details>

## Verify

```bash
pdf-mcp --help
```

If the server starts and your client lists the pdf-mcp tools, you are set.
See [tool-reference.md](tool-reference.md) for what each tool does.

## OCR setup

Scanned PDFs (pages that are photos, with no selectable text) need
Tesseract. Everything else works without it.

With the Claude Desktop bundle on Windows or a Mac there is nothing to do:
the first scanned page you ask about downloads an English-only Tesseract
(about 14 MB) into pdf-mcp's cache folder. If that first call says OCR is
being set up, ask again a minute later. To read other languages, or with a
pip or uvx install, install Tesseract yourself:

- **Windows:** download the 64-bit installer from the
  [UB Mannheim Tesseract page](https://github.com/UB-Mannheim/tesseract/wiki)
  and run it with the default folder. Or, in a terminal:
  `winget install -e --id UB-Mannheim.TesseractOCR`.
- **macOS:** `brew install tesseract`.
- **Linux:** `sudo apt install tesseract-ocr` (or your distribution's package).

pdf-mcp finds Tesseract in its standard install folder even when it is not
on `PATH`, so there is nothing to configure and no restart: ask Claude to
read the scanned page again.
