# -*- coding: utf-8 -*-
"""纯函数特征测试（不依赖 gs/pdfinfo/libreoffice，宿主可跑）。"""
import contextlib
import io
import json
import os
import shutil
import struct
import tempfile
import time
import unittest

import server


def _block(tag, body_dwords):
    """构造一个 KPSL 块：12 字节头（第 4 字节是 tag，4..8 是 dword 长度）+ body。"""
    hdr = bytearray(12)
    hdr[3:4] = tag
    struct.pack_into("<I", hdr, 4, body_dwords)
    return bytes(hdr) + b"\x00" * (body_dwords * 4)


class BerTests(unittest.TestCase):
    def test_ber_len(self):
        self.assertEqual(server._ber_len(5), b"\x05")
        self.assertEqual(server._ber_len(0x7F), b"\x7f")
        self.assertEqual(server._ber_len(0x80), b"\x81\x80")
        self.assertEqual(server._ber_len(300), b"\x82\x01\x2c")

    def test_ber_int_roundtrip(self):
        for n in (0, 1, 127, 128, 300, 65535, -1):
            enc = server._ber_int(n)
            tag, pos, ln = server._ber_parse(enc, 0)
            self.assertEqual(tag, 0x02)
            val = int.from_bytes(enc[pos:pos + ln], "big", signed=True)
            self.assertEqual(val, n)

    def test_ber_oid_roundtrip(self):
        for oid in ("1.3.6.1.2.1.43.10.2.1.4.1.1",
                    "1.3.6.1.2.1.25.3.2.1.3.1",
                    "1.3.6.1.999.200.128.7",
                    "2.999.1"):
            enc = server._ber_oid(oid)
            self.assertEqual(enc[0], 0x06)
            tag, pos, ln = server._ber_parse(enc, 0)
            self.assertEqual(tag, 0x06)
            self.assertEqual(server._oid_to_str(enc[pos:pos + ln]), oid)


class KpslPatchTests(unittest.TestCase):
    def test_patch_s_field_sets_0x10000(self):
        data = b"LSPK" + _block(b"S", 3) + _block(b"C", 1)
        out = server.patch_s_field(data)
        # S 块 body 起点 = 4 + 12 = 16，第 3 个双字 = 24..28
        self.assertEqual(struct.unpack("<I", out[24:28])[0], 0x10000)
        # 后面的 C 块不能被动到
        self.assertEqual(out[4 + 12 + 12 + 2], 0)

    def test_patch_s_field_skips_other_blocks(self):
        data = b"LSPK" + _block(b"C", 1) + _block(b"S", 4)
        out = server.patch_s_field(data, val=0x20000)
        s_body = 4 + 12 + 4 + 12          # C 块 12+4 dword 之后才是 S 块
        self.assertEqual(struct.unpack("<I", out[s_body + 8:s_body + 12])[0], 0x20000)

    def test_patch_s_field_no_s_block_unchanged(self):
        data = b"LSPK" + _block(b"C", 1)
        self.assertEqual(server.patch_s_field(data), data)

    def test_patch_duplex_sets_flag(self):
        data = b"LSPK" + _block(b"S", 3) + _block(b"C", 1)
        out = server.patch_duplex(data, 1)
        # S 块占 4+12+12=28，C 头 28..40，C body 从 40 起，标志位 = body+2
        c_body = 4 + 12 + 12 + 12
        self.assertEqual(out[c_body + 2], 1)
        out = server.patch_duplex(data, 0)
        self.assertEqual(out[c_body + 2], 0)


