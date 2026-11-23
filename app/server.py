#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TASKalfa 2011 局域网打印服务

流水线： 上传文件 -> PDF -> CUPS raster (gs cups device) -> KPSL (rastertokpsl)
        -> 9100 raw 发给打印机

KPSL 是京瓷私有光栅页描述语言（流内魔数 "LSPK"）。TASKalfa 2011 属 GDI 机型，
只认这种私有光栅流，不认 PCL / PS / PDF。
"""
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import traceback
import uuid
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    from PIL import Image, ImageOps, ImageSequence
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

# ---------------------------------------------------------------- config
PRINTER_IP = os.environ.get("PRINTER_IP", "192.168.0.199")
PRINTER_PORT = int(os.environ.get("PRINTER_PORT", "9100"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
FILTER = os.environ.get("KPSL_FILTER", "/app/rastertokpsl_A3")
SNMP_COMMUNITY = os.environ.get("SNMP_COMMUNITY", "public")
KEEP_DAYS = 7
MAX_UPLOAD = 300 * 1024 * 1024
# POST 并发上限：单个上传最多吃 300MB 内存 + 一次转换，ThreadingHTTPServer
# 默认无界接客，几个大文件同时传就可能把宿主内存打爆 → 超限直接 503。
MAX_POST_CONCURRENCY = 4
_post_slots = threading.BoundedSemaphore(MAX_POST_CONCURRENCY)

PAPERS = {
    "A4": (595, 842),
    "A3": (842, 1191),
    "A5": (420, 595),
    "Letter": (612, 792),
    "Legal": (612, 1008),
}

# 可打印区（pt，取自官方 PPD 的 ImageableArea：<纸> llx lly urx ury）。
# A4 = 12 10 583 832 → 571x822pt = 201.4x290.0mm，与 Windows 驱动发出的 4760x6814 点一致。
IMAGEABLE = {
    "A4": (571, 822),
    "A3": (818, 1171),     # 12 10 830 1181
    "A5": (397, 575),      # 12 10 409 585
    "Letter": (588, 768),  # 12 08 600 776
    "Legal": (588, 992),   # 12 08 600 1000
}
# 不可打印边距（仅用于非缩放时的 PageOffset 居中裁边）
PAGE_MARGIN = (12, 10)

# 打印机物理分辨率 600x600；不要用 300/1200——滤镜会按 PPD 默认值解释，
# 光栅分辨率与它声明的分辨率不一致时几何会整体错位。
RASTER_DPI = 600

SNMP_OIDS = {
    "page_count": "1.3.6.1.2.1.43.10.2.1.4.1.1",
    "panel": "1.3.6.1.2.1.43.16.5.1.2.1.1",
    "device_status": "1.3.6.1.2.1.25.3.2.1.5.1",
    "model": "1.3.6.1.2.1.25.3.2.1.3.1",
}

DEVICE_STATES = {1: "其他", 2: "未知", 3: "空闲", 4: "打印中", 5: "预热中", 6: "停止中"}

PREVIEW_DIR = os.path.join(DATA_DIR, "preview")
PREVIEW_MAX_PAGES = 12      # 预览最多渲染多少页
PREVIEW_DPI = 120
PREVIEW_KEEP_HOURS = 24

os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(PREVIEW_DIR, exist_ok=True)

_lock = threading.Lock()
_jobs = {}
_queue = []


class JobError(Exception):
    """带简体中文说明的作业错误。"""


def fmt_ts(ts):
    if not ts:
        return ""
    # 固定按东八区显示，避免容器时区为 UTC 导致时间差 8 小时
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(ts) + 8 * 3600))


# ---------------------------------------------------------------- SNMP
def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _ber_int(n):
    if n == 0:
        return b"\x02\x01\x00"
    b = n.to_bytes((n.bit_length() + 8) // 8, "big", signed=True)
    return b"\x02" + _ber_len(len(b)) + b


def _ber_oid(oid):
    parts = [int(x) for x in oid.split(".")]
    # 首子标识符 = arc1*40+arc2，可能 >=128（如 1.80 → 120 没事，2.x 可到 4 位数），
    # 必须按 base-128 多字节编码，单字节会像旧实现那样直接 ValueError。
    first = parts[0] * 40 + parts[1]
    body = bytearray(_ber_subid(first))
    for p in parts[2:]:
        body += _ber_subid(p)
    return b"\x06" + _ber_len(len(body)) + bytes(body)


def _ber_subid(p):
    """一个子标识符的 base-128 变长编码。"""
    if p < 0x80:
        return bytes([p])
    chunk = []
    while p:
        chunk.append(p & 0x7F)
        p >>= 7
    chunk.reverse()
    return bytes([c | 0x80 for c in chunk[:-1]] + [chunk[-1]])


def _ber_parse(data, pos=0):
    tag = data[pos]
    pos += 1
    ln = data[pos]
    pos += 1
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    return tag, pos, ln


def _snmp_get_one(oid, timeout=1.5):
    """京瓷 SNMP 代理不处理多 varbind 请求，必须一次只查一个 OID。"""
    # VarBind 必须是 SEQUENCE { name, NULL }，少一层会导致打印机直接丢包
    varbind = b"\x30" + _ber_len(len(_ber_oid(oid)) + 2) + _ber_oid(oid) + b"\x05\x00"
    vb_list = b"\x30" + _ber_len(len(varbind)) + varbind
    pdu_body = _ber_int(int(time.time()) & 0x7FFFFFFF) + b"\x02\x01\x00" + b"\x02\x01\x00"
    pdu = b"\xa0" + _ber_len(len(pdu_body) + len(vb_list)) + pdu_body + vb_list
    msg_body = (
        b"\x02\x01\x00"
        + b"\x04" + _ber_len(len(SNMP_COMMUNITY)) + SNMP_COMMUNITY.encode()
        + pdu
    )
    pkt = b"\x30" + _ber_len(len(msg_body)) + msg_body

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (PRINTER_IP, 161))
        data, _ = s.recvfrom(65535)
    finally:
        s.close()

    # 逐层定位：外层 -> version -> community -> pdu(进入) -> req-id -> err-status
    # -> err-index -> varbind-list(进入)
    _, pos, _ = _ber_parse(data, 0)
    for _ in range(2):            # version, community
        _, c, l = _ber_parse(data, pos)
        pos = c + l
    _, pos, _ = _ber_parse(data, pos)   # pdu，进入其内容
    for _ in range(3):            # req-id, err-status, err-index
        _, c, l = _ber_parse(data, pos)
        pos = c + l
    _, pos, _ = _ber_parse(data, pos)   # varbind-list，进入其内容
    out = {}
    while pos < len(data):
        try:
            _, p1, l1 = _ber_parse(data, pos)
            end = p1 + l1
            _, p2, l2 = _ber_parse(data, p1)
            oid_raw = data[p2:p2 + l2]
            tag, p3, l3 = _ber_parse(data, p2 + l2)
            raw = data[p3:p3 + l3]
            if tag == 0x04:
                val = raw.decode("utf-8", "replace").strip()
            elif tag == 0x02:
                val = int.from_bytes(raw, "big", signed=True)
            elif tag in (0x41, 0x42, 0x43, 0x44, 0x46):
                val = int.from_bytes(raw, "big")
            else:
                val = raw.hex()
            out[_oid_to_str(oid_raw)] = val
            pos = end
        except Exception:
            break
    return out


def snmp_get(oids, timeout=1.5):
    """逐个 OID 查询，返回 {oid: value}；全部失败时抛异常。"""
    out = {}
    for oid in oids:
        try:
            vals = _snmp_get_one(oid, timeout)
        except Exception:
            continue
        if oid in vals:
            out[oid] = vals[oid]
    if not out:
        raise RuntimeError("snmp timeout")
    return out


def _oid_to_str(raw):
    if not raw:
        return ""
    # 逐字节收 base-128，首子标识符解出来后再拆成 arc1/arc2（与 _ber_oid 对称）
    vals, val = [], 0
    for b in raw:
        val = (val << 7) | (b & 0x7F)
        if not b & 0x80:
            vals.append(val)
            val = 0
    if not vals:
        return ""
    first = vals[0]
    # X.690：arc1∈{0,1,2}，arc2<40；arc1=2 时 arc2 无上界（first>=80）
    if first < 40:
        arcs = [0, first]
    elif first < 80:
        arcs = [1, first - 40]
    else:
        arcs = [2, first - 80]
    parts = [str(a) for a in arcs] + [str(v) for v in vals[1:]]
    return ".".join(parts)


_status_cache = {"ts": 0, "value": None}
_status_lock = threading.Lock()      # 串行化真实探测，避免并发 SNMP 风暴
_refresh_lock = threading.Lock()
_refreshing = False

# 缓存有效期 / 后台刷新周期：探测（TCP 3s + 4 次 SNMP 1.5s）离线时约 9s，
# 放在请求线程里做会把 /api/state 卡死 9 秒——所以只在后台跑，请求侧
# stale-while-revalidate：有旧值立刻回旧值，同时踢一脚后台刷新。
STATUS_TTL = 8
STATUS_REFRESH = 5


def _fetch_status():
    """真实探测（同一时刻只允许一个在跑），结果写入 _status_cache。"""
    with _status_lock:
        now = time.time()
        if _status_cache["value"] and now - _status_cache["ts"] < STATUS_TTL:
            return _status_cache["value"]          # 等待期间别人已刷过
        result = {
            "reachable": False,
            "model": "TASKalfa 2011",
            "page_count": None,
            "panel": "",
            "state": "",
            "snmp_ok": False,
            "port_open": False,
        }
        try:
            s = socket.create_connection((PRINTER_IP, PRINTER_PORT), 3)
            s.close()
            result["reachable"] = True
            result["port_open"] = True
        except Exception:
            pass

        try:
            vals = snmp_get(list(SNMP_OIDS.values()))
            result["snmp_ok"] = True
            result["reachable"] = True
            result["page_count"] = vals.get(SNMP_OIDS["page_count"])
            result["panel"] = vals.get(SNMP_OIDS["panel"], "")
            ds = vals.get(SNMP_OIDS["device_status"])
            if isinstance(ds, int):
                result["state"] = DEVICE_STATES.get(ds, str(ds))
            else:
                result["state"] = str(ds)
            model = vals.get(SNMP_OIDS["model"])
            if model:
                result["model"] = model
        except Exception:
            result["snmp_ok"] = False

        _status_cache.update(ts=time.time(), value=result)
        return result


def _kick_refresh():
    """起一个后台探测线程（同一时刻至多一个）；不阻塞调用方。"""
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True

    def run():
        global _refreshing
        try:
            _fetch_status()
        finally:
            with _refresh_lock:
                _refreshing = False

    threading.Thread(target=run, daemon=True).start()


def _status_loop():
    while True:
        try:
            _fetch_status()
        except Exception:
            pass
        time.sleep(STATUS_REFRESH)


def printer_status(force=False):
    now = time.time()
    cached = _status_cache["value"]
    if cached and now - _status_cache["ts"] < STATUS_TTL:
        return cached
    if force:
        return _fetch_status()
    # 过期（或启动后还没测过）：立刻回旧值/占位值，后台补测
    _kick_refresh()
    return cached or {
        "reachable": False,
        "model": "TASKalfa 2011",
        "page_count": None,
        "panel": "",
        "state": "",
        "snmp_ok": False,
        "port_open": False,
    }


# ---------------------------------------------------------------- conversion
def _run(cmd, timeout=600):
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")[-800:]
        raise RuntimeError("%s: %s" % (cmd[0], err))
    return p.stdout


def to_pdf(src, outdir):
    ext = os.path.splitext(src)[1].lower()
    if ext == ".pdf":
        return src
    if ext in (".ps", ".eps"):
        out = os.path.join(outdir, "conv.pdf")
        _run(["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
              "-sOutputFile=" + out, src])
        return out
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"):
        if not HAVE_PIL:
            raise RuntimeError("no pillow")
        out = os.path.join(outdir, "conv.pdf")
        img = Image.open(src)
        frames = [ImageOps.exif_transpose(f.copy()).convert("RGB")
                  for f in ImageSequence.Iterator(img)]
        if not frames:
            frames = [img.convert("RGB")]
        frames[0].save(out, save_all=True, append_images=frames[1:], resolution=300)
        return out
    out = os.path.join(outdir, "conv.pdf")
    prof = "file://" + os.path.join(DATA_DIR, ".lo")
    try:
        _run(["libreoffice", "--headless", "--norestore",
              "-env:UserInstallation=" + prof,
              "--convert-to", "pdf", "--outdir", outdir, src], timeout=600)
    except Exception:
        pass
    if os.path.exists(out):
        return out
    for name in os.listdir(outdir):
        if name.lower().endswith(".pdf"):
            return os.path.join(outdir, name)
    raise RuntimeError("convert failed")


def page_box(paper):
    """返回该纸张的可打印区（pt）。"""
    box = IMAGEABLE.get(paper)
    if box:
        return box
    w, h = PAPERS.get(paper, PAPERS["A4"])
    mx, my = PAGE_MARGIN
    return w - 2 * mx, h - 2 * my


def pdf_to_raster(pdf, raster, opts):
    w, h = page_box(opts["paper"])
    cmd = ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=cups",
           "-r%d" % RASTER_DPI,
           "-dDEVICEWIDTHPOINTS=%d" % w, "-dDEVICEHEIGHTPOINTS=%d" % h,
           "-dFIXEDMEDIA",
           # 必须输出 8bpc 灰度（PPD 里 ColorModel Gray 的定义就是 cupsBitsPerColor 8）。
           # 若输出 1bpp，rastertokpsl 会走另一条条带编码路径，打印机把该条带
           # 按「1 字节 = 1 点」渲染，横向只有正确宽度的 1/8（实测 25mm vs 201mm）。
           "-dcupsBitsPerColor=8", "-dcupsColorSpace=3"]
    if opts.get("pages"):
        first, last = opts["pages"]
        cmd += ["-dFirstPage=%d" % first, "-dLastPage=%d" % last]
    if opts.get("fit"):
        cmd += ["-dPDFFitPage"]
    cmd += ["-sOutputFile=" + raster]
    if not opts.get("fit"):
        # 非缩放：把整页按不可打印边距整体下移左移，居中裁掉边距外的部分
        mx, my = PAGE_MARGIN
        cmd += ["-c", "<</PageOffset [%d %d]>> setpagedevice" % (-mx, -my), "-f", pdf]
    else:
        cmd += [pdf]
    _run(cmd)


CUPS_HDR_LEN = 1796


def patch_raster_header(raster, duplex, tumble):
    """把单/双面写进 CUPS raster 每页的页头（Duplex/Tumble 字段）。

    `pos` 指向 cups_page_header2_t 结构体起点（文件第 4 字节，前 4 字节是 v3 魔数），
    所以下表都是**结构体偏移**（文件绝对偏移 = 这些值 + 4）：
    Duplex=272, Tumble=368, cupsWidth=372, cupsHeight=376, cupsBytesPerLine=392。
    """
    with open(raster, "r+b") as f:
        data = bytearray(f.read())
        pos = 4
        pages = 0
        while pos + CUPS_HDR_LEN <= len(data):
            width = struct.unpack("<I", bytes(data[pos + 372:pos + 376]))[0]
            height = struct.unpack("<I", bytes(data[pos + 376:pos + 380]))[0]
            bpl = struct.unpack("<I", bytes(data[pos + 392:pos + 396]))[0]
            if bpl <= 0 or height <= 0 or width > 200000 or bpl > 400000:
                break
            data[pos + 272:pos + 276] = struct.pack("<I", 1 if duplex else 0)
            data[pos + 368:pos + 372] = struct.pack("<I", 1 if (duplex and tumble) else 0)
            pages += 1
            pos += CUPS_HDR_LEN + bpl * height
        f.seek(0)
        f.write(data)
    return pages


def _safe_title(title):
    title = re.sub(r"[^A-Za-z0-9 _.-]", "", str(title or "document"))
    title = title.strip() or "document"
    return title[:20]


def patch_duplex(data, flag):
    """KPSL 的 C 块第 3 字节为单双面标志。"""
    out = bytearray(data)
    i = 4
    while i + 12 <= len(out):
        tag = bytes(out[i + 3:i + 4])
        ln = struct.unpack("<I", bytes(out[i + 4:i + 8]))[0]
        body = i + 12
        if tag == b"C" and ln == 1:
            out[body + 2] = flag
            break
        i = body + ln * 4
    return bytes(out)


def patch_s_field(data, val=0x10000):
    """KPSL 的 S 块第 3 个双字必须为 0x10000（Windows 驱动恒定如此）。

    官方滤镜在默认 PPD 下写出 0，改与 Windows 一致后 A/B 对照已无差异。
    """
    out = bytearray(data)
    i = 4
    while i + 12 <= len(out):
        tag = bytes(out[i + 3:i + 4])
        ln = struct.unpack("<I", bytes(out[i + 4:i + 8]))[0]
        body = i + 12
        if tag == b"S" and ln >= 3:
            out[body + 8:body + 12] = struct.pack("<I", val)
            break
        i = body + ln * 4
    return bytes(out)


def raster_to_kpsl(raster, kpsl, opts, job_id):
    # 只传 PageSize。传 Resolution= 会让滤镜把 G 块分辨率写成 (1,1)、条带高度
    # 变成 608/640，整页几何随之错位。
    options = ["PageSize=%s" % opts["paper"]]
    if opts.get("duplex") in ("long", "short"):
        options.append("Duplex=" + ("DuplexTumble" if opts["duplex"] == "short"
                                    else "DuplexNoTumble"))
    if not os.path.exists(FILTER):
        raise RuntimeError("filter missing")
    cmd = [FILTER, str(job_id), "print", _safe_title(opts.get("title", "document")),
           str(opts.get("copies", 1)), " ".join(options), raster]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if p.returncode != 0:
        raise RuntimeError("filter exit %d" % p.returncode)
    data = p.stdout
    if not data.startswith(b"LSPK"):
        raise RuntimeError("bad magic")
    data = patch_s_field(data)
    if opts.get("duplex") in ("long", "short"):
        data = patch_duplex(data, 1)
    with open(kpsl, "wb") as f:
        f.write(data)
    return len(data)


SEND_RETRIES = 3
SEND_RETRY_WAIT = 5


# ---------------------------------------------------------------- preview
def pdf_page_count(pdf):
    """用 poppler 的 pdfinfo 数页数（镜像里 poppler-utils 已带，argv 传参对任意
    文件名安全）。

    旧实现是 `gs -c "(路径) (r) file runpdfbegin ..."` 拼 PostScript：路径含
    `)` / `\\` 会截断出错，`-dNOSAFER` 下还是一整条 PS 执行面——虽然当前唯一
    调用方传的是固定名 job.pdf，但这是留给未来调用者的地雷，直接换掉。
    """
    out = _run(["pdfinfo", pdf], timeout=60)
    m = re.search(r"(?m)^Pages:\s+(\d+)", out.decode("utf-8", "replace"))
    if not m:
        raise RuntimeError("pdfinfo: no Pages field")
    return int(m.group(1))


PAGES_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def parse_pages(text):
    """解析「起-止」页码范围：空 → None；格式/次序错 → JobError（中文，走 400）。"""
    text = (text or "").strip()
    if not text:
        return None
    m = PAGES_RE.match(text)
    if not m:
        raise JobError("页码范围格式应如 1-4（起止均为正整数）")
    first, last = int(m.group(1)), int(m.group(2))
    if first < 1 or last < first:
        raise JobError("页码范围不合法：起始页至少为 1，且结束页不小于起始页")
    return first, last


def resolve_pages(pdf, pages, count=None):
    """把页码范围对到实际页数：起始页超界 → JobError；结束页超界 → 夹取。

    旧链路直接把范围塞给 gs：FirstPage 超界时 gs 只打一行英文错误、不产出
    光栅，最后报「打印指令生成失败」，用户完全看不懂。
    """
    if not pages:
        return None
    if count is None:
        count = pdf_page_count(pdf)
    first, last = pages
    if first < 1 or last < first:
        raise JobError("页码范围不合法：起始页至少为 1，且结束页不小于起始页")
    if first > count:
        raise JobError("起始页 %d 超出文档页数（共 %d 页）" % (first, count))
    return first, min(last, count)


def merge_pdfs(pdfs, out):
    if len(pdfs) == 1:
        if os.path.abspath(pdfs[0]) != os.path.abspath(out):
            shutil.copyfile(pdfs[0], out)
        return out
    _run(["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
          "-dCompatibilityLevel=1.4", "-sOutputFile=" + out] + list(pdfs))
    return out


def job_pdf(job, workdir):
    """把作业里的原始文件转成（多文件时先合并成）一个 PDF。"""
    names = job.get("files") or ([job["file"]] if job.get("file") else [])
    if not names:
        raise RuntimeError("no input file")
    if len(names) == 1:
        return to_pdf(os.path.join(workdir, names[0]), workdir)
    pdfs = []
    for i, name in enumerate(names):
        sub = os.path.join(workdir, "src%d" % i)
        os.makedirs(sub, exist_ok=True)
        pdfs.append(to_pdf(os.path.join(workdir, name), sub))
    return merge_pdfs(pdfs, os.path.join(workdir, "merged.pdf"))


def render_preview_page(pdf, png, page, paper, fit):
    """用和正式打印完全相同的几何参数渲染单页 PNG（缩放/裁边一致）。"""
    w, h = page_box(paper)
    cmd = ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pnggray",
           "-r%d" % PREVIEW_DPI,
           "-dDEVICEWIDTHPOINTS=%d" % w, "-dDEVICEHEIGHTPOINTS=%d" % h,
           "-dFIXEDMEDIA",
           "-dFirstPage=%d" % page, "-dLastPage=%d" % page,
           "-sOutputFile=" + png]
    if fit:
        cmd += ["-dPDFFitPage", pdf]
    else:
        mx, my = PAGE_MARGIN
        cmd += ["-c", "<</PageOffset [%d %d]>> setpagedevice" % (-mx, -my), "-f", pdf]
    _run(cmd)


def build_preview(upload_parts, meta):
    """上传文件 → 转 PDF → 渲染前若干页 PNG，返回前端渲染预览所需的元信息。"""
    token = uuid.uuid4().hex[:16]
    workdir = os.path.join(PREVIEW_DIR, token)
    os.makedirs(workdir, exist_ok=True)
    names = []
    for i, (filename, payload) in enumerate(upload_parts):
        safe = os.path.basename(filename) or ("file%d" % i)
        while os.path.exists(os.path.join(workdir, safe)):
            safe = "%d_%s" % (i, safe)
        with open(os.path.join(workdir, safe), "wb") as f:
            f.write(payload)
        names.append(safe)
    total = sum(len(payload) for _, payload in upload_parts)
    pdf = job_pdf({"files": names}, workdir)
    target = os.path.join(workdir, "job.pdf")
    if os.path.abspath(pdf) != os.path.abspath(target):
        shutil.copyfile(pdf, target)
    for name in names:                       # 原文件留着没用，省空间
        try:
            os.unlink(os.path.join(workdir, name))
        except Exception:
            pass
    try:
        count = pdf_page_count(target)
    except Exception:
        raise JobError("文件已损坏或不是有效的 PDF") from None
    if count <= 0:
        raise JobError("文件里没有可打印的页面")
    paper = meta.get("paper", "A4")
    fit = bool(meta.get("fit"))
    rng = resolve_pages(target, meta.get("pages"), count)
    first, last = rng if rng else (1, count)
    span = last - first + 1
    # meta.json 是 token 打印的几何权威（WYSIWYG：预览什么样就打什么样）
    with open(os.path.join(workdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "、".join(names)[:80], "size": total,
                   "created": time.time(), "paper": paper, "fit": fit,
                   "pages": list(rng) if rng else None}, f, ensure_ascii=False)
    rendered = min(span, PREVIEW_MAX_PAGES)
    images = []
    for page in range(first, first + rendered):
        png = os.path.join(workdir, "p%d.png" % page)
        render_preview_page(target, png, page, paper, fit)
        pw = ph = 0
        with open(png, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            pw, ph = struct.unpack(">II", head[16:24])
        images.append({"page": page, "w": pw, "h": ph,
                       "url": "/api/preview/%s/p%d.png" % (token, page)})
    pw_pt, ph_pt = PAPERS.get(paper, PAPERS["A4"])
    cw_pt, ch_pt = page_box(paper)
    return {"ok": True, "token": token, "pages": count, "rendered": rendered,
            "truncated": rendered < span, "range": [first, last],
            "paper": paper, "fit": fit,
            "paper_pt": [pw_pt, ph_pt], "content_pt": [cw_pt, ch_pt],
            "margin_pt": list(PAGE_MARGIN), "images": images}


def purge_previews():
    now = time.time()
    try:
        names = os.listdir(PREVIEW_DIR)
    except Exception:
        return
    for name in names:
        fp = os.path.join(PREVIEW_DIR, name)
        try:
            if os.path.isdir(fp) and now - os.path.getmtime(fp) > PREVIEW_KEEP_HOURS * 3600:
                shutil.rmtree(fp, ignore_errors=True)
        except Exception:
            pass


def send_to_printer(data, timeout=120):
    """发到 9100。打印机忙时会短暂拒连，按 3 次 / 间隔 5s 重试。"""
    last = None
    for attempt in range(SEND_RETRIES):
        try:
            return _send_once(data, timeout)
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            last = exc
            if attempt + 1 < SEND_RETRIES:
                time.sleep(SEND_RETRY_WAIT)
    raise RuntimeError("打印机未响应或未就绪：%s" % last)


def _send_once(data, timeout=120):
    s = socket.create_connection((PRINTER_IP, PRINTER_PORT), 10)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(timeout)
    try:
        view = memoryview(data)
        sent = 0
        while sent < len(data):
            n = s.send(view[sent:sent + 65536])
            if n <= 0:
                raise RuntimeError("send aborted")
            sent += n
        s.shutdown(socket.SHUT_WR)
        try:
            while s.recv(65536):
                pass
        except socket.timeout:
            pass
    finally:
        s.close()
    return sent


# ---------------------------------------------------------------- jobs
def _persist():
    try:
        tmp = os.path.join(DATA_DIR, "jobs.json.tmp")
        with open(tmp, "w") as f:
            json.dump(list(_jobs.values()), f, ensure_ascii=False)
        os.replace(tmp, os.path.join(DATA_DIR, "jobs.json"))
    except Exception:
        pass


def _load():
    path = os.path.join(DATA_DIR, "jobs.json")
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for job in json.load(f):
                if job.get("status") in ("pending", "printing"):
                    job["status"] = "error"
                    job["message"] = "服务重启，任务已中断"
                _jobs[job["id"]] = job
    except Exception:
        pass


def _cleanup():
    with _lock:                       # _jobs 有并发读写（worker/HTTP 线程）
        now = time.time()
        for job in list(_jobs.values()):
            if job.get("status") in ("done", "error") and now - job.get("created_ts", now) > KEEP_DAYS * 86400:
                _jobs.pop(job["id"], None)
                shutil.rmtree(os.path.join(JOBS_DIR, job["id"]), ignore_errors=True)
        _persist()
    purge_previews()                  # 纯文件系统操作，放锁外


def _cleanup_loop():
    """过期作业/预览定期清理：原来只在启动时跑一次，服务长跑会无限堆积。"""
    while True:
        time.sleep(30 * 60)
        try:
            _cleanup()
        except Exception:
            traceback.print_exc()


def _update(job_id, **kw):
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job.update(kw)
            _persist()


def _worker():
    while True:
        job_id = None
        with _lock:
            if _queue:
                job_id = _queue.pop(0)
        if job_id is None:
            time.sleep(0.5)
            continue
        job = _jobs.get(job_id)
        if not job:
            continue
        _update(job_id, status="printing", message="正在生成打印数据…")
        workdir = os.path.join(JOBS_DIR, job_id)
        try:
            try:
                pdf = job_pdf(job, workdir)
            except Exception:
                traceback.print_exc()
                raise JobError("文件转换失败，请确认文件格式是否受支持")

            opts = {
                "paper": job["paper"],
                "dpi": job["dpi"],
                "copies": job["copies"],
                "duplex": job["duplex"],
                "fit": job["fit"],
                "pages": resolve_pages(pdf, job.get("pages")),
                "title": job["name"],
            }
            raster = os.path.join(workdir, "page.raster")
            kpsl = os.path.join(workdir, "job.kpsl")

            _update(job_id, message="正在渲染页面…")
            try:
                pdf_to_raster(pdf, raster, opts)
            except Exception:
                traceback.print_exc()
                raise JobError("页面渲染失败，请稍后重试")

            try:
                patch_raster_header(raster, opts.get("duplex") in ("long", "short"),
                                    opts.get("duplex") == "short")
            except Exception:
                traceback.print_exc()

            _update(job_id, message="正在生成打印指令…")
            try:
                size = raster_to_kpsl(raster, kpsl, opts, job_id)
            except Exception:
                traceback.print_exc()
                raise JobError("打印指令生成失败，请稍后重试")

            _update(job_id, kpsl_size=size, message="正在发送到打印机…")
            try:
                with open(kpsl, "rb") as f:
                    send_to_printer(f.read())
            except Exception:
                traceback.print_exc()
                raise JobError("发送失败：打印机未响应或未就绪，请检查打印机状态")

            _update(job_id, status="done", message="已发送到打印机",
                    finished_ts=time.time())
            for name in ("page.raster", "job.kpsl", "conv.pdf"):
                p = os.path.join(workdir, name)
                if os.path.exists(p):
                    try:
                        os.unlink(p)
                    except Exception:
                        pass
        except JobError as exc:
            _update(job_id, status="error", message=str(exc),
                    finished_ts=time.time())
        except Exception:
            traceback.print_exc()
            _update(job_id, status="error", message="处理失败，请稍后重试",
                    finished_ts=time.time())


def _push_job(job):
    with _lock:
        _jobs[job["id"]] = job
        _queue.append(job["id"])
        _persist()
    return job


def enqueue(job, upload_parts):
    workdir = os.path.join(JOBS_DIR, job["id"])
    os.makedirs(workdir, exist_ok=True)
    names = []
    for i, (filename, payload) in enumerate(upload_parts):
        safe = os.path.basename(filename) or ("upload%d.bin" % i)
        while os.path.exists(os.path.join(workdir, safe)):
            safe = "%d_%s" % (i, safe)
        with open(os.path.join(workdir, safe), "wb") as f:
            f.write(payload)
        names.append(safe)
    job["files"] = names
    if names:
        job["file"] = names[0]
    return _push_job(job)


def make_test_pdf():
    workdir = os.path.join(JOBS_DIR, "testpage")
    os.makedirs(workdir, exist_ok=True)
    ps = os.path.join(workdir, "test.ps")
    with open(ps, "w") as f:
        f.write("""%!PS
