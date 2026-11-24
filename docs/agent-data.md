# Agent 数据接口

发布时由 `build_pages.py` 调用 `build_agent_data.py`，从同一批网页数据生成纯 JSON。
无需 Node、第三方 Python 包或 JavaScript 执行环境。`file://` 浏览器仍读取原 JSONP。

站点根路径：`https://chenjing002.github.io/AshareValueLine/`。

| 路径 | 内容 |
| --- | --- |
| `llms.txt` | 中文读取说明与直链 |
| `data.html` | 无 JavaScript 的数据导航页 |
| `api/v1/catalog.json` | A 股最新版本及全部接口入口 |
| `api/v1/versions.json` | 网站实际保留的版本，不包含未发布的本地版本 |
| `api/v1/{market}/companies.json` | 公司索引、可用性、年份和 `history_url` |
| `api/v1/{market}/companies/{code}.json` | 公司全部现有年度指标、资产负债表、行情、季报和主营构成 |
| `api/v1/{market}/rankings/pe_ttm.json` | 低 PE 榜单，按网页相同顺序排序 |
| `api/v1/{market}/rankings/ev_ebit.json` | 低 EV/EBIT 榜单，沿用网页金融行业排除规则 |

本接口只提供 A 股，`market` 固定为 `a_share`。公司代码必须保留交易所后缀与前导零。
接口只支持静态 GET；`?year=2025` 等参数不会在服务端过滤数据。

```python
import json
from urllib.request import urlopen

base = "https://chenjing002.github.io/AshareValueLine/"
def get(url):
    with urlopen(url, timeout=30) as response:
        return json.load(response)

catalog = get(base + "api/v1/catalog.json")
market = catalog["markets"]["a_share"]
ranking = get(market["rankings"][0]["url"])
print(ranking["title"], ranking["rows"][:10])
index = get(market["companies_url"])
company = next(c for c in index["companies"] if c["code"] == "600519.SH")
history = get(company["history_url"])
assert history["version"] == index["version"] == market["version"]
print(history["annual_fields"]["营业收入"])
print(history["annual"]["营业收入"])
```

## 数据口径

- `annual[科目][年份]` 按可用年份补齐 `null`；中文标签与单位见 `annual_fields`。
- A 股金额统一为人民币元，`unit=CNY_yuan`；总股本为股，股价为元/股。
  原数据的亿元、亿股乘以 100000000，而不是重命名单元。原有舍入精度不变。
- `ROE` 与销售毛利率用 `percent`：15 表示 15%。这与商业模式探索器的 `pct`（0.15）不同。
- `balance_sheet.fields[].unit` 区分金额和股数。`quote.total_mv`、`circ_mv` 为对应行情币种的元级金额，
  `quote.total_share` 为股，`quote.price` 不缩放。其他财务金额块使用 `units.financial_amount`。
- EV/EBIT 沿用原网站口径：分母实际为最新年度利润总额，不能当作标准 EBIT。
  `fx_mismatch`、`financial`、`pe_calc` 标记均保留。新接口仅覆盖 A 股。
- `null`、缺少字段、缺少年度都不表示零。旧源数据已经将部分 EV 分项补零，导出无法恢复其缺失原因。
- `fetched_at` 为原始抓取时间，`quote_refreshed_at` 为行情刷新时间，`quote.trade_date` 为行情交易日，
  `version` 为快照版本。
- API 的公司直链指向最新已发布版本，部署过程中读取多个文件时应比对 `version`；不一致则重试。
  老版本的 JSONP 文件仍按 `versions.json` 保留；`source_url` 指回当前导出的源文件。

## 本地验证和发布

```sh
python3 -m unittest discover -s tests -p 'test_agent_data.py'
python3 build_pages.py --output _site
python3 -m http.server 8000 --directory _site
```

输出目录必须不存在，避免误删已有数据。生成物不提交；推送源码至 `main` 后，
现有 GitHub Actions 会测试、构建、上传并发布整个站点。仍保留每个市场最新两个浏览器版本，
标准 JSON 接口只复制 A 股最新版本。构建会检查 950 MB 容量上限，并对未知指标单位报错。
