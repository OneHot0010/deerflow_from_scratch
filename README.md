# mini-deerflow

一个对标 **DeerFlow 2.0**（bytedance/deer-flow）的最小复刻，按《DeerFlow 复刻实施路线图》分阶段（P0 → P13）从零构建。

模型调用统一走火山方舟(Volcengine Ark) SDK，参考自 `call_llm.py`（Chat）与 `embedding_model.py`（Embedding）。

## 当前阶段：P0 · 最小可运行 Agent

| 项 | 内容 |
|---|---|
| 目标产物 | 命令行单轮对话：输入问题 → LLM 回答 → 打印结果 |
| 对标模块 | DeerFlow `agents/lead_agent`（最简形态） |
| 核心功能点 | 1) 接入单个 LLM  2) 单轮 prompt→completion  3) 打印结果 |
| 验收标准 | 能在终端与 LLM 对话，单轮问答正常返回 |

## 目录结构

```
mini-deerflow/
├── config.py            # 环境变量 / .env 读取，API Key 与模型名集中管理
├── llm.py               # Ark SDK 封装：chat_completion() + embed()
├── agents/
│   ├── __init__.py
│   └── lead_agent.py    # LeadAgent：单轮问答（对标 agents/lead_agent 最简形态）
├── main.py              # CLI 入口（单发 + 交互两种模式）
├── call_llm.py          # 参考文件：Chat 模型调用样例
├── embedding_model.py   # 参考文件：Embedding 模型调用样例
├── requirements.txt
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
python main.py "介绍一下你自己"     # 单发模式
python main.py                       # 交互模式（输入 exit 退出）
```

## 设计说明

- **密钥不落地**：参考脚本把 `ARK_API_KEY` 硬编码在源码里；本项目统一从环境变量 / `.env` 读取（`config.py`），源码不含密钥。
- **单点收口模型调用**：所有对 Ark 的调用集中在 `llm.py`，未来 P6「多模型工厂」只需改这一处。
- **纯增量、可对齐 DeerFlow**：`agents/lead_agent.py` 保留类形态，后续 P1（工具调用）、P9（子智能体）可直接在此扩展，无需改动调用方。

## 路线图（后续阶段）

P1 工具调用 → P2 Web/SSE → P3 会话持久化 → P4 中间件链 → P5 沙箱 → P6 多模型工厂 → P7 MCP → P8 技能系统 → P9 子智能体 → P10 架构分层 → P11 IM 渠道 → P12 定时/长任务 → P13 生产加固。
