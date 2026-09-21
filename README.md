# mini-deerflow

一个对标 **DeerFlow 2.0**（bytedance/deer-flow）的最小复刻，按《DeerFlow 复刻实施路线图》分阶段（P0 → P13）从零构建。

模型调用统一走火山方舟(Volcengine Ark) SDK，参考自 `call_llm.py`（Chat）与 `embedding_model.py`（Embedding）。

## 当前阶段：P7 · MCP 工具集成

| 项 | 内容 |
|---|---|
| 目标产物 | 让 Agent **可配置地连接外部 MCP 服务器**（GitHub / filesystem 等），把它们暴露的工具**动态**折叠进与内置工具同一个注册表——LLM 调用远程 MCP 工具与调用本地 `bash`/`read_file` 完全一致 |
| 对标模块 | DeerFlow 的 `mcp/` 模块（`MultiServerMCPClient` + 每服务器一个 transport 会话） |
| 核心功能点 | 1) **stdio / SSE / HTTP 三种传输**（分别走 stdlib `subprocess` / `urllib`，零新依赖）  2) **工具缓存(mtime 失效)**：`mcp.yaml` 改动自动失效重建  3) **命名空间隔离**：每个工具以 `<server>__<tool>` 暴露，两服务器同名工具不冲突  4) **运行时热重载**（`reload()` / `add_server()`）与每服务器错误隔离（坏服务器降级为零工具，不拖垮健康服务器） |
| 验收标准 | 可配置连接 MCP 服务器、工具动态生效；坏服务器不影响健康服务器；MCP 默认关闭时 P0–P6 行为与其离线测试逐字节不变 |

> 说明：延续“最小可运行、零新依赖”的原则——三种传输直接用 stdlib（`subprocess` 起子进程走 stdio、
> `urllib` POST 走 streamable HTTP / SSE）说 MCP JSON-RPC 2.0 协议（`initialize` →
> `notifications/initialized` → `tools/list` → `tools/call`）。官方 `mcp` SDK /
> `langchain-mcp-adapters` 是“关键技术”的远期后端：抽象已按“换 `McpSdkSession(MCPSession)`
> 不动 client 与工具层”的形态预留。MCP **默认关闭**：无 `mcp.yaml`（且 `config.MCP_ENABLED`
> 缺省 False）时 client 零服务器、贡献零工具，故 P0–P6 行为与其离线测试逐字节不变；Web/CLI 层
> 显式 `MCP_ENABLED=1` 开启后，`mcp.yaml` 里声明的服务器工具方才动态生效——**内置工具名与
> schema 完全不变**，调用点 `get_available_tools()` 保持稳定（新增可选 `include_mcp=`）。

### SSE 事件协议

`POST /chat/stream` 返回 `text/event-stream`，首帧为 `thread_id`（告知客户端本轮所属会话），其后每个帧的 `event:` 名与 agent 事件一一对应，`data:` 为 JSON：

| event | data 字段 | 含义 |
|---|---|---|
| `thread_id` | `{thread_id}` | 本轮对话所属 thread（首帧，P3 新增） |
| `message_chunk` | `{delta}` | 一段增量回答文本 |
| `tool_start` | `{name, arguments}` | 开始调用某工具 |
| `tool_end` | `{name, result}` | 工具返回结果 |
| `max_steps` | `{max_steps}` | 触发步数护栏 |
| `final` | `{content}` | 最终回答文本 |
| `title` / `context_compressed` | 由中间件决定 | 中间件透传的结构化事件（P4，穿插于标准事件之间） |
| `error` | `{message}` | 出错终止 |
| `done` | `{title, todos}` | 流正常结束标志（P4 携带派生标题与任务清单） |

### 运行 Web 服务

