# -*- coding: utf-8 -*-
"""由现有的 icon.ico 生成尺寸齐全的 icon.ico。

**只补齐尺寸，不改画面。** 原有的 32x32 与 256x256 两张图会**逐字节原样保留**，
其余尺寸从 256x256 源图做面积平均降采样得到（这是缩放里最不失真的做法）。

为什么必须补齐尺寸：托盘、通知区域、标题栏、任务栏实际请求的是 16~24px，
资源管理器「中图标」用 48px，而原来的 ico 只有 32 和 256 —— 于是所有小尺寸
都是系统临时缩放的，放大到屏幕上就发虚。

生成的文件结构（每个尺寸都带 32 位 alpha 通道）：

    16 / 20 / 24 / 32 / 40 / 48 / 64   -> BMP 格式（兼容性最好）
    128 / 256                          -> PNG 格式（体积小，Vista 以上均支持）

用法：
    python build_icon.py            # 重新生成 icon.ico
    python build_icon.py --check    # 只检查现有 icon.ico 的尺寸是否齐全
"""

import os
import struct
import sys
import zlib

# 需要写进 ico 的尺寸。20/40 是 125% / 250% 缩放的托盘尺寸，别省。
TARGET_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# >= 该尺寸的条目用 PNG 压缩，其余用 BMP
PNG_FROM = 128

HERE = os.path.dirname(os.path.abspath(__file__))
ICO_PATH = os.path.join(HERE, "icon.ico")
BACKUP_PATH = os.path.join(HERE, "icon_original.ico")


# ---------------------------------------------------------------- 读取 ico

def parse_ico(raw):
    """拆开 ico，返回 {尺寸: (是否PNG, 原始字节)} 与条目顺序。"""
    if len(raw) < 6:
        raise ValueError("文件过小，不是合法的 ico")
    reserved, image_type, count = struct.unpack("<HHH", raw[:6])
    if reserved != 0 or image_type != 1:
        raise ValueError("不是 ico 文件（reserved=%d type=%d）" % (reserved, image_type))

    entries = {}
    for i in range(count):
        off = 6 + i * 16
        w, h, colors, res, planes, bpp, size, data_off = struct.unpack(
            "<BBBBHHII", raw[off:off + 16])
        width = w or 256
        blob = raw[data_off:data_off + size]
        entries[width] = (blob[:8] == b"\x89PNG\r\n\x1a\n", blob)
    return entries


def decode_source_png(blob):
    """解码 8bit RGBA 非交错 PNG，返回 (宽, 高, 逐行 RGBA)。"""
    w, h, bit_depth, color_type = struct.unpack(">IIBB", blob[16:26])
    if bit_depth != 8 or color_type != 6:
        raise ValueError("只支持 8bit RGBA 的 PNG（实际 depth=%d type=%d）"
                         % (bit_depth, color_type))

    pos, idat = 8, b""
    while pos < len(blob) - 8:
        length = struct.unpack(">I", blob[pos:pos + 4])[0]
        chunk_type = blob[pos + 4:pos + 8]
        if chunk_type == b"IDAT":
            idat += blob[pos + 8:pos + 8 + length]
        pos += 12 + length

    data = zlib.decompress(idat)
    bpp, stride = 4, w * 4
    prev = bytearray(stride)
    rows = []
    cursor = 0
    for _ in range(h):
        filter_type = data[cursor]
        cursor += 1
        line = bytearray(data[cursor:cursor + stride])
        cursor += stride
        if filter_type == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif filter_type == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif filter_type == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 255
        elif filter_type == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 255
        elif filter_type != 0:
            raise ValueError("不支持的 PNG 行过滤方式: %d" % filter_type)
        rows.append(bytes(line))
        prev = line
    return w, h, rows


# ---------------------------------------------------------------- 缩放与编码

