/**
 * DAG 布局与渲染引擎的冒烟测试（零依赖，直接用 node 跑）。
 *
 * ## 为什么用「手写断言 + node 直跑」而不是引入 vitest
 *
 * 前端是**零构建**的：没有 package.json、没有 node_modules。为了让
 * `graph.js` 这类纯逻辑可被测试而引入整套 JS 测试框架，会打破这个前提
 * —— 贡献者 clone 后必须先 `npm install` 才能跑测试，而收益仅仅是
 * 省下几十行断言代码。
 *
 * 因此：只对**纯函数**做测试（布局、事件叠加、diff 解析），DOM 渲染
 * 部分靠 `node --check` 保证语法正确 + 浏览器里人工验证。这个边界是
 * 刻意划的：可测的核心价值在几何与语义映射，不在 appendChild。
 *
 * 运行：`node web/graph.test.js`（或 `make web-check`）
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  LEGEND_ITEMS, STATUS_GROUP, agentColor, applyEvent, buildGraphState,
  computeLayers, diffStats, layoutDag, parseDiff, statusGroup, statusLabel,
} from './graph.js?v=20261008-live';

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

/* ------------------------------------------------------------------ *
 * 拓扑分层
 * ------------------------------------------------------------------ */

test('线性链逐层递增', () => {
  const layers = computeLayers([
    { id: 'N1', deps: [] },
    { id: 'N2', deps: ['N1'] },
    { id: 'N3', deps: ['N2'] },
    { id: 'N4', deps: ['N3'] },
  ]);
  assert.deepEqual([...layers.values()], [0, 1, 2, 3]);
});

test('多依赖取最长路径（不是最短）', () => {
  // A→B→C，且 A→C。C 的层必须是 2（经 B），不能是 1（直达）。
  // 取最短会让 C 与 B 同层，边的方向就地被破坏。
  const layers = computeLayers([
    { id: 'A', deps: [] },
    { id: 'B', deps: ['A'] },
    { id: 'C', deps: ['A', 'B'] },
  ]);
  assert.equal(layers.get('C'), 2);
});

test('菱形结构：汇合点落在最深层', () => {
  const L = layoutDag([
    { id: 'A', deps: [] },
    { id: 'B', deps: ['A'] },
    { id: 'C', deps: ['A'] },
    { id: 'D', deps: ['B', 'C'] },
  ]);
  assert.equal(L.layers, 3);
  assert.equal(L.edges.length, 4);
});

test('无依赖节点全在第 0 层', () => {
  const layers = computeLayers([{ id: 'A', deps: [] }, { id: 'B', deps: [] }]);
  assert.deepEqual([...layers.values()], [0, 0]);
});

test('环被检测并抛出，且错误信息含完整路径', () => {
  assert.throws(
    () => computeLayers([{ id: 'X', deps: ['Y'] }, { id: 'Y', deps: ['X'] }]),
    (err) => err instanceof Error && err.message.includes('环') && err.message.includes('X'),
  );
});

test('自环被检测', () => {
  assert.throws(() => computeLayers([{ id: 'A', deps: ['A'] }]), /环/);
});

test('悬空依赖被跳过：不崩、不算作一层、不产生边', () => {
  // 关键回归：早期实现把幽灵依赖当成 0 层父节点，导致节点被右移一格，
  // 读者会以为多了一个不存在的阶段。
  const L = layoutDag([{ id: 'P', deps: ['ghost'] }]);
  assert.equal(L.nodes[0].layer, 0);
  assert.equal(L.edges.length, 0);
});

test('深链不爆栈（递归改迭代的回归防线）', () => {
  const deep = Array.from({ length: 3000 }, (_, i) => ({
    id: `N${i}`,
    deps: i ? [`N${i - 1}`] : [],
  }));
  const layers = computeLayers(deep);
  assert.equal(layers.get('N2999'), 2999);
});

/* ------------------------------------------------------------------ *
 * 几何布局
 * ------------------------------------------------------------------ */

test('空输入返回零尺寸而不抛异常', () => {
  const L = layoutDag([]);
  assert.equal(L.nodes.length, 0);
  assert.equal(L.layers, 0);
  assert.equal(L.width, 0);
});

