# app.py
import asyncio
import json
import os
import sys
import threading
import time
import tkinter as tk

import qrcode
from PIL import ImageTk
from pydglab_ws import DGLabWSConnect
from pydglab_ws.server import DGLabWSServer

import vision

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
WS_PORT = 5678

PULSE_DATA = [
    ((10, 10, 10, 10), (0, 0, 0, 0)),
    ((10, 10, 10, 10), (0, 5, 10, 20)),
    ((10, 10, 10, 10), (20, 25, 30, 40)),
    ((10, 10, 10, 10), (40, 45, 50, 60)),
    ((10, 10, 10, 10), (60, 65, 70, 80)),
    ((10, 10, 10, 10), (80, 90, 100, 100)),
    ((10, 10, 10, 10), (80, 90, 100, 100)),
    ((0, 0, 0, 0), (0, 0, 0, 0)),
    ((0, 0, 0, 0), (0, 0, 0, 0)),
]

LOCK = threading.Lock()

STATE = {
    "armor_region": None,
    "health_region": None,
    "hp": 0.0,
    "ar": 0.0,
    "ar_color": None,
    "has_colored": False,
    "health_visible": False,
    "health_debug": {},
    "armor_debug": {},
    "bound": False,
    "running": False,
    "status": "启动中...",
    "qr_url": None,
    "shocking": False,
    "client": None,
    "current_intensity": 0,
    "triggered_by": "",
}

PARAMS = {
    "armor_threshold": 30.0,
    "armor_intensity": 50,
    "health_threshold": 98.0,
    "health_intensity": 70,
    "mode": "continuous",
    "interval_sec": 3.0,
    "channel": "A",
}

