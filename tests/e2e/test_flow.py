"""Browser flow: the scatter plot and the gate table must render from the
same /api/evaluate response, and a stale response must never overwrite a
newer edit.
"""


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
