"""跨境教育技术集成试点管理服务。

职责：
- 登记不可变的组件制品（摘要 + 能力声明 + 依赖契约）；
- 维护各校本地化适配方案（版本化，可追溯）；
- 按 兼容检查 → 小规模验证 → 观察期 → 推广评审 逐阶段收集证据并把关晋级；
- 失败回退、限时豁免（按类别限定审批角色）、严重问题冻结与回撤；
- 全部状态落盘，进程重启后原试点可继续。
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from .errors import (
    ArtifactConflictError,
    DomainError,
    FrozenError,
    IncompatibleAdaptationError,
    InvalidTransitionError,
    NotFoundError,
    RecalledError,
    StageGateError,
    UnauthorizedApproverError,
)
from .models import (
    CATEGORY_APPROVER_ROLES,
    METRIC_EXEMPTION_CATEGORY,
    STAGE_RULES,
    AdaptationPlan,
    ArtifactRegistration,
    Evidence,
    Exemption,
    PilotRecord,
    PilotStatus,
    Recall,
    RecallStatus,
    Stage,
    artifact_key,
    from_iso,
    pilot_id_for,
    plan_key,
    to_iso,
    utcnow,
)
from .store import JsonStore


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PilotService:
    def __init__(self, store_path: str | Path, clock: Callable[[], Any] = utcnow) -> None:
        self._clock = clock
        self._store = JsonStore(store_path)
        self._load()

    # ------------------------------------------------------------ 持久化

    def _load(self) -> None:
        data = self._store.data
        self._counters: dict[str, int] = {"exemption": 0, "recall": 0}
        self._counters.update(data.get("counters") or {})
        self._artifacts = {
            k: ArtifactRegistration.from_dict(v) for k, v in data.get("artifacts", {}).items()
        }
        self._plans = {k: AdaptationPlan.from_dict(v) for k, v in data.get("plans", {}).items()}
        self._pilots = {k: PilotRecord.from_dict(v) for k, v in data.get("pilots", {}).items()}
        self._exemptions = {k: Exemption.from_dict(v) for k, v in data.get("exemptions", {}).items()}
        self._recalls = {k: Recall.from_dict(v) for k, v in data.get("recalls", {}).items()}
        self._events: list[dict[str, Any]] = list(data.get("events", []))

    def _persist(self) -> None:
        self._store.data = {
            "counters": self._counters,
            "artifacts": {k: v.to_dict() for k, v in self._artifacts.items()},
            "plans": {k: v.to_dict() for k, v in self._plans.items()},
            "pilots": {k: v.to_dict() for k, v in self._pilots.items()},
            "exemptions": {k: v.to_dict() for k, v in self._exemptions.items()},
            "recalls": {k: v.to_dict() for k, v in self._recalls.items()},
            "events": self._events,
        }
        self._store.save()

    def _event(self, event_type: str, **details: Any) -> None:
        self._events.append({"ts": to_iso(self._clock()), "type": event_type, **details})

    # ------------------------------------------------------------ 制品登记

    @staticmethod
    def _validate_capabilities(capabilities: dict[str, Any]) -> None:
        if not isinstance(capabilities, dict):
            raise ValueError("能力声明必须是对象")
        for field_name in ("course_data_formats", "language_packs"):
            value = capabilities.get(field_name)
            if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
                raise ValueError(f"能力声明缺少有效的 {field_name} 列表")
        if not isinstance(capabilities.get("runtime"), dict):
            raise ValueError("能力声明缺少 runtime 运行能力对象")

    @staticmethod
    def _validate_dependencies(dependencies: list[dict[str, Any]]) -> None:
        if not isinstance(dependencies, list):
            raise ValueError("依赖契约必须是列表")
        for dep in dependencies:
            if not isinstance(dep, dict) or not dep.get("name") or not dep.get("constraint"):
                raise ValueError("依赖契约条目必须包含 name 与 constraint")

    def register_artifact(
        self,
        *,
        artifact_id: str,
        revision: int,
        display_name: str,
        artifact_digest: str,
        capabilities: dict[str, Any],
        dependencies: list[dict[str, Any]],
        registered_by: str,
    ) -> dict[str, Any]:
        """登记制品。同一版本重复登记：内容一致则复用，不一致则拒绝（不可变）。"""
        if not artifact_id or not display_name or not artifact_digest:
            raise ValueError("制品标识、名称与摘要均不能为空")
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("revision 必须为 >=1 的整数")
        self._validate_capabilities(capabilities)
        self._validate_dependencies(dependencies)

        key = artifact_key(artifact_id, revision)
        existing = self._artifacts.get(key)
        if existing is not None:
            incoming = {
                "display_name": display_name,
                "artifact_digest": artifact_digest,
                "capabilities": capabilities,
                "dependencies": dependencies,
            }
            if _canonical(existing.content_fingerprint()) != _canonical(incoming):
                raise ArtifactConflictError(
                    "同一制品版本已登记且内容不一致，制品登记不可变",
                    {"artifact_id": artifact_id, "revision": revision},
                )
            return {**existing.to_dict(), "reused": True}

        registration = ArtifactRegistration(
            artifact_id=artifact_id,
            revision=revision,
            display_name=display_name,
            artifact_digest=artifact_digest,
            capabilities=capabilities,
            dependencies=dependencies,
            registered_by=registered_by,
            registered_at=to_iso(self._clock()),
        )
        self._artifacts[key] = registration
        self._event("artifact.registered", artifact_id=artifact_id, revision=revision)
        self._persist()
        return {**registration.to_dict(), "reused": False}

    def get_artifact(self, artifact_id: str, revision: int) -> dict[str, Any]:
        return self._require_artifact(artifact_id, revision).to_dict()

    def _require_artifact(self, artifact_id: str, revision: int) -> ArtifactRegistration:
        registration = self._artifacts.get(artifact_key(artifact_id, revision))
        if registration is None:
            raise NotFoundError("制品版本未登记", {"artifact_id": artifact_id, "revision": revision})
        return registration

    def _resolve_revision(self, artifact_id: str, revision: int | None) -> int:
        if revision is not None:
            return revision
        revisions = [a.revision for a in self._artifacts.values() if a.artifact_id == artifact_id]
        if not revisions:
            raise NotFoundError("制品未登记", {"artifact_id": artifact_id})
        return max(revisions)

    # ------------------------------------------------------------ 适配方案

    @staticmethod
    def _check_adaptation(
        artifact: ArtifactRegistration,
        course_data_format: str,
        language_pack: str,
        runtime_profile: dict[str, Any],
    ) -> list[str]:
        """适配方案不得超出制品能力声明，返回问题列表（空为通过）。"""
        problems: list[str] = []
        caps = artifact.capabilities
        if course_data_format not in caps["course_data_formats"]:
            problems.append(f"课程数据格式 {course_data_format} 不在声明范围 {caps['course_data_formats']}")
        if language_pack not in caps["language_packs"]:
            problems.append(f"语言包 {language_pack} 不在声明范围 {caps['language_packs']}")
        runtime = caps["runtime"]
        if runtime_profile.get("offline_mode") and not runtime.get("offline_mode"):
            problems.append("适配要求离线运行，但制品未声明 offline_mode 能力")
        if "mem_gb" in runtime_profile and runtime_profile["mem_gb"] < runtime.get("min_mem_gb", 0):
            problems.append(f"内存配置 {runtime_profile['mem_gb']}GB 低于声明下限 {runtime.get('min_mem_gb')}GB")
        if "cpu" in runtime_profile and runtime_profile["cpu"] < runtime.get("min_cpu", 0):
            problems.append(f"CPU 配置 {runtime_profile['cpu']} 核低于声明下限 {runtime.get('min_cpu')} 核")
        return problems

    def upsert_adaptation(
        self,
        *,
        artifact_id: str,
        revision: int,
        school_id: str,
        course_data_format: str,
        language_pack: str,
        runtime_profile: dict[str, Any],
        operator: str,
    ) -> dict[str, Any]:
        """建立或更新某校的本地化适配方案；更新会升版本并保留历史快照。"""
        artifact = self._require_artifact(artifact_id, revision)
        if not school_id:
            raise ValueError("school_id 不能为空")
        problems = self._check_adaptation(artifact, course_data_format, language_pack, runtime_profile or {})
        if problems:
            raise IncompatibleAdaptationError("适配方案超出组件能力声明", {"problems": problems})

        key = plan_key(artifact_id, revision, school_id)
        now = to_iso(self._clock())
        plan = self._plans.get(key)
        if plan is None:
            plan = AdaptationPlan(
                artifact_id=artifact_id,
                revision=revision,
                school_id=school_id,
                course_data_format=course_data_format,
                language_pack=language_pack,
                runtime_profile=runtime_profile or {},
                version=1,
                created_by=operator,
                created_at=now,
                updated_at=now,
            )
            self._plans[key] = plan
        else:
            plan.history.append(plan.snapshot())
            plan.version += 1
            plan.course_data_format = course_data_format
            plan.language_pack = language_pack
            plan.runtime_profile = runtime_profile or {}
            plan.updated_at = now
        self._event(
            "adaptation.upserted",
            artifact_id=artifact_id,
            revision=revision,
            school_id=school_id,
            plan_version=plan.version,
        )
        self._persist()
        return plan.to_dict()

    # ------------------------------------------------------------ 试点开设（幂等）

    def open_pilot(
        self,
        *,
        artifact_id: str,
        revision: int,
        school_id: str,
        operator: str,
    ) -> dict[str, Any]:
        """开设试点。同一制品版本 + 学校的重复请求必须复用原记录。"""
        self._require_artifact(artifact_id, revision)
        plan = self._plans.get(plan_key(artifact_id, revision, school_id))
        if plan is None:
            raise NotFoundError(
                "该校尚未建立本地化适配方案，无法开设试点",
                {"artifact_id": artifact_id, "revision": revision, "school_id": school_id},
            )
        pid = pilot_id_for(artifact_id, revision, school_id)
        existing = self._pilots.get(pid)
        if existing is not None:
            return {**existing.to_dict(), "reused": True}
        if self._active_recall(artifact_id, revision) is not None:
            raise RecalledError(
                "该制品版本处于回撤范围，禁止新开试点",
                {"artifact_id": artifact_id, "revision": revision},
            )
        now = to_iso(self._clock())
        pilot = PilotRecord(
            pilot_id=pid,
            artifact_id=artifact_id,
            revision=revision,
            school_id=school_id,
            plan_version=plan.version,
            created_at=now,
            updated_at=now,
        )
        self._pilots[pid] = pilot
        self._event(
            "pilot.opened",
            pilot_id=pid,
            artifact_id=artifact_id,
            revision=revision,
            school_id=school_id,
            plan_version=plan.version,
            operator=operator,
        )
        self._persist()
        return {**pilot.to_dict(), "reused": False}

    def _require_pilot(self, pilot_id: str) -> PilotRecord:
        pilot = self._pilots.get(pilot_id)
        if pilot is None:
            raise NotFoundError("试点记录不存在", {"pilot_id": pilot_id})
        return pilot

    @staticmethod
    def _ensure_active(pilot: PilotRecord) -> None:
        if pilot.status == PilotStatus.FROZEN:
            raise FrozenError("组合已被冻结，等待回撤处理", {"pilot_id": pilot.pilot_id})
        if pilot.status == PilotStatus.RECALLED:
            raise RecalledError("组合已回撤，试点终止", {"pilot_id": pilot.pilot_id})

    # ------------------------------------------------------------ 证据与晋级

    def submit_evidence(
        self,
        pilot_id: str,
        *,
        metrics: dict[str, Any],
        submitted_by: str,
    ) -> dict[str, Any]:
        """为当前阶段提交证据；旧证据作废，有效期按阶段规则重新计算。"""
        pilot = self._require_pilot(pilot_id)
        self._ensure_active(pilot)
        if pilot.stage == Stage.PROMOTED:
            raise InvalidTransitionError("试点已推广，无需再提交证据")
        known = set(STAGE_RULES[pilot.stage]["metrics"])
        unknown = sorted(set(metrics) - known)
        if unknown:
            raise ValueError(f"证据包含当前阶段未知指标: {unknown}")
        now = self._clock()
        ttl_days = STAGE_RULES[pilot.stage]["ttl_days"]
        for old in pilot.evidence.get(pilot.stage, []):
            old.superseded = True
        evidence = Evidence(
            stage=pilot.stage,
            metrics=dict(metrics),
            submitted_by=submitted_by,
            submitted_at=to_iso(now),
            expires_at=to_iso(now + timedelta(days=ttl_days)),
            plan_version=pilot.plan_version,
        )
        pilot.evidence.setdefault(pilot.stage, []).append(evidence)
        pilot.updated_at = to_iso(now)
        self._event("evidence.submitted", pilot_id=pilot_id, stage=pilot.stage, submitted_by=submitted_by)
        self._persist()
        return evidence.to_dict()

    @staticmethod
    def _current_evidence(pilot: PilotRecord) -> Evidence | None:
        for evidence in reversed(pilot.evidence.get(pilot.stage, [])):
            if not evidence.superseded:
                return evidence
        return None

    def _valid_exemption(self, pilot: PilotRecord, metric: str) -> Exemption | None:
        now = self._clock()
        candidates = [
            e
            for e in self._exemptions.values()
            if e.artifact_id == pilot.artifact_id
            and e.revision == pilot.revision
            and e.school_id == pilot.school_id
            and e.metric == metric
            and e.is_valid(now)
        ]
        candidates.sort(key=lambda e: e.granted_at, reverse=True)
        return candidates[0] if candidates else None

    def _evaluate(self, pilot: PilotRecord) -> list[dict[str, Any]]:
        """评估当前阶段门禁，返回原因列表；blocking=True 的条目阻止晋级。"""
        if pilot.status == PilotStatus.FROZEN:
            return [{"code": "FROZEN", "blocking": True, "message": "组合已冻结，等待回撤处理"}]
        if pilot.status == PilotStatus.RECALLED:
            return [{"code": "RECALLED", "blocking": True, "message": "组合已回撤"}]
        if pilot.stage == Stage.PROMOTED:
            return []
        reasons: list[dict[str, Any]] = []
        evidence = self._current_evidence(pilot)
        if evidence is None:
            return [{"code": "NO_EVIDENCE", "blocking": True, "message": "当前阶段尚未提交有效证据"}]
        if from_iso(evidence.expires_at) <= self._clock():
            return [
                {
                    "code": "EVIDENCE_EXPIRED",
                    "blocking": True,
                    "message": "证据已过期，需重新收集",
                    "expired_at": evidence.expires_at,
                }
            ]
        for metric, (validator, label) in STAGE_RULES[pilot.stage]["metrics"].items():
            value = evidence.metrics.get(metric)
            if value is None:
                code, message = "METRIC_MISSING", f"关键指标缺失: {metric}（{label}）"
            elif not validator(value):
                code, message = "METRIC_FAILED", f"关键指标不达标: {metric}={value}（要求 {label}）"
            else:
                continue
            exemption = self._valid_exemption(pilot, metric)
            if exemption is not None:
                reasons.append(
                    {
                        "code": f"{code}_EXEMPTED",
                        "blocking": False,
                        "metric": metric,
                        "message": f"{message}；已由限时豁免覆盖",
                        "exemption_id": exemption.exemption_id,
                        "exemption_expires_at": exemption.expires_at,
                    }
                )
            else:
                reasons.append({"code": code, "blocking": True, "metric": metric, "message": message})
        return reasons

    def advance(self, pilot_id: str, *, operator: str) -> dict[str, Any]:
        """晋级到下一阶段；关键指标缺失或证据过期时不得晋级。"""
        pilot = self._require_pilot(pilot_id)
        self._ensure_active(pilot)
        if pilot.stage == Stage.PROMOTED:
            raise InvalidTransitionError("试点已处于推广终态")
        blocking = [r for r in self._evaluate(pilot) if r.get("blocking", True)]
        if blocking:
            raise StageGateError(
                "关键指标缺失或证据不满足要求，禁止晋级",
                {"pilot_id": pilot_id, "stage": pilot.stage, "reasons": blocking},
            )
        from_stage = pilot.stage
        pilot.stage = Stage.next(pilot.stage)
        pilot.updated_at = to_iso(self._clock())
        self._event(
            "pilot.advanced",
            pilot_id=pilot_id,
            from_stage=from_stage,
            to_stage=pilot.stage,
            plan_version=pilot.plan_version,
            operator=operator,
        )
        self._persist()
        return pilot.to_dict()

    def rollback(self, pilot_id: str, *, operator: str, reason: str) -> dict[str, Any]:
        """验证失败后回退到前一阶段，当前阶段证据作废。"""
        pilot = self._require_pilot(pilot_id)
        self._ensure_active(pilot)
        if pilot.stage == Stage.PROMOTED:
            raise InvalidTransitionError("已推广组合不可回退，请通过回撤流程处理")
        previous = Stage.previous(pilot.stage)
        if previous is None:
            raise InvalidTransitionError("已处于首个阶段，无法继续回退")
        for evidence in pilot.evidence.get(pilot.stage, []):
            evidence.superseded = True
        from_stage = pilot.stage
        pilot.stage = previous
        pilot.updated_at = to_iso(self._clock())
        self._event(
            "pilot.rolled_back",
            pilot_id=pilot_id,
            from_stage=from_stage,
            to_stage=previous,
            reason=reason,
            operator=operator,
        )
        self._persist()
        return pilot.to_dict()

    def sync_plan(self, pilot_id: str, *, operator: str) -> dict[str, Any]:
        """将试点钉住的适配方案版本同步到最新，便于追踪使用了哪份配置。"""
        pilot = self._require_pilot(pilot_id)
        self._ensure_active(pilot)
        plan = self._plans.get(plan_key(pilot.artifact_id, pilot.revision, pilot.school_id))
        if plan is None:
            raise NotFoundError("适配方案不存在", {"pilot_id": pilot_id})
        old_version = pilot.plan_version
        pilot.plan_version = plan.version
        pilot.updated_at = to_iso(self._clock())
        self._event(
            "pilot.plan_synced",
            pilot_id=pilot_id,
            from_plan_version=old_version,
            to_plan_version=plan.version,
            operator=operator,
        )
        self._persist()
        return pilot.to_dict()

    # ------------------------------------------------------------ 限时豁免

    def grant_exemption(
        self,
        *,
        artifact_id: str,
        revision: int,
        school_id: str,
        metric: str,
        approver_role: str,
        approver_name: str,
        ttl_hours: int,
        reason: str,
    ) -> dict[str, Any]:
        """签发限时风险豁免；不同指标类别必须由对应角色批准。"""
        self._require_artifact(artifact_id, revision)
        category = METRIC_EXEMPTION_CATEGORY.get(metric)
        if category is None:
            raise ValueError(f"未知指标，无法签发豁免: {metric}")
        allowed_roles = CATEGORY_APPROVER_ROLES[category]
        if approver_role not in allowed_roles:
            raise UnauthorizedApproverError(
                f"类别 {category} 的豁免须由 {list(allowed_roles)} 批准，当前角色 {approver_role} 无权",
                {"metric": metric, "category": category, "allowed_roles": list(allowed_roles)},
            )
        if not isinstance(ttl_hours, int) or ttl_hours <= 0:
            raise ValueError("豁免必须限时，ttl_hours 应为正整数")
        if not reason:
            raise ValueError("豁免必须说明理由")
        now = self._clock()
        self._counters["exemption"] += 1
        exemption = Exemption(
            exemption_id=f"EX-{self._counters['exemption']:04d}",
            artifact_id=artifact_id,
            revision=revision,
            school_id=school_id,
            metric=metric,
            category=category,
            approver_role=approver_role,
            approver_name=approver_name,
            reason=reason,
            granted_at=to_iso(now),
            expires_at=to_iso(now + timedelta(hours=ttl_hours)),
        )
        self._exemptions[exemption.exemption_id] = exemption
        self._event(
            "exemption.granted",
            exemption_id=exemption.exemption_id,
            artifact_id=artifact_id,
            revision=revision,
            school_id=school_id,
            metric=metric,
            approver_role=approver_role,
            expires_at=exemption.expires_at,
        )
        self._persist()
        return exemption.to_dict()

    # ------------------------------------------------------------ 冻结与回撤

    def _active_recall(self, artifact_id: str, revision: int) -> Recall | None:
        for recall in self._recalls.values():
            if (
                recall.artifact_id == artifact_id
                and recall.revision == revision
                and recall.status == RecallStatus.ACTIVE
            ):
                return recall
        return None

    def declare_severe_issue(
        self,
        *,
        artifact_id: str,
        revision: int,
        reported_by: str,
        description: str,
    ) -> dict[str, Any]:
        """严重问题上报：冻结相关组合、列出全部采用方并启动回撤。"""
        self._require_artifact(artifact_id, revision)
        if not description:
            raise ValueError("严重问题必须填写说明")
        existing = self._active_recall(artifact_id, revision)
        if existing is not None:
            return {**existing.to_dict(), "pending": existing.pending_schools(), "reused": True}

        now = to_iso(self._clock())
        self._counters["recall"] += 1
        recall_id = f"RC-{self._counters['recall']:04d}"
        affected = [
            p
            for p in self._pilots.values()
            if p.artifact_id == artifact_id and p.revision == revision and p.status != PilotStatus.RECALLED
        ]
        adopters = sorted({p.school_id for p in affected})
        for pilot in affected:
            pilot.status = PilotStatus.FROZEN
            pilot.recall_id = recall_id
            pilot.freeze_reason = description
            pilot.updated_at = now
        recall = Recall(
            recall_id=recall_id,
            artifact_id=artifact_id,
            revision=revision,
            reason=description,
            reported_by=reported_by,
            initiated_at=now,
            adopters=adopters,
            status=RecallStatus.ACTIVE if adopters else RecallStatus.COMPLETED,
        )
        self._recalls[recall_id] = recall
        self._event(
            "recall.declared",
            recall_id=recall_id,
            artifact_id=artifact_id,
            revision=revision,
            adopters=adopters,
            reported_by=reported_by,
        )
        self._persist()
        return {**recall.to_dict(), "pending": recall.pending_schools(), "reused": False}

    def confirm_withdrawal(self, recall_id: str, *, school_id: str, operator: str) -> dict[str, Any]:
        """采用方确认回撤；全部确认后回撤单完结。"""
        recall = self._recalls.get(recall_id)
        if recall is None:
            raise NotFoundError("回撤单不存在", {"recall_id": recall_id})
        if recall.status != RecallStatus.ACTIVE:
            raise InvalidTransitionError("回撤单已完结")
        if school_id not in recall.adopters:
            raise NotFoundError("该校不在回撤范围内", {"recall_id": recall_id, "school_id": school_id})
        if school_id in recall.withdrawals:
            return {**recall.to_dict(), "pending": recall.pending_schools(), "reused": True}
        now = to_iso(self._clock())
        recall.withdrawals[school_id] = now
        for pilot in self._pilots.values():
            if (
                pilot.recall_id == recall_id
                and pilot.school_id == school_id
                and pilot.status == PilotStatus.FROZEN
            ):
                pilot.status = PilotStatus.RECALLED
                pilot.updated_at = now
        if not recall.pending_schools():
            recall.status = RecallStatus.COMPLETED
        self._event("recall.withdrawn", recall_id=recall_id, school_id=school_id, operator=operator)
        self._persist()
        return {**recall.to_dict(), "pending": recall.pending_schools(), "reused": False}

    # ------------------------------------------------------------ 查询端点

    def get_pilot(self, pilot_id: str) -> dict[str, Any]:
        pilot = self._require_pilot(pilot_id)
        return self._pilot_detail(pilot)

    def _pilot_detail(self, pilot: PilotRecord) -> dict[str, Any]:
        reasons = self._evaluate(pilot)
        events = [e for e in self._events if e.get("pilot_id") == pilot.pilot_id][-20:]
        return {
            **pilot.to_dict(),
            "decision": self._decision_of(pilot),
            "gate_reasons": reasons,
            "recent_events": events,
        }

    @staticmethod
    def _decision_of(pilot: PilotRecord) -> str:
        return "放行" if pilot.stage == Stage.PROMOTED and pilot.status == PilotStatus.ACTIVE else "阻断"

    def release_board(self, artifact_id: str, revision: int | None = None) -> dict[str, Any]:
        """放行/阻断看板：逐校展示阶段、证据与阻断原因。"""
        revision = self._resolve_revision(artifact_id, revision)
        artifact = self._require_artifact(artifact_id, revision)
        rows = []
        for pilot in sorted(
            (p for p in self._pilots.values() if p.artifact_id == artifact_id and p.revision == revision),
            key=lambda p: p.school_id,
        ):
            evidence = self._current_evidence(pilot)
            rows.append(
                {
                    "school_id": pilot.school_id,
                    "pilot_id": pilot.pilot_id,
                    "stage": pilot.stage,
                    "status": pilot.status,
                    "decision": self._decision_of(pilot),
                    "gate_reasons": self._evaluate(pilot),
                    "plan_version": pilot.plan_version,
                    "current_evidence": evidence.to_dict() if evidence else None,
                }
            )
        return {
            "artifact_id": artifact_id,
            "revision": revision,
            "artifact_digest": artifact.artifact_digest,
            "schools": rows,
        }

    def adaptation_diff(self, artifact_id: str, revision: int | None = None) -> dict[str, Any]:
        """学校间适配差异：逐字段列出各校取值并标记存在差异的字段。"""
        revision = self._resolve_revision(artifact_id, revision)
        self._require_artifact(artifact_id, revision)
        plans = [
            p for p in self._plans.values() if p.artifact_id == artifact_id and p.revision == revision
        ]
        schools = {
            p.school_id: {
                "course_data_format": p.course_data_format,
                "language_pack": p.language_pack,
                "runtime_profile": p.runtime_profile,
                "plan_version": p.version,
            }
            for p in sorted(plans, key=lambda p: p.school_id)
        }
        differing = [
            field_name
            for field_name in ("course_data_format", "language_pack", "runtime_profile")
            if len({_canonical(s[field_name]) for s in schools.values()}) > 1
        ]
        return {
            "artifact_id": artifact_id,
            "revision": revision,
            "schools": schools,
            "differing_fields": differing,
        }

    def recall_scope(self, artifact_id: str, revision: int | None = None) -> dict[str, Any]:
        """当前回撤范围：回撤单、采用方清单与各校确认进度。"""
        recalls = [
            r
            for r in self._recalls.values()
            if r.artifact_id == artifact_id and (revision is None or r.revision == revision)
        ]
        return {
            "artifact_id": artifact_id,
            "recalls": [
                {**r.to_dict(), "pending": r.pending_schools()}
                for r in sorted(recalls, key=lambda r: r.recall_id)
            ],
        }
