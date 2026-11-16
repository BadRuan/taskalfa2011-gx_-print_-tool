#!/usr/bin/env python3
"""把 KPSL 里 S 块的第 3 个 dword 设为指定值（Windows 驱动是 0x10000）。"""
import struct
import sys

path = sys.argv[1]
val = int(sys.argv[2], 0)
data = bytearray(open(path, "rb").read())
assert data[:4] == b"LSPK"
i = 4
done = 0
while i + 12 <= len(data):
    tag = data[i + 3:i + 4]
    ln = struct.unpack("<I", bytes(data[i + 4:i + 8]))[0]
    body = i + 12
    if tag == b"S" and ln == 6:
        old = struct.unpack("<I", bytes(data[body + 8:body + 12]))[0]
        data[body + 8:body + 12] = struct.pack("<I", val)
        done += 1
        print("S@%d dword2 0x%X -> 0x%X" % (i, old, val))
    i = body + ln * 4
assert done, "no S chunk found"
open(path, "wb").write(bytes(data))
