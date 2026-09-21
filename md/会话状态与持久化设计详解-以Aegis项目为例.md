# 会话状态与持久化 —— 设计、应用与实现（以 Aegis 项目为例）

> 本文基于 `tob_audit_agent-master/backend` 源码，系统梳理 Aegis（内部代号，基于 LangGraph 构建的 Agent 运行时）中「会话状态与持久化」这一子系统的**设计理念、职责边界、装配方式与运行时链路**，并结合项目里真实的代码路径与调用点逐一举例说明。

---

## 1. 全局图景：什么是「会话状态」，又持久化了什么

在 Aegis 中，一次「会话」被抽象为一个 **thread**（线程）），由唯一的 `thread_id` 标识。围绕 thread，系统需要回答三个问题：

1. **这轮对话的"记忆"是什么？** —— 消息历史、待办、产物、已看过的图片等，即 **图状态（graph state）**。
2. **系统里一共有哪些会话？** —— 会话列表、标题、状态、创建/更新时间等**会话级元数据**。
3. **这轮对话产生的文件放在哪？** —— 上传件、工作区、输出件等**文件系统产物**。

Aegis 用**三个相互独立又彼此配合**的持久化设施分别承担这三件事，这是理解本子系统的第一把钥匙：

| 持久化设施 | 承载内容 | 关键抽象 | 典型后端 | 代码位置 |
|---|---|---|---|---|
| **Checkpointer（检查点）** | 图状态 `ThreadState`（逐检查点快照，含完整消息历史） | LangGraph `BaseCheckpointSaver` | memory / sqlite / postgres | `aegis/agents/checkpointer/` |
| **Store（键值仓）** | 会话列表元数据（thread 记录、标题） | LangGraph `BaseStore` | memory / sqlite / postgres | `aegis/runtime/store/` |
| **文件系统 / 沙箱** | uploads / workspace / outputs 等物理文件 | `Paths` + owner 沙箱 | 本地 `.aegis/` 或 AIO Sandbox | `aegis/config/paths.py` |

> **设计要点**：Checkpointer 与 Store **共用同一段 `checkpointer` 配置**（见 §4），因此它们的后端技术始终一致（要么都 sqlite，要么都 postgres），避免"状态存 A、列表存 B"的运维割裂。文件系统则完全独立，由沙箱租户隔离模型接管。

---

## 2. 三条职责边界为何要分开

初看会觉得"状态都存 checkpointer 不就好了？"。Aegis 之所以再引入一个 Store，是出于**读取模式**的巨大差异：

- **Checkpointer 面向"单 thread 深度读"**：给定 `thread_id`，取它最新/历史检查点。它按 thread 组织，天然不擅长"列出所有 thread 并排序分页"——那需要全表扫描并反序列化每个巨大的状态快照。
- **Store 面向"跨 thread 广度读"**：会话列表页需要一次性列出成百上千个 thread 的标题与时间。Store 里每条记录只是一个**极小的元数据字典**（`thread_id / status / created_at / updated_at / metadata / values`），`asearch` 一次拉回上万条也很廉价。

这一"**快路径 Store + 慢路径 Checkpointer 兜底**"的两阶段设计，集中体现在 `POST /api/threads/search`（见 §7.2）。

---

## 3. 状态的形状：`ThreadState` 与 reducer

会话内状态的 schema 定义在 `aegis/agents/thread_state.py`：

```python
class ThreadState(AgentState):                       # 继承 LangChain AgentState（自带 messages）
    sandbox: NotRequired[SandboxState | None]        # 当前沙箱 id
    thread_data: NotRequired[ThreadDataState | None] # 线程路径元数据（workspace/uploads/outputs...）
    title: NotRequired[str | None]                   # 会话标题
    artifacts: Annotated[list[str], merge_artifacts]         # 产物路径列表（带 reducer）
    todos: NotRequired[list | None]                          # 待办
    uploaded_files: NotRequired[list[dict] | None]           # 上传文件清单
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]  # 已查看图片
```

**设计精髓 —— reducer（归并函数）**：LangGraph 中，节点返回的状态更新不是简单覆盖，而是通过 `Annotated[类型, reducer]` 声明的归并策略合并进旧状态。Aegis 为两个字段定制了 reducer：

