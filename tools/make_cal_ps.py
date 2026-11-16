#!/usr/bin/env python3
"""生成 A4 可打印区(571x822pt) 标定页 PostScript。

用途：判断打印机是否把 600dpi 光栅按 1200dpi 横向渲染（症状=横向半幅）。
内容：外框、上/左毫米刻度尺、100mm 基准条、大号标记。
"""
import sys

PT_PER_MM = 72.0 / 25.4
W_PT, H_PT = 571.0, 822.0
W_MM, H_MM = W_PT / PT_PER_MM, H_PT / PT_PER_MM   # 201.4 x 290.0

TAG = sys.argv[1] if len(sys.argv) > 1 else "TEST-1"
SUB = sys.argv[2] if len(sys.argv) > 2 else "S=0x10000"


def mm(v):
    return "%.2f" % (v * PT_PER_MM)


out = []
w = out.append

w("%!PS-Adobe-3.0")
w("<< /PageSize [%.2f %.2f] >> setpagedevice" % (W_PT, H_PT))
w("/Helvetica-Bold findfont 10 scalefont setfont")
w("/TXT { /s exch def /fs exch def currentfont fs scalefont setfont "
  "moveto s show } bind def")
w("/LINE { newpath moveto lineto stroke } bind def")
w("0.7 setlinewidth")


def txt(x, y, size, s):
    w("%s %s %d (%s) TXT" % (mm(x), mm(y), size, s))


def line(x1, y1, x2, y2, lw=None):
    if lw:
        w("%s setlinewidth" % lw)
    w("%s %s %s %s LINE" % (mm(x1), mm(y1), mm(x2), mm(y2)))


# 外框（内缩 5mm）：191 x 280 mm
line(5, 5, W_MM - 5, 5)
line(W_MM - 5, 5, W_MM - 5, H_MM - 5)
line(W_MM - 5, H_MM - 5, 5, H_MM - 5)
line(5, H_MM - 5, 5, 5)

# 上方刻度尺
base_y = H_MM - 8
line(0, base_y, W_MM, base_y)
for i in range(0, int(W_MM), 10):
    ln = 4 if i % 50 == 0 else 2
    line(i, base_y, i, base_y + ln)
    if i % 50 == 0 and i > 0:
        txt(i - 1.5, base_y + 5, 9, str(i))

# 左侧刻度尺
base_x = 4
line(base_x, 0, base_x, H_MM)
for j in range(0, int(H_MM), 10):
    ln = 4 if j % 50 == 0 else 2
    line(base_x, j, base_x + ln, j)
    if j % 50 == 0 and j > 0:
        txt(base_x + 5, j - 1.5, 9, str(j))

# 100mm 基准条
line(50, 120, 150, 120, 3)
line(50, 115, 50, 125, 1.2)
line(150, 115, 150, 125, 1.2)
txt(90, 108, 10, "100 mm")

# 中心十字
line(85, 200, 115, 200, 1)
line(100, 185, 100, 215, 1)

# 大号标记
txt(20, 235, 40, TAG)
txt(20, 228, 11, SUB)
txt(20, 220, 11, "page 571x822pt = 201.4x290.0mm @600dpi")
txt(20, 212, 11, "frame 191x280mm, ticks every 10mm")

w("showpage")
print("\n".join(out))
