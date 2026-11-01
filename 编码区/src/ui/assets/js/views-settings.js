/* 面板设置 · 登录口令 / 客户端接入 / 配置重载 / 缓存刷新 / 日志清理 / 运行状态
 * 入口在索引抽屉底部；只做本地 8888 的读写，不出网。 */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;
  var BASE = 'http://127.0.0.1:8888';

  function health() { return (HUB.data.state.data || {}).health || {}; }

  function size(bytes) {
    var n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(2) + ' MB';
  }

  function kv(rows) {
    return '<div class="fx-wo-kv">' + rows.map(function (row) {
      return '<span>' + esc(row[0]) + '</span><b>' + esc(row[1]) + '</b>';
    }).join('') + '</div>';
  }

  function cli(label, command, note) {
    return '<button class="fx-cli" type="button" data-copy="' + esc(command) + '" title="复制命令">'
      + '<span class="fx-cli-top"><b>' + esc(label) + '</b><em>COPY</em></span>'
      + '<code>' + esc(command) + '</code>'
      + (note ? '<span class="fx-cli-note">' + esc(note) + '</span>' : '') + '</button>';
  }

  function egress() {
    return (health().control && health().control.egress) || {};
  }

  function budgetRows() {
    var b = HUB.data.state.data && HUB.data.state.data.budgets;
    if (!b) return '<div class="fx-wo-text">预算表还没读回来，稍后重开本面板。</div>';
    var rows = [];
    if (b.global) {
      rows.push({ key: 'global', label: '全局 / GLOBAL', amount: b.global.amount,
                  spent: b.global.spent_cny, pct: b.global.used_pct,
                  over: b.global.over, unit: b.global.unit || 'CNY' });
    }
    (b.items || []).forEach(function (item) {
      rows.push({ key: item.key, label: item.label || item.key, amount: item.amount,
                  spent: item.spent_cny, pct: item.used_pct, over: item.over,
                  unit: item.unit || 'CNY' });
    });
    if (!rows.length) return '<div class="fx-wo-text">暂无预算配置。</div>';
    return rows.map(function (row) {
      var capped = row.amount !== null && row.amount !== undefined;
      var note = (capped ? F.num(row.amount, Number(row.amount) < 0.01 ? 6 : 0) + ' ' + row.unit : 'NO CAP')
        + (row.spent === undefined ? '' : ' · 今 ' + F.cny(row.spent, 4))
        + (row.over ? ' · 已满' : (row.pct !== null && row.pct !== undefined ? ' · ' + F.num(row.pct, 1) + '%' : ''));
      return '<label class="fx-budget-row' + (row.over ? ' over' : '') + '"><span>' + esc(row.label) + '</span><b>' + esc(note) + '</b>'
        + '<input class="fx-budget-input" type="number" min="0" step="0.01" inputmode="decimal"'
        + ' data-setting-budget="' + esc(row.key) + '" value="' + (capped ? row.amount : '') + '"'
        + ' placeholder="无硬顶" aria-label="' + esc(row.label + ' 预算上限 CNY') + '"></label>';
    }).join('');
  }

  function probeEgress() {
    var node = HUB.$('#setEgressUrl');
    var out = HUB.$('#setEgressOut');
    var url = node ? String(node.value || '').trim() : '';
    if (out) out.textContent = '正在探测直连与代理链路…';
    fetch('/api/egress/probe', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(url ? { url: url } : {})
    }).then(function (res) { return res.json(); }).then(function (body) {
      if (!out) return;
      var d = body.direct || {}, p = body.proxy || {};
      out.innerHTML = '直连：' + (d.ok ? 'OK' : 'FAIL') + ' · ' + (d.elapsed_ms || '—') + 'ms'
        + (d.ip ? ' · ' + esc(d.ip) : '') + '<br>代理：'
        + (body.proxy_configured
          ? ((p.ok ? 'OK' : 'FAIL') + ' · ' + (p.elapsed_ms || '—') + 'ms' + (p.ip ? ' · ' + esc(p.ip) : ''))
          : '未配置（走直连）')
        + '<br>生效：' + esc(String(body.effective || '—').toUpperCase());
    }).catch(function (err) {
      if (out) out.textContent = '探针失败：' + String((err && err.message) || err);
    });
  }

  function body() {
    var h = health();
    var rl = h.rate_limit || {};
    var pool = h.conn_pool || {};
    var auth = h.admin_auth || {};
    var channel = h.ledger_channel || 'open-design';
    return '<div class="fx-wo-block"><div class="fx-wo-label"><span>ACCESS PASSWORD / 登录口令</span><b>LOCAL</b></div>'
      + '<div class="fx-wo-text">设置本机管理员口令，用于面板写操作与 API 请求；保存后旧口令立即失效。</div>'
      + '<div class="fx-form">'
      + '<label>当前口令<input id="setPwdOld" type="password" autocomplete="current-password"></label>'
      + '<label>新口令<input id="setPwdNew" type="password" autocomplete="new-password" placeholder="至少 8 位"></label>'
      + '<label class="full">再输一次新口令<input id="setPwdNew2" type="password" autocomplete="new-password"></label>'
      + '</div>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" id="setPwdSave">保存新口令</button></div>'
      + '<div class="fx-wo-text">口令以 scrypt 哈希落盘，明文不落盘、不回显。</div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>INBOUND RATE LIMIT / 入站限流</span><b>REQ/S</b></div>'
      + '<div class="fx-form">'
      + '<label>每秒请求<input id="setRps" type="number" min="1" max="10000" step="1" value="' + esc(rl.rps || '') + '"></label>'
      + '<label>突发容量<input id="setBurst" type="number" min="1" max="100000" step="1" value="' + esc(rl.burst || '') + '"></label>'
      + '</div>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" id="setRateSave">应用限流</button></div>'
      + '<div class="fx-wo-text">更新 control.json 并即时重建令牌桶；/health 与静态资源不受限流。</div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>LOGIN LOCKOUT / 登录熔断</span><b id="setLockCount">—</b></div>'
      + '<div id="setLockList" class="fx-wo-text">读取中…</div>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" id="setUnlockAll">全部解封</button><button class="btn small" type="button" id="setLockReload">重新读取</button></div>'
      + '<div class="fx-wo-warning">解封会清空这些 IP 的失败计数与退避锁。</div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>BUDGET CAPS / 预算硬顶</span><b>记账 CNY · DAY</b></div>'
      + '<div class="fx-wo-text">按上游与全局设日上限；留空 = 无硬顶，到上限即对新请求回 402。改完即时生效。</div>'
      + budgetRows()
      + '<div class="fx-wo-warning">数字写 control.json；今日支出是账本实际花费，不是预估。</div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>DISPLAY UNIT / 显示单位</span><b id="setUnitNow">'
      + esc(F.unitLabel()) + '</b></div>'
      + '<div class="fx-wo-text">账本按人民币记账；这里只决定整个面板怎么显示，不改账、不改价目表。切完全部视图立即重画；美元按 1 USD = '
      + esc(String(F.usdCny())) + ' CNY 换算（取自价目表表头汇率）。</div>'
      + '<div class="fx-btn-row"><button class="btn small' + (F.unitMode() === 'CNY' ? ' filled' : '') + '" type="button" data-unit="CNY">¥ 人民币</button>'
      + '<button class="btn small' + (F.unitMode() === 'USD' ? ' filled' : '') + '" type="button" data-unit="USD">$ 美元</button></div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>EGRESS / 全局出口</span><b id="setEgressMode">' + (egress().proxy_enabled ? 'PROXY' : 'DIRECT') + '</b></div>'
      + '<div class="fx-form"><label class="full">PROXY URL / 默认出口<input id="setEgressUrl" type="text" value="' + esc(egress().proxy_url || '') + '" placeholder="http://host:7897（留空 = 直连）"></label></div>'
      + '<div class="fx-btn-row"><button class="btn small filled" type="button" id="setEgressSave">保存默认出口</button><button class="btn small" type="button" id="setEgressTest">连通性测试</button></div>'
      + '<div class="fx-wo-text" id="setEgressOut">未配置专属出口的渠道使用默认出口；可在 06 视图设置渠道级出口。</div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>CLIENT ACCESS / 客户端接入</span><b>COPY</b></div>'
      + kv([['BASE URL', BASE + '/v1'], ['CHANNEL', channel], ['AUTH', 'Bearer <KEY>']])
      + '<div class="fx-cli-grid">'
      + cli('BASE URL / 接入地址', BASE + '/v1', '用于客户端的 Base URL 配置。')
      + cli('CURL / 冒烟一条', 'curl -sS ' + BASE + '/v1/chat/completions -H "Authorization: Bearer <KEY>" -H "content-type: application/json" -d \'{"model":"<MODEL>","messages":[{"role":"user","content":"ping"}]}\'', '使用 07 视图签发的 API Key；模型标识以 03 模型目录为准。')
      + '</div>'
      + '<div class="fx-wo-text">API Key 明文仅在签发回执中返回一次，后续仅显示掩码。</div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>CONFIG RELOAD / 配置重载</span><b>HOT</b></div>'
      + '<div class="fx-wo-text">修改上游、路由分组或控制面配置后执行重载，使路由表、分组与限流策略即时生效。</div>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" id="setReload">重载配置</button></div>'
      + '<div class="fx-wo-warning">仅热重载配置，不中断现有连接或重启进程。</div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>CACHE FLUSH / 缓存刷新</span><b>MEMORY</b></div>'
      + '<div class="fx-wo-text">刷新模型目录、官方额度、对账与预算缓存；后续读取将重新获取数据。</div>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" id="setCache">刷新缓存</button></div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>LOG ROTATION / 日志清理</span><b>HISTORY</b></div>'
      + '<div class="fx-wo-text">清理历史测试日志（run-*.log），保留最近 5 份；正在写入的日志不受影响。</div>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" id="setLogs">清理历史日志</button></div></div>'

      + '<div class="fx-wo-block"><div class="fx-wo-label"><span>RUNTIME / 运行状态</span><b>READ-ONLY</b></div>'
      + kv([['版本', h.version || '—'],
        ['端口', String(h.port || '—')],
        ['账本', (h.ledger_backend || '—') + ' · ' + (h.ledger_target || '—')],
        ['模式', (h.ledger_mode || '—') + ' · ' + (h.upstream_role || '—')],
        ['限流', rl.enabled === false ? '已关闭' : ((rl.rps || '—') + ' req/s · burst ' + (rl.burst || '—'))],
        ['连接池', '每主机 ' + (pool.max_per_host || '—') + ' · 空闲 ' + (pool.idle_timeout_s || '—') + 's'],
        ['管理员口令', auth.configured ? '已设置口令' : '未设口令 · 走 ADMIN_TOKEN'],
        ['账本渠道', channel]])
      + '<div class="fx-wo-text">运行状态取自 /health；端口与账本配置需在配置文件中修改并热重载。</div></div>';
  }

  /* ── 登录熔断：锁定清单只读门面（仅回环可读） ────────────── */
  function loadLockedIps() {
    var host = HUB.$('#setLockList');
    var count = HUB.$('#setLockCount');
    if (!host) return;
    fetch('/api/admin/locked-ips', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (body) {
        var rows = (body && body.rows) || [];
        var locked = (body && body.locked) || [];
        if (count) count.textContent = locked.length + ' LOCKED';
        if (!rows.length) { host.textContent = '当前没有失败记录。'; return; }
        host.innerHTML = rows.map(function (row) {
          return '<div class="fx-wo-chip">' + esc(row.ip) + ' <b>' + row.fails + ' FAIL'
            + (row.locked_for ? ' · 锁 ' + row.locked_for + 's' : '') + '</b>'
            + ' <button class="btn small" type="button" data-unlock-ip="' + esc(row.ip) + '">解封</button></div>';
        }).join('');
      })
      .catch(function () { host.textContent = '锁定清单读取失败（仅回环可读）。'; });
  }

  function wireLockRows() {
    var host = HUB.$('#setLockList');
    if (!host) return;
    host.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-unlock-ip]');
      if (!btn) return;
      var ip = btn.getAttribute('data-unlock-ip');
      HUB.job({
        title: '解封 IP / ' + ip,
        endpoint: '/api/admin/unlock?ip=' + encodeURIComponent(ip),
        method: 'GET',
        confirm: '解除 ' + ip + ' 的失败计数与退避锁？',
        local: true,
        refresh: false,
        timeout: 15000,
        channels: []
      }).then(function (res) { if (res && res.ok) loadLockedIps(); });
    });
  }

  function open() {
    HUB.openWO({
      kicker: 'SETTINGS / 面板设置',
      title: '面板设置',
      meta: 'LOCAL / ' + BASE.replace(/^https?:\/\//, ''),
      body: body(),
      actions: [{ label: '收起设置', close: true }]
    });
    wire();
    /* 快照未到时预算表是空的：等 hub:data 落地后自动重画一次，避免读数假空。 */
    if (!(HUB.data.state.data && HUB.data.state.data.budgets)) {
      var once = function () {
        document.removeEventListener('hub:data', once);
        if (HUB.data.state.data && HUB.data.state.data.budgets && HUB.$('#setEgressUrl')) open();
      };
      document.addEventListener('hub:data', once);
    }
  }

  function val(sel) {
    var node = HUB.$('#' + sel);
    return node ? String(node.value || '') : '';
  }

  function wire() {
    Array.from(document.querySelectorAll('[data-unit]')).forEach(function (btn) {
      btn.addEventListener('click', function () {
        F.setUnit(btn.getAttribute('data-unit'));
        HUB.toast('显示单位已切换 / ' + F.unitMode());
        open();
        if (HUB.refresh) HUB.refresh();
      });
    });
    var savePwd = HUB.$('#setPwdSave');
    if (savePwd) savePwd.addEventListener('click', function () {
      var oldPw = val('setPwdOld'), newPw = val('setPwdNew'), again = val('setPwdNew2');
      if (newPw.length < 8) { HUB.toast('新口令至少 8 位'); return; }
      if (newPw !== again) { HUB.toast('两次输入的新口令不一致'); return; }
      HUB.job({
        title: '修改登录口令',
        endpoint: '/api/admin/password',
        payload: { old_password: oldPw, new_password: newPw },
        confirm: '确认修改登录口令？旧口令立即失效。',
        local: true,
        refresh: false,
        timeout: 20000,
        channels: []
      }).then(function (res) {
        if (res && res.ok) {
          ['setPwdOld', 'setPwdNew', 'setPwdNew2'].forEach(function (id) {
            var node = HUB.$('#' + id);
            if (node) node.value = '';
          });
        }
      });
    });

    var reload = HUB.$('#setReload');
    if (reload) reload.addEventListener('click', function () {
      HUB.job({
        title: '重载配置',
        endpoint: '/api/system/reload',
        payload: {},
        local: true,
        refresh: false,
        timeout: 20000,
        channels: []
      }).then(function (res) {
        var b = (res && res.results && res.results[0] && res.results[0].body) || {};
        if (b && b.ok === false) return;
        if (b && b.channels !== undefined) {
          HUB.toast('已重载 · ' + b.channels + ' 个上游 · 分组 ' + (b.groups === undefined ? '—' : b.groups));
        }
      });
    });

    var cache = HUB.$('#setCache');
    if (cache) cache.addEventListener('click', function () {
      HUB.job({
        title: '刷新缓存',
        endpoint: '/api/system/cache/clear',
        payload: {},
        local: true,
        refresh: false,
        timeout: 15000,
        channels: []
      });
    });

    var rateSave = HUB.$('#setRateSave');
    if (rateSave) rateSave.addEventListener('click', function () {
      var value = Number(val('setRps'));
      var burst = Number(val('setBurst'));
      if (!isFinite(value) || value < 1) { HUB.toast('每秒请求需 ≥ 1'); return; }
      if (!isFinite(burst) || burst < 1) { HUB.toast('突发容量需 ≥ 1'); return; }
      HUB.job({
        title: '入站限流 / ' + value + ' req/s · burst ' + burst,
        endpoint: '/api/rate-limit/config',
        payload: { rps: value, burst: burst },
        confirm: '把令牌桶改为 ' + value + ' req/s（burst ' + burst + '）？已有桶会清空重建。',
        local: true,
        refresh: false,
        timeout: 20000,
        channels: []
      });
    });

    var unlockAll = HUB.$('#setUnlockAll');
    if (unlockAll) unlockAll.addEventListener('click', function () {
      HUB.job({
        title: '全部解封',
        endpoint: '/api/admin/unlock',
        method: 'GET',
        confirm: '清空全部 IP 的失败计数与退避锁？',
        local: true,
        refresh: false,
        timeout: 15000,
        channels: []
      }).then(function (res) { if (res && res.ok) loadLockedIps(); });
    });
    var lockReload = HUB.$('#setLockReload');
    if (lockReload) lockReload.addEventListener('click', loadLockedIps);
    wireLockRows();
    loadLockedIps();

    Array.prototype.forEach.call(document.querySelectorAll('[data-setting-budget]'), function (input) {
      input.addEventListener('change', function () {
        var key = input.getAttribute('data-setting-budget');
        var raw = String(input.value || '').trim();
        var amount = raw === '' ? null : Number(raw);
        if (amount !== null && (!isFinite(amount) || amount < 0)) {
          HUB.toast('预算需为非负数字或留空');
          return;
        }
        HUB.job({
          title: '预算硬顶 / ' + key,
          endpoint: '/api/budgets',
          payload: { key: key, amount: amount, unit: 'CNY' },
          local: true,
          timeout: 20000,
          channels: []
        });
      });
    });

    var egressSave = HUB.$('#setEgressSave');
    if (egressSave) egressSave.addEventListener('click', function () {
      var node = HUB.$('#setEgressUrl');
      var url = node ? String(node.value || '').trim() : '';
      HUB.job({
        title: '全局出口 / ' + (url || 'DIRECT'),
        endpoint: '/api/egress/proxy-url',
        payload: { url: url },
        confirm: '将全局默认出口设为 ' + (url || '直连') + '？未配置专属出口的渠道将使用此设置。',
        local: true,
        refresh: true,
        timeout: 20000,
        channels: []
      });
    });
    var egressTest = HUB.$('#setEgressTest');
    if (egressTest) egressTest.addEventListener('click', probeEgress);

    var logs = HUB.$('#setLogs');
    if (logs) logs.addEventListener('click', function () {
      HUB.job({
        title: '清理历史日志',
        endpoint: '/api/system/logs/clear',
        payload: { keep: 5 },
        confirm: '删除历史测试运行记录，只留最近 5 份？',
        local: true,
        refresh: false,
        timeout: 20000,
        channels: []
      }).then(function (res) {
        var b = (res && res.results && res.results[0] && res.results[0].body) || {};
        if (b && b.removed !== undefined) {
          HUB.toast('已清理 ' + b.removed + ' 份 · 释放 ' + size(b.freed_bytes));
        }
      });
    });
  }

  function init() {
    var entry = HUB.$('#settingsEntry');
    if (!entry) return;
    entry.addEventListener('click', function () {
      HUB.drawerClose(false);
      open();
    });
  }

  HUB.openSettings = open;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