- `merge_artifacts`：合并并**去重**（用 `dict.fromkeys` 保序去重），使多个节点/子智能体产出的产物路径可以持续累积而不重复。
- `merge_viewed_images`：合并图片字典，并约定**传入空字典 `{}` 表示清空**——让中间件在处理完图片后能主动清理这部分状态，避免图片 base64 无限膨胀检查点体积。

> **举例**：当 `view_image` 工具看了一张图，`viewed_images` 里会新增 `{路径: {base64, mime_type}}`；`ViewImageMiddleware` 消费后返回 `{"viewed_images": {}}`，reducer 识别空字典并清空，从而这份沉重的 base64 不会被写进后续每一个检查点。这正是 reducer 语义在"控制持久化体积"上的实战应用。

---

## 4. 三种后端与配置模型

后端类型由 `aegis/config/checkpointer_config.py` 定义：

```python
CheckpointerType = Literal["memory", "sqlite", "postgres"]

class CheckpointerConfig(BaseModel):
    type: CheckpointerType
    connection_string: str | None = None   # sqlite: 文件路径; postgres: DSN
```

- **memory** —— `InMemorySaver`，进程内，重启即丢。默认（配置缺省时）行为。
- **sqlite** —— `SqliteSaver`，单文件持久化，单进程适用。**本项目当前采用**。
- **postgres** —— `PostgresSaver`，多进程/生产适用。

项目实际配置（`config.yaml`）启用了 sqlite：

```yaml
checkpointer:
  type: sqlite
  connection_string: checkpoints.db
```

对应磁盘上确实存在落盘文件（印证持久化生效）：

```
backend/.aegis/checkpoints.db          # 主库
backend/.aegis/checkpoints.db-shm      # WAL 共享内存
backend/.aegis/checkpoints.db-wal      # WAL 日志
```

**路径解析的巧思**（`aegis/runtime/store/_sqlite_utils.py`）：`resolve_sqlite_conn_str` 会对 `:memory:` 与 `file:` URI 原样放行，对普通相对/绝对路径统一经 `resolve_path` 解析为绝对路径；`ensure_sqlite_parent_dir` 在连接前自动创建父目录。因此配置里写相对路径 `checkpoints.db`，最终落到 `backend/.aegis/checkpoints.db` 而与进程 cwd 无关。

> **一致性约束**：Store 的 provider 显式复用 `get_checkpointer_config()`，注释写明"backend mirrors the configured checkpointer"。所以你**无需**单独配置 Store 后端；若整个 `checkpointer` 段�<�失，Store 会退化为 `InMemoryStore` 并打印警告——"Thread list will be lost on server restart"。

---

## 5. 装配：三种入口，同一套后端

Aegis 对同一后端提供了**三类构造入口**，分别服务不同运行形态。这是"应用"层面的关键设计。

### 5.1 异步上下文管理器（服务器主路径）

`aegis/agents/checkpointer/async_provider.py` 的 `make_checkpointer()` 与 `aegis/runtime/store/async_provider.py` 的 `make_store()` 是 **FastAPI 生命周期**使用的入口。它们在 `enter` 时开连接、`exit` 时关连接，不留全局状态。

装配发生在 `app/gateway/deps.py` 的 `langgraph_runtime`：

```python
async with AsyncExitStack() as stack:
    app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge())
    app.state.checkpointer   = await stack.enter_async_context(make_checkpointer())
    app.state.store          = await stack.enter_async_context(make_store())
    app.state.run_manager    = RunManager()
    yield
```

而 `langgraph_runtime` 又被 `app/gateway/app.py` 的 `lifespan` 包裹，于是**服务器启动即建连、关闭即释放**，checkpointer/store 作为单例挂在 `app.state` 上，供所有路由通过 `deps.get_checkpointer(request)` / `deps.get_store(request)` 取用。

### 5.2 同步单例（CLI / 嵌入式 Client）

`aegis/agents/checkpointer/provider.py` 的 `get_checkpointer()` 与 `aegis/runtime/store/provider.py` 的 `get_store()` 提供**进程级单例**，首次调用惰性构造，进程退出时关闭。它们被 CLI 工具和嵌入式 `AegisClient` 使用（见 `client.py` 中 `_ensure_agent` 里 `get_checkpointer()` 的兜底调用）。

单例实现里有一个**测试隔离**的细节：仅当"既没有显式设过 checkpointer 配置、`_app_config` 也未初始化"时，才惰性加载 `config.yaml`；这让测试可以显式注入配置而不被磁盘上的 `config.yaml` 干扰。

