# SQL Agent + MySQL MCP Server

![release](https://img.shields.io/github/v/release/1940633231/sql-mcp-agent)

> **当前版本：v0.7.0（Observability Hardening）**；V0.4 Schema Intelligence + 契约锁定 + CI → V0.5 Domain Query Layer → V0.6 Agent Reliability → V0.7 可观测性加固

一个通过 **MCP（Model Context Protocol）** 把数据库能力封装成工具、并用 **Agent** 自然语言查询 MySQL 的学习型项目。按「生产级 MCP」分层设计：数据访问是标准 MCP Server，安全防线独立成模块、策略外置为 YAML，Agent 动态发现工具、由大模型决定调用哪个工具解题。

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

```
┌──────────────────────────────┐
│  agent/                       │   自然语言问题
│   agent.py   ReAct 主循环     │─────────┐
│   client.py  MCP 会话 / 适配  │         │ ① 发现工具 (list_tools) + schema
│   config.py  LLM + 端点       │         │ ② 调用工具 (call_tool)
└──────────────┬───────────────┘         │
               │                          │
┌──────────────▼───────────────┐         │
│  mcp_server/                  │  python -m mcp_server.server
│   server.py    组装工具+资源   │   默认 Streamable HTTP（常驻服务）
│   tools/       schema / query │   可切 stdio（MCP_TRANSPORT=stdio）
│   security/    parser/policy/ │
│                validator/     │  ← 只读安全防线（策略见 configs/security.yaml）
│                models         │
│   database/    connection/    │
│                executor       │
│   config.py    DB + 传输参数   │
└──────────────┬───────────────┘
               │ ③ 连接
┌──────────────▼───────────────┐
│  MySQL sales_demo            │  独立业务库：company / sale_records
└──────────────────────────────┘
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
| Tool | `search_schema(keyword)` | 按关键字检索表/列的业务语义（业务名/别名/同义词/描述） |
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

## 稳定 API 契约（V0.5）

以下对外接口为 **v0.5.0 基线冻结**，任何增删改须显式走版本演进并同步更新 `tests/test_api_contract.py`（改动即回归失败）。

- **Tool（13 个）**：`list_tables` / `get_schema(table_name)` / `search_schema(keyword, deep=false)` / `run_query(sql)` / `sales_summary(start_date, end_date, granularity, company_id, limit)` / `company_ranking(start_date, end_date, industry, sort_by, order_dir, limit)` / `industry_analysis(start_date, end_date, industry, sort_by, order_dir, limit)` / `get_policy_status` / `reload_permission_policy` / `validate_permission_policy(document)` / `publish_permission_policy(document, expected_version)` / `list_permission_policy_versions(limit)` / `export_permission_policy`。
- **Resource（2 个）**：`database://schema`、`database://table/{table_name}`（均返回完整语义 DTO：表级 `label/description/primary_keys/foreign_keys/indexes` + 列 `name/type/label/description/synonyms/enum_values`）。
- **权限动作**：`schema:list` / `schema:read` / `query:run` / `policy:read|validate|publish|rollback`。
- **错误结构**：`run_query` / `get_schema` 与三个领域工具返回统一 dict；出错形如 `{"error": str, "code": str}`（领域工具另有 `invalid_parameter` / `unknown_domain_tool`）；查询成功含 `rows` / `columns` / `row_count` / `query_ms`。
- **领域工具成功 DTO**：`data` / `columns` / `meta`（`tool` / `parameters` / `semantics` / `row_count` / `elapsed_seconds` / `result_bytes` / `truncated_reason`）/ `definition_version` / `policy_version` / `truncated`。

### Domain Query Layer（V0.5）

- 定义由服务端维护在 `configs/domain_queries.yaml`（SQL 模板 / 参数定义 / 结果契约 / 领域口径），以内容哈希生成 `definition_version` 随 DTO 返回；按 mtime + TTL 自动重载。
- **参数绑定**：值参数（日期 / 整数 / 枚举）先做类型与范围校验，再以 `?` 占位符经数据库驱动绑定执行，值文本绝不进入 SQL；排序 / 分组 / 时间粒度等标识符只能取 `identifiers` 白名单映射的固定片段。
- **口径显式定义**：`sales_summary`（时间粒度 / 时区 / 销售额口径 / 退款与取消不计入）、`company_ranking`（排名指标 / 时间窗口 / 并列按 company_id 升序 / limit ≤ 50）、`industry_analysis`（行业维度 / 未知行业拒绝 / 占比分母与覆盖范围）。
- **安全一致性**：领域工具不直接访问数据库，统一经 QueryService（AST Guard / RBAC / ACL / RLS / LIMIT / 审计 / 指标），任意主体的越权与行级过滤行为与 `run_query` 完全一致。

## 模块边界与依赖方向

三层划分明确所属，避免能力重复实现：

```text
agent/（LLM 编排层）
  └─ 提示 / 工具发现 / function-calling 主循环；唯一 MCP 客户端入口；不做 SQL 安全与授权
server.py（传输/组装层）
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

约束（各层"不做什么"）：
- `agent/` 不得 import `mcp_server` 内部的 security / authorization；只经由公开的 MCP 端点和工具交互。
- `tools/` 不实现安全判定与授权决策，只校验参数并调用下层。
- `domain/` 只做模板渲染与 DTO 包装，不直接访问数据库；安全判定全部委托 QueryService。
- `catalog/` 不做表/列级授权，可见性裁剪统一由 `tools/` 层依据 `authorization` 完成。
- `database/` 不判断 SQL 是否合法，只负责执行、驱动参数绑定与结果限量。
- 新增领域能力时，按此边界落到对应包；能力归属不清时优先 `services/` 编排、实体进 `catalog/models`。

## 快速开始

### 1. 环境准备

```bash
# Python 3.12+
python -m venv .venv
.venv\Scripts\activate        # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置连接

```bash
cp .env.example .env          # 然后编辑 .env 填入数据库密码 与 LLM API Key
```

### 3. 建库造数

```bash
python scripts/db_init.py --reset   # 创建 sales_demo 库、建表、随机造数
python scripts/db_init.py --permission-fixtures  # 补充固定多主体权限测试数据
```

### 4. 启动 MCP Server（HTTP 常驻）

```bash
python -m mcp_server.server
# 监听 http://127.0.0.1:8000/mcp（可用 MCP_HOST / MCP_PORT / MCP_PATH 覆盖）
```

启用静态 Bearer Token 时，在 `.env` 中配置：

```bash
AUTH_MODE=static
AUTH_TOKENS_JSON={"alice-token":"alice"}
MCP_AUTH_TOKEN=alice-token
```

服务端验证 token 并映射到 `configs/permissions.yaml` 中的 principal；Agent 会在 MCP HTTP 请求中发送 `Authorization: Bearer ...`。

启用外部 OAuth 2.1 / JWT 时：

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
python -m mcp_server.policy_admin publish configs/permissions.yaml --actor admin --reason v0.3
```

### 5. 跑 Agent

```bash
# 方式一：直接问答（另开一个终端）
python -m agent.agent "今年销售额最高的10家公司"

# 方式二：打印工具调用过程（看 Agent 一步步调了哪些工具、SQL、结果）
python -m agent.agent "各行业今年的销售总额排名" --trace
```

> 包内使用相对导入，请在**项目根目录**用 `python -m ...` 方式运行。

## 目录结构

```
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
    domain.py           sales_summary / company_ranking / industry_analysis（领域工具，委托 DomainQueryService）
    policy_admin.py     策略状态、热加载、版本列表、导出与发布工具
  domain/               V0.5 领域查询层
    models.py           QueryTemplate / QuerySpec / ParameterSpec（模板、参数、结果契约）
    binder.py           参数绑定：白名单内联 + 类型校验 + ? 占位符
    registry.py         configs/domain_queries.yaml 加载与 mtime 重载
    service.py          编排：渲染 → QueryService → 统一 DTO（data/columns/meta/版本/truncated）
  services/
    query_service.py    编排层：安全校验 → RBAC/ACL/RLS → LIMIT → 执行 → 错误收敛（V0.7：延迟成功/失败 + 错误率 + Span）
  auth/                  认证：Principal / RequestContext / 静态 Token / OAuth 2.1 JWT
  catalog/               SchemaCatalog：表/列/键/索引缓存
                        + semantic.py：业务描述加载与轻量语义检索
  observability/         Trace ID / Audit sink / Metrics / Health / Lifecycle / Alerts（V0.7 加固）
    tracing.py           Server 侧 Span 封装（绑定 TraceContext，跨线程延续父子链）
    metrics.py           有界直方图 + 标签卫生（禁止原始 SQL/高基数）
    alerts.py            进程内告警引擎 + Dashboard 信号聚合
    health.py            /healthz /readyz /metrics /traces /dashboard /alerts 响应构造
  authorization/         授权：RBAC / ACL / 列可见性 / RLS / SQL 策略改写 / 审计
                        + Schema/Semantic/Security Validator / Policy DB / 事务发布
                        + 策略版本存储与热加载 / CTE 列血缘 / JSON 审计（V0.7：异步+失败策略+保留+归档）
  security/             只读安全防线
    models.py           数据模型（策略/解析结果/校验结果/查询结果）
    parser.py           基于 sqlglot 的 AST 解析（表/列/函数/JOIN/子查询深度）
    policy.py           策略加载（YAML）与规则匹配
    validator.py        校验流水线 → ValidationResult（语句/表/列 ACL/JOIN 上限）
  database/
    connection.py       pymysql 连接与游标上下文
    executor.py         只读查询执行（超时 + 行数 + 字节上限 + 驱动参数绑定 + Database Span）
  policy_admin.py       策略管理 CLI：validate/status/publish/list/export

configs/security.yaml   安全策略（只读白名单/系统库/表列 Allowlist/JOIN/成本限制）
configs/permissions.yaml 权限策略（角色、主体、表/列 ACL、行级策略）
configs/schema_desc.yaml 业务语义元数据（表/列业务名、用途、口径、同义词、枚举）
configs/domain_queries.yaml V0.5 领域查询定义（SQL 模板/参数/结果契约/领域口径）
configs/alerts.yaml      V0.7 进程内告警规则（阈值/窗口/严重度）
configs/prometheus/alerts.yml  V0.7 外部 Prometheus 告警规则（与 /metrics 对齐）
configs/grafana/dashboard.json V0.7 Grafana 面板（错误率/P95/P99/超时率/工具失败率/池饱和度）

tests/                  单元测试（pytest）
  test_sql_parser.py    解析：拆分、分类、抽表名、标识符
  test_sql_policy.py    策略：加载与关键字/正则/系统库匹配
  test_sql_validator.py 校验流水线：放行与各类拒绝
  test_query.py         run_query：校验拦截、LIMIT 补全、错误收敛
  test_authorization.py RBAC/ACL/列展开/RLS/schema 裁剪/静态认证
  test_policy_manager.py 策略序列化、版本存储与热加载
  test_policy_validation.py Schema/Semantic/Security 校验
  test_policy_db.py      策略库连接隔离与事务边界
  test_policy_admin.py   策略管理工具权限与发布流程
  test_permission_integration.py 真实 MySQL 多主体与 CTE 集成测试
  test_schema_semantic.py  Schema Intelligence：语义加载/检索/查询模型/ACL 裁剪
  test_domain_query.py    V0.5 领域查询：参数白名单/驱动绑定/RLS 一致性/DTO/截断超时/DB 集成
  test_observability_v07.py V0.7 有界直方图/标签卫生/traceparent+Span/告警/审计

scripts/                辅助脚本
  db_init.py            建库、建表、造数
  policy_db_init.py     初始化独立策略库
  migrate_policy_db.py  从业务库迁移历史策略版本
  batch_test.py         8 类问题批量回归 + 恶意 SQL 拦截测试

examples.md             已验证问题、SQL 与结果、8 类测试报告
.env.example            配置模板（键名 + 占位，无真实密钥）
```

## 测试

```bash
python -m pytest -q                      # 全部单元测试（不依赖数据库）
python -m pytest tests/test_sql_validator.py -q
python -m pytest tests/test_authorization.py -q

# 可选：连真实数据库的集成用例（默认跳过）
$env:RUN_DB_TESTS=1; python -m pytest tests/test_query.py -q   # PowerShell
$env:RUN_DB_TESTS=1; python -m pytest tests/test_permission_integration.py -q   # 多主体验收
$env:RUN_DB_TESTS=1; python -m pytest tests/test_domain_query.py -q            # 领域工具验收
```

## 生产化要点

- **只读三层纵深**：Agent 约定只读 → MCP Server 基于 AST 的 Allowlist 校验（语句 / 表 / 列 ACL / JOIN 上限 / 系统库封禁 / 行数 + 字节 + 长度 + 并发）→ 数据库账号建议只授 SELECT 权限
- **请求级权限**：Bearer Token → Principal → RBAC/ACL/RLS；角色与主体属性只从服务端策略解析，不接受模型或工具参数自报身份
- **控制面隔离**：`query_admin`、`policy_admin` 分离；全局 `*`、控制面权限与查询权限混用会被 Validator 拒绝
- **JWT 默认策略模式**：`AUTH_PRINCIPAL_MODE=policy`；claims 模式必须通过服务端角色映射，原始 JWT 角色不能升级为 admin
- **独立 Policy DB**：策略库使用 `POLICY_DB_*`，业务库账号不再需要策略表写权限
- **原子发布**：active 行锁、版本写入、指针切换和审计在同一事务提交；支持 `expected_version` 防止覆盖
- **授权后再校验**：RLS 与 `SELECT *` 展开后再次经过 AST 安全校验，避免策略改写引入新的越权面
- **策略外置**：安全规则集中在 `configs/security.yaml`，收紧/放宽无需改代码；`mcp_server/security/` 负责解析与判定
- **校验先于连库**：`run_query` 先校验再建连，恶意 SQL 在无数据库时也会被直接拒绝
- **凭证隔离**：连接串、Key 全部走 `.env`，代码零硬编码；`.env` 已被 `.gitignore` 拦截
- **版本注意**：本项目用 mcp **2.x** 的 `MCPServer`（v1 的 `FastMCP` 已改名，勿用旧教程 API）
- **策略生命周期**：MySQL 版本表保存 document，active 指针原子切换；查询前按 TTL 检查并支持手动强制重载
- **连接池与 SchemaCatalog**：Business/Policy 独立连接池，SchemaCatalog 提供 TTL 缓存和表/列/主键/外键/索引接口
- **Schema Intelligence（V0.4）**：业务语义外置在 `configs/schema_desc.yaml`（无数据库只读账号写权限依赖、纯内存加载）；`search_schema` 先过 `schema:read` 权限，再按表/列 ACL 裁剪结果——敏感列的**描述本身**也不会泄露
- **Domain Query Layer（V0.5）**：领域工具不直接访问数据库；模板与口径外置 `configs/domain_queries.yaml`（mtime 重载 + `definition_version` 哈希）；值参数类型/范围校验后经驱动绑定，排序/分组/粒度标识符只走白名单；RLS 改写发生在统一 QueryService 内，任意主体行为与 `run_query` 一致
- **审计**：默认 JSON 日志，可通过 `AUDIT_STORE=mysql` 写入策略库；V0.7 支持后台异步批量落库、失败策略（ignore/warn/fail）、保留周期与归档表；`AUDIT_REQUIRED=true` 时同步保证必然入库；包含 policy_version、trace_id、request_id、SQL fingerprint、耗时和结果统计
- **运行态接口**：`/healthz`、`/readyz`、`/metrics`（Prometheus），支持容器探针和 Prometheus 采集；V0.7 新增 `/traces`（最近 span）、`/dashboard`（运行态概览）、`/alerts`（告警状态）
- **可观测性（V0.7）**：共享 `telemetry` 内核提供 OpenTelemetry 数据模型 + W3C `traceparent` 跨服务传播，Agent → MCP → Authorization → Database 四层 Span 落在同一条 trace；SQL 延迟成功/失败都记录，错误率按错误码 × 工具 × 主体类型聚合；Metrics 用有界直方图（固定分桶）避免内存无限增长，标签卫生禁止原始 SQL/完整用户输入/高基数字段；进程内告警引擎评估错误率 / P95/P99 延迟 / 超时率 / 工具失败率 / 连接池饱和度；配套 Grafana 面板 `configs/grafana/dashboard.json` 与 Prometheus 告警规则 `configs/prometheus/alerts.yml`
- **优雅退出**：停止接收新请求、等待在途请求排空、排空审计队列、关闭连接池后退出

## 已知边界

- 关键字黑名单是**粗粒度**防线，只应对常规风险；生产环境仍应以数据库最小权限（只读账号、禁止 UDF/`LOAD_FILE` 等）为根本
- 解析基于 `sqlglot` 的 AST，表/列/函数/JOIN 取自真实语法结构而非字符串匹配；`@@version` 这类符号 token AST 不可见，故保留 `forbidden_patterns` 作辅助防线兜底
- V0.3 已支持普通 CTE、嵌套 CTE 与派生表的列血缘；无法完整解析血缘时仍 fail closed
- 静态 Bearer Token 用于本地测试；生产认证应使用 `AUTH_MODE=jwt` 接入外部 OAuth 2.1 / JWKS

## License

示例代码，MIT。
