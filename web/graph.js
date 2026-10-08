/**
 * DAG 布局与渲染引擎（零依赖、原生 ES 模块）。
 *
 * ## 为什么独立成文件
 *
 * 布局算法是**纯函数**，与 DOM 无关。把它从页面渲染代码里拆出来有两个好处：
 * 1. 可以单独做语法/行为校验（`node --check`、直接 import 跑断言）；
 * 2. 强制「数据 → 几何」与「几何 → DOM」分离，避免布局逻辑悄悄依赖
 *    当前 DOM 状态（这是可视化代码最常见的腐化方式）。
 *
 * ## 坐标系约定
 *
 * 采用**列 = 拓扑层，行 = 层内序号**的层次布局（layered / Sugiyama 简化版）：
 *
 * - `layer(n)`：从根节点出发的最长路径长度 → 保证所有前驱都在更小的层里；
 * - 层内按 `deps` 的字典序稳定排序，减少边交叉；
 * - 节点矩形固定宽高，中心点落格 —— 不做力导向，因为 DAG 的语义是
 *   「有方向的依赖」，力导向图会破坏层次感，读者需要自己找方向。
 *
 * 对一条线性的 `req → arch → coder → verifier` 链，这个算法退化为一条
 * 水平直线，这正是我们希望读者第一眼看到的东西。
 */

// 状态/角色映射表已收敛到 ./js/status.js（唯一真源）。
// 这里把**原有公开 API 原样再导出**，让既有调用方（workbench.js 等页面
// 以及 graph.test.js）无需改动 import 路径，同时保证全站只有一份表 ——
// 不会再出现"改了 A 忘了 B"的缺口。
//
// 注意必须逐个列出，不能用 `export *`：那样会把 status.js 里的
// taskStatusBadge 等与本文件无关的符号也泄漏出去，模糊模块边界。
export {
  AGENT_COLOR,
  STATUS_GROUP,
  agentColor,
  statusGroup,
  statusLabel,
} from './js/status.js?v=20261008-live';

import {
  agentColor as _agentColor,
  statusGroup as _statusGroup,
  statusLabel as _statusLabel,
} from './js/status.js?v=20261008-live';

/** 节点矩形的固定尺寸（SVG 用户单位）。 */
export const NODE_W = 168;
export const NODE_H = 62;
/** 同层节点之间的垂直间距与层间水平间距。 */
const GAP_Y = 22;
const GAP_X = 76;
/** 图形四周留白（给外部坐标标签和阴影留空间）。 */
const PAD = 18;

/**
 * 计算每个节点的拓扑层号。
 *
 * 用**迭代 + 记忆化 + 环检测**，而不是递归：递归在深链上会爆栈，
 * 而 DAG 理论上可以有任意深度。环检测是必需的 —— 虽然 `DAG` 类在
 * 服务端已保证无环，但前端不可假设后端永远正确（也便于单测里构造
 * 畸形输入验证降级行为）。
 *
 * @param {Array<{id:string, deps:string[]}>} nodes
 * @returns {Map<string, number>} nodeId → 层号（从 0 开始）
 * @throws {Error} 存在环时抛出，调用方需捕获并降级为「不渲染图」
 */
export function computeLayers(nodes) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const layers = new Map();
  const visiting = new Set();

  const visit = (id, stack) => {
    if (layers.has(id)) return layers.get(id);
    if (visiting.has(id)) {
      // 环：把路径打出来，否则调试时完全无从下手
      throw new Error(`DAG 存在环：${[...stack, id].join(' → ')}`);
    }
    const node = byId.get(id);
    visiting.add(id);
    let layer = 0;
    for (const dep of node.deps || []) {
      // 悬空依赖（deps 指向不存在的节点）：**跳过而不是崩**，也不要
      // 把它算作一层 —— 否则一个拼错的 id 会把整条链右移一格，
      // 读者会以为多了一个不存在的阶段。真实场景里这更可能意味着
      // 「后端增量推送时先发了边、后发了点」，稍后就会被纠正。
      if (!byId.has(dep)) continue;
      layer = Math.max(layer, visit(dep, [...stack, id]) + 1);
    }
    visiting.delete(id);
    layers.set(id, layer);
    return layer;
  };

  for (const node of nodes) visit(node.id, []);
  return layers;
}

