# SQL Agent + MySQL MCP Server

一个通过 **MCP（Model Context Protocol）** 把数据库能力封装成工具、并用 **Agent** 自然语言查询 MySQL 的学习型项目。面向「生产级 MCP」设计：数据访问做成标准 MCP Server，Agent 动态发现工具、由大模型决定调用哪个工具解题。

## 它能做什么

输入一句自然语言，Agent 自动完成：发现工具 → 查表结构 → 生成只读 SQL → 执行 → 汇总答案。

- 简单查询、多表 JOIN、聚合统计、排序 + LIMIT
- SQL 错误自动修正
- 识别不存在的字段 / 表，不编造
- 拦截恶意 / 危险 SQL（只读白名单 + 单语句校验 + 危险关键字黑名单 + 行数上限 + 执行超时）

## 架构

```
┌─────────────────────────┐
│  SQL Agent (sql_agent)  │   自然语言问题
│  LLM function calling   │─────────┐
└─────────────┬───────────┘         │
              │ ① 发现工具 (list_tools) + schema
              │ ② 调用工具 (call_tool)
┌─────────────▼───────────┐
│  MySQL MCP Server       │  mcp_server.py（mcp 2.x MCPServer）
│  list_tables            │   默认 Streamable HTTP（常驻服务）
│  get_schema             │   可切 stdio（MCP_TRANSPORT=stdio）
│  run_query (只读SELECT) │
│  database://schema      │   Resource：全库结构，URI 寻址，供应用预取
│  database://table/{名}  │   Resource 模板：单表结构
└─────────────┬───────────┘
              │ ③ 连接
┌─────────────▼───────────┐
│  MySQL sales_demo       │  独立业务库：company / sale_records
└─────────────────────────┘
```

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
python db_init.py --reset     # 创建 sales_demo 库、建表、随机造 40 家公司 + 数千条销售流水
```

### 4. 启动 MCP Server（HTTP 常驻）

```bash
python mcp_server.py
# 监听 http://127.0.0.1:8000/mcp（可用 MCP_HOST / MCP_PORT / MCP_PATH 覆盖）
```

### 5. 跑 Agent

```bash
# 方式一：直接问答（另开一个终端）
python sql_agent.py "今年销售额最高的10家公司"

# 方式二：打印工具调用过程（看 Agent 一步步调了哪些工具、SQL、结果）
python sql_agent.py "各行业今年的销售总额排名" --trace
```

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `mcp_server.py` | MySQL SQL MCP Server（list_tables / get_schema / run_query），含安全防护 |
| `sql_agent.py` | Agent：MCP 客户端动态发现工具 + LLM function calling（支持 `--trace`） |
| `db_init.py` | 创建独立 `sales_demo` 库、建表、造测试数据 |
| `config.py` | 从 `.env` 读取 DB / LLM 配置 |
| `test_mcp.py` | MCP Server 端到端验证（不依赖 LLM） |
| `batch_test.py` | 8 类问题批量回归 + 恶意 SQL 拦截测试 |
| `examples.md` | 已验证问题、SQL 与结果、8 类测试报告 |
| `.env.example` | 配置模板（键名 + 占位，无真实密钥） |

## 项目文件

- `requirements.txt`：`mcp`、`pymysql`、`openai`、`python-dotenv`

## 生产化要点

- **只读三层纵深**：Agent 约定只读 → MCP Server 白名单 / 单语句 / 危险关键字黑名单 / 行数上限 / 执行超时 → 数据库账号建议只授 SELECT 权限
- **凭证隔离**：连接串、Key 全部走 `.env`，代码零硬编码；`.env` 已被 `.gitignore` 拦截
- **版本注意**：本项目用 mcp **2.x** 的 `MCPServer`（v1 的 `FastMCP` 已改名，勿用旧教程 API）
- **传输**：默认 Streamable HTTP，Server 常驻独立进程，可多客户端共享、便于部署与鉴权；设 `MCP_TRANSPORT=stdio` 可回到子进程模式

## 已知边界

- 关键字黑名单是**粗粒度**防线，只应对常规风险；生产环境仍应以数据库最小权限（只读账号、禁止 UDF/`LOAD_FILE` 等）为根本
- 拦截名单为关键字存在性匹配，极端场景（分号等价编码、大小写混布）建议结合白名单语法解析或预编译语句参数化更稳妥

## License

示例代码，MIT。