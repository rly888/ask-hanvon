"""工具中心数据契约：schema / 上下文 / 结果。

schema 先行（开发计划 §8.2）：每个工具的输入输出契约、权限边界、幂等要求
都在 ToolSchema 里声明，manifest 以 MCP 工具格式对外输出。
"""
from dataclasses import dataclass, field


@dataclass
class ToolSchema:
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    required_role: str = "anonymous"
    dangerous: bool = False            # 高危工具（下单等）
    confirmation_required: bool = False  # 需要二次确认
    idempotent: bool = True
    timeout_s: float = 25.0

    def to_mcp(self) -> dict:
        return {
            "name": self.name,
            "description": self.description
            + ("（高危：需二次确认）" if self.confirmation_required else ""),
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": {
                "required_role": self.required_role,
                "dangerous": self.dangerous,
                "idempotent": self.idempotent,
            },
        }


@dataclass
class ToolContext:
    user_id: int | None = None
    role: str = "anonymous"
    session_id: str = ""
    trace_id: str = ""

    @property
    def is_authed(self) -> bool:
        return self.user_id is not None


@dataclass
class ToolResult:
    ok: bool = True
    data: dict = field(default_factory=dict)
    error: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "meta": self.meta,
        }


DEGRADE_MESSAGE = "我暂时查不到，请稍后再试。"