```bash
# 启动（任选一）
uvicorn server:app --host 0.0.0.0 --port 8000
python server.py

# 健康检查
curl localhost:8000/health

# 阻塞式问答
curl -X POST localhost:8000/chat \\
     -H 'content-type: application/json' \\
     -d '{"question": "列出当前目录的文件"}'

# 流式（SSE，-N 禁用缓冲，逐 token 输出）
curl -N -X POST localhost:8000/chat/stream \\
     -H 'content-type: application/json' \\
     -d '{"question": "用 bash 执行 echo hi 并告诉我结果"}'

# P3 · 会话续聊：第一次拿到 thread_id，第二次带上它即可续接上下文
curl -X POST localhost:8000/chat \\
     -H 'content-type: application/json' \\
     -d '{"question": "我叫小明"}'
# -> {"content": "...", "thread_id": "<TID>"}
curl -X POST localhost:8000/chat \\
     -H 'content-type: application/json' \\
     -d '{"question": "我叫什么？", "thread_id": "<TID>"}'

# P3 · thread 管理
curl -X POST localhost:8000/threads -H 'content-type: application/json' -d '{"title":"我的会话"}'
curl localhost:8000/threads                 # 列出全部会话（按最近活跃排序）
curl localhost:8000/threads/<TID>           # 取回某会话的完整消息历史
curl -X DELETE localhost:8000/threads/<TID> # 删除会话
```

## 目录结构

```
mini-deerflow/
├── config.py            # 环境变量 / .env 读取，API Key/模型名 + P5 SANDBOX_* + P7 MCP_ENABLED/MCP_CONFIG
├── llm.py               # Ark SDK 封装：chat()(含 tool_calls) / chat_completion() / embed()
├── tools/
│   ├── __init__.py      # get_available_tools() 注册入口（含 P5 sandbox= / P7 include_mcp= 可选参）
│   ├── base.py          # Tool 抽象：callable + JSON schema + 安全 run()
│   ├── builtins.py      # 内置工具（主机直连）：bash / read_file / write_file
│   └── sandbox_tools.py # P5 沙箱版三工具：同名同 schema，绑定到 Sandbox（虚拟路径）
├── sandbox/             # P5 沙箱化执行（对标 DeerFlow sandbox/）
│   ├── __init__.py      # get_sandbox_provider() 进程级单例 + set_sandbox_provider()
│   ├── base.py          # 抽象契约：Sandbox / SandboxProvider / PathMapping / SandboxPathError
│   └── local.py         # LocalSandbox + LocalSandboxProvider（虚拟路径映射 + 防穿越）
├── mcp/                 # P7 MCP 工具集成（对标 DeerFlow mcp/）
│   ├── __init__.py      # get_mcp_client() 进程级单例 + set_mcp_client()（便于测试注入）
│   ├── base.py          # 抽象契约：MCPTransport / MCPServerConfig / MCPToolSpec / MCPSession + 三类错误
│   ├── client.py        # MultiServerMCPClient：多服务器连接 + 工具发现/命名空间 + mtime 缓存 + 热重载
│   └── transports.py    # StdioSession / HttpSession / SseSession + JSON-RPC 2.0 收发（stdlib）
├── agents/
│   ├── __init__.py
│   ├── lead_agent.py    # LeadAgent：多轮 ReAct 循环 + P4 中间件链接入
│   └── middlewares/     # P4 中间件：base(链+上下文) / summarization / title / todo
├── store.py             # P3 会话持久化：ThreadStore（stdlib sqlite3 单表）+ P4 set_title
├── main.py              # CLI 入口（单发 + 交互，含工具活动 trace）
├── server.py             # P3 Web 入口：FastAPI + SSE + thread 端点
│                        #   /health /chat /chat/stream /threads(CRUD)
├── call_llm.py          # 参考文件：Chat 模型调用样例
├── embedding_model.py   # 参考文件：Embedding 模型调用样例
├── requirements.txt
├── mcp.example.yaml      # P7 MCP 服务器配置样例（复制为 mcp.yaml 并置 MCP_ENABLED=1 生效）
├── pytest.ini            # pytest 配置
├── tests/                # P0-P7 测试套件（离线，mock 模型层 / 假 MCP 会话）
└── .env.example
```

