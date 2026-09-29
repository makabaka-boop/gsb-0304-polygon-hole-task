/* 颗粒门控分析 — viewer 单页应用。
 *
 * 任何编辑都会触发重新评估；每次请求携带递增序号，响应只在序号仍是最新时
 * 才会应用到界面上，因此慢速的旧响应永远无法覆盖更新的编辑结果。
 */

const SAMPLE_POINTS = [
  [1, 50, 50], [2, 100, 300], [3, 300, 300], [4, 480, 350],
  [5, 600, 400], [6, 500, 500], [7, 650, 480], [8, 900, 700],
  [9, 850, 650], [10, 400, 300], [11, 200, 700], [12, 750, 500],
];

const SAMPLE_GATES = [
  {
    id: "G1",
    type: "polygon",
    vertices: [[100, 100], [500, 100], [500, 500], [100, 500]],
    holes: [[[350, 150], [420, 150], [420, 220], [350, 220]]],
  },
  { id: "G2", type: "polygon", vertices: [[400, 300], [900, 300], [900, 700]] },
  { id: "G3", type: "combo", op: "AND", left: "G1", right: "G2" },
  { id: "G4", type: "combo", op: "DIFF", left: "G1", right: "G2" },
  { id: "G5", type: "combo", op: "OR", left: "G1", right: "G2" },
];

const GATE_COLORS = [
  "#7c6cf0", "#1f9d8f", "#d1495b", "#e8a13d",
  "#3d7bd8", "#8f5fb8", "#5a8f3c", "#b84a8a",
];

