# ADR-0004：沙箱执行默认使用本地进程，Docker 作为可选强化

- **状态**：已采纳
- **日期**：2024-10
- **决策者**：核心开发

---

## 背景

Tester Agent 需要真正执行测试代码，并回传客观结果（stdout / 通过数 / 失败数）。
把模型生成的代码直接跑在宿主进程上是不安全的。需要沙箱。

约束：

1. 开发者在 Windows / macOS / Linux 上都应能用
2. Docker 不是所有人都有，且 CI 中启动容器会显著拖慢测试
3. 安全要求是**真实的**（模型可能生成删除文件的代码），不能假装

## 决策

**双实现 + 统一接口**：

- `LocalProcessSandbox`：默认。进程级隔离 + 命令白名单 + 超时 + 输出截断 + 审计日志
- `DockerSandbox`：可选强化。`--network none`、内存/CPU/PID 限制、只读根文件系统、tmpfs

两者实现同一 `Sandbox` Protocol，通过配置切换。

## 备选方案与权衡

### 方案 A：只提供 Docker

**否决**：

- CI 每个测试都起容器 → 测试从 20 秒变成数分钟
- Windows 用户的 Docker Desktop 需要额外配置（WSL2、共享盘）
- 「clone 即跑」的承诺失效

### 方案 B：只提供本地进程

**否决**：安全边界不足。模型生成的代码可访问网络、读环境变量（含 API Key）、
写入宿主任意路径。不能在没有更强隔离的情况下声称"安全"。

### 方案 C：Python `exec` 沙箱 / RestrictedPython

**否决**：Python 层面无法构造可靠的安全边界，大量已知逃逸手段。
「看起来安全」比「明显不安全」更危险。

### 方案 D：gVisor / Firecracker（微虚拟机）

**否决**：隔离强度优秀，但部署复杂度与项目定位不匹配。
已记录为未来演进方向。

## 实现细节

### `LocalProcessSandbox`

```python
class LocalProcessSandbox:
    """本地进程沙箱。

    这是一个**受限执行环境**，不是安全边界。设计目标是防止意外破坏
    （误删、死循环、输出爆炸），而非防御针对性的逃逸攻击。
    需要强隔离时请使用 DockerSandbox。
    """
```

具体措施：

| 措施 | 实现 |
| --- | --- |
| 命令白名单 | 只允许 `pytest` / `python` 等登记过的可执行文件 |
| 超时 | `subprocess` + 超时强杀，默认 120s |
| 输出截断 | 保留头尾各 N KB，中间省略（避免 100MB 输出撑爆上下文） |
| 工作目录限制 | 解析后必须落在项目根内（防路径穿越） |
| 环境变量清洗 | 移除 `*_API_KEY` 等敏感变量后传递 |
| 审计日志 | 每次执行记录命令、耗时、退出码 |

**明确定位**：这是「防意外」而非「防攻击」。文档和 docstring 中如实说明，
不夸大能力。

### `DockerSandbox`

```python
docker run --rm \
  --network none \
  --memory 512m --memory-swap 512m \
  --cpus 1.0 \
  --pids-limit 128 \
  --read-only \
  --tmpfs /tmp:rw,size=64m \
  -v <workspace>:/workspace:ro \
  -w /workspace \
  <image> <command>
```

- `--network none`：彻底断网，防数据外泄
- `--read-only` + `tmpfs`：根文件系统不可写，仅 `/tmp` 可写且有大小上限
- 工作区挂载为**只读**，测试产物通过单独的可写挂载点回传
- `--pids-limit`：防 fork 炸弹
- `docker` Python SDK 惰性导入，未安装时抛出清晰错误并提示安装 `[sandbox]` extra

### 路径穿越防护

无论哪个实现，执行前都对目标路径做解析 + 包含性检查：

```python
resolved = (project_root / user_path).resolve()
if not resolved.is_relative_to(project_root.resolve()):
    raise SandboxError(f"路径越界：{user_path}")
```

`test_runner.py` 中同样有此检查（`../../etc/passwd` 类输入有专门测试）。

## 后果

### 正面

- 默认路径零依赖，测试快（全量 315 个测试 20 秒）
- Windows / macOS / Linux 一致可用
- 需要强隔离时一条配置切换，接口不变
- 安全能力的**真实边界被如实记录**，不制造虚假安全感

### 负面

- 默认配置的安全性弱于 Docker（已明确标注，不隐藏）
- 两套实现需要维护一致性（由共享的 Protocol 与测试约束）
- Docker 路径在 CI 中覆盖不足（需要 Docker-in-Docker）

### 未解决 / 未来工作

- **默认不启用 `DockerSandbox`** 意味着默认配置不足以运行不可信代码。
  生产部署文档中必须显式提示这一点。
- 更细粒度的资源限制（如磁盘配额）尚未实现。
- 微虚拟机方案（gVisor）保留为演进选项。

## 相关

- `src/devagent/tools/sandbox.py`
- `src/devagent/tools/test_runner.py`
- `tests/unit/test_tools.py`
- `docker/sandbox/Dockerfile`
