# debug_vision.py
import json
import os
import numpy as np
from PIL import Image

import vision

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)


def save_debug(name, region, core, mask, ratio, col_min_ratio=0.3):
    if core is None:
        print(f"[{name}] 区域读取失败，检查游戏是否开着、坐标是否正确")
        return

    # 保存原始图和掩码图
    Image.fromarray(core.astype(np.uint8)).save(f"debug_{name}_原始.png")
    mask_img = (mask.astype(np.uint8)) * 255
    Image.fromarray(mask_img).save(f"debug_{name}_掩码.png")

    # 打印逐列填充比例（每 10 列取一个点，避免刷屏）
    col_ratio = mask.mean(axis=0)
    step = max(1, len(col_ratio) // 30)
    sampled = col_ratio[::step]
    print(f"\n[{name}] 逐列填充比例（左→右，每 {step} 列采样一次）：")
    print(np.round(sampled, 2))
    print(f"[{name}] 当前识别值: {ratio:.1f}%")
    print(f"[{name}] 判定阈值: 每列 >= {col_min_ratio:.2f} 才算填充")

    # 找“断点”：理论上的连续填充应该在哪个位置断掉
    col_filled = col_ratio >= col_min_ratio
    if col_filled.any():
        last = np.where(col_filled)[0].max()
        gaps = []
        for i in range(last):
            if not col_filled[i]:
                gaps.append(i)
        if gaps:
            print(f"[{name}] 填充区中间的“空洞”列号（前 10 个）：{gaps[:10]}")
    print(f"[{name}] 已保存 debug_{name}_原始.png 和 debug_{name}_掩码.png\n")


print("=" * 50)
print("诊断血条识别")
print("=" * 50)
core_h, mask_h = vision.health_mask(cfg["HEALTH_REGION"])
save_debug("血量", cfg["HEALTH_REGION"], core_h, mask_h,
           vision.health_ratio(cfg["HEALTH_REGION"]))

print("=" * 50)
print("诊断护甲识别")
print("=" * 50)
core_a, mask_a = vision.armor_mask(cfg["ARMOR_REGION"])
save_debug("护甲", cfg["ARMOR_REGION"], core_a, mask_a,
           vision.armor_ratio(cfg["ARMOR_REGION"]))

print("请用图片查看器打开这几个 debug_*.png 文件：")
print("  1. debug_血量_原始.png —— 实际截到的画面")
print("  2. debug_血量_掩码.png —— 白色部分是被判定为“有血”的像素")
print("  3. debug_护甲_原始.png")
print("  4. debug_护甲_掩码.png")
print()
print("判断标准：")
print("  - 掩码图里白色区域应该是一条连续的横条，右端停在血量真实位置")
print("  - 如果白色区域包含大量背景或文字 → 阈值需要收紧")
print("  - 如果白色区域严重缺失（横条有很多洞）→ 阈值需要放宽")