def area_downscale(w, h, rows, target):
    """面积平均降采样：每个目标像素取其覆盖区域内源像素的 alpha 加权平均。

    比最近邻/双线性更适合图标缩小：不会有漏掉的像素，边缘也不会出现偏色
    （透明的黑边像素会因为权重为 0 被排除）。
    """
    out = []
    for gy in range(target):
        y0 = gy * h // target
        y1 = max(y0 + 1, (gy + 1) * h // target)
        line = bytearray(target * 4)
        for gx in range(target):
            x0 = gx * w // target
            x1 = max(x0 + 1, (gx + 1) * w // target)
            sum_r = sum_g = sum_b = sum_a = 0
            count = 0
            for y in range(y0, y1):
                row = rows[y]
                for x in range(x0, x1):
                    i = x * 4
                    alpha = row[i + 3]
                    sum_r += row[i] * alpha
                    sum_g += row[i + 1] * alpha
                    sum_b += row[i + 2] * alpha
                    sum_a += alpha
                    count += 1
            if sum_a:
                line[gx * 4] = sum_r // sum_a
                line[gx * 4 + 1] = sum_g // sum_a
                line[gx * 4 + 2] = sum_b // sum_a
            line[gx * 4 + 3] = sum_a // count if count else 0
        out.append(bytes(line))
    return out


def encode_png(w, h, rows):
    """把逐行 RGBA 编成 PNG（单 IDAT，不做行过滤）。"""
    raw = bytearray()
    for row in rows:
        raw.append(0)                      # 过滤方式 0 = None
        raw += row

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def encode_bmp(w, h, rows):
    """把逐行 RGBA 编成 ico 用的 BMP 条目（BITMAPINFOHEADER + XOR + AND）。"""
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, w * h * 4, 0, 0, 0, 0)

    xor = bytearray()
    for y in range(h - 1, -1, -1):          # BMP 自底向上
        row = rows[y]
        for x in range(w):
            i = x * 4
            xor += bytes((row[i + 2], row[i + 1], row[i], row[i + 3]))   # BGRA

    # AND 掩码：1 表示透明。32bpp 条目以 alpha 通道为准，这里全 0（全不透明），
    # 与原始 32x32 条目的写法保持一致 —— 老程序读不到 alpha 时结果也不会突变。
    and_stride = ((w + 31) // 32) * 4
    return header + bytes(xor) + bytes(and_stride * h)


def build_ico(images):
    """images: [(尺寸, 字节), ...] -> 完整 ico 字节。"""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)

    offset = 6 + count * 16
    directory = bytearray()
    payload = bytearray()
    for size, blob in images:
        dimension = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32,
                                 len(blob), offset)
        payload += blob
        offset += len(blob)
    return bytes(header + directory + payload)


# ---------------------------------------------------------------- 主流程

def regenerate(verbose=True):
    """重新生成 icon.ico。返回 (是否成功, 说明)。"""
    if not os.path.isfile(ICO_PATH):
        return False, "找不到 %s" % ICO_PATH

    original = open(ICO_PATH, "rb").read()

    # 首次运行先留一份原图备份，便于随时回退
    if not os.path.isfile(BACKUP_PATH):
        with open(BACKUP_PATH, "wb") as f:
            f.write(original)
        if verbose:
            print("已备份原图为 %s" % os.path.basename(BACKUP_PATH))

    entries = parse_ico(original)
    if 256 not in entries:
        return False, "原 ico 里没有 256x256 源图，无法生成其它尺寸"

    is_png, source_blob = entries[256]
    if not is_png:
        return False, "256x256 条目不是 PNG 格式，无法解析"
    src_w, src_h, src_rows = decode_source_png(source_blob)

    images = []
    for size in TARGET_SIZES:
        if size in entries:
            # 原图已有的尺寸：原样搬过来，保证画面逐字节不变
            images.append((size, entries[size][1]))
            if verbose:
                print("  %3dx%-3d 沿用原图（%s，%d 字节）"
                      % (size, size, "PNG" if entries[size][0] else "BMP",
                         len(entries[size][1])))
            continue

        rows = area_downscale(src_w, src_h, src_rows, size)
        blob = encode_png(size, size, rows) if size >= PNG_FROM else encode_bmp(size, size, rows)
        images.append((size, blob))
        if verbose:
            print("  %3dx%-3d 由源图降采样生成（%s，%d 字节）"
                  % (size, size, "PNG" if size >= PNG_FROM else "BMP", len(blob)))

    data = build_ico(images)
    with open(ICO_PATH, "wb") as f:
        f.write(data)
    if verbose:
        print("\n已写入 %s（%d 字节，%d 张图）"
              % (os.path.basename(ICO_PATH), len(data), len(images)))
    return True, "已生成 %d 个尺寸" % len(images)


