"""Core gating logic for the particle-readout gate service.

A payload is a list of integer sample points (id, size, intensity) plus an
ordered list of gates.  A gate is either a convex polygon (3..12 distinct
vertices, non-zero area, boundary counts as a hit) with optional internal
exclusion holes, or a combination of two earlier gates via AND / OR / DIFF
(left set minus right set).

A polygon gate may carry up to MAX_HOLES convex holes.  A point belongs to
the gate iff it is in (or on the boundary of) the outer polygon AND not in
(or on the boundary of) any hole.  Every hole must be a valid convex ring
lying strictly inside the outer polygon; holes may not touch or overlap the
outer boundary or each other (a nested hole counts as overlap).

Every gate produces its own hit set; combination gates are pure set algebra
over the hit sets of the gates they reference.  All validation failures raise
ValidationError, which the HTTP layer maps to a single 422 for the whole
request (nothing is partially evaluated).
"""

from __future__ import annotations

import math

MAX_POINTS = 5000
MAX_GATES = 20
MAX_HOLES = 8
MIN_VERTICES = 3
MAX_VERTICES = 12
COORD_MIN = 0
COORD_MAX = 1000
COMBO_OPS = ("AND", "OR", "DIFF")

_TOP_KEYS = {"points", "gates"}
_POINT_KEYS = {"id", "size", "intensity"}
_POLYGON_KEYS = {"id", "type", "vertices", "holes"}
_COMBO_KEYS = {"id", "type", "op", "left", "right"}


class ValidationError(ValueError):
    """Any payload violation; the HTTP layer turns this into a 422."""


