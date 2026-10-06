import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from integration_pilot import (
    CapabilityDeclaration,
    DependencyContract,
    JsonStore,
    NotFoundError,
    PilotService,
    RuleViolation,
)


class FakeClock:
    def __init__(self):
        self.moment = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.moment

    def advance(self, hours):
        self.moment += timedelta(hours=hours)


CAPS = CapabilityDeclaration(
    data_formats=("xAPI-1.0", "CMS-COURSE-v2"),
    language_packs=("zh-CN", "vi-VN", "th-TH"),
    runtime_capabilities=("offline_mode", "low_bandwidth"),
)

COMPAT_METRICS = {
    "data_format_match": 1.0,
    "language_pack_coverage": 0.99,
    "runtime_capability": 1.0,
}


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = Path(self.tmp.name) / "store.json"
        self.clock = FakeClock()
        self.service = PilotService(JsonStore(self.store_path), clock=self.clock)
        self.service.register_artifact(
            "ART-1", "sha256:aaa", "智能代数组件", CAPS,
            dependencies=(DependencyContract("lrs-gateway", ">=2.1"),),
            actor="tester",
        )
        self.service.create_adaptation_plan(
            "ART-1", "SCH-A",
            {"language_pack": "vi-VN", "data_format": "xAPI-1.0", "runtime_features": ["low_bandwidth"]},
            actor="tester",
        )

    def _pilot(self, school="SCH-A"):
        return self.service.request_pilot("ART-1", school, actor="tester")

    def _advance_stage(self, pilot_id, metrics):
        self.service.submit_evidence(pilot_id, metrics, actor="tester")
        return self.service.advance(pilot_id, actor="tester")


class ArtifactRegistrationTests(ServiceTestCase):
    def test_conflicting_digest_is_rejected(self):
        with self.assertRaises(RuleViolation):
            self.service.register_artifact("ART-1", "sha256:bbb", "改名组件", CAPS, actor="tester")

    def test_same_digest_reregistration_is_idempotent(self):
        again = self.service.register_artifact("ART-1", "sha256:aaa", "智能代数组件", CAPS, actor="tester")
        self.assertEqual(again.digest, "sha256:aaa")
        self.assertEqual(len(self.service.artifacts), 1)

    def test_registration_keeps_dependencies(self):
        artifact = self.service.artifacts["ART-1"]
        self.assertEqual(artifact.dependencies[0].name, "lrs-gateway")


class AdaptationPlanTests(ServiceTestCase):
    def test_plan_rejects_undeclared_language_pack(self):
        with self.assertRaises(RuleViolation):
            self.service.create_adaptation_plan(
                "ART-1", "SCH-B",
                {"language_pack": "km-KH", "data_format": "xAPI-1.0"},
                actor="tester",
            )

    def test_plan_rejects_undeclared_data_format(self):
        with self.assertRaises(RuleViolation):
            self.service.create_adaptation_plan(
                "ART-1", "SCH-B",
                {"language_pack": "vi-VN", "data_format": "SCORM-2004"},
                actor="tester",
            )

    def test_plan_rejects_undeclared_runtime_feature(self):
        with self.assertRaises(RuleViolation):
            self.service.create_adaptation_plan(
                "ART-1", "SCH-B",
                {"language_pack": "vi-VN", "data_format": "xAPI-1.0", "runtime_features": ["gpu_render"]},
                actor="tester",
            )

    def test_new_plan_supersedes_previous_revision(self):
        first = self.service.active_plan("ART-1", "SCH-A")
        second = self.service.create_adaptation_plan(
            "ART-1", "SCH-A",
            {"language_pack": "zh-CN", "data_format": "CMS-COURSE-v2"},
            actor="tester",
        )
        self.assertEqual(second.revision, first.revision + 1)
        self.assertEqual(self.service.active_plan("ART-1", "SCH-A").plan_id, second.plan_id)
        self.assertEqual(self.service.plans[first.plan_id].status, "SUPERSEDED")


class PilotRequestTests(ServiceTestCase):
    def test_duplicate_request_reuses_record(self):
        first = self._pilot()
        second = self.service.request_pilot("ART-1", "SCH-A", actor="someone-else")
        self.assertEqual(first.pilot_id, second.pilot_id)
        self.assertEqual(len(self.service.pilots), 1)

    def test_request_requires_active_plan(self):
        with self.assertRaises(RuleViolation):
            self.service.request_pilot("ART-1", "SCH-NO-PLAN", actor="tester")

    def test_request_requires_registered_artifact(self):
        with self.assertRaises(NotFoundError):
            self.service.request_pilot("ART-UNKNOWN", "SCH-A", actor="tester")

    def test_pilot_records_plan_used(self):
        pilot = self._pilot()
        plan = self.service.active_plan("ART-1", "SCH-A")
        self.assertEqual(pilot.plan_id, plan.plan_id)


