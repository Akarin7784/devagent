/**
 * 指标解析与格式化函数的测试（零依赖，直接用 node 跑）。
 *
 * ## 为什么单独测这几个函数
 *
 * `util.js` 里的 `metric / histOverallMean / histMaxQuantile` 是**唯一**
 * 把后端 OTLP 指标结构翻译成人话的地方，也是整个前端最容易静默出错的地方：
 *
 *   - 指标名写错 → 页面不报错，只是显示 0 或「—」，没人会发现；
 *   - 直方图是三层嵌套（名称 → 标签 → 统计量），取错层会得到 NaN；
 *   - 真实后端**没有** `devagent_` 前缀，早期按前缀硬编码的写法全部落空。
 *
 * 这类 bug 不会被 `node --check` 抓到，也不会让页面崩溃 —— 它只会让
 * 看板悄悄显示错误结论。所以必须用测试钉死。
 *
 * 运行：`node web/format.test.js`（或 `make web-check`）
 */

import assert from 'node:assert/strict';

import {
  metric, metricKey, sumLabels, histMean, histOverallMean, histMaxQuantile,
  fmtInt, fmtPct, esc,
} from './js/util.js?v=20261008-live';

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    failures.push({ name, err });
    console.log(`  ✗ ${name}\n      ${err.message}`);
  }
}

console.log('指标解析（真实后端结构）');

test('metric：精确命中无前缀指标名', () => {
  const counters = { llm_calls: 12, llm_tokens: 3400 };
  assert.equal(metric(counters, 'llm_calls'), 12);
  assert.equal(metric(counters, 'llm_tokens'), 3400);
});

test('metric：兼容带 devagent_ 前缀的历史命名', () => {
  assert.equal(metric({ devagent_llm_cost_usd: 1.25 }, 'llm_cost_usd'), 1.25);
});

test('metric：未命中返回 undefined 而不是抛错', () => {
  assert.equal(metric({ llm_calls: 1 }, 'nope'), undefined);
});

test('metric：非对象输入安全（null/undefined/字符串）', () => {
  assert.equal(metric(null, 'x'), undefined);
  assert.equal(metric(undefined, 'x'), undefined);
  assert.equal(metric('oops', 'x'), undefined);
});

test('metric：值为 0 时不被当成缺失', () => {
  // 0 是合法计数（例如 hallucination_blocked=0），不能被短路成 undefined
  assert.equal(metric({ hallucination_blocked: 0 }, 'hallucination_blocked'), 0);
});

test('metricKey：返回真实键名，便于展示原始标签', () => {
  assert.equal(metricKey({ devagent_backtracks: 3 }, 'backtracks'), 'devagent_backtracks');
  assert.equal(metricKey({ backtracks: 3 }, 'backtracks'), 'backtracks');
});

test('sumLabels：把所有标签的值加总', () => {
  assert.equal(sumLabels({ 'agent="coder"': 2, 'agent="tester"': 3.5 }), 5.5);
});

test('sumLabels：空/非对象返回 0', () => {
  assert.equal(sumLabels({}), 0);
  assert.equal(sumLabels(null), 0);
});

console.log('\n直方图三层结构');

test('histMean：优先使用后端给出的 mean', () => {
  assert.equal(histMean({ count: 4, sum: 10, mean: 0.75 }), 0.75);
});

test('histMean：无 mean 时回退 sum / count', () => {
  assert.equal(histMean({ count: 4, sum: 10 }), 2.5);
});

test('histMean：无数据返回 null（供 UI 显示「—」）', () => {
  assert.equal(histMean({}), null);
  assert.equal(histMean(null), null);
});

test('histOverallMean：跨标签按 count 加权，而非简单平均', () => {
  // 10 次 × 0.5 与 90 次 × 1.0 的正确结果是 0.95；简单平均会得到 0.75
  const h = { 'agent="a"': { count: 10, mean: 0.5 }, 'agent="b"': { count: 90, mean: 1.0 } };
  assert.equal(histOverallMean(h), 0.95);
});

