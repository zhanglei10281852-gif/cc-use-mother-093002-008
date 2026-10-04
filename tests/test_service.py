import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from integration_pilot import (  # noqa: E402
    ArtifactConflictError,
    FrozenError,
    IncompatibleAdaptationError,
    InvalidTransitionError,
    NotFoundError,
    PilotService,
    RecalledError,
    Stage,
    StageGateError,
    UnauthorizedApproverError,
)


class FakeClock:
    def __init__(self):
        self.moment = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


CAPABILITIES = {
    "course_data_formats": ["xAPI", "SCORM1.2"],
    "language_packs": ["zh-CN", "vi-VN"],
    "runtime": {"offline_mode": True, "min_mem_gb": 4, "min_cpu": 2},
}
DEPENDENCIES = [{"name": "lms-core", "constraint": ">=4.2"}]

COMPAT_OK = {"course_data_format_ok": True, "language_pack_ok": True, "runtime_ok": True}
SMALL_SCALE_OK = {"sessions_completed": 4, "error_rate": 0.01, "learner_satisfaction": 0.9}
OBSERVATION_OK = {"observation_days": 20, "severity1_incidents": 0, "stability_score": 0.95}
REVIEW_OK = {"review_board_signoff": True, "budget_confirmed": True}


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "pilot.json"
        self.clock = FakeClock()
        self.svc = PilotService(self.store, clock=self.clock)
        self.svc.register_artifact(
            artifact_id="smart-lesson",
            revision=3,
            display_name="智能备课组件",
            artifact_digest="sha256:abc",
            capabilities=CAPABILITIES,
            dependencies=DEPENDENCIES,
            registered_by="登记员",
        )

    def make_plan(self, school="SCH-A", **overrides):
        params = dict(
            artifact_id="smart-lesson",
            revision=3,
            school_id=school,
            course_data_format="xAPI",
            language_pack="vi-VN",
            runtime_profile={"offline_mode": True, "mem_gb": 8},
            operator="适配员",
        )
        params.update(overrides)
        return self.svc.upsert_adaptation(**params)

    def make_pilot(self, school="SCH-A"):
        self.make_plan(school)
        return self.svc.open_pilot(
            artifact_id="smart-lesson", revision=3, school_id=school, operator="操作员"
        )

    def advance_stage(self, pilot_id, metrics):
        self.svc.submit_evidence(pilot_id, metrics=metrics, submitted_by="提交人")
        return self.svc.advance(pilot_id, operator="操作员")


class ArtifactRegistrationTests(ServiceTestBase):
    def test_reregister_same_content_is_idempotent(self):
        again = self.svc.register_artifact(
            artifact_id="smart-lesson",
            revision=3,
            display_name="智能备课组件",
            artifact_digest="sha256:abc",
            capabilities=CAPABILITIES,
            dependencies=DEPENDENCIES,
            registered_by="另一个人",
        )
        self.assertTrue(again["reused"])

    def test_reregister_with_different_content_is_rejected(self):
        with self.assertRaises(ArtifactConflictError):
            self.svc.register_artifact(
                artifact_id="smart-lesson",
                revision=3,
                display_name="智能备课组件",
                artifact_digest="sha256:CHANGED",
                capabilities=CAPABILITIES,
                dependencies=DEPENDENCIES,
                registered_by="登记员",
            )

    def test_invalid_capability_declaration_rejected(self):
        with self.assertRaises(ValueError):
            self.svc.register_artifact(
                artifact_id="bad", revision=1, display_name="坏组件",
                artifact_digest="sha256:x", capabilities={"runtime": {}},
                dependencies=[], registered_by="登记员",
            )


class AdaptationTests(ServiceTestBase):
    def test_adaptation_outside_declared_capabilities_rejected(self):
        with self.assertRaises(IncompatibleAdaptationError):
            self.make_plan(language_pack="th-TH")
        with self.assertRaises(IncompatibleAdaptationError):
            self.make_plan(runtime_profile={"offline_mode": True, "mem_gb": 2})

    def test_plan_update_bumps_version_and_keeps_history(self):
        self.make_plan()
        updated = self.make_plan(language_pack="zh-CN")
        self.assertEqual(updated["version"], 2)
        self.assertEqual(len(updated["history"]), 1)
        self.assertEqual(updated["history"][0]["language_pack"], "vi-VN")

    def test_adaptation_diff_marks_differing_fields(self):
        self.make_plan("SCH-A", language_pack="vi-VN")
        self.make_plan("SCH-B", language_pack="zh-CN", course_data_format="SCORM1.2")
        diff = self.svc.adaptation_diff("smart-lesson", 3)
        self.assertEqual(
            set(diff["differing_fields"]), {"course_data_format", "language_pack"}
        )
        self.assertEqual(diff["schools"]["SCH-B"]["language_pack"], "zh-CN")