### 5.3 一次性上下文管理器（脚本/测试）

`checkpointer_context()` / `store_context()` 每进入一次 `with` 就新建并销毁连接，不缓存，适合需要确定性清理的脚本与测试。

> **一句话总结装配**：*服务器用异步 CM（挂 app.state）、Client/CLI 用同步单例、脚本用一次性 CM；三者读同一段配置、构造同一族后端。*

---

## 6. 运行时链路：一次带记忆的对话如何跑通

这是"实现"层面最核心的一环。以 HTTP 网关发起一次 run 为例（`app/gateway/services.py: start_run`）：

1. **取单例**：`get_checkpointer / get_store / get_stream_bridge / get_run_manager` 从 `app.state` 拿到全套运行时。
2. **登记 run**：`run_mgr.create_or_reject(thread_id, ...)`，按 `multitask_strategy`（reject/interrupt/rollback）处理同一 thread 的并发在途 run（见 §8）。
3. **确保会话可见**：`_upsert_thread_in_store(store, thread_id, metadata)` 把 thread 写进 Store，使它出现在会话列表——**即使是从未显式创建过的无状态 run**。
4. **组装输入与配置**：`normalize_input(body.input)` → `graph_input`；`build_run_config(thread_id, ...)` → `config`，其中 `config["configurable"]["thread_id"]` 是**贯穿全局的关键**。
5. **后台执行**：`asyncio.create_task(run_agent(...))`，把 `checkpointer / store / graph_input / config` 一并交给 worker。
6. **收尾同步标题**：run 结束后 `_sync_thread_title_after_run` 把 `TitleMiddleware` 写进检查点的标题回灌 Store（见 §9）。

在 worker（`aegis/runtime/runs/worker.py: run_agent`）里，持久化如何真正发生：

- **恢复**：agent 是 `create_agent(..., checkpointer=checkpointer)` 编译出的图（`aegis/agents/factory.py`）。调用 `agent.astream(graph_input, config=runnable_config, ...)` 时，LangGraph 依据 `config.configurable.thread_id` **自动从 checkpointer 载入该 thread 的最新检查点**，把历史消息与状态"接续"到本轮输入之上——这就是"跨轮次会话记忆"的落地点，无需业务代码手动读历史。
- **注入 Runtime**：worker 手动构造 `Runtime(context=runtime_context, store=store)` 并塞进 `config.configurable["__pregel_runtime"]`，使中间件能访问 `thread_id`、owner 沙箱上下文与 Store（langgraph-cli 会自动做，网关路径必须手动补）。
- **写回**：随着图逐节点推进，LangGraph 在每一步**自动向 checkpointer 追加新检查点**；worker 只负责把流式产物 `serialize` 后经 `StreamBridge` 推给前端（SSE），不亲自写状态。

> **举例（多轮记忆）**：用户第一轮问"分析这篇论文”，thread_id=`T1`。图执行后 checkpointer 里 `T1` 有了含首轮问答的检查点。第二轮追问"它的第二个贡献是什么？"，网关用**同一个** `T1` 发起 run，`astream` 自动载入首轮消息，模型因此"记得"论文是什么——记忆完全由 checkpointer + thread_id 承载，而非 prompt 拼接。

---

## 7. Thread 路由：状态的对外读写面

`app/gateway/routers/threads.py` 是会话状态的 REST 门面，对齐 LangGraph Platform 线格式，供前端 `useStream` 消费。

### 7.1 创建 `POST /api/threads`

双写：Store 里写一条元数据记录（用于快速列表），checkpointer 里用 `empty_checkpoint()` 写一个空检查点（让状态端点立即可用）。**幂等**：Store 里已存在则直接返回。

### 7.2 搜索 `POST /api/threads/search` —— 两阶段 + 惰性迁移

这是全项目最能体现"Store/Checkpointer 分工"的一段：

- **Phase 1（快路径）**：`store.asearch(THREADS_NS, limit=10_000)` 一次性拿回所有 thread 元数据。这些 thread 是经本网关创建或运行过的。
- **Phase 2（兜底 + 惰性迁移）**：`checkpointer.alist(None)` 遍历检查点，发现**不在 Store 里**的 thread（例如由 LangGraph Server 直接创建的），补进结果；同时立刻 `_store_upsert` 把它写进 Store。于是 Store **随查询逐渐收敛为完整索引**，无需一次性迁移任务。跳过 `checkpoint_ns` 非空的子图检查点。
- **Phase 3**：按 `metadata / status` 过滤 → 按 `updated_at` 倢序 → 分页。

