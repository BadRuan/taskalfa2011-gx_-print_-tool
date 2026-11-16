#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 A4 比例尺标定页（PostScript），用于验证 100% 缩放与横向是否满幅。

用法: python3 tools/make_scale_ps.py <TAG> <SUB>
页面: A4 595x842pt (210x297mm)
元素: 外框内缩 10mm、水平 100mm 基准条、垂直 100mm 基准条、10mm 刻度尺
"""
import sys

MM = 72.0 / 25.4
TAG = sys.argv[1] if len(sys.argv) > 1 else "TEST-8"
SUB = sys.argv[2] if len(sys.argv) > 2 else "100% scale"

out = []
w = out.append
w("%!PS-Adobe-3.0")
w("<< /PageSize [595 842] >> setpagedevice")
w("%.3f %.3f scale" % (MM, MM))  # 之后所有坐标用毫米


def txt(x, y, fs, s):
    esc = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    w("/Helvetica findfont %.2f scalefont setfont" % fs)
    w("%.2f %.2f moveto (%s) show" % (x, y, esc))


def line(x1, y1, x2, y2, lw=0.5):
    w("%.2f setlinewidth %.2f %.2f moveto %.2f %.2f lineto stroke" % (lw, x1, y1, x2, y2))


def rect(x, y, ww, hh, lw=0.5):
    w("%.2f setlinewidth %.2f %.2f moveto %.2f %.2f lineto %.2f %.2f lineto %.2f %.2f lineto closepath stroke"
      % (lw, x, y, x + ww, y, x + ww, y + hh, x, y + hh))


# 外框：内缩 10mm -> 190x277mm
rect(10, 10, 190, 277, 0.6)

# 水平 100mm 基准条（从 x=55 到 x=155，y=120），两端竖标记
line(55, 118, 55, 122, 0.8)
line(155, 118, 155, 122, 0.8)
line(55, 120, 155, 120, 2.0)
for xt in range(55, 156, 10):
    line(xt, 116, xt, 118, 0.4)
txt(80, 122.5, 6, "100 mm horizontal")

# 垂直 100mm 基准条（x=30，从 y=140 到 y=240）
line(28, 140, 32, 140, 0.8)
line(28, 240, 32, 240, 0.8)
line(30, 140, 30, 240, 2.0)
for yt in range(140, 241, 10):
    line(32, yt, 34, yt, 0.4)
txt(36, 186, 6, "100 mm vertical")

# 顶部刻度尺：每 10mm 一个刻度，每 50mm 标数字
line(10, 265, 200, 265, 0.3)
for i in range(0, 20):
    xt = 10 + i * 10
    if xt > 200:
        break
    hh = 5 if i % 5 == 0 else 2.5
    line(xt, 265, xt, 265 + hh, 0.4)
    if i % 5 == 0 and i:
        txt(xt - 3, 271, 5, str(i * 10))

# 中间大号标签
txt(30, 200, 20, TAG)
txt(30, 192, 8, SUB)
txt(30, 60, 7, "frame: 10 mm inset, 190x277 mm")
txt(30, 52, 7, "bars: 100 mm; measure with ruler")

w("showpage")
print("\n".join(out))
