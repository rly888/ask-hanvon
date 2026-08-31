"""Agent 内部契约：Plan / PlanStep。"""
from dataclasses import dataclass, field


@dataclass
class PlanStep:
    tool: str
    args: dict = field(default_factory=dict)
    reason: str = ""


@dataclass
class Plan:
    intent: str
    steps: list = field(default_factory=list)