/**
 * 计算完整布局。
 *
 * @param {Array<{id:string, deps:string[]}>} nodes
 * @returns {{nodes: Array, edges: Array, width: number, height: number, layers: number}}
 *   `nodes[i]` 是 `{id, x, y, w, h, cx, cy, layer, row}`，坐标均为左上角；
 *   `edges[j]` 是 `{from, to, d, back}`，`d` 是 SVG path 的 `d` 属性，
 *   `back` 标记该边是否指向更小的层（若后端表达了回退边，前端要能画出来）。
 *   `width`/`height` 是**整张图的包围盒**：不仅包含节点矩形，也包含回退边
 *   向下绕行的那段弧线。调用方可以直接拿它当 viewBox，不需要自己估算边距。
 */
export function layoutDag(nodes) {
  const list = Array.isArray(nodes) ? nodes.filter((n) => n && n.id) : [];
  if (!list.length) {
    return { nodes: [], edges: [], width: 0, height: 0, layers: 0, byId: new Map() };
  }

  const layerOf = computeLayers(list);
  const maxLayer = Math.max(...layerOf.values());

  // 按层分桶；层内排序依据：先按父节点平均行号（barycenter 的极简版），
  // 再按 id 稳定排序。barycenter 能让「共同父节点的兄弟」紧挨着，
  // 显著减少长边交叉，而实现只需一行平均数。
  const buckets = Array.from({ length: maxLayer + 1 }, () => []);
  const byId = new Map(list.map((n) => [n.id, n]));
  for (const node of list) buckets[layerOf.get(node.id)].push(node);

  const rowOf = new Map();
  let maxRows = 0;
  for (const bucket of buckets) {
    bucket.sort((a, b) => {
      const ba = barycenter(a, byId, rowOf);
      const bb = barycenter(b, byId, rowOf);
      if (ba !== bb) return ba - bb;
      return String(a.id).localeCompare(String(b.id));
    });
    bucket.forEach((node, row) => rowOf.set(node.id, row));
    maxRows = Math.max(maxRows, bucket.length);
  }

  const placed = list.map((node) => {
    const layer = layerOf.get(node.id);
    const row = rowOf.get(node.id);
    const x = PAD + layer * (NODE_W + GAP_X);
    // 垂直居中：让每层围绕同一水平轴分布，视觉上比顶对齐稳定得多
    const y = PAD + row * (NODE_H + GAP_Y)
      + (maxRows - buckets[layer].length) * (NODE_H + GAP_Y) / 2;
    return {
      ...node,
      layer, row,
      x, y, w: NODE_W, h: NODE_H,
      cx: x + NODE_W / 2,
      cy: y + NODE_H / 2,
    };
  });

  const position = new Map(placed.map((n) => [n.id, n]));
  const edges = [];
  for (const node of placed) {
    for (const dep of node.deps || []) {
      const from = position.get(dep);
      if (!from) continue; // 悬空边：静默丢弃，不让一条坏边毁掉整张图
      edges.push({
        from: dep,
        to: node.id,
        back: from.layer >= node.layer,
        d: edgePath(from, node),
      });
    }
  }

  const width = PAD * 2 + (maxLayer + 1) * NODE_W + maxLayer * GAP_X;
  const height = dagBounds(placed, edges).height;

  return { nodes: placed, edges, width, height, layers: maxLayer + 1, byId: position };
}

