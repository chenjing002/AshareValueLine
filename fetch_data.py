#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据抓取脚本：从 Tushare 拉取全部 A 股数据，写入版本化本地存储。

用法：
    python fetch_data.py --test 600519,000001             # 测试少量公司
    python fetch_data.py --full                           # 全量抓取 A 股
    python fetch_data.py --full --resume 20260823_120000 # 续传中断的版本

数据布局（静态站点直接读取，无需服务器；A 股与港股版本目录严格分离）：
    data/versions.js                                  版本索引（按市场分组）
    data/versions/a_share/<版本号>/manifest.json      版本元信息
    data/versions/a_share/<版本号>/companies.js       公司列表（搜索用）
    data/versions/a_share/<版本号>/stocks/<代码>.js   单只股票全部数据
    data/versions/hk/<版本号>/...                     港股（由 fetch_hk_data.py 写入）
"""

import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import unicodedata

import pandas as pd

try:
    from pypinyin import lazy_pinyin, Style
except ImportError:
    lazy_pinyin = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
# A 股与港股版本目录严格分离：本脚本只读写 versions/a_share/。
MARKET_DIRS = ("a_share", "hk")
VERSIONS_DIR = DATA_DIR / "versions" / "a_share"
A_STOCK_CSV = BASE_DIR / "a_stock_companies_20260408.csv"

HISTORY_YEARS = 10          # 历史年度指标年数
QUARTERLY_YEARS = 3         # 季度营收年数
# 全局限速（接口上限约 400 次/分钟；默认保留 30% 余量，降低代理超时和限流概率）
# 实际吞吐可用 FETCH_RPM / FETCH_WORKERS 环境变量调节。
REQUESTS_PER_MIN = int(os.getenv("FETCH_RPM", "280"))


def load_env_file():
    """读取项目 .env（TUSHARE_TOKEN 等），已存在的环境变量优先。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class RateLimiter:
    """滑动窗口限速器（线程安全）：60 秒内不超过 max_per_min 次请求。"""

    def __init__(self, max_per_min: int):
        self.max_per_min = max_per_min
        self.calls = deque()
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > 60:
                    self.calls.popleft()
                if len(self.calls) < self.max_per_min:
                    self.calls.append(now)
                    return
                sleep_for = 60 - (now - self.calls[0]) + 0.05
            time.sleep(max(sleep_for, 0.05))


class TushareClient:
    def __init__(self):
        import tushare as ts

        token = os.getenv("TUSHARE_TOKEN", "")
        if not token:
            sys.exit("错误：未设置 TUSHARE_TOKEN（环境变量或项目 .env 文件）")
        api_url = os.getenv("TUSHARE_API_URL", "http://lianghua.nanyangqiankun.top")
        self.pro = ts.pro_api(token)
        self.pro._DataApi__token = token
        self.pro._DataApi__http_url = api_url
        self.limiter = RateLimiter(REQUESTS_PER_MIN)
        self.request_count = 0

    def query(self, api_name: str, retries: int = 3, **kwargs) -> pd.DataFrame:
        last_error = None
        for attempt in range(retries):
            self.limiter.wait()
            try:
                self.request_count += 1
                df = self.pro.query(api_name, **kwargs)
                return df if df is not None else pd.DataFrame()
            except Exception as e:
                last_error = e
                message = str(e)
                if "频率超限" in message:
                    # 触发频率限制：等一分钟再试
                    print(f"  [限速] {api_name} 频率超限，等待 61 秒后重试 ({attempt + 1}/{retries})")
                    time.sleep(61)
                else:
                    wait = 2 * (attempt + 1)
                    print(f"  [重试] {api_name} 失败: {message[:120]}，{wait} 秒后重试 ({attempt + 1}/{retries})")
                    time.sleep(wait)
        raise RuntimeError(f"{api_name} 请求失败: {last_error}")


# ---------------------------------------------------------------------------
# 数据整理工具
# ---------------------------------------------------------------------------

def _round(value, digits=4):
    try:
        f = float(value)
        if pd.isna(f):
            return None
        return round(f, digits)
    except (TypeError, ValueError):
        return None


def pinyin_initials(name: str) -> str:
    """公司名的拼音首字母（小写），非中文字符原样保留，如 万科Ａ -> wka、贵州茅台 -> gzmt。"""
    if not name or lazy_pinyin is None:
        return ""
    normalized = unicodedata.normalize("NFKC", name)  # 全角字母转半角
    parts = lazy_pinyin(normalized, style=Style.FIRST_LETTER)
    return "".join(ch.lower() for part in parts for ch in part if ch.isalnum())


def dedup_reports(df: pd.DataFrame, has_report_type: bool = True) -> pd.DataFrame:
    """筛选合并报表(report_type=1)并按报告期去重（保留最新公告）。"""
    if df.empty:
        return df
    if has_report_type and "report_type" in df.columns:
        merged = df[df["report_type"].astype(str) == "1"]
        if not merged.empty:
            df = merged
    if "update_flag" in df.columns:
        # 同一报告期可能有原始披露(0)和更正披露(1)两行，优先保留更正值
        df = df.sort_values("update_flag", ascending=False, kind="stable")
    df = df.drop_duplicates(subset="end_date", keep="first")
    return df.sort_values("end_date", ascending=False)


# ---------------------------------------------------------------------------
# 最新报告期补丁（*_vip 按期整市场拉取）
# 该代理的按股票查询接口数据滞后约一个季度（如 H1 报告缺失），
# 而 *_vip 按期查询是最新的；但 vip 响应有 ~6400 行封顶且不支持翻页，
# 因此只用 vip 覆盖最近几个报告期（数据量小），15 年历史仍走按股票接口。
# ---------------------------------------------------------------------------

VIP_ROW_CAP_WARN = 6300  # 接近该行数说明响应可能被截断