### 7.3 状态读取 `GET /{thread_id}/state`

`checkpointer.aget_tuple(config)` 取最新检查点，返回 `values / next / metadata / checkpoint_id / parent_checkpoint_id / tasks`。`channel_values` 经 `serialize_channel_values` 转成 JSON 安全 dict（见 §10）。`_derive_thread_status` 从 `pending_writes` 里的 `__error__` 与 `tasks` 推导 thread 状态（error / interrupted / idle）。

### 7.4 历史 `POST /{thread_id}/history`

`checkpointer.alist(config, limit)` 顺检查点链回溯，支持 `before` 游标分页，返回每个检查点的 `checkpoint_id / parent_checkpoint_id / values / next`——这就是"时间旅行/回看任意历史检查点"的数据来源。

### 7.5 状态更新 `POST /{thread_id}/state`

用于 human-in-the-loop 续跑或重命名：读最新检查点 → 把 `body.values` 合并进 `channel_values` → **不带 checkpoint_id 地 `aput`** 生成一个全新检查点。若更新了 `title`，同步回灌 Store，使搜索结果即时反映改名。

### 7.6 删除 `DELETE /{thread_id}`

三处清理：AIO 沙箱传输记录 → 本地文件系统（`_delete_thread_data`）→ Store 记录（`adelete`）→ 检查点（`adelete_thread`）。后三者瘆 best-effort，单点失败不阻断整体。

---

## 8. 并发、中断与回滚：状态的"事务性"保护

### 8.1 RunManager 与多任务策略

`aegis/runtime/runs/manager.py` 的 `RunManager` 是**内存态**的 run 注册表（注意：run 本身不持久化，只有状态检查点持久化）。`create_or_reject` 在同一把锁内完成"检查在途 + 创建"，消除 TOCTOU 竞态，支持三种策略：

- `reject`：已有在途 run → 抛 `ConflictError`（409）。
- `interrupt`：取消在途 run（保留其已产生的检查点）后再创建。
- `rollback`：取消并**回滚到本轮开始前的检查点**。

### 8.2 回滚的实现（pre-run snapshot）

worker 在标记 running **之前**，先 `checkpointer.aget_tuple` 抓取 运行前检查点快照�（`pre_run_snapshot`：checkpoint / metadata / pending_writes 的深拷贙）。若本轮被要求 `rollback`，`_rollback_to_pre_run_checkpoint` 会：

- 无快照 → `adelete_thread` 清空该 thread（回到"从未开始"态）；
- 有快照 → 用 `aput` 把旧检查点重新落盘为新头，并逐 task 用 `aput_writes` 复原 `pending_writes`。

> **设计价值**：这让"用户中途反悔"能够**把会话状态原子性地退回本轮之前**，而不是留下半截被污染的检查点。快照抓取失败（`snapshot_capture_failed`）时明确跳过回滚，避免用错误快照破坏状态。

---

## 9. 中间件如何"写状态"：标题的端到端旅程

会话状态的很多字段并非模型直接产出，而是**中间件**在图执行的钩子里写入的。以标题为例串起全链路：

1. **生成**（`aegis/agents/middlewares/title_middleware.py`）：`TitleMiddleware` 在首软"用户+助手"交换完成后触发（`_should_generate_title`：恰好 1 条 human + ≥1 条 ai 且尚无 title），调用小模型生成标题，失败则回退用用户首句截断。它返回 `{"title": ...}`，经由 LangGraph 合并进 `ThreadState.title`，**随下一个检查点持久化**。
2. **回灌 Store**（`services.py: _sync_thread_title_after_run`）：run 结束后从 checkpointer 读出标题，`_store_upsert` 写进 Store 的 `values.title`，使 `/threads/search` 立刻能显示标题——**这正是 Checkpointer→Store 单向同步的典型用例**。

另一个例子是 `ThreadDataMiddleware`（`thread_data_middleware.py`）：它在 `before_agent` 解析当前 thread 的路径元数据写入 `thread_data`，并向最后一条 human 消息**一次性**注入 `<session thread_id="..."/>` 标记（已注入则跳过，避免逐轮重复），让模型知道自己服务于哪个 thread、从而正确� scope `/mnt/uploads/{thread_id}`。