## 快速开始

```bash
# 1. 安装依赖（复用已存在的 .agent 虚拟环境）
pip install -r requirements.txt

# 2. 配置密钥
cp .env.example .env
# 编辑 .env，填入 ARK_API_KEY

# 3. 运行
python main.py "在当前目录建一个 hello.txt 写入 hi 再读回来"   # 单发模式
python main.py                                                  # 交互模式（输入 exit 退出）
python main.py --quiet "列出当前目录的文件"                       # 隐藏工具活动 trace

# P5 · 在沙箱内执行工具（虚拟路径 /workspace，防目录穿越）
SANDBOX_ENABLED=1 python main.py "在 /workspace 建一个 hello.txt 写入 hi 再读回来"

# P7 · 连接外部 MCP 服务器（先 cp mcp.example.yaml mcp.yaml 并按需编辑）
MCP_ENABLED=1 python main.py "用 filesystem 的工具读一下 README 的开头"
```

> P5 沙箱默认关闭；置 `SANDBOX_ENABLED=1`（或 `true`/`yes`/`on`）后，`bash`/`read_file`/`write_file`
> 三工具改为在沙箱内执行，只认虚拟路径 `/workspace/...`，真实文件落在 `SANDBOX_DIR`（缺省
> 为源码树旁的 `sandboxes/`）下。Web 服务同样支持 `SANDBOX_ENABLED=1 uvicorn server:app ...`。
>
> P7 MCP 默认关闭；置 `MCP_ENABLED=1` 并提供 `mcp.yaml`（或用 `MCP_CONFIG` 指向别处）后，声明的
> MCP 服务器工具以 `<server>__<tool>` 追加进工具集，与沙箱开关**可组合**。两开关皆关时走 P1 主机内置
> 工具，P0–P6 行为逐字节不变。

运行时默认在 stderr 打印工具调用轨迹，便于观察 ReAct 过程：

```
  · calling write_file({"path": "hello.txt", "content": "hi"})
    -> [write_file] wrote 2 chars to hello.txt (mode=overwrite)
  · calling read_file({"path": "hello.txt"})
    -> hi
```

## 工具调用工作机制（P1）

1. `tools.get_available_tools()` 返回内置 `Tool` 列表，每个工具带 JSON schema。
2. `LeadAgent.run()` 把工具 schema 连同对话发给 LLM（`llm.chat(..., tools=...)`）。
3. 若模型返回 `tool_calls`，则本地执行对应工具，把结果作为 `tool` 消息回灌。
4. 循环“推理 → 调用 → 观察”，直到模型给出不含工具调用的最终回答；
   `max_steps`（默认 10）作为防死循环护栏。

## 会话持久化工作机制（P3）

1. **存储层 `store.ThreadStore`**：用标准库 `sqlite3` 建一张 `threads` 表
   （`id / title / created_at / updated_at / messages`）。每个 thread 的完整消息
   列表以一条 JSON blob 存于 `messages` 列——thread 体量小，单 blob 让 schema 极简，
   且 `tool_calls`、`tool` 结果都能无损往返。
2. **Agent 续聊**：`LeadAgent.run()` / `run_stream()` 新增可选 `history=` 入参。
   传入某 thread 的历史消息后，会话从历史续接（而非每次都从 `[system, user]` 重开）；
   跑完后完整对话留在 `self.messages`，供 Web 层回存。
3. **Web 层收口**：`/chat`、`/chat/stream` 接受可选 `thread_id`——
   - 命中已有 thread → 载入历史喂给 agent；
   - 缺省 / 未知 id → 视为一个新 thread。
   一轮跑完（非报错）后，把 `agent.messages` 整体 `save_messages()` 回存，并把
   `thread_id` 回传给客户端（`/chat` 在响应体、`/chat/stream` 作为 SSE 首帧）。
