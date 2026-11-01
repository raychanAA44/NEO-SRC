/* 03 模型目录 · readmodel/catalog + mtok_prices + dashboard traffic */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;
  var state = {
    density: 'cards', axis: 'model',
    channel: 'all', combo: 'all', realm: 'all', enabled: 'all', cap: 'all', effort: 'all', bill: 'all', q: '',
    sortKey: 'traffic', sortDir: -1
  };

  function data() { return HUB.data.state.data || {}; }
  function models() { return (data().catalog && data().catalog.models) || []; }
  function traffic() {
    var out = {};
    ((data().dashboard && data().dashboard.models) || []).forEach(function (m) { out[m.key] = m; });
    return out;
  }

  function groups() {
    /* 按模型 = 同名模型合并成一张铭牌；按渠道 = 同一渠道下行成一个机柜。
     * 旧版只切按钮高亮不重组数据，“按渠道”点了没任何变化。 */
    var byChannel = state.axis === 'channel';
    var map = new Map();
    models().forEach(function (m) {
      var key = byChannel ? (m.channel || '—') : m.id;
      var g = map.get(key);
      if (!g) {
        g = {
          id: key,
          name: byChannel ? HUB.channelLabel(m.channel, data()) : (m.name || m.id),
          vendor: byChannel ? 'CHANNEL' : (m.vendor || '—'),
          byChannel: byChannel, modelIds: [], models: [],
          channels: [], contexts: [], efforts: [], enabled: 0, free: 0,
          reasoning: false, vision: false, tools: false, traffic: 0,
          decision: false, inputOnly: false, endpoints: []
        };
        map.set(key, g);
      }
      if (g.modelIds.indexOf(m.id) < 0) g.modelIds.push(m.id);
      g.models.push(m);
      if (g.channels.indexOf(m.channel) < 0) g.channels.push(m.channel);
      if (m.context) g.contexts.push(Number(m.context));
      (m.efforts || []).forEach(function (e) { if (g.efforts.indexOf(e) < 0) g.efforts.push(e); });
      if (m.enabled) g.enabled += 1;
      if (m.free) g.free += 1;
      if (m.reasoning) g.reasoning = true;
      if (m.vision) g.vision = true;
      if (m.tools) g.tools = true;
      if (m.decision) g.decision = true;
      if (m.billing_basis === 'input_only') g.inputOnly = true;
      if (m.endpoint && g.endpoints.indexOf(m.endpoint) < 0) g.endpoints.push(m.endpoint);
    });
    var tr = traffic();
    return Array.from(map.values()).map(function (g) {
      g.context = Math.max.apply(null, g.contexts.concat([0]));
      g.traffic = g.models.reduce(function (a, m) {
        var hit = tr[m.id];
        return a + (hit ? Number(hit.requests) || 0 : 0);
      }, 0);
      g.priceMin = Math.min.apply(null, g.models.map(function (m) {
        var v = HUB.priceValue(m, data());
        return v === null ? Infinity : v;
      }).concat([Infinity]));
      g.rateLabels = g.models.map(function (m) { return HUB.priceLabel(m, data()); });
      return g;
    });
  }

  /* 渠道×模型组合：同一渠道里的同名模型各算一行，选组合时两段都必须命中。 */
  function comboOf(m) { return String(m.channel || '') + '|||' + String(m.id || ''); }

  function matches(m) {
    if (state.channel !== 'all' && m.channel !== state.channel) return false;
    if (state.combo !== 'all') {
      var parts = String(state.combo).split('|||');
      if (m.channel !== parts[0] || m.id !== parts[1]) return false;
    }
    if (state.realm !== 'all' && HUB.realmOf(m.channel) !== state.realm) return false;
    if (state.enabled === 'on' && !m.enabled) return false;
    if (state.enabled === 'off' && m.enabled) return false;
    if (state.cap === 'reasoning' && !m.reasoning) return false;
    if (state.cap === 'vision' && !m.vision) return false;
    if (state.cap === 'tools' && !m.tools) return false;
    if (state.cap === 'long' && !(Number(m.context) >= 200000)) return false;
    if (state.effort !== 'all' && (m.efforts || []).indexOf(state.effort) < 0) return false;
    var price = HUB.priceValue(m, data());
    var free = m.free || price === 0 || String(m.rate) === 'x0' || String(m.rate) === 'x0.00';
    if (state.bill === 'free' && !free) return false;
    if (state.bill === 'metered' && free) return false;
    var q = state.q.trim().toLowerCase();
    if (q) {
      var hay = [m.id, m.name, m.vendor, m.channel, HUB.channelLabel(m.channel, data())].join(' ').toLowerCase();
      if (hay.indexOf(q) < 0) return false;
    }
    return true;
  }

  function filteredGroups() {
    return groups().map(function (g) {
      var ms = g.models.filter(matches);
      if (!ms.length) return null;
      var copy = Object.assign({}, g, { models: ms, channels: Array.from(new Set(ms.map(function (m) { return m.channel; }))) });
      copy.enabled = ms.filter(function (m) { return m.enabled; }).length;
      copy.free = ms.filter(function (m) { return m.free; }).length;
      copy.context = Math.max.apply(null, ms.map(function (m) { return Number(m.context) || 0; }).concat([0]));
      copy.priceMin = Math.min.apply(null, ms.map(function (m) {
        var v = HUB.priceValue(m, data()); return v === null ? Infinity : v;
      }).concat([Infinity]));
      copy.traffic = g.traffic;
      copy.decision = ms.some(function (m) { return !!m.decision; });
      copy.inputOnly = ms.some(function (m) { return m.billing_basis === 'input_only'; });
      copy.endpoints = Array.from(new Set(ms.map(function (m) { return m.endpoint; }).filter(Boolean)));
      return copy;
    }).filter(Boolean);
  }

  function priceRange(g) {
    var labels = Array.from(new Set(g.models.map(function (m) { return HUB.priceLabel(m, data()); }).filter(function (x) { return x && x !== '—'; })));
    if (!labels.length) return '—';
    if (labels.length === 1) return labels[0];
    if (labels.every(function (x) { return String(x).indexOf('¥') === 0; })) {
      var nums = labels.map(function (x) { return parseFloat(String(x).replace(/[^\d.]/g, '')); }).filter(isFinite);
      if (nums.length) return '¥' + Math.min.apply(null, nums).toFixed(2) + '–' + Math.max.apply(null, nums).toFixed(2) + '/M';
    }
    if (labels.every(function (x) { return String(x).indexOf('x') === 0; })) {
      var xs = labels.map(function (x) { return parseFloat(String(x).slice(1)); }).filter(isFinite);
      if (xs.length) return 'x' + Math.min.apply(null, xs).toFixed(2) + '–' + Math.max.apply(null, xs).toFixed(2);
    }
    return 'MIXED';
  }

  function capTags(m) {
    var t = [];
    if (m.reasoning) t.push('REASON');
    if (m.vision) t.push('VISION');
    if (m.tools) t.push('TOOLS');
    if (Number(m.context) >= 200000) t.push('LONG');
    if (m.decision) t.push('DECISION');
    if (m.billing_basis === 'input_only' || m.inputOnly) t.push('IN-ONLY');
    return t;
  }

  function toggleArm(channel, id, enabled, name) {
    HUB.job({
      title: (enabled ? '启用模型 / ' : '停用模型 / ') + name,
      endpoint: '/api/models/toggle',
      payload: { channel: channel, id: id, enabled: enabled },
      /* 模型开关已本地化：写 control.json，快照档也能改。 */
      local: true,
      timeout: 60000,
      channels: [channel],
      after: HUB.closeWO
    });
  }
  /* /api/models/bench 只覆盖内建渠道；custom:* 返回 total=0，不能落 PASSED。 */
  function benchSupported(channel) {
    return String(channel || '').indexOf('custom:') !== 0;
  }
  function benchUnsupported(channel, name) {
    HUB.ticket({
      title: '测速 / 不适用 · ' + name,
      note: 'custom:* 无测速分支；未发请求。',
      status: 'SKIPPED',
      tone: 'skip',
      channels: Array.isArray(channel) ? channel : [channel],
      ttl: 6500
    });
    HUB.toast('自定义上游不支持测速 / CUSTOM BENCH N/A');
  }
  function benchArm(channel, id, name) {
    if (!benchSupported(channel)) { benchUnsupported(channel, name); return; }
    HUB.job({
      title: '测速撞针 / ' + name,
      endpoint: '/api/models/bench',
      payload: { channel: channel, ids: [id], only_enabled: true, fresh: true, timeout: 45 },
      timeout: 120000,
      channels: [channel],
      after: HUB.closeWO
    });
  }

  /* ── 渠道价目 / PRICE BOOK ─────────────────────────────────────────
   * 03 只展示当前目录里的 open-design 模型；旧渠道价目已迁入 pricing.json，
   * 但没有目录/上游，不会出现在卡片里。这里按 channel 分行展示全表，
   * 并把 legacy_migration.skipped_rows 的缺口直接摆出来，避免“表里有价”被误读。
   */
  function priceRuleLabel(block) {
    var rule = block && block.peak_rule;
    if (!rule) return 'FLAT / 无峰谷';
    if (rule.kind === 'utc_weekday_window') {
      return 'PEAK ' + rule.start_hour + ':00–' + rule.end_hour + ':00 UTC';
    }
    if (rule.kind === 'utc_weekday_windows') {
      return 'PEAK ' + (rule.windows || []).map(function (w) {
        return w[0] + ':00–' + w[1] + ':00';
      }).join(' / ') + ' UTC';
    }
    return 'FLAT / 无峰谷';
  }

  function priceBookBody(payload) {
    var rows = (payload && payload.rows) || [];
    var doc = (payload && payload.doc) || {};
    var summary = (payload && payload.summary) || {};
    var blocks = doc.upstreams || {};
    var live = new Set((((data().custom || {}).items) || []).map(function (u) { return u.name; }));
    var skipped = ((doc._meta || {}).legacy_migration || {}).skipped_rows || {};
    var groups = {};
    rows.forEach(function (row) {
      var ch = row.channel || '—';
      (groups[ch] = groups[ch] || []).push(row);
    });
    var channels = summary.channels && summary.channels.length ? summary.channels : Object.keys(groups);
    var skippedCount = Object.keys(skipped).reduce(function (a, key) {
      return a + (Array.isArray(skipped[key]) ? skipped[key].length : 0);
    }, 0);

    var head = '<div class="fx-wo-block"><div class="fx-wo-label"><span>PRICE BOOK / 渠道价目</span><b>'
      + F.int(summary.models || rows.length) + ' ROWS · ' + channels.length + ' CHANNELS</b></div>'
      + '<div class="fx-wo-kv"><span>单位 / UNIT</span><b>' + esc(summary.unit || 'USD_per_mtok') + '</b>'
      + '<span>汇率 / USD-CNY</span><b>' + esc(summary.usd_cny || '—') + '</b>'
      + '<span>缺口 / SKIPPED</span><b>' + F.int(skippedCount) + '</b>'
      + '<span>降级 / FALLBACK</span><b>' + F.int(summary.fallback_rows || 0) + ' 行</b>'
      + '<span>数据源 / SOURCE</span><b>pricing.json</b></div>'
      + '<div class="fx-wo-text">按 channel + model 分行；同名模型不合并。LIVE 为当前 /api/custom 已配置渠道，PRICE ONLY 为待接入价目。CACHE* 为 cache=in 降级；OUT† 为输入计费、不计输出。</div></div>';

    var fallback = rows.filter(function (row) { return row.cache_source || row.out_source; })
      .sort(function (a, b) { return String(a.channel + '/' + a.model).localeCompare(String(b.channel + '/' + b.model)); });
    var fallbackBlock = fallback.length
      ? '<div class="fx-wo-block"><div class="fx-wo-label"><span>FALLBACK / 降级行</span><b>' + F.int(fallback.length) + ' 行</b></div>'
        + '<table class="fx-wo-table"><thead><tr><th>CHANNEL / MODEL</th><th>CACHE</th><th>OUT</th><th>MARK</th></tr></thead><tbody>'
        + fallback.map(function (row) {
          var mark = [];
          if (row.cache_source) mark.push('CACHE=IN');
          if (row.out_source) mark.push('OUT=0');
          return '<tr><td title="' + esc(row.cache_note || row.out_note || '') + '">' + esc(HUB.shortChannel(row.channel) + ' / ' + row.model)
            + '</td><td>' + esc(F.usd(row.cache)) + '</td><td>' + esc(F.usd(row.out))
            + '</td><td>' + esc(mark.join(' · ')) + '</td></tr>';
        }).join('') + '</tbody></table>'
        + '<div class="fx-wo-text">CACHE=IN：官方未公布缓存价，按输入价计；OUT=0：官方按输入 token 计费，不计输出。</div></div>'
      : '';

    var body = channels.map(function (ch) {
      var block = blocks[ch] || {};
      var list = (groups[ch] || []).slice().sort(function (a, b) {
        return String(a.model).localeCompare(String(b.model));
      });
      var visible = list.slice(0, 10);
      var gap = list.length - visible.length;
      var isLive = live.has(ch);
      var mode = block.billing_kind === 'subscription' ? 'SUBSCRIPTION' : 'USAGE';
      /* 价目能不能追溯：多少行带出处、最近哪天核验过。手填价一目了然。 */
      var sourced = list.filter(function (r) { return r.source_url; }).length;
      var stamp = list.map(function (r) { return r.checked_at || ''; }).filter(Boolean).sort().pop() || '—';
      return '<div class="fx-wo-block"><div class="fx-wo-label"><span>' + esc(ch) + ' / '
        + esc(HUB.shortChannel(ch)) + '</span><b>' + F.int(list.length) + ' 行</b></div>'
        + '<div class="fx-wo-kv"><span>状态 / STATE</span><b>' + (isLive ? 'LIVE' : 'PRICE ONLY') + '</b>'
        + '<span>计费 / BILLING</span><b>' + mode + '</b>'
        + '<span>峰谷 / PEAK</span><b>' + esc(priceRuleLabel(block)) + '</b></div>'
        + '<div class="fx-wo-text">出处 ' + F.int(sourced) + ' / ' + F.int(list.length)
        + ' 行带来源 · 最近核验 ' + esc(stamp) + '</div>'
        + '<table class="fx-wo-table"><thead><tr><th>MODEL</th><th>IN</th><th>CACHE*</th><th>OUT†</th><th>¥ IN</th></tr></thead><tbody>'
        + visible.map(function (row) {
          return '<tr><td>' + esc(row.model) + '</td><td>' + esc(F.usd(row.in))
            + '</td><td title="' + esc(row.cache_note || '') + '">' + esc(F.usd(row.cache))
            + (row.cache_source ? '<em class="fx-row-tag">IN</em>' : '') + '</td>'
            + '<td title="' + esc(row.out_note || '') + '">' + esc(F.usd(row.out))
            + (row.out_source ? '<em class="fx-row-tag">IN-ONLY</em>' : '') + '</td>'
            + '<td>' + esc(F.cny(row.cny_in)) + '</td></tr>';
        }).join('') + '</tbody></table>'
        + (gap ? '<div class="fx-wo-text">另有 ' + F.int(gap) + ' 行未展开；完整表见 GET /api/pricing。</div>' : '')
        + '</div>';
    }).join('');

    var gaps = Object.keys(skipped).filter(function (ch) { return Array.isArray(skipped[ch]); }).map(function (ch) {
      return '<div class="fx-wo-block"><div class="fx-wo-label"><span>SKIPPED / ' + esc(ch) + '</span><b>'
        + skipped[ch].length + ' 行</b></div><div class="fx-wo-table"><table><thead><tr><th>MODEL</th><th>MISSING</th><th>已有</th></tr></thead><tbody>'
        + skipped[ch].map(function (item) {
          var have = Object.keys(item.available || {}).map(function (k) { return k + '=' + item.available[k]; }).join(' · ') || '—';
          return '<tr><td>' + esc(item.model) + '</td><td>' + esc((item.missing || []).join(' / ')) + '</td><td>' + esc(have) + '</td></tr>';
        }).join('') + '</tbody></table></div>'
        + '<div class="fx-wo-text">旧仓缺档，不补 0；补价后再按契约录入。</div></div>';
    }).join('');

    return head + fallbackBlock + body + (gaps || '<div class="fx-wo-block"><div class="fx-wo-label"><span>SKIPPED / 缺口</span><b>0 行</b></div><div class="fx-wo-text">五渠道三档价齐全；三行降级见上方 FALLBACK。</div></div>');
  }

  /* 价目的唯一正当来源：登录态官方账单 unitPrices。取不到登录态就如实说，
     只有明确点「改用公开价目端点」才退回目录价。 */
  function syncPriceBook(mode) {
    var out = HUB.$('#priceSyncOut');
    if (out) out.textContent = mode === 'public'
      ? '拉公开价目端点（目录价）…'
      : '用登录态读官方账单 unitPrices…';
    fetch('/api/pricing/sync', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify({ channel: 'open-design', mode: mode || 'account', apply: true })
    }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      var b = res.body || {};
      if (res.status !== 200) throw new Error((b.error && b.error.message) || ('HTTP ' + res.status));
      if (!b.ok) {
        if (out) out.innerHTML = '<b>' + esc(b.error || '取价失败') + '</b>'
          + (b.hint ? '<br>' + esc(b.hint) : '')
          + (b.code === 'no_session' || b.code === 'auth_error'
            ? '<br><button class="btn small" type="button" data-price-public>改用公开价目端点</button>' : '');
        return;
      }
      var ap = b.applied || {};
      HUB.toast('价目已同步 / ' + F.int(ap.written));
      openPriceBook();
      var again = HUB.$('#priceSyncOut');
      if (again) again.innerHTML = '已同步 <b>' + esc(b.channel) + '</b> · ' + F.int(ap.written)
        + ' 行 · ' + (b.source === 'account' ? '登录态官方账单' : '公开价目端点')
        + ' · <span title="' + esc(b.source_url || '') + '">' + esc(String(b.source_url || '').slice(0, 48)) + '</span>';
    }).catch(function (err) {
      if (out) out.textContent = '同步失败 · ' + String((err && err.message) || err).slice(0, 160);
    });
  }

  function openPriceBook() {
    HUB.openWO({
      kicker: 'PRICE BOOK / 渠道价目',
      title: '五渠道价目与降级标记',
      meta: 'SOURCE / api/pricing · 登录态同步',
      body: '<div class="fx-wo-block"><div class="fx-wo-text">读取价目表…</div></div>',
      actions: [
        { label: '⇅ 登录态同步价目', filled: true, close: false, onClick: function () { syncPriceBook('account'); } },
        { label: '收起', close: true }
      ]
    });
    fetch('/api/pricing', { credentials: 'same-origin', cache: 'no-store', headers: { 'accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (payload) {
        var host = HUB.$('#woBody');
        if (host) {
          host.innerHTML = priceBookBody(payload)
            + '<div class="fx-wo-block"><div class="fx-wo-label"><span>SYNC / 价目同步</span><b>ACCOUNT FIRST</b></div>'
            + '<div class="fx-wo-text" id="priceSyncOut">默认走登录态官方账单 unitPrices；没有登录态会如实报错，不会静默拿目录价顶替。</div></div>';
        }
      })
      .catch(function (err) {
        var host = HUB.$('#woBody');
        if (host) host.innerHTML = '<div class="fx-wo-block"><div class="fx-wo-warning">价目表读取失败 / '
          + esc(String((err && err.message) || err).slice(0, 120)) + '</div></div>';
      });
  }

  function stepsForToggle(list, enabled) {
    var map = {};
    list.forEach(function (m) { (map[m.channel] = map[m.channel] || []).push(m.id); });
    return Object.keys(map).map(function (ch) {
      return { endpoint: '/api/models/toggle_bulk', payload: { channel: ch, ids: map[ch], enabled: enabled }, label: HUB.shortChannel(ch) };
    });
  }

  function runBatch(kind) {
    var list = [];
    if (kind === 'vendor-off') {
      var vendor = (HUB.$('#batchVendor') || {}).value;
      list = models().filter(function (m) { return m.vendor === vendor; });
    } else if (kind === 'effort-on') {
      var effort = (HUB.$('#batchEffort') || {}).value;
      list = models().filter(function (m) { return (m.efforts || []).indexOf(effort) >= 0; });
    } else {
      filteredGroups().forEach(function (g) { g.models.forEach(function (m) { list.push(m); }); });
    }
    if (!list.length) { HUB.toast('当前范围没有模型 / EMPTY'); return; }
    if (kind === 'bench-filter') {
      var benchable = list.filter(function (m) { return m.enabled && benchSupported(m.channel); });
      var customSkipped = list.filter(function (m) { return m.enabled && !benchSupported(m.channel); }).length;
      if (!benchable.length) { HUB.toast('当前筛选没有可测速的内建模型 / CUSTOM N/A'); return; }
      var capped = benchable.slice(0, 24);
      var bmap = {};
      capped.forEach(function (m) { (bmap[m.channel] = bmap[m.channel] || []).push(m.id); });
      var benchSteps = Object.keys(bmap).map(function (ch) {
        return { endpoint: '/api/models/bench', payload: { channel: ch, ids: bmap[ch], only_enabled: true, fresh: true, timeout: 60 }, label: HUB.shortChannel(ch) };
      });
      HUB.job({
        title: '批量测速 / 当前筛选',
        steps: benchSteps,
        channels: Object.keys(bmap),
        confirm: '对当前筛选里 ' + capped.length + ' 个已启用模型打最小请求测速？' + (benchable.length > capped.length ? '（超过 24 个，只取前 24 个）' : '') + (customSkipped ? '（custom:* 渠道 ' + customSkipped + ' 个不参与）' : '') + '近 10 分钟已测过的会复用缓存。',
        timeout: 240000
      });
    } else {
      var on = kind === 'filter-on' || kind === 'effort-on';
      var steps = stepsForToggle(list, on);
      HUB.job({
        title: (on ? '批量启用 / ' : '批量停用 / ') + list.length + ' 模型',
        steps: steps,
        local: true,
        channels: steps.map(function (s) { return s.payload.channel; }),
        confirm: (on ? '启用' : '停用') + ' ' + list.length + ' 个模型（' + steps.length + ' 个渠道组）？会立即改变网关调度。',
        timeout: 120000
      });
    }
    closeBatch();
  }

  function closeBatch() {
    var menu = HUB.$('#modelBatchMenu');
    var btn = HUB.$('#modelBatchBtn');
    if (menu) menu.hidden = true;
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  function cardHtml(g) {
    var enabled = g.enabled > 0;
    var caps = capTags(g);
    var tr = traffic()[g.id];
    var px = HUB.priceParts(g);
    /* 价格写具体三档，不写 ¥2.016–8.064 这种区间：区间对使用者没有信息量。 */
    var priceLine = !px ? '—'
      : (px.free ? 'FREE'
        : 'IN ' + HUB.money3(px.in) + ' · 缓存 ' + HUB.money3(px.cache)
          + ' · OUT ' + HUB.money3(px.out));
    var priceStamp = px && px.checkedAt ? ('价格同步 ' + px.checkedAt) : '价格同步 —';
    return '<article class="fx-model-card ' + (enabled ? '' : 'is-off') + '" data-model="' + esc(g.id) + '">'
      + '<div class="fx-card-top"><span class="fx-plate-no">REG / ' + esc(g.vendor) + ' · ' + g.channels.length + ' LINK</span><div class="fx-card-stamps">'
      + (g.decision ? '<span class="stamp">DECISION</span>' : '')
      + (g.inputOnly ? '<span class="stamp hollow">IN-ONLY</span>' : '')
      + (enabled ? '<span class="stamp">ENABLED</span>' : '<span class="stamp hollow">OFF</span>')
      + (g.free ? '<span class="stamp hollow">FREE</span>' : '')
      + (tr && tr.requests ? '<span class="stamp hollow">' + F.int(tr.requests) + ' REQ</span>' : '')
      + '</div></div>'
      + '<h3 title="' + esc(g.name) + '">' + esc(g.name) + '</h3>'
      + '<div class="fx-model-meta">' + esc(g.vendor) + ' · ' + F.tok(g.context) + ' CTX · '
      + esc(g.byChannel ? (g.models.length + ' 款在架') : g.channels.map(function (c) { return HUB.shortChannel(c); }).join(' / ')) + '</div>'
      + (g.byChannel ? '<div class="fx-model-meta">' + esc(g.modelIds.slice(0, 4).join(' / '))
        + (g.modelIds.length > 4 ? ' +' + (g.modelIds.length - 4) : '') + '</div>' : '')
      + '<div class="fx-model-param is-price"><span>计费率 / ¥ 每百万 token</span><b>' + esc(priceLine) + '</b></div>'
      + '<div class="fx-model-meta">' + esc(priceStamp) + '</div>'
      + (g.decision
        ? '<div class="fx-call-plate" title="' + esc('决策端点 ' + (g.endpoints.join(' / ') || '/v1/systemone') + '；官方按输入 token 计费，output_tokens=0') + '">'
          + '<div><span>ENDPOINT</span><b>' + esc(g.endpoints.join(' / ') || '/v1/systemone') + '</b></div>'
          + '<div><span>计费口径</span><b>IN-ONLY · output_tokens=0</b></div></div>'
        : '')
      + (g.byChannel ? '' : '<div class="fx-model-channels">' + g.models.slice(0, 4).map(function (m) {
        return '<button type="button" class="fx-ch-btn ' + (m.enabled ? '' : 'is-off') + '" data-act="toggle" data-channel="' + esc(m.channel) + '" data-id="' + esc(m.id) + '" data-enabled="' + (m.enabled ? '0' : '1') + '"'
          + ' title="' + esc(HUB.shortChannel(m.channel) + (m.enabled ? ' · 当前启用，点击停用' : ' · 当前停用，点击启用')) + '"'
          + ' aria-label="' + esc(HUB.shortChannel(m.channel) + (m.enabled ? '，当前启用，点击停用' : '，当前停用，点击启用')) + '">'
          + '<i aria-hidden="true"></i>' + esc(HUB.shortChannel(m.channel))
          + '<em class="fx-ch-state">' + (m.enabled ? '启用' : '停用') + '</em></button>';
      }).join('') + '</div>')
      + '<div class="fx-card-foot"><span>' + (caps.length ? esc(caps.slice(0, 3).join(' · ')) : 'BASE') + '</span><button class="btn small" type="button" data-act="detail" data-model="' + esc(g.id) + '">详情</button></div>'
      + '</article>';
  }

  function sortGroups(list) {
    var key = state.sortKey, dir = state.sortDir;
    return list.slice().sort(function (a, b) {
      var av, bv;
      if (key === 'name') { av = a.name; bv = b.name; }
      else if (key === 'channel') { av = a.channels.length; bv = b.channels.length; }
      else if (key === 'price') { av = a.priceMin; bv = b.priceMin; }
      else if (key === 'context') { av = a.context; bv = b.context; }
      else if (key === 'enabled') { av = a.enabled; bv = b.enabled; }
      else { av = a.traffic; bv = b.traffic; }
      if (av === bv) return String(a.name).localeCompare(String(b.name));
      return (av > bv ? 1 : -1) * dir;
    });
  }

  function matrixRows(gs) {
    if (state.axis === 'model') {
      return gs.map(function (g) { return { type: 'group', g: g }; });
    }
    var out = [];
    gs.forEach(function (g) {
      g.models.forEach(function (m) { out.push({ type: 'model', g: g, m: m }); });
    });
    return out;
  }

  function matrixSort(rows) {
    var key = state.sortKey, dir = state.sortDir;
    return rows.slice().sort(function (a, b) {
      var av, bv;
      if (key === 'channel') { av = a.m ? a.m.channel : a.g.channels[0]; bv = b.m ? b.m.channel : b.g.channels[0]; }
      else if (key === 'price') { av = a.m ? HUB.priceValue(a.m, data()) : a.g.priceMin; bv = b.m ? HUB.priceValue(b.m, data()) : b.g.priceMin; }
      else if (key === 'context') { av = a.m ? Number(a.m.context) : a.g.context; bv = b.m ? Number(b.m.context) : b.g.context; }
      else if (key === 'enabled') { av = a.m ? !!a.m.enabled : a.g.enabled; bv = b.m ? !!b.m.enabled : b.g.enabled; }
      else if (key === 'channel') { av = a.m ? a.m.channel : a.g.channels.join(','); bv = b.m ? b.m.channel : b.g.channels.join(','); }
      else { av = a.m ? a.m.name : a.g.name; bv = b.m ? b.m.name : b.g.name; }
      if (av === bv) return 0;
      return (av > bv ? 1 : -1) * dir;
    });
  }

  function tableHtml(gs) {
    var rows = matrixSort(matrixRows(gs));
    var head = '<tr>'
      + '<th class="col-ch" data-sort="channel">渠道 ' + sortMark('channel') + '</th>'
      + '<th class="col-model" data-sort="name">模型 ' + sortMark('name') + '</th>'
      + '<th class="col-rate" data-sort="price">价格 ' + sortMark('price') + '</th>'
      + '<th class="col-ctx" data-sort="context">CTX ' + sortMark('context') + '</th>'
      + '<th class="col-cap">能力</th>'
      + '<th class="col-state" data-sort="enabled">状态 ' + sortMark('enabled') + '</th>'
      + '<th class="col-ops">操作</th></tr>';
    var body = rows.map(function (r) {
      if (r.type === 'group') {
        var g = r.g;
        var groupBenchable = g.models.some(function (m) { return benchSupported(m.channel); });
        return '<tr>'
          + '<td>' + esc(g.channels.map(function (c) { return HUB.shortChannel(c); }).join(' / ')) + '</td>'
          + '<td><span class="m-name">' + esc(g.name) + '</span><span class="m-sub">' + esc(g.vendor) + ' · ' + g.models.length + ' LINK' + (g.endpoints.length ? ' · ' + g.endpoints.join(' / ') : '') + '</span></td>'
          + '<td>' + esc(priceRange(g)) + '</td>'
          + '<td>' + esc(F.tok(g.context)) + '</td>'
          + '<td title="' + esc(capTags(g).join(' · ') || '—') + '">' + esc(capTags(g).join(' · ') || '—') + '</td>'
          + '<td><span class="m-state ' + (g.enabled ? '' : 'off') + '"></span>' + g.enabled + ' / ' + g.models.length + '</td>'
          + '<td class="m-right"><button class="btn small" type="button" data-act="detail" data-model="' + esc(g.id) + '">详情</button>' + (groupBenchable ? ' <button class="btn small" type="button" data-act="bench-group" data-model="' + esc(g.id) + '" aria-label="' + esc('测速：' + g.name + ' 全渠道') + '">测速</button>' : '') + '</td>'
          + '</tr>';
      }
      var m = r.m;
      return '<tr>'
        + '<td>' + esc(HUB.channelLabel(m.channel, data())) + '</td>'
        + '<td><span class="m-name">' + esc(m.name || m.id) + '</span><span class="m-sub">' + esc(m.id + (m.endpoint ? ' · ' + m.endpoint : '')) + '</span></td>'
        + '<td>' + esc(HUB.priceLabel(m, data())) + '</td>'
        + '<td>' + esc(F.tok(m.context)) + '</td>'
        + '<td title="' + esc(capTags(m).join(' · ') || '—') + '">' + esc(capTags(m).join(' · ') || '—') + '</td>'
        + '<td><span class="m-state ' + (m.enabled ? '' : 'off') + '"></span>' + (m.enabled ? 'ON' : 'OFF') + '</td>'          + '<td class="m-right"><button class="btn small" type="button" data-act="toggle" data-channel="' + esc(m.channel) + '" data-id="' + esc(m.id) + '" data-enabled="' + (m.enabled ? '0' : '1') + '" aria-label="' + esc((m.name || m.id) + (m.enabled ? '，当前启用，点击停用' : '，当前停用，点击启用')) + '">' + (m.enabled ? '停用' : '启用') + '</button>' + (benchSupported(m.channel) ? ' <button class="btn small" type="button" data-act="bench" data-channel="' + esc(m.channel) + '" data-id="' + esc(m.id) + '">测速</button>' : '') + '</td>'
        + '</tr>';
    }).join('');
    return '<table class="fx-matrix"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>';
  }

  function sortMark(key) {
    if (state.sortKey !== key) return '';
    return state.sortDir > 0 ? '▲' : '▼';
  }

  function renderCards(gs) {
    var host = HUB.$('#modelView');
    if (!host) return;
    var sorted = gs.slice().sort(function (a, b) {
      if (a.enabled !== b.enabled) return b.enabled - a.enabled;
      if (a.traffic !== b.traffic) return b.traffic - a.traffic;
      return a.name.localeCompare(b.name);
    }).slice(0, 12);
    host.innerHTML = sorted.length ? '<div class="fx-card-grid">' + sorted.map(cardHtml).join('') + '</div>'
      : '<div class="fx-receipt-empty" style="padding:14px">当前筛选无模型。</div>';
  }

  function renderMatrix(gs) {
    var host = HUB.$('#modelView');
    if (!host) return;
    host.innerHTML = gs.length ? tableHtml(gs) : '<div class="fx-receipt-empty" style="padding:14px">当前筛选无模型。</div>';
  }

  function renderSpare() {
    var host = HUB.$('#spareBay');
    if (!host) return;
    var catalogIds = new Set(models().map(function (m) { return m.id; }));
    var items = ((data().custom && data().custom.items) || []).map(function (u) {
      var extra = (u.models || []).filter(function (id) { return !catalogIds.has(id); });
      return { name: u.name, extra: extra, total: (u.models || []).length, enabled: u.enabled };
    }).filter(function (u) { return u.extra.length; });
    var total = items.reduce(function (a, b) { return a + b.extra.length; }, 0);
    host.innerHTML = '<span>SPARE BAY / 待收录</span><b>' + items.length + ' 上游 · ' + F.int(total) + ' 模型未入目录</b><span class="spacer"></span><span>仅供查看</span><button class="btn small" type="button" id="spareOpen">查看待收录</button>';
    var btn = HUB.$('#spareOpen');
    if (btn) btn.addEventListener('click', function () {
      HUB.openWO({
        kicker: 'SPARE BAY / 待收录清单',
        title: '上游已有 · 目录未收',
        meta: 'SOURCE / api/custom + readmodel/catalog',
        body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>COUNT / 合计</span><b>' + F.int(total) + ' MODELS</b></div>'
          + '<div class="fx-wo-text">按 /api/custom 与目录清单求差；不自动入册。</div></div>'
          + items.map(function (u) {
            return '<div class="fx-wo-block"><div class="fx-wo-label"><span>' + esc(u.name) + '</span><b>' + u.extra.length + ' / ' + u.total + '</b></div>'
              + '<div class="fx-wo-table"><table><thead><tr><th>模型</th><th>状态</th></tr></thead><tbody>'
              + u.extra.slice(0, 10).map(function (id) { return '<tr><td>' + esc(id) + '</td><td>' + (u.enabled ? '上游启用' : '上游停用') + '</td></tr>'; }).join('')
              + '</tbody></table></div>'
              + (u.extra.length > 10 ? '<div class="fx-wo-text">另有 ' + (u.extra.length - 10) + ' 个未展开。</div>' : '')
              + '</div>';
          }).join('')
          + '<div class="fx-wo-warning">只读清单；入册需另行操作。</div>',
        actions: [{ label: '收起', close: true }]
      });
    });
  }

  function syncBatchSelects() {
    var all = models();
    function fill(sel, values, current) {
      if (!sel) return;
      var html = values.map(function (v) { return '<option value="' + esc(v) + '">' + esc(v) + '</option>'; }).join('');
      if (sel.innerHTML !== html) sel.innerHTML = html;
      if (current && values.indexOf(current) >= 0) sel.value = current;
    }
    var vendors = Array.from(new Set(all.map(function (m) { return m.vendor; }).filter(function (v) { return v && v !== '-' && v !== '—'; }))).sort();
    var efforts = Array.from(new Set(all.reduce(function (a, m) { return a.concat(m.efforts || []); }, []))).sort();
    fill(HUB.$('#batchVendor'), vendors, HUB.$('#batchVendor') && HUB.$('#batchVendor').value);
    fill(HUB.$('#batchEffort'), efforts, HUB.$('#batchEffort') && HUB.$('#batchEffort').value);
  }

  function render() {
    /* 目录要等读数回来才有内容：每次重画都刷新筛选下拉，
     * 否则首屏下拉永远只有「全部渠道×模型」一项。 */
    syncSelects();
    var gs = filteredGroups();
    var total = HUB.$('#modelTotal');
    if (total) total.textContent = F.int(data().catalog && data().catalog.total);
    var enabled = HUB.$('#modelEnabled');
    if (enabled) enabled.textContent = F.int(models().filter(function (m) { return m.enabled; }).length);
    var count = HUB.$('#modelCount');
    if (count) count.textContent = F.int(gs.reduce(function (a, g) { return a + g.models.length; }, 0));
    var countTotal = HUB.$('#modelCountTotal');
    if (countTotal) countTotal.textContent = F.int(data().catalog && data().catalog.total);
    var footSnap = HUB.$('#modelFootSnap');
    if (footSnap) footSnap.textContent = F.int(data().visibility && data().visibility.snapshot_models);
    syncBatchSelects();
    HUB.$$('#modelDensity button').forEach(function (b) { b.classList.toggle('is-active', b.getAttribute('data-density') === state.density); });
    HUB.$$('#modelAxis button').forEach(function (b) { b.classList.toggle('is-active', b.getAttribute('data-axis') === state.axis); });
    if (state.density === 'cards') renderCards(gs); else renderMatrix(gs);
    renderSpare();
  }

  function syncSelects() {
    var sel = HUB.$('#fChannel');
    if (!sel) return;
    var chs = Array.from(new Set(models().map(function (m) { return m.channel; })));
    var html = '<option value="all">全部渠道</option>' + chs.map(function (c) {
      return '<option value="' + esc(c) + '">' + esc(HUB.channelLabel(c, data())) + '</option>';
    }).join('');
    if (sel.innerHTML !== html) sel.innerHTML = html;
    sel.value = chs.indexOf(state.channel) >= 0 ? state.channel : 'all';
    if (sel.value !== state.channel) state.channel = 'all';
    syncCombo();
  }

  /* 组合筛选：一行一个「渠道 · 模型」，找单个模型不用先在两个下拉里对齐。 */
  function syncCombo() {
    var sel = HUB.$('#fCombo');
    if (!sel) return;
    var pairs = models().map(function (m) { return { value: comboOf(m), label: HUB.shortChannel(m.channel) + ' · ' + m.id }; });
    pairs.sort(function (a, b) { return a.label.localeCompare(b.label); });
    var html = '<option value="all">全部渠道×模型</option>' + pairs.map(function (p) {
      return '<option value="' + esc(p.value) + '">' + esc(p.label) + '</option>';
    }).join('');
    if (sel.innerHTML !== html) sel.innerHTML = html;
    var exists = pairs.some(function (p) { return p.value === state.combo; });
    if (!exists) state.combo = 'all';
    sel.value = state.combo;
  }

  function openGroup(id) {
    var g = groups().find(function (x) { return x.id === id; });
    if (!g) return;
    HUB.openWO({
      kicker: 'MODEL REGISTER / 模型铭牌',
      title: g.name,
      meta: 'REG / ' + g.id,
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>VENDOR / 厂商</span><b>' + esc(g.vendor) + '</b></div>'
        + '<div class="fx-wo-kv"><span>目录链接</span><b>' + g.models.length + '</b><span>启用</span><b>' + g.enabled + '</b><span>最大上下文</span><b>' + F.tok(g.context) + '</b><span>思考档</span><b>' + esc(g.efforts.join(' / ') || '自动') + '</b><span>能力</span><b>' + esc(capTags(g.models[0]).join(' · ') || '—') + '</b><span>今日请求</span><b>' + F.int(g.traffic) + '</b></div>'
        + '<div class="fx-wo-text">档位由网关按请求的 reasoning_effort 自动判定，目录里只做展示，不提供手选。</div></div>'
        + (g.decision ? '<div class="fx-wo-block"><div class="fx-wo-label"><span>决策端点 / DECISION ROUTE</span><b>INPUT-ONLY</b></div>'
          + '<div class="fx-wo-kv"><span>端点 / ENDPOINT</span><b>' + esc(g.endpoints.join(' / ') || '/v1/systemone') + '</b>'
          + '<span>计费 / BASIS</span><b>IN-ONLY</b><span>输出 / OUTPUT TOKENS</span><b>0 · 不计费</b></div>'
          + '<div class="fx-wo-text">官方按输入 token 计费，output_tokens 恒为 0；价目 out=0 表示“不计输出”，不是免费牌价。入口仍是 /v1/chat/completions，网关按模型改道到该端点。</div></div>' : '')
        + '<div class="fx-wo-block"><div class="fx-wo-label"><span>CHANNEL LINKS / 渠道链接</span><b>' + g.models.length + '</b></div>'
        /* 一行一个「渠道 × 模型」，价格写输入/缓存/输出三档：
         * 原来只写渠道名 + 区间价，一列全是同一个名字，看不出哪行对应哪个模型。 */
        + '<table class="fx-wo-table fx-price-table"><thead><tr><th>模型 / MODEL</th><th>渠道</th><th>IN</th><th>缓存</th><th>OUT</th><th>CTX</th><th>动作</th></tr></thead><tbody>'
        + g.models.map(function (m) {
          var rate = (m.rate && typeof m.rate === 'object') ? m.rate : null;
          var cell = function (key) {
            return rate && rate[key] !== null && rate[key] !== undefined ? HUB.money3(rate[key]) : '—';
          };
          return '<tr><td title="' + esc(m.id) + '">' + esc(m.id) + '</td><td>' + esc(HUB.shortChannel(m.channel))
            + '</td><td>' + esc(cell('in')) + '</td><td>' + esc(cell('cache')) + '</td><td>' + esc(cell('out'))
            + '</td><td>' + esc(F.tok(m.context))
            + '</td><td><button class="btn small" data-wo-price="' + esc(m.channel) + '" data-wo-price-model="' + esc(m.id) + '">改价</button> <button class="btn small" data-wo-toggle="' + esc(m.channel) + '" data-wo-id="' + esc(m.id) + '" data-wo-enabled="' + (m.enabled ? '0' : '1') + '">' + (m.enabled ? '停用' : '启用') + '</button></td></tr>';
        }).join('') + '</tbody></table></div>'
        + '<div class="fx-wo-block"><div class="fx-wo-label"><span>SOURCE / 字段</span><span>READ MODEL</span></div><div class="fx-wo-text">readmodel/catalog: id · channel · context · efforts · reasoning · vision · tools · rate · free · enabled · pricing_mode · decision · endpoint · billing_basis。价目行带 source / source_url / checked_at，来自各渠道自己公布的官方价目页（见 /api/pricing）；取不到就是空，不推算。</div></div>'
        + '<div class="fx-wo-warning">停用与测速均需确认；快照档只生成 ARMED 票。</div>',
      actions: [{ label: '收起', close: true }]
    });
    Array.from(HUB.$('#woBody').querySelectorAll('[data-wo-toggle]')).forEach(function (b) {
      b.addEventListener('click', function () {
        toggleArm(b.getAttribute('data-wo-toggle'), b.getAttribute('data-wo-id'), b.getAttribute('data-wo-enabled') === '1', b.getAttribute('data-wo-id'));
      });
    });
    Array.from(HUB.$('#woBody').querySelectorAll('[data-wo-price]')).forEach(function (b) {
      b.addEventListener('click', function () {
        var ch = b.getAttribute('data-wo-price');
        var mid = b.getAttribute('data-wo-price-model');
        var model = g.models.find(function (m) { return m.channel === ch && m.id === mid; }) || {};
        openPriceEdit(ch, mid, model.rate);
      });
    });
  }

  function openPriceEdit(channel, id, rate) {
    rate = rate && typeof rate === 'object' ? rate : {};
    HUB.openWO({
      kicker: 'PRICING / SINGLE MODEL',
      title: '单模型改价',
      meta: 'CH / ' + channel + ' · MODEL / ' + id,
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>CNY / 每百万 TOKEN</span><b>AUTO FX</b></div>'
        + '<div class="fx-form"><label>INPUT / 输入<input id="priceIn" type="number" min="0" step="0.001" value="' + esc(rate.in === undefined || rate.in === null ? '' : rate.in) + '"></label>'
        + '<label>CACHE / 缓存<input id="priceCache" type="number" min="0" step="0.001" value="' + esc(rate.cache === undefined || rate.cache === null ? '' : rate.cache) + '"></label>'
        + '<label>OUTPUT / 输出<input id="priceOut" type="number" min="0" step="0.001" value="' + esc(rate.out === undefined || rate.out === null ? '' : rate.out) + '"></label></div>'
        + '<div class="fx-wo-text">按价目表 usd_cny 汇率换算为 USD_per_mtok 落盘；缓存留空 = 跟随输入价。</div>'
        + '<div class="fx-wo-warning">改价只影响后续请求计价，不回溯已记账行。</div></div>',
      actions: [
        { label: '保存改价', filled: true, close: false, onClick: function () {
          var inNode = HUB.$('#priceIn');
          var outNode = HUB.$('#priceOut');
          var cacheNode = HUB.$('#priceCache');
          var inV = inNode ? Number(inNode.value) : NaN;
          var outV = outNode ? Number(outNode.value) : NaN;
          var cacheV = cacheNode && String(cacheNode.value || '') !== '' ? Number(cacheNode.value) : inV;
          if (!isFinite(inV) || !isFinite(outV) || inV < 0 || outV < 0) {
            HUB.toast('输入/输出价需为非负数字'); return;
          }
          HUB.job({
            title: '单模型改价 / ' + id,
            endpoint: '/api/pricing/single',
            payload: { channel: channel, model: id, in_cny: inV,
                       out_cny: outV, cache_cny: cacheV },
            confirm: '把 ' + channel + ' / ' + id + ' 的牌价改为 输入 ¥' + inV
              + '、输出 ¥' + outV + '（每百万 token）？只影响后续计价。',
            timeout: 20000,
            local: true,
            refresh: true,
            channels: [channel],
            after: HUB.closeWO
          });
        } },
        { label: '取消', close: true }
      ]
    });
  }

  HUB.views.models = render;

  document.addEventListener('change', function (e) {
    /* fEffort 已从筛选行移出（档位由网关自动判定），这里保留映射也不会抛错。 */
    if (e.target && e.target.id === 'fCombo') {
      state.combo = e.target.value || 'all';
      /* 选组合时把渠道下拉对齐到同一渠道，避免两个筛选互相掐成空表。 */
      if (state.combo !== 'all') {
        state.channel = String(state.combo).split('|||')[0];
        var chSel = HUB.$('#fChannel');
        if (chSel) chSel.value = state.channel;
      }
      render();
      return;
    }
    var map = { fChannel: 'channel', fRealm: 'realm', fState: 'enabled', fCap: 'cap', fEffort: 'effort', fBill: 'bill' };
    if (e.target && map[e.target.id]) { state[map[e.target.id]] = e.target.value; render(); }
  });
  document.addEventListener('input', function (e) {
    if (e.target.id === 'modelSearch') { state.q = e.target.value; render(); }
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-price-public]')) { syncPriceBook('public'); return; }
    if (e.target.closest('#priceBookBtn')) {
      closeBatch();
      openPriceBook();
      return;
    }
    if (e.target.closest('#modelBatchBtn')) {
      var menu = HUB.$('#modelBatchMenu');
      var btn = HUB.$('#modelBatchBtn');
      if (menu) {
        menu.hidden = !menu.hidden;
        if (btn) btn.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true');
      }
      return;
    }
    var batch = e.target.closest('[data-batch]');
    if (batch) { runBatch(batch.getAttribute('data-batch')); return; }
    if (!e.target.closest('#modelBatch')) closeBatch();
    var d = e.target.closest('#modelDensity button');
    if (d) { state.density = d.getAttribute('data-density'); render(); return; }
    var a = e.target.closest('#modelAxis button');
    if (a) { state.axis = a.getAttribute('data-axis'); render(); return; }
    var th = e.target.closest('.fx-matrix th[data-sort]');
    if (th) {
      var key = th.getAttribute('data-sort');
      if (state.sortKey === key) state.sortDir *= -1; else { state.sortKey = key; state.sortDir = key === 'name' || key === 'channel' ? 1 : -1; }
      render(); return;
    }
    var card = e.target.closest('[data-act]');
    if (card) {
      var act = card.getAttribute('data-act');
      if (act === 'detail') { openGroup(card.getAttribute('data-model')); return; }
      if (act === 'toggle') {
        toggleArm(card.getAttribute('data-channel'), card.getAttribute('data-id'), card.getAttribute('data-enabled') === '1', card.getAttribute('data-id'));
        return;
      }
      if (act === 'bench') { benchArm(card.getAttribute('data-channel'), card.getAttribute('data-id'), card.getAttribute('data-id')); return; }
      if (act === 'bench-group') {
        var g = groups().find(function (x) { return x.id === card.getAttribute('data-model'); });
        if (g) {
          var target = g.models.find(function (m) { return benchSupported(m.channel); });
          if (target) benchArm(target.channel, target.id, g.name);
          else benchUnsupported(g.models.map(function (m) { return m.channel; }), g.name);
        }
        return;
      }
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeBatch();
  });
  document.addEventListener('DOMContentLoaded', function () { syncSelects(); render(); });
  if (document.readyState !== 'loading') { syncSelects(); render(); }
})();
