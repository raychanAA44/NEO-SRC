/* 05 用量查账 · status.traffic + dashboard + usage + errors + official */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;
  var state = { range: 'today', costAxis: 'model', channel: 'all', from: null, to: null };

  function data() { return HUB.data.state.data || {}; }
  function traffic() { return (data().status && data().status.traffic) || {}; }
  function windowOf(range) { return traffic()[range] || {}; }
  function rows() { return (data().usage && data().usage.rows) || []; }
  function channelOf(r) { return String(r.channel || r.upstream || ''); }
  function shanghaiDayStart(now) {
    var parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now || new Date());
    var p = {};
    parts.forEach(function (x) { p[x.type] = x.value; });
    return Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day)) / 1000 - 8 * 3600;
  }
  /* datetime-local 的字符串按墙钟时间理解：面板只报北京时间的账，
   * 所以减 8 小时再当 UTC 解析，不能交给浏览器按本机时区猜。 */
  function parseLocalEpoch(text) {
    var s = String(text || '').trim();
    if (!s) return null;
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    if (!m) return null;
    var ms = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]),
                      Number(m[4]), Number(m[5])) - 8 * 3600 * 1000;
    return isFinite(ms) ? Math.floor(ms / 1000) : null;
  }
  function customWindow() {
    if (state.from === null || state.to === null) return null;
    if (!(state.from < state.to)) return null;
    return { from: state.from, to: state.to };
  }
  function syncCustomWindow() {
    var from = HUB.$('#ledgerFrom'), to = HUB.$('#ledgerTo');
    state.from = from ? parseLocalEpoch(from.value) : null;
    state.to = to ? parseLocalEpoch(to.value) : null;
    if (state.from !== null && state.to !== null && state.from >= state.to) {
      state.from = null;                       // 起止反了就不算自定义段，回页签口径
      state.to = null;
    }
  }
  function selectedRows(scope) {
    var start = shanghaiDayStart();
    var end = null;
    var win = customWindow();
    if (win) {
      start = win.from;
      end = win.to;
    } else {
      if (state.range === 'week') {
        var weekday = new Date(start * 1000 + 8 * 3600).getUTCDay();
        start -= ((weekday + 6) % 7) * 86400;
      } else if (state.range === 'all') {
        start = 0;
      }
    }
    var list = rows().filter(function (r) {
      if (state.channel !== 'all' && channelOf(r) !== state.channel) return false;
      var at = Number(r.at) || 0;
      if (!at) return false;
      if (at < start) return false;
      if (end !== null) { if (at >= end) return false; }
      else if (state.range === 'today' && at >= start + 86400) return false;
      return true;
    });
    return scope === 'decision' ? list.filter(function (r) { return HUB.decisionInfo(r).decision; }) : list;
  }
  function decisionRows() { return selectedRows('decision'); }
  function channelLabel(key) {
    if (!key || key === 'all') return '全部渠道';
    return HUB.shortChannel(key) || key;
  }
  function renderChannelOptions() {
    var select = HUB.$('#ledgerChannel');
    if (!select) return;
    var channels = {};
    rows().forEach(function (r) { var key = channelOf(r); if (key) channels[key] = true; });
    var keys = Object.keys(channels).sort();
    var html = '<option value="all">全部渠道（当前样本）</option>' + keys.map(function (key) {
      return '<option value="' + esc(key) + '">' + esc(channelLabel(key)) + '</option>';
    }).join('');
    select.innerHTML = html;
    if (keys.indexOf(state.channel) < 0) state.channel = 'all';
    select.value = state.channel;
  }
  function exportFilterLabel() {
    return (customWindow() ? '自定义' : state.range === 'today' ? '今日' : state.range === 'week' ? '本周' : '累计')
      + ' · ' + channelLabel(state.channel);
  }
  function costOf(r) {
    if (r._cost && r._cost.cny !== undefined) return Number(r._cost.cny) || 0;
    if (r.cost_cny !== null && r.cost_cny !== undefined) return Number(r.cost_cny) || 0;
    return 0;
  }
  function officialOf(channel) {
    return ((data().official && data().official.channels) || []).find(function (x) { return x.channel === channel; }) || {};
  }
  function rangeLabel(range) {
    var win = customWindow();
    if (win) {
      return F.hm(win.from).slice(5) + ' → ' + F.hm(win.to).slice(5) + ' CST';
    }
    return range === 'week' ? '本周汇总' : range === 'all' ? '累计汇总' : '00:00 — 24:00 CST';
  }
  /* 本笔缓存占比：优先用 token 实算，缺字段时退回后端百分比。 */
  function cachePctOf(r) {
    var p = Number(r.prompt_tokens) || 0;
    var c = Number(r.cached_tokens) || 0;
    if (p > 0) return c / p * 100;
    return Number(r.cache_hit_pct) || 0;
  }
  /* 账目行的判定只讲计费性质，不再重复对账字段。 */
  function billTag(r) {
    var billable = (r._cost || {}).billable;
    if (r.error) {
      if (billable === false) return '失败·不计费';
      if (billable === true) return '失败·计费';
      return '失败';
    }
    if (billable === false) return '不计费';
    if (costOf(r) <= 0) return '零计费';
    if (cachePctOf(r) >= 30) return '缓存折价';
    return '全价';
  }
  /* 缓存节省：后端未下发就返回 null，不补 0、不估算。 */
  function savedText(v) {
    if (v === null || v === undefined) return null;
    var n = Number(v);
    return isFinite(n) ? F.cny(n, 4) : null;
  }

  function updateExportButton() {
    var n = decisionRows().length;
    var btn = HUB.$('#ledgerExportDecision');
    var count = HUB.$('#ledgerDecisionCount');
    if (count) count.textContent = String(n);
    if (!btn) return;
    btn.disabled = n === 0;
    btn.setAttribute('aria-label', '仅导出决策模型 CSV，当前 ' + n + ' 行' + (n ? '' : '，不可导出'));
    btn.title = n ? '只导出当前样本中的 ' + n + ' 行 DECISION 模型' : '当前样本无 DECISION 模型流水';
  }

  function renderTabs() {
    renderChannelOptions();
    /* 自定义时段生效时，账期页签全部退为非激活：两条口径不能同时亮着。 */
    var custom = !!customWindow();
    HUB.$$('#ledgerRange button').forEach(function (b) {
      b.classList.toggle('is-active', !custom && b.getAttribute('data-range') === state.range);
    });
    var w = HUB.$('#ledgerWindow');
    if (w) w.textContent = rangeLabel(state.range) + ' · 最多载入 50 条明细';
    var d = HUB.$('#ledgerDate');
    if (d) d.textContent = F.date((data().source && data().source.generated_at) || 0).slice(0, 7);
    var c = HUB.$('#ledgerCheck');
    if (c) c.textContent = (customWindow() || state.range === 'today') ? 'SAMPLE / READ MODEL' : 'AGGREGATE / DETAIL TODAY';
  }

  /* 自定义时段没有预聚合窗口：直接拿筛选到的明细算，绝不拿「今日」的数冒充。 */
  function sampleWindow() {
    var list = selectedRows('all');
    var cost = 0, prompt = 0, cached = 0, tokens = 0, errors = 0;
    list.forEach(function (r) {
      cost += costOf(r);
      prompt += Number(r.prompt_tokens) || 0;
      cached += Number(r.cached_tokens) || 0;
      tokens += Number(r.total_tokens) || 0;
      if (r.error) errors += 1;
    });
    return {
      /* 窗口内一笔都没有时金额给 null：那叫“没数据”，不叫“花了 0 元”。 */
      requests: list.length, errors: errors, cost_cny: list.length ? cost : null,
      tokens: list.length ? tokens : null,
      prompt_tokens: prompt, cached_tokens: cached, completion_tokens: tokens - prompt,
      cache_hit_pct: prompt ? cached / prompt * 100 : null,
      cost_usd: null, official_cny: null, credit: null, sample: true
    };
  }

  function renderVouchers() {
    var host = HUB.$('#ledgerVouchers');
    if (!host) return;
    var w = customWindow() ? sampleWindow() : windowOf(state.range);
    var intl = officialOf('workbuddy-intl');
    var cn = officialOf('workbuddy-cn');
    var delta = state.range === 'today'
      ? (Number(intl.delta && intl.delta.calls) || 0) + (Number(cn.delta && cn.delta.calls) || 0)
      : null;
    var deltaAlert = delta !== null && delta > 100;
    var avg = w.requests ? (Number(w.cost_cny) || 0) / Number(w.requests) : null;
    var cached = Number(w.cached_tokens) || 0;
    var prompt = Number(w.prompt_tokens) || 0;
    host.innerHTML = ''
      + '<article class="fx-voucher"><div class="fx-voucher-top"><span>WINDOW SPEND / 本期结算</span><span class="stamp hollow">' + esc(F.unitMode()) + '</span></div>'
      + '<div class="fx-voucher-value">' + esc(F.cny(w.cost_cny, 4)) + '</div>'
      + '<div class="fx-voucher-note">' + esc(rangeLabel(state.range)) + ' · ' + esc(F.int(w.requests)) + ' 笔 · 均单 ' + esc(F.cny(avg, 4)) + '</div>'
      + '<div class="fx-voucher-foot"><span>OFFICIAL ' + esc(F.cny(w.official_cny, 4)) + '</span><span>USD ' + esc(F.usd(w.cost_usd)) + ' · 积分 ' + esc(F.num(w.credit, 1)) + '</span></div></article>'
      + '<article class="fx-voucher"><div class="fx-voucher-top"><span>CACHE / 缓存命中</span><span class="stamp hollow">HIT</span></div>'
      + '<div class="fx-voucher-value">' + esc(F.pct(w.cache_hit_pct)) + '</div>'
      + '<div class="fx-voucher-note">缓存 ' + esc(F.tok(cached)) + ' / 输入 ' + esc(F.tok(prompt)) + ' · 缓存段按渠道折价结算</div>'
      + '<div class="fx-voucher-foot"><span>CACHED ' + esc(F.tok(cached)) + '</span>'
      + (savedText(w.cache_saved_cny) ? '<span>省 ' + esc(savedText(w.cache_saved_cny)) + '</span>' : '<span>INPUT ' + esc(F.tok(prompt)) + '</span>') + '</div></article>'
      + '<article class="fx-voucher is-alert' + (deltaAlert ? ' is-hot' : '') + '"><div class="fx-voucher-top"><span>OFFICIAL DELTA / 官方差额</span><span class="stamp">' + (deltaAlert ? 'ALERT' : (state.range === 'today' ? 'OPEN' : 'TODAY ONLY')) + '</span></div>'
      + '<div class="fx-voucher-value">' + (delta === null ? '—' : F.int(delta)) + '<small> CALLS</small></div>'
      + '<div class="fx-voucher-note">' + (state.range === 'today' ? '国际 ' + esc(intl.cost_label || '—') + ' · 国内 ' + esc(cn.cost_label || '—') + ' · 阈值 100' : '官方汇总仅 1 日窗口') + '</div>'
      + (deltaAlert ? '<button class="btn small fx-voucher-btn" type="button" data-fresh-official>拍摄官方快照</button>' : '')
      + '<div class="fx-voucher-foot"><span>INTL ' + F.int(intl.official && intl.official.calls) + '</span><span>CN ' + F.int(cn.official && cn.official.calls) + '</span></div></article>';
  }

  /* 非今日口径（本周/累计/自定义段）没有预聚合：用筛选到的明细现场算，
   * 并在标题上标明是样本，不把今日的全量数搬过去。 */
  function channelSampleStats() {
    var bucket = {};
    selectedRows('all').forEach(function (r) {
      var key = channelOf(r) || '—';
      var it = bucket[key] || (bucket[key] = { key: key, requests: 0, ok: 0, tokens: 0,
                                               prompt: 0, cached: 0, ms: 0, msN: 0, cost: 0, priced: 0 });
      it.requests += 1;
      it.ok += r.error ? 0 : 1;
      it.tokens += Number(r.total_tokens) || 0;
      it.prompt += Number(r.prompt_tokens) || 0;
      it.cached += Number(r.cached_tokens) || 0;
      if (r.elapsed_ms !== null && r.elapsed_ms !== undefined) { it.ms += Number(r.elapsed_ms) || 0; it.msN += 1; }
      if (r._cost && r._cost.cny !== undefined && r._cost.cny !== null) { it.cost += Number(r._cost.cny) || 0; it.priced += 1; }
      else if (r.cost_cny !== null && r.cost_cny !== undefined) { it.cost += Number(r.cost_cny) || 0; it.priced += 1; }
    });
    var total = 0;
    Object.keys(bucket).forEach(function (k) { total += bucket[k].cost; });
    return Object.keys(bucket).sort().map(function (k) {
      var it = bucket[k];
      return {
        key: it.key, requests: it.requests, ok: it.ok,
        success_rate: it.requests ? it.ok / it.requests * 100 : null,
        tokens: it.tokens, prompt_tokens: it.prompt, cached_tokens: it.cached,
        cache_hit_pct: it.prompt ? it.cached / it.prompt * 100 : null,
        avg_ms: it.msN ? it.ms / it.msN : null,
        avg_cny: it.priced ? it.cost / it.priced : null,
        cost_cny: it.cost, share: total ? it.cost / total : null
      };
    });
  }

  /* 按渠道 = 每个渠道自己的统计数（请求/成功率/令牌/缓存/均耗时/均单/费用/占比），
   * 不是把同一根费用条换个标签。今日读 dashboard.channels 的全量聚合。 */
  function channelTable(host, seg) {
    var fullDay = state.range === 'today' && !customWindow();
    var list = fullDay ? ((data().dashboard && data().dashboard.channels) || []).slice()
      : channelSampleStats();
    if (state.channel !== 'all') {
      list = list.filter(function (c) { return String(c.key) === state.channel; });
    }
    var body = list.length ? list.map(function (c) {
      return '<tr><td title="' + esc(String(c.key || '')) + '">' + esc(HUB.shortChannel(c.key) || '—') + '</td>'
        + '<td class="num">' + esc(F.int(c.requests)) + '</td>'
        + '<td class="num">' + esc(F.pct(c.success_rate)) + '</td>'
        + '<td class="num">' + esc(F.tok(c.tokens)) + '</td>'
        + '<td class="num">' + esc(c.cache_hit_pct === null || c.cache_hit_pct === undefined ? '—' : F.pct(c.cache_hit_pct)) + '</td>'
        + '<td class="num">' + esc(F.dur(c.avg_ms)) + '</td>'
        + '<td class="num">' + esc(F.cny(c.avg_cny, 4)) + '</td>'
        + '<td class="num">' + esc(F.cny(c.cost_cny, 4)) + '</td>'
        + '<td class="num">' + esc(c.share === null || c.share === undefined ? '—' : F.pct(Number(c.share) * 100, 1)) + '</td></tr>';
    }).join('') : '<tr class="is-empty"><td colspan="9">当前窗口无渠道流水。</td></tr>';
    host.innerHTML = '<span class="head">CHANNEL LEDGER / 渠道分账</span>' + seg
      + '<span class="legend">' + esc(fullDay ? '今日全量 · 账本聚合'
        : ('明细样本 ' + selectedRows('all').length + ' 条 · 非全量')) + '</span>'
      + '<table class="fx-ch-table"><thead><tr><th>渠道</th><th class="num">请求</th><th class="num">成功率</th><th class="num">令牌</th>'
      + '<th class="num">缓存命中</th><th class="num">均耗时</th><th class="num">均单</th><th class="num">费用</th><th class="num">占比</th></tr></thead>'
      + '<tbody>' + body + '</tbody></table>';
  }

  function renderCost() {
    var host = HUB.$('#ledgerCost');
    if (!host) return;
    var axis = state.costAxis;
    var seg = '<div class="seg fx-cost-axis" role="group" aria-label="费用归集口径">'
      + '<button type="button" data-cost-axis="model"' + (axis === 'model' ? ' class="is-active"' : '') + '>按模型</button>'
      + '<button type="button" data-cost-axis="channel"' + (axis === 'channel' ? ' class="is-active"' : '') + '>按渠道</button>'
      + '</div>';
    if (axis === 'channel') { channelTable(host, seg); return; }
    /* 模型构成只有「今日」预聚合；自定义时段不拿今日数据冒充，直接说明口径。 */
    if (state.range !== 'today' || customWindow()) {
      host.innerHTML = '<span class="head">COST COMPOSITION / 费用构成</span>' + seg
        + '<span class="legend">模型 / 渠道构成仅今日 · ' + esc(rangeLabel(state.range)) + '</span>';
      return;
    }
    var d = data();
    var src = axis === 'model'
      ? ((d.dashboard && d.dashboard.models) || [])
      : ((d.dashboard && d.dashboard.channels) || []);
    var list = src.map(function (c) {
      return { label: axis === 'model' ? String(c.key || '—') : HUB.shortChannel(c.key), cost: Number(c.cost_cny) || 0 };
    }).filter(function (c) { return c.cost > 0; }).sort(function (a, b) { return b.cost - a.cost; });
    var total = list.reduce(function (a, c) { return a + c.cost; }, 0);
    var top = list.slice(0, 4);
    host.innerHTML = '<span class="head">COST COMPOSITION / 费用构成</span>' + seg
      + (list.length
        ? '<div class="bar">' + top.map(function (c) {
          return '<i style="width:' + (c.cost / total * 100) + '%"></i>';
        }).join('') + '</div><div class="legend">' + top.map(function (c) {
          return '<span>' + esc(c.label) + ' ' + esc(F.cny(c.cost, 4)) + ' · ' + esc(F.pct(c.cost / total * 100, 1)) + '</span>';
        }).join('') + (list.length > top.length ? '<span>其余 ' + (list.length - top.length) + ' 项</span>' : '') + '</div>'
        : '<span class="legend">当前窗口无计费流水。</span>');
  }

  function tierInfo(r) {
    var t = r && r.tier;
    if (t && typeof t === 'object') return { k: String(t.k || ''), txt: String(t.txt || '') };
    return { k: '', txt: String(t || '') };
  }

  function renderPeak() {
    var host = HUB.$('#ledgerPeak');
    if (!host) return;
    var by = {};
    rows().forEach(function (r) {
      var key = r.upstream || r.channel || '—';
      if (!by[key]) by[key] = { n: 0, peak: 0, off: 0, latest: r, txt: '' };
      var ti = tierInfo(r);
      by[key].n += 1;
      if (ti.txt.indexOf('峰') >= 0 || ti.k === 'peak') by[key].peak += 1;
      else if (ti.txt.indexOf('谷') >= 0 || ti.k === 'off') by[key].off += 1;
      if (!by[key].txt && ti.txt) by[key].txt = ti.txt;
    });
    var meta = (data().meta && data().meta.custom) || [];
    var notes = {};
    meta.forEach(function (m) {
      var up = m.upstream || '';
      if (up && m.peak_rule_note) notes[up] = m.peak_rule_note;
    });
    var now = new Date();
    var utcH = now.getUTCHours();
    var ruler = '';
    for (var i = 0; i < 24; i++) {
      ruler += '<i class="' + (i === utcH ? 'now' : '') + (i < utcH ? ' past' : '') + '" title="UTC ' + (i < 10 ? '0' : '') + i + ':00"></i>';
    }
    var chips = Object.keys(by).sort().map(function (k) {
      var x = by[k];
      var peak = x.txt.indexOf('峰') >= 0;
      var note = notes[k] || '未声明峰谷 · 单一价';
      return '<span class="fx-peak-chip ' + (peak ? 'is-peak' : 'is-off') + '" title="' + esc(note) + '"><b>' + esc(k) + '</b><em>' + esc(x.txt || '—') + '</em><span>' + x.n + ' 笔</span></span>';
    }).join('');
    host.innerHTML = '<span class="head">PEAK / VALLEY CLOCK</span><span class="fx-peak-utc">UTC ' + (utcH < 10 ? '0' : '') + utcH + ':' + (now.getUTCMinutes() < 10 ? '0' : '') + now.getUTCMinutes() + '</span>'
      + '<div class="fx-peak-ruler" aria-hidden="true">' + ruler + '</div>'
      + '<div class="fx-peak-chips">' + (chips || '<span class="legend">暂无符合条件的流水。</span>')
      + (Object.keys(notes).length ? '<span class="fx-peak-rule" title="' + esc(Object.keys(notes).map(function (k) { return k + ': ' + notes[k]; }).join('\n')) + '">RULES ' + Object.keys(notes).length + '</span>' : '')
      + '</div>';
  }

  function renderRows() {
    var host = HUB.$('#ledgerRows');
    if (!host) return;
    var list = rows();
    host.innerHTML = list.length ? list.map(function (r) {
      var pct = cachePctOf(r);
      var info = HUB.decisionInfo(r);
      var modelTitle = r.model || '—';
      if (info.decision) modelTitle += ' · ' + (info.endpoint || '/v1/systemone');
      if (info.inputOnly) modelTitle += ' · input_only · output_tokens=0';
      var decisionSub = info.decision
        ? 'ENDPOINT ' + (info.endpoint || '/v1/systemone') + (info.inputOnly ? ' · IN-ONLY · OUT 0' : '')
        : '';
      return '<div class="fx-ledger-row">'
        + '<span>' + esc(F.clock(r.at)) + '</span>'
        + '<span title="' + esc(modelTitle) + '">' + esc(r.model || '—')
          + (info.decision ? ' <em class="fx-decision-flag">DECISION</em>' : '')
          + (info.inputOnly ? ' <em class="fx-decision-flag">IN-ONLY</em>' : '')
          + (decisionSub ? '<span class="fx-ledger-sub">' + esc(decisionSub) + '</span>' : '')
        + '</span>'
        + '<span title="' + esc(r.upstream || r.channel_label || '') + '">' + esc(HUB.shortChannel(r.channel)) + '</span>'
        + '<span>' + (Number(r.total_tokens) ? esc(F.tok(r.total_tokens)) : '—') + (pct > 0 ? ' · 缓存 ' + esc(F.pct(pct, 0)) : '') + '</span>'
        + '<span class="num">' + esc(F.cny(costOf(r), 4)) + '<em class="fx-row-tag">' + esc(billTag(r)) + '</em></span>'
        + '</div>';
    }).join('') : '<div class="fx-receipt-empty" style="padding:12px">暂无符合条件的流水。</div>';
    var sum = list.reduce(function (a, r) { return a + costOf(r); }, 0);
    var tokens = list.reduce(function (a, r) { return a + (Number(r.total_tokens) || 0); }, 0);
    var total = HUB.$('#ledgerTotal');
    if (total) total.innerHTML = '<span>Σ</span><span>已载入明细</span><span>最多 50 笔</span><span>' + esc(F.tok(tokens)) + '</span><span class="num">' + esc(F.cny(sum, 4)) + '</span>';
  }

  function renderRail() {
    var host = HUB.$('#ledgerRail');
    if (!host) return;
    var w = windowOf(state.range);
    var errs = data().errors || {};
    var cloud = data().cloud || {};
    var intl = officialOf('workbuddy-intl');
    var cn = officialOf('workbuddy-cn');
    var failCount = Number(w.errors) || 0;
    var billedErr = w.errors_billed;
    var failCheck = (billedErr === null || billedErr === undefined)
      ? ['计费口径', '待后端', '失败 ' + failCount + ' 行 · 计费状态未提供']
      : (Number(billedErr) === 0
        ? ['失败不计费', 'PASS', '失败 ' + failCount + ' 行 · 无一计费']
        : ['失败部分计费', 'REVIEW', '失败 ' + failCount + ' 行中 ' + F.int(billedErr) + ' 行仍计费']);
    var checks = [
      failCheck,
      ['缓存折价', w.cache_hit_pct !== null ? 'OBSERVED' : '—', '命中 ' + F.pct(w.cache_hit_pct) + ' · 缓存段按渠道价目'
        + (savedText(w.cache_saved_cny) ? ' · 省 ' + savedText(w.cache_saved_cny) : '')],
      ['云端水位', cloud.ok && Number(cloud.usage && cloud.usage.lag) === 0 ? 'PASS' : 'WATCH', 'usage lag ' + F.int(cloud.usage && cloud.usage.lag) + ' · event lag ' + F.int(cloud.events && cloud.events.lag)]
    ];
    host.innerHTML = '<div class="fx-rail-title"><b>账册校验 / AUDIT</b><span>READ MODEL</span></div>'
      + checks.map(function (c) {
        return '<div class="fx-check"><div><b>' + c[0] + '</b><p>' + c[2] + '</p></div><span class="stamp ' + (c[1] === 'PASS' ? '' : 'hollow') + '">' + c[1] + '</span></div>';
      }).join('')
      + '<div class="fx-rail-block"><div class="fx-rail-title"><b>OFFICIAL / 官方口径</b><span>DELTA</span></div>'
      + '<div class="fx-receipt-kv"><span>国际官方</span><b>' + F.int(intl.official && intl.official.calls) + '</b><span>国际 Hub</span><b>' + F.int(intl.hub && intl.hub.calls) + '</b><span>国内官方</span><b>' + F.int(cn.official && cn.official.calls) + '</b><span>额度标签</span><b>' + esc(intl.cost_label || '—') + '</b></div>'
      + '<div class="fx-receipt-note">官方含客户端入口 · 差额不并入结算。</div></div>'
      + '<div class="fx-rail-block"><div class="fx-rail-title"><b>ERROR LEDGER / 错误台账</b><span>' + F.int(errs.total) + '</span></div>'
      + '<div class="fx-receipt-kv">' + Object.keys(errs.kind_counts || {}).slice(0, 4).map(function (k) {
        return '<span>' + esc(k) + '</span><b>' + F.int(errs.kind_counts[k]) + '</b>';
      }).join('') + '</div>'
      + '<button class="btn small" type="button" id="ledgerErrors2">查看最近异常</button></div>'
      + '<div class="fx-rail-block"><div class="fx-rail-title"><b>NOTE / 口径</b><span>READ MODEL</span></div><div class="fx-receipt-note">明细取 _cost.cny · 缓存段仅报命中量，节省金额后端未下发 · 官方差额仅 1 日。</div></div>';
    var btn = HUB.$('#ledgerErrors2');
    if (btn) btn.addEventListener('click', openErrors);
  }

  function openErrors() {
    var errs = data().errors || {};
    HUB.openWO({
      kicker: 'ERROR LEDGER / 错误台账',
      title: '最近异常',
      meta: 'TOTAL / ' + F.int(errs.total) + ' · SHOW ' + F.int((errs.rows || []).length),
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>KIND COUNTS</span><b>' + Object.keys(errs.kind_counts || {}).length + ' KINDS</b></div><div class="fx-wo-kv">'
        + Object.keys(errs.kind_counts || {}).map(function (k) { return '<span>' + esc(k) + '</span><b>' + F.int(errs.kind_counts[k]) + '</b>'; }).join('')
        + '</div></div><table class="fx-wo-table"><thead><tr><th>时间</th><th>状态</th><th>渠道 / 模型</th><th>上游原文</th><th>耗时</th><th>RID</th><th>已确认</th></tr></thead><tbody>'
        + (errs.rows || []).map(function (e) {
          /* 台账行本身就是明细：/api/errors 给的是 channel/model/status/error/req_id，
             旧版读 iso/level/detail 三个不存在的字段，于是整列只剩「—」。 */
          var when = e.iso ? String(e.iso).slice(5, 19) : F.clock(e.at);
          var msg = String(e.error || '').trim() || '（上游没回错误文本）';
          return '<tr><td>' + esc(when) + '</td><td>' + esc(e.kind || (e.status ? 'HTTP ' + e.status : '—'))
            + '</td><td>' + esc([e.channel, e.model].filter(Boolean).join(' / ') || '—') + '</td>'
            + '<td title="' + esc(msg) + '"><span class="fx-err-msg">' + esc(msg.slice(0, 90))
            + (msg.length > 90 ? '…' : '') + '</span></td>'
            + '<td>' + esc(e.elapsed_ms === null || e.elapsed_ms === undefined ? '—' : F.dur(e.elapsed_ms)) + '</td>'
            + '<td title="' + esc(e.req_id || '') + '">' + esc(String(e.req_id || '—').slice(0, 8)) + '</td>'
            + '<td>' + (e.acked ? 'YES' : 'NO') + '</td></tr>';
        }).join('')
        + '</tbody></table><div class="fx-wo-warning">明细直接取账本行的上游错误原文与 RID；一键修复需确认，快照档只生成 ARMED 票。</div>',
      actions: [
        { label: '生成修复作业票', onClick: function () {
          HUB.arm('错误修复 / ARMED', 'POST /api/errors/fix；可带 { ids: [...] }。');
        }},
        { label: '收起', close: true }
      ]
    });
  }

  function exportCsv(scope) {
    var list = selectedRows(scope === 'decision' ? 'decision' : 'all');
    if (!list.length) {
      HUB.toast(scope === 'decision' ? '当前筛选无决策模型明细 / EMPTY' : '当前筛选无账目明细 / EMPTY');
      return;
    }
    var header = ['time', 'channel', 'model', 'upstream', 'rid', 'ttft_ms', 'elapsed_ms', 'prompt_tokens', 'cached_tokens', 'completion_tokens', 'cost_cny', 'error', 'decision', 'endpoint', 'billing_basis', 'filter_range', 'filter_channel', 'filter_scope', 'sample_limit'];
    var filterRange = customWindow() ? 'custom' : state.range;
    var filterChannel = state.channel;
    var filterScope = scope === 'decision' ? 'decision' : 'all';
    var lines = [header.join(',')].concat(list.map(function (r) {
      var info = HUB.decisionInfo(r);
      var vals = [F.clock(r.at), r.channel, r.model, r.upstream, r.rid, r.ttft_ms, r.elapsed_ms, r.prompt_tokens, r.cached_tokens, r.completion_tokens, costOf(r).toFixed(6), r.error || '', info.decision ? 'true' : 'false', info.endpoint || '', info.basis || '', filterRange, filterChannel, filterScope, '50'];
      return vals.map(function (v) { return '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"'; }).join(',');
    }));
    var metadata = [
      'HUB NEO USAGE EXPORT',
      '账期=' + (customWindow() ? ('自定义 ' + rangeLabel(state.range))
        : (state.range === 'today' ? '今日' : state.range === 'week' ? '本周' : '累计')),
      '渠道=' + channelLabel(filterChannel),
      '范围=' + filterScope,
      '数据范围=当前载入的最多50条明细；不代表完整历史'
    ].map(function (value) { return '"' + value.replace(/"/g, '""') + '"'; });
    var blob = new Blob(['\ufeff' + metadata.join('\r\n') + '\r\n' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    blob.exportMeta = { filter_range: filterRange, filter_channel: filterChannel, filter_scope: filterScope, sample_limit: 50 };
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'hubng-usage-' + (scope === 'decision' ? 'decision-' : 'sample-') + state.range + '-' + (state.channel === 'all' ? 'all-channels' : state.channel.replace(/[^a-zA-Z0-9_-]/g, '_')) + '-' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    HUB.ticket({
      title: scope === 'decision' ? '决策模型导出 / CSV' : '账册导出 / CSV',
      note: '已导出 ' + list.length + ' 行（' + exportFilterLabel() + ' · ' + (scope === 'decision' ? '决策模型' : '全部模型') + ' · 当前明细样本最多 50 条）。',
      status: 'PASSED', tone: 'pass', ttl: 3200
    });
  }

  function render() {
    renderTabs();
    updateExportButton();
    renderVouchers();
    renderCost();
    renderPeak();
    renderRows();
    renderRail();
  }
  HUB.views.ledger = render;

  document.addEventListener('click', function (e) {
    var axisBtn = e.target.closest('[data-cost-axis]');
    if (axisBtn) { state.costAxis = axisBtn.getAttribute('data-cost-axis'); renderCost(); return; }
    var b = e.target.closest('#ledgerRange button');
    if (b) { state.range = b.getAttribute('data-range'); render(); return; }
    if (e.target.closest('#ledgerExportDecision')) { exportCsv('decision'); return; }
    if (e.target.closest('#ledgerExport')) { exportCsv('all'); return; }
    if (e.target.closest('#ledgerErrors')) { openErrors(); return; }
    if (e.target.closest('[data-fresh-official]')) { HUB.freshOfficial(); return; }
  });
  document.addEventListener('change', function (e) {
    if (!e.target) return;
    if (e.target.id === 'ledgerChannel') {
      state.channel = e.target.value || 'all';
      updateExportButton();
      renderCost();
      return;
    }
    if (e.target.id === 'ledgerFrom' || e.target.id === 'ledgerTo') {
      syncCustomWindow();
      render();
    }
  });
  document.addEventListener('click', function (e) {
    var clear = e.target.closest('#ledgerRangeClear');
    if (!clear) return;
    var from = HUB.$('#ledgerFrom'), to = HUB.$('#ledgerTo');
    if (from) from.value = '';
    if (to) to.value = '';
    state.from = null;
    state.to = null;
    render();
  });
  document.addEventListener('DOMContentLoaded', render);
  if (document.readyState !== 'loading') render();
})();
