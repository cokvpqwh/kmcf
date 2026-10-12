"""程序图标：用 QPainter 现画（蓝色圆角方块 + 白色上升折线 + 红色端点），不依赖图片文件。

    python -m kmcf.gui.icon data\\cache\\kmcf.ico   # 导出 .ico（桌面快捷方式用）
"""

import struct
import sys
from pathlib import Path

from .. import qt_compat

qt_compat.prepare()

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen,  # noqa: E402
                           QPixmap)

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def paint_image(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)

    # 圆角方块（小尺寸留白更少，保证清晰）
    m = s * (0.04 if size <= 24 else 0.06)
    rect = QRectF(m, m, s - 2 * m, s - 2 * m)
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0.0, QColor("#5a7bf0"))
    grad.setColorAt(1.0, QColor("#2f4eb5"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(rect, rect.width() * 0.22, rect.width() * 0.22)

    def pt(x: float, y: float) -> QPointF:  # 0~1 坐标 -> 方块内坐标
        return QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())

    # 底部浅色量柱（大尺寸才画）
    if size >= 32:
        p.setBrush(QColor(255, 255, 255, 60))
        for x, h in ((0.22, 0.10), (0.36, 0.16), (0.50, 0.12), (0.64, 0.20), (0.78, 0.26)):
            w = 0.08
            p.drawRoundedRect(QRectF(pt(x - w / 2, 0.84 - h), pt(x + w / 2, 0.84)), s * 0.01, s * 0.01)

    # 上升折线
    pts = [pt(0.18, 0.66), pt(0.38, 0.50), pt(0.54, 0.58), pt(0.80, 0.28)]
    path = QPainterPath(pts[0])
    for q in pts[1:]:
        path.lineTo(q)
    pen = QPen(QColor("#ffffff"), max(1.6, s * 0.085))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)

    # 红色端点（A 股红涨）
    r = max(1.8, s * 0.095)
    p.setPen(QPen(QColor("#ffffff"), max(1.0, s * 0.035)))
    p.setBrush(QColor("#ff4d4f"))
    p.drawEllipse(pts[-1], r, r)
    p.end()
    return img


def paint_icon(size: int) -> QPixmap:
    return QPixmap.fromImage(paint_image(size))


def app_icon() -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(paint_icon(size))
    return icon


def paint_sparkle(size: int, color: str) -> QPixmap:
    """「AI 分析」按钮的小图标：一大一小两颗四角星。size 为逻辑像素，按屏幕缩放比例绘制。"""
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    dpr = app.devicePixelRatio() if app is not None else 1.0
    px = max(1, int(round(size * dpr)))
    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    s = float(px)

    def star(cx: float, cy: float, r: float, k: float = 0.28) -> None:
        path = QPainterPath(QPointF(cx, cy - r))
        for x, y in ((cx + r * k, cy - r * k), (cx + r, cy), (cx + r * k, cy + r * k), (cx, cy + r),
                     (cx - r * k, cy + r * k), (cx - r, cy), (cx - r * k, cy - r * k)):
            path.lineTo(QPointF(x, y))
        path.closeSubpath()
        p.drawPath(path)

    star(s * 0.42, s * 0.56, s * 0.40)
    star(s * 0.80, s * 0.20, s * 0.19)
    p.end()
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


def sparkle_icon(color: str, checked_color: str | None = None, size: int = 16) -> QIcon:
    icon = QIcon()
    icon.addPixmap(paint_sparkle(size, color), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(paint_sparkle(size, checked_color or color), QIcon.Mode.Normal, QIcon.State.On)
    return icon


def _png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def write_ico(path: Path, sizes=ICO_SIZES) -> Path:
    """写多尺寸 .ico（每个尺寸内嵌 PNG，Windows Vista 起支持）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pngs = [(s, _png_bytes(paint_image(s))) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for s, png in pngs:
        dim = 0 if s >= 256 else s  # 256 在目录项里记为 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    path.write_bytes(header + entries + blobs)
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("kmcf.ico")
    print(write_ico(out))
