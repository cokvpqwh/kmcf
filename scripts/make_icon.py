"""生成 assets/kmcf.ico（打包 exe、桌面快捷方式用）和多尺寸 PNG。

图标图案复用 kmcf/gui/icon.py 的 paint_image()（蓝色圆角方块 + 白色上升折线 + 红色端点），
保证 exe 图标与软件窗口/任务栏图标一致。

ICO 组装（不依赖 Pillow 等第三方库）：
  - 256x256 用 PNG 数据（Windows Vista 起支持，体积小）；
  - 16~128 用 32 位 BGRA 位图（BITMAPINFOHEADER + 自下而上像素 + AND 掩码），兼容性最好
    （资源管理器、任务栏、PyInstaller 写入 exe 资源、老版本工具都能正确读取）。
  加 --png-all 时所有尺寸都内嵌 PNG。

用法（项目根目录）：
    .venv\\Scripts\\python.exe scripts\\make_icon.py            # 输出 assets\\kmcf.ico + assets\\icon\\kmcf_<size>.png
    .venv\\Scripts\\python.exe scripts\\make_icon.py --check    # 生成后用 Qt 读回校验每个尺寸
"""

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kmcf import qt_compat  # noqa: E402

qt_compat.prepare()  # 必须在导入 PySide6 之前（Anaconda 的 ICU 冲突）

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from kmcf.gui.icon import paint_image  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)
PNG_MIN_SIZE = 256  # >= 这个尺寸内嵌 PNG，其余用 32 位位图


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        raise RuntimeError("PNG 编码失败")
    buf.close()
    return bytes(data)


def dib_bytes(img: QImage) -> bytes:
    """ICO 里的 32 位位图：BITMAPINFOHEADER(高度为 2 倍) + BGRA 像素(自下而上) + 1 位 AND 掩码。"""
    img = img.convertToFormat(QImage.Format.Format_ARGB32)  # 非预乘；小端内存顺序正好是 B G R A
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    raw = bytes(img.constBits())[: bpl * h]
    rows = [raw[y * bpl: y * bpl + w * 4] for y in range(h)]

    xor = b"".join(reversed(rows))  # 自下而上
    mask_stride = ((w + 31) // 32) * 4
    mask_rows = []
    for row in rows:
        bits = bytearray(mask_stride)
        for x in range(w):
            if row[x * 4 + 3] == 0:  # 完全透明的像素在 AND 掩码里置 1（给只认掩码的老程序用）
                bits[x >> 3] |= 0x80 >> (x & 7)
        mask_rows.append(bytes(bits))
    and_mask = b"".join(reversed(mask_rows))

    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(and_mask), 0, 0, 0, 0)
    return header + xor + and_mask


def build_ico(images: dict[int, QImage], png_all: bool = False) -> bytes:
    entries = []
    for size in sorted(images):
        img = images[size]
        data = png_bytes(img) if (png_all or size >= PNG_MIN_SIZE) else dib_bytes(img)
        entries.append((size, data))

    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))  # reserved, type=1(icon), count
    offset = 6 + 16 * len(entries)
    blobs = bytearray()
    for size, data in entries:
        dim = 0 if size >= 256 else size  # 目录项里 256 记为 0
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    return bytes(out + blobs)


def check_ico(path: Path) -> list[str]:
    """用 Qt 的 ico 插件逐帧读回，返回问题列表（空列表表示正常）。"""
    from PySide6.QtGui import QImageReader

    problems = []
    data = path.read_bytes()
    reserved, typ, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or typ != 1 or count != len(SIZES):
        problems.append(f"ICO 头不正确：reserved={reserved} type={typ} count={count}")
    for i in range(count):
        w, h, _c, _r, planes, bpp, size, off = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        w, h = w or 256, h or 256
        if off + size > len(data):
            problems.append(f"第 {i} 帧越界")
        if planes != 1 or bpp != 32:
            problems.append(f"第 {i} 帧 planes/bpp 异常：{planes}/{bpp}")
    reader = QImageReader(str(path))
    reader.setFormat(b"ico")
    got = []
    for i in range(reader.imageCount()):
        reader.jumpToImage(i)
        img = reader.read()
        if img.isNull():
            problems.append(f"Qt 无法解码第 {i} 帧：{reader.errorString()}")
        else:
            got.append(img.width())
            # 抽查中心像素不透明（图标中心是蓝色方块）
            c = img.pixelColor(img.width() // 2, img.height() // 2)
            if c.alpha() < 200:
                problems.append(f"{img.width()}px 帧中心像素透明，绘制可能有误")
    if sorted(got) != sorted(SIZES):
        problems.append(f"Qt 读到的尺寸 {sorted(got)} 与期望 {list(SIZES)} 不一致")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成 kmcf 多尺寸图标")
    ap.add_argument("--out", default=str(ROOT / "assets" / "kmcf.ico"), help="ICO 输出路径")
    ap.add_argument("--png-dir", default=str(ROOT / "assets" / "icon"), help="多尺寸 PNG 输出目录")
    ap.add_argument("--png-all", action="store_true", help="所有尺寸都内嵌 PNG（默认只有 256 用 PNG）")
    ap.add_argument("--check", action="store_true", help="生成后读回校验")
    args = ap.parse_args(argv)

    app = QGuiApplication.instance() or QGuiApplication(["make_icon", "-platform", "offscreen"])  # noqa: F841

    images = {s: paint_image(s) for s in SIZES}
    png_dir = Path(args.png_dir)
    png_dir.mkdir(parents=True, exist_ok=True)
    for s, img in images.items():
        (png_dir / f"kmcf_{s}.png").write_bytes(png_bytes(img))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_ico(images, png_all=args.png_all))
    print(f"{out}  ({out.stat().st_size:,} 字节，尺寸 {', '.join(map(str, SIZES))})")
    print(f"{png_dir}\\kmcf_<size>.png")

    if args.check:
        problems = check_ico(out)
        for p in problems:
            print("问题：" + p)
        if problems:
            return 1
        print("校验通过：Qt 能读出全部尺寸")
    return 0


if __name__ == "__main__":
    sys.exit(main())
