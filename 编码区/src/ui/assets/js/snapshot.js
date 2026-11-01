/* 快照数据已裁撤(2026-10-24 审计减法):旧快照为已下线 hub-ng(8795)的过时
 * 数据,误导价值大于兜底价值。保留 window.HUB_SNAPSHOT = null 以维持 data.js
 * 契约;UI 离线时显示 PARTIAL/空态而非过期账目。 */
window.HUB_SNAPSHOT = null;
