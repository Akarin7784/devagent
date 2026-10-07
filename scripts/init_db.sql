-- DevAgent 数据库初始化
--
-- 由 docker compose 在 postgres 容器首次启动时自动执行
-- （挂载到 /docker-entrypoint-initdb.d/）。
--
-- 幂等：所有语句都带 IF NOT EXISTS / 条件判断，重复执行安全。

-- ---------------------------------------------------------------------- #
-- 扩展
-- ---------------------------------------------------------------------- #

-- pgvector：代码片段与上下文的向量检索
CREATE EXTENSION IF NOT EXISTS vector;

-- 用于 gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 模糊匹配与相似度（上下文去重的辅助手段）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------- #
-- 任务表
--
-- 列名/类型/非空/默认值必须与 ORM 的 TaskRow（src/devagent/db/models.py）逐列一致：
-- app.py 用的是 create_all(checkfirst=True)，表已存在时**不会**被纠正，
-- 因此这里的 DDL 一旦与 ORM 不一致，SQL 模式下的第一次写入就会
-- 报 "no such column: tasks.succeeded"。
-- 时间戳统一为 double precision（Unix 秒），与 ORM 的 Float 列对应。
-- ---------------------------------------------------------------------- #

CREATE TABLE IF NOT EXISTS tasks (
    id              VARCHAR(64)      PRIMARY KEY,
    goal            TEXT             NOT NULL,
    status          VARCHAR(32)      NOT NULL DEFAULT 'pending',
    succeeded       BOOLEAN          NOT NULL DEFAULT FALSE,
    error           TEXT             NOT NULL DEFAULT '',
    duration_ms     INTEGER          NOT NULL DEFAULT 0,
    total_tokens    INTEGER          NOT NULL DEFAULT 0,
    total_cost_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- steps / nodes / context_metrics / extra 整体读写，从不按字段查询
    steps           JSON             NOT NULL DEFAULT '[]'::json,
    nodes           JSON             NOT NULL DEFAULT '[]'::json,
    context_metrics JSON             NOT NULL DEFAULT '{}'::json,
    extra           JSON             NOT NULL DEFAULT '{}'::json,
    created_at      DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at      DOUBLE PRECISION NOT NULL DEFAULT 0
);

-- 索引名与 ORM 的 __table_args__ 保持一致，避免两条建表路径产出不同的 schema
-- 按状态查询（列表页最常见）
CREATE INDEX IF NOT EXISTS ix_tasks_status_created
    ON tasks (status, created_at);

-- 列表按创建时间倒序
CREATE INDEX IF NOT EXISTS ix_tasks_created
    ON tasks (created_at);

-- ---------------------------------------------------------------------- #
-- 任务事件表（SSE 事件流的历史持久化，ORM：TaskEventRow）
-- ---------------------------------------------------------------------- #

CREATE TABLE IF NOT EXISTS task_events (
    id          SERIAL           PRIMARY KEY,
    task_id     VARCHAR(64)      NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq         INTEGER          NOT NULL DEFAULT 0,
    kind        VARCHAR(48)      NOT NULL,
    payload     JSON             NOT NULL DEFAULT '{}'::json,
    timestamp   DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- 序列号在任务内单调递增：既用于去重，也支持断点续拉
    CONSTRAINT uq_task_events_task_seq UNIQUE (task_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_task_events_task_seq
    ON task_events (task_id, seq);

-- ---------------------------------------------------------------------- #
-- DAG 节点表
-- ---------------------------------------------------------------------- #

CREATE TABLE IF NOT EXISTS task_nodes (
    -- 外键列的类型必须与 tasks.id（VARCHAR(64)）一致，避免跨类型外键
    task_id         VARCHAR(64) NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    node_id         TEXT NOT NULL,
    goal            TEXT NOT NULL DEFAULT '',
    agent_type      TEXT NOT NULL,
    deps            TEXT[] NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending',
    attempt         INT  NOT NULL DEFAULT 1,
    tokens_used     BIGINT NOT NULL DEFAULT 0,
    last_error      TEXT,
    PRIMARY KEY (task_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_task_nodes_status
    ON task_nodes (task_id, status);

-- ---------------------------------------------------------------------- #
-- 执行步骤表（时间线）
-- ---------------------------------------------------------------------- #

CREATE TABLE IF NOT EXISTS task_steps (
    id              BIGSERIAL PRIMARY KEY,
    task_id         VARCHAR(64) NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id         TEXT NOT NULL,
    agent_type      TEXT NOT NULL,
    attempt         INT  NOT NULL DEFAULT 1,
    model_tier      TEXT,
    tokens_used     BIGINT NOT NULL DEFAULT 0,
    cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0,
    duration_ms     BIGINT NOT NULL DEFAULT 0,
    output          JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_steps_task
    ON task_steps (task_id, created_at);

-- ---------------------------------------------------------------------- #
-- 代码索引（AST 解析结果 + 向量）
-- ---------------------------------------------------------------------- #

CREATE TABLE IF NOT EXISTS code_symbols (
    id              BIGSERIAL PRIMARY KEY,
    repo_path       TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    symbol_name     TEXT NOT NULL,
    symbol_kind     TEXT NOT NULL,          -- class / function / method
    start_line      INT  NOT NULL,
    end_line        INT  NOT NULL,
    content         TEXT NOT NULL,
    -- 维度与所用 embedding 模型绑定；换模型需要重建索引
    embedding       vector(1024),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo_path, file_path, symbol_name, start_line)
);

CREATE INDEX IF NOT EXISTS idx_code_symbols_lookup
    ON code_symbols (repo_path, file_path);

-- 向量近邻检索（HNSW：查询快，构建比 IVFFlat 慢但无需训练）
-- 余弦距离，与 embedding 模型的归一化假设一致
CREATE INDEX IF NOT EXISTS idx_code_symbols_embedding
    ON code_symbols USING hnsw (embedding vector_cosine_ops);

-- 全文检索兜底（模型不可用时的降级路径）
CREATE INDEX IF NOT EXISTS idx_code_symbols_trgm
    ON code_symbols USING gin (content gin_trgm_ops);

-- ---------------------------------------------------------------------- #
-- 上下文装配审计（可观测性：为什么这个片段被选中 / 丢弃）
-- ---------------------------------------------------------------------- #

CREATE TABLE IF NOT EXISTS context_decisions (
    id              BIGSERIAL PRIMARY KEY,
    -- 与 tasks.id 保持同一类型（VARCHAR(64)）
    task_id         VARCHAR(64) NOT NULL,
    step_id         TEXT NOT NULL,
    agent_type      TEXT NOT NULL,
    chunk_ref       TEXT NOT NULL,
    selected        BOOLEAN NOT NULL,
    drop_reason     TEXT,                    -- over_budget / duplicate / low_score
    score           DOUBLE PRECISION,
    tokens          INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_context_decisions_step
    ON context_decisions (task_id, step_id);

-- ---------------------------------------------------------------------- #
-- 更新 updated_at 的触发器
-- ---------------------------------------------------------------------- #

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    -- updated_at 是 double precision（Unix 秒），与 ORM 的 Float 列一致。
    -- 直接写 now() 会因 timestamptz → float8 没有隐式转换而在**每次 UPDATE** 时报错
    -- （SqlTaskStore.save() 更新已有任务时就会踩到），必须显式取 epoch。
    NEW.updated_at = EXTRACT(EPOCH FROM now())::double precision;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tasks_updated_at ON tasks;
CREATE TRIGGER trg_tasks_updated_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