def recent_quarter_ends(until: datetime, count: int = 4) -> list:
    """最近 count 个已到期的季度末（YYYYMMDD，含当季）。"""
    result = []
    year, today = until.year, until.strftime("%Y%m%d")
    for y in range(year - 2, year + 1):
        for md in ("0331", "0630", "0930", "1231"):
            period = f"{y}{md}"
            if period <= today:
                result.append(period)
    return result[-count:]


def ingest_period_df(store: dict, df: pd.DataFrame):
    """将一期 vip 数据并入 {ts_code: {end_date: row}}，只留合并报表并取最新披露。"""
    if df is None or df.empty:
        return
    if "report_type" in df.columns:
        df = df[df["report_type"].astype(str) == "1"]
    if "update_flag" in df.columns:
        df = df.sort_values("update_flag", ascending=False)
    df = df.drop_duplicates(subset="ts_code", keep="first")
    for rec in df.to_dict("records"):
        store.setdefault(rec["ts_code"], {})[str(rec["end_date"])] = rec


def fetch_overlay_stores(client: TushareClient, wanted_codes: set | None = None) -> dict:
    """抓取最近几个报告期的利润表/资产负债表补丁数据。"""
    periods = recent_quarter_ends(datetime.now())
    stores = {"income": {}, "balance": {}}

    def pull(store, api_name, fields, **kwargs):
        try:
            df = client.query(api_name, fields=fields, limit=10000, **kwargs)
        except Exception as e:
            print(f"  [警告] {api_name}({kwargs}) 失败: {str(e)[:120]}")
            return
        if len(df) >= VIP_ROW_CAP_WARN:
            print(f"  [警告] {api_name}({kwargs.get('period')}) 返回 {len(df)} 行，可能被截断")
        if wanted_codes is not None and not df.empty and "ts_code" in df.columns:
            df = df[df["ts_code"].isin(wanted_codes)]
        ingest_period_df(store, df)

    for period in periods:
        pull(stores["income"], "income_vip",
             "ts_code,end_date,report_type,update_flag,revenue,oper_cost,total_profit,n_income_attr_p",
             period=period)
        # 资产负债表按公司类型分片，降低单次行数以避开截断
        for comp_type in ("1", "2", "3", "4"):
            pull(stores["balance"], "balancesheet_vip", BS_ALL_FIELDS,
                 period=period, comp_type=comp_type)
    print(f"[补丁] 最新报告期 {periods}: 利润表 {len(stores['income'])} 家, "
          f"资产负债表 {len(stores['balance'])} 家")
    return stores


def merge_overlay(df: pd.DataFrame, store: dict, ts_code: str) -> pd.DataFrame:
    """将 vip 补丁行合并进按股票查询的历史数据；同一报告期以补丁（更新）为准。"""
    rows = store.get(ts_code)
    if not rows:
        return df
    extra = pd.DataFrame(list(rows.values()))
    combined = extra if df.empty else pd.concat([extra, df], ignore_index=True)
    combined["end_date"] = combined["end_date"].astype(str)
    combined = combined.drop_duplicates(subset="end_date", keep="first")
    return combined.sort_values("end_date", ascending=False)


def annual_series(df: pd.DataFrame, column: str, scale: float = 1e8, digits: int = 4) -> dict:
    """提取年报(1231)数据 -> {年份: 数值}，默认换算为亿元。"""
    result = {}
    if df.empty or column not in df.columns:
        return result
    for _, row in df.iterrows():
        end_date = str(row["end_date"])
        if len(end_date) == 8 and end_date.endswith("1231"):
            value = _round(row[column] / scale if scale != 1 and pd.notna(row[column]) else row[column], digits)
            if value is not None:
                result[int(end_date[:4])] = value
    return result


def limit_years(series_map: dict) -> dict:
    """所有指标统一裁剪为最近 HISTORY_YEARS 年。"""
    all_years = sorted({y for s in series_map.values() for y in s}, reverse=True)
    keep = set(all_years[:HISTORY_YEARS])
    return {
        name: {y: v for y, v in series.items() if y in keep}
        for name, series in series_map.items()
        if any(y in keep for y in series)
    }


def build_quarterly_revenue(income_df: pd.DataFrame) -> dict:
    """
    由利润表构建最近 3 年分季度营收（亿元）。
    Tushare 的 revenue 为年内累计值，单季 = 本期累计 - 上期累计。
    """
    if income_df.empty or "revenue" not in income_df.columns:
        return {"years": [], "data": {}}
    cumulative = {}  # {(year, quarter_idx): 累计营收}
    for _, row in income_df.iterrows():
        end_date = str(row["end_date"])
        if len(end_date) != 8 or pd.isna(row["revenue"]):
            continue
        year, month = int(end_date[:4]), int(end_date[4:6])
        quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(month)
        if quarter:
            cumulative[(year, quarter)] = float(row["revenue"])

    current_year = max((y for y, _ in cumulative), default=None)
    if current_year is None:
        return {"years": [], "data": {}}
    years = sorted({y for y, _ in cumulative if y > current_year - QUARTERLY_YEARS - 1}, reverse=True)[:QUARTERLY_YEARS + 1]

    data = {}
    for year in years:
        row = {}
        for quarter in (1, 2, 3, 4):
            current = cumulative.get((year, quarter))
            if current is None:
                row[f"Q{quarter}"] = None
                continue
            previous = cumulative.get((year, quarter - 1)) if quarter > 1 else 0.0
            row[f"Q{quarter}"] = _round((current - previous) / 1e8) if previous is not None else None
        full_year = cumulative.get((year, 4))
        row["FY"] = _round(full_year / 1e8) if full_year is not None else None
        data[year] = row
    return {"years": years, "data": data}


# ---------------------------------------------------------------------------
# A 股
# ---------------------------------------------------------------------------

