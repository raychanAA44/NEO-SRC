# -*- coding: utf-8 -*-
"""client_runtime —— HUB NEO 端侧执行层三大基座(NEO-SC-02 移植)。

导出公共原语:
  · shadow_git : Shadow Git 快照与两阶段 Diff 事务原子回滚(CR-1)
  · tool_guard : 端侧工具白名单微沙箱 + 路径越界 + 凭据清洗(CR-2)
  · repo_map   : ast + PageRank 的 Top-K 符号地图(CR-3)

100% Python 标准库,零第三方依赖。
"""
from .shadow_git import ShadowGitError, ShadowGitManager, SnapshotInfo
from .tool_guard import BLOCKED_MARK, DEFAULT_ALLOWLIST, ClientToolGuard, ToolBlocked
from .repo_map import SYMBOL_KINDS, RepoMapGenerator

__all__ = [
    "ShadowGitManager", "ShadowGitError", "SnapshotInfo",
    "ClientToolGuard", "ToolBlocked", "DEFAULT_ALLOWLIST", "BLOCKED_MARK",
    "RepoMapGenerator", "SYMBOL_KINDS",
]