class StageGateTests(ServiceTestCase):
    def test_advance_requires_evidence(self):
        pilot = self._pilot()
        with self.assertRaises(RuleViolation):
            self.service.advance(pilot.pilot_id, actor="tester")

    def test_advance_blocked_on_missing_key_metric(self):
        pilot = self._pilot()
        self.service.submit_evidence(pilot.pilot_id, {"data_format_match": 1.0}, actor="tester")
        with self.assertRaises(RuleViolation) as ctx:
            self.service.advance(pilot.pilot_id, actor="tester")
        self.assertIn("关键指标缺失", str(ctx.exception))
        self.assertEqual(pilot.stage, "COMPATIBILITY_CHECK")

    def test_advance_blocked_on_expired_evidence(self):
        pilot = self._pilot()
        self.service.submit_evidence(pilot.pilot_id, COMPAT_METRICS, actor="tester", ttl_hours=24)
        self.clock.advance(25)
        with self.assertRaises(RuleViolation) as ctx:
            self.service.advance(pilot.pilot_id, actor="tester")
        self.assertIn("证据已过期", str(ctx.exception))

    def test_full_pipeline_to_promoted(self):
        pilot = self._pilot()
        for metrics in (
            COMPAT_METRICS,
            {"success_rate": 0.97, "error_count": 1},
            {"stability_score": 0.95, "incident_count": 0},
            {"review_score": 0.9},
        ):
            pilot = self._advance_stage(pilot.pilot_id, metrics)
        self.assertEqual(pilot.stage, "PROMOTED")
        self.assertEqual(pilot.status, "PROMOTED")

    def test_rollback_returns_to_previous_stage(self):
        pilot = self._pilot()
        self._advance_stage(pilot.pilot_id, COMPAT_METRICS)
        updated = self.service.rollback(pilot.pilot_id, actor="tester", reason="验证失败")
        self.assertEqual(updated.stage, "COMPATIBILITY_CHECK")
        kinds = [t.kind for t in updated.transitions]
        self.assertEqual(kinds, ["ADVANCE", "ROLLBACK"])

    def test_rollback_at_first_stage_rejected(self):
        pilot = self._pilot()
        with self.assertRaises(RuleViolation):
            self.service.rollback(pilot.pilot_id, actor="tester", reason="无路可退")


class ExemptionTests(ServiceTestCase):
    def test_exemption_role_is_enforced(self):
        pilot = self._pilot()
        with self.assertRaises(RuleViolation):
            self.service.grant_exemption(
                pilot.pilot_id, "runtime_capability",
                approver="principal", approver_role="ACADEMIC_DIRECTOR",
                reason="越权角色应被拒绝",
            )

    def test_exemption_covers_missing_metric(self):
        pilot = self._pilot()
        self.service.submit_evidence(
            pilot.pilot_id,
            {"data_format_match": 1.0, "language_pack_coverage": 0.98},
            actor="tester",
        )
        self.service.grant_exemption(
            pilot.pilot_id, "runtime_capability",
            approver="eng", approver_role="INTEGRATION_ENGINEER",
            reason="兼容模式临时运行", ttl_hours=48,
        )
        updated = self.service.advance(pilot.pilot_id, actor="tester")
        self.assertEqual(updated.stage, "SMALL_SCALE_VALIDATION")

    def test_expired_exemption_does_not_cover(self):
        pilot = self._pilot()
        self.service.submit_evidence(
            pilot.pilot_id,
            {"data_format_match": 1.0, "language_pack_coverage": 0.98},
            actor="tester", ttl_hours=720,
        )
        self.service.grant_exemption(
            pilot.pilot_id, "runtime_capability",
            approver="eng", approver_role="INTEGRATION_ENGINEER",
            reason="短时限豁免", ttl_hours=24,
        )
        self.clock.advance(25)
        with self.assertRaises(RuleViolation):
            self.service.advance(pilot.pilot_id, actor="tester")

    def test_unknown_requirement_rejected(self):
        pilot = self._pilot()
        with self.assertRaises(RuleViolation):
            self.service.grant_exemption(
                pilot.pilot_id, "nonexistent_metric",
                approver="eng", approver_role="INTEGRATION_ENGINEER",
                reason="未知指标",
            )