# 资产负债表：按报表顺序的完整字段清单（Tushare 默认返回集 + 新准则字段）
BS_META_FIELDS = ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type", "update_flag"]
BS_LABELS = {
    "total_share": "期末总股本", "cap_rese": "资本公积金", "undistr_porfit": "未分配利润",
    "surplus_rese": "盈余公积金", "special_rese": "专项储备", "money_cap": "货币资金",
    "trad_asset": "交易性金融资产", "notes_receiv": "应收票据", "accounts_receiv": "应收账款",
    "oth_receiv": "其他应收款", "prepayment": "预付款项", "div_receiv": "应收股利",
    "int_receiv": "应收利息", "inventories": "存货", "amor_exp": "待摊费用",
    "nca_within_1y": "一年内到期的非流动资产", "sett_rsrv": "结算备付金",
    "loanto_oth_bank_fi": "拆出资金", "premium_receiv": "应收保费", "reinsur_receiv": "应收分保账款",
    "reinsur_res_receiv": "应收分保合同准备金", "pur_resale_fa": "买入返售金融资产",
    "oth_cur_assets": "其他流动资产", "total_cur_assets": "流动资产合计",
    "fa_avail_for_sale": "可供出售金融资产", "htm_invest": "持有至到期投资",
    "lt_eqt_invest": "长期股权投资", "invest_real_estate": "投资性房地产",
    "time_deposits": "定期存款", "oth_assets": "其他资产", "lt_rec": "长期应收款",
    "fix_assets": "固定资产", "cip": "在建工程", "const_materials": "工程物资",
    "fixed_assets_disp": "固定资产清理", "produc_bio_assets": "生产性生物资产",
    "oil_and_gas_assets": "油气资产", "intan_assets": "无形资产", "r_and_d": "研发支出",
    "goodwill": "商誉", "lt_amor_exp": "长期待摊费用", "defer_tax_assets": "递延所得税资产",
    "decr_in_disbur": "发放贷款及垫款", "oth_nca": "其他非流动资产", "total_nca": "非流动资产合计",
    "cash_reser_cb": "现金及存放中央银行款项", "depos_in_oth_bfi": "存放同业和其它金融机构款项",
    "prec_metals": "贵金属", "deriv_assets": "衍生金融资产",
    "rr_reins_une_prem": "应收分保未到期责任准备金", "rr_reins_outstd_cla": "应收分保未决赔款准备金",
    "rr_reins_lins_liab": "应收分保寿险责任准备金", "rr_reins_lthins_liab": "应收分保长期健康险责任准备金",
    "refund_depos": "存出保证金", "ph_pledge_loans": "保户质押贷款",
    "refund_cap_depos": "存出资本保证金", "indep_acct_assets": "独立账户资产",
    "client_depos": "客户资金存款", "client_prov": "客户备付金",
    "transac_seat_fee": "交易席位费", "invest_as_receiv": "应收款项类投资",
    "total_assets": "资产总计", "lt_borr": "长期借款", "st_borr": "短期借款",
    "cb_borr": "向中央银行借款", "depos_ib_deposits": "吸收存款及同业存放",
    "loan_oth_bank": "拆入资金", "trading_fl": "交易性金融负债", "notes_payable": "应付票据",
    "acct_payable": "应付账款", "adv_receipts": "预收款项",
    "sold_for_repur_fa": "卖出回购金融资产款", "comm_payable": "应付手续费及佣金",
    "payroll_payable": "应付职工薪酬", "taxes_payable": "应交税费", "int_payable": "应付利息",
    "div_payable": "应付股利", "oth_payable": "其他应付款", "acc_exp": "预提费用",
    "deferred_inc": "递延收益", "st_bonds_payable": "应付短期债券",
    "payable_to_reinsurer": "应付分保账款", "rsrv_insur_cont": "保险合同准备金",
    "acting_trading_sec": "代理买卖证券款", "acting_uw_sec": "代理承销证券款",
    "non_cur_liab_due_1y": "一年内到期的非流动负债", "oth_cur_liab": "其他流动负债",
    "total_cur_liab": "流动负债合计", "bond_payable": "应付债券", "lt_payable": "长期应付款",
    "specific_payables": "专项应付款", "estimated_liab": "预计负债",
    "defer_tax_liab": "递延所得税负债", "defer_inc_non_cur_liab": "递延收益-非流动负债",
    "oth_ncl": "其他非流动负债", "total_ncl": "非流动负债合计",
    "depos_oth_bfi": "同业和其它金融机构存放款项", "deriv_liab": "衍生金融负债",
    "depos": "吸收存款", "agency_bus_liab": "代理业务负债", "oth_liab": "其他负债",
    "prem_receiv_adva": "预收保费", "depos_received": "存入保证金",
    "ph_invest": "保户储金及投资款", "reser_une_prem": "未到期责任准备金",
    "reser_outstd_claims": "未决赔款准备金", "reser_lins_liab": "寿险责任准备金",
    "reser_lthins_liab": "长期健康险责任准备金", "indept_acc_liab": "独立账户负债",
    "pledge_borr": "质押借款", "indem_payable": "应付赔付款",
    "policy_div_payable": "应付保单红利", "total_liab": "负债合计",
    "treasury_share": "减:库存股", "ordin_risk_reser": "一般风险准备",
    "forex_differ": "外币报表折算差额", "invest_loss_unconf": "未确认的投资损失",
    "minority_int": "少数股东权益", "total_hldr_eqy_exc_min_int": "股东权益合计(不含少数股东)",
    "total_hldr_eqy_inc_min_int": "股东权益合计(含少数股东)",
    "total_liab_hldr_eqy": "负债及股东权益总计", "lt_payroll_payable": "长期应付职工薪酬",
    "oth_comp_income": "其他综合收益", "oth_eqt_tools": "其他权益工具",
    "oth_eqt_tools_p_shr": "其他权益工具(优先股)", "lending_funds": "融出资金",
    "acc_receivable": "应收款项", "st_fin_payable": "应付短期融资款", "payables": "应付款项",
    "hfs_assets": "持有待售的资产", "hfs_sales": "持有待售的负债",
    "cost_fin_assets": "以摊余成本计量的金融资产",
    "fair_value_fin_assets": "以公允价值计量且其变动计入其他综合收益的金融资产",
    "contract_assets": "合同资产", "contract_liab": "合同负债",
    "accounts_receiv_bill": "应收票据及应收账款", "accounts_pay": "应付票据及应付账款",
    "oth_rcv_total": "其他应收款(合计)", "fix_assets_total": "固定资产(合计)",
    "cip_total": "在建工程(合计)", "oth_pay_total": "其他应付款(合计)",
    "long_pay_total": "长期应付款(合计)", "debt_invest": "债权投资",
    "oth_debt_invest": "其他债权投资", "oth_eq_invest": "其他权益工具投资",
    "oth_illiq_fin_assets": "其他非流动金融资产", "oth_eq_ppbond": "其他权益工具:永续债",
    "receiv_financing": "应收款项融资", "use_right_assets": "使用权资产",
    "lease_liab": "租赁负债",
}
# 请求字段 = 元信息 + 全部报表科目
BS_ALL_FIELDS = ",".join(["ts_code", "end_date", "report_type", "comp_type", "update_flag"] + list(BS_LABELS.keys()))

