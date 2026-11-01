/* HUBNG 融合适配版 · 启动器 */
(function () {
  'use strict';
  var HUB = window.HUB;

  /* 首屏还没拿到读数时，视图一个都不画：先出占位，数据到达后 renderAll 自动重画。
   * 这样不会出现「0 个渠道 / 空表 / —」被当成真实读数的情况。 */
  function paintPending() {
    var el = document.getElementById('bootPending');
    var st = HUB.data.state;
    var failed = st.attempted && !HUB.data.ready();
    var locked = st.mode === 'locked';
    if (el) {
      el.hidden = false;
      el.setAttribute('data-state', failed ? 'failed' : 'loading');
      el.innerHTML = '<div class="fx-pending-kicker">'
        + (locked ? 'AUTH REQUIRED / 需要登录'
          : (failed ? 'READINGS UNAVAILABLE / 读数不可用' : 'READINGS PENDING / 读数加载中')) + '</div>'
        + '<p class="fx-pending-text">' + (locked
          ? '读数接口要求登录。登录成功后本页自动重画；在此之前不展示任何数字与表格。'
          : (failed
            ? '网关没有返回有效读数，页面每 90 秒自动重试；在此之前不展示任何数字与表格。'
            : '正在读取网关读数，到达后自动重画本页；在此之前不展示任何数字与表格。')) + '</p>';
    }
    var root = document.getElementById('view-root');
    if (root) root.setAttribute('data-ready', '0');
  }

  function clearPending() {
    var el = document.getElementById('bootPending');
    if (el) el.hidden = true;
    var root = document.getElementById('view-root');
    if (root) root.setAttribute('data-ready', '1');
  }

  /* 每个视图的「命脉读数」：缺它时整页只出占位，不用 0 和空表冒充。
   * audit 是设计台账，不依赖读数，不给入口就永远正常渲染。 */
  var VIEW_SOURCE = {
    overview: 'dashboard',
    stream: 'usage',
    models: 'catalog',
    routing: 'routes',
    ledger: 'usage',
    channels: 'channels',
    vault: 'keys'
  };

  function paintViewStale(el, need) {
    var note = el.querySelector('.fx-stale-note');
    if (!note) {
      note = document.createElement('div');
      note.className = 'fx-stale-note';
      var head = el.querySelector('.view-head');
      if (head && head.parentNode === el) head.insertAdjacentElement('afterend', note);
      else el.insertBefore(note, el.firstChild);
    }
    note.innerHTML = '<div class="fx-pending-kicker">READINGS PENDING / 读数未到</div>'
      + '<p class="fx-pending-text">本页依赖的 <b>' + HUB.esc(need) + '</b> 还没回来，到达后自动重画；在此之前不展示这一页的数字与表格。</p>';
    el.setAttribute('data-stale', '1');
  }

  function clearViewStale(el) {
    if (el.getAttribute('data-stale')) el.removeAttribute('data-stale');
    var note = el.querySelector('.fx-stale-note');
    if (note && note.parentNode) note.parentNode.removeChild(note);
  }

  function renderView(key) {
    var el = document.getElementById('v-' + key);
    var need = VIEW_SOURCE[key];
    var data = HUB.data.state.data || {};
    if (el && need && !data[need]) { paintViewStale(el, need); return; }
    if (el) clearViewStale(el);
    try { HUB.views[key](); } catch (e) { console.error('[HUB view] ' + key, e); }
  }

  function renderAll() {
    if (!HUB.data.ready()) { paintPending(); return; }
    clearPending();
    Object.keys(HUB.views).forEach(renderView);
  }
  /* 唯一的视图出口：刷新按钮、定时轮询、hash 切换都从这里进出，
   * 保证「没有读数就不画」只有一套判定。 */
  HUB.renderAll = renderAll;

  function boot() {
    HUB.paintShell();
    renderAll();
    HUB.refresh().then(function () {
      HUB.paintShell();
      renderAll();
    });
  }

  document.addEventListener('hub:data', function () {
    HUB.paintShell();
    renderAll();
  });
  /* 显示单位切换：重画全部视图，避免同一屏混着 ¥ 和 $。 */
  document.addEventListener('hub:unit', function () {
    if (HUB.data.ready()) renderAll();
  });
  document.addEventListener('hub:view', function () {
    if (!HUB.data.ready()) { paintPending(); return; }
    clearPending();
    var id = HUB.currentView && HUB.currentView();
    if (id && HUB.views[id]) renderView(id);
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  /* LIVE 模式每 90 秒拉一次全量读数；上游探针单独 30 秒轮询，失败告警不依赖全量刷新。 */
  setInterval(function () {
    if (document.hidden) return;
    if (HUB.data && HUB.data.mode === 'live' && !HUB.data.state.loading) {
      HUB.refresh();
    }
  }, 90000);
  setInterval(function () {
    if (document.hidden || !HUB.probeHealth) return;
    HUB.probeHealth(false).catch(function () {});
  }, 30000);
  /* 后台标签页不轮询；回到前台立即补一次，避免旧告警继续挂着。 */
  document.addEventListener('visibilitychange', function () {
    if (document.hidden || !HUB.probeHealth) return;
    HUB.probeHealth(false).catch(function () {});
  });
})();