test('过滤掉缺 id 的脏节点', () => {
  const L = layoutDag([{ id: 'A', deps: [] }, { deps: [] }, null, { id: '' }]);
  assert.equal(L.nodes.length, 1);
});

test('非数组输入降级为空布局', () => {
  assert.equal(layoutDag(undefined).nodes.length, 0);
  assert.equal(layoutDag('nonsense').nodes.length, 0);
});

test('同层节点共用一个 x（列对齐）', () => {
  const L = layoutDag([
    { id: 'A', deps: [] },
    { id: 'B', deps: ['A'] },
    { id: 'C', deps: ['A'] },
  ]);
  const xs = Object.fromEntries(L.nodes.map((n) => [n.id, n.x]));
  assert.equal(xs.B, xs.C);
  assert.ok(xs.B > xs.A, '下一层必须更靠右');
});

test('层的 x 严格单调递增', () => {
  const L = layoutDag([
    { id: 'A', deps: [] },
    { id: 'B', deps: ['A'] },
    { id: 'C', deps: ['B'] },
  ]);
  const xs = [...L.nodes].sort((a, b) => a.layer - b.layer).map((n) => n.x);
  for (let i = 1; i < xs.length; i += 1) assert.ok(xs[i] > xs[i - 1]);
});

test('同层节点在垂直方向不重叠', () => {
  const L = layoutDag([
    { id: 'A', deps: [] },
    { id: 'B', deps: ['A'] },
    { id: 'C', deps: ['A'] },
    { id: 'D', deps: ['A'] },
  ]);
  const rows = L.nodes.filter((n) => n.layer === 1).sort((a, b) => a.y - b.y);
  for (let i = 1; i < rows.length; i += 1) {
    assert.ok(rows[i].y >= rows[i - 1].y + rows[i - 1].h, '矩形不得交叠');
  }
});

test('画布尺寸能容纳所有节点', () => {
  const L = layoutDag([
    { id: 'A', deps: [] },
    { id: 'B', deps: ['A'] },
    { id: 'C', deps: ['A'] },
    { id: 'D', deps: ['B', 'C'] },
  ]);
  for (const n of L.nodes) {
    assert.ok(n.x >= 0 && n.y >= 0);
    assert.ok(n.x + n.w <= L.width, '节点不得溢出右边界');
    assert.ok(n.y + n.h <= L.height, '节点不得溢出下边界');
  }
});

test('每条边的 path 以 M 开头且包含一段贝塞尔控制点', () => {
  const L = layoutDag([{ id: 'A', deps: [] }, { id: 'B', deps: ['A'] }]);
  assert.equal(L.edges.length, 1);
  assert.match(L.edges[0].d, /^M [\d.]+ [\d.]+ C /);
});

test('正向边被标记为 back=false', () => {
  const L = layoutDag([{ id: 'A', deps: [] }, { id: 'B', deps: ['A'] }]);
  assert.equal(L.edges[0].back, false);
});

test('层内排序稳定：输入顺序不影响结果', () => {
  const order1 = layoutDag([
    { id: 'A', deps: [] }, { id: 'B', deps: ['A'] }, { id: 'C', deps: ['A'] },
  ]).nodes.map((n) => `${n.id}${n.row}`).join(',');
  const order2 = layoutDag([
    { id: 'C', deps: ['A'] }, { id: 'B', deps: ['A'] }, { id: 'A', deps: [] },
  ]).nodes.map((n) => `${n.id}${n.row}`).join(',');
  // 集合相同即可（顺序按 id 稳定排序）
  assert.deepEqual(order1.split(',').sort(), order2.split(',').sort());
});

/* ------------------------------------------------------------------ *
 * 状态语义
 * ------------------------------------------------------------------ */

test('状态映射到视觉分组', () => {
  assert.equal(statusGroup('success'), 'ok');
  assert.equal(statusGroup('running'), 'running');
  assert.equal(statusGroup('backtracked'), 'backtrack');
  assert.equal(statusGroup('failed'), 'fail');
  assert.equal(statusGroup('pending'), 'pending');
});