# 按公司类型的科目排列顺序（comp_type: 1一般工商业 2银行 3保险 4证券）
BS_EQUITY_ORDER = [
    "total_share", "oth_eqt_tools", "oth_eqt_tools_p_shr", "oth_eq_ppbond", "cap_rese",
    "treasury_share", "special_rese", "surplus_rese", "ordin_risk_reser", "undistr_porfit",
    "oth_comp_income", "forex_differ", "invest_loss_unconf", "total_hldr_eqy_exc_min_int",
    "minority_int", "total_hldr_eqy_inc_min_int", "total_liab_hldr_eqy",
]
BS_ORDER_GENERAL = [
    # 流动资产
    "money_cap", "trad_asset", "deriv_assets", "notes_receiv", "accounts_receiv",
    "accounts_receiv_bill", "receiv_financing", "prepayment", "oth_receiv", "oth_rcv_total",
    "int_receiv", "div_receiv", "inventories", "contract_assets", "hfs_assets", "amor_exp",
    "nca_within_1y", "oth_cur_assets", "total_cur_assets",
    # 非流动资产
    "debt_invest", "oth_debt_invest", "fa_avail_for_sale", "oth_eq_invest", "htm_invest",
    "oth_illiq_fin_assets", "lt_rec", "lt_eqt_invest", "invest_real_estate", "fix_assets",
    "fix_assets_total", "cip", "cip_total", "const_materials", "fixed_assets_disp",
    "produc_bio_assets", "oil_and_gas_assets", "use_right_assets", "intan_assets", "r_and_d",
    "goodwill", "lt_amor_exp", "defer_tax_assets", "oth_nca", "oth_assets", "total_nca",
    "total_assets",
    # 流动负债
    "st_borr", "trading_fl", "deriv_liab", "notes_payable", "acct_payable", "accounts_pay",
    "adv_receipts", "contract_liab", "payroll_payable", "taxes_payable", "int_payable",
    "div_payable", "oth_payable", "oth_pay_total", "acc_exp", "st_bonds_payable", "hfs_sales",
    "non_cur_liab_due_1y", "oth_cur_liab", "total_cur_liab",
    # 非流动负债
    "lt_borr", "bond_payable", "lease_liab", "lt_payable", "long_pay_total",
    "lt_payroll_payable", "specific_payables", "estimated_liab", "deferred_inc",
    "defer_inc_non_cur_liab", "defer_tax_liab", "oth_ncl", "total_ncl", "oth_liab",
    "total_liab",
] + BS_EQUITY_ORDER
BS_ORDER_BANK = [
    # 资产
    "cash_reser_cb", "depos_in_oth_bfi", "prec_metals", "loanto_oth_bank_fi", "money_cap",
    "trad_asset", "deriv_assets", "pur_resale_fa", "int_receiv", "invest_as_receiv",
    "decr_in_disbur", "debt_invest", "oth_debt_invest", "fa_avail_for_sale", "oth_eq_invest",
    "htm_invest", "lt_eqt_invest", "invest_real_estate", "fix_assets", "use_right_assets",
    "intan_assets", "goodwill", "defer_tax_assets", "oth_assets", "total_assets",
    # 负债
    "cb_borr", "depos_oth_bfi", "loan_oth_bank", "trading_fl", "deriv_liab",
    "sold_for_repur_fa", "depos", "depos_ib_deposits", "st_fin_payable", "acct_payable",
    "payroll_payable", "taxes_payable", "int_payable", "estimated_liab", "bond_payable",
    "lease_liab", "defer_tax_liab", "oth_liab", "total_liab",
] + BS_EQUITY_ORDER
BS_ORDER_INSURANCE = [
    # 资产
    "money_cap", "cash_reser_cb", "depos_in_oth_bfi", "loanto_oth_bank_fi", "trad_asset",
    "deriv_assets", "pur_resale_fa", "premium_receiv", "reinsur_receiv", "reinsur_res_receiv",
    "rr_reins_une_prem", "rr_reins_outstd_cla", "rr_reins_lins_liab", "rr_reins_lthins_liab",
    "int_receiv", "ph_pledge_loans", "refund_depos", "refund_cap_depos", "time_deposits",
    "invest_as_receiv", "fa_avail_for_sale", "htm_invest", "debt_invest", "oth_debt_invest",
    "oth_eq_invest", "lt_eqt_invest", "invest_real_estate", "indep_acct_assets", "fix_assets",
    "use_right_assets", "intan_assets", "goodwill", "defer_tax_assets", "oth_assets",
    "total_assets",
    # 负债
    "st_borr", "cb_borr", "loan_oth_bank", "trading_fl", "deriv_liab", "sold_for_repur_fa",
    "prem_receiv_adva", "payable_to_reinsurer", "payroll_payable", "taxes_payable",
    "int_payable", "indem_payable", "policy_div_payable", "depos_received", "ph_invest",
    "reser_une_prem", "reser_outstd_claims", "reser_lins_liab", "reser_lthins_liab",
    "rsrv_insur_cont", "indept_acc_liab", "lt_borr", "bond_payable", "lease_liab",
    "defer_tax_liab", "estimated_liab", "oth_liab", "total_liab",
] + BS_EQUITY_ORDER
BS_ORDER_SECURITIES = [
    # 资产
    "money_cap", "client_depos", "sett_rsrv", "client_prov", "loanto_oth_bank_fi",
    "trad_asset", "deriv_assets", "pur_resale_fa", "lending_funds", "int_receiv",
    "acc_receivable", "refund_depos", "fa_avail_for_sale", "htm_invest", "debt_invest",
    "oth_debt_invest", "oth_eq_invest", "lt_eqt_invest", "transac_seat_fee", "fix_assets",
    "use_right_assets", "intan_assets", "goodwill", "defer_tax_assets", "oth_assets",
    "total_assets",
    # 负债
    "st_borr", "st_fin_payable", "pledge_borr", "loan_oth_bank", "trading_fl", "deriv_liab",
    "sold_for_repur_fa", "acting_trading_sec", "acting_uw_sec", "payroll_payable",
    "taxes_payable", "int_payable", "payables", "estimated_liab", "lt_borr", "bond_payable",
    "lease_liab", "defer_tax_liab", "oth_liab", "total_liab",
] + BS_EQUITY_ORDER
BS_ORDERS = {"1": BS_ORDER_GENERAL, "2": BS_ORDER_BANK, "3": BS_ORDER_INSURANCE, "4": BS_ORDER_SECURITIES}
COMP_TYPE_NAMES = {"1": "一般工商业", "2": "银行", "3": "保险", "4": "证券"}


