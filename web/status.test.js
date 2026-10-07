/**
 * 状态/角色映射表的**跨语言契约测试**（零依赖，node 直跑）。
 *
 * ## 这个文件存在的唯一理由
 *
 * 我们已经**两次**踩到同一个坑：
 *   1. 第一次：`graph.js` 的 STATUS_GROUP 缺 ready/verifying/rejected，
 *      `AGENT_COLOR` 用了 `coordinator`（真实枚举是 `orchestrator`）。
 *   2. 第二次：修好 `graph.js` 后，`components.js` 里**另有一套**映射表
 *      没人管，仍然缺同样三项，任务列表的圆点全是中性灰、文案回显英文。
 *
 * 第 2 次不是"忘了改"，而是同一个知识存在两份。所以本轮做了两件事：
 *   - 把表收敛到 `js/status.js`（唯一真源）→ 消除结构性诱因；
 *   - 用本文件对**真源**做全集断言 → 下次新增枚举成员时直接红。
 *
 * ## 断言的强度
 *
 * 词表 `test_contract_words.json` 由 Python 侧从 `devagent/enums.py` 导出
 * （见 tests/conftest.py、Makefile 的 web-words target）。因此这里断言的
 * 不是"我以为的状态集合"，而是**后端实际会发出的字符串**。
 * 两边不同步时，`pytest` 和本文件里至少有一个会失败。
 *
 * 运行：`node web/status.test.js`（或 `make web-check`）
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// 静态导入（而非动态 import）：让「真源唯一性」的断言保持同步，
// 不必给测试运行器加 async 支持。
import * as components from './js/components.js';
import * as graphReexports from './graph.js';
import {
  AGENT_COLOR,
  AGENT_FALLBACK_COLOR,
  AGENT_LABEL,
  STATUS_GROUP,
  STATUS_ICON,
  STATUS_LABEL,
  STATUS_TONE,
  agentColor,
  agentLabel,
  statusDotClass,
  statusGroup,
  statusIcon,
  statusLabel,
  statusTone,
  taskStatusBadge,
} from './js/status.js';

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

/**
 * 读取跨语言词表。
 *
 * 读不到时**直接抛错而不是跳过测试** —— 一个静默跳过的契约测试
 * 比没有测试更糟：它让 CI 变绿，却什么也没验证。
 */
const WORDS = (() => {
  const url = new URL('./test_contract_words.json', import.meta.url);
  let raw;
  try {
    raw = readFileSync(url, 'utf8');
  } catch {
    throw new Error(
      '缺少 web/test_contract_words.json（跨语言词表）。\n'
      + '它由 Python 侧从 devagent/enums.py 导出，请先跑一次：\n'
      + '  PYTHONPATH=src ./.venv/Scripts/python.exe -m pytest tests/unit/test_node_contract.py\n'
      + '或者直接跑 `make check`（会先 pytest 再 web-check）。',
    );
  }
  return JSON.parse(raw);
})();

/* ------------------------------------------------------------------ *
 * 词表本身的可信度
 * ------------------------------------------------------------------ */

test('词表非空且结构正确（防止 fixture 缺失导致契约测试空跑）', () => {
  assert.ok(Array.isArray(WORDS.step_status) && WORDS.step_status.length >= 8,
    `step_status 词表异常：${JSON.stringify(WORDS.step_status)}`);
  assert.ok(Array.isArray(WORDS.agent_type) && WORDS.agent_type.length >= 7,
    `agent_type 词表异常：${JSON.stringify(WORDS.agent_type)}`);
});

test('词表元素都是非空字符串', () => {
  for (const list of [WORDS.step_status, WORDS.agent_type]) {
    for (const v of list) {
      assert.equal(typeof v, 'string');
      assert.ok(v.length > 0);
    }
  }
});

/* ------------------------------------------------------------------ *
 * StepStatus 全集覆盖
 * ------------------------------------------------------------------ */

test('StepStatus 的每个成员都有视觉分组（不回退到 pending）', () => {
  for (const s of WORDS.step_status) {
    const group = statusGroup(s);
    if (s !== 'pending') {
      assert.notEqual(group, 'pending',
        `StepStatus.${s} 未在 STATUS_GROUP 登记，静默降级为"待执行"`);
    }
    assert.ok(group && typeof group === 'string');
  }
});

test('StepStatus 的每个成员都有中文标签（不回显英文枚举名）', () => {
  for (const s of WORDS.step_status) {
    const label = statusLabel(s);
    assert.notEqual(label, s,
      `StepStatus.${s} 未在 STATUS_LABEL 登记，界面会显示英文枚举名`);
    assert.ok(label.length > 0);
  }
});

