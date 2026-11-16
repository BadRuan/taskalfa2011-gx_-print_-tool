import struct
import sys

ESC = b"\x1b$0"
MARK = b"@@@@"


def parse(path, maxchunks=40):
    data = open(path, "rb").read()
    print("file", path, "size", len(data), "magic", data[:4])
    i = 4
    n = 0
    while i < len(data) and n < maxchunks:
        if data[i:i + 3] != ESC:
            j = data.find(ESC, i + 1)
            print("  gap %d..%d (%d bytes): %s" % (i, j, (j - i) if j > 0 else -1, data[i:i + 24].hex()))
            if j < 0:
                break
            i = j
            continue
        tag = data[i + 3:i + 4]
        ln = struct.unpack("<I", data[i + 4:i + 8])[0]
        mark = data[i + 8:i + 12]
        body = data[i + 12:i + 12 + ln]
        print("  [%03d] off=%d tag=%r len=%d mark=%r body[%d]=%s" % (
            n, i, tag, ln, mark, len(body), body[:48].hex()))
        if tag in (b"J", b"D", b"Z", b"E", b"X"):
            try:
                print("        ascii:", body[:64])
            except Exception:
                pass
        i += 12 + ln
        n += 1
    print("stopped at", i, "of", len(data))


if __name__ == "__main__":
    parse(sys.argv[1])