def normalize_comp_type(value) -> str | None:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return str(int(float(value)))
    except (TypeError, ValueError):
        return None


def build_balance_sheet(balance_df: pd.DataFrame) -> dict | None:
    """完整资产负债表（年报，亿元）：只保留该公司实际有数的科目，
    并按公司类型（1一般工商业 2银行 3保险 4证券）的报表结构排列。"""
    if balance_df is None or balance_df.empty:
        return None
    comp_type = None
    if "comp_type" in balance_df.columns:
        for value in balance_df["comp_type"]:
            comp_type = normalize_comp_type(value)
            if comp_type:
                break
    annual_rows = []
    for _, row in balance_df.iterrows():
        end_date = str(row["end_date"])
        if len(end_date) == 8 and end_date.endswith("1231"):
            annual_rows.append((int(end_date[:4]), row))
    annual_rows = sorted(annual_rows, key=lambda x: x[0], reverse=True)[:HISTORY_YEARS]
    if not annual_rows:
        return None
    type_order = BS_ORDERS.get(comp_type, BS_ORDER_GENERAL)
    ordered = [c for c in type_order if c in balance_df.columns] + \
              [c for c in BS_LABELS if c not in type_order and c in balance_df.columns] + \
              [c for c in balance_df.columns if c not in BS_LABELS and c not in BS_META_FIELDS]
    fields, data = [], {}
    for column in ordered:
        series = {}
        for year, row in annual_rows:
            value = row.get(column)
            if value is not None and pd.notna(value):
                series[year] = _round(float(value) / 1e8)
        if series:
            fields.append({"key": column, "label": BS_LABELS.get(column, column)})
            data[column] = series
    if not fields:
        return None
    return {
        "comp_type": comp_type,
        "comp_type_name": COMP_TYPE_NAMES.get(comp_type),
        "fields": fields,
        "years": [y for y, _ in annual_rows],
        "data": data,
    }


def fetch_dividends(client: TushareClient, ts_code: str, quote: dict | None) -> dict:
    """
    年度现金分红总额（亿元）。按分红所属年度(end_date)归组：
    同一年度的年报分红 + 中期/特别分红合并；只统计已实施(div_proc=实施)的记录。
    """
    df = client.query("dividend", ts_code=ts_code,
                      fields="ts_code,end_date,div_proc,cash_div_tax,base_share,ex_date")
    result: dict = {}
    if df.empty:
        return result
    df = df[df["div_proc"] == "实施"].drop_duplicates(subset=["end_date", "ex_date"])
    # base_share 缺失时退回当前总股本（亿股 -> 万股）
    fallback_base = None
    if quote and quote.get("total_share_yi"):
        fallback_base = quote["total_share_yi"] * 1e4
    for _, row in df.iterrows():
        end_date = str(row["end_date"])
        cash = row.get("cash_div_tax")
        if len(end_date) != 8 or pd.isna(cash) or float(cash) <= 0:
            continue
        base = row.get("base_share")
        if pd.isna(base):
            base = fallback_base
        if base is None:
            continue
        year = int(end_date[:4])
        amount = float(cash) * float(base) * 1e4 / 1e8  # 元/股 × 万股 -> 亿元
        result[year] = round(result.get(year, 0.0) + amount, 4)
    return result


