#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线端到端验证：不碰真打印机，跑通 上传→PDF→raster→KPSL→发送 全链路。

流程（宿主上执行，需要 podman）：
  1. 起一个假打印机（本机 TCP 监听），把收到的原始字节存下来；
  2. 起一个一次性容器 taskalfa-e2e（PRINTER_IP=127.0.0.1 指向假打印机，不挂真实数据）；
  3. 把 tests/ 塞进容器跑全量单测（容器里有 gs/pdfinfo/libreoffice，skip 会真正跑起来）；
  4. 容器内造三个真实文件（csv→xlsx、odp→pptx、txt→pdf）并经 HTTP /api/print 上传；
  5. 等三个作业全部 done，断言 kpsl_size>0；
  6. 断言假打印机收到 3 条流：头部与 captures/ 金标准一致（LSPK 魔数 + 16 字节头 + UTF-16LE 标题）。

用法：python3 experiments/offline_e2e.py
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = "taskalfa-print:latest"
E2E_NAME = "talfa-e2e"
LISTEN_PORT = 1901          # 假打印机端口
WEB_PORT = 18099            # e2e 服务端口（避开生产的 8080）

# ----------------------------------------------------------------金标准
def load_golden_header():
    cap = os.path.join(ROOT, "captures", "cap-20261113-104619-04.bin")
    with open(cap, "rb") as f:
        head = f.read(16)
    assert head[:4] == b"LSPK", "金标准文件头不是 LSPK: %r" % head[:4]
    return head


# ---------------------------------------------------------------- 假打印机
class FakePrinter:
    def __init__(self, port):
        self.port = port
        self.streams = []
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(8)
        self._srv.settimeout(0.5)
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                buf = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                # 读到 EOF（客户端 shutdown(WR)）就关连接，
                # 服务端 _send_once 的 recv 会立刻拿到 EOF，不会等 120s 超时。
                self.streams.append(buf)

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=3)


