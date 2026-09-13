# Changelog

本项目的版本演进记录。遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定，
版本号遵循语义化版本（SemVer）。

## [Unreleased]

### Fixed
- `.env.example` 改为最小可复制模板：只启用本地启动必需配置，Static/JWT、MySQL Policy Store、审计与 OTLP 等模式全部改为注释示例，避免空字符串被误当成有效配置。
- 新增 `.env.example` 可复制性回归测试，实际加载模板并导入 `mcp_server.server`，防止空值、行尾注释和错误 Token 映射再次导致启动失败。

## [v0.7.1] - 2026-09-13

### Fixed
- **管理端点保护**：`/traces`、`/dashboard`、`/alerts` 新增 `_management_guard`——配置 `MANAGEMENT_AUTH_TOKEN` 后要求 Bearer 认证；未配置时默认 `MANAGEMENT_LOOPBACK_ONLY=true` 仅放行回环地址，实现内网隔离。
- **SQL 文本不落指标标签**：移除 `query_service` 中 `str(sql)[:20]` 的 `tool` 标签回退，指标不再可能泄露 SQL。
- **工具失败率分母修正**：`tool_failure_rate` 只统计 `tools/call` 的 MCP 请求作分母，握手/Resource 等不再计入。
- **OTLP 导出覆盖关闭场景**：Server 关闭前 `flush_exporter()` 一次性投递剩余 Span；Agent 在 `run()`（非仅 CLI）也 `start_exporter()` + 结束时 `flush_exporter()`。
- **required audit 统一统计**：`AUDIT_REQUIRED` 改为走 `AuditWriter.persist_sync`，与异步路径共享 persisted/healthy/failed 统计与 `audit_persisted_total` 指标。
- **Trace Context 加固**：`parse_traceparent` 拒绝全零 trace/span id；采样决策改为**继承父采样**（父未采样则子不采样，不再按自身比例重新决策）。
- 新增对应回归测试（管理保护 / 全零拒绝 / 采样继承 / required 统计 / tools-call 分母）。

## [v0.7.0] - 2026-09-13

### Added
- **共享遥测内核 `telemetry/`**：纯标准库实现 OpenTelemetry 数据模型 + W3C `traceparent` 传播 + 有界内存 span 缓冲 + 可选 OTLP/HTTP（JSON）导出；Server 与 Agent 共用，让 Agent → MCP → Authorization → Database 四层 Span 落在同一条 trace。
- **四层 Span 与跨服务链路**：`agent.run/agent.llm/agent.tool`、`mcp.server.*`、`authorization.decision`、`database.query` 均带状态；一次请求可凭 `request_id` + `trace_id` 完整定位；Agent 启动 OTLP 导出并在退出前 flush。
- **Metrics 加固**：有界直方图（固定分桶，不再在内存保留全部样本）；标签卫生（禁止原始 SQL/完整用户输入/高基数字段）；SQL 延迟成功/失败都记录；错误率按「错误码 × 工具 × 主体类型」聚合。
- **错误率统一分母**：新增 `query_requests_total` 作为统一分母（覆盖认证/校验/权限/并发/DB 全路径，保证 0–100%）；新增 `tool_business_errors_total` 计入返回业务错误的工具调用，修正 `tool_failure_rate`。
- **审计**：后台异步批量落库 + 失败策略（ignore/warn/fail）+ 保留周期 + 归档表；`AUDIT_REQUIRED` 时绕过有损队列直接同步写入并传播异常。
- **可观测性端点**：`/traces`（最近 span）、`/dashboard`（运行态概览）、`/alerts`（进程内告警状态）。
- **进程内告警引擎** + 配置 `configs/alerts.yaml`（可注入时钟，支持 pending→firing→resolved 状态机）；配套 Grafana 面板 `configs/grafana/dashboard.json` 与 Prometheus 规则 `configs/prometheus/alerts.yml`（错误率 / P95/P99 延迟 / 超时率 / 工具失败率 / 连接池饱和度）。
- `tests/test_observability_v07.py`：有界直方图、标签卫生、traceparent/Span、告警状态机与时序、采样、审计 required 语义、错误率口径等回归测试。