class RasterHeaderTests(unittest.TestCase):
    @staticmethod
    def _raster(pages, width=600, height=800, bpl=600):
        hdr = bytearray(server.CUPS_HDR_LEN)
        struct.pack_into("<I", hdr, 372, width)
        struct.pack_into("<I", hdr, 376, height)
        struct.pack_into("<I", hdr, 392, bpl)
        unit = bytes(hdr) + b"\x00" * (bpl * height)
        return b"RAST" + unit * pages

    def _tmp(self, data):
        fd, path = tempfile.mkstemp(suffix=".raster")
        os.write(fd, data)
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def test_patch_two_pages_duplex_tumble(self):
        path = self._tmp(self._raster(2))
        n = server.patch_raster_header(path, duplex=True, tumble=True)
        self.assertEqual(n, 2)
        with open(path, "rb") as f:
            data = f.read()
        # 第 1 页头从偏移 4 开始
        for pos in (4, 4 + server.CUPS_HDR_LEN + 600 * 800):
            self.assertEqual(struct.unpack("<I", data[pos + 272:pos + 276])[0], 1)
            self.assertEqual(struct.unpack("<I", data[pos + 368:pos + 372])[0], 1)

    def test_patch_single_page_simplex(self):
        path = self._tmp(self._raster(1))
        n = server.patch_raster_header(path, duplex=False, tumble=False)
        self.assertEqual(n, 1)
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(struct.unpack("<I", data[4 + 272:4 + 276])[0], 0)
        self.assertEqual(struct.unpack("<I", data[4 + 368:4 + 372])[0], 0)

    def test_patch_stops_on_invalid_header(self):
        # 第二页 bpl=0 → 只应处理第 1 页
        bad = bytearray(self._raster(2))
        second = 4 + server.CUPS_HDR_LEN + 600 * 800
        struct.pack_into("<I", bad, second + 392, 0)
        path = self._tmp(bytes(bad))
        self.assertEqual(server.patch_raster_header(path, True, True), 1)


class HelperTests(unittest.TestCase):
    def test_safe_title(self):
        # 中文被剥掉，只留 ASCII 安全字符
        self.assertEqual(server._safe_title("季度报表 v2.1_final.pdf"), "v2.1_final.pdf")
        self.assertEqual(server._safe_title(None), "document")
        self.assertEqual(server._safe_title("   "), "document")
        self.assertEqual(len(server._safe_title("x" * 50)), 20)

    def test_page_box_known_and_fallback(self):
        self.assertEqual(server.page_box("A4"), (571, 822))
        self.assertEqual(server.page_box("A3"), (818, 1171))
        # 未知纸张走 PAPERS 兜底（PAPERS 里没有 B5 → 按 A4 尺寸减边距）
        w, h = server.PAPERS["A4"]
        self.assertEqual(server.page_box("B5"), (w - 24, h - 20))

    def test_to_pdf_passthrough(self):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"%PDF-1.4 test")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        self.assertEqual(server.to_pdf(path, tempfile.gettempdir()), path)

    def test_merge_pdfs_single_copy(self):
        fd, src = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"%PDF-1.4 src")
        os.close(fd)
        self.addCleanup(os.unlink, src)
        out = os.path.join(tempfile.mkdtemp(), "merged.pdf")
        server.merge_pdfs([src], out)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4 src")


class MultipartTests(unittest.TestCase):
    @staticmethod
    def _body(fields, files, boundary="b0undary42"):
        chunks = []
        for k, v in fields.items():
            chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                           % (boundary, k, v)).encode())
        for name, (filename, payload) in files.items():
            chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                           "Content-Type: application/octet-stream\r\n\r\n"
                           % (boundary, name, filename)).encode())
            chunks.append(payload)
            chunks.append(b"\r\n")
        chunks.append(("--%s--\r\n" % boundary).encode())
        return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary

    def _parse(self, fields, files):
        raw, ctype = self._body(fields, files)
        h = server.Handler.__new__(server.Handler)
        h.headers = {"Content-Type": ctype}
        return h._parse_multipart(raw)

    def test_parse_fields_and_files(self):
        fields, files = self._parse(
            {"paper": "A4", "pages": "1-4", "copies": "2"},
            {"doc.pdf": ("doc.pdf", b"%PDF-payload")})
        self.assertEqual(fields["paper"], "A4")
        self.assertEqual(fields["pages"], "1-4")
        self.assertEqual(fields["copies"], "2")
        self.assertEqual(files, [("doc.pdf", b"%PDF-payload")])

    def test_parse_chinese_filename(self):
        fields, files = self._parse({}, {"报表.pdf": ("报表.pdf", b"xyz")})
        self.assertEqual(files[0][0], "报表.pdf")
        self.assertEqual(fields, {})


