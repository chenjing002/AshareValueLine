import json
import tempfile
import unittest
from pathlib import Path

from build_agent_data import build, company_payload, ranking_payloads, read_wrapped, scale


def sample():
    return {'code': '600519.SH', 'market': 'A', 'name': '贵州茅台', 'units': {'金额': '亿元'},
            'annual': {'营业收入': {'2024': 1.2345, '2025': None}, 'ROE': {'2024': 15},
                       '总股本': {'2024': 2.5}, '存货周转天数': {'2024': 30}},
            'quote': {'price': 12, 'total_mv_yi': 30, 'total_share_yi': 2.5},
            'balance_sheet': {'fields': [{'key': 'money_cap', 'label': '货币资金'}, {'key': 'total_share', 'label': '股本'}],
                              'data': {'money_cap': {'2024': 1.2}, 'total_share': {'2024': 2.5}}},
            'ev_items': {'报告期': '20250930', '货币资金': None},
            'quarterly_revenue': {'years': [2025], 'data': {'2025': {'Q1': 1, 'Q2': None}}},
            'period_trend': {'20250930': {'营业收入': 3}},
            'main_business': {'P': {'20241231': {'total': 1, 'items': [['产品', 1, None]]}}}}


class AgentDataTests(unittest.TestCase):
    def test_unit_conversion_and_missing_values(self):
        stock = sample()
        result = company_payload(stock, 'a_share', '20261121_201610')
        self.assertEqual(result['unit'], 'CNY_yuan')
        self.assertEqual(result['annual']['营业收入'], {'2024': 123450000, '2025': None})
        self.assertEqual(result['annual']['ROE'], {'2024': 15, '2025': None})
        self.assertEqual(result['annual_fields']['ROE']['unit'], 'percent')
        self.assertEqual(result['annual']['总股本']['2024'], 250000000)
        self.assertEqual(result['annual_fields']['总股本']['unit'], 'shares')
        self.assertEqual(result['quote']['price'], 12)
        self.assertEqual(result['quote']['total_mv'], 3000000000)
        self.assertNotIn('total_mv_yi', result['quote'])
        self.assertEqual(result['balance_sheet']['data']['money_cap']['2024'], 120000000)
        self.assertEqual(result['balance_sheet']['fields'][1]['unit'], 'shares')
        self.assertEqual(result['quarterly_revenue']['data']['2025'], {'Q1': 100000000, 'Q2': None})
        self.assertEqual(result['ev_items'], {'报告期': '20250930', '货币资金': None})
        self.assertEqual(result['main_business']['P']['20241231']['items'], [['产品', 100000000, None]])
        self.assertEqual(stock, sample())
        self.assertEqual(scale(0.0001), 10000)

    def test_hk_is_not_exported(self):
        with self.assertRaisesRegex(ValueError, '仅支持 A 股'):
            company_payload(sample() | {'market': 'HK'}, 'hk', '')
        with self.assertRaisesRegex(ValueError, '仅支持 A 股'):
            ranking_payloads({'rows': []}, 'hk', {})

    def test_unknown_metric_fails_instead_of_silently_scaling(self):
        stock = sample()
        stock['annual']['新比率'] = {'2024': 0.5}
        with self.assertRaisesRegex(ValueError, 'unknown annual field'):
            company_payload(stock, 'a_share', '')

    def test_rankings_match_browser_boundaries_flags_and_stable_ties(self):
        screen = {'version': '20261121_201610', 'trade_date': '20261120', 'rows': [
            {'code': 'B', 'pe_ttm': 5, 'ev_ebit': 3, 'financial': True, 'mv': 1},
            {'code': 'A', 'pe_ttm': 5, 'ev_ebit': 4, 'fx_mismatch': True, 'pe_calc': True, 'mv': None},
            {'code': 'C', 'pe_ttm': 10, 'ev_ebit': 0},
            {'code': 'D', 'pe_ttm': -1, 'ev_ebit': 10},
            {'code': 'E', 'pe_ttm': None, 'ev_ebit': None},
        ]}
        pe, ev = ranking_payloads(screen, 'a_share', {'A': 'https://example.test/A.json'})
        self.assertEqual([r['code'] for r in pe['rows']], ['B', 'A'])
        self.assertEqual([r['code'] for r in ev['rows']], ['A'])
        self.assertEqual(pe['rows'][0]['mv'], 100000000)
        self.assertEqual(pe['rows'][1]['rank'], 2)
        self.assertIsNone(pe['rows'][1]['mv'])
        self.assertTrue(ev['rows'][0]['fx_mismatch'])
        self.assertTrue(ev['rows'][0]['pe_calc'])
        self.assertFalse(pe['rows'][0]['both_low'])

    def test_build_discovers_only_published_versions_and_existing_stocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version = '20261121_201610'
            source = root / 'data/versions/a_share' / version
            (source / 'stocks').mkdir(parents=True)
            (root / 'data/versions.js').write_text('window.VL_VERSIONS = ' + json.dumps({'a_share': [{'version': version}], 'hk': []}) + ';')
            (source / 'companies.js').write_text('window.VL_registerCompanies(' + json.dumps({'companies': [{'code': '600519.SH'}, {'code': '000001.SZ'}]}) + ');')
            (source / 'stocks/600519.SH.js').write_text('window.VL_registerStock(' + json.dumps(sample()) + ');')
            (source / 'screen.js').write_text('window.VL_registerScreen(' + json.dumps({'market': 'a_share', 'version': version, 'rows': []}) + ');')
            (root / 'api/v1/hk/companies').mkdir(parents=True)
            (root / 'api/v1/hk/companies/old.json').write_text('{}')
            build(root)
            index = json.loads((root / 'api/v1/a_share/companies.json').read_text())
            self.assertTrue(index['companies'][0]['available'])
            self.assertFalse(index['companies'][1]['available'])
            self.assertIsNone(index['companies'][1]['history_url'])
            self.assertNotIn('hk', json.loads((root / 'api/v1/versions.json').read_text()))
            self.assertNotIn('hk', json.loads((root / 'api/v1/catalog.json').read_text())['markets'])
            self.assertFalse((root / 'api/v1/hk').exists())
            for name in ('llms.txt', 'data.html', 'api/v1/catalog.json', 'api/v1/a_share/companies/600519.SH.json'):
                self.assertTrue((root / name).exists())

    def test_wrapped_json_is_parsed_without_executing_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x.js'
            path.write_text('window.VL_registerStock({"name":"a);b"});')
            self.assertEqual(read_wrapped(path, 'window.VL_registerStock(')['name'], 'a);b')
            path.write_text('window.VL_registerStock({}); alert(1);')
            with self.assertRaises(ValueError):
                read_wrapped(path, 'window.VL_registerStock(')


if __name__ == '__main__':
    unittest.main()
