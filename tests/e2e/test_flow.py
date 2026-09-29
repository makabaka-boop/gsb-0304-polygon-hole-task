"""Browser flow: the scatter plot and the gate table must render from the
same /api/evaluate response, and a stale response must never overwrite a
newer edit.
"""

import json


def _gate_counts_from_dom(page):
    counts = {}
    for row in page.locator("#gate-table tr.gate-row").all():
        gid = row.get_attribute("data-gid")
        counts[gid] = row.locator(".gate-count").inner_text()
    return counts


def _hit_pids_from_scatter(page):
    return sorted(
        int(pid)
        for pid in page.locator("#scatter circle.pt.hit").evaluate_all(
            "els => els.map(e => e.dataset.pid)"
        )
    )


def test_scatter_and_gate_table_share_one_response(page, services):
    """One browser flow: capture the evaluate response, then check that both
    the gate table and the scatter plot reflect exactly that response."""
    with page.expect_response("**/api/evaluate") as resp_info:
        page.goto(services)
    data = resp_info.value.json()

    page.wait_for_function(
        "document.querySelector('#status').dataset.pointCount !== ''"
    )

    # gate table counts come from this very response
    counts = _gate_counts_from_dom(page)
    assert len(counts) == len(data["gates"])
    for gate in data["gates"]:
        assert counts[gate["id"]] == str(gate["count"])

    # scatter renders every point from the same response
    assert page.locator("#scatter circle.pt").count() == len(data["points"])

    # selecting each gate highlights exactly its hit ids from the response
    for gate in data["gates"]:
        page.locator(f"#gate-table tr[data-gid='{gate['id']}']").click()
        assert _hit_pids_from_scatter(page) == gate["points"], gate["id"]

    # combo gate G3 highlights its input gates G1/G2 in the table
    page.locator("#gate-table tr[data-gid='G3']").click()
    assert "input" in (
        page.locator("#gate-table tr[data-gid='G1']").get_attribute("class") or ""
    )
    assert "input" in (
        page.locator("#gate-table tr[data-gid='G2']").get_attribute("class") or ""
    )

    # per-point hit vectors in the table match the response, in gate order
    hits_by_id = {p["id"]: p["hits"] for p in data["points"]}
    for pid, hits in hits_by_id.items():
        cell = page.locator(f"#points-table tr[data-pid='{pid}'] .hits")
        assert cell.inner_text() == "".join(map(str, hits))


def test_stale_response_cannot_overwrite_newer_edit(page, services):
    """Hold the initial evaluate response, edit, let the fresh response land,
    then release the stale one: the UI must keep showing the fresh result."""
    held = []

    def handler(route):
        if not held and route.request.method == "POST":
            held.append(route)  # 挂起首个评估请求，永不自动放行
        else:
            route.continue_()

    page.route("**/api/evaluate", handler)
    arrivals = []
    page.on(
        "response",
        lambda r: arrivals.append(r) if "/api/evaluate" in r.url else None,
    )

    page.goto(services)

    # 初始响应还被挂着，立刻编辑：添加点 13 (333,333)，落在 G1 内
    page.get_by_placeholder("id", exact=True).fill("13")
    page.get_by_placeholder("size").fill("333")
    page.get_by_placeholder("intensity").fill("333")
    page.get_by_role("button", name="添加点").click()

    # 新编辑的评估（第二个请求）正常完成：界面进入 13 点的世界
    page.wait_for_function(
        "document.querySelector('#status').dataset.pointCount === '13'",
        timeout=5000,
    )
    fresh = arrivals[0].json()
    assert len(fresh["points"]) == 13

    # 放行被挂起的旧响应（它对应 12 点的旧编辑）
    assert len(held) == 1
    held[0].continue_()
    page.wait_for_function(
        "window.__gatingApp && window.__gatingApp.evaluating === 0",
        timeout=5000,
    )
    page.wait_for_function("true && document.querySelectorAll('#scatter circle.pt').length >= 12")
    assert len(arrivals) == 2

    # 旧响应确实是旧世界（12 点），而且是在新响应之后到达的
    stale = arrivals[1].json()
    assert len(stale["points"]) == 12
    stale_g1 = next(g for g in stale["gates"] if g["id"] == "G1")
    fresh_g1 = next(g for g in fresh["gates"] if g["id"] == "G1")
    assert stale_g1["count"] != fresh_g1["count"]

    # 关键断言：界面仍展示新编辑的结果，旧响应被丢弃
    assert page.get_attribute("#status", "data-point-count") == "13"
    assert page.locator("#scatter circle.pt").count() == 13
    counts = _gate_counts_from_dom(page)
    assert counts["G1"] == str(fresh_g1["count"])
    seqs = page.evaluate(
        "({req: window.__gatingApp.reqSeq, applied: window.__gatingApp.appliedSeq})"
    )
    assert seqs["req"] == seqs["applied"]


