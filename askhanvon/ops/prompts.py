"""Prompt 版本管理（优化项 P1-5）。

- 内置默认模板为 v0（代码常量，兜底可用）；
- DB ops_prompts 存自定义版本（版本号递增，最新即生效）；
- get() 返回 (version, template)，评测指标记录 prompt_version，
  实现「改版 → 回归 → 择优 → 切流」的可追溯闭环。
- 安全校验：模板必须包含全部必需占位符，缺失则回退默认（防手滑改坏主链路）。
"""
import threading

from ..db import get_db

# 必需占位符校验表：name → 模板中必须出现的占位符
_REQUIRED_VARS = {
    "qa": ["{context}", "{profile}"],
    "intent": [],
    "chitchat": [],
    "judge": [],
}


class PromptService:
    def __init__(self):
        self._cache: dict = {}
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def get(self, name: str, default_template: str):
        """返回 (version, template)。DB 最新版合法则用之，否则 (0, 默认模板)。"""
        if name in self._cache:
            return self._cache[name]
        version, template = 0, default_template
        try:
            row = get_db().prompt_latest(name)
        except Exception:  # noqa: BLE001 — DB 不可用时回退默认模板
            row = None
        if row and row.get("template"):
            candidate = row["template"]
            missing = [v for v in _REQUIRED_VARS.get(name, []) if v not in candidate]
            if not missing:
                version, template = int(row["version"]), candidate
        self._cache[name] = (version, template)
        return version, template

    def set(self, name: str, template: str, by: str = "admin") -> int:
        """保存新版本（自增版本号）。占位符校验失败抛 ValueError。"""
        missing = [v for v in _REQUIRED_VARS.get(name, []) if v not in template]
        if missing:
            raise ValueError("模板缺少必需占位符: " + ", ".join(missing))
        latest = get_db().prompt_latest(name)
        version = (int(latest["version"]) + 1) if latest else 1
        get_db().prompt_save(name, version, template, by)
        self.invalidate()
        return version

    def history(self, name: str, limit: int = 20) -> list:
        return get_db()._read(
            "SELECT id, name, version, updated_by, updated_at FROM ops_prompts"
            " WHERE name=? ORDER BY version DESC LIMIT ?",
            (name, limit),
        )


prompt_service = PromptService()
