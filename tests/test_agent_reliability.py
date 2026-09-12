"""V0.6 Agent Reliability 评测集。

覆盖验收标准：正确性 / 工具选择 / 修复次数 / 超限退出 / 错误提示，
以及「临时错误可重试、永久错误立即终止、无无限循环」。

运行方式（仓库根目录，无需启动 MCP Server 或真实 LLM）：
    python -m pytest tests/test_agent_reliability.py -q
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import agent.agent as agent_module
from agent.agent import SQLAgent
from agent.reliability import (
    Budget, ErrorCode, ErrorKind,
    backoff_delay, classify_error, error_code_for_kind,
)


# ---------------------------------------------------------------------------
# 错误分类 / 预算 / 重试单元测试
# ---------------------------------------------------------------------------

def test_classify_permission_code_is_fatal():
    assert classify_error("table_not_allowed", "") == ErrorKind.FATAL_PERMISSION
    assert classify_error("column_not_allowed", "") == ErrorKind.FATAL_PERMISSION


def test_classify_timeout_text_is_fatal_timeout():
    assert classify_error("", "Lock wait timeout exceeded") == ErrorKind.FATAL_TIMEOUT
    assert classify_error(None, "操作超时") == ErrorKind.FATAL_TIMEOUT
    # MySQL 3024 实际文本：不能误判为可修复 SQL
    assert classify_error("", "Statement exceeded execution time") == ErrorKind.FATAL_TIMEOUT
    # 服务端 query_timeout 稳定 code 直接判超时
    assert classify_error("query_timeout", "Statement exceeded execution time") == ErrorKind.FATAL_TIMEOUT


def test_classify_query_error_unknown_defaults_fatal_tool():
    """未知数据库错误默认 FATAL_TOOL，不再默认为可修复 SQL。"""
    assert classify_error("query_error", "some arcane db message") == ErrorKind.FATAL_TOOL
    assert classify_error(None, "some arcane db message") == ErrorKind.FATAL_TOOL


def test_classify_execution_syntax_error_still_repairable():
    """执行层 1064/语法类仍可修复（SQL 实为拼写/字段问题）。"""
    assert classify_error("query_error",
                          "You have an error in your SQL syntax near 'from'") == ErrorKind.REPAIRABLE_SQL
    assert classify_error("query_error", "(1064, unknown function 'f')") == ErrorKind.REPAIRABLE_SQL


def test_classify_repairable_sql_text():
    assert classify_error("", "Unknown column 'foo' in field list") == ErrorKind.REPAIRABLE_SQL
    assert classify_error("", "syntax error near 'select'") == ErrorKind.REPAIRABLE_SQL


def test_classify_parse_error_is_repairable():
    """真正的 SQL 语法错误（服务器 parse_error）应进入修复流程，而非被判为权限拒绝。"""
    assert classify_error("parse_error", "无法解析的 SQL 语句") == ErrorKind.REPAIRABLE_SQL


def test_classify_dangerous_is_fatal():
    assert classify_error("", "LOAD_FILE('/etc/passwd')") == ErrorKind.FATAL_DANGEROUS


def test_error_code_mapping():
    assert error_code_for_kind(ErrorKind.FATAL_PERMISSION) is ErrorCode.PERMISSION_DENIED
    assert error_code_for_kind(ErrorKind.FATAL_TIMEOUT) is ErrorCode.TIMEOUT
    assert error_code_for_kind(ErrorKind.FATAL_TOOL) is ErrorCode.TOOL_ERROR


def test_backoff_delay_is_bounded_with_jitter():
    for n in range(1, 8):
        d = backoff_delay(n, base=0.5, cap=4.0)
        assert d <= 4.5  # cap + jitter 上界
        assert d > 0


def test_budget_iteration_and_repair_limits():
    b = Budget(max_iterations=2, max_tool_calls=5, max_sql_repairs=1)
    assert not b.any_exceeded()
    b.iterations_used = 2
    assert b.iterations_exceeded()


def test_budget_token_and_cost_limits():
    b = Budget(max_iterations=5, max_tool_calls=5, max_sql_repairs=5,
               max_tokens=100, max_cost=0.01)
    b.account_usage(60, 60)  # 120 tokens
    assert b.tokens_exceeded()
    b.total_cost = 0.02
    assert b.cost_exceeded()


def test_budget_zero_token_means_unlimited():
    b = Budget(max_iterations=5, max_tool_calls=5, max_sql_repairs=5, max_tokens=0)
    b.account_usage(10 ** 6, 10 ** 6)
    assert not b.tokens_exceeded()


# ---------------------------------------------------------------------------
# 端到端（faked MCP session + faked LLM responder）
# ---------------------------------------------------------------------------

def _tool_result(data, is_error=False):
    return SimpleNamespace(structured_content=data, content=[], is_error=is_error)


def _std_response(content=None, tool_calls=None, pt=10, ct=10):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct),
    )


def _tc(call_id, name, args):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


class FakeSession:
    """脚本化 MCP 会话：call_tool 按队列返回预设结果，并记录调用轨迹。"""

    def __init__(self, queue, tools=None, raise_on_call=None):
        self.queue = list(queue) if queue else []
        self.tools = tools or []   # SimpleNamespace(name=..., description=..., input_schema=...)
        self.raise_on_call = raise_on_call
        self.calls = []

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def read_resource(self, uri):
        return SimpleNamespace(contents=[SimpleNamespace(text="[]")])

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.raise_on_call is not None:
            if isinstance(self.raise_on_call, list):
                if self.raise_on_call:
                    raise self.raise_on_call.pop(0)
            else:
                raise self.raise_on_call
        item = self.queue.pop(0) if self.queue else {"columns": ["x"], "rows": []}
        if isinstance(item, tuple):  # (data, is_error)
            data, is_error = item
        else:
            data, is_error = item, False
        return _tool_result(data, is_error)


class _FakeMCP:
    """可替换 mcp_session 的异步上下文管理器。"""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _config_stub(max_iter, max_tool_calls, max_repairs, deadline):
    from agent import config as _real
    return SimpleNamespace(
        LLM_MAX_TOOL_ITERATIONS=max_iter,
        MAX_TOOL_CALLS=max_tool_calls,
        MAX_SQL_REPAIRS=max_repairs,
        MAX_TOKENS=0,
        MAX_COST=0.0,
        REQUEST_DEADLINE_SECONDS=deadline,
        LLM_MAX_CALL_SECONDS=_real.LLM_MAX_CALL_SECONDS,
        LLM_MAX_RETRIES=_real.LLM_MAX_RETRIES,
        LLM_MODEL=_real.LLM_MODEL,
        RETRY_BASE_DELAY_SECONDS=_real.RETRY_BASE_DELAY_SECONDS,
        RETRY_MAX_DELAY_SECONDS=_real.RETRY_MAX_DELAY_SECONDS,
        MCP_CALL_TIMEOUT_SECONDS=_real.MCP_CALL_TIMEOUT_SECONDS,
        MCP_MAX_RETRIES=_real.MCP_MAX_RETRIES,
        model_prices=lambda m: (0.0, 0.0),
        BLOCKED_TOOLS=_real.BLOCKED_TOOLS,
        RETRY_SAFE_TOOLS=_real.RETRY_SAFE_TOOLS,
    )


def make_agent(monkeypatch, responder, max_iter=5, max_tool_calls=40,
               max_repairs=3, deadline=120.0) -> SQLAgent:
    """构造注入 fake responder / fake config 的 SQLAgent。"""
    cfg = _config_stub(max_iter, max_tool_calls, max_repairs, deadline)
    monkeypatch.setattr(agent_module, "config", cfg)
    agent = SQLAgent.__new__(SQLAgent)  # 跳过 __init__ 的 Key 校验与真实客户端
    agent.verbose = False
    agent._responder = responder
    return agent


def run_agent(agent: SQLAgent, session: FakeSession,
              monkeypatch) -> object:
    """把 agent 内部的 mcp_session 指到 fake session 后运行一次。"""
    monkeypatch.setattr(agent_module, "mcp_session", lambda: _FakeMCP(session))

    async def _run():
        return await agent.run("测试问题？")
    return asyncio.run(_run())


def test_success_basic(monkeypatch):
    """正确性：先调 run_query 拿数据，再给出最终答案。"""
    responses = iter([
        _std_response(tool_calls=[_tc("c1", "run_query", {"sql": "SELECT 1"})]),
        _std_response(tool_calls=[_tc("c2", "run_query", {"sql": "SELECT 2"})]),
        _std_response(content="合计销售额为 100。"),
    ])

    async def responder(messages, schemas):
        return next(responses)

    session = FakeSession(queue=[
        {"columns": ["v"], "rows": [{"v": 1}]},
        {"columns": ["v"], "rows": [{"v": 2}]},
    ])
    agent = make_agent(monkeypatch, responder)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.SUCCESS
    assert "100" in result.answer
    assert result.error_message == ""   # 成功结果不应携带 error_message
    assert result.tool_call_count == 2
    assert result.repair_count == 0
    assert result.iteration_count == 2


def test_permission_denied_terminates_immediately(monkeypatch):
    """永久错误（权限拒绝）立即终止，不再继续调用模型。"""
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        return _std_response(
            tool_calls=[_tc("c1", "run_query", {"sql": "DELETE FROM x"})])

    session = FakeSession(queue=[
        {"error": "访问被禁用的表", "code": "table_denied"}])
    agent = make_agent(monkeypatch, responder, max_iter=10)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.PERMISSION_DENIED
    assert calls["n"] == 1          # 未被盲目重试
    assert result.tool_call_count == 1
    assert result.iteration_count == 0
    assert "[permission_denied]" in result.answer


def test_sql_repair_then_success(monkeypatch):
    """可修复的 SQL 错误：交给 LLM 修正后再查，最终成功。"""
    session = FakeSession(queue=[
        {"error": "Unknown column 'amout' in field list", "code": None},
        {"columns": ["total"], "rows": [{"total": "42"}]},
    ])
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        if calls["n"] == 1:
            return _std_response(tool_calls=[
                _tc("c1", "run_query", {"sql": "SELECT amout FROM sale"})])
        if calls["n"] == 2:
            return _std_response(tool_calls=[
                _tc("c2", "run_query", {"sql": "SELECT amount FROM sale"})])
        return _std_response(content="销售总额为 42。")

    agent = make_agent(monkeypatch, responder)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.SUCCESS
    assert result.repair_count == 1
    assert result.tool_call_count == 2


def test_repair_budget_exhausted(monkeypatch):
    """SQL 修复次数超限：稳定以 sql_syntax_error 结束，且循环有界。"""
    session = FakeSession(queue=[
        {"error": "syntax error near 'from'", "code": None}] * 5)
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        return _std_response(tool_calls=[
            _tc("c%d" % calls["n"], "run_query", {"sql": "SLEECT x"})])

    agent = make_agent(monkeypatch, responder, max_repairs=2, max_iter=10)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.SQL_SYNTAX_ERROR
    assert result.repair_count == 2
    assert calls["n"] == 3  # 首次 + 2 次修复尝试，之后停止
    assert "修复" in result.answer


def test_iteration_budget_exhausted(monkeypatch):
    """迭代轮数超限：稳定以 budget_exceeded 结束，绝不死循环。"""
    session = FakeSession(queue=[
        {"columns": ["v"], "rows": [{"v": 1}]}] * 20)
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        return _std_response(tool_calls=[
            _tc("c%d" % calls["n"], "run_query", {"sql": "SELECT v"})])

    agent = make_agent(monkeypatch, responder, max_iter=3, max_tool_calls=40)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.BUDGET_EXCEEDED
    assert result.iteration_count == 3
    assert result.tool_call_count == 3
    assert calls["n"] == 3


def test_tool_call_budget_exhausted(monkeypatch):
    """工具调用总数超限：恰好执行 max_tool_calls 次后结束，避免无限调用。"""
    session = FakeSession(queue=[
        {"columns": ["v"], "rows": [{"v": 1}]}] * 20)
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        return _std_response(tool_calls=[
            _tc("c%d" % calls["n"], "run_query", {"sql": "SELECT v"})])

    agent = make_agent(monkeypatch, responder, max_iter=10, max_tool_calls=2)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.BUDGET_EXCEEDED
    assert result.tool_call_count == 2          # 恰好执行 2 次
    assert len(session.calls) == 2


def test_tool_call_budget_one(monkeypatch):
    """边界：MAX_TOOL_CALLS=1 时只允许执行 1 次工具调用。"""
    session = FakeSession(queue=[
        {"columns": ["v"], "rows": [{"v": 1}]}] * 10)
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        return _std_response(tool_calls=[
            _tc("c%d" % calls["n"], "run_query", {"sql": "SELECT v"})])

    agent = make_agent(monkeypatch, responder, max_iter=10, max_tool_calls=1)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.BUDGET_EXCEEDED
    assert result.tool_call_count == 1
    assert len(session.calls) == 1


def test_mcp_is_error_flagged_triggers_repair(monkeypatch):
    """MCP CallToolResult.is_error=True 即便无标准错误结构也应进入错误流程并修复。"""
    session = FakeSession(queue=[
        ({"message": "Unknown column 'amout'"}, True),
        {"columns": ["total"], "rows": [{"total": "7"}]},
    ])
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        if calls["n"] == 1:
            return _std_response(tool_calls=[
                _tc("c1", "run_query", {"sql": "SELECT amout FROM sale"})])
        if calls["n"] == 2:
            return _std_response(tool_calls=[
                _tc("c2", "run_query", {"sql": "SELECT amount FROM sale"})])
        return _std_response(content="销售总额为 7。")

    agent = make_agent(monkeypatch, responder)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.SUCCESS
    assert result.repair_count == 1
    assert result.tool_call_count == 2


def test_parse_error_enters_repair_flow(monkeypatch):
    """服务器 parse_error（真语法错误）进入修复：先报错，再成功。"""
    session = FakeSession(queue=[
        {"error": "无法解析的 SQL 语句", "code": "parse_error"},
        {"columns": ["v"], "rows": [{"v": 1}]},
    ])
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        if calls["n"] == 1:
            return _std_response(tool_calls=[
                _tc("c1", "run_query", {"sql": "SELEC 1"})])
        return _std_response(content="查询完成。")

    agent = make_agent(monkeypatch, responder)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.SUCCESS
    assert result.repair_count == 1
    assert calls["n"] == 2


def test_timeout_terminates(monkeypatch):
    """全局请求截止时间耗尽：稳定以 timeout 结束，且不再调用模型。"""
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        raise AssertionError("不应调用模型")

    agent = make_agent(monkeypatch, responder, deadline=0.0)
    result = run_agent(agent, FakeSession(queue=[]), monkeypatch)
    assert result.status is ErrorCode.TIMEOUT
    assert calls["n"] == 0


def test_query_timeout_terminates_without_repair(monkeypatch):
    """SQL 执行超时（query_timeout）立即终止：不消耗修复预算、不再调用模型。"""
    session = FakeSession(queue=[
        {"error": "Statement exceeded execution time", "code": "query_timeout"}])
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        return _std_response(
            tool_calls=[_tc("c1", "run_query", {"sql": "SELECT COUNT(*) FROM big"})])

    agent = make_agent(monkeypatch, responder, max_repairs=3)
    result = run_agent(agent, session, monkeypatch)
    assert result.status is ErrorCode.TIMEOUT
    assert result.repair_count == 0      # 未误进修复流程
    assert calls["n"] == 1               # 立即终止，不再让模型修复
    assert "[timeout]" in result.answer


class _HangingListTools:
    """list_tools 阻塞远超过截止时间，模拟上下文读取阶段长期不返回。"""

    async def list_tools(self):
        await asyncio.sleep(30)
        return SimpleNamespace(tools=[])

    async def read_resource(self, uri):
        return SimpleNamespace(contents=[SimpleNamespace(text="[]")])


def test_context_read_timeout_converges_to_timeout(monkeypatch):
    """上下文读取阶段超时应收敛为 TIMEOUT 且带信息，而非落入 TOOL_ERROR/空 message。"""
    calls = {"n": 0}

    async def responder(messages, schemas):
        calls["n"] += 1
        return _std_response(content="不应到达这里")

    agent = make_agent(monkeypatch, responder, deadline=0.2)
    session = _HangingListTools()
    monkeypatch.setattr(agent_module, "mcp_session", lambda: _FakeMCP(session))
    result = asyncio.run(agent.run("问题"))
    assert result.status is ErrorCode.TIMEOUT
    assert calls["n"] == 0                      # 上下文阶段即中止，未进入 LLM 循环
    assert result.error_message                 # 明确信息而非空串
    assert result.answer


async def _invoke(agent, session, name, args=None, deadline=30.0):
    from agent.reliability import make_deadline
    return await agent._invoke_tool(session, name, args or {}, make_deadline(deadline))


def test_gather_context_filters_blocked_tools(monkeypatch):
    """BLOCKED_TOOLS 从发现结果中剔除，不暴露给 LLM。"""
    agent = make_agent(monkeypatch, async_identity)
    session = FakeSession(
        queue=[],
        tools=[
            SimpleNamespace(name="list_tables", description="列表",
                            input_schema={}),
            SimpleNamespace(name="publish_permission_policy", description="发布策略",
                            input_schema={}),
        ],
    )
    from agent.reliability import make_deadline
    _, _, schemas = asyncio.run(
        agent._gather_context(session, make_deadline(60)))
    names = [s["function"]["name"] for s in schemas]
    assert names == ["list_tables"]
    assert "publish_permission_policy" not in names


def test_blocked_tool_rejected_at_runtime(monkeypatch):
    """纵深防御：即便 LLM 请求被禁工具，也不得执行。"""
    agent = make_agent(monkeypatch, async_identity)
    session = FakeSession(queue=[])
    text, kind = asyncio.run(_invoke(agent, session, "publish_permission_policy"))
    assert kind is ErrorKind.FATAL_TOOL
    assert session.calls == []          # 未发起任何调用


def test_non_retry_safe_tool_executes_once_on_exception(monkeypatch):
    """非 RETRY_SAFE_TOOLS 工具遇传输异常只执行一次，不重试（避免写操作重复提交）。"""
    agent = make_agent(monkeypatch, async_identity)
    session = FakeSession(queue=[], raise_on_call=ConnectionError("boom"))
    text, kind = asyncio.run(_invoke(agent, session, "validate_permission_policy"))
    assert kind is ErrorKind.FATAL_TOOL
    assert len(session.calls) == 1


def test_retry_safe_tool_retries_on_exception(monkeypatch):
    """RETRY_SAFE_TOOLS 内只读工具遇传输异常可退避重试。"""
    agent = make_agent(monkeypatch, async_identity)
    monkeypatch.setattr(agent_module, "backoff_delay", lambda *a, **k: 0.0)
    session = FakeSession(queue=[{"columns": ["v"], "rows": []}],
                          raise_on_call=[ConnectionError("boom"), ConnectionError("boom")])
    text, kind = asyncio.run(_invoke(agent, session, "run_query"))
    assert kind is ErrorKind.SUCCESS
    assert len(session.calls) == 3      # 首试失败 + 2 次重试后成功
    assert session.queue == []          # 结果被消费


async def async_identity(messages, schemas):
    raise AssertionError("不应被调用")


def test_report_meta_present(monkeypatch):
    """指标齐全：迭代 / 工具调用 / 修复 / 耗时 / 状态。"""
    responses = iter([
        _std_response(tool_calls=[_tc("c1", "run_query", {"sql": "SELECT 1"})]),
        _std_response(content="完成。"),
    ])

    async def responder(messages, schemas):
        return next(responses)

    session = FakeSession(queue=[{"columns": ["v"], "rows": [{"v": 1}]}])
    agent = make_agent(monkeypatch, responder)
    result = run_agent(agent, session, monkeypatch)
    meta = result.to_dict()
    assert meta["status"] == "success"
    assert meta["metrics"]["tool_call_count"] == 1
    assert meta["metrics"]["iteration_count"] == 1
    assert meta["metrics"]["elapsed_seconds"] >= 0
    assert "token_count" in meta["metrics"]
    assert "cost" in meta["metrics"]