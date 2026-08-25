#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value Line 风格 A4 双面打印 PDF 生成器（独立于 Web 应用）。

从本地版本化数据（data/versions/<版本>/stocks/<代码>.js）读取指定公司数据，
生成两页 A4 纵向 PDF：
    第 1 页  关键财务总览（行情/估值、EV、历年指标、季度营收、同期趋势）
    第 2 页  完整资产负债表（按公司类型的科目结构，区分小计/合计）

用法：
    python generate_value_line_pdf.py 000002.SZ [600519 ...] [--version 版本号] [--out output]
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
# A 股与港股版本目录严格分离；默认读 A 股，set_market("hk") 切换。
VERSIONS_ROOT = DATA_DIR / "versions"
MARKET_DIRS = ("a_share", "hk")
VERSIONS_DIR = VERSIONS_ROOT / "a_share"


def set_market(market: str) -> None:
    global VERSIONS_DIR
    if market not in MARKET_DIRS:
        sys.exit(f"错误：未知市场 {market}（可选: {', '.join(MARKET_DIRS)}）")
    VERSIONS_DIR = VERSIONS_ROOT / market

# ---------------------------------------------------------------------------
# 排版配置（集中可调）
# ---------------------------------------------------------------------------

PAGE_W, PAGE_H = A4
MARGIN = 11 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

INK = (0.00, 0.00, 0.00)        # 主文字（纯黑，最高对比）
GRAY = (0.14, 0.16, 0.19)       # 次要文字（深灰近黑，打印清晰）
FAINT = (0.24, 0.27, 0.31)      # 提示文字（深灰，仍清晰）
RULE = (0.20, 0.23, 0.28)       # 分隔线（深）
HAIR = (0.42, 0.46, 0.51)       # 行间分隔线（深灰，印刷清晰）
BAND = (0.910, 0.920, 0.932)    # 小计底色

