/**
 * 模块图与样式加载的**静态一致性检查**（零依赖，node 直跑）。
 *
 * 这一组对应本项目最贵的一类缺陷：文件存在、语法正确、逻辑正确，
 * 但**根本没被加载**。已经真实发生过两次：
 *   1. `web/styles.css` 里的 DAG 规则无人引用 → 所有节点黑底黑字，
 *      状态色与图例全部失效（没有任何报错）；
 *   2. 相对导入路径写错 → 浏览器里 404、页面白屏，而 node 里毫无迹象。
 *
 * 因此这里做三件"部署视角"的断言：入口脚本唯一、导入图连通、样式表真被加载。
 *
 * 运行：`node web/imports.test.js`
 */

import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ENTRY = join(HERE, 'js', 'main.js');

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

/** 递归收集 web/ 下的 .js 文件（跳过 *.test.js 自身）。 */
function listModules(dir) {
  const out = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) out.push(...listModules(p));
    else if (e.name.endsWith('.js') && !e.name.endsWith('.test.js')) out.push(p);
  }
  return out;
}

/** 抽取一个文件里的相对导入/再导出路径。 */
function relativeSpecifiers(absPath) {
  const src = readFileSync(absPath, 'utf8');
  const out = [];
  for (const m of src.matchAll(/(?:from|import)\s*['"](\.[^'"]+)['"]/g)) out.push(m[1]);
  return out;
}