class PilotLifecycleTests(ServiceTestBase):
    def test_duplicate_open_reuses_original_record(self):
        first = self.make_pilot()
        second = self.svc.open_pilot(
            artifact_id="smart-lesson", revision=3, school_id="SCH-A", operator="操作员"
        )
        self.assertEqual(first["pilot_id"], second["pilot_id"])
        self.assertTrue(second["reused"])

    def test_open_requires_adaptation_plan(self):
        with self.assertRaises(NotFoundError):
            self.svc.open_pilot(
                artifact_id="smart-lesson", revision=3, school_id="SCH-NOPE", operator="操作员"
            )

    def test_advance_blocked_without_evidence(self):
        pilot = self.make_pilot()
        with self.assertRaises(StageGateError) as ctx:
            self.svc.advance(pilot["pilot_id"], operator="操作员")
        codes = [r["code"] for r in ctx.exception.details["reasons"]]
        self.assertIn("NO_EVIDENCE", codes)

    def test_advance_blocked_on_missing_metric(self):
        pilot = self.make_pilot()
        self.svc.submit_evidence(
            pilot["pilot_id"],
            metrics={"course_data_format_ok": True, "language_pack_ok": True},
            submitted_by="提交人",
        )
        with self.assertRaises(StageGateError) as ctx:
            self.svc.advance(pilot["pilot_id"], operator="操作员")
        missing = [r["metric"] for r in ctx.exception.details["reasons"] if r["code"] == "METRIC_MISSING"]
        self.assertEqual(missing, ["runtime_ok"])

    def test_advance_blocked_on_failed_metric(self):
        pilot = self.make_pilot()
        self.svc.submit_evidence(
            pilot["pilot_id"],
            metrics={"course_data_format_ok": True, "language_pack_ok": True, "runtime_ok": False},
            submitted_by="提交人",
        )
        with self.assertRaises(StageGateError) as ctx:
            self.svc.advance(pilot["pilot_id"], operator="操作员")
        failed = [r["metric"] for r in ctx.exception.details["reasons"] if r["code"] == "METRIC_FAILED"]
        self.assertEqual(failed, ["runtime_ok"])

    def test_advance_blocked_on_expired_evidence(self):
        pilot = self.make_pilot()
        self.svc.submit_evidence(pilot["pilot_id"], metrics=COMPAT_OK, submitted_by="提交人")
        self.clock.advance(days=31)  # 兼容检查证据有效期 30 天
        with self.assertRaises(StageGateError) as ctx:
            self.svc.advance(pilot["pilot_id"], operator="操作员")
        codes = [r["code"] for r in ctx.exception.details["reasons"]]
        self.assertIn("EVIDENCE_EXPIRED", codes)

    def test_unknown_metric_rejected(self):
        pilot = self.make_pilot()
        with self.assertRaises(ValueError):
            self.svc.submit_evidence(
                pilot["pilot_id"], metrics={"typo_metric": True}, submitted_by="提交人"
            )

    def test_full_path_to_promoted(self):
        pilot = self.make_pilot()
        pid = pilot["pilot_id"]
        self.advance_stage(pid, COMPAT_OK)
        self.advance_stage(pid, SMALL_SCALE_OK)
        self.advance_stage(pid, OBSERVATION_OK)
        final = self.advance_stage(pid, REVIEW_OK)
        self.assertEqual(final["stage"], Stage.PROMOTED)
        board = self.svc.release_board("smart-lesson", 3)
        self.assertEqual(board["schools"][0]["decision"], "放行")

    def test_rollback_returns_to_previous_stage(self):
        pilot = self.make_pilot()
        pid = pilot["pilot_id"]
        self.advance_stage(pid, COMPAT_OK)
        rolled = self.svc.rollback(pid, operator="操作员", reason="小规模验证环境不达标")
        self.assertEqual(rolled["stage"], Stage.COMPATIBILITY_CHECK)
        # 兼容检查证据仍在有效期内，可凭原证据重新晋级
        again = self.svc.advance(pid, operator="操作员")
        self.assertEqual(again["stage"], Stage.SMALL_SCALE_VALIDATION)

    def test_rollback_invalidates_current_stage_evidence(self):
        pilot = self.make_pilot()
        pid = pilot["pilot_id"]
        self.advance_stage(pid, COMPAT_OK)
        self.svc.submit_evidence(pid, metrics=SMALL_SCALE_OK, submitted_by="提交人")
        self.svc.rollback(pid, operator="操作员", reason="数据异常")
        # 回退后晋级到小规模验证，再晋级时上一阶段证据已作废
        self.svc.advance(pid, operator="操作员")
        with self.assertRaises(StageGateError):
            self.svc.advance(pid, operator="操作员")

    def test_rollback_at_first_stage_rejected(self):
        pilot = self.make_pilot()
        with self.assertRaises(InvalidTransitionError):
            self.svc.rollback(pilot["pilot_id"], operator="操作员", reason="无处可退")


