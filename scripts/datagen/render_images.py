"""Scanned-register and handwritten-sheet renderers (Pillow + numpy, fully seeded).

- Printed scan: a typed register, then rotation, blur, noise and JPEG artefacts.
- Handwriting: OFL handwriting fonts drawn glyph by glyph with jitter in size,
  rotation, baseline and ink, on ruled paper.
- Ambiguous sheet: strike-through correction, half-erased ID, smudged "P?".
"""

import io
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from scripts.datagen.manifest import Manifest
from scripts.datagen.util import FIXED_DT, dmy, to_date

FONT_DIR = Path(__file__).resolve().parents[2] / "data" / "fonts"
PRINT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
PRINT_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
PAGE = (1240, 1754)  # A4 at 150 dpi
PAPER = (250, 250, 244)
HAND_WORDS = {
    "present": "Present",
    "absent": "Absent",
    "leave": "Leave",
    "wfh": "WFH",
    "half_day": "Half day",
    "holiday": "Holiday",
}


@lru_cache(maxsize=256)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def degrade(img: Image.Image, rng, *, angle, blur, noise, jpeg_q) -> Image.Image:
    img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(238, 238, 232))
    img = img.filter(ImageFilter.GaussianBlur(blur))
    arr = np.asarray(img).astype(np.float32)
    arr += rng.normal(0, noise, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=jpeg_q)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _save_png(img: Image.Image, path: Path):
    img.save(path, "PNG", optimize=False)


def _save_pdf(img: Image.Image, path: Path):
    img.save(
        path,
        "PDF",
        resolution=150.0,
        creationDate=FIXED_DT.timetuple(),
        modDate=FIXED_DT.timetuple(),
    )


# --------------------------------------------------------------------------- printed scan


def write_printed_scan(png_path: Path, pdf_path: Path, rows, *, day: str, seed: int):
    cols = [
        ("ID", 80),
        ("Name", 220),
        ("Department", 520),
        ("Status", 790),
        ("In", 960),
        ("Out", 1080),
    ]
    img = Image.new("RGB", PAGE, "white")
    d = ImageDraw.Draw(img)
    d.text(
        (80, 90),
        "ACME CORP - DAILY ATTENDANCE REGISTER",
        font=_font(PRINT_FONT_BOLD, 38),
        fill="black",
    )
    d.text(
        (80, 150),
        f"Date: {dmy(to_date(day))}     Departments: Engineering, Human Resources",
        font=_font(PRINT_FONT, 26),
        fill="black",
    )
    top, rh = 230, 64
    body = _font(PRINT_FONT, 26)
    head = _font(PRINT_FONT_BOLD, 26)
    for name, x in cols:
        d.text((x, top + 18), name, font=head, fill="black")
    words = {
        "present": "Present",
        "absent": "Absent",
        "leave": "Leave",
        "wfh": "WFH",
        "half_day": "Half Day",
        "holiday": "Holiday",
    }
    for i, r in enumerate(rows, start=1):
        y = top + i * rh + 18
        vals = [
            r["employee_id"],
            r["employee_name"],
            r["department"],
            words[r["status"]],
            r["check_in"] or "-",
            r["check_out"] or "-",
        ]
        for (_, x), v in zip(cols, vals, strict=True):
            d.text((x, y), v, font=body, fill="black")
    for i in range(len(rows) + 2):
        d.line([(60, top + i * rh), (1180, top + i * rh)], fill="black", width=2)
    for x in [60] + [c[1] - 15 for c in cols[1:]] + [1180]:
        d.line([(x, top), (x, top + (len(rows) + 1) * rh)], fill="black", width=2)
    d.text(
        (80, top + (len(rows) + 2) * rh),
        "Supervisor signature: ________________",
        font=body,
        fill="black",
    )

    rng = np.random.default_rng(seed)
    scanned = degrade(img, rng, angle=1.8, blur=0.9, noise=12, jpeg_q=55)
    _save_png(scanned, png_path)
    _save_pdf(scanned, pdf_path)

    manifests = []
    for path, fmt in ((png_path, "image"), (pdf_path, "pdf_scanned")):
        m = Manifest(
            path.name,
            fmt,
            "tenant_a",
            logical_name=f"tenant_a_register_{day}_{fmt}",
            description=(
                "Printed register, scanned (rotated, blurred, noisy, JPEG artefacts); no text layer"
            ),
        )
        for i, r in enumerate(rows, start=2):
            m.add_row(
                f"page=1;row={i}",
                r,
                rendered={
                    "ID": r["employee_id"],
                    "Status": words[r["status"]],
                    "In": r["check_in"] or "-",
                    "Out": r["check_out"] or "-",
                },
            )
        manifests.append(m)
    return manifests


