import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from integration_pilot import PilotService  # noqa: E402
from integration_pilot.server import make_server  # noqa: E402


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        service = PilotService(Path(cls.tmp.name) / "pilot.json")
        cls.httpd = make_server(service, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_full_flow_over_http(self):
        status, artifact = self.request("POST", "/artifacts", {
            "artifact_id": "smart-lesson", "revision": 1,
            "display_name": "智能备课组件", "artifact_digest": "sha256:http",
            "capabilities": {
                "course_data_formats": ["xAPI"],
                "language_packs": ["zh-CN", "vi-VN"],
                "runtime": {"offline_mode": True, "min_mem_gb": 4},
            },
            "dependencies": [], "registered_by": "测试",
        })
        self.assertEqual(status, 200, artifact)

        status, _ = self.request("POST", "/adaptations", {
            "artifact_id": "smart-lesson", "revision": 1, "school_id": "SCH-HTTP",
            "course_data_format": "xAPI", "language_pack": "zh-CN",
            "runtime_profile": {"mem_gb": 8}, "operator": "测试",
        })
        self.assertEqual(status, 200)

        status, pilot = self.request("POST", "/pilots", {
            "artifact_id": "smart-lesson", "revision": 1,
            "school_id": "SCH-HTTP", "operator": "测试",
        })
        self.assertEqual(status, 200)
        pid = pilot["pilot_id"]

        # 重复开设 → 复用原记录
        _, again = self.request("POST", "/pilots", {
            "artifact_id": "smart-lesson", "revision": 1,
            "school_id": "SCH-HTTP", "operator": "测试",
        })
        self.assertTrue(again["reused"])

        # 无证据晋级 → 409 门禁阻断
        status, blocked = self.request("POST", f"/pilots/{pid}/advance", {"operator": "测试"})
        self.assertEqual(status, 409)
        self.assertEqual(blocked["error"], "STAGE_GATE_BLOCKED")

        self.request("POST", f"/pilots/{pid}/evidence", {
            "metrics": {
                "course_data_format_ok": True, "language_pack_ok": True, "runtime_ok": True,
            },
            "submitted_by": "测试",
        })
        status, advanced = self.request("POST", f"/pilots/{pid}/advance", {"operator": "测试"})
        self.assertEqual(status, 200)
        self.assertEqual(advanced["stage"], "SMALL_SCALE_VALIDATION")

        # 查询端点：放行/阻断看板
        status, board = self.request("GET", "/artifacts/smart-lesson/release-board?revision=1")
        self.assertEqual(status, 200)
        self.assertEqual(board["schools"][0]["decision"], "阻断")

        # 查询端点：学校间适配差异
        status, diff = self.request("GET", "/artifacts/smart-lesson/adaptation-diff?revision=1")
        self.assertEqual(status, 200)
        self.assertIn("SCH-HTTP", diff["schools"])

        # 严重问题 → 回撤范围查询
        status, recall = self.request("POST", "/recalls", {
            "artifact_id": "smart-lesson", "revision": 1,
            "reported_by": "值班员", "description": "严重缺陷",
        })
        self.assertEqual(status, 200)
        self.assertEqual(recall["adopters"], ["SCH-HTTP"])
        status, scope = self.request("GET", "/artifacts/smart-lesson/recall-scope?revision=1")
        self.assertEqual(scope["recalls"][0]["pending"], ["SCH-HTTP"])

        status, done = self.request(
            "POST", f"/recalls/{recall['recall_id']}/withdrawals",
            {"school_id": "SCH-HTTP", "operator": "测试"},
        )
        self.assertEqual(done["status"], "COMPLETED")

    def test_unknown_route_and_missing_pilot(self):
        status, _ = self.request("GET", "/no-such-route")
        self.assertEqual(status, 404)
        status, body = self.request("GET", "/pilots/P-missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "NOT_FOUND")

    def test_invalid_json_body_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/artifacts", body="{not json", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()


if __name__ == "__main__":
    unittest.main()