/**
 * 计算 DAG 的包围盒（**纯函数**，只吃几何，可直接单测）。
 *
 * 高度不能只看节点矩形：回退边会绕到所有节点的**下方**（弧顶下探 34），
 * 这段行程不在任何节点里。而 `renderDag()` 会用 `layout.width/height`
 * 覆盖调用方设好的 viewBox —— 一旦高度漏算了弧线，
 * 「验证驳回 → 回退重跑」这条本项目最想展示的边就会被裁掉一半，
 * 而且不会有任何报错（SVG 不会抱怨内容超出 viewBox）。
 *
 * 因此把"节点 + 边"的实际行程一起算进来：宁可多留白，不可裁内容。
 *
 * @param {Array<{x:number,y:number,w:number,h:number}>} nodes
 * @param {Array<{d:string}>} edges
 * @returns {{width:number, height:number}}
 */
export function dagBounds(nodes, edges) {
  const ns = Array.isArray(nodes) ? nodes : [];
  const es = Array.isArray(edges) ? edges : [];
  const width = ns.reduce((m, n) => Math.max(m, n.x + n.w), 0);
  const height = Math.max(
    ns.reduce((m, n) => Math.max(m, n.y + n.h), 0),
    ...es.map((e) => pathMaxY(e?.d)),
    0,
  );
  return { width, height };
}

/** 从 path 的 `d` 里取出最大 Y 坐标。用于把边（含回退弧线）纳入画布包围盒。 */
function pathMaxY(d) {
  let max = 0;
  const nums = String(d ?? '').match(/-?\d+(?:\.\d+)?/g) || [];
  // 每对数字是 (x, y)，取奇数位
  for (let i = 1; i < nums.length; i += 2) {
    const y = Number(nums[i]);
    if (Number.isFinite(y) && y > max) max = y;
  }
  return max;
}

/** 层内排序用的重心：父节点行号的平均值；无父节点视为 -1（排最前）。 */
function barycenter(node, byId, rowOf) {
  const deps = (node.deps || []).filter((d) => rowOf.has(d) && byId.has(d));
  if (!deps.length) return -1;
  return deps.reduce((sum, d) => sum + rowOf.get(d), 0) / deps.length;
}

/**
 * 生成一条边的三次贝塞尔路径。
 *
 * 控制点水平外推，让边从节点右侧中心出发、水平切入目标左侧中心 ——
 * 直线在多层布局里会斜穿节点，而「水平出、水平入」的曲线看起来像
 * 真正的流程图，也让 `back` 边（反向）能被一眼区分。
 *
 * **导出是为了可测**：`back` 分支在当前的严格分层算法下不会被
 * `layoutDag()` 走到（依赖必然落在更小的层里，见 computeLayers 的最长路径
 * 语义），但它是**必须正确**的防御分支 —— 一旦分层算法变化或后端送来
 * 违反拓扑序的依赖，画出来的就是这条弧线。测试直接构造几何来验证它，
 * 比"等它真的发生"可靠得多（这条弧线一旦被 viewBox 裁掉是静默的）。
 */
