"""Core gating logic for the particle-readout gate service.

A payload is a list of integer sample points (id, size, intensity) plus an
ordered list of gates.  A gate is either a convex polygon (3..12 distinct
vertices, non-zero area, boundary counts as a hit) or a combination of two
earlier gates via AND / OR / DIFF (left set minus right set).

A polygon gate may carry up to MAX_HOLES internal exclusion zones ("holes").
Each hole is itself a 3..12-vertex convex, non-degenerate polygon, strictly
inside the outer polygon (touching its boundary is rejected) and pairwise
strictly disjoint from the other holes (touching or overlapping is rejected).
A point that lies inside or on the boundary of any hole does not hit the
gate, even when it lies on the boundary of the outer polygon.

Every gate produces its own hit set; combination gates are pure set algebra
over the hit sets of the gates they reference.  All validation failures raise
ValidationError, which the HTTP layer maps to a single 422 for the whole
request (nothing is partially evaluated).
"""

from __future__ import annotations

import math

MAX_POINTS = 5000
MAX_GATES = 20
MIN_VERTICES = 3
MAX_VERTICES = 12
MAX_HOLES = 8
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


def _orientation(verts):
    """+1 / -1 for counter-clockwise / clockwise winding."""
    return 1 if _signed_area2(verts) > 0 else -1


def _strict_inside(px, py, verts, eps):
    """Strict interior test: boundary points (within eps) are outside."""
    orient = _orientation(verts)
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
        if orient * cross <= eps:
            return False
    return True


def _project(verts, nx, ny):
    lo = hi = verts[0][0] * nx + verts[0][1] * ny
    for x, y in verts[1:]:
        d = x * nx + y * ny
        lo = min(lo, d)
        hi = max(hi, d)
    return lo, hi


def _polygons_touch(a, b):
    """SAT overlap test for two convex polygons; boundary contact counts.

    Two convex polygons are disjoint iff there is an edge-normal axis with a
    strictly positive gap between the projections.  A zero (or tolerance-
    sized) gap on every axis means they touch or overlap.
    """
    tol = 1e-9 * max(1.0, _coord_scale(a), _coord_scale(b))
    for verts in (a, b):
        n = len(verts)
        for i in range(n):
            x1, y1 = verts[i]
            x2, y2 = verts[(i + 1) % n]
            nx = -(y2 - y1)
            ny = (x2 - x1)
            length = math.hypot(nx, ny)
            nx /= length
            ny /= length
            amin, amax = _project(a, nx, ny)
            bmin, bmax = _project(b, nx, ny)
            gap = max(amin, bmin) - min(amax, bmax)
            if gap > tol:
                return False
    return True


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


def _validate_vertices(raw, where, label="vertices"):
    """Validate one polygon ring (outer polygon or an exclusion zone)."""
    if not isinstance(raw, list):
        _fail(f"{where}.{label} must be a list of [x, y] pairs")
    if not MIN_VERTICES <= len(raw) <= MAX_VERTICES:
        _fail(
            f"{where}.{label} must have {MIN_VERTICES}..{MAX_VERTICES} "
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
            _fail(f"{where}.{label}[{j}] must be a [x, y] pair of finite numbers")
        pt = (float(v[0]), float(v[1]))
        if pt in seen:
            _fail(f"{where}.{label}[{j}] duplicates an earlier vertex")
        seen.add(pt)
        verts.append(pt)
    if not _is_convex(verts):
        _fail(f"{where}.{label} do not form a convex polygon")
    if abs(_signed_area2(verts)) <= _eps(verts):
        _fail(f"{where}.{label} form a polygon with zero area")
    return verts


def _validate_holes(raw, outer, where):
    """Validate exclusion zones of one polygon gate.

    Rules: a list of at most MAX_HOLES rings; each ring is a valid convex
    non-degenerate polygon; every hole lies strictly inside the outer polygon
    (touching its boundary is rejected); holes are pairwise strictly disjoint
    (touching or overlapping is rejected).
    """
    if not isinstance(raw, list):
        _fail(f"{where}.holes must be a list of polygons")
    if len(raw) > MAX_HOLES:
        _fail(f"{where} has more than {MAX_HOLES} holes: {len(raw)}")
    holes = []
    for h, raw_hole in enumerate(raw):
        hole = _validate_vertices(raw_hole, where, label=f"holes[{h}].vertices")
        outer_eps = _eps(outer)
        for j, (x, y) in enumerate(hole):
            if not _strict_inside(x, y, outer, outer_eps):
                _fail(
                    f"{where}.holes[{h}].vertices[{j}] is not strictly inside "
                    f"the outer polygon; exclusion zones may not touch or cross "
                    f"its boundary"
                )
        for k, earlier in enumerate(holes):
            if _polygons_touch(hole, earlier):
                _fail(
                    f"{where}.holes[{h}] touches or overlaps holes[{k}]; "
                    f"exclusion zones must be strictly disjoint"
                )
        holes.append(hole)
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
            verts = _validate_vertices(g["vertices"], where)
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
            eps = _eps(verts)
            holes = gate["holes"]
            hits = set()
            for p in points:
                if not point_in_convex(p["size"], p["intensity"], verts, eps):
                    continue
                # Points inside or on an exclusion-zone boundary are removed,
                # even when they sit on the outer polygon's boundary.
                if any(
                    point_in_convex(p["size"], p["intensity"], hole, _eps(hole))
                    for hole in holes
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
