/* 04 路由管理 · 槽位矩阵 + 分组编排 + Task Model Lock 监视
 * 真实数据源：
 *   /api/routes + /api/status.routing   槽位、冷却、切换台账
 *   /api/models/groups                  分组定义（本地热加载目录）
 *   /api/task_locks                     RequestDispatcher 内存粘性锁
 * 写操作只走本地网关：/api/groups/save、/api/task_locks/release。
 */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;
  var state = {
    group: 'all',
    tab: 'slots',
    selected: null,
    draft: null,
    dirty: false,
    groups: null,
    locks: null,
    groupsError: '',
    locksError: '',
    metaLoading: false,
    loadedAt: 0,
    dragFrom: -1
  };
  var META_TTL = 15000;
  var ENDPOINTS = [
    { key: 'chat', method: 'POST', path: '/v1/chat/completions', label: 'CHAT COMPLETIONS' },
    { key: 'responses', method: 'POST', path: '/v1/responses', label: 'RESPONSES API' },
    { key: 'messages', method: 'POST', path: '/v1/messages', label: 'ANTHROPIC MESSAGES' },
    { key: 'images', method: 'POST', path: '/v1/images/generations', label: 'IMAGE GENERATION' },
    { key: 'edits', method: 'POST', path: '/v1/images/edits', label: 'IMAGE EDITS' },
    { key: 'models', method: 'GET', path: '/v1/models', label: 'MODEL LIST' }
  ];

  function data() { return HUB.data.state.data || {}; }
  function routes() { return data().routes || {}; }
  function slots() { return routes().slots || []; }
  function cells() { return routes().cells || []; }
  function coolingCells() { return cells().filter(function (c) { return c.state === 'cooling'; }); }
  function coolingCount() {
    var st = (data().status && data().status.routing) || {};
    return Math.max(Number(st.cooling) || 0, coolingCells().length);
  }
  function groupOf(s) { return s.group || '—'; }
  function clone(obj) {
    if (window.structuredClone) {
      try { return window.structuredClone(obj); } catch (e) { /* fall through */ }
    }
    return JSON.parse(JSON.stringify(obj));
  }

  function slotTotals() {
    return slots().reduce(function (acc, s) {
      acc.total += Number(s.total) || 0;
      acc.available += Number(s.available) || 0;
      return acc;
    }, { total: 0, available: 0 });
  }

  /* ── 元数据：分组定义与任务锁 ─────────────────────────────────── */
  function fetchJSON(path, ms) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, ms || 8000);
    return fetch(path, {
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
      headers: { accept: 'application/json' }
    }).then(function (res) {
      if (!res.ok) {
        var err = new Error('HTTP ' + res.status);
        err.status = res.status;
        throw err;
      }
      return res.json();
    }).finally(function () { clearTimeout(timer); });
  }

  function loadMeta(force) {
    if (HUB.currentView && HUB.currentView() !== 'routing') return;
    if (state.metaLoading) return;
    if (!force && state.loadedAt && Date.now() - state.loadedAt < META_TTL) return;
    state.metaLoading = true;
    Promise.all([
      fetchJSON('/api/models/groups', 8000).catch(function (err) {
        return { __error: err && err.status ? 'HTTP ' + err.status : '未接线' };
      }),
      fetchJSON('/api/task_locks', 8000).catch(function (err) {
        return { __error: err && err.status ? 'HTTP ' + err.status : '未接线' };
      })
    ]).then(function (pair) {
      var groups = pair[0], locks = pair[1];
      state.metaLoading = false;
      state.loadedAt = Date.now();
      if (groups && !groups.__error && Array.isArray(groups.groups)) {
        state.groups = groups.groups;
        state.groupsError = '';
        if (!state.selected || !state.groups.some(function (g) { return g.group_id === state.selected; })) {
          state.selected = state.groups.length ? state.groups[0].group_id : null;
          state.dirty = false;
          state.draft = null;
        }
        if (!state.dirty) resetDraft();
      } else {
        state.groups = [];
        state.groupsError = (groups && groups.__error) || '未接线';
      }
      if (locks && !locks.__error && Array.isArray(locks.locks)) {
        state.locks = locks.locks;
        state.locksError = '';
      } else {
        state.locks = [];
        state.locksError = (locks && locks.__error) || '未接线';
      }
      paintMeta();
    }).catch(function () {
      state.metaLoading = false;
      state.groupsError = state.groupsError || '读取失败';
      paintMeta();
    });
  }

  function selectedGroup() {
    return (state.groups || []).filter(function (g) { return g.group_id === state.selected; })[0] || null;
  }
  function resetDraft() {
    var g = selectedGroup();
    if (!g) { state.draft = null; state.dirty = false; return; }
    state.draft = clone(g);
    state.draft.members = (state.draft.members || []).slice().sort(function (a, b) {
      return (Number(a.priority) || 0) - (Number(b.priority) || 0);
    });
    state.dirty = false;
  }
  function markDirty() { state.dirty = true; }
  function channelIdOf(m) {
    if (!m) return { channel: '', id: '' };
    if (m.channel && m.id) return { channel: String(m.channel), id: String(m.id) };
    var parts = String(m.name || '').split('/');
    var channel = parts.shift() || '';
    return { channel: channel, id: parts.join('/') };
  }
  function memberName(m) {
    var p = channelIdOf(m);
    return p.channel + '/' + p.id;
  }
  function catalogModels() {
    var cat = data().catalog || {};
    return Array.isArray(cat.models) ? cat.models.filter(function (m) {
      return m && m.channel && m.id;
    }) : [];
  }
  function canonicalSet() {
    var set = {};
    catalogModels().forEach(function (m) { set[m.channel + '/' + m.id] = 1; });
    return set;
  }
  function isOrphanMember(m, set) { return !set[memberName(m)]; }
  function groupOrphans(g, set) {
    return (g.members || []).filter(function (m) { return isOrphanMember(m, set); }).length;
  }
  function groupName(gid) {
    var g = (state.groups || []).filter(function (x) { return x.group_id === gid; })[0];
    return (g && g.name) || gid || '—';
  }
  function ttlText(sec) {
    var n = Number(sec) || 0;
    if (n >= 3600) return (n / 3600).toFixed(1) + 'h';
    if (n >= 60) return Math.round(n / 60) + 'm';
    return Math.round(n) + 's';
  }
  function shortGroup(gid) {
    return String(gid || '').replace(/^auto\//, '') || '—';
  }
  function renumber(members) {
    members.forEach(function (m, i) { m.priority = i + 1; });
  }

  /* ── 顶部状态尺 ─────────────────────────────────────────────── */
  function renderStrip() {
    var st = (data().status && data().status.routing) || {};
    var totals = slotTotals();
    var host = HUB.$('#routeStrip');
    if (!host) return;
    function cell(label, value, em) {
      return '<div><span>' + label + '</span><b>' + value + (em ? '<em>' + em + '</em>' : '') + '</b></div>';
    }
    var groups = state.groups ? state.groups.length : '—';
    var locks = state.locks ? state.locks.length : '—';
    host.innerHTML = cell('MATRIX ROWS / 槽行', F.int(slots().length), ' ROW')
      + cell('AVAILABLE / 可用槽', F.int(totals.available), ' / ' + F.int(totals.total))
      + cell('COOLING / 冷却', F.int(coolingCount()), ' SLOT')
      + cell('DISABLED / 停用', F.int(st.disabled), ' SLOT')
      + cell('CELLS / 账号×模型', F.int(cells().length), ' CELL')
      + cell('BREAKER / 熔断', F.int(st.breaker && st.breaker.active), ' OPEN')
      + cell('GROUPS / 分组', F.int(groups), ' AUTO')
      + cell('LOCKS / 任务锁', F.int(locks), ' LOCK');
  }

  /* ── 左栏：入站端点 + 分组清单 ───────────────────────────────── */
  function renderLeft() {
    var host = HUB.$('#routeLeft');
    if (!host) return;
    var st = (data().status && data().status.routing) || {};
    var accts = st.accounts || {};
    var guard = routes().cache_guard || {};
    var set = canonicalSet();
    var groupHtml;
    if (state.groupsError) {
      groupHtml = '<div class="fx-route-unwired"><b>分组目录未接线</b><span>' + esc(state.groupsError) + ' · 仅本地网关提供</span></div>';
    } else if (!state.groups) {
      groupHtml = '<div class="fx-route-unwired"><b>读取分组…</b><span>/api/models/groups</span></div>';
    } else if (!state.groups.length) {
      groupHtml = '<div class="fx-route-unwired"><b>尚无分组定义</b><span>运维区/configs/model_groups.json</span></div>';
    } else {
      groupHtml = state.groups.map(function (g) {
        var orph = groupOrphans(g, set);
        var lock = g.policy && g.policy.task_lock;
        return '<button class="fx-route-group' + (g.group_id === state.selected ? ' is-active' : '') + '" type="button" data-route-group="' + esc(g.group_id) + '" title="' + esc(g.group_id + ' · ' + (g.name || '')) + '">'
          + '<b>' + esc(g.group_id) + '</b>'
          + '<span class="stamp ' + (lock ? '' : 'hollow') + '">' + (lock ? 'LOCK' : 'FREE') + '</span>'
          + '<span class="fx-rg-name">' + esc(g.name || '未命名分组') + '</span>'
          + '<em>' + (g.members || []).length + ' 成员' + (orph ? ' · <b>' + orph + ' 孤儿</b>' : '') + '</em>'
          + '</button>';
      }).join('');
    }
    host.innerHTML = '<div class="fx-col-head"><b>INBOUND / 入站端点</b><span>06</span></div>'
      + ENDPOINTS.map(function (e) {
        return '<div class="fx-endpoint-chip" title="' + e.method + ' ' + esc(e.path) + '">'
          + '<b>' + esc(e.label) + '</b><span class="stamp hollow">' + e.method + '</span>'
          + '<code>' + esc(e.path) + '</code></div>';
      }).join('')
      + '<div class="fx-col-head" style="border-top:2px solid var(--ink)"><b>ROUTING GROUPS / 分组</b><span>' + (state.groups ? state.groups.length : '—') + ' <button class="btn small" type="button" id="groupCreateBtn" title="新建 auto/ 路由分组">＋ 新建</button> <button class="fx-mini-refresh" type="button" data-meta-refresh title="重读分组与任务锁" aria-label="重读分组与任务锁">↻</button></span></div>'
      + groupHtml
      + '<div class="fx-col-head" style="border-top:2px solid var(--ink)"><b>GUARD / 缓存守卫</b><span>RUNTIME</span></div>'
      + '<div class="fx-route-kv"><span>会话</span><b>' + F.int(guard.sessions) + '</b><span>TTL</span><b>' + F.int(guard.ttl) + 'S</b><span>账号并发</span><b>' + F.int(routes().account_concurrency) + '</b><span>绑定</span><b>' + F.int(st.bindings) + '</b><span>可签到</span><b>' + F.int(accts.can_checkin) + '</b></div>'
      + (function () {
        var po = (data().health && data().health.prompt_opt) || {};
        return '<div class="fx-route-kv"><span>PROMPT</span><b>' + esc(String(po.mode || 'off').toUpperCase())
          + ' · ' + esc(po.timeout_s ? F.dur(Number(po.timeout_s) * 1000) : '—') + '</b>'
          + '<span>REFINER</span><b>' + esc(po.refiner_model || '—') + '</b></div>';
      })()
      + '<div class="fx-col-head" style="border-top:2px solid var(--ink)"><b>STICKY / 渠道粘性</b><span>' + (routes().channel_sticky || []).length + '</span></div>'
      + (routes().channel_sticky || []).map(function (s) {
        return '<div class="fx-switch-row"><time>' + (s.sticky ? 'ON' : 'OFF') + '</time><span>' + esc(s.channel) + '</span><b>' + (s.sticky ? 'STICKY' : 'OFF') + '</b></div>';
      }).join('');
  }

  /* ── 中栏：槽位 / 分组编排 ─────────────────────────────────── */
  /* 上游标签一律取 /health 的实际地址，不再写死端口：
   * 旧版这里硬编码两个历史端口，换了上游就变成骗人的标签；改为一律取 /health 实际地址。 */
  function upHost(role) {
    var h = data().health || {};
    var key = role === 'local' ? 'upstream_local'
      : (role === 'model' ? 'model_upstream' : 'upstream');
    var host = String(h[key] || '').replace(/^https?:\/\//, '').replace(/\/$/, '');
    return host || '未配置';
  }

  function slotRows(list) {
    return list.map(function (s) {
      var total = Number(s.total) || 0;
      var avail = Number(s.available) || 0;
      var bits = '';
      for (var i = 0; i < total; i++) bits += '<i class="' + (i < avail ? 'on' : '') + '"></i>';
      return '<div class="fx-slot"><span class="g" title="' + esc(s.group) + '">' + esc(HUB.shortChannel(s.group)) + '</span>'
        + '<span class="m" title="' + esc(s.model) + '">' + esc(s.model) + '</span>'
        + '<span class="bar" title="' + avail + ' / ' + total + '">' + bits + '</span>'
        + '<span class="n">' + avail + '/' + total + '</span></div>';
    }).join('');
  }

  function renderSlots() {
    var groups = Array.from(new Set(slots().map(groupOf))).sort();
    var list = slots().filter(function (s) { return state.group === 'all' || groupOf(s) === state.group; });
    var totals = slotTotals();
    var health = data().health || {};
    var known = !!health.status;          /* 读数为空时不要冒充 DOWN */
    var role = String(health.upstream_role || '—').toUpperCase();
    var up = health.upstream_health || {};
    return '<div class="fx-slot-tools" style="border-bottom:1px solid var(--rule)">'
      + '<span class="fx-filter-result">UPSTREAM / 全局上游 <b>' + esc(role) + '</b> · '
      + (known ? (up.ok ? 'HEALTHY' : 'DOWN') : '—')
      + (up.latency_ms ? ' · ' + esc(F.dur(up.latency_ms)) : '') + '</span>'
      + '<span class="spacer"></span>'
      + '<span class="fx-filter-result">MODEL / 模型上游 <b>' + esc(upHost('model')) + '</b></span>'
      + '</div>'
      + '<div class="fx-slot-tools"><div class="seg" id="routeGroups" role="group" aria-label="渠道组">'
      + '<button type="button" data-group="all" class="' + (state.group === 'all' ? 'is-active' : '') + '">全部</button>'
      + groups.map(function (g) {
        return '<button type="button" data-group="' + esc(g) + '" class="' + (state.group === g ? 'is-active' : '') + '">' + esc(HUB.shortChannel(g)) + '</button>';
      }).join('') + '</div><span class="fx-filter-result">显示 <b>' + list.length + '</b> / ' + slots().length + ' · ' + F.int(totals.available) + '/' + F.int(totals.total) + ' AVAILABLE</span></div>'
      + '<div class="fx-slot-scroll"><div class="fx-slot-grid">' + (slotRows(list) || '<div class="fx-receipt-empty" style="padding:10px">当前渠道组无槽位。</div>') + '</div></div>';
  }

  function policyBtn(key, label, on) {
    return '<button class="fx-policy' + (on ? ' is-on' : '') + '" type="button" data-policy="' + key + '" aria-pressed="' + (on ? 'true' : 'false') + '">'
      + esc(label) + ' <em>' + (on ? 'ON' : 'OFF') + '</em></button>';
  }

  function memberRow(m, i, set) {
    var name = memberName(m);
    var orphan = isOrphanMember(m, set);
    var tier = String(m.tier || '—').toUpperCase();
    var enabled = m.enabled !== false;
    return '<div class="fx-gb-row' + (orphan ? ' is-orphan' : '') + '" draggable="true" data-idx="' + i + '">'
      + '<span class="p">' + String(i + 1).padStart(2, '0') + '</span>'
      + '<div class="fx-gb-model"><b title="' + esc(name) + '">' + esc(name) + '</b>'
      + (orphan ? '<span class="stamp fx-orphan-stamp">ORPHAN</span>' : '')
      + '<span>' + esc(HUB.shortChannel(m.channel || channelIdOf(m).channel)) + ' · ' + esc(m.id || channelIdOf(m).id) + '</span></div>'
      + '<span class="stamp ' + (tier === 'STRONG' ? '' : 'hollow') + '">' + esc(tier) + '</span>'
      + '<span class="stamp ' + (enabled ? '' : 'hollow') + '">' + (enabled ? 'ON' : 'OFF') + '</span>'
      + '<div class="fx-gb-ops">'
      + '<button class="btn small" type="button" data-member-up="' + i + '" aria-label="提升优先级" title="上移优先级">↑</button>'
      + '<button class="btn small" type="button" data-member-down="' + i + '" aria-label="降低优先级" title="下移优先级">↓</button>'
      + '<button class="btn small" type="button" data-member-toggle="' + i + '">' + (enabled ? '停用' : '启用') + '</button>'
      + '<button class="btn small" type="button" data-member-remove="' + i + '">移除</button>'
      + '</div></div>';
  }

  function policyPanel(g) {
    var routing = (data().health && data().health.control && data().health.control.routing) || {};
    return '<div class="fx-form" style="border-top:1px solid var(--rule);padding:8px 10px;flex:none">'
      + '<label>TIMEOUT / 超时(s)<input id="policyTimeout" type="number" min="1" max="600" step="1" value="' + esc(routing.timeout_s || 180) + '"></label>'
      + '<label>RETRIES / 重试次数<input id="policyRetries" type="number" min="0" max="3" step="1" value="' + esc(routing.max_retries === undefined ? 0 : routing.max_retries) + '"></label>'
      + '<label>STICKY TTL / 粘性(s)<input id="policySticky" type="number" min="30" max="86400" step="60" value="' + esc(routing.sticky_ttl_s || 43200) + '"></label>'
      + '</div>'
      + '<div class="fx-gb-foot">'
      + '<button class="btn small filled" type="button" id="policySaveBtn">应用策略</button>'
      + '<button class="btn small" type="button" id="groupDeleteBtn"' + (g && g.group_id ? '' : ' disabled') + '>删除分组</button>'
      + '<span class="spacer"></span><span class="fx-gb-hint">策略写 control.json；删除不可恢复</span>'
      + '</div>';
  }

  function renderBuilder() {
    if (state.groupsError) {
      return '<div class="fx-route-unwired is-wide"><b>分组编排未接线</b><span>' + esc(state.groupsError) + ' · 该功能只在本地 HUB NEO 网关可用</span></div>';
    }
    var g = state.draft;
    if (!g) {
      return '<div class="fx-route-unwired is-wide"><b>尚无分组可编排</b><span>在 运维区/configs/model_groups.json 定义 auto/&lt;id&gt; 分组后刷新</span></div>';
    }
    var set = canonicalSet();
    if (!Object.keys(set).length) {
      return '<div class="fx-route-unwired is-wide"><b>目录未就绪，暂不能编排</b><span>需要先读到完整 Catalog 才能判定孤儿成员，避免把有效模型误标 ORPHAN。</span></div>'
        + policyPanel(g);
    }
    var members = g.members || [];
    var orphans = members.filter(function (m) { return isOrphanMember(m, set); }).length;
    var policies = g.policy || {};
    return '<div class="fx-gb-head">'
      + '<div class="fx-gb-title"><span>GROUP / 分组策略</span><h3>' + esc(g.group_id) + '</h3>'
      + '<p>' + esc(g.name || '未命名分组') + ' · ' + members.length + ' 成员' + (orphans ? ' · ' + orphans + ' 孤儿' : '') + '</p></div>'
      + '<div class="fx-gb-policies">'
      + policyBtn('task_lock', 'TASK_LOCK', !!policies.task_lock)
      + policyBtn('smart_tier', 'SMART_TIER', !!policies.smart_tier)
      + policyBtn('hedged_racing', 'RACING', !!policies.hedged_racing)
      + '</div></div>'
      + '<div class="fx-gb-note"><span>优先级 1 先发 · 孤儿成员仍可保留，但上游已无此模型</span><span class="stamp ' + (state.dirty ? '' : 'hollow') + '">' + (state.dirty ? 'UNSAVED' : 'IN SYNC') + '</span></div>'
      + '<div class="fx-gb-list">' + (members.length ? members.map(function (m, i) { return memberRow(m, i, set); }).join('') : '<div class="fx-receipt-empty" style="padding:10px">分组没有成员；保存前至少加入一个模型。</div>') + '</div>'
      + '<div class="fx-gb-foot">'
      + '<button class="btn small" type="button" id="groupAddBtn"' + (state.dirty ? '' : '') + '>＋ 添加模型</button>'
      + '<button class="btn small filled" type="button" id="groupSaveBtn"' + (state.dirty && members.length ? '' : ' disabled') + '>保存编排</button>'
      + '<button class="btn small" type="button" id="groupResetBtn"' + (state.dirty ? '' : ' disabled') + '>还原</button>'
      + '<span class="fx-gb-state">' + (state.dirty ? '草稿未回写' : '已与配置文件同步') + '</span>'
      + '<span class="spacer"></span><span class="fx-gb-hint">拖拽行或 ↑↓ 调序</span>'
      + '</div>'
      + policyPanel(g);
  }

  function renderCenter() {
    var host = HUB.$('#routeCenter');
    if (!host) return;
    host.innerHTML = '<div class="fx-col-head"><b>ROUTING DECK / 路由工作台</b><span>' + (state.tab === 'slots' ? F.int(slots().length) + ' ROWS' : 'GROUP BUILDER') + '</span></div>'
      + '<div class="fx-route-tabs"><div class="seg" role="tablist" aria-label="路由工作台">'
      + '<button type="button" role="tab" data-route-tab="slots" aria-selected="' + (state.tab === 'slots' ? 'true' : 'false') + '" class="' + (state.tab === 'slots' ? 'is-active' : '') + '">槽位矩阵</button>'
      + '<button type="button" role="tab" data-route-tab="builder" aria-selected="' + (state.tab === 'builder' ? 'true' : 'false') + '" class="' + (state.tab === 'builder' ? 'is-active' : '') + '">分组编排</button>'
      + '</div><span class="fx-route-tabs-note">' + (state.tab === 'slots' ? 'SLOT MATRIX' : 'GROUP BUILDER · ' + esc(shortGroup(state.selected))) + '</span></div>'
      + (state.tab === 'slots' ? renderSlots() : renderBuilder());
  }

  /* ── 右栏：任务锁 + 故障与冷却 ───────────────────────────────── */
  function lockRows() {
    if (state.locksError) {
      return '<div class="fx-route-unwired"><b>任务锁未接线</b><span>' + esc(state.locksError) + ' · 仅本地网关提供</span></div>';
    }
    if (!state.locks || !state.locks.length) {
      return '<div class="fx-receipt-empty" style="padding:7px">暂无活跃任务锁。<br>分组请求建立任务锁后将在此列示。</div>';
    }
    return state.locks.map(function (l) {
      return '<div class="fx-lock">'
        + '<div class="fx-lock-top"><b>#' + esc(l.task_id) + '</b><span class="stamp">LOCKED</span></div>'
        + '<div class="fx-lock-model" title="' + esc(l.model) + '">' + esc(l.model) + '</div>'
        + '<div class="fx-lock-meta">' + esc(shortGroup(l.group_id)) + ' · TTL ' + esc(ttlText(l.ttl_remaining)) + '</div>'
        + '<button class="btn small" type="button" data-break-lock="' + esc(l.task_id) + '" data-break-group="' + esc(l.group_id) + '">BREAK LOCK</button>'
        + '</div>';
    }).join('');
  }

  /* 切换台账改成时间线：时间 / 动作 / 结果三列 —— 什么时候换的、从哪换到哪、为什么。 */
  function timelineHtml(list) {
    if (!list.length) return '<div class="fx-receipt-empty" style="padding:7px">近窗口无切换。</div>';
    return '<ol class="fx-timeline">' + list.map(function (s) {
      var from = s.from || '—', to = s.to || '—';
      return '<li class="fx-tl-item">'
        + '<time class="fx-tl-time">' + esc(F.clock(s.at)) + '</time>'
        + '<div class="fx-tl-body"><b class="fx-tl-act" title="' + esc(from + ' → ' + to) + '">' + esc(from + ' → ' + to) + '</b>'
        + (s.model ? '<span class="fx-tl-sub">' + esc(s.model) + '</span>' : '') + '</div>'
        + '<span class="fx-tl-res">' + esc(s.reason || '—') + '</span></li>';
    }).join('') + '</ol>';
  }

  function renderRight() {
    var host = HUB.$('#routeRight');
    if (!host) return;
    var st = (data().status && data().status.routing) || {};
    var cooling = coolingCells();
    var coolingN = coolingCount();
    var switches = (routes().switches || []).slice(0, 6);
    var breakerN = Number(st.breaker && st.breaker.active) || 0;
    var lights = '';
    for (var i = 0; i < 6; i++) lights += '<i class="' + (i < breakerN ? '' : 'off') + '" aria-hidden="true"></i>';
    host.innerHTML = '<div class="fx-col-head"><b>TASK MODEL LOCK / 活跃锁</b><span>' + (state.locks ? state.locks.length : '—') + '</span></div>'
      + '<div class="fx-lock-list">' + lockRows() + '</div>'
      + '<div class="fx-col-head" style="border-top:2px solid var(--ink)"><b>FAILOVER / 故障与冷却</b><span>' + switches.length + ' RECENT</span></div>'
      + '<div class="fx-mini-note">冷却 ' + coolingN + ' 格 · 熔断 ' + F.int(st.breaker && st.breaker.active) + ' OPEN</div>'
      + '<div class="fx-col-head" style="border-top:0"><b>CIRCUIT BREAKER / 熔断</b><span>' + F.int(st.breaker && st.breaker.active) + ' OPEN</span></div>'
      + '<div class="fx-breaker-lights" role="img" aria-label="熔断灯：' + breakerN + ' 路断开，' + Math.max(0, 6 - breakerN) + ' 路正常闭合">' + lights + '</div>'
      + '<div class="fx-breaker-legend"><span><i aria-hidden="true"></i>实心 = 熔断断开</span><span><i class="off" aria-hidden="true"></i>空心 = 正常闭合</span></div>'
      + '<div class="fx-cooling-head"><span>COOLING / 冷却槽</span><button class="btn small" type="button" id="clearCooling"' + (coolingN ? '' : ' disabled') + '>清全部冷却 · ' + coolingN + '</button></div>'
      + '<div class="fx-cooling-list">' + (cooling.length ? cooling.map(function (c) {
        return '<div class="fx-cooling"><div class="fx-cooling-main"><b>#' + esc(c.account) + ' · ' + esc(c.model) + '</b><span>' + HUB.fmtRange(c.secs_left) + ' · ' + esc(HUB.shortChannel(c.group)) + ' · ' + esc(c.reason || 'cooling') + '</span></div>'
          + '<button class="btn small" type="button" data-unfreeze-account="' + esc(c.account) + '" data-unfreeze-model="' + esc(c.model) + '">强制解冻</button></div>';
      }).join('') : '<div class="fx-receipt-empty">暂无冷却中的账号槽位。</div>') + '</div>'
      + '<div class="fx-col-head" style="border-top:2px solid var(--ink)"><b>SWITCH LEDGER / 切换台账</b><span>LAST 06</span></div>'
      + timelineHtml(switches)
      + '<div class="fx-route-kv" style="border-top:1px solid var(--rule)"><span>近 1H 切换</span><b>' + F.int(st.switches_1h) + '</b><span>冷却下一格</span><b>' + HUB.fmtRange(st.cooling_next_secs) + '</b><span>账号×模型</span><b>' + F.int(cells().length) + '</b></div>'
      + '<button class="btn small fx-route-btn" type="button" id="openCells">查看账号×模型矩阵</button>';
    var btn = HUB.$('#openCells');
    if (btn) btn.addEventListener('click', openCells);
    var clear = HUB.$('#clearCooling');
    if (clear) clear.addEventListener('click', clearAllCooling);
  }

  /* ── 分组编排动作 ───────────────────────────────────────────── */
  function selectGroup(gid) {
    if (state.selected === gid && state.draft) { state.tab = 'builder'; renderCenter(); return; }
    if (state.dirty && !window.confirm('当前分组草稿尚未保存，切换后会丢弃。继续？')) return;
    state.selected = gid;
    state.dirty = false;
    resetDraft();
    state.tab = 'builder';
    renderLeft();
    renderCenter();
  }

  function moveMember(from, to) {
    var arr = state.draft && state.draft.members;
    if (!arr || from === to || from < 0 || to < 0 || from >= arr.length || to >= arr.length) return;
    var item = arr.splice(from, 1)[0];
    arr.splice(to, 0, item);
    renumber(arr);
    markDirty();
    renderCenter();
  }

  function editMember(index, fn) {
    var arr = state.draft && state.draft.members;
    if (!arr || index < 0 || index >= arr.length) return;
    fn(arr[index], arr, index);
    renumber(arr);
    markDirty();
    renderCenter();
  }

  function openAddModel() {
    var g = state.draft;
    if (!g) return;
    var existing = {};
    (g.members || []).forEach(function (m) { existing[memberName(m)] = 1; });
    var models = catalogModels().filter(function (m) { return !existing[m.channel + '/' + m.id]; });
    var byChannel = {};
    models.forEach(function (m) {
      (byChannel[m.channel] = byChannel[m.channel] || []).push(m);
    });
    var selectHtml = Object.keys(byChannel).sort().map(function (ch) {
      return '<optgroup label="' + esc(ch) + '">'
        + byChannel[ch].sort(function (a, b) { return String(a.id).localeCompare(String(b.id)); }).map(function (m) {
          var val = m.channel + '/' + m.id;
          return '<option value="' + esc(val) + '">' + esc(val) + (m.name && m.name !== m.id ? ' · ' + esc(m.name) : '') + '</option>';
        }).join('') + '</optgroup>';
    }).join('');
    var maxPriority = (g.members || []).reduce(function (a, m) { return Math.max(a, Number(m.priority) || 0); }, 0) + 1;
    var body = '<div class="fx-wo-block"><div class="fx-wo-label"><span>GROUP / 目标分组</span><b>' + esc(g.group_id) + '</b></div>'
      + '<div class="fx-wo-kv"><span>当前成员</span><b>' + (g.members || []).length + '</b><span>目录可选</span><b>' + models.length + '</b><span>孤儿</span><b>' + groupOrphans(g, canonicalSet()) + '</b></div></div>'
      + (models.length ? '<div class="fx-wo-block"><div class="fx-wo-label"><span>MODEL / 规范全称</span><b>channel/id</b></div>'
        + '<div class="fx-gb-form"><label><span>从 Catalog 选择</span><select id="gmModel">' + selectHtml + '</select></label>'
        + '<div class="fx-gb-form-row"><label><span>TIER / 档位</span><select id="gmTier"><option value="strong">strong</option><option value="weak">weak</option></select></label>'
        + '<label><span>PRIORITY / 优先级</span><input id="gmPriority" type="number" min="1" max="99" value="' + maxPriority + '"></label></div></div>'
        + '<div class="fx-wo-text">仅列出 catalog 已登记且尚未入组的模型；加入后先落草稿，再统一保存回写。</div></div>'
        : '<div class="fx-wo-block"><div class="fx-wo-text">目录内已无未入组模型。若要接入新上游，先到 06 渠道管理加源。</div></div>')
      + '<div class="fx-wo-warning">保存会重写 model_groups.json 并立即热加载；不影响显式单模型请求。</div>';
    HUB.openWO({
      kicker: 'ROUTING / GROUP MEMBER',
      title: '加入分组',
      meta: 'DRAFT ONLY · ' + g.group_id,
      body: body,
      actions: models.length ? [
        { label: '加入草稿', filled: true, close: false, onClick: function () {
          var sel = HUB.$('#gmModel');
          var tier = HUB.$('#gmTier');
          var prio = HUB.$('#gmPriority');
          var val = sel && sel.value;
          if (!val) { HUB.toast('未选择模型 / EMPTY'); return; }
          var parts = val.split('/');
          var channel = parts.shift();
          var id = parts.join('/');
          var p = Math.max(1, Number(prio && prio.value) || maxPriority);
          state.draft.members = state.draft.members || [];
          state.draft.members.push({
            channel: channel,
            id: id,
            tier: (tier && tier.value) || 'strong',
            priority: p,
            enabled: true
          });
          state.draft.members.sort(function (a, b) { return (Number(a.priority) || 0) - (Number(b.priority) || 0); });
          renumber(state.draft.members);
          markDirty();
          HUB.closeWO();
          state.tab = 'builder';
          renderCenter();
          HUB.toast('已加入草稿 / 尚未保存');
        } },
        { label: '取消', close: true }
      ] : [{ label: '收起', close: true }]
    });
  }

  function saveDraft() {
    var g = state.draft;
    if (!g || !state.dirty) return;
    var catalogSet = canonicalSet();
    if (!Object.keys(catalogSet).length) { HUB.toast('目录未就绪 · 暂不允许保存编排'); return; }
    var members = (g.members || []).map(function (m, i) {
      var p = channelIdOf(m);
      var out = {
        channel: p.channel,
        id: p.id,
        tier: m.tier || 'strong',
        priority: i + 1,
        enabled: m.enabled !== false
      };
      if (isOrphanMember(m, catalogSet)) out.orphan = true;
      return out;
    });
    if (!members.length) { HUB.toast('分组至少保留一个成员 / EMPTY'); return; }
    var request = HUB.job({
      title: '保存分组编排 / ' + g.group_id,
      endpoint: '/api/groups/save',
      payload: {
        group_id: g.group_id,
        name: g.name || g.group_id,
        policy: g.policy || {},
        members: members
      },
      confirm: '将回写 ' + g.group_id + ' 的 ' + members.length + ' 名成员与策略，并立即热加载。\n会影响后续 auto/* 分组选模，不影响显式单模型请求。',
      timeout: 30000,
      local: true,
      refresh: false,
      channels: []
    });
    if (request && request.then) {
      request.then(function (res) {
        if (!res || !res.ok) return;
        state.dirty = false;
        state.draft = null;
        loadMeta(true);
      });
    }
  }

  function openCreateGroup() {
    HUB.openWO({
      kicker: 'ROUTING / NEW GROUP',
      title: '新建路由分组',
      meta: 'auto/<id>',
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>GROUP / 分组定义</span><b>NEW</b></div>'
        + '<div class="fx-form">'
        + '<label class="full">GROUP ID / 分组标识<input id="ngId" type="text" placeholder="auto/code-fast" autocomplete="off"></label>'
        + '<label class="full">NAME / 显示名<input id="ngName" type="text" placeholder="快速编程组" autocomplete="off"></label>'
        + '<label class="full">ENDPOINT / 绑定入口<input id="ngEndpoint" type="text" value="/v1/chat/completions"></label>'
        + '</div><div class="fx-wo-text">必须以 auto/ 开头；创建后到「分组编排」里加成员。</div>'
        + '<div class="fx-wo-warning">写入 运维区/configs/model_groups.json 并立即热加载。</div></div>',
      actions: [
        { label: '创建分组', filled: true, close: false, onClick: function () {
          var idNode = HUB.$('#ngId');
          var nameNode = HUB.$('#ngName');
          var epNode = HUB.$('#ngEndpoint');
          var gid = idNode ? String(idNode.value || '').trim() : '';
          if (!gid) { HUB.toast('分组 ID 必填 / EMPTY'); return; }
          if (gid.indexOf('auto/') !== 0) gid = 'auto/' + gid.replace(/^\/+/, '');
          if (gid === 'auto/') { HUB.toast('auto/ 后不可为空'); return; }
          HUB.job({
            title: '新建分组 / ' + gid,
            endpoint: '/api/groups/create',
            payload: {
              group_id: gid,
              name: nameNode ? String(nameNode.value || '').trim() : '',
              endpoint: epNode ? String(epNode.value || '').trim() : '/v1/chat/completions'
            },
            confirm: '创建分组 ' + gid + '？会写入 model_groups.json 并热加载。',
            timeout: 30000,
            local: true,
            refresh: false,
            channels: []
          }).then(function (res) {
            if (!res || !res.ok) return;
            HUB.closeWO();
            HUB.toast('分组已创建 / ' + gid);
            state.selected = gid;
            loadMeta(true);
          });
        } },
        { label: '取消', close: true }
      ]
    });
  }

  function deleteGroup(gid) {
    if (!gid) return;
    HUB.job({
      title: '删除分组 / ' + gid,
      endpoint: '/api/groups/' + encodeURIComponent(gid),
      method: 'DELETE',
      confirm: '删除分组 ' + gid + '？成员配置一并移除，不可恢复。',
      timeout: 30000,
      local: true,
      refresh: false,
      channels: []
    }).then(function (res) {
      if (!res || !res.ok) return;
      HUB.toast('分组已删除 / ' + gid);
      state.selected = null;
      state.draft = null;
      loadMeta(true);
    });
  }

  function savePolicy() {
    function num(sel, fallback) {
      var node = HUB.$(sel);
      var value = node ? Number(node.value) : NaN;
      return isFinite(value) ? value : fallback;
    }
    var payload = {
      timeout_s: num('#policyTimeout', 180),
      max_retries: num('#policyRetries', 0),
      sticky_ttl_s: num('#policySticky', 43200)
    };
    HUB.job({
      title: '路由策略 / 超时与粘性',
      endpoint: '/api/routes/policy',
      payload: payload,
      confirm: '应用策略：超时 ' + payload.timeout_s + 's · 重试 ' + payload.max_retries
        + ' 次 · 粘性 ' + payload.sticky_ttl_s + 's？\n写入 control.json，超时即时生效。',
      timeout: 20000,
      local: true,
      refresh: true,
      channels: []
    });
  }

  function breakLock(taskId, groupId) {
    var request = HUB.job({
      title: '释放任务模型锁 / #' + taskId,
      endpoint: '/api/task_locks/release',
      payload: { task_id: taskId, group_id: groupId },
      confirm: '解除任务 #' + taskId + ' 在 ' + groupId + ' 上的粘性锁？\n下一轮请求会重新选模，Prefix Cache 将重建。',
      timeout: 15000,
      local: true,
      refresh: false,
      channels: []
    });
    if (request && request.then) {
      request.then(function (res) {
        if (res && res.ok) loadMeta(true);
      });
    }
  }

  /* ── 既有冷却 / 解冻动作 ─────────────────────────────────────── */
  function clearAllCooling() {
    var count = coolingCount();
    var list = coolingCells();
    if (!count) { HUB.toast('暂无冷却槽位 / NO COOLING'); return; }
    var scope = list.slice(0, 4).map(function (c) { return '#' + c.account + ' · ' + c.model; }).join('、');
    if (list.length > 4) scope += ' 等';
    HUB.job({
      title: '清除全部槽位冷却 / ' + count + ' 格',
      endpoint: '/api/errors/fix',
      payload: { fix: 'clear_cooldown' },
      confirm: '将复位 ' + count + ' 个冷却格' + (scope ? '（' + scope + '）' : '') + '；全局生效、不可逐格回滚。',
      timeout: 60000,
      channels: [],
      after: HUB.closeWO
    });
  }

  function unfreezeAccount(account, model, group) {
    HUB.job({
      title: '强制解冻 / #' + account + ' · ' + (model || 'ALL'),
      endpoint: '/api/accounts/inspect',
      payload: { ids: [Number(account)], live: true, force: true },
      confirm: '对账号 #' + account + ' 执行强制续期 + 上游探活？恢复后相关槽会一并复活。',
      timeout: 120000,
      channels: group ? [group] : [],
      after: HUB.closeWO
    });
  }

  function openCooling(account, model) {
    var list = cells().filter(function (c) { return Number(c.account) === Number(account); });
    if (!list.length && account) {
      HUB.toast('该账号已不在冷却列表 / NO CELL');
      return;
    }
    var cell = list.find(function (c) { return c.model === model; }) || list[0] || {};
    var rows = list.map(function (c) {
      return '<div class="fx-cooling"><div class="fx-cooling-main"><b>' + esc(c.model) + '</b><span>' + HUB.fmtRange(c.secs_left) + ' · ' + esc(c.group) + ' · ' + esc(c.reason || 'cooling') + '</span></div>'
        + '<button class="btn small" type="button" data-wo-unfreeze="' + esc(c.account) + '" data-wo-model="' + esc(c.model) + '" data-wo-group="' + esc(c.group || '') + '">解冻</button></div>';
    }).join('');
    HUB.openWO({
      kicker: 'ROUTING / UNFREEZE WORK ORDER',
      title: '冷却格 · #' + account,
      meta: 'WO-20261024-UNF-' + String(account || 0).padStart(2, '0'),
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>CELL / 当前格</span><b>' + esc(cell.model || model || '—') + '</b></div>'
        + '<div class="fx-wo-kv"><span>账号</span><b>#' + esc(account) + '</b><span>渠道组</span><b>' + esc(cell.group || '—') + '</b><span>剩余冷却</span><b>' + esc(HUB.fmtRange(cell.secs_left)) + '</b><span>原因</span><b>' + esc(cell.reason || 'cooling') + '</b><span>熔断</span><b>' + (cell.breaker ? 'OPEN' : '—') + '</b></div>'
        + '<div class="fx-wo-text">强制解冻 = 账号续期 + 探活；429 配额冷却不会因此清除。</div></div>'
        + '<div class="fx-wo-block"><div class="fx-wo-label"><span>ACCOUNT CELLS / 本账号全部格</span><b>' + list.length + '</b></div><div class="fx-cooling-list">' + rows + '</div></div>'
        + '<div class="fx-wo-warning">失败账号自动入冷却；解冻为高权动作。</div>',
      actions: (function () {
        var acts = [{ label: '强制解冻（账号体检）', filled: true, close: false, onClick: function () { unfreezeAccount(account, cell.model || model, cell.group); } }];
        var n = coolingCount();
        if (n) acts.push({ label: '清全部冷却 · ' + n, close: false, onClick: clearAllCooling });
        acts.push({ label: '收起', close: true });
        return acts;
      })()
    });
    Array.from(HUB.$('#woBody').querySelectorAll('[data-wo-unfreeze]')).forEach(function (b) {
      b.addEventListener('click', function () {
        unfreezeAccount(b.getAttribute('data-wo-unfreeze'), b.getAttribute('data-wo-model'), b.getAttribute('data-wo-group'));
      });
    });
  }

  HUB.gotoCooling = function (account, model) {
    HUB.setView('routing');
    setTimeout(function () { openCooling(account, model); }, 70);
  };

  function openCells() {
    var list = cells();
    HUB.openWO({
      kicker: 'ROUTING / ACCOUNT × MODEL',
      title: '账号 × 模型矩阵',
      meta: 'CELLS / ' + list.length,
      body: '<div class="fx-wo-block"><div class="fx-wo-label"><span>SCOPE / 范围</span><b>' + list.length + ' CELLS</b></div>'
        + '<div class="fx-wo-text">状态：active / cooling / disabled；冷却格带剩余秒数。</div></div>'
        + '<table class="fx-wo-table"><thead><tr><th>组</th><th>账号</th><th>模型</th><th>状态</th><th>冷却</th></tr></thead><tbody>'
        + list.map(function (c) {
          return '<tr><td>' + esc(HUB.shortChannel(c.group)) + '</td><td>#' + esc(c.account) + ' ' + esc(c.nickname || '') + '</td><td>' + esc(c.model) + '</td><td>' + esc(c.state) + (c.breaker ? ' / BREAKER' : '') + '</td><td>' + esc(HUB.fmtRange(c.secs_left)) + '</td></tr>';
        }).join('') + '</tbody></table>',
      actions: [{ label: '收起', close: true }]
    });
  }

  /* ── 渲染与事件 ─────────────────────────────────────────────── */
  function renderHead() {
    var s1 = HUB.$('#routeSlotsHead'); if (s1) s1.textContent = F.int(slots().length);
    var s2 = HUB.$('#routeCellsHead'); if (s2) s2.textContent = F.int(cells().length);
  }
  function paintMeta() {
    renderStrip();
    renderLeft();
    renderCenter();
    renderRight();
  }
  function render() {
    renderHead();
    renderStrip();
    renderLeft();
    renderCenter();
    renderRight();
    loadMeta(false);
  }
  HUB.views.routing = render;

  document.addEventListener('click', function (e) {
    var tab = e.target.closest('[data-route-tab]');
    if (tab) { state.tab = tab.getAttribute('data-route-tab'); renderCenter(); return; }
    var pick = e.target.closest('[data-route-group]');
    if (pick) { selectGroup(pick.getAttribute('data-route-group')); return; }
    var g = e.target.closest('#routeGroups button');
    if (g) { state.group = g.getAttribute('data-group'); renderCenter(); return; }
    if (e.target.closest('[data-meta-refresh]')) { loadMeta(true); return; }
    var uf = e.target.closest('[data-unfreeze-account]');
    if (uf) {
      e.stopPropagation();
      openCooling(uf.getAttribute('data-unfreeze-account'), uf.getAttribute('data-unfreeze-model'));
      return;
    }
    var policy = e.target.closest('[data-policy]');
    if (policy && state.draft) {
      var key = policy.getAttribute('data-policy');
      state.draft.policy = state.draft.policy || {};
      state.draft.policy[key] = !state.draft.policy[key];
      markDirty();
      renderCenter();
      return;
    }
    var up = e.target.closest('[data-member-up]');
    if (up) { moveMember(Number(up.getAttribute('data-member-up')), Number(up.getAttribute('data-member-up')) - 1); return; }
    var down = e.target.closest('[data-member-down]');
    if (down) { moveMember(Number(down.getAttribute('data-member-down')), Number(down.getAttribute('data-member-down')) + 1); return; }
    var toggle = e.target.closest('[data-member-toggle]');
    if (toggle) {
      editMember(Number(toggle.getAttribute('data-member-toggle')), function (m) { m.enabled = m.enabled === false; });
      return;
    }
    var remove = e.target.closest('[data-member-remove]');
    if (remove) {
      editMember(Number(remove.getAttribute('data-member-remove')), function (m, arr, i) { arr.splice(i, 1); });
      return;
    }
    if (e.target.closest('#groupAddBtn')) { openAddModel(); return; }
    if (e.target.closest('#groupSaveBtn')) { saveDraft(); return; }
    if (e.target.closest('#groupCreateBtn')) { openCreateGroup(); return; }
    if (e.target.closest('#policySaveBtn')) { savePolicy(); return; }
    if (e.target.closest('#groupDeleteBtn')) { deleteGroup(state.draft && state.draft.group_id); return; }
    if (e.target.closest('#groupResetBtn')) {
      if (state.dirty && !window.confirm('丢弃当前未保存的分组草稿？')) return;
      resetDraft();
      renderCenter();
      HUB.toast('草稿已还原 / RESET');
      return;
    }
    var brk = e.target.closest('[data-break-lock]');
    if (brk) { breakLock(brk.getAttribute('data-break-lock'), brk.getAttribute('data-break-group')); return; }
  });

  document.addEventListener('dragstart', function (e) {
    var row = e.target.closest('.fx-gb-row');
    if (!row) return;
    if (e.target.closest('button')) { e.preventDefault(); return; }
    state.dragFrom = Number(row.getAttribute('data-idx'));
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', String(state.dragFrom)); } catch (err) { /* ignore */ }
    }
    row.classList.add('is-dragging');
  });
  document.addEventListener('dragend', function (e) {
    var row = e.target.closest('.fx-gb-row');
    if (row) row.classList.remove('is-dragging');
    state.dragFrom = -1;
  });
  document.addEventListener('dragover', function (e) {
    if (e.target.closest('.fx-gb-row') && state.dragFrom >= 0) {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    }
  });
  document.addEventListener('drop', function (e) {
    var row = e.target.closest('.fx-gb-row');
    if (!row || state.dragFrom < 0) return;
    e.preventDefault();
    var to = Number(row.getAttribute('data-idx'));
    moveMember(state.dragFrom, to);
    state.dragFrom = -1;
  });

  document.addEventListener('hub:view', function (e) {
    if (e.detail && e.detail.id === 'routing') loadMeta(false);
  });
  document.addEventListener('DOMContentLoaded', render);
  if (document.readyState !== 'loading') render();
})();
