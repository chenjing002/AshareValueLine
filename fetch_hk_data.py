#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch Hong Kong stock data through the XHCJ MCP servers.

The generated files use the same static, versioned layout as ``fetch_data.py``.

Examples::

    python fetch_hk_data.py --test 00941
    python fetch_hk_data.py --full
    python fetch_hk_data.py --full --resume 20260824_120000

The script talks to the two MCP servers directly with Streamable HTTP.  Server
URLs and headers are read from ``~/.codex/config.toml``.  Environment variables
can override them; see ``McpConfig.from_codex`` below.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # pragma: no cover - optional search enhancement
    Style = None
    lazy_pinyin = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
# 港股与 A 股版本目录严格分离：本脚本只读写 versions/hk/。
MARKET_DIRS = ("a_share", "hk")
VERSIONS_DIR = DATA_DIR / "versions" / "hk"
HK_STOCK_CSV = BASE_DIR / "hk_stock_companies_20260408.csv"

HISTORY_YEARS = 10
QUARTERLY_YEARS = 3
FISCAL_ENDS = ("0331", "0630", "0930", "1231")
DEFAULT_BATCH_SIZE = int(os.getenv("HK_FETCH_BATCH_SIZE", "20"))
DEFAULT_YEAR_BATCH = int(os.getenv("HK_FETCH_YEAR_BATCH", "2"))

QUOTE_SERVER = "xhcj-mcp-quote-stock-real"
FINANCIAL_SERVER = "xhcj-mcp-financial-market"
HK_STOCK_CATEGORY_ID = "5030"
HK_FINANCIAL_CODE_PATTERN = re.compile(r"^\d{5}\.HK$")
HK_INDICATOR_NAME_PATTERN = re.compile(r"^HKS?_")


def _round(value: Any, digits: int = 4) -> float | None:
    try:
        number = float(value)
        if number != number:  # NaN
            return None
        return round(number, digits)
    except (TypeError, ValueError):
        return None


