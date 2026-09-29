"""Cross-checks for the gates service.

The reference implementation here is deliberately independent from
gates/gating.py: point-in-polygon uses point-on-segment + ray casting (the
service uses half-plane signs), and combination gates are evaluated with
plain Python set algebra computed directly from the reference geometry.
Randomized small payloads are compared field by field ("对拍").
"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402
from gating import evaluate, validate_payload  # noqa: E402


# ---------------------------------------------------------------------------
# independent reference implementation


def _ref_on_segment(px, py, ax, ay, bx, by):
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    tol = 1e-9 * max(1.0, abs(ax), abs(ay), abs(bx), abs(by)) ** 2
    if abs(cross) > tol:
        return False
    return (
        min(ax, bx) - 1e-9 <= px <= max(ax, bx) + 1e-9
        and min(ay, by) - 1e-9 <= py <= max(ay, by) + 1e-9
    )


def _ref_in_polygon(px, py, verts):
    n = len(verts)
    for i in range(n):
        ax, ay = verts[i]
        bx, by = verts[(i + 1) % n]
        if _ref_on_segment(px, py, ax, ay, bx, by):
            return True  # boundary counts as a hit
    inside = False
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            x_cross = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > px:
                inside = not inside
    return inside


def _ref_evaluate(points, gates):
    """Independent set computation straight from the gate definitions."""
    sets = {}
    expected_gates = []
    for gate in gates:
        if gate["type"] == "polygon":
            hits = {
                p["id"]
                for p in points
                if _ref_in_polygon(p["size"], p["intensity"], gate["vertices"])
            }
        else:
            left, right = sets[gate["left"]], sets[gate["right"]]
            if gate["op"] == "AND":
                hits = left & right
            elif gate["op"] == "OR":
                hits = left | right
            else:
                hits = left - right
        sets[gate["id"]] = hits
        expected_gates.append(
            {"id": gate["id"], "count": len(hits), "points": sorted(hits)}
        )
    expected_points = [
        {
            "id": p["id"],
            "hits": [1 if p["id"] in sets[g["id"]] else 0 for g in gates],
        }
        for p in sorted(points, key=lambda p: p["id"])
    ]
    return {"gates": expected_gates, "points": expected_points}


# ---------------------------------------------------------------------------
# random payload generation


def _convex_hull(points):
    points = sorted(set(points))
    if len(points) <= 2:
        return points

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _random_polygon(rng):
    for _ in range(200):
        pts = [
            (rng.uniform(0, 1000), rng.uniform(0, 1000))
            for _ in range(rng.randint(3, 14))
        ]
        hull = _convex_hull(pts)
        if 3 <= len(hull) <= 12:
            return [[round(x, 3), round(y, 3)] for x, y in hull]
    raise AssertionError("could not generate a convex polygon")


def _random_payload(rng, n_points=25, n_gates=8):
    ids = rng.sample(range(-50, 10_000), n_points)
    points = [
        {
            "id": pid,
            "size": rng.randint(0, 1000),
            "intensity": rng.randint(0, 1000),
        }
        for pid in ids
    ]
    gates = []
    for i in range(n_gates):
        gid = f"G{i}"
        if i >= 2 and rng.random() < 0.5:
            left, right = rng.sample([g["id"] for g in gates], 2)
            gates.append(
                {
                    "id": gid,
                    "type": "combo",
                    "op": rng.choice(["AND", "OR", "DIFF"]),
                    "left": left,
                    "right": right,
                }
            )
        else:
            gates.append(
                {"id": gid, "type": "polygon", "vertices": _random_polygon(rng)}
            )
    return {"points": points, "gates": gates}


# ---------------------------------------------------------------------------
# tests


class CrossCheckTest(unittest.TestCase):
    def test_randomized_against_reference(self):
        rng = random.Random(20260926)
        for case in range(300):
            payload = _random_payload(
                rng, n_points=rng.randint(0, 30), n_gates=rng.randint(0, 12)
            )
            points, gates = validate_payload(payload)
            got = evaluate(points, gates)
            expected = _ref_evaluate(payload["points"], payload["gates"])
            self.assertEqual(
                got,
                expected,
                msg=f"case {case} diverged",
            )

    def test_boundary_points_are_hits(self):
        payload = {
            "points": [
                {"id": 1, "size": 100, "intensity": 300},  # on left edge
                {"id": 2, "size": 500, "intensity": 500},  # exactly a vertex
                {"id": 3, "size": 300, "intensity": 100},  # on bottom edge
                {"id": 4, "size": 99, "intensity": 300},  # just outside
                {"id": 5, "size": 300, "intensity": 300},  # interior
            ],
            "gates": [
                {
                    "id": "box",
                    "type": "polygon",
                    "vertices": [[100, 100], [500, 100], [500, 500], [100, 500]],
                }
            ],
        }
        _, gates = validate_payload(payload)
        got = evaluate(validate_payload(payload)[0], gates)
        self.assertEqual(got["gates"][0]["points"], [1, 2, 3, 5])
        hits = {p["id"]: p["hits"] for p in got["points"]}
        self.assertEqual(hits[4], [0])

    def test_float_vertices_and_clockwise_winding(self):
        payload_points = [
            {"id": 1, "size": 10, "intensity": 10},
            {"id": 2, "size": 45, "intensity": 45},
            {"id": 3, "size": 90, "intensity": 90},
        ]
        gates = [
            {
                "id": "tri",
                "type": "polygon",
                "vertices": [[0.5, 0.5], [0.5, 99.5], [99.5, 0.5]],  # clockwise
            }
        ]
        points, parsed = validate_payload({"points": payload_points, "gates": gates})
        got = evaluate(points, parsed)
        self.assertEqual(got["gates"][0]["points"], [1, 2])

    def test_combo_chain_and_diff_direction(self):
        points = [
            {"id": 1, "size": 10, "intensity": 10},  # in A only
            {"id": 2, "size": 50, "intensity": 50},  # in A and B
            {"id": 3, "size": 90, "intensity": 90},  # in B only
            {"id": 4, "size": 500, "intensity": 500},  # in neither
        ]
        gates = [
            {"id": "A", "type": "polygon", "vertices": [[0, 0], [60, 0], [60, 60], [0, 60]]},
            {"id": "B", "type": "polygon", "vertices": [[40, 40], [100, 40], [100, 100], [40, 100]]},
            {"id": "AND", "type": "combo", "op": "AND", "left": "A", "right": "B"},
            {"id": "OR", "type": "combo", "op": "OR", "left": "A", "right": "B"},
            {"id": "AminusB", "type": "combo", "op": "DIFF", "left": "A", "right": "B"},
            {"id": "BminusA", "type": "combo", "op": "DIFF", "left": "B", "right": "A"},
            {"id": "nest", "type": "combo", "op": "DIFF", "left": "OR", "right": "AND"},
        ]
        pts, parsed = validate_payload({"points": points, "gates": gates})
        got = evaluate(pts, parsed)
        by_id = {g["id"]: g["points"] for g in got["gates"]}
        self.assertEqual(by_id["AND"], [2])
        self.assertEqual(by_id["OR"], [1, 2, 3])
        self.assertEqual(by_id["AminusB"], [1])
        self.assertEqual(by_id["BminusA"], [3])
        self.assertEqual(by_id["nest"], [1, 3])
        # hit vectors follow gate order
        hits = {p["id"]: p["hits"] for p in got["points"]}
        self.assertEqual(hits[1], [1, 0, 0, 1, 1, 0, 1])
        self.assertEqual(hits[4], [0, 0, 0, 0, 0, 0, 0])

    def test_empty_inputs(self):
        got = evaluate(*validate_payload({"points": [], "gates": []}))
        self.assertEqual(got, {"gates": [], "points": []})


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def _post(self, payload):
        return self.client.post("/api/evaluate", json=payload)

    def _valid_payload(self):
        return {
            "points": [{"id": 7, "size": 5, "intensity": 5}],
            "gates": [
                {"id": "g", "type": "polygon", "vertices": [[0, 0], [10, 0], [10, 10]]}
            ],
        }

    def test_happy_path_over_http(self):
        resp = self._post(self._valid_payload())
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["gates"][0]["points"], [7])
        self.assertEqual(body["points"][0]["hits"], [1])

    def test_health(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_422_cases(self):
        base = self._valid_payload()
        poly = base["gates"][0]

        cases = {
            "duplicate point id": {
                "points": [base["points"][0], base["points"][0]],
                "gates": base["gates"],
            },
            "extra point field": {
                "points": [{**base["points"][0], "label": "x"}],
                "gates": base["gates"],
            },
            "missing point field": {
                "points": [{"id": 7, "size": 5}],
                "gates": base["gates"],
            },
            "extra top-level field": {**base, "meta": {}},
            "missing top-level field": {"points": base["points"]},
            "coordinate out of range": {
                "points": [{"id": 7, "size": 1001, "intensity": 5}],
                "gates": base["gates"],
            },
            "negative coordinate": {
                "points": [{"id": 7, "size": -1, "intensity": 5}],
                "gates": base["gates"],
            },
            "bool coordinate": {
                "points": [{"id": 7, "size": True, "intensity": 5}],
                "gates": base["gates"],
            },
            "non-integer coordinate": {
                "points": [{"id": 7, "size": 5.5, "intensity": 5}],
                "gates": base["gates"],
            },
            "duplicate gate id": {"points": [], "gates": [poly, poly]},
            "extra gate field": {
                "points": [],
                "gates": [{**poly, "color": "red"}],
            },
            "unknown gate type": {
                "points": [],
                "gates": [{"id": "g", "type": "circle", "vertices": poly["vertices"]}],
            },
            "forward reference": {
                "points": [],
                "gates": [
                    {"id": "c", "type": "combo", "op": "AND", "left": "a", "right": "b"},
                    {"id": "a", "type": "polygon", "vertices": poly["vertices"]},
                    {"id": "b", "type": "polygon", "vertices": poly["vertices"]},
                ],
            },
            "unknown reference": {
                "points": [],
                "gates": [
                    {"id": "c", "type": "combo", "op": "OR", "left": "nope", "right": "nope2"}
                ],
            },
            "self reference": {
                "points": [],
                "gates": [
                    {"id": "c", "type": "combo", "op": "AND", "left": "c", "right": "c"}
                ],
            },
            "bad op": {
                "points": [],
                "gates": [
                    {"id": "a", "type": "polygon", "vertices": poly["vertices"]},
                    {"id": "b", "type": "polygon", "vertices": poly["vertices"]},
                    {"id": "c", "type": "combo", "op": "XOR", "left": "a", "right": "b"},
                ],
            },
            "too few vertices": {
                "points": [],
                "gates": [{"id": "g", "type": "polygon", "vertices": [[0, 0], [1, 1]]}],
            },
            "too many vertices": {
                "points": [],
                "gates": [
                    {
                        "id": "g",
                        "type": "polygon",
                        "vertices": [[i, (i * 7) % 13] for i in range(13)],
                    }
                ],
            },
            "duplicate vertices": {
                "points": [],
                "gates": [
                    {"id": "g", "type": "polygon", "vertices": [[0, 0], [10, 0], [0, 0]]}
                ],
            },
            "zero area polygon": {
                "points": [],
                "gates": [
                    {"id": "g", "type": "polygon", "vertices": [[0, 0], [5, 5], [10, 10]]}
                ],
            },
            "non-convex polygon": {
                "points": [],
                "gates": [
                    {
                        "id": "g",
                        "type": "polygon",
                        "vertices": [[0, 0], [10, 0], [10, 10], [6, 3]],
                    }
                ],
            },
            "self-intersecting polygon": {
                "points": [],
                "gates": [
                    {
                        "id": "g",
                        "type": "polygon",
                        "vertices": [[0, 0], [10, 10], [0, 10], [10, 0]],
                    }
                ],
            },
            "too many gates": {
                "points": [],
                "gates": [
                    {"id": f"g{i}", "type": "polygon", "vertices": poly["vertices"]}
                    for i in range(21)
                ],
            },
            "too many points": {
                "points": [
                    {"id": i, "size": 1, "intensity": 1} for i in range(5001)
                ],
                "gates": [],
            },
            "points not a list": {"points": {}, "gates": []},
            "gates not a list": {"points": [], "gates": {}},
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                resp = self._post(payload)
                self.assertEqual(resp.status_code, 422, resp.get_json())
                body = resp.get_json()
                self.assertEqual(body["error"]["code"], "VALIDATION_FAILED")
                self.assertTrue(body["error"]["message"])

    def test_non_json_body_is_422(self):
        resp = self.client.post(
            "/api/evaluate", data="not json", content_type="text/plain"
        )
        self.assertEqual(resp.status_code, 422)

    def test_maximum_size_payload_accepted(self):
        payload = {
            "points": [
                {"id": i, "size": i % 1001, "intensity": (i * 3) % 1001}
                for i in range(5000)
            ],
            "gates": [
                {
                    "id": f"g{i}",
                    "type": "polygon",
                    "vertices": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
                }
                for i in range(20)
            ],
        }
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(len(body["gates"]), 20)
        self.assertEqual(body["gates"][0]["count"], 5000)
        self.assertEqual(len(body["points"][0]["hits"]), 20)


if __name__ == "__main__":
    unittest.main()
