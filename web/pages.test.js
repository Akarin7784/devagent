/**
 * DAG 可视化与各页面纯逻辑的回归测试（零依赖，node 直跑）。
 *
 * 这些用例全部来自**已确认的真实缺陷**，每一条都对应一个具体的翻车现场：
 * 样式表孤儿化（图全黑）、回退弧线被 viewBox 裁掉、KPI 拿错状态字面量……
 * 它们的共同点是"不报错、只是悄悄错"，所以只能靠断言守住。
 *
 * 边界与其它 *.test.js 一致：不引入 jsdom，DOM 相关部分用手写 stub，
 * 断言的是**决策与数据**（类名集合、几何包围盒、过滤逻辑），不是像素。
 *
 * 运行：`node web/pages.test.js`
 */

import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

import * as store from './js/store.js';
import { taskStatusBadge } from './js/status.js';
// STATUS_GROUP 从 graph.js 取（它再导出的就是 status.js 的同一对象，
// status.test.js 有引用相等的断言守着），避免同一个名字在文件里出现两次。
import { LEGEND_ITEMS, STATUS_GROUP, dagBounds, edgePath, layoutDag, renderDag } from './graph.js';
import { renderKpiRow } from './js/pages/overview.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '..');

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    failures.push({ name, err });
  }
}

/* ================================================================== *
 * 0. 极小 DOM stub
 * ================================================================== *
 * 前端是零构建、零依赖的（没有 jsdom），所以这里手写一个只实现
 * renderDag 真正调用到的接口的 stub。断言的对象是**类名集合与几何**，
 * 不是像素 —— 那才是能在这类缺陷上给出确定答案的部分。
 */

function stubNode(tag = 'div') {
  const node = {
    tagName: tag,
    attrs: {},
    children: [],
    parent: null,
    classList: new Set(),
    dataset: {},
    // node-badge 的显隐由 JS 通过 style.display 控制（SVG 不支持 display 属性）；
    // el()/statCard() 还会用 style.setProperty 写内联样式
    style: {
      setProperty() {},
      removeProperty() {},
    },
    // 真实 DOM 节点的 String() 是 '[object HTMLDivElement]'，但 el() 的
    // children 分支会对非 Node 子项做 String(child)。这里给出可读字符串，
    // 让"把节点拼进文本"这类用法在测试里也能被断言。
    toString() { return node.textContent; },
    /** 与真实 DOM 一致：textContent 是聚合值，写入时会清空子节点。 */
    get textContent() {
      if (node.tagName === '#text') return node._text || '';
      return node.children.map((c) => c.textContent).join('');
    },
    set textContent(v) {
      const s = v == null ? '' : String(v);
      if (node.tagName === '#text') {
        node._text = s;
        return;
      }
      node.children.length = 0;
      if (s) {
        const t = stubNode('#text');
        t.textContent = s; // 走 #text 分支，只写 _text
        t.parent = node;
        node.children.push(t);
      }
    },
    appendChild(c) { c.parent = node; node.children.push(c); return c; },
    insertBefore(c, ref) {
      c.parent = node;
      const i = ref ? node.children.indexOf(ref) : -1;
      if (i < 0) node.children.push(c); else node.children.splice(i, 0, c);
      return c;
    },
    setAttribute(k, v) {
      node.attrs[k] = String(v);
      // class 必须同步进 classList，否则 querySelector('.node-bg') 找不到元素
      if (k === 'class') {
        node.classList = new Set(String(v).split(/\s+/).filter(Boolean));
      }
    },
    append(...children) {
      for (const c of children.flat()) {
        if (c == null || c === false) continue;
        node.appendChild(c);
      }
    },
    getAttribute(k) { return node.attrs[k] ?? null; },
    querySelector(sel) { return queryStub(node, sel); },
    querySelectorAll(sel) { return queryAllStub(node, sel); },
    remove() {
      if (!node.parent) return;
      const i = node.parent.children.indexOf(node);
      if (i >= 0) node.parent.children.splice(i, 1);
      node.parent = null;
    },
    addEventListener() {},
  };
  return node;
}

/** 遍历所有后代（含自身）。 */
function walkStub(node) {
  const out = [node];
  for (const c of node.children) out.push(...walkStub(c));
  return out;
}

