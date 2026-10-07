"""部署配置回归测试：``.env.example`` / ``docker-compose.yml`` / ``Dockerfile`` / ``init_db.sql``。

## 为什么给「配置文件」写单元测试

这几处缺陷有同一个特征：**代码全绿，部署即死**。

- ``.env.example`` 的路由模型少了 ``<provider>:`` 前缀 —— README 的「三步跑通」
  第一步 ``cp .env.example .env`` 之后应用直接 ``ValidationError``；
- ``docker-compose.yml`` 给 ``Literal`` 字段塞了非法值 —— api 容器崩溃重启；
- ``scripts/init_db.sql`` 的 ``tasks`` 表与 ORM 不一致，而 ``create_all(checkfirst=True)``
  从不纠正**已存在**的表 —— SQL 模式第一次写入就报 ``no such column: tasks.succeeded``；
- ``Dockerfile`` 只装基础依赖 —— 镜像里 ``import sqlalchemy.ext.asyncio`` 直接 ImportError
  （``[db]`` extra 才带 greenlet）。

这些都不是业务逻辑错误，任何「跑一遍单元测试」都发现不了，因此在这里把
「配置文件 ↔ 代码契约」固化成断言。全部离线：**不需要 Docker，也不需要数据库**
（SQL 走文本解析，compose 走内置解析 + 可选 pyyaml）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import JSON, Boolean, Column, Float, Integer, String, Table, Text

from devagent.config import RoutingConfig, Settings
from devagent.db.models import TaskEventRow, TaskRow

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
INIT_SQL = REPO_ROOT / "scripts" / "init_db.sql"

ENV_PREFIX = "DEVAGENT_"
# Settings.env 是 Literal["development", "testing", "production"]
ALLOWED_ENVS = ("development", "testing", "production")
# 这三条就是本次故障的主角：少一个冒号就起不来
ROUTING_KEYS = (
    "DEVAGENT_ROUTING__SMALL_MODEL",
    "DEVAGENT_ROUTING__MEDIUM_MODEL",
    "DEVAGENT_ROUTING__LARGE_MODEL",
)
# compose 的 ${VAR:-default} 占位符：容器里最终拿到的是默认值
_COMPOSE_PLACEHOLDER = re.compile(r"\$\{[^:}]+:-([^}]*)\}")


# ---------------------------------------------------------------------- #
# 夹具
# ---------------------------------------------------------------------- #


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉进程里可能存在的 ``DEVAGENT_*``。

    ``Settings`` 同时读环境变量与 ``.env`` 文件；若 CI 上有人 export 了
    ``DEVAGENT_*``，断言结果就取决于执行顺序 —— 这是 flaky 的经典来源。
    """
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------- #
# 解析工具
# ---------------------------------------------------------------------- #


def _settings_paths(model: type[BaseModel], prefix: str = "") -> set[str]:
    """递归收集 Settings 能识别的环境变量路径（形如 ``ROUTING__SMALL_MODEL``）。

    ``extra="ignore"`` 意味着**拼错的键不会报错，只会被静默丢弃** ——
    ``DEVAGENT_DB__URL`` 就是这么失效的，所以路径必须显式比对。
    """
    paths: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}".upper()
        paths.add(f"{ENV_PREFIX}{path}")
        for candidate in (field.annotation, *getattr(field.annotation, "__args__", ())):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                paths |= _settings_paths(candidate, f"{path}__")
    return paths


SETTINGS_PATHS = _settings_paths(Settings)