test('未知状态降级为 pending 而不是 undefined', () => {
  // 降级是为了不崩，但这只应发生在**真正未知**的字面量上。
  // 后端枚举里的每个成员都必须在上面登记，由下面的契约测试保证。
  assert.equal(statusGroup('who-knows'), 'pending');
  assert.equal(statusGroup(undefined), 'pending');
});

test('backtracked 与 failed 是不同的分组', () => {
  // 关键设计：回退表示「仍在进行中」，与终态失败必须视觉可分，
  // 否则读者会误判任务已经失败。
  assert.notEqual(statusGroup('backtracked'), statusGroup('failed'));
});

test('rejected 与 failed 也是不同的分组', () => {
  // rejected 是「被驳回、会带着反馈回退重跑」，failed 是「重试耗尽」。
  // 二者在快照里都是终态字面量，混为一谈会让读者以为任务已经结束。
  assert.equal(statusGroup('rejected'), 'backtrack');
  assert.notEqual(statusGroup('rejected'), statusGroup('failed'));
});

/* ------------------------------------------------------------------ *
 * 跨语言契约：后端枚举 → 前端映射表
 *
 * 这是本项目唯一一处「Python 产出字符串、JS 消费」的接口，中间没有类型
 * 系统兜底。漏登记一个枚举成员的后果不是报错，而是**静默降级**：
 * 状态显示成"待执行"、角色退成灰色——比不显示更误导。
 *
 * 因此这里不手抄词表，而是读 `test_contract_words.json`（由 Python 端
 * `tests/conftest.py` 的 session fixture 从 enums.py 导出、并由
 * `tests/unit/test_node_contract.py` 断言其新鲜度）。这样 Python 侧新增
 * 枚举成员 -> fixture 更新 -> 这里立刻变红。
 * ------------------------------------------------------------------ */

const WORDS = (() => {
  const url = new URL('./test_contract_words.json', import.meta.url);
  let raw;
  try {
    raw = readFileSync(url, 'utf8');
  } catch {
    // 这个词表由 pytest 的 session fixture 从 devagent/enums.py 导出。
    // 直接跑 `node web/graph.test.js` 而没跑过 pytest 时会缺文件——
    // 给出可执行的补救命令，而不是抛一个看不懂的 ENOENT。
    throw new Error(
      '缺少 web/test_contract_words.json（跨语言词表）。\n'
      + '它由 Python 侧从 devagent/enums.py 导出，请先跑一次：\n'
      + '  PYTHONPATH=src ./.venv/Scripts/python.exe -m pytest tests/unit/test_node_contract.py\n'
      + '或者直接跑 `make check`（会先 pytest 再 web-check）。',
    );
  }
  return JSON.parse(raw);
})();

test('fixture 非空且结构正确（防止 fixture 缺失导致契约测试空跑）', () => {
  assert.ok(Array.isArray(WORDS.step_status) && WORDS.step_status.length >= 8);
  assert.ok(Array.isArray(WORDS.agent_type) && WORDS.agent_type.length >= 7);
});

test('StepStatus 的每个成员都有明确的视觉分组', () => {
  for (const s of WORDS.step_status) {
    const group = statusGroup(s);
    // 未登记会落回 'pending'；pending 本身是合法分组，所以对
    // 'pending' 之外的成员，只要结果仍是 'pending' 就说明漏登记了。
    if (s !== 'pending') {
      assert.notEqual(
        group, 'pending',
        `StepStatus.${s} 未在前端 STATUS_GROUP 登记，静默降级为"待执行"`,
      );
    }
    assert.ok(group && typeof group === 'string');
  }
});

test('StepStatus 的每个成员都有中文标签（不回显英文枚举名）', () => {
  for (const s of WORDS.step_status) {
    const label = statusLabel(s);
    assert.notEqual(
      label, s,
      `StepStatus.${s} 未在前端 STATUS_LABEL 登记，界面会回显英文`,
    );
    assert.ok(label.length > 0);
  }
});

test('AgentType 的每个成员都有专属配色（不共用灰色兜底）', () => {
  const FALLBACK = agentColor('__no_such_agent__');
  const used = new Map();
  for (const a of WORDS.agent_type) {
    const c = agentColor(a);
    assert.notEqual(
      c, FALLBACK,
      `AgentType.${a} 未在前端 AGENT_COLOR 登记，退为兜底灰`,
    );
    // 两个不同角色用同一颜色 -> 图例失去区分能力
    assert.equal(
      used.get(c), undefined,
      `AgentType.${a} 与 ${used.get(c)} 配色相同（${c}），无法区分`,
    );
    used.set(c, a);
  }
});

