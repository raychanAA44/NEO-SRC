/* 06 渠道管理 · visibility + readmodel/channels + accounts + custom */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;

  function data() { return HUB.data.state.data || {}; }
  function visChannels() { return (data().visibility && data().visibility.channels) || []; }
  function readChannels() { return (data().channels && data().channels.channels) || []; }
  function customItems() { return (data().custom && data().custom.items) || []; }
  function accounts() { return data().accounts || []; }
  function usageRows() { return (data().usage && data().usage.rows) || []; }

  /* ── 06 单入口读模型 /api/channels/overview ─────────────────────
   * 官方额度与本地 8888 预算闸门为独立计量口径，分别展示，不合并计算。 */
  function overview() { return data().channels_overview || {}; }
  function ovChannels() { return overview().channels || []; }
  function ovOf(ch) {
    return ovChannels().find(function (x) { return x.channel === ch; }) || null;
  }
  /* 路由表里的渠道（如 opencode）可能没有 visibility 行：合并后一起上架。 */
  function allChannels() {
    var list = visChannels().slice();
    ovChannels().forEach(function (o) {
      if (!o || !o.channel) return;
      if (list.some(function (x) { return x.channel === o.channel; })) return;
      list.push({ channel: o.channel, label: o.label || o.channel,
                  hidden: !o.enabled, enabled: !!o.enabled, kind: 'route' });
    });
    return list;
  }
  function ovAccounts(ch) {
    var ov = ovOf(ch);
    return (ov && ov.accounts && ov.accounts.rows) || [];
  }

  function readOf(ch) { return readChannels().find(function (x) { return x.channel === ch; }) || null; }
  function customOf(ch) {
    var raw = String(ch || '');
    var name = raw.indexOf('custom:') === 0 ? raw.slice(7) : raw;
    return customItems().find(function (x) { return x.name === name; }) || null;
  }
  function accountsOf(ch) { return accounts().filter(function (a) { return a.channel === ch; }); }

  function creditsOf(id) {
    var a = accounts().find(function (x) { return Number(x.id) === Number(id); });
    return a && a.credits ? Number(a.credits.remain) : null;
  }
  function successIds(res) {
    var out = [];
    ((res && res.results) || []).forEach(function (step) {
      ((step.body && step.body.results) || []).forEach(function (r) {
        if (r && r.ok && r.id) out.push(Number(r.id));
      });
    });
    return Array.from(new Set(out));
  }
  function inspectCredits(res) {
    var out = {};
    ((res && res.results) || []).forEach(function (step) {
      ((step.body && step.body.accounts) || []).forEach(function (a) {
        if (!a || !a.id) return;
        var st = (a.steps || []).find(function (s) { return s.key === 'credits'; });
        out[a.id] = {
          remain: a.credits ? Number(a.credits.remain) : null,
          ok: !st || (st.ok !== false && !st.skipped),
          detail: st ? (st.skipped ? '账号未探活' : String(st.detail || '')) : ''
        };
      });
    });
    return out;
  }
  /* 签到只写 last_checkin；积分是另一份官方快照。
   * 成功签到后走一次不强制续期的 live 体检，把 fresh credits 落库，再刷新账号健康。 */
  function afterCheckin(res, ch) {
    if (!res || res.armed || res.busy || res.cancelled || res.empty) return Promise.resolve();
    var done = successIds(res);
    if (!done.length) {
      if (res.ok) HUB.closeWO();
      return HUB.refresh();
    }
    var before = {};
    done.forEach(function (id) { before[id] = creditsOf(id); });
    return HUB.job({
      title: '积分回读 / 签到后',
      endpoint: '/api/accounts/inspect',
      payload: { ids: done, live: true, force: false },
      timeout: 150000,
      refresh: false,
      channels: ch ? [ch] : []
    }).then(function (ins) {
      var after = inspectCredits(ins);
      var good = 0;
      var bits = done.map(function (id) {
        var b = before[id], a = after[id];
        if (!a || a.remain === null || !isFinite(a.remain)) return '#' + id + ' 余额未回读';
        if (!a.ok) return '#' + id + ' 积分未回读' + (a.detail ? '（' + a.detail.slice(0, 36) + '）' : '');
        good += 1;
        return '#' + id + ' ' + (b === null ? '—' : F.int(b)) + ' → ' + F.int(a.remain);
      });
      if (res.ok) HUB.closeWO();
      HUB.toast((good === done.length ? '积分已回读 · ' : '积分回读不全 · ') + bits.join(' / '));
      return HUB.refresh();
    });
  }
  function catalogCount(ch) {
    var c = data().catalog && data().catalog.index && data().catalog.index.channels;
    return c && c[ch] !== undefined ? c[ch] : null;
  }
  function modelCount(ch) {
    var cc = catalogCount(ch);
    if (cc !== null) return cc;
    var cu = customOf(ch);
    if (cu) return (cu.models || []).length;
    return null;
  }
  function spendOf(ch) {
    var rm = readOf(ch);
    if (rm && rm.today) return Number(rm.today.cost_cny) || 0;
    var d = ((data().dashboard && data().dashboard.channels) || []).find(function (x) { return x.key === ch; });
    return d ? Number(d.cost_cny) || 0 : null;
  }
  function requestsOf(ch) {
    var rm = readOf(ch);
    if (rm && rm.today) return Number(rm.today.requests) || 0;
    var d = ((data().dashboard && data().dashboard.channels) || []).find(function (x) { return x.key === ch; });
    return d ? Number(d.requests) || 0 : 0;
  }
  function latencyOf(ch) {
    var name = ch.indexOf('custom:') === 0 ? ch.slice(7) : '';
    var list = usageRows().filter(function (r) {
      if (r.channel === ch) return true;
      if (name && (r.upstream === name || String(r.channel_label || '').indexOf(name) >= 0)) return true;
      return false;
    });
    if (!list.length) return null;
    var ttfts = list.map(function (r) { return Number(r.ttft_ms); }).filter(function (n) { return n > 0; });
    if (!ttfts.length) return null;
    return { avg: ttfts.reduce(function (a, b) { return a + b; }, 0) / ttfts.length, n: list.length };
  }
  function officialOf(ch) {
    return ((data().official && data().official.channels) || []).find(function (x) { return x.channel === ch; }) || null;
  }
  function poolOf(ch) {
    return ((data().status && data().status.pool && data().status.pool.channels) || []).find(function (x) { return x.name === ch; }) || null;
  }
  function accountStats(ch) {
    /* 计数与明细必须同源：两处各取一个接口时，刷新不同步会出现「表头 0 / 表里 1 行」。 */
    var list = accountsOf(ch);
    if (list.length) {
      var ok = list.filter(function (a) { return (a.status || '') === 'ok' || a.status === 'active' || a.can_checkin; }).length;
      var ready = list.filter(function (a) { return !a.expired && !a.error; }).length;
      return { total: list.length, ready: ready || ok, canCheckin: list.filter(function (a) { return a.can_checkin; }).length,
               checkedToday: list.filter(function (a) { return Number(a.last_checkin) > 0; }).length,
               credits: list.reduce(function (s, a) { return s + (a.credits && a.credits.remain ? Number(a.credits.remain) : 0); }, 0) };
    }
    var ov = ovOf(ch);
    if (ov && ov.accounts && ov.accounts.total !== undefined) {
      var oa = ov.accounts;
      return { total: oa.total || 0, ready: oa.active || 0, canCheckin: oa.need_refresh || 0,
               checkedToday: oa.checked_in_today || 0, credits: oa.credits || 0 };
    }
    return { total: 0, ready: 0, canCheckin: 0, checkedToday: 0, credits: 0 };
  }
  function modelListOf(ch) {
    var list = ((data().catalog && data().catalog.models) || []).filter(function (m) { return m.channel === ch; });
    if (list.length) return list.map(function (m) { return m.id; });
    var cu = customOf(ch);
    return cu ? (cu.models || []) : [];
  }
  function recentOf(ch) {
    var name = ch.indexOf('custom:') === 0 ? ch.slice(7) : '';
    return usageRows().filter(function (r) {
      if (r.channel === ch) return true;
      return name && (r.upstream === name || String(r.channel_label || '').indexOf(name) >= 0);
    }).slice(0, 24).reverse();
  }
  function quotaOf(ch) {
    var off = officialOf(ch);
    if (off && off.cost_label) return off.cost_label;
    if (ch === 'custom:opencode-free') {
      var cu = customOf(ch);
      return (cu ? (cu.models || []).length : 0) + ' FREE';
    }
    var pool = poolOf(ch);
    if (pool && pool.ready === false) return 'STANDBY';
    return ch.indexOf('custom:') === 0 ? 'NO CAP' : '—';
  }
  function egressIpOf(ch) {
    if (ch !== 'custom:opencode-free') return '';
    var eg = (data().status && data().status.pool && data().status.pool.egress) || {};
    var probe = eg.probe || {};
    var ips = probe.egress_ips || {};
    return ips.proxy || ips.direct || '—';
  }
  function egressOf(ch) {
    var eg = (data().status && data().status.pool && data().status.pool.egress) || {};
    var override = eg.channel_overrides && eg.channel_overrides[ch];
    if (override) return String(override).toUpperCase();
    if (ch === 'custom:opencode-free') return 'LOCAL BRIDGE';
    if (ch.indexOf('custom:') === 0) return 'AUTO';
    if (eg.proxy_enabled === false) return 'DIRECT';
    return eg.proxy_enabled ? 'AUTO' : '—';
  }

  function renderStrip() {
    var host = HUB.$('#channelStrip');
    if (!host) return;
    var list = allChannels();
    var visible = list.filter(function (c) { return !c.hidden; }).length;
    var official = readChannels();
    var ready = official.filter(function (c) { return c.ready; }).length;
    host.innerHTML = '<div><span>可见渠道 / VISIBILITY</span><b>' + visible + '<em> / ' + list.length + ' 个渠道</em></b></div>'
      + '<div><span>官方渠道 / OFFICIAL</span><b>' + ready + '<em> / ' + official.length + ' READY</em></b></div>'
      + '<div><span>第三方渠道 / CUSTOM</span><b>' + customItems().length + '<em> 个已登记</em></b></div>'
      + '<div><span>账号 / ACCOUNTS</span><b>' + F.int((data().status && data().status.accounts && data().status.accounts.total)) + '<em> 个</em></b></div>'
      + (function () {
        var top = ((data().dashboard && data().dashboard.channels) || []).slice().sort(function (a, b) { return (Number(b.cost_cny) || 0) - (Number(a.cost_cny) || 0); })[0];
        return '<div><span>最高费用渠道 / TOP SPEND</span><b>' + esc(top ? F.cny(top.cost_cny, 4) : '—') + '<em> ' + esc(top ? HUB.shortChannel(top.key) : '—') + '</em></b></div>';
      })();
    var h = HUB.$('#channelVisibleHead'); if (h) h.textContent = String(visible);
    var tot = HUB.$('#channelTotalHead'); if (tot) tot.textContent = '/ ' + list.length;
    var foot = HUB.$('#channelFootStats');
    if (foot) foot.textContent = '可见渠道 ' + list.length + ' · 第三方渠道 ' + customItems().length;
    var c = HUB.$('#channelCountHead'); if (c) c.textContent = list.length + ' 个渠道';
  }

  function cabinetHtml(ch, idx) {
    var cu = customOf(ch.channel);
    var rm = readOf(ch.channel);
    var label = cu ? ('第三方 · ' + cu.name) : (ch.label || ch.channel);
    var hidden = !!ch.hidden;
    var on = !hidden;
    var latency = latencyOf(ch.channel);
    var req = requestsOf(ch.channel);
    var spend = spendOf(ch.channel);
    var models = modelCount(ch.channel);
    var acct = rm ? rm.accounts : accountsOf(ch.channel).length;
    var ready = hidden ? 'HIDDEN' : (rm ? (rm.ready ? 'READY' : 'NO ACCOUNT') : (cu ? (cu.enabled ? 'READY' : 'STANDBY') : 'UNKNOWN'));
    var maxReq = Math.max.apply(null, visChannels().map(function (c) { return requestsOf(c.channel); }).concat([1]));
    return '<article class="fx-cabinet ' + (on ? '' : 'is-off') + '" data-channel="' + esc(ch.channel) + '" tabindex="0" role="button" aria-label="' + esc(label + ' 检修') + '">'
      + '<div class="fx-cab-head"><span class="fx-cab-id">CH-' + String(idx + 1).padStart(2, '0') + ' / ' + esc(HUB.kindOf(ch.channel)) + '</span><span class="stamp ' + (on ? '' : 'hollow') + ' fx-cab-state">' + (on ? 'VISIBLE' : 'HIDDEN') + '</span></div>'
      + '<div class="fx-cab-name"><h3 title="' + esc(label) + '">' + esc(label) + '</h3><span>' + esc(HUB.realmOf(ch.channel).toUpperCase()) + '</span></div>'
      + '<div class="fx-cab-body"><div class="fx-knife-wrap">'
      + '<button class="fx-knife" type="button" aria-pressed="' + (on ? 'true' : 'false') + '"'
      + ' aria-label="' + esc(label + (on ? '，当前启用，点击停用' : '，当前停用，点击启用')) + '"'
      + ' title="' + esc(on ? '点击停用：从公开列表隐藏' : '点击启用：重新上架') + '"></button>'
      + '<span class="fx-knife-state' + (on ? '' : ' is-off') + '">' + (on ? '启用' : '停用') + '</span></div>'
      + '<div><div class="fx-cab-lat"><div><span class="label">TTFT / 样本均</span><span class="v">' + (latency ? esc(F.dur(latency.avg)) : '—') + '</span><em>' + (latency ? ' / ' + latency.n + ' 笔' : '') + '</em></div></div>'
      + '<div class="fx-cab-metrics"><span><b>' + F.int(req) + '</b> REQ</span><span><b>' + esc(spend === null ? '—' : F.cny(spend, 4)) + '</b></span><span>' + (models === null ? '—' : F.int(models)) + ' MDL</span></div></div></div>'
      + '<div class="fx-cab-ticks" aria-hidden="true"><i style="width:' + Math.max(0, Math.min(100, Math.round(req / maxReq * 100))) + '%"></i></div>'
      + (function () {
        var stats = accountStats(ch.channel);
        var list = modelListOf(ch.channel);
        var recent = recentOf(ch.channel);
        var pool = poolOf(ch.channel);
        var ip = egressIpOf(ch.channel);
        var maxT = Math.max.apply(null, recent.map(function (r) { return Number(r.ttft_ms) || 0; }).concat([1]));
        return '<div class="fx-cab-detail">'
          + '<span>QUOTA / 额度</span><b>' + esc(quotaOf(ch.channel)) + '</b>'
          + '<span>账号 / ACCOUNTS</span><b>' + (stats.total ? stats.ready + ' / ' + stats.total + ' READY' : 'VIRTUAL') + '</b>'
          + (ip ? '<span>EGRESS / 出口</span><b>' + esc(ip) + '</b>' : '<span>MODELS / 目录</span><b>' + (models === null ? '—' : F.int(models)) + '</b>')
          + '<span>SAMPLE / 近端</span><b>' + (recent.length ? recent.length + ' 笔 · ' + esc(F.dur(latency && latency.avg)) : '—') + '</b>'
          + '</div>'
          + '<div class="fx-cab-spark" aria-hidden="true">' + (recent.length ? recent.map(function (r) {
            var v = Number(r.ttft_ms) || 0;
            return '<i style="height:' + (v ? Math.max(12, Math.round(v / maxT * 100)) : 3) + '%" title="' + esc(F.dur(v)) + '"></i>';
          }).join('') : '<i class="zero" style="height:3%"></i><i class="zero" style="height:3%"></i><i class="zero" style="height:3%"></i><i class="zero" style="height:3%"></i><i class="zero" style="height:3%"></i><i class="zero" style="height:3%"></i>') + '</div>'
          + '<div class="fx-cab-bay" title="' + esc(list.slice(0, 8).join(' / ')) + '">' + (list.length ? list.slice(0, 8).map(function (m) { return '<span>' + esc(m) + '</span>'; }).join('') : '<span class="dim">' + esc(pool && pool.ready_reason ? pool.ready_reason : '无目录链接') + '</span>') + '</div>'
      })()
      + '<div class="fx-cab-foot"><span>' + esc(egressOf(ch.channel)) + ' · ' + (acct === null ? '—' : acct) + ' 账号</span><span class="stamp ' + (on ? 'hollow' : '') + '">' + esc(ready) + '</span></div>'
      + '</article>';
  }

  var shelfOpen = false;

  function coldCabinetHtml(item) {
    var ch = item.ch;
    var idx = item.idx;
    var stats = accountStats(ch.channel);
    var models = modelCount(ch.channel);
    var latency = latencyOf(ch.channel);
    return '<button class="fx-cold-cab" type="button" data-cold-cabinet="' + esc(ch.channel) + '" title="' + esc(ch.label || ch.channel) + ' · 点击检修">'
      + '<span class="fx-cold-id">CH-' + String(idx + 1).padStart(2, '0') + '</span>'
      + '<b>' + esc(ch.label || ch.channel) + '</b>'
      + '<span class="fx-cold-meta">' + esc(HUB.realmOf(ch.channel).toUpperCase()) + ' · ' + (stats.total ? stats.ready + '/' + stats.total + ' 个账号' : '无账号') + ' · ' + (models === null ? '—' : models) + ' 个模型' + (latency ? ' · ' + esc(F.dur(latency.avg)) : '') + '</span>'
      + '<span class="stamp hollow">HIDDEN</span>'
      + '<span class="fx-cold-extra">' + esc(quotaOf(ch.channel)) + ' · ' + esc(egressOf(ch.channel)) + (modelListOf(ch.channel).length ? ' · ' + esc(modelListOf(ch.channel).slice(0, 3).join(' / ')) : '') + '</span>'
      + '</button>';
  }

  function coldShelfHtml(cold) {
    return '<section class="fx-cold-shelf' + (shelfOpen ? ' is-open' : '') + '" aria-label="冷备机架">'
      + '<div class="fx-cold-head"><b>COLD SHELF / 冷备机架</b><span>' + cold.length + ' 组停用 · 视觉噪音减半</span><span class="spacer"></span><span>HIDDEN 不占主排架</span>'
      + '<button class="btn small" type="button" id="coldShelfToggle">' + (shelfOpen ? '收起详情' : '展开详情') + '</button></div>'
      + '<div class="fx-cold-items">' + (cold.map(coldCabinetHtml).join('') || '<span class="fx-cold-empty">当前没有停用机柜。</span>') + '</div>'
      + '</section>';
  }

  function renderGrid() {
    var host = HUB.$('#channelGrid');
    if (!host) return;
    var active = [], cold = [];
    allChannels().forEach(function (ch, idx) {
      (ch.hidden ? cold : active).push({ ch: ch, idx: idx });
    });
    host.innerHTML = '<div class="fx-active-rack">' + active.map(function (x) { return cabinetHtml(x.ch, x.idx); }).join('') + '</div>'
      + coldShelfHtml(cold);
    var head = HUB.$('#channelCountHead');
    if (head) head.textContent = active.length + ' ACTIVE · ' + cold.length + ' COLD';
  }

  function accountStatsCsv(list) {
    return list.map(function (a) { return String(a.id); }).join(',');
  }

  function accountsTable(ch) {
    var list = accountsOf(ch);
    var importBlock = renderImportText(ch);
    if (!list.length) {
      var rows = ovAccounts(ch);
      if (!rows.length) return importBlock + '<div class="fx-wo-text">本渠道无账号明细。</div>';
      var ids = rows.map(function (a) { return String(a.id); }).join(',');
      return importBlock + '<table class="fx-wo-table"><thead><tr><th>账号</th><th>状态</th><th>积分</th><th>签到</th><th>令牌</th><th>动作</th></tr></thead><tbody>'
        + rows.map(function (a) {
          return '<tr><td>#' + esc(a.id) + ' ' + esc(a.name || '—') + '<br><span style="color:var(--silver-filigree)">' + esc(a.realm || '') + '</span></td>'
            + '<td>' + esc(a.status || '—') + '</td>'
            + '<td>' + esc(a.credits === null || a.credits === undefined ? '—' : F.num(a.credits, 0)) + '</td>'
            + '<td>' + esc(a.last_checkin_iso || '—') + '</td>'
            + '<td>' + esc(a.token_masked || '—') + '</td>'
            + '<td><button class="btn small" type="button" data-wo-delete="' + esc(a.id) + '">删除</button></td></tr>';
        }).join('') + '</tbody></table>'
        + '<div class="fx-wo-tools"><span>BATCH / 批量作业</span>'
        + '<button class="btn small" type="button" data-batch-checkin-channel="' + esc(ch) + '">一键批量签到 · ' + rows.length + '</button></div>'
        + '<div class="fx-wo-warning">签到只写 last_checkin，绝不自动续期 refresh token；已存 ' + ids.split(',').length + ' 个账号。</div>';
    }
    var canCheckin = list.filter(function (a) { return a.can_checkin; });
    var checkinIds = canCheckin.map(function (a) { return String(a.id); }).join(',');
    var allIds = accountStatsCsv(list);
    return importBlock + '<div class="fx-wo-tools"><span>BATCH / 批量作业</span>'
      + '<button class="btn small" type="button" data-batch-checkin="' + esc(checkinIds) + '"' + (canCheckin.length ? '' : ' disabled') + '>一键批量签到 · ' + canCheckin.length + '</button>'
      + '<button class="btn small" type="button" data-batch-inspect="' + esc(allIds) + '">全量探针 · ' + list.length + '</button></div>'
      + '<table class="fx-wo-table"><thead><tr><th>账号</th><th>状态</th><th>积分</th><th>到期</th><th>签到</th><th>动作</th></tr></thead><tbody>'
      + list.map(function (a) {
        var cr = a.credits || {};
        var credit = cr.size ? (F.num(cr.remain, 0) + ' / ' + F.num(cr.size, 0)) : '—';
        return '<tr><td>#' + esc(a.id) + ' ' + esc(a.nickname || a.name || '—') + '<br><span style="color:var(--silver-filigree)">' + esc(a.realm || '') + '</span></td>'
          + '<td>' + esc(a.status || '—') + '</td><td>' + esc(credit) + '<br><span style="color:var(--silver-filigree)">' + esc(cr.plan || cr.unit || '') + '</span></td>'
          + '<td>' + esc(a.expires_iso || '—') + '</td><td>' + (a.can_checkin ? '可签到' : (a.checkin_supported === false ? '无入口' : '已签')) + '</td>'
          + '<td><button class="btn small" data-wo-checkin="' + esc(a.id) + '">签到</button> <button class="btn small" data-wo-inspect="' + esc(a.id) + '">检测</button> <button class="btn small" data-wo-delete="' + esc(a.id) + '">删除</button></td></tr>';
      }).join('') + '</tbody></table>'
      + '<div class="fx-wo-warning">签到后自动回读积分；写操作需确认。</div>';
  }

  function cliCapsule(label, command, note) {
    return '<button class="fx-cli" type="button" data-copy="' + esc(command) + '" title="复制命令"><span class="fx-cli-top"><b>' + esc(label) + '</b><em>COPY</em></span><code>' + esc(command) + '</code>'
      + (note ? '<span class="fx-cli-note">' + esc(note) + '</span>' : '') + '</button>';
  }

  /* ── 在线接入 / 编辑第三方渠道（全部网页表单，无终端命令） ─────── */

  /* ── 双轨额度条：官方源 / 本地闸门 ────────────────────────────
   * 官方额度与本地预算分别显示；数据缺失时标记 NO DATA，不以 0% 代替。 */
  function clampPct(v) {
    /* null/undefined/空串必须保持 null：Number(null) 是 0，会把“没有数据”画成 0%。 */
    if (v === null || v === undefined || v === '') return null;
    var n = Number(v);
    return isFinite(n) ? Math.max(0, Math.min(100, n)) : null;
  }
  function quotaValue(off) {
    if (!off) return '—';
    var bits = [];
    if (off.remaining !== null && off.remaining !== undefined) bits.push('剩余 ' + F.num(off.remaining, 0));
    if (off.limit !== null && off.limit !== undefined) bits.push('上限 ' + F.num(off.limit, 0));
    if (off.unit) bits.push(String(off.unit));
    return bits.join(' / ') || '—';
  }
  function renderDualQuota(ch) {
    var ov = ovOf(ch);
    var off = (ov && ov.official) || {};
    var gate = (ov && ov.gateway) || {};
    var seal = String((ov && ov.official_source) || off.source || 'none').toUpperCase();
    var offPct = clampPct(off.used_pct);
    var gatePct = clampPct(gate.used_pct);
    var label = (ov && ov.label) || HUB.shortChannel(ch);
    var gateNote = (gate.cap_cny === null || gate.cap_cny === undefined)
      ? '未设上限'
      : (F.cny(gate.spent_cny || 0, 4) + ' / ' + F.cny(gate.cap_cny, 4) + ' · ' + String(gate.key || ''));
    return '<div class="fx-wo-block fx-quota-dual"><div class="fx-wo-label"><span>QUOTA / 双轨额度</span><b>2 TRACKS</b></div>'
      + '<div class="fx-track-item"><div class="fx-track-meta">'
      + '<span>[官方源: ' + esc(label) + ']</span><b class="fx-quota-seal">' + esc(seal) + '</b>'
      + '<em>' + (offPct === null ? 'NO DATA' : esc(F.pct(offPct, 1))) + ' · ' + esc(quotaValue(off)) + '</em></div>'
      + '<div class="fx-mix-bar' + (offPct === null ? ' is-empty' : '') + '"><i class="fx-bar-official" style="width:' + (offPct === null ? 0 : offPct) + '%"></i></div>'
      + '<div class="fx-track-note">' + esc(off.note || (offPct === null ? '官方额度不可达且无手工快照' : '来源 ' + seal)) + (off.reset_at ? ' · 重置 ' + esc(off.reset_at) : '') + '</div></div>'
      + '<div class="fx-track-item"><div class="fx-track-meta">'
      + '<span>[本地闸门: 8888]</span><b class="fx-quota-seal">GATEWAY CAP</b>'
      + '<em>' + (gatePct === null ? 'NO DATA' : esc(F.pct(gatePct, 1))) + ' · ' + esc(gateNote) + '</em></div>'
      + '<div class="fx-mix-bar' + (gatePct === null ? ' is-empty' : '') + '"><i class="fx-bar-gateway" style="width:' + (gatePct === null ? 0 : gatePct) + '%"></i></div>'
      + '<div class="fx-track-note">' + (gate.blocked ? '已到上限：/v1 新请求会被 402 拒绝' : (gate.warn ? '已过 80% 预警线，到顶即拦' : '今日本地记账花费 vs 本地设定上限')) + '</div></div>'
      + (function () {
        var rc = (ov && ov.reconcile) || {};
        if (!rc.verdict) return '';
        return '<div class="fx-wo-text">RECONCILE / ' + esc(String(rc.verdict).toUpperCase())
          + (rc.comparable ? ' · 官方 ' + F.int(rc.official_used) + ' vs 本地 ' + F.int(rc.local_requests) + ' 次' : '')
          + (rc.note ? '｜' + esc(rc.note) : '') + '</div>';
      })()
      + '</div>';
  }

  /* ── 渠道价目明细：一个模型一行，输入 / 缓存 / 输出三档都写实数 ──
   * 行取自 readmodel/catalog 的 rate（元 / 百万 token）。没有 rate 就是
   * 没同步过价：整块如实说，不拿区间价或目录价补。 */
  function renderPriceDetail(ch) {
    var rows = (((data().catalog || {}).models) || []).filter(function (m) {
      return m && m.channel === ch;
    });
    if (!rows.length) {
      return '<div class="fx-wo-block"><div class="fx-wo-label"><span>PRICE / 价目明细</span><b>0 MODEL</b></div>'
        + '<div class="fx-wo-text">目录里还没有该渠道的模型：先「探测模型」，再同步价目表。</div></div>';
    }
    var priced = rows.filter(function (m) { return m.rate && typeof m.rate === 'object'; });
    if (!priced.length) {
      return '<div class="fx-wo-block"><div class="fx-wo-label"><span>PRICE / 价目明细</span><b>' + F.int(rows.length) + ' MODEL</b></div>'
        + '<div class="fx-wo-warning">该渠道暂无价目行（去 03 渠道价目同步）。</div></div>';
    }
    var sources = [], latest = '';
    priced.forEach(function (m) {
      var r = m.rate || {};
      if (r.source && sources.indexOf(r.source) < 0) sources.push(r.source);
      var day = String(r.checked_at || m.price_checked_at || '');
      if (day && day > latest) latest = day;
    });
    var cell = function (v) {
      return (v === null || v === undefined || !isFinite(Number(v))) ? '—' : HUB.money3(v);
    };
    return '<div class="fx-wo-block"><div class="fx-wo-label"><span>PRICE / 价目明细</span><b>'
      + F.int(priced.length) + ' / ' + F.int(rows.length) + ' PRICED</b></div>'
      + '<div class="fx-wo-text">价目来源 ' + esc(sources.join(' / ') || '—')
      + ' · 核验日 ' + esc(latest || '—') + ' · 单位 元 / 百万 token</div>'
      + '<table class="fx-wo-table fx-price-table"><thead><tr><th>模型 / MODEL</th><th>渠道</th><th>IN</th><th>缓存</th><th>OUT</th><th>CTX</th><th>来源</th></tr></thead><tbody>'
      + priced.map(function (m) {
        var r = m.rate || {};
        var src = String(r.source || r.channel || '—');
        var day = String(r.checked_at || m.price_checked_at || '');
        return '<tr><td title="' + esc(m.id) + '">' + esc(m.id) + '</td>'
          + '<td>' + esc(HUB.shortChannel(m.channel)) + '</td>'
          + '<td>' + esc(cell(r.in)) + '</td><td>' + esc(cell(r.cache)) + '</td><td>' + esc(cell(r.out)) + '</td>'
          + '<td>' + esc(F.tok(m.context)) + '</td>'
          + '<td title="' + esc(src + (day ? ' · ' + day : '')) + '">' + esc(src.slice(0, 16)) + (src.length > 16 ? '…' : '') + '</td></tr>';
      }).join('')
      + '</tbody></table></div>';
  }

  /* 官方额度块：只给已收录的渠道画；收录过就在 control.json 留档，下次开机仍算已收录。 */
  /* 官方额度：今日 / 近 5 小时 / 本周 / 本月 四个窗口。
   * 官方报了的窗口画官方进度，没报的窗口写明本地实测用量（标 LOCAL），
   * 两边都没有就直说没有——绝不把 0 当成「用完了」或「没用量」。 */
  var QUOTA_WINDOWS = [['today', '今日'], ['h5', '近 5 小时'], ['week', '本周'], ['month', '本月']];
  function windowLine(key, label, win) {
    var off = (win && win.official) || null;
    var loc = (win && win.local) || null;
    var pct = off && off.used_pct !== null && off.used_pct !== undefined ? clampPct(off.used_pct) : null;
    var right;
    var seal;
    if (off && (off.used !== null && off.used !== undefined || off.limit !== null && off.limit !== undefined)) {
      seal = 'OFFICIAL';
      right = (off.used === null || off.used === undefined ? '—' : F.num(off.used, 0))
        + (off.limit ? ' / ' + F.num(off.limit, 0) : '')
        + (off.unit && off.unit !== '%' ? ' ' + off.unit : '')
        + (pct === null ? ' · 官方未报上限' : ' · ' + F.pct(pct, 1))
        + (off.requests ? ' · ' + F.int(off.requests) + ' 笔' : '')
        + (off.tokens ? ' · ' + F.tok(off.tokens) + ' TOK' : '');
    } else if (loc) {
      seal = 'LOCAL';
      right = F.int(loc.requests) + ' 笔 · ' + F.cny(loc.cost_cny, 4)
        + ' · ' + F.tok(loc.tokens) + ' TOK' + (loc.errors ? ' · 失败 ' + F.int(loc.errors) : '');
    } else {
      seal = 'NO DATA';
      right = '官方未公布，本地无记录';
    }
    return '<div class="fx-track-item"><div class="fx-track-meta"><span>' + esc(label) + '</span>'
      + '<b class="fx-quota-seal">' + esc(seal) + '</b><em>' + esc(right) + '</em></div>'
      + (pct === null ? '' : '<div class="fx-mix-bar"><i class="fx-bar-official" style="width:' + pct + '%"></i></div>')
      + (off && off.reset_at ? '<div class="fx-track-note">重置 ' + esc(off.reset_at) + '</div>' : '')
      + '</div>';
  }
  function renderOfficialBlock(ch, off) {
    var ov = ovOf(ch) || {};
    if (ov.recorded === false) return '';
    var o = (off && off.official) || ov.official || {};
    var wins = (off && off.windows) || {};
    /* 官方只报了一个总窗口时，今日那一行用总窗口顶上并注明，其余窗口保留本地实测。 */
    var rows = QUOTA_WINDOWS.map(function (pair) {
      var win = wins[pair[0]] || {};
      if (pair[0] === 'today' && !win.official && o.source && o.source !== 'none'
          && o.used !== null && o.used !== undefined) {
        win = { official: o, local: win.local, source: 'official' };
      }
      return windowLine(pair[0], pair[1], win);
    }).join('');
    var hasOfficial = o && o.source && o.source !== 'none';
    var filled = ['today', 'h5', 'week', 'month'].filter(function (k) { return wins[k] && wins[k].official; }).length;
    var pending = !!(off && off.official_pending);
    var missing = (off && off.official_missing) || [];
    var extra = (o && o.windows_extra) || {};
    var stamp = pending ? 'PENDING' : (filled ? 'OFFICIAL ' + filled + '/4' : (hasOfficial ? 'TOTAL ONLY' : 'LOCAL ONLY'));
    return '<div class="fx-wo-block fx-quota-dual"><div class="fx-wo-label"><span>OFFICIAL / 官方额度分窗</span><b>'
      + esc(stamp) + '</b></div>'
      + rows
      + (extra.balance_usd !== null && extra.balance_usd !== undefined
        ? '<div class="fx-wo-kv"><span>官方余额</span><b>' + esc(F.cny(extra.balance_usd, 4))
          + (extra.allowance_usd ? ' / ' + esc(F.cny(extra.allowance_usd, 2)) : '') + '</b>'
          + (extra.period_end ? '<span>周期至</span><b>' + esc(String(extra.period_end).slice(0, 10)) + '</b>' : '') + '</div>'
        : '')
      + '<div class="fx-wo-text">官方报了几窗就画几窗；未公布的窗口给本地实测用量并标 LOCAL，不拿 0 充数。'
      + (hasOfficial ? '额度 SNAPSHOT：' + esc(quotaValue(o)) : '登录一次即同步官方额度，收录配置已保留。') + '</div>'
      + (pending ? '<div class="fx-wo-text">正在向官方取分窗数据（首次约几秒），读数会自己补上；也可以点下方「登录并立即对账」立刻取一次。</div>' : '')
      + (missing.length
        ? '<details class="fx-wo-import"><summary>缺字段 / MISSING ' + missing.length + '</summary>'
          + missing.map(function (m) {
            return '<div class="fx-wo-text">· ' + esc(m.label || m.window || '*') + '：' + esc(m.why || '—') + '</div>';
          }).join('') + '</details>'
        : '')
      + '</div>';
  }

  /* ── 登录即同步：一次完成「存凭据 → 抓额度 → 对账」 ──────────── */
  /* 各家官方账单认的东西不一样，登录框下方就按渠道说清缺哪个字段。 */
  var LOGIN_HINT = {
    'open-design': '这个渠道的官方账单只认登录态：填用户名+密码（网关现换会话）或勾选浏览器会话；sk- 开头的 key 只能推理，读不到额度。',
    'ollama': 'TOKEN 填 ollama 的 API key（Authorization: Bearer）；官方限流 10 次/分钟，本页 60s 取一次。'
  };
  function renderVendorLogin(ch) {
    var ov = ovOf(ch);
    var creds = (ov && ov.creds) || {};
    var masked = creds.masked || {};
    var stored = !!creds.channel;
    var maskText = Object.keys(masked).map(function (k) { return k + ': ' + masked[k]; }).join(' / ');
    return '<div class="fx-wo-block fx-vendor-login"><div class="fx-wo-label"><span>LOGIN / 登录即同步</span><b>' + (stored ? 'STORED' : 'NOT STORED') + '</b></div>'
      + '<div class="fx-wo-text">' + (stored ? '已存凭据 ' + esc(maskText || '（只有掩码）') : '未存凭据：登录后立即拉官方额度，无需第二次点同步。') + '</div>'
      + '<div class="fx-form">'
      + '<label class="full">TOKEN / 会话令牌<input data-vendor-token type="password" autocomplete="off" placeholder="粘贴 token / cookie / api key"></label>'
      + '<label>USERNAME / 用户名<input data-vendor-username type="text" autocomplete="off" placeholder="可选"></label>'
      + '<label>PASSWORD / 密码<input data-vendor-password type="password" autocomplete="off" placeholder="可选"></label>'
      + '</div>'
      + '<label class="fx-vendor-browser"><input data-vendor-browser type="checkbox"> 从本机浏览器取会话（只读，不写回）</label>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" data-vendor-login>登录并立即对账</button></div>'
      + '<div class="fx-wo-text">' + esc(LOGIN_HINT[ch] || '凭据加密落盘，接口只回掩码；拿不到浏览器 profile 会如实报 profile_unavailable。') + '</div></div>';
  }

  /* ── 原文导入：贴什么就解析什么，只入库不签到 ─────────────────── */
  function renderImportText(ch) {
    return '<div class="fx-wo-import"><details><summary>原文导入 / PASTE ACCOUNT</summary>'
      + '<div class="fx-form"><label class="full">RAW / 会话原文<textarea data-import-text rows="3" placeholder="Session Token / Cookies / JSON 串"></textarea></label>'
      + '<label>REALM / 分区<input data-import-realm type="text" placeholder="default"></label>'
      + '<label>NAME / 备注<input data-import-name type="text" placeholder="可选"></label></div>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" data-import-submit>导入账号</button></div>'
      + '<div class="fx-wo-text">只解析入库，不触发签到；凭据加密落库，只回掩码。</div></details></div>';
  }

  /* ── 自定义出站头：只展示掩码 ─────────────────────────────── */
  function headerChips(ov) {
    var headers = (ov && ov.headers) || {};
    var names = (ov && ov.header_names) || Object.keys(headers);
    if (!names.length) return '';
    return '<div class="fx-wo-headers"><div class="fx-wo-label"><span>OUTBOUND HEADERS / 自定义出站头</span><b>' + names.length + ' HDR</b></div>'
      + '<div class="fx-wo-chips">' + names.map(function (k) {
        return '<span class="fx-wo-chip">' + esc(k) + ' <b>' + esc(headers[k] || '●●●●●●') + '</b></span>';
      }).join('') + '</div></div>';
  }

  function customBlock(cu) {
    if (!cu) return '';
    var models = cu.models || [];
    var headerLines = (cu.header_names || Object.keys(cu.headers || {})).map(function (k) {
      return k + ': ' + ((cu.headers && cu.headers[k]) || '');
    }).join('\n');
    return '<div class="fx-wo-block" data-custom-block="' + esc(cu.name) + '"><div class="fx-wo-label"><span>CUSTOM UPSTREAM / 第三方上游</span><b>' + (cu.enabled === false ? 'DISABLED' : 'ENABLED') + '</b></div>'
      + '<div class="fx-wo-kv"><span>BASE URL</span><b>' + esc(cu.base_url || '—') + '</b><span>KEY</span><b>' + (cu.has_key ? 'MASKED / PRESENT' : 'MISSING') + '</b><span>MODELS</span><b>' + models.length + '</b><span>UPDATED</span><b>' + esc(F.hm(cu.updated_at)) + '</b></div>'
      + headerChips(ovOf('custom:' + (cu.name || '')) || ovOf(cu.name || ''))
      + (cu.note ? '<div class="fx-wo-text">' + esc(cu.note) + '</div>' : '')
      + (cu.quirks && Object.keys(cu.quirks).length ? '<div class="fx-wo-chips">' + Object.keys(cu.quirks).map(function (k) { return '<span class="fx-wo-chip">' + esc(k) + ' <b>' + esc(cu.quirks[k]) + '</b></span>'; }).join('') + '</div>' : '')
      + '<div class="fx-form">'
      + '<label>NAME / 渠道标识<input data-custom-name type="text" value="' + esc(cu.name || '') + '" readonly></label>'
      + '<label>KEY / 密钥<input data-custom-key type="password" autocomplete="off" placeholder="留空 = 保持当前密钥"></label>'
      + '<label class="full">BASE URL / 上游地址<input data-custom-base type="text" value="' + esc(cu.base_url || '') + '" placeholder="https://host/v1"></label>'
      + '<label class="full">MODELS / 支持模型（逗号或换行分隔）<textarea data-custom-models rows="2">' + esc(models.join(', ')) + '</textarea></label>'
      + '<label class="full">HEADERS / 出站头（每行 Key: Value）<textarea data-custom-headers rows="2">' + esc(headerLines) + '</textarea></label>'
      + '</div>'
      + '<div class="fx-btn-row"><button class="btn small filled" type="button" data-custom-save>保存配置</button><button class="btn small" type="button" data-custom-delete>删除渠道</button><button class="btn small" type="button" data-cu-probe>⚡ 探测模型</button><button class="btn small" type="button" data-cu-prices>⇅ 同步价目表</button></div>'
      + '<div class="fx-wo-text" data-cu-probe-out>探测会按 BASE URL + KEY 直接读上游 /v1/models，并向官方公开价目端点拉单价；只读不写，回填模型框。</div>'
      + '<div class="fx-wo-text">保存即原子回写上游表并热加载路由；删除只摘路由配置，不动账本与账号。</div></div>';
  }

  function ocBlock(ch) {
    if (ch !== 'custom:opencode-free') return '';
    var cu = customOf(ch);
    /* 自定义渠道实际落盘是裸名 opencode-free；出口/额度按真实渠道名取，
     * 兼容旧版 custom: 前缀，避免覆盖写到不存在的键上。 */
    var ov = ovOf('opencode-free') || ovOf(ch) || {};
    var eg = ov.egress || {};
    var models = (cu && cu.models) || [];
    return '<div class="fx-wo-block fx-oc-bridge" data-oc-bridge>'
      + '<div class="fx-wo-label"><span>OC BRIDGE / 本地免费档桥</span><b data-oc-state>CHECKING</b></div>'
      + '<div class="fx-wo-kv"><span>BASE</span><b>' + esc((cu && cu.base_url) || '—') + '</b>'
      + '<span>MODELS</span><b>' + models.length + ' 款</b>'
      + '<span>KEY REALM</span><b>opencode-free</b>'
      + '<span>EGRESS</span><b>' + esc(eg.channel_proxy_url || 'DIRECT') + '</b></div>'
      + '<div class="fx-form"><label class="full">BRIDGE TOKEN / 桥接令牌<input id="ocBridgeToken" type="password" autocomplete="off" placeholder="留空 = 保持当前 Token"></label></div>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" data-oc-token-save>更新 Token</button>'
      + '<button class="btn small" type="button" data-oc-token-clear>清除 Token</button>'
      + '<button class="btn small filled" type="button" data-oc-issue-key>一键签发专属 KEY</button>'
      + '<button class="btn small" type="button" data-oc-probe>⚡ 实时双腿探活</button></div>'
      + '<div class="fx-wo-text" data-oc-mask>Token 状态读取中…</div>'
      + '<div class="fx-wo-text" data-oc-probe-out>尚未探活：点「实时双腿探活」分别测直连与代理出口。</div>'
      + '<div class="fx-wo-text">Token 加密存储，接口仅返回掩码；专属 API Key 明文仅在签发回执中返回一次。</div></div>';
  }

  function egressSelector(ch) {
    var ov = ovOf(ch) || {};
    var eg = ov.egress || {};
    var override = String(eg.channel_override || 'INHERIT');
    var mode = override === 'INHERIT' ? 'inherit' : (override === 'DIRECT' ? 'direct' : 'proxy');
    var globalUrl = eg.proxy_url || '';
    var url = mode === 'proxy' ? override : globalUrl;
    return '<div class="fx-wo-block" data-egress-selector="' + esc(ch) + '"><div class="fx-wo-label"><span>EGRESS / 渠道出口</span><b>' + (eg.channel_proxy_url ? 'PROXY' : 'DIRECT') + '</b></div>'
      + '<div class="fx-form"><label>出口策略<select data-egress-mode>'
      + '<option value="inherit"' + (mode === 'inherit' ? ' selected' : '') + '>全局默认 (' + esc(globalUrl || 'DIRECT') + ')</option>'
      + '<option value="direct"' + (mode === 'direct' ? ' selected' : '') + '>强制直连 (DIRECT)</option>'
      + '<option value="proxy"' + (mode === 'proxy' ? ' selected' : '') + '>独立代理</option>'
      + '</select></label><label class="full">PROXY URL<input data-egress-url type="text" value="' + esc(url) + '" placeholder="http://host:7897"></label></div>'
      + '<div class="fx-btn-row"><button class="btn small filled" type="button" data-egress-save>保存出口</button><button class="btn small" type="button" data-egress-test>连通性测试</button></div>'
      + '<div class="fx-wo-text" data-egress-result>当前生效：' + esc(eg.channel_proxy_url || 'DIRECT') + '</div></div>';
  }

  function probeText(leg) {
    if (!leg) return '—';
    return (leg.ok ? 'OK' : 'FAIL')
      + ' · ' + (leg.elapsed_ms === null || leg.elapsed_ms === undefined ? '—' : leg.elapsed_ms + 'ms')
      + (leg.ip ? ' · ' + leg.ip : '')
      + (leg.error ? ' · ' + leg.error : '');
  }

  function classifyLeg(leg) {
    if (!leg) return '';
    var text = String(leg.error || '') + ' ' + String(leg.http_status || '');
    if (/403|forbidden|abusive|风控|封/i.test(text)) return 'ABUSIVE';
    if (/402|quota|insufficient|payment|额度/i.test(text)) return 'QUOTA';
    return '';
  }

  function wireEgress(ch) {
    var root = HUB.$('#woBody').querySelector('[data-egress-selector]');
    if (!root) return;
    var modeNode = root.querySelector('[data-egress-mode]');
    var urlNode = root.querySelector('[data-egress-url]');
    var out = root.querySelector('[data-egress-result]');
    function syncDisabled() { if (urlNode) urlNode.disabled = !modeNode || modeNode.value !== 'proxy'; }
    if (modeNode) modeNode.addEventListener('change', syncDisabled);
    syncDisabled();
    var save = root.querySelector('[data-egress-save]');
    if (save) save.addEventListener('click', function () {
      var mode = modeNode ? modeNode.value : 'inherit';
      var url = urlNode ? String(urlNode.value || '').trim() : '';
      if (mode === 'proxy' && !/^https?:\/\//i.test(url)) {
        HUB.toast('独立代理需 http:// 或 https://'); return;
      }
      HUB.job({
        title: '渠道出口 / ' + ch,
        endpoint: '/api/egress/channel-override',
        payload: { channel: ch, mode: mode, url: url },
        confirm: '把 ' + ch + ' 的出口切到「' + mode + '」？只影响该渠道出站。',
        timeout: 20000, local: true, refresh: false, channels: [ch]
      }).then(function (res) {
        if (!res || !res.ok) return;
        HUB.toast('出口已保存 / ' + ch);
        HUB.refresh().then(function () { if (HUB.$('#woBody')) openChannel(ch); });
      });
    });
    var test = root.querySelector('[data-egress-test]');
    if (test) test.addEventListener('click', function () {
      if (out) out.textContent = '探测中…（直连 / 代理双腿）';
      fetch('/api/egress/probe', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ channel: ch })
      }).then(function (r) { return r.json(); }).then(function (body) {
        if (!out) return;
        out.innerHTML = '直连：' + esc(probeText(body.direct))
          + '<br>代理：' + (body.proxy_configured ? esc(probeText(body.proxy)) : '未配置 · 当前为直连');
      }).catch(function (err) {
        if (out) out.textContent = '探针失败：' + String((err && err.message) || err);
      });
    });
  }

  function wireOcBlock(ch) {
    if (ch !== 'custom:opencode-free') return;
    var body = HUB.$('#woBody');
    if (!body || !body.querySelector('[data-oc-bridge]')) return;
    var mask = body.querySelector('[data-oc-mask]');
    var state = body.querySelector('[data-oc-state]');
    var probeOut = body.querySelector('[data-oc-probe-out]');
    function paintStatus() {
      fetch('/api/oc-bridge/status', { credentials: 'same-origin', cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (b) {
          if (state) state.textContent = b.has_token ? 'TOKEN SET' : 'NO TOKEN';
          if (mask) mask.textContent = b.has_token
            ? ('Token 已配置 · ' + (b.token_masked || '') + ' · 更新 ' + F.hm(b.updated_at))
            : '尚未配置 Token；请填写后更新。';
        }).catch(function () { if (mask) mask.textContent = 'Token 状态读取失败'; });
    }
    paintStatus();
    var save = body.querySelector('[data-oc-token-save]');
    if (save) save.addEventListener('click', function () {
      var node = body.querySelector('#ocBridgeToken');
      var token = node ? String(node.value || '').trim() : '';
      if (!token) { HUB.toast('先粘贴 Bridge Token'); return; }
      HUB.job({
        title: 'OC Bridge Token / 更新',
        endpoint: '/api/oc-bridge/token',
        payload: { token: token },
        confirm: '更新 OC 桥令牌？旧令牌立即失效。',
        timeout: 20000, local: true, refresh: false, channels: []
      }).then(function (res) {
        if (res && res.ok) { if (node) node.value = ''; paintStatus(); }
      });
    });
    var clear = body.querySelector('[data-oc-token-clear]');
    if (clear) clear.addEventListener('click', function () {
      HUB.job({
        title: 'OC Bridge Token / 清除',
        endpoint: '/api/oc-bridge/token',
        payload: { clear: true },
        confirm: '清除已保存的 OC 桥令牌？',
        timeout: 20000, local: true, refresh: false, channels: []
      }).then(function (res) { if (res && res.ok) paintStatus(); });
    });
    var issue = body.querySelector('[data-oc-issue-key]');
    if (issue) issue.addEventListener('click', function () {
      HUB.job({
        title: '签发 OC 专属 KEY',
        endpoint: '/api/keys',
        payload: { name: 'oc-bridge-client', realm: 'opencode-free',
                   note: 'OC 桥专属下游 KEY' },
        confirm: '签发一把 opencode-free 专属 KEY？明文只返回一次。',
        timeout: 20000, local: true, refresh: false, channels: ['opencode-free']
      }).then(function (res) {
        var b = (res && res.results && res.results[0] && res.results[0].body) || {};
        if (b.key && HUB.showIssuedKey) HUB.showIssuedKey(b);
      });
    });
    var probe = body.querySelector('[data-oc-probe]');
    if (probe) probe.addEventListener('click', function () {
      if (probeOut) probeOut.textContent = '探活中…（直连 / 代理双腿并行）';
      fetch('/api/egress/probe', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ channel: ch })
      }).then(function (r) { return r.json(); }).then(function (b) {
        if (!probeOut) return;
        var verdict = b.ok
          ? ('两条腿至少一条通；当前生效：' + (b.effective === 'proxy' ? '代理' : '直连'))
          : '两条腿都不通：检查本机网络与代理地址';
        var kind = classifyLeg(b.direct) || classifyLeg(b.proxy);
        probeOut.innerHTML = '<b>直连</b> ' + esc(probeText(b.direct))
          + '<br><b>代理</b> ' + (b.proxy_configured ? esc(probeText(b.proxy)) : '未配置 · 当前为直连')
          + '<br><b>VERDICT</b> ' + esc(verdict)
          + (kind ? '<br><b>TYPE</b> ' + esc(kind) : '');
      }).catch(function (err) {
        if (probeOut) probeOut.textContent = '探活失败：' + String((err && err.message) || err);
      });
    });
  }

  function openOcBridge() {
    HUB.openWO({
      kicker: 'CHANNEL / OC BRIDGE',
      title: 'OC 桥 · 本地免费档',
      meta: 'CH / opencode-free',
      body: ocBlock('custom:opencode-free') + egressSelector('opencode-free'),
      actions: [{ label: '收起', close: true }]
    });
    wireOcBlock('custom:opencode-free');
    wireEgress('opencode-free');
  }

  /* ── 在线接入 / 编辑第三方渠道（不再打印 curl） ─────────────── */
  function customHeaderLines(cu) {
    var names = (cu && cu.header_names) || Object.keys((cu && cu.headers) || {});
    return names.map(function (k) {
      return k + ': ' + (((cu.headers || {})[k]) || '');
    }).join('\n');
  }

  /* 探测回来的模型元数据（context / max_out / vision）按 BASE URL 暂存，
     保存时随 catalog 一起落库，新渠道才不至于在目录里显示 0 CTX。 */
  var probeCache = {};

  function customPayloadFrom(scope) {
    function val(sel) {
      var node = scope.querySelector(sel);
      return node ? String(node.value || '').trim() : '';
    }
    return {
      name: val('[data-custom-name], #cuName'),
      base_url: val('[data-custom-base], #cuBase'),
      api_key: val('[data-custom-key], #cuKey'),
      models: val('[data-custom-models], #cuModels'),
      headers: val('[data-custom-headers], #cuHeaders'),
      catalog: probeCache[val('[data-custom-base], #cuBase')] || undefined,
      note: (function () {
        var note = scope.querySelector('#cuNote');
        return note ? String(note.value || '').trim() : undefined;
      })()
    };
  }

  function submitCustom(scope, editing, after) {
    var payload = customPayloadFrom(scope);
    if (!payload.name) { HUB.toast('渠道标识必填 / EMPTY'); return; }
    if (!payload.base_url) { HUB.toast('上游地址必填 / EMPTY'); return; }
    HUB.job({
      title: (editing ? '保存渠道 / ' : '接入渠道 / ') + payload.name,
      endpoint: '/api/custom',
      payload: payload,
      confirm: (editing ? '回写 ' + payload.name + ' 的上游配置并热加载路由？'
        : '接入新渠道 ' + payload.name + ' 并立即热加载路由？')
        + '\n密钥若填写将加密落盘，接口只回掩码。',
      timeout: 30000,
      local: true,
      refresh: false,
      channels: []
    }).then(function (res) {
      if (!res || !res.ok) return;
      HUB.toast('渠道已保存 / ' + payload.name);
      HUB.refresh().then(function () { if (after) after(); });
    });
  }

  function deleteCustom(name) {
    HUB.job({
      title: '删除渠道 / ' + name,
      endpoint: '/api/custom/' + encodeURIComponent(name),
      method: 'DELETE',
      confirm: '删除渠道 ' + name + '？会从上游表摘除并热加载；账本历史与账号不删。',
      timeout: 30000,
      local: true,
      refresh: false,
      channels: []
    }).then(function (res) {
      if (!res || !res.ok) return;
      HUB.toast('渠道已删除 / ' + name);
      HUB.closeWO();
      HUB.refresh();
    });
  }

  function openCustomForm(cu) {
    cu = cu || {};
    var editing = !!cu.name;
    HUB.openWO({
      kicker: 'CHANNEL / CUSTOM UPSTREAM',
      title: editing ? ('编辑渠道 · ' + cu.name) : '接入新渠道',
      meta: editing ? ('CH / ' + cu.name) : 'NEW / CUSTOM UPSTREAM',
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>UPSTREAM / 第三方渠道</span><b>' + (editing ? 'EDIT' : 'NEW') + '</b></div>'
        + '<div class="fx-form">'
        + '<label>NAME / 渠道标识<input id="cuName" type="text" value="' + esc(cu.name || '') + '"' + (editing ? ' readonly' : '') + ' placeholder="acme"></label>'
        + '<label>KEY / 认证密钥<input id="cuKey" type="password" autocomplete="off" placeholder="' + (editing ? '留空 = 保持' : '可留空') + '"></label>'
        + '<label class="full">BASE URL / 上游地址<input id="cuBase" type="text" value="' + esc(cu.base_url || '') + '" placeholder="https://host/v1"></label>'
        + '<label class="full">MODELS / 支持模型（逗号或换行分隔）<textarea id="cuModels" rows="3">' + esc((cu.models || []).join(', ')) + '</textarea></label>'
        + '<label class="full">HEADERS / 出站头（每行 Key: Value）<textarea id="cuHeaders" rows="3">' + esc(customHeaderLines(cu)) + '</textarea></label>'
        + '<label class="full">NOTE / 备注<input id="cuNote" type="text" value="' + esc(cu.note || '') + '"></label>'
        + '</div><div class="fx-btn-row"><button class="btn small" type="button" data-cu-probe>⚡ 探测模型</button><button class="btn small filled" type="button" data-cu-prices>⇅ 同步价目表</button></div>'
        + '<div class="fx-wo-text" data-cu-probe-out>探测会按 BASE URL + KEY 直接读上游 /v1/models；只读不写，回填到模型框。</div>'
        + '<div class="fx-wo-warning">保存即原子回写上游表并热加载；密钥加密落盘，接口只回掩码。</div></div>',
      actions: [
        { label: editing ? '保存配置' : '接入渠道', filled: true, close: false, onClick: function () {
          submitCustom(HUB.$('#woBody'), editing, HUB.closeWO);
        } },
        { label: '取消', close: true }
      ]
    });
    wireCustomProbe();
    wirePriceSync();
  }

  function wireCustomProbe() {
    var root = HUB.$('#woBody');
    var btn = root && root.querySelector('[data-cu-probe]');
    if (!btn) return;
    var out = root.querySelector('[data-cu-probe-out]');
    btn.addEventListener('click', function () {
      var payload = customPayloadFrom(root);
      if (!payload.base_url) { HUB.toast('上游地址必填 / EMPTY'); return; }
      btn.disabled = true;
      if (out) out.textContent = '探测中…读取上游 /v1/models';
      fetch('/api/custom/probe', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name: payload.name, base_url: payload.base_url,
                               api_key: payload.api_key, headers: payload.headers })
      }).then(function (r) { return r.json(); }).then(function (b) {
        var models = (b && b.models) || [];
        var node = root.querySelector('[data-custom-models], #cuModels');
        if (models.length && node) node.value = models.join(', ');
        if (b && b.details && b.details.length) probeCache[payload.base_url] = b.details;
        var prices = (b && b.prices) || {};
        var priceText = prices.ok
          ? ('官方价目 ' + F.int(prices.count) + ' 条 · ' + prices.source_url)
          : ('官方价目未拉到 · ' + (prices.error || '端点不可用'));
        /* 探测把「试过哪些端点」也写出来：只看一句「未通过」无法判断是
         * 路径不对、密钥不对，还是上游根本没挂模型清单。 */
        var tried = (b && b.tried) || [];
        var triedText = tried.map(function (t) {
          return esc(String(t.path || '—')) + ' → HTTP ' + esc(t.http_status || '—')
            + ' · ' + esc(t.status || '—') + (t.count ? ' · ' + F.int(t.count) : '')
            + (t.error ? ' · ' + esc(String(t.error).slice(0, 60)) : '');
        }).join('<br>');
        var fields = (b && b.fields) || [];
        if (out) {
          out.innerHTML = (b.ok
            ? ('探测成功 · ' + models.length + ' 款模型已回填 · ' + b.latency_ms + 'ms')
            : ('探测未通过 · ' + esc(b.status || '—') + (b.error ? ' · ' + esc(b.error) : '')
               + (b.status === 'auth_error' ? '：密钥被上游拒绝，先核对 KEY' : '')))
            + '<br>' + esc(priceText) + (prices.ok ? ' · 点「同步价目表」落盘' : '')
            + (triedText ? '<br><b>ENDPOINTS / 已试端点</b><br>' + triedText : '')
            + (fields.length ? '<br><b>FIELDS / 已收字段</b> ' + esc(fields.join(' · ')) : '');
        }
        HUB.toast(b.ok ? ('模型已回填 / ' + models.length) : ('探测未通过 / ' + (b.status || 'FAIL')));
      }).catch(function (err) {
        if (out) out.textContent = '探测失败 · ' + String((err && err.message) || err).slice(0, 160);
      }).finally(function () { btn.disabled = false; });
    });
  }

  /* 官方价目落盘：只在用户点按钮时写，探测本身不改账本。 */
  function wirePriceSync() {
    var root = HUB.$('#woBody');
    if (!root) return;
    var btn = root.querySelector('[data-cu-prices]');
    if (!btn) return;
    var out = root.querySelector('[data-cu-probe-out]');
    var allowPublic = false;      /* 默认只认登录态；报 no_session 后用户再点一次才用目录价 */
    btn.addEventListener('click', function () {
      var payload = customPayloadFrom(root);
      if (!payload.name) { HUB.toast('渠道标识必填 / EMPTY'); return; }
      if (!allowPublic && !payload.base_url) { HUB.toast('上游地址必填 / EMPTY'); return; }
      btn.disabled = true;
      if (out) out.textContent = allowPublic ? '拉公开价目端点并落盘…' : '用登录态读官方账单 unitPrices…';
      fetch('/api/pricing/sync', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify({ channel: payload.name, base_url: payload.base_url,
                               mode: allowPublic ? 'public' : 'account', apply: true })
      }).then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
        .then(function (res) {
          var b = res.body || {};
          if (res.status !== 200) throw new Error((b.error && b.error.message) || ('HTTP ' + res.status));
          var applied = b.applied || {};
          if (b.ok) {
            allowPublic = false;
            if (out) out.innerHTML = '价目已写入 ' + esc(b.channel) + ' · ' + F.int(applied.written) + ' 行 · '
              + (b.source === 'account' ? '登录态官方账单' : '公开价目端点') + ' '
              + esc(b.source_url)
              + (applied.kept && applied.kept.length ? ' · 保留未在官方表的 ' + applied.kept.length + ' 行' : '');
            HUB.toast('价目已同步 / ' + (applied.written || 0));
            return;
          }
          if (['no_session', 'auth_error', 'turnstile_required', 'no_cookie', 'rate_limited',
               'no_credentials', 'no_endpoint', 'network', 'account_unavailable']
              .indexOf(b.code) >= 0 && !allowPublic) {
            allowPublic = true;
            if (out) out.innerHTML = '<b>' + esc(b.error || '没有登录态') + '</b><br>'
              + esc(b.hint || '') + '<br>正在改用官方价目页抓取…';
            /* 登录态拿不到就退回「官方公开价目页」，四条渠道各有自己的源。 */
            fetch('/api/pricing/feeds', {
              method: 'POST', credentials: 'same-origin', cache: 'no-store',
              headers: { 'content-type': 'application/json', accept: 'application/json' },
              body: JSON.stringify({ channels: [payload.name], apply: true })
            }).then(function (r) { return r.json(); }).then(function (fb) {
              var mine = (fb.channels || {})[payload.name] || {};
              var ap = mine.applied || {};
              if (mine.ok) {
                if (out) out.innerHTML = '价目已按官方页写入 ' + esc(payload.name) + ' · '
                  + F.int(ap.written) + ' 行 · ' + esc(mine.source_url || '')
                  + (ap.kept && ap.kept.length ? ' · 保留未在官方表的 ' + ap.kept.length + ' 行' : '');
                HUB.toast('官方价目已同步 / ' + (ap.written || 0));
                return;
              }
              if (out) out.textContent = '官方价目页也没拿到 · '
                + String(mine.error || mine.code || '—').slice(0, 120);
            }).catch(function (e2) {
              if (out) out.textContent = '价目抓取失败 · ' + String((e2 && e2.message) || e2).slice(0, 120);
            }).finally(function () { btn.disabled = false; });
            return;
          }
          if (out) out.textContent = '价目未同步 · ' + String(b.error || b.code || '—').slice(0, 160);
          HUB.toast('价目未同步');
        })
        .catch(function (err) {
          if (out) out.textContent = '价目同步失败 · ' + String((err && err.message) || err).slice(0, 160);
        }).finally(function () { btn.disabled = false; });
    });
  }

  function wireCustomBlock(cu) {
    var root = HUB.$('#woBody').querySelector('[data-custom-block]');
    if (!root || !cu) return;
    var saveBtn = root.querySelector('[data-custom-save]');
    if (saveBtn) saveBtn.addEventListener('click', function () {
      submitCustom(root, true, HUB.closeWO);
    });
    var delBtn = root.querySelector('[data-custom-delete]');
    if (delBtn) delBtn.addEventListener('click', function () { deleteCustom(cu.name); });
    wireCustomProbe();
    wirePriceSync();
  }

  function openChannel(ch) {
    /* 路由表里的渠道可能没有 visibility 行：合并口径下也要能开机柜。 */
    var vis = allChannels().find(function (x) { return x.channel === ch; })
      || { channel: ch, label: HUB.shortChannel(ch), hidden: false };
    var cu = customOf(ch);
    var rm = readOf(ch);
    var off = officialOf(ch);
    var latency = latencyOf(ch);
    var title = cu ? ('第三方 · ' + cu.name) : (vis.label || ch);
    HUB.openWO({
      kicker: 'CHANNEL WORK ORDER / 机柜检修',
      title: title,
      meta: 'CH / ' + ch,
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>STATE / 状态</span><b>' + (vis.hidden ? 'HIDDEN' : 'VISIBLE') + '</b></div>'
        + '<div class="fx-wo-kv"><span>渠道</span><b>' + esc(ch) + '</b><span>类型</span><b>' + esc(HUB.kindOf(ch)) + '</b><span>Realm</span><b>' + esc(HUB.realmOf(ch)) + '</b><span>账号</span><b>' + (rm ? rm.accounts : accountsOf(ch).length) + '</b><span>模型</span><b>' + (modelCount(ch) === null ? '—' : modelCount(ch)) + '</b><span>今日请求</span><b>' + F.int(requestsOf(ch)) + '</b><span>今日花费</span><b>' + esc(spendOf(ch) === null ? '—' : F.cny(spendOf(ch), 4)) + '</b><span>TTFT 均</span><b>' + esc(latency ? F.dur(latency.avg) : '—') + '</b></div></div>'
        + renderDualQuota(ch)
        + renderOfficialBlock(ch, off)
        + renderPriceDetail(ch)
        + renderVendorLogin(ch)
        + egressSelector(ch)
        + (customBlock(cu) || headerChips(ovOf(ch)))
        + ocBlock(ch)
        + '<div class="fx-wo-block"><div class="fx-wo-label"><span>账号池 / ACCOUNTS</span><b>' + accountStats(ch).total + '</b></div>' + accountsTable(ch) + '</div>'
        + '<div class="fx-wo-block"><div class="fx-wo-label"><span>渠道可见性 / VISIBILITY</span><b>' + (vis.hidden ? 'HIDDEN' : 'VISIBLE') + '</b></div><div class="fx-wo-text">三处同步摘除或恢复；不删账号。</div>'
        + '<button class="btn small" type="button" data-wo-visibility="' + esc(ch) + '" data-wo-hidden="' + (vis.hidden ? '0' : '1') + '">' + (vis.hidden ? '恢复可见' : '停用该渠道') + '</button></div>',
      actions: [{ label: '收起工单', close: true }]
    });
    function idsOf(sel) {
      var node = HUB.$('#woBody').querySelector(sel);
      if (!node) return [];
      return String(node.getAttribute(sel.slice(1, -1)) || '').split(',').map(function (x) { return Number(x); }).filter(function (n) { return n > 0; });
    }
    function wire(sel, label, endpoint, payloadOf, confirmText, timeout, afterOk) {
      var node = HUB.$('#woBody').querySelector(sel);
      if (!node) return;
      node.addEventListener('click', function () {
        var ids = idsOf(sel);
        if (!ids.length) { HUB.toast('没有可执行的账号 / EMPTY'); return; }
        var p = HUB.job({
          title: label + ' · ' + title,
          endpoint: endpoint,
          payload: payloadOf(ids),
          confirm: confirmText(ids.length),
          timeout: timeout,
          refresh: !afterOk,
          channels: [ch],
          after: afterOk ? null : HUB.closeWO
        });
        if (afterOk) p.then(afterOk);
      });
    }
    wire('[data-batch-checkin]', '批量签到', '/api/accounts/checkin', function () { return { channel: ch }; },
      function (n) { return '对 ' + n + ' 个账号执行真实签到？成功后立即回读积分余额（不强制续期）。'; }, 120000,
      function (r) { return afterCheckin(r, ch); });
    var batchByChannel = HUB.$('#woBody').querySelector('[data-batch-checkin-channel]');
    if (batchByChannel) batchByChannel.addEventListener('click', function () {
      HUB.job({
        title: '批量签到 / ' + ch,
        endpoint: '/api/accounts/checkin',
        payload: { channel: ch },
        confirm: '对 ' + ch + ' 全部账号执行真实签到？成功后立即回读积分余额（不强制续期）。',
        timeout: 120000,
        refresh: false,
        channels: [ch]
      }).then(function (r) { return afterCheckin(r, ch); });
    });
    wire('[data-batch-inspect]', '全量探针', '/api/accounts/inspect', function (ids) { return { ids: ids, live: true, force: false }; },
      function (n) { return '对 ' + n + ' 个账号执行「续期 → 探活 → 积分」全链体检？会打上游，耗时较长。'; }, 180000);

    Array.from(HUB.$('#woBody').querySelectorAll('[data-wo-checkin]')).forEach(function (b) {
      b.addEventListener('click', function () {
        var id = b.getAttribute('data-wo-checkin');
        HUB.job({
          title: '账号签到 / #' + id,
          endpoint: '/api/accounts/checkin',
          payload: { account_id: Number(id) },
          confirm: '对账号 #' + id + ' 执行真实签到？成功后立即回读积分余额（不强制续期）。',
          timeout: 60000,
          refresh: false,
          channels: [ch]
        }).then(function (r) { return afterCheckin(r, ch); });
      });
    });
    Array.from(HUB.$('#woBody').querySelectorAll('[data-wo-inspect]')).forEach(function (b) {
      b.addEventListener('click', function () {
        var id = b.getAttribute('data-wo-inspect');
        HUB.job({
          title: '账号体检 / #' + id,
          endpoint: '/api/accounts/inspect',
          payload: { ids: [Number(id)], live: true, force: true },
          confirm: '对账号 #' + id + ' 执行强制续期、探活与积分快照？',
          timeout: 90000,
          channels: [ch],
          after: HUB.closeWO
        });
      });
    });
    Array.from(HUB.$('#woBody').querySelectorAll('[data-wo-delete]')).forEach(function (b) {
      b.addEventListener('click', function () {
        var id = b.getAttribute('data-wo-delete');
        HUB.job({
          title: '删除账号 / #' + id,
          endpoint: '/api/accounts/delete',
          payload: { id: Number(id), channel: ch },
          confirm: '删除账号 #' + id + '？只删本地这一行，不碰官方会话与保险仓凭据。',
          local: true,
          refresh: false,
          timeout: 30000,
          channels: [ch]
        }).then(function (res) {
          var body = (res && res.results && res.results[0] && res.results[0].body) || {};
          if (body && body.ok) { HUB.toast('账号已删除 · #' + id); reopenAfterWrite(); }
        });
      });
    });
    Array.from(HUB.$('#woBody').querySelectorAll('[data-wo-visibility]')).forEach(function (b) {
      b.addEventListener('click', function () {
        var chn = b.getAttribute('data-wo-visibility');
        var hidden = b.getAttribute('data-wo-hidden') === '1';
        HUB.job({
          title: '可见性闸刀 / ' + chn,
          endpoint: '/api/visibility',
          payload: { action: 'toggle', channel: chn, hidden: hidden, reason: '面板机柜检修' },
          confirm: hidden ? '停用 ' + chn + '：三处同步隐藏并停用模型，不删账号。'
            : '恢复 ' + chn + '：按可见性快照还原模型？',
          local: true,
          timeout: 60000,
          channels: [chn],
          after: HUB.closeWO
        });
      });
    });

    function fieldValue(sel) {
      var node = HUB.$('#woBody').querySelector(sel);
      return node ? String(node.value || '').trim() : '';
    }
    /* 工单刷新只重绘页面视图，抽屉在打开时已定型：写成功后重开同一张工单，
     * 让「未存凭据 / 无账号」这类回执跟着变，不然用户会以为没生效。 */
    function reopenAfterWrite() {
      if (!HUB.refresh) return;
      HUB.refresh().then(function () {
        if (HUB.$('#woBody')) openChannel(ch);
      });
    }
    var loginBtn = HUB.$('#woBody').querySelector('[data-vendor-login]');
    if (loginBtn) loginBtn.addEventListener('click', function () {
      var payload = { channel: ch };
      var token = fieldValue('[data-vendor-token]');
      var user = fieldValue('[data-vendor-username]');
      var pass = fieldValue('[data-vendor-password]');
      var browser = HUB.$('#woBody').querySelector('[data-vendor-browser]');
      if (token) payload.token = token;
      if (user) payload.username = user;
      if (pass) payload.password = pass;
      if (browser && browser.checked) payload.from_browser = true;
      if (!token && !(user && pass) && !(browser && browser.checked)) {
        HUB.toast('至少填 Token / 用户名+密码 / 勾选浏览器会话');
        return;
      }
      HUB.job({
        title: '登录即同步 / ' + ch,
        endpoint: '/api/official/vendor-login',
        payload: payload,
        local: true,
        refresh: false,
        timeout: 30000,
        channels: [ch]
      }).then(function (res) {
        var body = (res && res.results && res.results[0] && res.results[0].body) || {};
        if (body && body.ok) {
          var q = body.quota || {};
          var login = body.login || {};
          var prices = body.prices || {};
          var wins = q.windows || {};
          var filled = ['today', 'h5', 'week', 'month'].filter(function (k) { return wins[k]; }).length;
          HUB.toast('已登录并同步 · ' + (body.quota_label || '—')
            + (q.used_pct === null || q.used_pct === undefined ? '' : ' · ' + F.pct(q.used_pct, 1))
            + ' · 官方分窗 ' + filled + '/4'
            + (login.ok ? ' · 会话已换到' : '')
            + (prices.ok ? ' · 官方价目 ' + F.int(prices.count) + ' 行' : ''));
          if (!filled) {
            var miss = (q.windows_missing || [])[0];
            HUB.toast('官方分窗没取到 / ' + String((miss && miss.why) || '官方端点未返回窗口字段').slice(0, 90));
          }
          if (login && !login.ok && login.code && login.code !== 'no_credentials') {
            HUB.toast('登录态未拿到 / ' + String(login.error || login.code).slice(0, 70));
          }
          reopenAfterWrite();
        } else if (body && body.code === 'profile_unavailable') {
          HUB.toast('浏览器会话不可用 / ' + String(body.error || '').slice(0, 60));
        }
      });
    });

    var importBtn = HUB.$('#woBody').querySelector('[data-import-submit]');
    if (importBtn) importBtn.addEventListener('click', function () {
      var raw = fieldValue('[data-import-text]');
      if (!raw) { HUB.toast('先粘贴会话原文 / EMPTY'); return; }
      HUB.job({
        title: '原文导入 / ' + ch,
        endpoint: '/api/accounts/import_text',
        payload: {
          channel: ch,
          realm: fieldValue('[data-import-realm]') || 'default',
          name: fieldValue('[data-import-name]'),
          raw_text: raw
        },
        local: true,
        refresh: false,
        timeout: 30000,
        channels: [ch]
      }).then(function (res) {
        var body = (res && res.results && res.results[0] && res.results[0].body) || {};
        if (body && body.ok) {
          HUB.toast('账号已导入 · #' + body.id + ' ' + (body.token_masked || ''));
          reopenAfterWrite();
        }
      });
    });

    wireCustomBlock(cu);
    wireEgress(ch);
    wireOcBlock(ch);
  }

  function mountChannelActions() {
    var side = HUB.$('#v-channels .head-side');
    if (!side) return;
    if (!HUB.$('#ocBridgeBtn')) {
      var oc = document.createElement('button');
      oc.type = 'button';
      oc.id = 'ocBridgeBtn';
      oc.className = 'btn small';
      oc.textContent = 'OC 桥';
      oc.title = 'OC 桥令牌、专属 KEY 与双腿探活';
      oc.addEventListener('click', openOcBridge);
      side.appendChild(oc);
    }
    if (HUB.$('#addChannelBtn')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'addChannelBtn';
    btn.className = 'btn small filled';
    btn.textContent = '+ 接入渠道';
    btn.addEventListener('click', function () { openCustomForm(null); });
    side.appendChild(btn);
  }

  function render() {
    mountChannelActions();
    renderStrip();
    renderGrid();
  }
  HUB.views.channels = render;

  document.addEventListener('click', function (e) {
    if (e.target.closest('#coldShelfToggle')) {
      e.stopPropagation();
      shelfOpen = !shelfOpen;
      renderGrid();
      return;
    }
    var knife = e.target.closest('.fx-knife');
    if (knife) {
      e.stopPropagation();
      var cab = knife.closest('.fx-cabinet');
      if (!cab) return;
      var ch = cab.getAttribute('data-channel');
      var vis = visChannels().find(function (x) { return x.channel === ch; });
      var hide = !(vis && vis.hidden);
      HUB.job({
        title: '可见性闸刀 / ' + ch,
        endpoint: '/api/visibility',
        payload: { action: 'toggle', channel: ch, hidden: hide, reason: '面板机柜闸刀' },
        confirm: hide ? '停用 ' + ch + '：三处同步摘除，不删账号。' : '恢复 ' + ch + ' 为可见？',
        local: true,
        timeout: 60000,
        channels: [ch]
      });
      return;
    }
    var cold = e.target.closest('.fx-cold-cab');
    if (cold) {
      openChannel(cold.getAttribute('data-cold-cabinet'));
      return;
    }
    var cab2 = e.target.closest('.fx-cabinet');
    if (cab2) openChannel(cab2.getAttribute('data-channel'));
  });
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('.fx-cabinet')) {
      e.preventDefault();
      openChannel(e.target.getAttribute('data-channel'));
    }
  });
  document.addEventListener('DOMContentLoaded', render);
  if (document.readyState !== 'loading') render();
})();