class SevereIssueTests(ServiceTestCase):
    def _two_school_pilots(self):
        self.service.create_adaptation_plan(
            "ART-1", "SCH-B",
            {"language_pack": "th-TH", "data_format": "CMS-COURSE-v2"},
            actor="tester",
        )
        return self._pilot("SCH-A"), self._pilot("SCH-B")

    def test_severe_issue_freezes_lists_adopters_and_recalls(self):
        pilot_a, pilot_b = self._two_school_pilots()
        self._advance_stage(pilot_a.pilot_id, COMPAT_METRICS)
        recall = self.service.report_severe_issue("ART-1", "弱网泄漏答题缓存", reporter="sec")

        self.assertEqual(len(recall.adopters), 2)
        self.assertEqual({a["school_id"] for a in recall.adopters}, {"SCH-A", "SCH-B"})
        for pid in (pilot_a.pilot_id, pilot_b.pilot_id):
            self.assertEqual(self.service.pilots[pid].status, "FROZEN")
            with self.assertRaises(RuleViolation):
                self.service.advance(pid, actor="tester")

        scope = self.service.recall_scope("ART-1")
        self.assertEqual(len(scope["recalls"]), 1)
        self.assertEqual(len(scope["frozen_combinations"]), 2)
        self.assertEqual(len(scope["adopters"]), 2)

    def test_severe_issue_on_unknown_artifact_rejected(self):
        with self.assertRaises(NotFoundError):
            self.service.report_severe_issue("ART-UNKNOWN", "问题", reporter="sec")


class PersistenceTests(ServiceTestCase):
    def test_restart_continues_original_pilot(self):
        pilot = self._pilot()
        self._advance_stage(pilot.pilot_id, COMPAT_METRICS)

        restarted = PilotService(JsonStore(self.store_path), clock=self.clock)
        restored = restarted.pilots[pilot.pilot_id]
        self.assertEqual(restored.stage, "SMALL_SCALE_VALIDATION")
        self.assertEqual(len(restored.transitions), 1)

        continued = restarted.submit_evidence(
            pilot.pilot_id, {"success_rate": 0.98, "error_count": 0}, actor="tester"
        )
        restarted.advance(pilot.pilot_id, actor="tester")
        self.assertEqual(restarted.pilots[pilot.pilot_id].stage, "OBSERVATION")
        self.assertEqual(continued.stage, "SMALL_SCALE_VALIDATION")


class QueryTests(ServiceTestCase):
    def test_release_decision_shows_blockers_and_evidence(self):
        pilot = self._pilot()
        self.service.submit_evidence(pilot.pilot_id, {"data_format_match": 1.0}, actor="tester")
        decision = self.service.release_decision(pilot.pilot_id)
        self.assertFalse(decision["released"])
        self.assertTrue(decision["blocked"])
        self.assertTrue(any("关键指标缺失" in b for b in decision["blockers"]))
        self.assertEqual(len(decision["evidence"]), 1)

    def test_release_decision_released_after_promotion(self):
        pilot = self._pilot()
        for metrics in (
            COMPAT_METRICS,
            {"success_rate": 0.97, "error_count": 1},
            {"stability_score": 0.95, "incident_count": 0},
            {"review_score": 0.9},
        ):
            pilot = self._advance_stage(pilot.pilot_id, metrics)
        decision = self.service.release_decision(pilot.pilot_id)
        self.assertTrue(decision["released"])
        self.assertFalse(decision["blocked"])

    def test_adaptation_diff_between_schools(self):
        self.service.create_adaptation_plan(
            "ART-1", "SCH-B",
            {"language_pack": "th-TH", "data_format": "CMS-COURSE-v2", "runtime_features": ["offline_mode"]},
            actor="tester",
        )
        diff = self.service.adaptation_diff("ART-1", "SCH-A", "SCH-B")
        self.assertEqual(
            set(diff["differences"].keys()),
            {"language_pack", "data_format", "runtime_features"},
        )
        self.assertEqual(diff["differences"]["language_pack"]["SCH-A"], "vi-VN")
        self.assertEqual(diff["differences"]["language_pack"]["SCH-B"], "th-TH")

    def test_adaptation_diff_requires_both_plans(self):
        with self.assertRaises(NotFoundError):
            self.service.adaptation_diff("ART-1", "SCH-A", "SCH-MISSING")


if __name__ == "__main__":
    unittest.main()
