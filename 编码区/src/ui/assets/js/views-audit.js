/* 附录 A 融合裁决 · 现网实测 + 字段校正 + 施工状态 */
(function () {
  'use strict';
  var HUB = window.HUB, F = HUB.fmt, esc = F.esc;

  function data() { return HUB.data.state.data || {}; }

  function renderHeader() {
    var s = HUB.$('#auditSlots'); if (s) s.textContent = F.int((data().routes && data().routes.slots || []).length);
    var m = HUB.$('#auditMode');
    if (m) m.textContent = HUB.data.state.mode === 'live' ? 'LIVE / WRITE-CONFIRM'
      : (HUB.data.state.mode === 'degraded' ? 'PARTIAL / READ MODEL'
        : (HUB.data.state.mode === 'locked' ? 'AUTH REQUIRED' : 'NO READINGS / PENDING'));
    var t = HUB.$('#auditData');
    if (t) t.textContent = (data().source && data().source.iso) || '—';
  }

  function renderBaseline() {
    var host = HUB.$('#auditBaseline');
    if (!host) return;
    var rows = [
      ['看板', 'DASHBOARD', '2010', '2.01', '今日花费 ×3 · 渠道就绪 ×2 · 报头 189px', '拆'],
      ['实时流水', 'USAGE', '2078', '2.08', '今日花费 ×3 · 输入/输出段 ×2 · 时段分布 ×2', '拆'],
      ['消耗统计', 'STATS', '1288', '1.29', '今日花费 ×2 · 官方/订阅/汇总三口径并列', '并'],
      ['模型目录', 'CATALOG', '1024', '1.02', '三张 TOP5 榜单分列 · 卡片无交叉筛选', '并'],
      ['渠道管理', 'CHANNELS', '1487', '1.49', '卡墙 + 停用区 + 价目三段重复入口', '并'],
      ['密钥管理', 'API KEYS', '1000', '1.00', '正文止于 724px · 下方空白 28% · 无配额刻度', '重制'],
      ['路由状态', 'ROUTES', '1515', '1.52', '驾驶舱与流水线双叙事 · 槽位矩阵散在四段', '重制']
    ];
    host.innerHTML = '<div class="fx-col-head"><b>A / 现网实测</b><span>MEASURED BASELINE · PX @1600×1000</span></div>'
      + '<table class="fx-audit-table"><thead><tr><th>页面 / ROUTE</th><th>高度</th><th>屏</th><th>重复与空白证据</th><th>处置</th></tr></thead><tbody>'
      + rows.map(function (r) {
        return '<tr><td class="pg"><b>' + r[0] + '</b><em>' + r[1] + '</em></td><td class="num"><b>' + r[2] + '</b>px</td><td class="num">' + r[3] + '</td><td class="ev">' + r[4] + '</td><td><span class="stamp hollow">' + r[5] + '</span></td></tr>';
      }).join('') + '</tbody></table>'
      + '<div class="fx-map-row" style="margin-top:6px"><b>实测合计</b><span>7 页最高 2.08 屏 · 22 条已挂载路由 · 5 层导航 · 3 次今日花费复读</span></div>';
  }

  function renderMapping() {
    var host = HUB.$('#auditMapping');
    if (!host) return;
    var map = [
      ['01 运行速览', '运行状态 · 今日请求 · 官方用量汇总'],
      ['02 实时流水', '用量记录 · 派发轨迹 · 实时计费'],
      ['03 模型目录', '模型 · 目录 · 价格 · 促销'],
      ['04 路由管理', '路由槽位 · 冷却池 · 切换记录'],
      ['05 用量账本', '用量 · 账单 · 异常 · 操作记录'],
      ['06 渠道管理', '渠道 · 账号 · 第三方接入 · 资源池'],
      ['07 密钥管理', 'API keys · 密码本 · 安全设置'],
      ['A 融合裁决', '实测台账 · 字段校正 · 施工状态']
    ];
    host.innerHTML = '<div class="fx-col-head"><b>B / 归并矩阵</b><span>31 PAGES → 7 VIEWS + 1 APPENDIX</span></div>'
      + map.map(function (x) { return '<div class="fx-map-row"><b>' + x[0] + '</b><span>' + x[1] + '</span></div>'; }).join('')
      + '<div class="fx-col-head" style="margin-top:7px;border-top:2px solid var(--ink)"><b>RED LINE / 四条红线</b><span>ENFORCED</span></div>'
      + '<div class="fx-map-row"><b>主路由</b><span>7 + 1 定死；旧页面按业务域降维，不复活 31 个路由。</span></div>'
      + '<div class="fx-map-row"><b>唯一出处</b><span>今日花费 / 请求量只在 01 速览与 05 账册总凭证出现。</span></div>'
      + '<div class="fx-map-row"><b>工程白话</b><span>实现说明不进入正文；只在附录与工单里出现。</span></div>'
      + '<div class="fx-map-row"><b>单屏纪律</b><span>外壳定高；全量目录 / 万行流水只在版心内滑轨滚动。</span></div>';
  }

  function renderCorrections() {
    var host = HUB.$('#auditCorrections');
    if (!host) return;
    var items = [
      ['槽位冷却 / 端点', '/api/pool.slots.cooling 不存在。冷却在 /api/routes.data.cells[].state = cooling + secs_left；熔断在 status.routing.breaker。'],
      ['TTFT / 均值', 'dashboard 没有 ttft_avg；行级 ttft_ms 在 /api/usage。首 token 均值由最近 50 行推导，并标注 SAMPLE。'],
      ['积分 / 6900-904', '流量由第三方渠道按人民币计费，credit 不适用；/api/budgets 上限来自本地 control.json，默认未设上限（amount = null），spent_cny 为今日账本实际支出。旧版积分样本 6900/904 不适用于当前数据。'],
      ['KEY 配额 / 有效期', '/api/keys 只有 realm / scope / uses / masked；没有 ¥ 配额、到期时间、轮换队列。预算按上游记录，不按 KEY。'],
      ['明细 origin / 本地云端', '/api/usage 行级没有 origin 字段；不能造 local/cloud 分列。保留 upstream/channel_label，云端水位单独由 /api/cloud/sync 展示。'],
      ['Profile / 401 语义', '/api/visibility 401 只说明未登录，不等于 KWSA。Profile 以公开 /health.profile 为准；401 显示 AUTH REQUIRED，不自动降级并伪装成云端。'],
      ['测速状态 / 无持久字段', 'readmodel/catalog 与 usage 都不带 bench 结果；POST /api/models/bench 不落目录。筛选仍只保留六维，测速结果不伪装成第七维。'],
      ['促销 / 倒计时', 'readmodel/catalog 只有 rate / free，没有 rate_promo 或倒计时。零倍率显示 FREE，不生成虚假截止日。'],
      ['写操作 / 边界', '渠道启停、模型开关、预算上限已本地化：只写 control.json，不反代云端，快照档也可执行。其余写端点仍要求 LIVE：SNAPSHOT / PARTIAL / LOCKED 只生成 ARMED 作业票。代理层放行 14 个固定写端点 + KEY realm（共 15 个白名单端点），其余 POST 直接 403；批量写先确认、按渠道分组提交，回执只报端点原话。']
    ];
    host.innerHTML = '<div class="fx-col-head"><b>C / 字段校正</b><span>FIELD NOTES · ' + items.length + ' 条</span></div>'
      + items.map(function (x) {
        return '<div class="fx-correction"><b>' + esc(x[0]) + '</b><p>' + esc(x[1]) + '</p><em>APPLIED</em></div>';
      }).join('');
  }

  function renderPhases() {
    var host = HUB.$('#auditPhases');
    if (!host) return;
    var phases = [
      ['P0', '虚构清零', 'RPS/TPS/权重/伪配额全部拆除；01、04、07 换真实字段。', 'DONE', true],
      ['P1', '三容器落位', '工单抽屉 + 冲压作业票 + 版心滑轨；Esc 收轨。', 'DONE', true],
      ['P2', '目录 / 槽位', '卡片/钢板双密度、模型/渠道双轴、六维真实筛选、真实 ' + ((data().routes && data().routes.slots || []).length) + ' 槽。', 'DONE', true],
      ['P3', '实操回填', '账号/加源/OC 桥/三口径/密码本容器已就位；21 读端点已接 LIVE，10 类写工单经代理白名单执行，dry-run 联调通过。', 'WIRED', true],
      ['P4', '双 Profile', '本机为 LOCAL；KWSA 未联调。', 'NOT RUN', false]
    ];
    host.innerHTML = phases.map(function (p) {
      return '<div class="fx-phase ' + (p[4] ? 'is-done' : '') + '"><span>' + p[0] + ' / PHASE</span><b>' + p[1] + '</b><p>' + p[2] + '</p><span class="stamp ' + (p[4] ? '' : 'hollow') + '">' + p[3] + '</span></div>';
    }).join('');
  }

  function render() {
    renderHeader();
    renderBaseline();
    renderMapping();
    renderCorrections();
    renderPhases();
  }
  HUB.views.audit = render;

  document.addEventListener('DOMContentLoaded', render);
  if (document.readyState !== 'loading') render();
})();
