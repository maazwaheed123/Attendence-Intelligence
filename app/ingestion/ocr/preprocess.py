"""Image clean-up before OCR: grayscale, deskew, grid-line removal, denoise, upscale.

Table grid lines are the main reason Tesseract fails on registers (it reads the
lines as characters), so they are detected with long morphological kernels and
erased before recognition.
"""

import io

import cv2
import numpy as np
from PIL import Image

MAX_SKEW_DEG = 8.0


def load_gray(content: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(content))
    img.load()
    return np.array(img.convert("L"))


def estimate_skew(gray: np.ndarray) -> float:
    """Median angle of long near-horizontal lines (table rules / ruled paper / text rows)."""
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 1800, threshold=200, minLineLength=gray.shape[1] // 4, maxLineGap=20
    )
    if lines is None:
        return 0.0
    angles = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(a) <= MAX_SKEW_DEG:
            angles.append(a)
    return float(np.median(angles)) if angles else 0.0


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.05:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def remove_lines(gray: np.ndarray) -> np.ndarray:
    """Erase long horizontal/vertical strokes (grid, ruled paper, margins)."""
    binary = cv2.adaptiveThreshold(
        ~gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -10
    )
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, gray.shape[1] // 25), 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(40, gray.shape[0] // 25)))
    mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel) | cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, v_kernel
    )
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
    out = gray.copy()
    out[mask > 0] = 255
    return out


def prepare(content: bytes) -> tuple[np.ndarray, dict]:
    """Returns (clean grayscale image, info about what was done)."""
    gray = load_gray(content)
    angle = estimate_skew(gray)
    gray = rotate(gray, angle)
    gray = remove_lines(gray)
    gray = cv2.medianBlur(gray, 3)
    scale = 1.0
    if gray.shape[1] < 1600:
        scale = 1600 / gray.shape[1]
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return gray, {"deskew_deg": round(angle, 2), "scale": round(scale, 2)}


def to_png(gray: np.ndarray, max_side: int = 1280) -> bytes:
    """Downscaled PNG for the vision model (smaller = much faster on CPU)."""
    h, w = gray.shape[:2]
    f = min(1.0, max_side / max(h, w))
    if f < 1:
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", gray)
    return buf.tobytes()