test('StepStatus 的每个成员都有徽章色调', () => {
  // 直接断言表里有登记，而不是拿 'neutral' 当"未登记"的哨兵 ——
  // pending 本身就是中性灰，用哨兵法会误报。
  const VALID_TONES = new Set([
    'success', 'danger', 'warning', 'info', 'neutral', 'brand', 'running',
  ]);
  for (const s of WORDS.step_status) {
    assert.ok(STATUS_TONE[s],
      `STATUS_TONE.${s} 缺失，徽章会退为默认色，与 pending 无法区分`);
    assert.ok(VALID_TONES.has(STATUS_TONE[s]),
      `STATUS_TONE.${s} = "${STATUS_TONE[s]}" 不是 components.css 里定义的 tone`);
  }
});

test('StepStatus 的每个成员都有图标', () => {
  for (const s of WORDS.step_status) {
    assert.ok(STATUS_ICON[s], `STATUS_ICON.${s} 缺失，徽章会没有图标`);
    assert.ok(statusIcon(s).length > 0);
  }
});

/* ------------------------------------------------------------------ *
 * 语义正确性 —— 不只是"有登记"，还要"分得对"
 * ------------------------------------------------------------------ */

test('rejected 与 failed 是不同分组（驳回 ≠ 失败）', () => {
  assert.equal(statusGroup('rejected'), 'backtrack');
  assert.notEqual(statusGroup('rejected'), statusGroup('failed'),
    'rejected 与 failed 混为一组：读者会误判任务已失败，实际它还会回退重跑');
});

test('verifying 与 running 同组（都在占用资源）', () => {
  assert.equal(statusGroup('verifying'), statusGroup('running'));
});

test('ready 有独立分组，不与 pending 混同', () => {
  assert.notEqual(statusGroup('ready'), statusGroup('pending'),
    'ready 并入 pending：无法区分"依赖未满足"与"就绪待调度"');
});

test('backtracked 不是 StepStatus 成员，但前端仍需支持', () => {
  assert.ok(!WORDS.step_status.includes('backtracked'),
    'backtracked 混进了 StepStatus —— 它是事件层派生态，不该出现在枚举里');
  // 尽管不是枚举成员，事件层会发出它，前端必须有映射
  assert.ok(STATUS_GROUP.backtracked, 'backtracked 缺少分组映射');
  assert.ok(STATUS_LABEL.backtracked, 'backtracked 缺少中文标签');
});

/* ------------------------------------------------------------------ *
 * AgentType 全集覆盖
 * ------------------------------------------------------------------ */

test('AgentType 的每个成员都有中文标签', () => {
  for (const a of WORDS.agent_type) {
    const label = agentLabel(a);
    assert.notEqual(label, a,
      `AgentType.${a} 未在 AGENT_LABEL 登记，界面会显示英文枚举名`);
    assert.ok(label.length > 0);
  }
});

test('AgentType 的每个成员都有专属配色（不共用兜底灰）', () => {
  for (const a of WORDS.agent_type) {
    const c = agentColor(a);
    assert.notEqual(c, AGENT_FALLBACK_COLOR,
      `AgentType.${a} 未在 AGENT_COLOR 登记，退为兜底灰 ${AGENT_FALLBACK_COLOR}`);
    assert.ok(AGENT_COLOR[a], `AGENT_COLOR.${a} 缺失`);
  }
});

test('AgentType 各成员配色互不相同（否则图例无法区分角色）', () => {
  const used = new Map();
  for (const a of WORDS.agent_type) {
    const c = agentColor(a);
    assert.equal(used.get(c), undefined,
      `AgentType.${a} 与 ${used.get(c)} 配色相同（${c}），DAG 上无法区分`);
    used.set(c, a);
  }
});

test('AGENT_COLOR 不含未知键（防止枚举改名后留下孤儿条目）', () => {
  const known = new Set(WORDS.agent_type);
  for (const k of Object.keys(AGENT_COLOR)) {
    assert.ok(known.has(k),
      `AGENT_COLOR 含未知角色 "${k}"：枚举里没有它，`
      + `很可能是改名后遗留的孤儿（曾发生：coordinator → orchestrator）`);
  }
});

test('AGENT_LABEL 不含未知键', () => {
  const known = new Set(WORDS.agent_type);
  for (const k of Object.keys(AGENT_LABEL)) {
    assert.ok(known.has(k), `AGENT_LABEL 含未知角色 "${k}"`);
  }
});

test('STATUS_GROUP 不含未知键（StepStatus + 派生态是唯一合法集合）', () => {
  const allowed = new Set([...WORDS.step_status, 'backtracked']);
  for (const k of Object.keys(STATUS_GROUP)) {
    assert.ok(allowed.has(k),
      `STATUS_GROUP 含未知状态 "${k}"：既不是 StepStatus 成员，也不是已知派生态`);
  }
});

