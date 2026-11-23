# -*- coding: utf-8 -*-
"""HTTP 冒烟测试：起一个临时端口的真实服务（打印机指向闭合端口，秒失败）。"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request

import server
from tests.test_functions import MultipartTests


class HttpSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # SNMP 打到 161 会超时 1.5s×4，直接把 snmp_get 换成立即失败
        server.snmp_get = lambda oids, timeout=1.5: (_ for _ in ()).throw(
            RuntimeError("snmp timeout"))
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        # 用例共享同一进程状态：清掉上一个用例留下的作业
        with server._lock:
            server._jobs.clear()

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path), timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def _post(self, path, data=b"", ctype="application/json"):
        req = urllib.request.Request(self._url(path), data=data, method="POST",
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_health(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_index_served(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body[:100].lower())

    def test_state_offline_fast(self):
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertFalse(state["printer"]["reachable"])
        self.assertFalse(state["printer"]["snmp_ok"])
        self.assertEqual(state["jobs"], [])

    def test_unknown_get_404(self):
        status, body = self._get("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("未找到该接口", body.decode())

    def test_preview_without_file_400(self):
        status, body = self._post("/api/preview")
        self.assertEqual(status, 400)
        self.assertIn("没有收到文件", body.decode())

    def test_print_without_file_400(self):
        status, body = self._post("/api/print")
        self.assertEqual(status, 400)
        self.assertIn("没有收到文件", body.decode())

    def test_unknown_post_404(self):
        status, body = self._post("/api/nope")
        self.assertEqual(status, 404)

    def test_preview_bad_pages_400(self):
        raw, ctype = MultipartTests._body(
            {"paper": "A4", "pages": "abc"}, {"a.pdf": ("a.pdf", b"%PDF-1.4")})
        status, body = self._post("/api/preview", raw, ctype)
        self.assertEqual(status, 400)
        self.assertIn("页码范围", body.decode())

    def test_print_bad_pages_400(self):
        raw, ctype = MultipartTests._body(
            {"pages": "4-2", "copies": "1"}, {"a.pdf": ("a.pdf", b"%PDF-1.4")})
        status, body = self._post("/api/print", raw, ctype)
        self.assertEqual(status, 400)
        self.assertIn("页码范围不合法", body.decode())

    @staticmethod
    def _fake_preview(token, meta):
        pdir = os.path.join(server.PREVIEW_DIR, token)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "job.pdf"), "wb") as f:
            f.write(b"%PDF-fake")
        with open(os.path.join(pdir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

    def test_print_token_meta_is_geometry_authority(self):
        token = "abcdef1234567890"
        self._fake_preview(token, {"name": "预览文件.pdf", "size": 9,
                                   "paper": "A3", "fit": False, "pages": [2, 3]})
        # 表单里传的是另一套几何——必须被 meta 覆盖（WYSIWYG）
        raw, ctype = MultipartTests._body(
            {"token": token, "paper": "A4", "fit": "1", "pages": "1-9",
             "copies": "2", "duplex": "off"}, {})
        status, body = self._post("/api/print", raw, ctype)
        self.assertEqual(status, 200, body)
        job = json.loads(body)["job"]
        self.assertEqual(job["paper"], "A3")
        self.assertFalse(job["fit"])
        self.assertEqual(job["pages"], [2, 3])
        self.assertEqual(job["name"], "预览文件.pdf")
        self.assertEqual(job["copies"], 2)

    def test_print_token_legacy_meta_falls_back_to_fields(self):
        # 旧预览目录的 meta.json 没有 paper/fit/pages → 回落到表单字段
        token = "fedcba0987654321"
        self._fake_preview(token, {"name": "旧预览.pdf", "size": 5})
        raw, ctype = MultipartTests._body(
            {"token": token, "paper": "A5", "fit": "1", "pages": "1-2",
             "copies": "1", "duplex": "long"}, {})
        status, body = self._post("/api/print", raw, ctype)
        self.assertEqual(status, 200, body)
        job = json.loads(body)["job"]
        self.assertEqual(job["paper"], "A5")
        self.assertTrue(job["fit"])
        self.assertEqual(job["pages"], [1, 2])
        self.assertEqual(job["duplex"], "long")


if __name__ == "__main__":
    unittest.main()