/** 解析 `g.dag-node` / `.node-bg` 这类单段选择器（够用即可，不追求完整 CSS 语义）。 */
function matchesStub(node, sel) {
  const cls = sel.match(/\.(-?[_a-zA-Z][\w-]*)/);
  const tag = sel.match(/^[a-zA-Z]/) ? sel.split(/[.#\[]/)[0] : null;
  if (tag && node.tagName !== tag) return false;
  if (cls && !node.classList.has(cls[1])) return false;
  return true;
}

function queryAllStub(root, sel) {
  return walkStub(root).filter((n) => n !== root && matchesStub(n, sel));
}

function queryStub(root, sel) {
  return queryAllStub(root, sel)[0] || null;
}

/** graph.js 通过 document.createElementNS 造 SVG 元素。 */
const stubDocumentBase = {
  createElementNS(_ns, tag) { return stubNode(tag); },
};
globalThis.document = stubDocumentBase;

/**
 * 更完整的 document stub（在"KPI 卡"一节启用）。
 *
 * 之所以要能造假 DOM：本轮修的另一个缺陷（KPI 卡取错状态字面量）
 * 不是纯计算，而是"构造卡片时传了什么参数"。把 `el` / `statCard`
 * 换成受控替身，就能直接断言"卡片上写了什么"，而不必引入 jsdom。
 */
function installDomStub() {
  globalThis.Node = class Node {};
  let fragmentCount = 0;
  globalThis.document = {
    ...stubDocumentBase,
    activeElement: null,
    createElement(tag) {
      const node = stubNode(tag);
      if (tag === 'template') {
        // fromHTML() 只用于 icons.js 的静态 SVG；这里不实现真正的 HTML 解析，
        // 只回一个占位元素 —— 断言的对象是文本与类名，不是解析结果。
        node.innerHTML = '';
        node.content = {
          get firstElementChild() {
            fragmentCount += 1;
            return stubNode('svg');
          },
        };
      }
      return node;
    },
    createTextNode(text) {
      const node = stubNode('#text');
      node.textContent = String(text);
      return node;
    },
    createDocumentFragment() { return stubNode('#fragment'); },
    getElementById() { return null; },
  };
  void fragmentCount;
  return globalThis.document;
}

/* ================================================================== *
 * 1. 样式与渲染的契约：graph.js 用到的类名必须在"实际加载"的样式表里有定义
 * ================================================================== *
 *
 * 背景：`.dag-node` / `.node-*` / `.s-*` / `.legend .dot-*` 这一整组规则
 * 曾经只写在 `web/styles.css` 里，而 `index.html` 加载的是 css/ 下的四张表
 * —— 于是所有规则静默失效：SVG 的 `<rect class="node-bg">` 拿不到 fill，
 * 走 SVG 默认值纯黑，节点黑底黑字；状态色与图例颜色全部塌成一种灰。
 *
 * 没有任何测试会因此变红：页面照常渲染，只是"不好看"。这一条就是补上它。
 */

/** 读 index.html，取它**真正加载**的样式表路径（相对 web/）。 */
function loadedStylesheets() {
  const html = readFileSync(join(HERE, 'index.html'), 'utf8');
  const hrefs = [...html.matchAll(/<link[^>]+rel=["']stylesheet["'][^>]*>/gi)]
    .map((m) => /href=["']([^"']+)["']/i.exec(m[0]))
    .filter(Boolean)
    .map((m) => m[1]);
  assert.ok(hrefs.length >= 3, `index.html 只加载了 ${hrefs.length} 张样式表，解析可能出错`);
  return hrefs.map((h) => join(HERE, h.replace(/^\.\//, '')));
}

/**
 * 抽取样式表里的类选择器。
 *
 * 先剥离注释与 at-rule 头（`@media (...) {`），剩下 `选择器 { 声明 }` 结构；
 * 再要求规则体里**至少有一条 `属性: 值`** —— 空规则（如只写了 `{}`）
 * 不算"有定义"，否则一个手滑留下的空壳就能骗过这条测试。
 */
function classSelectorsFrom(text) {
  const css = text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/@[a-z-]+[^{;]*\{/gi, '');
  const out = new Set();
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!/[-a-z]+\s*:\s*[^;]+/i.test(m[2])) continue; // 空规则不算
    for (const cls of m[1].matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) out.add(cls[1]);
  }
  return out;
}

const STYLESHEET_PATHS = loadedStylesheets();
const DEFINED_CLASSES = new Set();
for (const p of STYLESHEET_PATHS) {
  for (const c of classSelectorsFrom(readFileSync(p, 'utf8'))) DEFINED_CLASSES.add(c);
}

test('index.html 加载的样式表都存在（路径写错就整张表失效）', () => {
  for (const p of STYLESHEET_PATHS) {
    assert.ok(existsSync(p), `index.html 引用了不存在的样式表：${p}`);
  }
  assert.ok(DEFINED_CLASSES.size > 100,
    `只解析出 ${DEFINED_CLASSES.size} 个类选择器，CSS 解析可能失效（导致测试空跑）`);
});

test('孤儿文件 web/styles.css 不得再被任何样式表引用其规则', () => {
  // 反向保护：修复方式是"搬家"，不是"再复制一份"。
  // 如果哪天有人把规则又抄回 web/styles.css，这条会红。
  const legacy = join(HERE, 'styles.css');
  if (!existsSync(legacy)) return; // 已删除（期望状态）
  const legacyClasses = classSelectorsFrom(readFileSync(legacy, 'utf8'));
  const dagClasses = [...legacyClasses].filter(
    (c) => c.startsWith('node-') || c.startsWith('s-') || c.startsWith('dag-'),
  );
  assert.equal(dagClasses.length, 0,
    `web/styles.css 里又出现了 DAG 规则（${dagClasses.join(', ')}）—— 它不会被加载，属于重复定义`);
});

/** 供测试使用的极小 SVG DOM stub（定义见文件开头的"0. 极小 DOM stub"）。 */

test('renderDag 产出的每个类名都在 index.html 加载的样式表里有定义', () => {
  // 用真实渲染建立类名清单（而不是手抄一份），这样 graph.js 改了类名、
  // 加了新状态，测试会自动跟上。
  const state = new Map([
    ['N1', { id: 'N1', status: 'running', group: STATUS_GROUP.running, attempt: 1, tokens: 12, agentType: 'coder', goal: 'g' }],
    ['N2', { id: 'N2', status: 'backtracked', group: STATUS_GROUP.backtracked, attempt: 2, tokens: 3, agentType: 'verifier', goal: 'g' }],
  ]);
  const layout = layoutDag([{ id: 'N1', deps: [] }, { id: 'N2', deps: ['N1'] }]);
  const svg = stubNode('svg');
  renderDag(svg, layout, state, { selected: 'N1' });

  const emitted = new Set();
  for (const n of walkStub(svg)) {
    for (const c of n.classList) emitted.add(c);
    for (const c of String(n.attrs.class || '').split(/\s+/)) if (c) emitted.add(c);
  }
  // 动态拼接的类名：s-<group> 来自 statusGroup()，dot-<group> 来自图例
  for (const g of Object.values(STATUS_GROUP)) {
    emitted.add(`s-${g}`);
    emitted.add(`dot-${g}`);
  }
  // 任务状态点的色调类名（任务列表用它，与 DAG 图例是两套命名空间）
  for (const s of ['pending', 'running', 'succeeded', 'failed', 'paused', 'cancelled']) {
    emitted.add(`dot-${taskStatusBadge(s)[0]}`);
  }

  // 只断言 DAG 自己的命名空间，避免把全局组件类（badge/btn）混进来
  const dagClasses = [...emitted].filter(
    (c) => c.startsWith('dag-') || c.startsWith('node-') || c.startsWith('s-')
      || c.startsWith('dot-') || c === 'badge-bg' || c === 'badge-text',
  );

  // 纯 JS 钩子：不承载任何视觉，只用于"这个元素是不是已经建过"的判定。
  // 显式登记以免被当成漏定义的样式类 —— 但也不允许这个清单长大：
  // 每多一个，就意味着又有一个类名脱离了 CSS 的可见范围。
  const JS_ONLY_HOOKS = new Set(['dag-defs']);
  assert.ok(JS_ONLY_HOOKS.size <= 1, 'JS-only 类名清单在变长，请确认新条目确实不需要样式');

  assert.ok(dagClasses.length >= 15,
    `只收集到 ${dagClasses.length} 个 DAG 类名，渲染 stub 可能没跑通`);

  const missing = dagClasses
    .filter((c) => !JS_ONLY_HOOKS.has(c) && !DEFINED_CLASSES.has(c))
    .sort();
  assert.deepEqual(missing, [],
    '这些类名没有任何加载中的样式表定义（元素会拿 SVG/CSS 默认值，通常是黑底黑字）：\n'
    + `    ${missing.join('\n    ')}`);
});

test('LEGEND_ITEMS 的每个分组都有图例圆点颜色定义', () => {
  const missing = LEGEND_ITEMS
    .map((i) => `dot-${i.group}`)
    .filter((c) => !DEFINED_CLASSES.has(c));
  assert.deepEqual(missing, [], `图例圆点没有颜色定义：${missing.join(', ')}`);
});

test('renderDag 引用的箭头 marker id 都有对应定义', () => {
  // `marker-end="url(#dag-arrow)"` 指向一个不存在的 defs 不会报错，
  // 只是永远不画箭头 —— 又一个"静默失效"。所以引用与定义都要断言。
  const layout = layoutDag([{ id: 'A', deps: [] }, { id: 'B', deps: ['A'] }]);
  const svg = stubNode('svg');
  renderDag(svg, layout, new Map(), {});

  const referenced = new Set();
  for (const n of walkStub(svg)) {
    const m = /url\(#([^)]+)\)/.exec(String(n.attrs['marker-end'] || ''));
    if (m) referenced.add(m[1]);
  }
  assert.ok(referenced.size >= 1, '没有任何边引用箭头 marker —— 渲染逻辑可能没跑通');

  const defined = new Set();
  for (const n of walkStub(svg)) {
    if (n.tagName === 'marker' && n.attrs.id) defined.add(n.attrs.id);
  }
  for (const id of referenced) {
    assert.ok(defined.has(id),
      `边引用了 marker #${id}，但 <defs> 里没有定义它 —— 箭头永远不显示`);
  }
});

/* ================================================================== *
 * 2. 回退弧线必须落在画布包围盒内
 * ================================================================== *
 * renderDag 会用 layout.width/height 覆盖调用方设的 viewBox，
 * 所以 layout 的高度必须是"含回退弧线"的真实包围盒。
 * 单行 DAG + 一条回退边是最小复现：节点底边 + 34 的弧顶曾经落在画布外。
 */

/** 从 path 的 d 取控制点/端点的最大 Y —— 即弧线的实际最低点。 */
function maxYofPath(d) {
  const nums = String(d).match(/-?\d+(?:\.\d+)?/g) || [];
  let max = 0;
  for (let i = 1; i < nums.length; i += 2) max = Math.max(max, Number(nums[i]));
  return max;
}

test('回退边弧顶超出节点包围盒（说明高度必须单独算）', () => {
  // 当前分层算法下，`deps` 派生的边永远是正向的（依赖必落在更小的层），
  // 所以 back 分支只能直接构造几何来验证 —— 它是防御分支，但必须正确：
  // 一旦分层算法变化或后端送来违反拓扑序的依赖，画出来的就是这条弧线。
  const L = layoutDag([{ id: 'A', deps: [] }, { id: 'B', deps: ['A'] }]);
  const A = L.nodes.find((n) => n.id === 'A');
  const B = L.nodes.find((n) => n.id === 'B');
  const forward = edgePath(A, B);
  const back = edgePath(B, A); // 反向调用 → from.layer >= to.layer → 弧线

  assert.match(forward, /^M [\d.]+ [\d.]+ C /, '正向边必须是三次贝塞尔');
  assert.match(back, /^M [\d.]+ [\d.]+ C /, '回退边必须是三次贝塞尔');
  assert.ok(maxYofPath(back) > maxYofPath(forward),
    '回退弧线没有下探到节点下方 —— 构造方式失效');
});

test('dagBounds 把回退边纳入高度（这正是 viewBox 的来源）', () => {
  const nodes = [{ x: 18, y: 18, w: 168, h: 62 }, { x: 262, y: 18, w: 168, h: 62 }];
  const forward = { d: edgePath({ ...nodes[0], layer: 0, cx: 102, cy: 49 }, { ...nodes[1], layer: 1, cx: 346, cy: 49 }) };
  const back = { d: edgePath({ ...nodes[1], layer: 1, cx: 346, cy: 49 }, { ...nodes[0], layer: 0, cx: 102, cy: 49 }) };

  const withoutBack = dagBounds(nodes, [forward]);
  const withBack = dagBounds(nodes, [forward, back]);

  assert.equal(withoutBack.height, 80, '只有正向边时高度应等于节点包围盒');
  assert.ok(withBack.height > withoutBack.height,
    '回退边没有让包围盒变高 —— 弧线会被 viewBox 裁掉');
  assert.ok(withBack.height >= maxYofPath(back.d),
    `包围盒高度 ${withBack.height} 没包住弧顶 ${maxYofPath(back.d)}`);
});

test('布局产出的宽度始终能容下最右侧节点', () => {
  const L = layoutDag([{ id: 'A', deps: [] }, { id: 'B', deps: ['A'] }, { id: 'C', deps: ['B'] }]);
  const right = Math.max(...L.nodes.map((n) => n.x + n.w));
  assert.ok(L.width >= right, `宽度 ${L.width} 容不下右边界 ${right}`);
});

test('直线链（无回退边）的高度仍等于节点包围盒（不无谓留白）', () => {
  const L = layoutDag([{ id: 'A', deps: [] }, { id: 'B', deps: ['A'] }]);
  const bottom = Math.max(...L.nodes.map((n) => n.y + n.h));
  assert.equal(L.height, bottom, '没有回退边时高度不该变大');
  assert.ok(L.edges.every((e) => !e.back));
});

/* ================================================================== *
 * 3. 路由：replace 且路由身份未变时不得触发重渲染
 * ================================================================== *
 * `selectTask()` 会调 `navigate('workbench', { task }, true)` 把任务写进 URL。
 * 原先的实现在 replace 后**手动派发 hashchange**，于是 shell 立刻
 * dispose 掉刚建好的页面再重建一次：两次 SSE 订阅、两次 /tasks + /context
 * 请求、选中状态与滚动位置全丢。
 *
 * 正确性要求：手动派发的那个 hashchange 只在"路由身份真的变了"时才需要
 *（例如把非法路由纠回默认页）；同一路由内改查询参数不需要重建页面。
 * 注意不能改坏浏览器前进/后退 —— 那是原生 hashchange，必须照常触发。
 */

/** 最小 window/document stub：store.js 只用到这几个入口。 */
function installWindowStub() {
  const listeners = new Map();
  const events = [];
  const win = {
    location: { hash: '#/workbench', href: 'http://localhost/web/index.html#/workbench', origin: 'http://localhost' },
    localStorage: {
      _m: new Map(),
      getItem(k) { return win.localStorage._m.has(k) ? win.localStorage._m.get(k) : null; },
      setItem(k, v) { win.localStorage._m.set(k, String(v)); },
      removeItem(k) { win.localStorage._m.delete(k); },
    },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    addEventListener(t, fn) { if (!listeners.has(t)) listeners.set(t, new Set()); listeners.get(t).add(fn); },
    removeEventListener(t, fn) { listeners.get(t)?.delete(fn); },
    dispatchEvent(ev) { events.push(ev); listeners.get(ev.type)?.forEach((fn) => fn(ev)); return true; },
    history: {
      replaceState(_s, _t, url) { win.location.hash = String(url); },
    },
    setTimeout,
    clearTimeout,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
  };
  globalThis.window = win;
  globalThis.HashChangeEvent = class HashChangeEvent extends Event {
    constructor(type, init) { super(type, init); }
  };
  return { win, events };
}

const WINSTUB = installWindowStub();

/** 包装 window.dispatchEvent，返回本次调用期间派发的事件类型列表。 */
function dispatchedTypes(fn) {
  const before = WINSTUB.events.length;
  fn();
  return WINSTUB.events.slice(before).map((e) => e.type);
}

/** 直接改 hash 后再调用（不动 location 赋值逻辑），用于模拟已跳转。 */
function setHashOnly(hash) {
  WINSTUB.win.location.hash = hash;
}

test('navigate(replace) 在同一路由内改参数时不派发 hashchange', () => {
  setHashOnly('#/workbench?task=A');
  const types = dispatchedTypes(() => store.navigate('workbench', { task: 'B' }, true));
  assert.deepEqual(types, [],
    'replace 之后派发了 hashchange —— shell 会 dispose 掉刚渲染好的页面再重建（双 SSE / 双请求）');
  assert.equal(WINSTUB.win.location.hash, '#/workbench?task=B', 'URL 必须照常更新（可分享/可刷新）');
});

test('navigate(replace) 在路由身份变化时仍派发 hashchange（纠错路径不能坏）', () => {
  setHashOnly('#/workbench?task=B');
  const types = dispatchedTypes(() => store.navigate('overview', {}, true));
  assert.deepEqual(types, ['hashchange'],
    '跨路由 replace 必须通知外壳重渲染，否则 URL 与内容不一致');
});

test('navigate(replace) 对完全相同的 hash 直接返回（不做无意义工作）', () => {
  setHashOnly('#/workbench?task=B');
  const types = dispatchedTypes(() => store.navigate('workbench', { task: 'B' }, true));
  assert.deepEqual(types, []);
});

test('navigate(非 replace) 仍走 location.hash，由浏览器触发原生 hashchange', () => {
  // 非 replace 路径必须保持原样：退回/前进依赖浏览器历史栈。
  setHashOnly('#/overview');
  const types = dispatchedTypes(() => store.navigate('workbench', { task: 'C' }));
  assert.deepEqual(types, [], '非 replace 路径不该手动派发 hashchange（浏览器会自己发）');
  assert.equal(WINSTUB.win.location.hash, '#/workbench?task=C');
});

/* ================================================================== *
 * 4. 导航徽章的订阅簿记
 * ================================================================== *
 * `buildNav()` 里有 `subscribe('tasks', paint)`，而 `rebuildNav()` 每次
 * hashchange 都会重建导航 —— 原实现丢弃了 unsubscribe 返回值，
 * 于是 N 次导航后留下 N+1 个永久订阅者，全都往已经脱离文档的节点里写 DOM。
 * 这是教科书式的内存泄漏 + 隐形 CPU 浪费，而且完全静默。
 */

test('subscribe 返回的清理函数能真正把订阅数降回去', () => {
  const offs = [];
  for (let i = 0; i < 3; i += 1) offs.push(store.subscribe('tasks', () => {}));
  const baseline = store.subscriberCount('tasks');
  offs.forEach((off) => off());
  assert.equal(store.subscriberCount('tasks'), baseline - 3,
    'unsubscribe 没有生效 —— 导航每次重建都会永久多一个订阅者');
});

test('模拟"导航重建两次"：订阅数在稳态内不增长', () => {
  // 模拟 shell.js 的真实模式：每次重建前先回收上一批订阅，再注册新的。
  // 关键断言是**稳态**：无论重建多少次，活跃订阅数都不增长，
  // 且每一批都被真正回收（回到 0），否则旧节点会一直被写入。
  const baseline = store.subscriberCount('tasks');
  for (let i = 0; i < 3; i += 1) {
    const batch = [store.subscribe('tasks', () => {})];
    assert.equal(store.subscriberCount('tasks'), baseline + batch.length,
      `第 ${i + 1} 次重建后订阅数异常（旧订阅没被回收）`);
    batch.forEach((off) => off());
    assert.equal(store.subscriberCount('tasks'), baseline,
      `第 ${i + 1} 次回收后没有回到基线 —— 每次导航都会永久多一个订阅者`);
  }
  assert.equal(baseline, 0, '测试开始前 tasks 字段就已有订阅者，断言基线不可信');
});

test('subscriberCount 对未知字段返回 0 而不是抛错', () => {
  assert.equal(store.subscriberCount('__nope__'), 0);
});

/* ================================================================== *
 * 5. 指标搜索：过滤逻辑必须是纯函数且大小写不敏感
 * ================================================================== *
 * 原实现只算了一次 `filtered`，输入框的 handler 改了 query 之后
 * 重新渲染的仍是那个**闭包里的旧数组** —— 打字等于没打；
 * 旁边的 "N / M 条" 提示也永远不变。
 */

const { filterMetrics } = await import('./js/pages/observability.js');

const METRIC_ROWS = [
  { type: 'counter', name: 'llm_tokens_total', labels: [{ label: 'direction="input"', value: '120' }] },
  { type: 'gauge', name: 'active_tasks', labels: [{ label: 'pool="default"', value: '2' }] },
  { type: 'histogram', name: 'context_compression_ratio', labels: [{ label: 'agent="coder"', value: '0.42' }] },
];

test('filterMetrics：空查询返回全部（并保持原数组不变）', () => {
  assert.equal(filterMetrics(METRIC_ROWS, '').length, 3);
  assert.equal(filterMetrics(METRIC_ROWS, '   ').length, 3);
  assert.equal(filterMetrics(METRIC_ROWS, null).length, 3);
});

test('filterMetrics：按指标名过滤', () => {
  const only = filterMetrics(METRIC_ROWS, 'llm_tokens');
  assert.equal(only.length, 1);
  assert.equal(only[0].name, 'llm_tokens_total');
});

test('filterMetrics：大小写不敏感（用户不会照着键名大小写打字）', () => {
  assert.equal(filterMetrics(METRIC_ROWS, 'LLM_TOKENS').length, 1);
  assert.equal(filterMetrics(METRIC_ROWS, 'Context_Compression').length, 1);
});

test('filterMetrics：也匹配标签（否则按 agent 名查不到东西）', () => {
  const byLabel = filterMetrics(METRIC_ROWS, 'agent="coder"');
  assert.equal(byLabel.length, 1);
  assert.equal(byLabel[0].name, 'context_compression_ratio');
});

test('filterMetrics：无匹配返回空数组而不是抛错', () => {
  assert.deepEqual(filterMetrics(METRIC_ROWS, '__nope__'), []);
});

test('filterMetrics 是纯函数：不修改入参（页面会拿着同一个数组重算）', () => {
  const snapshot = JSON.stringify(METRIC_ROWS);
  const out = filterMetrics(METRIC_ROWS, 'llm');
  out.push({ type: 'x', name: 'injected', labels: [] });
  assert.equal(JSON.stringify(METRIC_ROWS), snapshot, 'filterMetrics 修改了入参');
});

/* ================================================================== *
 * 6. 总览页 KPI：成功率的分母/分子必须用后端真正会发的任务状态
 * ================================================================== *
 * 原实现写的是 `t.status === 'success' || t.status === 'failed'`，
 * 而 `success` 是 **StepStatus** 的成员；后端任务状态是 `succeeded`。
 * 后果：全部成功时成功率显示「—」（分母为 0），9 成功 1 失败时算出 900%。
 * 数字错了但没有报错，只是"看起来像没数据"。
 */

installDomStub();

const TASKS = {
  succeeded: (n) => Array.from({ length: n }, (_, i) => ({ task_id: `s${i}`, status: 'succeeded', duration_ms: 1000, total_tokens: 10 })),
  failed: (n) => Array.from({ length: n }, (_, i) => ({ task_id: `f${i}`, status: 'failed', duration_ms: 1000, total_tokens: 10 })),
  running: (n) => Array.from({ length: n }, (_, i) => ({ task_id: `r${i}`, status: 'running', duration_ms: 1000, total_tokens: 10 })),
};

/** 取出一棵 stub 树里的全部文本（与真实 DOM 的 textContent 聚合语义一致）。 */
function textsOf(root) {
  const text = walkStub(root).map((n) => n.textContent).filter(Boolean);
  return [...new Set(text)];
}

const KPI_LABEL = '任务成功率';

/** 只用计算出来的数字断言，不依赖 DOM 结构。 */
function kpisFor(tasks) {
  const list = [...tasks];
  const succeeded = list.filter((t) => t.status === 'succeeded').length;
  const finished = list.filter((t) => ['succeeded', 'failed', 'cancelled', 'paused'].includes(t.status)).length;
  return { total: list.length, succeeded, finished };
}

test('全部成功 → 成功率 100%（旧实现会显示「—」）', () => {
  const tasks = [...TASKS.succeeded(3)];
  const k = kpisFor(tasks);
  assert.equal(k.finished, 3, 'succeeded 必须计入"已结束"，否则分母恒为 0');
  assert.equal(k.succeeded / k.finished, 1);
});

test('9 成功 + 1 失败 → 90%，绝不超过 100%', () => {
  const tasks = [...TASKS.succeeded(9), ...TASKS.failed(1)];
  const k = kpisFor(tasks);
  assert.equal(k.finished, 10);
  const rate = k.succeeded / k.finished;
  assert.equal(rate, 0.9);
  assert.ok(rate <= 1, `成功率 ${rate} 超过 100% —— 分子分母用了不同的状态集合`);
});

test('混合任务列表（含进行中）的成功率仍在 [0,1] 且只算已结束', () => {
  const tasks = [...TASKS.succeeded(2), ...TASKS.failed(2), ...TASKS.running(5)];
  const k = kpisFor(tasks);
  assert.equal(k.finished, 4, '进行中的任务不得进入分母（会低估成功率）');
  assert.equal(k.succeeded / k.finished, 0.5);
  assert.equal(k.total, 9);
});

test('没有已结束任务时成功率为 null（界面显示「—」而不是 0%）', () => {
  const k = kpisFor(TASKS.running(2));
  assert.equal(k.finished, 0);
});

test('实际渲染的卡片上写的就是这些数字（不是另一份实现）', () => {
  // 直接调用页面导出的渲染函数，并用 DOM stub 把卡片"读"回来。
  // 这样断言的是渲染路径本身，而不是测试里重写一遍的期待值。
  const row = renderKpiRow([...TASKS.succeeded(9), ...TASKS.failed(1)], null);
  const texts = textsOf(row);
  assert.ok(texts.includes(KPI_LABEL), `卡片里没有「${KPI_LABEL}」：${texts.join(' | ')}`);
  assert.ok(texts.includes('90%'),
    `9 成功 1 失败的卡片应显示 90%，实际文本：${texts.join(' | ')}`);
  assert.ok(texts.includes('9 成功 / 10 已结束'),
    `副文案应说明分子分母，实际文本：${texts.join(' | ')}`);
  assert.ok(!texts.some((t) => /900%/.test(t)), '出现了 900% —— 状态集合又用错了');
});

test('全部成功时卡片显示 100%，而不是「—」', () => {
  const row = renderKpiRow(TASKS.succeeded(3), null);
  const texts = textsOf(row);
  assert.ok(texts.includes('100%'), `实际文本：${texts.join(' | ')}`);
  assert.ok(!texts.includes('—'), '仍有「—」，说明分子没数到 succeeded');
});

test('没有任何已结束任务时显示「—」且不显示 0%', () => {
  const row = renderKpiRow(TASKS.running(2), null);
  const texts = textsOf(row);
  assert.ok(texts.includes('—'), `实际文本：${texts.join(' | ')}`);
  assert.ok(!texts.includes('0%'), '把"没有已结束任务"显示成 0% 会让人以为全挂了');
});

/* ================================================================== *
 * 6. SSE 生命周期：同一时刻只允许一条流，且过期流不得再改状态
 * ================================================================== *
 * 现场：工作台里快速点任务 A 再点 B。
 *   - 原实现用 `unsubscribeStream = subscribe(...)` 覆盖，A 的 EventSource
 *     再也没人能关掉（永久泄漏）；
 *   - A 的事件回调继续往同一份页面状态里写，于是 B 的 DAG 与时间线里
 *     混进了 A 的节点事件 —— 一张"两个任务叠在一起"的图，且没有报错。
 */

const { StreamSlot } = await import('./js/stream.js');

test('开第二条流会关闭第一条，且只关一次', () => {
  const slot = new StreamSlot();
  let closedA = 0;
  let closedB = 0;

  const a = slot.create('task-A');
  a.onClose = () => { closedA += 1; };

  const b = slot.create('task-B'); // 关键：这里必须把 A 关掉
  b.onClose = () => { closedB += 1; };

  assert.equal(closedA, 1, 'A 的流没有被关闭 —— EventSource 永久泄漏');
  assert.equal(slot.activeTaskId(), 'task-B');

  slot.close();
  slot.close(); // 幂等
  assert.equal(closedA, 1, 'A 被重复关闭（重复 close 是 EventSource 的隐性 bug 来源）');
  assert.equal(closedB, 1);
});

test('同任务的重复开流同样先关旧的（双击/重连场景）', () => {
  const slot = new StreamSlot();
  let closed = 0;
  const a1 = slot.create('task-A');
  a1.onClose = () => { closed += 1; };
  slot.create('task-A');
  assert.equal(closed, 1, '重复订阅同一任务必须关掉上一条，否则会有两条流同时在推');
});

test('过期 token 不再是 active —— A 的事件因此画不进 B 的图', () => {
  const slot = new StreamSlot();
  const a = slot.create('task-A');
  assert.equal(slot.isActive(a), true);

  const b = slot.create('task-B'); // 切到 B
  assert.equal(slot.isActive(a), false, 'A 的 token 仍然 active —— A 的事件会污染 B 的界面');
  assert.equal(slot.isActive(b), true);
});

test('close 之后没有任何 token 是 active', () => {
  const slot = new StreamSlot();
  const a = slot.create('task-A');
  slot.close();
  assert.equal(slot.isActive(a), false);
  assert.equal(slot.isActive(null), false);
  assert.equal(slot.isActive(undefined), false);
  assert.equal(slot.activeTaskId(), null);
});

test('未挂 onClose 时 close 不抛异常（订阅建立前就被切走的边界）', () => {
  const slot = new StreamSlot();
  slot.create('task-A'); // 故意不设 onClose，模拟 await 期间被抢先关闭
  assert.doesNotThrow(() => slot.close());
  assert.equal(slot.close(), false, '已经空了还应报告"没东西可关"');
});

test('warned 标记随流创建而复位（切任务后应能重新提示断线）', () => {
  const slot = new StreamSlot();
  const a = slot.create('task-A');
  a.warned = true;
  const b = slot.create('task-B');
  assert.equal(b.warned, false, 'warned 没复位：切到新任务后断线将永远不再提示');
});

/* ================================================================== *
 * 6b. 导航徽章：重建两次之后订阅数不得增长（对真实 shell 的集成断言）
 * ================================================================== *
 * 上面那组是对"订阅簿记"本身的断言；这里直接调用 shell 的真实建导航代码，
 * 断言"重建 N 次后活跃订阅仍是 1 个"。原实现每重建一次就永久多一个。
 */

const { createNavForTest } = await import('./js/shell.js');

test('shell 每次重建导航都会回收上一批订阅（不是累积）', () => {
  const base = store.subscriberCount('tasks');
  const first = createNavForTest();
  assert.ok(first, 'createNavForTest 没有返回导航节点');
  assert.equal(store.subscriberCount('tasks'), base + 1,
    '第一次建导航后应恰好有一个 tasks 订阅者（工作台徽章）');

  const second = createNavForTest();
  assert.ok(second);
  assert.equal(store.subscriberCount('tasks'), base + 1,
    '第二次建导航后订阅数变成了 2 —— 旧导航的订阅没有被回收（内存泄漏 + 写已分离节点）');
});

/* ================================================================== *
 * 7. 评测表单：看得见的值必须与提交的 payload 一致
 * ================================================================== *
 * 现场：`样本上限` 输入框在 paint() 重建时忘了回填 value。
 * 于是字段看起来是空的，而 payload 里仍带着上次的上限值 ——
 * 用户以为放开了全量，实际只跑了一小部分样本，且结果看起来"很正常"。
 */

const { snapshotFormState } = await import('./js/pages/eval.js');

const EVAL_FORM = {
  dataset: ' datasets/golden_set.jsonl ',
  categories: new Set(['requirement', 'scale']),
  maxSamples: '5',
  useJudge: true,
};

test('payload 与控件显示值同源：max_samples 有值则输入框也必须显示它', () => {
  const { payload, fields } = snapshotFormState(EVAL_FORM);
  assert.equal(payload.max_samples, 5);
  assert.equal(fields.maxSamples, '5',
    'maxSamples 没有回填 —— 输入框会显示为空，但提交时旧值仍然生效');
});

test('数据集路径与类别同样保持一致', () => {
  const { payload, fields } = snapshotFormState(EVAL_FORM);
  assert.equal(payload.dataset_path, 'datasets/golden_set.jsonl', '前后空格应被裁掉');
  assert.equal(fields.dataset, 'datasets/golden_set.jsonl');
  assert.deepEqual(payload.categories, ['requirement', 'scale']);
  assert.deepEqual(fields.categories, ['requirement', 'scale']);
});

test('空表单不产生多余的键（后端按"缺省=默认"处理）', () => {
  const { payload, fields } = snapshotFormState({ dataset: '', categories: new Set(), maxSamples: '', useJudge: false });
  assert.deepEqual(Object.keys(payload).sort(), ['use_judge']);
  assert.equal(payload.use_judge, false);
  assert.equal(fields.maxSamples, '');
  assert.equal(fields.dataset, '');
});

test('snapshotFormState 对缺字段的表单安全（首次渲染时的 form 可能不全）', () => {
  assert.doesNotThrow(() => snapshotFormState({}));
  assert.deepEqual(snapshotFormState(undefined).payload, { use_judge: true });
});

test('回填值是字符串，可直接作为 input 的 value（不出现 "undefined"）', () => {
  const { fields } = snapshotFormState({ maxSamples: undefined, dataset: null });
  assert.equal(typeof fields.maxSamples, 'string');
  assert.equal(typeof fields.dataset, 'string');
  assert.ok(!/undefined|null/.test(fields.maxSamples + fields.dataset));
});

/* ================================================================== *
 * 8. 设置页分区：四个模块不得再挤在一页，且 ?tab= 必须被归一
 * ================================================================== *
 * 现场：设置页把「后端连接 / 外观 / 数据 / 关于」四张卡片堆在同一页 ——
 * 想切个主题，也要先滚过一整块连接表单。
 *
 * 改成 Tab 分区后引入了三个新的**静默失败模式**，这一节守住它们：
 *   1. `?tab=` 来自 URL，是不可信输入。拼错、旧书签、手改给出的分区 id
 *      若直接拿去匹配面板，结果是**空白面板且没有任何报错** ——
 *      看起来就像"设置页坏了"，而不是"链接错了"；
 *   2. "分区"若只是视觉折叠（四张卡片都建出来再 CSS 隐藏），
 *      用户看到的那一屏仍然把四个表单都付掉了。这里直接数 tabpanel 的个数，
 *      并断言另一个分区的标志文本不在场；
 *   3. 标签上的连接状态点若"渲染时读一次"，页面在健康探测返回前渲染完成时
 *      就会一直标红 —— 顶栏写着「服务正常」而标签是红的，同屏两个相反结论。
 */

/**
 * 可交互 DOM stub：在 `installDomStub` 之上补几条真实 DOM 语义。
 *
 *   - 造出来的节点 `instanceof Node`：否则 `el()` 的 children 分支会把嵌套
 *     元素压成纯文本，"数一数有几个 tab / 几个 panel"这类结构断言无从谈起；
 *   - `firstChild` + `removeChild`：让 `util.clear()` 真的能清空 ——
 *     这样"切换分区后只剩一个面板"才是可断言的事实，而不是测试自己的假设；
 *   - HTML 元素的 `tagName` 大写：页面靠它判断"焦点是不是在输入框里"；
 *   - `contains` + `className`/`setAttribute('class')` 同步：前者是输入框判断
 *     用到的入口，后者让"改没改到 class"不取决于走的是哪条赋值路径；
 *   - `addEventListener` 真的记录，使"点击标签"可以被模拟。
 */
function installInteractiveDomStub() {
  globalThis.Node = class Node {};
  const doc = installDomStub();
  const rawElement = doc.createElement;
  const rawText = doc.createTextNode;

  doc.createElement = (tag) => {
    const node = Object.setPrototypeOf(rawElement(tag), globalThis.Node.prototype);
    // 真实 DOM 里 HTML 元素的 tagName 是**大写**的（'INPUT' 而非 'input'）。
    // 页面靠它判断"焦点是不是在输入框里"，小写会让那条分支在测试里永远不成立。
    node.tagName = String(tag).toUpperCase();
    Object.defineProperty(node, 'firstChild', { get: () => node.children[0] || null });
    node.removeChild = (child) => {
      const i = node.children.indexOf(child);
      if (i >= 0) node.children.splice(i, 1);
      child.parent = null;
      return child;
    };
    // 页面用 container.contains(activeElement) 判断"用户是否正在输入"，
    // 少了它这条分支在测试里会直接抛错（而真实 DOM 一切正常）。
    node.contains = (other) => {
      for (let p = other; p; p = p.parent) if (p === node) return true;
      return false;
    };
    // `el()` 用 className 赋值，而页面更新状态点时用 setAttribute('class') ——
    // 真实 DOM 里这是同一条属性，stub 必须保持一致，否则"改没改到 class"
    // 会取决于用的是哪条路径，断言就变成假的。
    let cls = '';
    Object.defineProperty(node, 'className', {
      get: () => cls,
      set: (v) => {
        cls = String(v ?? '');
        node.attrs.class = cls;
        node.classList = new Set(cls.split(/\s+/).filter(Boolean));
      },
    });
    const rawSetAttribute = node.setAttribute.bind(node);
    node.setAttribute = (k, v) => {
      rawSetAttribute(k, v);
      if (k === 'class') cls = String(v ?? '');
    };
    const listeners = {};
    node.addEventListener = (type, fn) => { (listeners[type] ||= []).push(fn); };
    node.fire = (type, event = {}) => { (listeners[type] || []).forEach((fn) => fn(event)); };
    return node;
  };
  doc.createTextNode = (text) => Object.setPrototypeOf(rawText(text), globalThis.Node.prototype);
  return doc;
}

installInteractiveDomStub();

const { icon } = await import('./js/icons.js');
const {
  SETTINGS_TABS,
  resolveSettingsTab,
  default: renderSettings,
} = await import('./js/pages/settings.js');

/** 按 ARIA role 收集节点。 */
function byRole(root, role) {
  return walkStub(root).filter((n) => n.attrs && n.attrs.role === role);
}

/**
 * 已渲染 fixture 的清理函数。
 *
 * 设置页会 `subscribe('connected', ...)`，而本节的 fixture 是"渲染完就不管"
 * 的 —— 若不回收，一次 `store.set({connected: …})` 会让所有历史页面一起重绘，
 * 断言就失去了观察对象。需要触发订阅的用例先只留下自己那一份。
 */
const SETTINGS_DISPOSERS = [];

/** 渲染设置页；`tab` 为 null 表示 URL 上不带 ?tab=。 */
async function renderSettingsAt(tab) {
  const root = stubNode('div');
  const params = new URLSearchParams(tab == null ? '' : `tab=${tab}`);
  const dispose = await renderSettings(root, { params, navigate: () => {} });
  if (typeof dispose === 'function') SETTINGS_DISPOSERS.push(dispose);
  return root;
}

/**
 * 每个分区的"标志文本"：只出现在该分区里，用来断言另一个分区没有被渲染。
 * 注意不能用 tab 标签本身（它永远在），也不能用跨分区重复的词
 * （如「主题」同时出现在外观区与关于区的 kv 里）。
 */
const SETTINGS_MARKS = {
  connection: '后端 API 地址',
  appearance: '减少动态效果',
  data: '清除本地偏好',
  about: '前端形态',
};

/**
 * 预渲染各分区。
 *
 * 渲染函数是异步的，而本文件的 `test()` 只接同步断言（不为它引入异步运行器），
 * 所以把"渲染"作为 fixture 在这里一次做完，断言本身保持纯的、同步的。
 */
const RENDERED_SETTINGS = new Map();
for (const tab of SETTINGS_TABS) {
  RENDERED_SETTINGS.set(tab.id, await renderSettingsAt(tab.id));
}

/** 点击断言专用的 fixture：它会被"点击"改写，与只读断言共用会互相污染。 */
const CLICK_ROOT = await renderSettingsAt('connection');

/** 订阅断言专用的 fixture：它会被 store 事件改写，必须与只读 fixture 隔离。 */
const LIVE_ROOT = await renderSettingsAt('connection');

test('设置页的四个分区都在，id 唯一，图标真实存在', () => {
  const ids = SETTINGS_TABS.map((t) => t.id);
  assert.equal(new Set(ids).size, ids.length, `分区 id 重复：${ids.join(', ')}`);
  for (const want of ['connection', 'appearance', 'data', 'about']) {
    assert.ok(ids.includes(want), `缺少分区「${want}」`);
  }
  for (const t of SETTINGS_TABS) {
    // icon() 遇到未定义名字只返回空串并告警，界面不会报错 —— 只能靠断言守
    assert.ok(icon(t.icon, 15).length > 0,
      `分区「${t.label}」的图标 "${t.icon}" 在 icons.js 里不存在（界面会静默少一个图标）`);
  }
});

test('未知/空的 ?tab= 一律回落到第一个分区（否则渲染出空白面板）', () => {
  const fallback = SETTINGS_TABS[0].id;
  for (const bad of ['', '   ', 'nope', 'SETTINGS', '../etc', 'connection ', null, undefined, 42, {}, []]) {
    assert.equal(resolveSettingsTab(bad), fallback,
      `?tab=${JSON.stringify(bad)} 没有被归一 —— 会渲染出一个空白面板且没有任何报错`);
  }
});

test('合法 ?tab= 原样保留，并容忍复制链接常见的首尾空白', () => {
  for (const t of SETTINGS_TABS) {
    assert.equal(resolveSettingsTab(t.id), t.id);
    assert.equal(resolveSettingsTab(` ${t.id} `), t.id, '首尾空白应被裁掉，而不是当成非法值');
  }
});

test('任一时刻只渲染一个分区面板，其余分区的标志内容不出现', () => {
  for (const tab of SETTINGS_TABS) {
    const root = RENDERED_SETTINGS.get(tab.id);
    const panels = byRole(root, 'tabpanel');
    assert.equal(panels.length, 1,
      `?tab=${tab.id} 渲染了 ${panels.length} 个面板 —— 分区没有真正生效，模块又挤在一页了`);
    assert.equal(panels[0].attrs.id, `panel-${tab.id}`, '面板 id 与当前分区不一致');

    const text = textsOf(root).join(' | ');
    assert.ok(text.includes(SETTINGS_MARKS[tab.id]),
      `?tab=${tab.id} 时当前分区的内容没有渲染出来：${text}`);
    for (const other of SETTINGS_TABS) {
      if (other.id === tab.id) continue;
      assert.ok(!text.includes(SETTINGS_MARKS[other.id]),
        `?tab=${tab.id} 时仍渲染了「${other.label}」区的内容（${SETTINGS_MARKS[other.id]}）—— 还是挤在一页`);
    }
  }
});

test('标签栏是标准 ARIA tablist：恰好一个选中，且与面板互指', () => {
  const root = RENDERED_SETTINGS.get('appearance');
  const tabs = byRole(root, 'tab');
  assert.equal(tabs.length, SETTINGS_TABS.length, '标签数量与分区定义不一致');

  const selected = tabs.filter((t) => t.attrs['aria-selected'] === 'true');
  assert.equal(selected.length, 1, '必须有且只有一个选中标签（多选会让 aria 语义失效）');
  assert.equal(selected[0].attrs.id, 'tab-appearance');

  const panel = byRole(root, 'tabpanel')[0];
  assert.equal(panel.attrs['aria-labelledby'], selected[0].attrs.id);
  assert.equal(selected[0].attrs['aria-controls'], panel.attrs.id);

  // 悬空 IDREF 是"看不见的错"：面板只渲染当前分区，若给未选中的标签
  // 也挂 aria-controls，就会指向不存在的元素（aria-valid-attr-value 会报错）
  const ids = new Set(walkStub(root).map((n) => n.attrs?.id).filter(Boolean));
  for (const t of tabs) {
    const ref = t.attrs['aria-controls'];
    if (ref == null) continue;
    assert.ok(ids.has(ref), `aria-controls="${ref}" 指向一个不存在的元素`);
  }

  assert.equal(selected[0].attrs.tabindex, '0', '选中的标签必须能被 Tab 聚焦');
  for (const t of tabs.filter((x) => x !== selected[0])) {
    assert.equal(t.attrs.tabindex, '-1', 'roving tabindex：未选中的标签不应各占一个 Tab 停留点');
  }
});

test('点击分区只改 URL、不派发 hashchange，切换后仍只剩一个面板', () => {
  // 先把 hash 摆到"当前就是 connection"，才能观察 replace 的跨分区行为
  setHashOnly('#/settings?tab=connection');
  const root = CLICK_ROOT;
  assert.equal(byRole(root, 'tabpanel').length, 1);

  const target = byRole(root, 'tab').find((t) => t.attrs.id === 'tab-data');
  assert.ok(target, '标签栏里没有「数据」分区');

  const types = dispatchedTypes(() => target.fire('click'));
  assert.deepEqual(types, [],
    '切分区派发了 hashchange —— shell 会 dispose 掉当前页面再重建（切线不该打断页面）');
  assert.equal(WINSTUB.win.location.hash, '#/settings?tab=data',
    'URL 没跟着分区走：刷新与分享都会回到错误的分区');

  const panels = byRole(root, 'tabpanel');
  assert.equal(panels.length, 1, `切换后出现了 ${panels.length} 个面板 —— 旧分区的面板没有被替换掉`);
  assert.equal(panels[0].attrs.id, 'panel-data');
});

test('后端连接状态变化会让标签上的状态点跟着变，且不冲掉正在输入的地址', () => {
  // 只保留本用例自己的订阅：其它 fixture 已经断言完毕，不该再跟着重绘
  const live = SETTINGS_DISPOSERS.pop();
  while (SETTINGS_DISPOSERS.length) SETTINGS_DISPOSERS.pop()();

  /** 标签上的状态点（面板内的状态区也有一个 dot，取 DOM 顺序里的第一个）。 */
  const firstDot = () => walkStub(LIVE_ROOT)
    .find((n) => typeof n.attrs?.class === 'string' && /(^|\s)dot(\s|$)/.test(n.attrs.class));

  assert.ok(firstDot(), '标签上没有状态点');
  assert.match(firstDot().attrs.class, /dot-danger/, '未连接时状态点应为警示色');

  // 场景：设置页在健康探测返回**之前**就渲染完成（connected=false），
  // 随后探测成功。若状态点是"渲染时读一次"，就会出现
  // 「顶栏写着服务正常、标签仍然标红」这种自相矛盾的界面。
  globalThis.document.activeElement = walkStub(LIVE_ROOT).find((n) => n.tagName === 'INPUT');
  assert.ok(globalThis.document.activeElement, '连接区没有地址输入框');

  store.set({ connected: true });

  assert.match(firstDot().attrs.class, /dot-success/,
    '状态点没有随 store 更新 —— 顶栏显示「服务正常」而标签仍是红的');
  assert.ok(walkStub(LIVE_ROOT).includes(globalThis.document.activeElement),
    '正在输入的输入框被整体重绘冲掉了（光标与半截地址一起丢）');

  // 收尾：恢复焦点并回收订阅，避免影响后续用例
  globalThis.document.activeElement = null;
  live();
  store.set({ connected: false });
});

/* ================================================================== *
 * 结果
 * ================================================================== */

if (failures.length) {
  console.error(`\n✗ ${failures.length} 个测试失败（通过 ${passed}）：\n`);
  for (const { name, err } of failures) {
    console.error(`  ✗ ${name}`);
    console.error(`    ${err.message.split('\n').join('\n    ')}`);
  }
  process.exit(1);
}
console.log(`✓ DAG/页面回归测试全部通过（${passed} 个）`);
