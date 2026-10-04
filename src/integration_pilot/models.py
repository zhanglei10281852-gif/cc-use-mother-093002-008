"""试点管理领域模型与阶段门禁规则。

所有模型均可序列化为 JSON，配合 JsonStore 在进程重启后恢复原状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Stage:
    """试点阶段：兼容检查 → 小规模验证 → 观察期 → 推广评审 → 已推广（终态）。"""

    COMPATIBILITY_CHECK = "COMPATIBILITY_CHECK"
    SMALL_SCALE_VALIDATION = "SMALL_SCALE_VALIDATION"
    OBSERVATION = "OBSERVATION"
    PROMOTION_REVIEW = "PROMOTION_REVIEW"
    PROMOTED = "PROMOTED"

    ORDER = (COMPATIBILITY_CHECK, SMALL_SCALE_VALIDATION, OBSERVATION, PROMOTION_REVIEW)

    @classmethod
    def next(cls, stage: str) -> str:
        idx = cls.ORDER.index(stage)
        return cls.ORDER[idx + 1] if idx + 1 < len(cls.ORDER) else cls.PROMOTED

    @classmethod
    def previous(cls, stage: str) -> str | None:
        idx = cls.ORDER.index(stage)
        return cls.ORDER[idx - 1] if idx > 0 else None


class PilotStatus:
    ACTIVE = "ACTIVE"      # 正常推进中
    FROZEN = "FROZEN"      # 严重问题后被冻结，等待回撤确认
    RECALLED = "RECALLED"  # 已回撤（终态）


class RecallStatus:
    ACTIVE = "ACTIVE"        # 回撤进行中
    COMPLETED = "COMPLETED"  # 全部采用方已确认回撤


# ---------------------------------------------------------------- 阶段门禁规则

def _bool_true(value: Any) -> bool:
    return value is True


def _int_at_least(limit: int) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= limit


def _int_equal(limit: int) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and v == limit


def _num_at_most(limit: float) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v <= limit


def _num_at_least(limit: float) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= limit


# 每个阶段：证据有效期（天） + 关键指标校验器与中文说明。
STAGE_RULES: dict[str, dict[str, Any]] = {
    Stage.COMPATIBILITY_CHECK: {
        "ttl_days": 30,
        "metrics": {
            "course_data_format_ok": (_bool_true, "课程数据格式兼容"),
            "language_pack_ok": (_bool_true, "语言包兼容"),
            "runtime_ok": (_bool_true, "运行能力兼容"),
        },
    },
    Stage.SMALL_SCALE_VALIDATION: {
        "ttl_days": 60,
        "metrics": {
            "sessions_completed": (_int_at_least(3), "完成教学会话数>=3"),
            "error_rate": (_num_at_most(0.05), "错误率<=0.05"),
            "learner_satisfaction": (_num_at_least(0.7), "学员满意度>=0.7"),
        },
    },
    Stage.OBSERVATION: {
        "ttl_days": 90,
        "metrics": {
            "observation_days": (_int_at_least(14), "观察天数>=14"),
            "severity1_incidents": (_int_equal(0), "一级事故数=0"),
            "stability_score": (_num_at_least(0.9), "稳定性评分>=0.9"),
        },
    },
    Stage.PROMOTION_REVIEW: {
        "ttl_days": 45,
        "metrics": {
            "review_board_signoff": (_bool_true, "评审委员会签字"),
            "budget_confirmed": (_bool_true, "推广预算确认"),
        },
    },
}

# 指标 → 豁免类别；类别 → 有权审批的角色。限时豁免必须人岗匹配。
METRIC_EXEMPTION_CATEGORY: dict[str, str] = {
    "course_data_format_ok": "data_format",
    "language_pack_ok": "language",
    "runtime_ok": "runtime",
    "error_rate": "runtime",
    "stability_score": "runtime",
    "sessions_completed": "process",
    "learner_satisfaction": "process",
    "observation_days": "process",
    "severity1_incidents": "process",
    "review_board_signoff": "process",
    "budget_confirmed": "process",
}

CATEGORY_APPROVER_ROLES: dict[str, tuple[str, ...]] = {
    "data_format": ("data_governance_officer",),
    "language": ("academic_director",),
    "runtime": ("technology_director",),
    "process": ("steering_committee_chair",),
}


# ---------------------------------------------------------------- 自然键

def artifact_key(artifact_id: str, revision: int) -> str:
    return f"{artifact_id}@{revision}"


def plan_key(artifact_id: str, revision: int, school_id: str) -> str:
    return f"{artifact_id}@{revision}@{school_id}"


def pilot_id_for(artifact_id: str, revision: int, school_id: str) -> str:
    """试点记录使用确定性 ID：同一制品版本 + 学校天然幂等。"""
    import hashlib

    digest = hashlib.sha1(plan_key(artifact_id, revision, school_id).encode("utf-8")).hexdigest()
    return f"P-{digest[:12]}"


# ---------------------------------------------------------------- 模型

@dataclass
class ArtifactRegistration:
    """不可变的组件制品登记：摘要、能力声明与依赖契约。"""

    artifact_id: str
    revision: int
    display_name: str
    artifact_digest: str
    capabilities: dict[str, Any]
    dependencies: list[dict[str, Any]]
    registered_by: str
    registered_at: str

    @property
    def key(self) -> str:
        return artifact_key(self.artifact_id, self.revision)

    def content_fingerprint(self) -> dict[str, Any]:
        """用于幂等登记比对的内容字段（不含登记人/时间）。"""
        return {
            "display_name": self.display_name,
            "artifact_digest": self.artifact_digest,
            "capabilities": self.capabilities,
            "dependencies": self.dependencies,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "display_name": self.display_name,
            "artifact_digest": self.artifact_digest,
            "capabilities": self.capabilities,
            "dependencies": self.dependencies,
            "registered_by": self.registered_by,
            "registered_at": self.registered_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactRegistration":
        return cls(**data)


@dataclass
class AdaptationPlan:
    """某学校针对某制品版本的本地化适配方案（版本化，保留历史）。"""

    artifact_id: str
    revision: int
    school_id: str
    course_data_format: str
    language_pack: str
    runtime_profile: dict[str, Any]
    version: int
    created_by: str
    created_at: str
    updated_at: str
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return plan_key(self.artifact_id, self.revision, self.school_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "course_data_format": self.course_data_format,
            "language_pack": self.language_pack,
            "runtime_profile": self.runtime_profile,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "school_id": self.school_id,
            "course_data_format": self.course_data_format,
            "language_pack": self.language_pack,
            "runtime_profile": self.runtime_profile,
            "version": self.version,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdaptationPlan":
        return cls(**data)


@dataclass
class Evidence:
    """某阶段的一份证据：关键指标、提交人、有效期与所用适配方案版本。"""

    stage: str
    metrics: dict[str, Any]
    submitted_by: str
    submitted_at: str
    expires_at: str
    plan_version: int
    superseded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "metrics": self.metrics,
            "submitted_by": self.submitted_by,
            "submitted_at": self.submitted_at,
            "expires_at": self.expires_at,
            "plan_version": self.plan_version,
            "superseded": self.superseded,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(**data)


@dataclass
class PilotRecord:
    """同一制品版本 + 学校唯一对应的试点记录。"""

    pilot_id: str
    artifact_id: str
    revision: int
    school_id: str
    plan_version: int
    stage: str = Stage.COMPATIBILITY_CHECK
    status: str = PilotStatus.ACTIVE
    evidence: dict[str, list[Evidence]] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    recall_id: str | None = None
    freeze_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pilot_id": self.pilot_id,
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "school_id": self.school_id,
            "plan_version": self.plan_version,
            "stage": self.stage,
            "status": self.status,
            "evidence": {s: [e.to_dict() for e in items] for s, items in self.evidence.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "recall_id": self.recall_id,
            "freeze_reason": self.freeze_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PilotRecord":
        data = dict(data)
        data["evidence"] = {
            stage: [Evidence.from_dict(e) for e in items]
            for stage, items in data.get("evidence", {}).items()
        }
        return cls(**data)


@dataclass
class Exemption:
    """限时风险豁免：覆盖指定组合指定指标的缺失/不达标，到期自动失效。"""

    exemption_id: str
    artifact_id: str
    revision: int
    school_id: str
    metric: str
    category: str
    approver_role: str
    approver_name: str
    reason: str
    granted_at: str
    expires_at: str

    def is_valid(self, now: datetime) -> bool:
        return from_iso(self.expires_at) > now

    def to_dict(self) -> dict[str, Any]:
        return {
            "exemption_id": self.exemption_id,
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "school_id": self.school_id,
            "metric": self.metric,
            "category": self.category,
            "approver_role": self.approver_role,
            "approver_name": self.approver_name,
            "reason": self.reason,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Exemption":
        return cls(**data)


@dataclass
class Recall:
    """回撤单：冻结相关组合、记录全部采用方并跟踪回撤确认进度。"""

    recall_id: str
    artifact_id: str
    revision: int
    reason: str
    reported_by: str
    initiated_at: str
    adopters: list[str]
    withdrawals: dict[str, str] = field(default_factory=dict)
    status: str = RecallStatus.ACTIVE

    def pending_schools(self) -> list[str]:
        return [s for s in self.adopters if s not in self.withdrawals]

    def to_dict(self) -> dict[str, Any]:
        return {
            "recall_id": self.recall_id,
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "reason": self.reason,
            "reported_by": self.reported_by,
            "initiated_at": self.initiated_at,
            "adopters": self.adopters,
            "withdrawals": self.withdrawals,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Recall":
        return cls(**data)