class ExemptionTests(ServiceTestBase):
    def test_wrong_role_cannot_approve(self):
        self.make_pilot()
        with self.assertRaises(UnauthorizedApproverError):
            self.svc.grant_exemption(
                artifact_id="smart-lesson", revision=3, school_id="SCH-A",
                metric="language_pack_ok", approver_role="technology_director",
                approver_name="技术主管", ttl_hours=24, reason="语言包临时缺审",
            )

    def test_exemption_covers_missing_metric_until_expiry(self):
        pilot = self.make_pilot()
        pid = pilot["pilot_id"]
        self.svc.submit_evidence(
            pid,
            metrics={"course_data_format_ok": True, "runtime_ok": True},
            submitted_by="提交人",
        )
        self.svc.grant_exemption(
            artifact_id="smart-lesson", revision=3, school_id="SCH-A",
            metric="language_pack_ok", approver_role="academic_director",
            approver_name="教学主管", ttl_hours=24, reason="越南语包复核流程中",
        )
        advanced = self.svc.advance(pid, operator="操作员")
        self.assertEqual(advanced["stage"], Stage.SMALL_SCALE_VALIDATION)

    def test_expired_exemption_does_not_count(self):
        pilot = self.make_pilot()
        pid = pilot["pilot_id"]
        self.svc.submit_evidence(
            pid,
            metrics={"course_data_format_ok": True, "runtime_ok": True},
            submitted_by="提交人",
        )
        self.svc.grant_exemption(
            artifact_id="smart-lesson", revision=3, school_id="SCH-A",
            metric="language_pack_ok", approver_role="academic_director",
            approver_name="教学主管", ttl_hours=1, reason="短时限豁免",
        )
        self.clock.advance(hours=2)
        with self.assertRaises(StageGateError):
            self.svc.advance(pid, operator="操作员")


class RecallTests(ServiceTestBase):
    def test_severe_issue_freezes_combinations_and_lists_adopters(self):
        pa = self.make_pilot("SCH-A")
        self.make_pilot("SCH-B")
        self.advance_stage(pa["pilot_id"], COMPAT_OK)
        recall = self.svc.declare_severe_issue(
            artifact_id="smart-lesson", revision=3,
            reported_by="值班员", description="越界读取课程数据",
        )
        self.assertEqual(recall["adopters"], ["SCH-A", "SCH-B"])
        self.assertEqual(recall["status"], "ACTIVE")
        with self.assertRaises(FrozenError):
            self.svc.advance(pa["pilot_id"], operator="操作员")
        with self.assertRaises(FrozenError):
            self.svc.submit_evidence(pa["pilot_id"], metrics=COMPAT_OK, submitted_by="提交人")

    def test_new_pilot_blocked_during_recall_but_existing_reused(self):
        self.make_pilot("SCH-A")
        self.make_plan("SCH-NEW")  # 已建适配方案但尚未开设试点
        self.svc.declare_severe_issue(
            artifact_id="smart-lesson", revision=3,
            reported_by="值班员", description="严重缺陷",
        )
        with self.assertRaises(RecalledError):
            self.svc.open_pilot(
                artifact_id="smart-lesson", revision=3, school_id="SCH-NEW", operator="操作员"
            )
        reused = self.svc.open_pilot(
            artifact_id="smart-lesson", revision=3, school_id="SCH-A", operator="操作员"
        )
        self.assertTrue(reused["reused"])

    def test_withdrawal_progress_completes_recall(self):
        self.make_pilot("SCH-A")
        self.make_pilot("SCH-B")
        recall = self.svc.declare_severe_issue(
            artifact_id="smart-lesson", revision=3,
            reported_by="值班员", description="严重缺陷",
        )
        rid = recall["recall_id"]
        mid = self.svc.confirm_withdrawal(rid, school_id="SCH-A", operator="操作员")
        self.assertEqual(mid["status"], "ACTIVE")
        self.assertEqual(mid["pending"], ["SCH-B"])
        done = self.svc.confirm_withdrawal(rid, school_id="SCH-B", operator="操作员")
        self.assertEqual(done["status"], "COMPLETED")
        scope = self.svc.recall_scope("smart-lesson", 3)
        self.assertEqual(scope["recalls"][0]["pending"], [])
        pilot = self.svc.get_pilot(pilot_id_for_test("smart-lesson", 3, "SCH-A"))
        self.assertEqual(pilot["status"], "RECALLED")

    def test_double_declaration_reuses_recall(self):
        self.make_pilot("SCH-A")
        first = self.svc.declare_severe_issue(
            artifact_id="smart-lesson", revision=3,
            reported_by="值班员", description="严重缺陷",
        )
        second = self.svc.declare_severe_issue(
            artifact_id="smart-lesson", revision=3,
            reported_by="另一个人", description="重复上报",
        )
        self.assertEqual(first["recall_id"], second["recall_id"])
        self.assertTrue(second["reused"])


