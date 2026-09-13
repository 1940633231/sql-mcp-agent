"""SQL Agent：通过 MCP 发现并调用 MySQL 只读工具，解答自然语言数据库问题。

工作流程（ReAct + Function Calling）：
    1. 用 MCP Streamable HTTP 客户端连接 mcp_server（需先启动），动态发现工具
    2. 把 MCP 工具 schema 转换为 OpenAI 兼容的 function-calling schema
    3. 循环：LLM 决定调用哪些工具 → 经 MCP 执行 → 结果喂回 LLM
    4. 直到 LLM 给出最终答案，或以稳定错误码可控结束

V0.6（Agent Reliability）：
    - 同步 OpenAI 替换为 AsyncOpenAI
    - LLM / MCP 调用均有连接、读取、单次调用超时
    - 全局 Request Deadline：超时后停止继续修复或调用
    - 临时错误退避重试（仅只读调用），永久错误立即终止
    - 统一预算：迭代轮数 / 工具调用总数 / SQL 修复次数 / Token / 费用
    - SQL Repair 仅处理语法/字段等可修复问题；权限、超时、危险 SQL 不进入盲重试
    - 稳定错误码 + 用户可理解错误信息 + 运行指标

示例（在项目根目录）：
    python -m mcp_server.server &
    python -m agent.agent "今年销售额最高的10家公司"
    python -m agent.agent "今年销售额最高的10家公司" --trace
"""
import asyncio
import json
import sys
import time

import httpx2
import openai
from openai import AsyncOpenAI

from telemetry import SpanKind, tracer, start_exporter, flush_exporter

from . import config
from .client import extract_result, mcp_session, to_openai_tool
from .reliability import (
    AgentError,
    AgentModelError,
    AgentTimeout,
    AgentToolError,
    Budget,
    ErrorCode,
    ErrorKind,
    RunResult,
    backoff_delay,
    classify_error,
    deadline_exceeded,
    deadline_remaining,
    error_code_for_kind,
    make_deadline,
)


def _format_table(t: dict) -> str:
    """把单张表的完整 DTO（表描述 + 键 + 列）格式化为紧凑的提示文本。"""
    header = "表 %s" % t["table"]
    if t.get("label"):
        header += "（%s）" % t["label"]
    if t.get("description"):
        header += ": %s" % t["description"]
    parts = [header]
    if t.get("primary_keys"):
        parts.append("主键: " + ", ".join(t["primary_keys"]))
    for fk in t.get("foreign_keys") or []:
        parts.append(
            "外键: %s -> %s.%s"
            % (fk.get("COLUMN_NAME") or fk.get("column_name"),
               fk.get("REFERENCED_TABLE_NAME") or fk.get("referenced_table_name"),
               fk.get("REFERENCED_COLUMN_NAME") or fk.get("referenced_column_name"))
        )
    indexes = t.get("indexes") or []
    if indexes:
        idx_desc = ", ".join(
            "%s(%s%s)" % (i.get("name") or i.get("Key_name") or "?",
                          i.get("column") or i.get("Column_name") or "?",
                          "" if i.get("unique") or i.get("Non_unique") in (0, "0") else ", 非唯一")
            for i in indexes
        )
        parts.append("索引: " + idx_desc)
    parts.append("列: %s" % json.dumps(t.get("columns") or [], ensure_ascii=False))
    return "\n".join(parts)


def _parse_tool_result(text: str) -> tuple[str | None, str, bool]:
    """解析 MCP 工具返回文本，识别是否含错误结构。

    返回 (code, error_message, is_error)；run_query 等工具失败时返回
    {"error": ..., "code": ...}，这里抽出人类可读的错误文本。
    """
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, text, False
    if isinstance(value, dict) and ("error" in value or value.get("code")):
        return value.get("code"), value.get("error") or text, True
    return None, text, False


class _Outcome:
    """一轮驱动的内部结论：状态、答案与错误信息。

    error_message 仅对本轮终止类结论有意义；成功结论保持为空。
    """

    __slots__ = ("status", "answer", "error_message")

    def __init__(self, status: ErrorCode, answer: str = "", error_message: str = ""):
        self.status = status
        self.answer = answer
        self.error_message = error_message


