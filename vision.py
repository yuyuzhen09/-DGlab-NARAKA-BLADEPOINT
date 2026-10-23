# vision.py
import mss
import numpy as np
from collections import deque

_H = deque(maxlen=3)
_A = deque(maxlen=3)

# 护甲格数：蓝3、紫4、金5、红6
ARMOR_GRID = {
    "blue": 3,
    "purple": 4,
    "gold": 5,
    "red": 6,
}

# 每格判定阈值
CELL_FULL_RATIO = 0.35   # 一格内彩色像素占比 >= 35% → 满格
CELL_HALF_RATIO = 0.12   # 一格内彩色像素占比 12%~35% → 半格
ARMOR_MIN_COLOR_RATIO = 0.02  # 彩色像素至少占总像素 2%，否则视为无甲


def capture(region):
    x1, y1, x2, y2 = region
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return None
    with mss.mss() as sct:
        s = np.array(sct.grab({"left": x1, "top": y1, "width": w, "height": h}))
    return s[:, :, :3]


def _trim_vertical(rgb, top=0.10, bottom=0.90):
    h = rgb.shape[0]
    t = int(h * top)
    b = int(h * bottom)
    if b - t < 1:
        return rgb
    return rgb[t:b, :, :]


def _column_fill_ratio(mask, min_ratio=0.15):
    col_ratio = mask.mean(axis=0)
    col_filled = col_ratio >= min_ratio
    w = len(col_filled)
    if w == 0:
        return 0.0
    run = 0
    for i in range(w - 1, -1, -1):
        if col_filled[i]:
            run += 1
            if run >= 2:
                return (i + run) / w * 100.0
        else:
            run = 0
    return 0.0


def _color_masks(rgb):
    r = rgb[:, :, 0].astype(int)
    g = rgb[:, :, 1].astype(int)
    b = rgb[:, :, 2].astype(int)

    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    sat = maxc - minc
    brightness = (r + g + b) / 3.0

    is_colored = (sat >= 50) & (brightness >= 70)

    green = is_colored & (g > r + 30) & (g > b + 30)

    purple = is_colored & ~green & (b >= 100) & (r >= 90) & \
             (r > g + 30) & (b > g + 30) & (np.abs(r - b) < 80)

    blue = is_colored & ~green & ~purple & (b >= 90) & \
           (b > r + 30) & (b > g + 20)

    gold = is_colored & ~green & ~purple & (r >= 160) & (g >= 130) & \
           (b < 130) & (r > b + 40) & (g > b + 30)

    red = is_colored & ~green & ~purple & (r >= 160) & \
          (r > g + 70) & (r > b + 70)

    white = (brightness >= 120) & (sat < 30)

    dark = brightness < 60

    return {
        "blue": blue, "purple": purple, "gold": gold,
        "red": red, "white": white, "dark": dark,
        "green": green,
    }


def _row_jump_count(row_rgb, threshold=40):
    if row_rgb.shape[0] < 2:
        return 0
    diffs = np.abs(np.diff(row_rgb.astype(int), axis=0)).sum(axis=1)
    return int((diffs > threshold).sum())


