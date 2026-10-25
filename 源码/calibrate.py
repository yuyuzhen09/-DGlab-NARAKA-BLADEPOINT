# calibrate.py
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
from tkinter import messagebox
from PIL import ImageGrab, ImageTk
import numpy as np

import vision

REGIONS = []
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class Calibrator:
    def __init__(self, root):
        self.root = root
        self.root.title("DGLab 区域校准")
        self.root.withdraw()  # 关键：先隐藏窗口，避免截到自己

        self.sw = root.winfo_screenwidth()
        self.sh = root.winfo_screenheight()

        self.canvas = tk.Canvas(root, width=self.sw, height=self.sh,
                                bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.photo = None
        self.start = None
        self.rect = None
        self.transparent_mode = False

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.on_right_click)  # 右键撤销
        self.root.bind("<Escape>", self.finish)

        # 延迟 400ms，等窗口彻底隐藏后再截图
        self.root.after(400, self.show_window)

    def show_window(self):
        photo = None
        mostly_black = False

        try:
            img = ImageGrab.grab()
            if img.size != (self.sw, self.sh):
                img = img.resize((self.sw, self.sh))
            # 快速检查是否全黑：缩小到 80x45 再算平均亮度
            small = img.resize((80, 45))
            arr = np.array(small)
            if arr.mean() < 5:
                mostly_black = True
                print("检测到截图全黑（游戏可能是全屏独占），切换为半透明模式。")
            else:
                photo = ImageTk.PhotoImage(img)
        except Exception as e:
            print(f"截图失败: {e}")
            mostly_black = True

        self.root.deiconify()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)

        if photo is not None and not mostly_black:
            self.photo = photo
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
            self.root.attributes("-alpha", 1.0)
            self.transparent_mode = False
        else:
            # 半透明模式：用户透过窗口看到下面的游戏
            self.root.attributes("-alpha", 0.55)
            self.transparent_mode = True

        self.draw_hint()

    def draw_hint(self):
        hint = (
            "先拖拽画【血条】，再拖拽画【护甲条】。\n"
            "右键撤销上一个框，Esc 保存并进入实时预览。"
        )
        if self.transparent_mode:
            hint += "\n（截图全黑，已切换半透明模式。建议游戏改“无边框窗口”。）"

        self.canvas.create_rectangle(10, 10, 780, 130, fill="black",
                                     outline="yellow", width=2)
        self.canvas.create_text(
            20, 20, anchor="nw", fill="yellow",
            font=("Microsoft YaHei", 13, "bold"),
            text=hint, width=750
        )

    def on_press(self, event):
        self.start = (event.x, event.y)
        self.rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="lime", width=3)

    def on_drag(self, event):
        if self.rect and self.start:
            self.canvas.coords(self.rect, self.start[0], self.start[1],
                               event.x, event.y)

    def on_release(self, event):
        if self.start is None:
            return
        x1 = min(self.start[0], event.x)
        y1 = min(self.start[1], event.y)
        x2 = max(self.start[0], event.x)
        y2 = max(self.start[1], event.y)
        self.start = None
        self.rect = None
        if x2 - x1 < 5 or y2 - y1 < 5:
            return
        REGIONS.append((x1, y1, x2, y2))
        self.redraw_boxes()

    def on_right_click(self, event):
        if REGIONS:
            REGIONS.pop()
            self.redraw_boxes()

    def redraw_boxes(self):
        self.canvas.delete("box")
        for i, r in enumerate(REGIONS):
            color = "lime" if i == 0 else "cyan"
            label = "血条" if i == 0 else "护甲"
            self.canvas.create_rectangle(*r, outline=color, width=3, tags="box")
            self.canvas.create_text(
                r[0], max(r[1] - 6, 2), anchor="sw", fill=color,
                font=("Microsoft YaHei", 12, "bold"),
                text=f"{label} {r[2]-r[0]}x{r[3]-r[1]}", tags="box"
            )

    def finish(self, event=None):
        if len(REGIONS) < 2:
            messagebox.showwarning("未完成", "需要画两个区域：先血条，再护甲条。右键可撤销。")
            return

        config = {
            "HEALTH_REGION": list(REGIONS[0]),
            "ARMOR_REGION": list(REGIONS[1]),
            "BASE_INTENSITY": 1,
            "MAX_INTENSITY": 5,
            "INTENSITY_PER_GAP": 0.15,
            "FULL_THRESHOLD": 99.0,
            "CHECK_INTERVAL": 0.2,
            "HOLD_PULSE_TIME": 1.0,
            "CHANNEL": "A",
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print(f"已保存配置：{CONFIG_PATH}")
        print(f"血条区域: {REGIONS[0]}")
        print(f"护甲区域: {REGIONS[1]}")

        self.canvas.destroy()
        self.root.attributes("-fullscreen", False)
        self.root.attributes("-alpha", 1.0)
        self.root.geometry("460x220+60+60")
        self.root.attributes("-topmost", True)

        self.label = tk.Label(
            self.root, text="读取中...", font=("Consolas", 16),
            justify="left", anchor="nw", bg="black", fg="lime"
        )
        self.label.pack(fill="both", expand=True, padx=10, pady=10)
        self.update_live()

    def update_live(self):
        hp = vision.health_ratio(REGIONS[0])
        ar = vision.armor_ratio(REGIONS[1])
        self.label.config(text=(
            f"血量: {hp:6.1f}%\n"
            f"护甲: {ar:6.1f}%\n\n"
            f"进游戏受伤/掉甲，看数字是否变化。\n"
            f"关闭窗口结束。"
        ))
        self.root.after(150, self.update_live)


if __name__ == "__main__":
    print("=== DGLab 区域校准 ===")
    print("1. 全屏窗口出现后，先画血条，再画护甲条")
    print("2. 右键可撤销上一个框")
    print("3. 按 Esc 保存并进入实时预览\n")

    root = tk.Tk()
    Calibrator(root)
    root.mainloop()