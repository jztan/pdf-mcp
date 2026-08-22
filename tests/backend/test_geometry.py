from pdf_mcp.backend.geometry import Point, Quad, Rect


def test_rect_exposes_pymupdf_duck_typed_surface():
    """chart_extractor.collect() reads .width and .height directly, and
    content_trust intersects rects with & then reads .get_area(). A Rect
    missing any of these raises AttributeError deep inside a consumer."""
    r = Rect(10.0, 20.0, 40.0, 60.0)
    assert (r.x0, r.y0, r.x1, r.y1) == (10.0, 20.0, 40.0, 60.0)
    assert r.width == 30.0
    assert r.height == 40.0
    assert r.get_area() == 1200.0


def test_rect_intersection_returns_overlap():
    a = Rect(0.0, 0.0, 10.0, 10.0)
    b = Rect(5.0, 5.0, 20.0, 20.0)
    assert (a & b) == Rect(5.0, 5.0, 10.0, 10.0)


def test_disjoint_intersection_is_empty_not_negative():
    """A negative-area rect would make _covered_by_image mis-score."""
    a = Rect(0.0, 0.0, 5.0, 5.0)
    b = Rect(90.0, 90.0, 99.0, 99.0)
    assert (a & b).get_area() == 0.0


def test_point_and_quad_construct():
    p = Point(1.5, 2.5)
    assert (p.x, p.y) == (1.5, 2.5)
    q = Quad(Point(0, 0), Point(1, 0), Point(0, 1), Point(1, 1))
    assert q.ur.x == 1
