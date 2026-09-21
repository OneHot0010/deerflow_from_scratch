# 工具（Tools）与 MCP 设计详解 —— 以 Aegis 审计 Agent 项目为例

> 本文从 Aegis（内部代号）审计 Agent 项目出发，系统性地拆解「工具（Tool）」这一核心抽象的**设计理念、应用方式与工程实现**，并结合项目中的真实代码逐一举例；随后以完全相同的维度分析 **MCP（Model Context Protocol，模型上下文协议）**，说明它如何作为工具能力的「外部扩展总线」融入 Aegis 的工具体系。
>
> 全文分为两大部分：**第一部分聚焦本地工具（Tools）**，第二部分聚焦 **MCP 工具接入**。两部分均按「概念 → 设计 → 应用 → 实现」的顺序展开，最后给出对照总结。

---

## 目录

**第一部分：工具（Tools）**
1. 概念：什么是 Aegis 语境下的「工具」
2. 设计：文档即契约、依赖注入、声明式装配
3. 应用：工具在项目中的实际使用场景
4. 实现：从定义到装配的完整链路
5. 内置工具全景
6. 工具层的安全审计

**第二部分：MCP（Model Context Protocol）**
7. 概念：MCP 是什么，为什么需要它
8. 设计：客户端/服务端、传输层、鉴权、缓存
9. 应用：MCP 在项目中的接入方式
10. 实现：从配置到可调用工具的完整链路
11. Tools 与 MCP 的对照总结

---

# 第一部分：工具（Tools）

## 1. 概念：什么是 Aegis 语境下的「工具」

在 Aegis 中，**工具（Tool）是「Agent 能够调用的、具有明确输入输出契约的原子能力单元」**。大语言模型（LLM）本身只能生成文本，它无法直接读文件、跑命令、查数据库。工具就是把这些「副作用能力」封装成 LLM 可以通过结构化调用（function calling）触发的函数。

Aegis 的工具全部基于 LangChain 的 `@tool` 装饰器构建，一个工具在运行期表现为一个 `StructuredTool` 对象，它携带三样关键信息：

- **name**：工具名，LLM 通过它来指定要调用哪个工具；
- **description + args schema**：工具的用途说明和参数结构，直接决定 LLM「能否正确地用对工具」；
- **可调用体（coroutine/func）**：真正执行副作用的函数。

在 Aegis 中，工具按**来源**分为三类，这一分类贯穿全文：

| 来源 | 定义位置 | 典型代表 | 装配方式 |
|------|----------|----------|----------|
| **配置定义工具**（config tools） | `config.yaml` 中声明，指向某个 Python 变量 | `bash`、`read_file`、`write_file`、`grep`、`glob`、`ls` | 反射按需加载 |
| **内置工具**（builtin tools） | 代码中硬编码为常量列表 | `present_file`、`ask_clarification`、`tos_storage`、`upgrade_public_skills` | 直接引用 |
| **MCP 工具**（mcp tools） | 外部 MCP Server 提供 | GitHub、Postgres、Filesystem 等第三方能力 | 协议动态发现（见第二部分） |

这三类工具最终会被合并成一个统一的工具列表交给 Agent，LLM 在使用时**并不区分**它们的来源——这正是好的抽象带来的价值：无论能力来自本地函数还是远程 MCP 服务，对模型都是同一种「可调用工具」。

---

## 2. 设计：文档即契约、依赖注入、声明式装配

Aegis 工具体系有三条贯穿始终的设计原则。

### 2.1 文档即契约（Docstring as Schema）

Aegis 的所有工具都使用 `@tool(..., parse_docstring=True)`。这意味着**工具的参数说明直接来自函数的 docstring**，LangChain 会解析 Google 风格的 docstring，自动生成交给 LLM 的 JSON Schema。

以 `sandbox/tools.py` 中的 `read_file` 工具为例（简化）：

```python
@tool("read_file", parse_docstring=True)
async def read_file_tool(
    description: str,
    file_path: str,
    runtime: ToolRuntime[SandboxContext, ThreadState],
    offset: int = 0,
    limit: int = 2000,
) -> str:
    """Read the contents of a file from the sandbox.

    Args:
        description: One-line explanation of why this file is being read.
        file_path: Absolute path of the file to read.
        offset: The line number to start reading from (0-indexed).
        limit: Maximum number of lines to read.
    """
    ...
```

这里有两个值得注意的设计点：

- **`description` 是每个工具的第一个业务参数**。它不是给程序用的，而是强制 LLM 在每次调用时「说明自己为什么要用这个工具」，既提升了可解释性，也便于审计日志追踪 Agent 的意图。
- **docstring 是唯一契约来源**。开发者只要写清楚 docstring，schema 就自动同步，避免了「代码改了、schema 没改」的经典漂移问题。这就是「文档即契约」。

### 2.2 依赖注入：`ToolRuntime` 对 LLM 不可见

