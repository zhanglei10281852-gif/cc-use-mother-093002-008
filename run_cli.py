"""跨境教育技术集成试点管理服务：端到端冒烟演示。

覆盖：制品登记 → 适配方案 → 试点发起（重复请求复用）→ 证据与晋级 →
指标豁免 → 失败回退 → 严重问题冻结与回撤 → 进程重启后继续 → 查询端点。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from integration_pilot import (
    CapabilityDeclaration,
    DependencyContract,
    JsonStore,
    PilotService,
    RuleViolation,
)

STORE_PATH = Path(__file__).parent / "var" / "pilot-store.json"


def show(title, payload):
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main():
    # 冒烟演示从干净状态开始；真实部署中 store 持久保存、重启后复用。
    if STORE_PATH.exists():
        STORE_PATH.unlink()
    service = PilotService(JsonStore(STORE_PATH))

    # 1. 登记制品：不可变摘要 + 能力声明 + 依赖契约
    artifact = service.register_artifact(
        artifact_id="ART-SMART-ALG",
        digest="sha256:9f2c",
        display_name="智能代数练习组件",
        capabilities=CapabilityDeclaration(
            data_formats=("xAPI-1.0", "CMS-COURSE-v2"),
            language_packs=("zh-CN", "vi-VN", "th-TH"),
            runtime_capabilities=("offline_mode", "low_bandwidth"),
        ),
        dependencies=(DependencyContract("lrs-gateway", ">=2.1 <3.0"),),
        actor="registry-bot",
    )
    show("1. 制品登记", artifact.to_dict())

    # 2. 两所东盟合作院校的本地化适配方案
    plan_vn = service.create_adaptation_plan(
        "ART-SMART-ALG", "SCH-HANOI-01",
        {"language_pack": "vi-VN", "data_format": "xAPI-1.0", "runtime_features": ["low_bandwidth"]},
        actor="ops-li",
    )
    plan_th = service.create_adaptation_plan(
        "ART-SMART-ALG", "SCH-BANGKOK-02",
        {"language_pack": "th-TH", "data_format": "CMS-COURSE-v2", "runtime_features": ["offline_mode"]},
        actor="ops-li",
    )
    show("2. 学校间适配差异", service.adaptation_diff("ART-SMART-ALG", "SCH-HANOI-01", "SCH-BANGKOK-02"))

    # 3. 发起试点；重复请求复用原记录
    pilot_vn = service.request_pilot("ART-SMART-ALG", "SCH-HANOI-01", actor="ops-li")
    again = service.request_pilot("ART-SMART-ALG", "SCH-HANOI-01", actor="ops-wang")
    assert again.pilot_id == pilot_vn.pilot_id, "重复请求必须复用原记录"
    pilot_th = service.request_pilot("ART-SMART-ALG", "SCH-BANGKOK-02", actor="ops-li")
    print(f"\n=== 3. 试点发起 ===\n河内={pilot_vn.pilot_id}（重复请求复用） 曼谷={pilot_th.pilot_id}")

    # 4. 兼容检查阶段：关键指标缺失不得晋级，限时豁免由对应角色批准
    service.submit_evidence(
        pilot_vn.pilot_id,
        {"data_format_match": 1.0, "language_pack_coverage": 0.98},
        actor="qa-chen",
    )
    try:
        service.advance(pilot_vn.pilot_id, actor="ops-li")
    except RuleViolation as exc:
        print(f"\n=== 4. 缺失指标被阻断 ===\n{exc}")
    service.grant_exemption(
        pilot_vn.pilot_id, "runtime_capability",
        approver="eng-zhao", approver_role="INTEGRATION_ENGINEER",
        reason="目标教室暂以兼容模式运行，48 小时内补测", ttl_hours=48,
    )
    service.advance(pilot_vn.pilot_id, actor="ops-li", reason="兼容检查通过（运行能力指标限时豁免）")

    # 5. 小规模验证失败 → 回退前一阶段 → 重新取证晋级
    service.submit_evidence(pilot_vn.pilot_id, {"success_rate": 0.62, "error_count": 41}, actor="qa-chen")
    service.rollback(pilot_vn.pilot_id, actor="ops-li", reason="小规模验证成功率不达标，回退复测")
    service.submit_evidence(
        pilot_vn.pilot_id,
        {"data_format_match": 1.0, "language_pack_coverage": 0.99, "runtime_capability": 1.0},
        actor="qa-chen",
    )
    service.advance(pilot_vn.pilot_id, actor="ops-li", reason="复测通过")
    service.submit_evidence(pilot_vn.pilot_id, {"success_rate": 0.97, "error_count": 2}, actor="qa-chen")
    service.advance(pilot_vn.pilot_id, actor="ops-li", reason="小规模验证通过")

    # 6. 曼谷试点走完观察期与推广评审，予以放行
    for metrics in (
        {"data_format_match": 1.0, "language_pack_coverage": 1.0, "runtime_capability": 1.0},
        {"success_rate": 0.96, "error_count": 3},
        {"stability_score": 0.94, "incident_count": 0},
        {"review_score": 0.91},
    ):
        service.submit_evidence(pilot_th.pilot_id, metrics, actor="qa-chen")
        service.advance(pilot_th.pilot_id, actor="ops-li")
    show("6. 曼谷试点放行决定", service.release_decision(pilot_th.pilot_id))

    # 7. 严重问题：冻结相关组合、列出采用方、启动回撤
    recall = service.report_severe_issue("ART-SMART-ALG", "组件在弱网下泄漏学生答题缓存", reporter="sec-wu")
    show("7. 回撤范围", service.recall_scope("ART-SMART-ALG"))

    # 8. 模拟操作员重启进程：从同一 store 恢复，继续原试点查询
    restarted = PilotService(JsonStore(STORE_PATH))
    show("8. 重启后放行/阻断视图（河内）", restarted.release_decision(pilot_vn.pilot_id))
    print(f"\n回撤指令 {recall.recall_id} 已在重启后保留，采用方 {len(recall.adopters)} 所学校。")


if __name__ == "__main__":
    main()
