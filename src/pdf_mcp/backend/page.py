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
    def __init__(
        self,
        pdf_path: str,
        page_num: int,
        rect: Rect,
        raw_doc: Any = None,
    ) -> None:
        self._path = pdf_path
        self.number = page_num
        self.rect = rect
        # The owning Document's already-open pdfium handle, when a Document
        # made this page. Only count_chars uses it, and only to avoid
        # reopening the file once per page: pdf_info's coverage scan calls
        # it for every page, and 500 opens cost more than the counting.
        self._raw_doc = raw_doc

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

    def count_chars(self) -> int:
        return _text.count_chars(self._path, self.number, doc=self._raw_doc)

    def get_drawings(self) -> list[dict[str, Any]]:
        from .drawings_router import get_drawings

        return get_drawings(self._path, self.number)

    def get_texttrace(self) -> list[dict[str, Any]]:
        return _texttrace.get_texttrace(self._path, self.number)

    def get_image_info(self) -> list[dict[str, Any]]:
        return _raster.get_image_info(self._path, self.number)

    def get_images(self, full: bool = False) -> list[tuple[Any, ...]]:
        """PyMuPDF's get_images shape, deduped by image identity.

        Callers read only element 0 (the xref, used to dedupe and to look
        the image up again) and elements 2 and 3 (pixel width/height).
        pdfium has no xref, so element 0 is a hash of the raw image
        stream: repeated placements of one XObject collapse to a single
        entry exactly as an xref would, which is what pdf_info's distinct
        raster-image count depends on.
        """
        seen: dict[int, tuple[Any, ...]] = {}
        for image in _raster.page_images(self._path, self.number, self._raw_doc):
            key = image["key"]
            if key not in seen:
                seen[key] = (
                    key,
                    0,
                    image["width"],
                    image["height"],
                    image["bpc"],
                    "",
                    "",
                    "",
                    "",
                    0,
                )
        return list(seen.values())

    def get_image_rects(self, key: Any) -> list[Rect]:
        return [
            Rect(*image["bbox"])
            for image in _raster.page_images(self._path, self.number, self._raw_doc)
            if image["key"] == key
        ]

    def find_tables(self) -> Any:
        return _tables.TableFinding(tables=_tables.find_tables(self._path, self.number))

    def get_pixmap(self, dpi: int = 150, clip: Any = None) -> Any:
        """A Pixmap-shaped render (see raster.Pixmap for the surface)."""
        box = None
        if clip is not None:
            box = (float(clip[0]), float(clip[1]), float(clip[2]), float(clip[3]))
        return _raster.render_pixmap(self._path, self.number, dpi=dpi, clip=box)


class Document:
    """Indexable like a PyMuPDF Document, and a context manager."""

    def __init__(self, pdf_path: str) -> None:
        self._path = pdf_path
        self._doc = _document.open_document(pdf_path)
        self._page_count = self._doc.page_count
        # Lazy: loading a page just to read its size costs a pdfium page
        # object each. Computing every page's rect at open time made
        # open_pdf ~40ms on a 50-page document and accounted for 0.28s
        # of every corpus query (the excerpt path opens each hit doc).
        self._sizes: dict[int, Rect] = {}

    @property
    def name(self) -> str:
        """The file path, as PyMuPDF's Document.name reports it."""
        return self._path

    @property
    def page_count(self) -> int:
        return self._page_count

    def __len__(self) -> int:
        return self._page_count

    def _rect(self, index: int) -> Rect:
        rect = self._sizes.get(index)
        if rect is None:
            rect = self._doc[index].rect
            self._sizes[index] = rect
        return rect

    def __getitem__(self, index: int) -> Page:
        if not 0 <= index < self._page_count:
            raise IndexError(index)
        return Page(self._path, index, self._rect(index), self._doc.raw_pdf)

    def __iter__(self) -> Any:
        for i in range(self._page_count):
            yield self[i]

    @property
    def metadata(self) -> dict[str, Any]:
        return self._doc.metadata

    @property
    def needs_pass(self) -> bool:
        return self._doc.needs_pass

    def get_toc(self) -> list[list[Any]]:
        return self._doc.get_toc()

    def extract_image(self, key: Any) -> dict[str, Any]:
        """Pixel metadata for one image, looked up by get_images' key."""
        for index in range(self._page_count):
            for image in _raster.page_images(self._path, index):
                if image["key"] == key:
                    return {
                        "image": image["raw"],
                        "ext": "png",
                        "width": image["width"],
                        "height": image["height"],
                    }
        raise ValueError(f"no image with key {key!r}")

    def close(self) -> None:
        self._doc.close()

    def __enter__(self) -> "Document":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def open_document(pdf_path: str) -> Document:
    return Document(pdf_path)