def _env_example_pairs() -> dict[str, str]:
    """解析 ``.env.example`` 的 KEY=VALUE（忽略注释与空行）。"""
    pairs: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def _resolve_compose_value(value: object) -> str:
    """把 compose 的 ``${VAR:-default}`` 还原成容器里实际得到的字符串。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return _COMPOSE_PLACEHOLDER.sub(r"\1", str(value))


def _compose_service_env(service: str) -> dict[str, str]:
    """手工解析 compose 某个服务的 ``environment:`` 块（不依赖 pyyaml）。

    只处理本项目实际使用的「2 空格服务名 + 4 空格 environment + 6 空格 KEY: VALUE」
    形式；结构一旦变复杂，``test_compose_file_parses_as_yaml`` 会在有 pyyaml 时
    用真实解析结果比对，防止这里的兜底解析悄悄漂移。
    """
    env: dict[str, str] = {}
    current = ""
    in_env = False
    for raw in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 2 and stripped.endswith(":"):
            current, in_env = stripped[:-1], False
            continue
        if current != service:
            continue
        if indent == 4 and stripped == "environment:":
            in_env = True
            continue
        if in_env:
            if indent <= 4:  # 块结束（ports / healthcheck ...）
                in_env = False
                continue
            key, sep, value = stripped.partition(":")
            if sep:
                env[key.strip()] = _resolve_compose_value(value.strip().strip('"'))
    return env


def _table_body(table: str) -> str:
    """取出 ``CREATE TABLE IF NOT EXISTS <table> (...)`` 的列定义部分。"""
    text = INIT_SQL.read_text(encoding="utf-8")
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS\s+{table}\s*\((.*?)\n\);", text, re.IGNORECASE | re.S
    )
    assert match is not None, (
        f"scripts/init_db.sql 里找不到 {table} 表定义；"
        "ORM 的 create_all(checkfirst=True) 不会纠正已存在的表，缺表/缺列都会在 SQL 模式下报错"
    )
    return match.group(1)


def _ddl_columns(table: str) -> dict[str, str]:
    """列名 → 列定义（去掉注释与表级约束），如 ``{"id": "VARCHAR(64) PRIMARY KEY"}``。"""
    columns: dict[str, str] = {}
    for raw in _table_body(table).splitlines():
        line = raw.split("--", 1)[0].strip().rstrip(",")
        if not line:
            continue
        name, _, definition = line.partition(" ")
        if name.upper() in {"CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"}:
            continue  # 表级约束，不是列
        columns[name.strip('"')] = definition.strip()
    return columns


def _ddl_type(definition: str) -> str:
    """只取列定义里的类型部分（去掉 NOT NULL / DEFAULT / 约束）。"""
    head = re.split(
        r"\s+(?:NOT\s+NULL|NULL|DEFAULT|PRIMARY|UNIQUE|REFERENCES|CHECK)\b",
        definition.upper(),
    )
    return head[0].strip()


# SQLAlchemy 类型 → 可接受的 DDL 类型关键字。
# Text 必须排在 String 之前（Text 是 String 的子类）。
# Integer 额外接受 SERIAL：它是 Postgres 对「INTEGER + 自增序列」的写法，
# 与 ORM 的 Integer(autoincrement=True) 等价（SQLAlchemy 自己建表也用 SERIAL）。
_ORM_TYPE_TO_DDL: tuple[tuple[type, tuple[str, ...]], ...] = (
    (Text, ("TEXT",)),
    (String, ("VARCHAR",)),
    (Boolean, ("BOOLEAN",)),
    (Integer, ("INTEGER", "SERIAL")),
    (Float, ("DOUBLE PRECISION",)),
    (JSON, ("JSON",)),  # JSONB 也含 "JSON"，同样可接受
)


def _expected_ddl_types(column: Column[object]) -> tuple[str, ...]:
    for sa_type, keywords in _ORM_TYPE_TO_DDL:
        if isinstance(column.type, sa_type):
            return keywords
    raise AssertionError(f"未登记的类型，请补充 _ORM_TYPE_TO_DDL：{column.type!r}")


def _assert_table_matches_orm(table: str, orm_table: Table) -> None:
    """DDL 必须是 ORM 的**超集**，且同名列的类型/非空一致。"""
    columns = _ddl_columns(table)
    missing = sorted(set(orm_table.columns.keys()) - set(columns))
    assert missing == [], (
        f"init_db.sql 的 {table} 表缺列 {missing}；"
        "这些列由 ORM 写入，而 create_all(checkfirst=True) 不会给已存在的表补列"
    )
    # 注意：ColumnCollection 迭代产出的是 Column 对象而不是列名（只有 keys() 是名字），
    # 所以这里用 items() 一次拿到两者。
    for name, orm_column in orm_table.columns.items():
        definition = columns[name]
        expected = _expected_ddl_types(orm_column)
        assert any(keyword in _ddl_type(definition) for keyword in expected), (
            f"{table}.{name} 类型不一致：ORM={orm_column.type!r}（期望 {expected}），DDL={definition!r}"
        )
        if not orm_column.nullable:
            assert "NOT NULL" in definition.upper() or "PRIMARY KEY" in definition.upper(), (
                f"{table}.{name} 在 ORM 里非空，DDL 却允许 NULL：{definition!r}"
            )


# ---------------------------------------------------------------------- #
# 缺陷 1：.env.example 必须能直接构造 Settings
# ---------------------------------------------------------------------- #


def test_env_example_constructs_settings(clean_env: None) -> None:
    """README 让用户 ``cp .env.example .env``，所以它本身必须是一份合法配置。"""
    settings = Settings(_env_file=ENV_EXAMPLE)
    assert settings.env in ALLOWED_ENVS
    assert settings.storage.backend in {"memory", "sql"}


def test_env_example_routing_models_are_provider_qualified(clean_env: None) -> None:
    """路由档位必须是 ``<provider>:<model>``，否则 RoutingConfig 的校验器直接抛错。"""
    pairs = _env_example_pairs()
    parser = RoutingConfig()
    for key in ROUTING_KEYS:
        value = pairs[key]
        provider, model = parser.parse_model_spec(value)
        assert ":" in value and provider and model, (
            f"{key}={value!r} 不是 '<provider>:<model>' 形式（缺冒号会让应用启动即 ValidationError）"
        )
    assert ":" in pairs["DEVAGENT_ROUTING__EMBEDDING_MODEL"]


def test_env_example_keys_all_map_to_settings_fields() -> None:
    """拼错的键会被 ``extra="ignore"`` 静默丢弃，所以逐个核对路径存在。"""
    unknown = sorted(key for key in _env_example_pairs() if key not in SETTINGS_PATHS)
    assert unknown == [], f".env.example 里这些键在 Settings 中不存在（会被静默忽略）：{unknown}"


# ---------------------------------------------------------------------- #
# 缺陷 2：docker-compose 的环境变量必须合法
# ---------------------------------------------------------------------- #


def test_compose_file_parses_as_yaml() -> None:
    """有 pyyaml 时用真实解析结果，顺便校验兜底解析器没有漂移。"""
    yaml = pytest.importorskip("yaml", reason="校验 compose 的 YAML 结构需要 pyyaml")
    data = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "services" in data
    assert {"api", "postgres", "redis"} <= set(data["services"])

    raw_env = data["services"]["api"]["environment"]
    assert isinstance(raw_env, dict)
    from_yaml = {k: _resolve_compose_value(v) for k, v in raw_env.items()}
    assert from_yaml == _compose_service_env("api")


def test_compose_env_values_are_valid_literals(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``DEVAGENT_ENV`` 只能是三个合法值，且整份环境必须能构造出 Settings。"""
    env = _compose_service_env("api")
    assert env, "解析不到 api 服务的 environment，解析器或 compose 结构变了"
    assert env.get("DEVAGENT_ENV") in ALLOWED_ENVS, (
        f"DEVAGENT_ENV={env.get('DEVAGENT_ENV')!r} 不在 {ALLOWED_ENVS} 里，api 容器会崩溃重启"
    )

    for key, value in env.items():
        assert key in SETTINGS_PATHS, f"compose 里的 {key} 不是 Settings 认识的键（会被静默忽略）"
        monkeypatch.setenv(key, value)
    assert Settings(_env_file=None).env in ALLOWED_ENVS


