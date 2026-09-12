"""领域查询工具：sales_summary / company_ranking / industry_analysis。

仅做 MCP 协议转换（显式参数签名，供 Agent 发现工具 schema），委托
DomainQueryService；不直接访问数据库，统一经过 QueryService 的完整安全链路
（AST Guard / RBAC / ACL / RLS / LIMIT / 审计 / 指标）。
"""
from __future__ import annotations

from ..domain.registry import DomainRegistry
from ..domain.service import DomainQueryService

_registry = DomainRegistry.shared()
_service = DomainQueryService()


def sales_summary(
    start_date: str,
    end_date: str,
    granularity: str | None = None,
    company_id: int | None = None,
    limit: int | None = None,
) -> dict:
    """按时间粒度汇总销售流水（订单数、销售额、客单价）。

    参数：start_date/end_date 必填（YYYY-MM-DD，闭区间）；granularity 取
    day/week/month/quarter/year（默认 month）；company_id 可选（默认全部公司）；
    limit 默认 200、上限 200。
    口径：amount 为单笔成交额全额计入（无退款/取消记录）；record_date 为 DATE
    不含时区，日期区间按业务时区 Asia/Shanghai 解释。
    """
    return _service.run("sales_summary", _compact(locals()))


def company_ranking(
    start_date: str,
    end_date: str,
    industry: str | None = None,
    sort_by: str | None = None,
    order_dir: str | None = None,
    limit: int | None = None,
) -> dict:
    """按销售额/单量/客单价对公司排名（可指定行业与时间窗口）。

    参数：start_date/end_date 必填（YYYY-MM-DD）；industry 可选（须命中服务端
    行业白名单）；sort_by 取 total_amount/order_count/avg_amount（默认
    total_amount）；order_dir 取 asc/desc（默认 desc）；limit 默认 10、上限 50。
    并列规则：指标相同时按 company_id 升序，排名确定唯一。
    """
    return _service.run("company_ranking", _compact(locals()))


def industry_analysis(
    start_date: str,
    end_date: str,
    industry: str | None = None,
    sort_by: str | None = None,
    order_dir: str | None = None,
    limit: int | None = None,
) -> dict:
    """按行业聚合销售额/单量/公司数及行业占比（可指定时间窗口与行业过滤）。

    参数：start_date/end_date 必填（YYYY-MM-DD）；industry 可选（未知行业会以
    invalid_parameter 拒绝）；sort_by 取 total_amount/order_count/company_count；
    order_dir 取 asc/desc；limit 默认 50、上限 50。
    口径：share_percent = 行业销售额 / 窗口内全部行业销售额合计（分母由独立派生表
    计算，不受行业过滤影响；受 RLS 约束，按当前主体可见数据计算）；仅统计窗口内有
    成交记录的公司。
    """
    return _service.run("industry_analysis", _compact(locals()))


def _compact(locals_dict: dict) -> dict:
    """去掉未传入的 None 参数，避免把 None 当作「显式传参」。"""
    return {key: value for key, value in locals_dict.items() if key != "self" and value is not None}


def register(server) -> None:
    """把本模块的领域工具注册到 MCP Server。"""
    server.tool(
        description=(
            "按时间粒度汇总销售流水。必填 start_date/end_date（YYYY-MM-DD，闭区间）；"
            "granularity=day/week/month/quarter/year（默认 month）；company_id 可选；"
            "limit 默认 200 上限 200。销售额 = SUM(amount)（无退款/取消记录）。"
        )
    )(sales_summary)
    server.tool(
        description=(
            "按销售额/单量/客单价对公司排名。必填 start_date/end_date；industry 可选；"
            "sort_by=total_amount/order_count/avg_amount（默认 total_amount）；"
            "order_dir=asc/desc（默认 desc）；limit 默认 10 上限 50。"
            "并列按 company_id 升序。"
        )
    )(company_ranking)
    server.tool(
        description=(
            "按行业聚合销售额/单量/公司数及占比。必填 start_date/end_date；industry 可选"
            "（未知行业拒绝）；sort_by=total_amount/order_count/company_count；"
            "order_dir=asc/desc；limit 默认 50 上限 50。占比分母为窗口内全部行业销售额。"
        )
    )(industry_analysis)