COLOR_CN = {
    "blue": "蓝甲",
    "purple": "紫甲",
    "gold": "金甲",
    "red": "红甲",
    None: "",
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        if "armor_region" in cfg and isinstance(cfg["armor_region"], list) \
                and len(cfg["armor_region"]) == 4:
            STATE["armor_region"] = tuple(cfg["armor_region"])
        if "health_region" in cfg and isinstance(cfg["health_region"], list) \
                and len(cfg["health_region"]) == 4:
            STATE["health_region"] = tuple(cfg["health_region"])

        for key in ("armor_threshold", "armor_intensity",
                    "health_threshold", "health_intensity"):
            if key in cfg:
                PARAMS[key] = cfg[key]
        if "mode" in cfg:
            PARAMS["mode"] = cfg["mode"]
        if "interval_sec" in cfg:
            PARAMS["interval_sec"] = cfg["interval_sec"]

        return (STATE["armor_region"] is not None and
                STATE["health_region"] is not None)
    except Exception as e:
        print(f"[配置读取失败] {e}")
        return False


def save_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    with LOCK:
        if STATE["armor_region"]:
            cfg["armor_region"] = list(STATE["armor_region"])
        if STATE["health_region"]:
            cfg["health_region"] = list(STATE["health_region"])
        cfg["armor_threshold"] = PARAMS["armor_threshold"]
        cfg["armor_intensity"] = PARAMS["armor_intensity"]
        cfg["health_threshold"] = PARAMS["health_threshold"]
        cfg["health_intensity"] = PARAMS["health_intensity"]
        cfg["mode"] = PARAMS["mode"]
        cfg["interval_sec"] = PARAMS["interval_sec"]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def compute_trigger(hp, ar, has_colored, hp_thr, ar_thr, hp_int, ar_int):
    reasons = []
    intensities = []

    if has_colored and ar < ar_thr:
        reasons.append("护甲")
        intensities.append(int(round(ar_int)))

    if hp < hp_thr:
        reasons.append("血量")
        intensities.append(int(round(hp_int)))

    if not reasons:
        return False, 0, ""
    return True, max(intensities), "+".join(reasons)


async def stop_output(client, ch):
    try:
        await client.set_strength(ch, 0)
    except Exception as e:
        print(f"[停止输出失败] {e}")
    with LOCK:
        STATE["shocking"] = False
        STATE["current_intensity"] = 0
        STATE["triggered_by"] = ""
        STATE["status"] = "监测中"


async def monitor_loop():
    last_pulse = 0.0
    while True:
        with LOCK:
            running = STATE["running"]
            ar_region = STATE["armor_region"]
            hp_region = STATE["health_region"]
            bound = STATE["bound"]
            client = STATE["client"]
            ar_thr = PARAMS["armor_threshold"]
            ar_int = PARAMS["armor_intensity"]
            hp_thr = PARAMS["health_threshold"]
            hp_int = PARAMS["health_intensity"]
            mode = PARAMS["mode"]
            interval = PARAMS["interval_sec"]
            ch = PARAMS["channel"]

        if not running or ar_region is None or hp_region is None:
            await asyncio.sleep(0.2)
            continue

        health_res = vision.analyze_health(hp_region)
        hp = health_res.get("ratio", -1)
        health_visible = health_res.get("valid", False)
        health_debug = health_res.get("debug", {})

        armor_res = vision.analyze_armor(ar_region)
        ar = armor_res.get("ratio", 0)
        ar_color = armor_res.get("color")
        has_colored = armor_res.get("has_colored", False)
        armor_debug = armor_res.get("debug", {})

        with LOCK:
            STATE["hp"] = hp
            STATE["ar"] = ar
            STATE["ar_color"] = ar_color
            STATE["has_colored"] = has_colored
            STATE["health_visible"] = health_visible
            STATE["health_debug"] = health_debug
            STATE["armor_debug"] = armor_debug

        if not health_visible:
            if STATE["shocking"] and bound and client is not None:
                await stop_output(client, ch)
            elif not bound:
                with LOCK:
                    STATE["current_intensity"] = 0
                    STATE["triggered_by"] = ""
                    STATE["status"] = "监测中"
            await asyncio.sleep(0.2)
            continue

        if hp < 0:
            await asyncio.sleep(0.2)
            continue

        triggered, intensity, triggered_by = compute_trigger(
            hp, ar, has_colored, hp_thr, ar_thr, hp_int, ar_int
        )

        with LOCK:
            STATE["current_intensity"] = intensity if triggered else 0
            STATE["triggered_by"] = triggered_by if triggered else ""

        if not bound or client is None:
            with LOCK:
                if STATE["status"].startswith(("持续电击", "间隔电击")):
                    STATE["status"] = "监测中"
            await asyncio.sleep(0.2)
            continue

        now = time.monotonic()

        try:
            if triggered:
                if mode == "continuous":
                    if now - last_pulse >= 0.8:
                        await client.send_pulse(ch, PULSE_DATA, time=1.0)
                        await client.set_strength(ch, intensity)
                        with LOCK:
                            STATE["shocking"] = True
                            STATE["status"] = "监测中"
                        last_pulse = now
                else:
                    if now - last_pulse >= interval:
                        await client.send_pulse(ch, PULSE_DATA, time=1.0)
                        await client.set_strength(ch, intensity)
                        with LOCK:
                            STATE["shocking"] = True
                            STATE["status"] = "监测中"
                        last_pulse = now
            else:
                if STATE["shocking"]:
                    await client.set_strength(ch, 0)
                    with LOCK:
                        STATE["shocking"] = False
                        STATE["status"] = "监测中"
        except Exception as e:
            with LOCK:
                STATE["status"] = f"发送失败: {e}"

        await asyncio.sleep(0.2)


async def dglab_worker():
    try:
        async with DGLabWSServer("0.0.0.0", WS_PORT, 60):
            async with DGLabWSConnect(f"ws://127.0.0.1:{WS_PORT}") as client:
                qr_url = client.get_qrcode()
                with LOCK:
                    STATE["qr_url"] = qr_url
                    if STATE["status"] == "启动中...":
                        STATE["status"] = "二维码已就绪"
                print(f"[服务端] 二维码 URL: {qr_url}")

                bind_task = asyncio.create_task(client.bind())
                monitor_task = asyncio.create_task(monitor_loop())

                try:
                    await bind_task
                    with LOCK:
                        STATE["bound"] = True
                        STATE["client"] = client
                        STATE["status"] = "监测中"
                    print(f"✅ 已绑定 App: {client.target_id}")
                except Exception as e:
                    print(f"[绑定失败] {e}")
                    with LOCK:
                        STATE["status"] = f"绑定失败: {e}"

                await monitor_task
    except Exception as e:
        with LOCK:
            STATE["status"] = f"后台异常: {type(e).__name__}: {e}"
        print(f"[后台异常] {type(e).__name__}: {e}")


def run_async():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(dglab_worker())
    except Exception as e:
        print(f"[线程异常] {e}")


class App:
    def __init__(self, root):
        self.root = root
        root.title("DGLab 屏幕联动控制台")
        root.geometry("560x960+60+40")
        root.configure(bg="#1e1e1e")
        # 主窗口不置顶（已移除 root.attributes("-topmost", True)）

        self._qr_window = None
        self._tip_after_id = None

        self.status_label = tk.Label(
            root, text="启动中...", font=("Microsoft YaHei", 11),
            bg="#1e1e1e", fg="#ffaa55", anchor="w", wraplength=520
        )
        self.status_label.pack(fill="x", padx=14, pady=(12, 4))

        self.tip_var = tk.StringVar(value="")
        self.tip_label = tk.Label(
            root, textvariable=self.tip_var,
            font=("Microsoft YaHei", 11, "bold"),
            bg="#1e1e1e", fg="#ff5555", anchor="w", wraplength=520, height=2
        )
        self.tip_label.pack(fill="x", padx=14, pady=(0, 4))

        scan_frame = tk.LabelFrame(
            root, text=" 可选：扫码绑定设备（不绑定也能监测） ", font=("Microsoft YaHei", 10),
            bg="#1e1e1e", fg="#cccccc", bd=1, relief="solid"
        )
        scan_frame.pack(fill="x", padx=14, pady=6)

        self.scan_btn = tk.Button(
            scan_frame, text="扫码绑定（可选）", command=self.show_qr,
            font=("Microsoft YaHei", 11), bg="#2563eb", fg="white",
            activebackground="#1d4ed8"
        )
        self.scan_btn.pack(pady=8)

        self.bind_label = tk.Label(
            scan_frame, text="未绑定（仅监测）", font=("Microsoft YaHei", 10),
            bg="#1e1e1e", fg="#888888"
        )
        self.bind_label.pack(pady=(0, 8))

        region_frame = tk.LabelFrame(
            root, text=" 步骤1：分别框选护甲和血量（尽量框紧，会自动记住） ",
            font=("Microsoft YaHei", 10),
            bg="#1e1e1e", fg="#cccccc", bd=1, relief="solid"
        )
        region_frame.pack(fill="x", padx=14, pady=6)

        row = tk.Frame(region_frame, bg="#1e1e1e")
        row.pack(fill="x", padx=10, pady=6)

        self.armor_btn = tk.Button(
            row, text="框选护甲", command=lambda: self.select_region("armor"),
            font=("Microsoft YaHei", 11), bg="#0891b2", fg="white",
            activebackground="#0e7490"
        )
        self.armor_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.health_btn = tk.Button(
            row, text="框选血量", command=lambda: self.select_region("health"),
            font=("Microsoft YaHei", 11), bg="#ca8a04", fg="white",
            activebackground="#a16207"
        )
        self.health_btn.pack(side="right", fill="x", expand=True, padx=(4, 0))

        self.armor_region_label = tk.Label(
            region_frame, text="护甲区域: 未设置", font=("Consolas", 9),
            bg="#1e1e1e", fg="#55aaff", anchor="w"
        )
        self.armor_region_label.pack(fill="x", padx=12)

        self.health_region_label = tk.Label(
            region_frame, text="血量区域: 未设置", font=("Consolas", 9),
            bg="#1e1e1e", fg="#ff5555", anchor="w"
        )
        self.health_region_label.pack(fill="x", padx=12, pady=(0, 4))

        self.debug_label = tk.Label(
            region_frame, text="", font=("Consolas", 8),
            bg="#1e1e1e", fg="#888888", anchor="w", wraplength=520, justify="left"
        )
        self.debug_label.pack(fill="x", padx=12, pady=(0, 8))

        param_frame = tk.LabelFrame(
            root, text=" 步骤2：阈值与电击强度 ", font=("Microsoft YaHei", 10),
            bg="#1e1e1e", fg="#cccccc", bd=1, relief="solid"
        )
        param_frame.pack(fill="x", padx=14, pady=6)

        tk.Label(param_frame,
                 text="护甲（蓝3格 / 紫4格 / 金5格 / 红6格，逐格识别）",
                 font=("Microsoft YaHei", 10, "bold"),
                 bg="#1e1e1e", fg="#55aaff", anchor="w").pack(fill="x", padx=10, pady=(6, 0))

        self._slider(param_frame, "护甲阈值 (%)", 1, 100,
                     PARAMS["armor_threshold"],
                     lambda v: PARAMS.update(armor_threshold=float(v)))
        self._slider(param_frame, "护甲电击强度", 1, 100,
                     PARAMS["armor_intensity"],
                     lambda v: PARAMS.update(armor_intensity=int(float(v))))

        tk.Label(param_frame,
                 text="血量",
                 font=("Microsoft YaHei", 10, "bold"),
                 bg="#1e1e1e", fg="#ff5555", anchor="w").pack(fill="x", padx=10, pady=(6, 0))

        self._slider(param_frame, "血量阈值 (%)", 1, 100,
                     PARAMS["health_threshold"],
                     lambda v: PARAMS.update(health_threshold=float(v)))
        self._slider(param_frame, "血量电击强度", 1, 100,
                     PARAMS["health_intensity"],
                     lambda v: PARAMS.update(health_intensity=int(float(v))))

        mode_frame = tk.Frame(param_frame, bg="#1e1e1e")
        mode_frame.pack(fill="x", padx=10, pady=4)
        tk.Label(mode_frame, text="电击模式", font=("Microsoft YaHei", 10),
                 bg="#1e1e1e", fg="#cccccc").pack(side="left")
        self.mode_var = tk.StringVar(value=PARAMS["mode"])
        tk.Radiobutton(mode_frame, text="一直电", variable=self.mode_var,
                       value="continuous", bg="#1e1e1e", fg="#cccccc",
                       selectcolor="#1e1e1e", command=self.on_mode).pack(side="left", padx=8)
        tk.Radiobutton(mode_frame, text="间隔电", variable=self.mode_var,
                       value="interval", bg="#1e1e1e", fg="#cccccc",
                       selectcolor="#1e1e1e", command=self.on_mode).pack(side="left")

        self.interval_frame = tk.Frame(param_frame, bg="#1e1e1e")
        tk.Label(self.interval_frame, text="间隔 (秒)", font=("Microsoft YaHei", 10),
                 bg="#1e1e1e", fg="#cccccc").pack(side="left")
        self.interval_var = tk.DoubleVar(value=PARAMS["interval_sec"])
        tk.Scale(self.interval_frame, from_=1, to=10, resolution=0.5,
                 orient="horizontal", variable=self.interval_var,
                 bg="#1e1e1e", fg="#cccccc", highlightthickness=0,
                 command=lambda v: PARAMS.update(interval_sec=float(v))
                 ).pack(side="left", fill="x", expand=True)

        ctrl_frame = tk.Frame(root, bg="#1e1e1e")
        ctrl_frame.pack(fill="x", padx=14, pady=12)

        self.start_btn = tk.Button(
            ctrl_frame, text="开始监测", command=self.toggle_run,
            font=("Microsoft YaHei", 12, "bold"), bg="#16a34a", fg="white",
            activebackground="#15803d"
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.quit_btn = tk.Button(
            ctrl_frame, text="退出", command=self.on_quit,
            font=("Microsoft YaHei", 12), bg="#dc2626", fg="white",
            activebackground="#b91c1c"
        )
        self.quit_btn.pack(side="right", fill="x", expand=True, padx=(6, 0))

        self.create_overlay()

        loaded = load_config()
        self.refresh_region_labels()
        self._sync_params_to_ui()

        if loaded:
            with LOCK:
                STATE["running"] = True
                STATE["status"] = "监测中"
            self.start_btn.config(text="停止监测", bg="#dc2626")
            print(f"[自动识别] 已加载框选区域，自动开始监测: {CONFIG_PATH}")
        else:
            with LOCK:
                STATE["running"] = False
                STATE["status"] = "请先框选护甲和血量"
            self.start_btn.config(text="开始监测", bg="#16a34a")

        threading.Thread(target=run_async, daemon=True).start()
        self.root.after(300, self.poll_status)
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

    def _sync_params_to_ui(self):
        try:
            self.mode_var.set(PARAMS.get("mode", "continuous"))
            self.interval_var.set(PARAMS.get("interval_sec", 3.0))
            self.on_mode()
        except Exception:
            pass

    def refresh_region_labels(self):
        with LOCK:
            ar = STATE["armor_region"]
            hp = STATE["health_region"]
        self.armor_region_label.config(
            text=f"护甲区域: {ar}" if ar else "护甲区域: 未设置"
        )
        self.health_region_label.config(
            text=f"血量区域: {hp}" if hp else "血量区域: 未设置"
        )

    def show_tip(self, text, duration=4000, fg="#ff5555"):
        self.tip_var.set(text)
        self.tip_label.config(fg=fg)
        if self._tip_after_id:
            try:
                self.root.after_cancel(self._tip_after_id)
            except Exception:
                pass
        self._tip_after_id = self.root.after(duration, lambda: self.tip_var.set(""))

    def create_overlay(self):
        """
        悬浮窗：只有这个置顶。
        顺序改为：护甲在上，血量在下。
        """
        self.overlay = tk.Toplevel(self.root)
        self.overlay.withdraw()
        self.overlay.overrideredirect(True)
        self.overlay.attributes("-topmost", True)   # 仅悬浮窗置顶
        try:
            self.overlay.attributes("-transparentcolor", "#010101")
        except Exception:
            self.overlay.attributes("-alpha", 0.88)
        self.overlay.configure(bg="#010101")
        self.overlay.geometry("+20+20")

        box = tk.Frame(self.overlay, bg="#010101")
        box.pack(padx=6, pady=6)

        # 护甲在上
        self.ov_ar = tk.Label(
            box, text="护甲: --.-%", font=("Consolas", 18, "bold"),
            bg="#010101", fg="#666666", anchor="w"
        )
        self.ov_ar.pack(anchor="w")

        # 血量在下
        self.ov_hp = tk.Label(
            box, text="血量: --.-%", font=("Consolas", 18, "bold"),
            bg="#010101", fg="#666666", anchor="w"
        )
        self.ov_hp.pack(anchor="w")

        self.ov_color = tk.Label(
            box, text="", font=("Microsoft YaHei", 11),
            bg="#010101", fg="#ffaa55", anchor="w"
        )
        self.ov_color.pack(anchor="w")

        self.ov_intensity = tk.Label(
            box, text="", font=("Microsoft YaHei", 11, "bold"),
            bg="#010101", fg="#ff55ff", anchor="w"
        )
        self.ov_intensity.pack(anchor="w")

        self.ov_trigger = tk.Label(
            box, text="", font=("Microsoft YaHei", 9),
            bg="#010101", fg="#ffaa55", anchor="w"
        )
        self.ov_trigger.pack(anchor="w")

        self.ov_mode = tk.Label(
            box, text="", font=("Microsoft YaHei", 10),
            bg="#010101", fg="#888888", anchor="w"
        )
        self.ov_mode.pack(anchor="w")

        for w in (self.ov_hp, self.ov_ar, self.ov_color, self.ov_intensity,
                  self.ov_trigger, self.ov_mode, box):
            w.bind("<ButtonPress-1>", self._ov_drag_start)
            w.bind("<B1-Motion>", self._ov_drag)

        self.overlay.deiconify()

    def _ov_drag_start(self, event):
        self._ov_x = event.x
        self._ov_y = event.y

    def _ov_drag(self, event):
        x = self.overlay.winfo_x() + event.x - self._ov_x
        y = self.overlay.winfo_y() + event.y - self._ov_y
        self.overlay.geometry(f"+{x}+{y}")

    def _slider(self, parent, label, lo, hi, default, callback):
        frame = tk.Frame(parent, bg="#1e1e1e")
        frame.pack(fill="x", padx=10, pady=2)
        tk.Label(frame, text=label, font=("Microsoft YaHei", 10),
                 bg="#1e1e1e", fg="#cccccc", width=14, anchor="w").pack(side="left")
        var = tk.DoubleVar(value=default)
        tk.Scale(frame, from_=lo, to=hi, resolution=1,
                 orient="horizontal", variable=var,
                 bg="#1e1e1e", fg="#cccccc", highlightthickness=0,
                 command=lambda v: callback(v)).pack(side="left", fill="x", expand=True)

    def on_mode(self):
        mode = self.mode_var.get()
        PARAMS["mode"] = mode
        if mode == "continuous":
            self.interval_frame.pack_forget()
        else:
            self.interval_frame.pack(fill="x", padx=10, pady=2)

    def show_qr(self):
        if self._qr_window is not None:
            try:
                self._qr_window.lift()
                self._qr_window.focus_force()
                return
            except Exception:
                self._qr_window = None

        with LOCK:
            url = STATE["qr_url"]
        if not url:
            self.show_tip("二维码尚未生成，请等 2~3 秒再点")
            return

        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        pil_img = img.get_image()
        photo = ImageTk.PhotoImage(pil_img)

        win = tk.Toplevel(self.root)
        win.title("扫码绑定（可选）")
        win.attributes("-topmost", True)
        win.configure(bg="#ffffff")
        tk.Label(win, image=photo, bg="#ffffff").pack(padx=10, pady=10)
        tk.Label(win, text="用 DG-Lab App 扫描二维码绑定设备\n（不绑定也能监测，绑定后才能触发电击）",
                 font=("Microsoft YaHei", 10),
                 bg="#ffffff", fg="#333333").pack(pady=(0, 10))
        win.photo = photo
        self._qr_window = win

    def select_region(self, which):
        self.root.withdraw()
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

            overlay = tk.Toplevel()
            overlay.withdraw()
            overlay.attributes("-fullscreen", True)
            overlay.attributes("-topmost", True)
            overlay.configure(cursor="cross", bg="#010101")
            try:
                overlay.attributes("-transparentcolor", "#010101")
            except Exception:
                pass
            overlay.update_idletasks()
            overlay.deiconify()

            sw = overlay.winfo_screenwidth()
            sh = overlay.winfo_screenheight()

            canvas = tk.Canvas(overlay, width=sw, height=sh, bg="#010101",
                               highlightthickness=0)
            canvas.pack(fill="both", expand=True)

            overlay.after(300, lambda: self._do_capture(overlay, canvas, sw, sh, which))
        except Exception as e:
            self.root.deiconify()
            self.show_tip(f"框选失败: {e}")

    def _do_capture(self, overlay, canvas, sw, sh, which):
        try:
            from PIL import ImageGrab, ImageTk
            import numpy as np

            img = ImageGrab.grab()
            if img.size != (sw, sh):
                img = img.resize((sw, sh))
            small = img.resize((80, 45))
            arr = np.array(small)
            mostly_black = arr.mean() < 5

            start = [None]
            rect = [None]

            overlay.attributes("-alpha", 0.06)

            if not mostly_black:
                photo = ImageTk.PhotoImage(img)
                canvas.create_image(0, 0, anchor="nw", image=photo)
                canvas.photo = photo
                overlay.attributes("-alpha", 1.0)

            label = "护甲" if which == "armor" else "血量"
            color = "#00ffff" if which == "armor" else "#ffff00"

            canvas.create_rectangle(10, 10, 770, 150, fill="#0b0b0b",
                                    outline=color, width=3)
            extra = ("护甲条请框紧整条（左右边界贴近条本身），"
                     "程序按蓝3/紫4/金5/红6格逐格识别。" if which == "armor"
                     else "只框血条本身，不要包含数字和图标。")
            canvas.create_text(
                22, 22, anchor="nw", fill=color,
                font=("Microsoft YaHei", 14, "bold"),
                text=f"现在框选：{label}条\n{extra}\n右键重画，Esc 取消。",
                width=740
            )

            def on_press(e):
                start[0] = (e.x, e.y)
                rect[0] = canvas.create_rectangle(
                    e.x, e.y, e.x, e.y, outline="#00ff00", width=4
                )

            def on_drag(e):
                if rect[0] and start[0]:
                    canvas.coords(rect[0], start[0][0], start[0][1], e.x, e.y)

            def on_release(e):
                if start[0] is None:
                    return
                x1 = min(start[0][0], e.x)
                y1 = min(start[0][1], e.y)
                x2 = max(start[0][0], e.x)
                y2 = max(start[0][1], e.y)
                start[0] = None
                rect[0] = None
                if x2 - x1 < 10 or y2 - y1 < 3:
                    return

                overlay.attributes("-alpha", 1.0)

                with LOCK:
                    if which == "armor":
                        STATE["armor_region"] = (x1, y1, x2, y2)
                    else:
                        STATE["health_region"] = (x1, y1, x2, y2)
                save_config()
                print(f"已保存 {label}区域: {[x1, y1, x2, y2]}")

                canvas.delete("all")
                canvas.create_rectangle(x1, y1, x2, y2, outline="#00ff00", width=4)
                canvas.create_text(
                    x1, max(y1 - 8, 4), anchor="sw", fill="#00ff00",
                    font=("Microsoft YaHei", 14, "bold"),
                    text=f"{label} {x2-x1}x{y2-y1}，即将确认..."
                )
                overlay.after(700, finish)

            def finish(e=None):
                overlay.destroy()
                self.root.deiconify()
                # 不再给主窗口置顶
                self.refresh_region_labels()

                with LOCK:
                    ar = STATE["armor_region"]
                    hp = STATE["health_region"]
                if ar is not None and hp is not None and not STATE["running"]:
                    STATE["running"] = True
                    STATE["status"] = "监测中"
                    self.start_btn.config(text="停止监测", bg="#dc2626")
                    print("[自动识别] 两个区域已齐，自动开始监测")

            canvas.bind("<ButtonPress-1>", on_press)
            canvas.bind("<B1-Motion>", on_drag)
            canvas.bind("<ButtonRelease-1>", on_release)
            canvas.bind("<Button-3>", lambda e: (
                canvas.delete("all"),
                start.__setitem__(0, None),
                rect.__setitem__(0, None)
            ))
            overlay.bind("<Escape>", lambda e: finish())

        except Exception as e:
            try:
                overlay.destroy()
            except Exception:
                pass
            self.root.deiconify()
            self.show_tip(f"截图失败: {e}")

    def toggle_run(self):
        with LOCK:
            running = STATE["running"]
            ar = STATE["armor_region"]
            hp = STATE["health_region"]

        if running:
            with LOCK:
                STATE["running"] = False
                STATE["status"] = "已停止"
                STATE["shocking"] = False
            self.start_btn.config(text="开始监测", bg="#16a34a")
            return

        if ar is None or hp is None:
            self.show_tip("请先分别『框选护甲』和『框选血量』")
            return

        vision.reset_history()
        with LOCK:
            STATE["running"] = True
            STATE["status"] = "监测中"
        self.start_btn.config(text="停止监测", bg="#dc2626")

    def poll_status(self):
        try:
            with LOCK:
                status = STATE["status"]
                bound = STATE["bound"]
                hp = STATE["hp"]
                ar = STATE["ar"]
                color = STATE["ar_color"]
                running = STATE["running"]
                cur_int = STATE["current_intensity"]
                trig = STATE["triggered_by"]
                health_visible = STATE["health_visible"]
                health_debug = dict(STATE["health_debug"])
                armor_debug = dict(STATE["armor_debug"])

            self.status_label.config(text=status)

            if bound:
                self.bind_label.config(text="✅ 已绑定（电击生效）", fg="#55ff55")
            else:
                self.bind_label.config(text="未绑定（仅监测）", fg="#ffaa55")

            if bound and self._qr_window is not None:
                try:
                    self._qr_window.destroy()
                except Exception:
                    pass
                self._qr_window = None
                self.show_tip("绑定成功，电击逻辑已启用", fg="#55ff55")

            hp_thr = PARAMS["health_threshold"]
            ar_thr = PARAMS["armor_threshold"]

            lines = []
            if running:
                if health_debug:
                    vis_txt = "是" if health_visible else "否"
                    lines.append(
                        f"血条: 色数={health_debug.get('unique_colors','?')} "
                        f"平滑={health_debug.get('smooth_ratio','?')} "
                        f"白={health_debug.get('white','?')} "
                        f"边缘={health_debug.get('edges','?')} "
                        f"可见={vis_txt}"
                    )
                if armor_debug:
                    n = armor_debug.get("n_cells", 0)
                    details = armor_debug.get("details", [])
                    counts = armor_debug.get("counts", {})
                    det_txt = " ".join(f"{d:.2f}" for d in details)
                    lines.append(f"护甲: 格数={n} 每格={det_txt}")
                    lines.append(
                        f"像素: 蓝={counts.get('blue',0)} 紫={counts.get('purple',0)} "
                        f"金={counts.get('gold',0)} 红={counts.get('red',0)}"
                    )
            self.debug_label.config(text="\n".join(lines), fg="#888888")

            if not running:
                self.ov_ar.config(text="护甲: --.-%", fg="#666666")
                self.ov_hp.config(text="血量: --.-%", fg="#666666")
                self.ov_color.config(text="")
                self.ov_intensity.config(text="")
                self.ov_trigger.config(text="")
                self.ov_mode.config(text="")
            elif not health_visible:
                self.ov_ar.config(text="护甲: --.-%", fg="#666666")
                self.ov_hp.config(text="血量: --.-%", fg="#666666")
                self.ov_color.config(text="")
                self.ov_intensity.config(text="")
                self.ov_trigger.config(text="")
                self.ov_mode.config(text="")
            else:
                # 护甲行
                if color is not None and ar >= 0:
                    ar_color_disp = "#55ff55" if ar >= ar_thr else "#55aaff"
                    self.ov_ar.config(text=f"护甲: {ar:5.1f}%", fg=ar_color_disp)
                    self.ov_color.config(text=COLOR_CN.get(color, ""))
                else:
                    self.ov_ar.config(text="护甲: --.-%", fg="#666666")
                    self.ov_color.config(text="")

                # 血量行
                if hp < 0:
                    self.ov_hp.config(text="血量: --.-%", fg="#666666")
                else:
                    hp_color = "#55ff55" if hp >= hp_thr else "#ff5555"
                    self.ov_hp.config(text=f"血量: {hp:5.1f}%", fg=hp_color)

                if cur_int > 0:
                    self.ov_intensity.config(text=f"强度: {cur_int}", fg="#ff55ff")
                else:
                    self.ov_intensity.config(text="")

                self.ov_trigger.config(text=f"触发: {trig}" if trig else "")

                if bound:
                    self.ov_mode.config(text="电击模式", fg="#55ff55")
                else:
                    self.ov_mode.config(text="仅监测", fg="#888888")
        except Exception as e:
            print(f"[poll_status 异常] {e}")

        try:
            self.root.after(300, self.poll_status)
        except Exception:
            pass

    def on_quit(self):
        try:
            save_config()
        except Exception:
            pass
        try:
            with LOCK:
                STATE["running"] = False
        except Exception:
            pass
        try:
            if self._qr_window is not None:
                self._qr_window.destroy()
        except Exception:
            pass
        try:
            self.overlay.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()