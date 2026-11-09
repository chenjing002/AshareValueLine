#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
首页估值筛选数据：从版本内全部 stocks/*.js 汇总每只股票的 PE TTM 与 EV/EBIT，
写入 <版本目录>/screen.js，供首页「低 PE / 低 EV/EBIT」列表直接读取
（避免前端为筛选加载数千个单股文件）。

fetch_data.py / fetch_hk_data.py 在版本完成时自动调用；也可单独为已有版本补建：
    python build_screen.py                          # 两个市场的最新版本
    python build_screen.py --market a_share         # 仅 A 股最新版本
    python build_screen.py --market hk 20260824_080219
"""

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VERSIONS_ROOT = BASE_DIR / "data" / "versions"
MARKETS = ("a_share", "hk")

# 币种别名归一（与前端 CURRENCY_UNIT 一致）；缺省视为人民币（A 股）
CURRENCY_ALIASES = {"人民币元": "CNY", "人民币": "CNY", "港元": "HKD", "港币": "HKD"}

DEBT_KEYS = ("短期借款", "一年内到期的非流动负债", "长期借款", "应付债券", "租赁负债")
CASH_KEYS = ("货币资金", "交易性金融资产")
FINANCIAL_COMP_TYPES = {"2", "3", "4"}  # 资产负债表公司类型：2 银行 3 保险 4 证券
FINANCIAL_INDUSTRIES = {"银行", "证券", "保险"}  # 按一般企业报表披露的金融控股公司（如湘财股份）用行业兜底


def load_stock(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return json.loads(text[text.index("(") + 1:text.rindex(")")])


def currency_of(units: dict, key: str) -> str:
    name = (units or {}).get(key)
    return CURRENCY_ALIASES.get(name, name) if name else "CNY"


def compute_ev_ebit(stock: dict):
    """与前端 computeEV 同口径：EV = 总市值 + 总债务 + 少数股东权益 − 现金；EBIT = 最新年度利润总额。"""
    quote, items = stock.get("quote"), stock.get("ev_items")
    if not quote or not items or quote.get("total_mv_yi") is None:
        return None, None, None
    debt = sum(items.get(k) or 0 for k in DEBT_KEYS)
    cash = sum(items.get(k) or 0 for k in CASH_KEYS)
    ev = quote["total_mv_yi"] + debt + (items.get("少数股东权益") or 0) - cash
    profits = (stock.get("annual") or {}).get("利润总额") or {}
    if not profits:
        return ev, None, None
    year = max(profits, key=int)
    ebit = profits[year]
    ev_ebit = ev / ebit if ebit else None
    return ev, ebit, ev_ebit


def screen_row(stock: dict) -> dict | None:
    quote = stock.get("quote") or {}
    ev, ebit, ev_ebit = compute_ev_ebit(stock)
    row = {
        "code": stock["code"],
        "name": stock.get("name"),
        "industry": stock.get("industry"),
        "price": quote.get("price"),
        "pe_ttm": quote.get("pe_ttm"),
        "pb": quote.get("pb"),
        "mv": quote.get("total_mv_yi"),
        "ev_ebit": round(ev_ebit, 2) if ev_ebit is not None else None,
    }
    if quote.get("pe_ttm_calc"):
        row["pe_calc"] = True  # PE 为按财报自算的回退值（前端标 *）
    # EBIT 为负时 EV/EBIT 无意义（含 EV、EBIT 同为负得正值的情形），只保留 EBIT > 0
    if ebit is not None and ebit <= 0:
        row["ev_ebit"] = None
    # 港股行情币种（港元）与财报币种可能不同，此时 EV/EBIT 跨币种仅供参考
    units = stock.get("units") or {}
    if currency_of(units, "行情币种") != currency_of(units, "财务币种"):
        row["fx_mismatch"] = True
    # 银行 / 保险 / 证券：负债即经营资金、现金含客户资金，EV/EBIT 不适用（前端排除）
    comp_type = (stock.get("balance_sheet") or {}).get("comp_type")
    if comp_type in FINANCIAL_COMP_TYPES or stock.get("industry") in FINANCIAL_INDUSTRIES:
        row["financial"] = True
    if row["pe_ttm"] is None and row["ev_ebit"] is None:
        return None
    return row


def build_screen(market: str, version_dir: Path) -> int:
    version_id = version_dir.name
    rows, trade_date = [], None
    for path in sorted((version_dir / "stocks").glob("*.js")):
        try:
            stock = load_stock(path)
        except Exception as e:
            print(f"  [警告] 读取 {path.name} 失败: {e}")
            continue
        trade_date = trade_date or (stock.get("quote") or {}).get("trade_date")
        row = screen_row(stock)
        if row:
            rows.append(row)
    payload = json.dumps({"market": market, "version": version_id, "trade_date": trade_date, "rows": rows},
                         ensure_ascii=False, separators=(",", ":"))
    (version_dir / "screen.js").write_text(f"window.VL_registerScreen({payload});\n", encoding="utf-8")
    print(f"[筛选] {market}/{version_id}: 写入 screen.js（{len(rows)} 只）")
    return len(rows)


def latest_version_dir(market: str) -> Path | None:
    market_dir = VERSIONS_ROOT / market
    candidates = [d for d in market_dir.iterdir() if (d / "manifest.json").exists()] if market_dir.exists() else []
    return max(candidates, key=lambda d: d.name) if candidates else None


def main():
    parser = argparse.ArgumentParser(description="生成首页估值筛选数据 screen.js")
    parser.add_argument("--market", choices=MARKETS, help="只处理该市场（默认两个市场）")
    parser.add_argument("version", nargs="?", help="版本号（默认最新版本；需同时指定 --market）")
    args = parser.parse_args()
    if args.version and not args.market:
        sys.exit("错误：指定版本号时需同时指定 --market")
    for market in ([args.market] if args.market else MARKETS):
        version_dir = VERSIONS_ROOT / market / args.version if args.version else latest_version_dir(market)
        if not version_dir or not (version_dir / "stocks").is_dir():
            print(f"[筛选] {market}: 未找到版本目录，跳过")
            continue
        build_screen(market, version_dir)


if __name__ == "__main__":
    main()