export function edgePath(from, to) {
  if (from.layer >= to.layer) {
    // 回退边：走一段绕行弧线（下方）而不是穿过节点，避免与主链重叠
    const midY = Math.max(from.y + from.h, to.y + to.h) + 34;
    return `M ${from.cx} ${from.y + from.h} C ${from.cx} ${midY}, ${to.cx} ${midY}, ${to.cx} ${to.y + to.h}`;
  }
  const x1 = from.x + from.w;
  const y1 = from.cy;
  const x2 = to.x;
  const y2 = to.cy;
  const dx = Math.max(28, (x2 - x1) * 0.5);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

/* ------------------------------------------------------------------ *
 * 状态语义
 * ------------------------------------------------------------------ *
 * 表的定义已迁到 ./js/status.js，这里只做**本地别名**：
 * 文件内的布局/渲染代码仍写作 statusGroup(...)、agentColor(...)，
 * 改动最小；而表本身全站只有一份。
 *
 * 为什么不留在这里：`components.js` 也需要同一批映射（任务列表的圆点、
 * 徽章），两处各存一份就一定会漂移 —— 这不是假设，是已经发生过的事故。
 */

const statusGroup = _statusGroup;
const statusLabel = _statusLabel;
const agentColor = _agentColor;

/* ------------------------------------------------------------------ *
 * 图状态（增量更新）
 * ------------------------------------------------------------------ */

/**
 * 从节点数组构建渲染状态。
 *
 * 刻意**只接受整个 nodes 数组**（幂等重建），而不是提供 `applyEvent`：
 * 增量 API 看起来更"高效"，但会引入「事件乱序/丢失导致本地状态与服务端
 * 不一致」的整类 bug，而重画的成本是 O(节点数) —— 真实任务最多几十个节点，
 * 一次 `requestAnimationFrame` 内重建完全无感。
 *
 * 前端只做 **DOM diff（按 id 复用 `<g>` 元素）**，那是真正的性能瓶颈所在，
 * 而不是这里的对象重建。
 */
export function buildGraphState(nodes) {
  const list = Array.isArray(nodes) ? nodes.filter((n) => n && n.id) : [];
  const state = new Map();
  for (const n of list) {
    const status = n.status || 'pending';
    state.set(n.id, {
      id: n.id,
      agentType: n.agent_type || '',
      goal: n.goal || '',
      deps: Array.isArray(n.deps) ? n.deps : [],
      status,
      group: statusGroup(status),
      attempt: Number(n.attempt ?? 1) || 1,
      tokens: Number(n.tokens_used ?? 0) || 0,
      lastError: n.last_error || '',
    });
  }
  return state;
}

/**
 * 把 SSE 事件叠加到图状态上（**乐观更新**）。
 *
 * 用途：SSE 是流式的，而 `GET /tasks/{id}` 只在任务结束时才刷新一次。
 * 若只依赖轮询，用户在执行过程中看不到任何节点变色。因此节点级事件
 * 需要立即反映到图上；任务结束时再用服务端的权威快照整体覆盖。
 *
 * 事件 → 状态映射（与服务端 orchestrator 的 emit 契约**逐字段对齐**）：
 *   node_started  → running   （带 attempt，重跑时 attempt 递增）
 *   node_finished → 按 status 映射（backtracked 就是回退）
 *   node_verdict  → 仅在 reject 时把状态置为 backtracked
 *
 * @returns {boolean} 状态是否真的发生变化（用于跳过无意义的重绘）
 */
export function applyEvent(state, ev) {
  if (!ev || !ev.node_id) return false;
  const cur = state.get(ev.node_id);
  if (!cur) return false; // 未知节点：等下一次快照刷新，不凭空造节点

  let next = null;
  if (ev.kind === 'node_started') {
    next = 'running';
  } else if (ev.kind === 'node_finished') {
    next = ev.status || cur.status;
  } else if (ev.kind === 'node_verdict') {
    if (ev.verdict === 'reject') next = 'backtracked';
    else if (ev.verdict === 'pass') next = 'success';
  }
  if (!next) return false;

  const attempt = Number(ev.attempt ?? cur.attempt) || cur.attempt;
  const changed = next !== cur.status || attempt !== cur.attempt;
  cur.status = next;
  cur.group = statusGroup(next);
  cur.attempt = attempt;
  if (ev.last_error) cur.lastError = ev.last_error;
  if (Number.isFinite(ev.tokens_used)) cur.tokens = Number(ev.tokens_used);
  return changed;
}

/* ------------------------------------------------------------------ *
 * diff 渲染
 * ------------------------------------------------------------------ */

/**
 * 把 unified diff 文本切成带类型标注的行。
 *
 * 只认 `+`/`-`/`@@`/空格 四种行首，其余（如 `\ No newline at end of file`）
 * 归为 `meta`。刻意**不做语法高亮**：diff 的语义就是「增/删/上下文」，
 * 再叠一层语言高亮会让两种颜色体系互相干扰。
 *
 * @param {string} diffText
 * @returns {Array<{type:string, text:string, oldNo:number|null, newNo:number|null}>}
 */
export function parseDiff(diffText) {
  const text = String(diffText ?? '');
  // 空输入返回空数组，而不是「一条空上下文行」。
  // 这不是吹毛求疵：`diffStats('')` 若走这条路径会数出 0 增 0 删，
  // 而 `parseDiff('')` 却给出 1 行 —— 两个函数对同一输入给出矛盾结果，
  // 迟早有人据此写出「空 diff 也算改动」的 bug。
  if (!text) return [];

  const lines = text.split('\n');
  let oldNo = null;
  let newNo = null;
  const out = [];
  for (const raw of lines) {
    let type = 'ctx';
    let text = raw;
    let o = null;
    let n = null;

    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
    if (hunk) {
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[2]);
      out.push({ type: 'hunk', text, oldNo: null, newNo: null });
      continue;
    }
    if (raw.startsWith('+++') || raw.startsWith('---') || raw.startsWith('diff ') || raw.startsWith('index ')) {
      out.push({ type: 'meta', text, oldNo: null, newNo: null });
      continue;
    }
    if (raw.startsWith('+')) {
      type = 'add';
      text = raw.slice(1);
      n = newNo;
      if (newNo !== null) newNo += 1;
    } else if (raw.startsWith('-')) {
      type = 'del';
      text = raw.slice(1);
      o = oldNo;
      if (oldNo !== null) oldNo += 1;
    } else if (raw.startsWith('\\')) {
      out.push({ type: 'meta', text, oldNo: null, newNo: null });
      continue;
    } else {
      // 上下文行：可能是 ` foo` 也可能是裸 `foo`（无行首标记的 diff）
      text = raw.startsWith(' ') ? raw.slice(1) : raw;
      o = oldNo;
      n = newNo;
      if (oldNo !== null) oldNo += 1;
      if (newNo !== null) newNo += 1;
    }
    out.push({ type, text, oldNo: o, newNo: n });
  }
  return out;
}