注意上例中的 `runtime: ToolRuntime[SandboxContext, ThreadState]` 参数。这是 LangChain 的**运行时依赖注入**机制：

- `runtime` 参数**不会**出现在交给 LLM 的 schema 里，LLM 根本不知道它的存在；
- 它在工具真正执行时由框架自动注入，携带了「当前上下文」——比如沙箱句柄、租户信息、线程状态等。

这种设计把「模型需要决策的参数」（如 `file_path`）与「框架需要传递的运行期依赖」（如沙箱连接）彻底分离。LLM 只需关心业务语义，基础设施细节对它透明。

### 2.3 声明式装配：config.yaml + 反射

Aegis 不在代码里硬编码「加载哪些工具」，而是在 `config.yaml` 中**声明**，再通过反射加载。例如：

```yaml
tool_groups:
  - name: file:read
  - name: file:write
  - name: bash

tools:
  - name: read_file
    group: file:read
    use: aegis.sandbox.tools:read_file_tool
  - name: bash
    group: bash
    use: aegis.sandbox.tools:bash_tool
```

`use` 字段的格式是 `模块路径:变量名`。加载时由 `reflection/resolvers.py` 的 `resolve_variable()` 负责解析：它把字符串按冒号切开，`import` 对应模块，再 `getattr` 取出变量。这样一来，**新增/下线一个工具只需改配置，无需改装配代码**，实现了配置与代码的解耦。

---

## 3. 应用：工具在项目中的实际使用场景

在 Aegis 这个「审计 Agent」里，工具就是 Agent 完成审计任务的双手。典型的一次审计任务会串联多个工具：

1. **探查阶段**：Agent 先用 `ls` / `glob` 摸清目标代码库结构，再用 `grep` 定位关键代码或配置；
2. **取证阶段**：用 `read_file` 精确读取可疑文件的具体行段（借助 `offset`/`limit` 避免一次性读入超大文件）；
3. **验证阶段**：用 `bash` 执行只读的检查命令（如运行 linter、跑测试、统计依赖）；
4. **产出阶段**：用 `write_file` / `str_replace` 生成审计报告或修复补丁，用 `present_file` 把结果文件呈现给用户，必要时用 `tos_storage` 上传到对象存储；
5. **交互阶段**：当信息不足时，用 `ask_clarification` 反问用户，而不是盲目猜测。

### 3.1 举例：工具输出的截断策略

审计任务经常面对超大输出（一个日志文件几十万行、一次 `bash` 命令刷屏）。如果把全部内容塞回 LLM，既浪费上下文又可能撑爆窗口。Aegis 为此设计了**差异化截断策略**：

- **`bash` 工具：中间截断（middle-truncate）**。命令输出往往「开头是启动信息、结尾是最终结果」，两头都重要，所以保留首尾、省略中间，并用占位提示省略了多少内容。对应 `_truncate_bash_output()`。
- **`read_file` / `ls` 工具：头部截断（head-truncate）**。读文件和列目录通常从头看起，所以保留开头、截断尾部，对应 `_truncate_read_file_output()` / `_truncate_ls_output()`。

同一个「截断」需求，因工具语义不同而采用不同策略——这是「工具设计要贴合使用场景」的一个很好的例证。

### 3.2 举例：错误吞咽（Error-Swallowing）返回模式

Aegis 的工具在遇到错误时，**不抛异常，而是返回以 `Error:` 开头的字符串**。例如 `read_file` 读不到文件时返回 `"Error: file not found: /x/y"`。

这样设计的原因是：工具的调用者是 LLM，不是传统程序。如果抛异常，会中断整个 Agent 循环；而返回错误字符串，LLM 能「读到」错误、理解发生了什么，并**自主决策下一步**（换个路径重试、改用别的工具、或向用户求助）。这把错误处理的控制权交还给了模型，让 Agent 更有韧性。

---

## 4. 实现：从定义到装配的完整链路

工具从「被定义」到「被 Agent 用上」，在 Aegis 中经过一条清晰的流水线，核心入口是 `tools/tools.py` 的 `get_available_tools()`。

### 4.1 装配入口 `get_available_tools()`

```python
def get_available_tools(groups, include_mcp=True, model_name=None):
    # 1) 按 config.yaml 声明，反射加载「配置定义工具」
    loaded_tools = _load_config_tools(groups)

    # 2) 追加「内置工具」
    builtin_tools = list(BUILTIN_TOOLS)
    if jobs.enabled:
        builtin_tools += _JOB_TOOLS
    if skill_evolution.enabled:
        builtin_tools += [upgrade_public_skills_tool]

    # 3) 追加「MCP 工具」（见第二部分）
    mcp_tools = get_mcp_tools() if include_mcp else []

    # 4) 合并 + 去重（按 .name），并按优先级保留
    all_tools = loaded_tools + builtin_tools + mcp_tools
    return _dedup_by_name(all_tools)
```

### 4.2 三个实现要点

