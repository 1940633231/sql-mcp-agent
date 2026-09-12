# Changelog

本项目的版本演进记录。遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定，
版本号遵循语义化版本（SemVer）。

## [Unreleased]

（暂无）

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

## [v0.3.1] - 2026-09-10

### Added
- Policy Control Plane Hardening：控制面/查询面隔离、Schema/Semantic/Security 校验、策略发布原子化加固。

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