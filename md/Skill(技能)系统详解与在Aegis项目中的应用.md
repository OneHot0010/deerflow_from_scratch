# Skill(技能)系统详解与在 Aegis 项目中的应用

> 分析对象:`tob_audit_agent-master`(内部代号 **Aegis**,版本 `1.2.4`)
> 一个基于 **LangGraph** 构建的企业级(ToB)后端型智能审核 / 通用 Super Agent 系统。
> 本文聚焦「Skill(技能)」这一核心扩展机制,从**定义 → 设计动机 → 物理形态 → 实现细节 → 项目内应用举例**逐层拆解,并结合源码说明。

---

## 目录

1. [什么是 Skill——本项目的定义](#1-什么是-skill本项目的定义)
2. [设计动机与理念](#2-设计动机与理念)
3. [物理形态:目录结构与 SKILL.md 格式](#3-物理形态目录结构与-skillmd-格式)
4. [Skill 数据类(内核建模)](#4-skill-数据类内核建模)
5. [五阶段生命周期](#5-五阶段生命周期)
6. [渐进式加载(Progressive Loading)](#6-渐进式加载progressive-loading)
7. [两大分类维度](#7-两大分类维度)
8. [治理机制:校验 + 安全扫描 + 自进化](#8-治理机制校验--安全扫描--自进化)
9. [项目内真实技能举例](#9-项目内真实技能举例)
10. [端到端调用示例](#10-端到端调用示例)
11. [Skill vs Tool vs MCP 对比](#11-skill-vs-tool-vs-mcp-对比)
12. [总结](#12-总结)

---

## 1. 什么是 Skill——本项目的定义

在 Aegis 中,**Skill(技能)不是一段被硬编码进 Agent 的工具函数**,而是:

> 一个以 `SKILL.md` 为核心、可插拔的「能力包」——它把「一段可复用的领域知识 / 操作手册」与「可选的 CLI 可执行体」打包在一起,由 harness 内核在运行期解析、注入到 system prompt,并交由 LLM 自主决定何时调用。

换句话说,一个 Skill = **元数据(告诉 Agent 有这个能力、什么时候用)** + **使用手册(告诉 Agent 怎么用)** + **可选的可执行体(真正干活的 CLI)**。

Agent 本身不需要为每个新能力改代码;只要在 `skills/` 目录下放一个符合规范的 `SKILL.md`,该能力就"长"在了 Agent 身上。这正是 Aegis 「配置驱动、内核稳定」范式在能力层的体现。

---

## 2. 设计动机与理念

企业审核场景的能力是**高度碎片化且持续增长**的:准召报告、PE 标注、RCFlow 工作流、合规校验……如果每加一个能力就改一次 Agent 代码,会带来三个问题:

- **迭代慢**:能力与内核强耦合,发布节奏被绑死;
- **上下文爆炸**:把所有操作细节塞进 system prompt,既贵又干扰判断;
- **难治理**:无法按能力粒度做权限、安全、版本管理。

Aegis 的解法有三条设计主线:

| 理念 | 含义 | 落地手段 |
|------|------|----------|
| **能力即数据** | 能力用 Markdown 文件描述,而非代码 | `SKILL.md`(YAML front-matter + 正文) |
| **渐进式加载** | system prompt 只放"索引",正文按需读 | 只注入 name/description/位置,正文靠 `read_file` 拉取 |
| **可治理** | 每个能力可校验、可扫描、可演进 | `validation.py` + `security_scanner.py` + 自进化机制 |

---

## 3. 物理形态:目录结构与 SKILL.md 格式

一个标准 Skill 的目录结构如下(以 `public` 类为例):

```
skills/public/<skill-name>/
├── SKILL.md              # 必需 — 元数据 + 使用说明(agent 读取此文件)
├── pyproject.toml        # 可选 — 包定义 + CLI 入口点
├── <package>/            # 可选 — Python 包(真正的可执行体)
│   ├── __main__.py       # CLI 入口(main 函数)
│   └── scripts/          # 业务逻辑
└── references/           # 可选 — 二级参考资料(按需加载)
    └── STRUCTURE.md
```

`SKILL.md` 由 **YAML front-matter** + **Markdown 正文** 两段构成:

```yaml
---
name: precision-recall-report
description: 准召报告生成工具。接收 CSV/XLSX 数据文件,生成交互式准召分析报告并上传至 TOS,返回可访问的 HTML 链接。触发词:准召、准召报告、precision recall report。
---

(以下 Markdown 正文即"使用手册":一句话概述 / 子命令表 / 输入契约 / 输出约定 / 常见问题)
```

**front-matter 字段**(来自 `docs/skills/SKILL_DEVELOPMENT.md` 与 `validation.py`):

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 标识符,须与目录名一致,小写连字符命名(如 `pe-toolkit`) |
| `description` | 是 | 功能描述 + 触发词,Agent 依据它判断是否调用 |
| `license` | 否 | 许可证 |
| `long_running` | 否 | `true` 表示必须经 `run_background` 调用,不阻塞主循环 |
| `protected_cli` / `protected_clis` | 否 | 声明依赖用户身份的 CLI,命令在 owner sandbox 内执行 |

> 正文之所以被称为 Agent 的"使用手册",是因为它会被原样注入到 system prompt(经渐进式加载后按需),Agent 读它来学会如何构造命令行、如何解析输出。

---

## 4. Skill 数据类(内核建模)

内核用一个 `dataclass` 把 `SKILL.md` 建模成结构化对象——`backend/packages/harness/aegis/skills/types.py`:

```python
@dataclass
class Skill:
    name: str                       # 技能名(= 目录名)
    description: str                # 功能 + 触发词
    license: str | None
    skill_dir: Path | str           # 技能目录
    skill_file: Path | str          # SKILL.md 路径
    relative_path: PurePath         # 相对 category 根的路径
    category: str                   # public 或 custom
    enabled: bool = False           # 是否启用
    long_running: bool = False      # True 则须经 run_background 调用
    protected_clis: tuple[str, ...] = ()
    content_digest: str = ""        # 内容摘要(用于变更检测/缓存)
```

关键方法 `get_container_file_path()` 把技能映射为**容器内的绝对路径**:

```python
def get_container_file_path(self, container_base_path="$HOME/.aegis/skills"):
    return f"{self.get_container_path(container_base_path)}/SKILL.md"
    # 结果形如:$HOME/.aegis/skills/{category}/{skill_path}/SKILL.md
```

这意味着 Agent 在 system prompt 里看到的技能"位置",正是它稍后可以用 `read_file` 打开的真实文件路径——**索引与实体一一对应**。

---

## 5. 五阶段生命周期

从磁盘上的一个 `SKILL.md`,到最终被 LLM 调用,经历五个阶段:

```
① 解析(parse)   →  ② 加载(load)  →  ③ 注入(inject)  →  ④ 决策(decide)  →  ⑤ 执行(execute)
   parser.py          loader.py         prompt.py           LLM 自主判断        bash / run_background
```

| 阶段 | 负责模块 | 做了什么 |
|------|----------|----------|
| ① 解析 | `skills/parser.py` | 用正则 `^---\\s*\\n(.*?)\\n---\\s*\\n` 切出 front-matter,解析成元数据;`_parse_protected_clis()` 兼容 `protected_cli` 与 `protected_clis` 两种写法 |
| ② 加载 | `skills/loader.py` | `os.walk` 遍历 `public/`、`custom/`,跳过软链和隐藏目录,并从 `ExtensionsConfig.from_file()` 读取启用状态 |
| ③ 注入 | `agents/lead_agent/prompt.py` | `_get_cached_skills_prompt_section()` 把每个技能渲染成 `<skill>` 段(标注 `[built-in]`/`[custom, editable]`、`[long-running:...]`、`[protected CLI:...]`),拼进 `SYSTEM_PROMPT_TEMPLATE` |
| ④ 决策 | LLM | 依据 system prompt 中的 **MANDATORY Skill-First Rule / STEP 1 SKILL CHECK**,先判断"是否有匹配技能",命中则优先走技能 |
| ⑤ 执行 | `bash` / `run_background` | 短任务直接 `bash` 跑 CLI;`long_running` 技能则由 `run_background` 后台启动,不轮询阻塞 |

> 每次模型调用前,`OwnerSkillsMiddleware` 会用当前 owner 的技能快照重建 system prompt,保证"刚创建/修改的技能"立即对本轮生效。

---

## 6. 渐进式加载(Progressive Loading)

这是 Skill 机制**控制成本、避免上下文爆炸**的关键设计:

- **一级(常驻)**:只有 `name + description + 位置` 进入 system prompt。几十个技能也只占极小篇幅,相当于一张"能力索引表"。
- **二级(按需)**:LLM 决定要用某技能后,才用 `read_file` 打开对应 `SKILL.md` 正文,拿到完整使用手册。
- **三级(再按需)**:手册里若引用 `references/*.md`,再在需要时进一步读取。

`prompt.py` 中用 `@lru_cache` 缓存渲染结果,并配合**后台刷新线程**周期性重建,兼顾性能与实时性。

> 效果:Agent"知道自己有哪些能力"的成本极低,而"如何使用某能力"的详细知识只在真正用到时才付费加载。

---

## 7. 两大分类维度

Skill 沿两个正交维度分类:

### 维度一:归属与可写性(`category`)

| 类别 | 位置 | 特点 |
|------|------|------|
| **public(内置)** | `skills/public/` | 全局共享、**只读**,随发布固化,由官方维护 |
| **custom(自定义)** | owner sandbox 内 `$HOME/.aegis/skills/custom/` | 按 owner 隔离、**可增删改**,支持运行期创建与自进化 |

### 维度二:运行形态

| 形态 | 声明方式 | 调用方式 |
|------|----------|----------|
| **短任务** | 默认 | 直接 `bash` 执行 CLI,同步返回结果 |
| **长任务** | `long_running: true` | 经 `run_background` 后台启动,不轮询、不阻塞主循环 |
| **受保护 CLI** | `protected_clis: [...]` | 先在 sandbox 内完成身份认证(如 `rcflow` 依赖 `bytedcli`),再执行 |

---

## 8. 治理机制:校验 + 安全扫描 + 自进化

Skill 是"可运行的外部内容",尤其 `custom` 类可在运行期被创建,因此 Aegis 为其配了三道治理闸门。

### 8.1 结构校验(`skills/validation.py`)

- `ALLOWED_FRONTMATTER_PROPERTIES` 白名单:仅允许 `name / description / license / allowed-tools / metadata / compatibility / version / author / long_running / protected_cli / protected_clis`;
- `name` ≤ 64 字符、小写连字符;`description` ≤ 1024 字符且禁止尖括号;
- 路径穿越防护:阻止 `../` 等越界写入。

### 8.2 安全扫描(`skills/security_scanner.py`)

- `scan_skill_content()` 返回 `ScanResult(decision, reason)`,决策分 **allow / warn / block** 三档;
- 采用**保守失败策略**:一旦扫描本身失败或含有无法判定的可执行内容,直接按 `block` 处理;
- 在 `skill_manage_tool.py` 的 `_scan_or_raise` 中兜底:`block` → 抛异常;非 `allow` 的可执行内容 → 抛异常,阻止落盘。

### 8.3 技能自进化

Agent 在积累经验(如 5 次以上工具调用、命中坑点、收到用户纠正)后,可通过 `skill_manage_tool` 的 `create / patch / edit / delete / write_file / remove_file` 动作,在自己的 owner sandbox 中**沉淀出新的 custom 技能或修补已有技能**——把"这次踩过的坑""用户纠正过的做法"固化为下次可复用的能力。所有写入前都要过上面两道校验/扫描闸门。

---

## 9. 项目内真实技能举例

下面结合 `skills/public/` 中真实存在的技能,说明三种典型形态。

### 9.1 短任务示例:`precision-recall-report`(准召报告)

- **形态**:短任务,默认 `bash` 调用。
- **能力**:接收 CSV/XLSX 数据文件 → 生成交互式准召分析 HTML 报告 → 上传 TOS → 返回可访问链接。
- **手册要点**:明确规定了**输入格式契约**(调用前必须把数据转成指定 CSV/XLSX 结构)与**输出约定**(结果为 JSON,含 HTML URL,Agent 应把链接呈现给用户)。
- **典型触发**:用户说"帮我生成这份标注数据的准召报告"。

### 9.2 长任务示例:`pe-toolkit`(PE 评估/优化)

- **形态**:`long_running: true`,经 `run_background` 启动。
- **能力**:PE(Prompt Engineering)评估、优化、auto-pe 自动迭代、FP/FN 分析等一站式生命周期 CLI。
- **为何长任务**:自动迭代/评估耗时长,若同步执行会长时间阻塞 Agent 主循环。system prompt 会自动为它附加 `[long-running: must be invoked via run_background tool]` 提示,引导 Agent 用后台方式启动。
- **典型触发**:"跑一次 PE 优化""帮我做 auto-pe 迭代"。

### 9.3 受保护 CLI 示例:`rcflow`(RCFlow 工作流)

- **形态**:`protected_clis: [bytedcli]`,依赖用户身份。
- **能力**:调用 RCFlow 工作流(run workflow、取执行结果、拿输入模板等)。
- **关键约束**:**Step 0 强制认证**——命令必须先在 owner sandbox 内完成 `bytedcli` 登录/鉴权,且 status/login/smoke 命令都在 sandbox 内执行,不依赖 host 侧 token。手册大量引用 `references/` 二级资料以压缩一级注入体积。
- **典型触发**:"帮我跑一下这个 rcflow workflow""拿一下这个 execution 的结果"。

> 此外项目还内置了如 `auto-pe-labeling`(自动 PE 标注)等技能。它们共同构成 Aegis 面向审核场景的"能力货架",Agent 按需取用。

---

## 10. 端到端调用示例

以"用户上传一份标注结果,要求出准召报告"为例,串起整条链路:

```
1. 加载期:loader.py 扫描到 skills/public/precision-recall-report/SKILL.md,
           解析出 name/description,标记 enabled。

2. 注入期:prompt.py 把它渲染成 <skill> 段(标注 [built-in]),
           连同其它技能一起拼进 system prompt(仅 name+description+位置)。

3. 用户提问:"这是我的标注 CSV,帮我出准召报告。"

4. 决策期:LLM 依据 STEP 1 SKILL CHECK 发现 description 命中"准召报告",
           判定应优先使用该技能。

5. 拉手册:LLM 用 read_file 打开该技能的 SKILL.md 正文,
           学习输入契约(CSV 需转成规定列)与 CLI 用法。

6. 执行期:LLM 通过 bash 调用该技能 CLI(短任务),
           CLI 生成 HTML 报告并上传 TOS。

7. 呈现:LLM 按"输出约定"解析返回 JSON,把 HTML 链接交给用户。
```

整个过程 Agent **没有改任何代码**,能力完全来自 `SKILL.md`。

---

## 11. Skill vs Tool vs MCP 对比

三者都是"给 Agent 增加能力"的手段,但定位不同:

| 维度 | Skill(技能) | Tool(工具) | MCP |
|------|-------------|------------|-----|
| 本质 | Markdown 描述的能力包 | 代码里注册的函数 | 外部协议接入的能力 |
| 加载方式 | 渐进式(索引常驻,正文按需) | 全量注册到工具列表 | 由 MCP server 声明 |
| 增删改 | 放/改一个 SKILL.md 即可,可运行期演进 | 需改代码、重发布 | 需配置/连接 server |
| 执行体 | 可选 CLI(bash/run_background) | 函数本身 | 远端 server 提供 |
| 治理 | 校验 + 安全扫描 + 白名单 | 靠代码评审 | 靠 server 侧管控 |
| 适用 | 领域操作手册 + 可复用流程 | 原子、通用、高频动作 | 跨系统/跨服务集成 |

一句话:**Tool 是"原子动作",MCP 是"外部接线",Skill 是"带说明书的领域能力包"**。Skill 的独特价值在于——能力以数据形式存在、按需加载、可被 Agent 自己沉淀和演进。

---

## 12. 总结

Aegis 的 Skill 系统把"给 Agent 加能力"这件事从**改代码**变成了**写 Markdown**:

- **定义上**,Skill 是"元数据 + 使用手册 + 可选 CLI"三合一的能力包,以 `SKILL.md` 为核心;
- **实现上**,经"解析 → 加载 → 注入 → 决策 → 执行"五阶段,靠渐进式加载把成本压到最低;
- **分类上**,沿 public/custom 与 短任务/长任务/受保护 CLI 两维组织;
- **治理上**,用结构校验 + 安全扫描 + 白名单守住可运行外部内容的安全边界,并支持 Agent 运行期自进化;
- **应用上**,`precision-recall-report`、`pe-toolkit`、`rcflow` 等真实技能共同构成面向审核场景的能力货架。

这套机制让 Aegis 在"内核稳定"的同时保持"能力可无限扩展",是其作为企业级 Super Agent 的关键工程基础。

