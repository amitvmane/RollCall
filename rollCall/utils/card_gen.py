"""
Image card generation for RollCall.

Functions return BytesIO PNG objects ready to pass to bot.send_photo().
All rendering uses PIL (Pillow). Fonts are loaded from system paths with
graceful fallback to PIL's built-in bitmap font.
"""
import io
import logging
import math
import os
from datetime import datetime
from io import BytesIO
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

# ── Palette ──────────────────────────────────────────────────────────────────

_C_BG         = (255, 255, 255)
_C_HEADER_BG  = (37,  99,  235)   # blue-600
_C_HEADER_FG  = (255, 255, 255)
_C_HEADER_SUB = (191, 219, 254)   # blue-200
_C_TEXT       = (17,  24,  39)    # gray-900
_C_MUTED      = (107, 114, 128)   # gray-500
_C_DIVIDER    = (229, 231, 235)   # gray-200
_C_OWED       = (185, 28,  28)    # red-700
_C_SETTLED    = (21,  128, 61)    # green-700
_C_ACCENT     = (37,  99,  235)
_C_STRIP_ODD  = (249, 250, 251)   # gray-50
_C_FOOTER_BG  = (243, 244, 246)   # gray-100


# ── Font loader ───────────────────────────────────────────────────────────────

_FONT_PATHS = [
    # Linux (Debian/Ubuntu with fonts-dejavu-core)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # Linux alternatives
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    # macOS
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]
_FONT_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]

_font_cache: dict = {}
_cmap_cache: dict = {}


def _mpl_dejavu(bold: bool) -> Optional[str]:
    """DejaVu bundled inside matplotlib (already a dependency) — guaranteed
    fallback even if no system fonts are installed."""
    try:
        import matplotlib
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        path = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", name)
        return path if os.path.exists(path) else None
    except Exception:
        return None


def _font_path(bold: bool = False) -> Optional[str]:
    paths = _FONT_BOLD_PATHS + _FONT_PATHS if bold else _FONT_PATHS
    for path in paths:
        if os.path.exists(path):
            return path
    return _mpl_dejavu(bold)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    path = _font_path(bold)
    if path:
        try:
            f = ImageFont.truetype(path, size)
            _font_cache[key] = f
            return f
        except Exception:
            pass
    f = ImageFont.load_default(size=size)
    _font_cache[key] = f
    return f


def _glyph_coverage(bold: bool = False) -> Optional[set]:
    """Codepoints the active font can actually render, or None if unknown."""
    path = _font_path(bold)
    if path is None:
        return None
    if path in _cmap_cache:
        return _cmap_cache[path]
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, fontNumber=0, lazy=True)
        cov = set(tt.getBestCmap().keys())
        tt.close()
    except Exception:
        logging.exception("glyph coverage load failed for %s", path)
        cov = None
    _cmap_cache[path] = cov
    return cov


def _sanitize(text: str, fallback: str = "?", bold: bool = False) -> str:
    """Drop characters the font cannot render (emoji, unsupported scripts).

    PIL draws missing glyphs as tofu boxes — worse than omission on a share
    card. Whitespace is always kept; if nothing renderable remains, return
    `fallback`. No-op when coverage is unknown (PIL default bitmap font).
    """
    cov = _glyph_coverage(bold)
    if cov is None or not text:
        return text or fallback
    kept = "".join(c for c in text if c.isspace() or ord(c) in cov)
    kept = " ".join(kept.split())   # collapse gaps left by dropped chars
    return kept if kept else fallback


