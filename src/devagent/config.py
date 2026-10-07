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
        return self.api_key is not None and bool(self.base_url)


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
    judge_model: str = "deepseek:deepseek-chat"
    enable_bidirectional_judge: bool = Field(
        default=True,
        description="启用双向评估以消除 LLM-as-Judge 的位置偏差",
    )
    report_dir: str = "reports/eval"


class DatabaseConfig(BaseModel):
    url: str = "postgresql+asyncpg://devagent:devagent@localhost:5432/devagent"
    echo: bool = False
    pool_size: int = Field(default=10, gt=0)
    max_overflow: int = Field(default=20, ge=0)


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

    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
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