class SanitizeMsgTests(unittest.TestCase):
    """规则：整条不含中文、且含 3+ 连续英文字母 → 换成状态默认文案；其余保留。"""

    def test_chinese_kept(self):
        self.assertEqual(server.sanitize_msg("error", "发送失败：打印机未响应"), "发送失败：打印机未响应")

    def test_mixed_chinese_english_kept(self):
        # 旧规则（含 3+ 字母就整条换）会把中文错误误伤成「打印机未响应」
        self.assertEqual(server.sanitize_msg("error", "文件已损坏或不是有效的 PDF"),
                         "文件已损坏或不是有效的 PDF")
        self.assertEqual(server.sanitize_msg("error", "文档转换失败：该文件格式暂不支持或文件已损坏"),
                         "文档转换失败：该文件格式暂不支持或文件已损坏")

    def test_english_replaced_by_status_default(self):
        self.assertEqual(server.sanitize_msg("error", "convert failed: source file"),
                         "发送失败：打印机未响应或未就绪")
        self.assertEqual(server.sanitize_msg("done", "filter exit 3"), "已发送到打印机")

    def test_empty_uses_default(self):
        # 现状：空消息只映射 done，其余状态落到「处理中…」
        self.assertEqual(server.sanitize_msg("done", ""), "已发送到打印机")
        self.assertEqual(server.sanitize_msg("pending", ""), "处理中…")
        self.assertEqual(server.sanitize_msg("unknown", None), "处理中…")


class FmtTsTests(unittest.TestCase):
    def test_uses_local_time(self):
        ts = 1700000000
        self.assertEqual(server.fmt_ts(ts),
                         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)))

    def test_empty(self):
        self.assertEqual(server.fmt_ts(0), "")
        self.assertEqual(server.fmt_ts(None), "")


class ToPdfTests(unittest.TestCase):
    def test_unconvertible_format_raises_joberror(self):
        # 宿主与容器都转不了该扩展名 → to_pdf 必须抛中文 JobError，
        # 而不是旧的 RuntimeError("convert failed")
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "a.unknownext")
            with open(src, "wb") as f:
                f.write(b"garbage")
            with contextlib.redirect_stderr(io.StringIO()):   # 静音预期内的 traceback
                with self.assertRaises(server.JobError) as cm:
                    server.to_pdf(src, td)
            self.assertIn("转换失败", str(cm.exception))

    def test_pdf_passthrough_no_conversion(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "a.pdf")
            with open(src, "wb") as f:
                f.write(b"%PDF-1.4")
            self.assertEqual(server.to_pdf(src, td), src)