FONT_CANDIDATES = [
    ("VLSans", "/System/Library/Fonts/STHeiti Light.ttc", 0),
    ("VLSans", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
]
FONT_BOLD_CANDIDATES = [
    ("VLSansBold", "/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("VLSansBold", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
]

F = "VLSans"
FB = "VLSansBold"

INDICATOR_ROWS = [
    # (指标名, 格式: money/percent/days/int)
    ("营业收入", "money"), ("利润总额", "money"), ("销售毛利率", "percent"),
    ("归母净利润", "money"), ("ROE", "percent"), ("总资产", "money"),
    ("总负债", "money"), ("负债率", "percent"), ("账面价值", "money"),
    ("流动资产", "money"), ("流动负债", "money"), ("营运资本", "money"),
    ("存货", "money"), ("存货周转天数", "days"), ("货币资金", "money"),
    ("交易性金融资产", "money"), ("长期借款", "money"), ("经营现金净额", "money"),
    ("分红", "money"), ("员工总数", "int"),
]

TREND_PERIOD_NAMES = {"0331": "一季报(Q1)", "0630": "半年报(H1)", "0930": "三季报(Q3)", "1231": "年报(FY)"}

# 资产负债表小计/合计科目（加粗 + 底色）
BS_SUBTOTALS = {
    "total_cur_assets", "total_nca", "total_assets", "total_cur_liab", "total_ncl",
    "total_liab", "total_hldr_eqy_exc_min_int", "total_hldr_eqy_inc_min_int",
    "total_liab_hldr_eqy",
}


def register_fonts():
    def register(candidates):
        for name, path, index in candidates:
            try:
                pdfmetrics.registerFont(TTFont(name, path, subfontIndex=index))
                return True
            except Exception:
                continue
        return False

    if not register(FONT_CANDIDATES) or not register(FONT_BOLD_CANDIDATES):
        sys.exit("错误：找不到可嵌入的中文字体（STHeiti / Arial Unicode）")


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------

def parse_js_payload(path: Path, prefix: str) -> dict:
    text = path.read_text(encoding="utf-8").strip()
    if not text.startswith(prefix):
        raise ValueError(f"数据文件格式不符: {path}")
    return json.loads(text[len(prefix):].rstrip(";\n").rstrip(")"))


def load_versions(market: str = "a_share") -> list:
    path = DATA_DIR / "versions.js"
    if not path.exists():
        sys.exit("错误：找不到 data/versions.js，请先运行 fetch_data.py")
    text = path.read_text(encoding="utf-8")
    match = re.search(r"window\.VL_VERSIONS\s*=\s*([\[{].*[\]}]);", text, re.S)
    if not match:
        sys.exit("错误：data/versions.js 格式不符")
    data = json.loads(match.group(1))
    if isinstance(data, list):  # 旧格式（未分市场）视为 A 股
        return data if market == "a_share" else []
    return data.get(market) or []


def load_companies(version_id: str) -> list:
    path = VERSIONS_DIR / version_id / "companies.js"
    if not path.exists():
        sys.exit(f"错误：版本 {version_id} 缺少 companies.js")
    return parse_js_payload(path, "window.VL_registerCompanies(")["companies"]


def resolve_code(raw: str, companies: list) -> str:
    code = raw.strip().upper()
    codes = {c["code"] for c in companies}
    if code in codes:
        return code
    if code.isdigit():
        for candidate in (code.zfill(6) + ".SH", code.zfill(6) + ".SZ",
                          code.zfill(6) + ".BJ", code.zfill(5) + ".HK"):
            if candidate in codes:
                return candidate
    names = {c["name"]: c["code"] for c in companies}
    if raw.strip() in names:
        return names[raw.strip()]
    sys.exit(f"错误：版本数据中找不到公司 {raw}")


def load_stock(version_id: str, code: str) -> dict:
    path = VERSIONS_DIR / version_id / "stocks" / f"{code}.js"
    if not path.exists():
        sys.exit(f"错误：找不到数据文件 {path}")
    return parse_js_payload(path, "window.VL_registerStock(")


# ---------------------------------------------------------------------------
# 数值格式化 / 派生指标（与前端口径一致）
# ---------------------------------------------------------------------------

def fmt(value, kind="money"):
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if kind == "money":
        return f"{v:,.2f}"
    if kind == "percent":
        return f"{v:.2f}%"
    if kind == "days":
        return f"{v:,.1f}"
    if kind == "int":
        return f"{v:,.0f}"
    if kind == "price":
        return f"{v:,.2f}"
    return f"{v:,.2f}"


def derive_indicators(annual: dict) -> dict:
    result = {k: dict(v) for k, v in annual.items()}
    assets, liabs = result.get("总资产", {}), result.get("总负债", {})
    cur_a, cur_l = result.get("流动资产", {}), result.get("流动负债", {})
    book, ratio, working = {}, {}, {}
    for year, a in assets.items():
        l = liabs.get(year)
        if l is not None:
            book[year] = a - l
            if a:
                ratio[year] = l / a * 100
    for year, a in cur_a.items():
        l = cur_l.get(year)
        if l is not None:
            working[year] = a - l
    if book:
        result["账面价值"] = book
    if ratio:
        result["负债率"] = ratio
    if working:
        result["营运资本"] = working
    return result


def compute_ev(stock: dict):
    quote, items = stock.get("quote"), stock.get("ev_items")
    if not quote or not items or quote.get("total_mv_yi") is None:
        return None
    debt = sum(items.get(k) or 0 for k in
               ("短期借款", "一年内到期的非流动负债", "长期借款", "应付债券", "租赁负债"))
    cash = (items.get("货币资金") or 0) + (items.get("交易性金融资产") or 0)
    minority = items.get("少数股东权益") or 0
    ev = quote["total_mv_yi"] + debt + minority - cash
    profits = stock.get("annual", {}).get("利润总额") or {}
    ebit = profits[max(profits)] if profits else None
    return {"ev": ev, "debt": debt, "cash": cash, "minority": minority,
            "ebit": ebit, "ev_ebit": (ev / ebit) if ebit else None,
            "period": items.get("报告期", "—"), "mktcap": quote["total_mv_yi"]}


def trend_receivables(entry: dict):
    if not entry:
        return None
    notes, accounts = entry.get("应收票据"), entry.get("应收账款")
    if notes is not None or accounts is not None:
        return (notes or 0) + (accounts or 0)
    return entry.get("应收票据及应收账款")


# ---------------------------------------------------------------------------
# 画布辅助
# ---------------------------------------------------------------------------

class Sheet:
    """canvas 简单封装：文本对齐、细线、色块。y 从页面顶部向下计。"""

    def __init__(self, c: canvas.Canvas):
        self.c = c

    def _y(self, y):
        return PAGE_H - y

    def text(self, x, y, s, font=F, size=7.6, color=INK, align="left"):
        self.c.setFont(font, size)
        self.c.setFillColorRGB(*color)
        if align == "right":
            self.c.drawRightString(x, self._y(y), s)
        elif align == "center":
            self.c.drawCentredString(x, self._y(y), s)
        else:
            self.c.drawString(x, self._y(y), s)

    def fit_text(self, x, y, s, max_w, font=F, size=7, color=INK, min_size=6.0):
        """超宽自动缩字号，最小 6.0pt（印刷可读下限），仍超宽则截断加省略号。"""
        while size > min_size and pdfmetrics.stringWidth(s, font, size) > max_w:
            size -= 0.2
        while s and pdfmetrics.stringWidth(s + "…", font, size) > max_w and len(s) > 2:
            s = s[:-1]
            clipped = True
        self.text(x, y, s, font, size, color)

    def hline(self, x0, x1, y, width=0.4, color=RULE):
        self.c.setLineWidth(width)
        self.c.setStrokeColorRGB(*color)
        self.c.line(x0, self._y(y), x1, self._y(y))

    def band(self, x, y, w, h, color=BAND):
        self.c.setFillColorRGB(*color)
        self.c.rect(x, self._y(y) - h, w, h, stroke=0, fill=1)


def metric_cells(sheet: Sheet, x, y, w, items, value_size=10.0, label_size=6.9, bold=True):
    """一排 label-over-value 指标格。items: [(label, value_str), ...]"""
    if not items:
        return
    cell_w = w / len(items)
    for i, (label, value) in enumerate(items):
        cx = x + i * cell_w
        sheet.text(cx, y, label, F, label_size, GRAY)
        sheet.text(cx, y + value_size + 3.0, value, FB if bold else F, value_size, INK)


# ---------------------------------------------------------------------------
# 第 1 页：关键财务总览
# ---------------------------------------------------------------------------

def draw_page1(sheet: Sheet, stock: dict, meta: dict):
    x0, x1 = MARGIN, MARGIN + CONTENT_W
    y = 16 * mm

    # ---- 页眉 ----
    quote = stock.get("quote") or {}
    name = stock.get("name") or stock["code"]
    sheet.text(x0, y, name, FB, 20, INK)
    name_w = pdfmetrics.stringWidth(name, FB, 20)
    sheet.text(x0 + name_w + 12, y, stock["code"], F, 10, GRAY)
    tag_list = [t for t in (stock.get("industry"),
                            (stock.get("balance_sheet") or {}).get("comp_type_name")) if t]
    tags = "  ·  ".join(dict.fromkeys(tag_list))  # 去重（如 行业=银行 且 报表类型=银行）
    if tags:
        sheet.text(x0 + name_w + 12, y - 13, tags, F, 8.2, GRAY)
    sheet.text(x1, y - 13, "VALUE LINE 财务速览", FB, 7.6, GRAY, "right")
    sheet.text(x1, y, f"数据版本 {meta.get('version', '—')}   行情日 {quote.get('trade_date', '—')}",
               F, 8.4, GRAY, "right")
    y += 6 * mm
    sheet.hline(x0, x1, y, 1.1, INK)

    # ---- 行情 / 估值指标带 ----
    y += 7 * mm
    employees = stock.get("employees")
    metric_cells(sheet, x0, y, CONTENT_W, [
        ("股价(元)", fmt(quote.get("price"), "price")),
        ("市盈率TTM" + ("*" if quote.get("pe_ttm_calc") else ""), fmt(quote.get("pe_ttm"), "days")),
        ("市盈率(静)" + ("*" if quote.get("pe_calc") else ""), fmt(quote.get("pe"), "days")),
        ("市净率", fmt(quote.get("pb"), "days")),
        ("总市值(亿)", fmt(quote.get("total_mv_yi"), "int")),
        ("流通市值(亿)", fmt(quote.get("circ_mv_yi"), "int")),
        ("总股本(亿股)", fmt(quote.get("total_share_yi"), "price")),
        ("员工总数", fmt(employees, "int")),
    ])
    y += 9.6 * mm
    if quote.get("pe_calc") or quote.get("pe_ttm_calc"):
        sheet.text(x0, y - 2, "*PE 为计算值（总市值 ÷ 归母净利润，亏损时为负）", F, 6.6, GRAY)
        y += 2.6 * mm
    sheet.hline(x0, x1, y, 0.4, RULE)

    # ---- EV 指标带 ----
    ev = compute_ev(stock)
    y += 7 * mm
    if ev:
        metric_cells(sheet, x0, y, CONTENT_W, [
            ("EV(亿)", fmt(ev["ev"])),
            ("EV / EBIT", fmt(ev["ev_ebit"], "days") if ev["ev_ebit"] is not None else "—"),
            ("总债务(亿)", fmt(ev["debt"])),
            ("现金(亿)", fmt(ev["cash"])),
            ("少数股东权益(亿)", fmt(ev["minority"])),
            ("EBIT(最新年报,亿)", fmt(ev["ebit"])),
            ("EV报告期", str(ev["period"])),
        ], value_size=9.2)
        y += 8.6 * mm
        sheet.text(x0, y, "EV = 总市值 + 总债务 + 少数股东权益 − 现金（总债务 = 短借+一年内到期非流动负债+长借+应付债券+租赁负债；现金 = 货币资金+交易性金融资产）",
                   F, 6.4, GRAY)
        y += 3.8 * mm
    else:
        sheet.text(x0, y, "EV 指标：缺少行情或资产负债表数据", F, 8.0, GRAY)
        y += 5 * mm
    sheet.hline(x0, x1, y, 0.4, RULE)

    # ---- 底部区高度预留（锚定页底，历年表格自适应撑满中间） ----
    qr = stock.get("quarterly_revenue") or {}
    q_years = qr.get("years") or []
    trend = stock.get("period_trend") or {}
    bottom_rows = max(len(q_years), 3)
    bottom_h = (10 + 6) * mm + bottom_rows * 15.5
    bottom_top = PAGE_H - 14 * mm - bottom_h

    # ---- 历年财务指标表（撑满剩余高度） ----
    y += 9 * mm
    sheet.text(x0, y, "历年财务指标", FB, 10, INK)
    sheet.text(x0 + 68, y, "单位：亿元 / %", F, 7.0, GRAY)

    annual = derive_indicators(stock.get("annual") or {})
    years = sorted({yr for s in annual.values() for yr in s}, reverse=True)[:10]
    if stock.get("employees") is not None and years:
        annual.setdefault("员工总数", {})
        annual["员工总数"].setdefault(years[0], stock["employees"])

    rows = [(label, kind) for label, kind in INDICATOR_ROWS if label in annual]
    if years and rows:
        label_w = 70
        col_w = (CONTENT_W - label_w) / len(years)
        # 行高自适应：撑满历年表可用区域，字号随行高放大
        avail = bottom_top - y - 14 * mm - 7 * mm  # 扣除表头行高与组间距
        row_h = max(11.5, min(26.0, avail / len(rows)))
        body_size = max(7.6, min(8.8, row_h * 0.55))
        y += 5.4 * mm
        for i, yr in enumerate(years):
            sheet.text(x0 + label_w + (i + 1) * col_w, y, str(yr), FB, body_size + 0.4, INK, "right")
        y += 1.6 * mm
        sheet.hline(x0, x1, y, 0.7, INK)
        for label, kind in rows:
            y += row_h
            series = annual[label]
            sheet.text(x0, y, label, F, body_size, GRAY)
            for i, yr in enumerate(years):
                sheet.text(x0 + label_w + (i + 1) * col_w, y,
                           fmt(series.get(yr) if yr in series else series.get(str(yr)), kind),
                           F, body_size, INK, "right")
            sheet.hline(x0, x1, y + row_h * 0.26, 0.6, HAIR)
    else:
        y += 6 * mm
        sheet.text(x0, y, "暂无历年数据", F, 8, GRAY)

    # 历年表行数少（如银行）时底部区上移紧跟表格，避免页中出现大片空白
    bottom_top = min(bottom_top, y + 13 * mm)

    # ---- 底部：季度营收（左） + 同期趋势（右），锚定页底 ----
    left_w = CONTENT_W * 0.46
    right_x = x0 + left_w + 8 * mm

    # 季度营收
    qy = bottom_top
    sheet.text(x0, qy, "季度营收", FB, 10, INK)
    sheet.text(x0 + 46, qy, "单季 · 亿元", F, 7.0, GRAY)
    qy += 5.6 * mm
    if q_years:
        cols = ["Q1", "Q2", "Q3", "Q4", "FY"]
        qcol_w = (left_w - 34) / len(cols)
        for i, qn in enumerate(cols):
            sheet.text(x0 + 34 + (i + 1) * qcol_w, qy, qn, FB, 7.8, GRAY, "right")
        qy += 1.6 * mm
        sheet.hline(x0, x0 + left_w, qy, 0.6, INK)
        for yr in q_years:
            qy += 15.5
            row = (qr.get("data") or {}).get(str(yr)) or {}
            sheet.text(x0, qy, str(yr), F, 7.8, GRAY)
            for i, qn in enumerate(cols):
                sheet.text(x0 + 34 + (i + 1) * qcol_w, qy, fmt(row.get(qn)),
                           FB if qn == "FY" else F, 7.8, INK, "right")
            sheet.hline(x0, x0 + left_w, qy + 3.6, 0.6, HAIR)
    else:
        sheet.text(x0, qy + 3, "暂无季度数据", F, 8.0, GRAY)

    # 同期趋势（默认年度对比；无年报数据时退回最新报告期类型）
    ty = bottom_top
    if trend:
        period_md = "1231" if any(d.endswith("1231") for d in trend) else sorted(trend.keys())[-1][4:]
        t_years = sorted({int(d[:4]) for d in trend if d[4:] == period_md}, reverse=True)[:5]
        sheet.text(right_x, ty, "同期趋势", FB, 10, INK)
        sheet.text(right_x + 46, ty, f"{TREND_PERIOD_NAMES.get(period_md, period_md)} · 累计/期末 · 亿元 · 下行为同比",
                   F, 7.0, GRAY)
        ty += 5.6 * mm
        t_label_w = 44
        t_col_w = (CONTENT_W - left_w - 8 * mm - t_label_w) / max(len(t_years), 1)
        for i, yr in enumerate(t_years):
            sheet.text(right_x + t_label_w + (i + 1) * t_col_w, ty, str(yr), FB, 7.8, GRAY, "right")
        ty += 1.6 * mm
        sheet.hline(right_x, x1, ty, 0.6, INK)
        metrics = [("营业收入", lambda e: (e or {}).get("营业收入")),
                   ("存货", lambda e: (e or {}).get("存货")),
                   ("应收款项", trend_receivables)]
        for label, getter in metrics:
            values = [getter(trend.get(f"{yr}{period_md}")) for yr in t_years]
            if not any(v is not None for v in values):
                continue
            ty += 18.5
            sheet.text(right_x, ty - 4, label, F, 7.8, GRAY)
            for i, yr in enumerate(t_years):
                cx = right_x + t_label_w + (i + 1) * t_col_w
                v = values[i]
                sheet.text(cx, ty - 4, fmt(v), F, 7.8, INK, "right")
                prev = getter(trend.get(f"{yr - 1}{period_md}"))
                if v is not None and prev:
                    pct = (v - prev) / abs(prev) * 100
                    sheet.text(cx, ty + 3.6, f"{pct:+.1f}%", F, 6.4, GRAY, "right")
            sheet.hline(right_x, x1, ty + 5.6, 0.6, HAIR)

    draw_footer(sheet, stock, meta, page=1)


# ---------------------------------------------------------------------------
# 第 2 页：完整资产负债表
# ---------------------------------------------------------------------------

def balance_sections(fields: list) -> list:
    """按科目顺序切分为 资产/负债/股东权益/其他 四段。"""
    sections, current, title = [], [], "资产"
    state = 0  # 0 资产 1 负债 2 股东权益 3 其他
    titles = ["资产", "负债", "股东权益", "其他披露科目"]
    for field in fields:
        current.append(field)
        key = field["key"]
        if state == 0 and key == "total_assets":
            sections.append((titles[0], current)); current, state = [], 1
        elif state == 1 and key == "total_liab":
            sections.append((titles[1], current)); current, state = [], 2
        elif state == 2 and key == "total_liab_hldr_eqy":
            sections.append((titles[2], current)); current, state = [], 3
    if current:
        sections.append((titles[min(state, 3)], current))
    return [(t, f) for t, f in sections if f]


def draw_page2(sheet: Sheet, stock: dict, meta: dict):
    x0, x1 = MARGIN, MARGIN + CONTENT_W
    y = 14 * mm

    bs = stock.get("balance_sheet")
    sheet.text(x0, y, f"{stock.get('name', '')}  资产负债表", FB, 13.5, INK)
    subtitle = "单位：亿元"
    if bs and bs.get("comp_type_name"):
        subtitle = f"{bs['comp_type_name']}报表结构 · " + subtitle
    sheet.text(x1, y, subtitle, F, 7.4, GRAY, "right")
    y += 2.6 * mm
    sheet.hline(x0, x1, y, 0.9, INK)

    if not bs or not bs.get("fields"):
        sheet.text(x0, y + 8 * mm, "暂无资产负债表数据", F, 8.5, GRAY)
        draw_footer(sheet, stock, meta, page=2)
        return

    years = bs.get("years") or []
    fields = bs["fields"]
    data = bs.get("data") or {}
    sections = balance_sections(fields)

    # 自适应行高：内容行 + 每段标题行，压进一页
    total_rows = len(fields) + len(sections)
    header_h = 6 * mm
    avail = PAGE_H - y - header_h - 18 * mm
    row_h = min(13.0, avail / max(total_rows, 1))
    font_size = max(6.4, min(7.6, row_h * 0.62))

    label_w = 132
    col_w = (CONTENT_W - label_w) / max(len(years), 1)

    # 年份表头
    y += header_h
    for i, yr in enumerate(years):
        sheet.text(x0 + label_w + (i + 1) * col_w, y, str(yr), FB, min(8.2, font_size + 0.8), INK, "right")
    sheet.hline(x0, x1, y + 1.5, 0.6, INK)
    y += 1.5

    for title, section_fields in sections:
        y += row_h
        sheet.text(x0, y, title, FB, font_size + 0.8, INK)
        sheet.hline(x0, x1, y + row_h * 0.28, 0.7, RULE)
        for field in section_fields:
            y += row_h
            key, label = field["key"], field["label"]
            is_subtotal = key in BS_SUBTOTALS
            if is_subtotal:
                sheet.band(x0, y - row_h * 0.72, CONTENT_W, row_h * 0.98)
            sheet.fit_text(x0 + (0 if is_subtotal else 5), y, label, label_w - 8,
                           FB if is_subtotal else F, font_size,
                           INK if is_subtotal else GRAY)
            series = data.get(key) or {}
            for i, yr in enumerate(years):
                value = series.get(str(yr), series.get(yr))
                sheet.text(x0 + label_w + (i + 1) * col_w, y, fmt(value),
                           FB if is_subtotal else F, font_size, INK, "right")
            sheet.hline(x0, x1, y + row_h * 0.26, 0.5, HAIR)

    draw_footer(sheet, stock, meta, page=2)


def draw_footer(sheet: Sheet, stock: dict, meta: dict, page: int):
    y = PAGE_H - 8 * mm
    sheet.hline(MARGIN, MARGIN + CONTENT_W, y - 6, 0.3, RULE)
    sheet.text(MARGIN + CONTENT_W, y, f"第 {page} / 2 页", F, 6.4, GRAY, "right")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def generate(code: str, version_id: str, out_dir: Path) -> Path:
    stock = load_stock(version_id, code)
    meta = {"version": version_id}
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{code.replace('.', '_')}_value_line.pdf"

    c = canvas.Canvas(str(out_path), pagesize=A4)
    c.setTitle(f"{stock.get('name', code)} Value Line 财务速览")
    c.setAuthor("My Value Line")
    sheet = Sheet(c)
    draw_page1(sheet, stock, meta)
    c.showPage()
    sheet = Sheet(c)
    draw_page2(sheet, stock, meta)
    c.showPage()
    c.save()
    return out_path


def main():
    parser = argparse.ArgumentParser(description="生成 Value Line 风格 2 页 A4 PDF")
    parser.add_argument("codes", nargs="+", help="股票代码（如 000002.SZ / 600519 / 公司名）")
    parser.add_argument("--version", help="数据版本号（默认最新）")
    parser.add_argument("--market", default="a_share", choices=MARKET_DIRS, help="市场（默认 a_share）")
    parser.add_argument("--out", default="output", help="输出目录（默认 output/）")
    args = parser.parse_args()

    set_market(args.market)
    register_fonts()
    versions = load_versions(args.market)
    version_id = args.version or (versions[0]["version"] if versions else None)
    if not version_id:
        sys.exit("错误：没有可用的数据版本")
    companies = load_companies(version_id)

    for raw in args.codes:
        code = resolve_code(raw, companies)
        out_path = generate(code, version_id, BASE_DIR / args.out)
        print(f"[完成] {code} -> {out_path}")


if __name__ == "__main__":
    main()