def test_compose_api_keys_use_devagent_prefix() -> None:
    """裸的 ``DEEPSEEK_API_KEY`` 不会被 pydantic-settings 读取（曾因此丢过 Key）。"""
    naked = sorted(
        key
        for key in _compose_service_env("api")
        if key.endswith("API_KEY") and not key.startswith(ENV_PREFIX)
    )
    assert naked == [], f"这些 Key 少了 {ENV_PREFIX} 前缀，容器里等于没配：{naked}"


# ---------------------------------------------------------------------- #
# 缺陷 3：init_db.sql 必须与 ORM 一致
# ---------------------------------------------------------------------- #


def test_init_db_sql_tasks_matches_orm() -> None:
    _assert_table_matches_orm("tasks", TaskRow.__table__)


def test_init_db_sql_task_events_matches_orm() -> None:
    _assert_table_matches_orm("task_events", TaskEventRow.__table__)


def test_init_db_sql_tasks_timestamps_are_float_epoch() -> None:
    """ORM 的时间戳是 Float（Unix 秒）；写成 TIMESTAMPTZ 后 floats 写不进去。"""
    columns = _ddl_columns("tasks")
    for name in ("created_at", "updated_at"):
        assert _ddl_type(columns[name]) == "DOUBLE PRECISION", (
            f"tasks.{name} 必须是 double precision（与 ORM 的 Float 一致），实际：{columns[name]!r}"
        )


def test_init_db_sql_updated_at_trigger_writes_epoch() -> None:
    """触发器写 ``now()`` 会让每次 UPDATE 报类型错（timestamptz 不能隐式转 float8）。"""
    text = INIT_SQL.read_text(encoding="utf-8")
    match = re.search(r"FUNCTION touch_updated_at.*?\$\$ LANGUAGE plpgsql;", text, re.S)
    assert match is not None, "找不到 touch_updated_at 触发器函数"
    assert "EXTRACT(EPOCH FROM" in match.group(0).upper(), (
        "触发器必须写 EXTRACT(EPOCH FROM now())；直接写 now() 会让 tasks 的每次 UPDATE 失败"
    )


def test_init_db_sql_is_idempotent() -> None:
    """重复执行（compose 重启 / 手工 psql -f）必须安全。"""
    text = INIT_SQL.read_text(encoding="utf-8")
    unguarded = re.findall(
        r"CREATE\s+(?:TABLE|INDEX|EXTENSION)\s+(?!IF\s+NOT\s+EXISTS)([^\s(]+)", text, re.IGNORECASE
    )
    assert unguarded == [], f"这些 CREATE 语句缺少 IF NOT EXISTS，重复执行会报错：{unguarded}"
    if "CREATE TRIGGER" in text:
        assert "DROP TRIGGER IF EXISTS" in text, "CREATE TRIGGER 之前必须先 DROP TRIGGER IF EXISTS"


# ---------------------------------------------------------------------- #
# 缺陷 4：镜像必须能跑 SQL 模式
# ---------------------------------------------------------------------- #


def test_dockerfile_installs_db_extra() -> None:
    """``[db]`` extra 才带 greenlet（sqlalchemy[asyncio]）、aiosqlite、asyncpg。"""
    install_lines = [
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if "pip install" in line
    ]
    assert install_lines, "Dockerfile 里没有 pip install 步骤"
    assert any(re.search(r"\.\s*\[\s*db\s*\]", line) for line in install_lines), (
        "Dockerfile 必须安装 '.[db]'，否则镜像里 import sqlalchemy.ext.asyncio 会 ImportError"
    )
