# 沙箱(Sandbox)设计与实现详解 —— 以 Aegis(tob_audit_agent) 项目为例

> 分析对象：`tob_audit_agent-master`（内部代号 **Aegis**）
> 关键代码路径：
> - 抽象层：`backend/packages/harness/aegis/sandbox/`（`sandbox.py`、`sandbox_provider.py`、`context.py`、`exceptions.py`、`tools.py`、`middleware.py`、`file_operation_lock.py`、`search.py`）
> - AIO 实现层：`backend/packages/harness/aegis/sandbox/aiosandbox/`（`provider.py`、`sandbox.py`、`session_coordinator.py`、`lifecycle_api.py`、`auth.py`、`bootstrap.py`、`paths.py`、`transfer.py`、`activity.py`）
> - 配置：`backend/packages/harness/aegis/config/aiosandbox_config.py`、根目录 `config.yaml`
> - 状态可视化：`backend/app/channels/sandbox_status.py`
> - 上传/传输：`backend/app/gateway/routers/uploads.py`

本文详细讲解 Aegis 中的**沙箱系统（Sandbox）**：为什么需要沙箱、抽象设计、AIO 沙箱的完整生命周期、按用户隔离、工具与文件传输、鉴权与安全、失败容错，并逐处结合项目源码给出举例。

---

## 目录

