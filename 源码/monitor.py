# monitor.py
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import json
import os
import tkinter as tk

import vision

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"找不到 {CONFIG_PATH}。请先运行一次 python calibrate.py 完成框选。"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class Monitor:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg

        self.root.title("DGLab 实时监控")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.85)
        self.root.configure(bg="#111111")
        self.root.geometry("300x150+40+40")
        self.root.resizable(False, False)

        self.hp_label = tk.Label(
            root, text="血量: --.-%", font=("Consolas", 22, "bold"),
            bg="#111111", fg="#ff5555", anchor="w"
        )
        self.hp_label.pack(fill="x", padx=14, pady=(14, 4))

        self.ar_label = tk.Label(
            root, text="护甲: --.-%", font=("Consolas", 22, "bold"),
            bg="#111111", fg="#55aaff", anchor="w"
        )
        self.ar_label.pack(fill="x", padx=14, pady=4)

        self.status_label = tk.Label(
            root, text="实时识别中...", font=("Microsoft YaHei", 10),
            bg="#111111", fg="#888888", anchor="w"
        )
        self.status_label.pack(fill="x", padx=14, pady=(8, 12))

        # 拖动窗口
        for widget in (self.hp_label, self.ar_label, self.status_label):
            widget.bind("<ButtonPress-1>", self.start_move)
            widget.bind("<B1-Motion>", self.on_move)
        self._drag_x = 0
        self._drag_y = 0

        self.root.bind("<Escape>", lambda e: root.destroy())
        self.update()

    def start_move(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def on_move(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def update(self):
        hp = vision.health_ratio(self.cfg["HEALTH_REGION"])
        ar = vision.armor_ratio(self.cfg["ARMOR_REGION"])

        if hp < 0:
            self.hp_label.config(text="血量: 读取失败", fg="#888888")
        else:
            color = "#55ff55" if hp >= self.cfg["FULL_THRESHOLD"] else "#ff5555"
            self.hp_label.config(text=f"血量: {hp:5.1f}%", fg=color)

        if ar < 0:
            self.ar_label.config(text="护甲: 读取失败", fg="#888888")
        else:
            color = "#55ff55" if ar >= self.cfg["FULL_THRESHOLD"] else "#55aaff"
            self.ar_label.config(text=f"护甲: {ar:5.1f}%", fg=color)

        full = (hp >= self.cfg["FULL_THRESHOLD"]) and (ar >= self.cfg["FULL_THRESHOLD"])
        self.status_label.config(
            text="满血满甲（安全）" if full else "血/甲不满（会持续输出）",
            fg="#55ff55" if full else "#ffaa55"
        )

        self.root.after(200, self.update)


if __name__ == "__main__":
    print("=== DGLab 实时监控 ===")
    print("Esc 关闭窗口。可拖动窗口到任意位置。\n")
    cfg = load_config()
    root = tk.Tk()
    Monitor(root, cfg)
    root.mainloop()