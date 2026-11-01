/* HUBNG 融合适配版 · 壳层与重机容器
 * 工单抽屉（容器二）、冲压作业票（容器三）、共用映射与格式化。
 */
(function () {
  'use strict';
  var HUB = window.HUB;
  var $ = HUB.$, $$ = HUB.$$, esc = HUB.fmt.esc;
  HUB.views = HUB.views || {};

  var MODE_LABEL = {
    live: ['LIVE / WRITE-CONFIRM', '实时 · 写需确认'],
    degraded: ['PARTIAL / READ MODEL', '部分端点超时，保留已取回读数'],
    snapshot: ['NO READINGS / PENDING', '读数未落地 · 只显示占位'],
    locked: ['AUTH REQUIRED', '需登录 · 不展示读数']
  };

  function stamp(text, cls) {
    return '<span class="stamp ' + (cls || '') + '">' + esc(text) + '</span>';
  }
  HUB.stamp = stamp;

  function shortChannel(ch) {
    var s = String(ch || '');
    if (s === 'workbuddy-intl') return 'WB-INTL';
    if (s === 'workbuddy-cn') return 'WB-CN';
    if (s.indexOf('custom:') === 0) return s.slice(7);
    if (s === 'custom') return 'CUSTOM';
    return s || '—';
  }
  function channelLabel(ch, data) {
    var vis = (data && data.visibility && data.visibility.channels) || [];
    var hit = vis.find(function (x) { return x.channel === ch; });
    if (hit && hit.label) return hit.label;
    var chs = (data && data.channels && data.channels.channels) || [];
    var c = chs.find(function (x) { return x.channel === ch; });
    if (c && c.display) return c.display;
    if (ch && ch.indexOf('custom:') === 0) return '第三方 · ' + ch.slice(7);
    return ch || '—';
  }
  function realmOf(ch) {
    var s = String(ch || '');
    if (s === 'workbuddy-intl' || s === 'traeintl') return 'intl';
    if (s === 'workbuddy-cn' || s === 'trae' || s === 'qoder' || s === 'qwen') return 'cn';
    if (s.indexOf('custom') === 0) return 'custom';
    return '—';
  }
  function kindOf(ch) {
    var s = String(ch || '');
    if (s === 'workbuddy-intl' || s === 'workbuddy-cn') return 'INFO-JSON';
    if (s.indexOf('custom:') === 0) return 'CUSTOM · OPENAI-COMPAT';
    if (s === 'trae' || s === 'traeintl') return 'TRAE-STORAGE';
    if (s === 'qoder') return 'CLI';
    if (s === 'qwen') return 'HTTP';
    return s || '—';
  }
  HUB.shortChannel = shortChannel;
  HUB.channelLabel = channelLabel;
  HUB.realmOf = realmOf;
  HUB.kindOf = kindOf;

  function mtokRow(model, data) {
    var rows = (data && data.mtok) || [];
    var ch = model.channel || '';
    var id = model.id || '';
    if (ch.indexOf('custom:') === 0) {
      // 价目表落盘时已把 custom:foo 归一成 foo；也兼容旧接口返回的 channel=custom。
      var bare = ch.slice(7);
      var suffix = '/' + id;
      return rows.find(function (r) {
        var sameChannel = r.channel === bare || r.channel === ch || r.channel === 'custom';
        var sameModel = r.model === id || String(r.model || '').slice(-suffix.length) === suffix;
        return sameChannel && sameModel;
      }) || null;
    }
    return rows.find(function (r) {
      return r.channel === ch && (r.model === id || r.model === ch + '/' + id);
    }) || null;
  }
  function priceLabel(model, data) {
    var ch = model.channel || '';
    if (ch.indexOf('custom:') === 0) {
      var row = mtokRow(model, data);
      if (!row) return '—';
      var cny = Number(row.in_cny);
      if (!isFinite(cny)) return '—';
      if (cny === 0) return 'FREE';
      return '¥' + cny.toFixed(3) + '/M';
    }
    if (model.free === true) return 'FREE';
    // 决策模型按输入 token 计费；out=0 不是报价，别把 0 写进价格区间。
    if (model.billing_basis === 'input_only' && model.rate && typeof model.rate === 'object') {
      var onlyIn = Number(model.rate.in);
      if (!isFinite(onlyIn)) return '—';
      return onlyIn === 0 ? 'FREE' : '¥' + onlyIn.toFixed(3) + '/M IN';
    }
    // 本地账本 / 云端 readmodel 可能把 rate 下发为 {in,cache,out} 或数字。
    // 这里先归一成可读价格，避免价格格直接打印 [object Object]。
    if (model.rate && typeof model.rate === 'object') {
      var input = Number(model.rate.in);
      var output = Number(model.rate.out);
      if (!isFinite(input)) return '—';
      if (input === 0 && (!isFinite(output) || output === 0)) return 'FREE';
      if (isFinite(output) && output !== input) {
        return '¥' + input.toFixed(3) + '–' + output.toFixed(3) + '/M';
      }
      return '¥' + input.toFixed(3) + '/M';
    }
    // 云端 readmodel 可能把 rate 下发为数字（如 0.1），这里统一成字符串，
    // 避免 priceRange() 对数字调用 indexOf 崩溃。
    if (typeof model.rate === 'number') {
      return model.rate === 0 ? 'FREE' : 'x' + model.rate;
    }
    if (String(model.rate) === 'x0' || String(model.rate) === 'x0.00') return 'FREE';
    return model.rate || '—';
  }
  function priceValue(model, data) {
    var ch = model.channel || '';
    if (ch.indexOf('custom:') === 0) {
      var row = mtokRow(model, data);
      if (!row || row.in_cny === null || row.in_cny === undefined) return null;
      var cny = Number(row.in_cny);
      return isFinite(cny) ? cny : null;
    }
    if (model.rate && typeof model.rate === 'object') {
      var input = Number(model.rate.in);
      return isFinite(input) ? input : null;
    }
    if (typeof model.rate === 'number') return isFinite(model.rate) ? model.rate : null;
    var m = String(model.rate || '').match(/x([\d.]+)/);
    return m ? Number(m[1]) : null;
  }
  HUB.mtokRow = mtokRow;
  HUB.priceLabel = priceLabel;
  HUB.priceValue = priceValue;

  /* 具体三档价：卡片要显示输入/缓存/输出各自的数，不显示 ¥a–b 区间。
   * 优先用目录里的 rate（已是人民币/百万 token），没有价就是 null（不猜）。 */
  function money3(v) {
    /* 价目表按人民币/百万 token 落盘；设置里切到美元时同口径换算，
     * 不另存第二份价目，也不改账本金额。 */
    if (v === null || v === undefined) return '—';
    var n = Number(v);
    if (!isFinite(n)) return '—';
    var F = HUB.fmt || {};
    if (F.unitMode && F.unitMode() === 'USD' && F.usdCny) n = n / F.usdCny();
    if (n === 0) return '0';
    return n < 0.1 ? n.toFixed(4) : n.toFixed(3);
  }
  function priceParts(model, data) {
    var list = (model && model.models && model.models.length) ? model.models : [model];
    var best = null;
    list.forEach(function (m) {
      var rate = (m && m.rate && typeof m.rate === 'object') ? m.rate : null;
      if (!rate) return;
      var input = Number(rate.in);
      if (!isFinite(input)) return;
      var cache = Number(rate.cache);
      var output = Number(rate.out);
      var entry = {
        in: input,
        cache: isFinite(cache) ? cache : null,
        out: isFinite(output) ? output : null,
        checkedAt: (m && (m.price_checked_at || (rate && rate.checked_at))) || '',
        channel: (m && m.channel) || rate.channel || '',
        free: input === 0 && (!isFinite(output) || output === 0)
      };
      if (!best || entry.in < best.in) best = entry;
    });
    return best;
  }
  HUB.priceParts = priceParts;
  HUB.money3 = money3;

  /* ── 决策模型元数据（03 / 02 / 05 共用）──────────────────────────
   * usage 行只带 model/channel；端点与 input-only 口径取自同一份
   * readmodel/catalog，按 model + channel 精确匹配，不做跨渠道猜测。
   */
  function catalogMeta(row) {
    var data = (HUB.data && HUB.data.state && HUB.data.state.data) || {};
    var list = (data.catalog && data.catalog.models) || [];
    var id = String((row && row.model) || '');
    var channel = String((row && row.channel) || '');
    var exact = list.find(function (m) {
      return String(m.id) === id && String(m.channel) === channel;
    });
    if (exact) return exact;
    if (channel) return null;
    return list.find(function (m) { return String(m.id) === id; }) || null;
  }
  function decisionInfo(row) {
    row = row || {};
    var meta = catalogMeta(row);
    var billing = row.billing || {};
    var inputOnly = billing.basis === 'input_only'
      || billing.out_source === 'official_input_only'
      || !!(meta && meta.billing_basis === 'input_only');
    return {
      decision: !!(meta && meta.decision),
      inputOnly: inputOnly,
      endpoint: (meta && meta.endpoint) || (meta && meta.decision ? '/v1/systemone' : ''),
      basis: inputOnly ? 'input_only' : (billing.basis || 'token')
    };
  }
  HUB.catalogMeta = catalogMeta;
  HUB.decisionInfo = decisionInfo;

  function fmtRange(secs) {
    if (!secs && secs !== 0) return '—';
    if (secs >= 3600) return (secs / 3600).toFixed(1) + 'h';
    if (secs >= 60) return Math.round(secs / 60) + 'm';
    return Math.round(secs) + 's';
  }
  HUB.fmtRange = fmtRange;

  /* ── 工单抽屉 ─────────────────────────────────────────────────────── */
  var wo, woScrim, woTitle, woKicker, woMeta, woBody, woFoot, lastFocus = null;
  function openWO(opts) {
    opts = opts || {};
    if (!wo) return;
    lastFocus = document.activeElement;
    woKicker.textContent = opts.kicker || 'WORK ORDER / 检修工单';
    woTitle.textContent = opts.title || '检修抽屉';
    woMeta.textContent = opts.meta || ('WO-20261024-' + String(Math.floor(Math.random() * 9000) + 1000));
    woBody.innerHTML = opts.body || '';
    woFoot.innerHTML = '';
    (opts.actions || []).forEach(function (action) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn ' + (action.filled ? 'filled' : '');
      b.textContent = action.label;
      b.addEventListener('click', function () {
        if (action.onClick) action.onClick();
        else if (action.close !== false) closeWO();
      });
      woFoot.appendChild(b);
    });
    if (!opts.actions || !opts.actions.length) {
      var close = document.createElement('button');
      close.type = 'button';
      close.className = 'btn';
      close.textContent = '收起工单';
      close.addEventListener('click', closeWO);
      woFoot.appendChild(close);
    }
    wo.inert = false;
    wo.classList.add('is-open');
    woScrim.classList.add('is-open');
    wo.setAttribute('aria-hidden', 'false');
    document.documentElement.style.overflow = 'hidden';
    woBody.scrollTop = 0;
    setTimeout(function () { var f = wo.querySelector('button, input, select'); if (f) f.focus({ preventScroll: true }); }, 30);
  }
  function closeWO() {
    if (!wo) return;
    /* 收轨前先移走抽屉内的焦点，否则 aria-hidden 会作用在聚焦元素上。 */
    if (wo.contains(document.activeElement) && document.activeElement.blur) document.activeElement.blur();
    if ('inert' in wo) wo.inert = true;
    wo.classList.remove('is-open');
    woScrim.classList.remove('is-open');
    wo.setAttribute('aria-hidden', 'true');
    document.documentElement.style.overflow = '';
    if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
  }
  HUB.openWO = openWO;
  HUB.closeWO = closeWO;

  /* ── 冲压作业票 ─────────────────────────────────────────────────────
   * 生命周期：RUNNING 只是执行占位，任务结束（成功或失败）立即撤掉；
   * 最终回执留足阅读时间，悬停时暂停倒计时，右上角 × 可手动收起。
   */
  var ticketSeq = 0;
  var TICKET_LIMIT = 4;
  var RECEIPT_TTL = 24000;
  var HOVER_GRACE = 6000;

  function ticket(opts) {
    opts = opts || {};
    var host = $('#tickets');
    if (!host) return null;
    ticketSeq += 1;
    var el = document.createElement('div');
    el.className = 'fx-ticket' + (opts.tone === 'pass' ? ' is-pass' : '') + (opts.tone === 'skip' ? ' is-skip' : '');
    var code = opts.code || ('WO-' + new Date().toISOString().slice(0, 10).replace(/-/g, '') + '-' + String(ticketSeq).padStart(3, '0'));
    var status = opts.status || 'ARMED';
    var noteText = opts.note || '作业票已登记。';
    el.innerHTML = '<div class="fx-ticket-top"><div><b>' + esc(opts.title || '作业票') + '</b>'
      + '<div class="fx-ticket-code">' + esc(code) + '</div></div>'
      + '<div class="fx-ticket-acts"><span class="stamp">' + esc(status) + '</span>'
      + '<button type="button" class="fx-ticket-x" aria-label="收起回执" title="收起回执">×</button></div></div>'
      + '<div class="fx-ticket-note">' + esc(noteText) + '</div>'
      + '<div class="fx-ticket-dots"><i class="on"></i><i class="on"></i><i class="' + (opts.tone === 'pass' ? 'on' : '') + '"></i><i class="' + (opts.tone === 'pass' ? 'on' : '') + '"></i></div>';
    var ttl = opts.ttl || 5200;
    var timer = 0;
    var deadline = 0;
    var remaining = ttl;
    var closed = false;
    function hold() {
      if (timer) {
        clearTimeout(timer);
        timer = 0;
        remaining = Math.max(0, deadline - Date.now());
      }
    }
    function dismiss(instant) {
      hold();
      if (closed) return;
      closed = true;
      if (instant || !el.isConnected) { el.remove(); return; }
      el.style.transition = 'opacity 160ms steps(2,end)';
      el.style.opacity = '0';
      setTimeout(function () { el.remove(); }, 180);
    }
    function schedule(ms) {
      hold();
      remaining = ms;
      deadline = Date.now() + ms;
      timer = setTimeout(function () {
        /* 页面在后台时不吞回执：回到前台后重新计满一轮。 */
        if (document.hidden) { schedule(ms); return; }
        dismiss(false);
      }, ms);
    }
    el.addEventListener('mouseenter', hold);
    el.addEventListener('mouseleave', function () { schedule(Math.max(HOVER_GRACE, remaining)); });
    el.querySelector('.fx-ticket-x').addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      dismiss(false);
    });
    host.appendChild(el);
    if (['PASSED', 'PARTIAL', 'FAILED', 'SKIPPED'].indexOf(status) >= 0) {
      receiptArchive({ code: code, title: opts.title || '作业票', status: status, note: noteText, channels: normChannels(opts.channels || opts.channel), at: Math.floor(Date.now() / 1000) });
    }
    while (host.children.length > TICKET_LIMIT) host.removeChild(host.firstElementChild);
    schedule(ttl);
    return { el: el, dismiss: dismiss };
  }
  HUB.ticket = ticket;

  /* ── 回执档案：最终票留档到本次浏览会话 ─────────────────────────────
   * 24s 只是视觉退场；历史抽屉直接读 sessionStorage，刷新当前标签页也不丢。
   * 只收最终态（PASSED / PARTIAL / FAILED / SKIPPED），RUNNING/ARMED 不入档。
   */
  var RECEIPT_KEY = 'hub.receipts.v1';
  var RECEIPT_MAX = 40;
  var receiptSeq = 0;
  var receiptLog = receiptLoad();
  var receiptBtn = null, receiptCount = null;
  /* 勾选状态只活在当前抽屉会期里，按稳定 id 记录；换筛选不丢选择。 */
  var receiptSelection = new Set();

  function receiptUid() {
    receiptSeq += 1;
    return 'r' + Date.now().toString(36) + '-' + receiptSeq.toString(36);
  }
  function receiptLoad() {
    try {
      var raw = window.sessionStorage.getItem(RECEIPT_KEY);
      var list = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(list)) return [];
      return list.filter(function (r) {
        return r && r.code && r.title && r.status && r.at;
      }).slice(0, RECEIPT_MAX).map(function (r) {
        r.channels = normChannels(r.channels);
        if (!r.id) r.id = receiptUid();
        return r;
      });
    } catch (e) { return []; }
  }
  function receiptSave() {
    try { window.sessionStorage.setItem(RECEIPT_KEY, JSON.stringify(receiptLog)); } catch (e) {}
  }
  function receiptPruneSelection() {
    if (!receiptSelection.size) return;
    var alive = new Set(receiptLog.map(function (r) { return r.id; }));
    Array.from(receiptSelection).forEach(function (id) {
      if (!alive.has(id)) receiptSelection.delete(id);
    });
  }
  function receiptCountPaint() {
    if (!receiptBtn || !receiptCount) return;
    receiptCount.textContent = String(receiptLog.length);
    receiptBtn.title = receiptLog.length
      ? '回执历史 · 当前会话 ' + receiptLog.length + ' 条'
      : '回执历史 · 暂无记录';
  }
  function receiptArchive(rec) {
    rec.id = rec.id || receiptUid();
    receiptLog.unshift(rec);
    if (receiptLog.length > RECEIPT_MAX) receiptLog.length = RECEIPT_MAX;
    receiptPruneSelection();
    receiptSave();
    receiptCountPaint();
  }
  var arcFilter = { status: 'ALL', channel: 'ALL', q: '' };
  var ARC_STATUSES = ['PASSED', 'PARTIAL', 'FAILED', 'SKIPPED'];

  function normChannels(value) {
    if (!value) return [];
    var list = Array.isArray(value) ? value : [value];
    var seen = {};
    return list.filter(Boolean).map(function (ch) { return String(ch); }).filter(function (ch) {
      if (seen[ch]) return false;
      seen[ch] = 1;
      return true;
    });
  }
  function receiptStatusStamp(status) {
    return '<span class="stamp' + (status === 'PASSED' ? '' : ' hollow') + '">' + esc(status) + '</span>';
  }
  function receiptChannelsOf(r) { return normChannels(r && r.channels); }
  function receiptChannelLabel(ch) { return HUB.channelLabel(ch, HUB.data.state.data || {}); }
  function receiptChannelKeys() {
    var seen = {}, out = [];
    receiptLog.forEach(function (r) {
      receiptChannelsOf(r).forEach(function (ch) {
        if (!seen[ch]) { seen[ch] = 1; out.push(ch); }
      });
    });
    return out.sort(function (a, b) { return receiptChannelLabel(a).localeCompare(receiptChannelLabel(b), 'zh'); });
  }
  function receiptRowHTML(r) {
    var chs = receiptChannelsOf(r).map(receiptChannelLabel).join(' · ');
    var picked = receiptSelection.has(r.id);
    return '<article class="fx-arc is-' + esc(String(r.status || '').toLowerCase()) + (picked ? ' is-picked' : '') + '" data-arc-id="' + esc(r.id) + '">'
      + '<label class="fx-arc-pick" title="选择这条回执"><input type="checkbox" data-arc-select="' + esc(r.id) + '"' + (picked ? ' checked' : '') + ' aria-label="选择回执 ' + esc(r.code) + '"><span aria-hidden="true"></span></label>'
      + '<div class="fx-arc-main"><div class="fx-arc-top">' + receiptStatusStamp(r.status)
      + '<b>' + esc(r.title) + '</b><time>' + esc(HUB.fmt.clock(r.at)) + '</time></div>'
      + '<div class="fx-arc-code">' + esc(r.code) + ' · ' + esc(chs || '全局') + '</div>'
      + '<p class="fx-arc-note">' + esc(r.note) + '</p></div></article>';
  }
  function receiptEmptyHTML() {
    return '<div class="fx-arc-empty">当前会话暂无作业回执。<br>签到、模型启停、测速、清理冷却或导出记录将在此保存。</div>';
  }
  function receiptNoMatchHTML() {
    return '<div class="fx-arc-empty">暂无符合条件的回执。<br>请调整状态、渠道或关键字筛选。</div>';
  }
  function receiptHeadHTML() {
    return '<div class="fx-arc-head"><span>SESSION ARCHIVE / 当前会话</span><span>' + receiptLog.length + ' / ' + RECEIPT_MAX + '</span></div>';
  }
  function receiptStatusOptions() {
    return '<option value="ALL">全部状态</option>' + ARC_STATUSES.map(function (st) {
      var n = receiptLog.filter(function (r) { return r.status === st; }).length;
      return '<option value="' + st + '">' + st + ' (' + n + ')</option>';
    }).join('');
  }
  function receiptChannelOptions() {
    var html = '<option value="ALL">全部渠道</option>' + receiptChannelKeys().map(function (ch) {
      return '<option value="' + esc(ch) + '">' + esc(receiptChannelLabel(ch)) + '</option>';
    }).join('');
    if (receiptLog.some(function (r) { return !receiptChannelsOf(r).length; })) {
      html += '<option value="__none__">未标注 / 全局</option>';
    }
    return html;
  }
  function receiptToolsHTML() {
    return '<div class="fx-arc-tools">'
      + '<label>STATUS / 状态<select id="arcStatus">' + receiptStatusOptions() + '</select></label>'
      + '<label>CHANNEL / 渠道<select id="arcChannel">' + receiptChannelOptions() + '</select></label>'
      + '<label class="fx-arc-q">SEARCH / 关键字<input id="arcQuery" type="search" autocomplete="off" placeholder="标题 / 工单号 / 回执 / 渠道"></label>'
      + '<div class="fx-arc-shown"><span>SHOWING <b id="arcShown"></b></span><button class="btn small" type="button" id="arcReset">清除筛选</button></div>'
      + '<div class="fx-arc-manage">'
      + '<div class="fx-arc-pickset"><label class="fx-arc-pickall"><input type="checkbox" id="arcPickAll"><span>全选当前</span></label>'
      + '<span class="fx-arc-selinfo" id="arcSelInfo">已选 0</span>'
      + '<button class="btn small" type="button" id="arcSelClear" disabled>清除选择</button></div>'
      + '<div class="fx-arc-purge"><button class="btn small" type="button" id="arcPurgeSelected" disabled>删除选中 (0)</button>'
      + '<button class="btn small" type="button" id="arcPurge">删除当前筛选 (0)</button>'
      + '<button class="btn small" type="button" id="arcClear">清空全部 (0)</button></div></div>'
      + '</div>';
  }
  function receiptFilterSummary() {
    var parts = [];
    if (arcFilter.status !== 'ALL') parts.push(arcFilter.status);
    if (arcFilter.channel === '__none__') parts.push('未标注 / 全局');
    else if (arcFilter.channel !== 'ALL') parts.push(receiptChannelLabel(arcFilter.channel));
    if (arcFilter.q) parts.push('关键字 "' + arcFilter.q + '"');
    return parts.length ? parts.join(' · ') : '全部回执';
  }
  function receiptMatches(r) {
    if (arcFilter.status !== 'ALL' && r.status !== arcFilter.status) return false;
    var chs = receiptChannelsOf(r);
    if (arcFilter.channel === '__none__') {
      if (chs.length) return false;
    } else if (arcFilter.channel !== 'ALL' && chs.indexOf(arcFilter.channel) < 0) {
      return false;
    }
    var tokens = String(arcFilter.q || '').toLowerCase().split(/\s+/).filter(Boolean);
    if (!tokens.length) return true;
    var labels = chs.length ? chs.map(receiptChannelLabel) : ['全局'];
    var hay = [r.title, r.note, r.code, r.status].concat(chs, labels).join(' ').toLowerCase();
    return tokens.every(function (t) { return hay.indexOf(t) >= 0; });
  }
  function receiptExportRows() {
    return receiptLog.filter(receiptMatches).map(function (r) {
      var chs = receiptChannelsOf(r);
      var epoch = Number(r.at);
      var ms = epoch < 1e12 ? epoch * 1000 : epoch;
      var iso = '';
      if (isFinite(ms)) {
        try { iso = new Date(ms).toISOString(); } catch (e) { iso = ''; }
      }
      return {
        at: epoch,
        at_iso: iso,
        status: r.status,
        title: r.title,
        code: r.code,
        channels: chs,
        channel_labels: chs.length ? chs.map(receiptChannelLabel) : ['全局'],
        note: r.note
      };
    });
  }
  function csvCell(v) {
    return '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  }
  function fileStamp() {
    /* 档案时间轴统一用上海时区，导出文件名必须与抽屉里的时钟读数一致，
     * 不跟随工作站本地时区漂移。 */
    var parts = {};
    try {
      new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
      }).formatToParts(new Date()).forEach(function (p) { parts[p.type] = p.value; });
    } catch (e) {}
    function p(n) { return String(n).padStart(2, '0'); }
    if (!parts.year) {
      var d = new Date();
      return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
    }
    return parts.year + parts.month + parts.day + '-' + parts.hour + parts.minute + parts.second;
  }
  function downloadBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  function receiptExport(format) {
    var rows = receiptExportRows();
    if (!rows.length) { HUB.toast('当前筛选没有可导出的回执 / EMPTY'); return; }
    var stamp = fileStamp();
    if (format === 'json') {
      var payload = {
        exported_at: new Date().toISOString(),
        filter: { status: arcFilter.status, channel: arcFilter.channel, q: arcFilter.q },
        count: rows.length,
        receipts: rows
      };
      downloadBlob(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' }), 'hub-receipts-' + stamp + '.json');
    } else {
      var cols = ['at', 'at_iso', 'status', 'title', 'code', 'channels', 'channel_labels', 'note'];
      var lines = [cols.map(csvCell).join(',')].concat(rows.map(function (r) {
        return cols.map(function (k) {
          var v = Array.isArray(r[k]) ? r[k].join('|') : r[k];
          return csvCell(v);
        }).join(',');
      }));
      downloadBlob(new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }), 'hub-receipts-' + stamp + '.csv');
    }
    HUB.toast('已导出 ' + rows.length + ' 条回执 / ' + String(format).toUpperCase());
  }
  function openReceipts() {
    var body = receiptLog.length
      ? receiptHeadHTML() + receiptToolsHTML()
        + '<div class="fx-arc-confirm" id="arcConfirm" aria-live="polite" hidden></div>'
        + '<div id="arcList"></div>'
      : receiptHeadHTML() + receiptEmptyHTML();
    HUB.openWO({
      kicker: 'RECEIPT ARCHIVE / 作业票档案',
      title: '回执历史',
      meta: 'SESSION ' + receiptLog.length + ' / ' + RECEIPT_MAX + ' · 只存本次浏览会话',
      body: '<div class="on-steel">' + body + '</div>',
      actions: receiptLog.length ? [
        { label: '导出 CSV', close: false, onClick: function () { receiptExport('csv'); } },
        { label: '导出 JSON', close: false, onClick: function () { receiptExport('json'); } },
        { label: '收起档案', filled: true }
      ] : [{ label: '收起档案', filled: true }]
    });
    if (!receiptLog.length) return;
    var statusSel = HUB.$('#arcStatus');
    var channelSel = HUB.$('#arcChannel');
    var queryEl = HUB.$('#arcQuery');
    var listEl = HUB.$('#arcList');
    var shownEl = HUB.$('#arcShown');
    var resetBtn = HUB.$('#arcReset');
    var purgeBtn = HUB.$('#arcPurge');
    var clearBtn = HUB.$('#arcClear');
    var purgeSelectedBtn = HUB.$('#arcPurgeSelected');
    var selClearBtn = HUB.$('#arcSelClear');
    var pickAll = HUB.$('#arcPickAll');
    var selInfo = HUB.$('#arcSelInfo');
    var confirmEl = HUB.$('#arcConfirm');
    var locked = false;
    if (ARC_STATUSES.indexOf(arcFilter.status) < 0) arcFilter.status = 'ALL';
    if (arcFilter.channel !== 'ALL' && arcFilter.channel !== '__none__'
      && receiptChannelKeys().indexOf(arcFilter.channel) < 0) arcFilter.channel = 'ALL';
    statusSel.value = arcFilter.status;
    channelSel.value = arcFilter.channel;
    queryEl.value = arcFilter.q;
    receiptPruneSelection();
    function visibleRows() { return receiptLog.filter(receiptMatches); }
    function paintManage() {
      if (!pickAll) return;
      var rows = visibleRows();
      var visSel = rows.filter(function (r) { return receiptSelection.has(r.id); }).length;
      var totalSel = receiptSelection.size;
      pickAll.checked = rows.length > 0 && visSel === rows.length;
      pickAll.indeterminate = visSel > 0 && visSel < rows.length;
      pickAll.disabled = locked || !rows.length;
      selInfo.textContent = totalSel
        ? ('已选 ' + totalSel + (totalSel !== visSel ? ' · 当前 ' + visSel : ''))
        : '已选 0';
      selClearBtn.disabled = locked || !totalSel;
      purgeSelectedBtn.disabled = locked || !totalSel;
      purgeSelectedBtn.textContent = '删除选中 (' + totalSel + ')';
    }
    function lockFilters(on) {
      locked = !!on;
      [statusSel, channelSel, queryEl, resetBtn, purgeBtn, clearBtn, purgeSelectedBtn, selClearBtn, pickAll].forEach(function (el) {
        el.disabled = !!on;
      });
      if (!on) paintManage();
    }
    function cancelDelete(trigger) {
      confirmEl.hidden = true;
      confirmEl.innerHTML = '';
      lockFilters(false);
      paint();
      if (trigger) trigger.focus({ preventScroll: true });
    }
    function applyDelete(mode, targets) {
      /* 只删确认条里列出的那批；确认期间新完成的票据不应被顺手抹掉。 */
      var removed = targets.length;
      receiptLog = receiptLog.filter(function (r) { return targets.indexOf(r) < 0; });
      targets.forEach(function (r) { receiptSelection.delete(r.id); });
      receiptPruneSelection();
      if (!receiptLog.length) arcFilter = { status: 'ALL', channel: 'ALL', q: '' };
      receiptSave();
      receiptCountPaint();
      HUB.toast((mode === 'all' ? '已清空 ' : '已删除 ') + removed + ' 条回执 / 不可撤销');
      openReceipts();
    }
    function askDelete(mode, trigger) {
      var rows = visibleRows();
      var targets = mode === 'all' ? receiptLog.slice()
        : (mode === 'selected' ? receiptLog.filter(function (r) { return receiptSelection.has(r.id); }) : rows);
      var count = targets.length;
      if (!count) { HUB.toast(mode === 'selected' ? '没有选中的回执 / EMPTY' : '没有可删除的回执 / EMPTY'); return; }
      var offFilter = mode === 'selected' ? targets.filter(function (r) { return !receiptMatches(r); }).length : 0;
      var scope = mode === 'all'
        ? '将清空全部 <b>' + count + '</b> 条回执。'
        : mode === 'selected'
          ? '将删除已选 <b>' + count + '</b> 条回执（共 ' + receiptLog.length + ' 条）。' + (offFilter ? '<br>其中 <b>' + offFilter + '</b> 条不在当前筛选。' : '')
          : '将删除当前筛选的 <b>' + count + '</b> 条回执（共 ' + receiptLog.length + ' 条）。<br>条件：' + esc(receiptFilterSummary()) + '。';
      confirmEl.innerHTML = '<b>CONFIRM / 二次确认</b>'
        + '<p>' + scope + '<br>此操作不可撤销。</p>'
        + '<div class="fx-arc-confirm-acts">'
        + '<button class="btn small" type="button" id="arcConfirmNo">取消</button>'
        + '<button class="btn small filled" type="button" id="arcConfirmYes">' + (mode === 'all' ? '确认清空' : '确认删除') + '</button>'
        + '</div>';
      confirmEl.hidden = false;
      lockFilters(true);
      var yes = HUB.$('#arcConfirmYes');
      var no = HUB.$('#arcConfirmNo');
      no.addEventListener('click', function () { cancelDelete(trigger); });
      yes.addEventListener('click', function () { applyDelete(mode, targets); });
      /* 默认焦点落在取消，避免连按回车直接抹掉档案。 */
      no.focus({ preventScroll: true });
    }
    function paint() {
      var rows = visibleRows();
      listEl.innerHTML = rows.length ? rows.map(receiptRowHTML).join('') : receiptNoMatchHTML();
      shownEl.textContent = rows.length + ' / ' + receiptLog.length;
      resetBtn.disabled = arcFilter.status === 'ALL' && arcFilter.channel === 'ALL' && !arcFilter.q;
      purgeBtn.textContent = '删除当前筛选 (' + rows.length + ')';
      purgeBtn.disabled = !rows.length;
      clearBtn.textContent = '清空全部 (' + receiptLog.length + ')';
      paintManage();
    }
    statusSel.addEventListener('change', function () { arcFilter.status = statusSel.value; paint(); });
    channelSel.addEventListener('change', function () { arcFilter.channel = channelSel.value; paint(); });
    queryEl.addEventListener('input', function () { arcFilter.q = queryEl.value.trim(); paint(); });
    resetBtn.addEventListener('click', function () {
      arcFilter = { status: 'ALL', channel: 'ALL', q: '' };
      statusSel.value = 'ALL';
      channelSel.value = 'ALL';
      queryEl.value = '';
      paint();
    });
    listEl.addEventListener('change', function (e) {
      var box = e.target.closest('[data-arc-select]');
      if (!box) return;
      var id = box.getAttribute('data-arc-select');
      if (box.checked) receiptSelection.add(id); else receiptSelection.delete(id);
      var card = box.closest('.fx-arc');
      if (card) card.classList.toggle('is-picked', box.checked);
      paintManage();
    });
    pickAll.addEventListener('change', function () {
      var rows = visibleRows();
      rows.forEach(function (r) {
        if (pickAll.checked) receiptSelection.add(r.id); else receiptSelection.delete(r.id);
      });
      paint();
    });
    selClearBtn.addEventListener('click', function () {
      receiptSelection.clear();
      paint();
      selClearBtn.focus({ preventScroll: true });
    });
    purgeSelectedBtn.addEventListener('click', function () { askDelete('selected', purgeSelectedBtn); });
    purgeBtn.addEventListener('click', function () { askDelete('filter', purgeBtn); });
    clearBtn.addEventListener('click', function () { askDelete('all', clearBtn); });
    paint();
  }
  HUB.receipts = { open: openReceipts, list: function () { return receiptLog.slice(); } };

  function arm(label, detail) {
    ticket({
      title: label,
      note: detail || '快照档：未写入网关。',
      status: 'ARMED',
      tone: 'armed'
    });
    HUB.toast('已生成待执行作业票 / READ-ONLY');
  }
  HUB.arm = arm;

  /* ── 写工单执行层 ─────────────────────────────────────────────────────
   * 纪律：
   *   ① 只有 LIVE 档才发请求；SNAPSHOT / PARTIAL / LOCKED 一律落 ARMED 票；
   *   ② 每个工单先确认（确认文案由调用方给），不静默改网关；
   *   ③ 同一时刻只跑一张工单，避免连点造成并发写；
   *   ④ 回执只报端点原话（changed / passed / detail），失败不伪装成功。
   */
  var jobBusy = false;
  function detailText(detail) {
    if (!detail) return '';
    if (Array.isArray(detail)) {
      return detail.map(function (x) {
        if (!x || typeof x !== 'object') return String(x);
        var loc = Array.isArray(x.loc) ? x.loc.join('.') : '';
        return (loc ? loc + ' ' : '') + (x.msg || x.type || '校验失败');
      }).join('；').slice(0, 220);
    }
    if (typeof detail === 'object') return String(detail.message || detail.msg || JSON.stringify(detail)).slice(0, 220);
    return String(detail).slice(0, 220);
  }

  function replyText(payload) {
    if (!payload) return '端点未返回内容';
    if (payload.detail !== undefined && payload.detail !== null && payload.detail !== '') return detailText(payload.detail);
    if (payload.error) return detailText(payload.error.message || payload.error);
    /* 批量端点（签到/测速）的逐条失败要落进回执，不能只报「通过 0 / 3」。 */
    if (Array.isArray(payload.results) && payload.results.length) {
      var bad = payload.results.filter(function (r) { return r && r.ok === false; });
      if (bad.length) {
        var head = (payload.passed !== undefined && payload.total !== undefined)
          ? '通过 ' + payload.passed + ' / ' + payload.total + ' · ' : '';
        return head + bad.slice(0, 4).map(function (r) {
          return '#' + (r.id || r.model || r.channel || '?') + ' ' + (detailText(r.reason || r.msg || r.detail) || '失败');
        }).join('；');
      }
    }
    var bits = [];
    if (payload.saved) bits.push('已保存 ' + payload.saved);
    if (payload.released !== undefined) bits.push(payload.released ? '锁已解除' : '锁不存在');
    if (payload.changed !== undefined) bits.push('变更 ' + payload.changed);
    if (payload.passed !== undefined) bits.push('通过 ' + payload.passed + ' / ' + (payload.total || 0));
    if (payload.done !== undefined) bits.push('完成 ' + payload.done + ' / ' + (payload.total || 0));
    if (payload.skipped !== undefined && Number(payload.skipped)) bits.push('缓存 ' + payload.skipped);
    if (payload.total !== undefined && payload.passed === undefined && payload.changed === undefined && payload.done === undefined) bits.push('返回 ' + payload.total + ' 条');
    return bits.join(' · ') || '端点已确认';
  }

  /* 端点整体 200 不代表逐目标成功：passed<total 是部分失败，passed=0 是全失败。 */
  function stepTone(body) {
    if (!body || body.ok === false) return 'fail';
    if (body.total !== undefined && body.passed !== undefined && Number(body.total) > 0) {
      var p = Number(body.passed) || 0;
      if (p <= 0) return 'fail';
      if (p < Number(body.total)) return 'partial';
    }
    return 'ok';
  }

  function postJSON(step, timeout) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeout || 60000);
    var init = {
      method: step.method || 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
      headers: { 'accept': 'application/json', 'X-HUB-UI': '1' }
    };
    if (init.method !== 'GET' && step.payload !== undefined) {
      init.headers['content-type'] = 'application/json';
      init.body = JSON.stringify(step.payload);
    }
    return fetch(step.endpoint, init).then(function (res) {
      return res.text().then(function (text) {
        var body = null;
        try { body = text ? JSON.parse(text) : null; } catch (e) { body = { detail: text.slice(0, 240) }; }
        if (!res.ok) {
          var msg = (body && (body.detail !== undefined ? body.detail : (body.error && body.error.message))) || ('HTTP ' + res.status);
          throw new Error(detailText(msg) || ('HTTP ' + res.status));
        }
        return body;
      });
    }).finally(function () { clearTimeout(timer); });
  }

  function job(opts) {
    opts = opts || {};
    var steps = opts.steps || [{ endpoint: opts.endpoint, payload: opts.payload, method: opts.method }];
    steps = steps.filter(function (s) { return s && s.endpoint; });
    if (!steps.length) { HUB.toast('工单没有可执行步骤 / EMPTY'); return Promise.resolve({ ok: false, empty: true }); }
    var live = HUB.data && HUB.data.state && HUB.data.state.mode === 'live';
    var title = opts.title || '作业工单';
    /* 本地配置/内存锁写只依赖 8888 本地网关，不要求上游全量读模型处于 LIVE。 */
    if (!live && !opts.local) {
      arm(title, '快照档不执行写操作 · ' + steps.map(function (s) { return (s.method || 'POST') + ' ' + s.endpoint; }).join(' / '));
      return Promise.resolve({ ok: false, armed: true });
    }
    if (jobBusy) {
      HUB.toast('已有工单在执行 / BUSY');
      return Promise.resolve({ ok: false, busy: true });
    }
    if (opts.confirm && !window.confirm(opts.confirm)) {
      HUB.toast('工单已取消 / ABORTED');
      return Promise.resolve({ ok: false, cancelled: true });
    }
    jobBusy = true;
    var running = ticket({ title: title, note: '执行中 / RUNNING · ' + steps.length + ' STEP', status: 'RUNNING', ttl: 900000 });
    var timeout = opts.timeout || 90000;
    var seq = Promise.resolve([]);
    steps.forEach(function (step, i) {
      seq = seq.then(function (acc) {
        return postJSON(step, timeout).then(function (body) {
          var tone = stepTone(body);
          acc.push({ ok: tone !== 'fail', partial: tone === 'partial', body: body, label: step.label || String(i + 1) });
          return acc;
        }).catch(function (err) {
          /* 批量作业必须逐组落印：一组失败不能吞掉后续组的结果。 */
          acc.push({ ok: false, body: { detail: String((err && err.message) || err) }, label: step.label || String(i + 1) });
          return acc;
        });
      });
    });
    return seq.then(function (results) {
      if (running) running.dismiss(true);
      var failed = results.filter(function (r) { return !r.ok; });
      var partial = results.filter(function (r) { return r.partial; });
      var ok = failed.length === 0 && partial.length === 0;
      var anyOk = results.some(function (r) { return r.ok; });
      var changed = results.reduce(function (a, r) { return a + (Number(r.body && r.body.changed) || 0); }, 0);
      var passed = results.reduce(function (a, r) { return a + (Number(r.body && r.body.passed) || 0); }, 0);
      /* 端点回 0 个目标时不能落 PASSED：custom:* 测速就是典型空回执。 */
      var zero = results.length > 0 && results.every(function (r) {
        var b = r.body || {};
        if (b.total === undefined) return false;
        return Number(b.total) === 0 && Number(b.changed || 0) === 0 &&
          Number(b.done || 0) === 0 && Number(b.passed || 0) === 0 && Number(b.ran || 0) === 0;
      });
      var skipped = ok && zero;
      var detail = failed.length
        ? failed.map(function (r) { return (steps.length > 1 ? r.label + ': ' : '') + replyText(r.body); }).join(' ｜ ')
        : replyText(results[results.length - 1] && results[results.length - 1].body)
          + (changed ? ' · 合计变更 ' + changed : '') + (passed ? ' · 合计通过 ' + passed : '');
      var part = !skipped && !ok && (anyOk || partial.length > 0);
      ticket({
        title: title + (skipped ? ' / 空回执' : (ok ? ' / 回执' : (part ? ' / 部分未通过' : ' / 未通过'))),
        note: (skipped ? '端点返回 0 个目标，未执行任何动作 · ' : '') + detail + ' · ' + steps.length + ' STEP',
        status: skipped ? 'SKIPPED' : (ok ? 'PASSED' : (part ? 'PARTIAL' : 'FAILED')),
        tone: skipped ? 'skip' : (ok ? 'pass' : 'fail'),
        channels: normChannels(opts.channels || opts.channel),
        ttl: RECEIPT_TTL
      });
      HUB.toast(skipped ? '工单空回执 / SKIPPED' : (ok ? '工单完成 / PASSED' : (part ? '工单部分失败 / PARTIAL' : '工单失败 / FAILED')));
      if (anyOk && opts.refresh !== false && HUB.refresh) {
        HUB.refresh().then(function () {
          /* 只有全通过才收轨；部分失败保留抽屉，交给人处理。 */
          if (ok && opts.after) opts.after();
        });
      }
      return { ok: ok, results: results, skipped: skipped };
    }).catch(function (err) {
      if (running) running.dismiss(true);
      var msg = String((err && err.message) || err).slice(0, 220);
      ticket({ title: title + ' / 失败', note: msg, status: 'FAILED', tone: 'fail', ttl: RECEIPT_TTL });
      HUB.toast('工单失败 / ' + msg);
      return { ok: false, error: msg };
    }).finally(function () { jobBusy = false; });
  }
  HUB.job = job;
  HUB.freshOfficial = function () {
    return job({
      title: '复核官方口径 / FRESH',
      method: 'GET',
      endpoint: '/api/official/summary?days=1&fresh=1',
      confirm: '跳过 12 小时缓存，重新读取官方 calls？会真打上游，耗时且有限额。',
      timeout: 180000
    });
  };

  function copyText(text, okLabel) {
    var done = function () { HUB.toast(okLabel || '已复制'); };
    var fail = function () { HUB.toast('复制失败 / 手动选择'); };
    /* 非安全上下文 / 无用户手势时 Clipboard API 会被拒：退回 textarea + execCommand。 */
    var legacy = function () {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      if (ok) done(); else fail();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(legacy);
    } else {
      legacy();
    }
  }
  HUB.copyText = copyText;

  document.addEventListener('click', function (e) {
    var node = e.target.closest('[data-copy]');
    if (!node) return;
    e.preventDefault();
    copyText(node.getAttribute('data-copy'), 'CLI 已复制 / READY TO PASTE');
  });

  /* ── 上游告警与一键回退 ─────────────────────────────────────────── */
  var upstreamBusy = false;

  function upstreamAlertState() {
    var data = HUB.data.state.data || {};
    var health = data.health || {};
    var probe = health.upstream_health || null;
    if (!probe || probe.ok !== false) return null;
    var role = health.upstream_role || probe.role || 'cloud';
    return {
      probe: probe,
      role: role,
      target: role === 'local' ? 'cloud' : 'local',
      targetLabel: role === 'local' ? '云端 KSWA' : '本地 hub-ng'
    };
  }

  function paintUpstreamAlert() {
    var el = $('#upstreamAlert');
    if (!el) return;
    var st = upstreamAlertState();
    document.body.classList.toggle('has-upstream-alert', !!st);
    if (!st) {
      el.hidden = true;
      var idle = $('#upstreamAlertAction');
      if (idle) idle.disabled = upstreamBusy;
      var idleProbe = $('#upstreamAlertProbe');
      if (idleProbe) idleProbe.disabled = upstreamBusy;
      return;
    }
    var p = st.probe;
    el.hidden = false;
    var stamp = $('#upstreamAlertStamp');
    if (stamp) stamp.textContent = p.status === 'auth_error' ? 'UPSTREAM AUTH' : 'UPSTREAM DOWN';
    var title = $('#upstreamAlertTitle');
    if (title) title.textContent = st.role === 'local' ? '本地 hub-ng 不可达' : '云端上游不可达';
    var detail = $('#upstreamAlertDetail');
    if (detail) {
      detail.textContent = 'TARGET ' + (p.target || '—')
        + ' · ' + (p.http_status ? 'HTTP ' + p.http_status : String(p.status || 'DOWN').toUpperCase())
        + (p.error ? ' · ' + p.error : '') + ' · 探针间隔 ' + (p.interval_s || 30) + 's';
    }
    var time = $('#upstreamAlertTime');
    if (time) {
      time.textContent = 'CHECKED ' + (p.checked_at ? HUB.fmt.clock(p.checked_at) : '—')
        + ' · FAIL ' + (p.consecutive_failures || 0);
    }
    var probeBtn = $('#upstreamAlertProbe');
    if (probeBtn) probeBtn.disabled = upstreamBusy;
    var action = $('#upstreamAlertAction');
    if (action) {
      action.disabled = upstreamBusy;
      action.setAttribute('data-upstream-target', st.target);
      action.textContent = upstreamBusy
        ? '切换中…'
        : (st.target === 'local' ? '一键回退到本地 hub-ng' : '切回云端上游');
    }
  }

  /* ── 预算占用率预警条 ────────────────────────────────────────────
     与 /api/budgets 同一份数字：`over`=已被拦（402），`warn`=到 80% 先报。 */
  function budgetAlertState() {
    var d = HUB.data.state.data || {};
    var b = d.budgets || null;
    if (!b) return null;
    var rows = [];
    if (b.global) rows.push(Object.assign({ key: 'global' }, b.global));
    (b.items || []).forEach(function (it) { rows.push(it); });
    var full = rows.filter(function (r) { return r.over; });
    if (full.length) return { level: 'blocked', rows: full, warnPct: b.warn_pct };
    var near = rows.filter(function (r) { return r.warn; });
    return near.length ? { level: 'warn', rows: near, warnPct: b.warn_pct } : null;
  }

  function budgetMoney(value) {
    /* 额度可能是分级（¥0.00015），四位小数会把上限显示成 0.00 */
    var n = Number(value) || 0;
    return '\u00a5' + n.toFixed(n >= 1 ? 2 : (n >= 0.01 ? 4 : 6));
  }

  function paintBudgetAlert() {
    var el = $('#budgetAlert');
    if (!el) return;
    var st = budgetAlertState();
    el.hidden = !st;
    el.classList.toggle('is-warn', !!st && st.level === 'warn');
    if (!st) return;
    var r = st.rows[0];
    var cap = Number(r.amount) || 0;
    var spent = Number(r.spent_cny) || 0;
    var pct = (r.used_pct === null || r.used_pct === undefined) ? null : Number(r.used_pct);
    var blocked = st.level === 'blocked';
    var stamp = $('#budgetAlertStamp');
    if (stamp) stamp.textContent = blocked ? 'BUDGET SPENT OUT'
      : 'BUDGET \u2265 ' + (st.warnPct || 80) + '%';
    var title = $('#budgetAlertTitle');
    if (title) title.textContent = blocked ? '预算已到上限，/v1 新请求被拒' : '预算接近上限，到顶即拦';
    var detail = $('#budgetAlertDetail');
    if (detail) {
      detail.textContent = 'KEY ' + String(r.key || r.label || '—')
        + ' \u00b7 今日 ' + budgetMoney(spent) + ' / 上限 ' + budgetMoney(cap)
        + (pct === null ? '' : ' \u00b7 ' + pct.toFixed(1) + '%')
        + (blocked ? ' \u00b7 402 budget_exceeded' : ' \u00b7 到上限后 /v1 回 402')
        + (st.rows.length > 1 ? ' \u00b7 另有 ' + (st.rows.length - 1) + ' 档同状态' : '');
    }
    var time = $('#budgetAlertTime');
    if (time) time.textContent = 'PERIOD DAY \u00b7 5S \u5185\u751f\u6548 \u00b7 \u4e0a\u9650\u5199 control.json';
  }

  function probeUpstreamNow() {
    var btn = $('#upstreamAlertProbe');
    if (btn) btn.disabled = true;
    return HUB.probeHealth(true).then(function (health) {
      var h = health && health.upstream_health;
      HUB.toast(h && h.ok ? '上游探针正常 / OK' : '上游仍不可达 / DOWN');
      paintUpstreamAlert();
      return health;
    }).catch(function (err) {
      HUB.toast('探针请求失败 / ' + String((err && err.message) || err).slice(0, 80));
    }).finally(function () {
      if (btn) btn.disabled = upstreamBusy;
    });
  }

  function switchUpstreamNow(target) {
    if (upstreamBusy) return Promise.resolve({ busy: true });
    var label = target === 'local' ? '本地 hub-ng' : '云端 KSWA';
    upstreamBusy = true;
    paintUpstreamAlert();
    var running = ticket({
      title: '上游切换 / ' + label,
      note: '执行中 · 目标 ' + target + ' · 切换前先做 /health 探活',
      status: 'RUNNING',
      ttl: 900000
    });
    return HUB.switchUpstream(target).then(function (body) {
      if (running) running.dismiss(true);
      var h = (body && body.upstream_health) || {};
      var healthy = h.ok !== false;
      ticket({
        title: '上游切换 / ' + label,
        note: '已切到 ' + ((body && body.upstream) || '—')
          + ' · ' + (healthy ? '探针 OK' : '目标仍不可达')
          + (body && body.changed === false ? ' · 未变更' : ''),
        status: healthy ? 'PASSED' : 'PARTIAL',
        tone: healthy ? 'pass' : 'fail',
        ttl: RECEIPT_TTL
      });
      HUB.toast(healthy ? '上游已切换 / ' + label : '已切换但探针仍失败 / ' + label);
      return HUB.refresh().then(function () {
        paintUpstreamAlert();
        return body;
      });
    }).catch(function (err) {
      if (running) running.dismiss(true);
      var msg = String((err && err.message) || err).slice(0, 220);
      ticket({
        title: '上游切换 / 未执行',
        note: msg,
        status: 'FAILED',
        tone: 'fail',
        ttl: RECEIPT_TTL
      });
      HUB.toast('切换未执行 / ' + msg);
      return HUB.probeHealth(false).catch(function () { return null; });
    }).finally(function () {
      upstreamBusy = false;
      paintUpstreamAlert();
    });
  }

  /* ── 壳层状态 ─────────────────────────────────────────────────────── */
  function paintShell() {
    var st = HUB.data.state;
    var data = st.data || {};
    var mode = MODE_LABEL[st.mode] || MODE_LABEL.snapshot;
    var modeEl = $('#folioMode');
    if (modeEl) {
      modeEl.textContent = mode[0];
      modeEl.title = mode[1];
      modeEl.className = st.mode === 'live' ? 'stamp' : 'stamp hollow';
      if (st.mode === 'degraded') modeEl.title = mode[1] + (st.errors && st.errors.length ? ' · ' + st.errors.join(' · ') : '');
    }
    var profileEl = $('#folioProfile');
    if (profileEl) profileEl.textContent = st.profile === 'local' ? 'LOCAL' : String(st.profile || '—').toUpperCase();
    var dateEl = $('#folioDate');
    if (dateEl) {
      var gen = data.source && Number(data.source.generated_at);
      dateEl.textContent = gen ? (HUB.fmt.date(gen) || '—') : '—';
    }
    var nodeEl = $('#folioNode');
    if (nodeEl && data.source) nodeEl.textContent = (data.source.name || 'HUB NEO') + ' / ' + (data.source.url || '127.0.0.1:8888');
    var sourceNode = data.source && data.source.url ? String(data.source.url).replace(/^https?:\/\//, '') : '';
    var drawerNode = $('#drawerNode');
    if (drawerNode && sourceNode) drawerNode.textContent = sourceNode;
    var auditNode = $('#ovAuditNode');
    if (auditNode && sourceNode) auditNode.textContent = sourceNode;
    var upstreamEl = $('#ovUpstreamState');
    if (upstreamEl) {
      var probe = data.health && data.health.upstream_health;
      upstreamEl.textContent = !probe || probe.ok === null ? 'CHECKING'
        : (probe.ok ? 'OK ' + (probe.latency_ms || 0) + 'ms'
          : (probe.status === 'auth_error' ? 'AUTH FAIL' : 'DOWN'));
    }
    var dState = $('#drawerState');
    if (dState) dState.textContent = st.mode === 'live' ? 'LIVE' : (st.mode === 'locked' ? 'AUTH' : (st.mode === 'degraded' ? 'PARTIAL' : 'NO DATA'));
    var dSource = $('#drawerSource');
    if (dSource) {
      var lead = st.mode === 'live' ? '实时 · 写需确认' : (st.mode === 'degraded' ? '部分读数' : '读数未到');
      dSource.textContent = lead + (st.sourceIso ? ' · ' + st.sourceIso : '');
    }
    var da = $('#drawerAccountCount');
    if (da) da.textContent = HUB.fmt.int(data.status && data.status.accounts && data.status.accounts.total) + ' ACCOUNT';
    var btn = $('#refreshBtn');
    if (btn) {
      btn.disabled = !!st.loading;
      btn.textContent = st.loading ? '刷新中…' : (st.mode === 'live' ? '刷新读数' : (st.mode === 'degraded' ? '补齐读数' : '重试连接'));
    }
    document.body.setAttribute('data-mode', st.mode);
    document.body.setAttribute('data-profile', st.profile);
    paintUpstreamAlert();
  }
  HUB.paintShell = paintShell;
  HUB.paintUpstreamAlert = paintUpstreamAlert;
  HUB.switchUpstreamNow = switchUpstreamNow;

  function init() {
    receiptBtn = $('#receiptBtn');
    receiptCount = $('#receiptCount');
    if (receiptBtn) receiptBtn.addEventListener('click', openReceipts);
    receiptCountPaint();
    var upstreamProbe = $('#upstreamAlertProbe');
    if (upstreamProbe) upstreamProbe.addEventListener('click', probeUpstreamNow);
    var upstreamAction = $('#upstreamAlertAction');
    if (upstreamAction) upstreamAction.addEventListener('click', function () {
      var target = upstreamAction.getAttribute('data-upstream-target');
      if (target) switchUpstreamNow(target);
    });
    document.addEventListener('hub:health', paintUpstreamAlert);
    document.addEventListener('hub:data', paintBudgetAlert);
    var budgetGoto = $('#budgetAlertGoto');
    if (budgetGoto) budgetGoto.addEventListener('click', function () {
      location.hash = '#/vault';
    });
    wo = $('#workOrder');
    woScrim = $('#woScrim');
    woTitle = $('#woTitle');
    woKicker = $('#woKicker');
    woMeta = $('#woMeta');
    woBody = $('#woBody');
    woFoot = $('#woFoot');
    if (wo) {
      if ('inert' in wo) wo.inert = true;
      $('#woClose').addEventListener('click', closeWO);
      woScrim.addEventListener('click', closeWO);
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && wo.classList.contains('is-open')) {
          e.preventDefault();
          closeWO();
        }
      });
    }
    var refresh = $('#refreshBtn');
    if (refresh) refresh.addEventListener('click', function () {
      HUB.refresh().then(function () {
        paintShell();
        /* 走 app.js 的统一出口：没有真读数就只留占位，绝不画 0 或空表。 */
        if (HUB.renderAll) HUB.renderAll();
        HUB.toast(HUB.data.mode === 'live' ? '读数已刷新 / LIVE'
          : (HUB.data.mode === 'degraded' ? '部分读数已刷新 / PARTIAL' : '连接失败 / 保持占位'));
      });
    });
    document.addEventListener('hub:data', paintShell);
    paintShell();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
