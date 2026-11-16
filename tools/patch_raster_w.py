#!/usr/bin/env python3
"""把 CUPS raster 头里的 cupsWidth 改成指定值（用于对齐 Windows 驱动的 4760px）。

页头布局（gs 10.x，小端 "3SaR"）：376=width, 380=height, 396=bytes_per_line。
"""
import struct
import sys

path, width = sys.argv[1], int(sys.argv[2])
with open(path, "r+b") as f:
    data = f.read(1796)
    assert data[:4] in (b"RaS3", b"3SaR"), data[:4]
    old = struct.unpack("<I", data[376:380])[0]
    bpl = struct.unpack("<I", data[396:400])[0]
    assert (width + 7) // 8 == bpl, "bpl mismatch %d -> %d" % (bpl, width)
    f.seek(376)
    f.write(struct.pack("<I", width))
print("cupsWidth %d -> %d (bpl=%d)" % (old, width, bpl))