/Helvetica-Bold findfont 30 scalefont setfont
72 740 moveto (TASKalfa 2011 PRINT TEST) show
/Helvetica findfont 14 scalefont setfont
72 700 moveto (via fedora + podman + KPSL filter) show
0 setlinewidth 72 660 moveto 523 660 lineto stroke
/Times-Roman findfont 16 scalefont setfont
72 620 moveto (Page 1 - front side) show
showpage
%!PS
/Helvetica-Bold findfont 30 scalefont setfont
72 740 moveto (PAGE 2 - BACK SIDE) show
/Helvetica findfont 14 scalefont setfont
72 700 moveto (Duplex test page) show
showpage
""")
    out = os.path.join(workdir, "test.pdf")
    _run(["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
          "-sOutputFile=" + out, ps])
    return out


# ---------------------------------------------------------------- HTTP
STATUS_TEXT = {
    "pending": "排队中",
    "printing": "处理中",
    "done": "已发送",
    "error": "失败",
}

DUPLEX_TEXT = {"off": "单面", "long": "双面·长边", "short": "双面·短边"}


def sanitize_msg(status, message):
    """保证页面上的“说明”只出现简体中文。"""
    text = str(message or "").strip()
    if not text:
        return {"done": "已发送到打印机"}.get(status, "处理中…")
    if re.search(r"[A-Za-z]{3,}", text):
        return {
            "done": "已发送到打印机",
            "error": "发送失败：打印机未响应或未就绪",
            "pending": "排队中",
        }.get(status, "处理中…")
    return text


def job_view(job):
    out = dict(job)
    out["status_text"] = STATUS_TEXT.get(job.get("status"), "未知")
    out["message"] = sanitize_msg(job.get("status"), job.get("message"))
    out["duplex_text"] = DUPLEX_TEXT.get(job.get("duplex"), "单面")
    out["created_text"] = fmt_ts(job.get("created_ts"))
    out["finished_text"] = fmt_ts(job.get("finished_ts"))
    out["print_text"] = out["finished_text"] or out["created_text"]
    out["paper_text"] = "%s / %d dpi" % (job.get("paper", "A4"), job.get("dpi", 600))
    out["client_ip"] = job.get("client_ip") or "-"
    size = job.get("size") or 0
    out["size_text"] = ("%.1f KB" % (size / 1024.0)) if size < 1024 * 1024 else ("%.1f MB" % (size / 1048576.0))
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "taskalfa-print/3.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _client_ip(self):
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            ui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
            with open(ui, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/health":
            self._send(200, json.dumps({"ok": True}))
        elif path.startswith("/api/preview/"):
            self._serve_preview(path)
        elif path == "/api/state":
            jobs = sorted(_jobs.values(), key=lambda j: j.get("created_ts", 0),
                          reverse=True)[:30]
            self._send(200, json.dumps(
                {"printer": printer_status(), "jobs": [job_view(j) for j in jobs]},
                ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "未找到该接口"}, ensure_ascii=False))

    def do_POST(self):
        path = urlparse(self.path).path
        if not _post_slots.acquire(blocking=False):
            # 不排队：读体之前先拒绝，避免内存继续涨
            self._send(503, json.dumps({"error": "服务繁忙，请稍后重试"},
                                       ensure_ascii=False))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD:
                self._send(413, json.dumps({"error": "文件超过 300 MB 上限"},
                                           ensure_ascii=False))
                return
            raw = self.rfile.read(length)
            if path == "/api/print":
                self._handle_print(raw)
            elif path == "/api/preview":
                self._handle_preview(raw)
            elif path == "/api/test":
                self._handle_test(raw)
            else:
                self._send(404, json.dumps({"error": "未找到该接口"},
                                           ensure_ascii=False))
        except Exception:
            traceback.print_exc()
            self._send(500, json.dumps({"error": "服务处理失败，请稍后重试"},
                                       ensure_ascii=False))
        finally:
            _post_slots.release()

    def _parse_multipart(self, raw):
        ctype = self.headers.get("Content-Type", "")
        msg = BytesParser(policy=policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
        fields, files = {}, []
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files.append((filename, payload))
            elif name:
                fields[name] = payload.decode("utf-8", "replace").strip()
        return fields, files

    def _serve_preview(self, path):
        parts = path.strip("/").split("/")
        if len(parts) != 4 or not re.match(r"^[0-9a-f]{8,32}$", parts[2]):
            self._send(404, json.dumps({"error": "预览不存在"}, ensure_ascii=False))
            return
        name = os.path.basename(parts[3])
        fp = os.path.join(PREVIEW_DIR, parts[2], name)
        if not name.endswith(".png") or not os.path.isfile(fp):
            self._send(404, json.dumps({"error": "预览不存在"}, ensure_ascii=False))
            return
        with open(fp, "rb") as f:
            body = f.read()
        self._send(200, body, "image/png")

    def _handle_preview(self, raw):
        fields, files = self._parse_multipart(raw)
        if not files:
            self._send(400, json.dumps({"error": "没有收到文件"}, ensure_ascii=False))
            return
        try:
            meta = {
                "paper": fields.get("paper") if fields.get("paper") in PAPERS else "A4",
                "fit": fields.get("fit") in ("1", "true", "on"),
                "pages": parse_pages(fields.get("pages")),
            }
            info = build_preview(files, meta)
        except JobError as exc:
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return
        except Exception:
            traceback.print_exc()
            self._send(500, json.dumps({"error": "预览生成失败，请稍后重试"},
                                       ensure_ascii=False))
            return
        self._send(200, json.dumps(info, ensure_ascii=False))

    def _handle_print(self, raw):
        fields, files = self._parse_multipart(raw)
        token = (fields.get("token") or "").strip()
        if not files and not token:
            self._send(400, json.dumps({"error": "没有收到文件"}, ensure_ascii=False))
            return
        pages = None
        try:
            paper = fields.get("paper") if fields.get("paper") in PAPERS else "A4"
            fit = fields.get("fit") in ("1", "true", "on")
            pages = parse_pages(fields.get("pages"))
        except JobError as exc:
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return
        name = os.path.basename(files[0][0])[:80] if files else ""
        size = sum(len(p) for _, p in files)
        if token:
            if not re.match(r"^[0-9a-f]{8,32}$", token):
                self._send(400, json.dumps({"error": "预览标识不合法"}, ensure_ascii=False))
                return
            pdir = os.path.join(PREVIEW_DIR, token)
            if not os.path.isfile(os.path.join(pdir, "job.pdf")):
                self._send(400, json.dumps({"error": "预览已过期，请重新选择文件"},
                                           ensure_ascii=False))
                return
            try:
                with open(os.path.join(pdir, "meta.json"), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                name = meta.get("name") or name
                size = meta.get("size") or size
                # 几何参数以预览时的 meta 为权威：预览渲染成什么样，就打什么样
                if meta.get("paper") in PAPERS:
                    paper = meta["paper"]
                if "fit" in meta:
                    fit = bool(meta["fit"])
                if "pages" in meta:
                    mp = meta.get("pages")
                    pages = (int(mp[0]), int(mp[1])) if (
                        isinstance(mp, list) and len(mp) == 2) else None
            except Exception:
                pass
            name = name or "打印预览.pdf"
        job = {
            "id": uuid.uuid4().hex[:8],
            "name": name or "未命名文件",
            "size": size,
            "copies": max(1, min(99, int(fields.get("copies") or 1))),
            "duplex": fields.get("duplex") if fields.get("duplex") in ("long", "short") else "off",
            "paper": paper,
            "dpi": RASTER_DPI,
            "fit": fit,
            "pages": pages,
            "status": "pending",
            "message": "排队中",
            "client_ip": self._client_ip(),
            "created_ts": time.time(),
        }
        if token:
            src = os.path.join(PREVIEW_DIR, token, "job.pdf")
            workdir = os.path.join(JOBS_DIR, job["id"])
            os.makedirs(workdir, exist_ok=True)
            shutil.copyfile(src, os.path.join(workdir, "job.pdf"))
            job["files"] = ["job.pdf"]
            job["file"] = "job.pdf"
            _push_job(job)
        else:
            enqueue(job, files)
        self._send(200, json.dumps({"ok": True, "job": job_view(job)}, ensure_ascii=False))

    def _handle_test(self, raw):
        pdf = make_test_pdf()
        job = {
            "id": "test-" + uuid.uuid4().hex[:6],
            "name": "测试页.pdf",
            "size": os.path.getsize(pdf),
            "copies": 1,
            "duplex": "long",
            "paper": "A4",
            "dpi": 600,
            "fit": False,
            "pages": None,
            "status": "pending",
            "message": "排队中",
            "client_ip": self._client_ip(),
            "created_ts": time.time(),
        }
        with open(pdf, "rb") as f:
            payload = f.read()
        enqueue(job, [("test.pdf", payload)])
        self._send(200, json.dumps({"ok": True, "job": job_view(job)}, ensure_ascii=False))


def main():
    _load()
    _cleanup()
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_status_loop, daemon=True).start()
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    print("taskalfa-print 启动: http://0.0.0.0:%d  打印机 %s:%d"
          % (WEB_PORT, PRINTER_IP, PRINTER_PORT))
    ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
