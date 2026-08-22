"""Document open, metadata and TOC over pypdfium2."""

from __future__ import annotations

from typing import Any

import pypdfium2 as pdfium

from .geometry import Rect


class Page:
    """Minimal page handle. Later tasks add text, drawings and raster."""

    def __init__(self, doc: "Document", index: int) -> None:
        self._doc = doc
        self.number = index
        self._raw = doc._pdf[index]
        width, height = self._raw.get_size()
        self.rect = Rect(0.0, 0.0, width, height)


class Document:
    def __init__(self, path: str) -> None:
        self._path = path
        self._pdf = pdfium.PdfDocument(path)

    @property
    def page_count(self) -> int:
        return len(self._pdf)

    def __len__(self) -> int:
        return len(self._pdf)

    def __getitem__(self, index: int) -> Page:
        return Page(self, index)

    @property
    def needs_pass(self) -> bool:
        # pypdfium2 raises at open time on a password-protected file, so a
        # Document that exists at all is already decrypted.
        return False

    @property
    def metadata(self) -> dict[str, Any]:
        """PyMuPDF's raw metadata shape.

        extractor.extract_metadata maps these exact keys (creationDate,
        modDate, format) to its snake_case response, so the key names here
        are a contract, not a style choice.
        """
        meta = self._pdf.get_metadata_dict()
        version = self._pdf.get_version()
        return {
            "format": f"PDF {version // 10}.{version % 10}" if version else "",
            "title": meta.get("Title", ""),
            "author": meta.get("Author", ""),
            "subject": meta.get("Subject", ""),
            "keywords": meta.get("Keywords", ""),
            "creator": meta.get("Creator", ""),
            "producer": meta.get("Producer", ""),
            "creationDate": meta.get("CreationDate", ""),
            "modDate": meta.get("ModDate", ""),
            "trapped": meta.get("Trapped", ""),
            "encryption": None,
        }

    def get_toc(self) -> list[list[Any]]:
        """[level, title, page] entries, matching PyMuPDF's list shape.

        pypdfium2 levels and page indices are both 0-based; PyMuPDF's are
        1-based.
        """
        out: list[list[Any]] = []
        for item in self._pdf.get_toc():
            dest = item.get_dest()
            page_index = dest.get_index() if dest is not None else None
            out.append(
                [
                    item.level + 1,
                    item.get_title(),
                    (page_index + 1) if page_index is not None else -1,
                ]
            )
        return out

    def close(self) -> None:
        self._pdf.close()


def open_document(path: str) -> Document:
    return Document(path)