def pinyin_initials(name: str) -> str:
    if not name or lazy_pinyin is None:
        return ""
    normalized = unicodedata.normalize("NFKC", name)
    parts = lazy_pinyin(normalized, style=Style.FIRST_LETTER)
    return "".join(ch.lower() for part in parts for ch in part if ch.isalnum())


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def parse_json_or_sse(body: bytes) -> dict:
    """Parse either an application/json or an MCP text/event-stream response."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                events.append(json.loads(payload))
    if not events:
        raise RuntimeError("MCP 返回了无法识别的响应格式")
    return events[-1]


@dataclass
class McpConfig:
    url: str
    headers: dict[str, str]

    @classmethod
    def from_codex(cls, server_name: str) -> "McpConfig":
        env_prefix = "XHCJ_QUOTE" if server_name == QUOTE_SERVER else "XHCJ_FINANCIAL"
        env_url = os.getenv(f"{env_prefix}_MCP_URL")
        env_auth = os.getenv("XHCJ_MCP_AUTHORIZATION")

        config_path = Path(os.getenv("CODEX_CONFIG_PATH", Path.home() / ".codex" / "config.toml"))
        section: dict[str, Any] = {}
        if config_path.exists():
            with config_path.open("rb") as handle:
                config = tomllib.load(handle)
            section = (config.get("mcp_servers") or {}).get(server_name) or {}

        url = env_url or section.get("url")
        if not url:
            raise RuntimeError(
                f"未找到 {server_name} 的 URL；请配置 ~/.codex/config.toml "
                f"或环境变量 {env_prefix}_MCP_URL"
            )
        headers = {str(k): str(v) for k, v in (section.get("http_headers") or {}).items()}
        if env_auth:
            headers["Authorization"] = env_auth
        return cls(url=str(url), headers=headers)


class StreamableHttpMcpClient:
    """Small MCP Streamable HTTP client using only the Python standard library."""

    def __init__(self, config: McpConfig, name: str, retries: int = 3):
        self.config = config
        self.name = name
        self.retries = retries
        self.session_id: str | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self.request_count = 0
        self._initialize()

    def _new_id(self) -> int:
        with self._lock:
            value = self._next_id
            self._next_id += 1
            return value

    def _post(self, payload: dict, allow_empty: bool = False) -> dict:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.retries):
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-03-26",
                **self.config.headers,
            }
            if self.session_id:
                headers["Mcp-Session-Id"] = self.session_id
            request = urllib.request.Request(self.config.url, data=encoded, headers=headers, method="POST")
            try:
                self.request_count += 1
                with urllib.request.urlopen(request, timeout=120) as response:
                    session_id = response.headers.get("Mcp-Session-Id")
                    if session_id:
                        self.session_id = session_id
                    body = response.read()
                if not body and allow_empty:
                    return {}
                message = parse_json_or_sse(body)
                if message.get("error"):
                    raise RuntimeError(f"MCP JSON-RPC 错误: {message['error']}")
                return message
            except (
                urllib.error.URLError,
                TimeoutError,
                RuntimeError,
                json.JSONDecodeError,
                http.client.IncompleteRead,
                ConnectionResetError,
                ssl.SSLError,
            ) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{self.name} 请求失败: {last_error}")

    def _initialize(self) -> None:
        message = self._post({
            "jsonrpc": "2.0",
            "id": self._new_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ashare-value-line-hk-fetcher", "version": "1.0"},
            },
        })
        if not (message.get("result") or {}).get("serverInfo"):
            raise RuntimeError(f"{self.name} MCP 初始化失败")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, allow_empty=True)

    def call_tool(self, tool_name: str, arguments: dict) -> Any:
        message = self._post({
            "jsonrpc": "2.0",
            "id": self._new_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        result = message.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"{self.name}.{tool_name} 返回错误: {tool_text(result)[:300]}")
        return result


def tool_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for item in result.get("content") or []:
            if item.get("type") == "text":
                return item.get("text", "")
    return ""


def parse_prefixed_data(result: Any) -> Any:
    """Parse XHCJ responses formatted as ``success:true, data:<json>``."""
    text = tool_text(result).strip()
    marker = "data:"
    position = text.find(marker)
    if position < 0:
        if text.startswith(("{", "[")):
            return json.loads(text)
        raise RuntimeError(f"无法解析 XHCJ 响应: {text[:200]}")
    return json.loads(text[position + len(marker):].strip())


def parse_browser_rows(result: Any) -> list[dict]:
    outer = parse_prefixed_data(result)
    nested = outer.get("result") if isinstance(outer, dict) else None
    if isinstance(nested, str):
        nested = json.loads(nested)
    if not isinstance(nested, dict) or str(nested.get("code")) != "200":
        message = nested.get("msg") if isinstance(nested, dict) else "未知错误"
        raise RuntimeError(f"股票浏览器请求失败: {message}")
    return (((nested.get("data") or {}).get("rows")) or [])


@dataclass(frozen=True)
class Indicator:
    key: str
    label: str
    code: str
    name: str
    kind: str = "amount"  # amount / ratio


ANNUAL_INDICATORS = [
    Indicator("revenue", "营业收入", "54504", "HKS_GE_IS_REVENUE_DW"),
    Indicator("oper_cost", "营业成本", "54518", "HKS_GE_IS_SALECOST_DW"),
    Indicator("gross_profit", "毛利", "54508", "HKS_GE_IS_MARGINPROFIT_DW"),
    Indicator("total_profit", "利润总额", "54556", "HKS_GE_IS_TOTALPROFIT_DW"),
    Indicator("net_profit", "净利润", "54552", "HKS_GE_IS_NETPROFIT_DW"),
    Indicator("parent_net_profit", "归母净利润", "54572", "HKS_GE_IS_NPMINORITY_DW"),
    Indicator("minority_profit", "少数股东损益", "54550", "HKS_GE_IS_MINORITYPROFIT_DW"),
    Indicator("operating_cashflow", "经营现金净额", "54458", "HK_GE_CFS_NETOPCASHFLOW_DW"),
]

BALANCE_INDICATORS = [
    Indicator("money_cap", "现金及等价物", "54316", "HK_GE_BS_CASH_DW"),
    Indicator("trad_asset", "交易性金融资产", "57750", "HKS_GE_BS_TRADINGFINASSETS_DW"),
    Indicator("notes_receiv", "应收票据", "54322", "HK_GE_BS_BILLRECEIVABLE_DW"),
    Indicator("accounts_receiv", "应收账款", "54352", "HK_GE_BS_ACCOUNTRECEIVABLE_DW"),
    Indicator("prepayment", "预付款、按金及其他应收款", "54302", "HK_GE_BS_ADVANCEPAYMENT_DW"),
    Indicator("inventories", "存货", "54336", "HK_GE_BS_INBENTORY_DW"),
    Indicator("total_cur_assets", "流动资产合计", "54334", "HK_GE_BS_CURRENTASSETS_DW"),
    Indicator("lt_eqt_invest", "联营公司权益", "54304", "HK_GE_BS_ASSOCIATEDEQUITY_DW"),
    Indicator("invest_real_estate", "投资物业", "54342", "HK_GE_BS_INVESTPROPERTY_DW"),
    Indicator("fix_assets", "物业、厂房及设备", "54320", "HK_GE_BS_PROPERTY_DW"),
    Indicator("cip", "在建工程", "54296", "HK_GE_BS_CONSTRUINPROCESS_DW"),
    Indicator("intan_assets", "无形资产", "54340", "HK_GE_BS_INTANGIBLEASSETS_DW"),
    Indicator("goodwill", "商誉", "54312", "HK_GE_BS_GOODWILL_DW"),
    Indicator("defer_tax_assets", "递延税项资产", "54314", "HK_GE_BS_DEFFEREDTAXASSETS_DW"),
    Indicator("total_nca", "非流动资产合计", "54346", "HK_GE_BS_NONCURRENTASSETS_DW"),
    Indicator("total_assets", "总资产", "54328", "HK_GE_BS_TOTALASSETS_DW"),
    Indicator("st_borr", "短期借款", "54386", "HK_GE_BS_SHORTTERMLOAN_DW"),
    Indicator("notes_payable", "应付票据", "54388", "HK_GE_BS_NOTESPAYABLE_DW"),
    Indicator("acct_payable", "应付账款", "54398", "HK_GE_BS_ACCOUNTPAYABLE_DW"),
    Indicator("adv_receipts", "预收款项", "54402", "HK_GE_BS_ADVANCERECEIPT_DW"),
    Indicator("payroll_payable", "应付职工薪酬", "54400", "HK_GE_BS_SALARYPAYABLE_DW"),
    Indicator("taxes_payable", "应付税项", "54414", "HK_GE_BS_TAXPAYABLE_DW"),
    Indicator("oth_payable", "其他应付款及应计费用", "54372", "HK_GE_BS_OTHERPAYABLE_DW"),
    Indicator("total_cur_liab", "流动负债合计", "54394", "HK_GE_BS_CURRLIABILITY_DW"),
    Indicator("lt_borr", "长期借款", "54410", "HK_GE_BS_LONGLOAN_DW"),
    Indicator("bond_payable", "可转换票据及债券", "54396", "HK_GE_BS_TRANSBONDS_DW"),
    Indicator("lt_payable", "长期应付款", "54406", "HK_GE_BS_LONGTERMAP_DW"),
    Indicator("defer_tax_liab", "递延税项负债", "54440", "HK_GE_BS_DEFERREDTAXLIABILITY_DW"),
    Indicator("total_ncl", "非流动负债合计", "54370", "HK_GE_BS_NONCURRLIABILITY_DW"),
    Indicator("total_liab", "总负债", "54416", "HK_GE_BS_TOTALLIABILITY_DW"),
    Indicator("total_share", "股本", "54358", "HK_GE_BS_PAIDINCAPITAL_DW"),
    Indicator("cap_rese", "资本公积", "54428", "HK_GE_BS_CAPITALRESERVE_DW"),
    Indicator("undistr_porfit", "未分配利润", "54432", "HK_GE_BS_UNDISTRIBUTEDPROFIT_DW"),
    Indicator("minority_int", "非控股权益", "54426", "HK_GE_BS_MINORITYEQUITY_DW"),
    Indicator("parent_equity", "归属母公司股东权益", "54420", "HK_GE_BS_SEWITHOUTMI_DW"),
    Indicator("total_hldr_eqy_inc_min_int", "总权益", "54436", "HK_GE_BS_TOTALEQUITY_DW"),
    Indicator("total_liab_hldr_eqy", "总权益及总负债", "54424", "HK_GE_BS_LIABILITYEQUITY_DW"),
]

ALL_INDICATORS = ANNUAL_INDICATORS + BALANCE_INDICATORS
INDICATOR_BY_KEY = {item.key: item for item in ALL_INDICATORS}

# Category 5030 separates raw statements by issuer type.  All families map to
# the same canonical keys so the generated file format remains stable.
BANK_INDICATORS = [
    Indicator("revenue", "营业收入", "57744", "HKS_B_IS_OPREVENUE_DW"),
    Indicator("total_profit", "利润总额", "54204", "HK_B_IS_TOTALPROFIT_DW"),
    Indicator("net_profit", "净利润", "54168", "HK_B_IS_NETPROFIT_DW"),
    Indicator("parent_net_profit", "归母净利润", "54208", "HKS_B_IS_NPMINORITY_DW"),
    Indicator("minority_profit", "少数股东损益", "54190", "HK_B_IS_MINORITYPROFIT_DW"),
    Indicator("operating_cashflow", "经营现金净额", "54152", "HK_B_CFS_NETOPCASHFLOW_DW"),
    Indicator("money_cap", "现金及等价物", "53960", "HK_B_BS_CASHHOLDINGS_DW"),
    Indicator("intan_assets", "无形资产", "53978", "HK_B_BS_INTANGIBLEASSETS_DW"),
    Indicator("goodwill", "商誉", "54008", "HK_B_BS_GOODWILL_DW"),
    Indicator("total_assets", "总资产", "54028", "HK_B_BS_TOTALASSETS_DW"),
    Indicator("total_liab", "总负债", "54046", "HK_B_BS_TOTALLIABILITY_DW"),
    Indicator("total_share", "股本", "54048", "HK_B_BS_PAIDINCAPITAL_DW"),
    Indicator("minority_int", "非控股权益", "54066", "HK_B_BS_MINORITYEQUITY_DW"),
    Indicator("parent_equity", "归属母公司股东权益", "54080", "HK_B_BS_SEWITHOUTMI_DW"),
    Indicator("total_hldr_eqy_inc_min_int", "总权益", "54038", "HK_B_BS_TOTALEQUITY_DW"),
    Indicator("total_liab_hldr_eqy", "总权益及总负债", "54058", "HK_B_BS_LIABILITYEQUITY_DW"),
]

INSURANCE_INDICATORS = [
    Indicator("revenue", "营业收入", "53862", "HKS_I_IS_OPREVENUE_DW"),
    Indicator("total_profit", "利润总额", "53850", "HK_I_IS_TOTALPROFIT_DW"),
    Indicator("net_profit", "净利润", "53848", "HK_I_IS_NETPROFIT_DW"),
    Indicator("parent_net_profit", "归母净利润", "53868", "HKS_I_IS_NPMINORITY_DW"),
    Indicator("minority_profit", "少数股东损益", "53874", "HK_I_IS_MINORITYPROFIT_DW"),
    Indicator("operating_cashflow", "经营现金净额", "53776", "HK_I_CFS_NETOPCASHFLOW_DW"),
    Indicator("money_cap", "现金及等价物", "53770", "HK_I_BS_CASH_DW"),
    Indicator("goodwill", "商誉", "53696", "HKS_I_BS_GOODWILL_DW"),
    Indicator("intan_assets", "无形资产", "53700", "HKS_I_BS_INTANGIBLEASSETS_DW"),
    Indicator("total_assets", "总资产", "53708", "HK_I_BS_TOTALASSETS_DW"),
    Indicator("total_liab", "总负债", "53744", "HK_I_BS_TOTALLIABILITY_DW"),
    Indicator("total_share", "股本", "53742", "HK_I_BS_PAIDINCAPITAL_DW"),
    Indicator("minority_int", "非控股权益", "53738", "HK_I_BS_MINORITYEQUITY_DW"),
    Indicator("parent_equity", "归属母公司股东权益", "53766", "HK_I_BS_SEWITHOUTMI_DW"),
    Indicator("total_hldr_eqy_inc_min_int", "总权益", "53732", "HK_I_BS_TOTALEQUITY_DW"),
    Indicator("total_liab_hldr_eqy", "总权益及总负债", "53750", "HK_I_BS_LIABILITYEQUITY_DW"),
]

SECURITIES_INDICATORS = [
    Indicator("revenue", "营业收入", "54704", "HKS_S_IS_REVENUE_DW"),
    Indicator("total_profit", "利润总额", "54734", "HKS_S_IS_TOTALPROFIT_DW"),
    Indicator("net_profit", "净利润", "54722", "HKS_S_IS_NETPROFIT_DW"),
    Indicator("parent_net_profit", "归母净利润", "54712", "HKS_S_IS_NPMINORITY_DW"),
    Indicator("money_cap", "现金及等价物", "54640", "HKS_S_BS_CASH_DW"),
    Indicator("trad_asset", "交易性金融资产", "57780", "HKS_S_BS_TRADINGFINASSETS_DW"),
    Indicator("accounts_receiv", "应收账款及票据", "54642", "HKS_S_BS_ACOUTANDBILLREC_DW"),
    Indicator("total_cur_assets", "流动资产合计", "54658", "HKS_S_BS_CURRENTASSETS_DW"),
    Indicator("goodwill", "商誉", "54670", "HKS_S_BS_GOODWILL_DW"),
    Indicator("intan_assets", "无形资产", "54668", "HKS_S_BS_INTANGIBLEASSETS_DW"),
    Indicator("total_nca", "非流动资产合计", "54676", "HKS_S_BS_NONCURRENTASSETS_DW"),
    Indicator("total_assets", "总资产", "58736", "HKS_S_BS_LIABILITYEQUITY_DW"),
    Indicator("st_borr", "短期借款", "54678", "HKS_S_BS_SHORTTERMLOAN_DW"),
    Indicator("total_cur_liab", "流动负债合计", "54672", "HKS_S_BS_CURRLIABILITY_DW"),
    Indicator("lt_borr", "长期借款", "54652", "HKS_S_BS_LONGLOAN_DW"),
    Indicator("total_ncl", "非流动负债合计", "54656", "HKS_S_BS_NONCURRLIABILITY_DW"),
    Indicator("total_liab", "总负债", "54682", "HKS_S_BS_TOTALLIABILITY_DW"),
    Indicator("total_share", "股本", "54660", "HKS_S_BS_PAIDINCAPITAL_DW"),
    Indicator("minority_int", "非控股权益", "58742", "HKS_S_BS_MINORITYEQUITY_DW"),
    Indicator("parent_equity", "归属母公司股东权益", "58734", "HKS_S_BS_SEWITHOUTMI_DW"),
    Indicator("total_hldr_eqy_inc_min_int", "总权益", "58744", "HKS_S_BS_TOTALEQUITY_DW"),
    Indicator("total_liab_hldr_eqy", "总权益及总负债", "58736", "HKS_S_BS_LIABILITYEQUITY_DW"),
]

FAMILY_INDICATORS = {
    "general": ALL_INDICATORS,
    "bank": BANK_INDICATORS,
    "insurance": INSURANCE_INDICATORS,
    "securities": SECURITIES_INDICATORS,
}

# 单季度（非累计）营收指标，按发行人族区分；港股多数公司只披露中期/年度报表，
# 无对应单季申报的报告期该指标返回 null（而非伪造差值），与前端季度表的空值语义一致。
QUARTERLY_REVENUE_INDICATORS = {
    "general": Indicator("revenue_q", "单季营业收入", "54592", "HKS_GE_ISQ_REVENUE_DW"),
    "bank": Indicator("revenue_q", "单季营业收入", "54286", "HKS_B_ISQ_OPREVENUE_DW"),
    "insurance": Indicator("revenue_q", "单季营业收入", "53940", "HKS_I_ISQ_OPREVENUE_DW"),
    "securities": Indicator("revenue_q", "单季营业收入", "54760", "HKS_S_ISQ_REVENUE_DW"),
}


def indicator_expression(indicator: Indicator, report_date: str) -> dict:
    params: dict[str, Any] = {"ReportDate": report_date}
    if indicator.kind == "amount":
        params.update({"ReportType": "0", "Unit": "8"})  # 合并未调整；亿元
    return {"Code": indicator.code, "Name": indicator.name, "Params": params}


def make_expressions(report_dates: list[str], indicators: list[Indicator]) -> tuple[list[dict], list[tuple[str, Indicator]]]:
    expressions, columns = [], []
    for report_date in report_dates:
        for indicator in indicators:
            expressions.append(indicator_expression(indicator, report_date))
            columns.append((report_date, indicator))
    return expressions, columns


def extract_browser_values(rows: list[dict], columns: list[tuple[str, Indicator]]) -> dict[str, dict[str, dict[str, float]]]:
    values: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        code = str(row.get("secucode") or "").upper()
        if not code:
            continue
        for index, (report_date, indicator) in enumerate(columns):
            raw = row.get(f"[{indicator.code}]{index}_0")
            number = _round(raw, 4)
            if number is not None:
                values[code][report_date][indicator.key] = number
    return values


def merge_values(target: dict, incoming: dict) -> None:
    for code, by_date in incoming.items():
        for report_date, row in by_date.items():
            target[code][report_date].update(row)


def complete_derived_financials(values: dict) -> None:
    """Fill fields that the HK browser exposes only through components."""
    for by_date in values.values():
        for row in by_date.values():
            if row.get("parent_net_profit") is None:
                net_profit = row.get("net_profit")
                minority_profit = row.get("minority_profit")
                if net_profit is not None and minority_profit is not None:
                    row["parent_net_profit"] = round(net_profit - minority_profit, 4)
            if row.get("total_assets") is None:
                current_assets = row.get("total_cur_assets")
                noncurrent_assets = row.get("total_nca")
                if current_assets is not None and noncurrent_assets is not None:
                    row["total_assets"] = round(current_assets + noncurrent_assets, 4)
            if row.get("total_liab") is None:
                current_liab = row.get("total_cur_liab")
                noncurrent_liab = row.get("total_ncl")
                if current_liab is not None and noncurrent_liab is not None:
                    row["total_liab"] = round(current_liab + noncurrent_liab, 4)
            if row.get("total_hldr_eqy_inc_min_int") is None:
                parent_equity = row.get("parent_equity")
                minority_equity = row.get("minority_int")
                if parent_equity is not None and minority_equity is not None:
                    row["total_hldr_eqy_inc_min_int"] = round(parent_equity + minority_equity, 4)
            if row.get("total_liab_hldr_eqy") is None:
                liabilities = row.get("total_liab")
                equity = row.get("total_hldr_eqy_inc_min_int")
                if liabilities is not None and equity is not None:
                    row["total_liab_hldr_eqy"] = round(liabilities + equity, 4)


def choose_fiscal_end(by_date: dict[str, dict[str, float]], years: list[int]) -> str | None:
    """Infer fiscal year-end from the largest reported annual revenue in two years."""
    scores = {month_day: 0.0 for month_day in FISCAL_ENDS}
    hits = {month_day: 0 for month_day in FISCAL_ENDS}
    for year in years:
        for month_day in FISCAL_ENDS:
            row = by_date.get(f"{year}-{month_day[:2]}-{month_day[2:]}", {})
            revenue = row.get("revenue")
            assets = row.get("total_assets")
            if revenue is not None or assets is not None:
                hits[month_day] += 1
                scores[month_day] += abs(revenue or 0.0)
    candidates = [month_day for month_day in FISCAL_ENDS if hits[month_day]]
    if not candidates:
        return None
    return max(candidates, key=lambda month_day: (hits[month_day], scores[month_day], month_day))


def parse_quote_snapshots(result: Any) -> dict[str, dict[str, Any]]:
    payload = parse_prefixed_data(result)
    data = (payload.get("data") or {}) if isinstance(payload, dict) else {}
    snapshot = data.get("snapshot") or {}
    # get-market-real-data-v2 uses a compact object instead of the vector
    # returned by get-stock-real-data.  Monetary/share values are in thousands.
    if not snapshot and data:
        quotes = {}
        for raw_code, compact in data.items():
            if not isinstance(compact, dict):
                continue
            quotes[raw_code.upper()] = {
                "prod_code": raw_code.upper(),
                "prod_name": compact.get("abbr"),
                "last_px": compact.get("last"),
                "preclose_px": compact.get("pre_close"),
                "open_px": compact.get("open"),
                "high_px": compact.get("high"),
                "low_px": compact.get("low"),
                "business_amount": compact.get("volume"),
                "business_balance": compact.get("amount"),
                "market_value": compact.get("mv") * 1000 if compact.get("mv") is not None else None,
                "circulation_value": compact.get("float_mv") * 1000 if compact.get("float_mv") is not None else None,
                "total_shares": compact.get("total_share") * 1000 if compact.get("total_share") is not None else None,
                "circulation_amount": compact.get("float_share") * 1000 if compact.get("float_share") is not None else None,
                "pe_rate": compact.get("pe"),
                "trade_date": compact.get("date"),
            }
        return quotes
    fields = snapshot.get("fields") or []
    quotes = {}
    for raw_code, vector in snapshot.items():
        if raw_code == "fields" or not isinstance(vector, list):
            continue
        row = dict(zip(fields, vector))
        code = str(row.get("prod_code") or raw_code).upper()
        quotes[code] = row
    return quotes


def quote_code_candidates(code: str) -> tuple[str, str]:
    digits = code.split(".", 1)[0].zfill(5)
    return f"{digits}.HKM", f"{digits}.HKG"


def normalize_quote(row: dict[str, Any] | None, fetched_date: str) -> dict | None:
    if not row:
        return None
    total_mv = _round(row.get("market_value"))
    circ_mv = _round(row.get("circulation_value"))
    total_shares = _round(row.get("total_shares"))
    return {
        "trade_date": fetched_date,
        "price": _round(row.get("last_px"), 2),
        "pe": _round(row.get("static_pe_rate") if row.get("static_pe_rate") is not None else row.get("pe_rate"), 2),
        "pe_ttm": _round(row.get("ttm_pe_rate") if row.get("ttm_pe_rate") is not None else row.get("pe_rate"), 2),
        "pb": _round(row.get("dyn_pb_rate"), 2),
        "total_mv_yi": _round(total_mv / 1e8, 2) if total_mv is not None else None,
        "circ_mv_yi": _round(circ_mv / 1e8, 2) if circ_mv is not None else None,
        "total_share_yi": _round(total_shares / 1e8, 4) if total_shares is not None else None,
    }


def annual_series(by_date: dict[str, dict[str, float]], key: str, fiscal_end: str) -> dict[int, float]:
    result = {}
    suffix = f"-{fiscal_end[:2]}-{fiscal_end[2:]}"
    for report_date, row in by_date.items():
        if report_date.endswith(suffix) and row.get(key) is not None:
            result[int(report_date[:4])] = row[key]
    return dict(sorted(result.items(), reverse=True)[:HISTORY_YEARS])


_QUARTER_BY_MONTH = {"03": 1, "06": 2, "09": 3, "12": 4}


def build_quarterly_revenue(by_date: dict[str, dict[str, float]], fiscal_end: str) -> dict:
    """按日历季度整理单季营收；报告期无对应单季申报时该季留空（不伪造差值）。

    与 A 股 quarterly_revenue 保持同一形状：{"years": [...], "data": {year: {Q1..Q4, FY}}}。
    FY 取该公司实际财年结束日的累计营收（可能不等于任一日历季度列，如 6 月结账的发行人）。
    """
    by_year: dict[int, dict[str, float]] = defaultdict(dict)
    for report_date, row in by_date.items():
        value = row.get("revenue_q")
        if value is None:
            continue
        month = report_date[5:7]
        quarter = _QUARTER_BY_MONTH.get(month)
        if quarter:
            by_year[int(report_date[:4])][f"Q{quarter}"] = value

    annual_revenue = annual_series(by_date, "revenue", fiscal_end)
    years = sorted(set(by_year) | set(annual_revenue), reverse=True)[:QUARTERLY_YEARS + 1]

    data = {}
    for year in years:
        row = {f"Q{q}": by_year.get(year, {}).get(f"Q{q}") for q in (1, 2, 3, 4)}
        row["FY"] = annual_revenue.get(year)
        if any(v is not None for v in row.values()):
            data[year] = row
    return {"years": [year for year in years if year in data], "data": data}


def build_balance_sheet(by_date: dict[str, dict[str, float]], fiscal_end: str,
                        family: str = "general") -> dict | None:
    years = sorted({int(date[:4]) for date in by_date if date.endswith(f"-{fiscal_end[:2]}-{fiscal_end[2:]}")}, reverse=True)
    years = years[:HISTORY_YEARS]
    fields, data = [], {}
    for indicator in BALANCE_INDICATORS:
        series = annual_series(by_date, indicator.key, fiscal_end)
        if series:
            fields.append({"key": indicator.key, "label": indicator.label})
            data[indicator.key] = series
    if not fields:
        return None
    family_names = {
        "general": "一般企业", "bank": "银行", "insurance": "保险",
        "securities": "证券",
    }
    return {
        "comp_type": {
            "general": "1", "bank": "2", "insurance": "3",
            "securities": "4",
        }.get(family, "1"),
        "comp_type_name": f"港股{family_names.get(family, '一般企业')}披露口径",
        "fields": fields,
        "years": years,
        "data": data,
    }


def latest_balance_row(by_date: dict[str, dict[str, float]], fiscal_end: str) -> tuple[str, dict] | None:
    suffix = f"-{fiscal_end[:2]}-{fiscal_end[2:]}"
    dates = sorted((date for date in by_date if date.endswith(suffix)), reverse=True)
    for report_date in dates:
        if by_date[report_date].get("total_assets") is not None:
            return report_date, by_date[report_date]
    return None


def build_stock(entry: dict, quote_row: dict | None, by_date: dict[str, dict[str, float]],
                fiscal_end: str, reporting_currency: str | None = None,
                financial_family: str = "general") -> dict:
    annual = {
        indicator.label: annual_series(by_date, indicator.key, fiscal_end)
        for indicator in ANNUAL_INDICATORS
    }
    annual.update({
        "总资产": annual_series(by_date, "total_assets", fiscal_end),
        "总负债": annual_series(by_date, "total_liab", fiscal_end),
        "流动资产": annual_series(by_date, "total_cur_assets", fiscal_end),
        "流动负债": annual_series(by_date, "total_cur_liab", fiscal_end),
        "存货": annual_series(by_date, "inventories", fiscal_end),
        "货币资金": annual_series(by_date, "money_cap", fiscal_end),
        "交易性金融资产": annual_series(by_date, "trad_asset", fiscal_end),
        "长期借款": annual_series(by_date, "lt_borr", fiscal_end),
    })
    annual = {label: series for label, series in annual.items() if series}

    # 毛利率接口缺失时，以营业收入和营业成本补齐。
    revenue = annual_series(by_date, "revenue", fiscal_end)
    cost = annual_series(by_date, "oper_cost", fiscal_end)
    margin = annual.setdefault("销售毛利率", {})
    for year, amount in revenue.items():
        if year not in margin and amount:
            year_cost = cost.get(year)
            if year_cost is not None:
                margin[year] = round((amount - year_cost) / amount * 100, 2)
    if not margin:
        annual.pop("销售毛利率", None)

    parent_profit = annual_series(by_date, "parent_net_profit", fiscal_end)
    parent_equity = annual_series(by_date, "parent_equity", fiscal_end)
    roe = {}
    for year, profit in parent_profit.items():
        current_equity = parent_equity.get(year)
        if current_equity in (None, 0):
            continue
        previous_equity = parent_equity.get(year - 1)
        average_equity = (current_equity + previous_equity) / 2 if previous_equity is not None else current_equity
        if average_equity:
            roe[year] = round(profit / average_equity * 100, 2)
    if roe:
        annual["ROE"] = roe

    inventory = annual.get("存货", {})
    inv_days = {}
    for year, year_cost in cost.items():
        current = inventory.get(year)
        if not year_cost or current is None:
            continue
        previous = inventory.get(year - 1)
        average = (current + previous) / 2 if previous is not None else current
        inv_days[year] = round(360 * average / year_cost, 1)
    if inv_days:
        annual["存货周转天数"] = inv_days

    period_trend = {}
    for report_date, row in by_date.items():
        if not report_date.endswith(f"-{fiscal_end[:2]}-{fiscal_end[2:]}"):
            continue
        compact = report_date.replace("-", "")
        values = {}
        for key, label in (("revenue", "营业收入"), ("inventories", "存货"),
                           ("notes_receiv", "应收票据"), ("accounts_receiv", "应收账款")):
            if row.get(key) is not None:
                values[label] = row[key]
        if values:
            period_trend[compact] = values

    ev_items = None
    latest = latest_balance_row(by_date, fiscal_end)
    if latest:
        report_date, row = latest
        ev_items = {
            "报告期": report_date.replace("-", ""),
            "短期借款": row.get("st_borr", 0) or 0,
            "一年内到期的非流动负债": row.get("non_cur_liab_due_1y", 0) or 0,
            "长期借款": row.get("lt_borr", 0) or 0,
            "应付债券": row.get("bond_payable", 0) or 0,
            "租赁负债": 0,
            "少数股东权益": row.get("minority_int", 0) or 0,
            "货币资金": row.get("money_cap", 0) or 0,
            "交易性金融资产": row.get("trad_asset", 0) or 0,
        }

    quote = normalize_quote(quote_row, datetime.now().strftime("%Y%m%d"))
    # 兜底市净率只允许财务币种为港元的公司：行情市值是港元，混用美元/人民币
    # 账面权益会放大数倍。
    if quote and quote.get("pb") is None and latest and reporting_currency in ("港元", "港币"):
        equity = latest[1].get("parent_equity") or latest[1].get("total_hldr_eqy_inc_min_int")
        if equity and quote.get("total_mv_yi") is not None:
            quote["pb"] = round(quote["total_mv_yi"] / equity, 2)

    return {
        "code": entry["code"],
        "name": entry["name"],
        "industry": entry.get("industry"),
        "market": "HK",
        "source": {
            "quote_mcp": QUOTE_SERVER,
            "quote_code": (quote_row or {}).get("prod_code"),
            "financial_mcp": FINANCIAL_SERVER,
            "financial_code": entry["code"],
            "financial_category": HK_STOCK_CATEGORY_ID,
            "financial_family": financial_family,
        },
        "quote": quote,
        "employees": None,
        "annual": annual,
        "quarterly_revenue": build_quarterly_revenue(by_date, fiscal_end),
        "period_trend": period_trend,
        "ev_items": ev_items,
        "balance_sheet": build_balance_sheet(by_date, fiscal_end, financial_family),
        "fiscal_year_end": fiscal_end,
        "units": {
            "金额": "亿元",
            "销售毛利率": "%",
            "ROE": "%",
            "财务币种": reporting_currency or "未知",
            "行情币种": "港元",
        },
    }


def validate_stock(stock: dict) -> tuple[list[str], list[str]]:
    """Local sanity checks before writing a stock file.

    Returns ``(hard_issues, warnings)``.  Hard issues block the write;
    warnings are recorded but the file is still generated.
    """
    hard: list[str] = []
    warnings: list[str] = []
    annual = stock.get("annual") or {}
    if not annual.get("营业收入"):
        hard.append("缺少营业收入年度序列")
    if not annual.get("总资产"):
        hard.append("缺少总资产年度序列")

    quote = stock.get("quote")
    if not quote:
        warnings.append("无行情快照（可能停牌或已退市）")
    else:
        price = quote.get("price")
        if price is None or price <= 0:
            warnings.append(f"行情价格异常: {price}")
        for field in ("pe", "pe_ttm", "pb"):
            value = quote.get(field)
            if value is not None and not -10000 < value < 10000:
                warnings.append(f"{field} 异常: {value}")

    balance = stock.get("balance_sheet")
    if balance:
        data = balance.get("data") or {}
        assets = data.get("total_assets") or {}
        liabilities = data.get("total_liab") or {}
        equity = data.get("total_hldr_eqy_inc_min_int") or {}
        for year in sorted(set(assets) & set(liabilities) & set(equity), reverse=True)[:3]:
            total = liabilities[year] + equity[year]
            tolerance = max(abs(assets[year]) * 0.01, 1.0)
            if abs(assets[year] - total) > tolerance:
                warnings.append(
                    f"{year} 年资产负债恒等式偏差: 总资产 {assets[year]} vs 负债+权益 {round(total, 4)}"
                )
    return hard, warnings


class HkFetcher:
    def __init__(self):
        self.quote = StreamableHttpMcpClient(McpConfig.from_codex(QUOTE_SERVER), QUOTE_SERVER)
        self.financial = StreamableHttpMcpClient(McpConfig.from_codex(FINANCIAL_SERVER), FINANCIAL_SERVER)

    @property
    def request_count(self) -> int:
        return self.quote.request_count + self.financial.request_count

    def verify_catalog(self) -> None:
        """Verify the undocumented HK category and two critical indicators."""
        checks = (("营业额", "54504"), ("总资产", "54328"))
        for node_name, expected_code in checks:
            result = self.financial.call_tool("get-query-body-for-stock", {
                "CategoryIDs": HK_STOCK_CATEGORY_ID, "NodeName": node_name, "PageNo": 1, "PageSize": 100,
            })
            rows = parse_prefixed_data(result)
            found = False
            for row in rows:
                config_text = row.get("ModObjConfig")
                if not config_text:
                    continue
                try:
                    config = json.loads(config_text)
                    if str((config.get("ApiCodes") or {}).get("港股")) == expected_code:
                        found = True
                        break
                except json.JSONDecodeError:
                    continue
            if not found:
                raise RuntimeError(f"指标目录校验失败: {node_name}({expected_code})")

    def fetch_reporting_currencies(self, codes: list[str]) -> dict[str, str]:
        invalid_codes = [code for code in codes if not HK_FINANCIAL_CODE_PATTERN.fullmatch(code)]
        if invalid_codes:
            raise ValueError(f"港股本位币查询禁止非 .HK 代码: {invalid_codes[:3]}")
        result = self.financial.call_tool("stock-browser", {
            "GilCodes": codes,
            "IndicatorExps": [{"Code": "57844", "Name": "HK_FR_CURRENCY_DW", "Params": {}}],
            "Sorts": [],
        })
        currencies = {}
        for row in parse_browser_rows(result):
            code = str(row.get("secucode") or "").upper()
            value = row.get("[57844]0_0")
            if code and value:
                currencies[code] = str(value)
        return currencies

    def fetch_quotes(self, entries: list[dict]) -> dict[str, dict]:
        candidates = [candidate for entry in entries for candidate in quote_code_candidates(entry["code"])]
        # get-stock-real-data 的向量快照带有 dyn_pb_rate / ttm_pe_rate 等原始估值
        # 字段，避免用港元市值除以非港元账面权益推算市净率。
        result = self.quote.call_tool("get-stock-real-data", {"en_prod_code": ",".join(candidates)})
        snapshots = parse_quote_snapshots(result)
        output = {}
        for entry in entries:
            for candidate in quote_code_candidates(entry["code"]):
                if candidate in snapshots:
                    output[entry["code"]] = snapshots[candidate]
                    break
        return output

    def browser(self, codes: list[str], report_dates: list[str], indicators: list[Indicator]) -> dict:
        invalid_codes = [code for code in codes if not HK_FINANCIAL_CODE_PATTERN.fullmatch(code)]
        if invalid_codes:
            raise ValueError(f"港股财务浏览器禁止非 .HK 代码: {invalid_codes[:3]}")
        invalid_indicators = [item.name for item in indicators if not HK_INDICATOR_NAME_PATTERN.match(item.name)]
        if invalid_indicators:
            raise ValueError(f"港股抓取禁止非 HK_/HKS_ 指标: {invalid_indicators[:3]}")
        expressions, columns = make_expressions(report_dates, indicators)
        result = self.financial.call_tool("stock-browser", {
            "GilCodes": codes,
            "IndicatorExps": expressions,
            "Sorts": [],
        })
        return extract_browser_values(parse_browser_rows(result), columns)

    def fetch_financials(self, entries: list[dict], year_batch: int) -> tuple[dict, dict[str, str], dict[str, str]]:
        codes = [entry["code"] for entry in entries]
        now = datetime.now()
        probe_years = [now.year - 1, now.year - 2]
        probe_dates = [f"{year}-{month_day[:2]}-{month_day[2:]}" for year in probe_years for month_day in FISCAL_ENDS]
        values: dict = defaultdict(lambda: defaultdict(dict))
        probes: dict[str, dict] = {}
        for family, indicators in FAMILY_INDICATORS.items():
            probe_keys = {
                "revenue", "total_assets", "total_cur_assets", "total_nca",
                "total_liab", "total_cur_liab", "total_ncl", "parent_equity",
                "minority_int", "total_hldr_eqy_inc_min_int",
            }
            probe_indicators = [item for item in indicators if item.key in probe_keys]
            probes[family] = self.browser(codes, probe_dates, probe_indicators)
            complete_derived_financials(probes[family])

        fiscal_ends: dict[str, str | None] = {}
        families: dict[str, str] = {}
        for code in codes:
            candidates = []
            for family, family_values in probes.items():
                by_date = family_values.get(code, {})
                fiscal_end = choose_fiscal_end(by_date, probe_years)
                hits = sum(
                    1 for row in by_date.values()
                    if row.get("revenue") is not None or row.get("total_assets") is not None
                )
                if fiscal_end:
                    candidates.append((hits, family == "general", family, fiscal_end, by_date))
            if not candidates:
                fiscal_ends[code] = None
                continue
            _, _, family, fiscal_end, by_date = max(candidates)
            families[code] = family
            fiscal_ends[code] = fiscal_end
            for report_date, row in by_date.items():
                values[code][report_date].update(row)

        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        for code, fiscal_end in fiscal_ends.items():
            family = families.get(code)
            if fiscal_end and family:
                grouped[(family, fiscal_end)].append(code)

        history_years = list(range(now.year - 1, now.year - HISTORY_YEARS - 1, -1))
        for (family, fiscal_end), group_codes in grouped.items():
            for year_group in chunks(history_years, max(1, year_batch)):
                dates = [f"{year}-{fiscal_end[:2]}-{fiscal_end[2:]}" for year in year_group]
                merge_values(values, self.browser(group_codes, dates, FAMILY_INDICATORS[family]))

        # 上面的历史区间每年只取公司自身财年结束日一个报告期（年度累计值足够）。
        # 单季营收需要每年四个日历季末的数据，单独按“族”（不按财年结束日分组，
        # 因为单季指标用的是统一日历报告期）批量补抓最近 QUARTERLY_YEARS+1 年。
        family_codes: dict[str, list[str]] = defaultdict(list)
        for (family, _fiscal_end), group_codes in grouped.items():
            family_codes[family].extend(group_codes)
        quarterly_years = list(range(now.year, now.year - QUARTERLY_YEARS - 1, -1))
        for family, group_codes in family_codes.items():
            quarterly_indicator = QUARTERLY_REVENUE_INDICATORS.get(family)
            if not quarterly_indicator:
                continue
            for year in quarterly_years:
                dates = [f"{year}-{month_day[:2]}-{month_day[2:]}" for month_day in FISCAL_ENDS]
                merge_values(values, self.browser(group_codes, dates, [quarterly_indicator]))
        complete_derived_financials(values)
        return (
            values,
            {code: fiscal_end for code, fiscal_end in fiscal_ends.items() if fiscal_end},
            families,
        )


def load_company_list() -> list[dict]:
    if not HK_STOCK_CSV.exists():
        raise RuntimeError(f"港股公司清单不存在: {HK_STOCK_CSV}")
    companies = []
    with HK_STOCK_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_code = (row.get("股票代码") or "").strip().upper()
            name = (row.get("股票名称") or "").strip()
            if not raw_code or not name:
                continue
            digits = raw_code.split(".", 1)[0].zfill(5)
            companies.append({"code": f"{digits}.HK", "name": name, "industry": None, "market": "HK"})
    return companies


def normalize_test_code(raw: str, company_map: dict[str, dict]) -> dict | None:
    value = raw.strip().upper()
    if value in company_map:
        return company_map[value]
    digits = value.split(".", 1)[0]
    if digits.isdigit():
        return company_map.get(f"{digits.zfill(5)}.HK")
    return None


def write_js(path: Path, snippet: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snippet, encoding="utf-8")


def write_stock_file(version_dir: Path, stock: dict) -> None:
    payload = json.dumps(stock, ensure_ascii=False, separators=(",", ":"))
    write_js(version_dir / "stocks" / f"{stock['code']}.js", f"window.VL_registerStock({payload});\n")


def rebuild_versions_index() -> None:
    """Rebuild data/versions.js with per-market version lists (a_share / hk)."""
    index: dict[str, list[dict]] = {}
    for market in MARKET_DIRS:
        versions = []
        market_dir = DATA_DIR / "versions" / market
        for manifest_path in market_dir.glob("*/manifest.json"):
            try:
                versions.append(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        versions.sort(key=lambda item: item["version"], reverse=True)
        index[market] = versions
    write_js(DATA_DIR / "versions.js", "window.VL_VERSIONS = " + json.dumps(index, ensure_ascii=False, indent=1) + ";\n")


def finalize_version(version_id: str, version_dir: Path, companies: list[dict], meta: dict) -> None:
    payload = json.dumps({"market": "hk", "version": version_id, "companies": companies}, ensure_ascii=False, separators=(",", ":"))
    write_js(version_dir / "companies.js", f"window.VL_registerCompanies({payload});\n")
    manifest = {
        "version": version_id,
        "created_at": meta.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "a_count": 0,
        "hk_count": len(companies),
        "note": meta.get("note", "港股数据"),
        "trade_date_a": None,
        "trade_date_hk": datetime.now().strftime("%Y%m%d"),
    }
    (version_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    rebuild_versions_index()
    print(f"[完成] 版本 {version_id}: 港股 {len(companies)} 家")


def run(args: argparse.Namespace) -> None:
    companies = load_company_list()
    company_map = {entry["code"]: entry for entry in companies}
    if args.test:
        targets = []
        for raw in args.test.split(","):
            entry = normalize_test_code(raw, company_map)
            if entry is None:
                sys.exit(f"错误：在港股公司列表中找不到代码 {raw}")
            targets.append(entry)
        note = "港股测试版本"
    else:
        targets = companies
        note = "港股全量数据"

    if args.resume:
        version_id = args.resume
        version_dir = VERSIONS_DIR / version_id
        if not version_dir.exists():
            sys.exit(f"错误：版本目录不存在: {version_dir}")
        manifest_path = version_dir / "manifest.json"
        created_at = json.loads(manifest_path.read_text()).get("created_at") if manifest_path.exists() else None
    else:
        version_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        version_dir = VERSIONS_DIR / version_id
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    stocks_dir = version_dir / "stocks"
    pending = [entry for entry in targets if not (args.resume and (stocks_dir / f"{entry['code']}.js").exists())]
    skipped = len(targets) - len(pending)
    print(f"[开始] 版本 {version_id}: 港股 {len(targets)} 家（待抓取 {len(pending)}，续传跳过 {skipped}）")

    fetcher = HkFetcher()
    if not args.skip_catalog_check:
        print("[目录] 用 get-query-body-for-stock 校验港股 5030 目录的营业额和总资产指标 ...")
        fetcher.verify_catalog()

    done, failures = skipped, []
    validation_warnings: list[str] = []
    started = time.monotonic()
    for group_index, entry_group in enumerate(chunks(pending, max(1, args.batch_size)), 1):
        try:
            quote_rows = fetcher.fetch_quotes(entry_group)
            values, fiscal_ends, financial_families = fetcher.fetch_financials(entry_group, args.year_batch)
            reporting_currencies = fetcher.fetch_reporting_currencies([entry["code"] for entry in entry_group])
        except Exception as exc:
            codes = [entry["code"] for entry in entry_group]
            failures.extend(codes)
            print(f"  [批次失败] {codes[0]}..{codes[-1]}: {str(exc)[:240]}")
            continue

        for entry in entry_group:
            code = entry["code"]
            fiscal_end = fiscal_ends.get(code)
            by_date = values.get(code, {})
            has_financials = any(row.get("revenue") is not None and row.get("total_assets") is not None for row in by_date.values())
            if not fiscal_end or not has_financials:
                failures.append(code)
                print(f"  [跳过] {code} {entry['name']}: 股票浏览器无可用财务数据")
                continue
            try:
                quote_row = quote_rows.get(code)
                stock = build_stock(
                    entry, quote_row, by_date, fiscal_end,
                    reporting_currency=reporting_currencies.get(code),
                    financial_family=financial_families.get(code, "general"),
                )
                stock["version"] = version_id
                stock["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                hard_issues, soft_warnings = validate_stock(stock)
                if hard_issues:
                    failures.append(code)
                    print(f"  [校验失败] {code} {entry['name']}: {'；'.join(hard_issues)}")
                    continue
                validation_warnings.extend(f"{code} {entry['name']}: {item}" for item in soft_warnings)
                write_stock_file(version_dir, stock)
                done += 1
            except Exception as exc:
                failures.append(code)
                print(f"  [失败] {code} {entry['name']}: {str(exc)[:200]}")

        processed = min(group_index * args.batch_size, len(pending))
        elapsed = (time.monotonic() - started) / 60
        print(f"[进度] {processed}/{len(pending)}（累计成功 {done}，失败 {len(failures)}，MCP 请求 {fetcher.request_count}，{elapsed:.1f} 分钟）")

    fetched_codes = {path.stem for path in stocks_dir.glob("*.js")} if stocks_dir.exists() else set()
    fetched_companies = [
        {"code": entry["code"], "name": entry["name"], "py": pinyin_initials(entry["name"]),
         "industry": entry.get("industry"), "market": "HK"}
        for entry in companies if entry["code"] in fetched_codes
    ]
    finalize_version(version_id, version_dir, fetched_companies, {
        "created_at": created_at, "note": note,
    })
    if failures:
        unique_failures = list(dict.fromkeys(failures))
        failure_path = version_dir / "hk_fetch_failures.txt"
        failure_path.write_text("\n".join(unique_failures) + "\n", encoding="utf-8")
        print(f"[警告] {len(unique_failures)} 家无数据或抓取失败，清单: {failure_path}")
    if validation_warnings:
        warning_path = version_dir / "hk_validation_warnings.txt"
        warning_path.write_text("\n".join(validation_warnings) + "\n", encoding="utf-8")
        print(f"[提示] {len(validation_warnings)} 条软校验警告，清单: {warning_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="通过 XHCJ MCP 抓取港股数据（版本化本地存储）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", help="逗号分隔的港股代码（如 00941,00941.HK）")
    group.add_argument("--full", action="store_true", help="批量抓取港股清单")
    parser.add_argument("--resume", metavar="VERSION_ID", help="续传指定版本，跳过已有股票文件")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="每批股票数（默认 %(default)s）")
    parser.add_argument("--year-batch", type=int, default=DEFAULT_YEAR_BATCH, help="每次股票浏览器查询的年份数（默认 %(default)s）")
    parser.add_argument("--skip-catalog-check", action="store_true", help="跳过 tool 20 指标目录校验")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