1. [为什么需要沙箱](#1-为什么需要沙箱)
2. [顶层设计与分层](#2-顶层设计与分层)
3. [抽象契约：`Sandbox` 与 `SandboxProvider`](#3-抽象契约sandbox-与-sandboxprovider)
4. [Owner 隔离模型：`OwnerContext` / `SandboxAcquireContext`](#4-owner-隔离模型ownercontext--sandboxacquirecontext)
5. [AIO Sandbox 的实体与目录布局](#5-aio-sandbox-的实体与目录布局)
6. [会话生命周期：创建 / 恢复 / 续期 / 暂停 / 清理](#6-会话生命周期创建--恢复--续期--暂停--清理)
7. [会话协调器 `AioSandboxSessionCoordinator`](#7-会话协调器-aiosandboxsessioncoordinator)
8. [控制面客户端 `AioLifecycleClient` 与鉴权](#8-控制面客户端-aiolifecycleclient-与鉴权)
9. [沙箱中间件与懒初始化](#9-沙箱中间件与懒初始化)
10. [工具层：`bash / ls / read_file / write_file / str_replace / glob / grep`](#10-工具层bash--ls--read_file--write_file--str_replace--glob--grep)
11. [文件互传：`transfer.py` 与 Gateway uploads](#11-文件互传transferpy-与-gateway-uploads)
12. [用户可视化：唤醒 Banner 与卡片状态](#12-用户可视化唤醒-banner-与卡片状态)
13. [安全边界总结](#13-安全边界总结)
14. [端到端时序图](#14-端到端时序图)
15. [小结](#15-小结)

---

## 1. 为什么需要沙箱

Aegis 是一个企业级 Super Agent，**LLM 会生成任意 `bash` 命令、`python` 脚本、文件读写和 skill 执行**。如果这些代码直接跑在后端 Gateway/LangGraph 主机上：

- 命令可能读到主机的 `/etc/passwd`、`~/.ssh/`、环境变量中的密钥；
- 长跑 Python 训练脚本会耗尽 Gateway 内存；
- 不同用户会话之间没有物理隔离，A 用户能读到 B 用户的中间产物；
- 依赖污染：pip install 的东西会互相冲突。

Aegis 的答案是：**每一个用户拥有一个专属的远程沙箱容器（AIO Sandbox）**，所有 LLM 触发的执行都通过 HTTP 控制面转发到该容器内的 `$USER=gem` 用户身份执行；主机进程只保留"协调 / 编排 / 鉴权"角色。

Aegis 已经把这条路径写死：`config.yaml` 中 `sandbox.use` 只允许 `aegis.sandbox.aiosandbox.provider:AioSandboxProvider`，README 也明确"AIO sandbox is the supported runtime" —— 生产不允许 fallback 到本机执行。

---

## 2. 顶层设计与分层

沙箱模块自上而下有四层，职责严格分离：

| 层次 | 目录 / 文件 | 角色 |
|---|---|---|
| **契约层** | `sandbox/sandbox.py`、`sandbox_provider.py` | 定义 `Sandbox` 与 `SandboxProvider` 抽象基类，暴露 `execute_command / read_file / write_file / glob / grep / replace_in_file` 等纯净接口 |
| **实现层** | `sandbox/aiosandbox/*` | AIO 沙箱的真实实现，包括 provider、handle、生命周期客户端、会话协调、鉴权、bootstrap、传输 |
| **中间件层** | `sandbox/middleware.py`、`sandbox/tools.py` | 把 `SandboxProvider` 接到 LangGraph Agent 上：`SandboxMiddleware` 负责 acquire/release，`tools.py` 把工具函数暴露给 LLM |
| **业务层** | `app/gateway/routers/uploads.py`、`app/channels/sandbox_status.py` | Gateway 上传下载路由、Feishu 卡片唤醒 Banner 等 UI 集成 |

关键设计原则：

- **Fail-closed（失败即拒）**：`AioSandboxProvider._validate_context` 若拿不到 `SandboxAcquireContext`，直接抛 `SandboxRuntimeError`，从不退化到 `LocalSandbox`；
- **Owner-first**：所有 acquire、tool 调用、上传下载都必须携带同一 `owner_key`，中途换人立即报 `AIO sandbox owner mismatch`；
- **Phase 化**：`aiosandbox/` 内 `phase0_preflight` ~ `phase8_removal` 一系列测试文件表明模块是按阶段迁移设计的，抽象接口在数据面尚未实现时可以先"骨架化"（`_not_implemented`）。

---

## 3. 抽象契约：`Sandbox` 与 `SandboxProvider`

### 3.1 `Sandbox`（`sandbox/sandbox.py`）

```python
class Sandbox(ABC):
    _id: str

    @abstractmethod
    def execute_command(self, command: str, env: Mapping[str, str] | None = None) -> str: ...

    def execute_command_structured(self, command, env=None, *, no_change_timeout=None) -> CommandResult: ...

    @abstractmethod
    def read_file(self, path: str) -> str: ...

    @abstractmethod
    def list_dir(self, path: str, max_depth=2) -> list[str]: ...

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None: ...

    @abstractmethod
    def glob(self, path, pattern, *, include_dirs=False, max_results=200) -> tuple[list[str], bool]: ...

    @abstractmethod
    def grep(self, path, pattern, *, glob=None, literal=False, case_sensitive=False, max_results=100) -> tuple[list[GrepMatch], bool]: ...

    @abstractmethod
    def update_file(self, path: str, content: bytes) -> None: ...

    def replace_in_file(self, path, old_str, new_str, *, replace_all=False) -> None:
        # 默认走 read-modify-write，AIO Provider 可覆写为一次原子 RPC
        ...
```

要点：

- **`execute_command_structured` 是新一代接口**：返回 `CommandResult(output, exit_code, duration_ms)`，比只回 stdout 的老接口能让 Agent 判断 "exit_code != 0 就是失败"；
- **`no_change_timeout`** 是 AIO shell 特有的"输出无变化超时"，让长跑命令在真正卡死时才终止（体现在 AIO shell 的 `exec_command` 参数里，见后文）；
- **`replace_in_file` 默认实现是 read-modify-write，但 AIO 版本会覆写成一次 RPC**（`replace_in_file` 一次调用即可完成，避免 GET + PUT 竞争）。

### 3.2 `SandboxProvider`（`sandbox/sandbox_provider.py`）

```python
class SandboxProvider(ABC):
    @abstractmethod
    def acquire(self, context: SandboxAcquireContext | str | None = None) -> str: ...
    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None: ...
    @abstractmethod
    def release(self, sandbox_id: str) -> None: ...

# 单例缓存 + shutdown 钩子
_default_sandbox_provider: SandboxProvider | None = None
def get_sandbox_provider(**kwargs) -> SandboxProvider: ...
```

Provider 是 **进程级单例**（`RLock` 保护），保证同一 Python 进程内所有 tool 拿到的是同一份 provider，从而共享 owner→sandbox 的映射与 owner-level 的锁。

### 3.3 异常体系（`sandbox/exceptions.py`）

统一的异常层次，让上层可以按类型识别错误：

| 异常 | 用途 |
|---|---|
| `SandboxError` | 根异常，带 `details: dict` 结构化字段 |
| `SandboxRuntimeError` | 运行时/配置错误 |
| `AioProviderError(SandboxRuntimeError)` | 控制面错误，带 `category` 分类（如 `NotFound.SessionDeleted`） |
| `SandboxCommandError` | 命令执行失败，带 `command` / `exit_code` |
| `SandboxFileError` → `SandboxPermissionError` / `SandboxFileNotFoundError` | 文件操作 |
| `SandboxNotFoundError` | Sandbox 实例找不到 |

工具层通过 `except SandboxError as e: return f"Error: {e}"` 一网打尽，把结构化 details 折成人类可读文本回给 LLM。

---

## 4. Owner 隔离模型：`OwnerContext` / `SandboxAcquireContext`

沙箱多租户隔离的核心是**"owner_key"**，定义在 `sandbox/context.py`：

```python
def derive_owner_key(channel_name: str, app_id: str, open_id: str) -> str:
    return sha256(f"{channel_name}:{app_id}:{open_id}".encode()).hexdigest()

@dataclass(frozen=True)
class OwnerIdentity:
    channel_name: str    # e.g. "feishu"
    app_id: str          # 飞书 App ID（原文只用于哈希，不落盘）
    open_id: str         # 用户 open_id（原文只用于哈希，不落盘）

    @property
    def owner_key(self) -> str: ...
    @property
    def app_id_hash(self) -> str: ...
    @property
    def open_id_hash(self) -> str: ...
```

- `owner_key` 是 **SHA-256 派生**的，日志/落盘只见到哈希值，原始 `app_id/open_id` 只在派生时短暂使用；
- `SandboxAcquireContext` 是**所有 acquire 边界必须携带的对象**，包含 `thread_id / owner_key / channel_name / app_id_hash / open_id_hash / run_id / source(OwnerContextSource)`，其中 `source` 是枚举，标注是来自 `CHANNEL / GATEWAY / JOB / ARTIFACT / AUTH_CALLBACK / EMBEDDED_CLIENT / TEST`；
- `from_mapping` 支持从 JSON 反序列化，专门解决 LangGraph SDK 在 HTTP 上把 `context` 序列化为 dict 的问题；
- Provider 收到裸 `thread_id` 会直接抛 `SandboxRuntimeError("AIO sandbox requires SandboxAcquireContext; refusing naked thread_id fallback")` —— **fail-closed 的护栏**。

**举例**：飞书渠道收到用户消息 → `channel.py` 构造 `OwnerIdentity(channel_name="feishu", app_id=..., open_id=...)` → 派生 `owner_key` → 打包成 `SandboxAcquireContext` → 通过 LangGraph SDK `configurable.context` 传给后端 → 中间件 `SandboxMiddleware._acquire_context_from_runtime` 从 runtime.context 中还原对象。

---

## 5. AIO Sandbox 的实体与目录布局

### 5.1 沙箱内路径契约（`aiosandbox/paths.py`）

```python
@dataclass(frozen=True)
class SandboxPaths:
    home: str = "$HOME"
    local_root: str = "$HOME/.aegis"
    workspace: str = "$HOME/.aegis/workspace"
    outputs: str = "$HOME/.aegis/outputs"
    runtime: str = "$HOME/.aegis/runtime"
    cache: str = "$HOME/.aegis/cache"
    jobs: str = "$HOME/.aegis/jobs"
    tmp: str = "$HOME/.aegis/tmp"
    bin: str = "$HOME/.aegis/bin"
    skills_public: str = "$HOME/.aegis/skills/public"
    skills_custom: str = "$HOME/.aegis/skills/custom"
    skills_venv: str = "$HOME/.aegis/venv"
    uploads: str = "/mnt/uploads"
    state: str = "/mnt/state"
    manifests: str = "/mnt/manifests"
    setup_repo: str = "/home/tiger/audit_agent_setup"
```

沙箱内 `$HOME=/home/tiger`（与 AIO 官方文档中 `gem` 用户不同，Aegis 定制过），Agent 生成的所有产物落在 `$HOME/.aegis/*`；`/mnt/uploads` 与 `/mnt/state` 是 AIO 的**共享挂载点**，横跨会话生命周期（会话被暂停/重建时数据不丢）；`/home/tiger/audit_agent_setup` 是镜像内预置的 setup 脚本仓库。

在 `AioSandbox` 的 `_normalize_sandbox_path` 中，还有一条 **路径修正规则**：

```python
if path.startswith("$HOME/.aegis"):
    return "/home/tiger/.aegis" + path.removeprefix("$HOME/.aegis")
if path.startswith("/root/.aegis"):
    raise SandboxRuntimeError("AIO sandbox HOME is /home/tiger, not /root. ...")
```

—— 显式禁止 `/root/.aegis`，避免 LLM 写错路径把文件"写到不存在的 root 家目录"。

### 5.2 AIO 会话状态机（`aiosandbox/lifecycle_api.py`）

```python
class AioSessionState(StrEnum):
    CREATING = "creating"   # 首次创建中
    ACTIVE = "active"       # 已就绪、可执行
    PAUSING = "pausing"     # 从 active 转为 paused 中
    PAUSED = "paused"       # 已暂停（磁盘保留，节省算力）
    RESUMING = "resuming"   # 从 paused 唤醒中
    UNKNOWN = "unknown"     # 控制面查询失败
    LOST = "lost"           # 上游确认已丢失
    ERROR = "error"         # 无法恢复的错误
```

对应到用户体验：**冷启动创建**≈ 80s、**恢复暂停**≈ 15s（`provider.py` 中的 `_COLD_CREATE_ETA_SECONDS = 80` / `_RESUME_ETA_SECONDS = 15`）；**热复用**（active session 直接命中）零等待。

---

## 6. 会话生命周期：创建 / 恢复 / 续期 / 暂停 / 清理

### 6.1 AIO 沙箱配置（`config/aiosandbox_config.py`）

```python
class AioSandboxConfig(BaseModel):
    psm: str = "bytedance.sandbox.rc_tob_agent_sandbox"
    region: str = "CHINA_NORTH6"
    image: str | None = None
    provider_ttl_seconds: int = 21600           # 6h 硬 TTL
    renew_interval_seconds: int = 7200          # 2h 续期一次
    idle_pause_seconds: int = 10800             # 3h 空闲即暂停
    status_stale_seconds: int = 300
    paused_drift_check_seconds: int = 3600
    lifecycle_scheduler_enabled: bool = True
    lifecycle_scheduler_interval_seconds: float = 30.0
    lifecycle_scheduler_batch_size: int = 100
    lifecycle_scheduler_lock_ttl_seconds: int = 120
    ...
    max_owner_sessions: int = 50
    fake_mode: bool = False
    local_auth: AioSandboxLocalAuthConfig = ...

    @model_validator(mode="after")
    def validate_lifecycle_margin(self) -> Self:
        # 续期间隔 <= TTL/2，保证 TTL 到期前至少续一次
        # idle_pause < TTL，保证暂停在 TTL 到期前发生
        # scheduler_interval < lock_ttl，保证扫描器锁不会到期时无人续
        ...
```

Pydantic 层的**跨字段约束**把生命周期不变式写死：`renew_interval * 2 <= provider_ttl`、`idle_pause < provider_ttl` —— 配置写错启动就报错，避免线上出现"续期还没跑，会话已 TTL 过期"这种时序 bug。

### 6.2 Provider 的 acquire 主流程（`aiosandbox/provider.py`）

```python
class AioSandboxProvider(SandboxProvider):
    def acquire(self, context, *, stream_writer=None) -> str:
        context = self._validate_context(context)        # ① fail-closed 校验
        with self._lock:
            owner_lock = self._owner_locks.setdefault(context.owner_key, Lock())
        with owner_lock:                                 # ② owner 级本地锁（同进程去抖）
            return self._acquire_transaction(context, stream_writer=stream_writer)

    def _acquire_transaction(self, context, *, stream_writer=None) -> str:
        readiness_wait_timeout = min(
            float(self._coordinator.owner_lock_ttl_seconds),
            _COLD_CREATE_ETA_SECONDS + _READINESS_WAIT_MARGIN_SECONDS,  # 80 + 40
        )
        with self._coordinator.distributed_lease(          # ③ ABase 分布式读就绪锁
            f"{self._coordinator.prefix}:lock:readiness:{context.owner_key}",
            wait_timeout=readiness_wait_timeout,
        ):
            return self._acquire_transaction_under_lease(context, stream_writer=stream_writer)
```

三层锁的意义：

1. **`_validate_context`**：非法调用者立刻拦下；
2. **`owner_locks`（threading.Lock）**：进程内多协程并发对同一 owner 只有一个能进入 acquire；
3. **`distributed_lease`（ABase 分布式租约）**：跨 Gateway/LangGraph 多实例部署时，同一 owner 全局只有一个 acquire 事务，且租约 TTL 与冷启动 ETA 对齐，避免"另一台实例等 5s 就抢"。

进入事务后，`_acquire_transaction_under_lease` 会：

- 通过 `AioSandboxSessionCoordinator.acquire_with_allocator(context, allocation)` 尝试**复用/恢复/新建** provider session；
- 若 `coordinated.created` 或 `coordinated.resumed` 为真（即真正发生了唤醒），调用 `_emit_wake_event` 通过 LangGraph `stream_writer` 推一个 `sandbox_waking` 自定义事件给上游渠道（用于飞书卡片显示 Banner，见 §12）；
- 构造/复用 `AioSandbox` 句柄，写入 `_sandboxes` 表；返回 `sandbox_id`。

### 6.3 Lifecycle Scheduler（Gateway 后台调度）

`lifecycle_scheduler_enabled=True` 会在 Gateway `lifespan` 内启动**单实例调度线程**，周期性（默认 30s）：

- 扫描到期需要 `renew` 的 owner，调用 `session_coordinator.process_due_renewals`；
- 扫描 `paused` 会话做 drift check（默认 1h 一次），确认远端没被清理；
- 扫描 orphan（无 owner 引用的 provider session），做兜底 `delete`。

分布式领导人选举通过 ABase `lock:leader` + `lifecycle_scheduler_lock_ttl_seconds=120` 实现，多副本部署时只有 leader 跑清理任务，其它副本 standby。

---

## 7. 会话协调器 `AioSandboxSessionCoordinator`

（`aiosandbox/session_coordinator.py`，约 2400 行，是整个沙箱模块最重的组件）

它把"owner ↔ sandbox_id ↔ provider_session_id"三层映射存在 **ABase**（Redis 兼容 KV 存储）里，key 前缀 `aegis:aio:v2:*`，用同一把分布式锁保护每一次转移。

核心 API：

| 方法 | 作用 |
|---|---|
| `acquire_with_allocator(context, allocator)` | 完整事务：查已有 owner→session 映射；若无则调 `allocator.create`；处理 paused→resume；写回 `renew_deadline`；返回 `CoordinatedSession` |
| `process_due_renewals(...)` | 遍历 `renew` 队列，对到期 session 调 `provider.renew`；若返回 `NotFound.SessionDeleted`，走 `_reap_session` 清理 |
| `process_due_drift_checks(...)` | 对 paused session 抽查 `provider.status`，防止上游偷偷删了 |
| `process_due_orphan_cleanup(...)` | 清理 dangling provider session |
| `capacity_lock_preflight(...)` | 预检 `max_owner_sessions` 容量锁，避免 owner 越界 |
| `distributed_lease(key, ...)` | 通用 ABase 锁上下文管理器 |
| `_upsert_thread_mapping(context)` | 记录 `thread_id → owner_key` 双向索引，便于 Gateway 上传下载时反查 owner |

`CoordinatedSession` 数据类：

```python
@dataclass(frozen=True)
class CoordinatedSession:
    sandbox_id: str
    provider_session_id: str
    owner_key: str
    state: SessionState                    # ACTIVE / PAUSED / UNKNOWN / LOST
    generation: int                        # 每次重建 +1，防"过期句柄误用"
    created: bool                          # 是否新建
    renew_deadline: float
    resumed: bool = False                  # 是否是 paused → active
```

**举例：热复用 vs 冷启动 vs 恢复**

| 场景 | Coordinator 行为 | 用户感知 |
|---|---|---|
| 3 分钟前刚聊过，session 还是 `ACTIVE` | 直接返回缓存 `sandbox_id`，`created=resumed=False` | 无 Banner，秒回 |
| 4 小时前聊过，session 已 `PAUSED` | 触发 `provider.resume`，状态 `PAUSED→RESUMING→ACTIVE`，`resumed=True` | 显示"欢迎回来，沙箱唤醒中，预计 15 秒" |
| 全新用户/被清理 | 触发 `provider.create` → `runtime-ready` 探针 → `ACTIVE`，`created=True` | 显示"专属沙箱创建中，预计 1-2 分钟" |

---

## 8. 控制面客户端 `AioLifecycleClient` 与鉴权

### 8.1 HTTP 控制面（`aiosandbox/lifecycle_api.py`）

用 `httpx` 直接调 AIO Control Plane 的 REST API（`create / resume / pause / status / renew / delete`），带 `control_plane_timeout_seconds=60.0` 硬超时。

亮点是**日志脱敏**：

```python
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 匹配 URL query 里的 jwt_token / zti_token / access_key / ticket
    # 匹配 Header 中的 Authorization / X-Jwt-Token / aegis_internal_api_secret
    # 匹配 "Bearer <base64>" 形式
    # 匹配 "eyJ..." 三段 JWT
    ...
)

def redact_lifecycle_text(text: str) -> str:
    # 先替换动态注册的密钥字面量，再走正则
    ...

def _redact_log_record(record: logging.LogRecord) -> logging.LogRecord:
    # 把 record.msg / exc_text / stack_info 全部脱敏
    ...
```

**注册自定义 logging Filter**：任何写到 `aegis.sandbox.aiosandbox.*` 记录器的日志都会先过一遍 `_redact_log_record`，保证 JWT / session_url / cookie 永远不会出现在 stdout 或 tos 日志归档中 —— 这是企业级合规硬要求。

### 8.2 本地开发鉴权（`aiosandbox/auth.py`）

生产 Gateway 通过服务身份（PSM）签名调用；本地开发者需要以自己身份鉴权：

```python
_TOKEN_COMMAND = ("bytedcli", "auth", "get-bytecloud-jwt-token")

# 缓存到 ~/.bytedcli/aegis-aio-bytecloud-jwt.json
def _cache_path(config): return Path(config.local_auth.jwt_cache_path).expanduser()

# TTL 由 jwt_cache_ttl_seconds 与 JWT 内 `exp` 中的较小者决定
def _refresh_deadline(config, token, *, generated_at):
    deadline = generated_at + config.local_auth.jwt_cache_ttl_seconds
    exp = _decode_jwt_exp(token)
    if exp is not None:
        deadline = min(deadline, float(exp - _JWT_EXPIRY_MARGIN_SECONDS))  # 提前 30s 刷新
    return deadline
```

- 单次开发同时启多个进程（LangGraph + Gateway）时，通过 **`fcntl` 文件锁**保证只有一个进程真正调 `bytedcli`；
- 生成的 token 打上 `_GENERATED_MARKER_SUFFIX` 环境变量指纹，避免与用户手动 `export AIO_SANDBOX_JWT_TOKEN=...` 的 token 混用；
- `AioSandboxLocalAuthConfig.jwt_header_enabled` 默认 `False`，**production must leave this disabled** —— 白纸黑字写在字段 description 里。

---

## 9. 沙箱中间件与懒初始化

`SandboxMiddleware`（`sandbox/middleware.py`）是把 Provider 装到 LangGraph Agent 上的胶水：

```python
class SandboxMiddleware(AgentMiddleware[SandboxMiddlewareState]):
    def __init__(self, lazy_init: bool = True):
        self._lazy_init = lazy_init

    @override
    def before_agent(self, state, runtime):
        if self._lazy_init:
            return super().before_agent(state, runtime)   # 懒模式：这里不 acquire
        # 饥饿模式：进 agent 前就 acquire
        ...

    @override
    def after_agent(self, state, runtime):
        # agent 结束后 release，让 provider 决定是保留 active 还是 pause
        sandbox_id = ...  # 从 state 或 runtime.context 里取
        get_sandbox_provider().release(sandbox_id)
```

默认 **lazy_init=True**：如果这一轮 LLM 只是纯对话（没有工具调用），就完全跳过 acquire，节省 80s 冷启动；只有当第一个 tool 真正执行时，`tools.py` 里的 `ensure_sandbox_initialized(runtime)` 才会触发 `provider.acquire(...)`。

`ensure_sandbox_initialized` 的完整流程：

```python
def ensure_sandbox_initialized(runtime):
    # 1. 检查 state["sandbox"] 是否已有 sandbox_id 且 provider 还持有
    # 2. 若持有，还要走 _validate_sandbox_owner —— 拒绝跨 owner 复用
    # 3. 否则：从 runtime.context/config 中还原 SandboxAcquireContext
    # 4. provider.acquire(acquire_context) 拿到 sandbox_id
    # 5. 写回 runtime.state["sandbox"]，供后续 tool 复用
```

`_validate_sandbox_owner` 是**跨会话安全的最后一道闸**：

```python
if current_context.owner_key != sandbox_context.owner_key:
    raise SandboxRuntimeError(
        "AIO sandbox owner mismatch",
        {"sandbox_id": ..., "expected_owner_key": ..., "sandbox_owner_key": ...},
    )
```

—— 即使 provider 缓存不小心把 A 用户的 sandbox 返给了 B 用户，这里也会立即抛错。测试文件 `test_cross_user_security_boundaries.py` 就是覆盖这条路径。

---

## 10. 工具层：`bash / ls / read_file / write_file / str_replace / glob / grep`

`sandbox/tools.py` 用 `@tool` 装饰器把每个原语暴露给 LLM。所有工具的骨架都是同一个模式：

```python
@tool("bash", parse_docstring=True)
def bash_tool(runtime, description: str, command: str) -> str:
    """Execute a bash command in a Linux environment.
    - Use `python` to run Python code.
    - Use `python -m pip` to install Python packages.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)   # 懒 acquire
        max_chars = get_app_config().sandbox.bash_output_max_chars if ... else 20000
        result = sandbox.execute_command_structured(command)
        BASH_TOOL_METADATA.set({"exit_code": result.exit_code, "duration_ms": result.duration_ms})
        return _truncate_bash_output(result.output, max_chars)
    except SandboxError as e:
        _set_bash_error_metadata()   # 打上 exit_code=-1 元数据，便于上游识别失败
        return f"Error: {e}"
    ...
```

值得注意的几个工程细节：

### 10.1 输出截断策略（`_truncate_*`）

- `_truncate_bash_output`：**中间截断**（保留头尾），因 bash 的 stderr/stdout 顺序不确定，两端都可能有错误信息；
- `_truncate_read_file_output`：**头部截断**（保留文件前 N 字节），源代码顶部往往有 imports / class 定义；
- `_truncate_ls_output`：**头部截断**（目录列表从上往下最重要）。

截断标记里明确写出 skipped 字符数和 total 长度，Agent 可以知道"我看到的不完整"并选择用 `start_line/end_line` 重新读。

### 10.2 公共 skill 目录只读保护

```python
_PUBLIC_SKILLS_RUNTIME_PREFIXES = (
    "$HOME/.aegis/skills/public",
    "/home/tiger/.aegis/skills/public",
)

def _is_public_skills_runtime_path(path): ...
def _public_skills_runtime_write_error(path):
    return (
        f"Error: Refusing to modify runtime public skills path {path!r}. "
        "$HOME/.aegis/skills/public is a generated runtime copy that is "
        "overwritten by upgrade_public_skills. Edit the host source ..."
    )
```

`write_file_tool` 与 `str_replace_tool` 都会先过这一层白名单，防止 Agent 修改沙箱内运行时 skill 的拷贝（这些拷贝会在下次 upgrade 时被完全覆盖）。

### 10.3 文件操作锁（`file_operation_lock.py`）

```python
_FILE_OPERATION_LOCKS: weakref.WeakValueDictionary[_LockKey, threading.Lock] = ...

def get_file_operation_lock(sandbox, path) -> threading.Lock:
    lock_key = (sandbox.id, path)
    ...
    return lock
```

`write_file_tool` / `str_replace_tool` 都用 `with get_file_operation_lock(sandbox, path):` 包裹，防止同一进程内两个并发工具调用对同一路径出现 write-write 或 write-replace 竞争。用 `WeakValueDictionary` 是为了避免长跑进程 lock 表无限膨胀。

### 10.4 元数据回填

```python
BASH_TOOL_METADATA: contextvars.ContextVar[dict[str, int] | None] = ContextVar(...)
```

`bash_tool` 通过 `ContextVar` 把 `exit_code / duration_ms` 传出去，上层 middleware 会读这个 ContextVar 把结构化元数据附到 tool message 上，供后续 Agent 判断"上次 bash 是不是失败"。

---

## 11. 文件互传：`transfer.py` 与 Gateway uploads

用户从飞书上传的 PDF / 图片，与 Agent 在沙箱内产出的 outputs，都要跨"主机 ↔ 沙箱"两侧穿梭。`aiosandbox/transfer.py`（约 1900 行）承担这个角色。

### 11.1 路径前缀分工

```python
_OUTPUTS_PREFIX   = "/home/tiger/.aegis/outputs/"
_WORKSPACE_PREFIX = "/home/tiger/.aegis/workspace/"
_UPLOADS_PREFIX   = "/mnt/uploads/"
_STATE_PREFIX     = "/mnt/state/"
_TOS_PREFIX       = "tos://"
```

- 上传流入：`Gateway /uploads` → `/mnt/uploads/`（AIO 的持久挂载，跨 session 生命周期不丢）；
- 沙箱产出：Agent 写到 `$HOME/.aegis/outputs/` → 用户下载走 `Gateway /artifacts` 反向拉；
- 大文件走 **TOS 中转**：`tos_storage_large_threshold_bytes=30MB` 以上自动走后台 job，避免 Gateway 主线程阻塞。

### 11.2 传输配置

```python
class TransferConfig(BaseModel):
    upload_memory_limit_bytes: int = 30 * 1024 * 1024
    upload_chunk_size_bytes: int = 1024 * 1024
    download_chunk_size_bytes: int = 1024 * 1024
    transfer_timeout_seconds: float = 120.0
    spool_memory_limit_bytes: int = 4 * 1024 * 1024
    artifact_max_file_bytes: int = 30 * 1024 * 1024
    artifact_cache_ttl_seconds: int = 86400
    artifact_cache_soft_limit_bytes: int = 512 * 1024 * 1024
    local_soft_quota_bytes: int = 2 * 1024 * 1024 * 1024
    local_hard_quota_bytes: int = 3 * 1024 * 1024 * 1024
    tos_storage_large_threshold_bytes: int = 30 * 1024 * 1024
```

- **soft quota / hard quota**：sandbox 内 `LOCAL` 运行时文件的软限/硬限，超了软限触发告警，超硬限拒写；
- **artifact_cache**：Gateway 缓存"沙箱产物"的 TTL 与总容量，仅是缓存 —— 真源始终是沙箱内文件；
- **spool_memory_limit_bytes**：上传流大于 4MB 就落盘 spool 文件，防止内存爆掉。

### 11.3 Gateway 反查 owner 的关键调用

`app/gateway/routers/uploads.py`：

```python
async def _resolve_aio_acquire_context(thread_id, sandbox_provider):
    if not isinstance(sandbox_provider, AioSandboxProvider):
        return None
    context = await sandbox_provider._coordinator.get_acquire_context_for_thread_async(thread_id)
    return context
```

上传路由拿到用户请求（带 `thread_id`）后，反查 `thread_id → owner_key → SandboxAcquireContext`，然后 acquire 沙箱、走 transfer 写入 `/mnt/uploads/`。整个过程 **owner_key 始终和 thread_id 挂钩**，防止 A 用户用 B 用户的 thread_id 拿到 B 的沙箱。

### 11.4 日志脱敏（再一次）

```python
_SESSION_URL_RE = re.compile(r"(session_url=)\S+", re.IGNORECASE)
_QUERY_TOKEN_RE = re.compile(r"([?&][^=\s&]*(?:token|sign|signature|credential|secret)[^=\s&]*=)[^\s&]+", ...)

def _redact(text):
    redacted = _SESSION_URL_RE.sub(r"\1<redacted>", text)
    redacted = _QUERY_TOKEN_RE.sub(r"\1<redacted>", redacted)
    return redacted
```

`transfer.py` 的日志同样过一遍脱敏，防止 AIO 的 `session_url`（带临时签名）被写进日志盘。

---

## 12. 用户可视化：唤醒 Banner 与卡片状态

（`app/channels/sandbox_status.py`）

冷启动 80s 对用户是煎熬，Aegis 用两条路径把"沙箱在唤醒"这件事告诉用户：

```python
def format_sandbox_wake_status(reason: str | None = None, estimated_seconds=None) -> str:
    if reason == "resume":
        eta = max(1, int(float(estimated_seconds or DEFAULT_RESUME_ETA_SECONDS)))
        return f"欢迎回来，沙箱唤醒中，预计 {eta} 秒"
    return "专属沙箱创建中，预计 1-2 分钟"
```

- 路径 A（Provider 内推事件）：`AioSandboxProvider._acquire_transaction_under_lease` 检测到 `created=True` 或 `resumed=True`，通过 LangGraph `stream_writer` 推 `{"type": "sandbox_waking", "reason": "create"/"resume", "estimated_seconds": 80/15}`；上游 Feishu channel 收到后渲染卡片 Banner；
- 路径 B（Channel 侧首帧预置）：为了避免 Provider 事件到达前的空白，channel 层在冷启动的**第一帧卡片**就把 `format_sandbox_wake_status("create")` 塞进去，两者用同一格式化函数**保证文案一致**从而不会重复渲染。

这段代码的 docstring 明确说明了为什么放在单独文件而非 `manager.py`：**避免 import 循环**。

---

## 13. 安全边界总结

把前面散落的安全设计汇总成一张表：

| 边界 | 实现 | 违反后果 |
|---|---|---|
| 只能用 AIO Provider | `config.yaml` 硬编码 + `_validate_context` fail-closed | 启动即失败或运行时 `SandboxRuntimeError` |
| Owner 隔离 | `owner_key = sha256(channel:app_id:open_id)` | `AIO sandbox owner mismatch` |
| Thread → Owner 映射不可篡改 | Coordinator `_upsert_thread_mapping` + ABase 锁 | 反查失败即拒 |
| 分布式并发 | ABase `distributed_lease` + owner-level readiness lock | 阻塞等待或超时报错 |
| 生命周期不变式 | Pydantic `validate_lifecycle_margin` | 启动即失败 |
| 私钥不落日志 | `_SECRET_PATTERNS` + `_redact_log_record` + `_register_secret_values` | 日志中永远 `[REDACTED]` |
| 只读 skill 目录保护 | `_is_public_skills_runtime_path` | 工具返回 error message，不落盘 |
| 路径规范化 | `_normalize_sandbox_path` 拒绝 `/root/.aegis` | 显式抛错 |
| 传输配额 | soft / hard quota + 30MB TOS 阈值 | 超软限告警、超硬限拒写 |
| 生产禁本地 JWT header | `AioSandboxLocalAuthConfig.jwt_header_enabled=False` | Pydantic field description 明示 |
| 上传流内存保护 | `spool_memory_limit_bytes=4MB` 落盘 | 大文件不 OOM |
| 单 owner 上限 | `max_owner_sessions=50` + `capacity_lock_preflight` | 超限拒 acquire |

---

## 14. 端到端时序图

```
User (Feishu) ──▶ Channel                      Gateway/LangGraph                    AIO Control Plane          AIO Session Container
      │                │                                │                                    │                          │
      │ send msg       │                                │                                    │                          │
      ├───────────────▶│                                │                                    │                          │
      │                │ 派生 OwnerIdentity/Context     │                                    │                          │
      │                │─────── LangGraph.invoke ──────▶│                                    │                          │
      │                │        (context: acquire ctx)  │                                    │                          │
      │                │                                │ SandboxMiddleware.before_agent     │                          │
      │                │                                │  (lazy_init: skip acquire)         │                          │
      │                │                                │                                    │                          │
      │                │                                │ LLM decides: call bash("ls /")     │                          │
      │                │                                │                                    │                          │
      │                │                                │ tools.py: ensure_sandbox_init.     │                          │
      │                │                                │   ├─▶ Provider.acquire(context)    │                          │
      │                │                                │   │    ├─ owner_locks (thread)     │                          │
      │                │                                │   │    ├─ ABase readiness lease    │                          │
      │                │                                │   │    ├─ Coordinator.acquire     │                          │
      │                │                                │   │    │   ├─ ABase kv lookup     │                          │
      │                │                                │   │    │   └─ (miss) allocator ──▶│ POST /session/create      │
      │                │                                │   │    │                          ├─────────────────────────▶ │ (~80s cold)
      │                │                                │   │    │                          │                           │ container up
      │                │                                │   │    │                          │                           │ setup.sh runtime-ready
      │                │                                │   │    │                          │◀─ session_id + url ───────│
      │                │                                │   │    ├─ emit sandbox_waking ────│                           │
      │                │◀── stream: 唤醒 Banner ────────│   │    └─ CoordinatedSession       │                           │
      │◀───────── 卡片显示 ───────────────────────────  │   │                                │                           │
      │                │                                │   └─ AioSandbox handle 缓存      │                           │
      │                │                                │                                    │                           │
      │                │                                │ bash_tool: sandbox.execute_cmd  ──┼─── POST /shell/exec ─────▶│
      │                │                                │                                    │                           │ 执行 ls /
      │                │                                │                                    │◀── {output, exit_code} ───│
      │                │◀── LLM 回复 ─────────────────  │                                    │                           │
      │◀───────── 结果 ─────────────────────────────────│                                    │                           │
      │                │                                │                                    │                           │
      │                │                                │ (agent done) after_agent:          │                           │
      │                │                                │   Provider.release(sandbox_id)     │                           │
      │                │                                │   (仍是 ACTIVE，Coord 不销毁)      │                           │
      │                │                                │                                    │                           │
      │  ...  3h idle  │                                │  Lifecycle Scheduler:              │                           │
      │                │                                │   ├─ process_due_renewals ────────▶│ POST /session/renew       │
      │                │                                │   └─ idle_pause_seconds 触发 ─────▶│ POST /session/pause       │
      │                │                                │                                    │                           │ container 暂停/回收算力
      │                │                                │                                    │                           │
      │ send msg (下一轮)                                 │                                    │                           │
      ├───────────────▶│─── LangGraph.invoke ──────────▶│  ensure_sandbox_init:              │                           │
      │                │                                │   Provider.acquire → PAUSED       │                           │
      │                │                                │   → allocator.resume ────────────▶│ POST /session/resume ────▶│
      │                │                                │   emit sandbox_waking(resume,15s)  │                           │ 15s 恢复
      │                │◀── 唤醒 Banner ────────────────│                                    │                           │
```

---

## 15. 小结

Aegis 的沙箱系统不是"随手写一个 Docker exec"，而是一套**面向多租户 SaaS Agent 场景**的完整基础设施，做对了以下几件事：

1. **契约与实现分离**：`Sandbox` / `SandboxProvider` 抽象让 LLM 工具不感知"到底是 Local 还是 AIO"，未来换成 Firecracker/Codespace 都是新加一个 Provider 的事；
2. **Owner-first 全链路**：从 `OwnerIdentity` 派生 `owner_key`，到 `SandboxAcquireContext` 强制携带，到 middleware 里 `_validate_sandbox_owner` 兜底 —— 跨用户误用在每一层都会立即抛错；
3. **生命周期显式建模**：`CREATING/ACTIVE/PAUSING/PAUSED/RESUMING/LOST` 六态 + Pydantic 跨字段约束 + 后台 Scheduler + ABase 分布式租约，把"会话到期"这类时序 bug 化解在启动时；
4. **懒初始化 + 热复用 + Paused Resume 三级性能优化**：让"纯闲聊零成本 / 老用户 15s 唤醒 / 新用户一次 80s"这条曲线成立；
5. **合规友好**：日志脱敏、私钥 fingerprint、`/mnt` 持久挂载、上传配额、TOS 大文件中转 —— 全部按企业级审计要求做过；
6. **UX 融合**：`sandbox_waking` 事件 + 前后端一致的 Banner 文案，把 80s 冷启动包装成用户可接受的"沙箱创建中"提示。

对读者的启示：任何要给 LLM 提供"任意代码执行"能力的产品，都可以照抄这套骨架 —— 抽象层 + 远程隔离容器 + Owner-lock + ABase Coordinator + 中间件懒 acquire + 工具层结构化输出 —— 从 Day 1 就把多租户安全、可观测性和生命周期做对，比后期回填便宜很多。
