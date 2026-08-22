"""A PyMuPDF-shaped page and document over the permissive backend.

The individual backend modules take (pdf_path, page_num) because that is
the honest unit of work: pypdfium2, pdfplumber and pypdf each open the
file themselves. Consumers, though, were written against PyMuPDF's
object model and pass `doc` and `page` around (extract_charts takes a
doc and indexes it; extract_text_from_page takes a page). This module
presents that model so those consumers change only where they open the
file.

Only the surface pdf_mcp actually uses is implemented. Anything else is
deliberately absent rather than stubbed, so a missed call site raises
AttributeError at the call rather than returning a plausible empty
value.
"""

from __future__ import annotations

from typing import Any

from . import document as _document
from . import raster as _raster
from . import tables as _tables
from . import texttrace as _texttrace
from . import text as _text
from .geometry import Rect


class Page:
    def __init__(self, pdf_path: str, page_num: int, rect: Rect) -> None:
        self._path = pdf_path
        self.number = page_num
        self.rect = rect

    def get_text(
        self,
        kind: str = "text",
        *,
        sort: bool = False,
        clip: Any = None,
        **_ignored: Any,
    ) -> Any:
        box = None
        if clip is not None:
            box = (float(clip[0]), float(clip[1]), float(clip[2]), float(clip[3]))
        return _text.get_text(self._path, self.number, kind, sort=sort, clip=box)

    def get_drawings(self) -> list[dict[str, Any]]:
        from .drawings_router import get_drawings

        return get_drawings(self._path, self.number)

    def get_texttrace(self) -> list[dict[str, Any]]:
        return _texttrace.get_texttrace(self._path, self.number)

    def get_image_info(self) -> list[dict[str, Any]]:
        return _raster.get_image_info(self._path, self.number)

    def find_tables(self) -> Any:
        return _tables.TableFinding(tables=_tables.find_tables(self._path, self.number))

    def get_pixmap(self, dpi: int = 150, clip: Any = None) -> Any:
        return _raster.render_page(self._path, self.number, dpi=dpi, clip=clip)


class Document:
    """Indexable like a PyMuPDF Document, and a context manager."""

    def __init__(self, pdf_path: str) -> None:
        self._path = pdf_path
        self._doc = _document.open_document(pdf_path)
        self._sizes = [self._doc[i].rect for i in range(self._doc.page_count)]

    @property
    def page_count(self) -> int:
        return len(self._sizes)

    def __len__(self) -> int:
        return len(self._sizes)

    def __getitem__(self, index: int) -> Page:
        return Page(self._path, index, self._sizes[index])

    def __iter__(self) -> Any:
        for i in range(len(self._sizes)):
            yield self[i]

    @property
    def metadata(self) -> dict[str, Any]:
        return self._doc.metadata

    @property
    def needs_pass(self) -> bool:
        return self._doc.needs_pass

    def get_toc(self) -> list[list[Any]]:
        return self._doc.get_toc()

    def close(self) -> None:
        self._doc.close()

    def __enter__(self) -> "Document":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def open_document(pdf_path: str) -> Document:
    return Document(pdf_path)
