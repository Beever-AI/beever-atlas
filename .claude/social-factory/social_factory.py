#!/usr/bin/env python3
"""Social Factory — render one on-brand social tile per run (Votee + Beever).

Cross-platform (macOS + Linux). Latin fonts are bundled in assets/fonts/ so the
look is identical on every machine; CJK falls back to a system font.
"""
import os, math, json, argparse, datetime

W, H = 1080, 1350
HERE = os.path.dirname(os.path.abspath(__file__))
FONTDIR = os.path.join(HERE, "assets", "fonts")
LOGODIR = os.path.join(HERE, "assets", "logos")


def _resolve(bundled, *system):
    cands = [os.path.join(FONTDIR, bundled)] + list(system)
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


SANS_B = _resolve("DejaVuSans-Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf")
SANS_R = _resolve("DejaVuSans.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/System/Library/Fonts/Supplemental/Arial.ttf")
SERIF_B = _resolve("DejaVuSerif-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
                   "/System/Library/Fonts/Supplemental/Georgia Bold.ttf")
CJK = _resolve("__none__",
               "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
               "/System/Library/Fonts/PingFang.ttc")

BRANDS = {
    "votee": {
        "bg": (233, 225, 204), "dark": (51, 49, 42), "accent": (224, 122, 40),
        "grey": (107, 101, 87), "kicker": (51, 49, 42),
        "accent_font": SERIF_B, "mark": "diamond", "mark_color": (224, 122, 40),
    },
    "beever": {
        "bg": (251, 248, 255), "dark": (58, 58, 64), "accent": (10, 10, 12),
        "grey": (126, 117, 118), "kicker": (76, 69, 70),
        "accent_font": SANS_B, "mark": "square", "mark_color": (10, 10, 12),
        "logo": "beever.png",
    },
}

KICK_SZ, HEAD_SZ, SUB_SZ, MARGIN = 46, 132, 46, 80
MAXW = W - 2 * MARGIN


def _font(p, s):
    from PIL import ImageFont
    if p and os.path.exists(p):
        return ImageFont.truetype(p, s)
    try:
        return ImageFont.load_default(s)
    except TypeError:
        return ImageFont.load_default()


def _tw(d, s, f):
    b = d.textbbox((0, 0), s, font=f); return b[2] - b[0]


def _lh(f):
    a, de = f.getmetrics(); return a + de


def _centered(d, y, s, f, fill):
    d.text(((W - _tw(d, s, f)) / 2, y), s, font=f, fill=fill)


def _fit_text(d, text, path, start, maxw, minsz=26):
    sz = start
    while sz > minsz and _tw(d, text, _font(path, sz)) > maxw:
        sz -= 2
    return _font(path, sz)


def _mark(d, cy, cfg, s=66):
    cx = W / 2
    if cfg["mark"] == "diamond":
        L, Wd, inv = s, s * 0.30, 1 / math.sqrt(2)
        for dx, dy in [(1, 1), (1, -1)]:
            ux, uy = dx * inv, dy * inv; px, py = -uy, ux
            pts = [(cx + ux * a + px * b, cy + uy * a + py * b)
                   for a, b in [(-L, Wd), (-L, -Wd), (L, -Wd), (L, Wd)]]
            d.polygon(pts, fill=cfg["mark_color"])
        hs = Wd * 0.92
        d.polygon([(cx, cy - hs), (cx + hs, cy), (cx, cy + hs), (cx - hs, cy)], fill=cfg["bg"])
    else:
        r = s * 0.62
        d.rectangle([cx - r, cy - r, cx + r, cy + r], fill=cfg["mark_color"])


def _fit_head(d, head, cfg):
    sz = HEAD_SZ
    while sz > 58:
        fonts = {"dark": _font(SANS_B, sz), "accent": _font(cfg["accent_font"], sz)}
        if all(_tw(d, t, fonts[k]) <= MAXW for t, k in head):
            return fonts
        sz -= 2
    return {"dark": _font(SANS_B, sz), "accent": _font(cfg["accent_font"], sz)}


def render(idea, cfg, out_path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), cfg["bg"]); d = ImageDraw.Draw(img)
    kf = _fit_text(d, idea["kicker"], SANS_R, KICK_SZ, MAXW)
    sf = _font(SANS_R, SUB_SZ)
    head = [(t, k) for t, k in idea["head"]]
    base = _fit_head(d, head, cfg)
    hfonts = []
    for t, k in head:
        f = base[k]
        if k == "accent" and CJK and any(ord(c) > 0x2E7F for c in t):
            f = _font(CJK, f.size)
        hfonts.append(f)
    kick_h, sub_h = _lh(kf), _lh(sf)
    head_hs = [_lh(f) for f in hfonts]
    g_kh, g_hs, hlg, slg = 70, 95, 14, 8
    block = (kick_h + g_kh + sum(head_hs) + hlg * (len(head_hs) - 1)
             + g_hs + sub_h * len(idea["sub"]) + slg * (len(idea["sub"]) - 1))
    y = (H - block) / 2 - 60
    _centered(d, y, idea["kicker"], kf, cfg["kicker"]); y += kick_h + g_kh
    for (t, k), f, hh in zip(head, hfonts, head_hs):
        _centered(d, y, t, f, cfg["accent"] if k == "accent" else cfg["dark"])
        y += hh + hlg
    y += g_hs - hlg
    for ln in idea["sub"]:
        _centered(d, y, ln, sf, cfg["grey"]); y += sub_h + slg
    logo_file = cfg.get("logo")
    logo_path = os.path.join(LOGODIR, logo_file) if logo_file else None
    if logo_path and os.path.exists(logo_path):
        logo = Image.open(logo_path).convert("RGBA")
        th = 122
        logo = logo.resize((max(1, round(logo.width * th / logo.height)), th), Image.LANCZOS)
        img.paste(logo, ((W - logo.width) // 2, round(H - 210 - logo.height / 2)), logo)
    else:
        _mark(d, H - 195, cfg)
    img.save(out_path, "PNG")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", required=True, choices=list(BRANDS))
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--queue", default=None)
    ap.add_argument("--outdir", default=os.path.join(HERE, "output"))
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    a = ap.parse_args()
    queue = a.queue or os.path.join(HERE, "queues", f"queue-{a.brand}.json")
    with open(queue) as f:
        ideas = json.load(f)
    if a.status:
        rem = sum(1 for i in ideas if not i.get("used"))
        print(f"{a.brand}: {rem}/{len(ideas)} ideas unused"); return
    os.makedirs(a.outdir, exist_ok=True)
    nxt = next((i for i in ideas if not i.get("used")), None)
    if nxt is None:
        print(f"QUEUE_EMPTY brand={a.brand} — all {len(ideas)} ideas used. Refill {queue}.")
        raise SystemExit(2)
    out = os.path.join(a.outdir, f"{a.date}-{a.brand}-{nxt['id']}.png")
    render(nxt, BRANDS[a.brand], out)
    nxt["used"] = True; nxt["used_date"] = a.date
    with open(queue, "w") as f:
        json.dump(ideas, f, indent=2, ensure_ascii=False)
    rem = sum(1 for i in ideas if not i.get("used"))
    print(f"OK {out}\nidea={nxt['id']} remaining={rem}")


if __name__ == "__main__":
    main()
