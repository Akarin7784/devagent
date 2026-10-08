/**
 * 事件流去重与回放判定的测试（零依赖，node 直跑）。
 *
 * 覆盖的是**静默失败**类逻辑：判重规则写错不会抛异常，只会让时间线
 * 悄悄多出或丢掉一批事件。这类 bug 只能靠断言发现。
 *
 * 运行：`node web/eventstream.test.js`（或 `make web-check`）
 */

import assert from 'node:assert/strict';

import { EventDedup, ReplayWindow, eventFingerprint } from './js/eventstream.js?v=20261008-live';

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
 * 指纹
 * ------------------------------------------------------------------ */

test('指纹包含 kind / node_id / attempt / timestamp', () => {
  const a = eventFingerprint('node_finished', {
    node_id: 'N1', attempt: 1, timestamp: 100,
  });
  const b = eventFingerprint('node_finished', {
    node_id: 'N1', attempt: 2, timestamp: 100,
  });
  assert.notEqual(a, b, 'attempt 不同却得到相同指纹');
});

test('缺少可选字段时用空串占位，不产生 undefined', () => {
  const fp = eventFingerprint('task_started', {});
  assert.ok(!fp.includes('undefined'), `指纹里出现了 undefined：${fp}`);
});

test('字段顺序不同不影响指纹（按固定顺序取值而非拼接对象）', () => {
  const a = eventFingerprint('k', { node_id: 'N1', attempt: 1, timestamp: 5 });
  const b = eventFingerprint('k', { timestamp: 5, attempt: 1, node_id: 'N1' });
  assert.equal(a, b);
});

/* ------------------------------------------------------------------ *
 * 去重：核心场景
 * ------------------------------------------------------------------ */

test('首次事件不算重复', () => {
  const d = new EventDedup();
  assert.equal(d.isDuplicate('node_started', { node_id: 'N1', attempt: 1, timestamp: 1 }), false);
});

test('同一条事件第二次到达被判为重复（重连回放场景）', () => {
  const d = new EventDedup();
  const ev = { node_id: 'N1', attempt: 1, timestamp: 1 };
  assert.equal(d.isDuplicate('node_started', ev), false);
  assert.equal(d.isDuplicate('node_started', ev), true, '重连后的重复投递未被识别');
});

test('回退重跑产生的新事件不算重复（attempt 递增）', () => {
  const d = new EventDedup();
  // 这是最危险的误判：只看 (kind, node_id) 会把"驳回后重跑"当成重复丢掉，
  // 用户就看不到第二次尝试的结果了。
  assert.equal(d.isDuplicate('node_started', { node_id: 'N1', attempt: 1, timestamp: 1 }), false);
  assert.equal(d.isDuplicate('node_finished', { node_id: 'N1', attempt: 1, timestamp: 2 }), false);
  assert.equal(d.isDuplicate('node_started', { node_id: 'N1', attempt: 2, timestamp: 3 }), false,
    'attempt=2 的重跑被误判为重复 —— 用户会丢失第二次尝试的记录');
  assert.equal(d.isDuplicate('node_finished', { node_id: 'N1', attempt: 2, timestamp: 4 }), false);
});

test('不同节点的同类事件不算重复', () => {
  const d = new EventDedup();
  assert.equal(d.isDuplicate('node_started', { node_id: 'N1', attempt: 1, timestamp: 1 }), false);
  assert.equal(d.isDuplicate('node_started', { node_id: 'N2', attempt: 1, timestamp: 1 }), false,
    'N1 与 N2 被判为同一条事件');
});

test('重复计数被记录（便于诊断）', () => {
  const d = new EventDedup();
  const ev = { node_id: 'N1', attempt: 1, timestamp: 1 };
  d.isDuplicate('node_started', ev);
  d.isDuplicate('node_started', ev);
  d.isDuplicate('node_started', ev);
  assert.equal(d.duplicates, 2);
});

/* ------------------------------------------------------------------ *
 * 去重：边界
 * ------------------------------------------------------------------ */

test('有界 —— 超过 limit 后最旧的指纹被淘汰，内存不无限增长', () => {
  const d = new EventDedup(10);
  for (let i = 0; i < 50; i += 1) {
    d.isDuplicate('k', { node_id: `N${i}`, timestamp: i });
  }
  assert.ok(d.seen.size <= 10, `指纹集合超出上限：${d.seen.size}`);
  assert.equal(d.order.length, 10);
});

