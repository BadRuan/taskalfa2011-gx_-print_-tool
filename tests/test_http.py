# -*- coding: utf-8 -*-
"""HTTP 冒烟测试：起一个临时端口的真实服务（打印机指向闭合端口，秒失败）。"""
import json
import os
import threading
import time
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
        # 用例共享同一进程状态：清掉上一个用例留下的作业与缓存
        with server._lock:
            server._jobs.clear()
        deadline = time.time() + 3
        while server._refreshing and time.time() < deadline:
            time.sleep(0.01)                     # 等上个用例踢起的后台探测收尾
        server._status_cache.update(ts=0, value=None)

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

    def test_status_fresh_cache_never_probes(self):
        sentinel = {"reachable": True, "snmp_ok": True, "model": "缓存",
                    "page_count": 1, "panel": "", "state": "", "port_open": True}
        server._status_cache.update(ts=time.time(), value=sentinel)
        orig = server.snmp_get
        server.snmp_get = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("缓存新鲜时不应发起真实探测"))
        try:
            self.assertIs(server.printer_status(), sentinel)
        finally:
            server.snmp_get = orig

    def test_status_stale_returns_old_value_without_blocking(self):
        # 离线探测约 9s：过期时必须立刻回旧值，后台补测（stale-while-revalidate）
        old = {"reachable": False, "snmp_ok": False, "model": "旧值",
               "page_count": None, "panel": "", "state": "", "port_open": False}
        server._status_cache.update(ts=time.time() - 999, value=old)
        calls = []

        def slow_fail(*a, **k):
            calls.append(1)
            time.sleep(0.8)
            raise RuntimeError("timeout")

        orig = server.snmp_get
        server.snmp_get = slow_fail
        try:
            t0 = time.monotonic()
            got = server.printer_status()
            elapsed = time.monotonic() - t0
            self.assertIs(got, old)              # 立刻回旧值
            self.assertLess(elapsed, 0.3)        # 绝不等真实探测
            deadline = time.time() + 3
            while not calls and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(calls, "应踢起后台探测线程")
            while server._refreshing and time.time() < deadline:
                time.sleep(0.02)
        finally:
            server.snmp_get = orig
            server._status_cache.update(ts=0, value=None)

    def test_state_endpoint_never_blocks_on_offline_printer(self):
        # /api/state 在打印机离线时也必须秒回（后台探测）
        server._status_cache.update(ts=0, value=None)
        t0 = time.monotonic()
        status, body = self._get("/api/state")
        elapsed = time.monotonic() - t0
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 1.0, "state 接口被真实探测阻塞了")
        state = json.loads(body)
        self.assertIn("printer", state)
        self.assertIn("reachable", state["printer"])
        while server._refreshing:               # 等后台线程收尾，别污染下一用例
            time.sleep(0.02)
        server._status_cache.update(ts=0, value=None)

    def test_post_concurrency_bounded_503(self):
        held = 0
        try:
            for _ in range(server.MAX_POST_CONCURRENCY):
                self.assertTrue(server._post_slots.acquire(blocking=False))
                held += 1
            status, body = self._post("/api/print")
            self.assertEqual(status, 503)
            self.assertIn("繁忙", body.decode())
        finally:
            for _ in range(held):
                server._post_slots.release()
        # 槽位归还后恢复正常
        status, body = self._post("/api/print")
        self.assertEqual(status, 400)
        self.assertIn("没有收到文件", body.decode())

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

    def test_print_bad_copies_400(self):
        # 旧实现 int("abc") 抛 ValueError → 500
        for bad in ("abc", "0", "150"):
            raw, ctype = MultipartTests._body(
                {"copies": bad}, {"a.pdf": ("a.pdf", b"%PDF-1.4")})
            status, body = self._post("/api/print", raw, ctype)
            self.assertEqual(status, 400, (bad, body))
            self.assertIn("份数", body.decode())

    def test_bad_content_length_400(self):
        # urllib 不会替我们发非法头，用裸 socket 手工构造
        import socket as sock
        req = (b"POST /api/print HTTP/1.0\r\n"
               b"Content-Type: application/json\r\n"
               b"Content-Length: abc\r\n\r\n")
        with sock.create_connection(("127.0.0.1", self.port), 5) as s:
            s.sendall(req)
            resp = s.recv(1024)
        self.assertIn(b" 400 ", resp.split(b"\r\n")[0] + b" ")
        self.assertIn("Content-Length".encode(), resp)

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