# --------------------------------------------------------------------------- handwriting


def _ruled_paper() -> Image.Image:
    img = Image.new("RGB", PAGE, PAPER)
    d = ImageDraw.Draw(img)
    for y in range(220, PAGE[1] - 80, 72):
        d.line([(0, y), (PAGE[0], y)], fill=(172, 200, 230), width=2)
    d.line([(110, 0), (110, PAGE[1])], fill=(230, 150, 150), width=2)
    return img


def draw_hand(img, x, y, text, font_path, size, rng, ink=(28, 40, 110)):
    """Write text glyph by glyph on baseline y. Returns the bounding box drawn."""
    x0 = x
    for ch in text:
        if ch == " ":
            x += size * 0.32 * rng.uniform(0.9, 1.25)
            continue
        fs = int(size + rng.integers(-3, 4))
        font = _font(font_path, fs)
        left, _top, right, _bottom = font.getbbox(ch)
        glyph = Image.new("L", (right - left + 16, fs * 2), 0)
        ImageDraw.Draw(glyph).text((8 - left, int(fs * 0.3)), ch, font=font, fill=255)
        glyph = glyph.rotate(float(rng.uniform(-7, 7)), resample=Image.BICUBIC, expand=True)
        strength = float(rng.uniform(0.78, 1.0))
        glyph = glyph.point(lambda v, s=strength: int(v * s))
        yoff = int(np.sin(x / 53.0) * 3 + rng.normal(0, 1.3))
        color = tuple(int(np.clip(c + rng.integers(-14, 15), 0, 255)) for c in ink)
        img.paste(Image.new("RGB", glyph.size, color), (int(x), int(y - fs * 1.05 + yoff)), glyph)
        x += font.getlength(ch) * rng.uniform(0.93, 1.05)
    return (int(x0), int(y - size), int(x), int(y + size * 0.3))


def _strike(img, box, rng, ink=(28, 40, 110)):
    x0, y0, x1, y1 = box
    ym = (y0 + y1) // 2 + 4
    ImageDraw.Draw(img).line(
        [(x0 - 4, ym + int(rng.integers(-3, 4))), (x1 + 4, ym - 6)], fill=ink, width=5
    )


def _erase_tail(img, box, fraction=0.3):
    x0, y0, x1, y1 = box
    ex = int(x1 - (x1 - x0) * fraction)
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(over).rectangle([ex, y0 - 6, x1 + 8, y1 + 6], fill=(*PAPER, 215))
    img.paste(Image.alpha_composite(img.convert("RGBA"), over).convert("RGB"))


def _smudge(img, box):
    x0, y0, x1, y1 = box
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse([x0 - 25, y0 - 5, x1 + 30, y1 + 15], fill=150)
    mask = mask.filter(ImageFilter.GaussianBlur(14))
    img.paste(Image.new("RGB", img.size, (95, 95, 115)), (0, 0), mask)


COLS = {"ID": 140, "Name": 300, "Status": 700, "In": 930, "Out": 1070}


def _sheet_header(img, title, font, rng):
    draw_hand(img, 140, 150, title, font, 58, rng)
    y = 220 + 72
    for name, x in COLS.items():
        draw_hand(img, x, y - 12, name, font, 44, rng, ink=(60, 60, 60))
    return y