test('淘汰后旧指纹重新出现会被当作新事件（有界集合的既定代价）', () => {
  const d = new EventDedup(3);
  d.isDuplicate('k', { node_id: 'A', timestamp: 1 });
  d.isDuplicate('k', { node_id: 'B', timestamp: 2 });
  d.isDuplicate('k', { node_id: 'C', timestamp: 3 });
  d.isDuplicate('k', { node_id: 'D', timestamp: 4 }); // 淘汰 A
  // A 被淘汰了，所以再来一次不判重 —— 这是有界集合的代价，
  // 实践中不会发生（重连重发的是整个缓冲，不会跨过 limit）。
  assert.equal(d.isDuplicate('k', { node_id: 'A', timestamp: 1 }), false);
});

test('reset 清空状态（切换任务时必须调用）', () => {
  const d = new EventDedup();
  d.isDuplicate('k', { node_id: 'N1', timestamp: 1 });
  d.reset();
  assert.equal(d.seen.size, 0);
  assert.equal(d.duplicates, 0);
  assert.equal(d.isDuplicate('k', { node_id: 'N1', timestamp: 1 }), false,
    'reset 后同一条事件仍被判为重复 —— 切任务会丢事件');
});

test('模拟完整重连：历史事件整段重发，只有新增事件通过', () => {
  const d = new EventDedup();
  const history = [
    ['task_started', { timestamp: 1 }],
    ['node_started', { node_id: 'N1', attempt: 1, timestamp: 2 }],
    ['node_verdict', { node_id: 'N1', attempt: 1, timestamp: 3 }],
    ['node_finished', { node_id: 'N1', attempt: 1, timestamp: 4 }],
  ];

  const first = history.filter(([k, p]) => !d.isDuplicate(k, p));
  assert.equal(first.length, 4, '首次订阅应全部通过');

  // 断线重连 → 服务端回放同样的历史 + 一条新事件
  const replayed = [...history, ['node_started', { node_id: 'N2', attempt: 1, timestamp: 5 }]];
  const accepted = replayed.filter(([k, p]) => !d.isDuplicate(k, p));

  assert.equal(accepted.length, 1, `重连后应只接受 1 条新事件，实际 ${accepted.length}`);
  assert.equal(accepted[0][1].node_id, 'N2', '接受的应该是新节点 N2 的事件');
  assert.equal(d.duplicates, 4, '应有 4 条被判为重复');
});

/* ------------------------------------------------------------------ *
 * 回放窗口
 * ------------------------------------------------------------------ */

test('未 begin 时不判定为回放（避免把实时事件误标）', () => {
  const w = new ReplayWindow(1000);
  assert.equal(w.isReplaying(0), false);
});

test('订阅后窗口内到达的事件算回放', () => {
  const w = new ReplayWindow(1000);
  w.begin(0);
  assert.equal(w.isReplaying(10), true);
  assert.equal(w.isReplaying(999), true);
});

test('窗口结束后到达的事件不算回放', () => {
  const w = new ReplayWindow(1000);
  w.begin(0);
  assert.equal(w.isReplaying(1001), false);
  assert.equal(w.isReplaying(50_000), false);
});

test('restart 重新开启窗口（重连会触发第二次回放）', () => {
  const w = new ReplayWindow(1000);
  w.begin(0);
  assert.equal(w.isReplaying(5000), false, '窗口已过，不应再算回放');
  w.restart(5000);
  assert.equal(w.isReplaying(5100), true, '重连后重新回放，应被标为 replayed');
});

test('默认 grace 为 1.5s', () => {
  const w = new ReplayWindow();
  w.begin(0);
  assert.equal(w.isReplaying(1400), true);
  assert.equal(w.isReplaying(1600), false);
});

/* ------------------------------------------------------------------ *
 * 结果
 * ------------------------------------------------------------------ */

if (failures.length) {
  console.error(`\n✗ ${failures.length} 个事件流测试失败（通过 ${passed}）：\n`);
  for (const { name, err } of failures) {
    console.error(`  ✗ ${name}`);
    console.error(`    ${err.message.split('\n')[0]}`);
  }
  process.exit(1);
}
console.log(`✓ 事件流测试全部通过（${passed} 个）`);