### Fixed
- **告警永远不 firing**：`pending_since` 首次进入 pending 即记录，修复后 `for` 窗口可达，规则能正常转为 firing/resolved。
- **Agent→MCP trace 未接通**：`agent.run` 根 Span 用 `set_current` 接通 `agent.llm/agent.tool` child 与 `client.py` 的 `traceparent` 注入。
- **OTLP 重复导出**：导出改消费型队列（出队即不再重发）；`OTEL_SAMPLE_RATIO=0` 时未采样 Span 不再被记录/导出。
- **`AUDIT_REQUIRED` 不保证入库**：required 模式绕过有损队列直接同步写入并向上抛错。

## [v0.6.0] - 2026-09-12

### Added
- **Agent Reliability（V0.6）**：让 Agent 在模型异常、工具失败与 SQL 错误下仍能可控结束。
- `agent/reliability.py`：稳定错误码（`success`/`timeout`/`model_error`/`tool_error`/`sql_syntax_error`/`permission_denied`/`budget_exceeded`）、错误分类（临时/可修复SQL/权限/超时/危险）、指数退避+抖动重试、统一预算（迭代/工具调用/SQL修复/Token/费用）、全局请求截止时间与 `RunResult`（答案+状态+指标）。
- `agent/`：同步 `OpenAI` 替换为 `AsyncOpenAI`；LLM 与 MCP 调用均含连接/读取/单次调用超时；临时错误退避重试、永久错误立即终止；SQL Repair 仅修复语法/字段错误，权限/超时/危险 SQL 不进入盲重试；每次结束返回稳定错误码与用户可理解信息。
- `tests/test_agent_reliability.py`：端到端评测集（正确性/工具选择/修复次数/超限退出/错误提示），无需真实 LLM 与 MCP Server，用 fake responder + fake session 驱动。
- `requirements.txt`：显式声明直接导入的 `httpx2==2.12.0`，不再依赖 mcp/openai 的传递引入。

### Fixed（评审回修，承接 V0.6）
- 真正的 SQL 语法错误（服务器 `parse_error`）不再被误判为权限拒绝，正确进入 SQL Repair。
- 全局 Request Deadline 改为硬上限：LLM 每次尝试、上下文获取都按剩余时长压缩单次调用超时。
- 工具调用预算修正 off-by-one：恰好执行 `MAX_TOOL_CALLS` 次后第 `N+1` 次才超限退出。
- 尊重 MCP `CallToolResult.is_error`，即便无标准错误结构也按错误分类处理。
- `AsyncOpenAI` 真正配置 connect/read/write/pool 四类超时。
- 成功结果不再携带 `error_message`。
- `scripts/batch_test.py` 适配 `run()` 返回 `RunResult` 的新契约。
- 新增 `.env.example`：覆盖 Agent V0.6 可靠性相关环境变量与默认值说明。
- 工具重试限制为显式 `RETRY_SAFE_TOOLS` 白名单：仅白名单内只读工具在临时错误时退避重试；非白名单工具至多执行一次（失败即终止），杜绝 `publish_permission_policy` 等有副作用工具被重复提交。
- 新增 `BLOCKED_TOOLS`：发现阶段即从工具清单剔除 `publish_permission_policy`/`reload_permission_policy`，运行时再拦截（纵深防御），避免 Agent 调用服务端管理操作。
- MySQL 执行超时（3024）此前文本 `Statement exceeded execution time` 不匹配超时特征、被误判为可修复 SQL 消耗修复预算；现 **Server 侧**将数据库异常映射为稳定 code（超时→`query_timeout`、未知→`query_error`），**Agent 侧**新增 `query_timeout` 码识别、补齐超时文本特征，并把无法归类的错误默认降级为 `FATAL_TOOL`（立即终止）而非默认可修复。
- 全局 Deadline 进一步收紧为硬上限：`run()` 外层用统一的 `asyncio.timeout(REQUEST_DEADLINE_SECONDS)` 包住 MCP 建连 + `initialize` + 上下文读取 + 全循环；`asyncio.TimeoutError` 现统一收敛为 `TIMEOUT`（带明确信息），不再落入通用 `TOOL_ERROR`/空 `error_message`。
- `.env.example` 补全为覆盖 `agent/config.py` 与 `mcp_server/config.py` 全部 `os.getenv` 变量的全量模板（75 项）：新增 `AUTH_JWT_*`、`AUTH_PRINCIPAL_MODE`、`POLICY_DB_*`、`AUDIT_*`、`SCHEMA_*`、`DOMAIN_QUERIES_PATH` 及连接池/优雅退出配置；`AUTH_TOKENS_JSON` 样例改为合法 JSON（默认注释，验证可解析）；`LLM_API_KEY` 占位符改为含「填入」字样，确保 `SQLAgent` 未配置校验能被提前触发。

