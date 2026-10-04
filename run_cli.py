"""端到端冒烟：登记制品 → 两校适配 → 试点推进 → 豁免 → 回撤 → 重启续跑 → 查询。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from integration_pilot import PilotService, Stage  # noqa: E402


def show(title, payload):
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main():
    store_path = Path(tempfile.mkdtemp(prefix="pilot-demo-")) / "pilot.json"
    svc = PilotService(store_path)

    artifact = svc.register_artifact(
        artifact_id="smart-lesson",
        revision=3,
        display_name="智能备课组件",
        artifact_digest="sha256:9f2c…demo",
        capabilities={
            "course_data_formats": ["xAPI", "SCORM1.2"],
            "language_packs": ["zh-CN", "vi-VN", "th-TH"],
            "runtime": {"offline_mode": True, "min_mem_gb": 4, "min_cpu": 2},
        },
        dependencies=[{"name": "lms-core", "constraint": ">=4.2"}],
        registered_by="集成团队-登记员",
    )
    show("1. 制品登记（摘要+能力声明+依赖契约，不可变）", artifact)

    svc.upsert_adaptation(
        artifact_id="smart-lesson", revision=3, school_id="SCH-HANOI",
        course_data_format="xAPI", language_pack="vi-VN",
        runtime_profile={"offline_mode": True, "mem_gb": 8}, operator="适配员A",
    )
    svc.upsert_adaptation(
        artifact_id="smart-lesson", revision=3, school_id="SCH-BANGKOK",
        course_data_format="SCORM1.2", language_pack="th-TH",
        runtime_profile={"offline_mode": False, "mem_gb": 4}, operator="适配员B",
    )
    show("2. 学校间适配差异", svc.adaptation_diff("smart-lesson", 3))

    pilot_a = svc.open_pilot(artifact_id="smart-lesson", revision=3, school_id="SCH-HANOI", operator="操作员")
    again = svc.open_pilot(artifact_id="smart-lesson", revision=3, school_id="SCH-HANOI", operator="操作员")
    show("3. 重复试点请求复用原记录", {"pilot_id": pilot_a["pilot_id"], "reused": again["reused"]})

    pid = pilot_a["pilot_id"]
    svc.submit_evidence(pid, metrics={
        "course_data_format_ok": True, "language_pack_ok": True, "runtime_ok": True,
    }, submitted_by="兼容检查组")
    svc.advance(pid, operator="操作员")
    svc.submit_evidence(pid, metrics={
        "sessions_completed": 5, "error_rate": 0.02, "learner_satisfaction": 0.83,
    }, submitted_by="试点学校")
    svc.advance(pid, operator="操作员")
    show("4. 兼容检查/小规模验证晋级后", svc.get_pilot(pid))

    svc.submit_evidence(pid, metrics={
        "observation_days": 21, "severity1_incidents": 0, "stability_score": 0.95,
    }, submitted_by="观察组")
    svc.advance(pid, operator="操作员")
    # 推广评审缺预算确认 → 限时豁免（教学主管无权 → 指导委员会主席批准）
    svc.submit_evidence(pid, metrics={"review_board_signoff": True}, submitted_by="评审办")
    try:
        svc.grant_exemption(
            artifact_id="smart-lesson", revision=3, school_id="SCH-HANOI",
            metric="budget_confirmed", approver_role="academic_director",
            approver_name="教学主管", ttl_hours=72, reason="预算流程跨年",
        )
    except Exception as exc:  # noqa: BLE001
        show("5a. 越权审批被拒", {"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)})
    exemption = svc.grant_exemption(
        artifact_id="smart-lesson", revision=3, school_id="SCH-HANOI",
        metric="budget_confirmed", approver_role="steering_committee_chair",
        approver_name="指导委员会主席", ttl_hours=72, reason="预算流程跨年，限期补齐",
    )
    show("5b. 限时豁免签发", exemption)
    svc.advance(pid, operator="操作员")
    show("5c. 豁免覆盖后晋级推广", svc.release_board("smart-lesson", 3))

    # 另一所学校推进到观察期后，出现严重问题
    pilot_b = svc.open_pilot(artifact_id="smart-lesson", revision=3, school_id="SCH-BANGKOK", operator="操作员")
    svc.submit_evidence(pilot_b["pilot_id"], metrics={
        "course_data_format_ok": True, "language_pack_ok": True, "runtime_ok": True,
    }, submitted_by="兼容检查组")
    svc.advance(pilot_b["pilot_id"], operator="操作员")

    recall = svc.declare_severe_issue(
        artifact_id="smart-lesson", revision=3,
        reported_by="安全值班员", description="越界读取课程数据，疑似隐私泄露",
    )
    show("6. 严重问题 → 冻结组合 + 采用方清单 + 启动回撤", recall)

    # 操作员重启进程：从同一存储恢复，继续处理回撤
    svc = PilotService(store_path)
    show("7. 重启后回撤范围查询", svc.recall_scope("smart-lesson", 3))
    svc.confirm_withdrawal(recall["recall_id"], school_id="SCH-HANOI", operator="操作员")
    final = svc.confirm_withdrawal(recall["recall_id"], school_id="SCH-BANGKOK", operator="操作员")
    show("8. 全部采用方确认回撤后", final)
    show("9. 最终放行/阻断看板", svc.release_board("smart-lesson", 3))
    print(f"\n冒烟完成，阶段常量自检: {Stage.ORDER}")


if __name__ == "__main__":
    main()