def _fail(message):
    raise ValidationError(message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


# ---------------------------------------------------------------------------
# geometry helpers (tolerance-scaled exact-sign tests)


def _coord_scale(verts):
    scale = 1.0
    for x, y in verts:
        scale = max(scale, abs(x), abs(y))
    return scale


def _eps(verts):
    """Tolerance for cross products (units of coordinate^2)."""
    s = _coord_scale(verts)
    return 1e-9 * s * s


def _signed_area2(verts):
    total = 0.0
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total


def _is_convex(verts):
    """All consecutive-edge cross products share one sign (zeros allowed)."""
    n = len(verts)
    eps = _eps(verts)
    sign = 0
    for i in range(n):
        ox, oy = verts[i]
        ax, ay = verts[(i + 1) % n]
        bx, by = verts[(i + 2) % n]
        cross = (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)
        if cross > eps:
            if sign < 0:
                return False
            sign = 1
        elif cross < -eps:
            if sign > 0:
                return False
            sign = -1
    return True


def point_in_convex(px, py, verts, eps):
    """Half-plane test: inside (or on the boundary) of a convex polygon.

    A convex polygon is the intersection of its edge half-planes, so a point
    is in it iff every edge cross product has a consistent sign; a zero cross
    product means the point lies on that edge's supporting line, i.e. on the
    boundary when the other signs agree.
    """
    n = len(verts)
    sign = 0
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
        if cross > eps:
            if sign < 0:
                return False
            sign = 1
        elif cross < -eps:
            if sign > 0:
                return False
            sign = -1
    return True


def _strictly_inside(px, py, verts):
    """Strict interior test: boundary points and points outside are False.

    A point is strictly inside a convex polygon iff it lies on the interior
    side of every edge.  Edge cross products are compared against the
    polygon's sign with the same tolerance used for the half-plane tests, so
    a point sitting on (or tolerance-close to) an edge never passes.
    """
    eps = _eps(verts)
    n = len(verts)
    area_sign = 1 if _signed_area2(verts) > 0 else -1
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
        if cross * area_sign <= eps:
            return False
    return True


def _project(verts, nx, ny):
    lo = hi = nx * verts[0][0] + ny * verts[0][1]
    for x, y in verts[1:]:
        v = nx * x + ny * y
        lo = min(lo, v)
        hi = max(hi, v)
    return lo, hi


def _strictly_separated(verts_a, verts_b):
    """True iff two convex polygons have a strictly positive gap.

    Separating-axis theorem for convex polygons: the polygons are disjoint
    with a gap iff some edge-normal axis projects them onto intervals whose
    open gap is positive.  Touching (a shared point or collinear overlap),
    crossing, containment and equality all return False.
    """
    scale = max(_coord_scale(verts_a), _coord_scale(verts_b))
    tol = 1e-9 * scale
    for verts, other in ((verts_a, verts_b), (verts_b, verts_a)):
        n = len(verts)
        for i in range(n):
            x1, y1 = verts[i]
            x2, y2 = verts[(i + 1) % n]
            dx, dy = x2 - x1, y2 - y1
            length = math.hypot(dx, dy)
            if length == 0.0:
                continue  # duplicate vertices are rejected elsewhere
            nx, ny = -dy / length, dx / length  # unit edge normal
            a0, a1 = _project(verts, nx, ny)
            b0, b1 = _project(other, nx, ny)
            if a1 + tol < b0 or b1 + tol < a0:
                return True
    return False


# ---------------------------------------------------------------------------
# validation


def _validate_points(raw):
    if not isinstance(raw, list):
        _fail("points must be a list")
    if len(raw) > MAX_POINTS:
        _fail(f"at most {MAX_POINTS} points allowed, got {len(raw)}")
    seen = set()
    points = []
    for i, p in enumerate(raw):
        where = f"points[{i}]"
        if not isinstance(p, dict):
            _fail(f"{where} must be an object")
        extra = set(p) - _POINT_KEYS
        if extra:
            _fail(f"{where} has unexpected fields: {sorted(extra)}")
        missing = _POINT_KEYS - set(p)
        if missing:
            _fail(f"{where} is missing fields: {sorted(missing)}")
        pid = p["id"]
        if not _is_int(pid):
            _fail(f"{where}.id must be an integer")
        if pid in seen:
            _fail(f"duplicate point id {pid}")
        seen.add(pid)
        for axis in ("size", "intensity"):
            value = p[axis]
            if not _is_int(value):
                _fail(f"{where}.{axis} must be an integer")
            if not COORD_MIN <= value <= COORD_MAX:
                _fail(f"{where}.{axis} must be within {COORD_MIN}..{COORD_MAX}")
        points.append({"id": pid, "size": p["size"], "intensity": p["intensity"]})
    return points


def _validate_ring(raw, where):
    """Validate a polygon ring (outer polygon or a hole): a list of 3..12
    distinct finite [x, y] pairs forming a convex, non-zero-area polygon."""
    if not isinstance(raw, list):
        _fail(f"{where} must be a list of [x, y] pairs")
    if not MIN_VERTICES <= len(raw) <= MAX_VERTICES:
        _fail(
            f"{where} must have {MIN_VERTICES}..{MAX_VERTICES} "
            f"vertices, got {len(raw)}"
        )
    verts = []
    seen = set()
    for j, v in enumerate(raw):
        if not (
            isinstance(v, (list, tuple))
            and len(v) == 2
            and _is_num(v[0])
            and _is_num(v[1])
        ):
            _fail(f"{where}[{j}] must be a [x, y] pair of finite numbers")
        pt = (float(v[0]), float(v[1]))
        if pt in seen:
            _fail(f"{where}[{j}] duplicates an earlier vertex")
        seen.add(pt)
        verts.append(pt)
    if not _is_convex(verts):
        _fail(f"{where} do not form a convex polygon")
    if abs(_signed_area2(verts)) <= _eps(verts):
        _fail(f"{where} form a polygon with zero area")
    return verts


def _validate_holes(raw, outer, where):
    """Validate the exclusion holes of a polygon gate.

    Every hole must be a well-formed convex ring lying strictly inside the
    outer polygon, and holes must be pairwise disjoint (no touching or
    overlap, including nesting).  The normalized holes are returned in order.
    """
    if not isinstance(raw, list):
        _fail(f"{where}.holes must be a list of rings")
    if len(raw) > MAX_HOLES:
        _fail(f"{where} has at most {MAX_HOLES} holes, got {len(raw)}")
    holes = []
    for k, ring in enumerate(raw):
        hwhere = f"{where}.holes[{k}]"
        verts = _validate_ring(ring, hwhere)
        for j, (vx, vy) in enumerate(verts):
            if not _strictly_inside(vx, vy, outer):
                _fail(
                    f"{hwhere}[{j}] is outside or on the boundary of the "
                    f"outer polygon; holes must be strictly inside it"
                )
        for prev_k, prev in enumerate(holes):
            if not _strictly_separated(prev, verts):
                _fail(
                    f"{hwhere} touches or overlaps holes[{prev_k}]; "
                    f"holes must be disjoint"
                )
        holes.append(verts)
    return holes


def _validate_gate_id(raw, where):
    if isinstance(raw, str):
        if raw:
            return raw
    elif _is_int(raw):
        return raw
    _fail(f"{where}.id must be a non-empty string or an integer")


def _validate_gates(raw):
    if not isinstance(raw, list):
        _fail("gates must be a list")
    if len(raw) > MAX_GATES:
        _fail(f"at most {MAX_GATES} gates allowed, got {len(raw)}")
    gates = []
    earlier = set()
    for i, g in enumerate(raw):
        where = f"gates[{i}]"
        if not isinstance(g, dict):
            _fail(f"{where} must be an object")
        if "id" not in g or "type" not in g:
            _fail(f"{where} requires both 'id' and 'type'")
        gid = _validate_gate_id(g["id"], where)
        if gid in earlier:
            _fail(f"duplicate gate id {gid!r}")
        gtype = g["type"]
        if gtype == "polygon":
            extra = set(g) - _POLYGON_KEYS
            if extra:
                _fail(f"{where} has unexpected fields: {sorted(extra)}")
            if "vertices" not in g:
                _fail(f"{where} is missing 'vertices'")
            verts = _validate_ring(g["vertices"], f"{where}.vertices")
            holes = _validate_holes(g.get("holes", []), verts, where)
            gates.append(
                {"id": gid, "type": "polygon", "vertices": verts, "holes": holes}
            )
        elif gtype == "combo":
            extra = set(g) - _COMBO_KEYS
            if extra:
                _fail(f"{where} has unexpected fields: {sorted(extra)}")
            missing = _COMBO_KEYS - set(g)
            if missing:
                _fail(f"{where} is missing fields: {sorted(missing)}")
            op = g["op"]
            if op not in COMBO_OPS:
                _fail(f"{where}.op must be one of {list(COMBO_OPS)}")
            for ref_key in ("left", "right"):
                ref = g[ref_key]
                if not isinstance(ref, (str, int)) or isinstance(ref, bool):
                    _fail(f"{where}.{ref_key} must reference an earlier gate id")
                if ref not in earlier:
                    _fail(
                        f"{where}.{ref_key} references unknown or not-yet-defined "
                        f"gate {ref!r}"
                    )
            gates.append(
                {
                    "id": gid,
                    "type": "combo",
                    "op": op,
                    "left": g["left"],
                    "right": g["right"],
                }
            )
        else:
            _fail(f"{where}.type must be 'polygon' or 'combo'")
        earlier.add(gid)
    return gates


def validate_payload(data):
    """Validate the whole request body; return normalized (points, gates)."""
    if not isinstance(data, dict):
        _fail("request body must be a JSON object")
    extra = set(data) - _TOP_KEYS
    if extra:
        _fail(f"unexpected top-level fields: {sorted(extra)}")
    missing = _TOP_KEYS - set(data)
    if missing:
        _fail(f"missing top-level fields: {sorted(missing)}")
    points = _validate_points(data["points"])
    gates = _validate_gates(data["gates"])
    return points, gates


# ---------------------------------------------------------------------------
# evaluation


def evaluate(points, gates):
    """Return per-gate sorted hit ids/counts and per-point hit vectors."""
    gate_sets = []
    index_by_id = {}
    for gate in gates:
        if gate["type"] == "polygon":
            verts = gate["vertices"]
            hole_tests = [(hole, _eps(hole)) for hole in gate["holes"]]
            eps = _eps(verts)
            hits = set()
            for p in points:
                px, py = p["size"], p["intensity"]
                if not point_in_convex(px, py, verts, eps):
                    continue
                # Points inside a hole or on its boundary are excluded.
                if any(
                    point_in_convex(px, py, hole, heps)
                    for hole, heps in hole_tests
                ):
                    continue
                hits.add(p["id"])
        else:
            left = gate_sets[index_by_id[gate["left"]]]
            right = gate_sets[index_by_id[gate["right"]]]
            if gate["op"] == "AND":
                hits = left & right
            elif gate["op"] == "OR":
                hits = left | right
            else:  # DIFF
                hits = left - right
        index_by_id[gate["id"]] = len(gate_sets)
        gate_sets.append(hits)

    gate_results = [
        {"id": gate["id"], "count": len(hits), "points": sorted(hits)}
        for gate, hits in zip(gates, gate_sets)
    ]
    point_results = [
        {
            "id": p["id"],
            "hits": [1 if p["id"] in hits else 0 for hits in gate_sets],
        }
        for p in sorted(points, key=lambda p: p["id"])
    ]
    return {"gates": gate_results, "points": point_results}