test('LEGEND_ITEMS 覆盖 STATUS_GROUP 用到的全部分组', () => {
  // 读者看到某个颜色，必须能在图例里查到它的含义。
  const used = new Set(Object.values(STATUS_GROUP));
  const documented = new Set(LEGEND_ITEMS.map((i) => i.group));
  for (const g of used) {
    assert.ok(documented.has(g), `分组 ${g} 被状态使用但图例未解释`);
  }
});

test('LEGEND_ITEMS 的 group 可用于拼 CSS 类名（无空格/大写）', () => {
  for (const item of LEGEND_ITEMS) {
    assert.match(item.group, /^[a-z][a-z0-9-]*$/, `非法分组名：${item.group}`);
    assert.ok(item.label && item.label.length > 0);
  }
});

/* ------------------------------------------------------------------ *
 * 事件叠加
 * ------------------------------------------------------------------ */

function graphWith(nodes) {
  return buildGraphState(nodes);
}

test('node_started 把节点置为 running', () => {
  const g = graphWith([{ id: 'N1', deps: [], status: 'pending' }]);
  assert.equal(applyEvent(g, { kind: 'node_started', node_id: 'N1', attempt: 1 }), true);
  assert.equal(g.get('N1').status, 'running');
});

test('node_verdict reject 把节点置为 backtracked', () => {
  const g = graphWith([{ id: 'N1', deps: [], status: 'running' }]);
  applyEvent(g, { kind: 'node_verdict', node_id: 'N1', verdict: 'reject', attempt: 1 });
  assert.equal(g.get('N1').status, 'backtracked');
  assert.equal(g.get('N1').group, 'backtrack');
});

test('node_verdict pass 把节点置为 success', () => {
  const g = graphWith([{ id: 'N1', deps: [], status: 'running' }]);
  applyEvent(g, { kind: 'node_verdict', node_id: 'N1', verdict: 'pass' });
  assert.equal(g.get('N1').status, 'success');
});

test('完整回退流程：reject → 重跑 attempt+1 → success', () => {
  const g = graphWith([{ id: 'C', deps: [], status: 'pending', attempt: 1 }]);
  applyEvent(g, { kind: 'node_started', node_id: 'C', attempt: 1 });
  applyEvent(g, { kind: 'node_verdict', node_id: 'C', verdict: 'reject', attempt: 1 });
  assert.equal(g.get('C').status, 'backtracked');
  applyEvent(g, { kind: 'node_started', node_id: 'C', attempt: 2 });
  assert.equal(g.get('C').status, 'running');
  assert.equal(g.get('C').attempt, 2, '重跑必须反映在 attempt 上');
  applyEvent(g, { kind: 'node_finished', node_id: 'C', status: 'success', attempt: 2, tokens_used: 88 });
  assert.equal(g.get('C').status, 'success');
  assert.equal(g.get('C').tokens, 88);
});

test('node_finished 的 status 直接映射（含 backtracked）', () => {
  const g = graphWith([{ id: 'N1', deps: [], status: 'running' }]);
  applyEvent(g, { kind: 'node_finished', node_id: 'N1', status: 'backtracked' });
  assert.equal(g.get('N1').status, 'backtracked');
});

test('未知节点的事件被忽略，不会凭空造节点', () => {
  const g = graphWith([{ id: 'N1', deps: [] }]);
  assert.equal(applyEvent(g, { kind: 'node_started', node_id: 'GHOST' }), false);
  assert.equal(g.size, 1);
});

test('无 node_id 的事件不改变任何状态', () => {
  const g = graphWith([{ id: 'N1', deps: [] }]);
  assert.equal(applyEvent(g, { kind: 'task_started' }), false);
});

test('状态未变化时返回 false（用于跳过无意义重绘）', () => {
  const g = graphWith([{ id: 'N1', deps: [], status: 'success', attempt: 1 }]);
  assert.equal(applyEvent(g, { kind: 'node_finished', node_id: 'N1', status: 'success', attempt: 1 }), false);
});