const TEMPLATE = `
<header>
  <h1>颗粒门控分析</h1>
  <div id="status" :class="{bad: !!error}"
       :data-point-count="result ? result.points.length : ''"
       :data-gate-count="result ? result.gates.length : ''">
    <span v-if="error">⚠ {{ error }}</span>
    <span v-else-if="result">已评估 {{ result.points.length }} 点 · {{ result.gates.length }} 门 · {{ lastUpdated }}</span>
    <span v-else>评估中…</span>
    <span v-if="evaluating > 0" class="spin" title="评估中">⟳</span>
  </div>
</header>
<main>
  <section id="left">
    <div class="panel">
      <h2>散点 <span class="dim">{{ points.length }}/5000</span></h2>
      <div class="row">
        <button @click="addPointMode = !addPointMode" :class="{on: addPointMode}">
          {{ addPointMode ? "■ 停止加点" : "✚ 图上加点" }}
        </button>
        <button @click="loadSample">载入示例</button>
        <button @click="points = []">清空</button>
      </div>
      <div class="row">
        <input v-model="newPoint.id" placeholder="id" class="w4">
        <input v-model="newPoint.size" placeholder="size" class="w5">
        <input v-model="newPoint.intensity" placeholder="intensity" class="w6">
        <button @click="addPoint">添加点</button>
      </div>
      <details>
        <summary>批量导入（每行 id,size,intensity，或 JSON 数组）</summary>
        <textarea v-model="bulkText" rows="4" spellcheck="false"
          placeholder="1,50,50&#10;2,100,300&#10;…"></textarea>
        <button @click="applyBulk">替换全部散点</button>
      </details>
      <p v-if="localError" class="err">{{ localError }}</p>
      <table id="points-table">
        <thead>
          <tr><th>id</th><th>size</th><th>int.</th><th>命中向量(门序)</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="p in shownPoints" :key="p.id" :data-pid="p.id"
              :class="{rowhit: selectedHitIds.has(p.id)}">
            <td>{{ p.id }}</td>
            <td>{{ p.size }}</td>
            <td>{{ p.intensity }}</td>
            <td class="hits">{{ hitVectorText(p.id) }}</td>
            <td><button class="del" @click="removePoint(p.id)" title="删除">×</button></td>
          </tr>
        </tbody>
      </table>
      <p v-if="points.length > shownPoints.length" class="note">
        表格仅显示前 {{ shownPoints.length }} 行，散点图仍展示全部 {{ points.length }} 点。
      </p>
    </div>

    <div class="panel">
      <h2>门 <span class="dim">{{ gates.length }}/20 · 按顺序定义，组合门只能引用更早的门</span></h2>
      <table id="gate-table">
        <thead>
          <tr><th>#</th><th>id</th><th>类型</th><th>定义</th><th>命中数</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="(g, i) in gates" :key="g.id" class="gate-row" :data-gid="g.id"
              :class="{selected: g.id === selectedGateId, input: inputGateIds.includes(g.id)}"
              @click="selectGate(g.id)">
            <td class="dim">{{ i }}</td>
            <td class="gid" :style="{color: gateColor(g.id)}">{{ g.id }}</td>
            <td>{{ g.type === "polygon" ? "多边形" : g.op }}</td>
            <td class="def">{{ gateDefText(g) }}</td>
            <td class="gate-count">{{ gateCount(g.id) }}</td>
            <td><button class="del" @click.stop="removeGate(g.id)" title="删除">×</button></td>
          </tr>
        </tbody>
      </table>
      <p v-if="selectedGate" class="note">
        已选 <b>{{ selectedGateId }}</b>：散点图中橙色为命中。
        <span v-if="inputGateIds.length">输入门 {{ inputGateIds.join("、") }} 已在表中高亮。</span>
        <span v-else>多边形门，无输入门。</span>
      </p>
      <div class="addgate">
        <div class="row">
          <input v-model="newGate.id" placeholder="新门 id" class="w6">
          <select v-model="newGate.kind">
            <option value="polygon">多边形</option>
            <option value="combo">组合</option>
          </select>
          <template v-if="newGate.kind === 'combo'">
            <select v-model="newGate.op">
              <option>AND</option><option>OR</option><option>DIFF</option>
            </select>
            <select v-model="newGate.left">
              <option disabled value="">左集</option>
              <option v-for="g in gates" :value="g.id">{{ g.id }}</option>
            </select>
            <select v-model="newGate.right">
              <option disabled value="">右集</option>
              <option v-for="g in gates" :value="g.id">{{ g.id }}</option>
            </select>
          </template>
          <button @click="addGate">添加门</button>
        </div>
        <div class="row" v-if="newGate.kind === 'polygon'">
          <input v-model="newGate.verticesText" class="grow" spellcheck="false"
            placeholder="顶点 JSON，如 [[100,100],[500,100],[500,500]]（3~12 个，凸，非零面积）">
        </div>
        <div class="row" v-if="newGate.kind === 'polygon'">
          <input v-model="newGate.holesText" class="grow" spellcheck="false"
            placeholder="排除区 JSON，如 [[[200,200],[300,200],[300,300]]]；可留空">
        </div>
      </div>
    </div>
  </section>

  <section id="right">
    <svg id="scatter" ref="svg" viewBox="-70 -30 1120 1120"
         preserveAspectRatio="xMidYMid meet" @click="onScatterClick">
      <g class="grid">
        <line v-for="t in ticks" :key="'v'+t" :x1="t" y1="0" :x2="t" y2="1000"></line>
        <line v-for="t in ticks" :key="'h'+t" x1="0" :y1="t" x2="1000" :y2="t"></line>
      </g>
      <rect class="frame" x="0" y="0" width="1000" height="1000"></rect>
      <!-- 外轮廓与所有排除区拼成一条路径，fill-rule=evenodd 使排除区成为真空洞 -->
      <path v-for="g in polygonGates" :key="'poly'+g.id" class="gate-poly"
            :data-gid="g.id"
            :class="{emph: g.id === selectedGateId || inputGateIds.includes(g.id)}"
            :d="polygonPathAttr(g)" fill-rule="evenodd"
            :style="{stroke: gateColor(g.id), fill: gateColor(g.id)}"></path>
      <circle v-for="p in points" :key="'pt'+p.id" class="pt" :class="pointClass(p)"
              :cx="p.size" :cy="1000 - p.intensity" :r="pointRadius(p)" :data-pid="p.id">
        <title>#{{ p.id }} ({{ p.size }}, {{ p.intensity }}) 命中 {{ hitVectorText(p.id) }}</title>
      </circle>
      <g class="labels">
        <text v-for="t in ticks" :key="'xl'+t" class="tick" :x="t" y="1032">{{ t }}</text>
        <text v-for="t in ticks" :key="'yl'+t" class="tick" x="-14" :y="1000 - t + 5">{{ t }}</text>
        <text class="axis" x="470" y="1072">size</text>
        <text class="axis" x="-52" y="520" transform="rotate(-90 -52 520)">intensity</text>
      </g>
    </svg>
  </section>
</main>
`;

