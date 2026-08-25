import json
import unittest

import fetch_hk_data as hk


def text_result(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


class FetchHkDataTests(unittest.TestCase):
    def test_hk_families_never_include_a_share_indicators(self):
        for indicators in hk.FAMILY_INDICATORS.values():
            for indicator in indicators:
                self.assertRegex(indicator.name, r"^HKS?_")

    def test_browser_rejects_mainland_code(self):
        fetcher = object.__new__(hk.HkFetcher)
        with self.assertRaisesRegex(ValueError, "禁止非 \\.HK 代码"):
            fetcher.browser(
                ["600941.SH"],
                ["2025-12-31"],
                [hk.INDICATOR_BY_KEY["revenue"]],
            )

    def test_parse_browser_rows_and_columns(self):
        nested = {
            "code": "200",
            "data": {
                "rows": [{
                    "secucode": "00700.HK",
                    "[54504]0_0": 6602.57,
                    "[54328]1_0": 17809.95,
                }]
            },
        }
        result = text_result("success:true, data:" + json.dumps({"result": json.dumps(nested)}))
        rows = hk.parse_browser_rows(result)
        columns = [
            ("2024-12-31", hk.INDICATOR_BY_KEY["revenue"]),
            ("2024-12-31", hk.INDICATOR_BY_KEY["total_assets"]),
        ]
        values = hk.extract_browser_values(rows, columns)
        self.assertEqual(values["00700.HK"]["2024-12-31"]["revenue"], 6602.57)
        self.assertEqual(values["00700.HK"]["2024-12-31"]["total_assets"], 17809.95)

    def test_complete_derived_parent_profit(self):
        values = {"00700.HK": {"2024-12-31": {
            "net_profit": 1964.67,
            "minority_profit": 23.94,
            "total_cur_assets": 1921.93,
            "total_nca": 9203.49,
            "total_cur_liab": 1309.85,
            "total_ncl": 3289.65,
            "parent_equity": 5347.15,
            "minority_int": 1178.77,
        }}}
        hk.complete_derived_financials(values)
        row = values["00700.HK"]["2024-12-31"]
        self.assertEqual(row["parent_net_profit"], 1940.73)
        self.assertEqual(row["total_assets"], 11125.42)
        self.assertEqual(row["total_liab"], 4599.5)
        self.assertEqual(row["total_hldr_eqy_inc_min_int"], 6525.92)
        self.assertEqual(row["total_liab_hldr_eqy"], 11125.42)

    def test_choose_fiscal_end(self):
        values = {
            "2024-03-31": {"revenue": 100},
            "2024-06-30": {"revenue": 40},
            "2024-09-30": {"revenue": 70},
            "2024-12-31": {"revenue": 90},
            "2025-03-31": {"revenue": 110},
            "2025-12-31": {"revenue": 80},
        }
        self.assertEqual(hk.choose_fiscal_end(values, [2024, 2025]), "0331")

    def test_parse_v2_quote_and_normalize_units(self):
        payload = {
            "data": {
                "00941.HKM": {
                    "date": 20260824,
                    "last": 82.35,
                    "pe": 9.826,
                    "mv": 1785942087.146,
                    "float_mv": 1711599153.299,
                    "total_share": 21687214.173,
                }
            }
        }
        result = text_result(json.dumps(payload))
        row = hk.parse_quote_snapshots(result)["00941.HKM"]
        quote = hk.normalize_quote(row, "20260824")
        self.assertEqual(quote["price"], 82.35)
        self.assertEqual(quote["total_mv_yi"], 17859.42)
        self.assertEqual(quote["total_share_yi"], 216.8721)
        self.assertEqual(quote["pe"], 9.83)

    def test_build_stock_has_compatible_shape(self):
        by_date = {}
        for year in range(2016, 2026):
            by_date[f"{year}-12-31"] = {
                "revenue": 1000 + year,
                "oper_cost": 600 + year,
                "total_profit": 100 + year,
                "parent_net_profit": 80 + year,
                "total_assets": 3000 + year,
                "total_liab": 1000 + year,
                "total_hldr_eqy_inc_min_int": 2000,
                "money_cap": 500,
            }
        stock = hk.build_stock(
            {"code": "00941.HK", "name": "中国移动", "market": "HK"},
            {"last_px": 80, "market_value": 2_000_000_000_000, "total_shares": 20_000_000_000},
            by_date,
            "1231",
        )
        self.assertEqual(stock["market"], "HK")
        self.assertEqual(stock["source"]["financial_category"], "5030")
        self.assertEqual(stock["source"]["financial_code"], "00941.HK")
        self.assertEqual(len(stock["annual"]["营业收入"]), 10)
        self.assertEqual(len(stock["balance_sheet"]["years"]), 10)
        self.assertEqual(stock["quote"]["total_mv_yi"], 20000.0)
        self.assertIsNone(stock["quote"]["pb"])  # 财务币种未知时禁止本地推算市净率

    def test_pb_fallback_requires_hkd_reporting_currency(self):
        by_date = {"2025-12-31": {
            "revenue": 1000, "total_assets": 3000, "total_liab": 1000,
            "parent_equity": 2000, "total_hldr_eqy_inc_min_int": 2000,
        }}
        quote_row = {"last_px": 80, "market_value": 2_000_000_000_000}
        entry = {"code": "00016.HK", "name": "新鸿基地产", "market": "HK"}
        hkd = hk.build_stock(entry, quote_row, by_date, "1231", reporting_currency="港元")
        self.assertEqual(hkd["quote"]["pb"], 10.0)
        usd = hk.build_stock(entry, quote_row, by_date, "1231", reporting_currency="美元")
        self.assertIsNone(usd["quote"]["pb"])

    def test_validate_stock_hard_and_soft_issues(self):
        stock = {
            "annual": {"营业收入": {2025: 100}, "总资产": {2025: 300}},
            "quote": {"price": 10.0, "pe": 12.0, "pe_ttm": 11.0, "pb": 1.5},
            "balance_sheet": {"data": {
                "total_assets": {2025: 300},
                "total_liab": {2025: 100},
                "total_hldr_eqy_inc_min_int": {2025: 200},
            }},
        }
        hard, warnings = hk.validate_stock(stock)
        self.assertEqual(hard, [])
        self.assertEqual(warnings, [])

        broken = {
            "annual": {"总资产": {2025: 300}},
            "quote": {"price": 0, "pb": 99999},
            "balance_sheet": {"data": {
                "total_assets": {2025: 300},
                "total_liab": {2025: 100},
                "total_hldr_eqy_inc_min_int": {2025: 100},
            }},
        }
        hard, warnings = hk.validate_stock(broken)
        self.assertEqual(hard, ["缺少营业收入年度序列"])
        self.assertEqual(len(warnings), 3)  # 价格异常 + pb 异常 + 恒等式偏差

    def test_quarterly_revenue_families_use_correct_indicator_names(self):
        expected = {
            "general": "HKS_GE_ISQ_REVENUE_DW",
            "bank": "HKS_B_ISQ_OPREVENUE_DW",
            "insurance": "HKS_I_ISQ_OPREVENUE_DW",
            "securities": "HKS_S_ISQ_REVENUE_DW",
        }
        for family, name in expected.items():
            self.assertEqual(hk.QUARTERLY_REVENUE_INDICATORS[family].name, name)

    def test_build_quarterly_revenue_full_discloser(self):
        # 腾讯这类按季披露的公司：Q1-Q4 均有单季值，FY 取年度累计营收
        by_date = {
            "2025-03-31": {"revenue_q": 1800.22, "revenue": 1800.22},
            "2025-06-30": {"revenue_q": 1845.04, "revenue": 3645.26},
            "2025-09-30": {"revenue_q": 1928.69, "revenue": 5573.95},
            "2025-12-31": {"revenue_q": 1943.71, "revenue": 7517.66},
            "2024-12-31": {"revenue_q": 1724.46, "revenue": 6602.57},
        }
        qr = hk.build_quarterly_revenue(by_date, "1231")
        self.assertEqual(qr["years"], [2025, 2024])
        self.assertEqual(qr["data"][2025],
                          {"Q1": 1800.22, "Q2": 1845.04, "Q3": 1928.69, "Q4": 1943.71, "FY": 7517.66})
        self.assertEqual(qr["data"][2024]["Q4"], 1724.46)
        self.assertIsNone(qr["data"][2024]["Q1"])

    def test_build_quarterly_revenue_semiannual_discloser_leaves_quarters_null(self):
        # 汇丰这类只披露中期/年报的公司：单季指标全为 null，仅 FY 有值，不伪造 Q1/Q3
        by_date = {
            "2025-06-30": {"revenue": 490.08},
            "2025-12-31": {"revenue": 978.72},
        }
        qr = hk.build_quarterly_revenue(by_date, "1231")
        self.assertEqual(qr["data"][2025],
                          {"Q1": None, "Q2": None, "Q3": None, "Q4": None, "FY": 978.72})

    def test_build_quarterly_revenue_non_december_fiscal_year(self):
        # 6 月结账发行人：FY 取 0630 累计值，与日历季度列（Q1..Q4）分开存放
        by_date = {
            "2024-09-30": {"revenue_q": 200.0},
            "2024-12-31": {"revenue_q": 210.0},
            "2025-03-31": {"revenue_q": 190.0},
            "2025-06-30": {"revenue_q": 220.0, "revenue": 820.0},
        }
        qr = hk.build_quarterly_revenue(by_date, "0630")
        self.assertEqual(qr["data"][2025]["FY"], 820.0)
        self.assertEqual(qr["data"][2025]["Q1"], 190.0)
        self.assertEqual(qr["data"][2025]["Q2"], 220.0)


if __name__ == "__main__":
    unittest.main()
