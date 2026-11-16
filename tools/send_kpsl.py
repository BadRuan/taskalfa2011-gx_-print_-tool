#!/usr/bin/env python3
"""把 KPSL 文件直接送到打印机 9100 端口。"""
import os
import socket
import sys
import time

path = sys.argv[1]
ip = os.environ.get("PRINTER_IP", "192.168.0.199")
data = open(path, "rb").read()
assert data[:4] == b"LSPK", "not KPSL"
s = socket.create_connection((ip, 9100), 10)
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
s.settimeout(120)
sent = 0
t0 = time.time()
while sent < len(data):
    n = s.send(data[sent:sent + 65536])
    if n <= 0:
        raise SystemExit("send aborted at %d" % sent)
    sent += n
s.shutdown(socket.SHUT_WR)
try:
    while s.recv(65536):
        pass
except socket.timeout:
    pass
s.close()
print("sent %d bytes in %.1fs" % (sent, time.time() - t0))