**（1）条件挂载（Conditional Mounting）**

工具不是无条件全挂上的，而是根据运行时开关和模型能力动态决定：

- `jobs.enabled` 为真才挂载定时任务相关工具（`_JOB_TOOLS`）；
- `skill_evolution.enabled` 为真才挂载 `upgrade_public_skills`；
- `model.supports_vision` 为真才挂载需要视觉能力的工具。

这保证了「模型用不了的工具不会出现在它面前」，减少误用和 token 浪费。

**（2）工具名去重与优先级**

三类工具合并后可能出现重名。Aegis 按 `.name` 去重，并遵循 **配置工具 > 内置工具 > MCP 工具** 的优先级（对应 issue #1803 的修复）。这样即便某个 MCP Server 提供了与本地工具同名的工具，本地可信实现也会优先胜出，避免外部服务「劫持」核心能力。

装配过程中还有一段**名称一致性校验**：如果 `config.yaml` 里声明的 `name` 与实际加载到的工具对象的 `.name` 不一致，会打印告警日志，帮助开发者及早发现配置漂移。

**（3）沙箱的惰性获取与归属校验**

需要沙箱的工具（bash、文件类）在**首次调用时才惰性初始化沙箱**（`ensure_sandbox_initialized()`），而不是启动即分配，节省资源。同时，每次使用前会做**归属校验**（`_validate_sandbox_owner()`）：比对沙箱的 `owner_key` 与当前上下文，一旦不匹配立即拒绝，防止跨租户/跨会话误用他人沙箱。写操作还会额外检查目标路径是否落在受保护的公共技能运行目录内（`_is_public_skills_runtime_path()`），保护共享资源不被污染。

---

## 5. 内置工具全景

下表汇总 Aegis 中主要工具的用途、来源与关键实现特征，便于快速索引：

| 工具名 | 来源 | 用途 | 关键实现特征 |
|--------|------|------|--------------|
| `bash` | 配置 | 在沙箱内执行命令 | 中间截断输出；受安全审计中间件拦截 |
| `read_file` | 配置 | 读取文件内容 | 支持 offset/limit；头部截断 |
| `write_file` | 配置 | 写入文件 | 公共技能目录写保护 |
| `str_replace` | 配置 | 按精确字符串替换编辑文件 | 要求唯一匹配，避免误改 |
| `ls` | 配置 | 列出目录 | 头部截断 |
| `glob` | 配置 | 按通配模式找文件 | 按修改时间排序 |
| `grep` | 配置 | 内容检索 | 基于 ripgrep 语义 |
| `present_file` | 内置 | 把结果文件呈现给用户 | 面向交付 |
| `ask_clarification` | 内置 | 信息不足时反问用户 | 避免盲目猜测 |
| `tos_storage` | 内置 | 上传文件到对象存储 | 面向跨会话分享 |
| `upgrade_public_skills` | 内置（条件） | 升级公共技能 | 受 skill_evolution 开关控制 |
| `_JOB_TOOLS` | 内置（条件） | 定时任务管理 | 受 jobs 开关控制 |

所有这些工具最终都汇入 `get_available_tools()` 的统一列表，对 LLM 呈现为一致的可调用能力。

---

## 6. 工具层的安全审计

`bash` 工具是威力最大、也最危险的工具——它能执行任意命令。Aegis 为此设置了一道**安全审计中间件**（`SandboxAuditMiddleware`），在命令真正进入沙箱执行之前进行拦截式审查。

其核心是一组**预编译的高风险行为正则规则**（`_HIGH_RISK_PATTERNS`）。中间件会把待执行命令与这组规则逐一比对，一旦命中就阻断执行并返回明确的拒绝原因。它所防范的高风险行为大致可归为以下几类（此处只做自然语言描述，不列具体命令字面量）：

- **破坏性文件操作**：递归强制删除关键根目录、清空整块存储区域等可能造成不可逆数据丢失的行为；
- **磁盘与分区操作**：对磁盘进行格式化、重建分区、低层写盘等直接危害底层存储的操作；
- **敏感路径写入**：向系统凭据、设备节点等敏感位置写入内容；
- **凭据与隐私读取**：读取系统账户凭据文件等隐私敏感信息；
- **可疑的远程下载后直接执行**：把远程内容拉下来后不经检查直接喂给解释器执行的「一步到位」模式；
- **隐蔽的编码绕过**：通过多层编码/解码来规避文本审查后再执行的行为；
- **注入与劫持**：借助动态库预加载、伪造网络连接通道等方式劫持进程行为。

这套机制体现了 Aegis 的一个重要理念：**Agent 的能力越强，越需要在工具边界处设防**。安全审计不放在模型侧（模型可能被绕过），而是放在工具执行的必经之路上，形成一道确定性的、不可协商的防线。这与前面「沙箱归属校验」「公共目录写保护」一起，构成了工具层纵深防御的三道关卡。

---

