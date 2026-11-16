#!/usr/bin/env python3
"""诊断用透传代理：接收 TCP 数据，原样转发给打印机 9100，并把收到的字节存盘。
用途：把 Windows 上"能正常打印"的数据流抓下来，分析 TASKalfa 2011 的 GDI 私有格式。
监听端口默认 8080（fedora 防火墙已放行 8080；9100 未放行，需 root）。
"""
import os
import socket
import sys
import threading
import time

LISTEN_HOST = os.environ.get("CAP_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("CAP_PORT", "8080"))
DEST = (os.environ.get("CAP_DEST", "192.168.0.199"), int(os.environ.get("CAP_DEST_PORT", "9100")))
OUTDIR = os.environ.get("CAP_OUT", os.path.expanduser("~/captures"))

_counter = 0
_lock = threading.Lock()


def handle(client, addr):
    global _counter
    with _lock:
        _counter += 1
        idx = _counter
    name = "cap-%s-%02d.bin" % (time.strftime("%Y%m%d-%H%M%S"), idx)
    path = os.path.join(OUTDIR, name)
    print("[%s] conn #%d from %s:%s -> %s" % (time.strftime("%H:%M:%S"), idx, addr[0], addr[1], path), flush=True)
    try:
        up = socket.create_connection(DEST, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print("  cannot reach printer: %s" % exc, flush=True)
        client.close()
        return

    fh = open(path, "wb")
    total = [0]

    def pump(src, dst, save):
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                if save:
                    fh.write(chunk)
                    fh.flush()
                    total[0] += len(chunk)
                dst.sendall(chunk)
        except Exception:  # noqa: BLE001
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:  # noqa: BLE001
            pass

    back = threading.Thread(target=pump, args=(up, client, False), daemon=True)
    back.start()
    pump(client, up, True)
    back.join(timeout=5)
    try:
        fh.close()
        client.close()
        up.close()
    except Exception:  # noqa: BLE001
        pass
    print("  captured %d bytes -> %s" % (total[0], path), flush=True)


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(8)
    print("capture proxy listening on %s:%d -> %s:%d, out=%s" % (LISTEN_HOST, LISTEN_PORT, DEST[0], DEST[1], OUTDIR), flush=True)
    while True:
        client, addr = srv.accept()
        threading.Thread(target=handle, args=(client, addr), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