const app = Vue.createApp({
  template: TEMPLATE,

  data() {
    return {
      points: SAMPLE_POINTS.map(([id, size, intensity]) => ({ id, size, intensity })),
      gates: SAMPLE_GATES.map((g) => JSON.parse(JSON.stringify(g))),
      selectedGateId: "G3",
      result: null,
      error: null,
      localError: null,
      evaluating: 0,
      lastUpdated: null,
      addPointMode: false,
      bulkText: "",
      newPoint: { id: "", size: "", intensity: "" },
      newGate: { id: "", kind: "polygon", verticesText: "", holesText: "", op: "AND", left: "", right: "" },
      reqSeq: 0,      // 已发出的最新请求序号
      appliedSeq: 0,  // 已应用到界面的请求序号
      resultSignature: null, // 已应用响应对应的请求体（防止同 id 改形状时旧响应高亮）
      pointRowLimit: 300,
      _debounce: null,
    };
  },

  computed: {
    ticks() {
      const t = [];
      for (let v = 0; v <= 1000; v += 100) t.push(v);
      return t;
    },
    // 响应必须仍对应当前的完整请求（点集与门定义，含顶点/排除区），否则视为
    // 过期编辑的残留，不用于渲染。仅门 id 相同不足以保证形状相同。
    resultUsable() {
      return !!this.result && this.resultSignature === this.requestBody();
    },
    selectedIndex() {
      return this.gates.findIndex((g) => g.id === this.selectedGateId);
    },
    selectedGate() {
      return this.selectedIndex >= 0 ? this.gates[this.selectedIndex] : null;
    },
    selectedHitIds() {
      if (!this.resultUsable || this.selectedIndex < 0) return new Set();
      return new Set(this.result.gates[this.selectedIndex].points);
    },
    inputGateIds() {
      const g = this.selectedGate;
      return g && g.type === "combo" ? [g.left, g.right] : [];
    },
    polygonGates() {
      return this.gates.filter((g) => g.type === "polygon");
    },
    hitVectorById() {
      const m = new Map();
      if (this.resultUsable) {
        for (const p of this.result.points) m.set(p.id, p.hits);
      }
      return m;
    },
    shownPoints() {
      return this.points.slice(0, this.pointRowLimit);
    },
  },

  watch: {
    points: { deep: true, handler() { this.scheduleEvaluate(); } },
    gates: { deep: true, handler() { this.scheduleEvaluate(); } },
  },

  mounted() {
    window.__gatingApp = this; // 供端到端测试探查请求序号
    this.evaluate();
  },

  methods: {
    scheduleEvaluate() {
      clearTimeout(this._debounce);
      this._debounce = setTimeout(() => this.evaluate(), 200);
    },

    requestBody() {
      return JSON.stringify({
        points: this.points.map((p) => ({ id: p.id, size: p.size, intensity: p.intensity })),
        gates: this.gates.map((g) =>
          g.type === "polygon"
            ? { id: g.id, type: "polygon", vertices: g.vertices.map((v) => [v[0], v[1]]), holes: g.holes || [] }
            : { id: g.id, type: "combo", op: g.op, left: g.left, right: g.right }
        ),
      });
    },

    async evaluate() {
      const seq = ++this.reqSeq;
      const body = this.requestBody(); // 本次请求的输入快照
      this.evaluating++;
      try {
        const resp = await fetch("/api/evaluate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
        });
        let data = null;
        try { data = await resp.json(); } catch (_) { /* 非 JSON 响应 */ }
        if (seq !== this.reqSeq || body !== this.requestBody()) return; // 旧响应丢弃
        if (resp.ok) {
          this.result = data;
          this.resultSignature = body;
          this.error = null;
          this.appliedSeq = seq;
          this.lastUpdated = new Date().toLocaleTimeString();
        } else {
          // 校验失败时旧结果同样不得继续高亮：它属于上一份输入
          this.result = null;
          this.resultSignature = null;
          this.error = (data && data.error && data.error.message) || ("HTTP " + resp.status);
        }
      } catch (e) {
        if (seq !== this.reqSeq || body !== this.requestBody()) return;
        this.result = null;
        this.resultSignature = null;
        this.error = "无法连接 gates 服务：" + e.message;
      } finally {
        this.evaluating--;
      }
    },

    gateColor(gid) {
      const i = this.gates.findIndex((g) => g.id === gid);
      return GATE_COLORS[(i < 0 ? 0 : i) % GATE_COLORS.length];
    },

    gateDefText(g) {
      if (g.type === "combo") return `${g.left} ${g.op} ${g.right}`;
      return g.vertices.map((v) => `(${v[0]},${v[1]})`).join(" ") + ` · 排除区 ${(g.holes || []).length}`;
    },

    gateCount(gid) {
      if (!this.resultUsable) return "…";
      const i = this.gates.findIndex((g) => g.id === gid);
      return i >= 0 ? this.result.gates[i].count : "…";
    },

    hitVectorText(pid) {
      const h = this.hitVectorById.get(pid);
      return h ? h.join("") : "—";
    },

    pointClass(p) {
      if (!this.resultUsable || !this.selectedGateId) return "idle";
      return this.selectedHitIds.has(p.id) ? "hit" : "miss";
    },

    pointRadius(p) {
      if (!this.resultUsable || !this.selectedGateId) return 8;
      return this.selectedHitIds.has(p.id) ? 11 : 6;
    },

    polygonPathAttr(g) {
      // 外轮廓后追加每个排除区，作为同一路径的子多边形；evenodd 让内部环变空洞
      const rings = [g.vertices, ...(g.holes || [])];
      return rings.map((ring) => {
        const d = ring
          .map((v, i) => `${i === 0 ? "M" : "L"}${v[0]},${1000 - v[1]}`)
          .join(" ");
        return d + " Z";
      }).join(" ");
    },

    selectGate(id) {
      this.selectedGateId = this.selectedGateId === id ? null : id;
    },

    addPoint() {
      this.localError = null;
      const id = Number(this.newPoint.id);
      const size = Number(this.newPoint.size);
      const intensity = Number(this.newPoint.intensity);
      if (![id, size, intensity].every(Number.isInteger)) {
        this.localError = "id、size、intensity 都必须是整数";
        return;
      }
      if (size < 0 || size > 1000 || intensity < 0 || intensity > 1000) {
        this.localError = "坐标须在 0~1000";
        return;
      }
      if (this.points.some((p) => p.id === id)) {
        this.localError = `点 id ${id} 已存在`;
        return;
      }
      if (this.points.length >= 5000) {
        this.localError = "最多 5000 个点";
        return;
      }
      this.points.push({ id, size, intensity });
      this.newPoint = { id: "", size: "", intensity: "" };
    },

    removePoint(id) {
      this.points = this.points.filter((p) => p.id !== id);
    },

    onScatterClick(evt) {
      if (!this.addPointMode) return;
      const svg = this.$refs.svg;
      const pt = new DOMPoint(evt.clientX, evt.clientY).matrixTransform(
        svg.getScreenCTM().inverse()
      );
      const size = Math.round(pt.x);
      const intensity = Math.round(1000 - pt.y);
      if (size < 0 || size > 1000 || intensity < 0 || intensity > 1000) return;
      if (this.points.length >= 5000) return;
      const used = new Set(this.points.map((p) => p.id));
      let id = 1;
      while (used.has(id)) id++;
      this.points.push({ id, size, intensity });
    },

    applyBulk() {
      this.localError = null;
      const text = this.bulkText.trim();
      if (!text) return;
      try {
        let rows;
        if (text.startsWith("[")) {
          rows = JSON.parse(text).map((o) => [o.id, o.size, o.intensity]);
        } else {
          rows = text.split(/\n+/).map((line) => line.trim().split(/[,\s]+/).map(Number));
        }
        const pts = rows.map(([id, size, intensity]) => {
          if (![id, size, intensity].every(Number.isInteger)) {
            throw new Error("存在非整数项");
          }
          if (size < 0 || size > 1000 || intensity < 0 || intensity > 1000) {
            throw new Error(`坐标越界: ${id},${size},${intensity}`);
          }
          return { id, size, intensity };
        });
        if (new Set(pts.map((p) => p.id)).size !== pts.length) {
          throw new Error("存在重复 id");
        }
        if (pts.length > 5000) throw new Error("最多 5000 个点");
        this.points = pts;
      } catch (e) {
        this.localError = "批量导入失败：" + e.message;
      }
    },

    loadSample() {
      this.points = SAMPLE_POINTS.map(([id, size, intensity]) => ({ id, size, intensity }));
      this.gates = SAMPLE_GATES.map((g) => JSON.parse(JSON.stringify(g)));
      this.selectedGateId = "G3";
      this.localError = null;
    },

    addGate() {
      this.localError = null;
      const id = this.newGate.id.trim();
      if (!id) { this.localError = "门 id 不能为空"; return; }
      if (this.gates.some((g) => String(g.id) === id)) {
        this.localError = `门 ${id} 已存在`;
        return;
      }
      if (this.gates.length >= 20) { this.localError = "最多 20 个门"; return; }
      if (this.newGate.kind === "polygon") {
        let verts;
        try {
          verts = JSON.parse(this.newGate.verticesText);
        } catch (_) {
          this.localError = "顶点须为 JSON，如 [[100,100],[500,100],[500,500]]";
          return;
        }
        const ok = Array.isArray(verts) && verts.length >= 3 && verts.length <= 12 &&
          verts.every((v) => Array.isArray(v) && v.length === 2 && v.every(Number.isFinite));
        if (!ok) { this.localError = "顶点须为 3~12 个 [x,y] 数对"; return; }
        let holes = [];
        const holesText = this.newGate.holesText.trim();
        if (holesText) {
          try { holes = JSON.parse(holesText); }
          catch (_) { this.localError = "排除区须为 JSON 数组"; return; }
        }
        const ringOk = (r) =>
          Array.isArray(r) && r.length >= 3 && r.length <= 12 &&
          r.every((v) => Array.isArray(v) && v.length === 2 && v.every(Number.isFinite)) &&
          new Set(r.map((v) => `${v[0]},${v[1]}`)).size === r.length;
        if (!Array.isArray(holes) || holes.length > 8 || !holes.every(ringOk)) {
          this.localError =
            "排除区须为 0~8 个环；每个环 3~12 个不重复的 [x,y] 数对，且须严格位于门内、互不接触重叠（由服务端终验）";
          return;
        }
        this.gates.push({
          id,
          type: "polygon",
          vertices: verts.map((v) => [v[0], v[1]]),
          holes: holes.map((ring) => ring.map((v) => [v[0], v[1]])),
        });
      } else {
        const { op, left, right } = this.newGate;
        if (!left || !right) { this.localError = "组合门需要选择两个输入门"; return; }
        this.gates.push({ id, type: "combo", op, left, right });
      }
      this.newGate.id = "";
      this.newGate.verticesText = "";
      this.newGate.holesText = "";
    },

    removeGate(id) {
      const refs = this.gates.filter(
        (g) => g.type === "combo" &&
          (String(g.left) === String(id) || String(g.right) === String(id))
      );
      if (refs.length) {
        this.localError = `门 ${id} 被 ${refs.map((r) => r.id).join("、")} 引用，无法删除`;
        return;
      }
      this.gates = this.gates.filter((g) => g.id !== id);
      if (this.selectedGateId === id) this.selectedGateId = null;
    },
  },
});

app.mount("#app");
