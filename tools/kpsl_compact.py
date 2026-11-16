#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compact KPSL dump: chunk list with the 13 header dwords of B strips only."""
import struct
import sys

ESC = b"\x1b$0"


def walk(data):
    i = 4
    while i + 12 <= len(data):
        if data[i:i + 3] != ESC:
            j = data.find(ESC, i + 1)
            if j < 0:
                return
            i = j
            continue
        tag = data[i + 3:i + 4]
        ln = struct.unpack("<I", data[i + 4:i + 8])[0]
        body_off = i + 12
        body = data[body_off:body_off + ln * 4]
        yield i, tag, ln, body
        i = body_off + ln * 4


def dws(body, n):
    return [struct.unpack("<I", body[k * 4:k * 4 + 4])[0] for k in range(min(n, len(body) // 4))]


def dump(path, bgroups=13):
    data = open(path, "rb").read()
    print("== %s  size=%d" % (path, len(data)))
    nb = 0
    last_off = None
    for off, tag, ln, body in walk(data):
        t = tag.decode("latin1")
        if t == "B":
            nb += 1
            d = dws(body, bgroups)
            print("  B#%-3d off=%-6d len=%-4d %s" % (nb, off, ln, ",".join(str(x) for x in d)))
        else:
            d = dws(body, min(ln, 8))
            print("  %-2s off=%-6d len=%-4d dw=%s" % (t, off, ln, ",".join(str(x) for x in d)))
    print("  strips:", nb)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        dump(p)
