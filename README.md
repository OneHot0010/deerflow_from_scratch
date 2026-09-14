# mini-deerflow

一个对标 **DeerFlow 2.0**（bytedance/deer-flow）的最小复刻，按《DeerFlow 复刻实施路线图》分阶段（P0 → P13）从零构建。

模型调用统一走火山方舟(Volcengine Ark) SDK，参考自 `call_llm.py`（Chat）与 `embedding_model.py`（Embedding）。

## 当前阶段：P4 · 中间件链

| 项 | 内容 |
|---|---|
| 目标产物 | 给每次 run 套上一条 **中间件链（MiddlewareChain）**：在固定生命周期钩子上插入可插拔行为 |
| 对标模块 | DeerFlow 的 middleware 体系（Summarization / Title / TodoList 等 AgentMiddleware） |
| 核心功能点 | 1) 中间件注册机制 + 责任链按序执行  2) `before_agent/before_model/after_model/after_agent` 四个生命周期钩子  3) `SummarizationMiddleware` 超长上下文自动压缩  4) `TitleMiddleware` 标题自动生成 + `TodoListMiddleware` 任务跟踪 |
| 验收标准 | 中间件按注册序执行（`after_*` 逆序）、单个钩子抛错被隔离不影响其余；上下文超预算时自动压缩且不孤立 `tool` 结果；首轮对话后自动派生 thread 标题 |

> 说明：延续“最小可运行、零新依赖”的原则。链默认为空（无 factory 时），
> 故 P0–P3 行为与其离线测试逐字节不变；每次 run 由 `middleware_factory` 现造一条
> 新链，让 TodoList/Title 等持有的“每轮状态”不跨会话串味。

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
├── config.py            # 环境变量 / .env 读取，API Key 与模型名集中管理
├── llm.py               # Ark SDK 封装：chat()(含 tool_calls) / chat_completion() / embed()
├── tools/
│   ├── __init__.py      # get_available_tools() 注册入口（对标 DeerFlow tools/）
│   ├── base.py          # Tool 抽象：callable + JSON schema + 安全 run()
│   └── builtins.py      # 内置工具：bash / read_file / write_file
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
├── pytest.ini            # pytest 配置
├── tests/                # P0-P2 测试套件（离线，mock 模型层）
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
```

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


## 设计说明

- **密钥不落地**：统一从环境变量 / `.env` 读取（`config.py`），源码不含密钥。
- **单点收口模型调用**：所有对 Ark 的调用集中在 `llm.py`，P6「多模型工厂」只需改这一处。
- **工具可扩展**：新增工具只需在 `tools/builtins.py` 定义并加入 `BUILTIN_TOOLS`；
  调用入口 `get_available_tools()` 保持稳定，P5 沙箱 / P7 MCP / P8 技能均在此生长。
- **纯增量、可对齐 DeerFlow**：`agents/lead_agent.py` 保留类形态，后续 P9（子智能体）可直接扩展。

## 测试

P0–P4 全量单测 + SSE 集成测试，**完全离线**：mock 掉 `llm` 层与 Ark 客户端，不会发起任何网络 / 模型调用，也无需 `ARK_API_KEY`。

```bash
pip install -r requirements.txt   # 含 pytest
python -m pytest                   # 跑全部
python -m pytest tests/test_p2_server.py -v   # 单文件
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

共 **98** 个用例。服务层测试用 FastAPI `TestClient`（进程内 ASGI），不绑定端口；
P3 测试通过 autouse fixture 注入 `:memory:` 版 `ThreadStore`，全程离线、不落磁盘。

## 路线图（后续阶段）

~~P3 会话持久化~~ → ~~P4 中间件链~~（本阶段完成）→ P5 沙箱 → P6 多模型工厂 → P7 MCP → P8 技能系统 → P9 子智能体 → P10 架构分层 → P11 IM 渠道 → P12 定时/长任务 → P13 生产加固。