def fetch_a_bulk(client: TushareClient) -> dict:
    """一次性抓取全市场快照：行情指标 + 员工数。"""
    print("[A股] 抓取全市场快照 ...")
    # 找最近交易日（用上证指数附近任一股票的近期日线）
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
    trade_date = None
    for probe_code in ("000001.SZ", "600519.SH", "600000.SH"):  # 单只股票可能停牌，多备几只
        probe = client.query("daily", ts_code=probe_code, start_date=start, end_date=end)
        if not probe.empty:
            trade_date = str(probe.sort_values("trade_date", ascending=False).iloc[0]["trade_date"])
            break
    if trade_date is None:
        raise RuntimeError("无法确定最近交易日（daily 返回为空）")

    quote_df = client.query(
        "daily_basic", trade_date=trade_date,
        fields="ts_code,trade_date,close,pe,pe_ttm,pb,total_mv,circ_mv,total_share",
    )
    quotes = {}
    for _, row in quote_df.iterrows():
        quotes[row["ts_code"]] = {
            "trade_date": trade_date,
            "price": _round(row.get("close"), 2),
            "pe": _round(row.get("pe"), 2),
            "pe_ttm": _round(row.get("pe_ttm"), 2),
            "pb": _round(row.get("pb"), 2),
            "total_mv_yi": _round(row.get("total_mv") / 1e4 if pd.notna(row.get("total_mv")) else None, 2),   # 万元 -> 亿元
            "circ_mv_yi": _round(row.get("circ_mv") / 1e4 if pd.notna(row.get("circ_mv")) else None, 2),
            "total_share_yi": _round(row.get("total_share") / 1e4 if pd.notna(row.get("total_share")) else None, 4),  # 万股 -> 亿股
        }

    employees = {}
    for exchange in ("SSE", "SZSE", "BSE"):
        try:
            company_df = client.query("stock_company", exchange=exchange, fields="ts_code,employees")
            for _, row in company_df.iterrows():
                if pd.notna(row.get("employees")):
                    employees[row["ts_code"]] = int(row["employees"])
        except Exception as e:
            print(f"  [警告] stock_company({exchange}) 获取失败: {e}")

    print(f"[A股] 快照完成: 交易日 {trade_date}, 行情 {len(quotes)} 只, 员工数 {len(employees)} 家")
    return {"trade_date": trade_date, "quotes": quotes, "employees": employees}


def build_period_trend(income_df: pd.DataFrame, balance_df: pd.DataFrame) -> dict:
    """
    近 6 个会计年度各报告期的同期对比数据（亿元）：
    营业收入为累计值，存货/应收为期末值。前端按 Q1/H1/Q3/年度 选择同期比较。
    """
    window_start = f"{datetime.now().year - 5}0101"
    result = {}

    def bucket(end_date):
        return result.setdefault(end_date, {})

    if not income_df.empty and "revenue" in income_df.columns:
        for _, row in income_df.iterrows():
            end_date = str(row["end_date"])
            if end_date >= window_start and len(end_date) == 8 and pd.notna(row["revenue"]):
                bucket(end_date)["营业收入"] = _round(float(row["revenue"]) / 1e8)

    balance_columns = [("inventories", "存货"), ("notes_receiv", "应收票据"),
                       ("accounts_receiv", "应收账款"), ("accounts_receiv_bill", "应收票据及应收账款")]
    if not balance_df.empty:
        for _, row in balance_df.iterrows():
            end_date = str(row["end_date"])
            if end_date < window_start or len(end_date) != 8:
                continue
            for column, label in balance_columns:
                value = row.get(column)
                if value is not None and pd.notna(value):
                    bucket(end_date)[label] = _round(float(value) / 1e8)
    return result


def compute_ttm_profit(income_df: pd.DataFrame) -> float | None:
    """滚动 12 个月归母净利润（亿元）：最新累计 + 上年全年 - 上年同期累计。"""
    if income_df.empty or "n_income_attr_p" not in income_df.columns:
        return None
    cumulative = {}
    for _, row in income_df.iterrows():
        end_date = str(row["end_date"])
        if len(end_date) == 8 and pd.notna(row["n_income_attr_p"]):
            quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(int(end_date[4:6]))
            if quarter:
                cumulative[(int(end_date[:4]), quarter)] = float(row["n_income_attr_p"]) / 1e8
    if not cumulative:
        return None
    year, quarter = max(cumulative)
    if quarter == 4:
        return cumulative[(year, 4)]
    prior_fy = cumulative.get((year - 1, 4))
    prior_same = cumulative.get((year - 1, quarter))
    if prior_fy is None or prior_same is None:
        return None
    return cumulative[(year, quarter)] + prior_fy - prior_same


def apply_pe_fallback(quote: dict, income_df: pd.DataFrame, indicators: dict):
    """Tushare 亏损股 PE 为空：用 总市值/归母净利润 计算（允许负值），并标记为计算值。"""
    total_mv = quote.get("total_mv_yi")
    if total_mv is None:
        return
    if quote.get("pe") is None:
        profits = indicators.get("归母净利润") or {}
        if profits:
            latest = profits[max(profits)]
            if latest:
                quote["pe"] = round(total_mv / latest, 2)
                quote["pe_calc"] = True
    if quote.get("pe_ttm") is None:
        ttm = compute_ttm_profit(income_df)
        if ttm:
            quote["pe_ttm"] = round(total_mv / ttm, 2)
            quote["pe_ttm_calc"] = True


