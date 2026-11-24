#!/usr/bin/env python3
"""Export a standard-library-only static JSON API from the staged Pages data.

Only latest published versions get stable API URLs. Browser JSONP and the
published version retention policy are unchanged. Never execute JavaScript.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import re
import shutil
from decimal import Decimal
from pathlib import Path

BASE_URL = 'https://chenjing002.github.io/AshareValueLine/'
API = 'api/v1'
MONEY_LABELS = {
    '营业收入', '营业成本', '毛利', '利润总额', '净利润', '归母净利润', '少数股东损益',
    '经营现金净额', '总资产', '总负债', '流动资产', '流动负债', '存货', '货币资金',
    '交易性金融资产', '长期借款', '分红',
}
LABELS = {'归母净利润': '归属于母公司股东的净利润', '经营现金净额': '经营活动产生的现金流量净额',
          'ROE': '净资产收益率（ROE）', '分红': '年度现金分红总额'}
CURRENCIES = {'人民币元': 'CNY', '人民币': 'CNY', '港元': 'HKD', '港币': 'HKD',
              '美元': 'USD', '美元元': 'USD', '欧元': 'EUR', '日元': 'JPY'}


def read_wrapped(path: Path, prefix: str, suffix: str = ');'):
    text = path.read_text(encoding='utf-8').strip()
    if not text.startswith(prefix) or not text.endswith(suffix):
        raise ValueError(f'{path}: unexpected JavaScript wrapper')
    return json.loads(text[len(prefix):-len(suffix)])


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')


def scale(value):
    """Convert already rounded 亿元/亿股; do not invent additional precision."""
    if value is None:
        return None
    return float(Decimal(str(value)) * 100_000_000)


def currency(stock, kind):
    value = stock.get('units', {}).get(kind)
    if value is None:
        return 'HKD' if kind == '行情币种' and stock.get('market') == 'HK' else ('UNKNOWN' if stock.get('market') == 'HK' else 'CNY')
    value = CURRENCIES.get(value, value)
    return value if re.fullmatch('[A-Z]{3}', value) else 'UNKNOWN'


def money_unit(curr):
    return 'CNY_yuan' if curr == 'CNY' else f'{curr}_unit'


def scaled_series(series):
    return {str(k): scale(v) for k, v in series.items()}


def company_payload(stock, market, version, base_url=BASE_URL):
    if market != 'a_share' or stock.get('market') != 'A':
        raise ValueError('Agent 数据接口仅支持 A 股')
    data = copy.deepcopy(stock)
    fin, quote_currency = currency(stock, '财务币种'), currency(stock, '行情币种')
    if fin != 'CNY' or quote_currency != 'CNY':
        raise ValueError('A 股金额必须为人民币')
    unit = 'CNY_yuan'
    years = sorted({str(y) for values in stock.get('annual', {}).values() for y in values})
    fields = {}
    for key, values in stock.get('annual', {}).items():
        if key in MONEY_LABELS:
            field_unit, convert = unit, scale
        elif key == '总股本':
            field_unit, convert = 'shares', scale
        elif key in ('ROE', '销售毛利率'):
            field_unit, convert = 'percent', lambda v: v
        elif key == '存货周转天数':
            field_unit, convert = 'days', lambda v: v
        else:
            raise ValueError(f'{stock["code"]}: unknown annual field {key}; declare its unit first')
        fields[key] = {'label_zh': LABELS.get(key, key), 'unit': field_unit, 'source_field': f'annual.{key}'}
        data['annual'][key] = {y: convert(values.get(y)) for y in years}
    quote = data.get('quote')
    if quote:
        for old, new in [('total_mv_yi', 'total_mv'), ('circ_mv_yi', 'circ_mv'), ('total_share_yi', 'total_share')]:
            if old in quote:
                quote[new] = scale(quote.pop(old))
    bs = data.get('balance_sheet')
    if bs:
        bs['data'] = {key: scaled_series(values) for key, values in bs['data'].items()}
        for field in bs['fields']:
            field['unit'] = 'shares' if field['key'] == 'total_share' else unit
    quarterly = data.get('quarterly_revenue')
    if quarterly:
        quarterly['data'] = {year: scaled_series(values) for year, values in quarterly['data'].items()}
    if data.get('period_trend'):
        data['period_trend'] = {date: scaled_series(values) for date, values in data['period_trend'].items()}
    if data.get('ev_items'):
        data['ev_items'] = {key: value if key == '报告期' else scale(value) for key, value in data['ev_items'].items()}
    if data.get('main_business'):
        for periods in data['main_business'].values():
            for period in periods.values():
                period['total'] = scale(period['total'])
                period['items'] = [[row[0], *[scale(v) for v in row[1:]]] for row in period['items']]
    data.update({
        'schema_version': 1, 'unit': unit, 'market_id': market, 'version': version,
        'years': years, 'annual_fields': fields,
        'source_url': f'{base_url}data/versions/{market}/{version}/stocks/{stock["code"]}.js',
        'source_units': stock.get('units'),
        'units': {'financial_currency': fin, 'financial_amount': unit, 'quote_currency': quote_currency,
                  'quote_amount': money_unit(quote_currency), 'price': f'{quote_currency}/share',
                  'total_share': 'shares', 'percent': '百分数，15 表示 15%，不是 0.15',
                  'days': '天', 'pe': '倍', 'pe_ttm': '倍', 'pb': '倍'},
        'missing_policy': 'null 或缺少字段/年度表示源数据缺失或不适用，不是零。旧版源文件已补零的 EV 分项无法恢复缺失原因。',
        'precision_note': '金额、股数由原文件亿元、亿股乘以 100000000；原文件已舍入，转换不恢复原始精度。',
    })
    return data


def ranking_payloads(screen, market, company_urls, base_url=BASE_URL):
    if market != 'a_share':
        raise ValueError('Agent 榜单仅支持 A 股')
    rows = screen['rows']
    eligible = lambda v: v is not None and 0 < v < 10
    result = []
    for key, title in [('pe_ttm', '低市盈率（PE TTM）'), ('ev_ebit', '低 EV/EBIT')]:
        chosen = sorted((r for r in rows if eligible(r.get(key)) and (key != 'ev_ebit' or not r.get('financial'))), key=lambda r: r[key])
        output = []
        for rank, row in enumerate(chosen, 1):
            output.append({**row, 'rank': rank, 'mv': scale(row.get('mv')), 'company_url': company_urls.get(row['code']),
                           'both_low': eligible(row.get('pe_ttm')) and eligible(row.get('ev_ebit')) and not row.get('financial', False)})
        result.append({'schema_version': 1, 'id': key, 'title': title, 'market': market,
                       'version': screen['version'], 'trade_date': screen.get('trade_date'),
                       'unit': 'CNY_yuan',
                       'columns': {'pe_ttm': {'label_zh': '市盈率（TTM）', 'unit': 'x'},
                                   'ev_ebit': {'label_zh': '企业价值 / 最新年度利润总额（本站口径）', 'unit': 'x'},
                                   'mv': {'label_zh': '总市值', 'unit': 'CNY_yuan'},
                                   'price': {'label_zh': '股价', 'unit': 'CNY/share'}},
                       'criteria': f'0 < {key} < 10' + ('；排除银行、保险、证券' if key == 'ev_ebit' else ''),
                       'sort': f'{key} 升序，相同值保留源 screen.js 顺序，与网页一致',
                       'notes': ['EV/EBIT 沿用本站既有口径，分母为最新年度利润总额，不是标准 EBIT。',
                                 'fx_mismatch=true 表示行情与财报跨币种、未做汇率转换；仅复现网页结果。',
                                 'pe_calc=true 表示 PE 为财报回退计算值。财务数据与行情并非同一日期。',
                                 '缺失 EV 分项在既有数据中可能被补零；不在此接口重算或补值。'],
                       'count': len(output), 'rows': output,
                       'source_url': f'{base_url}data/versions/{market}/{screen["version"]}/screen.js'})
    return result


def build(site: Path, base_url=BASE_URL):
    base_url = base_url.rstrip('/') + '/'
    published = read_wrapped(site / 'data/versions.js', 'window.VL_VERSIONS = ', ';')
    versions = {'a_share': published.get('a_share', [])}
    # This directory is generated and owned by this exporter. Remove stale companies/markets.
    api_dir = site / API
    if api_dir.is_symlink():
        raise ValueError('refusing to replace symlinked API directory')
    if api_dir.exists():
        shutil.rmtree(api_dir)
    write_json(site / API / 'versions.json', versions)
    markets, links = {}, [('数据目录（先读）', f'{API}/catalog.json'), ('已发布版本', f'{API}/versions.json')]
    for market, entries in versions.items():
        if not entries:
            continue
        if market not in ('a_share', 'hk'):
            raise ValueError(f'unknown market: {market}')
        latest = max(entries, key=lambda e: e['version'])
        version = latest['version']
        if not re.fullmatch(r'\d{8}_\d{6}', version):
            raise ValueError(f'invalid version: {version}')
        source = site / 'data/versions' / market / version
        index = read_wrapped(source / 'companies.js', 'window.VL_registerCompanies(')
        companies, urls = [], {}
        for entry in index['companies']:
            code = entry['code']
            if not re.fullmatch(r'[A-Z0-9]+\.[A-Z]+', code):
                raise ValueError(f'invalid company code: {code}')
            path = source / 'stocks' / f'{code}.js'
            url, years = None, []
            if path.exists():
                stock = read_wrapped(path, 'window.VL_registerStock(')
                if stock['code'] != code:
                    raise ValueError(f'{path}: mismatched company code')
                data = company_payload(stock, market, version, base_url)
                relative = f'{API}/{market}/companies/{code}.json'
                write_json(site / relative, data)
                urls[code] = url = base_url + relative
                years = data['years']
            companies.append({**entry, 'available': url is not None, 'history_url': url, 'years': years,
                              'unavailable_reason': None if url else '该版本未生成公司数据，可能抓取失败或缺少财报'})
        relative = f'{API}/{market}/companies.json'
        write_json(site / relative, {'schema_version': 1, 'market': market, 'version': version,
                                    'manifest': latest, 'count': len(companies), 'companies': companies})
        links.append((f'{market} 公司索引', relative))
        screen = read_wrapped(source / 'screen.js', 'window.VL_registerScreen(')
        if screen['version'] != version or screen['market'] != market:
            raise ValueError('screen version/market does not match company snapshot')
        rankings = []
        for ranking in ranking_payloads(screen, market, urls, base_url):
            relative_rank = f'{API}/{market}/rankings/{ranking["id"]}.json'
            write_json(site / relative_rank, ranking)
            rankings.append({'id': ranking['id'], 'title': ranking['title'], 'count': ranking['count'], 'url': base_url + relative_rank})
            links.append((f'{market} {ranking["title"]}', relative_rank))
        markets[market] = {'version': version, 'manifest': latest, 'companies_url': base_url + relative, 'rankings': rankings}
    write_json(site / API / 'catalog.json', {
        'schema_version': 1, 'title': 'A 股公司信息卡 · Agent 数据接口', 'markets': markets,
        'versions_url': base_url + API + '/versions.json',
        'notes': ['纯静态 GET；不支持查询参数，筛选公司请读取索引。',
                  '稳定公司 URL 提供最新已发布版本的全部历史年度，按 years / annual / balance_sheet 读取。',
                  '读取多个文件时比较 version；若不同，重试以避免部署或缓存期间混用快照。',
                  'versions.json 只列网站实际保留的版本。旧版本源数据仍为 JSONP；不执行 JS，可去掉固定回调后 JSON 解析。',
                  'A 股金额为 CNY_yuan，股数为 shares，百分比为 percent（15=15%）。',
                  '本接口只导出 A 股。',
                  '每股价格与股数独立标注；原始舍入精度、抓取时间和行情日期保留。']})
    guide = '# A 股公司信息卡 · Agent 数据入口\n\n> 无需浏览器或执行 JavaScript。直接 HTTP GET 标准 JSON。\n\n'
    guide += '\n'.join(f'- [{label}]({base_url}{url})' for label, url in links)
    guide += ('\n\n先从公司索引按 code、name 或 industry 查找，再读取 history_url，一次取得全部可用历年数据。'
              'annual[科目][年份] 是年度指标，annual_fields 给中文科目和单位，balance_sheet 是完整年度资产负债表。'
              'years 升序，缺年/空值不当零。接口仅覆盖 A 股。\n'
              'A 股金额为人民币元（CNY_yuan），股数为股，百分数 15 代表 15%。'
              'EV/EBIT 分母沿用本站利润总额口径。\n'
              '最新接口的版本号由 catalog.json 指定；多文件读取须核对 version。source_url 指向保留的源文件。'
              '旧版本仅在 versions.json 所列范围内可用。金额从已有亿元数据换算，不能恢复舍入前精度。\n')
    (site / 'llms.txt').write_text(guide, encoding='utf-8')
    (site / 'data.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>数据接口 · 中国上市公司信息卡</title><h1>榜单与公司历年数据</h1><p>静态 JSON，无需 JavaScript。公司索引包含历年数据直链。</p><ul>' + ''.join(f'<li><a href="{html.escape(url)}">{html.escape(label)}</a></li>' for label, url in links) + '</ul><p>A 股金额为人民币元，百分数 15 表示 15%。仅覆盖 A 股，空值不表示零。</p><a href="llms.txt">Agent 使用说明</a></html>', encoding='utf-8')
    return markets


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='从 Pages 暂存目录生成 Agent JSON 接口')
    parser.add_argument('site', type=Path)
    args = parser.parse_args()
    build(args.site)
