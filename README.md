# 颗粒门控分析

多边形门可带多个内部排除区，点落在排除区内时不属于该门。

颗粒检测仪的两项读数（size、intensity）经过多级门控后，不仅要看最终数量，
还要能定位某个颗粒在哪一道门被排除。本仓库交付：

- **gates** — Python/Flask 计算服务：校验请求、凸多边形命中判定、组合门集合运算。
- **viewer** — Vue 3 散点编辑页：编辑散点与门、按门高亮命中散点及其输入门。

```
docker-compose.yml
gates/            # Flask 计算服务
  gating.py       #   校验 + 几何 + 集合运算（纯逻辑，无 HTTP 依赖）
  app.py          #   HTTP 层：POST /api/evaluate，GET /api/health
  tests/          #   pytest：独立实现对拍 + 422 规则
viewer/           # Vue 3 单页（无构建步骤，vue.global.prod.js 已内置于 vendor/）
  index.html app.js styles.css
  nginx.conf      #   容器内静态托管 + /api 反代到 gates
  dev_server.py   #   无 Docker 时的等价本地服务
tests/e2e/        # 浏览器流程（Playwright + Chromium）
```

## 启动

```bash
docker compose up --build
# viewer: http://localhost:8080     gates API: http://localhost:5001/api
```

无 Docker 的本地等价运行：

```bash
pip install flask
python3 gates/app.py                                   # gates 监听 :5000
GATES_URL=http://127.0.0.1:5000 python3 viewer/dev_server.py   # viewer 监听 :8080
```

## API 契约

`POST /api/evaluate`，请求体只允许 `points`、`gates` 两个字段：

```json
{
  "points": [{"id": 1, "size": 100, "intensity": 300}],
  "gates": [
    {"id": "G1", "type": "polygon", "vertices": [[100, 100], [500, 100], [500, 500], [100, 500]]},
    {"id": "G2", "type": "polygon", "vertices": [[400, 300], [900, 300], [900, 700]]},
    {"id": "G3", "type": "combo", "op": "AND", "left": "G1", "right": "G2"}
  ]
}
```

约束（任一违反则**整次**返回 422，不做部分求值）：

- 至多 5000 个点；`id` 为唯一整数；`size`、`intensity` 为 0~1000 的整数。
- 至多 20 个门，按顺序定义；门 id 唯一。
- 多边形门：3~12 个顶点、顶点不重复、凸、非零面积；边界上的点算命中。
- 组合门：`op` ∈ `AND`（交）/ `OR`（并）/ `DIFF`（左集减右集），`left`、`right`
  只能引用**更早**定义的门；未知或逆向引用即 422。
- 任何多余字段（顶层、点、门）即 422。

200 响应：

```json
{
  "gates": [{"id": "G1", "count": 1, "points": [1]}],
  "points": [{"id": 1, "hits": [1, 0, 0]}]
}
```

- `gates[i].points`：该门命中的**排序**点 id；`count` 为数量。每个门独立产生点集，
  组合门由引用门的点集做交/并/差得到。
- `points[i].hits`：该点按门顺序的命中向量（0/1），点按 id 排序。

## 前端

- 左侧编辑散点（单点添加、图上点击加点、CSV/JSON 批量导入）与门（多边形顶点
  JSON、组合门引用两个更早的门）；右侧 0~1000 × 0~1000 散点图叠加多边形门。
- 点击门表中任一门：散点图橙色高亮该门命中点，组合门的两个输入门在门表中
  高亮；散点表同时给出每点按门顺序的命中向量——可以直接看到颗粒在哪一道门
  被排除。
- 任何编辑去抖 200ms 后重新评估；每个请求带递增序号，响应只在序号仍为最新时
  才应用到界面，慢速旧响应无法覆盖新编辑。

## 测试

```bash
pip install flask pytest playwright && python3 -m playwright install chromium
python3 -m pytest
```

- `gates/tests/`：随机小点集（凸包随机多边形 + 随机组合门）与**独立实现**对拍
  300 例——参考侧用射线法 + 边上判定做几何、用裸集合运算算组合门，逐字段比对
  排序点 id、数量、命中向量；另有边界点、浮点顶点、DIFF 方向、26 个 422 子用例、
  满负载（5000 点 × 20 门）用例。
- `tests/e2e/`：一条浏览器流程——截获 `/api/evaluate` 响应，逐门核对散点高亮
  集合与门表数量都来自这同一份响应；另一条流程挂起初始响应、编辑后放行，验证
  旧响应不会覆盖新编辑。默认自起本地服务；对 Compose 部署运行可用
  `E2E_BASE_URL=http://localhost:8080 python3 -m pytest tests/e2e`。