def fetch_a_stock(client: TushareClient, ts_code: str, name: str, industry: str,
                  bulk: dict, overlays: dict) -> dict:
    start_date = f"{datetime.now().year - HISTORY_YEARS}0101"
    end_date = datetime.now().strftime("%Y%m%d")

    income = dedup_reports(client.query(
        "income", ts_code=ts_code, start_date=start_date, end_date=end_date,
        fields="ts_code,end_date,report_type,update_flag,revenue,oper_cost,total_profit,n_income_attr_p"))
    balance = dedup_reports(client.query(
        "balancesheet", ts_code=ts_code, start_date=start_date, end_date=end_date,
        fields=BS_ALL_FIELDS))
    cashflow = dedup_reports(client.query(
        "cashflow", ts_code=ts_code, start_date=start_date, end_date=end_date,
        fields="ts_code,end_date,report_type,n_cashflow_act"))
    fina = dedup_reports(client.query(
        "fina_indicator", ts_code=ts_code, start_date=start_date, end_date=end_date,
        fields="ts_code,end_date,roe,grossprofit_margin"), has_report_type=False)

    # 用最新报告期补丁修正按股票接口的滞后（如最新半年报）
    income = merge_overlay(income, overlays["income"], ts_code)
    balance = merge_overlay(balance, overlays["balance"], ts_code)

    quote = dict(bulk["quotes"][ts_code]) if ts_code in bulk["quotes"] else None

    # 分红为可选数据：接口偶发失败不应导致整只股票抓取失败
    try:
        dividends = fetch_dividends(client, ts_code, quote)
    except Exception as e:
        dividends = {}
        print(f"  [警告] dividend({ts_code}) 获取失败: {str(e)[:100]}")
    # 只保留已出年报年份的分红，避免因当年中期分红出现只有分红一行的年份列
    revenue_years = set(annual_series(income, "revenue"))
    if revenue_years:
        latest_annual_year = max(revenue_years)
        dividends = {y: v for y, v in dividends.items() if y <= latest_annual_year}

    indicators = limit_years({
        "营业收入": annual_series(income, "revenue"),
        "利润总额": annual_series(income, "total_profit"),
        "归母净利润": annual_series(income, "n_income_attr_p"),
        "销售毛利率": annual_series(fina, "grossprofit_margin", scale=1, digits=2),
        "ROE": annual_series(fina, "roe", scale=1, digits=2),
        "总资产": annual_series(balance, "total_assets"),
        "总负债": annual_series(balance, "total_liab"),
        "流动资产": annual_series(balance, "total_cur_assets"),
        "流动负债": annual_series(balance, "total_cur_liab"),
        "存货": annual_series(balance, "inventories"),
        "货币资金": annual_series(balance, "money_cap"),
        "交易性金融资产": annual_series(balance, "trad_asset"),
        "长期借款": annual_series(balance, "lt_borr"),
        "经营现金净额": annual_series(cashflow, "n_cashflow_act"),
        "分红": dividends,
    })

    # 销售毛利率：fina_indicator 缺失的年份用 (营收-营业成本)/营收 补齐
    margin = indicators.get("销售毛利率", {})
    revenue_series = annual_series(income, "revenue")
    cost_series = annual_series(income, "oper_cost")
    window_years = {y for series in indicators.values() for y in series}
    for year, revenue in revenue_series.items():
        if year not in window_years or year in margin or not revenue:
            continue
        cost = cost_series.get(year)
        if cost is not None:
            margin[year] = round((revenue - cost) / revenue * 100, 2)
    if margin:
        indicators["销售毛利率"] = margin

    # 存货周转天数 = 360 × 平均存货 / 营业成本（代理接口不提供 invturn_days，自行计算）
    inventory = indicators.get("存货", {})
    oper_cost = annual_series(income, "oper_cost")
    invturn_days = {}
    for year, cost in oper_cost.items():
        current = inventory.get(year)
        if not cost or current is None:
            continue
        previous = inventory.get(year - 1)
        average = (current + previous) / 2 if previous is not None else current
        invturn_days[year] = round(360 * average / cost, 1)
    if invturn_days:
        indicators["存货周转天数"] = invturn_days

    # EV 计算项：取最新一期合并资产负债表（含季报）
    ev_items = None
    if not balance.empty:
        latest = balance.iloc[0]
        ev_items = {
            "报告期": str(latest["end_date"]),
            "短期借款": _round(latest.get("st_borr", 0) / 1e8 if pd.notna(latest.get("st_borr")) else 0, 2) or 0,
            "一年内到期的非流动负债": _round(latest.get("non_cur_liab_due_1y", 0) / 1e8 if pd.notna(latest.get("non_cur_liab_due_1y")) else 0, 2) or 0,
            "长期借款": _round(latest.get("lt_borr", 0) / 1e8 if pd.notna(latest.get("lt_borr")) else 0, 2) or 0,
            "应付债券": _round(latest.get("bond_payable", 0) / 1e8 if pd.notna(latest.get("bond_payable")) else 0, 2) or 0,
            "租赁负债": _round(latest.get("lease_liab", 0) / 1e8 if pd.notna(latest.get("lease_liab")) else 0, 2) or 0,
            "少数股东权益": _round(latest.get("minority_int", 0) / 1e8 if pd.notna(latest.get("minority_int")) else 0, 2) or 0,
            "货币资金": _round(latest.get("money_cap", 0) / 1e8 if pd.notna(latest.get("money_cap")) else 0, 2) or 0,
            "交易性金融资产": _round(latest.get("trad_asset", 0) / 1e8 if pd.notna(latest.get("trad_asset")) else 0, 2) or 0,
        }

    if quote:
        apply_pe_fallback(quote, income, indicators)

    return {
        "code": ts_code,
        "name": name,
        "industry": industry,
        "market": "A",
        "quote": quote,
        "employees": bulk["employees"].get(ts_code),
        "annual": indicators,
        "quarterly_revenue": build_quarterly_revenue(income),
        "period_trend": build_period_trend(income, balance),
        "ev_items": ev_items,
        "balance_sheet": build_balance_sheet(balance),
        "units": {"金额": "亿元", "销售毛利率": "%", "ROE": "%"},
    }

# ---------------------------------------------------------------------------
# 版本化存储
# ---------------------------------------------------------------------------

def write_js(path: Path, snippet: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snippet, encoding="utf-8")


def write_stock_file(version_dir: Path, stock: dict):
    payload = json.dumps(stock, ensure_ascii=False, separators=(",", ":"))
    write_js(version_dir / "stocks" / f"{stock['code']}.js", f"window.VL_registerStock({payload});\n")


