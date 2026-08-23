"""Permissive PDF backend.

Exposes the call shapes pdf_mcp already uses (rawdict trees, get_drawings
items, get_texttrace spans) over pypdfium2, pdfplumber and pypdf, so
consumers change only their import. Nothing above this package may import
those libraries directly.
"""