def write_handwritten_sheet(path: Path, rows, *, day: str, seed: int):
    rng = np.random.default_rng(seed)
    font = str(FONT_DIR / "Caveat.ttf")
    img = _ruled_paper()
    y = _sheet_header(img, f"HR sign-in sheet   {dmy(to_date(day))}", font, rng)
    m = Manifest(
        path.name,
        "image",
        "tenant_a",
        logical_name=f"tenant_a_hr_signin_{day}",
        description="Clear handwriting (Caveat font), HR, one day",
    )
    for i, r in enumerate(rows, start=2):
        y += 72
        vals = {
            "ID": r["employee_id"],
            "Name": r["employee_name"],
            "Status": HAND_WORDS[r["status"]],
            "In": (r["check_in"] or "-").lstrip("0"),
            "Out": r["check_out"] or "-",
        }
        for col, v in vals.items():
            draw_hand(img, COLS[col], y - 12, v, font, 50, rng)
        m.add_row(f"page=1;row={i}", r, rendered=vals)
    img = degrade(img, rng, angle=0.6, blur=0.5, noise=5, jpeg_q=80)
    _save_png(img, path)
    return m


def write_handwritten_ambiguous(path: Path, rows, *, day: str, seed: int):
    """Rows: clear, strike-through correction, clear, half-erased ID + smudged 'P?'."""
    rng = np.random.default_rng(seed)
    messy = str(FONT_DIR / "ReenieBeanie.ttf")
    neat = str(FONT_DIR / "IndieFlower.ttf")
    img = _ruled_paper()
    y = _sheet_header(img, f"Sales sign-in  {dmy(to_date(day))}", messy, rng)
    m = Manifest(
        path.name,
        "image",
        "tenant_a",
        logical_name=f"tenant_a_sales_signin_{day}",
        description="Messy handwriting with deliberate ambiguity (review_required expected)",
    )
    for i, r in enumerate(rows, start=2):
        y += 72
        font = neat if i % 2 == 0 else messy
        size = 46 if font == neat else 60
        effect = {"E009": "strike", "E011": "erase_smudge"}.get(r["employee_id"])
        id_box = draw_hand(img, COLS["ID"], y - 12, r["employee_id"], font, size, rng)
        draw_hand(img, COLS["Name"], y - 12, r["employee_name"], font, size, rng)
        rendered = {"ID": r["employee_id"], "Name": r["employee_name"]}
        if effect == "strike":
            wrong = "Absent" if r["status"] != "absent" else "Present"
            box = draw_hand(img, COLS["Status"] - 40, y - 12, wrong, font, size, rng)
            _strike(img, box, rng)
            draw_hand(img, box[2] + 18, y - 12, HAND_WORDS[r["status"]], font, size - 8, rng)
            rendered["Status"] = f"~~{wrong}~~ {HAND_WORDS[r['status']]}"
            note = "status overwritten (strike-through)"
        elif effect == "erase_smudge":
            _erase_tail(img, id_box, 0.3)
            box = draw_hand(img, COLS["Status"], y - 12, "P?", font, size, rng)
            _smudge(img, box)
            rendered["ID"] = r["employee_id"][:-1] + "?"
            rendered["Status"] = "P? (smudged)"
            note = "ID partly erased, status smudged"
        else:
            draw_hand(img, COLS["Status"], y - 12, HAND_WORDS[r["status"]], font, size, rng)
            rendered["Status"] = HAND_WORDS[r["status"]]
            note = None
        if effect is None:
            draw_hand(img, COLS["In"], y - 12, (r["check_in"] or "-").lstrip("0"), font, size, rng)
            draw_hand(img, COLS["Out"], y - 12, r["check_out"] or "-", font, size, rng)
            rendered.update({"In": r["check_in"] or "-", "Out": r["check_out"] or "-"})
        m.add_row(
            f"page=1;row={i}", r, rendered=rendered, expected_review=effect is not None, note=note
        )
    img = degrade(img, rng, angle=-0.9, blur=0.6, noise=6, jpeg_q=75)
    _save_png(img, path)
    return m
