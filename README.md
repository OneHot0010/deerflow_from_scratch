# mini-deerflow

一个对标 **DeerFlow 2.0**（bytedance/deer-flow）的最小复刻，按《DeerFlow 复刻实施路线图》分阶段（P0 → P13）从零构建。

模型调用统一走火山方舟(Volcengine Ark) SDK，参考自 `call_llm.py`（Chat）与 `embedding_model.py`（Embedding）。

## 当前阶段：P2 · Web/SSE

| 项 | 内容 |
|---|---|
| 目标产物 | 把工具调用 Agent 通过 HTTP 暴露，支持 token 级 SSE 流式输出 |
| 对标模块 | DeerFlow `POST /stream` + `text/event-stream`（gateway runs 雏形） |
| 核心功能点 | 1) `llm.stream_chat()` 流式调用  2) `LeadAgent.run_stream()` 流式 ReAct 循环  3) FastAPI 三个端点  4) SSE 事件协议 |
| 验收标准 | `curl -N` 请求 `/chat/stream` 可逐 token 看到回答，并实时看到 tool_start / tool_end 事件 |

> 说明：为保持每阶段“最小可运行”，本阶段的 Web 层是无状态的（不落会话）；
> 会话持久化 / thread 存储留到 P3。

### SSE 事件协议

`POST /chat/stream` 返回 `text/event-stream`，每个帧的 `event:` 名与 agent 事件一一对应，`data:` 为 JSON：

| event | data 字段 | 含义 |
|---|---|---|
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
├── main.py              # CLI 入口（单发 + 交互，含工具活动 trace）
├── server.py             # P2 Web 入口：FastAPI + SSE（/health /chat /chat/stream）
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

共 **64** 个用例。服务层测试用 FastAPI `TestClient`（进程内 ASGI），不绑定端口。

## 路线图（后续阶段）

P3 会话持久化 → P4 中间件链 → P5 沙箱 → P6 多模型工厂 → P7 MCP → P8 技能系统 → P9 子智能体 → P10 架构分层 → P11 IM 渠道 → P12 定时/长任务 → P13 生产加固。
