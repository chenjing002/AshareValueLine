# 中国上市公司信息卡

纯静态的股票财务数据查询站点：A 股由 Tushare 抓取，港股由 XHCJ MCP 抓取，结果保存为**版本化本地文件**。网页直接读取本地数据展示，**无需任何后端或本地服务器**。

## 架构

```
fetch_data.py                       A 股数据抓取/更新脚本（Tushare）
fetch_hk_data.py                    港股数据抓取/更新脚本（XHCJ MCP）
docs/hk_mcp_tool_schema.json        港股 MCP 工具与 IndicatorExps 补充 Schema
generate_value_line_pdf.py          单只/多只股票 Value Line 风格 PDF 生成器
batch_generate_all.py               批量为版本内全部 A 股生成 PDF（逐只容错）
collect_industry.py                 按行业筛选并归集已生成的 PDF 到子目录
index.html + static/                静态前端（浏览器直接打开 index.html 即可，含顶部 A股/港股 市场切换）
data/
├── versions.js                     版本索引（按市场分组：{ a_share: [...], hk: [...] }）
└── versions/
    ├── a_share/                    A 股版本目录（fetch_data.py 专用，写读均不涉及港股）
    │   └── 20260823_143000/        一个数据版本（每次抓取新建，永不覆盖旧版本）
    │       ├── manifest.json       版本元信息
    │       ├── companies.js        公司列表（搜索/联想用）
    │       └── stocks/
    │           └── 600519.SH.js    单只股票全部数据
    └── hk/                         港股版本目录（fetch_hk_data.py 专用，写读均不涉及 A 股）
        └── 20260824_090000/
            ├── manifest.json
            ├── companies.js
            └── stocks/
                └── 00700.HK.js
output/                             PDF 输出目录（含按行业归集的子文件夹）
```

数据文件为 `.js` 格式（JSON 外包一层回调），因为浏览器在 `file://` 协议下禁止
`fetch` 本地 JSON，而 `<script>` 标签不受限制。

A 股与港股的数据版本目录严格分离：`fetch_data.py` 只读写 `data/versions/a_share/`，
`fetch_hk_data.py` 只读写 `data/versions/hk/`，前端市场切换器据此加载对应市场的
`companies.js` / `stocks/*.js`，两个市场的数据集不会混用。生成 PDF 时用
`--market hk` 切换 `generate_value_line_pdf.py` / `batch_generate_all.py` 的读取目录
（默认 `a_share`）。

## 使用

### 1. 安装依赖

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. 配置 Token

在项目根目录 `.env` 文件中写入（或导出同名环境变量）：

```
TUSHARE_TOKEN=你的token
```

可选：`TUSHARE_API_URL` 覆盖默认接口地址。

港股脚本从 `~/.codex/config.toml` 读取以下两个 MCP server 的 `url` 和
`http_headers`：

```toml
[mcp_servers.xhcj-mcp-quote-stock-real]
url = "你的行情 MCP URL"
http_headers = { Authorization = "你的认证信息" }

[mcp_servers.xhcj-mcp-financial-market]
url = "你的财务 MCP URL"
http_headers = { Authorization = "你的认证信息" }
```

也可用 `XHCJ_QUOTE_MCP_URL`、`XHCJ_FINANCIAL_MCP_URL` 和
`XHCJ_MCP_AUTHORIZATION` 覆盖配置；认证信息不要提交到仓库。

### 3. 抓取数据

```bash
# 测试少量公司（先验证数据正确性）
./venv/bin/python fetch_data.py --test 600519,000001

# 全量抓取 A 股
./venv/bin/python fetch_data.py --full

# 中断后续传（跳过已完成的公司）
./venv/bin/python fetch_data.py --full --resume 20260823_143000

# 先验证腾讯控股的港股行情、10 年财务和资产负债表
./venv/bin/python fetch_hk_data.py --test 00700

# 腾讯验证通过后才执行港股全量抓取
./venv/bin/python fetch_hk_data.py --full

# 港股中断后续传
./venv/bin/python fetch_hk_data.py --full --resume 20260824_120000
```

- 每次运行 `--test` / `--full`（不带 `--resume`）都会创建一个新的版本目录，
  旧版本数据完整保留，可在网页的版本下拉框中随时切换。
- 全局默认限速 280 次/分钟（低于 Tushare 约 400 次/分钟的限制）。
- 港股失败项写入版本目录的 `hk_fetch_failures.txt`；没有完整港股财报的证券
  不会用 A 股数据补齐，也不会写成成功文件。

### 港股 MCP tool schema 与路由

完整的可校验补充 Schema 见
[`docs/hk_mcp_tool_schema.json`](docs/hk_mcp_tool_schema.json)。MCP 的
`tools/list` 只把 `GilCodes`、`IndicatorExps`、`Sorts` 标成泛型 `list`，且
`get-query-body-for-stock` 的说明只标注股票目录 `2202`。实测港股必须使用：

| 用途 | MCP server | Tool | 关键参数 |
|---|---|---|---|
| 港股行情 | `xhcj-mcp-quote-stock-real` | `get-market-real-data-v2` | `codes=["00700.HKM"]`；创业板可为 `.HKG` |
| 港股指标目录 | `xhcj-mcp-financial-market` | `get-query-body-for-stock` | `CategoryIDs="5030"` |
| 港股财务浏览器 | `xhcj-mcp-financial-market` | `stock-browser` | `GilCodes=["00700.HK"]`，指标只允许 `HK_*` / `HKS_*` |

