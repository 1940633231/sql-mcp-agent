# SQL Agent 示例问题

运行方式：

```bash
.venv\Scripts\python.exe -m agent.agent "你的问题"
```

> 说明：以下「已验证」的解答均由 Agent 独立完成（动态发现 MCP 工具 → 生成/执行只读 SQL → 汇总答案），金额等数字已与 `sale_records` 表直接聚合结果交叉核对一致。

---

## 1. 查询今年销售额最高的 10 家公司

```
python -m agent.agent "今年销售额最高的10家公司"
```

Agent 生成的查询逻辑：

```sql
SELECT co.name, ROUND(SUM(s.amount), 0) AS total
FROM sale_records s
JOIN company co ON co.company_id = s.company_id
WHERE YEAR(s.record_date) = 2026
GROUP BY co.company_id, co.name
ORDER BY total DESC
LIMIT 10;
```

结果（Top 10）：

| 排名 | 公司 | 今年销售额(元) |
| --- | --- | --- |
| 1 | 示例实业第33公司 | 13,609,265 |
| 2 | 示例集团第27公司 | 13,386,132 |
| 3 | 示例实业第09公司 | 12,230,773 |
| 4 | 示例实业第07公司 | 12,107,256 |
| 5 | 示例实业第19公司 | 11,494,548 |
| 6 | 示例控股第05公司 | 11,119,758 |
| 7 | 示例实业第32公司 | 10,656,492 |
| 8 | 示例控股第29公司 | 10,616,812 |
| 9 | 示例实业第13公司 | 10,429,726 |
| 10 | 示例集团第12公司 | 9,985,658 |

---

## 2. 按行业汇总今年销售总额并排名

```
python -m agent.agent "按行业汇总今年的销售总额，从高到低给出排名"
```

Agent 生成的查询逻辑：

```sql
SELECT co.industry, ROUND(SUM(s.amount), 2) total
FROM sale_records s
JOIN company co ON co.company_id = s.company_id
WHERE YEAR(s.record_date) = 2026
GROUP BY co.industry
ORDER BY total DESC;
```

结果（从高到低）：

| 排名 | 行业 | 今年销售总额(元) |
| --- | --- | --- |
| 1 | 服装 | 58,038,200.32 |
| 2 | 餐饮 | 30,685,416.56 |
| 3 | 金融 | 30,241,098.84 |
| 4 | 快消 | 25,122,395.83 |
| 5 | 零售 | 23,352,974.62 |
| 6 | 互联网 | 17,709,928.69 |
| 7 | 汽车 | 16,677,367.51 |
| 8 | 能源 | 15,891,336.77 |
| 9 | 家居 | 13,819,364.60 |
| 10 | 医药 | 12,926,969.45 |
| 11 | 地产 | 8,685,195.55 |

---

## 建议自行试跑的问题

下面这些同样只需一句话，Agent 会自动完成工具发现和 SQL 生成：

- 2025年每个季度的销售总额走势
- 员工人数最多的 5 家公司及其行业
- 哪个行业的公司平均销售额最高
- 总部在"北京"的公司今年表现如何
- 2024 年相比 2025 年，各行业销售额的增减幅度
- 成立最早 / 最晚的公司有哪些

---

## 8 类问题测试报告

批处理入口：`scripts/batch_test.py`（Agent 对话用例 + Server 工具级安全用例，带 `--trace` 观察中间过程）。

| # | 类型 | 测试问题 | 结果 |
| --- | --- | --- | --- |
| ① | 简单查询 | 公司表里一共有多少家公司？ | `COUNT(*)` → **40 家** |
| ② | 多表 JOIN | 列出员工大于5000的公司名称及行业，按员工数降序 | 两字段均在 `company` 表，Agent 判断无需 JOIN，正确返回 26 家 |
| ③ | 聚合统计 | 按总部所在城市统计公司数量 | `GROUP BY headquarters`，杭州/武汉并列最多各 9 家 |
| ④ | 排序+LIMIT | 今年销售额最高的5家公司 | 真实 `JOIN sale_records` + `GROUP BY` + `ORDER BY` + `LIMIT 5` |
| ⑤ | SQL 错误自动修正 | 提供的 SQL 缺 `GROUP BY`，让它修正后执行 | 先补上 `GROUP BY co.name` 再执行，Top10 正确 |
| ⑥ | 不存在的字段 | 查询每家公司的 salary 字段 | 检查 schema 后告知无 `salary`，并列出可用字段、给出替代建议 |
| ⑦ | 不存在的表 | 统计 orders 表的记录数 | 明确告知无 `orders`，列出实际存在的 `company`/`sale_records` |
| ⑧ | 恶意/危险 SQL | 见下方明细 | 三层防护全部生效 |

