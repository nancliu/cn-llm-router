"""领域类型定义（docs/spec/router-v1.md §3）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

COMPLEXITIES = ("低", "中", "高")
STRATEGIES = ("纯能力优先", "平衡", "性价比优先")
DEFAULT_COMPLEXITY = "中"
DEFAULT_CATEGORY = "知识问答/检索"


@dataclass
class Classification:
    """分类器输出（ADR-0002）。"""

    category: str
    complexity: str
    confidence: float = 0.5
    second_guess: Optional[str] = None
    low_confidence: bool = False
    fallback_reason: Optional[str] = None
    raw: Optional[dict] = None  # 仅 debug 模式输出

    def to_dict(self, debug: bool = False) -> dict:
        d = {
            "category": self.category,
            "complexity": self.complexity,
            "confidence": self.confidence,
            "second_guess": self.second_guess,
            "low_confidence": self.low_confidence,
            "fallback_reason": self.fallback_reason,
        }
        if debug:
            d["raw"] = self.raw
        return d


@dataclass
class ModelChoice:
    """推荐中的单个模型选择。"""

    logical_name: str
    vendor: str
    score: float
    cost: float
    rank: int
    reason: str


@dataclass
class Recommendation:
    """选择器输出（ADR-0001/0005）。"""

    strategy: str
    availability_filtered: bool
    primary: ModelChoice
    backup: Optional[ModelChoice] = None
    notice: list[str] = field(default_factory=list)


class RouteError(Exception):
    """网关最终失败的结构化错误（ADR-0002 第 2 轮 / ADR-0003）。"""

    def __init__(self, message: str, cause: Optional[str] = None, attempted: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.attempted = attempted or []

    def to_dict(self) -> dict:
        return {"message": self.message, "cause": self.cause, "attempted": self.attempted}


@dataclass
class RouteResult:
    """route() 组合入口的完整输出。"""

    request: str
    classification: Classification
    recommendation: Recommendation
    client: Any  # RouterClient（懒加载，见 gateway.py）