4. **thread 管理**：`POST /threads` 建、`GET /threads` 列（按最近活跃排序，不带消息体）、
   `GET /threads/{id}` 取（带完整历史）、`DELETE /threads/{id}` 删。
5. **进程级默认 store**：`get_store()` 懒加载一个文件级默认实例（路径由
   `config.THREAD_DB_PATH` 决定，可用 `:memory:`）；`set_store()` 便于测试注入。

> 与 DeerFlow 的对齐点：thread 即会话单元，历史可载入续聊、可列出、可删除——
> 这是后续 P4 中间件链、P12 长任务续跑的基础。差别在于我们不引入 checkpointer /
> 分支（branch）等重型能力，只落最小可用的“存 + 读 + 续”。

## 中间件链工作机制（P4）

1. **责任链 `MiddlewareChain`**：按注册顺序持有一组 `Middleware`。`before_*` 钩子正序执行、`after_*` 逆序执行（像上下文管理器一样嵌套）；任一钩子抛错都会被**隔离**并记到 `ctx.scratch["_errors"]`，绝不打断其余中间件或主循环。
2. **四个生命周期钩子**：`before_agent`（每轮一次，播种会话后，可注入工具 / 改写系统提示——之后重新快照工具 schema，使 `write_todos` 之类注入工具可见）、`before_model`（每次模型调用前，可改写待发送消息，如摘要压缩）、`after_model`（每次回复后）、`after_agent`（每轮一次，收尾派生标题 / 任务清单）。
3. **每轮现造新链**：`LeadAgent(middleware_factory=...)` 每次 run 用工厂造一条**全新**链，使 TodoList/Title 的每轮状态不跨会话串味；默认 `middleware_factory=None` → 空链，P0–P3 行为不变。
4. **内置三件套**（`default_middlewares()`，顺序 Title → Summarization → TodoList）：
   - `SummarizationMiddleware`：以 `~len/4` 估算 token，超 `max_tokens` 时保留头部系统提示 + 末尾 `keep_last` 条，中间折叠成一条 `[conversation-summary]` 系统消息；切点自动前移，绝不把 `tool` 结果与其配对的 `assistant tool_calls` 拆散。
   - `TitleMiddleware`：首轮 `after_agent` 用一次 `llm.chat_completion` 生成 ≤8 词标题，失败则回退为首条用户消息前缀；已有历史（续聊）则跳过。
   - `TodoListMiddleware`：注入 `write_todos` 工具，`before_model` 把当前计划以 `[todo-list]` 临时系统消息挂到模型面前（每轮先删再挂、不堆叠），`after_agent` 剥离该提醒并把计划落到 `ctx.todos`。
5. **事件透传**：中间件 `ctx.emit(...)` 的结构化事件——阻塞路径转发给 `on_event`，流式路径作为 SSE 帧穿插在标准事件间；派生的 `title` / `todos` 落到 `agent.title` / `agent.todos`，Web 层据此回存（`store.set_title` 覆盖派生标题）并在 `/chat` 响应体、`/chat/stream` 的 `done` 帧回传。


## 沙箱化执行工作机制（P5）