/** diff 的增删统计（用于在文件标题上标 +n/−m）。 */
export function diffStats(diffText) {
  let add = 0;
  let del = 0;
  for (const line of parseDiff(diffText)) {
    if (line.type === 'add') add += 1;
    else if (line.type === 'del') del += 1;
  }
  return { add, del };
}

/* ------------------------------------------------------------------ *
 * SVG 渲染
 * ------------------------------------------------------------------ */

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * 箭头 marker 的 id 与取色方式。
 *
 * 这些 id 是 `renderDag()` 写进 `marker-end` 的引用（`url(#dag-arrow)` 等）。
 * 曾经只写了引用、没有写 `<marker>` 定义 —— 于是**一条箭头都画不出来**，
 * 而且是静默失败：`url(#不存在)` 在浏览器里不会报错，只是什么都不渲染。
 * 所以定义与引用现在放在同一个文件里，并由 graph.test.js 断言"引用的 id
 * 必须有定义"。
 *
 * 颜色用 `context-stroke`：marker 因此自动跟随所在边的描边色，
 * 边的状态色（成功/失败/回退）改一次，箭头跟着变，不需要第二处配色。
 */
const ARROW_MARKERS = ['dag-arrow', 'dag-arrow-back', 'dag-arrow-done'];

/** 建一次 marker 定义（幂等）。 */
function ensureDefs(svg) {
  if (svg.querySelector('defs.dag-defs')) return;
  const defs = svgEl('defs', { class: 'dag-defs' });
  for (const id of ARROW_MARKERS) {
    const marker = svgEl('marker', {
      id,
      viewBox: '0 0 8 8',
      refX: 7,
      refY: 4,
      markerWidth: 6,
      markerHeight: 6,
      orient: 'auto-start-reverse',
      markerUnits: 'strokeWidth',
    });
    marker.appendChild(svgEl('path', {
      d: 'M0.5 1.2 7 4 0.5 6.8',
      fill: 'context-stroke',
    }));
    defs.appendChild(marker);
  }
  svg.insertBefore(defs, svg.firstChild);
}

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined && v !== '') el.setAttribute(k, String(v));
  }
  return el;
}

