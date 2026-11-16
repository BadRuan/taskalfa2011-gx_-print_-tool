#!/usr/bin/env python3
"""通用 KPSL 字段补丁：patch_chunk.py <file> <tag> <dword_index> <value>

tag 为单字符 chunk 标签（J/D/C/S/G/P/B/F/E/T），dword_index 从 0 开始。
"""
import struct
import sys

path, tag, idx, val = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4], 0)
data = bytearray(open(path, "rb").read())
assert data[:4] == b"LSPK"
i = 4
hit = 0
while i + 12 <= len(data):
    t = chr(data[i + 3])
    ln = struct.unpack("<I", bytes(data[i + 4:i + 8]))[0]
    body = i + 12
    if t == tag and ln > idx:
        off = body + idx * 4
        old = struct.unpack("<I", bytes(data[off:off + 4]))[0]
        data[off:off + 4] = struct.pack("<I", val)
        hit += 1
        if hit == 1:
            print("%s[%d] 0x%X -> 0x%X" % (tag, idx, old, val))
    i = body + ln * 4
assert hit, "chunk %s not found" % tag
print("patched %d %s chunk(s)" % (hit, tag))
open(path, "wb").write(bytes(data))
