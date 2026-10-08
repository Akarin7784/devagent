/** Schema-driven backend editor. Credentials stay in memory and are never persisted in the browser. */
import { api } from './api.js?v=20261008-live';
import { alert, button, card, toast } from './components.js?v=20261008-live';
import { clear, el } from './util.js?v=20261008-live';

export const CONFIG_GROUPS = [
  ['application', '服务与访问'], ['routing', '模型路由'], ['cache', '模型缓存'],
  ['context', '上下文工程'], ['reliability', '可靠性与预算'], ['sandbox', '测试沙箱'],
  ['security', 'API 访问控制'], ['observability', '可观测性'], ['evaluation', '评测'],
  ['storage', '任务存储'], ['database', '数据库'], ['redis', 'Redis'],
];

const LABELS = {
  env: '运行环境', debug: '调试错误详情', log_level: '日志级别', api_host: 'API 监听地址',
  api_port: 'API 监听端口', cors_origins: '允许的跨域来源', web_dir: '前端静态目录',
  api_key: 'API Key', base_url: 'API 基址', timeout_seconds: '请求超时（秒）', max_retries: '最大重试次数',
  protocol: '接口协议', auth_mode: '认证方式', send_temperature: '发送 temperature 参数',
  max_tokens_field: '输出长度参数', input_price_per_million: '输入单价（USD / 百万 token）',
  output_price_per_million: '输出单价（USD / 百万 token）',
  small_model: '小模型', medium_model: '中模型', large_model: '大模型', embedding_model: '嵌入模型',
  weight_reasoning_depth: '推理深度权重', weight_context_size: '上下文长度权重',
  weight_tool_calls: '工具调用权重', weight_retry_history: '重试历史权重',
  threshold_small_upper: '小模型复杂度上界', threshold_medium_upper: '中模型复杂度上界',
  enabled: '启用', semantic: '启用语义缓存', similarity_threshold: '语义相似度阈值',
  max_entries: '缓存条目上限', model_filter: '按模型隔离缓存', default_budget: '默认上下文 token 预算',
  compression_threshold: '压缩触发比例', hard_constraint_untouchable: '保持硬约束不压缩',
  weight_relevance: '相关性权重', weight_recency: '时效性权重', weight_dependency: '依赖权重',
  weight_density: '信息密度权重', weight_redundancy: '冗余惩罚权重', recency_lambda: '时效衰减系数',
  redundancy_cosine_threshold: '冗余相似度阈值', redundancy_gamma: '冗余惩罚指数',
  weight_trust: '内容信任权重', injection_guard: '不可信内容边界防护',
  max_task_tokens: '单任务 token 上限', max_task_steps: '单任务步骤上限',
  max_backtrack_depth: '回退链深度上限', same_failure_threshold: '重复失败升级阈值',
  checkpoint_enabled: '启用检查点', image: '沙箱镜像', memory_limit: '内存限制', cpu_limit: 'CPU 限额',
  pids_limit: '进程数上限', network_disabled: '禁止沙箱网络', read_only_root: '只读容器根目录',
  workspace_mount: '测试基线目录', allow_local_fallback: 'Docker 不可用时允许本地执行', user: '容器用户',
  tracing_enabled: '启用链路追踪', otlp_endpoint: 'OTLP 导出地址', log_json: 'JSON 日志',
  metrics_enabled: '启用指标', capture_llm_payloads: '记录完整模型输入与输出',
  golden_set_path: '默认评测数据集', dataset_dir: '可读取的数据集目录', judge_model: '主裁判模型',
  enable_bidirectional_judge: '双向评估', reference_judge_model: '参考裁判模型',
  calibration_sample_limit: '标定样本上限（0 为全部）', report_dir: '评测报告目录',
  backend: '存储后端', memory_capacity: '内存任务容量', event_history_limit: 'SQL 事件回放上限',
  url: '连接地址', echo: '输出 SQL 日志', pool_size: '连接池大小', max_overflow: '额外连接上限',
  stream_max_len: '事件流长度上限',
};
const GROUP_HINTS = {
  application: '监听地址、端口与跨域策略在重启后生效。',
  routing: '模型格式为 供应商ID:模型名。可在模型供应商页直接设置路由。',
  cache: '语义缓存会调用嵌入模型，需配置可用的 embedding_model。',
  sandbox: '本地执行没有容器隔离；网络与资源限制由 Docker 后端执行。',
  security: '新的访问密钥在重启后生效，届时在后端连接页填写会话访问密钥。',
  observability: '记录完整输入和输出会包含任务内容，请按部署环境选择。',
  storage: 'memory 模式重启会清空任务，sql 模式需要数据库与相应依赖。',
  database: '连接地址可能含密码；已有地址不回显，留空保留。',
  redis: '连接地址可能含密码；已有地址不回显，留空保留。',
};