### ⑧ 恶意/危险 SQL 拦截明细（工具级直测）

| 恶意输入 | 结果 |
| --- | --- |
| `DELETE FROM company` | 拦截：`仅允许 SELECT 只读查询` |
| `SELECT * FROM company; DROP TABLE ...`（多语句） | 拦截：`仅允许执行单条 SQL 语句` |
| `... WHERE name='x' OR '1'='1'`（等价永真） | 放行（纯 SELECT，无注入危害），正常返回 40 行 |
| `SELECT * FROM information_schema.tables`（信息泄露） | 拦截：`包含被禁止的关键字` |
| `SELECT LOAD_FILE('/etc/passwd')`（文件读取） | 拦截：`包含被禁止的关键字` |
| `SELECT sys_exec('id')`（UDF/系统调用） | 拦截：`包含被禁止的关键字` |
| `SELECT @@version`（版本/路径指纹） | 拦截：`包含被禁止的关键字` |
| `SELECT * FROM sale_records`（拖库） | 执行但 `truncated=true`，仅返回 200 行上限，防止全量拖出 |

> 关键字拦截范围（`configs/security.yaml` 的 `forbidden_keywords` / `forbidden_patterns`）：写操作（`insert/update/delete/...`）、多语句特征、`union select`、文件读取（`load_file/outfile/dumpfile`）、UDF 与系统调用（`sys_*`、`xp_cmd*`）、服务控制（`shutdown`）、版本/路径指纹（`@@version` 等）；系统库（`information_schema` / `mysql` / `performance_schema` / `sys`）另由 `blocked_schemas` 封禁。
> 实现注记：`@@version` 这类以符号开头的 token 不能套 `\b` 单词边界（`@` 非词字符会绕过匹配），故规则拆成「词 token 带边界」（`forbidden_keywords`）与「正则不带边界」（`forbidden_patterns`）两组并列。

### 测试中发现并修复的 Bug

批量测试第一轮暴露：当 SQL 自带结尾分号（`SELECT COUNT(*) FROM company;`）时，server 追加行数上限会拼成 `… FROM company; LIMIT 200` 导致语法错误，Agent 反复重试仍报错。
修复：追加 `LIMIT` 前先剥离结尾分号（现由 `mcp_server/security/validator.py` 放行时剥离，`mcp_server/tools/query.py` 再追加行数上限）。修复后 ①-⑤ 全部一次跑通。

### 补充结论

- **⑧a Agent 层**：直接让它“删掉 company 表数据”，Agent 基于 system prompt 明确拒绝写操作并说明只读限制。
- **安全纵深**：Agent（约定只读）→ MCP Server（SQL 白名单/单语句/危险关键字/行数上限/超时）→ 数据库（无写权限账号）三层，任一层兜底。

---

## 生产化备忘

- 所有工具均为只读（仅放行 `SELECT`），并做单语句校验、危险关键字拦截、系统库封禁、行数上限、执行超时保护；策略集中在 `configs/security.yaml`。
- 连接参数（`MYSQL_*`）与 LLM 参数（`LLM_*`）统一从 `.env` 读取，代码无硬编码。
- Server 使用 mcp **2.x** 的 `MCPServer`，Agent 通过 Streamable HTTP 客户端动态发现工具（`list_tools`）并调用。
- 想在别的 MCP 客户端里复用这套能力，可直接把 `mcp_server` 包以 stdio/HTTP 传输（`python -m mcp_server.server`）接入即可。