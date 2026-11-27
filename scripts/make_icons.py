"""Draw RollCall's app icons from one definition (Pillow — already a dependency).

The mark: a clipboard with a check — "you're counted in" — white on the
brand blue (#2563eb, deepening to #1d4ed8). Keep rollCall/api/web/logo.svg
the same drawing.

    python scripts/make_icons.py

Writes into rollCall/api/web/:
  icon-192.png, icon-512.png        rounded tile, transparent corners ("any")
  icon-maskable-192/512.png         full-bleed, mark inside the 80% safe zone
  apple-touch-icon.png (180)        full-bleed and opaque — iOS rounds it, and
                                    paints transparent pixels black
  badge-96.png                      white mark on transparent: Android shows a
                                    notification badge as a silhouette, so a
                                    full-colour icon there is a white square
"""
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(__file__), "..", "rollCall", "api", "web")
TOP, BOTTOM = (59, 130, 246), (29, 78, 216)     # gradient, top-left → bottom-right
WHITE = (255, 255, 255, 255)
SS = 4                                          # supersampling for smooth edges


def _gradient(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)) + (255,)
    return img


def _mark(draw: ImageDraw.ImageDraw, size: int, scale: float, color) -> None:
    """The clipboard + check, in the SVG's 40-unit grid, centred and scaled."""
    u = size / 40 * scale
    off = size / 2 - 20 * u

    def p(x, y):
        return off + x * u, off + y * u
    w = 2.2 * u
    x0, y0 = p(10.5, 11.5)
    x1, y1 = p(29.5, 33)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=3 * u, outline=color, width=round(w))
    cx0, cy0 = p(15, 8.5)
    cx1, cy1 = p(25, 14)
    draw.rounded_rectangle([cx0, cy0, cx1, cy1], radius=1.8 * u, fill=color)
    pts = [p(14.8, 23.2), p(18.4, 26.8), p(25.2, 19.4)]
    cw = 2.6 * u
    draw.line(pts, fill=color, width=round(cw), joint="curve")
    for x, y in (pts[0], pts[-1]):                # round caps
        draw.ellipse([x - cw / 2, y - cw / 2, x + cw / 2, y + cw / 2], fill=color)


def tile(size: int, rounded: bool, scale: float = 1.0) -> Image.Image:
    big = size * SS
    img = _gradient(big)
    _mark(ImageDraw.Draw(img), big, scale, WHITE)
    if rounded:
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=big * 10 / 40, fill=255)
        img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


def badge(size: int) -> Image.Image:
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    _mark(ImageDraw.Draw(img), big, 1.45, WHITE)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    tile(192, rounded=True).save(os.path.join(OUT, "icon-192.png"), optimize=True)
    tile(512, rounded=True).save(os.path.join(OUT, "icon-512.png"), optimize=True)
    # Maskable: launchers crop to a circle/squircle — keep the mark inside 80%.
    tile(192, rounded=False, scale=0.78).save(os.path.join(OUT, "icon-maskable-192.png"), optimize=True)
    tile(512, rounded=False, scale=0.78).save(os.path.join(OUT, "icon-maskable-512.png"), optimize=True)
    tile(180, rounded=False, scale=0.9).convert("RGB").save(os.path.join(OUT, "apple-touch-icon.png"), optimize=True)
    badge(96).save(os.path.join(OUT, "badge-96.png"), optimize=True)
    print("icons written to", os.path.normpath(OUT))


if __name__ == "__main__":
    main()
