/* 01 运行速览 · 只取 readmodel/dashboard + status + billing.live + cloud/sync */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;
  var currentMetric = 'requests';

  function data() { return HUB.data.state.data || {}; }

  function metrics() {
    var d = data();
    var t = (d.dashboard && d.dashboard.today) || {};
    var st = (d.status && d.status.traffic && d.status.traffic.today) || {};
    var avg = (d.billing && d.billing.avg_per_call) || {};
    var official = (d.official && d.official.channels) || [];
    var intl = official.find(function (x) { return x.channel === 'workbuddy-intl'; });
    var cn = official.find(function (x) { return x.channel === 'workbuddy-cn'; });
    var cap = capText();
    var h = (d.dashboard && d.dashboard.health) || {};
    var rs = (d.status && d.status.routing) || {};
    var acc = (d.status && d.status.accounts) || {};
    var ready = Number(h.channels_ready) || 0;
    var totalCh = Number(h.channels_total) || 0;
    return [
      {
        id: 'requests', label: 'TODAY REQUESTS / 今日请求',
        value: F.int(t.requests), unit: 'REQ',
        note: '近 1H ' + F.int(d.status && d.status.traffic && d.status.traffic.last_hour && d.status.traffic.last_hour.requests)
          + ' · 失败 ' + F.int(t.errors) + ' · 样本 ' + F.int(t.latency && t.latency.count),
        stamp: 'SUCCESS ' + F.pct(t.success_rate),
        sub: '今日请求'
      },
      {
        id: 'spend', label: 'TODAY SPEND / 今日费用',
        value: F.cny(t.cost_cny, 4), unit: 'CNY',
        note: '积分 ' + F.num(t.credit, 1) + ' · 均单 ' + F.cny(avg.cny, 4) + ' · 官方差 ' + F.int(intl && intl.delta && intl.delta.calls),
        stamp: cap ? 'CAP ' + cap : 'NO HARD CAP',
        sub: '今日费用'
      },
      {
        id: 'tokens', label: 'TODAY TOKENS / 今日令牌',
        value: F.tok(st.tokens), unit: 'TOK',
        note: '输入 ' + F.tok(Math.max(0, (Number(st.prompt_tokens) || 0) - (Number(st.cached_tokens) || 0)))
          + ' · 缓存 ' + F.tok(st.cached_tokens) + ' · 输出 ' + F.tok(st.completion_tokens),
        stamp: 'CACHE HIT ' + F.pct(st.cache_hit_pct),
        sub: '今日令牌'
      },
      {
        id: 'channels', label: 'AVAILABLE CHANNELS / 可用渠道',
        value: F.int(ready), unit: ' / ' + F.int(totalCh),
        note: '冷却槽 ' + F.int(rs.cooling) + ' · 账号 ' + F.int(acc.ok) + ' / ' + F.int(acc.total) + ' 正常 '
          + '· 熔断 ' + (rs.breaker && rs.breaker.active ? 'OPEN' : '无'),
        stamp: rs.breaker && rs.breaker.active ? 'BREAKER OPEN'
          : (ready >= totalCh ? 'ALL READY' : 'DEGRADED ' + F.int(totalCh - ready)),
        sub: '可用渠道'
      }
    ];
  }

  function heroHtml(m) {
    return '<div class="fx-hero-value"><span class="flip" id="ovHeroValue">' + esc(m.value) + '</span><small>' + esc(m.unit) + '</small></div>'
      + '<div class="fx-hero-side"><div class="hero-unit">' + esc(m.label) + '</div>'
      + '<p>' + esc(m.note) + '</p><span class="stamp hollow">' + esc(m.stamp) + '</span></div>';
  }

  function renderHero() {
    var list = metrics();
    var m = list.find(function (x) { return x.id === currentMetric; }) || list[0];
    var host = HUB.$('#ovHero');
    if (!host) return;
    if (host.getAttribute('data-first') === '1') host.innerHTML = heroHtml(m);
    else {
      host.innerHTML = heroHtml(m);
      host.setAttribute('data-first', '1');
    }
    HUB.$$('#ovMetricSwitch button').forEach(function (b) {
      var on = b.getAttribute('data-metric') === currentMetric;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  function renderMasthead() {
    var d = data(), st = d.status || {}, src = d.source || {};
    var health = st.health_score;
    var text = st.health_text || '';
    var set = function (id, value, cls) {
      var el = HUB.$('#' + id);
      if (!el) return;
      el.textContent = value;
      if (cls) el.className = cls;
    };
    /* 本地账本只给 generated_at，没有云端 iso；缺了就是「—」，绝不回落到 1970。 */
    var stamp = (st.iso && String(st.iso))
      || (st.generated_at ? (F.date(st.generated_at) + ' ' + F.clock(st.generated_at)) : '')
      || (src.iso && String(src.iso)) || '';
    var hasScore = health !== null && health !== undefined && isFinite(Number(health));
    set('ovVersion', src.version || '—');
    set('ovStamp', stamp ? String(stamp).slice(0, 16) : '—');
    set('ovHealth', hasScore ? (F.int(health) + ' / ' + text) : 'NO READING');
    set('ovHealthStamp', hasScore ? ('HEALTH ' + F.int(health) + ' / ' + text) : 'HEALTH —');
    set('ovDataTime', stamp ? String(stamp).slice(0, 16) : '—');
    var cloud = d.cloud || {};
    set('ovSync', cloud.ok === false ? 'LINK DOWN' : ('LAG ' + F.int(cloud.usage && cloud.usage.lag) + ' · ' + (cloud.ok ? 'IDEMPOTENT' : '—')));
    set('ovNode', src.url ? src.url.replace(/^https?:\/\//, '') : '127.0.0.1:8888');
    var foot = HUB.$('#ovFootSource');
    if (foot) {
      var feed = { live: 'LIVE', degraded: 'PARTIAL', locked: 'AUTH', snapshot: 'NO READINGS' }[HUB.data.state.mode] || 'NO READINGS';
      foot.textContent = feed + ' · ' + (stamp ? String(stamp).slice(0, 19) : '—');
    }
  }

  function quotaOf(channel) {
    var label = String((channel && channel.cost_label) || '');
    return label.split('/')[0] || '—';
  }

  function capText() {
    var g = data().budgets && data().budgets.global;
    if (!g || g.amount === null || g.amount === undefined) return null;
    /* over = 今日支出已到上限（/v1 会被 402 拒）；warn = 到 80% 预留缓冲。 */
    var tag = g.over ? ' · 已满' : (g.warn ? ' · ' + F.num(g.used_pct, 1) + '%' : '');
    /* 上限可能是分级额度，0 位小数会把它显示成 0 CNY */
    var digits = Number(g.amount) < 0.01 ? 6 : 0;
    return F.num(g.amount, digits) + ' ' + (g.unit || '') + tag;
  }

  function renderMix() {
    var d = data(), st = (d.status && d.status.traffic && d.status.traffic.today) || {};
    /* status.today.mix 的计数与 total_tokens 不同量级（后端按请求求和）；
       以 prompt / cached / completion 三段重算，百分比与后端 mix 一致。 */
    var prompt = Number(st.prompt_tokens) || 0;
    var cache = Number(st.cached_tokens) || 0;
    var out = Number(st.completion_tokens) || 0;
    var input = Math.max(0, prompt - cache);
    var total = Number(st.total_tokens) || (prompt + out);
    var pc = function (n) { return total ? (n / total * 100) : 0; };
    var inputPct = pc(input), cachePct = pc(cache), outPct = pc(out);
    var host = HUB.$('#ovMix');
    if (!host) return;
    /* 这里只画结构（比例条），一个数字都不写：四段数量与占比、命中率都在下面的
     * WINDOW TOKENS 表里，同一组数不重复出两遍（红线：不堆重复读数）。 */
    host.innerHTML = '<div class="fx-mix-head"><span>TOKEN MIX / 令牌构成</span></div>'
      + '<div class="fx-mix-bar"><i class="mix-in" style="width:' + Math.max(0, inputPct) + '%"></i><i class="mix-cache" style="width:' + Math.max(0, cachePct) + '%"></i><i class="mix-out" style="width:' + Math.max(0, outPct) + '%"></i></div>';
  }

  /* 窗口读数的 TOKEN 表：把「输入 / 缓存 / 输出 / 合计」摊成一张可对账的表，
     并列命中率与未计价行——这才是使用者真正关心的那张表。 */
  function renderTokens() {
    var st = (data().status && data().status.traffic && data().status.traffic.today) || {};
    var host = HUB.$('#ovTokens');
    if (!host) return;
    var prompt = Number(st.prompt_tokens) || 0;
    var cached = Number(st.cached_tokens) || 0;
    var out = Number(st.completion_tokens) || 0;
    var input = Math.max(0, prompt - cached);
    var total = Number(st.total_tokens) || (prompt + out);
    var has = Number(st.requests) > 0;
    var pc = function (n) { return total && has ? (n / total * 100).toFixed(2) + '%' : '—'; };
    var rows = [
      ['输入 / INPUT', input, pc(input)],
      ['缓存 / CACHE', cached, pc(cached)],
      ['输出 / OUTPUT', out, pc(out)],
      ['合计 / TOTAL', total, has ? '100.00%' : '—']
    ];
    host.innerHTML = '<div class="fx-token-head"><span>WINDOW TOKENS / 窗口令牌</span>'
      + '<b>命中 ' + esc(has ? F.pct(st.cache_hit_pct) : '—') + '</b></div>'
      + '<table class="fx-token-table"><thead><tr><th>段 / SEGMENT</th><th>TOKENS</th><th>占比</th></tr></thead><tbody>'
      + rows.map(function (r) {
        return '<tr><td>' + esc(r[0]) + '</td><td>' + esc(F.tok(r[1])) + '</td><td>' + esc(r[2]) + '</td></tr>';
      }).join('')
      + '</tbody></table>'
      + '<div class="fx-token-foot">'
      + '<span>未计价 ' + esc(has ? F.int(st.unpriced_rows) + ' 行' : '—') + '</span>'
      + '<span>入站 ' + esc(has ? F.int(st.requests) + ' 笔' : '—') + '</span></div>';
  }

  function renderMicro() {
    var d = data();
    var t = (d.dashboard && d.dashboard.today) || {};
    var st = (d.status && d.status.traffic && d.status.traffic.today) || {};
    var rows = (d.usage && d.usage.rows) || [];
    var ttfts = rows.map(function (r) { return Number(r.ttft_ms); }).filter(function (n) { return n > 0 && isFinite(n); });
    var ttft = ttfts.length ? ttfts.reduce(function (a, b) { return a + b; }, 0) / ttfts.length : null;
    var host = HUB.$('#ovMicro');
    if (!host) return;
    host.innerHTML = [
      ['P50 / 中位', F.dur(t.latency && t.latency.p50_ms), ''],
      ['P95 / 尾延迟', F.dur(t.latency && t.latency.p95_ms), ''],
      ['AVG / 平均', F.dur(st.elapsed_ms_avg), ''],
      ['TTFT / 首 token', ttft === null ? '—' : F.dur(ttft), 'SAMPLE ' + ttfts.length]
    ].map(function (x) {
      return '<div><span>' + x[0] + '</span><b>' + esc(x[1]) + '</b>' + (x[2] ? '<em>' + esc(x[2]) + '</em>' : '') + '</div>';
    }).join('');
  }

  function renderPulse() {
    var d = data();
    var series = (d.billing && d.billing.series) || [];
    var nums = series.map(function (x) { return Number(x.n) || 0; });
    var max = Math.max.apply(null, nums.concat([1]));
    var total = nums.reduce(function (a, b) { return a + b; }, 0);
    var peak = Math.max.apply(null, nums.concat([0]));
    var host = HUB.$('#ovPulse');
    if (!host) return;
    host.innerHTML = '<div class="fx-pulse-head"><span>60 MIN PULSE / 近 1H 请求</span><b>' + F.int(total) + ' REQ · PEAK ' + F.int(peak) + ' · 30 × 2MIN</b></div>'
      + '<div class="fx-pulse-bars' + (total ? '' : ' is-empty') + '" aria-hidden="true">' + series.map(function (x) {
        var n = Number(x.n) || 0;
        var h = n ? Math.max(6, Math.round(n / max * 100)) : 1;
        return '<i class="' + (n ? '' : 'zero') + '" style="height:' + h + '%" title="' + esc(F.clock(x.t) + ' · ' + n + ' req') + '"></i>';
      }).join('') + '</div>'
      + '<div class="fx-pulse-axis"><span>' + esc(F.clock(series[0] && series[0].t)) + '</span><span>' + esc(F.clock(series[Math.floor(series.length / 2)] && series[Math.floor(series.length / 2)].t)) + '</span><span>NOW</span></div>';
  }

  function renderSub() {
    var d = data();
    var channels = (d.dashboard && d.dashboard.channels) || [];
    var max = Math.max.apply(null, channels.map(function (c) { return Number(c.requests) || 0 }).concat([1]));
    var st = d.status || {};
    var routing = st.routing || {};
    var accounts = st.accounts || {};
    var official = (d.official && d.official.channels) || [];
    var intl = official.find(function (x) { return x.channel === 'workbuddy-intl'; });
    var cn = official.find(function (x) { return x.channel === 'workbuddy-cn'; });
    var host = HUB.$('#ovSub');
    if (!host) return;
    host.innerHTML = ''
      + '<div class="fx-sub-block"><div class="fx-sub-head"><span>INGRESS BY CHANNEL / 渠道构成</span><b>' + F.int(st.traffic && st.traffic.today && st.traffic.today.requests) + ' REQ</b></div>'
      + channels.slice(0, 3).map(function (c) {
        return '<div class="fx-sub-row"><span>' + esc(HUB.shortChannel(c.key)) + '</span><i><b style="width:' + Math.max(2, Math.round((Number(c.requests) || 0) / max * 100)) + '%"></b></i><em>' + F.int(c.requests) + '</em></div>';
      }).join('')
      + (channels.length ? '' : '<div class="fx-sub-note">窗口无渠道流水。</div>')
      + '<div class="fx-sub-note">官方就绪 ' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.channels_ready) + ' / ' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.channels_total) + ' · 账号活跃 ' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.accounts_active) + ' / ' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.accounts_total) + '</div></div>'
      + '<div class="fx-sub-block"><div class="fx-sub-head"><span>FAILURE BUDGET / 熔断与冷却</span><b>' + F.int(routing.breaker && routing.breaker.active) + ' OPEN</b></div>'
      + '<div class="fx-fail-line"><i class="' + (routing.breaker && routing.breaker.active ? '' : 'on') + '"></i><i></i><i></i><span>' + (routing.breaker && routing.breaker.active ? 'BREAKER ACTIVE' : 'NO BREAKER') + '</span></div>'
      + '<div class="fx-kv-grid"><span>冷却槽</span><b>' + F.int(routing.cooling) + '</b><span>竞态开关</span><b>' + F.int(routing.switches_1h) + '</b><span>账号异常</span><b>' + F.int(accounts.error) + '</b><span>可签到</span><b>' + F.int(accounts.can_checkin) + '</b></div>'
      + '<div class="fx-sub-note">断流 ' + F.int(accounts.breaker) + ' · 到期 ' + F.int(accounts.expired) + ' · 限流 ' + F.int(accounts.throttled) + '</div></div>'
      + '<div class="fx-sub-block"><div class="fx-sub-head"><span>OFFICIAL DELTA / 官方口径</span><b>' + F.int(intl && intl.delta && intl.delta.calls) + ' CALLS</b></div>'
      + '<div class="fx-kv-grid"><span>国际 / ' + esc(quotaOf(intl)) + '</span><b>' + F.int(intl && intl.official && intl.official.calls) + '</b><span>国内 / ' + esc(quotaOf(cn)) + '</span><b>' + F.int(cn && cn.official && cn.official.calls) + '</b><span>Hub 入站</span><b>' + F.int(intl && intl.hub && intl.hub.calls) + '</b><span>额度标签</span><b>' + esc((intl && intl.cost_label) || '—') + '</b></div>'
      + '<div class="fx-sub-note">官方含客户端入口 · 差额不并入结算。</div></div>';
  }

  function renderTape() {
    var d = data();
    /* dashboard.today 只是汇总；缓存命中/令牌口径取 status，两者同源但字段不同。 */
    var t = d.dashboard && d.dashboard.today ? Object.assign({}, d.dashboard.today, {
      cache_hit_pct: (d.status && d.status.traffic && d.status.traffic.today
        && d.status.traffic.today.cache_hit_pct) !== undefined
        ? d.status.traffic.today.cache_hit_pct : d.dashboard.today.cache_hit_pct
    }) : {};
    var host = HUB.$('#ovTape');
    if (!host) return;
    host.innerHTML = ''
      + '<div><span>今日入站 / INGRESS</span><b>' + F.int(t.requests) + '</b><em>REQ</em></div>'
      + '<div><span>今日计费 / SPEND</span><b>' + F.cny(t.cost_cny, 4) + '</b><em>CNY</em><span class="stamp hollow" style="margin-left:5px">CREDIT ' + F.num(t.credit, 1) + '</span></div>'
      + '<div><span>缓存命中 / CACHE</span><b>' + F.num(t.cache_hit_pct, 1) + '</b><em>%</em></div>'
      + '<div><span>失败 / ERRORS</span><b>' + F.int(t.errors) + '</b><em>REQ</em><span class="stamp hollow" style="margin-left:5px">SUCCESS ' + F.pct(t.success_rate) + '</span></div>';
  }

  function renderIngress() {
    var d = data();
    var series = (d.billing && d.billing.series) || [];
    var max = Math.max.apply(null, series.map(function (x) { return Number(x.n) || 0 }).concat([1]));
    var host = HUB.$('#ovIngress');
    if (!host) return;
    host.innerHTML = '<div class="fx-ingress-head"><b>INGRESS PULSE</b>60 MIN / 30 BARS<br>PEAK ' + F.int(Math.max.apply(null, series.map(function (x) { return Number(x.n) || 0 }).concat([0]))) + '</div>'
      + '<div class="fx-ingress-bars" aria-hidden="true">' + series.map(function (x) {
        var n = Number(x.n) || 0;
        return '<i style="height:' + (n ? Math.max(6, Math.round(n / max * 100)) : 2) + '%;opacity:' + (n ? '.72' : '.22') + '" title="' + esc(n + ' req') + '"></i>';
      }).join('') + '</div>'
      + '<div class="fx-ingress-axis"></div>';
  }

  /* 异常：默认只列 2 条，其余收进 <details>；行内不展示上游原文（太长把右栏顶爆），
   * 点一下开抽屉看详情。 */
  function eventRow(e, index) {
    var who = [e.channel, e.model].filter(Boolean).join(' · ');
    var msg = String(e.error || '').trim();
    return '<div class="fx-brief-item is-click" role="button" tabindex="0" data-err-detail="' + index + '"'
      + (msg ? ' title="' + esc(msg.slice(0, 300)) + '"' : '') + '><time>' + esc(F.clock(e.at))
      + '</time><div><b>' + esc(e.kind || e.action || '事件') + '</b><p>' + esc(who || '—')
      + '</p></div><span class="stamp hollow">' + esc(String(e.level || 'INFO').toUpperCase()) + '</span></div>';
  }

  function briefEvents(errors) {
    var head = errors.slice(0, 2).map(function (e, i) { return eventRow(e, i); }).join('');
    if (errors.length <= 2) return head;
    return head + '<details class="fx-brief-more"><summary>查看全部 ' + F.int(errors.length) + ' 条</summary>'
      + '<div class="fx-brief-scroll">'
      + errors.slice(2).map(function (e, i) { return eventRow(e, i + 2); }).join('')
      + '</div></details>';
  }

  /* 额度进度：今日支出对着预算上限一条轨，已收录的官方额度再各占一条轨。
   * 未收录（官方 source=none）的渠道干脆不画，避免一排 —。 */
  function quotaBlock(d) {
    var budgets = d.budgets || {};
    var global = budgets.global || {};
    /* 窗口读数里的官方额度：只看「已收录 + 今日真的有调用」的渠道。
     * 今日没用过的渠道不占进度条位置，免得一排 NO DATA；
     * 官方没公布上限的渠道仍出一行，展示今日实耗并标 LOCAL。 */
    var official = ((d.official && d.official.channels) || []).filter(function (x) {
      if (x.recorded === false) return false;
      var today = (x.windows && x.windows.today) || {};
      var loc = today.local || {};
      var o = x.official || {};
      return Number(loc.requests) > 0 || (o.source && o.source !== 'none');
    });
    var bar = function (label, right, pct, alert) {
      var empty = pct === null || pct === undefined || !isFinite(Number(pct));
      var width = empty ? 0 : Math.max(2, Math.min(100, Number(pct)));
      return '<div class="fx-track-item"><div class="fx-track-meta"><span>' + esc(label) + '</span><em>' + esc(right) + '</em></div>'
        + '<div class="fx-mix-bar' + (empty ? ' is-empty' : '') + (alert ? ' is-alert' : '') + '">'
        + '<i class="fx-bar-gateway" style="width:' + width + '%"></i></div></div>';
    };
    var items = (budgets.items || []).filter(function (x) {
      return x.amount !== null && x.amount !== undefined && Number(x.amount) > 0;
    });
    var rows = [];
    if (global.amount !== null && global.amount !== undefined) {
      rows.push(bar('今日支出 / 全局预算',
        F.cny(global.spent_cny, 3) + ' / ' + F.cny(global.amount, 0)
          + (global.used_pct === null || global.used_pct === undefined ? '' : ' · ' + F.num(global.used_pct, 1) + '%'),
        global.used_pct, global.over || global.warn));
    } else if (items.length) {
      /* 没设全局上限但有渠道上限：用渠道额度合计当尺子，免得一条进度都没得看。 */
      var capSum = items.reduce(function (a, x) { return a + Number(x.amount || 0); }, 0);
      var spentSum = items.reduce(function (a, x) { return a + Number(x.spent_cny || 0); }, 0);
      rows.push(bar('今日支出 / 渠道预算合计', F.cny(spentSum, 3) + ' / ' + F.cny(capSum, 0)
        + (capSum ? ' · ' + F.num(spentSum / capSum * 100, 1) + '%' : ''),
        capSum ? spentSum / capSum * 100 : null, false));
    } else {
      var today = (d.dashboard && d.dashboard.today) || {};
      rows.push('<div class="fx-brief-text">今日支出 ' + esc(F.cny(global.spent_cny, 3))
        + ' · ' + F.int(today.requests) + ' 笔 · 未设上限（面板设置 → 预算硬顶 可加一道 402 闸门）</div>');
    }
    items.forEach(function (x) {
      rows.push(bar((x.label || HUB.shortChannel(x.key)) + ' / 渠道预算',
        F.cny(x.spent_cny, 3) + ' / ' + F.cny(x.amount, 0)
          + (x.used_pct === null || x.used_pct === undefined ? '' : ' · ' + F.num(x.used_pct, 1) + '%'),
        x.used_pct, x.over || x.warn));
    });
    official.forEach(function (x) {
      var today = (x.windows && x.windows.today) || {};
      var loc = today.local || {};
      /* 官方只报了总窗口（不分日/周/月）时拿它当今日那一行，并注明未分窗——
       * 有官方数就不该退回本地去充数。 */
      var whole = x.official && x.official.source && x.official.source !== 'none' ? x.official : null;
      var off = today.official || (whole && (whole.used !== null && whole.used !== undefined) ? whole : null);
      var unscoped = !today.official && !!off;
      var pct = off && off.used_pct !== null && off.used_pct !== undefined ? off.used_pct : null;
      var right;
      if (off) {
        right = F.num(off.used, 0) + (off.limit ? ' / ' + F.num(off.limit, 0) : '')
          + (off.unit ? ' ' + off.unit : '')
          + (pct === null ? ' · 官方未报上限' : ' · ' + F.num(pct, 1) + '%')
          + (unscoped ? ' · 官方未分窗' : '')
          + (off.reset_at ? ' · 重置 ' + off.reset_at : '');
      } else {
        right = '今日 ' + F.int(loc.requests) + ' 笔 · ' + F.cny(loc.cost_cny, 3) + ' · 官方未公布';
      }
      rows.push(bar(HUB.shortChannel(x.channel) + ' / 官方今日', right, pct, pct !== null && pct >= 80));
    });
    if (!official.length) {
      rows.push('<div class="fx-brief-text">官方额度：暂无已收录且有官方读数的渠道（到 06 机柜登录一次即收录，配置保留）。</div>');
    }
    var warn = (global.warn || global.over || items.some(function (x) { return x.warn || x.over; }));
    return '<div class="fx-brief-block' + (warn ? ' is-alert' : '') + '"><div class="fx-brief-label"><span>QUOTA / 额度进度</span><span>'
      + (warn ? '≥80%' : F.int(official.length) + ' 收录') + '</span></div>' + rows.join('') + '</div>';
  }

  /* 官方口径对比：只对已收录渠道画；一个都没收录就整块不出现。 */
  function officialBrief(list) {
    var rows = (list || []).filter(function (x) {
      var o = x.official || {};
      if (x.recorded === false) return false;
      return o.source && o.source !== 'none';
    });
    if (!rows.length) return '';
    var delta = rows.reduce(function (a, x) {
      var rec = x.reconcile || {};
      return a + (Number(rec.delta_requests) || 0);
    }, 0);
    var alert = delta > 100;
    return '<div class="fx-brief-block' + (alert ? ' is-alert' : '') + '"><div class="fx-brief-label"><span>OFFICIAL DELTA / 官方口径</span>'
      + '<span class="stamp ' + (alert ? '' : 'hollow') + '">' + (alert ? 'DELTA ALERT' : 'DELTA ' + F.int(delta)) + '</span></div>'
      + rows.map(function (x) {
        var local = x.local || {};
        return '<div class="fx-brief-pair"><span>' + esc(HUB.shortChannel(x.channel)) + ' HUB</span><b>' + F.int(local.requests) + ' 笔</b></div>';
      }).join('')
      + (alert ? '<button class="btn small" type="button" data-fresh-official>拍摄官方快照</button>' : '')
      + '</div>';
  }

  function renderBrief() {
    var d = data(), st = d.status || {};
    var health = st.health_score, htext = st.health_text || '';
    var errors = (d.errors && d.errors.rows) || [];
    var switches = (d.routes && d.routes.switches) || [];
    var cloud = d.cloud || {};
    var official = (d.official && d.official.channels) || [];
    var cooling = ((d.routes && d.routes.cells) || []).filter(function (c) { return c.state === 'cooling'; });
    var host = HUB.$('#ovBrief');
    if (!host) return;
    host.innerHTML = '<div class="fx-brief-head"><b>异常与降级</b><em>EXCEPTION BRIEF</em><span class="stamp">' + F.int(errors.length) + ' 条</span></div>'
      + '<div class="fx-brief-block"><div class="fx-brief-label"><span>HEALTH / 健康分</span><span>' + esc(htext) + '</span></div>'
      + '<div class="fx-health-line"><b>' + (health === null || health === undefined || !isFinite(Number(health)) ? '—' : F.int(health)) + '</b><span class="stamp hollow">'
      + (health === null || health === undefined || !isFinite(Number(health)) ? 'NO READING'
        : (health >= 90 ? 'GOOD' : health >= 70 ? 'WATCH' : 'DEGRADED')) + '</span></div>'
      + ((st.health_reasons || []).length ? '<div class="fx-brief-text">' + esc(st.health_reasons.join(' · ')) + '</div>' : '')
      + '<div class="fx-brief-text">账号 ' + F.int(st.accounts && st.accounts.ok) + ' / ' + F.int(st.accounts && st.accounts.total) + ' 正常 · 渠道 ' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.channels_ready) + ' / ' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.channels_total) + ' 就绪 · 冷却 ' + F.int(st.routing && st.routing.cooling) + '</div></div>'
      + quotaBlock(d)
      + '<div class="fx-brief-block"><div class="fx-brief-label"><span>LATEST EVENTS / 最新异常</span><span>TAIL ' + F.int(errors.length) + '</span></div>'
      + (errors.length ? briefEvents(errors) : '<div class="fx-brief-text">窗口无异常记录。</div>')
      + '</div>'
      + '<div class="fx-brief-block"><div class="fx-brief-label"><span>ROUTE ACTIONS / 切换</span><span>' + F.int(switches.length) + '</span></div>'
      + (switches.slice(0, 4).map(function (s) {
        return '<div class="fx-brief-item"><time>' + esc(F.clock(s.at)) + '</time><div><b>' + esc(s.from + ' → ' + s.to) + '</b><p>' + esc((s.group || '') + ' · ' + (s.model || '') + ' · ' + (s.reason || '')) + '</p></div><span class="stamp hollow">FAILOVER</span></div>';
      }).join('') || '<div class="fx-brief-text">近窗口无槽位切换。</div>')
      + '</div>'
      + officialBrief(official)
      + '<div class="fx-brief-block"><div class="fx-brief-label"><span>COOLING / 冷却格</span><span>' + cooling.length + '</span></div>'
      + (cooling.slice(0, 3).map(function (c) {
        return '<div class="fx-brief-item fx-brief-action" role="button" tabindex="0" data-cool-account="' + esc(c.account) + '" data-cool-model="' + esc(c.model) + '"><time>' + esc(HUB.fmtRange(c.secs_left)) + '</time><div><b>#' + esc(c.account) + ' · ' + esc(c.model) + '</b><p>' + esc(HUB.shortChannel(c.group)) + ' · ' + esc(c.reason || 'cooling') + '</p></div><span class="stamp hollow">解冻</span></div>';
      }).join('') || '<div class="fx-brief-text">当前无冷却格。</div>')
      + (cooling.length ? '<button class="btn small" type="button" data-open-routing>打开路由冷却台账</button>' : '')
      + '</div>'
      + '<div class="fx-brief-block"><div class="fx-brief-label"><span>CLOUD SYNC / 双网关</span><span>' + (cloud.ok ? 'OK' : 'DOWN') + '</span></div>'
      + '<div class="fx-brief-pair"><span>USAGE LAG</span><b>' + F.int(cloud.usage && cloud.usage.lag) + ' 行</b><span>EVENT LAG</span><b>' + F.int(cloud.events && cloud.events.lag) + ' 行</b><span>LAST RUN</span><b>' + esc(cloud.at ? F.clock(cloud.at) : '—') + '</b><span>INTERVAL</span><b>' + F.int(cloud.interval_s) + 'S</b></div></div>'
      + '<div class="fx-brief-block"><div class="fx-brief-label"><span>SECURITY / 安全基线</span><span>' + esc(d.security && d.security.level) + '</span></div>'
      + '<div class="fx-brief-pair"><span>SCORE</span><b>' + F.int(d.security && d.security.score) + ' / 100</b><span>KEYS</span><b>' + F.int(d.security && d.security.stats && d.security.stats.keys) + '</b><span>CRYPTO</span><b>' + esc(d.security && d.security.stats && d.security.stats.crypto) + '</b></div></div>';
    /* 右栏不铺明细：点某一条才开抽屉看上游原文。 */
    Array.from(host.querySelectorAll('[data-err-detail]')).forEach(function (node) {
      node.addEventListener('click', function () {
        openEventDetail(errors[Number(node.getAttribute('data-err-detail'))]);
      });
    });
  }

  function openEventDetail(e) {
    if (!e) return;
    var rows = [
      ['时间 / AT', F.clock(e.at)],
      ['渠道 / CHANNEL', e.channel || '—'],
      ['模型 / MODEL', e.model || '—'],
      ['类型 / KIND', e.kind || e.action || '—'],
      ['请求 ID', e.req_id || e.rid || '—']
    ];
    HUB.openWO({
      kicker: 'EVENT / 异常详情',
      title: e.kind || e.action || '事件',
      meta: F.clock(e.at),
      body: '<div class="fx-wo-block"><div class="fx-wo-kv">'
        + rows.map(function (r) { return '<span>' + esc(r[0]) + '</span><b>' + esc(r[1]) + '</b>'; }).join('')
        + '</div></div><div class="fx-wo-block"><div class="fx-wo-label"><span>UPSTREAM / 上游原文</span><b>RAW</b></div>'
        + '<div class="fx-wo-text fx-wo-raw">' + esc(String(e.error || '（上游没回错误文本）')) + '</div></div>',
      actions: [{ label: '收起', close: true }]
    });
  }

  function renderInventory() {
    var d = data(), st = d.status || {};
    var host = HUB.$('#ovInventory');
    if (!host) return;
    host.innerHTML = '<div class="inv-label">SYSTEM<br>INVENTORY<em>系统底账</em></div>'
      + '<div class="fx-inv-item"><b>' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.channels_ready) + '<em> / ' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.channels_total) + '</em></b><span>官方通道就绪</span></div>'
      + '<div class="fx-inv-item"><b>' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.accounts_active) + '<em> / ' + F.int(d.dashboard && d.dashboard.health && d.dashboard.health.accounts_total) + '</em></b><span>活跃账户 / 总账户</span></div>'
      + '<div class="fx-inv-item"><b>' + F.int(d.catalog && d.catalog.total) + '<em> / ' + F.int((d.catalog && d.catalog.models || []).filter(function (m) { return m.enabled; }).length) + '</em></b><span>目录模型 / 已启用</span></div>'
      + '<div class="fx-inv-item"><b>' + F.int((d.keys || []).length) + '<em> / ' + F.int(st.accounts && st.accounts.total) + '</em></b><span>密钥 / 账号</span></div>';
  }

  function setPromptMode(mode) {
    HUB.job({
      title: '提示词优化 / ' + String(mode || '').toUpperCase(),
      endpoint: '/api/prompt-opt/mode',
      payload: { mode: mode },
      confirm: '把提示词前置优化切到 ' + mode
        + '？shadow 只记录不注入，enforce 会真实改写上游请求。',
      timeout: 20000,
      local: true,
      refresh: true,
      channels: []
    });
  }

  /* 一句话状态：报头下的首屏结论，代替读数堆叠。 */
  function renderStatusLine() {
    var d = data(), st = d.status || {};
    var h = (d.dashboard && d.dashboard.health) || {};
    var rs = st.routing || {}, cloud = d.cloud || {};
    var po = (d.health && d.health.prompt_opt) || {};
    var mode = String(po.mode || 'off').toLowerCase();
    var el = HUB.$('#ovStatusLine');
    if (!el) return;
    var score = Number(st.health_score);
    var breaker = !!(rs.breaker && rs.breaker.active);
    var ok = (isFinite(score) ? score >= 70 : true) && !breaker;
    var ready = Number(h.channels_ready) || 0;
    var totalCh = Number(h.channels_total) || 0;
    el.className = 'fx-status-line' + (ok ? '' : ' is-warn');
    el.innerHTML = '<b>' + (ok ? '网关正常' : '网关降级') + '</b> '
      + '<span>渠道 ' + F.int(ready) + ' / ' + F.int(totalCh) + ' 就绪</span>'
      + '<span>冷却 ' + F.int(rs.cooling) + '</span>'
      + (breaker ? '<span>熔断已触发</span>' : '')
      + '<span>云端 ' + (cloud.ok === false ? '链路中断' : '同步正常') + '</span>'
      + '<span class="fx-opt-switch" role="group" aria-label="提示词优化模式"><b>PROMPT</b>'
      + ['off', 'shadow', 'enforce'].map(function (m) {
        return '<button type="button" data-prompt-mode="' + m + '" class="' + (mode === m ? 'is-on' : '') + '" title="切到 ' + m + '">' + m.toUpperCase() + '</button>';
      }).join('') + '</span>';
  }

  function toggleDetail(open) {
    var btn = HUB.$('#ovDetailBtn');
    ['#ovMicro', '#ovIngress'].forEach(function (sel) {
      var el = HUB.$(sel);
      if (el) el.hidden = !open;
    });
    if (btn) {
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      btn.textContent = open ? '细节 ▴' : '细节 ▾';
    }
  }

  function render() {
    renderMasthead();
    renderStatusLine();
    renderHero();
    renderMix();
    renderTokens();
    renderMicro();
    renderPulse();
    renderSub();
    renderTape();
    renderIngress();
    renderBrief();
    renderInventory();
  }

  HUB.views.overview = render;

  document.addEventListener('click', function (e) {
    var pm = e.target.closest('[data-prompt-mode]');
    if (pm) { setPromptMode(pm.getAttribute('data-prompt-mode')); return; }
    var fresh = e.target.closest('[data-fresh-official]');
    if (fresh) { HUB.freshOfficial(); return; }
    var cool = e.target.closest('[data-cool-account]');
    if (cool) {
      HUB.gotoCooling(cool.getAttribute('data-cool-account'), cool.getAttribute('data-cool-model'));
      return;
    }
    if (e.target.closest('[data-open-routing]')) { HUB.setView('routing'); return; }
    var detail = e.target.closest('#ovDetailBtn');
    if (detail) {
      toggleDetail(detail.getAttribute('aria-expanded') !== 'true');
      return;
    }
    var b = e.target.closest('#ovMetricSwitch button');
    if (!b) return;
    currentMetric = b.getAttribute('data-metric') || 'requests';
    renderHero();
  });
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('[data-cool-account]')) {
      e.preventDefault();
      var cool = e.target.closest('[data-cool-account]');
      HUB.gotoCooling(cool.getAttribute('data-cool-account'), cool.getAttribute('data-cool-model'));
    }
  });
  document.addEventListener('DOMContentLoaded', render);
  if (document.readyState !== 'loading') render();
})();
