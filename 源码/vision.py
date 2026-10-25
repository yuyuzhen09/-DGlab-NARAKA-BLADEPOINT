# vision.py
import numpy as np
import mss

try:
    import dxcam
    _camera = dxcam.create(output_color="RGB")
except ImportError:
    dxcam = None
    _camera = None
except Exception:
    dxcam = None
    _camera = None

TARGET_COLORS = {
    "blue":   (104, 143, 255),
    "purple": (193, 123, 250),
    "gold":   (246, 193, 67),
    "red":    (255, 70, 70),
}

# --- 防跳变状态机 ---
_last_armor_pct = -1.0
_last_color = None
_pending_pct = -1.0
_pending_count = 0
_lock = threading.Lock() if 'threading' in globals() else None

import threading
_lock = threading.Lock()

def update_target_colors(new_colors):
    global TARGET_COLORS
    TARGET_COLORS.update(new_colors)

def capture(region):
    x1, y1, x2, y2 = region
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0: return None
    
    if _camera is not None:
        try:
            frame = _camera.grab(region=(x1, y1, x2, y2))
            if frame is not None and frame.size > 0:
                return frame
        except Exception: pass

    try:
        with mss.mss() as sct:
            s = np.array(sct.grab({"left": x1, "top": y1, "width": w, "height": h}))
        return s[:, :, :3]
    except Exception:
        return None

def _trim_vertical(rgb, top=0.10, bottom=0.90):
    h = rgb.shape[0]
    t, b = int(h * top), int(h * bottom)
    return rgb[t:b, :, :] if b - t >= 1 else rgb

def _column_fill_ratio(mask, min_ratio=0.20):
    col_ratio = mask.mean(axis=0)
    col_filled = col_ratio >= min_ratio
    w = len(col_filled)
    if w == 0: return 0.0
    run = 0
    for i in range(w - 1, -1, -1):
        if col_filled[i]:
            run += 1
            if run >= 2: return (i + run) / w * 100.0
        else: run = 0
    return 0.0

def _color_masks(rgb):
    r, g, b = rgb[:, :, 0].astype(int), rgb[:, :, 1].astype(int), rgb[:, :, 2].astype(int)
    brightness = (r + g + b) / 3.0
    
    masks = {}
    for name, (tr, tg, tb) in TARGET_COLORS.items():
        dist = np.sqrt((r - tr)**2 + (g - tg)**2 + (b - tb)**2)
        # 限制亮度，排除极暗或极亮的高光
        masks[name] = (dist < 75) & (brightness > 60) & (brightness < 240)
        
    # 收紧广域特征，提高识别门槛防高光误判
    blue_feat = (b >= 110) & (b > r + 25) & (b > g + 15) & (brightness > 70) & (brightness < 240)
    purple_feat = (b >= 100) & (r >= 80) & (np.abs(r - b) < 110) & (g < r - 10) & (g < b - 10) & (brightness > 70) & (brightness < 240)
    gold_feat = (r >= 120) & (g >= 90) & (b < 150) & (r > b + 25) & (g > b + 15) & (brightness > 80) & (brightness < 240)
    red_feat = (r >= 130) & (r > g + 35) & (r > b + 35) & (brightness > 60) & (brightness < 240)
    
    masks["blue"] = masks["blue"] | blue_feat
    masks["purple"] = masks["purple"] | purple_feat
    masks["gold"] = masks["gold"] | gold_feat
    masks["red"] = masks["red"] | red_feat

    white = (r > 120) & (g > 120) & (b > 120) & (np.abs(r - g) < 50) & (np.abs(g - b) < 50)
    masks["white"] = white
    return masks

def _region_is_visible(core, masks):
    return int(masks["white"].sum()) >= 5

def analyze_armor(region):
    global _last_armor_pct, _last_color, _pending_pct, _pending_count
    
    rgb = capture(region)
    if rgb is None: return {"color": None, "ratio": 0.0, "has_colored": False, "valid": False, "debug": {}}
    
    try:
        # 【核心修复】如果整张截图亮度极高（屏幕受击闪白），直接跳过这一帧，保留上次的值
        if rgb.mean() > 230:
            if _last_color:
                return {"color": _last_color, "ratio": _last_armor_pct, "has_colored": True, "valid": True, "debug": {"skip": "flash"}}
            return {"color": None, "ratio": 0.0, "has_colored": False, "valid": False, "debug": {}}

        core = _trim_vertical(rgb)
        masks = _color_masks(core)
        
        counts = {k: int(masks[k].sum()) for k in ("blue", "purple", "gold", "red")}
        best_color = max(counts, key=counts.get)
        best_count = counts[best_color]
        
        dbg = {"counts": counts}
        
        if best_count < 5:
            _last_armor_pct = 0.0
            _last_color = None
            return {"color": None, "ratio": 0.0, "has_colored": False, "valid": False, "debug": dbg}
            
        color_mask = masks[best_color]
        pct = _column_fill_ratio(color_mask, min_ratio=0.20)
        
        # 【核心修复】防跳变逻辑
        with _lock:
            if _last_armor_pct < 0:
                # 第一次识别
                _last_armor_pct = pct
                _last_color = best_color
            else:
                if pct < _last_armor_pct - 2:
                    # 掉甲了，瞬间更新，绝不延迟
                    _last_armor_pct = pct
                    _last_color = best_color
                    _pending_pct = -1
                    _pending_count = 0
                elif pct > _last_armor_pct + 15:
                    # 瞬间暴涨 15% 以上（比如受击红光、特效），判定为假象
                    # 需要连续 5 帧（0.25秒）都保持高值，才认为是真吃药回甲
                    if _pending_pct != -1 and abs(pct - _pending_pct) < 10:
                        _pending_count += 1
                    else:
                        _pending_pct = pct
                        _pending_count = 1
                    
                    if _pending_count >= 5:
                        _last_armor_pct = pct
                        _last_color = best_color
                        _pending_pct = -1
                        _pending_count = 0
                    else:
                        # 拦截反弹，维持上一帧的掉甲状态
                        pct = _last_armor_pct
                else:
                    # 正常波动范围，直接更新
                    _last_armor_pct = pct
                    _last_color = best_color
                    _pending_pct = -1
                    _pending_count = 0

        n_cells = {"blue": 3, "purple": 4, "gold": 5, "red": 6}[best_color]
        dbg["n_cells"] = n_cells
        
        return {"color": _last_color, "ratio": pct, "has_colored": True, "valid": True, "debug": dbg}
    except Exception as e:
        return {"color": None, "ratio": 0.0, "has_colored": False, "valid": False, "debug": {"error": str(e)}}

def analyze_health(region):
    rgb = capture(region)
    if rgb is None: return {"ratio": -1.0, "valid": False, "debug": {}}
    try:
        core = _trim_vertical(rgb)
        masks = _color_masks(core)
        valid = _region_is_visible(core, masks)
        ratio = _column_fill_ratio(masks["white"], min_ratio=0.15)
        if not valid: return {"ratio": ratio, "valid": False, "debug": {"white": int(masks["white"].sum())}}
        return {"ratio": ratio, "valid": True, "debug": {}}
    except Exception:
        return {"ratio": -1.0, "valid": False, "debug": {}}

def reset_history():
    global _last_armor_pct, _last_color, _pending_pct, _pending_count
    with _lock:
        _last_armor_pct = -1.0
        _last_color = None
        _pending_pct = -1.0
        _pending_count = 0