def _add_point(page, pid, size, intensity):
    page.get_by_placeholder("id", exact=True).fill(str(pid))
    page.get_by_placeholder("size").fill(str(size))
    page.get_by_placeholder("intensity").fill(str(intensity))
    page.get_by_role("button", name="添加点").click()


def test_hole_render_counts_and_highlights_share_one_response(page, services):
    """A gate with an exclusion zone: the even-odd path shows a real hole,
    the gate-table count, the orange highlight set and the per-point hit
    vectors all come from the same response, and points inside/on the hole
    are excluded by all three surfaces."""
    responses = []
    page.on(
        "response",
        lambda r: responses.append(r) if "/api/evaluate" in r.url else None,
    )

    with page.expect_response("**/api/evaluate"):
        page.goto(services)
    page.wait_for_function(
        "document.querySelector('#status').dataset.pointCount !== ''"
    )

    # 21: hole interior, 22: on the hole boundary, 23: free interior
    _add_point(page, 21, 250, 250)
    _add_point(page, 22, 200, 250)
    _add_point(page, 23, 400, 400)

    # add a polygon gate H with one square exclusion zone via the form
    page.get_by_placeholder("新门 id").fill("H")
    page.locator(".addgate input.grow").first.fill(
        "[[100,100],[500,100],[500,500],[100,500]]"
    )
    page.locator(".addgate input.grow").nth(1).fill(
        "[[[200,200],[300,200],[300,300],[200,300]]]"
    )
    page.get_by_role("button", name="添加门").click()

    # wait until the 6-gate response is applied
    page.wait_for_function(
        "document.querySelector('#status').dataset.gateCount === '6'", timeout=5000
    )
    # pick the newest response that already carries gate H (read bodies here,
    # not inside the event handler, where .json() would be a dangling coroutine)
    data = None
    for resp in reversed(responses):
        body = resp.json()
        if any(g["id"] == "H" for g in body["gates"]):
            data = body
            break
    assert data is not None
    gate_h = next(g for g in data["gates"] if g["id"] == "H")
    # samples 2/4/6/10 plus point 23 are inside the box; 21 and 22 land in
    # (or on) the hole and must be gone
    assert gate_h["points"] == [2, 4, 6, 10, 23]
    assert gate_h["count"] == 5

    # gate table count comes from this response
    assert _gate_counts_from_dom(page)["H"] == "5"

    # the shape is ONE evenodd path containing both rings -> real hole
    path = page.locator("#scatter path.gate-poly").last
    assert path.get_attribute("fill-rule") == "evenodd"
    d = path.get_attribute("d")
    assert d.count("Z") == 2, d

    # selecting H highlights exactly the response hit set
    page.locator("#gate-table tr[data-gid='H']").click()
    assert _hit_pids_from_scatter(page) == [2, 4, 6, 10, 23]
    for pid in (21, 22):
        cls = page.locator(f"#scatter circle.pt[data-pid='{pid}']").get_attribute("class")
        assert "hit" not in cls.split()
    cls23 = page.locator("#scatter circle.pt[data-pid='23']").get_attribute("class")
    assert "hit" in cls23.split()

    # per-point vectors for the new points match that same response (H last)
    hits_by_id = {p["id"]: p["hits"] for p in data["points"]}
    for pid in (21, 22, 23):
        cell = page.locator(f"#points-table tr[data-pid='{pid}'] .hits")
        assert cell.inner_text() == "".join(map(str, hits_by_id[pid]))
    assert hits_by_id[21][-1] == 0
    assert hits_by_id[22][-1] == 0
    assert hits_by_id[23][-1] == 1