/** 单行截断，避免长 goal 撑破节点矩形。 */
function clip(text, max) {
  const s = String(text ?? '');
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/**
 * 渲染/更新整张 DAG 到给定 `<svg>` 元素。
 *
 * 采用**按 id 复用 `<g>` 的增量更新**：节点结构（矩形、文本）只在节点
 * 首次出现时创建，之后仅改 class 与文本。这样每次 SSE 事件触发的重绘
 * 不会重建 DOM，也不会丢失用户的 hover/焦点状态。
 *
 * @param {SVGSVGElement} svg
 * @param {ReturnType<typeof layoutDag>} layout
 * @param {Map<string, object>} state
 * @param {{onSelect?: (id:string)=>void, selected?: string}} [opts]
 */
export function renderDag(svg, layout, state, opts = {}) {
  const { onSelect, selected } = opts;
  const existing = new Map(
    [...svg.querySelectorAll('g.dag-node')].map((g) => [g.dataset.id, g]),
  );

  // 尺寸：用 viewBox + 固定 width/height，让浏览器负责缩放（SVG 原生能力）
  svg.setAttribute('viewBox', `0 0 ${Math.max(layout.width, 1)} ${Math.max(layout.height, 1)}`);
  svg.setAttribute('width', String(Math.max(layout.width, 1)));
  svg.setAttribute('height', String(Math.max(layout.height, 1)));

  let layerNodes = svg.querySelector('g.dag-edges');
  if (!layerNodes) {
    // 边层必须在节点层**之下**：先建边层，再建节点层
    svg.appendChild(svgEl('g', { class: 'dag-edges' }));
    svg.appendChild(svgEl('g', { class: 'dag-nodes' }));
  }
  // 箭头定义必须在写 marker-end 之前就位（否则第一帧的箭头是缺失的）
  ensureDefs(svg);
  const edgeLayer = svg.querySelector('g.dag-edges');
  const nodeLayer = svg.querySelector('g.dag-nodes');

  // ---- 边：整体重建（边数量少且无状态，重建成本可忽略） ----
  edgeLayer.textContent = '';
  for (const edge of layout.edges) {
    const to = state.get(edge.to);
    const cls = ['dag-edge'];
    if (edge.back) cls.push('back');
    if (to && to.group === 'ok') cls.push('done');
    if (to && (to.group === 'fail' || to.group === 'backtrack')) cls.push('bad');
    const attrs = { class: cls.join(' '), d: edge.d, 'data-from': edge.from, 'data-to': edge.to };
    if (edge.back) attrs['marker-end'] = 'url(#dag-arrow-back)';
    else if (to && to.group === 'ok') attrs['marker-end'] = 'url(#dag-arrow-done)';
    else attrs['marker-end'] = 'url(#dag-arrow)';
    edgeLayer.appendChild(svgEl('path', attrs));
  }

  // ---- 节点：按 id 复用 ----
  const seen = new Set();
  for (const pos of layout.nodes) {
    seen.add(pos.id);
    const st = state.get(pos.id) || { status: 'pending', group: 'pending', attempt: 1 };
    let g = existing.get(pos.id);
    if (!g) {
      g = buildNodeGroup(onSelect);
      g.dataset.id = pos.id;
      nodeLayer.appendChild(g);
    }
    g.setAttribute('transform', `translate(${pos.x},${pos.y})`);
    g.setAttribute(
      'class',
      ['dag-node', `s-${st.group}`, pos.id === selected ? 'selected' : ''].filter(Boolean).join(' '),
    );

    // 关键文本/属性：每次都写（幂等，代价极低）
    g.querySelector('.node-accent').setAttribute('fill', agentColor(st.agentType));
    g.querySelector('.node-agent').textContent = clip(st.agentType || st.id, 16);
    g.querySelector('.node-id').textContent = pos.id;
    g.querySelector('.node-goal').textContent = clip(st.goal, 22);
    g.querySelector('.node-status').textContent = st.attempt > 1
      ? `${statusLabel(st.status)} ×${st.attempt}`
      : statusLabel(st.status);
    g.querySelector('.node-tokens').textContent = st.tokens ? `${st.tokens} tok` : '';
    g.setAttribute('aria-label', `${pos.id} ${st.agentType} ${statusLabel(st.status)}`);

    // 回退提示：badge 只在 attempt>1 时出现，避免污染第一轮的视觉
    const badge = g.querySelector('.node-badge');
    badge.style.display = st.attempt > 1 ? '' : 'none';
    badge.querySelector('text').textContent = `×${st.attempt}`;
  }

  // 移除已不存在的节点（例如重新选中了另一个任务）
  for (const [id, g] of existing) {
    if (!seen.has(id)) g.remove();
  }
}

/** 构建一个节点的 `<g>` 子树（只在首次出现时调用）。 */
function buildNodeGroup(onSelect) {
  const g = svgEl('g', { class: 'dag-node', tabindex: '0', role: 'button' });
  // 左侧角色色条：用 rect 而非 CSS border，便于随角色变色
  g.appendChild(svgEl('rect', { class: 'node-accent', x: 0, y: 0, width: 4, height: NODE_H, rx: 2 }));
  g.appendChild(svgEl('rect', { class: 'node-bg', x: 0, y: 0, width: NODE_W, height: NODE_H, rx: 10 }));
  g.appendChild(svgEl('circle', { class: 'node-dot', cx: NODE_W - 16, cy: 16, r: 5 }));

  const agent = svgEl('text', { class: 'node-agent', x: 14, y: 20 });
  const id = svgEl('text', { class: 'node-id', x: NODE_W - 30, y: 20, 'text-anchor': 'end' });
  const goal = svgEl('text', { class: 'node-goal', x: 14, y: 39 });
  const status = svgEl('text', { class: 'node-status', x: 14, y: 55 });
  const tokens = svgEl('text', { class: 'node-tokens', x: NODE_W - 12, y: 55, 'text-anchor': 'end' });
  [agent, id, goal, status, tokens].forEach((t) => g.appendChild(t));

  const badge = svgEl('g', { class: 'node-badge', transform: `translate(${NODE_W - 34},${NODE_H - 24})` });
  badge.appendChild(svgEl('rect', { x: -4, y: -12, width: 34, height: 18, rx: 9, class: 'badge-bg' }));
  badge.appendChild(svgEl('text', { x: 13, y: 1, 'text-anchor': 'middle', class: 'badge-text' }));
  g.appendChild(badge);

  const fire = () => { if (onSelect) onSelect(g.dataset.id); };
  g.addEventListener('click', fire);
  g.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); }
  });
  return g;
}

/* ------------------------------------------------------------------ *
 * 图例
 * ------------------------------------------------------------------ */

/** DAG 图例项，由同一份语义表驱动，避免图例与渲染逻辑漂移。 */
/**
 * 图例条目。
 *
 * 覆盖 `STATUS_GROUP` 的**全部取值**（不是全部状态）——
 * 多个状态可以合并到同一分组，但每个分组都必须在图例里有解释，
 * 否则读者看到一个颜色却查不到它意味着什么。
 */
export const LEGEND_ITEMS = [
  { group: 'pending', label: '待执行' },
  { group: 'ready', label: '待调度' },
  { group: 'running', label: '执行中' },
  { group: 'ok', label: '成功' },
  { group: 'backtrack', label: '回退重跑' },
  { group: 'fail', label: '失败' },
  { group: 'muted', label: '跳过' },
];