def check():
    """检查 icon.ico 的尺寸是否齐全。返回 (是否齐全, 缺失列表)。"""
    if not os.path.isfile(ICO_PATH):
        return False, list(TARGET_SIZES)
    try:
        entries = parse_ico(open(ICO_PATH, "rb").read())
    except Exception:
        return False, list(TARGET_SIZES)
    missing = [s for s in TARGET_SIZES if s not in entries]
    return not missing, missing


def _self_check():
    """自检：生成结果必须满足"尺寸齐全"且"原图两张逐字节不变"。"""
    if not os.path.isfile(ICO_PATH):
        print("[失败] 找不到 icon.ico")
        return 1

    original = open(ICO_PATH, "rb").read()
    before = parse_ico(original)

    print("生成前尺寸:", sorted(before))
    print()

    ok, message = regenerate()
    print()
    if not ok:
        print("[失败] 生成失败:", message)
        return 1

    after = parse_ico(open(ICO_PATH, "rb").read())
    failures = []

    missing = [s for s in TARGET_SIZES if s not in after]
    print("[%s] 目标尺寸齐全 %s" % ("通过" if not missing else "失败", sorted(after)))
    if missing:
        failures.append("缺失尺寸 %s" % missing)

    for size in sorted(before):
        same = before[size][1] == after[size][1]
        print("[%s] %dx%d 与原图逐字节一致"
              % ("通过" if same else "失败", size, size))
        if not same:
            failures.append("%dx%d 被改动" % (size, size))

    # 每个条目解出来必须是指定尺寸
    for size, (is_png, blob) in sorted(after.items()):
        if is_png:
            w, h, _, _ = struct.unpack(">IIBB", blob[16:26])
        else:
            _, w, h2, _, _, _, _ = struct.unpack("<IiiHHII", blob[:24])
            h = h2 // 2
        good = (w, h) == (size, size)
        if not good:
            failures.append("%dx%d 条目尺寸实为 %dx%d" % (size, size, w, h))
    print("[%s] 所有条目尺寸正确（%d 个）"
          % ("通过" if not any("条目尺寸" in f for f in failures) else "失败",
             len(after)))

    # 生成的 ico 必须能被 Qt 读取（真实使用方）
    try:
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtGui import QIcon
        app = QApplication.instance() or QApplication(sys.argv)
        icon = QIcon(ICO_PATH)
        sizes = sorted(s.width() for s in icon.availableSizes())
        readable = sizes == sorted(after)
        print("[%s] Qt 能读出的尺寸与文件一致: %s"
              % ("通过" if readable else "失败", sizes))
        if not readable:
            failures.append("Qt 读出的尺寸不一致: %s" % sizes)
    except Exception as e:
        print("（跳过 Qt 校验: %s）" % e)

    print()
    if failures:
        print("自检未通过：")
        for f in failures:
            print("  -", f)
        return 1
    print("自检通过")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        ok, missing = check()
        print("icon.ico 尺寸齐全" if ok else "icon.ico 缺少尺寸: %s" % missing)
        sys.exit(0 if ok else 1)
    sys.exit(_self_check())