def _ellipsize(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def _text_w(draw: ImageDraw.Draw, text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _text_h(draw: ImageDraw.Draw, text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]


# ── QR code ──────────────────────────────────────────────────────────────────

def qr_png(vpa: str, amount: Optional[int] = None) -> BytesIO:
    """Return a PNG BytesIO of a UPI QR for the given VPA and amount.

    `amount=None` (or 0) means "let the payer type the amount" — the `am`
    parameter is omitted entirely rather than interpolated, which is what
    produced `am=None` in the URL and a QR that every UPI app rejected.
    """
    import qrcode  # soft import — already in requirements
    amt_part = f"&am={amount}" if amount else ""
    upi_url = f"upi://pay?pa={vpa}{amt_part}&cu=INR&tn=RollCall"
    img = qrcode.make(upi_url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ── Match-day card ────────────────────────────────────────────────────────────

def matchday_card(
    title: str,
    date_str: str,
    in_members: List[str],
    venue: Optional[str] = None,
) -> BytesIO:
    """Generate a shareable match-day card showing the IN list.

    Args:
        title:      Rollcall / game title
        date_str:   Formatted date string (e.g. "Saturday, 5 Jul 2026")
        in_members: Display names of IN members, in order
        venue:      Optional venue / event note

    Returns BytesIO PNG.
    """
    W       = 640
    PAD     = 28
    HDR_H   = 90
    ROW_H   = 30
    COL_H   = 36   # section header row
    FTR_H   = 34

    f_title  = _font(24, bold=True)
    f_sub    = _font(15)
    f_col    = _font(13)
    f_name   = _font(15)
    f_badge  = _font(13, bold=True)
    f_footer = _font(12)

    n        = len(in_members)
    per_col  = math.ceil(n / 2) if n > 10 else n
    cols     = 2 if n > 10 else 1
    body_h   = COL_H + per_col * ROW_H + PAD

    H = HDR_H + body_h + FTR_H

    img  = Image.new("RGB", (W, H), _C_BG)
    draw = ImageDraw.Draw(img)

    # ── Header ────────────────────────────────────────────────────────────────
    draw.rectangle([0, 0, W, HDR_H], fill=_C_HEADER_BG)
    draw.text((PAD, 14), _ellipsize(_sanitize(title, "Game Day", bold=True), 40),
              font=f_title, fill=_C_HEADER_FG)
    sub = f"{date_str}  •  {_sanitize(venue, '')}" if venue else date_str
    draw.text((PAD, 48), _ellipsize(sub, 60), font=f_sub, fill=_C_HEADER_SUB)

    # IN count badge (top-right)
    badge = f"{n} IN"
    bw    = _text_w(draw, badge, f_badge) + 20
    bx    = W - bw - PAD
    draw.rounded_rectangle([bx, 20, bx + bw, 60], radius=10, fill=_C_HEADER_FG)
    draw.text((bx + 10, 29), badge, font=f_badge, fill=_C_ACCENT)

    # ── Section label ─────────────────────────────────────────────────────────
    y = HDR_H + 8
    draw.text((PAD, y + 6), "PLAYERS", font=f_col, fill=_C_MUTED)
    y += COL_H

    # ── Player list ───────────────────────────────────────────────────────────
    col_w = (W - PAD * 2) // cols
    for i, name in enumerate(in_members):
        col   = i // per_col
        row   = i % per_col
        x     = PAD + col * col_w
        ry    = y + row * ROW_H
        # subtle alternating stripe
        if row % 2 == 0:
            draw.rectangle([x, ry, x + col_w - 4, ry + ROW_H - 2], fill=_C_STRIP_ODD)
        num_str = f"{i + 1:2d}."
        draw.text((x + 6, ry + 7), num_str, font=f_name, fill=_C_MUTED)
        clean = _sanitize(name, f"Player {i + 1}")
        draw.text((x + 34, ry + 7), _ellipsize(clean, 22), font=f_name, fill=_C_TEXT)

    # ── Footer ────────────────────────────────────────────────────────────────
    fy = H - FTR_H
    draw.rectangle([0, fy, W, H], fill=_C_FOOTER_BG)
    draw.text((PAD, fy + 10), "RollCall", font=f_footer, fill=_C_MUTED)
    ts = datetime.now().strftime("Generated %d %b %Y %H:%M")
    tw = _text_w(draw, ts, f_footer)
    draw.text((W - PAD - tw, fy + 10), ts, font=f_footer, fill=_C_MUTED)

    return _to_bytes(img)


# ── Month wrap-up card ────────────────────────────────────────────────────────

def month_wrapup_card(
    group_name: str,
    month_label: str,
    session_count: int,
    avg_attendance: float,
    top_attendees: List[dict],
) -> BytesIO:
    """Generate the monthly season wrap-up card.

    top_attendees is [{'name': str, 'attended': int}], most-attended first.
    Shareable: names and counts only — no tokens, links, or user IDs.
    """
    W       = 640
    PAD     = 28
    HDR_H   = 96
    STAT_H  = 72
    ROW_H   = 34
    COL_H   = 36
    FTR_H   = 38

    f_title  = _font(24, bold=True)
    f_sub    = _font(15)
    f_stat_v = _font(26, bold=True)
    f_stat_l = _font(12)
    f_col    = _font(13)
    f_name   = _font(16)
    f_count  = _font(16, bold=True)
    f_footer = _font(13, bold=True)

    top = top_attendees[:5]
    body_h = COL_H + len(top) * ROW_H + PAD
    H = HDR_H + STAT_H + body_h + FTR_H

    img  = Image.new("RGB", (W, H), _C_BG)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle([0, 0, W, HDR_H], fill=_C_HEADER_BG)
    draw.text((PAD, 16), _ellipsize(_sanitize(group_name, "RollCall Group", bold=True), 34),
              font=f_title, fill=_C_HEADER_FG)
    draw.text((PAD, 52), f"Season wrap-up  •  {month_label}", font=f_sub, fill=_C_HEADER_SUB)

    # Stat row: sessions + avg attendance
    sy = HDR_H + 12
    half = (W - PAD * 2) // 2
    for i, (val, label) in enumerate([
        (str(session_count), "GAMES PLAYED"),
        (f"{avg_attendance:g}", "AVG PLAYERS / GAME"),
    ]):
        x = PAD + i * half
        draw.text((x, sy), val, font=f_stat_v, fill=_C_ACCENT)
        draw.text((x, sy + 34), label, font=f_stat_l, fill=_C_MUTED)

    # Top attendees
    y = HDR_H + STAT_H + 8
    draw.text((PAD, y + 6), "MOST ACTIVE", font=f_col, fill=_C_MUTED)
    y += COL_H
    medals = ["🥇", "🥈", "🥉"]
    for i, a in enumerate(top):
        ry = y + i * ROW_H
        if i % 2 == 0:
            draw.rectangle([PAD, ry, W - PAD, ry + ROW_H - 2], fill=_C_STRIP_ODD)
        rank = medals[i] if i < 3 else f"{i + 1}."
        draw.text((PAD + 6, ry + 7), _sanitize(rank, f"{i + 1}."), font=f_name, fill=_C_MUTED)
        clean = _sanitize(a.get("name") or "?", f"Player {i + 1}")
        draw.text((PAD + 44, ry + 7), _ellipsize(clean, 28), font=f_name, fill=_C_TEXT)
        cnt = f"{a.get('attended', 0)} games"
        cw = _text_w(draw, cnt, f_count)
        draw.text((W - PAD - cw - 6, ry + 7), cnt, font=f_count, fill=_C_SETTLED)

    # Footer — the viral hook
    fy = H - FTR_H
    draw.rectangle([0, fy, W, H], fill=_C_FOOTER_BG)
    draw.text((PAD, fy + 11), "⚡ made with RollCall", font=f_footer, fill=_C_ACCENT)

    return _to_bytes(img)


# ── Close receipt card ────────────────────────────────────────────────────────

def close_receipt_card(
    title: str,
    ground_cost: int,
    subsidy: int,
    per_head: int,
    in_count: int,
    fund_balance: int,
    balances: List[dict],
) -> BytesIO:
    """Generate a receipt card after /settle_dues.

    balances is a list of {member_name, balance} dicts (positive = owes,
    negative = credit). Sorted by balance descending.
    """
    W     = 640
    PAD   = 28
    HDR_H = 80
    SUM_H = 110
    ROW_H = 28
    FTR_H = 34

    f_title  = _font(22, bold=True)
    f_sub    = _font(13)
    f_label  = _font(13)
    f_value  = _font(14, bold=True)
    f_name   = _font(14)
    f_bal    = _font(14, bold=True)
    f_sec    = _font(12)
    f_footer = _font(12)

    owed    = [b for b in balances if b["balance"] > 0]
    settled = [b for b in balances if b["balance"] == 0]

    n_rows = max(len(owed) + len(settled) + 2, 1)  # +2 for section headers
    H      = HDR_H + SUM_H + n_rows * ROW_H + PAD * 2 + FTR_H

    img  = Image.new("RGB", (W, H), _C_BG)
    draw = ImageDraw.Draw(img)

    # ── Header ────────────────────────────────────────────────────────────────
    draw.rectangle([0, 0, W, HDR_H], fill=_C_HEADER_BG)
    draw.text((PAD, 12), "Game Closed", font=f_sub, fill=_C_HEADER_SUB)
    draw.text((PAD, 32), _ellipsize(_sanitize(title, "Game", bold=True), 38),
              font=f_title, fill=_C_HEADER_FG)

    # ── Summary block ─────────────────────────────────────────────────────────
    y  = HDR_H + PAD
    mx = W // 2

    def _kv(x, yy, label, value, value_color=_C_TEXT):
        draw.text((x, yy), label, font=f_label, fill=_C_MUTED)
        draw.text((x, yy + 18), value, font=f_value, fill=value_color)

    _kv(PAD, y, "Ground cost", f"Rs.{ground_cost}")
    _kv(mx,  y, "Per head",    f"Rs.{per_head}", _C_ACCENT)
    y += 46
    _kv(PAD, y, "Players",     str(in_count))
    if subsidy > 0:
        _kv(mx, y, "Fund subsidy", f"-Rs.{subsidy}", _C_SETTLED)
    y += 46
    draw.line([(PAD, y), (W - PAD, y)], fill=_C_DIVIDER, width=1)
    y += PAD // 2
    draw.text((PAD, y), f"Fund balance after: Rs.{fund_balance}", font=f_label, fill=_C_MUTED)
    y += PAD

    # ── Balances ──────────────────────────────────────────────────────────────
    draw.line([(PAD, y), (W - PAD, y)], fill=_C_DIVIDER, width=1)
    y += 8

    if owed:
        draw.text((PAD, y), "OUTSTANDING", font=f_sec, fill=_C_MUTED)
        y += ROW_H - 4
        for i, b in enumerate(owed):
            if i % 2 == 0:
                draw.rectangle([PAD, y, W - PAD, y + ROW_H - 2], fill=_C_STRIP_ODD)
            clean = _ellipsize(_sanitize(b["member_name"], "Member"), 28)
            draw.text((PAD + 6, y + 6), clean, font=f_name, fill=_C_TEXT)
            bal_str = f"Rs.{b['balance']}"
            bw = _text_w(draw, bal_str, f_bal)
            draw.text((W - PAD - bw, y + 6), bal_str, font=f_bal, fill=_C_OWED)
            y += ROW_H

    if settled:
        draw.text((PAD, y + 4), "SETTLED", font=f_sec, fill=_C_SETTLED)
        y += ROW_H - 4
        for b in settled:
            clean = _ellipsize(_sanitize(b["member_name"], "Member"), 28)
            draw.text((PAD + 6, y + 6), clean, font=f_name, fill=_C_MUTED)
            draw.text((W - PAD - 20, y + 6), "✓", font=f_name, fill=_C_SETTLED)
            y += ROW_H

    # ── Footer ────────────────────────────────────────────────────────────────
    fy = H - FTR_H
    draw.rectangle([0, fy, W, H], fill=_C_FOOTER_BG)
    draw.text((PAD, fy + 10), "RollCall", font=f_footer, fill=_C_MUTED)
    ts = datetime.now().strftime("%d %b %Y %H:%M")
    tw = _text_w(draw, ts, f_footer)
    draw.text((W - PAD - tw, fy + 10), ts, font=f_footer, fill=_C_MUTED)

    return _to_bytes(img)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_bytes(img: Image.Image) -> BytesIO:
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
