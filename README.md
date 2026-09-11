# mini-deerflow

一个对标 **DeerFlow 2.0**（bytedance/deer-flow）的最小复刻，按《DeerFlow 复刻实施路线图》分阶段（P0 → P13）从零构建。

模型调用统一走火山方舟(Volcengine Ark) SDK，参考自 `call_llm.py`（Chat）与 `embedding_model.py`（Embedding）。

## 当前阶段：P3 · 会话持久化

| 项 | 内容 |
|---|---|
| 目标产物 | 给无状态 Web 层加上 **thread（会话）** 存储：对话可保存、可列出、可续聊 |
| 对标模块 | DeerFlow 的 thread 模型 + checkpointer（`/api/threads` CRUD + 断点续聊） |
| 核心功能点 | 1) `store.ThreadStore` —— stdlib `sqlite3` 单表持久化  2) `LeadAgent.run/run_stream` 支持 `history=` 续聊，并把完整对话留在 `self.messages`  3) `/chat`·`/chat/stream` 接受可选 `thread_id`，跑完自动回存  4) `/threads` 增删查列四个端点 |
| 验收标准 | 带同一 `thread_id` 连续两次 `/chat`，第二次能读到第一次的上下文；`GET /threads/{id}` 能拿回完整消息历史；进程重启后 thread 仍在 |

> 说明：延续每阶段“最小可运行、零新依赖”的原则，持久化只用标准库 `sqlite3`
> （不引入 SQLAlchemy / LangGraph checkpointer）。每个 thread 的消息历史以一条
> JSON blob 存一行，schema 极简且 `tool_calls` 无损往返。

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
| `error` | `{message}` | 出错终止 |
| `done` | `{}` | 流正常结束标志 |

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
│   └── lead_agent.py    # LeadAgent：多轮 ReAct 工具调用循环
├── store.py             # P3 会话持久化：ThreadStore（stdlib sqlite3 单表）
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

## 设计说明

- **密钥不落地**：统一从环境变量 / `.env` 读取（`config.py`），源码不含密钥。
- **单点收口模型调用**：所有对 Ark 的调用集中在 `llm.py`，P6「多模型工厂」只需改这一处。
- **工具可扩展**：新增工具只需在 `tools/builtins.py` 定义并加入 `BUILTIN_TOOLS`；
  调用入口 `get_available_tools()` 保持稳定，P5 沙箱 / P7 MCP / P8 技能均在此生长。
- **纯增量、可对齐 DeerFlow**：`agents/lead_agent.py` 保留类形态，后续 P9（子智能体）可直接扩展。

## 测试

P0–P2 全量单测 + SSE 集成测试，**完全离线**：mock 掉 `llm` 层与 Ark 客户端，不会发起任何网络 / 模型调用，也无需 `ARK_API_KEY`。

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

共 **83** 个用例。服务层测试用 FastAPI `TestClient`（进程内 ASGI），不绑定端口；
P3 测试通过 autouse fixture 注入 `:memory:` 版 `ThreadStore`，全程离线、不落磁盘。

## 路线图（后续阶段）

~~P3 会话持久化~~（本阶段完成）→ P4 中间件链 → P5 沙箱 → P6 多模型工厂 → P7 MCP → P8 技能系统 → P9 子智能体 → P10 架构分层 → P11 IM 渠道 → P12 定时/长任务 → P13 生产加固。
