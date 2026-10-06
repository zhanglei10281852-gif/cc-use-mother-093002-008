"""跨境教育技术集成试点管理服务。

职责：
- 登记不可变的组件制品摘要、能力声明与依赖契约；
- 为学校建立本地化适配方案（依据能力声明校验）；
- 按 兼容检查 → 小规模验证 → 观察期 → 推广评审 逐阶段收集证据并晋级；
- 失败回退前一阶段；关键指标缺失或证据过期时不得晋级；
- 同一制品与学校的重复试点请求复用原记录；
- 限时风险豁免按指标类别由不同角色批准；
- 严重问题触发冻结组合、列出采用方并启动回撤；
- 所有状态持久化，进程重启后可继续原试点。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Callable, Optional

from .models import (
    PLAN_ACTIVE,
    PLAN_SUPERSEDED,
    PROMOTED,
    STAGE_LABELS,
    STAGES,
    STATUS_ACTIVE,
    STATUS_FROZEN,
    STATUS_LABELS,
    STATUS_PROMOTED,
    AdaptationPlan,
    ArtifactRegistration,
    CapabilityDeclaration,
    DependencyContract,
    Evidence,
    FreezeOrder,
    PilotRecord,
    RecallOrder,
    RiskExemption,
    StageTransition,
    format_ts,
    now_utc,
    parse_ts,
)
from .store import JsonStore


class PilotError(Exception):
    """服务层基础异常。"""


class NotFoundError(PilotError):
    """引用的实体不存在。"""


class RuleViolation(PilotError):
    """违反领域规则（如证据缺失、越权批准、重复登记冲突）。"""


# 每个阶段必须采集的关键指标
DEFAULT_STAGE_METRICS = {
    "COMPATIBILITY_CHECK": ("data_format_match", "language_pack_coverage", "runtime_capability"),
    "SMALL_SCALE_VALIDATION": ("success_rate", "error_count"),
    "OBSERVATION": ("stability_score", "incident_count"),
    "ROLLOUT_REVIEW": ("review_score",),
}

# 指标所属的豁免类别
METRIC_CATEGORIES = {
    "data_format_match": "compatibility",
    "language_pack_coverage": "compatibility",
    "runtime_capability": "compatibility",
    "success_rate": "quality",
    "error_count": "quality",
    "stability_score": "operations",
    "incident_count": "operations",
    "review_score": "governance",
}

# 各类别豁免的批准角色（不同类别由不同角色批准）
CATEGORY_APPROVER_ROLES = {
    "compatibility": ("INTEGRATION_ENGINEER",),
    "quality": ("ACADEMIC_DIRECTOR",),
    "operations": ("OPS_MANAGER",),
    "governance": ("PROGRAM_BOARD",),
}

DEFAULT_EVIDENCE_TTL_HOURS = 72
DEFAULT_EXEMPTION_TTL_HOURS = 24


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class PilotService:
    def __init__(
        self,
        store: JsonStore,
        clock: Callable = now_utc,
        stage_metrics: Optional[dict] = None,
    ):
        self.store = store
        self.clock = clock
        self.stage_metrics = stage_metrics or DEFAULT_STAGE_METRICS
        raw = self.store.load()
        self.artifacts = {k: ArtifactRegistration.from_dict(v) for k, v in raw["artifacts"].items()}
        self.plans = {k: AdaptationPlan.from_dict(v) for k, v in raw["plans"].items()}
        self.pilots = {k: PilotRecord.from_dict(v) for k, v in raw["pilots"].items()}
        self.freezes = {k: FreezeOrder.from_dict(v) for k, v in raw["freezes"].items()}
        self.recalls = {k: RecallOrder.from_dict(v) for k, v in raw["recalls"].items()}

    # ------------------------------------------------------------------ 持久化

    def _save(self) -> None:
        self.store.save(
            {
                "artifacts": {k: v.to_dict() for k, v in self.artifacts.items()},
                "plans": {k: v.to_dict() for k, v in self.plans.items()},
                "pilots": {k: v.to_dict() for k, v in self.pilots.items()},
                "freezes": {k: v.to_dict() for k, v in self.freezes.items()},
                "recalls": {k: v.to_dict() for k, v in self.recalls.items()},
            }
        )

    # ------------------------------------------------------------------ 制品登记

    def register_artifact(
        self,
        artifact_id: str,
        digest: str,
        display_name: str,
        capabilities: CapabilityDeclaration,
        dependencies=(),
        actor: str = "system",
    ) -> ArtifactRegistration:
        """登记制品；摘要不可变，同一标识重复登记须摘要一致（幂等复用）。"""
        existing = self.artifacts.get(artifact_id)
        if existing is not None:
            if existing.digest != digest:
                raise RuleViolation("制品摘要不可变：与已登记摘要不一致")
            return existing
        registration = ArtifactRegistration(
            artifact_id=artifact_id,
            digest=digest,
            display_name=display_name,
            capabilities=capabilities,
            dependencies=tuple(dependencies),
            registered_by=actor,
            registered_at=format_ts(self.clock()),
        )
        self.artifacts[artifact_id] = registration
        self._save()
        return registration

    # ------------------------------------------------------------------ 适配方案

    def _get_artifact(self, artifact_id: str) -> ArtifactRegistration:
        artifact = self.artifacts.get(artifact_id)
        if artifact is None:
            raise NotFoundError(f"制品未登记：{artifact_id}")
        return artifact

    def active_plan(self, artifact_id: str, school_id: str) -> Optional[AdaptationPlan]:
        for plan in self.plans.values():
            if (
                plan.artifact_id == artifact_id
                and plan.school_id == school_id
                and plan.status == PLAN_ACTIVE
            ):
                return plan
        return None

    def create_adaptation_plan(
        self,
        artifact_id: str,
        school_id: str,
        settings: dict,
        actor: str,
    ) -> AdaptationPlan:
        """为学校建立本地化适配方案，依据制品能力声明校验配置合法性。"""
        artifact = self._get_artifact(artifact_id)
        if not school_id:
            raise RuleViolation("必须指定学校")
        language_pack = settings.get("language_pack")
        data_format = settings.get("data_format")
        runtime_features = tuple(settings.get("runtime_features", ()))
        if language_pack not in artifact.capabilities.language_packs:
            raise RuleViolation(f"语言包不在制品能力声明内：{language_pack}")
        if data_format not in artifact.capabilities.data_formats:
            raise RuleViolation(f"课程数据格式不在制品能力声明内：{data_format}")
        unsupported = [f for f in runtime_features if f not in artifact.capabilities.runtime_capabilities]
        if unsupported:
            raise RuleViolation(f"运行能力不在制品能力声明内：{','.join(unsupported)}")

        previous = self.active_plan(artifact_id, school_id)
        revision = 1
        if previous is not None:
            previous.status = PLAN_SUPERSEDED
            revision = previous.revision + 1
        plan = AdaptationPlan(
            plan_id=_new_id("PLAN"),
            artifact_id=artifact_id,
            school_id=school_id,
            revision=revision,
            settings={
                "language_pack": language_pack,
                "data_format": data_format,
                "runtime_features": list(runtime_features),
            },
            status=PLAN_ACTIVE,
            created_by=actor,
            created_at=format_ts(self.clock()),
        )
        self.plans[plan.plan_id] = plan
        self._save()
        return plan

    # ------------------------------------------------------------------ 试点生命周期

    def _get_pilot(self, pilot_id: str) -> PilotRecord:
        pilot = self.pilots.get(pilot_id)
        if pilot is None:
            raise NotFoundError(f"试点记录不存在：{pilot_id}")
        return pilot

    def find_pilot(self, artifact_id: str, school_id: str) -> Optional[PilotRecord]:
        for pilot in self.pilots.values():
            if pilot.artifact_id == artifact_id and pilot.school_id == school_id:
                return pilot
        return None

    def request_pilot(self, artifact_id: str, school_id: str, actor: str) -> PilotRecord:
        """发起试点；同一制品与学校的重复请求复用原记录。"""
        self._get_artifact(artifact_id)
        existing = self.find_pilot(artifact_id, school_id)
        if existing is not None:
            return existing
        plan = self.active_plan(artifact_id, school_id)
        if plan is None:
            raise RuleViolation("缺少本地化适配方案，无法发起试点")
        now = format_ts(self.clock())
        pilot = PilotRecord(
            pilot_id=_new_id("PILOT"),
            artifact_id=artifact_id,
            school_id=school_id,
            plan_id=plan.plan_id,
            stage=STAGES[0],
            status=STATUS_ACTIVE,
            created_by=actor,
            created_at=now,
            updated_at=now,
        )
        self.pilots[pilot.pilot_id] = pilot
        self._save()
        return pilot

    def _require_mutable(self, pilot: PilotRecord) -> None:
        if pilot.status == STATUS_FROZEN:
            raise RuleViolation("组合已被冻结，等待回撤，禁止变更")
        if pilot.status == STATUS_PROMOTED:
            raise RuleViolation("试点已完成推广评审，禁止变更")

    def submit_evidence(
        self,
        pilot_id: str,
        metrics: dict,
        actor: str,
        ttl_hours: float = DEFAULT_EVIDENCE_TTL_HOURS,
        note: str = "",
    ) -> Evidence:
        """为当前阶段提交证据，证据带过期时间。"""
        pilot = self._get_pilot(pilot_id)
        self._require_mutable(pilot)
        if not metrics:
            raise RuleViolation("证据必须包含至少一项指标")
        now = self.clock()
        evidence = Evidence(
            evidence_id=_new_id("EV"),
            stage=pilot.stage,
            metrics=dict(metrics),
            collected_by=actor,
            collected_at=format_ts(now),
            expires_at=format_ts(now + timedelta(hours=ttl_hours)),
            note=note,
        )
        pilot.evidence.append(evidence)
        pilot.updated_at = format_ts(now)
        self._save()
        return evidence

    def grant_exemption(
        self,
        pilot_id: str,
        requirement: str,
        approver: str,
        approver_role: str,
        reason: str,
        ttl_hours: float = DEFAULT_EXEMPTION_TTL_HOURS,
    ) -> RiskExemption:
        """批准限时风险豁免；不同指标类别须由对应角色批准。"""
        pilot = self._get_pilot(pilot_id)
        self._require_mutable(pilot)
        category = METRIC_CATEGORIES.get(requirement)
        if category is None:
            raise RuleViolation(f"未知的关键指标，无法豁免：{requirement}")
        allowed_roles = CATEGORY_APPROVER_ROLES[category]
        if approver_role not in allowed_roles:
            raise RuleViolation(
                f"角色 {approver_role} 无权批准 {category} 类豁免，须为：{','.join(allowed_roles)}"
            )
        now = self.clock()
        exemption = RiskExemption(
            exemption_id=_new_id("EXM"),
            requirement=requirement,
            approver=approver,
            approver_role=approver_role,
            reason=reason,
            issued_at=format_ts(now),
            expires_at=format_ts(now + timedelta(hours=ttl_hours)),
        )
        pilot.exemptions.append(exemption)
        pilot.updated_at = format_ts(now)
        self._save()
        return exemption

    def _advance_blockers(self, pilot: PilotRecord) -> list:
        """返回当前阻碍晋级的全部原因；为空表示可以晋级。"""
        if pilot.status == STATUS_FROZEN:
            return ["组合已被冻结，等待回撤"]
        if pilot.status == STATUS_PROMOTED:
            return ["试点已完成推广评审"]
        now = self.clock()
        evidence = pilot.latest_evidence()
        if evidence is None:
            return ["缺少当前阶段证据"]
        if evidence.is_expired(now):
            return ["证据已过期，须重新采集"]
        required = self.stage_metrics.get(pilot.stage, ())
        missing = [m for m in required if m not in evidence.metrics]
        waived = {x.requirement for x in pilot.exemptions if x.is_active(now)}
        uncovered = [m for m in missing if m not in waived]
        if uncovered:
            return [f"关键指标缺失：{','.join(uncovered)}"]
        return []

    def advance(self, pilot_id: str, actor: str, reason: str = "") -> PilotRecord:
        """晋级到下一阶段；关键指标缺失或证据过期时不得晋级。"""
        pilot = self._get_pilot(pilot_id)
        blockers = self._advance_blockers(pilot)
        if blockers:
            raise RuleViolation("；".join(blockers))
        from_stage = pilot.stage
        index = STAGES.index(from_stage)
        now = format_ts(self.clock())
        if index == len(STAGES) - 1:
            pilot.stage = PROMOTED
            pilot.status = STATUS_PROMOTED
        else:
            pilot.stage = STAGES[index + 1]
        pilot.transitions.append(
            StageTransition("ADVANCE", from_stage, pilot.stage, actor, reason, now)
        )
        pilot.updated_at = now
        self._save()
        return pilot

    def rollback(self, pilot_id: str, actor: str, reason: str) -> PilotRecord:
        """失败回退到前一阶段。"""
        pilot = self._get_pilot(pilot_id)
        self._require_mutable(pilot)
        index = STAGES.index(pilot.stage)
        if index == 0:
            raise RuleViolation("已处于首个阶段，无法回退")
        from_stage = pilot.stage
        pilot.stage = STAGES[index - 1]
        now = format_ts(self.clock())
        pilot.transitions.append(
            StageTransition("ROLLBACK", from_stage, pilot.stage, actor, reason, now)
        )
        pilot.updated_at = now
        self._save()
        return pilot

    # ------------------------------------------------------------------ 严重问题与回撤

    def report_severe_issue(self, artifact_id: str, description: str, reporter: str) -> RecallOrder:
        """严重问题处置：冻结相关组合、列出全部采用方并启动回撤。"""
        self._get_artifact(artifact_id)
        now = format_ts(self.clock())
        recall = RecallOrder(
            recall_id=_new_id("RCL"),
            artifact_id=artifact_id,
            reason=description,
            severity="SEVERE",
            issued_by=reporter,
            issued_at=now,
            adopters=[],
        )
        for pilot in self.pilots.values():
            if pilot.artifact_id != artifact_id or pilot.status == STATUS_FROZEN:
                continue
            recall.adopters.append(
                {
                    "pilot_id": pilot.pilot_id,
                    "school_id": pilot.school_id,
                    "stage": pilot.stage,
                    "prior_status": pilot.status,
                }
            )
            freeze = FreezeOrder(
                freeze_id=_new_id("FRZ"),
                artifact_id=artifact_id,
                school_id=pilot.school_id,
                pilot_id=pilot.pilot_id,
                reason=description,
                issued_by=reporter,
                issued_at=now,
            )
            self.freezes[freeze.freeze_id] = freeze
            pilot.status = STATUS_FROZEN
            pilot.recall_id = recall.recall_id
            pilot.transitions.append(
                StageTransition("FREEZE", pilot.stage, pilot.stage, reporter, description, now)
            )
            pilot.updated_at = now
        self.recalls[recall.recall_id] = recall
        self._save()
        return recall

    # ------------------------------------------------------------------ 查询端点

    def release_decision(self, pilot_id: str) -> dict:
        """放行/阻断视图：当前状态、阻断原因与全部证据留痕。"""
        pilot = self._get_pilot(pilot_id)
        released = pilot.status == STATUS_PROMOTED
        blockers = [] if released else self._advance_blockers(pilot)
        return {
            "pilot_id": pilot.pilot_id,
            "artifact_id": pilot.artifact_id,
            "school_id": pilot.school_id,
            "plan_id": pilot.plan_id,
            "stage": pilot.stage,
            "stage_label": STAGE_LABELS.get(pilot.stage, pilot.stage),
            "status": pilot.status,
            "status_label": STATUS_LABELS.get(pilot.status, pilot.status),
            "released": released,
            "blocked": (not released) and bool(blockers),
            "blockers": blockers,
            "evidence": [e.to_dict() for e in pilot.evidence],
            "exemptions": [x.to_dict() for x in pilot.exemptions],
            "transitions": [t.to_dict() for t in pilot.transitions],
        }

    def adaptation_diff(self, artifact_id: str, school_a: str, school_b: str) -> dict:
        """学校间适配差异：比较两所学校当前生效的适配方案。"""
        plan_a = self.active_plan(artifact_id, school_a)
        plan_b = self.active_plan(artifact_id, school_b)
        if plan_a is None or plan_b is None:
            raise NotFoundError("两所学校必须均存在生效的适配方案")
        keys = sorted(set(plan_a.settings) | set(plan_b.settings))
        differences = {
            key: {school_a: plan_a.settings.get(key), school_b: plan_b.settings.get(key)}
            for key in keys
            if plan_a.settings.get(key) != plan_b.settings.get(key)
        }
        return {
            "artifact_id": artifact_id,
            "schools": [school_a, school_b],
            "plan_revisions": {school_a: plan_a.revision, school_b: plan_b.revision},
            "differences": differences,
        }

    def recall_scope(self, artifact_id: str) -> dict:
        """当前回撤范围：回撤指令、被冻结组合与全部采用方。"""
        recalls = [r for r in self.recalls.values() if r.artifact_id == artifact_id]
        freezes = [
            f for f in self.freezes.values() if f.artifact_id == artifact_id and f.status == "ACTIVE"
        ]
        return {
            "artifact_id": artifact_id,
            "recalls": [r.to_dict() for r in recalls],
            "frozen_combinations": [
                {
                    "freeze_id": f.freeze_id,
                    "pilot_id": f.pilot_id,
                    "school_id": f.school_id,
                    "reason": f.reason,
                }
                for f in freezes
            ],
            "adopters": [a for r in recalls for a in r.adopters],
        }
