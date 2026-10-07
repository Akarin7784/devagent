"""配置系统。

基于 ``pydantic-settings``，支持：
- 环境变量 / ``.env`` 文件
- 嵌套配置（用 ``__`` 分隔层级）
- 明确的前缀 ``DEVAGENT_``，避免与系统环境变量冲突

用法::

    from devagent.config import get_settings

    settings = get_settings()
    print(settings.models.deepseek.api_key)
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderConfig(BaseModel):
    """单个模型提供商的接入配置。"""

    api_key: SecretStr | None = None
    base_url: str = ""
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=3, ge=0)

    @property
    def enabled(self) -> bool:
        """是否「已配置且可用」。

        ``api_key`` 为空串**不算**已配置。docker-compose 里常见的
        ``${DEEPSEEK_API_KEY:-}`` 在宿主机没设变量时会注入空串，
        而 ``SecretStr("")`` 是"有值"的 —— 早先的实现据此把 provider 判为
        enabled，于是运行时报的是上游 401，而不是一句清楚的"未配置"。
        """
        if self.api_key is None or not self.api_key.get_secret_value().strip():
            return False
        return bool(self.base_url.strip())


class ModelsConfig(BaseModel):
    """全部模型提供商的配置。"""

    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    qwen: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)

    def enabled_providers(self) -> dict[str, ProviderConfig]:
        candidates = {
            "deepseek": self.deepseek,
            "qwen": self.qwen,
            "zhipu": self.zhipu,
        }
        return {name: cfg for name, cfg in candidates.items() if cfg.enabled}


class RoutingConfig(BaseModel):
    """模型分级路由配置。

    每个档位映射到一个具体模型名（格式：``<provider>:<model>``）。
    """

    small_model: str = "qwen:qwen-turbo"
    medium_model: str = "deepseek:deepseek-chat"
    large_model: str = "deepseek:deepseek-reasoner"
    embedding_model: str = "qwen:text-embedding-v3"

    # 复杂度评分权重（可调以便做对照实验）
    weight_reasoning_depth: float = 0.3
    weight_context_size: float = 0.2
    weight_tool_calls: float = 0.2
    weight_retry_history: float = 0.3

    # 分档阈值
    threshold_small_upper: float = 0.35
    threshold_medium_upper: float = 0.70

    @field_validator("small_model", "medium_model", "large_model", "embedding_model")
    @classmethod
    def _validate_model_spec(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError(f"模型标识应为 '<provider>:<model>' 格式，收到：{v!r}")
        return v

    def parse_model_spec(self, spec: str) -> tuple[str, str]:
        provider, _, model = spec.partition(":")
        return provider, model


class CacheConfig(BaseModel):
    """模型响应缓存配置。

    默认 **关闭语义检索**（``semantic=False``）：开启后每次未命中的请求
    都要额外调用一次嵌入模型，一方面增加延迟与成本，另一方面要求
    嵌入模型可用（默认是 qwen text-embedding-v3，需要 QWEN_API_KEY）。

    关闭时行为与升级前完全一致（精确匹配），因此这个默认值是**安全的**：
    打开它是一个显式决策，而不是一个隐式惊喜。
    """

    enabled: bool = True
    """总开关。关闭则完全不做缓存（连精确匹配也不做）。"""

    semantic: bool = False
    """是否启用向量检索。需要 embedded 模型可用，且会产生额外调用。"""

    similarity_threshold: float = Field(
        default=0.92,
        gt=0.0,
        le=1.0,
        description="向量命中的余弦相似度阈值。太低会混淆不同需求，太高则退化回精确匹配。",
    )

    max_entries: int = Field(default=512, gt=0, description="缓存条目上限（LRU）")

    model_filter: bool = Field(
        default=True,
        description="按 (model, temperature) 分桶检索；关闭可跨模型命中，但可能返回别的模型的结果。",
    )


class ContextConfig(BaseModel):
    """上下文工程配置。"""

    default_budget: int = Field(default=16_000, gt=0)
    compression_threshold: float = Field(
        default=0.7,
        gt=0.0,
        le=1.0,
        description="上下文占用超过该比例时触发压缩",
    )
    hard_constraint_untouchable: bool = Field(
        default=True,
        description="硬约束片段是否永不压缩（建议保持 True）",
    )

    # 装配打分权重
    weight_relevance: float = 1.0
    weight_recency: float = 0.5
    weight_dependency: float = 2.0
    weight_density: float = 0.3
    weight_redundancy: float = 0.8

    # 时效衰减系数 λ（recency = e^(-λ·age)）
    recency_lambda: float = 0.15

    # 冗余判定阈值：余弦相似度高于该值视为冗余
    redundancy_cosine_threshold: float = 0.90

    # 冗余惩罚的非线性指数（越大则高度重复的片段被压制得越狠）
    redundancy_gamma: float = Field(default=4.0, gt=0.0)

    # ---- 内容信任度（提示词注入防护） ----
    weight_trust: float = Field(
        default=1.0,
        ge=0.0,
        description="信任度在装配打分中的权重；0 = 关闭信任度影响（退回升级前行为）",
    )
    injection_guard: bool = Field(
        default=True,
        description="是否给不可信来源的上下文加内容边界标记（见 context/trust.py）",
    )


class ReliabilityConfig(BaseModel):
    """可靠性配置。"""

    max_retries: int = Field(default=3, ge=0, le=10)
    max_task_tokens: int = Field(default=500_000, gt=0, description="单任务 token 熔断阈值")
    max_task_steps: int = Field(default=50, gt=0, description="单任务最大步骤数（防无限循环）")
    max_backtrack_depth: int = Field(default=5, gt=0, description="回退链最大深度")
    same_failure_threshold: int = Field(
        default=3, gt=0, description="相同失败连续出现该次数后升级策略"
    )
    checkpoint_enabled: bool = True


class SandboxConfig(BaseModel):
    """沙箱执行配置。"""

    enabled: bool = True
    image: str = "devagent/sandbox:latest"
    timeout_seconds: int = Field(default=60, gt=0)
    memory_limit: str = "512m"
    cpu_limit: float = Field(default=1.0, gt=0)
    pids_limit: int = Field(default=128, gt=0)
    network_disabled: bool = True
    read_only_root: bool = True
    workspace_mount: str = ""
    """测试工作区目录。留空则每次启动使用一个临时目录。

    为什么默认不是仓库目录：模型生成的"测试代码"会先落盘再执行，
    把仓库当工作区等于让不受信内容直接覆写源码（含 .git/hooks）。
    """

    allow_local_fallback: bool = Field(
        default=True,
        description=(
            "Docker 不可用时是否降级为本地子进程沙箱。"
            "默认 True 是为了让没有 Docker 的开发机仍能跑通；"
            "但它**确实降低了隔离级别**（本地沙箱没有容器边界，"
            "也无法真正限制内存/网络），生产环境应设为 False 让装配直接失败。"
        ),
    )
    user: str = "10001:10001"
    """容器内运行用户（``--user``）。显式传入而不是依赖镜像里的 USER 指令，
    这样换镜像也不会悄悄失去非 root 保证。"""


class SecurityConfig(BaseModel):
    """访问控制。

    默认不启用（``api_key`` 为空）：本地开发与演示需要零配置可用。
    一旦设置，``/api/v1/**`` 全部要求 ``X-API-Key`` —— 本项目默认绑定
    0.0.0.0 且没有任何鉴权，任何能访问端口的人都能消耗模型额度。
    """

    api_key: str = Field(
        default="",
        description="非空时对 /api/v1 强制校验 X-API-Key 请求头",
    )


class ObservabilityConfig(BaseModel):
    """可观测性配置。"""

    tracing_enabled: bool = True
    otlp_endpoint: str = ""
    log_json: bool = False
    metrics_enabled: bool = True
    # 是否记录完整 prompt/response（生产环境注意隐私）
    capture_llm_payloads: bool = False


class EvaluationConfig(BaseModel):
    """评测配置。"""

    golden_set_path: str = "datasets/golden_set.jsonl"

    dataset_dir: str = Field(
        default="datasets",
        description=(
            "允许评测接口读取的数据集根目录。"
            "评测接口接受调用方传入 dataset_path，若不限制范围，"
            "任何能访问服务的人都能让服务器读取任意可读文件。"
        ),
    )
    judge_model: str = "deepseek:deepseek-chat"
    enable_bidirectional_judge: bool = Field(
        default=True,
        description="启用双向评估以消除 LLM-as-Judge 的位置偏差",
    )

    reference_judge_model: str = Field(
        default="qwen:qwen-plus",
        description=(
            "异构参考裁判。与 judge_model **不同模型族**时，可用于标定"
            "judge_model 的自我偏好偏差。留空则不做标定。"
        ),
    )

    calibration_sample_limit: int = Field(
        default=0,
        ge=0,
        description=(
            "标定用的样本数上限。0 表示用数据集全部样本。"
            "样本数低于 20 时标定结果不可靠，不会用于分数校正。"
        ),
    )

    report_dir: str = "reports/eval"


class DatabaseConfig(BaseModel):
    url: str = "postgresql+asyncpg://devagent:devagent@localhost:5432/devagent"
    echo: bool = False
    pool_size: int = Field(default=10, gt=0)
    max_overflow: int = Field(default=20, ge=0)


class StorageConfig(BaseModel):
    """任务存储后端选择。

    默认 ``memory``：零依赖即可启动，贡献者 clone 后不用先起数据库。
    生产环境应设为 ``sql`` 并配置 ``database.url``。
    """

    backend: Literal["memory", "sql"] = "memory"
    memory_capacity: int = Field(default=200, gt=0, description="内存模式下的 LRU 容量")
    event_history_limit: int = Field(
        default=2000, gt=0, description="SQL 模式下单任务可回放的事件条数上限"
    )


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379/0"
    stream_max_len: int = Field(default=10_000, gt=0)


class Settings(BaseSettings):
    """应用总配置。

    环境变量示例::

        DEVAGENT_ENV=production
        DEVAGENT_MODELS__DEEPSEEK__API_KEY=sk-xxx
        DEVAGENT_CONTEXT__DEFAULT_BUDGET=32000
    """

    model_config = SettingsConfigDict(
        env_prefix="DEVAGENT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["development", "testing", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"

    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, gt=0, le=65535)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # 前端静态托管。
    # 默认 None = 不托管，`GET /` 返回 API 元信息 JSON（保持既有行为与测试）。
    # 设为目录路径（如 "web"）后，该目录会被挂载到 `/`，
    # 于是「后端 + 前端」同源，一个地址即可访问，无跨域、无 ?api= 参数。
    web_dir: str | None = None

    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_testing(self) -> bool:
        return self.env == "testing"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例。

    使用 ``lru_cache`` 缓存；测试中如需重置，调用 ``get_settings.cache_clear()``。
    """
    return Settings()


def reset_settings_cache() -> None:
    """清除配置缓存（供测试使用）。"""
    get_settings.cache_clear()
