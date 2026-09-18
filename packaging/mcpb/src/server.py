"""Claude Desktop bundle entry point for pdf-mcp.

The manifest passes PDF_MCP_UPDATE_CHECK from the install dialog's
"Check for updates" box. setdefault covers a host that does not substitute
it, and never overrides an explicit value.
"""

import os

os.environ.setdefault("PDF_MCP_UPDATE_CHECK", "1")
# Zero-install OCR: fetch a pinned Tesseract on the first scanned page when
# none is installed. Bundle installs only; [ocr] auto_install can turn it off.
os.environ.setdefault("PDF_MCP_OCR_AUTO_INSTALL", "1")

from pdf_mcp.server import main  # noqa: E402

main()