class SQLAgent:
    """基于 MCP 工具 + LLM function calling 的 SQL 查询 Agent（V0.6 可靠性版）。"""

    def __init__(self, verbose: bool = False):
        if not config.LLM_API_KEY or "填入" in config.LLM_API_KEY:
            raise SystemExit(
                "未配置 LLM_API_KEY：请先在 .env 里填入有效的 API Key，"
                "或设置环境变量 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL。"
            )
        self.client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            max_retries=0,  # 重试交由本模块的退避策略控制，避免 OpenAI 内部无限重试
            timeout=httpx2.Timeout(
                connect=config.LLM_CONNECT_TIMEOUT_SECONDS,
                read=config.LLM_READ_TIMEOUT_SECONDS,
                write=config.LLM_WRITE_TIMEOUT_SECONDS,
                pool=config.LLM_POOL_TIMEOUT_SECONDS,
            ),
        )
        self.verbose = verbose
        # 可注入的响应器（默认走 OpenAI）；测试可替换为脚本化的 fake responder
        self._responder = self._openai_responder

    def _trace(self, msg: str) -> None:
        if self.verbose:
            print("  [trace] " + msg, flush=True)

    @staticmethod
    def _build_system_prompt(tables_desc: str, schema_desc: str) -> str:
        return (
            "你是一个只读 SQL 数据分析助手，通过调用 MCP 工具查询 MySQL 数据库。\n\n"
            "可用工具：\n"
            "  - list_tables       列举所有数据表\n"
            "  - get_schema(tbl)   查看某表完整结构（含业务描述/外键/索引/枚举）\n"
            "  - search_schema(kw)  按业务名/别名/同义词检索表、字段的语义含义\n"
            "  - sales_summary(start_date, end_date, granularity=month, company_id?, limit?)\n"
            "                       按日/周/月/季/年汇总销售额、订单数、客单价\n"
            "  - company_ranking(start_date, end_date, industry?, sort_by=total_amount, order_dir=desc, limit=10)\n"
            "                       按销售额/单量/客单价对公司排名（并列按 company_id 升序）\n"
            "  - industry_analysis(start_date, end_date, industry?, sort_by=total_amount, order_dir=desc, limit=50)\n"
            "                       按行业聚合销售额/单量/公司数及占比（分母为窗口内全部行业销售额）\n"
            "  - run_query(sql)    执行只读 SELECT 查询（数据库会强制只读、单语句、限行）\n\n"
            "当前业务库中的数据表：\n"
            f"{tables_desc}\n\n"
            "各表结构：\n"
            f"{schema_desc}\n\n"
            "规则：\n"
            "1. 回答用户问题时，先判断需要哪些数据，必要时先 list_tables / get_schema 了解结构。\n"
            "2. 常见分析优先调用领域工具（sales_summary / company_ranking / industry_analysis），\n"
            "   它们已内置安全白名单与统计口径，参数日期格式为 YYYY-MM-DD；\n"
            "   仅当问题复杂或领域工具未覆盖时才用 run_query 自行写 SQL。\n"
            "3. 写 SQL 前，先用 search_schema 确认表/列的业务含义与别名，避免编造或拼错列名；\n"
            "   再从 get_schema 拿完整结构确认可用的表/列名。\n"
            "4. 用 run_query 编写 SQL，只做只读聚合查询；不要编造数据，结果以真实查询为准。\n"
            "5. 若 run_query 返回语法/字段错误（如 Unknown column、表不存在），请根据报错修正 SQL 后重试；\n"
            "   若报错是权限拒绝、超时或危险操作，不要盲目重试同一 SQL。\n"
            "6. '今年'/'今年内' 等相对时间词，按当前年度 2026 处理（数据含 2023~2026）。\n"
            "7. 得到结果后，用自然语言组织成清晰答案给用户。\n"
        )

    async def _gather_context(self, session, deadline: float):
        """发现工具；库结构走 Resource（database://schema）一次性预取。

        连接会话建立后的元数据读取同样受全局 Deadline 约束。
        仅把「非 BLOCKED_TOOLS」的工具暴露给 LLM，避免它调用有副作用的服务端管理工具。
        """
        if deadline_exceeded(deadline):
            raise AgentTimeout("请求截止时间已耗尽，停止获取上下文。")
        tools = await asyncio.wait_for(
            session.list_tools(),
            timeout=min(config.MCP_CALL_TIMEOUT_SECONDS,
                        max(0.0, deadline_remaining(deadline))),
        )
        blocked = set(config.BLOCKED_TOOLS)
        allowed = [t for t in tools.tools if t.name not in blocked]
        openai_schemas = [to_openai_tool(t) for t in allowed]
        res = await asyncio.wait_for(
            session.read_resource("database://schema"),
            timeout=min(config.MCP_CALL_TIMEOUT_SECONDS,
                        max(0.0, deadline_remaining(deadline))),
        )
        schema_data = json.loads(res.contents[0].text)
        tables_desc = json.dumps([t["table"] for t in schema_data], ensure_ascii=False)
        schema_desc = "\n".join(_format_table(t) for t in schema_data)
        return tables_desc, schema_desc, openai_schemas

    # ---- LLM 调用：AsyncOpenAI + 连接/读取/单次超时 + 退避重试 ----

    async def _openai_responder(self, messages, schemas):
        """真实 OpenAI 调用（无内部重试；超时与重试由外层统一控制）。"""
        return await self.client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=messages,
            tools=schemas,
            tool_choice="auto",
        )

    async def _call_llm(self, messages, schemas, deadline, budget):
        """调用 LLM：单次超时 + 临时错误退避重试，永久错误直接终止。

        每次尝试都按「剩余截止时间」重新计算超时，确保全局 Deadline 是硬上限：
        截止临近时，单次调用时长会被压缩到剩余量内，避免越过 Deadline 继续等待。
        """
        attempt = 0
        while True:
            if deadline_exceeded(deadline):
                raise AgentTimeout("请求截止时间已耗尽，停止模型调用。")
            call_timeout = min(config.LLM_MAX_CALL_SECONDS,
                               deadline_remaining(deadline))
            if call_timeout <= 0:
                raise AgentTimeout("请求截止时间已耗尽，停止模型调用。")
            try:
                with tracer.span(
                    "agent.llm", kind=SpanKind.CLIENT,
                    attributes={"model": config.LLM_MODEL},
                ):
                    resp = await asyncio.wait_for(
                        self._responder(messages, schemas), timeout=call_timeout
                    )
                self._account_usage(budget, resp)
                return resp
            except asyncio.TimeoutError:
                raise AgentTimeout("单次模型调用超时（%.0fs）。" % call_timeout)
            except AgentError:
                raise
            except (openai.APIConnectionError, openai.APITimeoutError,
                    openai.RateLimitError, openai.InternalServerError) as e:
                attempt += 1
                if attempt > config.LLM_MAX_RETRIES or deadline_exceeded(deadline):
                    raise AgentModelError("模型临时错误且重试耗尽：%s" % e)
                await self._sleep_with_deadline(
                    backoff_delay(attempt, config.RETRY_BASE_DELAY_SECONDS,
                                  config.RETRY_MAX_DELAY_SECONDS), deadline)
            except openai.APIStatusError as e:
                if e.status_code >= 500:
                    attempt += 1
                    if attempt > config.LLM_MAX_RETRIES or deadline_exceeded(deadline):
                        raise AgentModelError("模型服务端错误：%s" % e)
                    await self._sleep_with_deadline(
                        backoff_delay(attempt, config.RETRY_BASE_DELAY_SECONDS,
                                      config.RETRY_MAX_DELAY_SECONDS), deadline)
                else:
                    # 4xx（认证、无效请求、额度等）为永久错误，立即终止
                    raise AgentModelError("模型调用被拒绝（HTTP %s）：%s"
                                          % (e.status_code, e))
            except Exception as e:
                raise AgentModelError("模型调用异常：%s" % e)

    def _account_usage(self, budget: Budget, resp) -> None:
        """把一次响应的 token 用量与费用记入预算。"""
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        budget.account_usage(pt, ct)
        pi, po = config.model_prices(config.LLM_MODEL)
        budget.total_cost += (pt * pi + ct * po) / 1_000_000

    @staticmethod
    async def _sleep_with_deadline(seconds: float, deadline: float) -> None:
        """在截止时间内休眠；若休眠会越过截止则立即返回。"""
        await asyncio.sleep(min(seconds, max(0.0, deadline_remaining(deadline))))

    # ---- 工具调用：单次超时 + 传输/临时错误退避重试（仅只读）----

    async def _invoke_tool(self, session, name: str, args: dict, deadline: float):
        """调用 MCP 工具，返回 (抽取文本, ErrorKind)。

        重试边界（重要）：
          - 仅 RETRY_SAFE_TOOLS 白名单内的只读工具在传输层异常/临时错误时做退避重试；
          - 不在白名单内的工具至多执行一次，失败即返回 FATAL_TOOL —— 杜绝写操作反复被提交。
          - BLOCKED_TOOLS：纵深防御，运行时也拒绝调用（不应发生，因发现阶段已剔除）。
        """
        if name in config.BLOCKED_TOOLS:
            return "工具 %s 已被 Agent 禁用。" % name, ErrorKind.FATAL_TOOL

        can_retry = name in config.RETRY_SAFE_TOOLS
        attempt = 0
        while True:
            if deadline_exceeded(deadline):
                return "请求截止时间已耗尽。", ErrorKind.FATAL_TIMEOUT
            call_timeout = min(config.MCP_CALL_TIMEOUT_SECONDS,
                               deadline_remaining(deadline))
            try:
                with tracer.span(
                    "agent.tool." + name, kind=SpanKind.CLIENT,
                    attributes={"tool": name},
                ):
                    raw = await asyncio.wait_for(
                        session.call_tool(name, args), timeout=call_timeout
                    )
            except asyncio.TimeoutError:
                return "工具调用超时：%s" % name, ErrorKind.FATAL_TIMEOUT
            except AgentError:
                raise
            except Exception as e:
                if not can_retry:
                    return "工具调用失败：%s" % e, ErrorKind.FATAL_TOOL
                attempt += 1
                if attempt > config.MCP_MAX_RETRIES or deadline_exceeded(deadline):
                    return "工具调用失败：%s" % e, ErrorKind.FATAL_TOOL
                await self._sleep_with_deadline(
                    backoff_delay(attempt, config.RETRY_BASE_DELAY_SECONDS,
                                  config.RETRY_MAX_DELAY_SECONDS), deadline)
                continue

            text = extract_result(raw)
            # 协议层 is_error（MCP CallToolResult）与内容里的 {"error": ...} 均视为错误。
            # 即便内容不是标准错误结构，只要 is_error=True 就按错误分类处理。
            is_error_flag = bool(getattr(raw, "is_error", False))
            code, error_message, parsed_error = _parse_tool_result(text)
            if not (parsed_error or is_error_flag):
                return text, ErrorKind.SUCCESS
            if not parsed_error:
                error_message = text
            kind = classify_error(code, error_message)
            if kind == ErrorKind.TRANSIENT:
                if not can_retry:
                    return error_message, ErrorKind.FATAL_TOOL
                attempt += 1
                if attempt > config.MCP_MAX_RETRIES or deadline_exceeded(deadline):
                    return error_message, ErrorKind.FATAL_TOOL
                await self._sleep_with_deadline(
                    backoff_delay(attempt, config.RETRY_BASE_DELAY_SECONDS,
                                  config.RETRY_MAX_DELAY_SECONDS), deadline)
                continue
            return error_message, kind

    async def _run_tool(self, session, tool_call, messages, deadline, budget):
        """执行单个工具调用，返回 (是否终止, 终止时结论)。

        对可修复的 SQL 错误做「有限次 LLM 修复」（计入 sql_repairs）；
        权限 / 超时 / 危险 SQL 立即终止，不进入盲重试。
        """
        name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            text = "工具参数不是合法 JSON：%r" % (tool_call.function.arguments,)
            self._trace("%s(参数解析失败) => 已回写模型" % name)
            messages.append({"role": "tool", "tool_call_id": tool_call.id,
                             "content": text})
            return None, False

        self._trace("%s(%s)" % (name, json.dumps(args, ensure_ascii=False)))
        text, kind = await self._invoke_tool(session, name, args, deadline)
        self._trace("=> " + (text if len(text) < 300 else text[:300] + "...[截断]"))

        if kind not in (ErrorKind.SUCCESS, ErrorKind.REPAIRABLE_SQL):
            # 永久错误 / 超时 / 危险操作：立即终止，给出稳定错误码与可读信息
            messages.append({"role": "tool", "tool_call_id": tool_call.id,
                             "content": text})
            code = error_code_for_kind(kind)
            return _Outcome(code, error_message=self._fatal_message(code, text)), True

        if kind == ErrorKind.REPAIRABLE_SQL:
            if budget.repairs_exceeded():
                messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                 "content": text})
                return _Outcome(
                    ErrorCode.SQL_SYNTAX_ERROR,
                    error_message="SQL 语法/字段错误在 %d 次修复内未解决：%s"
                                  % (budget.max_sql_repairs, text),
                ), True
            budget.sql_repairs_used += 1
            self._trace("SQL 修复 #%d/%d" % (budget.sql_repairs_used,
                                             budget.max_sql_repairs))

        messages.append({"role": "tool", "tool_call_id": tool_call.id,
                         "content": text})
        return None, False

    @staticmethod
    def _fatal_message(code: ErrorCode, text: str) -> str:
        """把终止类错误拼成用户可理解的一句提示。"""
        from .reliability import ERROR_CODE_MESSAGES
        return "%s [%s] %s" % (ERROR_CODE_MESSAGES.get(code, ""), code.value, text)

    # ---- 主循环：统一预算 + 全局截止时间 ----

    async def _drive(self, session, messages, schemas, deadline, budget):
        """迭代驱动：每个 LLM 轮次调用模型并执行其工具调用，直至产出结论。"""
        while True:
            if deadline_exceeded(deadline):
                return _Outcome(ErrorCode.TIMEOUT,
                                error_message="已达全局请求截止时间。")
            if budget.iterations_exceeded():
                return _Outcome(ErrorCode.BUDGET_EXCEEDED,
                                error_message="已达最大迭代轮数上限 (%d 轮)。"
                                              % budget.max_iterations)

            resp = await self._call_llm(messages, schemas, deadline, budget)
            if budget.tokens_exceeded():
                return _Outcome(ErrorCode.BUDGET_EXCEEDED,
                                error_message="已达 Token 预算上限 (%d)。"
                                              % budget.total_tokens)
            if budget.cost_exceeded():
                return _Outcome(ErrorCode.BUDGET_EXCEEDED,
                                error_message="已达费用预算上限 ($%.4f)。"
                                              % budget.total_cost)

            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                return _Outcome(ErrorCode.SUCCESS, answer=msg.content or "（模型未返回内容）")

            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name,
                                     "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                # 先判定再执行：恰好允许执行 max_tool_calls 次，第 max+1 次才超限退出
                if budget.tool_calls_used >= budget.max_tool_calls:
                    return _Outcome(ErrorCode.BUDGET_EXCEEDED,
                                    error_message="已达工具调用总数上限 (%d)。"
                                                  % budget.max_tool_calls)
                budget.tool_calls_used += 1
                outcome, done = await self._run_tool(
                    session, tc, messages, deadline, budget)
                if done:
                    return outcome
            budget.iterations_used += 1

    async def run(self, question: str) -> RunResult:
        """主入口：连接 MCP，循环调用 LLM 直到得到最终答案或稳定可控结束。

        返回 RunResult（答案 + 稳定错误码 + 指标），不再返回裸字符串。
        """
        started = time.monotonic()
        # V0.7.1：即使以库（非 CLI）方式调用，也确保 OTLP 导出被启动并在结束后投递。
        start_exporter()
        # V0.7：Agent 层根 Span；不记录完整问题（标签卫生，只留长度与指纹）。
        # 必须 set_current 让 _call_llm/_invoke_tool 的 child span 与 client.py 的
        # traceparent 注入都落在同一 trace_id 下。
        root_span = tracer.start(
            "agent.run", kind=SpanKind.SERVER,
            attributes={"question_len": len(question), "model": config.LLM_MODEL},
        )
        _root_token = tracer.set_current(root_span)
        deadline = make_deadline(config.REQUEST_DEADLINE_SECONDS)
        budget = Budget(
            max_iterations=config.LLM_MAX_TOOL_ITERATIONS,
            max_tool_calls=config.MAX_TOOL_CALLS,
            max_sql_repairs=config.MAX_SQL_REPAIRS,
            max_tokens=config.MAX_TOKENS or None,
            max_cost=config.MAX_COST or None,
        )
        result = RunResult(status=ErrorCode.MODEL_ERROR)
        try:
            # 统一硬上限：覆盖 MCP 建连 + initialize + 上下文读取 + 全循环。
            # 与内层 deadline（逐轮/逐次调用）互补，兜住所有未落到 wait_for 的阶段。
            async with asyncio.timeout(config.REQUEST_DEADLINE_SECONDS):
                async with mcp_session() as session:
                    tables_desc, schema_desc, openai_schemas = await self._gather_context(
                        session, deadline
                    )
                    messages = [
                        {"role": "system",
                         "content": self._build_system_prompt(tables_desc, schema_desc)},
                        {"role": "user", "content": question},
                    ]
                    outcome = await self._drive(session, messages, openai_schemas,
                                                deadline, budget)
                    result.status = outcome.status
                    result.answer = outcome.answer
                    result.error_message = outcome.error_message
                    # 非成功终止时保证 answer 也携带一段用户可理解的信息
                    if outcome.status is not ErrorCode.SUCCESS and not result.answer \
                            and result.error_message:
                        result.answer = result.error_message
        except (TimeoutError, asyncio.TimeoutError) as e:
            # asyncio.timeout / wait_for 超时统一收敛为 TIMEOUT，而非落入通用 TOOL_ERROR
            result.status = ErrorCode.TIMEOUT
            result.error_message = str(e) or "请求超时（全局截止时间）。"
            result.answer = "请求超时：%s" % (result.error_message)
        except AgentTimeout as e:
            result.status = ErrorCode.TIMEOUT
            result.error_message = str(e)
            result.answer = "请求超时：%s" % e
        except AgentModelError as e:
            result.status = ErrorCode.MODEL_ERROR
            result.error_message = str(e)
            result.answer = "模型异常：%s" % e
        except AgentToolError as e:
            result.status = ErrorCode.TOOL_ERROR
            result.error_message = str(e)
            result.answer = "工具异常：%s" % e
        except Exception as e:  # 兜底：绝不让循环/调用外泄导致失控
            result.status = ErrorCode.TOOL_ERROR
            result.error_message = str(e)
            result.answer = "发生未预期错误：%s" % e
        finally:
            result.iteration_count = budget.iterations_used
            result.tool_call_count = budget.tool_calls_used
            result.repair_count = budget.sql_repairs_used
            result.token_count = budget.total_tokens
            result.cost = round(budget.total_cost, 6)
            result.elapsed_seconds = round(time.monotonic() - started, 3)
            tracer.end(
                root_span,
                status="ok" if result.status is ErrorCode.SUCCESS else "error",
                attributes={
                    "status": result.status.value,
                    "iterations": result.iteration_count,
                    "tool_calls": result.tool_call_count,
                    "tokens": result.token_count,
                    "duration_ms": round(result.elapsed_seconds * 1000, 3),
                },
            )
            tracer.restore(_root_token)
            flush_exporter()  # 非 CLI 场景也能在结束后导出本批 Span
        return result


def _render_result(result: RunResult) -> str:
    """把 RunResult 渲染为终端可读文本（答案 + 状态 + 指标）。"""
    lines = ["\n===== 最终答案 =====\n%s" % (result.answer or result.error_message)]
    lines.append("\n[状态] %s" % result.status.value)
    if result.status.value != "success" and result.error_message:
        lines.append("[错误] %s" % result.error_message)
    lines.append(
        "[指标] 迭代=%d 工具调用=%d SQL修复=%d 耗时=%.2fs tokens=%d cost=$%.6f"
        % (result.iteration_count, result.tool_call_count, result.repair_count,
           result.elapsed_seconds, result.token_count, result.cost)
    )
    return "\n".join(lines)


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verbose = "--trace" in sys.argv[1:]
    if len(args) < 1:
        print('用法: python -m agent.agent "你想查询的问题" [--trace]')
        return
    start_exporter()  # Agent 独立进程也启动 OTLP 导出
    agent = SQLAgent(verbose=verbose)
    result = await agent.run(args[0])
    print(_render_result(result))
    flush_exporter()  # CLI 退出前把队列里的 Span 兜底导出


if __name__ == "__main__":
    asyncio.run(main())