test('attempt 变化即使状态相同也算变化', () => {
  const g = graphWith([{ id: 'N1', deps: [], status: 'running', attempt: 1 }]);
  assert.equal(applyEvent(g, { kind: 'node_started', node_id: 'N1', attempt: 2 }), true);
});

test('缺失 attempt 时保留原值而不是写成 NaN', () => {
  const g = graphWith([{ id: 'N1', deps: [], attempt: 3 }]);
  applyEvent(g, { kind: 'node_started', node_id: 'N1' });
  assert.equal(g.get('N1').attempt, 3);
});

/* ------------------------------------------------------------------ *
 * diff 解析
 * ------------------------------------------------------------------ */

const SAMPLE = [
  '--- a/src/x.py',
  '+++ b/src/x.py',
  '@@ -1,3 +1,4 @@',
  ' def f():',
  '-    return 1',
  '+    return 2',
  '+    # 注释',
].join('\n');

test('识别 add / del / ctx / hunk / meta 五类行', () => {
  const kinds = parseDiff(SAMPLE).map((l) => l.type);
  assert.deepEqual(kinds, ['meta', 'meta', 'hunk', 'ctx', 'del', 'add', 'add']);
});

test('diff 统计正确', () => {
  assert.deepEqual(diffStats(SAMPLE), { add: 2, del: 1 });
});

test('行号按 hunk 头起始并各自推进', () => {
  const lines = parseDiff(SAMPLE);
  const ctx = lines.find((l) => l.type === 'ctx');
  assert.equal(ctx.oldNo, 1);
  assert.equal(ctx.newNo, 1);
  const del = lines.find((l) => l.type === 'del');
  const add = lines.find((l) => l.type === 'add');
  assert.equal(del.oldNo, 2);
  assert.equal(del.newNo, null);
  assert.equal(add.oldNo, null);
  assert.equal(add.newNo, 2);
});

test('行内容去掉行首标记符', () => {
  const add = parseDiff(SAMPLE).find((l) => l.type === 'add');
  assert.equal(add.text, '    return 2');
});

test('多个 hunk 各自重置行号', () => {
  const lines = parseDiff([
    '@@ -1,1 +1,1 @@', '-a', '+b',
    '@@ -50,1 +60,1 @@', '-c', '+d',
  ].join('\n'));
  const adds = lines.filter((l) => l.type === 'add');
  assert.equal(adds[0].newNo, 1);
  assert.equal(adds[1].newNo, 60);
});

test('“\\ No newline at end of file” 归为 meta 而不是上下文', () => {
  const lines = parseDiff(['@@ -1 +1 @@', '-a', '+b', '\\ No newline at end of file'].join('\n'));
  assert.equal(lines.at(-1).type, 'meta');
});

test('空 diff 返回空数组而不抛异常', () => {
  // 关键：必须与 diffStats('') === {add:0, del:0} 一致。
  // 早期实现对 '' 调 split('\n') 得到 ['']，于是产出一条假的空上下文行。
  assert.deepEqual(parseDiff(''), []);
  assert.deepEqual(parseDiff(null), []);
  assert.deepEqual(parseDiff(undefined), []);
  assert.deepEqual(diffStats(''), { add: 0, del: 0 });
});

test('没有 hunk 头时行号为 null 但不崩', () => {
  const lines = parseDiff('-old\n+new');
  assert.equal(lines.length, 2);
  assert.equal(lines[0].type, 'del');
  assert.equal(lines[0].oldNo, null);
  assert.equal(lines[1].type, 'add');
  assert.equal(lines[1].newNo, null);
});

/* ------------------------------------------------------------------ *
 * 结果
 * ------------------------------------------------------------------ */

if (failures.length) {
  console.error(`\n✗ ${failures.length} 个测试失败（通过 ${passed}）：\n`);
  for (const { name, err } of failures) {
    console.error(`  ✗ ${name}`);
    console.error(`    ${err.message.split('\n')[0]}`);
  }
  process.exit(1);
}
console.log(`✓ web 前端逻辑测试全部通过（${passed} 个）`);