`SummarizationMiddleware` 则从"控制状态体积"角度参与持久化：当消息过长时把旧消息压缩成摘要（`RemoveMessage` + 摘要），使检查点里的 `messages` 不会无限增长——**短期记忆的容量治理**。

---

## 10. 序列化：状态出��关前的最后一道工序

`aegis/runtime/serialization.py` 是"把 LangChain/LangGraph 对象转 JSON 安全结构"的**唯一真源**，被 worker（SSE 推流的) 与 threads 路由（REST 响应）共用：

- `serialize_lc_object`：递归处理，优先 Pydantic v2 `model_dump()`、回退 v1 `dict()`、最后 `str()`。
- `serialize_channel_values`：序列化 `channel_values` 时**剥离 LangGraph 内部键**（`__pregel_*`、`__interrupt__`），对齐 Platform 线格式，避免把内部实现细节泄露给前端。
- `serialize(obj, mode=...)`：`messages` 模式处理 `(chunk, metadata)` 元组，`values` 模式处理整份状态 dict。

> 没有这层，含 `AIMessage` 等富对象的检查点无法被 `useStream` 前端直接消费。它是"持久化状态 ↔ 对外契约"之间的适配器。

---

## 11. 端到端举例：把所有环节连起来

假设用户在飞书里对 Aegis 连续对话（sqlite 后端）：

1. **首轮**"帮我审计这段合同"到达网关 → `start_run` 生成 `thread_id=T`，`create_or_reject` 登记 run，`_upsert_thread_in_store` 把 `T` 写进 Store。
2. `run_agent` 以 `config.configurable.thread_id=T` 跑图；`ThreadDataMiddleware` 注入 session 标记与路径；模型调用工具产出文件，`artifacts` 经 reducer 累积。**每步自动写 `checkpoints.db`**。
3. 首轮结束，`TitleMiddleware` 生成标题写进 `ThreadState.title`（入检查点），`_sync_thread_title_after_run` 回灌 Store。
4. **次轮**"第三条风险点详细说作答"，复用 `T` → `astream` 自动从 `checkpoints.db` 载入首轮全部消息 → 模型接续记忆作答。
5. 前端打开会话列表 → `POST /threads/search`：Phase 1 从 Store 秒回含标题的 `T`；若曾有旁路创建的 thread，Phase 2 兜底并惰性迁移。
6. 用户点开 `T` 回看 → `GET /{T}/state` 取最新快照、`POST /{T}/history` 回溯检查点链，`serialize_channel_values` 保证前端可渲染。
7. 服务器重启 → 因 sqlite 落盘，`T` 的记忆、标题、列表**全部还在**；若当则是 memory 后端，则全部丢失。

---

## 12. 设计取舍与要点总结

- **三设施分权**：Checkpointer（深度/单 thread 状态）、Store（广度/会话列表）、文件系统（物理产物）各司其职，共用一段配置，保证后端技术一致。
- **thread_id 是主线**：一切恢复/写回都围绕 `config.configurable.thread_id`，业务层几乎不手写"读历史"。
- **reducer 治理体积**：`merge_artifacts` 去重、`merge_viewed_images` 支持清空、`SummarizationMiddleware` 压缩历史，共同抑制检查点膨胀。
- **两阶段搜索 + 惰性迁移**：兼顾列表性能与“旧路 thread”的最终一致，免去一次性迁移。
- **快照式回滚‍**：pre-run snapshot 让"反悔具备事务语义，失败即安全跳过。
- **单向同步**：状态权威在 Checkpointer，派生视图（标题/列表）向 Store 单向回灌，读写职责清晰。
- **序列化唯一真源**：统一的序列化层把内部对象适配为对外契约，并剥离内部键。
- **多入口同后端**：异步 CM / 同步单例 / 一次性 CM 覆盖服务器、Client、脚本三种形态。

> 一言以蔽之：**Aegis 用 LangGraph 的 Checkpointer 承载“会话内记忆”、用 Store 承载“会话间索引”、用沙箱文件系统承载“物理产物”，以 `thread_id` 为主键、以 reducer 与摘要控制体积、以快照回滚保证事务性，构成了一套职责清晰、性能与一致性兼顾的会话状态与持久化体系。**
