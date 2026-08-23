"""Value types matching PyMuPDF's duck-typed geometry surface.

Consumers read .width/.height, call .get_area() and intersect with &.
None of that is documented anywhere; it was discovered by crashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def get_area(self) -> float:
        return self.width * self.height

    def __iter__(self) -> Iterator[float]:
        # _table_spans_full_page does `x0, y0, x1, y1 = page.rect`, and
        # several call sites do list(rect) or round each of four values.
        # Without this the TypeError is swallowed into "extraction
        # failed" and the page reports zero tables instead of raising.
        return iter((self.x0, self.y0, self.x1, self.y1))

    def __len__(self) -> int:
        return 4

    def __getitem__(self, index: int) -> float:
        return (self.x0, self.y0, self.x1, self.y1)[index]

    def __and__(self, other: "Rect") -> "Rect":
        x0 = max(self.x0, other.x0)
        y0 = max(self.y0, other.y0)
        x1 = min(self.x1, other.x1)
        y1 = min(self.y1, other.y1)
        if x1 < x0 or y1 < y0:
            return Rect(0.0, 0.0, 0.0, 0.0)
        return Rect(x0, y0, x1, y1)


@dataclass(frozen=True)
class Quad:
    ul: Point
    ur: Point
    ll: Point
    lr: Point
