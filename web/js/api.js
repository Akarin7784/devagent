/**
 * API 客户端。
 *
 * 设计要点：
 *
 * 1. **统一错误形态**：服务端所有失败路径都返回 `{error, detail, code}`，
 *    这里把它规范化为 `ApiError`，让上层只需处理一种错误类型。
 * 2. **超时**：`fetch` 没有内置超时。SSE 之外的请求都套 15s 超时，
 *    否则后端挂起时前端会永久转圈 —— 比报错更糟。
 * 3. **可观测**：每个请求发出/结束都通过 `requestEvent` 通知外层，
 *    用于驱动连接状态指示灯与路由进度条。
 */

import { EventDedup, ReplayWindow } from './eventstream.js?v=20261008-live';
import { createEmitter } from './util.js?v=20261008-live';

export const API_PREFIX = '/api/v1';

/** 请求生命周期事件总线（pending +1/-1、error）。 */
export const requestEvent = createEmitter();

/** 运行时 API 基址。默认同源，可通过 ?api= 覆盖。 */
let apiBase = '';
let apiKey = '';
export function setApiKey(key) { apiKey = String(key || '').trim(); }
export function getApiKey() { return apiKey; }

export function setApiBase(base) {
  const next = String(base || '').trim().replace(/\/+$/, '');
  if (next !== apiBase) apiKey = '';
  apiBase = next;
}

export function getApiBase() {
  return apiBase;
}

/** 拼接完整 URL。 */
export function apiUrl(path) {
  return `${apiBase}${API_PREFIX}${path}`;
}

/** 规范化后的 API 错误。 */
export class ApiError extends Error {
  /**
   * @param {string} message 面向用户的简短说明
   * @param {object} [info]
   * @param {number} [info.status] HTTP 状态码（网络失败为 0）
   * @param {string} [info.code] 服务端错误码
   * @param {string} [info.detail] 服务端详细信息
   * @param {string} [info.path] 请求路径（便于定位）
   */
  constructor(message, info = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = info.status ?? 0;
    this.code = info.code ?? '';
    this.detail = info.detail ?? '';
    this.path = info.path ?? '';
  }

  /** 是否属于「重试可能有用」的暂时性失败。 */
  get isTransient() {
    return this.status === 0 || this.status === 429 || this.status >= 500;
  }
}

const DEFAULT_TIMEOUT = 15_000;

/** 请求计数，用于并发追踪。 */
let inflight = 0;

function trackStart() {
  inflight += 1;
  requestEvent.emit('pending', inflight);
}

function trackEnd() {
  inflight = Math.max(0, inflight - 1);
  requestEvent.emit('pending', inflight);
}

/**
 * 底层请求。
 *
 * @param {string} path 相对 `/api/v1` 的路径
 * @param {object} [opts]
 * @param {string} [opts.method='GET']
 * @param {any} [opts.body] 会被 JSON 序列化
 * @param {number} [opts.timeout]
 * @param {AbortSignal} [opts.signal] 外部取消信号（路由切换时用）
 * @param {boolean} [opts.silent] 不计入全局 pending（用于轮询，避免进度条闪烁）
 */
export async function request(path, opts = {}) {
  const {
    method = 'GET',
    body,
    timeout = DEFAULT_TIMEOUT,
    signal,
    silent = false,
  } = opts;

  const controller = new AbortController();
  const onAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) throw new ApiError('请求已取消', { path });
    signal.addEventListener('abort', onAbort, { once: true });
  }

  let timedOut = false;
  const timer = timeout > 0
    ? window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeout)
    : 0;

  if (!silent) trackStart();

  try {
    const res = await fetch(apiUrl(path), {
      method,
      redirect: 'error',
      headers: { ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...(apiKey ? { 'X-API-Key': apiKey } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });

    if (!res.ok) {
      // 尽量解析服务端的统一错误结构；解析失败则给出状态文本
      let payload = null;
      try {
        payload = await res.json();
      } catch {
        payload = null;
      }
      const detail = payload?.detail || payload?.error || '';
      throw new ApiError(detail || `请求失败（HTTP ${res.status}）`, {
        status: res.status,
        code: payload?.code || '',
        detail,
        path,
      });
    }

    // 204 或无内容
    if (res.status === 204) return null;
    const text = await res.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      throw new ApiError('服务端返回了非 JSON 内容', { status: res.status, path });
    }
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if (err.name === 'AbortError') {
      throw new ApiError(timedOut ? `请求超时（${timeout / 1000}s）` : '请求已取消', {
        path,
        status: 0,
      });
    }
    // TypeError: Failed to fetch —— 网络不通、CORS、服务未启动
    throw new ApiError('无法连接到服务端，请检查后端是否已启动', { path, status: 0 });
  } finally {
    if (timer) window.clearTimeout(timer);
    signal?.removeEventListener?.('abort', onAbort);
    if (!silent) trackEnd();
  }
}