1. **两层契约（`sandbox/base.py`）**：`Sandbox` 是一个只认虚拟路径的执行环境（`execute_command` / `read_file` / `write_file` / `list_dir`）；`SandboxProvider` 是它的工厂 + 生命周期管理器（`acquire(thread_id) -> id`、`get(id)`、`release(id)`、`reset()`）。应用代码只面向该接口，日后换 `DockerSandboxProvider` 不动任何调用点。
2. **虚拟路径映射（`PathMapping`）**：每个沙箱持有一组 `virtual_path -> host_path` 绑定。`LocalSandboxProvider` 默认把 `/workspace` 映射到 `<SANDBOX_DIR>/<sandbox_id>/workspace`——按 `thread_id` 隔离，故 P3 的并发会话各有独立工作区；`acquire(None)` 返回共享的 `local` 沙箱（供 CLI / 测试）。
3. **防目录穿越**：每次路径解析都先在**原始输入**上拦 NUL 字节，再把相对部分拼到主机根、`os.path.realpath` 收敛 `..` 与软链，最后用 `os.path.commonpath` 校验仍落在根内——`../../etc/passwd`、绝对路径 `/etc/passwd`、指向外部的软链都在**任何 I/O 之前**抛 `SandboxPathError`。只读映射的写入抛 `OSError(EROFS)`。
4. **命令内路径改写 + 输出反向映射**：`execute_command` 以主 `/workspace` 的主机目录为 CWD 运行，先用一段“段边界”正则把命令串里的虚拟路径改写成主机路径（故 `cat /workspace/a.txt` 可用），再把 stdout/stderr 与目录列举里的主机前缀反向映射回虚拟根——主机路径绝不泄露给模型。
5. **同名工具、稳定入口**：`tools.get_available_tools(sandbox=...)` 传入沙箱时返回由 `make_sandbox_tools` 现造、闭包绑定该沙箱的 `bash`/`read_file`/`write_file`——**工具名与 JSON schema 与主机版逐字段一致**，仅描述文案从主机路径改为虚拟路径。被拦截的穿越降级为纯文本（`[read_file] blocked: ...`）喂回 ReAct 循环。
6. **默认关闭、显式开启**：`config.SANDBOX_ENABLED`（缺省 False）决定 Web/CLI 是否启用沙箱。`server.py._build_agent` 按 `thread_id` 取沙箱、`main.py` 用通用 `local` 沙箱；关闭时走 P1 主机内置工具，P0–P4 行为逐字节不变。`get_sandbox_provider()` 懒加载进程级单例（根目录由 `config.SANDBOX_DIR` 决定），`set_sandbox_provider()` 便于测试注入 `tmp_path` 版。

> 与 DeerFlow 的对齐点：稳定的 `get_available_tools()` 工具面 + 可替换的沙箱后端，
> 是后续 P7 MCP 工具、P8 技能系统、P13 Docker `AioSandbox` 的地基。差别在于本阶段
> 只落零依赖的本地后端，不引入容器 / 网络隔离等重型能力。

## 多模型工厂工作机制（P6）

1. **抽象基类（`models/base.py`）**：`BaseChatModel` / `BaseEmbeddingModel` 定义与实现无关的调用面（`chat` / `stream_chat` / `embed`）+ `ModelCapabilities`（是否支持工具、流式、思考、embedding）。
2. **反射装配（`models/reflection.py`）**：`resolve_class` 按“规范名 / 点分全路径”把配置里的字符串解析成具体 provider 类并缓存——`models.yaml` 改 provider 名即可换实现，无需改代码。
3. **工厂（`models/factory.py`）**：`ModelFactory` 解析 `models.yaml`（PyYAML，缺省用 `_tiny_yaml_load` 微型回退），按别名懒加载模型实例、解析默认模型、透出能力自省；无 `models.yaml` 时回退内置 Ark 配置。
4. **收口 `llm.py`**：`chat / stream_chat / embed` 全部改走工厂——**改配置即可切换模型**，调用点不动。`get_factory()` 进程级单例、`set_factory()` 便于测试注入。

## MCP 工具集成工作机制（P7）

