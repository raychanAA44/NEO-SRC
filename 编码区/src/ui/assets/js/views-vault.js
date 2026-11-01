/* 07 KEY 管理 · /api/keys + budgets + security + usage sample */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;

  function data() { return HUB.data.state.data || {}; }
  function keys() { return data().keys || []; }
  function usageRows() { return (data().usage && data().usage.rows) || []; }
  function budgets() { return (data().budgets && data().budgets.items) || []; }

  function usageOf(name) {
    return usageRows().filter(function (r) { return r.key_name === name; });
  }

  function realmAllows(k, ch) {
    var realm = String(k.realm || 'all');
    if (realm === 'all' || realm === ch) return true;
    if (HUB.realmOf(ch) === realm) return true;
    if (realm.indexOf('custom:') === 0 && HUB.shortChannel(ch).toLowerCase() === realm.slice(7)) return true;
    return false;
  }
  function scopeLabel(ch) {
    var s = HUB.shortChannel(ch);
    if (s === 'WB-INTL') return 'WBI';
    if (s === 'WB-CN') return 'WBC';
    return s.length > 5 ? s.slice(0, 4).toUpperCase() : s;
  }

  function renderSummary() {
    var host = HUB.$('#vaultSummary');
    if (!host) return;
    var active = keys().filter(function (k) { return k.enabled; }).length;
    var all = keys().filter(function (k) { return k.realm === 'all'; }).length;
    var global = data().budgets && data().budgets.global;
    var capped = global && global.amount !== null && global.amount !== undefined;
    var cap = capped
      ? F.num(global.amount, Number(global.amount) < 0.01 ? 6 : 0) + ' ' + (global.unit || '')
      : 'NULL';
    if (global && global.over) cap += ' · 已满';
    else if (global && global.warn) cap += ' · ' + F.num(global.used_pct, 1) + '%';
    host.innerHTML = '<div><span>ISSUED / 已签发</span><b>' + keys().length + '</b></div>'
      + '<div><span>ACTIVE / 在用</span><b>' + active + '<em> / ' + keys().length + '</em></b></div>'
      + '<div><span>REALM / 全局范围</span><b>' + all + '<em> / ' + keys().length + '</em></b></div>'
      + '<div><span>BUDGET / 硬顶</span><b>' + esc(cap) + '<em> / GLOBAL</em></b></div>';
    var h = HUB.$('#vaultActiveHead'); if (h) h.textContent = String(active);
    var s = HUB.$('#vaultSeal'); if (s) s.textContent = 'SEALED · ' + keys().length + ' KEYS';
  }

  function maskedBlocks(k) {
    var raw = String(k.key_masked || '');
    var parts = raw.split('…');
    if (parts.length === 2) {
      return '<span class="fx-key-block">' + esc(parts[0]) + '</span><span class="fx-key-block">■■■■</span><span class="fx-key-block">' + esc(parts[1]) + '</span>';
    }
    return '<span class="fx-key-block">' + esc(raw || '■■■■') + '</span>';
  }

  function keyCard(k, idx) {
    var u = usageOf(k.name);
    var req = u.reduce(function (a, r) { return a + (Number(r.requests) || 1); }, 0);
    var spend = u.reduce(function (a, r) {
      if (r._cost && r._cost.cny !== undefined) return a + (Number(r._cost.cny) || 0);
      return a + (Number(r.cost_cny) || 0);
    }, 0);
    return '<article class="fx-key-card" data-key="' + esc(k.name) + '">'
      + '<div class="fx-key-top"><div class="fx-key-name"><h3>' + esc(k.name) + '</h3><span>ID ' + esc(k.id) + ' / ' + (idx === 0 ? 'PRIMARY' : 'ISSUED') + '</span></div>'
      + '<span class="stamp ' + (k.enabled ? '' : 'hollow') + '">' + (k.enabled ? 'ACTIVE' : 'DISABLED') + '</span></div>'
      + '<div class="fx-key-blocks" aria-label="凭据掩码">' + maskedBlocks(k)
      + '<div class="fx-key-tools"><button class="btn" type="button" data-key-reveal="' + esc(k.id) + '">查验</button><button class="btn" type="button" data-key-regen="' + esc(k.id) + '">轮换</button><button class="btn" type="button" data-key-realm="' + esc(k.id) + '">Realm</button>'
      + '<button class="btn" type="button" data-key-toggle="' + esc(k.id) + '" data-key-enabled="' + (k.enabled ? '1' : '0') + '">' + (k.enabled ? '停用' : '启用') + '</button>'
      + '<button class="btn" type="button" data-key-revoke="' + esc(k.id) + '">吊销</button></div></div>'
      + '<div class="fx-key-meta"><div><span>REALM / 范围</span><b>' + esc(k.realm || 'all') + '</b></div><div><span>ISSUED / 签发</span><b>' + esc(F.date(k.created_at)) + '</b></div><div><span>LAST / 最近</span><b>' + esc(F.hm(k.last_used_at)) + '</b></div><div><span>USES / 累计</span><b>' + F.int(k.uses) + '</b></div><div><span>SAMPLE / 近 50</span><b>' + F.int(req) + ' REQ · ' + esc(F.cny(spend, 4)) + '</b></div></div>'
      + '<div class="fx-key-foot"><span>SCOPE <b>' + esc(k.scope || 'ALL CHANNELS') + '</b></span><span>密钥 <b>' + esc(k.key_masked || 'MASKED') + '</b></span><span class="stamp hollow">NO EXPIRY FIELD</span></div>'
      + '</article>';
  }

  function renderKeys() {
    var host = HUB.$('#vaultKeys');
    if (!host) return;
    host.innerHTML = keys().map(keyCard).join('');
    Array.from(host.querySelectorAll('[data-key-reveal]')).forEach(function (b) {
      b.addEventListener('click', function () {
        HUB.arm('凭据查验 / ID ' + b.getAttribute('data-key-reveal'), 'GET /api/keys/{id}/reveal 返回明文；需二次授权。');
      });
    });
    Array.from(host.querySelectorAll('[data-key-regen]')).forEach(function (b) {
      b.addEventListener('click', function () {
        HUB.arm('KEY 轮换 / ID ' + b.getAttribute('data-key-regen'), 'POST /api/keys/{id}/regenerate；新明文只返回一次。');
      });
    });
    Array.from(host.querySelectorAll('[data-key-realm]')).forEach(function (b) {
      b.addEventListener('click', function () {
        var id = b.getAttribute('data-key-realm');
        var key = keys().find(function (k) { return String(k.id) === String(id); }) || {};
        var all = keys().filter(function (k) { return k.realm === 'all'; }).length;
        var vis = ((data().visibility && data().visibility.channels) || []).filter(function (c) { return !c.hidden; });
        HUB.openWO({
          kicker: 'REALM / 渠道白名单',
          title: '修改 KEY 适用范围',
          meta: 'KEY ID / ' + id,
          body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>REALM / 当前</span><b>' + esc(key.realm || 'all') + '</b></div><div class="fx-wo-text">' + all + ' / ' + keys().length + ' 把为 all；变更走 POST /api/keys/{id}/realm。</div>'
            + '<div class="fx-form"><label>REALM<select id="woRealmValue"><option' + ((key.realm || 'all') === 'all' ? ' selected' : '') + '>all</option>'
            + vis.map(function (c) { return '<option' + (key.realm === c.channel ? ' selected' : '') + '>' + esc(c.channel) + '</option>'; }).join('')
            + '</select></label></div>'
            + '<div class="fx-wo-warning">高风险写操作；执行前二次确认。</div></div>',
          actions: [
            { label: '提交 Realm 变更', filled: true, close: false, onClick: function () {
              var sel = HUB.$('#woRealmValue');
              var realm = sel ? sel.value : 'all';
              HUB.job({
                title: 'KEY Realm / ' + key.name,
                endpoint: '/api/keys/' + encodeURIComponent(id) + '/realm',
                payload: { realm: realm },
                confirm: '把 KEY「' + key.name + '」的 Realm 改为 ' + realm + '？会立即改变它能访问的渠道。',
                timeout: 60000,
                channels: realm === 'all' ? [] : [realm],
                after: HUB.closeWO
              });
            }},
            { label: '收起', close: true }
          ]
        });
      });
    });
    Array.from(host.querySelectorAll('[data-key-toggle]')).forEach(function (b) {
      b.addEventListener('click', function () {
        var id = b.getAttribute('data-key-toggle');
        var next = b.getAttribute('data-key-enabled') !== '1';
        var key = keys().find(function (k) { return String(k.id) === String(id); }) || {};
        HUB.job({
          title: (next ? '启用 KEY / ' : '停用 KEY / ') + (key.name || id),
          endpoint: '/api/keys/' + encodeURIComponent(id) + '/toggle',
          payload: { enabled: next },
          confirm: (next ? '启用' : '停用') + ' KEY「' + (key.name || id)
            + '」？' + (next ? '启用后立即恢复调用。' : '停用后携带该 KEY 的请求会被拒。'),
          timeout: 20000,
          local: true,
          refresh: true,
          channels: []
        });
      });
    });
    Array.from(host.querySelectorAll('[data-key-revoke]')).forEach(function (b) {
      b.addEventListener('click', function () {
        var id = b.getAttribute('data-key-revoke');
        var key = keys().find(function (k) { return String(k.id) === String(id); }) || {};
        HUB.job({
          title: '吊销 KEY / ' + (key.name || id),
          endpoint: '/api/keys/' + encodeURIComponent(id),
          method: 'DELETE',
          confirm: '吊销 KEY「' + (key.name || id) + '」？不可恢复，需重新签发。',
          timeout: 20000,
          local: true,
          refresh: true,
          channels: []
        });
      });
    });
  }

  function issueKey() {
    var vis = ((data().visibility && data().visibility.channels) || []).filter(function (c) { return !c.hidden; });
    HUB.openWO({
      kicker: 'VAULT / ISSUE KEY',
      title: '签发新 API KEY',
      meta: 'ONE-TIME PLAINTEXT',
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>NEW KEY / 下游凭据</span><b>ONCE</b></div>'
        + '<div class="fx-form">'
        + '<label>NAME / 名称<input id="keyName" type="text" placeholder="oc-bridge-client" autocomplete="off"></label>'
        + '<label>REALM / 范围<select id="keyRealm"><option value="all">all</option>'
        + vis.map(function (c) {
          return '<option value="' + esc(c.channel) + '">' + esc(HUB.channelLabel(c.channel, data())) + '</option>';
        }).join('') + '</select></label>'
        + '<label class="full">NOTE / 备注<input id="keyNote" type="text" placeholder="可选"></label>'
        + '</div><div class="fx-wo-warning">明文只在签发回执出现一次；之后只能看到掩码。</div></div>',
      actions: [
        { label: '签发 KEY', filled: true, close: false, onClick: function () {
          var nameNode = HUB.$('#keyName');
          var realmNode = HUB.$('#keyRealm');
          var noteNode = HUB.$('#keyNote');
          var name = nameNode ? String(nameNode.value || '').trim() : '';
          if (!name) { HUB.toast('KEY 名称必填 / EMPTY'); return; }
          var payload = { name: name, realm: (realmNode && realmNode.value) || 'all',
                          note: noteNode ? String(noteNode.value || '').trim() : '' };
          HUB.job({
            title: '签发 KEY / ' + name,
            endpoint: '/api/keys',
            payload: payload,
            confirm: '签发一把新 KEY（Realm ' + payload.realm + '）？\n明文只返回一次，请立即保存。',
            timeout: 20000,
            local: true,
            refresh: false,
            channels: payload.realm === 'all' ? [] : [payload.realm]
          }).then(function (res) {
            var body = (res && res.results && res.results[0] && res.results[0].body) || {};
            if (!body.key) return;
            HUB.closeWO();
            showIssuedKey(body);
            HUB.refresh();
          });
        } },
        { label: '取消', close: true }
      ]
    });
  }

  function showIssuedKey(body) {
    var token = String(body.key || '');
    if (!token) return;
    var label = (body.item && body.item.name) || body.issued || 'NEW KEY';
    var ticket = HUB.ticket({
      title: '一次性明文 / ' + label,
      note: token,
      status: 'SEALED',
      tone: 'pass',
      ttl: 600000
    });
    if (ticket && ticket.el) {
      var note = ticket.el.querySelector('.fx-ticket-note');
      if (note) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn small';
        btn.textContent = '复制 KEY';
        btn.style.marginTop = '6px';
        btn.addEventListener('click', function () { HUB.copyText(token, 'KEY 已复制'); });
        note.parentNode.insertBefore(btn, note.nextSibling);
      }
    }
    HUB.openWO({
      kicker: 'VAULT / KEY ISSUED',
      title: '新 KEY 已签发',
      meta: 'ID / ' + (body.issued || ''),
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>PLAINTEXT / 一次性明文</span><b>COPY NOW</b></div>'
        + '<div class="fx-wo-kv"><span>KEY</span><b style="word-break:break-all">' + esc(token) + '</b>'
        + '<span>REALM</span><b>' + esc((body.item && body.item.realm) || 'all') + '</b>'
        + '<span>MASKED</span><b>' + esc((body.item && body.item.key_masked) || '') + '</b></div>'
        + '<div class="fx-btn-row"><button class="btn small filled" type="button" id="copyIssuedKey">复制完整 KEY</button></div>'
        + '<div class="fx-wo-warning">关闭本抽屉后无法再次查看明文；丢失请吊销后重签。</div></div>',
      actions: [{ label: '我已保存', close: true }]
    });
    var copyBtn = HUB.$('#copyIssuedKey');
    if (copyBtn) copyBtn.addEventListener('click', function () { HUB.copyText(token, 'KEY 已复制'); });
  }

  function mountVaultActions() {
    var side = HUB.$('#v-vault .head-side');
    if (!side || HUB.$('#issueKeyBtn')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'issueKeyBtn';
    btn.className = 'btn small filled';
    btn.textContent = '+ 签发新 KEY';
    btn.addEventListener('click', issueKey);
    side.appendChild(btn);
  }

  function renderRail() {
    var host = HUB.$('#vaultRail');
    if (!host) return;
    var vis = ((data().visibility && data().visibility.channels) || []).filter(function (c) { return !c.hidden; });
    var sec = data().security || {};
    var budgetItems = budgets();
    var noCap = budgetItems.filter(function (b) { return b.amount === null || b.amount === undefined; }).length;
    host.innerHTML = '<div class="fx-rail-block"><div class="fx-rail-title"><b>REALM MATRIX / 渠道白名单</b><span>' + keys().length + ' × ' + vis.length + '</span></div>'
      + '<table class="fx-scope"><thead><tr><th>密钥 / KEY</th>' + vis.map(function (c) { return '<th title="' + esc(HUB.channelLabel(c.channel, data())) + '">' + esc(scopeLabel(c.channel)) + '</th>'; }).join('') + '</tr></thead><tbody>'
      + keys().map(function (k) {
        return '<tr><td><b>' + esc(k.name) + '</b></td>' + vis.map(function (c) {
          var allowed = realmAllows(k, c.channel);
          return '<td class="' + (allowed ? 'on' : '') + '" title="' + esc(c.channel) + '">' + (allowed ? '●' : '·') + '</td>';
        }).join('') + '</tr>';
      }).join('') + '</tbody></table>'
      + '<div class="fx-receipt-note">打孔 = 允许访问 · ' + keys().filter(function (k) { return k.realm === 'all'; }).length + ' / ' + keys().length + ' 把为 ALL。</div></div>'
      + '<div class="fx-rail-block"><div class="fx-rail-title"><b>BUDGETS / 预算</b><span>' + (data().budgets ? budgetItems.length + ' UPSTREAM' : '读数缺失') + '</span></div>'
      + '<div class="fx-wo-text" style="color:var(--silver-filigree)">按上游设限 · ' + noCap + ' / ' + budgetItems.length + ' 未设硬顶 · 到上限即 402 拒新请求。</div>'
      + budgetItems.map(function (b) {
        var capped = b.amount !== null && b.amount !== undefined;
        var spent = b.spent_cny === undefined ? '' : ' · 今 ¥' + Number(b.spent_cny).toFixed(4);
        var full = b.over ? '<em class="fx-budget-full" title="今日支出已达上限，/v1 新请求返回 402 budget_exceeded">已满</em>' : '';
        var near = b.warn ? '<em class="fx-budget-near" title="占用率 ≥ 80%，到上限即 402 拒请求">' + F.num(b.used_pct, 1) + '%</em>' : '';
        return '<div class="fx-budget-row' + (b.over ? ' over' : '') + '" title="上限写 control.json · 留空 = 无硬顶"><span>' + esc(b.label || b.key) + '</span><b>'
          + (capped ? F.num(b.amount, Number(b.amount) < 0.01 ? 6 : 0) + ' ' + b.unit : 'NO CAP')
          + spent + near + full + '</b></div>';
      }).join('')
      + '<div class="fx-btn-row"><button class="btn small" type="button" id="openBudgetSettings">去面板设置改预算</button></div></div>'
      + '<div class="fx-rail-block"><div class="fx-rail-title"><b>SECURITY / 安全基线</b><span>' + esc(sec.level || '—') + '</span></div>'
      + '<div class="fx-receipt-kv"><span>SCORE</span><b>' + F.int(sec.score) + ' / 100</b><span>KEYS</span><b>' + F.int(sec.stats && sec.stats.keys) + '</b><span>ALL REALM</span><b>' + F.int(sec.stats && sec.stats.keys_all_realm) + '</b><span>CRYPTO</span><b>' + esc(sec.stats && sec.stats.crypto) + '</b><span>ADMIN TOKEN</span><b>' + (sec.stats && sec.stats.admin_token ? 'ON' : 'OFF') + '</b><span>SESSION TTL</span><b>' + F.int(sec.session && sec.session.ttl) + 'S</b></div>'
      + '<div class="fx-rail-block" style="border-top:1px solid var(--paper-rule);margin-top:6px;padding-top:6px"><div class="fx-rail-title"><b>FINDINGS / 发现</b><span>' + ((sec.findings || []).length) + '</span></div>'
      + (sec.findings || []).map(function (f) { return '<div class="fx-flag"><time>' + esc(f.level) + '</time><span>' + esc(f.text) + '</span><b>' + esc(f.kind) + '</b></div>'; }).join('')
      + '<div class="fx-wo-text">口令、限流与登录锁定设置统一由面板设置管理；本页展示安全基线。</div>'
      + '<div class="fx-btn-row"><button class="btn small" type="button" id="openSecurity">去面板设置改口令与限流</button></div></div>'
      + (function () {
        var egress = (data().health && data().health.control && data().health.control.egress) || {};
        return '<div class="fx-rail-block"><div class="fx-rail-title"><b>EGRESS NETWORK / 全局出口</b><span>' + (egress.proxy_enabled ? 'PROXY' : 'DIRECT') + '</span></div>'
          + '<div class="fx-wo-text" style="color:var(--silver-filigree)">默认出口：' + esc(egress.proxy_url || '直连（DIRECT）') + '</div>'
          + '<div class="fx-btn-row"><button class="btn small" type="button" id="openEgressSettings">去面板设置改出口</button></div>'
          + '<div class="fx-wo-text">分渠道覆盖仍在 06 机柜抽屉内设置。</div></div>';
      })()
      + '<div class="fx-sealed"><b>乙舱 · 机密密码本</b><p>机密密码本 · 需要宿主授权</p><button class="btn small" type="button" id="openSealed">申请授权 / 只读预览</button></div>';
    var b = HUB.$('#openSealed');
    if (b) b.addEventListener('click', function () {
      HUB.arm('机密密码本 / 授权申请', 'GET /api/vault 返回渠道登录凭据；需宿主独立授权。');
    });
    var secBtn = HUB.$('#openSecurity');
    if (secBtn) secBtn.addEventListener('click', function () {
      if (HUB.openSettings) HUB.openSettings();
    });
    var budgetBtn = HUB.$('#openBudgetSettings');
    if (budgetBtn) budgetBtn.addEventListener('click', function () {
      if (HUB.openSettings) HUB.openSettings();
    });
    var egressBtn = HUB.$('#openEgressSettings');
    if (egressBtn) egressBtn.addEventListener('click', function () {
      if (HUB.openSettings) HUB.openSettings();
    });
  }

  function render() {
    mountVaultActions();
    renderSummary();
    renderKeys();
    renderRail();
  }
  HUB.views.vault = render;
  HUB.showIssuedKey = showIssuedKey;

  document.addEventListener('DOMContentLoaded', render);
  if (document.readyState !== 'loading') render();
})();
