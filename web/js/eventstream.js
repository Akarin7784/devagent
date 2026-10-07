/**
 * 事件流的**纯逻辑层**：去重与回放判定。
 *
 * ## 为什么单独成文件
 *
 * `api.js` 的 `subscribeTaskEvents()` 依赖 `EventSource`（浏览器独有），
 * 在 Node 里无法直接测试。而它内部那两条规则 —— 「识别历史事件回放」
 * 与「丢弃重连重复投递」 —— 恰恰是**最容易出错且后果最隐蔽**的部分：
 * 逻辑错了不会报错，只会让时间线悄悄出现整段重复。
 *
 * 所以把这两个判定抽成不依赖 DOM 的纯函数，放在这里单独测。
 * 与 graph.js 的取舍一致：只对纯逻辑做测试，DOM 部分靠语法检查 + 人工验证。
 */

/**
 * 计算事件指纹。
 *
 * 服务端 `TaskEvent.to_sse_data()` **不含事件 id**，因此 `Last-Event-ID`
 * 续传机制无法生效 —— 重连时服务端会把历史整段再发一遍。
 * 只能在前端自行判重。
 *
 * 指纹取 (kind, node_id, attempt, timestamp)：
 *  - 同一节点同一 attempt 的同一种事件，时间戳必然相同（由服务端生成一次）；
 *  - 反过来，回退重跑会产生 attempt=2 的新事件，不会被误判为重复。
 * 这正是需要保留 attempt 的原因 —— 只看 (kind, node_id) 会把
 * 「驳回后重跑」这种**真正不同的新事件**误杀掉。
 *
 * @param {string} kind 事件类型
 * @param {object} payload 事件负载
 * @returns {string} 指纹
 */
export function eventFingerprint(kind, payload = {}) {
  return [
    kind,
    payload.node_id ?? '',
    payload.attempt ?? '',
    payload.timestamp ?? '',
  ].join('\u0000');
}

/**
 * 有界去重集合。
 *
 * 必须有界：服务端历史缓冲上限 500 条，而任务执行期间事件会持续增长。
 * 若无限累积，一个跑了很久的任务会让这个 Set 吃满内存。
 * 用 FIFO 淘汰 —— 淘汰掉的指纹理论上还可能重复，但那需要事件在
 * 超过 `limit` 条之后才重发，实践中不会发生（重连只会重发整个缓冲）。
 */
export class EventDedup {
  /**
   * @param {number} [limit=1000] 保留的指纹上限
   */
  constructor(limit = 1000) {
    this.limit = limit;
    /** @type {Set<string>} */
    this.seen = new Set();
    /** @type {string[]} 用于 FIFO 淘汰 */
    this.order = [];
    this.duplicates = 0;
  }

  /**
   * 判断事件是否为重复投递。
   * 首次见到时**自动登记**（seen-and-add 语义），调用方无需两步操作。
   *
   * @param {string} kind
   * @param {object} payload
   * @returns {boolean} true 表示重复，应丢弃
   */
  isDuplicate(kind, payload) {
    const key = eventFingerprint(kind, payload);
    if (this.seen.has(key)) {
      this.duplicates += 1;
      return true;
    }
    this.seen.add(key);
    this.order.push(key);
    if (this.order.length > this.limit) {
      const oldest = this.order.shift();
      if (oldest !== undefined) this.seen.delete(oldest);
    }
    return false;
  }

  /** 清空（切换任务时调用）。 */
  reset() {
    this.seen.clear();
    this.order.length = 0;
    this.duplicates = 0;
  }
}

/**
 * 回放窗口判定器。
 *
 * ## 为什么需要
 *
 * 服务端每次订阅都会**先回放该任务的完整历史**（最多 500 条），
 * 然后才推实时事件。前端必须能区分两者，否则：
 *   - 几百条历史事件会触发几百次自动滚动，画面抖动；
 *   - 用户无法区分"正在发生"与"补课"，会误以为任务重跑了。
 *
 * ## 为什么用时间窗口而不是别的判据
 *
 * 也曾考虑「收到 task_finished 之前都算回放」，但那会把**运行中任务**
 * 的实时事件误判成回放 —— 更糟。
 * 时间窗口不完美（严格来说服务端应下发事件序号），但足够拿到正确性：
 * 实测本地 8 条事件在 <10ms 内推完，而实时事件之间的间隔至少是模型
 * 调用的耗时（数百毫秒起）。1.5s 的窗口对两端都有足够余量。
 *
 * 因此这里如实暴露 `elapsed`/`confidence`，调用方可据此决定是否展示标记。
 */
export class ReplayWindow {
  /**
   * @param {number} [graceMs=1500] 回放窗口时长（毫秒）
   */
  constructor(graceMs = 1500) {
    this.graceMs = graceMs;
    this.start = null;
  }

  /**
   * 标记订阅开始。计时用 `performance.now()` 而非 `Date.now()`：
   * 后者受系统时钟调整（NTP 校时、用户改时间）影响，可能跳变。
   */
  begin(now = nowMs()) {
    this.start = now;
  }

  /**
   * 判断此刻到达的事件是否属于历史回放。
   * @param {number} [now] 当前时间，默认取 performance.now()
   * @returns {boolean}
   */
  isReplaying(now = nowMs()) {
    if (this.start == null) return false;
    return now - this.start < this.graceMs;
  }

  /** 重新开始计时（重连时调用 —— 重连也会触发一次回放）。 */
  restart(now = nowMs()) {
    this.begin(now);
  }
}

/** 单调时钟。Node 里没有 performance 时回退到 Date.now()。 */
function nowMs() {
  if (typeof performance !== 'undefined' && typeof performance.now === 'function') {
    return performance.now();
  }
  return Date.now();
}

export { nowMs };