test('histOverallMean：缺少 count 的标签按权重 1 计入（不丢真实数据）', () => {
  // 宁可低估权重，也不要因为后端漏给 count 就把整个标签丢掉
  const h = { 'agent="a"': { count: 10, mean: 0.5 }, 'agent="b"': { mean: 0.9 } };
  assert.equal(histOverallMean(h), (0.5 * 10 + 0.9 * 1) / 11);
});

test('histOverallMean：完全无数据返回 null', () => {
  assert.equal(histOverallMean({}), null);
  assert.equal(histOverallMean(null), null);
});

test('histMaxQuantile：取最坏标签的分位数（暴露长尾）', () => {
  const h = { 'agent="a"': { p90: 0.4 }, 'agent="b"': { p90: 0.98 } };
  assert.equal(histMaxQuantile(h, 'p90'), 0.98);
});

test('histMaxQuantile：标签缺失该分位数时跳过', () => {
  const h = { 'agent="a"': { p90: 0.4 }, 'agent="b"': { p50: 0.9 } };
  assert.equal(histMaxQuantile(h, 'p90'), 0.4);
});

test('histMaxQuantile：全无数据返回 null', () => {
  assert.equal(histMaxQuantile({}, 'p90'), null);
});

console.log('\n格式化与转义');

test('fmtInt：千分位', () => {
  assert.equal(fmtInt(1234567), '1,234,567');
  assert.equal(fmtInt(0), '0');
});

test('fmtInt：非数字输入不产生 NaN 字样', () => {
  assert.ok(!/NaN/.test(fmtInt(undefined)));
  assert.ok(!/NaN/.test(fmtInt(null)));
});

test('fmtPct：小数转百分比', () => {
  assert.ok(fmtPct(0.5).includes('50'));
  assert.ok(!/NaN/.test(fmtPct(null)));
});

test('esc：转义 HTML 特殊字符，阻断注入', () => {
  assert.equal(esc('<script>'), '&lt;script&gt;');
  assert.equal(esc('a & b'), 'a &amp; b');
  assert.equal(esc(`"x" 'y'`), '&quot;x&quot; &#39;y&#39;');
});

console.log('\n哈希路由解析');

// parseHash 依赖 window.location.hash，这里提供一个最小替身
const fakeWindow = { location: { hash: '#/' } };
globalThis.window = fakeWindow;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
const { parseHash, ROUTES } = await import('./js/store.js?v=20261008-live');

const withHash = (h, fn) => { fakeWindow.location.hash = h; fn(parseHash()); };

test('合法路由正常解析', () => {
  withHash('#/workbench', (r) => { assert.equal(r.id, 'workbench'); });
});

test('路由带查询参数', () => {
  withHash('#/workbench?task=abc', (r) => {
    assert.equal(r.id, 'workbench');
    assert.equal(r.params.get('task'), 'abc');
  });
});

test('空 hash 与 #/ 回退到默认路由', () => {
  withHash('', (r) => { assert.equal(r.id, ROUTES[0].id); });
  withHash('#/', (r) => { assert.equal(r.id, ROUTES[0].id); });
});

test('页内锚点不冒充路由（skip-link 回归）', () => {
  // 这是真实 bug：skip-link 指向 #page-root，此前会被当成未知路由
  // 回退到 overview，导致「跳转到主内容」实际跳转了首页。
  withHash('#page-root', (r) => {
    assert.equal(r.id, null, '不应解析为路由');
    assert.equal(r.anchor, 'page-root', '应保留锚点信息');
  });
});

test('未知路由 id 返回 null 而非静默回退默认页', () => {
  withHash('#/does-not-exist', (r) => {
    assert.equal(r.id, null);
    assert.equal(r.anchor, 'does-not-exist');
  });
});

test('所有已注册路由自身都能被解析回来', () => {
  for (const route of ROUTES) {
    withHash(`#/${route.id}`, (r) => { assert.equal(r.id, route.id, `路由 ${route.id} 解析失败`); });
  }
});

console.log('');
if (failures.length) {
  console.log(`✗ 指标/格式化测试失败 ${failures.length} 个（通过 ${passed} 个）`);
  process.exit(1);
}
console.log(`✓ 指标/格式化测试全部通过（${passed} 个）`);