def finalize_version(version_id: str, version_dir: Path, companies: list, meta: dict):
    """写公司列表 / manifest，并更新全局版本索引。"""
    companies_payload = json.dumps(
        {"market": "a_share", "version": version_id, "companies": companies}, ensure_ascii=False, separators=(",", ":"))
    write_js(version_dir / "companies.js", f"window.VL_registerCompanies({companies_payload});\n")

    manifest = {
        "version": version_id,
        "created_at": meta.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "a_count": sum(1 for c in companies if c["market"] == "A"),
        "hk_count": 0,  # 保留版本元数据字段以兼容现有前端
        "note": meta.get("note", ""),
        "trade_date_a": meta.get("trade_date_a"),
        "trade_date_hk": None,
    }
    (version_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 重建 data/versions.js（按市场扫描各自版本目录的 manifest，互不混用）
    index = {}
    for market in MARKET_DIRS:
        versions = []
        for manifest_path in (DATA_DIR / "versions" / market).glob("*/manifest.json"):
            try:
                versions.append(json.loads(manifest_path.read_text(encoding="utf-8")))
            except Exception:
                continue
        versions.sort(key=lambda m: m["version"], reverse=True)
        index[market] = versions
    write_js(DATA_DIR / "versions.js",
             "window.VL_VERSIONS = " + json.dumps(index, ensure_ascii=False, indent=1) + ";\n")
    print(f"[完成] 版本 {version_id}: A股 {manifest['a_count']} 家")
    print(f"[完成] 版本索引已更新: {DATA_DIR / 'versions.js'}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def load_company_list():
    companies = []
    if A_STOCK_CSV.exists():
        df = pd.read_csv(A_STOCK_CSV, encoding="utf-8-sig", dtype=str)
        for _, row in df.iterrows():
            if pd.isna(row["股票代码"]) or pd.isna(row["股票名称"]):
                continue  # 缺失行会产生 NaN，写入 JSON 后不是合法 JS
            industry = row.get("所属行业")
            companies.append({"code": row["股票代码"].strip(), "name": row["股票名称"].strip(),
                              "industry": industry.strip() if isinstance(industry, str) else None,
                              "market": "A"})
    return companies


def normalize_test_code(raw: str, company_map: dict):
    """把用户输入（600519 / 600519.SH）解析为 A 股公司条目。"""
    code = raw.strip().upper()
    if code in company_map:
        return company_map[code]
    if code.isdigit():
        for suffix in (".SH", ".SZ", ".BJ"):
            normalized = code.zfill(6) + suffix
            if normalized in company_map:
                return company_map[normalized]
    return None


def run(args):
    load_env_file()
    client = TushareClient()
    companies = load_company_list()
    company_map = {c["code"]: c for c in companies}

    if args.resume:
        version_id = args.resume
        version_dir = VERSIONS_DIR / version_id
        if not version_dir.exists():
            sys.exit(f"错误：版本目录不存在: {version_dir}")
        created_at = None
        manifest_path = version_dir / "manifest.json"
        if manifest_path.exists():
            created_at = json.loads(manifest_path.read_text()).get("created_at")
    else:
        version_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        version_dir = VERSIONS_DIR / version_id
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if args.test:
        targets, note = [], "测试版本"
        for raw in args.test.split(","):
            entry = normalize_test_code(raw, company_map)
            if entry is None:
                sys.exit(f"错误：在公司列表中找不到代码 {raw}")
            targets.append(entry)
    else:
        note = "全量数据"
        targets = companies

    print(f"[开始] 版本 {version_id}: A股 {len(targets)} 家")
    bulk_a = fetch_a_bulk(client)
    wanted = {t["code"] for t in targets} if args.test else None
    print("[补丁] 抓取最新报告期数据（vip 接口，修正按股票接口的滞后）...")
    overlays = fetch_overlay_stores(client, wanted)

    stocks_dir = version_dir / "stocks"
    skipped = 0
    pending = []
    for entry in targets:
        if args.resume and (stocks_dir / f"{entry['code']}.js").exists():
            skipped += 1
        else:
            pending.append(entry)
    if skipped:
        print(f"[续传] 已存在 {skipped} 家，剩余 {len(pending)} 家")

    done, failed = skipped, []
    started = time.monotonic()

    def process(entry):
        stock = fetch_a_stock(client, entry["code"], entry["name"], entry.get("industry"), bulk_a, overlays)
        stock["version"] = version_id
        stock["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_stock_file(version_dir, stock)

    # 并发抓取：网络延迟是主要瓶颈，多线程共享限速器仍安全低于 400 次/分钟
    workers = max(1, int(os.getenv("FETCH_WORKERS", "8")))
    print(f"[配置] 并发 {workers} 线程，全局限速 {REQUESTS_PER_MIN} 次/分钟")
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(process, entry): entry for entry in pending}
        for index, future in enumerate(as_completed(futures), 1):
            entry = futures[future]
            try:
                future.result()
                done += 1
            except Exception as e:
                failed.append(entry["code"])
                print(f"  [失败] {entry['code']} {entry['name']}: {str(e)[:150]}")
            if index % 50 == 0 or index == len(pending):
                elapsed = time.monotonic() - started
                print(f"[进度] {skipped + index}/{len(targets)} "
                      f"(成功 {done}, 失败 {len(failed)}, 请求 {client.request_count} 次, 用时 {elapsed / 60:.1f} 分钟)")
        pool.shutdown(wait=True)
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        print(f"\n[中断] 已完成 {done} 家。续传命令: "
              f"python fetch_data.py --full --resume {version_id}")

    # 公司列表只收录本版本实际生成了数据文件的公司
    fetched_codes = {p.stem for p in stocks_dir.glob("*.js")} if stocks_dir.exists() else set()
    if lazy_pinyin is None:
        print("[警告] 未安装 pypinyin，公司列表将缺少拼音首字母索引（pip install pypinyin）")
    fetched_companies = [
        {"code": c["code"], "name": c["name"], "py": pinyin_initials(c["name"]),
         "industry": c.get("industry"), "market": c["market"]}
        for c in companies if c["code"] in fetched_codes
    ]
    finalize_version(version_id, version_dir, fetched_companies, {
        "created_at": created_at, "note": note,
        "trade_date_a": bulk_a.get("trade_date"),
    })
    if failed:
        print(f"[警告] {len(failed)} 家失败: {', '.join(failed[:20])}{' ...' if len(failed) > 20 else ''}")
        print(f"        可运行 python fetch_data.py --full --resume {version_id} 重试失败部分")


def main():
    parser = argparse.ArgumentParser(description="Tushare 数据抓取（版本化本地存储）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", help="逗号分隔的 A 股代码，仅抓取这些公司（如 600519,000001）")
    group.add_argument("--full", action="store_true", help="全量抓取")
    parser.add_argument("--resume", metavar="VERSION_ID", help="续传指定版本（跳过已生成的公司）")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
