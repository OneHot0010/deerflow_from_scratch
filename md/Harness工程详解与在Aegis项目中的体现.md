# Harness 工程详解 —— 以 Aegis 审核 Agent 项目为例

> 本文从 `tob_audit_agent-master`(内部代号 **Aegis**)项目出发,系统解释**什么是 Harness 工程**。每个要点都给出通用说明 + 举例,并逐一对照它在 Aegis 中的具体代码体现。
>
> 关键线索:该项目把 Agent 内核直接命名为一个独立 Python 包 —— `backend/packages/harness/aegis/`(包名 `aegis-harness`)。也就是说,「Harness」不是一个抽象比喻,而是这个项目**真实存在、可独立发布**的工程实体。

---

## 目录

1. 什么是 Harness / Harness 工程(总览)
2. 要点一:内核与应用分层(Import Firewall)
3. 要点二:Agent 装配工厂(Factory / 声明式组装)
4. 要点三:中间件链(Middleware Chain)——Harness 的骨架
5. 要点四:工具接入层(Tool Harness)
6. 要点五:执行隔离与运行时(Sandbox Runtime)
7. 要点六:状态与记忆管理(State / Memory)
8. 要点七:健壮性与容错(Robustness)
9. 要点八:可扩展性(Skills / MCP)
10. 要点九:多入口统一(Runtime Surfaces)
11. 要点十:可测试性与版本纪律(Engineering Discipline)
12. 总结:Aegis 为什么是一个「Harness 工程」的范本

---

## 1. 什么是 Harness / Harness 工程(总览)

### 概念

**Harness(马具 / 挽具)** 原意是「把一匹烈马套进可控框架、让它按缰绳做功」的装置。在 AI Agent 语境下,**Harness 指包裹在大模型(LLM)外面的一整套工程脚手架**:它本身不产生智能,但决定了这份智能能否被**安全、稳定、可复用、可观测**地驱动起来。

一个裸 LLM 只能「输入文本→输出文本」。要把它变成能查资料、跑命令、管理多轮任务、失败自恢复的 **Agent**,你必须在它外面补齐:

- **输入侧**:System Prompt 拼装、上下文裁剪、工具 schema 注入;
- **输出侧**:工具调用解析、结果回填、异常兜底;
- **循环控制**:多轮 loop、终止条件、循环检测;
- **副作用治理**:命令在哪执行、能不能删库、租户隔离;
- **状态**:跨轮记忆、检查点、压缩;
- **可靠性**:重试、熔断、降级。

**这一整套「围绕模型的工程」就是 Harness 工程。** 它的核心命题是:*模型会变、会抖动、会犯错,但工程框架必须稳定、可预测、可维护。*

### 一句话对比

| | 负责什么 | 例子 |
|---|---|---|
| **Model(模型)** | 推理、生成 | 「我应该调用 bash 执行 `ls`」 |
| **Harness(挽具)** | 让这个决策被安全、可靠地执行 | 拦截危险命令、放进沙箱跑、失败转成错误消息、防止无限循环 |

### 在 Aegis 中的直接体现

项目把 Harness 做成了**独立包**,与应用层物理隔离:

```
backend/
├── packages/harness/aegis/   ← Harness 内核(包名 aegis-harness,import: aegis.*)
│   ├── agents/               ← 装配工厂 + 中间件链 + 状态
│   ├── sandbox/              ← 执行隔离
│   ├── tools/                ← 工具接入
│   ├── skills/  mcp/  models/  jobs/  reflection/  config/
│   └── client.py             ← 内嵌客户端
└── app/                      ← 应用层(import: app.*)—— Harness 的"消费者"
    ├── gateway/              ← FastAPI 网关
    └── channels/feishu/      ← 飞书渠道
```

`backend/packages/harness/pyproject.toml` 里明确声明:

```toml
[project]
name = "aegis-harness"
description = "Aegis agent harness framework"
```

> **结论:在 Aegis 里,"Harness 工程"= `aegis.*` 这个可独立发布的 Agent 框架内核。下面每个要点,都是这个内核里的一个工程支柱。**

---

## 2. 要点一:内核与应用分层(Import Firewall)

### 通用说明

Harness 工程的第一原则是**依赖方向单一**:内核(可复用的 Agent 引擎)不能反向依赖具体应用(网关、渠道、UI)。否则内核就被某个业务绑死,无法复用,也无法独立测试。

