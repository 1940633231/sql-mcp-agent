# SQL Agent + MySQL MCP Server

[![Release](https://img.shields.io/github/v/release/1940633231/sql-mcp-agent)](https://github.com/1940633231/sql-mcp-agent/releases)
[![CI](https://github.com/1940633231/sql-mcp-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/1940633231/sql-mcp-agent/actions/workflows/ci.yml)

> **当前版本：v0.8.0（标准可观测性导出 + CI 安全扫描 + 审计落盘）**；V0.4 Schema Intelligence + 契约锁定 + CI → V0.5 Domain Query Layer → V0.6 Agent Reliability → V0.7 可观测性加固 → v0.8.0 工程化收尾（Metrics 标准导出、Alertmanager 外置、零基础设施审计落盘、CodeQL/Trivy/Dependabot 安全门禁）。MCP Tool 契约仍以 v0.5.0 兼容基线为准。

企业级 SQL Agent + MCP 数据访问平台：基于 MCP 构建 Agent 与数据库之间的标准化工具调用层，引入 SQL AST 安全校验、RBAC/表列 ACL、RLS 行级权限、领域查询工具及 Schema Intelligence，限制 LLM 对数据库的直接访问；同时实现 Agent 超时/重试/调用预算、统一错误码、OpenTelemetry Trace、Prometheus Metrics、审计及 CI 安全扫描，形成从 Agent → MCP → 权限 → SQL → 数据库的完整治理链路。

一个通过 **MCP（Model Context Protocol）** 把数据库能力封装成工具、并用 **Agent** 自然语言查询 MySQL 的学习型 Reference Implementation。数据访问是标准 MCP Server，安全防线独立成模块，策略外置为 YAML，Agent 动态发现工具并调用受治理的领域能力。

本项目已经覆盖 Agent 可靠性、权限控制和可观测性，但默认配置不等同于可直接公开部署的生产系统；部署前请阅读「生产化要点」和「已知边界」。

## 它能做什么

输入一句自然语言，Agent 自动完成：发现工具 → 查表结构 → 生成只读 SQL → 执行 → 汇总答案。

- 简单查询、多表 JOIN、聚合统计、排序 + LIMIT
- SQL 错误自动修正
- 识别不存在的字段 / 表，不编造
- 拦截恶意 / 危险 SQL（基于 sqlglot AST 的校验管线：只读语句白名单 + 单语句 + 表/列 Allowlist + JOIN 上限 + 系统库封禁 + 行数/字节/长度/并发成本限制；关键字黑名单仅作辅助防线）
- 请求级权限：认证 Principal → RBAC → 表/列 ACL → Row-Level Security → SQL 重写 → 审计
- 受限用户查询 `SELECT *` 时自动展开可见列；schema 工具与 Resource 也只返回当前主体可见的表和列
- Schema Intelligence：为表/列补充业务名、用途、口径与同义词（外置 `configs/schema_desc.yaml`），提供轻量语义检索 `search_schema`，让 Agent 生成 SQL 前先理解字段语义、减少幻觉列名
- Domain Query Layer（V0.5）：`sales_summary` / `company_ranking` / `industry_analysis` 三个领域工具，模板与领域口径由服务端维护（`configs/domain_queries.yaml`），值参数经数据库驱动绑定、排序/分组/时间粒度等标识符只能取白名单；Agent 优先调用领域工具，复杂问题才回退 `run_query`
- Agent Reliability（V0.6）：`AsyncOpenAI` + 连接/读取/单次调用超时 + 全局请求截止时间；临时错误退避重试（仅只读调用）、永久错误立即终止；统一预算（迭代/工具调用/SQL修复/Token/费用上限）杜绝无限循环与无限等待；SQL 修复仅针对语法/字段错误，权限/超时/危险 SQL 不进入盲重试；每次结束都返回稳定错误码（`success`/`timeout`/`model_error`/`tool_error`/`sql_syntax_error`/`permission_denied`/`budget_exceeded`）与用户可理解信息及运行指标（迭代数/工具调用数/修复次数/耗时/最终状态）
- Observability Hardening（V0.7）：接入共享 `telemetry` 内核（OpenTelemetry 数据模型 + W3C `traceparent` 传播 + 有界内存缓冲 + 可选 OTLP/HTTP 导出），让 Agent → MCP → Authorization → Database 四层落在同一条 trace；任意一次请求都能靠 `request_id` 与 `trace_id` 跨服务完整定位。SQL 延迟**成功/失败都记录**，错误率按错误码、工具、主体类型聚合；Metrics 改为**有界直方图**（固定分桶，不再在内存保留全部样本），指标标签卫生化（禁止原始 SQL / 完整用户输入 / 高基数字段）。审计支持**异步落库、失败策略、保留周期与归档**。内置 `/dashboard`、`/alerts`、`/traces` 端点与进程内告警引擎，并配套 Grafana 面板与 Prometheus 告警规则（错误率 / P95/P99 延迟 / 超时率 / 工具失败率 / 连接池饱和度）

## 架构

```text
agent/
  agent.py             ReAct / function-calling 主循环 + 可靠性预算
  client.py            MCP Session + traceparent 注入
  reliability.py       超时、重试、错误码、RunResult

              │ MCP Streamable HTTP / stdio
              ▼

mcp_server/
  server.py            工具/Resource 注册、HTTP/stdio、探针与管理端点
  tools/               list_tables / get_schema / search_schema
                       run_query / 领域工具 / 策略管理工具
  services/            QueryService：安全 → 授权 → LIMIT → 执行 → 审计/指标
  domain/              QueryTemplate / QuerySpec / 安全参数绑定 / 领域 DTO
  security/            sqlglot AST / Security Policy / Validator
  authorization/       RBAC / ACL / RLS / SQL 重写 / Policy DB / Audit
  auth/                Bearer / JWT / Principal / RequestContext
  catalog/             SchemaCatalog / 业务语义检索
  database/            连接池 / 参数绑定 / 只读执行
  observability/       Metrics / Trace / Alerts / Health / Lifecycle

telemetry/             共享 Span 模型 / W3C traceparent / OTLP 导出

              │ SQL
              ▼

MySQL                  sales_demo：company / sale_records
```

### SQL 安全架构（V0.2）

![SQL 安全重构 V0.2 目标架构](exported_image.png)

- **① 三层职责分离**：Tool 层只负责 MCP 协议；Security 层只回答「这条 SQL 能不能执行」；Database 层只负责执行。
- **② 校验管线基于 AST**：SQL → Parser → AST（表 / 列 / JOIN / 子查询 / LIMIT）→ Statement Type → Policy → ValidationResult，结构提取后再比对，避免字符串匹配的误判与绕过。
- **③ Allowlist 优先于 Denylist**：不是「发现危险才拒绝」，而是「符合安全 Policy 才允许执行」。

### 权限架构（V0.3）

```text
MCP Request
  -> Authentication（Bearer Token / 本地服务身份）
  -> 外部 OAuth 2.1 / JWT JWKS 或静态 Token
  -> Principal（subject / roles / attributes）
  -> RBAC（query:run / schema:read）
  -> Table ACL + Column ACL
  -> Row-Level Security（AST 条件注入）
  -> CTE / 派生表列血缘与结果列二次防护
  -> SQL 二次安全校验
  -> QueryExecutor
```

- 策略集中在 `configs/permissions.yaml`，角色、主体、ACL、RLS 均不写死在 SQL 中。
- Policy DB 与 Business DB 使用独立连接和账号；MySQL 发布在单事务内完成 active 行锁、版本写入、指针切换和审计。
- 发布前执行 Schema / Semantic / Security Validation；当前禁止全局 `*`、控制面与查询面混用。
- 默认 `AUTH_PRINCIPAL_MODE=policy`；JWT claims 必须经过服务端角色映射，不能直接获得 query/policy admin。
- JWT 模式校验签名、issuer、audience、exp，可通过 JWKS 或静态公钥加载密钥。
- 决策采用 `deny > allow > 默认拒绝`；受 RLS 保护的表没有适用策略时直接拒绝。
- 表结构工具同样经过授权：不可见表不返回，列级 ACL 隐藏的字段不会出现在 schema 结果中。
- `AUTH_MODE=disabled` 保持本地开发体验；`AUTH_MODE=static` 可用 Bearer Token 验证完整认证链。

MCP 对外能力：

| 类型 | 名称 | 说明 |
| --- | --- | --- |
| Tool | `list_tables` | 列出业务库所有表 |
| Tool | `get_schema(table_name)` | 查看某表完整结构 DTO（表级描述/主键/外键/索引 + 列含业务描述/枚举），按 ACL 裁剪可见列与被引用表/列 |
| Tool | `search_schema(keyword, deep=false)` | 按关键字检索表/列的业务语义（业务名/别名/同义词/描述）；`deep=true` 全库扫描 |
| Tool | `run_query(sql)` | 执行只读 SQL（仅单条 SELECT），返回行数据 |
| Tool | `sales_summary(start_date, end_date, granularity, company_id, limit)` | 按时间粒度汇总销售额/订单数/客单价（时间粒度/时区/销售额口径显式定义） |
| Tool | `company_ranking(start_date, end_date, industry, sort_by, order_dir, limit)` | 按销售额/单量/客单价对公司排名（并列按 company_id 升序，limit ≤ 50） |
| Tool | `industry_analysis(start_date, end_date, industry, sort_by, order_dir, limit)` | 按行业聚合销售额/单量/公司数及占比（分母为窗口内全部行业销售额，受 RLS 约束） |
| Tool | `get_policy_status()` | 查看 active 权限策略版本与来源 |
| Tool | `reload_permission_policy()` | 强制热重载策略 |
| Tool | `validate_permission_policy(document)` | 校验策略但不发布（需 `policy:validate`） |
| Tool | `publish_permission_policy(document, expected_version)` | 事务发布新策略版本（需 `policy:publish`） |
| Tool | `list_permission_policy_versions(limit)` | 查看策略版本历史 |
| Tool | `export_permission_policy()` | 导出当前策略 |
| Resource | `database://schema` | 全库各表完整结构 DTO（含表描述/键/外键/索引/枚举，应用预取，省钱省轮次） |
| Resource | `database://table/{table_name}` | 单表完整结构 DTO（URI 模板资源） |

## 稳定 API 契约（V0.5 兼容基线，当前实现 v0.7.0）

V0.5.0 冻结了以下 MCP Tool 和 Resource 契约。V0.6、V0.7 增加了 Agent 可靠性和可观测性，但没有破坏既有 MCP Tool 名称与参数。任何增删改仍须走版本演进，并同步更新 `tests/test_api_contract.py`。

- **Tool（13 个）**：`list_tables` / `get_schema(table_name)` / `search_schema(keyword, deep=false)` / `run_query(sql)` / `sales_summary(start_date, end_date, granularity, company_id, limit)` / `company_ranking(start_date, end_date, industry, sort_by, order_dir, limit)` / `industry_analysis(start_date, end_date, industry, sort_by, order_dir, limit)` / `get_policy_status` / `reload_permission_policy` / `validate_permission_policy(document)` / `publish_permission_policy(document, expected_version)` / `list_permission_policy_versions(limit)` / `export_permission_policy`。
- **Resource（2 个）**：`database://schema`、`database://table/{table_name}`（均返回完整语义 DTO：表级 `label/description/primary_keys/foreign_keys/indexes` + 列 `name/type/label/description/synonyms/enum_values`）。
- **权限动作**：`schema:list` / `schema:read` / `query:run` / `policy:read|validate|publish|rollback`。
- **查询错误结构**：`run_query`、`get_schema` 与三个领域工具出错时返回 `{"error": str, "code": str}`。
- **查询成功结构**：`rows` / `columns` / `row_count` / `elapsed_seconds` / `truncated` / `result_bytes` / `truncated_reason`。
- **领域工具成功 DTO**：`data` / `columns` / `meta`（`tool` / `parameters` / `semantics` / `row_count` / `elapsed_seconds` / `result_bytes` / `truncated_reason`）/ `definition_version` / `policy_version` / `truncated`。

### Domain Query Layer（V0.5）

- 定义由服务端维护在 `configs/domain_queries.yaml`（SQL 模板 / 参数定义 / 结果契约 / 领域口径），以内容哈希生成 `definition_version` 随 DTO 返回；按 mtime + TTL 自动重载。
- **参数绑定**：值参数（日期 / 整数 / 枚举）先做类型与范围校验，再以 `?` 占位符经数据库驱动绑定执行，值文本绝不进入 SQL；排序 / 分组 / 时间粒度等标识符只能取 `identifiers` 白名单映射的固定片段。
- **口径显式定义**：`sales_summary` 当前表没有退款/取消状态字段，所有销售记录均视为有效成交并全额计入；`company_ranking` 按指定指标排序并以 `company_id` 保证唯一顺序；`industry_analysis` 的占比分母始终是窗口内全部行业销售额，不随 `industry` 过滤改变。
- **安全一致性**：领域工具不直接访问数据库，统一经 QueryService（AST Guard / RBAC / ACL / RLS / LIMIT / 审计 / 指标），任意主体的越权与行级过滤行为与 `run_query` 完全一致。

### Agent 返回契约（V0.6/V0.7）

`SQLAgent.run()` 不再返回裸字符串，而是返回 `RunResult`：

```python
from agent.agent import SQLAgent

agent = SQLAgent()
result = await agent.run("今年销售额最高的10家公司")

print(result.status)          # success / timeout / model_error / ...
print(result.answer)          # 成功时为用户答案
print(result.error_message)   # 失败时为稳定错误说明
print(result.to_dict())       # answer + status + 运行指标
```

运行指标包括迭代数、工具调用数、SQL 修复次数、耗时、Token 数量和估算费用。CLI 入口会将这些信息渲染为终端文本。

## 模块边界与依赖方向

三层划分明确所属，避免能力重复实现：

```text
agent/（LLM 编排层）
  └─ 提示 / 工具发现 / function-calling 主循环；唯一 MCP 客户端入口；不做 SQL 安全与授权
mcp_server/server.py（传输/组装层）
  └─ 组装 MCPServer：工具注册、Resource、HTTP/stdio、探针；不实现任何业务判定
mcp_server/（领域层）
  tools/        只做 MCP 协议转换 + 参数校验 + 授权裁剪；不做安全判定
  services/     编排（QueryService：校验→RBAC/ACL/RLS→LIMIT→执行）
  domain/       领域查询层（QueryTemplate/QuerySpec、参数绑定、Registry、统一 DTO）
  security/     只读安全判断（AST 解析 / 策略 / 校验）；不含授权决策
  catalog/      表结构 + 语义元数据缓存；不依赖授权
  authorization/ 授权（RBAC / ACL / RLS / SQL 重写 / 策略版本发布）；不含查询执行
  auth/         认证（Principal / Token / JWT）
  database/     连接池 + 只读执行（含驱动参数绑定）；不含安全/授权
```

依赖方向：`agent → server(HTTP/stdio) → tools → services → security / authorization / catalog → database`。

约束（各层“不做什么”）：

- `agent/` 不得 import `mcp_server` 内部的 security / authorization；只经由公开的 MCP 端点和工具交互。
- `tools/` 不实现安全判定与授权决策，只校验参数并调用下层。
- `domain/` 只做模板渲染与 DTO 包装，不直接访问数据库；安全判定全部委托 QueryService。
- `catalog/` 不做表/列级授权，可见性裁剪统一由 `tools/` 层依据 `authorization` 完成。
- `database/` 不判断 SQL 是否合法，只负责执行、驱动参数绑定与结果限量。
- 新增领域能力时，按此边界落到对应包；能力归属不清时优先 `services/` 编排、实体进 `catalog/models`。

## 快速开始

### 1. 环境准备

需要 Python 3.12+ 和 MySQL 8.0+。业务库建议使用独立的只读账号。

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置连接

```bash
cp .env.example .env
```

`.env.example` 是最小可复制模板。Static/JWT、MySQL Policy Store、Audit、OTLP 和告警配置均以注释形式提供，只在需要对应模式时取消注释。

至少需要确认：

- `LLM_API_KEY` 和模型端点。
- `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE`。
- 本地开发保持 `AUTH_MODE=disabled`；启用认证时设置 `AUTH_MODE` 和 `MCP_AUTH_TOKEN`。

### 3. 建库造数

```bash
python scripts/db_init.py --reset
python scripts/db_init.py --permission-fixtures
```

### 4. 启动 MCP Server

```bash
python -m mcp_server.server
# 默认监听 http://127.0.0.1:8000/mcp
```

启动后可验证：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

启用静态 Bearer Token：

```bash
AUTH_MODE=static
AUTH_TOKENS_JSON={"alice-token":"alice"}
MCP_AUTH_TOKEN=alice-token
```

服务端验证 token 并映射到 `configs/permissions.yaml` 中的 principal；Agent 会在 MCP HTTP 请求中发送 `Authorization: Bearer ...`。

启用外部 OAuth 2.1 / JWT：

```bash
AUTH_MODE=jwt
AUTH_JWT_ISSUER=https://issuer.example
AUTH_JWT_AUDIENCE=http://127.0.0.1:8000/mcp
AUTH_JWT_JWKS_URL=https://issuer.example/.well-known/jwks.json
AUTH_JWT_ALGORITHMS=RS256
MCP_AUTH_TOKEN=<access-token>
```

策略存 MySQL 并启用热加载：

```bash
POLICY_STORE=mysql
POLICY_RELOAD_SECONDS=5
POLICY_DB_HOST=127.0.0.1
POLICY_DB_PORT=3306
POLICY_DB_USER=policy_app
POLICY_DB_PASSWORD=<policy-db-password>
POLICY_DB_DATABASE=mcp_policy
python scripts/policy_db_init.py
python -m mcp_server.policy_admin validate configs/permissions.yaml
python -m mcp_server.policy_admin publish configs/permissions.yaml --actor admin --reason v0.7
```

### 5. 运行 Agent

```bash
python -m agent.agent "今年销售额最高的10家公司"
python -m agent.agent "各行业今年的销售总额排名" --trace
```

> 包内使用相对导入，请在项目根目录使用 `python -m ...` 执行。

## 配置速查

完整键位和默认值见 [`.env.example`](.env.example) 以及 `agent/config.py`、`mcp_server/config.py`。

| 分类 | 关键配置 | 说明 |
| --- | --- | --- |
| Agent | `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | OpenAI 兼容模型端点 |
| Agent | `REQUEST_DEADLINE_SECONDS` | 单个问题总截止时间 |
| Agent | `LLM_MAX_TOOL_ITERATIONS` / `MAX_TOOL_CALLS` / `MAX_SQL_REPAIRS` | 防止无限循环和修复 |
| Agent | `MAX_TOKENS` / `MAX_COST` | Token 和费用预算，0 表示不限制 |
| Agent | `BLOCKED_TOOLS` / `RETRY_SAFE_TOOLS` | 禁用工具与可安全重试的只读工具 |
| MCP | `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | HTTP 或 stdio 传输 |
| DB | `MYSQL_*` | 业务数据库连接 |
| Auth | `AUTH_MODE` / `AUTH_TOKENS_JSON` / `AUTH_JWT_*` | disabled、static 或 JWT |
| Policy | `POLICY_STORE` / `POLICY_DB_*` | file 或 MySQL 策略存储 |
| Schema | `SCHEMA_DESC_PATH` / `DOMAIN_QUERIES_PATH` | 语义和领域定义文件 |
| Audit | `AUDIT_STORE` / `AUDIT_REQUIRED` / `AUDIT_FAILURE` | 日志或 MySQL 审计 |
| Observability | `OTEL_OTLP_HTTP_ENDPOINT` / `OTEL_SAMPLE_RATIO` | OTLP 导出与采样 |
| Alerts | `ALERT_RULES_PATH` / `ALERT_EVAL_SECONDS` | 进程内告警配置 |

## 目录结构

```text
agent/                  Agent 侧
  config.py             LLM 参数 + MCP 端点
  client.py             MCP 会话管理与 MCP→OpenAI 适配（V0.7：注入 traceparent）
  agent.py              SQLAgent（ReAct + function calling）主循环（V0.7：Agent 层 Span）
  reliability.py        超时/退避重试/统一预算/稳定错误码

telemetry/              共享遥测内核（V0.7，纯标准库，Server 与 Agent 共用）
  __init__.py           Span 模型 / W3C traceparent / 有界内存缓冲 / OTLP-HTTP 导出

mcp_server/             Server 侧
  server.py             入口：注册工具/资源、启动传输层、/healthz /readyz /metrics /traces /dashboard /alerts
  config.py             数据库连接 + 传输参数 + 审计/遥测/告警配置
  tools/
    schema.py           list_tables / get_schema / search_schema
    query.py            run_query（仅 MCP 协议转换，委托 QueryService）
    domain.py           sales_summary / company_ranking / industry_analysis
    policy_admin.py     策略状态、热加载、版本列表、导出与发布工具
  domain/               V0.5 领域查询层
    models.py           QueryTemplate / QuerySpec / ParameterSpec
    binder.py           白名单内联 + 类型校验 + ? 占位符
    registry.py         configs/domain_queries.yaml 加载与 mtime 重载
    service.py          渲染 → QueryService → 统一 DTO
  services/
    query_service.py    安全校验 → RBAC/ACL/RLS → LIMIT → 执行 → 错误收敛
  auth/                 Principal / RequestContext / 静态 Token / OAuth 2.1 JWT
  catalog/              SchemaCatalog + 业务语义加载与检索
  observability/        Trace / Metrics / Health / Lifecycle / Alerts；prom_exporter 桥接标准 Prometheus 导出（v0.8）
  authorization/        RBAC / ACL / RLS / SQL 重写 / Policy DB / Audit
  security/             AST 解析 / 策略匹配 / 只读校验
  database/             连接池 + 只读执行 + 参数绑定
  policy_admin.py       策略管理 CLI

configs/security.yaml             安全策略
configs/permissions.yaml          权限策略
configs/schema_desc.yaml          业务语义
configs/domain_queries.yaml       领域查询定义
configs/alerts.yaml               进程内告警规则
configs/prometheus/alerts.yml     Prometheus 告警规则
configs/prometheus/alertmanager.yml  Alertmanager 去重/静默/路由配置（v0.8）
configs/grafana/dashboard.json    Grafana 面板
.github/workflows/                CI 安全门禁：CodeQL / Trivy / Dependabot（v0.8）

tests/                  pytest 测试
scripts/                建库、策略库、迁移和批处理脚本
examples.md             已验证问题和测试报告
.env.example            最小可复制配置模板
```

## 测试

```bash
python -m pytest -q
python -m pytest tests/test_sql_validator.py -q
python -m pytest tests/test_authorization.py -q
```

默认会跳过需要真实 MySQL 的集成用例。设置 `RUN_DB_TESTS=1` 后运行完整测试：

```powershell
$env:RUN_DB_TESTS=1; python -m pytest -q
$env:RUN_DB_TESTS=1; python -m pytest tests/test_query.py -q
$env:RUN_DB_TESTS=1; python -m pytest tests/test_permission_integration.py -q
$env:RUN_DB_TESTS=1; python -m pytest tests/test_domain_query.py -q
```

## 生产化要点

- **只读三层纵深**：Agent 约定只读 → MCP Server AST Allowlist 校验 → 数据库账号只授 SELECT。
- **请求级权限**：Bearer Token → Principal → RBAC/ACL/RLS；不接受模型或工具参数自报身份。
- **控制面隔离**：`query_admin`、`policy_admin` 分离；控制面权限不能混入查询角色。
- **独立 Policy DB**：策略库使用 `POLICY_DB_*`，业务库账号无需策略表写权限。
- **原子发布**：active 行锁、版本写入、指针切换和审计在同一事务提交。
- **授权后再校验**：RLS 与 `SELECT *` 展开后再次经过 AST 安全校验。
- **凭证隔离**：连接串和 Key 使用 `.env` 或 Secret Manager，不进入镜像和仓库。
- **策略外置**：安全规则集中在 `configs/security.yaml`。
- **校验先于连库**：恶意 SQL 在无数据库连接时也会被拒绝。
- **连接池与 SchemaCatalog**：业务库与策略库独立连接池，SchemaCatalog 提供 TTL 缓存。
- **审计**：`AUDIT_STORE=mysql` 支持异步批量落库、失败策略、保留周期和归档；`AUDIT_REQUIRED=true` 会同步写入并向上传播失败；v0.8 起设置 `AUDIT_LOG_FILE` 可将结构化 JSON 审计事件以纯 JSON 独立落盘并按大小轮转（零基础设施，可直接被 Promtail / Fluent Bit 采集）。
- **可观测性**：`telemetry` 提供 W3C `traceparent` 和 OTLP/HTTP JSON 导出；`/metrics` 自 v0.8 起输出官方 `prometheus_client` 标准文本（薄适配层），可直接被 Prometheus 抓取；告警外置配合 `configs/prometheus/alertmanager.yml`。生产环境建议接入 OpenTelemetry Collector、Prometheus、Grafana 和 Alertmanager。
- **CI 安全门禁**：仓库内置 CodeQL、Trivy、Dependabot 工作流，PR/主分支自动执行依赖与代码漏洞扫描。
- **优雅退出**：停止接收新请求、等待在途请求排空、排空审计队列、关闭连接池后退出。

## 已知边界

- 关键字黑名单是粗粒度防线，生产环境仍应以数据库最小权限（只读账号、禁止 UDF/`LOAD_FILE` 等）为根本。
- 解析基于 `sqlglot` AST；`@@version` 这类符号 token AST 不可见，因此保留 `forbidden_patterns` 作为辅助防线。
- 普通 CTE、嵌套 CTE 与派生表列血缘已支持；无法完整解析血缘时 fail closed。
- 静态 Bearer Token 只适合本地测试；生产认证应使用 `AUTH_MODE=jwt` 接入外部 OAuth 2.1 / JWKS。
- `/metrics`、`/traces`、`/dashboard`、`/alerts` 是运维接口，不应直接暴露到公网。部署时必须限制内网访问或由反向代理增加管理认证。
- `/traces` 和 `/alerts` 展示的是当前进程内有界状态；多实例场景应通过 OpenTelemetry Collector、Prometheus 和 Alertmanager 聚合。
- 默认不提供 TLS、HA、Secret Manager、备份和灾备能力，需要由部署平台补齐。
- 本项目不是通用 BI 平台，也不替代数据库自身的权限、审计和备份机制。

## License

示例代码，MIT。