财务指标表达式的实际结构为：

```json
{
  "Code": "54504",
  "Name": "HKS_GE_IS_REVENUE_DW",
  "Params": {
    "ReportDate": "2025-12-31",
    "ReportType": "0",
    "Unit": "8"
  }
}
```

- `ReportType="0"` 表示合并报表，`Unit="8"` 表示亿元。
- 一般企业、银行、保险、证券分别使用 `5030` 中对应的港股指标族，并映射到
  相同的本地字段结构。
- 财务金额按公司记账本位币保存；例如腾讯为人民币、汇丰为美元。行情仍为港元。
- 部分港股不返回“总资产/总负债”合计行，但会返回流动和非流动分项；脚本只在
  合计行缺失时按会计恒等式求和，并保留原始分项。
- 腾讯控股校验样例：MCP 返回的 2025 年营业额 7,517.66 亿元、销售成本
  3,291.73 亿元、毛利 4,225.93 亿元、税前利润 2,772.49 亿元、归母利润
  2,248.42 亿元，与腾讯年报的人民币百万元披露值换算一致。

#### 两地上市隔离规则

港股和 A 股是两套独立数据路由。即使同一发行人同时在两地上市，
`fetch_hk_data.py` 仍只允许 `.HK` 的 `GilCodes`、港股目录 `5030` 和
`HK_*` / `HKS_*` 指标；明确禁止 `.SH` / `.SZ`、目录 `2202` 和 `S_E_*`
等 A 股指标回退。MCP 没有港股报表时记录失败，不复制同一发行人的 A 股报表。

### 4. 浏览

直接双击打开 `index.html`（file:// 协议即可，无需服务器）。

- 右上角下拉框切换**数据版本**
- 搜索框支持代码（600519）或名称（茅台）联想搜索
- 展示内容：实时行情快照、最近 3 年单季营收、EV 指标（含计算细项）、
  最近 **10 年**历史财务指标

## 生成 Value Line PDF

在已有数据版本的基础上，可离线生成两页 A4 纵向的 Value Line 风格财务速览
PDF（第 1 页关键财务总览，第 2 页完整资产负债表），输出到 `output/`。

```bash
# 单只 / 多只（支持代码、6 位数字或公司名，默认最新版本）
./venv/bin/python generate_value_line_pdf.py 600519 000002.SZ 招商蛇口

# 指定版本与输出目录
./venv/bin/python generate_value_line_pdf.py 600519 --version 20260823_190219 --out output

# 批量为版本内全部 A 股生成（逐只容错，失败不中断，写 batch_failures.log）
./venv/bin/python batch_generate_all.py [版本号]

# 按行业归集已生成的 PDF 到 output/<行业>/ 子目录（行业名在脚本内的 INDUSTRY 变量）
./venv/bin/python collect_industry.py
```

- PDF 文件名格式为 `<代码下划线>_value_line.pdf`（如 `600519_SH_value_line.pdf`）。
- 需要系统中文字体（macOS 的 STHeiti / Arial Unicode），脚本会自动查找并嵌入。
- 全量约 5500 只，单进程约 2～3 分钟完成。

## 数据说明

| 数据 | 来源接口 | 说明 |
|---|---|---|
| 行情快照 | daily_basic（全市场一次拉取） | 股价、PE、PB、市值、股本 |
| 年度指标 | income / balancesheet / cashflow / fina_indicator | 合并报表(report_type=1)，最近 10 年 |
| 最新报告期补丁 | income_vip / balancesheet_vip | 按股票接口约滞后一个季度，用 vip 按期接口覆盖最近 4 个报告期（vip 响应有 ~6400 行截断，只适合小数据量的近期报告期） |
| 资产负债表(完整) | balancesheet | 全部科目动态保留，按公司类型(1一般工商业/2银行/3保险/4证券)的报表结构排序 |
| 季度营收 | income | 单季值 = 本期累计 - 上期累计（Tushare 原始值为年内累计） |
| EV 计算项 | balancesheet 最新一期 | 短期/长期借款、应付债券、租赁负债、少数股东权益、货币资金等 |
| 员工总数 | stock_company | 仅当前值（显示在最新年份列），Tushare 无历史员工数接口 |

港股数据来源：

| 数据 | MCP Tool | 说明 |
|---|---|---|
| 行情快照 | `get-market-real-data-v2` | 港交所主板 `.HKM` / 创业板 `.HKG`，股价、PE、市值、股本 |
| 10 年年度指标 | `get-query-body-for-stock` + `stock-browser` | 港股目录 `5030`，按公司财年末取最近 10 年合并报表 |
| 资产负债表 | `stock-browser` | 港股原始 `HK_*` / `HKS_*` 科目，金额统一换算为本位币亿元 |
| 记账本位币 | `stock-browser` / `HK_FR_CURRENCY_DW` | 区分人民币、港元、美元等财报口径 |

- 金额单位统一为**亿元**；毛利率、ROE、负债率为百分比。
- 衍生指标（账面价值、营运资本、负债率、EV、EV/EBIT）由前端根据已存数据实时计算。
