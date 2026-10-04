import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error, request

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


def _client(method, url, body=None, role="viewer", actor="demo"):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "X-Role": role, "X-Actor": actor})
    try:
        with request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Repository(str(Path(cls.tmp.name) / "http.db"))
        cls.service = Service(cls.repo)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(cls.service, str(Path(__file__).resolve().parents[1] / "static")))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.repo.close()
        cls.tmp.cleanup()

    def url(self, suffix):
        return f"http://127.0.0.1:{self.port}{suffix}"

    def test_single_item_route_not_shadowed_by_batch_routes(self):
        status, item = _client(
            "POST", self.url("/api/items"),
            {"title": "http item", "description": "route order",
             "severity": "urgent", "quantity": 5, "threshold": 10},
            role="duty_officer", actor="duty")
        self.assertEqual(status, 201)
        iid = item["id"]
        # 单条查询必须能正常鉴权返回（早期会被 /batch 前缀错误遮蔽）
        status, fetched = _client(
            "GET", self.url(f"/api/items/{iid}"),
            role="chief_engineer", actor="chief")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["id"], iid)
        # 未授权时批次视图404
        status, payload = _client(
            "GET", self.url(f"/api/items/{iid}/batch"),
            role="viewer", actor="v")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "NotFoundError")

    def test_batch_lifecycle_over_http(self):
        status, item = _client(
            "POST", self.url("/api/items"),
            {"title": "batch http", "description": "gates",
             "severity": "emergency", "quantity": 9, "threshold": 5},
            role="duty_officer", actor="duty")
        iid = item["id"]
        _client("POST", self.url(f"/api/items/{iid}/transition"),
                {"target": "checked", "expected_version": 1},
                role="duty_officer", actor="duty")
        status, cur = _client("GET", self.url(f"/api/items/{iid}"),
                              role="chief_engineer", actor="chief")
        _client("POST", self.url(f"/api/items/{iid}/transition"),
                {"target": "authorized", "expected_version": cur["version"]},
                role="chief_engineer", actor="chief")
        status, cur = _client("GET", self.url(f"/api/items/{iid}"),
                              role="chief_engineer", actor="chief")
        status, res = _client(
            "POST", self.url(f"/api/items/{iid}/batch/authorize"),
            {"expected_version": cur["version"], "basis": "b",
             "gates": [{"gate_code": "G1", "target_opening": 10.0}]},
            role="chief_engineer", actor="chief")
        self.assertEqual(status, 201)
        self.assertEqual(res["status"], "executing")
        # 非调度员派工被拒
        status, payload = _client(
            "POST", self.url(f"/api/items/{iid}/batch/dispatch"), {},
            role="viewer", actor="v")
        self.assertEqual(status, 403)
        # 派工、到位、完成、关闭
        _client("POST", self.url(f"/api/items/{iid}/batch/dispatch"), {},
                role="dispatcher", actor="disp")
        status, res = _client(
            "POST", self.url(f"/api/items/{iid}/batch/receipts"),
            {"gate_code": "G1", "outcome": "arrived", "actual_opening": 10.0,
             "client_token": "t-1"},
            role="duty_officer", actor="d1")
        self.assertEqual(status, 201)
        # 同token重试不重复追加
        status, _ = _client(
            "POST", self.url(f"/api/items/{iid}/batch/receipts"),
            {"gate_code": "G1", "outcome": "arrived", "actual_opening": 10.0,
             "client_token": "t-1"},
            role="duty_officer", actor="d1")
        self.assertEqual(status, 409)
        status, settled = _client(
            "POST", self.url(f"/api/items/{iid}/batch/settle"),
            role="dispatcher", actor="disp")
        self.assertEqual(status, 200)
        # 调度员不能关闭
        status, _ = _client(
            "POST", self.url(f"/api/items/{iid}/batch/close"),
            {"expected_version": settled["version"]},
            role="dispatcher", actor="disp")
        self.assertEqual(status, 403)
        status, closed = _client(
            "POST", self.url(f"/api/items/{iid}/batch/close"),
            {"expected_version": settled["version"]},
            role="chief_engineer", actor="chief")
        self.assertEqual(status, 200)
        self.assertEqual(closed["status"], "closed")


if __name__ == "__main__":
    unittest.main()
