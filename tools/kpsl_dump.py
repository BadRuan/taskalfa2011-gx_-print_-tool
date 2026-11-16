#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dump KPSL chunk headers in detail (all dwords of S/G/P/B)."""
import struct
import sys

ESC = b"\x1b$0"


def chunks(data):
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


def dwords(body):
    return [struct.unpack("<I", body[k:k + 4])[0] for k in range(0, len(body) - 3, 4)]


def dump(path, only=None):
    data = open(path, "rb").read()
    print("== %s  size=%d  magic=%r" % (path, len(data), data[:4]))
    page = 0
    seen_p = 0
    for off, tag, ln, body in chunks(data):
        if tag == b"P":
            seen_p += 1
            page = seen_p
        t = tag.decode()
        extra = ""
        if t in "SGPB" or t == "C":
            extra = " dw=" + ",".join(str(x) for x in dwords(body))
        print("  p%d off=%-6d %s len=%-3d%s" % (page, off, t, ln, extra))


if __name__ == "__main__":
    for p in sys.argv[1:]:
        dump(p)
