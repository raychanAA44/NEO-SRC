/* HUBNG 融合适配版 · 数据层
 * 1) 首次载入读取真实网关快照（snapshot.js），静态评审不依赖服务；
 * 2) 同源部署时探活 /health，并并行拉取读接口，成功即切 LIVE；
 * 3) 任何接口缺失都退回快照，不补 0、不猜字段；写工单由 ui.js 的 HUB.job 单独执行。
 */
(function () {
  'use strict';
  var HUB = window.HUB = window.HUB || {};
  var SNAP = window.HUB_SNAPSHOT;
  var API = {
    dashboard: '/api/readmodel/dashboard',
    status: '/api/status',
    billing: '/api/billing/live',
    catalog: '/api/readmodel/catalog',
    meta: '/api/models/meta',
    mtok: '/api/models/mtok_prices',
    usage: '/api/usage?limit=50&bind=0',
    routes: '/api/routes',
    pool: '/api/pool',
    visibility: '/api/visibility',
    channels: '/api/readmodel/channels',
    channels_overview: '/api/channels/overview',
    custom: '/api/custom',
    keys: '/api/keys',
    budgets: '/api/budgets',
    errors: '/api/errors?limit=12',
    cloud: '/api/cloud/sync',
    official: '/api/official/summary?days=1',
    security: '/api/security/audit',
    th: '/api/th/subscription',
    /* /api/agentgw 当前无视图消费，且云端 KSWA 未提供；不请求以避免固定 404。 */
    /* 账号健康单次约 10s / 100KB，放最后避免拖住 budgets / errors / official。 */
    accounts: '/api/accounts/health'
  };
  var REQUIRED = ['dashboard', 'status', 'usage', 'routes', 'catalog'];
  var state = {
    mode: 'snapshot',
    profile: (SNAP && SNAP.source && SNAP.source.profile) || 'local',
    /* 快照已裁撤：首屏先给空壳，refresh 成功后立即换真实读数；
     * 不再让 null 流进 mergeLive / 视图。 */
    data: SNAP || { source: {} },
    snapshot: SNAP,
    lastFetch: 0,
    sourceIso: (SNAP && SNAP.source && SNAP.source.iso) || '',
    errors: [],
    loading: false,
    /* 首轮刷新回来前 state.data 只有一层空壳；ready=false 时视图一律不画，
     * 用占位替代「0 / 空表」，避免把没有读数当成读数为零。 */
    ready: false,
    attempted: false
  };

  function clone(obj) {
    if (window.structuredClone) {
      try { return window.structuredClone(obj); } catch (e) { /* fall through */ }
    }
    return JSON.parse(JSON.stringify(obj));
  }

  function timeoutFetch(path, ms) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, ms || 15000);
    return fetch(path, {
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
      headers: { 'accept': 'application/json' }
    }).then(function (res) {
      if (!res.ok) {
        var err = new Error('HTTP ' + res.status + ' ' + path);
        err.status = res.status;
        throw err;
      }
      return res.json();
    }).finally(function () { clearTimeout(timer); });
  }

  function normalize(key, payload) {
    if (key === 'routes') return (payload && payload.data) || null;
    if (key === 'custom') return payload && payload.data ? { items: payload.data, chat_url_hint: payload.chat_url_hint } : null;
    if (key === 'accounts') return payload && payload.accounts ? payload.accounts : null;
    if (key === 'mtok') return payload && payload.rows ? payload.rows : null;
    if (key === 'errors') return payload && payload.rows ? { total: payload.total, kind_counts: payload.kind_counts, rows: payload.rows.slice(0, 12) } : null;
    return payload;
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  /* 写操作会让 readmodel 首读只重建 custom 渠道（约 24 行），数秒后才补齐内建渠道。
   * 完整性以「当前可见的非 custom 渠道」为准；visibility 缺失时退回上一份完整目录。 */
  function catalogCounts(cat) {
    return (cat && cat.index && cat.index.channels) || {};
  }
  function catalogExpected(live, base) {
    var out = [];
    var vis = live && live.visibility;
    if (vis && Array.isArray(vis.channels) && vis.channels.length) {
      vis.channels.forEach(function (c) {
        if (!c || c.hidden) return;
        var ch = String(c.channel || '');
        if (ch && ch.indexOf('custom:') !== 0) out.push(ch);
      });
      return out;
    }
    Object.keys(catalogCounts(base)).forEach(function (ch) {
      if (String(ch).indexOf('custom:') !== 0) out.push(ch);
    });
    return out;
  }
  function catalogComplete(cat, live, base) {
    if (!cat || cat.warm === false) return false;
    var have = catalogCounts(cat);
    if (!Object.keys(have).length) return false;
    return catalogExpected(live, base).every(function (ch) { return !!have[ch]; });
  }
  function catalogMissing(cat, live, base) {
    var have = catalogCounts(cat);
    return catalogExpected(live, base).filter(function (ch) { return !have[ch]; });
  }

  function mergeLive(live, base) {
    var sourceBase = base || SNAP || { source: {} };
    var source = sourceBase.source || {};
    var out = clone(sourceBase) || {};
    Object.keys(live).forEach(function (key) {
      if (live[key] === undefined || live[key] === null) return;
      out[key] = live[key];
    });
    var st = live.status || {};
    out.source = Object.assign({}, source, {
      live: true,
      fetched_at: Date.now(),
      version: (live.health && live.health.version) || source.version,
      profile: (live.health && live.health.profile) || source.profile,
      url: (live.health && live.health.upstream) || source.url,
      iso: st.iso || source.iso,
      /* 本地账本模式的 status 只给 generated_at（无云端 st.at）；
       * 账页「账期」读的就是这里，缺了会退成 1970-01。 */
      generated_at: st.at || st.generated_at || (live.billing && live.billing.at) || source.generated_at
    });
    return out;
  }

  /* 刷新收尾后目录仍不完整时，后台继续补拉；不占 refresh 的 loading 锁，
   * 只有拿到完整目录才覆盖界面，避免把 24 行半成品挂上去。 */
  var settleTimer = null;
  function settleCatalog() {
    if (settleTimer) return;
    var tries = 0;
    function attempt() {
      if (state.loading) { settleTimer = setTimeout(attempt, 1500); return; }
      timeoutFetch(API.catalog, 20000).then(function (payload) {
        var cat = normalize('catalog', payload);
        if (catalogComplete(cat, state.data, state.data && state.data.catalog)) {
          settleTimer = null;
          state.data.catalog = cat;
          state.errors = (state.errors || []).filter(function (e) { return String(e).indexOf('catalog') !== 0; });
          document.dispatchEvent(new CustomEvent('hub:data', { detail: state }));
        } else if (tries++ < 2) {
          settleTimer = setTimeout(attempt, 10000);
        } else {
          settleTimer = null;
        }
      }).catch(function () {
        if (tries++ < 2) settleTimer = setTimeout(attempt, 10000);
        else settleTimer = null;
      });
    }
    settleTimer = setTimeout(attempt, 1200);
  }

  async function refresh() {
    if (state.loading) return state;
    state.loading = true;
    var errors = [];
    var denied = 0;
    try {
      var health = await timeoutFetch('/health', 7000);
      if (!health || health.status !== 'ok') throw new Error('health unavailable');
      var live = { health: health };
      var keys = Object.keys(API);
      /* 网关的重查询会互相排队；按 4 个一批拉取，避免 20 条并发把 15s 预算耗尽。 */
      for (var i = 0; i < keys.length; i += 4) {
        var chunk = keys.slice(i, i + 4);
        var entries = await Promise.all(chunk.map(function (key) {
          return timeoutFetch(API[key], 15000).then(function (payload) {
            return [key, normalize(key, payload), null];
          }).catch(function (err) {
            if (err && err.status === 401) denied += 1;
            errors.push(key + ': ' + (err.message || String(err)));
            return [key, null, err];
          });
        }));
        entries.forEach(function (pair) {
          if (pair[1] !== null) live[pair[0]] = pair[1];
        });
      }
      /* 重查询偶尔会被网关的读模型锁拖过首批超时；必需端点在批次结束后单独补拉一次。 */
      var missing = REQUIRED.filter(function (key) { return !live[key]; });
      for (var m = 0; m < missing.length; m++) {
        var retryKey = missing[m];
        try {
          var retryPayload = await timeoutFetch(API[retryKey], 20000);
          live[retryKey] = normalize(retryKey, retryPayload);
        } catch (retryErr) {
          if (retryErr && retryErr.status === 401) denied += 1;
          errors.push(retryKey + ' (retry): ' + (retryErr.message || String(retryErr)));
        }
      }
      /* 写后 catalog 首读只剩 custom 渠道；最多补拉 2 次，仍不完整就保留上一份完整目录，
       * 交给后台 settle 继续补，避免模型页瞬间从 67 掉到 24。 */
      var prevCat = (state.mode === 'live' || state.mode === 'degraded')
        ? (state.data && state.data.catalog) : null;
      for (var c = 0; c < 2 && live.catalog && !catalogComplete(live.catalog, live, prevCat); c++) {
        await sleep(1200);
        try {
          live.catalog = normalize('catalog', await timeoutFetch(API.catalog, 20000));
        } catch (catErr) {
          errors.push('catalog (readmodel retry): ' + (catErr.message || String(catErr)));
          break;
        }
      }
      var partialCh = live.catalog ? catalogMissing(live.catalog, live, prevCat) : [];
      if (live.catalog && partialCh.length) {
        if (prevCat && Object.keys(catalogCounts(prevCat)).length > Object.keys(catalogCounts(live.catalog)).length) {
          live.catalog = prevCat;
          errors.push('catalog: 首读缺少 ' + partialCh.join('/') + '，已保留上一份完整目录，后台补齐中');
        }
        settleCatalog();
      } else if (!live.catalog) {
        settleCatalog();
      }
      missing = REQUIRED.filter(function (key) { return !live[key]; });
      var base = (state.mode === 'live' || state.mode === 'degraded') ? state.data : SNAP;
      state.data = mergeLive(live, base);
      state.errors = errors;
      state.profile = health.profile || state.profile;
      state.lastFetch = Date.now();
      state.sourceIso = (live.status && live.status.iso) || state.sourceIso;
      if (denied > 0 && missing.length === REQUIRED.length) {
        state.mode = 'locked';
        state.data = SNAP ? clone(SNAP) : { source: {} };
        state.sourceIso = (SNAP && SNAP.source && SNAP.source.iso) || state.sourceIso;
      } else if (missing.length) state.mode = 'degraded';
      else state.mode = 'live';
    } catch (err) {
      /* 已有实时读数时，刷新失败只降级，不把整页退回旧快照。 */
      if (state.mode === 'live' || state.mode === 'degraded') state.mode = 'degraded';
      else state.mode = 'snapshot';
      if (state.mode === 'snapshot') state.data = SNAP ? clone(SNAP) : { source: {} };
      state.errors = errors.concat([err.message || String(err)]);
      state.lastFetch = Date.now();
    } finally {
      state.loading = false;
      state.attempted = true;
      /* 只有真读数才能画：live / degraded 有实际返回；locked 与 snapshot 都是没有数据，
       * 视图保持占位，等下一轮刷新。 */
      state.ready = state.mode === 'live' || state.mode === 'degraded';
      document.body.setAttribute('data-mode', state.mode);
      document.body.setAttribute('data-profile', state.profile);
      document.dispatchEvent(new CustomEvent('hub:data', { detail: state }));
    }
    return state;
  }

  /* 只读探活：不触发全量 readmodel，用于失败告警条在 30s 内自行恢复。 */
  async function probeHealth(force) {
    var health = await timeoutFetch(force ? '/health?probe=1' : '/health', force ? 15000 : 7000);
    if (!health || health.status !== 'ok') throw new Error('health unavailable');
    state.data = state.data || {};
    state.data.health = health;
    if (state.data.source && health.upstream) state.data.source.url = health.upstream;
    if (health.profile) state.profile = health.profile;
    document.dispatchEvent(new CustomEvent('hub:health', { detail: health }));
    return health;
  }

  /* 一键回退：页面同源动作，后端额外校验回环 + Origin + X-HUB-UI。 */
  function switchUpstream(target) {
    return fetch('/api/upstream/switch', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        'accept': 'application/json',
        'content-type': 'application/json',
        'X-HUB-UI': '1'
      },
      body: JSON.stringify({ target: target })
    }).then(function (res) {
      return res.text().then(function (text) {
        var body = null;
        try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
        if (!res.ok) {
          var msg = body && body.error && body.error.message ? body.error.message : ('HTTP ' + res.status);
          var err = new Error(msg);
          err.status = res.status;
          err.body = body;
          throw err;
        }
        return body;
      });
    });
  }

  /* ── 格式化：全部输出为字符串，显式保留 0 / 空值的区别 ───────────── */
  function isNil(v) { return v === null || v === undefined || v === ''; }
  function int(v) { return isNil(v) ? '—' : Number(v).toLocaleString('zh-CN'); }
  function num(v, digits) {
    if (isNil(v) || !isFinite(Number(v))) return '—';
    return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0 });
  }
  /* 显示单位：账本始终按人民币记账，这里只改“怎么显示”。
   * 美元用价目表的 usd_cny 汇率换算，不另存第二份数字。 */
  var UNIT = { mode: 'CNY', usdCny: 6.72 };
  try {
    var savedUnit = window.localStorage.getItem('hub.display.unit');
    if (savedUnit === 'USD' || savedUnit === 'CNY') UNIT.mode = savedUnit;
    var savedRate = Number(window.localStorage.getItem('hub.display.usd_cny'));
    if (isFinite(savedRate) && savedRate > 0) UNIT.usdCny = savedRate;
  } catch (e) { /* 隐私模式下 localStorage 不可用：维持默认 CNY */ }

  function setUnit(mode, usdCny) {
    UNIT.mode = mode === 'USD' ? 'USD' : 'CNY';
    if (usdCny !== undefined && usdCny !== null && isFinite(Number(usdCny)) && Number(usdCny) > 0) {
      UNIT.usdCny = Number(usdCny);
    }
    try {
      window.localStorage.setItem('hub.display.unit', UNIT.mode);
      window.localStorage.setItem('hub.display.usd_cny', String(UNIT.usdCny));
    } catch (e) { /* 写不进去也不影响本次会话 */ }
    /* 单位是全局显示口径：切完让所有视图重画一次，
     * 否则只有下一次轮询才换单位，面板上两种币种会同时存在。 */
    try {
      document.dispatchEvent(new CustomEvent('hub:unit', { detail: { mode: UNIT.mode, usdCny: UNIT.usdCny } }));
    } catch (e) { /* 老浏览器忽略 */ }
  }
  function unitMode() { return UNIT.mode; }
  function unitLabel() { return UNIT.mode === 'USD' ? 'USD' : 'CNY'; }
  function unitSign() { return UNIT.mode === 'USD' ? '$' : '¥'; }
  function usdCny() { return UNIT.usdCny; }

  function cny(v, digits) {
    if (isNil(v) || !isFinite(Number(v))) return '—';
    var n = Number(v);
    var d = digits === undefined ? 4 : digits;
    if (UNIT.mode === 'USD') return '$' + (n / UNIT.usdCny).toFixed(Math.max(d, 4));
    return '¥' + n.toFixed(d);
  }
  function usd(v) {
    if (isNil(v) || !isFinite(Number(v))) return '—';
    return '$' + Number(v).toFixed(4);
  }
  function pct(v, digits) {
    if (isNil(v) || !isFinite(Number(v))) return '—';
    return Number(v).toFixed(digits === undefined ? 1 : digits) + '%';
  }
  function tok(v) {
    if (isNil(v) || !isFinite(Number(v))) return '—';
    var n = Number(v);
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(Math.round(n));
  }
  function dur(ms) {
    if (isNil(ms) || !isFinite(Number(ms))) return '—';
    var n = Number(ms);
    if (n >= 60000) return (n / 60000).toFixed(2) + 'm';
    if (n >= 1000) return (n / 1000).toFixed(2) + 's';
    return Math.round(n) + 'ms';
  }
  function clock(epoch) {
    if (isNil(epoch) || !isFinite(Number(epoch))) return '—';
    try {
      return new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
      }).format(new Date(Number(epoch) * (Number(epoch) < 1e12 ? 1000 : 1)));
    } catch (e) { return '—'; }
  }
  function hm(epoch) {
    if (isNil(epoch) || !isFinite(Number(epoch))) return '—';
    try {
      return new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
      }).format(new Date(Number(epoch) * (Number(epoch) < 1e12 ? 1000 : 1))).replace(/\//g, '-');
    } catch (e) { return '—'; }
  }
  function date(epoch) {
    if (isNil(epoch) || !isFinite(Number(epoch))) return '—';
    try {
      return new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'
      }).format(new Date(Number(epoch) * (Number(epoch) < 1e12 ? 1000 : 1))).replace(/\//g, '-');
    } catch (e) { return '—'; }
  }
  function ago(epoch) {
    if (isNil(epoch) || !epoch) return '—';
    var diff = Date.now() / 1000 - Number(epoch);
    if (diff < 60) return Math.max(0, Math.round(diff)) + 's';
    if (diff < 3600) return Math.round(diff / 60) + 'm';
    if (diff < 86400) return (diff / 3600).toFixed(1) + 'h';
    return Math.round(diff / 86400) + 'd';
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  HUB.api = API;
  HUB.refresh = refresh;
  HUB.probeHealth = probeHealth;
  HUB.switchUpstream = switchUpstream;
  HUB.data = {
    state: state,
    snapshot: SNAP,
    refresh: refresh,
    ready: function () { return !!state.ready; },
    get mode() { return state.mode; },
    get profile() { return state.profile; }
  };
  HUB.fmt = {
    isNil: isNil, int: int, num: num, cny: cny, usd: usd, pct: pct,
    setUnit: setUnit, unitMode: unitMode, unitLabel: unitLabel, unitSign: unitSign, usdCny: usdCny,
    tok: tok, dur: dur, clock: clock, hm: hm, date: date, ago: ago, esc: esc
  };
})();