class LogMessageTests(unittest.TestCase):
    def _handler(self, path):
        h = server.Handler.__new__(server.Handler)
        h.path = path
        h.client_address = ("127.0.0.1", 12345)   # super().log_message 要打来源地址
        return h

    def test_polling_ok_suppressed(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self._handler("/api/state").log_message(
                '"%s" %s %s', "GET /api/state HTTP/1.1", 200, 512)
            self._handler("/api/health").log_message(
                '"%s" %s %s', "GET /api/health HTTP/1.1", 200, 15)
        self.assertEqual(buf.getvalue(), "")

    def test_polling_error_and_normal_requests_logged(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self._handler("/api/state").log_message(
                '"%s" %s %s', "GET /api/state HTTP/1.1", 500, 0)
            self._handler("/api/print").log_message(
                '"%s" %s %s', "POST /api/print HTTP/1.1", 200, 100)
        out = buf.getvalue()
        self.assertIn("500", out)
        self.assertIn("/api/print", out)


class PagesTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(server.parse_pages("1-4"), (1, 4))
        self.assertEqual(server.parse_pages(" 2 - 3 "), (2, 3))
        self.assertIsNone(server.parse_pages(""))
        self.assertIsNone(server.parse_pages(None))

    def test_parse_invalid(self):
        for bad in ("abc", "5", "4-2", "0-3", "1-4-9", "-1-3", "1.5-2"):
            with self.assertRaises(server.JobError, msg=bad):
                server.parse_pages(bad)

    def test_resolve_without_pages(self):
        self.assertIsNone(server.resolve_pages("x.pdf", None, count=10))

    def test_resolve_clamps_last(self):
        self.assertEqual(server.resolve_pages("x.pdf", (2, 99), count=5), (2, 5))
        self.assertEqual(server.resolve_pages("x.pdf", (3, 5), count=5), (3, 5))

    def test_resolve_rejects_out_of_range(self):
        with self.assertRaises(server.JobError):
            server.resolve_pages("x.pdf", (6, 8), count=5)
        with self.assertRaises(server.JobError):
            server.resolve_pages("x.pdf", (0, 2), count=5)

    def test_build_preview_page_range(self):
        if not (shutil.which("gs") and shutil.which("pdfinfo")):
            self.skipTest("gs/pdfinfo 不在宿主 PATH（容器里会跑）")
        with open(server.make_test_pdf(), "rb") as f:      # 2 页文档
            payload = f.read()
        info = server.build_preview([("t.pdf", payload)],
                                    {"paper": "A4", "fit": True, "pages": (2, 5)})
        self.assertEqual(info["pages"], 2)
        self.assertEqual(info["range"], [2, 2])            # 结束页 5 被夹到 2
        self.assertEqual(len(info["images"]), 1)
        self.assertEqual(info["images"][0]["page"], 2)      # 文档页号，不是序号
        mpath = os.path.join(server.PREVIEW_DIR, info["token"], "meta.json")
        with open(mpath, encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["pages"], [2, 2])
        self.assertEqual(meta["paper"], "A4")
        # 起始页超界 → 中文 JobError
        with self.assertRaises(server.JobError):
            server.build_preview([("t.pdf", payload)],
                                 {"paper": "A4", "fit": True, "pages": (3, 4)})


class CopiesTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(server.parse_copies(None), 1)
        self.assertEqual(server.parse_copies(""), 1)
        self.assertEqual(server.parse_copies(" 5 "), 5)
        self.assertEqual(server.parse_copies("99"), 99)

    def test_parse_invalid(self):
        for bad in ("abc", "0", "100", "-2", "1.5", "两份"):
            with self.assertRaises(server.JobError, msg=bad):
                server.parse_copies(bad)


class CleanupTests(unittest.TestCase):
    def test_cleanup_removes_expired_done_jobs(self):
        old_id, keep_id = "deadbeef", "cafebabe"
        with server._lock:
            server._jobs[old_id] = {"id": old_id, "status": "done",
                                    "created_ts": time.time() - 8 * 86400}
            server._jobs[keep_id] = {"id": keep_id, "status": "done",
                                     "created_ts": time.time()}
        os.makedirs(os.path.join(server.JOBS_DIR, old_id), exist_ok=True)
        try:
            server._cleanup()                     # 加锁执行，不与 worker 抢 _jobs
            with server._lock:
                self.assertNotIn(old_id, server._jobs)
                self.assertIn(keep_id, server._jobs)   # 未过期的保留
        finally:
            with server._lock:
                server._jobs.pop(keep_id, None)
                server._jobs.pop(old_id, None)

    def test_cleanup_keeps_active_jobs(self):
        jid = "abcdef01"
        with server._lock:
            server._jobs[jid] = {"id": jid, "status": "printing",
                                 "created_ts": time.time() - 8 * 86400}
        try:
            server._cleanup()
            with server._lock:
                self.assertIn(jid, server._jobs)
        finally:
            with server._lock:
                server._jobs.pop(jid, None)


class SkipUnlessTools(unittest.TestCase):
    def test_make_test_pdf(self):
        if not shutil.which("gs"):
            self.skipTest("gs 不在宿主 PATH（容器里会跑）")
        out = server.make_test_pdf()
        self.assertTrue(os.path.isfile(out))
        self.assertGreater(os.path.getsize(out), 1000)

    def test_pdf_page_count_two_pages(self):
        if not (shutil.which("gs") and shutil.which("pdfinfo")):
            self.skipTest("gs/pdfinfo 不在宿主 PATH（容器里会跑）")
        self.assertEqual(server.pdf_page_count(server.make_test_pdf()), 2)

    def test_pdf_page_count_rejects_garbage(self):
        if not shutil.which("pdfinfo"):
            self.skipTest("pdfinfo 不在宿主 PATH（容器里会跑）")
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"this is not a pdf at all")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with self.assertRaises(RuntimeError):
            server.pdf_page_count(path)


if __name__ == "__main__":
    unittest.main()
