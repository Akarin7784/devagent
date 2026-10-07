# 沙箱镜像：用于执行模型生成的测试代码
#
# 设计目标：最小攻击面、确定性、无网络。
#
# 使用方式（由 DockerSandbox 自动构造命令，此处为等价说明）：
#   docker build -t devagent/sandbox:latest docker/sandbox
#   docker run --rm --network none --memory 512m --cpus 1.0 \
#     --pids-limit 128 --read-only --tmpfs /tmp:rw,size=64m \
#     -v "$PWD:/workspace:ro" -w /workspace \
#     devagent/sandbox:latest pytest -q

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# 仅安装测试执行所需的最小依赖。
# 刻意**不**安装项目自身的完整依赖树——沙箱只负责跑测试，
# 不需要 fastapi / sqlalchemy / numpy 等重型包。
RUN python -m pip install --upgrade pip && \
    python -m pip install "pytest>=8.3" "pytest-asyncio>=0.24" "pytest-cov>=6.0"

# 沙箱内不需要 root；容器本身以非 root 用户运行
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin sandbox

# 工作区由外部只读挂载；此处仅创建挂载点
RUN mkdir -p /workspace && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace

# 不设 CMD：命令由 DockerSandbox 在运行时指定（如 pytest -q tests/）
# 这样同一个镜像可以执行任意白名单内的命令
ENTRYPOINT []
