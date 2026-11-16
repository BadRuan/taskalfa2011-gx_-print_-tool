#!/usr/bin/env python3
"""把 KPSL 里所有 B 块的第 13 个 dword（索引 12）改成指定值。"""
import struct
import sys

path = sys.argv[1]
val = int(sys.argv[2], 0)
data = bytearray(open(path, "rb").read())
assert data[:4] == b"LSPK"
i = 4
n = 0
while i + 12 <= len(data):
    tag = data[i + 3:i + 4]
    ln = struct.unpack("<I", bytes(data[i + 4:i + 8]))[0]
    body = i + 12
    if tag == b"B" and ln > 12:
        old = struct.unpack("<I", bytes(data[body + 48:body + 52]))[0]
        data[body + 48:body + 52] = struct.pack("<I", val)
        if old != val:
            n += 1
    i = body + ln * 4
print("patched %d B strips to dw12=0x%X" % (n, val))
open(path, "wb").write(bytes(data))
