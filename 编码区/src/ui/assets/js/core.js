/* HUBNG 视觉原型 · 核心层：索引抽屉、哈希路由、机械翻字、轻提示 */
(function () {
  'use strict';

  var HUB = window.HUB = window.HUB || {};

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function clamp(n, min, max) { return Math.max(min, Math.min(max, n)); }
  function pad(n, len) { return String(n).padStart(len || 2, '0'); }

  HUB.$ = $;
  HUB.$$ = $$;
  HUB.esc = esc;
  HUB.clamp = clamp;
  HUB.pad = pad;

  /* ── 机械翻字：逐字符滚轮，翻完回落为纯文本，避免 DOM 无限增长 ── */
  function flipText(el, value, opts) {
    if (!el) return;
    var next = String(value == null ? '' : value);
    var prev = el.getAttribute('data-flip-value');
    if (prev === null) prev = el.textContent.trim();
    if (prev === next) return;
    el.setAttribute('data-flip-value', next);
    var oldChars = Array.from(prev);
    var newChars = Array.from(next);
    el.textContent = '';
    el.classList.add('flip');
    var frag = document.createDocumentFragment();
    newChars.forEach(function (ch, i) {
      var reel = document.createElement('span');
      reel.className = 'reel';
      reel.style.setProperty('--i', String(i));
      var oldFace = document.createElement('span');
      oldFace.className = 'reel-face reel-old';
      oldFace.textContent = oldChars[i] || '';
      var newFace = document.createElement('span');
      newFace.className = 'reel-face reel-new';
      newFace.textContent = ch === ' ' ? '\u00a0' : ch;
      reel.appendChild(oldFace);
      reel.appendChild(newFace);
      frag.appendChild(reel);
    });
    el.appendChild(frag);
    clearTimeout(el._flipTimer);
    var settle = 430 + newChars.length * 14;
    el._flipTimer = setTimeout(function () {
      el.textContent = next;
    }, settle);
  }
  HUB.flipText = flipText;

  /* ── 轻提示：硬切，无淡入 ── */
  var toastTimer = 0;
  function toast(message) {
    var node = $('#toast');
    if (!node) return;
    node.textContent = message;
    node.classList.remove('is-show');
    void node.offsetWidth;
    node.classList.add('is-show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.classList.remove('is-show'); }, 2800);
  }
  HUB.toast = toast;

  /* ── 索引抽屉 ── */
  var drawer, trigger, scrim, closeBtn, lastFocus = null;
  function drawerOpen() {
    if (!drawer) return;
    lastFocus = document.activeElement;
    drawer.inert = false;
    drawer.classList.add('is-open');
    scrim.classList.add('is-open');
    drawer.setAttribute('aria-hidden', 'false');
    trigger.setAttribute('aria-expanded', 'true');
    document.documentElement.style.overflow = 'hidden';
    var first = $('.drawer-link', drawer);
    if (first) first.focus({ preventScroll: true });
  }
  function drawerClose(restore) {
    if (!drawer) return;
    if (drawer.contains(document.activeElement) && document.activeElement.blur) document.activeElement.blur();
    if ('inert' in drawer) drawer.inert = true;
    drawer.classList.remove('is-open');
    scrim.classList.remove('is-open');
    drawer.setAttribute('aria-hidden', 'true');
    trigger.setAttribute('aria-expanded', 'false');
    document.documentElement.style.overflow = '';
    if (restore !== false && lastFocus) lastFocus.focus({ preventScroll: true });
  }
  function drawerToggle() {
    if (drawer.classList.contains('is-open')) drawerClose(); else drawerOpen();
  }

  function focusTrap(e) {
    if (e.key !== 'Tab' || !drawer.classList.contains('is-open')) return;
    var focusables = $$('a[href], button:not([disabled]), input, select, [tabindex]:not([tabindex="-1"])', drawer)
      .filter(function (el) { return el.offsetParent !== null; });
    if (!focusables.length) return;
    var first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  /* ── 视图路由 ── */
  var VIEW_IDS = ['overview', 'stream', 'models', 'routing', 'ledger', 'channels', 'vault', 'audit'];
  var TITLES = {
    overview: '运行速览 · HUBNG 网关工程纪事',
    stream: '实时流水 · HUBNG 网关工程纪事',
    models: '模型目录 · HUBNG 网关工程纪事',
    routing: '路由管理 · HUBNG 网关工程纪事',
    ledger: '用量查账 · HUBNG 网关工程纪事',
    channels: '渠道管理 · HUBNG 网关工程纪事',
    vault: 'KEY 管理 · HUBNG 网关工程纪事',
    audit: '视觉审计 · HUBNG 升级底稿'
  };
  var current = '';

  function parseHash() {
    var raw = (location.hash || '').replace(/^#\/?/, '').split(/[/?]/)[0].trim();
    return VIEW_IDS.indexOf(raw) >= 0 ? raw : 'overview';
  }

  function setView(id, opts) {
    if (VIEW_IDS.indexOf(id) < 0) id = 'overview';
    if (!opts || opts.force !== false || id !== current) {
      current = id;
      $$('.view').forEach(function (v) {
        var on = v.getAttribute('data-view') === id;
        v.classList.toggle('is-active', on);
        if (on) v.removeAttribute('aria-hidden'); else v.setAttribute('aria-hidden', 'true');
      });
      $$('.drawer-link').forEach(function (a) {
        var on = a.getAttribute('data-view') === id;
        a.classList.toggle('is-active', on);
        if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      });
      document.body.setAttribute('data-view', id);
      document.title = TITLES[id] || TITLES.overview;
      window.scrollTo(0, 0);
      document.dispatchEvent(new CustomEvent('hub:view', { detail: { id: id } }));
    }
    drawerClose(false);
  }

  function syncFromHash() {
    setView(parseHash(), { force: true });
  }

  function init() {
    drawer = $('#drawer');
    if (drawer && 'inert' in drawer) drawer.inert = true;
    trigger = $('#drawerTrigger');
    scrim = $('#drawerScrim');
    closeBtn = $('#drawerClose');
    if (trigger) trigger.addEventListener('click', drawerToggle);
    if (closeBtn) closeBtn.addEventListener('click', function () { drawerClose(); });
    if (scrim) scrim.addEventListener('click', function () { drawerClose(); });
    document.addEventListener('keydown', function (e) {
      focusTrap(e);
      if (e.key === 'Escape') {
        if (drawer && drawer.classList.contains('is-open')) { e.preventDefault(); drawerClose(); }
      }
      var tag = (e.target && e.target.tagName || '').toLowerCase();
      if (e.key && e.key.toLowerCase() === 'i' && tag !== 'input' && tag !== 'textarea' && tag !== 'select' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        drawerToggle();
      }
    });
    $$('.drawer-link').forEach(function (a) {
      a.addEventListener('click', function () {
        var id = a.getAttribute('data-view');
        setTimeout(function () { setView(id, { force: true }); }, 0);
        drawerClose(false);
      });
    });
    window.addEventListener('hashchange', syncFromHash);
    syncFromHash();
  }

  HUB.setView = setView;
  HUB.currentView = function () { return current; };
  HUB.drawerOpen = drawerOpen;
  HUB.drawerClose = drawerClose;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
