/* 02 实时流水 · /api/usage?limit=50 */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;
  var state = { status: 'all', channel: 'all', model: 'all', q: '', selected: 0, win: 0 };

  function data() { return HUB.data.state.data || {}; }
  function rows() { return (data().usage && data().usage.rows) || []; }

  /* 统计窗口：按真实时间窗筛行，而不是把「载入的 50 条」冒充成时间窗。
   * win=0 表示就看本次载入的样本，并在标题里直说是样本。 */
  function windowRows() {
    var all = rows();
    if (!state.win) return all;
    var cut = Date.now() / 1000 - state.win * 60;
    return all.filter(function (r) { return Number(r.at) >= cut; });
  }
  function windowMeta() {
    var all = windowRows();
    var label = state.win ? ('近 ' + state.win + ' 分钟') : '本次载入';
    if (!all.length) return { rows: 0, spanMin: null, covered: false, empty: true, label: label };
    var newest = Number(all[0].at) || 0, oldest = Number(all[all.length - 1].at) || 0;
    var spanMin = Math.max(0, Math.round((newest - oldest) / 60));
    /* 样本比窗口短 → 只覆盖到样本最老那条，不能宣称整窗。 */
    var covered = state.win ? (spanMin >= state.win - 1) : true;
    return { rows: all.length, spanMin: spanMin, covered: covered,
             label: state.win ? ('近 ' + state.win + ' 分钟') : '本次载入' };
  }
  function tierMeta(r) {
    var t = String(tierText(r) || '');
    if (/峰|peak/i.test(t)) return { cls: 'is-peak', text: t };
    if (/谷|off.?peak|valley/i.test(t)) return { cls: 'is-off', text: t };
    return { cls: 'is-none', text: t === '—' ? '' : t };
  }

  function statusOf(r) {
    if (r.error || (r.outcome && r.outcome !== 'completed' && r.outcome !== 'ok')) return 'error';
    return 'ok';
  }
  function tierText(r) {
    if (r.tier && typeof r.tier === 'object') return r.tier.txt || r.tier.k || '—';
    return r.tier || '—';
  }
  function savedCny(v) {
    if (v === null || v === undefined) return null;
    var n = Number(v);
    return isFinite(n) ? F.cny(n, 4) : null;
  }
  function cachePctText(r) {
    var p = Number(r.prompt_tokens) || 0;
    var c = Number(r.cached_tokens) || 0;
    if (p > 0) return F.pct(c / p * 100, 0);
    if (r.cache_hit_pct) return F.pct(r.cache_hit_pct, 0);
    return '—';
  }
  function pruneText(r) {
    if (!r.prune) return '—';
    var m = String(r.prune).match(/fold=(\d+),save=(\d+)/);
    if (m) return 'F' + m[1] + ' · ' + F.tok(m[2]);
    return String(r.prune);
  }
  function costOf(r) {
    if (r._cost && r._cost.cny !== undefined) return Number(r._cost.cny) || 0;
    if (r.cost_cny !== undefined && r.cost_cny !== null) return Number(r.cost_cny) || 0;
    return 0;
  }
  function channelOf(r) {
    return r.channel_label || r.upstream || r.channel || '—';
  }
  /* 决策模型元数据由 ui.js 统一提供；02 只负责渲染，03 / 05 共用同一口径。 */
  function streamFlags(r) {
    var info = HUB.decisionInfo(r);
    var tags = [];
    if (info.decision) tags.push('<em class="fx-decision-flag">DECISION</em>');
    if (info.inputOnly) tags.push('<em class="fx-decision-flag">IN-ONLY</em>');
    return tags.join(' ');
  }
  function receiptStatus(r) {
    var code = r.status === null || r.status === undefined ? '' : String(r.status);
    if (statusOf(r) === 'error') return code ? code + ' ERROR' : 'ERROR';
    return (code || '200') + ' OK';
  }
  function filtered() {
    var q = state.q.trim().toLowerCase();
    return rows().filter(function (r) {
      if (state.status !== 'all' && statusOf(r) !== state.status) return false;
      if (state.channel !== 'all' && String(r.channel) !== state.channel) return false;
      if (state.model !== 'all' && String(r.model) !== state.model) return false;
      if (q) {
        var hay = [r.model, r.rid, r.key_name, r.upstream, r.channel, r.channel_label, r.error].join(' ').toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    });
  }

  /* ── 网关派发台账：GROUP / LOCKED / TIER 的真实来源 ─────────────────
   * 本地账本用网关自己的 X-Request-Id 记 rid；反代模式绑定上游
   * x-hub-rid。只有 /api/usage 行带同一个 rid 时 server 才挂 hub_trace，
   * 行内角标与洋葱轨迹随之出现；不按模型名或时间邻接猜测。
   */
  var traces = { list: [], error: '', loading: false, last: 0, timer: 0 };

  function traceStatus(t) {
    if (!t) return { text: '—', cls: '' };
    if (t.state === 'running') return { text: 'RUNNING', cls: 'is-running' };
    if (t.state === 'failed') return { text: 'FAILED', cls: 'is-error' };
    var code = t.upstream_status;
    if (code === null || code === undefined) return { text: 'NO HTTP', cls: '' };
    var ms = t.upstream_ms === null || t.upstream_ms === undefined ? '' : ' · ' + F.dur(t.upstream_ms);
    return { text: String(code) + ms, cls: Number(code) >= 400 ? 'is-error' : '' };
  }
  function lockStamp(t) {
    if (t.locked) return '<span class="stamp is-lock">LOCKED</span>';
    var st = String(t.lock_state || '').toUpperCase();
    if (st === 'BOUND') return '<span class="stamp hollow">BOUND</span>';
    if (st === 'BYPASS') return '<span class="stamp hollow">NO TASK</span>';
    if (st === 'OFF') return '<span class="stamp hollow">LOCK OFF</span>';
    return '<span class="stamp hollow">NO LOCK</span>';
  }
  function tierStamp(t) {
    return '<span class="stamp hollow">TIER ' + esc(String(t.tier || '—').toUpperCase()) + '</span>';
  }
  function dispatchRowHtml(t) {
    var st = traceStatus(t);
    return '<button type="button" class="fx-dispatch-row" data-trace-id="' + esc(t.trace_id) + '" title="打开洋葱轨迹">'
      + '<span class="fx-dispatch-time">' + esc(F.clock(t.at)) + '</span>'
      + '<span class="fx-dispatch-group"><b>' + esc(t.group_id || '—') + '</b><em>' + (t.task_id ? '#' + esc(t.task_id) : 'NO TASK-ID') + '</em></span>'
      + '<span class="fx-dispatch-model">' + esc(t.model || '—') + '</span>'
      + '<span class="fx-dispatch-tags">' + lockStamp(t) + tierStamp(t) + '</span>'
      + '<span class="fx-dispatch-state ' + st.cls + '">' + esc(st.text) + '</span>'
      + '<span class="fx-dispatch-open">轨迹 ›</span>'
      + '</button>';
  }
  function renderDispatch() {
    var host = HUB.$('#streamDispatch');
    if (!host) return;
    var rows = traces.list;
    var head = '<div class="fx-dispatch-top"><div class="fx-dispatch-title"><b>DISPATCH / 网关派发</b><span>GROUP · LOCKED · TIER</span></div>'
      + '<span class="fx-dispatch-count">' + (traces.error ? 'UNWIRED' : rows.length + ' TRACE') + '</span>'
      + '<button class="btn small" type="button" id="streamDispatchAll"' + (rows.length ? '' : ' disabled') + '>台账</button></div>';
    var body;
    if (traces.error) {
      body = '<div class="fx-dispatch-empty">派发台账暂不可读：' + esc(traces.error) + '</div>';
    } else if (!rows.length) {
      body = '<div class="fx-dispatch-empty">等待 auto/* 入站 · 台账只记真实分组派发。</div>';
    } else {
      body = '<div class="fx-dispatch-list">' + rows.slice(0, 2).map(dispatchRowHtml).join('') + '</div>';
    }
    var html = head + body;
    if (host.innerHTML !== html) host.innerHTML = html;
  }
  function loadTraces(force) {
    if (HUB.currentView && HUB.currentView() !== 'stream') return;
    if (traces.loading) return;
    if (!force && Date.now() - traces.last < 6000) return;
    traces.loading = true;
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 8000);
    fetch('/api/dispatch/traces?limit=50', {
      credentials: 'same-origin',
      cache: 'no-store',
      signal: ctrl.signal,
      headers: { accept: 'application/json' }
    }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (payload) {
      traces.list = payload && Array.isArray(payload.traces) ? payload.traces : [];
      traces.error = '';
      traces.last = Date.now();
      renderDispatch();
    }).catch(function (err) {
      traces.error = err && err.name === 'AbortError' ? '超时' : (err.message || '不可用');
      traces.last = Date.now();
      renderDispatch();
    }).finally(function () {
      clearTimeout(timer);
      traces.loading = false;
    });
  }
  function ensureTracePoll() {
    if (traces.timer) return;
    traces.timer = setInterval(function () {
      if (document.hidden) return;
      if (HUB.currentView && HUB.currentView() !== 'stream') return;
      loadTraces();
    }, 8000);
  }
  function traceById(id) {
    return traces.list.find(function (t) { return t.trace_id === id; }) || null;
  }
  function onionStep(no, en, cn, stamp, stampCls, rows) {
    var kv = rows.map(function (row) {
      return '<span>' + esc(row[0]) + '</span><b>' + esc(row[1] === null || row[1] === undefined || row[1] === '' ? '—' : row[1]) + '</b>';
    }).join('');
    return '<section class="fx-onion-step" aria-label="' + esc(en) + '">'
      + '<div class="fx-onion-no">' + esc(no) + '</div>'
      + '<div class="fx-onion-main"><div class="fx-onion-top"><div><b>' + esc(en) + '</b><span class="fx-onion-cn">' + esc(cn) + '</span></div>'
      + '<span class="stamp ' + (stampCls || '') + '">' + esc(stamp) + '</span></div>'
      + '<div class="fx-wo-kv">' + kv + '</div></div></section>';
  }
  function policyText(t) {
    var p = t.policy || {};
    return ['TASK_LOCK ' + (p.task_lock ? 'ON' : 'OFF'),
      'SMART_TIER ' + (p.smart_tier ? 'ON' : 'OFF'),
      'RACING ' + (p.hedged_racing || p.racing ? 'ON' : 'OFF')].join(' · ');
  }
  function openTrace(id) {
    var t = traceById(id);
    if (!t) { HUB.toast('轨迹已滚出缓冲 / NOT FOUND'); return; }
    var st = traceStatus(t);
    var stateText = String(t.lock_state || '').toUpperCase();
    var lockCn = { HIT: '命中已有锁', BOUND: '首轮绑定', OFF: '分组策略关闭', BYPASS: '未携带任务号' }[stateText] || '未绑定';
    var reqId = t.request_id && t.request_id !== t.rid
      ? '<span class="fx-onion-rid">REQ ' + esc(t.request_id) + '</span>' : '';
    var ingress = [
      ['PATH / 路径', t.path || '—'],
      ['ASK / 请求', t.requested_model],
      ['TASK / 任务', t.task_id || '未携带'],
      ['RID / 计费行', t.rid || '—']
    ];
    if (t.request_id && t.request_id !== t.rid) ingress.push(['REQ / 网关', t.request_id]);
    var body = '<div class="on-steel">'
      + '<div class="fx-onion-head"><span class="stamp">TRACE</span><b>' + esc(t.trace_id) + '</b><span class="fx-onion-rid">RID ' + esc(t.rid || '—') + '</span>' + reqId + '</div>'
      + onionStep('01', 'INGRESS', '入站', 'RECEIVED', 'hollow', ingress)
      + onionStep('02', 'TASK LOCK', '任务锁', t.locked ? 'LOCKED' : (stateText || 'NO LOCK'), t.locked ? '' : 'hollow', [
        ['STATE / 判定', lockCn],
        ['TTL / 开票时', t.lock_ttl ? HUB.fmtRange(t.lock_ttl) : '—'],
        ['POLICY / 策略', policyText(t)]
      ])
      + onionStep('03', 'DISPATCHER', '分流裁决', 'RESOLVED', 'hollow', [
        ['MODEL / 下发', t.model],
        ['TIER / 档位', t.tier || '—'],
        ['PRIORITY / 优先级', t.priority],
        ['ELAPSED / 裁决', t.dispatch_ms === null || t.dispatch_ms === undefined ? '—' : Number(t.dispatch_ms).toFixed(2) + 'ms']
      ])
      + onionStep('04', 'UPSTREAM', '上游回执', st.text, st.cls === 'is-error' ? 'alert' : (t.state === 'running' ? 'hollow' : ''), [
        ['HTTP / 状态', t.upstream_status],
        ['ELAPSED / 耗时', t.upstream_ms === null || t.upstream_ms === undefined ? '—' : F.dur(t.upstream_ms)],
        ['BYTES / 字节', t.response_bytes === null || t.response_bytes === undefined ? '—' : F.int(t.response_bytes)],
        ['ERROR / 摘要', t.error || '—']
      ])
      + '<div class="fx-onion-unwired"><b>非本进程层</b><br>PRUNER · PII VAULT · SSE HEAL 没有回传遥测，不代填。</div>'
      + '</div>';
    HUB.openWO({
      kicker: 'TRACE / 洋葱轨迹',
      title: '请求轨迹',
      meta: 'TRACE ' + String(t.trace_id || '').slice(0, 12).toUpperCase() + ' · ' + (t.group_id || '—'),
      body: body,
      actions: [
        { label: '复制 TRACE ID', close: false, onClick: function () { HUB.copyText(t.trace_id, 'TRACE ID 已复制 / READY'); } },
        { label: '收起轨迹', filled: true }
      ]
    });
  }
  function openTraceLedger() {
    var rows = traces.list;
    if (!rows.length) { HUB.toast('派发台账为空 / EMPTY'); return; }
    var table = '<table class="fx-wo-table"><thead><tr><th>时间</th><th>分组 / 任务</th><th>模型</th><th>锁</th><th>档位</th><th>上游</th><th></th></tr></thead><tbody>'
      + rows.map(function (t) {
        var st = traceStatus(t);
        return '<tr><td>' + esc(F.clock(t.at)) + '</td>'
          + '<td><b>' + esc(t.group_id || '—') + '</b><br><span class="fx-onion-rid">' + (t.task_id ? '#' + esc(t.task_id) : 'NO TASK-ID') + '</span></td>'
          + '<td>' + esc(t.model || '—') + '</td>'
          + '<td>' + (t.locked ? 'LOCKED' : esc(t.lock_state || '—')) + '</td>'
          + '<td>' + esc(t.tier || '—') + '</td>'
          + '<td>' + esc(st.text) + '</td>'
          + '<td><button class="btn small" type="button" data-trace-id="' + esc(t.trace_id) + '">轨迹</button></td></tr>';
      }).join('')
      + '</tbody></table>';
    HUB.openWO({
      kicker: 'DISPATCH LEDGER / 派发台账',
      title: '派发轨迹',
      meta: rows.length + ' TRACE · 本次进程',
      body: '<div class="on-steel">' + table + '</div>',
      actions: [{ label: '收起台账', filled: true }]
    });
  }

  function syncFilters() {
    var chSel = HUB.$('#streamChannel');
    var mdSel = HUB.$('#streamModel');
    if (!chSel || !mdSel) return;
    var chs = Array.from(new Set(rows().map(function (r) { return String(r.channel || ''); }).filter(Boolean))).sort();
    var mds = Array.from(new Set(rows().map(function (r) { return String(r.model || ''); }).filter(Boolean))).sort();
    function fill(sel, values, allLabel, current) {
      var html = '<option value="all">' + allLabel + '</option>' + values.map(function (v) {
        return '<option value="' + esc(v) + '">' + esc(v) + '</option>';
      }).join('');
      if (sel.innerHTML !== html) sel.innerHTML = html;
      sel.value = values.indexOf(current) >= 0 ? current : 'all';
    }
    fill(chSel, chs, '全部渠道', state.channel);
    fill(mdSel, mds, '全部模型', state.model);
    if (state.channel !== chSel.value) state.channel = chSel.value;
    if (state.model !== mdSel.value) state.model = mdSel.value;
  }

  function renderHead() {
    var host = HUB.$('#streamHead');
    if (!host) return;
    host.innerHTML = '<span>时间<em>TIME</em></span><span>渠道<em>CHANNEL</em></span><span>模型 / 路由<em>MODEL / ROUTE</em></span><span>TTFT<em>FIRST TOKEN</em></span><span>峰谷<em>TIER</em></span><span>裁剪<em>PRUNE</em></span><span>令牌<em>IN / CACHE / OUT</em></span><span>成本<em>CNY</em></span><span>判定<em>VERDICT</em></span>';
  }

  function rowHtml(r, idx) {
    var st = statusOf(r);
    var sel = idx === state.selected ? ' is-selected' : '';
    var tier = tierText(r);
    var sub = esc(r.rid || '—') + (r.route ? ' · ' + esc(r.route) : '');
    if (r.hub_trace) {
      sub += ' · ' + esc(r.hub_trace.group_id || '—');
      if (r.hub_trace.locked) sub += ' · LOCKED';
      if (r.hub_trace.tier) sub += ' · ' + esc(String(r.hub_trace.tier).toUpperCase());
    }
    sub += ' ' + streamFlags(r);
    return '<div class="fx-stream-row' + sel + '" data-idx="' + idx + '" tabindex="0" role="row" aria-label="' + esc(r.model + ' ' + F.clock(r.at)) + '">'
      + '<span class="r-time">' + esc(F.clock(r.at)) + '</span>'
      + '<span>' + esc(HUB.shortChannel(r.channel)) + '</span>'
      + '<span class="r-model">' + esc(r.model || '—') + '<span class="r-sub">' + sub + '</span></span>'
      + '<span class="r-num">' + esc(F.dur(r.ttft_ms)) + '</span>'
      + '<span class="r-tier"><i class="fx-tier-icon ' + tierMeta(r).cls + '" aria-hidden="true"></i>' + esc(tier) + '</span>'
      + '<span>' + esc(pruneText(r)) + '</span>'
      + '<span class="r-num">' + esc(F.tok(r.prompt_tokens)) + ' / ' + esc(F.tok(r.cached_tokens)) + ' / ' + esc(F.tok(r.completion_tokens)) + '</span>'
      + '<span class="r-cost">' + esc(F.cny(costOf(r), 4)) + '</span>'
      + '<span class="' + (st === 'error' ? 'r-error' : '') + '">' + (st === 'error' ? 'ERROR' : 'OK') + '</span>'
      + '</div>';
  }

  function renderTable() {
    var host = HUB.$('#streamRows');
    if (!host) return;
    var list = filtered();
    if (state.selected >= list.length) state.selected = 0;
    host.innerHTML = list.length ? list.map(function (r, i) { return rowHtml(r, i); }).join('')
      : '<div class="fx-receipt-empty" style="padding:12px">当前筛选没有匹配的流水。</div>';
    var visible = HUB.$('#streamVisible');
    if (visible) visible.textContent = String(list.length);
    var buffer = HUB.$('#streamBuffer');
    if (buffer) buffer.textContent = String(rows().length);
    var countHead = HUB.$('#streamCountHead');
    if (countHead) countHead.textContent = String(list.length);
    var all = rows();
    var span = all.length > 1 ? Math.round((Number(all[0].at) - Number(all[all.length - 1].at)) / 60) : 0;
    var spanHead = HUB.$('#streamSpanHead');
    if (spanHead) spanHead.textContent = span ? String(span) : '—';
    var stateEl = HUB.$('#streamState');
    if (stateEl) stateEl.textContent = 'TAIL / 最近 ' + all.length + ' 条 · ' + (span || '—') + ' MIN · 统计窗 ' + windowMeta().label;
  }

  function receiptHtml(r) {
    if (!r) return '<div class="fx-receipt-empty">点击流水行查看回执。</div>';
    var tier = tierText(r);
    var cost = r._cost || {};
    var ht = r.hub_trace || null;
    var total = cost.cny !== undefined ? Number(cost.cny) : costOf(r);
    var info = HUB.decisionInfo(r);
    return '<div class="fx-rail-block"><div class="fx-rail-title"><b>SELECTED / 当前笔</b><span>RECEIPT</span></div>'
      + '<div class="fx-receipt-id" title="usage 行的计费请求号，用于挂接派发轨迹"><span>计费请求号 / USAGE RID</span>' + esc(r.rid || '—') + '</div>'
      + '<div class="fx-receipt-route">' + esc(r.model || '—') + ' · ' + esc(channelOf(r))
      + (r.route ? '<br>' + esc(r.route) : '') + '</div>'
      + (info.decision ? '<div class="fx-receipt-cost fx-receipt-decision"><div class="fx-rail-title"><b>DECISION ROUTE / 决策端点</b><span>INPUT-ONLY</span></div>'
        + '<div class="fx-receipt-kv"><span>端点 / ENDPOINT</span><b>' + esc(info.endpoint || '/v1/systemone') + '</b>'
        + '<span>计费 / BASIS</span><b>' + (info.inputOnly ? 'IN-ONLY' : 'TOKEN') + '</b>'
        + '<span>输出 / OUTPUT TOKENS</span><b>' + (info.inputOnly ? '0 · 不计费' : esc(F.tok(r.completion_tokens))) + '</b></div>'
        + '<div class="fx-receipt-note">' + (info.inputOnly
          ? '官方按输入 token 计费，output_tokens=0；out=0 不是免费牌价。'
          : '决策端点按输入输出分别计费。') + (statusOf(r) === 'error' ? ' 本笔失败，未计费。' : '') + '</div></div>' : '')
      + (ht ? '<div class="fx-receipt-trace">TRACE ' + esc(ht.trace_id || '—') + ' · ' + esc(ht.group_id || '—') + '</div>' : '')
      + '<div class="fx-receipt-tags">'
      + (info.decision ? '<span class="stamp">DECISION</span>' : '')
      + (info.inputOnly ? '<span class="stamp hollow">IN-ONLY</span>' : '')
      + '<span class="stamp hollow">' + esc(receiptStatus(r)) + '</span>'
      + (tier !== '—' ? '<span class="stamp hollow">' + esc(tier) + '</span>' : '')
      + (r.prune ? '<span class="stamp hollow">' + esc(pruneText(r)) + '</span>' : '')
      + (r.effort ? '<span class="stamp hollow">EFFORT ' + esc(r.effort) + '</span>' : '')
      + (ht ? '<span class="stamp hollow">' + esc(ht.group_id || '—') + '</span>' : '')
      + (ht && ht.locked ? '<span class="stamp">LOCKED</span>' : '')
      + (ht && ht.tier ? '<span class="stamp hollow">TIER ' + esc(String(ht.tier).toUpperCase()) + '</span>' : '')
      + (ht ? '<button class="btn small" type="button" data-trace-id="' + esc(ht.trace_id) + '">洋葱轨迹</button>' : '')
      + '</div>'
      + '<div class="fx-receipt-cost"><div class="fx-rail-title"><b>PRICING / 计价依据</b><span>' + (r._cost && r._cost.official ? 'OFFICIAL' : 'EST') + '</span></div>'
      + '<div class="fx-receipt-kv"><span>渠道</span><b>' + esc(channelOf(r)) + '</b>'
      + (tier !== '—' ? '<span>时段</span><b>' + esc(tier) + '</b>' : '')
      + '<span>缓存占比</span><b>' + esc(cachePctText(r)) + '</b>'
      + (info.inputOnly ? '<span>计费口径</span><b>IN-ONLY · input_only</b><span>输出 / OUT</span><b>0 · 不计费</b>' : '')
      + (cost.billable === true || cost.billable === false ? '<span>计费状态</span><b>' + (cost.billable ? '计入结算' : '不计入') + '</b>' : '') + '</div>'
      + '<div class="fx-receipt-note">金额按记账时的渠道价目结算，事后改价不影响本笔。</div></div>'
      + '<div class="fx-receipt-cost"><div class="fx-rail-title"><b>USAGE / 用量</b><span>TOKENS</span></div>'
      + '<div class="fx-receipt-usage">输入 ' + esc(F.tok(r.prompt_tokens)) + ' · 缓存 ' + esc(F.tok(r.cached_tokens)) + ' · 输出 ' + esc(F.tok(r.completion_tokens)) + ' → 合计 ' + esc(F.tok(r.total_tokens)) + '</div></div>'
      + '<div class="fx-receipt-cost"><div class="fx-rail-title"><b>本笔费用 / COST</b><span>CNY</span></div>'
      + '<div class="fx-receipt-total">' + esc(F.cny(total, 4)) + '</div>'
      + '<div class="fx-receipt-note">USD ' + esc(F.usd(cost.usd || r.usd)) + ' · CREDIT ' + F.num(r.credit, 2)
      + (savedCny(cost.saved_cny) ? ' · 缓存省 ' + esc(savedCny(cost.saved_cny)) : '')
      + (r.prune ? ' · ' + esc(pruneText(r)) : '') + '</div></div>'
      + '<details class="fx-receipt-more"><summary>展开明细</summary>'
      + '<div class="fx-receipt-kv"><span>时间 / TIME</span><b>' + esc(F.hm(r.at)) + '</b>'
      + '<span>账号 / ACCOUNT</span><b>#' + esc(r.account_id || '0') + '</b>'
      + '<span>KEY</span><b>' + esc(r.key_name || '—') + '</b>'
      + '<span>客户端 / CLIENT</span><b>' + esc(r.client || '—') + '</b>'
      + '<span>消息数 / MSGS</span><b>' + F.int(r.msgs) + '</b>'
      + '<span>总耗时 / ELAPSED</span><b>' + esc(F.dur(r.elapsed_ms)) + '</b>'
      + '<span>首 token / TTFT</span><b>' + esc(F.dur(r.ttft_ms)) + '</b>'
      + '<span>上游 / UPSTREAM</span><b>' + esc(r.upstream || r.channel || '—') + '</b>'
      + '<span>路由 / ROUTE</span><b>' + esc(r.route || '—') + '</b></div>'
      + '</details>'
      + (r.error ? '<div class="fx-receipt-note" style="border-top:1px solid var(--rule-strong);padding-top:5px"><b>ERROR</b> ' + esc(r.error) + '</div>' : '')
      + '</div>';
  }

  function percentile(sorted, p) {
    if (!sorted.length) return null;
    var idx = Math.min(sorted.length - 1, Math.max(0, Math.round((p / 100) * (sorted.length - 1))));
    return sorted[idx];
  }

  function renderRail() {
    var host = HUB.$('#streamRail');
    if (!host) return;
    var list = filtered();
    var r = list[state.selected] || list[0] || null;
    var meta = windowMeta();
    var all = windowRows();
    var sum = all.reduce(function (a, x) { return a + costOf(x); }, 0);
    var sampleSpan = rows().length > 1 ? Math.round((Number(rows()[0].at) - Number(rows()[rows().length - 1].at)) / 60) : null;
    var ttfts = all.map(function (x) { return Number(x.ttft_ms); }).filter(function (n) { return n > 0; }).sort(function (a, b) { return a - b; });
    var avgTtft = ttfts.length ? ttfts.reduce(function (a, b) { return a + b; }, 0) / ttfts.length : null;
    var p50 = percentile(ttfts, 50), p95 = percentile(ttfts, 95);
    var errs = all.filter(function (x) { return statusOf(x) === 'error'; }).length;
    var byCh = {};
    all.forEach(function (x) { var k = HUB.shortChannel(x.channel); byCh[k] = (byCh[k] || 0) + 1; });
    var maxCh = Math.max.apply(null, Object.keys(byCh).map(function (k) { return byCh[k]; }).concat([1]));
    host.innerHTML = receiptHtml(r)
      + '<div class="fx-rail-block"><div class="fx-rail-title"><b>WINDOW / 统计窗口</b><span>' + esc(meta.label) + ' · ' + meta.rows + ' 笔</span></div>'
      + '<div class="fx-window-grid"><div><span>SUM COST</span><b>' + esc(meta.rows ? F.cny(sum, 4) : '—') + '</b></div>'
      + '<div><span>AVG TTFT</span><b>' + esc(avgTtft === null ? '—' : F.dur(avgTtft)) + '</b></div>'
      + '<div><span>P50 TTFT</span><b>' + esc(p50 === null ? '—' : F.dur(p50)) + '</b></div>'
      + '<div><span>P95 TTFT</span><b>' + esc(p95 === null ? '—' : F.dur(p95)) + '</b></div>'
      + '<div><span>ERRORS</span><b>' + esc(meta.rows ? String(errs) : '—') + '</b></div>'
      + '<div><span>SPAN</span><b>' + (meta.spanMin === null ? '—' : meta.spanMin + 'm') + '</b></div></div>'
      + '<div class="fx-receipt-note">' + (meta.empty
        ? '该窗口内没有流水；载入样本最近 ' + (sampleSpan === null ? '—' : sampleSpan + ' 分钟') + '。把 WIN 换成「本次载入样本」看已载入的那一段。'
        : (meta.covered
          ? (state.win ? '整窗都在载入样本内；改窗口只改统计范围，不改表格尾部。' : '当前统计的是本次载入的样本，不是时间窗；要看时间窗请在上方 WIN 选。')
          : '样本只覆盖 ' + (meta.spanMin || 0) + ' 分钟，窗口比样本长：这里的数是已载入部分的实测值，不往外套也不估。')) + '</div></div>'
      + '<div class="fx-rail-block"><div class="fx-rail-title"><b>CHANNEL MIX / 渠道构成</b><span>' + esc(meta.label) + '</span></div>'
      + (Object.keys(byCh).length ? Object.keys(byCh).map(function (k) {
        return '<div class="fx-mix-row"><span>' + esc(k) + '</span><i><b style="width:' + Math.round(byCh[k] / maxCh * 100) + '%"></b></i><em>' + byCh[k] + '</em></div>';
      }).join('') : '<div class="fx-receipt-note">窗口内无流水。</div>')
      + '</div>'
      + '<div class="fx-rail-block"><div class="fx-rail-title"><b>FIELD SOURCE / 字段来源</b><span>READ ONLY</span></div><div class="fx-receipt-note">ttft_ms · tier.txt · prune · route · _cost.cny 原样来自 /api/usage。</div></div>';
  }

  function render() {
    syncFilters();
    renderHead();
    renderTable();
    renderRail();
    renderDispatch();
    ensureTracePoll();
    loadTraces();
  }

  HUB.views.stream = render;

  document.addEventListener('change', function (e) {
    if (e.target.id === 'streamChannel') { state.channel = e.target.value; state.selected = 0; renderTable(); renderRail(); }
    if (e.target.id === 'streamModel') { state.model = e.target.value; state.selected = 0; renderTable(); renderRail(); }
    if (e.target.id === 'streamWindow') { state.win = Number(e.target.value) || 0; renderTable(); renderRail(); }
  });
  document.addEventListener('input', function (e) {
    if (e.target.id === 'streamSearch') { state.q = e.target.value; state.selected = 0; renderTable(); renderRail(); }
  });
  document.addEventListener('click', function (e) {
    var traceBtn = e.target.closest('[data-trace-id]');
    if (traceBtn) {
      e.preventDefault();
      openTrace(traceBtn.getAttribute('data-trace-id'));
      return;
    }
    if (e.target.closest('#streamDispatchAll')) { openTraceLedger(); return; }
    var statusBtn = e.target.closest('#streamStatus button');
    if (statusBtn) {
      state.status = statusBtn.getAttribute('data-status');
      HUB.$$('#streamStatus button').forEach(function (b) { b.classList.toggle('is-active', b === statusBtn); });
      state.selected = 0; renderTable(); renderRail(); return;
    }
    if (e.target.closest('#streamReset')) {
      state = { status: 'all', channel: 'all', model: 'all', q: '', selected: 0, win: state.win || 0 };
      HUB.$$('#streamStatus button').forEach(function (b) { b.classList.toggle('is-active', b.getAttribute('data-status') === 'all'); });
      var ch = HUB.$('#streamChannel'); if (ch) ch.value = 'all';
      var md = HUB.$('#streamModel'); if (md) md.value = 'all';
      var q = HUB.$('#streamSearch'); if (q) q.value = '';
      render(); return;
    }
    var row = e.target.closest('#streamRows .fx-stream-row');
    if (row) {
      state.selected = Number(row.getAttribute('data-idx')) || 0;
      renderTable(); renderRail(); return;
    }
    if (e.target.closest('#streamRail')) {
      /* 回执内不绑定写操作；保留只读提示。 */
    }
  });
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('#streamRows .fx-stream-row')) {
      e.preventDefault();
      e.target.click();
    }
  });
  document.addEventListener('DOMContentLoaded', render);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && HUB.currentView && HUB.currentView() === 'stream') loadTraces(true);
  });
  if (document.readyState !== 'loading') render();
})();
