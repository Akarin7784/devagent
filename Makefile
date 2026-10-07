# DevAgent Makefile
# 常用开发命令入口

.PHONY: help install install-dev lint format typecheck test test-unit test-integration \
        test-cov test-fast check check-fix clean smoke run cli-eval index \
        pre-commit prepare-release docker-up docker-down docker-logs docker-build \
        sandbox-build docs-links web-check web-words web-serve

PYTHON := python
SRC := src/devagent
TESTS := tests
NODE := node

help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------- 环境 ----------

install:  ## 安装运行时依赖
	$(PYTHON) -m pip install -e .

install-dev:  ## 安装开发依赖（含测试与检查工具）
	$(PYTHON) -m pip install -e ".[dev,sandbox,eval]"
	pre-commit install

# ---------- 代码质量 ----------

lint:  ## 运行 ruff 静态检查
	ruff check $(SRC) $(TESTS)

format:  ## 格式化代码
	ruff format $(SRC) $(TESTS)
	ruff check --fix $(SRC) $(TESTS)

typecheck:  ## 运行 mypy 类型检查
	mypy $(SRC)

check: lint typecheck  ## 运行全部静态检查

check-fix: format  ## 格式化后再跑全部检查
	ruff check $(SRC) $(TESTS)
	mypy $(SRC)

# ---------- 测试 ----------

test:  ## 运行全部测试
	pytest

test-unit:  ## 只运行单元测试
	pytest -m unit

test-integration:  ## 只运行集成测试（需 DB/Redis）
	pytest -m integration

test-fast:  ## 只跑快速测试（跳过标记为 slow 的）
	pytest -m "not slow" -q

test-cov:  ## 运行测试并生成覆盖率报告
	pytest --cov=$(SRC) --cov-report=term-missing --cov-report=html

smoke:  ## 端到端冒烟（无需 API Key）
	$(PYTHON) scripts/demo_smoke.py

# ---------- 运行 ----------

run:  ## 启动开发服务器
	uvicorn devagent.api.app:create_app --factory --reload --port 8000

cli-eval:  ## 运行评测（golden set）
	$(PYTHON) scripts/run_eval.py

index:  ## 构建并检查代码索引
	devagent index $(SRC) --query "assemble budget"

migrate:  ## 执行数据库迁移
	alembic upgrade head

# ---------- 前端 ----------
# 注：web/ 为零构建前端，无需 npm install，直接打开 web/index.html 即可。
# 只有需要本地静态服务器（避免 file:// 的 CORS 限制）时才用下面这条。

web-words:  ## 导出跨语言词表（从 devagent/enums.py 生成前端可读的 fixture）
	@$(PYTHON) -c "import sys; sys.path.insert(0, 'src'); \
	from devagent.enums import AgentType, StepStatus; \
	import json, pathlib; \
	pathlib.Path('web').mkdir(exist_ok=True); \
	pathlib.Path('web/test_contract_words.json').write_text( \
	    json.dumps({'_generated_by': 'Makefile::web-words', \
	                'step_status': [s.value for s in StepStatus], \
	                'agent_type': [a.value for a in AgentType]}, \
	               ensure_ascii=False, indent=2) + '\n', encoding='utf-8')"
	@echo "[web-words] web/test_contract_words.json 已更新"

web-check: web-words  ## 校验前端（语法检查 + 纯逻辑测试，零依赖）
	$(NODE) --check web/graph.js
	$(NODE) --check web/app.js
	$(NODE) web/graph.test.js

web-serve:  ## 用静态服务器托管前端（零依赖）
	$(PYTHON) -m http.server 5173 --directory web

# ---------- Docker ----------

docker-up:  ## 启动依赖服务（Postgres/Redis）
	docker compose up -d

docker-down:  ## 停止依赖服务
	docker compose down

docker-logs:  ## 查看依赖服务日志
	docker compose logs -f

docker-build:  ## 构建应用镜像
	docker build -t devagent/api:latest .

sandbox-build:  ## 构建沙箱镜像（执行不可信代码前必须）
	docker build -t devagent/sandbox:latest docker/sandbox

# ---------- 发布 ----------

prepare-release:  ## 构建分发包并校验元数据
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

# ---------- 文档 ----------

docs-links:  ## 检查 Markdown 中的链接（需 npx）
	npx --yes lycheeverse/lychee --no-progress --exclude localhost '**/*.md'

# ---------- 清理 ----------

clean:  ## 清理缓存与构建产物
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
