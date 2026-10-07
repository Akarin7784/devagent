/**
 * 事件流（SSE）生命周期管理 —— **纯逻辑，零 DOM**。
 *
 * ## 为什么单独成模块
 *
 * 工作台原先只有一句：
 *
 * ```js
 * unsubscribeStream = subscribeTaskEvents(taskId, { ... });
 * ```
 *
 * 看起来没问题，实际同时埋了两个坑（都是"快速点两下"才会暴露）：
 *
 * 1. **泄漏**：连点任务 A、B 时，B 的赋值**覆盖**了 A 的取消函数，
 *    A 的 `EventSource` 再没有引用可关 —— 连接与它的回调永久存活。
 *    唯一的 `closeStream()` 在 `selectTask` 开头，早于 `await`，管不到之后。
 * 2. **串台**：A 的回调闭包与 B 共用同一份页面状态，于是 A 的 `node_*`
 *    事件会被 `applyEvent()` 画进 B 的 DAG，并追加到 B 的时间线上。
 *    用户看到的是一张"两个任务混在一起"的图，且没有任何报错。
 *
 * 抽成模块有两个好处：一是把"同一时刻只允许一条流"和"谁才有权处理事件"
 * 变成显式状态而不是约定；二是它是纯逻辑，可以在 node 里直接单测
 * （页面模块依赖 DOM，跑不了）。
 *
 * ## 用法
 *
 * ```js
 * const token = slot.create(taskId);          // 自动关闭上一条
 * token.onClose = subscribe(taskId, {
 *   onEvent: (ev) => { if (!slot.isActive(token)) return; ... },
 *   onError: (err) => { if (!slot.isActive(token) || token.warned) return; ... },
 * });
 * ```
 */
export class StreamSlot {
  constructor() {
    /** @type {{taskId: string, warned: boolean, onClose?: Function}|null} */
    this.current = null;
  }

  /**
   * 登记一条新流。**会先关闭上一条**（这是防泄漏的关键），
   * 返回本次的 token —— 回调必须持有它并先用 `isActive()` 自检。
   *
   * @param {string} taskId
   * @returns {{taskId: string, warned: boolean}}
   */
  create(taskId) {
    this.close();
    this.current = { taskId, warned: false };
    return this.current;
  }

  /**
   * token 是否仍是当前流。
   * 过期流（用户已切走、或任务已结束）的回调据此自我作废。
   */
  isActive(token) {
    return Boolean(token) && this.current === token;
  }

  /** 当前流的任务 id（没有流时为 null）。 */
  activeTaskId() {
    return this.current ? this.current.taskId : null;
  }

  /**
   * 关闭当前流（幂等）。
   *
   * 关闭动作委托给 `token.onClose`（由页面在建立订阅后挂上）：
   * 本模块不认识 `EventSource`，因此可以在 node 里被完整测试。
   * `current` **先**置空再回调，保证回调里再次 close 不会重入。
   */
  close() {
    const prev = this.current;
    this.current = null;
    if (prev) prev.onClose?.();
    return Boolean(prev);
  }
}