/* ------------------------------------------------------------------ *
 * 任务级状态（不是 StepStatus，但同一批视觉分类）
 * ------------------------------------------------------------------ */

test('任务状态徽章返回三元组且不缺项', () => {
  const statuses = ['pending', 'running', 'success', 'succeeded', 'failed',
    'error', 'cancelled', 'completed', 'skipped', 'backtracked'];
  for (const s of statuses) {
    const t = taskStatusBadge(s);
    assert.ok(Array.isArray(t) && t.length === 3, `${s} 返回结构不是三元组`);
    const [tone, label, ico] = t;
    assert.ok(tone && label, `${s} 的 tone/label 为空`);
    assert.notEqual(label, s, `${s} 的标签回显了英文`);
    void ico;
  }
});

test('未知任务状态保留原文而不是吞成 "未知"', () => {
  const [tone, label] = taskStatusBadge('some_future_status');
  assert.equal(label, 'some_future_status', '未知状态被吞掉了，排查时看不到原值');
  assert.equal(tone, 'neutral');
});

test('任务状态与步骤状态在共享成员上视觉一致', () => {
  // running / success / failed 三个成员两边都有，色调必须一致，
  // 否则同一个"成功"在 DAG 节点和任务列表里会是两种颜色。
  const shared = ['running', 'success', 'failed'];
  for (const s of shared) {
    assert.equal(statusTone(s), taskStatusBadge(s)[0],
      `状态 ${s} 在步骤域与任务域的色调不一致`);
  }
});

/* ------------------------------------------------------------------ *
 * 派生函数
 * ------------------------------------------------------------------ */

test('statusDotClass 产出合法的 CSS 类名', () => {
  for (const s of [...WORDS.step_status, 'backtracked', 'cancelled']) {
    const cls = statusDotClass(s);
    assert.match(cls, /^dot-[a-z][a-z0-9-]*$/, `非法类名：${cls}`);
  }
});

test('statusGroup 的分组名都能安全拼进 CSS 类名', () => {
  for (const g of Object.values(STATUS_GROUP)) {
    assert.match(g, /^[a-z][a-z0-9-]*$/, `非法分组名：${g}`);
  }
});

test('statusLabel 对未知状态保留原文（便于排查）', () => {
  assert.equal(statusLabel('__nope__'), '__nope__');
});

test('statusLabel 对空值回退到可读文案而不是 undefined', () => {
  const out = statusLabel('');
  assert.ok(out && out !== 'undefined');
});

test('agentLabel 对未知角色保留原文', () => {
  assert.equal(agentLabel('__nope__'), '__nope__');
});

test('agentColor 对未知角色退为兜底灰而不是 undefined', () => {
  assert.equal(agentColor('__nope__'), AGENT_FALLBACK_COLOR);
});

/* ------------------------------------------------------------------ *
 * 真源唯一性 —— 防止再次出现第二套表
 * ------------------------------------------------------------------ */

test('graph.js 再导出的表与 status.js 是同一对象（不是副本）', () => {
  // 这一条是防回归的核心：如果将来有人图省事在 graph.js 里重新
  // 定义一份 STATUS_GROUP，这里的**引用相等**断言会立刻失败。
  // 用 === 而非 deepEqual 是关键 —— deepEqual 对"内容相同的两份副本"
  // 会通过，而那正是我们要禁止的状态。
  assert.equal(graphReexports.STATUS_GROUP, STATUS_GROUP,
    'graph.js 的 STATUS_GROUP 不是 status.js 的同一对象 —— 出现了第二套表');
  assert.equal(graphReexports.AGENT_COLOR, AGENT_COLOR,
    'graph.js 的 AGENT_COLOR 不是 status.js 的同一对象 —— 出现了第二套表');
});

test('components.js 的 statusDotClass / agentLabel 也来自同一真源', () => {
  // components.js 是第二次踩坑的地方（它曾有独立的 STATUS_MAP/AGENT_LABELS）。
  // 这里断言它导出的函数就是 status.js 的实现本体。
  assert.equal(components.statusDotClass, statusDotClass,
    'components.js 的 statusDotClass 不是 status.js 的实现 —— 又出现了第二套表');
  assert.equal(components.agentLabel, agentLabel,
    'components.js 的 agentLabel 不是 status.js 的实现 —— 又出现了第二套表');
});

/* ------------------------------------------------------------------ *
 * 结果
 * ------------------------------------------------------------------ */

if (failures.length) {
  console.error(`\n✗ ${failures.length} 个契约测试失败（通过 ${passed}）：\n`);
  for (const { name, err } of failures) {
    console.error(`  ✗ ${name}`);
    console.error(`    ${err.message.split('\n')[0]}`);
  }
  process.exit(1);
}
console.log(`✓ 状态契约测试全部通过（${passed} 个）`);