export function resolveSchema(schema, root) {
  if (schema.$ref) return root.$defs[schema.$ref.split('/').pop()];
  if (schema.anyOf) return { ...schema.anyOf.find((item) => item.type !== 'null'), ...schema, anyOf: undefined };
  return schema;
}

export function configFields(schema, group) {
  const properties = group === 'application'
    ? Object.fromEntries(Object.entries(schema.properties).filter(([, item]) => !resolveSchema(item, schema).properties))
    : resolveSchema(schema.properties[group], schema).properties;
  return Object.entries(properties || {}).map(([key, item]) => ({
    key, path: group === 'application' ? key : `${group}.${key}`,
    schema: resolveSchema(item, schema), nullable: !!item.anyOf?.some((s) => s.type === 'null'),
  }));
}

export function buildChanges(entries) {
  const patch = {};
  for (const [path, value] of entries) {
    const keys = path.split('.');
    if (keys.some((key) => ['__proto__', 'prototype', 'constructor'].includes(key))) throw new Error('配置路径无效');
    let cursor = patch;
    for (const key of keys.slice(0, -1)) cursor = cursor[key] ||= {};
    cursor[keys.at(-1)] = value;
  }
  return patch;
}

export function parseConfigValue(raw, schema, nullable = false) {
  if (raw === '' && nullable) return null;
  if (schema.type === 'integer' || schema.type === 'number') {
    if (raw.trim() === '') throw new Error('请输入数值');
    const value = Number(raw);
    if (!Number.isFinite(value) || (schema.type === 'integer' && !Number.isInteger(value))) throw new Error('数值格式不正确');
    if (schema.minimum != null && value < schema.minimum) throw new Error(`不得小于 ${schema.minimum}`);
    if (schema.exclusiveMinimum != null && value <= schema.exclusiveMinimum) throw new Error(`必须大于 ${schema.exclusiveMinimum}`);
    if (schema.maximum != null && value > schema.maximum) throw new Error(`不得大于 ${schema.maximum}`);
    if (schema.exclusiveMaximum != null && value >= schema.exclusiveMaximum) throw new Error(`必须小于 ${schema.exclusiveMaximum}`);
    return value;
  }
  if (schema.type === 'array') return raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
  return raw;
}

function readPath(data, path) { return path.split('.').reduce((value, key) => value?.[key], data); }
function writePath(data, path, value) {
  const keys = path.split('.'); let cursor = data;
  for (const key of keys.slice(0, -1)) cursor = cursor[key] ||= {};
  cursor[keys.at(-1)] = value;
}