class PersistenceTests(ServiceTestBase):
    def test_restart_continues_original_pilot(self):
        pilot = self.make_pilot()
        pid = pilot["pilot_id"]
        self.advance_stage(pid, COMPAT_OK)
        # 模拟操作员重启进程：同一存储路径新建服务实例
        restarted = PilotService(self.store, clock=self.clock)
        detail = restarted.get_pilot(pid)
        self.assertEqual(detail["stage"], Stage.SMALL_SCALE_VALIDATION)
        continued = self.advance_stage_restarted(restarted, pid, SMALL_SCALE_OK)
        self.assertEqual(continued["stage"], Stage.OBSERVATION)

    def advance_stage_restarted(self, svc, pid, metrics):
        svc.submit_evidence(pid, metrics=metrics, submitted_by="提交人")
        return svc.advance(pid, operator="操作员")

    def test_exemptions_and_recalls_survive_restart(self):
        self.make_pilot("SCH-A")
        self.svc.grant_exemption(
            artifact_id="smart-lesson", revision=3, school_id="SCH-A",
            metric="runtime_ok", approver_role="technology_director",
            approver_name="技术主管", ttl_hours=48, reason="运行时热修复中",
        )
        recall = self.svc.declare_severe_issue(
            artifact_id="smart-lesson", revision=3,
            reported_by="值班员", description="严重缺陷",
        )
        restarted = PilotService(self.store, clock=self.clock)
        scope = restarted.recall_scope("smart-lesson", 3)
        self.assertEqual(scope["recalls"][0]["recall_id"], recall["recall_id"])
        done = restarted.confirm_withdrawal(recall["recall_id"], school_id="SCH-A", operator="操作员")
        self.assertEqual(done["status"], "COMPLETED")


class QueryTests(ServiceTestBase):
    def test_release_board_shows_block_reasons(self):
        pilot = self.make_pilot()
        self.svc.submit_evidence(
            pilot["pilot_id"],
            metrics={"course_data_format_ok": True},
            submitted_by="提交人",
        )
        board = self.svc.release_board("smart-lesson", 3)
        row = board["schools"][0]
        self.assertEqual(row["decision"], "阻断")
        codes = [r["code"] for r in row["gate_reasons"]]
        self.assertIn("METRIC_MISSING", codes)
        self.assertEqual(board["artifact_digest"], "sha256:abc")

    def test_release_board_defaults_to_latest_revision(self):
        self.make_pilot()
        board = self.svc.release_board("smart-lesson")
        self.assertEqual(board["revision"], 3)

    def test_pilot_detail_traces_plan_version_and_events(self):
        pilot = self.make_pilot()
        pid = pilot["pilot_id"]
        self.make_plan()  # 方案升级到 v2，试点仍钉住 v1
        detail = self.svc.get_pilot(pid)
        self.assertEqual(detail["plan_version"], 1)
        self.svc.sync_plan(pid, operator="操作员")
        detail = self.svc.get_pilot(pid)
        self.assertEqual(detail["plan_version"], 2)
        event_types = [e["type"] for e in detail["recent_events"]]
        self.assertIn("pilot.plan_synced", event_types)


def pilot_id_for_test(artifact_id, revision, school_id):
    from integration_pilot.models import pilot_id_for

    return pilot_id_for(artifact_id, revision, school_id)


if __name__ == "__main__":
    unittest.main()
