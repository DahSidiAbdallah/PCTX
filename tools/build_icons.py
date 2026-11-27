"""Regenerate the application icon files from the SVG sources.

    python tools/build_icons.py

Needs PySide6 (already a dependency) and Pillow (build-time only:
`pip install pillow`) -- Pillow is just used to write the multi-size .ico.

Outputs (repo root):
    app_icon.ico   multi-resolution Windows icon, used for the window/taskbar
    app_icon.png   512 px render of app_icon.svg (docs, non-Windows)
    logo.png       2x render of logo.svg (the README header)

Why the small sizes are drawn separately: the full mark (a thin glowing X
crossing the P's bowl, plus a second thin X below) is fine at 64 px and up but
turns to mush at 16-48 px -- the counter of the P is swallowed and the thin
strokes vanish into the dark badge. So 16-48 px use SMALL_SVG below: the
same P-and-X idea with a bigger P, one heavy X, and a brighter ring so it also
reads on a dark taskbar.
"""
import os
import sys

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SMALL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#67d5ff"/><stop offset="1" stop-color="#2f6bff"/>
    </linearGradient>
    <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#16294a"/><stop offset="1" stop-color="#0b1424"/>
    </linearGradient>
  </defs>
  <rect x="26" y="26" width="460" height="460" rx="112" fill="url(#fill)" stroke="url(#ring)" stroke-width="34"/>
  <path d="M130 96H268C346 96 396 142 396 208C396 274 346 320 268 320H208V412H130Z" fill="#ffffff"/>
  <path d="M208 164H260C292 164 312 182 312 208C312 234 292 252 260 252H208Z" fill="#0b1424"/>
  <path d="M296 296L424 424M424 296L296 424" stroke="#38bdf8" stroke-width="56" stroke-linecap="round"/>
</svg>"""


def render(svg_bytes: bytes, size: int) -> QImage:
    renderer = QSvgRenderer(QByteArray(svg_bytes))
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    renderer.render(p)
    p.end()
    return img


def to_pil(img: QImage):
    from PIL import Image
    img = img.convertToFormat(QImage.Format_RGBA8888)
    ptr = img.constBits()
    return Image.frombuffer("RGBA", (img.width(), img.height()), bytes(ptr), "raw", "RGBA", img.bytesPerLine(), 1)


def main() -> int:
    QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    full_svg = open(os.path.join(ROOT, "app_icon.svg"), "rb").read()

    big = render(full_svg, 512)
    big.save(os.path.join(ROOT, "app_icon.png"))

    frames = {}
    for s in (16, 24, 32, 48):
        frames[s] = to_pil(render(SMALL_SVG.encode(), s))
    for s in (64, 128, 256):
        frames[s] = to_pil(render(full_svg, s))

    largest = frames[256]
    largest.save(
        os.path.join(ROOT, "app_icon.ico"),
        format="ICO",
        sizes=[(s, s) for s in sorted(frames)],
        append_images=[frames[s] for s in sorted(frames) if s != 256],
    )
    renderer = QSvgRenderer(QByteArray(open(os.path.join(ROOT, "logo.svg"), "rb").read()))
    size = renderer.defaultSize() * 2
    logo = QImage(size, QImage.Format_ARGB32)
    logo.fill(Qt.transparent)
    p = QPainter(logo)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    renderer.render(p)
    p.end()
    logo.save(os.path.join(ROOT, "logo.png"))
    print("wrote app_icon.png, logo.png and app_icon.ico with sizes", sorted(frames))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