1. **两层契约（`mcp/base.py`）**：`MCPSession` 是“到单个 MCP 服务器的一条活连接”，生命周期镜像 MCP 协议——`initialize()`（握手，幂等）→ `list_tools()`（发现工具）→ `call_tool()`（按原始名调用）→ `close()`（拆传输）；`MCPServerConfig`（冻结 dataclass）声明一台服务器的连接（`transport` + stdio 的 `command`/`args`/`env` 或 sse/http 的 `url`/`headers`），`validate()` 按 transport 校验必填、`namespace()` 给工具名加 `<server>__` 前缀；`MCPToolSpec` 是发现到的工具元数据（name / description / JSON schema）。`MCPError` / `MCPTransportError` / `MCPToolError` 三类错误让工具层能区分“连不上”与“工具跑了但报错”。
2. **三种传输（`mcp/transports.py`，零新依赖）**：`_JsonRpcSession` owns JSON-RPC 2.0 记账（单调 id、`initialize`→`notifications/initialized` 握手、`tools/list`/`tools/call` 塑形、`content` 块拍平、`isError`→`MCPToolError`），子类只实现“字节怎么走”——`StdioSession` 用 `subprocess.Popen` 行缓冲收发、`HttpSession` 用 `urllib` POST（`text/event-stream` 时按 SSE 解析）、`SseSession` 是 `_force_sse=True` 的 HTTP 特化。`create_session` 按 `transport` 路由。工具输出统一 `_truncate` 到 20k 字符，防止话痨工具撑爆上下文。
3. **多服务器 client（`mcp/client.py`）**：`MultiServerMCPClient` 持有一组配置，**懒连接**（首次发现工具时才起会话并复用）、**每服务器错误隔离**（坏服务器记进 `self.errors` 并跳过，不拖垮健康服务器）、**命名空间隔离**（`<server>__<tool>`）。发现到的工具渲染成 `tools.base.Tool`，其 `func` 闭包持有活会话与原始工具名、错误降级为 `[mcp-error] ...` 文本喂回 ReAct 循环。`${VAR}` 环境变量在解析时展开（密钥不落配置文件）。
4. **工具缓存(mtime 失效) + 运行时热重载**：`get_tools()` 缓存结果，`mcp.yaml` 的 mtime 变化时透明重建；`reload()`（改文件后手动热重载）/`add_server()`（运行时注册新服务器）/`get_tools(force=True)` 都能强制失效。
5. **稳定入口、默认关闭**：`tools.get_available_tools(include_mcp=True)` 把 MCP 工具追加进池，与 `sandbox=` **可组合**；`config.MCP_ENABLED`（缺省 False）决定 Web/CLI 是否开启，`server.py`/`main.py` 的 `_build_agent` 据此拼装。`get_mcp_client()` 懒加载进程级单例（配置路径由 `config.MCP_CONFIG` 或源码树旁的 `mcp.yaml` 决定），`set_mcp_client()` 便于测试注入假会话工厂——故全套测试离线、绝不起子进程 / 开 socket。无 `mcp.yaml`（且 `MCP_ENABLED` 关）时 client 零服务器、贡献零工具，P0–P6 逐字节不变。

> 与 DeerFlow 的对齐点：`MultiServerMCPClient` + 每服务器 transport 会话 + 命名空间化工具面，
> 让远程 MCP 工具与本地工具在同一注册表里被 LLM 平等调用。差别在于本阶段只落零依赖的 stdlib
> 传输，官方 `mcp` SDK / `langchain-mcp-adapters` 作为可平滑替换的远期后端预留在 `MCPSession` 之后。

## 设计说明

- **密钥不落地**：统一从环境变量 / `.env` 读取（`config.py`），源码不含密钥；MCP 配置里的 `${VAR}` 亦在加载时从环境展开。
- **单点收口模型调用**：所有对 Ark 的调用集中在 `llm.py`，P6「多模型工厂」已把它改走工厂。
- **工具可扩展**：新增工具只需在 `tools/builtins.py` 定义并加入 `BUILTIN_TOOLS`；
  调用入口 `get_available_tools()` 保持稳定。P5 已生长出沙箱版工具（`sandbox=`），
  P7 已折入 MCP 远程工具（`include_mcp=`），P8 技能续接。
- **沙箱 / MCP 均可替换**：P5 面向 `SandboxProvider` 抽象、P7 面向 `MCPSession` 抽象，
  本地 / stdlib 后端之外可平滑替换为 `DockerSandboxProvider` / `McpSdkSession` 而不动调用点。
