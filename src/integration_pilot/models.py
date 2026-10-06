"""试点管理服务的领域模型。

阶段流水线：兼容检查 → 小规模验证 → 观察期 → 推广评审 → 已推广。
所有时间戳均为带时区的 ISO 8601 字符串，便于持久化与进程重启后恢复。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def format_ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# 试点阶段（按顺序晋级）
STAGES = ("COMPATIBILITY_CHECK", "SMALL_SCALE_VALIDATION", "OBSERVATION", "ROLLOUT_REVIEW")
PROMOTED = "PROMOTED"

STAGE_LABELS = {
    "COMPATIBILITY_CHECK": "兼容检查",
    "SMALL_SCALE_VALIDATION": "小规模验证",
    "OBSERVATION": "观察期",
    "ROLLOUT_REVIEW": "推广评审",
    "PROMOTED": "已推广",
}

# 试点状态
STATUS_ACTIVE = "ACTIVE"  # 进行中
STATUS_PROMOTED = "PROMOTED"  # 已通过推广评审
STATUS_FROZEN = "FROZEN"  # 因严重问题被冻结，等待回撤

STATUS_LABELS = {
    "ACTIVE": "进行中",
    "PROMOTED": "已推广",
    "FROZEN": "已冻结",
}

# 适配方案状态
PLAN_ACTIVE = "ACTIVE"
PLAN_SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class CapabilityDeclaration:
    """组件声明的运行能力：课程数据格式、语言包、运行特性。"""

    data_formats: tuple
    language_packs: tuple
    runtime_capabilities: tuple

    def __post_init__(self) -> None:
        if not self.data_formats or not self.language_packs:
            raise ValueError("能力声明必须包含课程数据格式与语言包")

    def to_dict(self) -> dict:
        return {
            "data_formats": list(self.data_formats),
            "language_packs": list(self.language_packs),
            "runtime_capabilities": list(self.runtime_capabilities),
        }

    @staticmethod
    def from_dict(data: dict) -> "CapabilityDeclaration":
        return CapabilityDeclaration(
            data_formats=tuple(data["data_formats"]),
            language_packs=tuple(data["language_packs"]),
            runtime_capabilities=tuple(data.get("runtime_capabilities", ())),
        )


@dataclass(frozen=True)
class DependencyContract:
    """组件的依赖契约：依赖名称与版本约束。"""

    name: str
    constraint: str
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.constraint:
            raise ValueError("依赖契约必须包含名称与版本约束")

    def to_dict(self) -> dict:
        return {"name": self.name, "constraint": self.constraint, "optional": self.optional}

    @staticmethod
    def from_dict(data: dict) -> "DependencyContract":
        return DependencyContract(data["name"], data["constraint"], data.get("optional", False))


@dataclass(frozen=True)
class ArtifactRegistration:
    """组件制品登记：摘要不可变，能力声明与依赖契约随登记固定。"""

    artifact_id: str
    digest: str
    display_name: str
    capabilities: CapabilityDeclaration
    dependencies: tuple
    registered_by: str
    registered_at: str

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.digest or not self.registered_by:
            raise ValueError("制品登记信息不完整")

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "digest": self.digest,
            "display_name": self.display_name,
            "capabilities": self.capabilities.to_dict(),
            "dependencies": [d.to_dict() for d in self.dependencies],
            "registered_by": self.registered_by,
            "registered_at": self.registered_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "ArtifactRegistration":
        return ArtifactRegistration(
            artifact_id=data["artifact_id"],
            digest=data["digest"],
            display_name=data["display_name"],
            capabilities=CapabilityDeclaration.from_dict(data["capabilities"]),
            dependencies=tuple(DependencyContract.from_dict(d) for d in data.get("dependencies", [])),
            registered_by=data["registered_by"],
            registered_at=data["registered_at"],
        )


@dataclass
class AdaptationPlan:
    """学校级本地化适配方案；同一制品与学校的新方案会取代旧方案并递增版本。"""

    plan_id: str
    artifact_id: str
    school_id: str
    revision: int
    settings: dict
    status: str
    created_by: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "artifact_id": self.artifact_id,
            "school_id": self.school_id,
            "revision": self.revision,
            "settings": self.settings,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "AdaptationPlan":
        return AdaptationPlan(
            plan_id=data["plan_id"],
            artifact_id=data["artifact_id"],
            school_id=data["school_id"],
            revision=data["revision"],
            settings=dict(data["settings"]),
            status=data["status"],
            created_by=data["created_by"],
            created_at=data["created_at"],
        )


@dataclass
class Evidence:
    """阶段证据：包含指标集合与过期时间，过期后不得用于晋级。"""

    evidence_id: str
    stage: str
    metrics: dict
    collected_by: str
    collected_at: str
    expires_at: str
    note: str = ""

    def is_expired(self, now: datetime) -> bool:
        return parse_ts(self.expires_at) <= now

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "stage": self.stage,
            "metrics": self.metrics,
            "collected_by": self.collected_by,
            "collected_at": self.collected_at,
            "expires_at": self.expires_at,
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: dict) -> "Evidence":
        return Evidence(
            evidence_id=data["evidence_id"],
            stage=data["stage"],
            metrics=dict(data["metrics"]),
            collected_by=data["collected_by"],
            collected_at=data["collected_at"],
            expires_at=data["expires_at"],
            note=data.get("note", ""),
        )


@dataclass
class StageTransition:
    """阶段流转记录：晋级、回退、冻结均留痕。"""

    kind: str  # ADVANCE / ROLLBACK / FREEZE
    from_stage: Optional[str]
    to_stage: Optional[str]
    actor: str
    reason: str
    at: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "actor": self.actor,
            "reason": self.reason,
            "at": self.at,
        }

    @staticmethod
    def from_dict(data: dict) -> "StageTransition":
        return StageTransition(
            kind=data["kind"],
            from_stage=data.get("from_stage"),
            to_stage=data.get("to_stage"),
            actor=data["actor"],
            reason=data["reason"],
            at=data["at"],
        )


@dataclass
class RiskExemption:
    """限时风险豁免：由指定角色批准，过期后自动失效。"""

    exemption_id: str
    requirement: str
    approver: str
    approver_role: str
    reason: str
    issued_at: str
    expires_at: str

    def is_active(self, now: datetime) -> bool:
        return parse_ts(self.expires_at) > now

    def to_dict(self) -> dict:
        return {
            "exemption_id": self.exemption_id,
            "requirement": self.requirement,
            "approver": self.approver,
            "approver_role": self.approver_role,
            "reason": self.reason,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "RiskExemption":
        return RiskExemption(
            exemption_id=data["exemption_id"],
            requirement=data["requirement"],
            approver=data["approver"],
            approver_role=data["approver_role"],
            reason=data["reason"],
            issued_at=data["issued_at"],
            expires_at=data["expires_at"],
        )


@dataclass
class PilotRecord:
    """试点记录：同一制品与学校仅有一份，重复请求复用原记录。"""

    pilot_id: str
    artifact_id: str
    school_id: str
    plan_id: str
    stage: str
    status: str
    created_by: str
    created_at: str
    updated_at: str
    evidence: list = field(default_factory=list)
    transitions: list = field(default_factory=list)
    exemptions: list = field(default_factory=list)
    recall_id: Optional[str] = None

    def latest_evidence(self) -> Optional[Evidence]:
        current = [e for e in self.evidence if e.stage == self.stage]
        if not current:
            return None
        return max(current, key=lambda e: e.collected_at)

    def to_dict(self) -> dict:
        return {
            "pilot_id": self.pilot_id,
            "artifact_id": self.artifact_id,
            "school_id": self.school_id,
            "plan_id": self.plan_id,
            "stage": self.stage,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "evidence": [e.to_dict() for e in self.evidence],
            "transitions": [t.to_dict() for t in self.transitions],
            "exemptions": [x.to_dict() for x in self.exemptions],
            "recall_id": self.recall_id,
        }

    @staticmethod
    def from_dict(data: dict) -> "PilotRecord":
        return PilotRecord(
            pilot_id=data["pilot_id"],
            artifact_id=data["artifact_id"],
            school_id=data["school_id"],
            plan_id=data["plan_id"],
            stage=data["stage"],
            status=data["status"],
            created_by=data["created_by"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
            transitions=[StageTransition.from_dict(t) for t in data.get("transitions", [])],
            exemptions=[RiskExemption.from_dict(x) for x in data.get("exemptions", [])],
            recall_id=data.get("recall_id"),
        )


@dataclass
class FreezeOrder:
    """冻结指令：针对具体的制品 × 学校组合。"""

    freeze_id: str
    artifact_id: str
    school_id: str
    pilot_id: str
    reason: str
    issued_by: str
    issued_at: str
    status: str = "ACTIVE"

    def to_dict(self) -> dict:
        return {
            "freeze_id": self.freeze_id,
            "artifact_id": self.artifact_id,
            "school_id": self.school_id,
            "pilot_id": self.pilot_id,
            "reason": self.reason,
            "issued_by": self.issued_by,
            "issued_at": self.issued_at,
            "status": self.status,
        }

    @staticmethod
    def from_dict(data: dict) -> "FreezeOrder":
        return FreezeOrder(
            freeze_id=data["freeze_id"],
            artifact_id=data["artifact_id"],
            school_id=data["school_id"],
            pilot_id=data["pilot_id"],
            reason=data["reason"],
            issued_by=data["issued_by"],
            issued_at=data["issued_at"],
            status=data.get("status", "ACTIVE"),
        )


@dataclass
class RecallOrder:
    """回撤指令：严重问题触发，列出全部采用方。"""

    recall_id: str
    artifact_id: str
    reason: str
    severity: str
    issued_by: str
    issued_at: str
    adopters: list
    status: str = "INITIATED"

    def to_dict(self) -> dict:
        return {
            "recall_id": self.recall_id,
            "artifact_id": self.artifact_id,
            "reason": self.reason,
            "severity": self.severity,
            "issued_by": self.issued_by,
            "issued_at": self.issued_at,
            "adopters": self.adopters,
            "status": self.status,
        }

    @staticmethod
    def from_dict(data: dict) -> "RecallOrder":
        return RecallOrder(
            recall_id=data["recall_id"],
            artifact_id=data["artifact_id"],
            reason=data["reason"],
            severity=data["severity"],
            issued_by=data["issued_by"],
            issued_at=data["issued_at"],
            adopters=list(data["adopters"]),
            status=data.get("status", "INITIATED"),
        )