/** index.html 真正加载的样式表（绝对路径）。 */
function loadedStylesheets() {
  const html = readFileSync(join(HERE, 'index.html'), 'utf8');
  return [...html.matchAll(/<link[^>]+rel=["']stylesheet["'][^>]*>/gi)]
    .map((m) => /href=["']([^"']+)["']/i.exec(m[0]))
    .filter(Boolean)
    .map((m) => join(HERE, m[1].replace(/^\.\//, '')));
}

const MODULES = listModules(HERE);

test('模块图非空（防止路径写错导致整套检查空跑）', () => {
  assert.ok(MODULES.length >= 12, `只找到 ${MODULES.length} 个模块，收集逻辑可能失效`);
});

test('每个相对导入都指向真实存在的文件（写错在浏览器里就是 404 白屏）', () => {
  const missing = [];
  for (const m of MODULES) {
    for (const spec of relativeSpecifiers(m)) {
      if (!existsSync(resolve(dirname(m), spec))) {
        missing.push(`${relative(HERE, m)} → ${spec}`);
      }
    }
  }
  assert.deepEqual(missing, [], `以下相对导入不存在：\n    ${missing.join('\n    ')}`);
});

test('所有模块都能从 js/main.js 顺着 import 图走到（没有孤儿模块）', () => {
  assert.ok(existsSync(ENTRY), '入口 js/main.js 不存在');

  const seen = new Set();
  const queue = [resolve(ENTRY)];
  while (queue.length) {
    const cur = queue.pop();
    if (seen.has(cur) || !existsSync(cur)) continue;
    seen.add(cur);
    for (const spec of relativeSpecifiers(cur)) queue.push(resolve(dirname(cur), spec));
  }

  // 不可达的模块 = 又一次"孤儿文件"事故（web/app.js 就是这么来的：
  // 27KB 的旧前端无人引用，却还在被人的注释与文档当作现状描述）
  const orphans = MODULES
    .map((m) => resolve(m))
    .filter((m) => !seen.has(m))
    .map((m) => relative(HERE, m))
    .sort();

  assert.deepEqual(orphans, [], `以下模块没有被任何入口引用（孤儿文件）：\n    ${orphans.join('\n    ')}`);
});

test('index.html 加载的样式表里确实定义了 DAG 节点样式', () => {
  const css = loadedStylesheets().map((p) => readFileSync(p, 'utf8')).join('\n');
  for (const cls of ['.node-bg', '.node-accent', '.node-dot', '.s-ok', '.s-backtrack', '.legend .dot-ok']) {
    assert.ok(css.includes(cls),
      `加载中的样式表里没有 ${cls} —— 图形会退化成 SVG 默认的黑底黑字`);
  }
});

test('所有 var(--token) 引用都有对应的自定义属性定义', () => {
  // `var(--text)` 写错一个字母不会报错，只会让那条声明**整条失效**
  // （浏览器把无效的 var() 当作 "unset"）。这与孤儿样式属于同一类
  // "静默失效"，所以一并纳入静态检查。
  // 旧 styles.css 的那套令牌（--bg-panel/--text-dim/--accent…）正是
  // 不能被搬运过来的原因：新皮肤里根本没有这些名字。
  const files = loadedStylesheets();
  const css = files.map((p) => readFileSync(p, 'utf8')).join('\n');

  const defined = new Set([...css.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
  const used = new Set([...css.matchAll(/var\(\s*(--[\w-]+)/g)].map((m) => m[1]));

  const missing = [...used].filter((t) => !defined.has(t)).sort();
  assert.deepEqual(missing, [],
    `以下 CSS 变量被使用但从未定义（相关声明会整条失效）：\n    ${missing.join('\n    ')}`);
  assert.ok(defined.size > 50, `只解析出 ${defined.size} 个自定义属性，解析逻辑可能失效`);
});

test('index.html 的脚本入口唯一（两套应用同时启动是最难查的状态污染）', () => {
  const html = readFileSync(join(HERE, 'index.html'), 'utf8');
  const scripts = [...html.matchAll(/<script[^>]*src=["']([^"']+)["']/gi)].map((m) => m[1]);
  assert.deepEqual(scripts, ['./js/main.js']);
});

test('被删除的孤儿文件确实不存在（防止有人把它们从历史里恢复）', () => {
  for (const legacy of ['app.js', 'styles.css']) {
    assert.ok(!existsSync(join(HERE, legacy)),
      `web/${legacy} 又出现了：它是零引用的旧前端，恢复它会造成两套样式/两套逻辑`);
  }
});

/* ------------------------------------------------------------------ *
 * HTML 注入面（XSS）的静态审计
 * ------------------------------------------------------------------ *
 * 动态审计（`alertBodyOptions` 的纯函数断言）只能覆盖被抽出来的那一处决策；
 * 静态审计负责回答"全仓还有没有别的地方把数据当标记"。两者缺一不可：
 * 前者证明修好了，后者防止明天又长出一处。
 * ------------------------------------------------------------------ */

/**
 * 允许出现在 `html:` 右侧的表达式白名单。
 *
 * 每一项都必须是**编译期常量或静态图标输出**：
 *   - 字符串/模板字面量：写成源码里的固定文案；
 *   - `icon(...)`：icons.js 的静态 SVG（入参在下方单独断言）；
 *   - `<常量对象>.icon`：图标名，来自本文件里的字面量选项数组；
 *   - `''` / `0` 之类的空值。
 *
 * 若将来确实需要真实富文本，请**传节点**（`el('div', {}, [node])`）
 * 而不是往这个白名单里加数据。
 */
const HTML_SINK_ALLOWED = [
  /^icon\(/,
  /^['"`]/, // 直接量（含模板字符串）
  /^(?:o|layer|opt|it|item|r|c|cfg|def|row)?\??\.?(?:icon)\b/, // 常量对象上的图标名
  /^''$|^""$/,
  // util.js 是 el()/fromHTML() 的**实现本体**，它自己的 innerHTML 赋值正是
  // 那个唯一的受控入口（下方另有断言保证它只接受显式 opts.html）。
  /^opts\.html$/,
  /^html\.trim\(\)$/,
];

/** 只关心**数据**来源。`name` 这类形参名不在其中：它由字面量实参传入。 */
const DATA_ARG = /\b(goal|lastError|last_error|detail|err|error|output|query)\b/;

/** `icon(...)` 的定义处不是调用（参数是形参），要排除。 */
const ICON_DECLARATION = /(?:function\s+icon|icon\s*=\s*\()/;

/** `el(..., { html: X })` 与直接赋值 innerHTML 都算注入面。 */
const HTML_SINK_PATTERNS = [
  // 匹配到行尾/逗号/换行为止，够用来做"这个表达式看起来是不是数据"
  /html:\s*([^,}\n]+)/g,
  /innerHTML\s*=\s*([^;\n]+)/g,
  /insertAdjacentHTML\s*\([^,]+,\s*([^)\n]+)/g,
];

test('web/ 下没有把动态数据当 HTML 解析的地方', () => {
  const offenders = [];
  for (const m of MODULES) {
    const src = readFileSync(m, 'utf8');
    // 注释行里的示例不算
    const code = src.replace(/^\s*\*.*$/gm, '').replace(/\/\/.*$/gm, '');
    for (const re of HTML_SINK_PATTERNS) {
      for (const hit of code.matchAll(re)) {
        const expr = hit[1].trim();
        if (HTML_SINK_ALLOWED.some((ok) => ok.test(expr))) continue;
        offenders.push(`${relative(HERE, m)}: ${expr}`);
      }
    }
  }
  assert.deepEqual(offenders, [],
    '以下位置把表达式的值当 HTML 解析。凡是模型/用户可影响的数据必须用 '
    + `text（或先过 esc()）：\n    ${offenders.join('\n    ')}`);
});

test('icon() 的入参全部是常量 —— 否则静态 SVG 的"可信"前提不成立', () => {
  // icon(name, size, className) 会把实参直接拼进 SVG 字符串，
  // 所以只要有一个实参来自数据，它就不再是"静态图标输出"而成了注入点。
  // 这条检查是启发式的（按形参名匹配数据来源），但它足以拦住
  // "顺手把 goal / lastError 传进去当图标名"这种写法。
  const offenders = [];
  for (const m of MODULES) {
    const src = readFileSync(m, 'utf8')
      .replace(/^\s*\*.*$/gm, '')
      .replace(/\/\/.*$/gm, '');
    for (const hit of src.matchAll(/\bicon\(([^)]*)\)/g)) {
      const args = hit[1];
      // 定义处（形参）不是调用点
      if (ICON_DECLARATION.test(hit[0])) continue;
      if (/\$\{/.test(args) || DATA_ARG.test(args)) {
        offenders.push(`${relative(HERE, m)}: icon(${args})`);
      }
    }
  }
  assert.deepEqual(offenders, [],
    `icon() 收到了可能来自数据的实参：\n    ${offenders.join('\n    ')}`);
});

test('util.el 的 html 分支只接受显式传入的 opts.html（没有隐式兜底）', () => {
  // 防回归：如果哪天 el() 被改成 `node.innerHTML = opts.html ?? opts.text`，
  // 所有用 text 的地方都会变成注入点，而单看调用方完全看不出来。
  const src = readFileSync(join(HERE, 'js', 'util.js'), 'utf8');
  const line = src.split('\n').find((l) => l.includes('innerHTML'));
  assert.ok(line && /opts\.html/.test(line),
    `util.js 的 innerHTML 赋值不再直接使用 opts.html：${line}`);
  assert.ok(!/opts\.html\s*\?\?/.test(src), 'html 分支出现了 ?? 兜底 —— 会把 text 数据当标记解析');
});

if (failures.length) {
  console.error(`\n✗ ${failures.length} 个测试失败（通过 ${passed}）：\n`);
  for (const { name, err } of failures) {
    console.error(`  ✗ ${name}`);
    console.error(`    ${err.message.split('\n').join('\n    ')}`);
  }
  process.exit(1);
}
console.log(`✓ 导入图/样式加载检查全部通过（${passed} 个）`);