def _region_is_visible(core, masks, max_colors=8,
                       min_smooth_ratio=0.50, max_row_jumps=3,
                       white_min=5, edge_min=1):
    h, w = core.shape[:2]
    if h < 3 or w < 8:
        return False, {"unique_colors": 0, "smooth_ratio": 0.0,
                       "white": 0, "edges": 0, "reason": "too_small"}

    quant = (core // 32) * 32
    unique_cnt = len(np.unique(quant.reshape(-1, 3), axis=0))
    colors_ok = unique_cnt <= max_colors

    cy1 = int(h * 0.20)
    cy2 = int(h * 0.80)
    if cy2 - cy1 < 2:
        cy1, cy2 = 0, h
    rows = core[cy1:cy2]
    smooth = 0
    for row in rows:
        if _row_jump_count(row, threshold=40) <= max_row_jumps:
            smooth += 1
    smooth_ratio = smooth / max(1, len(rows))
    smooth_ok = smooth_ratio >= min_smooth_ratio

    white_cnt = int(masks["white"].sum())
    white_ok = white_cnt >= white_min

    gray = core.mean(axis=2)
    if gray.shape[0] >= 2:
        v_grad = np.abs(np.diff(gray, axis=0))
        row_edge = (v_grad > 25).mean(axis=1)
        strong_edges = int((row_edge > 0.4).sum())
    else:
        strong_edges = 0
    bar_ok = strong_edges >= edge_min

    visible = white_ok or bar_ok or (colors_ok and smooth_ok)
    reason = "white" if white_ok else ("bar" if bar_ok else (
        "clean" if colors_ok and smooth_ok else "none"))

    return visible, {
        "unique_colors": unique_cnt,
        "smooth_ratio": round(smooth_ratio, 2),
        "white": white_cnt,
        "edges": strong_edges,
        "reason": reason,
    }


def _trim_to_colored_rows(color_mask):
    """用彩色像素的行范围裁切，去掉上下留白，只保留护甲条实际高度。"""
    row_any = color_mask.any(axis=1)
    if not row_any.any():
        return color_mask
    rows = np.where(row_any)[0]
    r1, r2 = int(rows[0]), int(rows[-1]) + 1
    if r2 - r1 < 2:
        return color_mask
    return color_mask[r1:r2, :]


def _grid_fill_ratio(color_mask, n_cells):
    """
    把护甲区域水平等分为 n_cells 格，逐格统计彩色像素占比。
    - >= CELL_FULL_RATIO → 满格
    - >= CELL_HALF_RATIO → 半格
    - 否则 → 空格
    返回 (百分比, 每格填充比例列表)
    """
    h, w = color_mask.shape
    if w < n_cells * 3 or h < 2:
        return 0.0, [0.0] * n_cells

    cell_w = w / n_cells
    full = 0
    half = 0
    details = []

    for i in range(n_cells):
        x1 = int(round(i * cell_w))
        x2 = int(round((i + 1) * cell_w))
        if x2 <= x1:
            x2 = x1 + 1
        x2 = min(x2, w)
        cell = color_mask[:, x1:x2]
        area = cell.size
        if area == 0:
            details.append(0.0)
            continue
        ratio = float(cell.sum()) / area
        details.append(round(ratio, 3))
        if ratio >= CELL_FULL_RATIO:
            full += 1
        elif ratio >= CELL_HALF_RATIO:
            half += 1

    filled = full + half * 0.5
    pct = filled / n_cells * 100.0
    return pct, details


def analyze_armor(region):
    """
    按格子识别护甲：
    - 先识别颜色（蓝/紫/金/红）
    - 根据颜色确定总格数（3/4/5/6）
    - 逐格判断满格/半格/空格
    - 百分比 = (满格 + 半格×0.5) / 总格数 × 100
    """
    rgb = capture(region)
    if rgb is None:
        return {"color": None, "ratio": 0.0, "has_colored": False,
                "valid": False, "debug": {}}

    try:
        core = _trim_vertical(rgb)
        masks = _color_masks(core)
        total = core.shape[0] * core.shape[1]

        counts = {
            "blue": int(masks["blue"].sum()),
            "purple": int(masks["purple"].sum()),
            "gold": int(masks["gold"].sum()),
            "red": int(masks["red"].sum()),
        }

        min_pixels = max(5, int(total * ARMOR_MIN_COLOR_RATIO))
        best_color = max(counts, key=counts.get)
        best_count = counts[best_color]

        base_dbg = {
            "counts": counts,
            "total": total,
            "best": best_color,
            "best_count": best_count,
            "min_pixels": min_pixels,
        }

        if best_count < min_pixels:
            _A.append(0.0)
            return {
                "color": None,
                "ratio": 0.0,
                "has_colored": False,
                "valid": False,
                "debug": {**base_dbg, "n_cells": 0, "details": []},
            }

        color_mask = masks[best_color]
        trimmed = _trim_to_colored_rows(color_mask)
        n_cells = ARMOR_GRID[best_color]
        pct, details = _grid_fill_ratio(trimmed, n_cells)

        _A.append(pct)
        return {
            "color": best_color,
            "ratio": float(np.median(_A)),
            "has_colored": True,
            "valid": True,
            "debug": {**base_dbg, "n_cells": n_cells, "details": details},
        }
    except Exception:
        return {"color": None, "ratio": 0.0, "has_colored": False,
                "valid": False, "debug": {}}


def analyze_health(region):
    rgb = capture(region)
    if rgb is None:
        return {"ratio": -1.0, "valid": False, "debug": {}}

    try:
        core = _trim_vertical(rgb)
        masks = _color_masks(core)
        valid, dbg = _region_is_visible(core, masks)
        ratio = _column_fill_ratio(masks["white"], min_ratio=0.20)

        if not valid:
            return {"ratio": ratio, "valid": False, "debug": dbg}

        _H.append(ratio)
        return {"ratio": float(np.median(_H)), "valid": True, "debug": dbg}
    except Exception:
        return {"ratio": -1.0, "valid": False, "debug": {}}


def reset_history():
    _H.clear()
    _A.clear()


def debug_armor_colors(region):
    rgb = capture(region)
    if rgb is None:
        return {"error": "capture_failed"}
    core = _trim_vertical(rgb)
    masks = _color_masks(core)
    total = core.shape[0] * core.shape[1]
    counts = {k: int(v.sum()) for k, v in masks.items()}
    counts["total"] = total
    return counts


def debug_health_visible(region):
    rgb = capture(region)
    if rgb is None:
        return {"error": "capture_failed"}
    core = _trim_vertical(rgb)
    masks = _color_masks(core)
    valid, dbg = _region_is_visible(core, masks)
    return {"valid": valid, **dbg}