### 举例

- **反例**:一个「通用 Agent 引擎」里 `import` 了 `feishu_client`。结果这个引擎永远离不开飞书,想接钉钉/Web 就得改内核。
- **正例**:引擎只暴露抽象接口;飞书、Web、CLI 各自作为「消费者」去调用引擎。引擎完全不知道调用方是谁。

### 在 Aegis 中的体现

Aegis 把这条原则**用 CI 测试强制**,叫 **Import Firewall(导入防火墙)**:

- 规则(`AGENTS.md` Hard Rule #1):`aegis.*` **绝不允许** `import app.*`;反向允许。
- 强制手段:`backend/tests/test_harness_boundary.py` 用 `ast` 静态扫描 harness 里每个 `.py`,一旦发现 `from app.` / `import app.` 就让 CI 构建失败:

```python
BANNED_PREFIXES = ("app.",)
def test_harness_does_not_import_app():
    violations = []
    for py_file in sorted(HARNESS_ROOT.rglob("*.py")):
        for lineno, module in _collect_imports(py_file):
            if any(module.startswith(p) for p in BANNED_PREFIXES):
                violations.append(...)
    assert not violations, "Harness layer must not import from app layer"
```

允许的方向示例:

```python
from aegis.agents import make_lead_agent      # harness 内部 ✅
from aegis.config import get_app_config        # app → harness ✅(允许)
# from app.gateway.routers.uploads import ...  # harness → app ❌(CI 直接 fail)
```

> **体现:Harness 是「可独立复用的纯 Agent 内核」,应用层(Gateway / Feishu)只是它的消费者。这条边界不是靠自觉,而是靠自动化测试焊死。**

---

## 3. 要点二:Agent 装配工厂(Factory / 声明式组装)

### 通用说明

Harness 需要一个**统一的装配入口**,把「模型 + 工具 + 中间件 + 状态 + 检查点」组装成一个可执行的 Agent。好的工厂应该:声明式(用配置/特性开关驱动)、可校验(非法组合直接报错)、有确定顺序(装配结果可预测)。

### 举例

- 你想要一个「带视觉、带任务规划、不带自动标题」的 Agent,不应该去改内核代码,而应该通过**特性开关**(feature flags)声明:`vision=True, plan_mode=True, auto_title=False`,工厂据此自动拼出正确的链。

### 在 Aegis 中的体现

`aegis/agents/factory.py` 的 `create_aegis_agent(...)` 就是这个「介于 `langchain.create_agent` 原语与业务 `make_lead_agent` 之间」的 SDK 级工厂:

- **纯参数、无 YAML、无全局单例** —— 只接收 `model / tools / features / middleware ...`。
- **非法组合直接 `ValueError`**,例如:

```python
if middleware is not None and features is not None:
    raise ValueError("Cannot specify both 'middleware' and 'features'.")
```

- **声明式特性驱动**:`RuntimeFeatures`(sandbox / guardrail / summarization / vision / auto_title 等开关)交给 `_assemble_from_features()`,由它按**固定顺序**产出中间件链;每个特性支持三态:`False`(跳过)/ `True`(用内置默认)/ 传入自定义 `AgentMiddleware` 实例(替换)。
- **工具去重**:特性注入的工具(如 vision 的 `view_image`)自动追加,且「用户提供的工具优先」。

> **体现:Aegis 用一个声明式工厂把 Agent 的装配收敛到一处,组合关系可校验、装配顺序可预测——这是 Harness"可复用"的前提。**

---

## 4. 要点三:中间件链(Middleware Chain)——Harness 的骨架

### 通用说明

这是 Harness 工程最核心的机制:把「模型调用」和「工具调用」用一条**严格排序的中间件链**层层包裹。每个中间件只管一件横切关注点(cross-cutting concern),彼此解耦,顺序决定行为。

**为什么顺序至关重要?** 例如「上下文压缩」必须在「循环检测」之前,否则被压缩掉的历史会让循环检测失去判断依据;「澄清中断」必须在最后,否则它 `goto=END` 会跳过后面所有中间件。

### 举例

想象一条流水线:原料(用户输入)→ 打磨(补历史)→ 质检(护栏)→ 加工(模型)→ 出错返修(重试)→ 成品质检(工具异常兜底)→ 包装(压缩)→ 出厂(响应)。每一站职责单一,顺序错了产品就废了。

### 在 Aegis 中的体现

`factory.py::_assemble_from_features` 明确固定了约 12 个中间件的顺序:

```
0-2  Sandbox 基础设施 (ThreadData → Uploads → Sandbox)
3    DanglingToolCall     悬挂调用修补
4    Guardrail            护栏
5    ToolErrorHandling    工具异常兜底
6    Summarization        上下文压缩
7    Todo                 任务规划(plan_mode)
8    Title                自动标题
9    Vision               视觉/看图
10   LoopDetection        循环检测
11   Clarification        澄清中断(必须最后)
```

工程细节:
- **@Next / @Prev 锚定插入**:`_insert_extra()` 允许额外中间件挂到某个锚点前/后,并做**冲突检测**(两个都 `@Next` 同一锚点报错)和**环检测**(循环依赖报错)。
- **不变式保护**:插入后若 `ClarificationMiddleware` 被挤离末尾,会强制把它移回队尾(因为它一旦触发就 `Command(goto=END)`)。

`backend/packages/harness/aegis/agents/middlewares/` 目录下每个文件就是一个中间件,一一对应上面的职责。

> **体现:中间件链是 Aegis Harness 的"脊椎"。所有健壮性、记忆、护栏能力都以中间件形式挂在这条链上,顺序被显式文档化并做冲突/环校验。**

---

## 5. 要点四:工具接入层(Tool Harness)

### 通用说明

Agent 的能力边界由工具决定。Harness 需要一层**工具注册/筛选/校验**机制:动态决定「这次对话暴露哪些工具给模型」,并保证工具名、schema 一致,避免模型「幻觉调用」不存在的工具。

### 举例

- 一个非视觉模型不应该看到 `view_image` 工具(否则它可能乱调);后台任务被关闭时,`run_background` 等工具不应出现在 schema 里。这就需要**运行时按条件裁剪工具集**。

### 在 Aegis 中的体现

`aegis/tools/tools.py::get_available_tools(groups, include_mcp, model_name)`:

- **分组裁剪**:按 `group` 过滤 config 里定义的工具,可按 Agent 配置收窄暴露面。
- **条件工具**:
  - 仅当 `jobs.enabled` 才追加 `_JOB_TOOLS`(run/check/list/cancel);
  - 仅当 `model_config.supports_vision` 才追加 `view_image_tool`;
  - 仅当 `skill_evolution.enabled` 才追加 `skill_manage_tool`。
- **名字一致性护栏(issue #1803)**:若「config 里的 `name`」与「工具对象的 `.name`」不一致,发出告警——因为这会让模型收到的 schema 名与运行时路由名对不上,产生 "not a valid tool" 错误。
- **按名去重**:config 工具 > 内置 > MCP,重复名跳过并告警。

内置工具本身也体现了「Harness 治理副作用」的思想,例如:
- `present_file` 只暴露 `$HOME/.aegis/outputs`,并前置校验飞书出站限制(文件 ≤30MB、图 ≤10MB);
- `tos_storage` 是**唯一** TOS 入口,沙箱内直接 `tosutil` 被阻断。

> **体现:Aegis 不是把所有工具一股脑丢给模型,而是"按模型能力 + 配置 + 分组"动态、去重、带一致性校验地装配工具集——这是 Tool Harness 的典型做法。**

---

## 6. 要点五:执行隔离与运行时(Sandbox Runtime)

### 通用说明

只要 Agent 能跑命令/改文件,就有**副作用与安全**问题。Harness 必须回答:代码在哪执行?能否伤害宿主?多租户如何隔离?原则通常是 **fail-closed(缺省即拒绝)** 而非 fail-open。

### 举例

- **fail-open(危险)**:找不到沙箱就退回宿主机执行——一旦上下文丢失,用户 A 的命令可能跑在用户 B 的环境里。
- **fail-closed(安全)**:owner 上下文缺失/不匹配就**直接失败**,绝不退回宿主执行。

### 在 Aegis 中的体现

- 唯一 Provider:`aegis.sandbox.aiosandbox.provider:AioSandboxProvider`,**owner-scoped、fail-closed、无宿主回退**(`AGENTS.md` Hard Rule #5)。
- `SandboxProvider.acquire()` 必须携带 `SandboxAcquireContext`,裸 `thread_id` 不支持。
- Agent 可见路径规范化:`$HOME/.aegis/workspace`(工作区)、`/mnt/uploads/<thread>/<transfer>/`(上传)、`$HOME/.aegis/outputs`(可回呈)、`/mnt/state`、`$HOME/.aegis/skills`。
- `/mnt` 隔离在 acquire 时用挂载元数据或 `/mnt/.aegis-owner-key` 标记证明;不匹配/嵌套/不可读写 = 硬失败。
- 生命周期:6h TTL、2h 续租、3h 空闲回收、`max_owner_sessions=50`;空闲是**暂停/恢复**而非销毁。

配套的**命令安全审计**(`sandbox_audit_middleware.py`)对 bash 做三级分类:
- **block**:递归删根、写盘/格式化、管道到 shell 直接执行、命令替换、反向 shell、fork 炸弹、覆盖系统二进制等;
- **warn**:全权限 `chmod`、`pip install`、`sudo`;
- **pass**:其余;并做引号感知拆分、`/mnt` 写保护、输入净化、JSON 审计日志。

> **体现:Aegis 把"模型的手脚"关进 owner 隔离、fail-closed 的 AIO 沙箱,并在进沙箱前用审计中间件按危险度拦截——这是审核类 ToB 产品对 Harness 安全底线的要求。**

---

## 7. 要点六:状态与记忆管理(State / Memory)

### 通用说明

LLM 无状态。Harness 必须补齐:**短期**(会话内结构化状态)、**压缩**(超长时裁剪但不失忆)、**长期**(跨会话检查点/持久存储)。

### 举例

- 用户第 20 轮说「用我刚才让你读的那个技能」,如果压缩策略把「刚读入的技能内容」当旧消息摘要掉,Agent 就"失忆"了。好的 Harness 会**抢救(rescue)**这类近期关键内容。

### 在 Aegis 中的体现

三层结构:
1. **结构化短期状态** `ThreadState(AgentState)`:用 `Annotated + reducer` 管理可合并字段——`artifacts`(`merge_artifacts` 去重)、`todos`、`uploaded_files`、`viewed_images`(`merge_viewed_images`,空 dict 可清空)、`sandbox`、`title` 等。
2. **上下文压缩** `AegisSummarizationMiddleware`:
   - 超 token 阈值时摘要旧消息 + 保留近消息;
   - **技能救援** `_partition_with_skill_rescue`(`preserve_recent_skill_count=5`、`preserve_recent_skill_tokens=25000`)避免刚加载的技能被冲掉;
   - **防失忆修复**:重写 `_trim_messages_for_summary`,去掉基类强制 `start_on=human` 的限制(因为 Aegis 的 system prompt 是 model-call 时才注入,切片里没有 SystemMessage/HumanMessage,基类会返回空切片导致整段历史被丢),保证非空输入永不产出空切片。
3. **长期记忆**:`Checkpointer`(memory / sqlite / postgres 三后端)保存整个 `ThreadState`;owner/session 权威映射落在 **ABase**;技能、SOUL.md 落在 owner 沙箱。

> **体现:Aegis 的记忆系统专门针对自身"运行时注入 system prompt"的特性重写了裁剪逻辑以规避失忆 bug,并有技能救援机制——这是 Harness 记忆工程"抠细节"的典型。**

---

## 8. 要点七:健壮性与容错(Robustness)

### 通用说明

模型和上游会抖动:超时、限流、半截 JSON、悬挂调用、无限循环。Harness 的价值恰恰在于**把这些不可靠"吸收"掉**,对用户始终呈现可控行为,而不是崩栈。

### 举例

- 模型返回了带 `tool_call` 但缺对应 `ToolMessage` 的悬挂消息(常因中断产生)——下一轮模型调用会直接报错。Harness 应**自动补一条合成的 error ToolMessage** 把结构补齐。

### 在 Aegis 中的体现(均为中间件)

- **循环检测** `loop_detection_middleware.py`:两层——完全相同调用(`_hash_tool_calls`,顺序无关 md5)+ 同类工具高频(`_stable_tool_key` 分桶);阈值 warn=5 / hard=8 / window=40;告警用 **HumanMessage** 注入(规避 Anthropic error #1299),硬停时剥离 tool_calls。
- **悬挂调用修补** `dangling_tool_call_middleware.py`:在问题 AIMessage 后插入合成 error ToolMessage。
- **工具异常兜底** `tool_error_handling_middleware.py`:工具异常转 error ToolMessage 使运行继续;检测截断/半 JSON 参数给中文提示;保留 `GraphBubbleUp` 控制流异常不吞。
- **LLM 失败重试 + 熔断** `llm_error_handling_middleware.py`:
  - 指数退避 `retry_max_attempts=3`(base 1000ms、cap 8000ms),**尊重 `Retry-After` 头**,可重试码 `{408,409,425,429,500,502,503,504}`;
  - 熔断器三态 closed/open/half_open(`failure_threshold=5`、`recovery_timeout=60s`);
  - 耗尽后**优雅降级**返回面向用户的兜底 AIMessage(`build_model_error_user_message`),不硬崩。

> **体现:Aegis 把"模型/工具/循环/上游"四类故障各自交给一个专职中间件消化,并以"重试→熔断→降级"三段式收尾——健壮性被工程化、模块化。**

---

## 9. 要点八:可扩展性(Skills / MCP)

### 通用说明

Harness 要能在**不改内核**的前提下扩展能力,通常两条路:声明式**技能(Skills)**(打包可复用的领域能力)和**MCP**(接入外部工具服务器)。扩展点还必须自带**安全审查**,否则等于开了后门。

### 举例

- 想给 Agent 加一个「精度-召回报告生成」能力,不该改内核,而是放一个 `SKILL.md`(带 YAML frontmatter 元数据),Agent 自动发现并复用。

### 在 Aegis 中的体现

**双层技能体系**:
- Public Skills:宿主 `skills/public`(全局共享、经审校),项目内置 `pe-toolkit`、`precision-recall-report`、`rcflow` 等;
- Custom Skills:owner 沙箱 `$HOME/.aegis/skills/custom`(用户私有、可自演进)。

工程机制:
- 每个技能是 `SKILL.md` + YAML frontmatter(`name`/`description`/`long_running`/`protected_cli`...);
- **注入**:`OwnerSkillsMiddleware` 在**每次 model call 前**用最新快照重建 system prompt;并有缓存 + 后台刷新线程(`_refresh_enabled_skills_cache_worker` + `lru_cache`)降低开销;
- **安全扫描** `security_scanner.py`:用 moderation LLM 把技能内容分类 allow/warn/block,识别 prompt injection、提权、数据外泄;**扫描不可用即 fail-closed 到 block**;
- **路径护栏**:`ensure_safe_support_path` 防穿越,白名单子目录 `{references, templates, scripts, assets}`;`atomic_write` 原子写;`HISTORY.jsonl` 记录变更。

**MCP 扩展** `mcp/client.py`:`MultiServerMCPClient` 支持 stdio/SSE/HTTP 三种 transport,支持 env/headers/OAuth,从 `ExtensionsConfig` 读启用的 server,**单个 server 配置失败不影响其它**(容错加载),按 mtime 失效缓存。

> **体现:Aegis 的能力增长走"技能 + MCP"两条声明式扩展路径,且每个扩展点都自带安全扫描/路径护栏/容错加载——扩展性与安全性同时被 Harness 收口。**

---

## 10. 要点九:多入口统一(Runtime Surfaces)

### 通用说明

同一个 Agent 内核,往往需要被多个入口消费:CLI、Web API、IM。Harness 的一大价值是「**一套内核,多个入口**」——入口只做协议适配,业务逻辑不重复。

### 举例

- 飞书渠道和 HTTP API 都要跑同一个审核 Agent。如果每个入口各写一套逻辑,行为必然发散。正确做法是入口只负责"翻译"进出消息,真正的执行交给同一个内核。

### 在 Aegis 中的体现

三个运行面共用同一套 `aegis.*` harness:
- **LangGraph Server(:2024)**:`langgraph.json` 注册 `make_lead_agent_graph`,承载图执行;
- **Gateway API(FastAPI :8001)**:对外 REST(models/mcp/skills/artifacts/uploads/threads/jobs),带 owner 路由鉴权;
- **TUI**(`aegis/tui/`):终端交互,本地调试;
- **Feishu 渠道**(`app/channels/feishu/`):作为 harness 的消费者接入,单用户单线程、CardKit 渲染。

还有内嵌客户端 `aegis/client.py::AegisClient`:无需 HTTP 服务即可直接在进程内调用 chat/stream/models/skills……返回类型与 Gateway schema 一致(由 `TestGatewayConformance` 校验)。

> **体现:LangGraph Server / Gateway / TUI / Feishu / 内嵌 Client 五个入口共享同一个 harness 内核,入口只做协议适配——这正是"Harness 可复用"的收益兑现。**

---

## 11. 要点十:可测试性与版本纪律(Engineering Discipline)

### 通用说明

Harness 是长期演进的基础设施,必须有**工程纪律**保证可维护:强制测试、边界校验、版本语义化、配置解析可预测。否则内核会随时间腐化。

### 举例

- 每个 bugfix 都配单测,否则同一个坑会反复踩;版本号语义化(MAJOR.MINOR.PATCH)让"改了什么级别的东西"一目了然。

### 在 Aegis 中的体现(`AGENTS.md` 六条 Hard Rules)

1. **Import Firewall**(见要点一),CI 强制;
2. **测试强制**:每个功能/修复必须在 `backend/tests/` 配 `test_<feature>.py`,`make test` 必须过;
3. **文档同步**:改代码同步更新 `README.md` / `AGENTS.md`;
4. **禁止 `git checkout`/回滚未提交改动**:只做外科式编辑;
5. **沙箱 owner-scoped fail-closed**(见要点五);
6. **每次提交前 bump `VERSION`**:`MAJOR.MINOR.PATCH`,bugfix 进 patch、feature 进 minor(当前 `VERSION=1.2.4`)。

配置解析也讲**可预测的三级回退**:请求参数 → Agent 配置 → 全局默认(如模型选择 `_resolve_model_name`);配置文件解析优先级 `config_path 参数 > AEGIS_CONFIG_PATH > 当前目录 > 父目录`;`get_app_config()` 热路径缓存 + 守护线程按 mtime 原子重载。

> **体现:Aegis 用 6 条硬规则 + CI 把"Harness 该有的工程纪律"制度化——这决定了内核能不能长期演进而不腐化。**

---

## 12. 总结:Aegis 为什么是一个「Harness 工程」的范本

**Harness 工程 = 围绕 LLM 构建的、让智能"可安全可靠地做功"的全套工程脚手架。** Aegis 几乎逐条命中了它的核心要素,而且做到了「真实成包、边界焊死」:

| Harness 要点 | Aegis 中的载体 | 关键证据 |
|---|---|---|
| 内核/应用分层 | `packages/harness/aegis/` 独立包 | `test_harness_boundary.py` CI 强制 |
| 装配工厂 | `agents/factory.py` | 声明式 `RuntimeFeatures` + 非法组合报错 |
| 中间件链 | `agents/middlewares/*`(~12 个) | 固定顺序 + @Next/@Prev + 环检测 |
| 工具接入 | `tools/tools.py` | 按模型/配置/分组动态装配 + 去重 |
| 执行隔离 | `sandbox/aiosandbox/*` | owner-scoped、fail-closed、命令审计 |
| 状态/记忆 | `ThreadState` + Summarization + Checkpointer | 技能救援 + 防失忆 trim |
| 健壮性 | Loop/Dangling/ToolError/LLMError 中间件 | 重试→熔断→降级三段式 |
| 可扩展 | `skills/*` + `mcp/*` | 技能安全扫描 fail-closed + MCP 容错加载 |
| 多入口 | LangGraph / Gateway / TUI / Feishu / Client | 五入口共享同一 harness |
| 工程纪律 | `AGENTS.md` 6 条 Hard Rules | 强制测试 + VERSION 语义化 |

**一句话:在 Aegis 里,模型只是"发动机",而 `aegis-harness` 这个包才是让这台发动机能装进一辆合规、稳定、可维护的"审核 Agent 整车"的底盘与传动系统——这就是 Harness 工程。**

---

*本文基于对 `tob_audit_agent-master` 源码(harness 内核、中间件、工具、技能、沙箱、网关、MCP)及 `AGENTS.md`、`ARCHITECTURE_ANALYSIS.md`、`README.md` 的系统性通读整理而成。*
