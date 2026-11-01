/* 05 用量查账 · 每日纪事信笺（EVENING DISPATCH）
 * 数据源：GET /api/reports/today（后端全量聚合，前端不做样本截断）
 * 交互：0→35→75→100 工业刻度进度条（约 600ms）→ 羊皮纸信笺 → 三模导出
 */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;
  var state = { report: null, progress: 0, timer: 0, revealed: false, typing: 0 };

  function $(sel) { return document.querySelector(sel); }
  function n(v) { return (v === null || v === undefined) ? '—' : F.int(v); }
  function cny(v) { return (v === null || v === undefined) ? '—' : F.cny(v, 4); }
  function pct(v) { return (v === null || v === undefined) ? '—' : F.pct(v, 1); }
  function dur(v) { return (v === null || v === undefined) ? '—' : F.dur(v); }

  /* ── 挂载触发按钮（不改写 05 视图既有渲染逻辑） ───────────── */
  function mount() {
    var side = document.querySelector('#v-ledger .head-side');
    if (!side || document.getElementById('dailyReportBtn')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'dailyReportBtn';
    btn.className = 'btn small filled';
    btn.textContent = '每日运营报告';
    btn.title = '查看今日运营与对账报告';
    btn.addEventListener('click', openLetter);
    side.appendChild(btn);
  }

  /* ── 模态开合 ────────────────────────────────────────────── */
  function setProgress(value, label) {
    var bar = $('#letterBar'), text = $('#letterStepText');
    state.progress = Math.max(0, Math.min(100, value));
    if (bar) bar.style.width = state.progress.toFixed(1) + '%';
    if (text && label) text.textContent = label;
  }

  function openLetter() {
    var modal = $('#letterModal'), scrim = $('#letterScrim');
    if (!modal) return;
    state.report = null;
    state.revealed = false;
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    scrim.classList.add('is-open');
    scrim.setAttribute('aria-hidden', 'false');
    document.documentElement.style.overflow = 'hidden';
    $('#letterBody').innerHTML = '';
    var stepper = $('#letterStepper');
    stepper.classList.remove('is-done');
    setProgress(0, '正在汇总入站流量…');

    fetch('/api/reports/today', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (doc) { state.report = doc; maybeReveal(); })
      .catch(function (err) {
        state.report = { ok: false, error: { message: String((err && err.message) || err) } };
        maybeReveal();
      });

    var started = Date.now();
    clearInterval(state.timer);
    state.timer = setInterval(function () {
      var pctDone = Math.min(100, (Date.now() - started) / 600 * 100);
      var label = pctDone < 35 ? '正在汇总入站流量…'
        : (pctDone < 75 ? '正在核对上游账目…' : '正在生成报告…');
      setProgress(pctDone, label);
      if (pctDone >= 100) {
        clearInterval(state.timer);
        state.timer = 0;
        maybeReveal();
      }
    }, 60);
  }

  function maybeReveal() {
    if (state.revealed || !state.report || state.progress < 100) return;
    state.revealed = true;
    setProgress(100, '正在生成报告…');
    var stepper = $('#letterStepper');
    if (stepper) stepper.classList.add('is-done');
    window.setTimeout(function () {
      renderLetter(state.report);
      var scroll = $('#letterScroll');
      if (scroll) scroll.scrollTop = 0;
    }, 220);
  }

  function closeLetter() {
    var modal = $('#letterModal'), scrim = $('#letterScrim');
    if (!modal) return;
    clearInterval(state.timer);
    state.timer = 0;
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
    scrim.classList.remove('is-open');
    scrim.setAttribute('aria-hidden', 'true');
    document.documentElement.style.overflow = '';
  }

  /* ── 信笺渲染 ────────────────────────────────────────────── */
  function row(cells, tag) {
    return '<tr>' + cells.map(function (cell) {
      return '<' + (tag || 'td') + '>' + cell + '</' + (tag || 'td') + '>';
    }).join('') + '</tr>';
  }

  /* 打印收口：屏幕仍全量，打印只留前 N 条并落一行余量摘要（数据不得静默消失）。 */
  var PRINT_LIMIT = { recon: 4, models: 5, tenants: 3, status: 2, errors: 1 };

  function capRow(html, index, limit) {
    return (limit && index >= limit) ? html.replace('<tr>', '<tr class="is-print-extra">') : html;
  }

  function moreNote(hidden, where) {
    return hidden > 0 ? '<p class="fx-epistle-more">… 另 ' + hidden + ' 条 · 明细见' + where + '</p>' : '';
  }

  function table(headers, rows, empty, limit, where) {
    if (!rows.length) return '<p class="fx-epistle-empty">' + esc(empty || 'No entries recorded.') + '</p>';
    var hidden = limit ? Math.max(0, rows.length - limit) : 0;
    return '<table class="fx-epistle-table"><thead>' + row(headers.map(function (h) { return esc(h); }), 'th')
      + '</thead><tbody>' + rows.map(function (r, i) { return capRow(r, i, limit); }).join('')
      + '</tbody></table>' + moreNote(hidden, where || '账本');
  }

  function renderLetter(doc) {
    var host = $('#letterBody');
    var meta = $('#letterMeta');
    if (!host) return;
    if (!doc || doc.ok === false) {
      var msg = (doc && doc.error && doc.error.message) || '报表不可用';
      host.innerHTML = '<div class="fx-epistle-error"><b>DISPATCH UNDELIVERED</b><p>' + esc(msg) + '</p></div>';
      if (meta) meta.textContent = 'INGRESS OBSERVATORY · UNAVAILABLE';
      return;
    }
    var ex = doc.executive || {};
    var recon = doc.reconciliation || { rows: [], source_mix: {} };
    var tenants = doc.tenants || { rows: [], kind: 'client_ip', note: '' };
    var inc = doc.incidents || {};
    var budget = doc.budgets || {};
    var date = doc.date || '';
    var title = $('#letterTitle');
    if (title) title.textContent = '每日运营报告 · ' + date;
    if (meta) meta.textContent = 'INGRESS OBSERVATORY · PORT ' + (doc.port || 8888)
      + ' · ' + String(doc.upstream_role || '—').toUpperCase();

    var costText = cny(ex.cost_cny);
    var calls = ex.calls || 0;
    var poem = '<p class="fx-epistle-poem">今日共处理 <b>' + n(calls)
      + '</b> 次入站请求；<br>' + n(ex.tokens_total) + ' 个令牌完成传输，错误响应 <b>'
      + n(ex.errors) + '</b> 次。账本费用为 <b>' + esc(costText)
      + '</b>，未定价记录 ' + n(ex.unpriced_rows) + ' 条。</p>';

    var execRows = [
      ['Calls / 请求数', n(ex.calls)],
      ['Success rate / 成功率', pct(ex.success_rate)],
      ['Errors / 错误数', n(ex.errors)],
      ['Prompt tokens', n(ex.prompt_tokens)],
      ['Cache tokens', n(ex.cached_tokens)],
      ['Output tokens', n(ex.completion_tokens)],
      ['Total tokens', n(ex.tokens_total)],
      ['Cost / 总费用', esc(costText)],
      ['Avg per call / 均笔单价', (ex.avg_cost_cny === null || ex.avg_cost_cny === undefined) ? '—' : '¥' + Number(ex.avg_cost_cny).toFixed(6)],
      ['TTFT avg', dur(ex.latency && ex.latency.ttft_avg_ms)],
      ['P95 latency', dur(ex.latency && ex.latency.p95_ms)],
      ['Unpriced rows / 待价目', n(ex.unpriced_rows)]
    ];

    var reconRows = (recon.rows || []).map(function (r) {
      return row([
        esc(r.channel || '—'),
        n(r.calls),
        n(r.tokens),
        cny(r.cost_cny),
        r.official_used === null || r.official_used === undefined
          ? '—' : esc(String(r.official_used)) + ' ' + esc(r.official_unit || ''),
        r.delta_requests === null || r.delta_requests === undefined ? '—' : (r.delta_requests > 0 ? '+' : '') + r.delta_requests,
        '<span class="fx-epistle-tag">' + esc(String(r.official_source || 'none').toUpperCase()) + '</span>'
      ]);
    });

    var modelRows = (doc.models || []).map(function (m) {
      return row([
        esc(m.model || '—'),
        esc(m.channel || '—'),
        n(m.calls),
        n(m.total),
        cny(m.cost_cny),
        dur(m.avg_latency_ms)
      ]);
    });

    var tenantRows = (tenants.rows || []).map(function (t) {
      return row([
        esc(t.tenant || '—'),
        n(t.calls),
        n(t.total),
        cny(t.cost_cny),
        esc(F.hm(t.last_at))
      ]);
    });

    var statusRows = (inc.status_counts || []).map(function (s) {
      return row([esc('HTTP ' + s.status), n(s.count)]);
    });
    var errorRows = (inc.recent_errors || []).map(function (e) {
      return '<li><time>' + esc(F.hm(e.at)) + '</time><span>' + esc(e.channel || '—')
        + ' · ' + esc(e.model || '—') + '</span><b>' + esc(e.status ? 'HTTP ' + e.status : 'ERR')
        + '</b><em>' + esc(e.error || '—') + '</em></li>';
    });
    var caps = (budget.caps || []).map(function (cap) {
      return row([esc(cap.key || '—'), esc(String(cap.amount)) + ' ' + esc(cap.period || 'CNY')]);
    });
    var budgetLine = budget.cap_count
      ? (budget.blocked ? 'Gate CLOSED · ' + esc(budget.blocked_by || '') : 'Gate open · '
        + pct(budget.used_pct) + ' of cap')
      : '未设置预算上限';

    host.innerHTML = '<div class="fx-epistle-inner">'
      + '<div class="fx-epistle-letterhead"><span>' + esc(doc.service || 'hub-neo-gateway') + '</span><span>'
      + esc(date) + ' · ' + esc(String(doc.ledger_channel || '').toUpperCase()) + '</span></div>'
      + '<p class="fx-epistle-salute">每日运行摘要</p>'
      + poem
      + '<section class="fx-epistle-section"><h3>一、运行指标</h3>'
      + '<table class="fx-epistle-table fx-epistle-kv"><tbody>'
      + execRows.map(function (pair) { return row([esc(pair[0]), pair[1]]); }).join('')
      + '</tbody></table></section>'
      + '<section class="fx-epistle-section"><h3>二、上游对账</h3>'
      + table(['渠道', '请求数', '令牌数', '本地费用（CNY）', '官方用量', '差额', '来源'], reconRows,
              '今日无上游请求记录。', PRINT_LIMIT.recon, ' 05 用量账本')
      + '<p class="fx-epistle-note">' + esc(recon.note || '') + '</p></section>'
      + '<section class="fx-epistle-section"><h3>三、模型用量</h3>'
      + table(['模型', '渠道', '请求数', '令牌数', '费用', '平均延迟'], modelRows,
              '今日无模型请求记录。', PRINT_LIMIT.models, ' 05 用量账本')
      + '</section>'
      + '<section class="fx-epistle-section"><h3>四、租户用量</h3>'
      + table(['租户', '请求数', '令牌数', '费用', '最近活动'], tenantRows,
              '今日无租户归因记录。', PRINT_LIMIT.tenants, ' 05 用量账本')
      + '<p class="fx-epistle-note">' + esc(tenants.note || '') + '</p></section>'
      + '<section class="fx-epistle-section"><h3>五、异常与预算</h3>'
      + '<div class="fx-epistle-two">'
      + '<div><h4>HTTP 错误分布</h4>'
      + table(['状态', '数量'], statusRows, '今日无失败请求。', PRINT_LIMIT.status, ' 02 实时流水')
      + '<p class="fx-epistle-note">冷却 / 熔断信号：' + n(inc.cooldown_signals) + '</p></div>'
      + '<div><h4>预算使用</h4><p class="fx-epistle-budget">' + budgetLine + '</p>'
      + table(['预算项', '上限'], caps, '无预算项')
      + '</div></div>'
      + (errorRows.length ? '<h4>近期异常</h4><ol class="fx-epistle-incidents">'
        + errorRows.map(function (r, i) {
          return i >= PRINT_LIMIT.errors ? r.replace('<li>', '<li class="is-print-extra">') : r;
        }).join('')
        + '</ol>' + moreNote(Math.max(0, errorRows.length - PRINT_LIMIT.errors), ' 02 实时流水') : '')
      + '</section>'
      + '<div class="fx-epistle-sign"><div class="fx-epistle-signature">HUB NEO Gateway '
      + esc(doc.port || 8888) + '</div><div class="fx-epistle-seal">[VERIFIED · '
      + esc(doc.port || 8888) + ' · SEALED]</div></div>'
      + '<div class="fx-epistle-colophon">Generated ' + esc(F.date(doc.generated_at))
      + ' ' + esc(F.clock(doc.generated_at)) + ' · Source: ' + esc(doc.source || 'local-ledger') + '</div>'
      + '</div>';
  }

  /* ── 三模导出 ────────────────────────────────────────────── */
  function markdownOf(doc) {
    var ex = doc.executive || {};
    var out = [];
    out.push('# 每日运营报告 · ' + (doc.date || '') + ' · 网关端口 ' + (doc.port || 8888));
    out.push('');
    out.push('每日运行摘要');
    out.push('');
    out.push('今日入站请求 ' + n(ex.calls) + ' 次；令牌用量 ' + n(ex.tokens_total) + '；错误响应 ' + n(ex.errors)
      + ' 次；账本费用 ' + cny(ex.cost_cny) + '。');
    out.push('');
    out.push('## 一、运行指标');
    out.push('| 指标 | 数值 |');
    out.push('| --- | --- |');
    [['Calls', n(ex.calls)], ['Success rate', pct(ex.success_rate)], ['Errors', n(ex.errors)],
     ['Prompt tokens', n(ex.prompt_tokens)], ['Cache tokens', n(ex.cached_tokens)],
     ['Output tokens', n(ex.completion_tokens)], ['Total tokens', n(ex.tokens_total)],
     ['Cost CNY', cny(ex.cost_cny)], ['Avg per call', ex.avg_cost_cny == null ? '—' : '¥' + Number(ex.avg_cost_cny).toFixed(6)],
     ['TTFT avg', dur(ex.latency && ex.latency.ttft_avg_ms)], ['P95 latency', dur(ex.latency && ex.latency.p95_ms)],
     ['Unpriced rows', n(ex.unpriced_rows)]].forEach(function (pair) {
      out.push('| ' + pair[0] + ' | ' + pair[1] + ' |');
    });
    out.push('');
    out.push('## 二、上游对账');
    out.push('| 渠道 | 请求数 | 令牌数 | 本地费用（CNY） | 官方用量 | 差额 | 数据来源 |');
    out.push('| --- | --- | --- | --- | --- | --- | --- |');
    ((doc.reconciliation || {}).rows || []).forEach(function (r) {
      out.push('| ' + [r.channel, r.calls, r.tokens, r.cost_cny,
        (r.official_used == null ? '—' : r.official_used + ' ' + (r.official_unit || '')),
        (r.delta_requests == null ? '—' : r.delta_requests), r.official_source].join(' | ') + ' |');
    });
    out.push('');
    out.push('## 三、模型用量');
    out.push('| 模型 | 渠道 | 请求数 | 令牌数 | 费用 | 平均延迟 |');
    out.push('| --- | --- | --- | --- | --- | --- |');
    (doc.models || []).forEach(function (m) {
      out.push('| ' + [m.model, m.channel, m.calls, m.total, m.cost_cny, dur(m.avg_latency_ms)].join(' | ') + ' |');
    });
    out.push('');
    out.push('## 四、租户用量（' + ((doc.tenants || {}).kind || 'client_ip') + '）');
    out.push('| 租户 | 请求数 | 令牌数 | 费用 | 最近活动 |');
    out.push('| --- | --- | --- | --- | --- |');
    (((doc.tenants || {}).rows) || []).forEach(function (t) {
      out.push('| ' + [t.tenant, t.calls, t.total, t.cost_cny, F.hm(t.last_at)].join(' | ') + ' |');
    });
    out.push('');
    out.push('## 五、异常与预算');
    out.push('| 状态 | 数量 |');
    out.push('| --- | --- |');
    (((doc.incidents || {}).status_counts) || []).forEach(function (s) {
      out.push('| HTTP ' + s.status + ' | ' + s.count + ' |');
    });
    out.push('');
    out.push('- 冷却 / 熔断信号：' + n((doc.incidents || {}).cooldown_signals));
    out.push('- 预算状态：' + (((doc.budgets || {}).cap_count)
      ? ((doc.budgets || {}).blocked ? 'CLOSED' : 'OPEN · ' + pct((doc.budgets || {}).used_pct))
      : '未设置上限'));
    out.push('');
    out.push('- HUB NEO Gateway · 端口 ' + (doc.port || 8888) + ' · 已核验');
    return out.join('\n');
  }

  function csvOf(doc) {
    var head = ['kind', 'name', 'channel', 'model', 'calls', 'prompt_tokens', 'cached_tokens',
      'completion_tokens', 'total_tokens', 'cost_cny', 'avg_latency_ms', 'p95_latency_ms',
      'ttft_avg_ms', 'last_at', 'status', 'note'];
    var rows = [head];
    var ex = doc.executive || {};
    rows.push(['summary', 'today', doc.ledger_channel || '', '',
      ex.calls, ex.prompt_tokens, ex.cached_tokens, ex.completion_tokens, ex.tokens_total,
      ex.cost_cny, ex.latency && ex.latency.avg_ms, ex.latency && ex.latency.p95_ms,
      ex.latency && ex.latency.ttft_avg_ms, doc.generated_at, '', 'success_rate=' + ex.success_rate]);
    (doc.channels || []).forEach(function (c) {
      rows.push(['channel', c.channel, c.channel, '', c.calls, c.prompt, c.cached, c.completion,
        c.total, c.cost_cny, c.avg_latency_ms, c.p95_latency_ms, c.ttft_avg_ms, c.last_at,
        '', 'errors=' + c.errors]);
    });
    (doc.models || []).forEach(function (m) {
      rows.push(['model', m.model, m.channel, m.model, m.calls, m.prompt, m.cached, m.completion,
        m.total, m.cost_cny, m.avg_latency_ms, m.p95_latency_ms, m.ttft_avg_ms, m.last_at, '', '']);
    });
    (((doc.tenants || {}).rows) || []).forEach(function (t) {
      rows.push(['tenant', t.tenant, '', '', t.calls, t.prompt, t.cached, t.completion,
        t.total, t.cost_cny, t.avg_latency_ms, t.p95_latency_ms, t.ttft_avg_ms, t.last_at,
        '', (doc.tenants || {}).kind || '']);
    });
    (((doc.incidents || {}).status_counts) || []).forEach(function (s) {
      rows.push(['incident', 'http_' + s.status, '', '', s.count, '', '', '', '', '', '', '', '', '', s.status, '']);
    });
    return rows.map(function (line) {
      return line.map(function (cell) {
        var text = cell === null || cell === undefined ? '' : String(cell);
        return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
      }).join(',');
    }).join('\n');
  }

  function downloadCsv() {
    if (!state.report || state.report.ok === false) { HUB.toast('报表尚未就绪 / NOT READY'); return; }
    var csv = csvOf(state.report);
    var blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = 'hub-neo-dispatch-' + (state.report.date || 'today') + '.csv';
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
    HUB.toast('CSV 已导出 / ' + link.download);
  }

  function printLetter() {
    if (!state.report) { HUB.toast('报表尚未就绪 / NOT READY'); return; }
    window.print();
  }

  function copyMarkdown() {
    if (!state.report || state.report.ok === false) { HUB.toast('报表尚未就绪 / NOT READY'); return; }
    HUB.copyText(markdownOf(state.report), '完整 Markdown 已复制');
  }

  /* ── 初始化 ─────────────────────────────────────────────── */
  function init() {
    mount();
    var closeBtn = $('#letterClose');
    if (closeBtn) closeBtn.addEventListener('click', closeLetter);
    var footClose = $('#letterCloseFoot');
    if (footClose) footClose.addEventListener('click', closeLetter);
    var scrim = $('#letterScrim');
    if (scrim) scrim.addEventListener('click', closeLetter);
    var copyBtn = $('#copyLetterBtn');
    if (copyBtn) copyBtn.addEventListener('click', copyMarkdown);
    var csvBtn = $('#exportCsvBtn');
    if (csvBtn) csvBtn.addEventListener('click', downloadCsv);
    var printBtn = $('#printLetterBtn');
    if (printBtn) printBtn.addEventListener('click', printLetter);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && $('#letterModal') && $('#letterModal').classList.contains('is-open')) {
        e.preventDefault();
        closeLetter();
      }
    });
    document.addEventListener('hub:view', function (e) {
      if (e.detail && e.detail.id === 'ledger') mount();
    });
  }

  HUB.openDailyDispatch = openLetter;
  document.addEventListener('DOMContentLoaded', init);
  if (document.readyState !== 'loading') init();
})();