/* ------------------------------------------------------------------ *
 * 业务接口
 * ------------------------------------------------------------------ */

export const api = {
  getSettings: (opts) => request('/settings', opts),
  saveSettings: (payload, opts) => request('/settings', { method: 'PUT', body: payload, ...opts }),
  /** 健康检查 + 能力探测。 */
  health: (opts) => request('/health', { timeout: 6_000, ...opts }),

  /**
   * 提交任务。
   * @param {string} goal 自然语言需求
   * @param {object} [meta] 附加 metadata
   */
  createTask: (goal, meta = {}, opts) =>
    request('/tasks', { method: 'POST', body: { goal, metadata: meta }, ...opts }),

  listTasks: ({ limit = 100, status = '' } = {}, opts) => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (status) q.set('status', status);
    return request(`/tasks?${q}`, { silent: true, ...opts });
  },

  getTask: (taskId, opts) =>
    request(`/tasks/${encodeURIComponent(taskId)}`, { silent: true, ...opts }),

  deleteTask: (taskId, opts) =>
    request(`/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE', ...opts }),

  cancelTask: (taskId, opts) =>
    request(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST', ...opts }),

  /** 任务的上下文工程指标。 */
  getContext: (taskId, opts) =>
    request(`/tasks/${encodeURIComponent(taskId)}/context`, { silent: true, ...opts }),

  /** 触发一次评测。耗时较长，单独放宽超时。 */
  runEvaluation: (payload, opts) =>
    request('/evaluate', { method: 'POST', body: payload, timeout: 300_000, ...opts }),

  metrics: (opts) => request('/metrics', { silent: true, ...opts }),

  cacheStats: (opts) => request('/cache', { silent: true, ...opts }),

  traces: ({ limit = 100 } = {}, opts) =>
    request(`/traces?limit=${limit}`, { silent: true, ...opts }),

  /** Prometheus 文本格式，供「导出」按钮使用。 */
  prometheusText: async () => {
    const res = await fetch(apiUrl('/metrics/prometheus'), { redirect: 'error', headers: apiKey ? { 'X-API-Key': apiKey } : {} });
    if (!res.ok) throw new ApiError(`导出失败（HTTP ${res.status}）`, { status: res.status });
    return res.text();
  },
};

/* ------------------------------------------------------------------ *
 * SSE
 * ------------------------------------------------------------------ */

/** 服务端会推送的事件类型。必须与后端 routes.py 保持一致。 */
export const SSE_EVENTS = [
  'task_started',
  'task_finished',
  'task_cancelled',
  'node_started',
  'node_finished',
  'node_verdict',
  'ping',
];

/**
 * 订阅任务事件流。
 *
 * 用原生 EventSource（而非 fetch + ReadableStream）的理由：浏览器内置了
 * 断线重连与 `Last-Event-ID` 续传语义，自己实现一遍只会更差。
 *
 * ## 服务端行为（决定了这里必须做什么）
 *
 * `EventBus.subscribe()` 每次订阅都会**回放该任务的完整历史**（环形缓冲，
 * 最多 500 条），然后才推实时事件。这带来两个必须在前端处理的后果：
 *
 * 1. **回放会淹没新事件**：切到一个跑了 200 步的旧任务，会有几百条历史
 *    事件瞬间到达。调用方需要知道自己是在被"补课"而不是在看直播，
 *    因此这里通过 `onEvent` 的第二个参数 `meta.replayed` 标明。
 * 2. **重连会重复投递**：EventSource 自动重连时会再订阅一次，服务端再回放
 *    一遍。而 `to_sse_data()` **不含事件 id**，`Last-Event-ID` 机制无法生效。
 *    所以这里内置了去重：以 (kind, node_id, attempt, timestamp) 作为
 *    事件指纹，重复到达的直接丢弃并计入 `meta.duplicate`。
 *
 * `onClose` 与 `onError` 分开：任务正常结束导致的关闭不是错误，
 * 不该弹错误提示 —— 这是事件流 UI 最常见的误报来源。
 *
 * @param {string} taskId
 * @param {object} handlers
 * @param {(ev: object, meta: {replayed: boolean, duplicate: boolean}) => void} handlers.onEvent
 * @param {(err: Error, meta: {willRetry: boolean}) => void} [handlers.onError]
 * @param {() => void} [handlers.onClose]
 * @param {number} [handlers.replayGraceMs] 判断"回放期"的时长窗口
 * @returns {() => void} 取消订阅
 */
export function subscribeTaskEvents(
  taskId,
  { onEvent, onError, onClose, replayGraceMs = 1500 } = {},
) {
  if (apiKey) return subscribeAuthenticatedEvents(taskId, { onEvent, onError, onClose, replayGraceMs });
  const es = new EventSource(apiUrl(`/tasks/${encodeURIComponent(taskId)}/events`));
  let closed = false;
  let disconnectCount = 0;

  // 去重与回放判定抽到 eventstream.js（纯函数，可单测）。
  // 这里只负责把它们接到 EventSource 的生命周期上。
  const dedup = new EventDedup();
  const replay = new ReplayWindow(replayGraceMs);
  replay.begin();

  const makeHandler = (kind) => (e) => {
    if (kind === 'ping') return;
    let payload = {};
    try {
      payload = JSON.parse(e.data);
    } catch {
      // 服务端应始终发 JSON；解析失败时保留原始文本便于排查
      payload = { raw: e.data };
    }

    if (dedup.isDuplicate(kind, payload)) {
      onEvent?.({ kind, ...payload }, { replayed: false, duplicate: true });
      return;
    }

    onEvent?.({ kind, ...payload }, {
      replayed: replay.isReplaying(),
      duplicate: false,
    });
  };

  for (const kind of SSE_EVENTS) {
    es.addEventListener(kind, makeHandler(kind));
  }

  es.onerror = () => {
    if (closed) return;
    if (es.readyState === EventSource.CLOSED) {
      // 服务端在任务结束后主动断开，这是正常路径
      closed = true;
      onClose?.();
    } else {
      // CONNECTING：EventSource 正在自动重连。
      //
      // 关键：重连后服务端会**再回放一遍历史**，所以这里必须：
      //  1. 重启回放窗口，让这批事件被标为 replayed（否则会被当成新事件
      //     触发自动滚动，把用户的视野冲掉）；
      //  2. 去重集合保留不清 —— 正是靠它把重复的历史丢掉。
      replay.restart();
      disconnectCount += 1;
      onError?.(new Error('事件流连接中断，正在重连…'), {
        willRetry: true,
        disconnectCount,
      });
    }
  };

  return () => {
    closed = true;
    es.close();
  };
}

/** EventSource cannot send X-API-Key. Use a header-authenticated stream without URL credentials. */
export function parseSSEBlock(block) {
  let kind = 'message'; const data = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) kind = line.slice(6).trim();
    if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
  }
  return { kind, data: data.join('\n') };
}

function subscribeAuthenticatedEvents(taskId, { onEvent, onError, onClose, replayGraceMs }) {
  const url = apiUrl(`/tasks/${encodeURIComponent(taskId)}/events`);
  const key = apiKey;
  const dedup = new EventDedup(); const replay = new ReplayWindow(replayGraceMs);
  let closed = false; let retryTimer = null; let controller; let disconnectCount = 0;
  const connect = async () => {
    controller = new AbortController(); replay.begin();
    try {
      const response = await fetch(url, { headers: { 'X-API-Key': key }, signal: controller.signal, redirect: 'error' });
      if (!response.ok) {
        if ([401, 403, 404].includes(response.status)) {
          closed = true; onError?.(new Error(`事件流访问失败（HTTP ${response.status}）`), { willRetry: false }); return;
        }
        throw new Error(`事件流请求失败（HTTP ${response.status}）`);
      }
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let pending = '';
      while (!closed) {
        const { value, done } = await reader.read(); if (done) break;
        pending += decoder.decode(value, { stream: true });
        let match;
        while ((match = /\r?\n\r?\n/.exec(pending))) {
          const block = pending.slice(0, match.index).replaceAll('\r\n', '\n');
          pending = pending.slice(match.index + match[0].length);
          const event = parseSSEBlock(block);
          if (!SSE_EVENTS.includes(event.kind) || event.kind === 'ping' || !event.data) continue;
          let payload; try { payload = JSON.parse(event.data); } catch { payload = { raw: event.data }; }
          const duplicate = dedup.isDuplicate(event.kind, payload);
          onEvent?.({ kind: event.kind, ...payload }, { duplicate, replayed: !duplicate && replay.isReplaying() });
          if (['task_finished', 'task_cancelled'].includes(event.kind)) {
            closed = true; controller.abort(); onClose?.(); return;
          }
        }
      }
      if (!closed) throw new Error('事件流连接中断');
    } catch (error) {
      if (closed) return;
      disconnectCount += 1; replay.restart();
      onError?.(error, { willRetry: true, disconnectCount });
      retryTimer = window.setTimeout(connect, 3000);
    }
  };
  connect();
  return () => { closed = true; controller?.abort(); if (retryTimer) window.clearTimeout(retryTimer); };
}