/** One session per settings page preserves unsaved drafts while switching sections. */
export function renderBackendSettings(mode, session) {
  session.changes ||= new Map(); session.errors ||= new Map(); session.rawValues ||= new Map(); session.group ||= 'routing';
  const root = el('div', { class: 'backend-settings' });
  const pending = el('span', { class: 'hint-text', attrs: { 'aria-live': 'polite' } });
  const message = el('div');
  const content = el('fieldset', { class: 'backend-config-content' });
  session.notify = paint;
  const saveBtn = button('保存后端配置', { icon: 'check', variant: 'primary', onClick: save });
  const reloadBtn = button('重新载入', { icon: 'refresh', variant: 'secondary', onClick: async () => {
    if (session.changes.size) {
      toast({ tone: 'warning', title: '还有未保存的修改', desc: '先保存，或点击放弃修改。' }); return;
    }
    await load();
  } });
  const discardBtn = button('放弃修改', { variant: 'ghost', onClick: () => {
    session.changes.clear(); session.errors.clear(); session.rawValues.clear(); session.draft = structuredClone(session.view.values); paint();
  } });
  root.append(card({ title: mode === 'models' ? '模型接入与供应商预设' : '服务运行配置',
    subtitle: '配置保存在后端，重启服务后生效', body: el('div', {}, [
      el('div', { class: 'backend-settings-toolbar' }, [saveBtn, reloadBtn, discardBtn, pending]), message, content,
    ]),
  }));

  function update(path, value) {
    writePath(session.draft, path, value);
    if (JSON.stringify(readPath(session.view.values, path)) === JSON.stringify(value)) session.changes.delete(path);
    else session.changes.set(path, value);
    session.errors.delete(path); syncToolbar();
    session.rawValues.delete(path);
  }
  function syncToolbar() {
    pending.textContent = `${session.view?.runtime_mode === 'live' ? '真实模型模式 · ' : ''}${session.changes.size ? `${session.changes.size} 项待保存` : '没有未保存的修改'}`;
    saveBtn.disabled = !session.view || !session.changes.size || !!session.errors.size || !!session.saving;
    discardBtn.disabled = !session.changes.size || !!session.saving; reloadBtn.disabled = !!session.loading || !!session.saving;
    content.disabled = !!session.saving;
  }
  async function load() {
    session.loading = true; session.error = ''; syncToolbar(); clear(message);
    try {
      const view = await api.getSettings();
      session.view = view; session.draft = structuredClone(view.values);
      session.changes.clear(); session.errors.clear(); session.rawValues.clear(); session.notify?.();
    } catch (error) {
      session.error = error.message;
    } finally { session.loading = false; session.notify?.(); }
  }
  async function save() {
    if (session.saving || session.errors.size || !session.changes.size) return;
    session.saving = true; syncToolbar();
    session.error = '';
    try {
      const view = await api.saveSettings({ revision: session.view.revision, changes: buildChanges(session.changes) });
      session.view = view; session.draft = structuredClone(view.values);
      session.changes.clear(); session.errors.clear(); session.rawValues.clear(); session.notify?.();
      toast({ tone: 'success', title: '后端配置已保存', desc: '重启后生效，当前运行任务继续使用原配置。' });
    } catch (error) {
      session.error = error.message;
    } finally { session.saving = false; session.notify?.(); }
  }
  function field({ path, key, schema, nullable }) {
    const value = readPath(session.draft, path);
    const secret = path.endsWith('.api_key') || ['database.url', 'redis.url'].includes(path);
    const id = `cfg-${path.replaceAll('.', '-')}`;
    const hint = el('div', { class: 'field-hint', attrs: { id: `${id}-hint` },
      text: secret ? (session.view.secrets[path] ? '已保存 · 留空保留' : '尚未设置')
        : schema.type === 'array' ? '每行一项，或用逗号分隔。' : '',
    });
    let control;
    hint.hidden = !hint.textContent;
    const common = { id, 'aria-describedby': `${id}-hint`, title: schema.description || `DEVAGENT_${path.replaceAll('.', '__').toUpperCase()}` };
    if (schema.type === 'boolean') {
      control = el('input', { attrs: { ...common, type: 'checkbox' }, on: { change: () => update(path, control.checked) } });
      control.checked = !!value;
    } else if (schema.enum) {
      control = el('select', { class: 'input', attrs: common, on: { change: () => update(path, control.value) } },
        schema.enum.map((choice) => el('option', { text: choice, attrs: { value: choice } })));
      control.value = value;
    } else {
      control = el(schema.type === 'array' ? 'textarea' : 'input', {
        class: 'input mono', attrs: { ...common, type: secret ? 'password' : ['integer', 'number'].includes(schema.type) ? 'number' : 'text',
          autocomplete: secret ? 'new-password' : 'off', spellcheck: 'false', step: schema.type === 'integer' ? '1' : 'any',
          placeholder: secret && session.view.secrets[path] ? '已保存 · 留空保持' : nullable ? '留空表示未设置' : '',
        }, on: { input: () => {
          session.rawValues.set(path, control.value);
          try {
            if (secret && !control.value) {
              session.changes.delete(path); writePath(session.draft, path, null); session.errors.delete(path); session.rawValues.delete(path);
            } else update(path, parseConfigValue(control.value, schema, nullable));
            control.removeAttribute('aria-invalid');
            hint.textContent = secret ? '待保存' : ''; hint.hidden = !hint.textContent;
          } catch (error) {
            session.errors.set(path, error.message); control.setAttribute('aria-invalid', 'true'); hint.textContent = error.message; hint.hidden = false;
          }
          syncToolbar();
        } },
      });
      control.value = session.rawValues.get(path) ?? (Array.isArray(value) ? value.join('\n') : value ?? '');
      if (session.errors.has(path)) { control.setAttribute('aria-invalid', 'true'); hint.textContent = session.errors.get(path); hint.hidden = false; }
    }
    const row = el('div', { class: `backend-config-field${schema.type === 'boolean' ? ' backend-config-toggle' : ''}` }, [
      el('label', { class: 'label', attrs: { for: id }, text: LABELS[key] || schema.title || key }), control, hint,
    ]);
    if (secret && session.view.secrets[path] && path.endsWith('.api_key')) row.append(button('清除已保存密钥', {
      variant: 'ghost', small: true, onClick: () => {
        control.value = ''; update(path, nullable ? null : ''); hint.textContent = '待清除：保存并重启后生效。';
      },
    }));
    return row;
  }
  function paint() {
    if (session.disposed) return;
    clear(message); clear(content);
    if (session.error) message.append(alert({ tone: 'danger', title: '后端配置操作失败，修改已保留', body: session.error }));
    const view = session.view; if (!view) { syncToolbar(); return; }
    if (view.runtime_mode === 'demo') message.append(alert({ tone: 'info', title: '当前为离线演示模式',
      body: '真实模型接入请使用 devagent serve 启动。' }));
    if (view.restart_required) message.append(alert({ tone: 'warning', title: '配置已保存，等待重启生效', body: '当前服务继续使用启动时的配置。重启后，新建任务才会使用更新后的设置。' }));
    if (mode === 'models') paintModels(); else paintBackend();
    syncToolbar();
  }
  function paintBackend() {
    const choose = el('select', { class: 'input', attrs: { 'aria-label': '后端配置分类' }, on: { change: () => { session.group = choose.value; paint(); } } },
      CONFIG_GROUPS.map(([id, label]) => el('option', { text: label, attrs: { value: id } })));
    choose.value = session.group;
    const search = el('input', { class: 'input', attrs: { type: 'search', placeholder: '搜索所有后端配置…', 'aria-label': '搜索后端配置' } });
    const grid = el('div', { class: 'backend-config-grid' });
    const groupInfo = el('p', { class: 'hint-text' });
    const draw = () => {
      clear(grid); const query = search.value.trim().toLowerCase();
      const groups = query ? CONFIG_GROUPS : CONFIG_GROUPS.filter(([id]) => id === session.group);
      groupInfo.textContent = query ? '搜索全部分类' : ''; groupInfo.hidden = !query; choose.title = GROUP_HINTS[session.group] || '';
      for (const [id] of groups) for (const item of configFields(session.view.schema, id)) {
        if (!query || `${LABELS[item.key]} ${item.path} ${item.schema.description || ''}`.toLowerCase().includes(query)) grid.append(field(item));
      }
      if (!grid.children.length) grid.append(el('p', { class: 'hint-text', text: '没有匹配的配置项。' }));
    };
    search.addEventListener('input', draw);
    content.append(el('div', { class: 'backend-config-filters' }, [choose, search]), groupInfo, grid); draw();
  }
  function paintModels() {
    const presets = session.view.presets;
    const select = el('select', { class: 'input', attrs: { 'aria-label': '供应商预设' } },
      presets.map((p) => el('option', { text: p.label, attrs: { value: p.id } })));
    select.value = session.preset || 'openai';
    const providerId = el('input', { class: 'input mono', attrs: { 'aria-label': '供应商 ID', placeholder: '供应商 ID' } });
    providerId.value = select.value;
    const note = el('p', { class: 'hint-text' });
    const describe = () => {
      const preset = presets.find((p) => p.id === select.value);
      session.preset = select.value; providerId.value = preset.id; note.textContent = preset.note || '填写 API Key 后，在下方选择模型路由。';
    };
    select.addEventListener('change', describe); describe();
    content.append(el('div', { class: 'provider-preset-bar' }, [select, providerId, button('添加 / 选择供应商', {
      icon: 'plus', variant: 'secondary', onClick: () => {
        const preset = presets.find((p) => p.id === select.value);
        const id = providerId.value.trim();
        if (!/^[a-z][a-z0-9_-]{0,47}$/.test(id) || id === 'providers' || ['constructor', 'prototype'].includes(id)) {
          toast({ tone: 'warning', title: '供应商 ID 格式不正确', desc: '使用小写字母开头的字母、数字、下划线或短横线。' }); return;
        }
        const builtin = ['deepseek', 'qwen', 'zhipu'].includes(id);
        const path = builtin ? `models.${id}` : `models.providers.${id}`;
        const existing = readPath(session.draft, path);
        if (!existing) {
          const defaults = Object.fromEntries(Object.entries(session.view.schema.$defs.ProviderConfig.properties).map(([key, item]) => [key, item.default ?? null]));
          for (const [key, value] of Object.entries({ ...defaults, ...preset.config })) update(`${path}.${key}`, value);
        } else if (!existing.base_url) {
          for (const [key, value] of Object.entries(preset.config)) update(`${path}.${key}`, value);
        }
        session.provider = id; paint();
      },
    })]), note);
    const providers = { deepseek: session.draft.models.deepseek, qwen: session.draft.models.qwen, zhipu: session.draft.models.zhipu, ...session.draft.models.providers };
    session.provider = providers[session.provider] ? session.provider : Object.keys(providers)[0];
    const chooser = el('select', { class: 'input', attrs: { 'aria-label': '已添加的供应商' }, on: { change: () => { session.provider = chooser.value; paint(); } } },
      Object.entries(providers).map(([id, config]) => el('option', {
        text: `${presets.find((p) => p.id === id)?.label || id} · ${config.base_url ? '已填写地址' : '未配置'}`, attrs: { value: id },
      })));
    chooser.value = session.provider;
    content.append(el('div', { class: 'field', style: { 'margin-top': 'var(--space-5)' } }, [el('label', { class: 'label', text: '编辑供应商' }), chooser]));
    const id = session.provider;
    const base = ['deepseek', 'qwen', 'zhipu'].includes(id) ? `models.${id}` : `models.providers.${id}`;
    const preset = presets.find((p) => p.id === id);
    if (preset?.docs) content.append(el('a', { class: 'backend-doc-link', attrs: { href: preset.docs, target: '_blank', rel: 'noopener noreferrer' }, text: `${preset.label} 官方接入文档 ↗` }));
    const grid = el('div', { class: 'backend-config-grid' });
    for (const [key, item] of Object.entries(session.view.schema.$defs.ProviderConfig.properties)) grid.append(field({
      path: `${base}.${key}`, key, schema: resolveSchema(item, session.view.schema), nullable: !!item.anyOf?.some((s) => s.type === 'null'),
    }));
    if (providers[id].protocol === 'anthropic') content.append(el('p', { class: 'hint-text', text: 'Anthropic 固定使用 x-api-key / max_tokens。' }));
    content.append(grid);
    const route = el('select', { class: 'input', attrs: { 'aria-label': '模型用途' } }, [
      ['routing.small_model', '小模型'], ['routing.medium_model', '中模型'], ['routing.large_model', '大模型'],
      ['routing.embedding_model', '嵌入模型'], ['evaluation.judge_model', '主裁判'], ['evaluation.reference_judge_model', '参考裁判'],
    ].map(([value, label]) => el('option', { text: label, attrs: { value } })));
    const model = el('input', { class: 'input mono', attrs: { 'aria-label': '模型 ID', placeholder: preset?.model_example || '账号中可用的模型 / 部署名称' } });
    const binding = el('div', { class: 'hint-text', attrs: { 'aria-live': 'polite' } });
    const syncBinding = () => { binding.textContent = `当前设置：${readPath(session.draft, route.value) || '未设置'}`; };
    route.addEventListener('change', syncBinding); syncBinding();
    content.append(el('h3', { class: 'backend-subheading', text: '设置模型路由' }), el('div', { class: 'provider-preset-bar' }, [route, model, button('设置到路由', {
      variant: 'secondary', onClick: () => {
        if (!model.value.trim()) { model.focus(); return; }
        if (route.value === 'routing.embedding_model' && providers[id].protocol === 'anthropic') {
          toast({ tone: 'warning', title: 'Anthropic 不提供嵌入接口', desc: '请选择支持 embeddings 的供应商。' }); return;
        }
        update(route.value, `${id}:${model.value.trim()}`); syncBinding();
      },
    })]), binding, el('details', { class: 'config-help' }, [
      el('summary', { text: '接入说明' }),
      el('p', { text: '留空基址停用供应商；已有密钥不回显，留空保留。未配置单价时成本估算为 0。模型名称以账号权限为准，此处不发起付费调用。' }),
    ]));
  }
  if (session.view) paint(); else { content.append(el('p', { class: 'hint-text', text: '正在读取后端配置…' })); if (!session.loading) load(); }
  return root;
}
