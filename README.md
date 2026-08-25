# 中国上市公司信息卡

纯静态的股票财务数据查询站点：数据抓取脚本从 Tushare 拉取全部 A 股数据并保存为**版本化本地文件**，网页直接读取本地数据展示，**无需任何后端或本地服务器**。

## 架构

```
fetch_data.py                       数据抓取/更新脚本（唯一联网组件）
generate_value_line_pdf.py          单只/多只股票 Value Line 风格 PDF 生成器
batch_generate_all.py               批量为版本内全部 A 股生成 PDF（逐只容错）
collect_industry.py                 按行业筛选并归集已生成的 PDF 到子目录
index.html + static/                静态前端（浏览器直接打开 index.html 即可）
data/
├── versions.js                     版本索引（前端下拉框数据源）
└── versions/
    └── 20260823_143000/            一个数据版本（每次抓取新建，永不覆盖旧版本）
        ├── manifest.json           版本元信息
        ├── companies.js            公司列表（搜索/联想用）
        └── stocks/
            └── 600519.SH.js        单只股票全部数据
output/                             PDF 输出目录（含按行业归集的子文件夹）
```

数据文件为 `.js` 格式（JSON 外包一层回调），因为浏览器在 `file://` 协议下禁止
`fetch` 本地 JSON，而 `<script>` 标签不受限制。

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

### 3. 抓取数据

```bash
# 测试少量公司（先验证数据正确性）
./venv/bin/python fetch_data.py --test 600519,000001

# 全量抓取 A 股
./venv/bin/python fetch_data.py --full

# 中断后续传（跳过已完成的公司）
./venv/bin/python fetch_data.py --full --resume 20260823_143000
```

- 每次运行 `--test` / `--full`（不带 `--resume`）都会创建一个新的版本目录，
  旧版本数据完整保留，可在网页的版本下拉框中随时切换。
- 全局默认限速 280 次/分钟（低于 Tushare 约 400 次/分钟的限制）。

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

- 金额单位统一为**亿元**；毛利率、ROE、负债率为百分比。
- 衍生指标（账面价值、营运资本、负债率、EV、EV/EBIT）由前端根据已存数据实时计算。