# ---------------------------------------------------------------- 容器小工具
def sh(args, check=True, input_bytes=None):
    p = subprocess.run(args, input=input_bytes, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise SystemExit("命令失败(%d): %s\n%s" % (p.returncode, " ".join(args), out))
    return p.returncode, out


def wait_health(port, timeout=60):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/health" % port, timeout=3) as r:
                if json.loads(r.read()) == {"ok": True}:
                    return
        except Exception as exc:          # 连接不上/还没起来
            last = exc
        time.sleep(0.5)
    raise SystemExit("服务健康检查超时: %s" % last)


# ---------------------------------------------------------------- 容器内脚本
INNER = r"""
import json, os, subprocess, sys, time, urllib.request, zipfile

PORT = __PORT__
FX = "/tmp/fx"
os.makedirs(FX, exist_ok=True)

def lo(src, fmt, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(
        ["libreoffice", "--headless", "--norestore", "--convert-to", fmt,
         "--outdir", FX, src],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=300)
    print(p.stdout.decode("utf-8", "replace").strip())
    out = os.path.join(FX, os.path.splitext(os.path.basename(src))[0] + "." + fmt)
    if not os.path.exists(out):
        raise SystemExit("LibreOffice %s→%s 没有产出: %s" % (src, fmt, out))
    return out

# --- 1. 造源文件 -------------------------------------------------------
with open(os.path.join(FX, "t.csv"), "w") as f:
    f.write("Name,Qty\nApple,3\nBanana,5\n")
with open(os.path.join(FX, "t.txt"), "w") as f:
    f.write("Offline E2E chain test page.\nsecond line\n")

# 最小 ODP（mimetype 必须 stored 且第一项）
ct = "application/vnd.oasis.opendocument.presentation"
manifest = '''<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">
 <manifest:file-entry manifest:full-path="/" manifest:media-type="%s"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
 <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>
 <manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>
</manifest:manifest>''' % ct
content = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:version="1.3">
 <office:body>
  <office:presentation>
   <draw:page draw:name="Page1">
    <draw:frame svg:x="2cm" svg:y="2cm" svg:width="12cm" svg:height="4cm">
     <draw:text-box><text:p>Offline E2E</text:p></draw:text-box>
    </draw:frame>
   </draw:page>
  </office:presentation>
 </office:body>
</office:document-content>'''
styles = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" office:version="1.3">
 <office:styles/>
</office:document-styles>'''
meta = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" office:version="1.3">
 <office:meta/>
</office:document-meta>'''
odp = os.path.join(FX, "t.odp")
with zipfile.ZipFile(odp, "w") as z:
    z.writestr(zipfile.ZipInfo("mimetype"), ct,
               compress_type=zipfile.ZIP_STORED)   # 规范要求 stored 且首个
    z.writestr("META-INF/manifest.xml", manifest, zipfile.ZIP_DEFLATED)
    z.writestr("content.xml", content, zipfile.ZIP_DEFLATED)
    z.writestr("styles.xml", styles, zipfile.ZIP_DEFLATED)
    z.writestr("meta.xml", meta, zipfile.ZIP_DEFLATED)

# --- 2. LibreOffice 转换（独立 profile，避免和常驻服务抢锁） -----------
prof = {"UserInstallation": "file:///tmp/lo_e2e_fix"}
files = {}
files["xlsx"] = lo(os.path.join(FX, "t.csv"), "xlsx", prof)
files["pptx"] = lo(odp, "pptx", prof)
files["pdf"] = lo(os.path.join(FX, "t.txt"), "pdf", prof)
for k, v in files.items():
    print("fixture %-5s %6d bytes" % (k, os.path.getsize(v)))

# --- 3. 经 HTTP 上传 ---------------------------------------------------
def post(path, fields, file_field, filename, payload, timeout=30):
    b = "b0undary42"
    chunks = []
    for k, v in fields.items():
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (b, k, v)).encode())
    chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                   "Content-Type: application/octet-stream\r\n\r\n"
                   % (b, file_field, filename)).encode())
    chunks.append(payload)
    chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % b).encode())
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path), data=b"".join(chunks),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % b})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

order = ["pdf", "xlsx", "pptx"]
for kind in order:
    with open(files[kind], "rb") as f:
        resp = post("/api/print", {"paper": "A4", "fit": "1", "copies": "1",
                                   "duplex": "off"},
                    "file", os.path.basename(files[kind]), f.read())
    print("upload %-5s -> job %s" % (kind, resp["job"]["id"]))

# --- 4. 轮询到全部完成 -------------------------------------------------
deadline = time.time() + 300
while time.time() < deadline:
    with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % PORT,
                                timeout=10) as r:
        jobs = json.loads(r.read())["jobs"]
    if len(jobs) >= 3 and all(j["status"] in ("done", "error") for j in jobs):
        break
    time.sleep(1)
else:
    raise SystemExit("轮询超时: %s" % json.dumps(jobs, ensure_ascii=False))

for j in jobs:
    print("job %-8s %-6s kpsl=%s msg=%s" % (
        j["name"], j["status"], j.get("kpsl_size"), j.get("message")))
bad = [j for j in jobs if j["status"] != "done" or not j.get("kpsl_size")]
if bad:
    raise SystemExit("有作业未成功: %s" % json.dumps(bad, ensure_ascii=False))
if len(jobs) != 3:
    raise SystemExit("期望 3 个作业，实际 %d" % len(jobs))
print("INNER_OK")
"""

# ---------------------------------------------------------------- 主流程
def main():
    golden = load_golden_header()
    print("金标准头: %s" % golden.hex())

    printer = FakePrinter(LISTEN_PORT)
    printer.start()
    print("假打印机已监听 127.0.0.1:%d" % LISTEN_PORT)

    # 清掉上次残留
    sh(["podman", "rm", "-f", E2E_NAME], check=False)
    sh(["podman", "run", "-d", "--name", E2E_NAME, "--network=host",
        "-e", "PRINTER_IP=127.0.0.1",
        "-e", "PRINTER_PORT=%d" % LISTEN_PORT,
        "-e", "WEB_PORT=%d" % WEB_PORT,
        "-e", "TZ=Asia/Shanghai",
        IMAGE])

    try:
        wait_health(WEB_PORT)
        print("e2e 服务健康检查通过 (:%d)" % WEB_PORT)

        # 容器内跑全量单测（gs/pdfinfo/libreoffice 都在，skip 会真正执行）
        p = subprocess.run("tar -C %s --exclude=__pycache__ -cf - tests" % ROOT,
                           shell=True, stdout=subprocess.PIPE)
        sh(["podman", "exec", "-i", E2E_NAME, "tar", "-C", "/app", "-xf", "-"],
           input_bytes=p.stdout)
        rc, out = sh(["podman", "exec", E2E_NAME, "python3", "-m", "unittest",
                      "discover", "-s", "/app/tests", "-t", "/app", "-v"],
                     check=False)
        print(out)
        if rc != 0:
            raise SystemExit("容器内单测失败 (rc=%d)" % rc)

        # 容器内造文件 + 上传 + 轮询
        rc, out = sh(["podman", "exec", "-i", E2E_NAME, "python3", "-"],
                     check=False,
                     input_bytes=INNER.replace("__PORT__", str(WEB_PORT)).encode())
        print(out)
        if rc != 0 or "INNER_OK" not in out:
            raise SystemExit("容器内 E2E 脚本失败 (rc=%d)" % rc)

        # 假打印机收到的流：与金标准头部逐字节比对。
        # 注意过滤空流：服务的 TCP 探活（create_connection 后立刻 close）
        # 也会被假打印机 accept 到，收到 0 字节。
        deadline = time.time() + 10
        while len([s for s in printer.streams if s]) < 3 and time.time() < deadline:
            time.sleep(0.2)
        streams = [s for s in printer.streams if s]
        print("假打印机共收到 %d 条非空流（另有 %d 次探活空连接）" % (
            len(streams), len(printer.streams) - len(streams)))
        if len(streams) != 3:
            raise SystemExit("期望 3 条流，实际 %d" % len(streams))
        expected_titles = {"pdf": "t.pdf", "xlsx": "t.xlsx", "pptx": "t.pptx"}
        # KPSL 头部布局（金标准与 t1.kpsl 实测一致）：
        #   0x00..0x10 LSPK+头块  0x14 user(UTF-16LE, 定长 32B)  0x34 时间戳(ASCII)
        #   0xa4 D 块（len 0x10 = 64B 载荷）  0xb0..0xf0 标题(UTF-16LE, NUL 结尾)
        # 金标准 user="RuanFumin"（Windows 登录名）；本服务固定传 "print"（server.py:518）。
        # 上传顺序 = worker FIFO = 收到顺序
        for i, kind in enumerate(["pdf", "xlsx", "pptx"]):
            s = streams[i]
            head = s[:16]
            user = s[20:52].decode("utf-16-le").split("\0")[0]
            title = s[0xb0:0xf0].decode("utf-16-le").split("\0")[0]
            want = expected_titles[kind]
            ok_head = head == golden
            ok_d = s[0xa4:0xa8] == b"\x1b$0D"
            ok_user = user == "print"
            ok_title = title == want
            print("流[%d] (%s) len=%d head=%s D块=%s user=%r title=%r" % (
                i, kind, len(s), "OK" if ok_head else "MISMATCH " + head.hex(),
                "OK" if ok_d else "MISMATCH", user, title))
            if not ok_head:
                raise SystemExit("流[%d] 头部与金标准不一致" % i)
            if not ok_d:
                raise SystemExit("流[%d] 0xa4 处不是 D 块" % i)
            if not ok_user:
                raise SystemExit("流[%d] user 应为 'print'，实为 %r" % (i, user))
            if not ok_title:
                raise SystemExit("流[%d] 标题应为 %r，实为 %r" % (i, want, title))
        print("E2E_OK: 3 个作业全部 done，KPSL 头部/user/标题均符合预期（金标准结构一致）")
    finally:
        sh(["podman", "rm", "-f", E2E_NAME], check=False)
        printer.stop()
        print("e2e 容器与假打印机已清理")


if __name__ == "__main__":
    main()