## [v0.5.0] - 2026-09-12

### Added
- **Domain Query Layer**：新增 `sales_summary`、`company_ranking`、`industry_analysis` 三个领域工具，模板与业务口径外置到 `configs/domain_queries.yaml`。
- **QueryTemplate / QuerySpec**：声明参数、SQL 槽位、结果列、时间粒度、排序字段和领域定义版本；配置按 mtime 与内容哈希热重载。
- **参数化执行**：值参数经类型、范围和白名单校验后使用数据库驱动绑定；标识符参数只能映射到服务端允许的固定 SQL 片段。
- **统一领域 DTO**：`data`、`columns`、`meta`、`definition_version`、`policy_version` 和 `truncated`；全部复用现有 AST Guard、RBAC、ACL、RLS、LIMIT、审计和指标链路。
- **Agent 领域工具优先**：常见分析优先调用领域工具，复杂或未覆盖问题再回退 `run_query`。
- **测试**：新增 `tests/test_domain_query.py`，覆盖参数白名单、驱动绑定、RLS 一致性、DTO、截断、超时、非法参数和真实 MySQL 集成。

### Fixed
- `industry_analysis` 在指定行业时仍使用窗口内全部行业作为占比分母。
- 领域工具必填日期在 MCP Schema 中标记为 required。
- 领域结果列与 `max_limit` 契约在运行时强制执行。

## [v0.4.1] - 2026-09-12（基线冻结）

### Changed
- 将当前能力固化为稳定的 **V0.4.1 基线**：对外 Tool（10 个）、Resource（2 个）、权限动作与错误结构冻结，见 README「稳定 API 契约」。

### Added
- `tests/test_api_contract.py`：锁定 MCP Tool 清单，改动即回归失败。
- `.github/workflows/ci.yml`：最小 CI，配 MySQL service 建库后运行全部测试，使原先被跳过的 DB 集成用例真正跑通。
- README「模块边界与依赖方向」：明确 `agent/`、`mcp_server/server.py` 与领域工具层的职责与依赖方向，避免后续重复实现。

### Fixed（承接 V0.4 多轮补缺口）
- `get_schema` 输出完整语义 DTO（表级元数据/外键/索引/列枚举），并按 ACL 裁剪可见列与被引用表/列，避免外键泄露无权对象。
- `database://schema`、`database://table/{table_name}` 返回完整 DTO；Agent 预取不再丢弃表描述/键。
- `search_schema` 覆盖物理表/列名，`deep=true` 全库列扫描（对含语义命中的表也照常扫描列）。
- `load_semantics` 对畸形 YAML、非法 UTF-8、非 dict/错误类型字段安全降级。
- 语义元数据按 YAML mtime 自动热重载；删除配置文件即清空旧语义。

## [v0.4.0] - 2026-09-12

### Added
- **Schema Intelligence**：新增 `catalog/semantic.py`，从外置 `configs/schema_desc.yaml` 加载业务语义（表/列业务名、用途、口径、同义词、枚举）。
- `TableSchema`/`ColumnSchema` 新增 `label`/`description`/`synonyms`/`enum_values`，`SchemaCatalog` 融合语义并走 TTL 缓存。
- 新增 MCP Tool `search_schema(keyword)`（轻量别名检索，按 `schema:read` + 表/列 ACL 裁剪）。
- `get_schema` 与资源输出附带业务描述。

## [v0.3.0] - 2026-09-09

### Added
- 请求级权限系统：Bearer/JWT 认证 → Principal → RBAC → 表/列 ACL → Row-Level Security → SQL 重写 → 审计。
- `configs/permissions.yaml` 外置授权策略；MySQL 版版本表 + active 指针 + 热加载。
- 策略管理工具集（validate / publish / list / export / status / reload）。

## [v0.2.0] - 2026-09-06

### Added
- 基于 sqlglot AST 的只读安全管线（三层职责分离）：读语句白名单 + 单语句 + 表/列 Allowlist + JOIN 上限 + 系统库封禁 + 行数/字节/长度/并发限制。
- `SchemaCatalog` 结构与缓存、探针与可观测性基础。

## [v0.1.0] - 2026-09-03

### Added
- 初始版本：SQL Agent（ReAct + function calling）+ MySQL MCP Server 最小闭环。