def test_stale_shape_edit_same_gate_ids_cannot_stay_highlighted(page, services):
    """Reshape gate G1 (vertices change, gate ids/order do not) while its
    fresh response is held: the UI must immediately stop showing the old
    gate-table counts and orange highlight; releasing the fresh response
    restores values that match an independent re-evaluation."""
    page.goto(services)
    page.wait_for_function(
        "document.querySelector('#status').dataset.pointCount !== ''"
    )

    # select G1 and confirm the initial response currently drives highlights
    page.locator("#gate-table tr[data-gid='G1']").click()
    page.wait_for_function(
        "document.querySelectorAll('#scatter circle.pt.hit').length > 0"
    )
    g1_before = _gate_counts_from_dom(page)["G1"]

    held = []

    def handler(route):
        if route.request.method == "POST":
            held.append(route)  # hold the reshape response
        else:
            route.continue_()

    page.route("**/api/evaluate", handler)

    # reshape G1 with the SAME gate id/order; skip the 200ms debounce
    new_verts = [[50, 50], [700, 50], [700, 700], [50, 700]]
    page.evaluate(
        "(verts) => {"
        "  const a = window.__gatingApp;"
        "  a.scheduleEvaluate = function(){};"
        "  a.gates[0].vertices = verts;"
        "  a.evaluate();"
        "}",
        new_verts,
    )
    page.wait_for_function("window.__gatingApp.reqSeq === 2")
    assert len(held) == 1

    # same ids, but the old response no longer matches the request signature:
    # no counts, no status data, no orange highlights may remain
    assert page.get_attribute("#status", "data-point-count") == ""
    assert page.get_attribute("#status", "data-gate-count") == ""
    assert _gate_counts_from_dom(page)["G1"] == "…"
    page.wait_for_function(
        "document.querySelectorAll('#scatter circle.pt.hit').length === 0"
    )

    # release the fresh response: the UI comes back driven by it; G1 stays
    # selected throughout, so its highlight set must reappear on its own
    held[0].continue_()
    page.wait_for_function(
        "document.querySelector('#status').dataset.pointCount !== ''", timeout=5000
    )
    page.wait_for_function(
        "document.querySelectorAll('#scatter circle.pt.hit').length > 0"
    )
    seqs = page.evaluate(
        "({req: window.__gatingApp.reqSeq, applied: window.__gatingApp.appliedSeq})"
    )
    assert seqs["req"] == seqs["applied"]
    g1_after = _gate_counts_from_dom(page)["G1"]
    assert g1_after != g1_before  # reshaping G1 did change the count

    # stop intercepting, then cross-check the UI against an independent call
    page.unroute("**/api/evaluate")
    fresh_body = json.loads(held[0].request.post_data)
    result = page.evaluate(
        """async (body) => {
          const r = await fetch('/api/evaluate', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)});
          return r.json();
        }""",
        fresh_body,
    )
    fresh_g1 = next(g for g in result["gates"] if g["id"] == "G1")
    assert g1_after == str(fresh_g1["count"])

    # G1 is still selected: the highlight equals that independently-checked set
    assert _hit_pids_from_scatter(page) == fresh_g1["points"]


def test_malformed_and_invalid_holes_never_reach_consistent_ui(page, services):
    """Structurally malformed hole JSON is rejected client-side before a gate
    is even added; a structurally valid but geometrically illegal hole
    (crossing the outer boundary) makes the whole request 422, so the page
    must not show any gate counts/highlights — until the bad gate is removed
    and a valid response arrives again."""
    page.goto(services)
    page.wait_for_function(
        "document.querySelector('#status').dataset.pointCount !== ''"
    )

    def fill_gate(gid, verts, holes_text):
        page.get_by_placeholder("新门 id").fill(gid)
        page.locator(".addgate input.grow").first.fill(verts)
        page.locator(".addgate input.grow").nth(1).fill(holes_text)
        page.get_by_role("button", name="添加门").click()

    # malformed hole JSON (ring with a 1-element pair): client-side reject
    fill_gate(
        "B",
        "[[100,100],[500,100],[500,500],[100,500]]",
        "[[[100,100],[200],[100,200]]]",
    )
    page.wait_for_selector("#left .err")
    assert page.locator("#gate-table tr.gate-row").count() == 5

    # well-formed JSON, but the hole crosses the outer boundary: server 422
    fill_gate(
        "B",
        "[[100,100],[500,100],[500,500],[100,500]]",
        "[[[0,0],[200,0],[0,200]]]",
    )
    page.wait_for_function(
        "document.querySelector('#status').classList.contains('bad')", timeout=5000
    )
    assert page.locator("#status").get_attribute("data-gate-count") == ""
    # no gate may show a count derived from a partial/old response
    assert all(v == "…" for v in _gate_counts_from_dom(page).values())
    assert _hit_pids_from_scatter(page) == []

    # remove the offending gate: the request is valid again, UI recovers
    page.locator("#gate-table tr[data-gid='B'] .del").click()
    page.wait_for_function(
        "document.querySelector('#status').dataset.gateCount === '5'", timeout=5000
    )
    counts = _gate_counts_from_dom(page)
    assert len(counts) == 5 and all(v != "…" for v in counts.values())