- **纯增量、可对齐 DeerFlow**：`agents/lead_agent.py` 保留类形态，后续 P9（子智能体）可直接扩展。

## 测试

P0–P7 全量单测 + SSE 集成测试，**完全离线**：mock 掉 `llm` 层与 Ark 客户端、MCP 用假会话工厂注入（绝不起子进程 / 开 socket），不会发起任何网络 / 模型调用，也无需 `ARK_API_KEY`。P5 的沙箱测试全部落在 pytest `tmp_path`，不在临时目录外产生任何文件。

```bash
pip install -r requirements.txt   # 含 pytest
python -m pytest                   # 跑全部
python -m pytest tests/test_p7_mcp.py -v   # 单文件
```

| 测试文件 | 阶段 | 覆盖 | 用例数 |
|---|---|---|---|
| `tests/test_p0_config.py` | P0 | `.env` 加载优先级、`require_api_key` 报错、模型默认值 | 6 |
| `tests/test_p0_llm.py` | P0 | `chat/chat_completion/stream_chat/embed` 委派 mock 客户端、参数透传 | 8 |
| `tests/test_p1_tools.py` | P1 | `Tool.run` 参数解析/容错、schema、注册表、bash/read_file/write_file | 26 |
| `tests/test_p1_agent.py` | P1 | 阻塞 ReAct 循环：直接回答、工具回合、未知工具、max_steps 护栏 | 7 |
| `tests/test_p2_agent_stream.py` | P2 | `run_stream` 事件序列、tool_call 分片重组、异常转 error | 9 |
| `tests/test_p2_server.py` | P2 | `/health` `/chat` `/chat/stream` SSE 帧、事件名、done、参数校验 | 8 |
| `tests/test_p3_store.py` | P3 | `ThreadStore` 增删查列、标题派生、`tool_calls` 无损往返、默认 store 访问器 | 10 |
| `tests/test_p3_server.py` | P3 | `/threads` CRUD、`/chat` 续聊与回存、SSE 首帧 `thread_id`、出错不落库 | 9 |
| `tests/test_p4_middlewares.py` | P4 | 链按序执行/`after_*`逆序、抛错隔离、生命周期钩子计数与事件透传、Title/TodoList/Summarization 行为、`default_middlewares` 顺序 | 15 |
| `tests/test_p5_sandbox.py` | P5 | Provider 生命周期(acquire/get/release/reset)与按 thread 隔离、虚拟路径映射与落盘、防穿越(`../`/绝对/软链/NUL)、只读映射 `EROFS`、`get_available_tools(sandbox=)` 路由与同 schema、工具错误文案 | 25 |
| `tests/test_p6_models.py` | P6 | 反射装配、`ModelFactory` 配置解析/懒加载/别名/默认/能力自省/Ark 回退、Ark 适配器参数装配与流式、`llm` 收口改配置切换 | (见文件) |
| `tests/test_p7_mcp.py` | P7 | transport 枚举 `coerce`、配置校验/命名空间、`parse_servers`/env 展开、多服务器发现/命名空间/错误隔离、mtime 缓存/`reload`/`add_server`、`[mcp-error]` 降级、JSON-RPC 握手/`tools/list`/`tools/call`/`isError`、SSE 抽取、`create_session` 路由、单例、`get_available_tools(include_mcp=)` 合并 | (见文件) |

服务层测试用 FastAPI `TestClient`（进程内 ASGI），不绑定端口；`:memory:` 版 `ThreadStore`、
空 MCP client 均由 autouse fixture 逐测重置，全程离线、不落磁盘、不起子进程。

## 路线图（后续阶段）

~~P3 会话持久化~~ → ~~P4 中间件链~~ → ~~P5 沙箱~~ → ~~P6 多模型工厂~~ → ~~P7 MCP~~（本阶段完成）→ P8 技能系统 → P9 子智能体 → P10 架构分层 → P11 IM 渠道 → P12 定时/长任务 → P13 生产加固。
