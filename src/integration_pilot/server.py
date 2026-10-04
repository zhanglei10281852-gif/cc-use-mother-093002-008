"""基于标准库的 HTTP 端点：操作类 POST + 查询类 GET。

启动示例：
    python -m integration_pilot.server --store data/pilot.json --port 8080
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, NotFoundError
from .service import PilotService

# (方法, 路径模式, 服务方法名)；路径参数与查询/请求体参数合并后透传给服务层。
ROUTES = [
    ("GET", r"/health", "health"),
    ("POST", r"/artifacts", "register_artifact"),
    ("GET", r"/artifacts/(?P<artifact_id>[^/]+)/revisions/(?P<revision>\d+)", "get_artifact"),
    ("POST", r"/adaptations", "upsert_adaptation"),
    ("GET", r"/artifacts/(?P<artifact_id>[^/]+)/adaptation-diff", "adaptation_diff"),
    ("GET", r"/artifacts/(?P<artifact_id>[^/]+)/release-board", "release_board"),
    ("GET", r"/artifacts/(?P<artifact_id>[^/]+)/recall-scope", "recall_scope"),
    ("POST", r"/pilots", "open_pilot"),
    ("GET", r"/pilots/(?P<pilot_id>[^/]+)", "get_pilot"),
    ("POST", r"/pilots/(?P<pilot_id>[^/]+)/evidence", "submit_evidence"),
    ("POST", r"/pilots/(?P<pilot_id>[^/]+)/advance", "advance"),
    ("POST", r"/pilots/(?P<pilot_id>[^/]+)/rollback", "rollback"),
    ("POST", r"/pilots/(?P<pilot_id>[^/]+)/sync-plan", "sync_plan"),
    ("POST", r"/exemptions", "grant_exemption"),
    ("POST", r"/recalls", "declare_severe_issue"),
    ("POST", r"/recalls/(?P<recall_id>[^/]+)/withdrawals", "confirm_withdrawal"),
]

_COMPILED = [(method, re.compile(f"^{pattern}$"), action) for method, pattern, action in ROUTES]


def _error_status(exc: Exception) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, DomainError):
        return 409
    if isinstance(exc, ValueError):
        return 400
    return 500


class PilotRequestHandler(BaseHTTPRequestHandler):
    server: "PilotHTTPServer"

    def log_message(self, format: str, *args: Any) -> None:  # 静默访问日志
        return

    # ---------------- 基础工具 ----------------

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    # ---------------- 路由 ----------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        for route_method, pattern, action in _COMPILED:
            if route_method != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            try:
                params: dict[str, Any] = {}
                params.update(match.groupdict())
                if "revision" in params:
                    params["revision"] = int(params["revision"])
                query = parse_qs(parsed.query)
                if "revision" in query:
                    params["revision"] = int(query["revision"][0])
                if method == "POST":
                    params.update(self._read_body())
                result = self._invoke(action, **params)
                self._send(result)
            except Exception as exc:  # noqa: BLE001 - 统一错误映射
                self._send_error(exc)
            return
        self._send({"error": "NOT_FOUND", "message": f"路由不存在: {method} {path}"}, status=404)

    def _invoke(self, action: str, **params: Any) -> dict[str, Any]:
        service = self.server.service
        if action == "health":
            return {"ok": True}
        handler = getattr(service, action)
        return handler(**params)

    def _send_error(self, exc: Exception) -> None:
        status = _error_status(exc)
        if isinstance(exc, DomainError):
            payload = exc.to_dict()
        elif isinstance(exc, ValueError):
            payload = {"error": "BAD_REQUEST", "message": str(exc)}
        else:
            payload = {"error": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"}
        self._send(payload, status=status)


class PilotHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service: PilotService, address: tuple[str, int]) -> None:
        super().__init__(address, PilotRequestHandler)
        self.service = service


def make_server(service: PilotService, host: str = "127.0.0.1", port: int = 8080) -> PilotHTTPServer:
    return PilotHTTPServer(service, (host, port))


def main() -> None:
    parser = argparse.ArgumentParser(description="跨境教育技术集成试点管理服务")
    parser.add_argument("--store", default="data/pilot.json", help="状态存储文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    service = PilotService(args.store)
    server = make_server(service, args.host, args.port)
    print(f"试点管理服务已启动: http://{args.host}:{args.port} (存储: {args.store})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
