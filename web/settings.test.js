import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { CONFIG_GROUPS, buildChanges, configFields, parseConfigValue } from './js/backend-settings.js?v=20261008-live';
import { api, getApiKey, parseSSEBlock, request, setApiBase, setApiKey, subscribeTaskEvents } from './js/api.js?v=20261008-live';

const { schema, presets } = JSON.parse(readFileSync(new URL('./test_settings_schema.json', import.meta.url), 'utf8'));
let passed = 0;
async function test(name, fn) {
  try { await fn(); passed += 1; }
  catch (error) { console.error(`✗ ${name}: ${error.message}`); process.exitCode = 1; }
}

await test('后端模型的全部分类都能编辑，新增分类不会静默漏掉', () => {
  const categories = CONFIG_GROUPS.map(([id]) => id).filter((id) => id !== 'application');
  const objects = Object.entries(schema.properties).filter(([, item]) => item.$ref).map(([key]) => key).filter((key) => key !== 'models');
  assert.deepEqual(categories.sort(), objects.sort());
  const application = configFields(schema, 'application').map((item) => item.key);
  assert.deepEqual(application.sort(), Object.entries(schema.properties).filter(([, item]) => !item.$ref).map(([key]) => key).sort());
});
await test('每一分类都覆盖对应的全部后端字段', () => {
  for (const [group] of CONFIG_GROUPS.filter(([id]) => id !== 'application')) {
    const definition = schema.$defs[schema.properties[group].$ref.split('/').pop()];
    assert.deepEqual(configFields(schema, group).map((item) => item.key).sort(), Object.keys(definition.properties).sort());
  }
});
await test('包含国内外主流供应商、云平台与本地服务，ID 唯一', () => {
  assert.equal(new Set(presets.map((p) => p.id)).size, presets.length);
  for (const id of ['openai', 'anthropic', 'gemini', 'azure', 'bedrock', 'vertex', 'deepseek', 'qwen', 'zhipu', 'moonshot', 'minimax', 'doubao', 'hunyuan', 'baidu', 'stepfun', 'spark', 'groq', 'mistral', 'xai', 'siliconflow', 'openrouter', 'ollama', 'lmstudio', 'custom']) {
    assert.ok(presets.some((p) => p.id === id), `Missing ${id}`);
  }
});
await test('Anthropic 使用原生协议，本地服务不要求伪造密钥', () => {
  assert.equal(presets.find((p) => p.id === 'anthropic').config.protocol, 'anthropic');
  assert.equal(presets.find((p) => p.id === 'ollama').config.auth_mode, 'none');
});
await test('仅提交改动，未编辑的密钥不进入补丁', () => {
  assert.deepEqual(buildChanges(new Map([['context.default_budget', 32000], ['models.providers.openai.timeout_seconds', 30]])), {
    context: { default_budget: 32000 }, models: { providers: { openai: { timeout_seconds: 30 } } },
  });
});
await test('明确清除密钥保留 null，不吞掉 false 或零', () => {
  assert.deepEqual(buildChanges(new Map([['models.qwen.api_key', null], ['cache.enabled', false], ['routing.weight_tool_calls', 0]])), {
    models: { qwen: { api_key: null } }, cache: { enabled: false }, routing: { weight_tool_calls: 0 },
  });
});
await test('拒绝可污染原型的配置路径', () => assert.throws(() => buildChanges([['models.providers.__proto__.api_key', 'x']])));
await test('空数字、小数整数、无穷值与越界值不能保存', () => {
  for (const value of ['', '1.5', 'Infinity', '0', '11']) assert.throws(() => parseConfigValue(value, { type: 'integer', exclusiveMinimum: 0, maximum: 10 }));
  assert.equal(parseConfigValue('3', { type: 'integer', minimum: 0 }), 3);
});
await test('列表和可选字段遵循后端类型', () => {
  assert.deepEqual(parseConfigValue('http://localhost:5173\nhttps://example.com', { type: 'array' }), ['http://localhost:5173', 'https://example.com']);
  assert.equal(parseConfigValue('', { type: 'string' }, true), null);
});
await test('SSE 正确解析多行 data 和命名事件', () => {
  assert.deepEqual(parseSSEBlock('event: node_started\ndata: {\ndata: "node_id":"N1"}\n: ping'), { kind: 'node_started', data: '{\n"node_id":"N1"}' });
});

globalThis.window = { setTimeout, clearTimeout };
await test('API Key 只放请求头，不跟随重定向，不写到 URL', async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response('{}'); };
  setApiBase('http://localhost:8000'); setApiKey('session-key'); await api.getSettings();
  assert.equal(calls[0].options.headers['X-API-Key'], 'session-key');
  assert.equal(calls[0].options.redirect, 'error');
  assert.ok(!calls[0].url.includes('session-key'));
  setApiBase('http://localhost:8001'); assert.equal(getApiKey(), '');
});
await test('鉴权事件流处理跨数据块 UTF-8，结束后不重连', async () => {
  const bytes = new TextEncoder().encode('event: node_started\r\ndata: {"node_id":"N1","goal":"中文"}\r\n\r\nevent: task_finished\r\ndata: {"status":"succeeded"}\r\n\r\n');
  let headers;
  globalThis.fetch = async (_url, options) => {
    headers = options.headers;
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(bytes.slice(0, 62)); controller.enqueue(bytes.slice(62)); controller.close();
    } }));
  };
  setApiKey('sse-key');
  const events = [];
  await new Promise((resolve, reject) => {
    subscribeTaskEvents('T', { onEvent: (event) => events.push(event), onClose: resolve, onError: reject });
  });
  assert.equal(headers['X-API-Key'], 'sse-key');
  assert.equal(events[0].goal, '中文'); assert.equal(events[1].kind, 'task_finished');
  setApiKey('');
});
await test('后端校验失败保留服务端错误消息', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: '配置已更新' }), { status: 409 });
  await assert.rejects(() => request('/settings', { method: 'PUT', body: {} }), /配置已更新/);
});
console.log(`✓ 后端设置与供应商测试全部通过（${passed} 个）`);
