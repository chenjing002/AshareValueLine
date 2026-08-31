// Ashare Value Line — 纯静态前端：直接读取本地版本化数据文件（file:// 可用，无需服务器）

(function () {
    'use strict';

    var INDICATOR_ORDER = [
        '营业收入', '利润总额', '销售毛利率', '归母净利润', 'ROE',
        '总资产', '总负债', '负债率', '账面价值', '流动资产', '流动负债',
        '营运资本', '存货', '存货周转天数', '货币资金', '交易性金融资产', '长期借款', '经营现金净额', '分红', '总股本', '员工总数'
    ];
    var DAYS_INDICATORS = { '存货周转天数': true };
    var PERCENT_INDICATORS = { '销售毛利率': true, 'ROE': true, '负债率': true };
    var HISTORY_YEARS = 10;

    // 币种 → 金额单位后缀。港股行情以港元计价，但财务报表按各公司记账本位币披露
    // （约半数为人民币，其余港元 / 美元 / 新元等），因此金额表与行情需分别标注。
    var CURRENCY_UNIT = {
        '人民币元': '亿人民币', '人民币': '亿人民币',
        '港元': '亿港元', '港币': '亿港元',
        '美元': '亿美元', '新加坡元': '亿新元', '马来西亚林吉特': '亿林吉特',
        '澳门元': '亿澳门元', '日本元': '亿日元', '欧元': '亿欧元',
        '澳大利亚元': '亿澳元', '加拿大元': '亿加元', '泰国铢': '亿泰铢'
    };
    // 财务报表金额单位（跟随记账本位币；缺省/未知回退到通用「亿元」，兼容 A 股）
    function finUnit(stock) {
        var u = stock && stock.units;
        return (u && CURRENCY_UNIT[u['财务币种']]) || '亿元';
    }
    // 行情金额单位（总市值 / 流通市值；港股恒为港元，A 股回退「亿元」）
    function quoteUnit(stock) {
        var u = stock && stock.units;
        return (u && CURRENCY_UNIT[u['行情币种']]) || '亿元';
    }

    var MARKETS = [
        { key: 'a_share', label: 'A股' },
        { key: 'hk', label: '港股' }
    ];
    var DEFAULT_MARKET = 'a_share';

    // 兼容旧版 versions.js（未分市场的扁平数组，视为全部 A 股）
    var rawVersions = window.VL_VERSIONS;
    var versionsByMarket = Array.isArray(rawVersions)
        ? { a_share: rawVersions, hk: [] }
        : (rawVersions || { a_share: [], hk: [] });

    var state = {
        versionsByMarket: versionsByMarket,
        market: DEFAULT_MARKET,
        versions: [],            // 当前市场的版本列表
        currentVersion: null,
        companies: [],          // 当前版本公司列表
        companyByCode: {},
        currentCode: null,
        pendingCode: null,      // 跨市场跳转时暂存的目标代码，切换市场后由 switchVersion 消费
        // 全局搜索索引：跨 A 股 / 港股统一检索。按市场存各自最新版本的公司列表。
        searchByMarket: {},     // { market: { version, companies } }
        searchList: []          // 扁平化后的全部公司（供 matchCompanies 遍历）
    };

    function normalizeMarketKey(c) { return c && c.market === 'HK' ? 'hk' : 'a_share'; }

    // 把某市场某版本的公司列表并入全局搜索索引（同市场后到的版本覆盖旧的）
    function indexCompanies(market, version, companies) {
        if (!market || !companies) return;
        var existing = state.searchByMarket[market];
        if (existing && existing.version === version) return; // 已索引，避免重复
        state.searchByMarket[market] = { version: version, companies: companies };
        var list = [];
        MARKETS.forEach(function (m) {
            var entry = state.searchByMarket[m.key];
            if (entry) list = list.concat(entry.companies);
        });
        state.searchList = list;
    }

    var el = {
        marketSwitch: document.getElementById('marketSwitch'),
        versionSelect: document.getElementById('versionSelect'),
        input: document.getElementById('stockCodeInput'),
        searchIcon: document.getElementById('searchIcon'),
        kbdHint: document.getElementById('kbdHint'),
        suggestions: document.getElementById('suggestions'),
        error: document.getElementById('errorMessage'),
        container: document.getElementById('stockContainer'),
        modal: document.getElementById('searchModal'),
        modalBackdrop: document.getElementById('modalBackdrop'),
        modalInput: document.getElementById('modalInput'),
        modalSuggestions: document.getElementById('modalSuggestions')
    };

    // ------------------------------------------------------------------
    // 数据加载：通过注入 <script> 读取本地 .js 数据文件（file:// 下 fetch 会被浏览器拦截）
    // ------------------------------------------------------------------

    // 快速连续切换（方向键/版本/市场切换）时脚本可能乱序到达：
    // 回调内校验 market/code/version 是否仍是当前目标，过期数据直接丢弃，不清空 pending
    window.VL_registerCompanies = function (payload) {
        if (!payload || !payload.companies) return;
        // 任何市场的公司列表都并入全局搜索索引（包括后台预加载的其它市场）
        indexCompanies(payload.market, payload.version, payload.companies);
        if (payload.market !== state.market || payload.version !== state.currentVersion) return;
        state.companies = payload.companies || [];
        state.companyByCode = {};
        state.companies.forEach(function (c) { state.companyByCode[c.code] = c; });
        if (state.onCompaniesLoaded) { state.onCompaniesLoaded(); state.onCompaniesLoaded = null; }
    };
    window.VL_registerStock = function (stock) {
        if (!stock || stock.code !== state.currentCode || stock.version !== state.currentVersion) return;
        if ((stock.market === 'HK' ? 'hk' : 'a_share') !== state.market) return;
        renderStock(stock);
    };

    function loadScript(src, onError) {
        var tag = document.createElement('script');
        tag.src = src;
        tag.onerror = function () { tag.remove(); if (onError) onError(); };
        tag.onload = function () { tag.remove(); };
        document.head.appendChild(tag);
    }

    function marketDataDir(market) { return 'data/versions/' + market + '/'; }

    // 后台预加载各市场最新版本的公司列表，填充全局搜索索引（不影响当前展示）。
    // companies.js 通过 VL_registerCompanies 自行并入索引，无需回调。
    function preloadSearchIndex() {
        MARKETS.forEach(function (m) {
            var versions = state.versionsByMarket[m.key];
            if (!versions || !versions.length) return;
            var latest = versions[0].version;
            if (state.searchByMarket[m.key]) return; // 已加载
            loadScript(marketDataDir(m.key) + latest + '/companies.js');
        });
    }

    function loadCompanies(market, versionId, done) {
        state.onCompaniesLoaded = done;
        loadScript(marketDataDir(market) + versionId + '/companies.js', function () {
            if (market === state.market && versionId === state.currentVersion) {
                showError('无法加载版本 ' + versionId + ' 的公司列表（companies.js 缺失）');
            }
        });
    }

    function loadStock(market, versionId, code) {
        clearError();
        loadScript(marketDataDir(market) + versionId + '/stocks/' + code + '.js', function () {
            if (market === state.market && versionId === state.currentVersion && code === state.currentCode) {
                el.container.innerHTML = '';
                showError('版本 ' + versionId + ' 中没有 ' + code + ' 的数据文件');
            }
        });
    }

    // ------------------------------------------------------------------
    // 市场 / 版本切换
    // ------------------------------------------------------------------

    var MARKET_CODE_PATTERN = { hk: /\.HK$/i, a_share: /\.(SH|SZ|BJ)$/i };

    function marketOfCode(code) {
        if (MARKET_CODE_PATTERN.hk.test(code)) return 'hk';
        if (MARKET_CODE_PATTERN.a_share.test(code)) return 'a_share';
        return null;
    }

    function rememberLastMarket(market) {
        try { localStorage.setItem('vl_market', market); } catch (e) { /* file:// 下个别浏览器禁用存储 */ }
    }
    function recallLastMarket() {
        try { return localStorage.getItem('vl_market'); } catch (e) { return null; }
    }

    function initMarketSwitcher() {
        el.marketSwitch.addEventListener('click', function (e) {
            var btn = e.target.closest('.seg-btn');
            if (!btn) return;
            switchMarket(btn.dataset.market);
        });

        // 初始市场：URL # 指定股票的市场 > 上次记忆的市场 > 默认 A 股
        var hashCode = location.hash.length > 1 ? decodeURIComponent(location.hash.slice(1)) : null;
        var initialMarket = (hashCode && marketOfCode(hashCode)) || recallLastMarket() || DEFAULT_MARKET;
        if (!state.versionsByMarket[initialMarket] || !state.versionsByMarket[initialMarket].length) {
            initialMarket = MARKETS.filter(function (m) {
                return state.versionsByMarket[m.key] && state.versionsByMarket[m.key].length;
            }).map(function (m) { return m.key; })[0] || DEFAULT_MARKET;
        }
        switchMarket(initialMarket);
        preloadSearchIndex();
    }

    function switchMarket(market) {
        if (!MARKETS.some(function (m) { return m.key === market; })) return;
        state.market = market;
        rememberLastMarket(market);
        Array.prototype.forEach.call(el.marketSwitch.querySelectorAll('.seg-btn'), function (btn) {
            btn.classList.toggle('active', btn.dataset.market === market);
        });
        state.versions = state.versionsByMarket[market] || [];
        state.currentVersion = null;
        state.currentCode = null;
        el.container.innerHTML = '';
        el.input.value = '';
        el.versionSelect.innerHTML = '';
        clearError();
        initVersions();
    }

    function initVersions() {
        if (!state.versions.length) {
            showError('当前市场未找到任何数据版本。请先运行对应的抓取脚本（fetch_data.py / fetch_hk_data.py）生成 --full 或 --test 版本。');
            return;
        }
        var dateOf = function (v) { return (v.created_at || v.version).slice(0, 10); };
        var dateCounts = {};
        state.versions.forEach(function (v) {
            dateCounts[dateOf(v)] = (dateCounts[dateOf(v)] || 0) + 1;
        });
        state.versions.forEach(function (v) {
            var option = document.createElement('option');
            option.value = v.version;
            // 只显示日期；同一天有多个版本时补充时间以便区分
            option.textContent = dateCounts[dateOf(v)] > 1
                ? (v.created_at || v.version).slice(0, 16) : dateOf(v);
            el.versionSelect.appendChild(option);
        });
        switchVersion(state.versions[0].version);
    }

    function switchVersion(versionId) {
        var market = state.market;
        state.currentVersion = versionId;
        el.versionSelect.value = versionId;
        loadCompanies(market, versionId, function () {
            if (market !== state.market || versionId !== state.currentVersion) return; // 已切走
            // 首次打开该市场时默认显示：URL # 指定的股票 > 该市场上次浏览的股票
            if (!state.currentCode) {
                // 跨市场跳转的目标代码优先；其次 URL # 指定股票；再次该市场上次浏览
                var pending = state.pendingCode; state.pendingCode = null;
                var hashCode = location.hash.length > 1 ? decodeURIComponent(location.hash.slice(1)) : null;
                var initial = pending
                    || ((hashCode && marketOfCode(hashCode) === market) ? hashCode : recallLastCode(market));
                var company = initial ? findCompany(initial) : null;
                if (company) {
                    state.currentCode = company.code;
                    el.input.value = company.code;
                    rememberLastCode(market, company.code);
                } else if (state.companies.length) {
                    // 无 URL # / 历史记录时，默认展示第一家公司，避免首页空白
                    state.currentCode = state.companies[0].code;
                    el.input.value = state.companies[0].code;
                }
            }
            if (state.currentCode) loadStock(market, versionId, state.currentCode);
        });
    }

    // ------------------------------------------------------------------
    // 搜索 / 联想
    // ------------------------------------------------------------------

    // 全局搜索池：优先用跨市场索引，索引未就绪时回退到当前市场公司列表
    function searchPool() {
        return state.searchList.length ? state.searchList : state.companies;
    }
    function findByCode(code) {
        var pool = searchPool();
        for (var i = 0; i < pool.length; i++) {
            if (pool[i].code === code) return pool[i];
        }
        return null;
    }

    function findCompany(query) {
        query = query.trim().toUpperCase();
        if (!query) return null;
        var byCode = findByCode(query);
        if (byCode) return byCode;
        if (/^\d+$/.test(query)) {
            var six = query.padStart(6, '0'), five = query.padStart(5, '0');
            var suffixes = [six + '.SH', six + '.SZ', six + '.BJ', five + '.HK'];
            for (var i = 0; i < suffixes.length; i++) {
                var hit = findByCode(suffixes[i]);
                if (hit) return hit;
            }
        }
        var lower = query.toLowerCase();
        var exact = searchPool().filter(function (c) { return c.name === query || c.py === lower; });
        if (exact.length) return exact[0];
        var ranked = matchCompanies(query);
        return ranked.length ? ranked[0] : null;
    }

    // 排序匹配：精确(代码/名称/拼音首字母) > 前缀 > 包含。跨市场检索。
    function matchCompanies(query) {
        query = query.trim();
        if (!query) return [];
        var upper = query.toUpperCase(), lower = query.toLowerCase();
        var pool = searchPool();
        var buckets = [[], [], []];
        for (var i = 0; i < pool.length; i++) {
            var c = pool[i];
            var name = (c.name || '').toUpperCase();
            var py = c.py || '';
            var score;
            if (c.code === upper || name === upper || py === lower) score = 0;
            else if (c.code.indexOf(upper) === 0 || name.indexOf(upper) === 0 || py.indexOf(lower) === 0) score = 1;
            else if (c.code.indexOf(upper) !== -1 || name.indexOf(upper) !== -1 || py.indexOf(lower) !== -1) score = 2;
            else continue;
            if (buckets[score].length < 12) buckets[score].push(c);
        }
        return buckets[0].concat(buckets[1], buckets[2]).slice(0, 12);
    }

    function rememberLastCode(market, code) {
        try { localStorage.setItem('vl_last_code_' + market, code); } catch (e) { /* file:// 下个别浏览器禁用存储 */ }
    }
    function recallLastCode(market) {
        try { return localStorage.getItem('vl_last_code_' + market); } catch (e) { return null; }
    }

    function selectCompany(company) {
        clearError();
        var targetMarket = normalizeMarketKey(company);
        // 跨市场选择：先切到目标市场，切换完成后由 switchVersion 消费 pendingCode
        if (targetMarket !== state.market) {
            state.pendingCode = company.code;
            switchMarket(targetMarket);
            return;
        }
        state.currentCode = company.code;
        el.input.value = company.code;
        rememberLastCode(state.market, company.code);
        loadStock(state.market, state.currentVersion, company.code);
    }

    // 共用搜索组件：内联搜索框和 ⌘K 弹窗都用同一份匹配 / 联想 / 键盘导航逻辑
    function createSearchBox(input, listEl, opts) {
        var matches = [], active = -1;

        function close() { matches = []; active = -1; listEl.innerHTML = ''; listEl.hidden = true; }

        function render() {
            if (!matches.length) { close(); return; }
            listEl.innerHTML = matches.map(function (c, i) {
                var mk = c.market === 'HK' ? '港' : 'A';
                return '<div class="suggestion-item' + (i === active ? ' active' : '') + '" data-index="' + i + '">' +
                    '<span class="s-market s-market-' + (c.market === 'HK' ? 'hk' : 'a') + '">' + mk + '</span>' +
                    '<span class="s-code">' + esc(c.code) + '</span>' +
                    '<span class="s-name">' + esc(c.name || '') + '</span>' +
                    (c.py ? '<span class="s-py">' + esc(c.py) + '</span>' : '') +
                    (c.industry ? '<span class="s-industry">' + esc(c.industry) + '</span>' : '') +
                    '</div>';
            }).join('');
            listEl.hidden = false;
        }

        function choose(company) {
            if (!company) return;
            close();
            if (opts.onSelect) opts.onSelect(company);
        }

        function submit() {
            if (active >= 0 && matches[active]) { choose(matches[active]); return; }
            var company = findCompany(input.value);
            if (company) choose(company);
            else showError('在当前版本公司列表中找不到 "' + input.value.trim() + '"');
        }

        input.addEventListener('input', function () {
            matches = matchCompanies(input.value);
            active = matches.length ? 0 : -1;
            render();
        });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                if (!matches.length) return;
                e.preventDefault();
                active = (active + (e.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
                render();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                submit();
            } else if (e.key === 'Escape') {
                if (!listEl.hidden) { e.stopPropagation(); close(); }
                else if (opts.onEscape) opts.onEscape();
            }
        });
        listEl.addEventListener('mousedown', function (e) {
            var item = e.target.closest('.suggestion-item');
            if (item) { e.preventDefault(); choose(matches[+item.dataset.index]); }
        });

        return { close: close, submit: submit };
    }

    // ------------------------------------------------------------------
    // 渲染
    // ------------------------------------------------------------------

    function fmtMoney(v) {
        if (v === null || v === undefined || isNaN(v)) return '--';
        return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    function fmt(v, digits) {
        if (v === null || v === undefined || isNaN(v)) return '--';
        return Number(v).toFixed(digits === undefined ? 2 : digits);
    }
    // 资产负债表百分比：value 占同年 base 的比例，无效/缺失返回破折号
    function fmtPct(value, base) {
        if (value == null || base == null || isNaN(value) || isNaN(base) || Number(base) === 0) return '—';
        return (Number(value) / Number(base) * 100).toFixed(1) + '%';
    }
    function fmtPE(v, isCalc) {
        if (v === null || v === undefined || isNaN(v)) return '--';
        return Number(v).toFixed(1) + (isCalc ? '*' : '');
    }
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, function (ch) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
        });
    }

    function deriveIndicators(annual) {
        var derived = {};
        Object.keys(annual).forEach(function (k) { derived[k] = annual[k]; });
        var assets = annual['总资产'] || {}, liabilities = annual['总负债'] || {};
        var currentAssets = annual['流动资产'] || {}, currentLiabilities = annual['流动负债'] || {};
        var bookValue = {}, debtRatio = {}, workingCapital = {};
        Object.keys(assets).forEach(function (year) {
            if (liabilities[year] !== undefined) {
                bookValue[year] = assets[year] - liabilities[year];
                if (assets[year] !== 0) debtRatio[year] = liabilities[year] / assets[year] * 100;
            }
        });
        Object.keys(currentAssets).forEach(function (year) {
            if (currentLiabilities[year] !== undefined) {
                workingCapital[year] = currentAssets[year] - currentLiabilities[year];
            }
        });
        if (Object.keys(bookValue).length) derived['账面价值'] = bookValue;
        if (Object.keys(debtRatio).length) derived['负债率'] = debtRatio;
        if (Object.keys(workingCapital).length) derived['营运资本'] = workingCapital;
        return derived;
    }

    function computeEV(stock) {
        var quote = stock.quote, items = stock.ev_items;
        if (!quote || !items || quote.total_mv_yi == null) return null;
        var totalDebt = (items['短期借款'] || 0) + (items['一年内到期的非流动负债'] || 0) +
            (items['长期借款'] || 0) + (items['应付债券'] || 0) + (items['租赁负债'] || 0);
        var totalCash = (items['货币资金'] || 0) + (items['交易性金融资产'] || 0);
        var minority = items['少数股东权益'] || 0;
        var ev = quote.total_mv_yi + totalDebt + minority - totalCash;
        var ebit = null;
        var profits = stock.annual && stock.annual['利润总额'];
        if (profits) {
            var years = Object.keys(profits).sort().reverse();
            if (years.length) ebit = profits[years[0]];
        }
        return {
            ev: ev, totalDebt: totalDebt, totalCash: totalCash, minority: minority,
            marketCap: quote.total_mv_yi, ebit: ebit,
            evEbit: (ebit && ebit !== 0) ? ev / ebit : null,
            items: items
        };
    }

    function renderQuote(stock) {
        var quote = stock.quote;
        var metrics = quote ? [
            ['股价', quote.price != null ? fmt(quote.price) : '--', true],
            ['PE TTM', fmtPE(quote.pe_ttm, quote.pe_ttm_calc), true],
            ['PE 静', fmtPE(quote.pe, quote.pe_calc), false],
            ['PB', fmt(quote.pb, 1), false],
            ['总市值', quote.total_mv_yi != null ? fmt(quote.total_mv_yi, 0) + quoteUnit(stock) : '--', true],
            ['流通市值', quote.circ_mv_yi != null ? fmt(quote.circ_mv_yi, 0) + quoteUnit(stock) : '--', false],
            ['总股本', quote.total_share_yi != null ? fmt(quote.total_share_yi) + '亿股' : '--', false]
        ] : [];
        return '<div class="card-header">' +
            '<div class="card-identity">' +
            '<span class="stock-name">' + esc(stock.name) + '</span>' +
            '<span class="stock-code">' + esc(stock.code) + '</span>' +
            (stock.industry ? '<span class="stock-tag">' + esc(stock.industry) + '</span>' : '') +
            '</div>' +
            (metrics.length ? '<div class="metric-strip">' + metrics.map(function (m) {
                return '<div class="metric-item' + (m[2] ? ' primary' : '') + '">' +
                    '<span class="metric-label">' + m[0] + '</span>' +
                    '<span class="metric-value">' + m[1] + '</span></div>';
            }).join('') + '</div>' : '') +
            '</div>';
    }

    function renderQuarterly(stock) {
        var qr = stock.quarterly_revenue;
        var html = '<div class="panel-title">季度营收 <span class="panel-sub">单季 · ' + finUnit(stock) + '</span></div>';
        if (!qr || !qr.years || !qr.years.length) {
            return html + '<div class="panel-empty">暂无季度营收数据</div>';
        }
        var quarters = ['Q1', 'Q2', 'Q3', 'Q4', 'FY'];
        html += '<table class="revenue-table"><thead><tr><th></th>' +
            quarters.map(function (q) { return '<th>' + q + '</th>'; }).join('') + '</tr></thead><tbody>';
        qr.years.forEach(function (year) {
            var row = qr.data[year] || {};
            html += '<tr><td>' + year + '</td>' + quarters.map(function (q) {
                var v = row[q];
                return '<td class="' + (q === 'FY' ? 'fy-cell' : '') + '">' + (v == null ? '-' : fmtMoney(v)) + '</td>';
            }).join('') + '</tr>';
        });
        return html + '</tbody></table>';
    }

    // 三年同期趋势：同一报告期跨年对比（营收为累计值，存货/应收为期末值）
    var TREND_PERIODS = [['0331', 'Q1'], ['0630', 'H1'], ['0930', 'Q3'], ['1231', '年度']];
    var trendSelection = null; // 当前选中的报告期后缀，切换股票时重置

    function trendReceivables(entry) {
        if (!entry) return null;
        var notes = entry['应收票据'], accounts = entry['应收账款'];
        if (notes != null || accounts != null) return (notes || 0) + (accounts || 0);
        return entry['应收票据及应收账款'] != null ? entry['应收票据及应收账款'] : null;
    }

    function defaultTrendPeriod(trend) {
        var dates = Object.keys(trend).sort().reverse();
        // 默认展示年度（1231）；若无年报数据再退回最新报告期
        if (dates.some(function (d) { return d.slice(4) === '1231'; })) return '1231';
        return dates.length ? dates[0].slice(4) : '1231';
    }

    function renderTrendBody(stock, periodMd) {
        var trend = stock.period_trend || {};
        var years = [];
        Object.keys(trend).forEach(function (endDate) {
            if (endDate.slice(4) === periodMd) years.push(+endDate.slice(0, 4));
        });
        years.sort(function (a, b) { return a - b; });
        var showYears = years.slice(-5).reverse(); // 最新年份在最左
        if (!showYears.length) return '<div class="panel-empty">该报告期暂无数据</div>';

        var metrics = [
            ['营业收入', function (e) { return e ? e['营业收入'] : null; }],
            ['存货', function (e) { return e ? e['存货'] : null; }],
            ['应收款项', trendReceivables]
        ];
        var html = '<table class="trend-table"><thead><tr><th></th>' +
            showYears.map(function (y) { return '<th>' + y + '</th>'; }).join('') + '</tr></thead><tbody>';
        metrics.forEach(function (m) {
            var values = showYears.map(function (y) { return m[1](trend[y + periodMd]); });
            if (!values.some(function (v) { return v != null; })) return; // 全空的指标不显示（如银行无存货）
            html += '<tr><td>' + m[0] + '</td>' + showYears.map(function (y, i) {
                var v = values[i];
                if (v == null) return '<td><span class="no-data">--</span></td>';
                var prev = m[1](trend[(y - 1) + periodMd]);
                var yoy = '';
                if (prev != null && prev !== 0) {
                    var pct = (v - prev) / Math.abs(prev) * 100;
                    yoy = '<span class="yoy ' + (pct >= 0 ? 'up' : 'down') + '">' +
                        (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%</span>';
                }
                return '<td>' + fmtMoney(v) + yoy + '</td>';
            }).join('') + '</tr>';
        });
        return html + '</tbody></table>';
    }

    function renderTrend(stock) {
        if (!stock.period_trend || !Object.keys(stock.period_trend).length) return '';
        if (!trendSelection) trendSelection = defaultTrendPeriod(stock.period_trend);
        var seg = TREND_PERIODS.map(function (p) {
            return '<button type="button" class="seg-btn' + (p[0] === trendSelection ? ' active' : '') +
                '" data-period="' + p[0] + '">' + p[1] + '</button>';
        }).join('');
        return '<div class="panel-title">同期趋势 <span class="panel-sub">累计 · ' + finUnit(stock) + '</span>' +
            '<span class="seg-control">' + seg + '</span></div>' +
            '<div id="trendBody">' + renderTrendBody(stock, trendSelection) + '</div>';
    }

    function renderEV(stock) {
        var m = computeEV(stock);
        var html = '<div class="panel-title">EV指标 <span class="panel-sub">' +
            (m ? '报告期 ' + esc(m.items['报告期'] || '-') + ' · ' + finUnit(stock) : '') + '</span></div>';
        if (!m) return html + '<div class="panel-empty">缺少行情或资产负债表数据</div>';
        // 港股行情币种（港元）与财务币种可能不同：EV = 港元市值 + 本位币负债 − 本位币现金，
        // 币种不一致时该数值仅供参考，避免误读。
        if (quoteUnit(stock) !== finUnit(stock)) {
            html += '<div class="ev-currency-warn">⚠ 行情为' + quoteUnit(stock).slice(1) +
                '、财务为' + finUnit(stock).slice(1) + '，EV 跨币种仅供参考</div>';
        }
        var highlight = m.evEbit != null && m.evEbit > 0 && m.evEbit < 10;
        var i = m.items;
        html += '<div class="ev-rows">' +
            '<div class="ev-row main"><span>EV</span><b>' + fmtMoney(m.ev) + '</b></div>' +
            '<div class="ev-row main"><span>EV / EBIT</span><b' + (highlight ? ' class="ev-ebit-highlight"' : '') + '>' +
                (m.evEbit == null ? '-' : fmt(m.evEbit)) + '</b></div>' +
            '<div class="ev-row"><span>总债务</span><b>' + fmtMoney(m.totalDebt) + '</b></div>' +
            '<div class="ev-row"><span>现金</span><b>' + fmtMoney(m.totalCash) + '</b></div>' +
            '<div class="ev-row"><span>少数股东权益</span><b>' + fmtMoney(m.minority) + '</b></div>' +
            '</div>' +
            '<details class="calc-details"><summary>计算细项</summary>' +
            '<div class="detail-item"><strong>EV</strong> = 总市值(' + fmt(m.marketCap) + ') + 总债务(' + fmt(m.totalDebt) +
            ') + 少数股东权益(' + fmt(m.minority) + ') − 现金(' + fmt(m.totalCash) + ')</div>' +
            '<div class="detail-item"><strong>总债务</strong> = 短期借款(' + fmt(i['短期借款']) + ') + 一年内到期非流动负债(' + fmt(i['一年内到期的非流动负债']) +
            ') + 长期借款(' + fmt(i['长期借款']) + ') + 应付债券(' + fmt(i['应付债券']) + ') + 租赁负债(' + fmt(i['租赁负债']) + ')</div>' +
            '<div class="detail-item"><strong>现金</strong> = 货币资金(' + fmt(i['货币资金']) + ') + 交易性金融资产(' + fmt(i['交易性金融资产']) + ')</div>' +
            '<div class="detail-item"><strong>EBIT</strong> = 最新年度利润总额 = ' + (m.ebit == null ? '-' : fmt(m.ebit)) + '</div>' +
            '</details>';
        return html;
    }

    function renderAnnualTable(stock) {
        var annual = deriveIndicators(stock.annual || {});
        var yearSet = {};
        Object.keys(annual).forEach(function (indicator) {
            Object.keys(annual[indicator]).forEach(function (year) { yearSet[year] = true; });
        });
        var years = Object.keys(yearSet).sort().reverse().slice(0, HISTORY_YEARS);
        if (!years.length) return '<div class="revenue-error">暂无历史年度数据</div>';

        // 最新员工数（stock_company 当前值）补到最新年份列
        if (stock.employees != null) {
            annual['员工总数'] = annual['员工总数'] || {};
            if (annual['员工总数'][years[0]] == null) annual['员工总数'][years[0]] = stock.employees;
        }

        var html = '<div class="results-container"><table class="results-table"><thead><tr><th>指标（' + finUnit(stock) + ' / %）</th>' +
            years.map(function (y) { return '<th>' + y + '</th>'; }).join('') + '</tr></thead><tbody>';
        INDICATOR_ORDER.forEach(function (indicator) {
            var series = annual[indicator];
            if (!series) return;
            html += '<tr><td>' + indicator + '</td>';
            years.forEach(function (year) {
                var value = null;
                if (series[year] != null) {
                    if (indicator === '员工总数') {
                        value = Number(series[year]).toLocaleString('zh-CN');
                    } else if (DAYS_INDICATORS[indicator]) {
                        value = fmt(series[year], 1) + '天';
                    } else {
                        value = PERCENT_INDICATORS[indicator] ? fmt(series[year]) + '%' : fmtMoney(series[year]);
                    }
                }
                html += '<td class="number">' + (value == null ? '<span class="no-data">--</span>' : value) + '</td>';
            });
            html += '</tr>';
        });
        return html + '</tbody></table></div>';
    }

    // 金额 / 占比 两种视图，模块级状态，切换时仅重渲染资产负债表（不动底层数据）
    var bsMode = 'amount';
    // 各分区的百分比基数：资产项→资产总计，负债项→负债合计，权益项→股东权益合计（含少数股东）
    var BS_ASSET_BASE = 'total_assets';
    var BS_LIAB_BASE = 'total_liab';
    var BS_EQUITY_BASE = 'total_hldr_eqy_inc_min_int';

    var BS_SECTION_LABELS = { asset: '资产', liab: '负债', equity: '股东权益' };

    // 按科目顺序推断每个科目所属分区（asset/liab/equity），以“资产总计 / 负债合计”两行为界
    function bsSectionByField(fields) {
        var map = {};
        var section = 'asset';
        fields.forEach(function (field) {
            map[field.key] = section;
            if (field.key === 'total_assets') section = 'liab';
            else if (field.key === 'total_liab') section = 'equity';
        });
        return map;
    }

    // 每个科目的百分比基数：资产项→资产总计，负债项→负债合计，权益项→股东权益合计（含少数股东）
    // 末行“负债及股东权益总计”等于资产总计，用资产基数（读作 100%）
    function bsBaseKeyByField(fields, sectionByField) {
        var map = {};
        fields.forEach(function (field) {
            if (field.key === 'total_liab_hldr_eqy') map[field.key] = BS_ASSET_BASE;
            else if (sectionByField[field.key] === 'asset') map[field.key] = BS_ASSET_BASE;
            else if (sectionByField[field.key] === 'liab') map[field.key] = BS_LIAB_BASE;
            else map[field.key] = BS_EQUITY_BASE;
        });
        return map;
    }

    // 完整资产负债表：按公司实际披露的科目动态生成（不同行业/市场科目不同）
    function renderBalanceSheet(stock) {
        var bs = stock.balance_sheet;
        if (!bs || !bs.fields || !bs.fields.length) return '';
        var years = (bs.years || []).map(String);
        if (!years.length) return '';
        var isPct = bsMode === 'pct';
        var sectionByField = bsSectionByField(bs.fields);
        var baseKey = bsBaseKeyByField(bs.fields, sectionByField);
        var toggle = '<div class="seg-control bs-mode-control">' +
            '<button class="seg-btn' + (isPct ? '' : ' active') + '" data-bs-mode="amount">金额</button>' +
            '<button class="seg-btn' + (isPct ? ' active' : '') + '" data-bs-mode="pct">%</button>' +
            '</div>';
        var html = '<div class="results-container balance-sheet-container">' +
            '<div class="table-section-title">资产负债表 <span class="panel-sub">' +
            (bs.comp_type_name ? esc(bs.comp_type_name) + ' · ' : '') +
            '年报 · ' + (isPct ? '结构占比' : finUnit(stock)) + ' · 共' + bs.fields.length + '项</span>' +
            toggle + '</div>' +
            '<table class="results-table"><thead><tr><th>科目</th>' +
            years.map(function (y) { return '<th>' + y + '</th>'; }).join('') + '</tr></thead><tbody>';
        var lastSection = null;
        bs.fields.forEach(function (field) {
            // 分区变化时插入一行分区标题（资产 / 负债 / 股东权益），使结构更清晰
            var sec = sectionByField[field.key];
            if (sec !== lastSection) {
                lastSection = sec;
                html += '<tr class="bs-section-row"><td class="bs-section-label">' +
                    BS_SECTION_LABELS[sec] + '</td><td colspan="' + years.length + '"></td></tr>';
            }
            var series = bs.data[field.key] || {};
            var baseSeries = isPct ? (bs.data[baseKey[field.key]] || {}) : null;
            // 分区小计/合计行加底色；排除“(合计)”这类明细汇总科目（如 其他应收款(合计)、固定资产(合计)）
            var isTotal = /合计|总计/.test(field.label) && !/[（(]合计[）)]/.test(field.label);
            html += '<tr' + (isTotal ? ' class="bs-total-row"' : '') + '><td>' + esc(field.label) + '</td>';
            years.forEach(function (year) {
                var v = series[year];
                var cell;
                if (isPct) {
                    var pct = fmtPct(v, baseSeries[year]);
                    cell = pct === '—' ? '<span class="no-data">—</span>' : pct;
                } else {
                    cell = v == null ? '<span class="no-data">--</span>' : fmtMoney(v);
                }
                html += '<td class="number">' + cell + '</td>';
            });
            html += '</tr>';
        });
        return html + '</tbody></table></div>';
    }

    var currentStock = null;

    function renderStock(stock) {
        currentStock = stock;
        trendSelection = null;
        var trendHtml = renderTrend(stock);
        el.container.innerHTML =
            '<div class="stock-card-compact">' +
            renderQuote(stock) +
            '<div class="card-body' + (trendHtml ? ' has-trend' : '') + '">' +
            '<div class="card-panel">' + renderQuarterly(stock) + '</div>' +
            (trendHtml ? '<div class="card-panel trend-panel">' + trendHtml + '</div>' : '') +
            '<div class="card-panel ev-panel">' + renderEV(stock) + '</div>' +
            '</div></div>' +
            renderAnnualTable(stock) +
            renderBalanceSheet(stock);
    }

    // 同期趋势报告期切换（事件委托，重渲染面板内容即可）
    el.container.addEventListener('click', function (e) {
        var btn = e.target.closest('.seg-btn');
        if (!btn || !currentStock) return;
        // 资产负债表 金额/占比 切换：仅重渲染该表，其余内容与状态不变
        if (btn.dataset.bsMode) {
            if (btn.dataset.bsMode === bsMode) return;
            bsMode = btn.dataset.bsMode;
            var container = el.container.querySelector('.balance-sheet-container');
            if (container) {
                var wrap = document.createElement('div');
                wrap.innerHTML = renderBalanceSheet(currentStock);
                if (wrap.firstChild) container.parentNode.replaceChild(wrap.firstChild, container);
            }
            return;
        }
        if (btn.dataset.period) {
            trendSelection = btn.dataset.period;
            var panel = el.container.querySelector('.trend-panel');
            if (panel) panel.innerHTML = renderTrend(currentStock);
        }
    });

    // 双击表格行：在该行上方临时插入一份表头，长表格中无需回滚顶部即可对照科目/年份。
    // 点击临时表头以外的任意位置即消失；全局同时只保留一份。
    var tempHeaderRows = [];

    function removeTempHeader() {
        tempHeaderRows.forEach(function (row) { row.remove(); });
        tempHeaderRows = [];
    }

    el.container.addEventListener('dblclick', function (e) {
        var row = e.target.closest('tbody tr');
        if (!row || row.classList.contains('temp-header-row')) return;
        var table = row.closest('table');
        if (!table || !table.tHead) return;
        removeTempHeader();
        Array.prototype.forEach.call(table.tHead.rows, function (headRow) {
            var clone = headRow.cloneNode(true);
            clone.classList.add('temp-header-row');
            row.parentNode.insertBefore(clone, row);
            tempHeaderRows.push(clone);
        });
    });

    document.addEventListener('click', function (e) {
        // 双击本身触发的两次 click 发生在插入之前，不会误删刚显示的表头
        if (tempHeaderRows.length && !e.target.closest('.temp-header-row')) removeTempHeader();
    });

    // 左右方向键切换上一只 / 下一只股票（按当前版本公司列表顺序，循环）
    // 左右方向键在“同一行业分组”内切换上/下一家公司（保持列表顺序，循环）
    function navigate(delta) {
        if (!state.companies.length || !state.currentVersion) return;
        var current = state.companyByCode[state.currentCode];
        var industry = current ? (current.industry || '') : '';
        // 同行业子列表（行业为空的公司自成一组）
        var group = state.companies.filter(function (c) { return (c.industry || '') === industry; });
        if (!group.length) group = state.companies;
        var index = -1;
        for (var i = 0; i < group.length; i++) {
            if (group[i].code === state.currentCode) { index = i; break; }
        }
        var next = index === -1 ? 0 : (index + delta + group.length) % group.length;
        selectCompany(group[next]);
    }

    // ------------------------------------------------------------------
    // 错误提示 & 事件绑定
    // ------------------------------------------------------------------

    function showError(message) { el.error.textContent = message; el.error.hidden = false; }
    function clearError() { el.error.hidden = true; }

    // 内联搜索框
    var inlineSearch = createSearchBox(el.input, el.suggestions, { onSelect: selectCompany });
    el.searchIcon.addEventListener('click', function () { inlineSearch.submit(); });
    el.kbdHint.addEventListener('click', function () { openModal(); });
    el.input.addEventListener('focus', function () { this.select(); });
    document.addEventListener('click', function (e) {
        if (!el.suggestions.contains(e.target) && e.target !== el.input) inlineSearch.close();
    });

    // ⌘K 居中搜索弹窗（复用同一搜索组件）
    var modalSearch = createSearchBox(el.modalInput, el.modalSuggestions, {
        onSelect: function (company) { closeModal(); selectCompany(company); },
        onEscape: closeModal
    });

    function openModal() {
        el.modal.hidden = false;
        el.modalInput.value = '';
        modalSearch.close();
        el.modalInput.focus();
    }
    function closeModal() { el.modal.hidden = true; }

    el.modalBackdrop.addEventListener('click', closeModal);
    el.versionSelect.addEventListener('change', function () { switchVersion(this.value); });

    document.addEventListener('keydown', function (e) {
        if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
            e.preventDefault();
            if (el.modal.hidden) openModal(); else closeModal();
            return;
        }
        if (e.key === 'Escape' && !el.modal.hidden) { closeModal(); return; }
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            var active = document.activeElement;
            if (active && (active.tagName === 'INPUT' || active.tagName === 'SELECT' || active.tagName === 'TEXTAREA')) return;
            e.preventDefault();
            navigate(e.key === 'ArrowRight' ? 1 : -1);
        }
    });

    initMarketSwitcher();
})();
