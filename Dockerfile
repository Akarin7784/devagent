# DevAgent 应用镜像
#
# 多阶段构建：builder 装依赖，runtime 只带运行必需品。
# 目标：镜像尽量小、不含编译工具链、以非 root 用户运行。

# ---------------------------------------------------------------------- #
# Stage 1: builder
# ---------------------------------------------------------------------- #
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 先只复制依赖声明，利用 Docker 层缓存：
# 只要 pyproject.toml 没变，依赖层就命中缓存
COPY pyproject.toml README.md ./
COPY src/ ./src/

# 装入独立前缀，便于整体复制到 runtime 阶段
RUN python -m pip install --upgrade pip build && \
    python -m pip install --prefix=/install .

# ---------------------------------------------------------------------- #
# Stage 2: runtime
# ---------------------------------------------------------------------- #
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

# 非 root 用户运行（安全基线）
RUN groupadd --gid 10001 devagent && \
    useradd --uid 10001 --gid devagent --create-home --shell /usr/sbin/nologin devagent

WORKDIR /app

# 从 builder 复制已安装的依赖
COPY --from=builder /install /usr/local

# 应用代码与前端静态资源
COPY --chown=devagent:devagent src/ ./src/
COPY --chown=devagent:devagent web/ ./web/
COPY --chown=devagent:devagent datasets/ ./datasets/
COPY --chown=devagent:devagent scripts/ ./scripts/
COPY --chown=devagent:devagent README.md ./

USER devagent

EXPOSE 8000

# 健康检查（与 compose 中保持一致）
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3).status==200 else 1)"

# 生产建议：单 worker + 多并发（编排本身是异步的，多进程会各自持有一份内存 store）
# 需要水平扩展时，应把 TaskStore 换成 Redis/Postgres 实现
CMD ["uvicorn", "devagent.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
