"""试点管理领域错误类型。"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """业务规则错误基类，携带稳定的错误码与结构化细节。"""

    code = "DOMAIN_ERROR"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFoundError(DomainError):
    code = "NOT_FOUND"


class ArtifactConflictError(DomainError):
    """同一制品版本重复登记但内容不一致。"""

    code = "ARTIFACT_CONFLICT"


class IncompatibleAdaptationError(DomainError):
    """适配方案超出组件能力声明。"""

    code = "INCOMPATIBLE_ADAPTATION"


class StageGateError(DomainError):
    """关键指标缺失、证据过期或组合被冻结，禁止晋级。"""

    code = "STAGE_GATE_BLOCKED"


class UnauthorizedApproverError(DomainError):
    """审批角色无权签发该类别豁免。"""

    code = "UNAUTHORIZED_APPROVER"


class FrozenError(DomainError):
    """组合已被冻结。"""

    code = "COMBINATION_FROZEN"


class RecalledError(DomainError):
    """组合已纳入回撤范围。"""

    code = "COMBINATION_RECALLED"


class InvalidTransitionError(DomainError):
    """非法的状态/阶段迁移。"""

    code = "INVALID_TRANSITION"
