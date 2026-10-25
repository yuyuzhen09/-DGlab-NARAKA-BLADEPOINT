# debug_live.py
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import json
import os
import tkinter as tk
from PIL import Image, ImageTk
import numpy as np

import vision

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_REGION = [120, 850, 480, 910]


def load_region():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if "region" in cfg and isinstance(cfg["region"], list):
                print(f"已读取区域: {cfg['region']}")
                return cfg["region"]
        except Exception as e:
            print(f"读取失败: {e}")
    print(f"使用默认区域: {DEFAULT_REGION}")
    return DEFAULT_REGION


def to_photo(rgb, mask=None, max_w=650):
    if rgb is None or rgb.size == 0:
        return None
    img = rgb.astype(np.uint8).copy()
    if mask is not None:
        overlay = img.copy()
        overlay[mask] = [255, 0, 0]
        img = (img * 0.4 + overlay * 0.6).astype(np.uint8)
    h, w = img.shape[:2]
    if w < 1:
        return None
    scale = max(1, max_w // max(w, 1))
    pil = Image.fromarray(img).resize((w * scale, h * scale), Image.NEAREST)
    return ImageTk.PhotoImage(pil)


class Debug:
    def __init__(self, root, region):
        self.root = root
        self.region = region

        root.title("DGLab 实时诊断")
        root.attributes("-topmost", True)
        root.configure(bg="#111111")

        tk.Label(root, text=f"区域: {region}", font=("Microsoft YaHei", 10),
                 bg="#111111", fg="#cccccc").pack(pady=(10, 4))

        tk.Label(root, text="完整截图（绿线=自动分割）", font=("Microsoft YaHei", 10),
                 bg="#111111", fg="#aaaaaa").pack()
        self.full_img = tk.Label(root, bg="#111111", text="(无画面)", fg="#666666")
        self.full_img.pack(padx=10)

        tk.Label(root, text="护甲区域（红=判定为护甲）", font=("Microsoft YaHei", 10),
                 bg="#111111", fg="#55aaff").pack(pady=(10, 2))
        self.ar_img = tk.Label(root, bg="#111111", text="(无画面)", fg="#666666")
        self.ar_img.pack(padx=10)
        self.ar_pct = tk.Label(root, text="护甲: --.-%", font=("Consolas", 14, "bold"),
                               bg="#111111", fg="#55aaff")
        self.ar_pct.pack(pady=2)

        tk.Label(root, text="血条区域（红=判定为血量）", font=("Microsoft YaHei", 10),
                 bg="#111111", fg="#ff5555").pack(pady=(10, 2))
        self.hp_img = tk.Label(root, bg="#111111", text="(无画面)", fg="#666666")
        self.hp_img.pack(padx=10)
        self.hp_pct = tk.Label(root, text="血量: --.-%", font=("Consolas", 14, "bold"),
                               bg="#111111", fg="#ff5555")
        self.hp_pct.pack(pady=2)

        self.color_label = tk.Label(root, text="护甲类型: --", font=("Microsoft YaHei", 12),
                                    bg="#111111", fg="#ffaa55")
        self.color_label.pack(pady=(8, 4))

        root.bind("<Escape>", lambda e: root.destroy())
        self.update()

    def update(self):
        try:
            info = vision.debug_image(self.region)
            if info is None or "rgb" not in info:
                self.full_img.config(text="截图失败")
                self.root.after(300, self.update)
                return

            rgb = info["rgb"]
            photo = to_photo(rgb, None, max_w=640)
            if photo:
                self.full_img.config(image=photo, text="")
                self.full_img.image = photo

            if "armor_rgb" in info:
                a_photo = to_photo(info["armor_rgb"], info["armor_mask"], max_w=640)
                if a_photo:
                    self.ar_img.config(image=a_photo, text="")
                    self.ar_img.image = a_photo

                h_photo = to_photo(info["health_rgb"], info["hp_mask"], max_w=640)
                if h_photo:
                    self.hp_img.config(image=h_photo, text="")
                    self.hp_img.image = h_photo

                color = info.get("armor_color")
                COLOR_CN = {"blue": "蓝甲", "purple": "紫甲", "gold": "金甲",
                            "red": "红甲", None: "无甲/未知"}
                self.color_label.config(text=f"护甲类型: {COLOR_CN.get(color, '未知')}")

            result = vision.analyze(self.region)
            self.ar_pct.config(text=f"护甲: {result.get('armor', -1):5.1f}%")
            self.hp_pct.config(text=f"血量: {result.get('hp', -1):5.1f}%")
        except Exception as e:
            self.color_label.config(text=f"异常: {e}", fg="#ff5555")

        self.root.after(300, self.update)


if __name__ == "__main__":
    print("=" * 50)
    print("DGLab 实时诊断")
    print("=" * 50)

    region = load_region()
    print(f"区域: {region}")
    print("窗口即将打开...")

    root = tk.Tk()
    app = Debug(root, region)
