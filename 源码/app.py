# app.py
import asyncio
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import ctypes
import re
import shutil
import socket
import subprocess
import atexit

from PIL import Image, ImageTk, ImageGrab
import zxingcpp
import qrcode
from pydglab_ws import DGLabWSConnect, Channel, StrengthOperationType
from pydglab_ws.server import DGLabWSServer

import vision
import lan_map

MUTEX_NAME = "DGLab_Naraka_Controller_Mutex_2024"

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

PID_FILE = os.path.join(APP_DIR, ".dglab.pid")


def _remove_pid_file():
    """删掉自己的 pid 文件，不抛异常。"""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def _cleanup_all():
    """退出兜底：停隧道（连子进程树）+ 删 pid 文件。可重复调用、永不抛异常。"""
    try:
        _remove_pid_file()
    except Exception:
        pass
    try:
        mapper = STATE.get("mapper")
        if mapper is not None:
            mapper.stop_tunnel()
    except Exception:
        pass


def _run_quiet(cmd, timeout=10):
    """跑个外部命令，永不抛异常，返回 stdout。"""
    try:
        flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW，别闪黑框
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="ignore", creationflags=flags)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def _pid_alive(pid):
    """进程还活着吗（不带任何权限要求，只查存在性）。"""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def _my_parent_pid():
    """自己的父进程 PID。打包成单文件 exe 时，父进程是同名的引导进程，绝对不能杀。"""
    try:
        return int(os.getppid())
    except Exception:
        return 0


def _list_other_instances():
    """列出还在跑的「本程序」其它进程（不含自己，也不含自己的父进程）。"""
    me = os.getpid()
    parent = _my_parent_pid()
    others = []
    if getattr(sys, "frozen", False):
        # 打包成 exe：按进程名找
        name = os.path.basename(sys.executable)
        for line in _run_quiet(["tasklist", "/FI", "IMAGENAME eq %s" % name, "/FO", "CSV", "/NH"]).splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            if len(parts) >= 2:
                try:
                    pid = int(parts[1])
                except ValueError:
                    continue
                if pid not in (me, parent):
                    others.append(pid)
    else:
        # 源码运行：找命令行里带 app.py 的 python
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            cmd = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
                   "$_.CommandLine -like '*app.py*' } | Select-Object -ExpandProperty ProcessId")
            for line in _run_quiet([ps, "-NoProfile", "-NonInteractive", "-Command", cmd]).splitlines():
                try:
                    pid = int(line.strip())
                except ValueError:
                    continue
                if pid not in (me, parent):
                    others.append(pid)
    return others


def kill_previous_instance():
    """
    【打开程序先退出上一个程序】
    两级保险：
      1) 上一次启动写下的 .dglab.pid（最准，源码跑 / exe 跑都认）
      2) 真的去进程列表里找同名的自己，一起结束掉
    结束后把自己的 PID 写进文件，留给下一次启动用。
    """
    me = os.getpid()
    parent = _my_parent_pid()
    killed = []

    # 1) PID 文件
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r", encoding="utf-8") as f:
                old = int((f.read() or "0").strip() or 0)
            if old and old not in (me, parent) and _pid_alive(old):
                _run_quiet(["taskkill", "/PID", str(old), "/F", "/T"])
                killed.append(old)
                time.sleep(0.8)
    except Exception:
        pass

    # 2) 进程列表兜底
    for pid in _list_other_instances():
        if pid in (me, parent) or pid in killed:
            continue
        if not _pid_alive(pid):
            continue
        _run_quiet(["taskkill", "/PID", str(pid), "/F", "/T"])
        killed.append(pid)
    if killed:
        time.sleep(0.5)

    # 3) 记下自己
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(me))
    except Exception:
        pass
    return killed


kill_previous_instance()
mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
WS_PORT = 5678   # 默认端口，可在「端口号」窗口里改

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
    "armor_region": None, "health_region": None,
    "hp": 0.0, "ar": 0.0, "ar_color": None, "has_colored": False,
    "health_visible": False, "health_debug": {}, "armor_debug": {},
    "bound": False, "running": False, "status": "启动中...",
    "shocking": False, "client": None, "remote_url": None,
    "current_intensity": 0, "triggered_by": "", "last_error": "",
    # ---- 内网映射（lan_map）相关 ----
    "ws_port": WS_PORT,        # 本地监听端口（「端口号」窗口可改）
    "server_url": "",          # 自建 relay 服务器地址；空=本机自己当服务器
    "advertise_host": "",      # 手动指定的内网 IP；空=自动挑
    "public_uri": None,        # 公网映射地址（UPnP 打洞 / cloudflared 隧道）
    "client_id": None,         # 本地服务端的终端 ID（换地址重新出二维码要靠它）
    "bound_uri": None,         # 当前二维码里实际写进去的地址
    "qr_url": None,            # 最后一次生成的二维码内容（保存图片 / 复制分享链接用）
    "mapper": None,            # lan_map.IntranetMapper 实例
}

PARAMS = {
    "armor_threshold": 30.0, "armor_intensity": 50,
    "health_threshold": 98.0, "health_intensity": 70,
    "mode": "continuous", "interval_sec": 3.0, "channel": "A",
}

COLOR_CN = {"blue": "蓝甲", "purple": "紫甲", "gold": "金甲", "red": "红甲", None: ""}

def get_local_ips():
    """
    本机可用的局域网 IPv4 列表（按「手机能不能连」从高到低）。

    【内网映射】换成了 lan_map 的多网卡智能选卡：会过滤 VMware / Hyper-V / WSL /
    Tailscale / 198.18.x.x 这类虚拟网卡，优先 192.168.x.x + 当前默认路由出口那个。
    原来那套 UDP connect + gethostbyname 只当兜底。
    """
    try:
        ips = [c["ip"] for c in lan_map.list_ipv4_candidates() if c["usable"]]
        if ips:
            return ips
    except Exception:
        pass

    fallback = []
    try:
        route_ip = lan_map.default_route_ip()
        if route_ip:
            fallback.append(route_ip)
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip not in fallback and ip.startswith(("192.", "10.", "172.")):
                fallback.append(ip)
    except Exception:
        pass
    return [ip for ip in fallback if not ip.startswith(("127.", "198.18.", "169.254."))] or ["127.0.0.1"]


def current_port():
    """当前本地监听端口（「端口号」窗口里可改）。"""
    with LOCK:
        try:
            return int(STATE.get("ws_port") or WS_PORT)
        except Exception:
            return WS_PORT


def get_mapper():
    """拿到（或创建）当前的内网映射会话对象。"""
    port = current_port()
    with LOCK:
        mapper = STATE.get("mapper")
    if mapper is None:
        mapper = lan_map.IntranetMapper(port)
        with LOCK:
            STATE["mapper"] = mapper
    else:
        mapper.port = port
    return mapper

def load_config():
    if not os.path.exists(CONFIG_PATH): return False
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f: cfg = json.load(f)
        if "armor_region" in cfg and len(cfg["armor_region"]) == 4: STATE["armor_region"] = tuple(cfg["armor_region"])
        if "health_region" in cfg and len(cfg["health_region"]) == 4: STATE["health_region"] = tuple(cfg["health_region"])
        for key in ("armor_threshold", "armor_intensity", "health_threshold", "health_intensity"):
            if key in cfg: PARAMS[key] = cfg[key]
        if "mode" in cfg: PARAMS["mode"] = cfg["mode"]
        if "interval_sec" in cfg: PARAMS["interval_sec"] = cfg["interval_sec"]
        
        if "target_colors" in cfg:
            vision.update_target_colors(cfg["target_colors"])

        if "advertise_host" in cfg:
            STATE["advertise_host"] = cfg.get("advertise_host") or ""
        if "ws_port" in cfg:
            try:
                STATE["ws_port"] = int(cfg.get("ws_port") or WS_PORT)
            except Exception:
                STATE["ws_port"] = WS_PORT
        if "server_url" in cfg:
            STATE["server_url"] = cfg.get("server_url") or ""
            
        return (STATE["armor_region"] is not None and STATE["health_region"] is not None)
    except Exception: return False

def save_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f: cfg = json.load(f)
        except Exception: pass
    with LOCK:
        if STATE["armor_region"]: cfg["armor_region"] = list(STATE["armor_region"])
        if STATE["health_region"]: cfg["health_region"] = list(STATE["health_region"])
        cfg["armor_threshold"] = PARAMS["armor_threshold"]
        cfg["armor_intensity"] = PARAMS["armor_intensity"]
        cfg["health_threshold"] = PARAMS["health_threshold"]
        cfg["health_intensity"] = PARAMS["health_intensity"]
        cfg["mode"] = PARAMS["mode"]
        cfg["interval_sec"] = PARAMS["interval_sec"]
        cfg["target_colors"] = vision.TARGET_COLORS
        cfg["advertise_host"] = STATE.get("advertise_host") or ""
        cfg["ws_port"] = int(STATE.get("ws_port") or WS_PORT)
        cfg["server_url"] = STATE.get("server_url") or ""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f: json.dump(cfg, f, ensure_ascii=False, indent=2)

def _dg_channel(ch):
    """'A'/'B'/1/2 -> pydglab_ws 的 Channel 枚举"""
    try:
        return Channel.B if str(ch).strip().upper().startswith("B") else Channel.A
    except Exception:
        return Channel.A


async def dg_set_strength(client, ch, value):
    """
    设置强度。适配 pydglab-ws 官方 API： set_strength(channel, operation_type, value)

    【重要】旧代码写的是 client.set_strength(ch, value) —— 少一个参数，直接 TypeError，
    又被外面的 try/except 吞掉了，所以其实「一直没电」。这里修正。
    """
    try:
        await client.set_strength(_dg_channel(ch), StrengthOperationType.SET_TO,
                                  int(max(0, min(200, int(value)))))
        return True
    except Exception as e:
        with LOCK:
            STATE["last_error"] = "强度下发失败: %s" % e
        return False


async def dg_send_pulse(client, ch):
    """
    下发波形。适配官方 API： add_pulses(channel, *pulses)

    【重要】旧代码写的是 client.send_pulse(ch, PULSE_DATA, time=1.0) —— pydglab-ws 里
    根本没有 send_pulse 这个方法，同样被吞掉。这里修正。
    """
    try:
        await client.add_pulses(_dg_channel(ch), *PULSE_DATA)
        return True
    except Exception as e:
        with LOCK:
            STATE["last_error"] = "波形下发失败: %s" % e
        return False


def compute_trigger(hp, ar, has_colored, hp_thr, ar_thr, hp_int, ar_int):
    reasons, intensities = [], []
    if has_colored and ar < ar_thr:
        reasons.append("护甲"); intensities.append(int(round(ar_int)))
    if hp < hp_thr:
        reasons.append("血量"); intensities.append(int(round(hp_int)))
    if not reasons: return False, 0, ""
    return True, max(intensities), "+".join(reasons)

def vision_worker():
    while True:
        with LOCK:
            running = STATE["running"]
            ar_region = STATE["armor_region"]
            hp_region = STATE["health_region"]

        if not running or ar_region is None or hp_region is None:
            time.sleep(0.1)
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

        # 【核心修改】每 0.05 秒（1秒20次）截图识别一次，极速响应
        time.sleep(0.05)

async def send_worker(client):
    last_pulse = 0.0
    while True:
        if not STATE["bound"]:
            break
            
        with LOCK:
            running = STATE["running"]
            hp = STATE["hp"]
            ar = STATE["ar"]
            has_colored = STATE["has_colored"]
            health_visible = STATE["health_visible"]
            ar_thr, ar_int = PARAMS["armor_threshold"], PARAMS["armor_intensity"]
            hp_thr, hp_int = PARAMS["health_threshold"], PARAMS["health_intensity"]
            mode, interval, ch = PARAMS["mode"], PARAMS["interval_sec"], PARAMS["channel"]

        if not running or not health_visible or hp < 0:
            if STATE["shocking"]:
                await dg_set_strength(client, ch, 0)
                with LOCK:
                    STATE["shocking"] = False; STATE["current_intensity"] = 0
                    STATE["triggered_by"] = ""; STATE["status"] = "已连接（血/甲安全）"
            await asyncio.sleep(0.1)
            continue

        triggered, intensity, triggered_by = compute_trigger(hp, ar, has_colored, hp_thr, ar_thr, hp_int, ar_int)

        with LOCK:
            STATE["current_intensity"] = intensity if triggered else 0
            STATE["triggered_by"] = triggered_by if triggered else ""

        now = time.monotonic()
        try:
            if triggered:
                if mode == "continuous":
                    if now - last_pulse >= 0.8:
                        await dg_send_pulse(client, ch)
                        await dg_set_strength(client, ch, intensity)
                        with LOCK:
                            STATE["shocking"] = True
                            STATE["status"] = f"同步输出: {intensity} ({triggered_by})"
                        last_pulse = now
                else:
                    if now - last_pulse >= interval:
                        await dg_send_pulse(client, ch)
                        await dg_set_strength(client, ch, intensity)
                        with LOCK:
                            STATE["shocking"] = True
                            STATE["status"] = f"间隔输出: {intensity} ({triggered_by})"
                        last_pulse = now
            else:
                if STATE["shocking"]:
                    await dg_set_strength(client, ch, 0)
                    with LOCK:
                        STATE["shocking"] = False
                        STATE["status"] = "已连接（血/甲安全）"
        except Exception:
            pass
            
        await asyncio.sleep(0.1)

def run_local_server(app_instance):
    async def _server_task():
        try:
            port = current_port()
            async with DGLabWSServer("0.0.0.0", port, 60):
                async with DGLabWSConnect(f"ws://127.0.0.1:{port}") as client:
                    # 【内网映射】不再做 "127.0.0.1" 字符串替换，改成：
                    #   先决定要对外公布的映射地址（手选内网 IP / UPnP 公网地址 / 隧道地址）
                    #   再按官方格式拼二维码（pydglab_ws.utils.dg_lab_client_qrcode 同款）
                    mapper = get_mapper()
                    mapper.port = port

                    try:
                        client_id = str(client.client_id)
                    except Exception:
                        client_id = None

                    uri = mapper.lan_uri()          # ws://本机局域网IP:端口
                    if client_id:
                        qr_url = lan_map.build_qrcode_url(uri, client_id)
                    else:
                        qr_url = (client.get_qrcode() or "").replace("127.0.0.1", mapper.lan_ip)

                    with LOCK:
                        STATE["client_id"] = client_id
                        STATE["bound_uri"] = uri

                    hint = f"请用 DG-Lab App 扫描二维码\n（手机连上同一 Wi-Fi 后扫码）\n{uri}"

                    app_instance.root.after(0, lambda: app_instance.show_qr_window(qr_url, hint, auto_hide=False))
                    
                    await client.bind()
                    with LOCK:
                        STATE["bound"] = True
                        STATE["client"] = client
                        STATE["status"] = "✅ 本地设备已绑定"
                    await send_worker(client)
        except Exception as e:
            app_instance.root.after(0, lambda: messagebox.showerror("服务端错误", f"本地服务启动失败: {e}"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try: loop.run_until_complete(_server_task())
    except Exception: pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("DGLab 屏幕联动控制台")
        root.geometry("560x1000+60+20") 
        root.resizable(False, False)
        root.configure(bg="#1e1e1e")

        self._tip_after_id = None
        self.extracted_rgb = (255, 255, 255)
        self.pick_win = None
        self.color_win = None
        self._qr_window = None
        self._qr_auto_hide = True

        self.status_label = tk.Label(root, text="启动中...", font=("Microsoft YaHei", 11), bg="#1e1e1e", fg="#ffaa55", anchor="w", wraplength=520)
        self.status_label.pack(fill="x", padx=14, pady=(5, 0))
        self.tip_var = tk.StringVar(value="")
        self.tip_label = tk.Label(root, textvariable=self.tip_var, font=("Microsoft YaHei", 11, "bold"), bg="#1e1e1e", fg="#ff5555", anchor="w", wraplength=520)
        self.tip_label.pack(fill="x", padx=14, pady=(0, 2))

        # 开发者的话（粉色）
        self.love_label = tk.Label(
            root,
            text="「开发者的话」\n她是一个超级可爱的女孩子。\n"
                 "喜欢在《我的世界》里搭积木，也喜欢在《永劫无间》里跟我并肩作战。\n"
                 "她特别喜欢塔菲，说话的声音总是甜甜的，带点糖音。\n"
                 "我很喜欢她。\n"
                 "--写下这段代码，只是想陪她玩喜欢的游戏。\n",
            font=("Microsoft YaHei", 9),
            bg="#1e1e1e", fg="#ff77e6",      # 粉色
            anchor="w", justify="left", wraplength=520,
        )
        self.love_label.pack(fill="x", padx=14, pady=(2, 0))

        scan_frame = tk.LabelFrame(root, text=" 连接设备 ", font=("Microsoft YaHei", 10), bg="#1e1e1e", fg="#cccccc", bd=1, relief="solid")
        scan_frame.pack(fill="x", padx=14, pady=4)
        
        row0 = tk.Frame(scan_frame, bg="#1e1e1e")
        row0.pack(fill="x", padx=10, pady=(4, 2))
        self.host_btn = tk.Button(row0, text="🚀 一键连接（出二维码，手机扫码）", command=self.start_host_mode, font=("Microsoft YaHei", 11, "bold"), bg="#8b5cf6", fg="white")
        self.host_btn.pack(fill="x", expand=True)

        

        self.bind_label = tk.Label(scan_frame, text="未连接", font=("Microsoft YaHei", 10), bg="#1e1e1e", fg="#888888")
        self.bind_label.pack(pady=(0, 4))

        region_frame = tk.LabelFrame(root, text=" 步骤1：分别框选护甲和血量 ", font=("Microsoft YaHei", 10), bg="#1e1e1e", fg="#cccccc", bd=1, relief="solid")
        region_frame.pack(fill="x", padx=14, pady=4)
        row = tk.Frame(region_frame, bg="#1e1e1e"); row.pack(fill="x", padx=10, pady=4)
        self.armor_btn = tk.Button(row, text="框选护甲", command=lambda: self.select_region("armor"), font=("Microsoft YaHei", 11), bg="#0891b2", fg="white")
        self.armor_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.health_btn = tk.Button(row, text="框选血量", command=lambda: self.select_region("health"), font=("Microsoft YaHei", 11), bg="#ca8a04", fg="white")
        self.health_btn.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.armor_region_label = tk.Label(region_frame, text="护甲区域: 未设置", font=("Consolas", 9), bg="#1e1e1e", fg="#55aaff", anchor="w"); self.armor_region_label.pack(fill="x", padx=12)
        self.health_region_label = tk.Label(region_frame, text="血量区域: 未设置", font=("Consolas", 9), bg="#1e1e1e", fg="#ff5555", anchor="w"); self.health_region_label.pack(fill="x", padx=12, pady=(0, 2))
        self.debug_label = tk.Label(region_frame, text="", font=("Consolas", 9), bg="#1e1e1e", fg="#888888", anchor="w", wraplength=520, justify="left"); self.debug_label.pack(fill="x", padx=12, pady=(0, 4))

        param_frame = tk.LabelFrame(root, text=" 步骤2：阈值与电击强度 ", font=("Microsoft YaHei", 10), bg="#1e1e1e", fg="#cccccc", bd=1, relief="solid")
        param_frame.pack(fill="x", padx=14, pady=4)
        tk.Label(param_frame, text="护甲（蓝3格 / 紫4格 / 金5格 / 红6格）", font=("Microsoft YaHei", 10, "bold"), bg="#1e1e1e", fg="#55aaff", anchor="w").pack(fill="x", padx=10, pady=(4, 0))
        self._slider(param_frame, "护甲阈值 (%)", 1, 100, PARAMS["armor_threshold"], lambda v: PARAMS.update(armor_threshold=float(v)))
        self._slider(param_frame, "护甲电击强度", 1, 100, PARAMS["armor_intensity"], lambda v: PARAMS.update(armor_intensity=int(float(v))))
        tk.Label(param_frame, text="💡 建议插入A接口，用1托2或者1托4的接口接入电机片", font=("Microsoft YaHei", 9), bg="#1e1e1e", fg="#a3a3a3", anchor="w").pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(param_frame, text="血量", font=("Microsoft YaHei", 10, "bold"), bg="#1e1e1e", fg="#ff5555", anchor="w").pack(fill="x", padx=10, pady=(2, 0))
        self._slider(param_frame, "血量阈值 (%)", 1, 100, PARAMS["health_threshold"], lambda v: PARAMS.update(health_threshold=float(v)))
        self._slider(param_frame, "血量电击强度", 1, 100, PARAMS["health_intensity"], lambda v: PARAMS.update(health_intensity=int(float(v))))
        tk.Label(param_frame, text="💡 建议插入B接口，用1托2或者1托4的接口接入其他.......", font=("Microsoft YaHei", 9), bg="#1e1e1e", fg="#a3a3a3", anchor="w").pack(fill="x", padx=14, pady=(0, 2))
        
        mode_frame = tk.Frame(param_frame, bg="#1e1e1e"); mode_frame.pack(fill="x", padx=10, pady=2)
        tk.Label(mode_frame, text="电击模式", font=("Microsoft YaHei", 10), bg="#1e1e1e", fg="#cccccc").pack(side="left")
        self.mode_var = tk.StringVar(value=PARAMS["mode"])
        tk.Radiobutton(mode_frame, text="一直电", variable=self.mode_var, value="continuous", bg="#1e1e1e", fg="#cccccc", selectcolor="#1e1e1e", command=self.on_mode).pack(side="left", padx=8)
        tk.Radiobutton(mode_frame, text="间隔电", variable=self.mode_var, value="interval", bg="#1e1e1e", fg="#cccccc", selectcolor="#1e1e1e", command=self.on_mode).pack(side="left")
        
        self.interval_frame = tk.Frame(param_frame, bg="#1e1e1e")
        tk.Label(self.interval_frame, text="间隔 (秒)", font=("Microsoft YaHei", 10), bg="#1e1e1e", fg="#cccccc").pack(side="left")
        self.interval_var = tk.DoubleVar(value=PARAMS["interval_sec"])
        tk.Scale(self.interval_frame, from_=1, to=10, resolution=0.5, orient="horizontal", variable=self.interval_var, bg="#1e1e1e", fg="#cccccc", highlightthickness=0, command=lambda v: PARAMS.update(interval_sec=float(v))).pack(side="left", fill="x", expand=True)

        ctrl_frame = tk.Frame(root, bg="#1e1e1e"); ctrl_frame.pack(fill="x", padx=14, pady=6)
        self.start_btn = tk.Button(ctrl_frame, text="开始监测", command=self.toggle_run, font=("Microsoft YaHei", 12, "bold"), bg="#16a34a", fg="white")
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.quit_btn = tk.Button(ctrl_frame, text="退出", command=self.on_quit, font=("Microsoft YaHei", 12), bg="#dc2626", fg="white")
        self.quit_btn.pack(side="right", fill="x", expand=True, padx=(6, 0))



        self.create_overlay()
        loaded = load_config()
        self.refresh_region_labels()
        self._sync_params_to_ui()

        with LOCK:
            STATE["running"] = False
            STATE["status"] = "区域已加载，请点击开始" if loaded else "请先框选护甲和血量"
        self.start_btn.config(text="开始监测", bg="#16a34a")

        threading.Thread(target=vision_worker, daemon=True).start()
        # 【核心修改】主界面也加速刷新，每 50 毫秒（1秒20次）更新一次显示
        self.root.after(50, self.poll_status)
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

    def open_color_window(self):
        if self.color_win is not None:
            try:
                self.color_win.lift()
                self.color_win.focus_force()
                return
            except Exception:
                self.color_win = None

        self.color_win = tk.Toplevel(self.root)
        self.color_win.title("颜色提取与保存")
        self.color_win.attributes("-topmost", True)
        self.color_win.configure(bg="#1e1e1e")
        self.color_win.geometry("450x200+200+200")
        self.color_win.resizable(False, False)

        color_top = tk.Frame(self.color_win, bg="#1e1e1e")
        color_top.pack(fill="x", padx=15, pady=15)

        self.color_display = tk.Label(color_top, text="未提取", bg="#333333", fg="white", width=12, font=("Consolas", 12, "bold"))
        self.color_display.pack(side="left", padx=(0, 10), ipady=5)

        self.btn_pick = tk.Button(color_top, text="🎯 提取屏幕颜色", command=self.pick_color, font=("Microsoft YaHei", 11), bg="#d97706", fg="white")
        self.btn_pick.pack(side="left", fill="x", expand=True, ipady=5)

        color_btns = tk.Frame(self.color_win, bg="#1e1e1e")
        color_btns.pack(fill="x", padx=15, pady=(0, 15))

        tk.Button(color_btns, text="保存为蓝甲", command=lambda: self.save_color("blue"), bg="#0891b2", fg="white").pack(side="left", fill="x", expand=True, padx=3, ipady=4)
        tk.Button(color_btns, text="保存为紫甲", command=lambda: self.save_color("purple"), bg="#9333ea", fg="white").pack(side="left", fill="x", expand=True, padx=3, ipady=4)
        tk.Button(color_btns, text="保存为金甲", command=lambda: self.save_color("gold"), bg="#ca8a04", fg="white").pack(side="left", fill="x", expand=True, padx=3, ipady=4)
        tk.Button(color_btns, text="保存为红甲", command=lambda: self.save_color("red"), bg="#dc2626", fg="white").pack(side="left", fill="x", expand=True, padx=3, ipady=4)

    def pick_color(self):
        self.root.withdraw()
        self.color_win.withdraw()
        time.sleep(0.3)
        img = ImageGrab.grab()
        
        self.pick_win = tk.Toplevel()
        self.pick_win.overrideredirect(True)
        self.pick_win.attributes("-topmost", True)
        self.pick_win.geometry(f"{img.width}x{img.height}+0+0")
        self.pick_win.configure(cursor="cross", bg="black")
        
        photo = ImageTk.PhotoImage(img)
        lbl = tk.Label(self.pick_win, image=photo, bg="black")
        lbl.image = photo
        lbl.pack(fill="both", expand=True)
        
        def on_click(event):
            r, g, b = img.getpixel((event.x, event.y))
            self.extracted_rgb = (r, g, b)
            
            self.pick_win.destroy()
            self.pick_win = None
            
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            if self.color_win:
                self.color_win.deiconify()
                self.color_win.attributes("-topmost", True)
            
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            self.color_display.config(text=f"{r},{g},{b}", bg=hex_color, fg="white" if (r+g+b)<380 else "black")
            self.show_tip("颜色提取成功，请点击按钮保存！", fg="#55ff55")
            
        lbl.bind("<Button-1>", on_click)
        self.pick_win.bind("<Escape>", lambda e: self.cancel_pick())

    def cancel_pick(self):
        if self.pick_win:
            self.pick_win.destroy()
            self.pick_win = None
        self.root.deiconify()
        if self.color_win: self.color_win.deiconify()

    def save_color(self, color_name):
        if not hasattr(self, 'extracted_rgb'):
            self.show_tip("请先提取颜色！", fg="#ff5555")
            return
            
        r, g, b = self.extracted_rgb
        vision.update_target_colors({color_name: (r, g, b)})
        save_config()
        
        name_cn = COLOR_CN[color_name]
        self.show_tip(f"已成功将颜色 ({r},{g},{b}) 保存为 {name_cn}！", fg="#55ff55")

    # ==================== 内网映射（局域网 / 公网地址映射） ====================


    def start_host_mode(self):
        if STATE["bound"]:
            self.show_tip("设备已连接，请勿重复操作", fg="#ff5555")
            return
        self.show_tip("一键连接：正在启动本地服务并生成二维码...", fg="#ffaa55")
        threading.Thread(target=run_local_server, args=(self,), daemon=True).start()

    def show_qr_window(self, qr_url, hint=None, auto_hide=True):
        """
        弹出二维码窗口。

        :param auto_hide: True = 设备连上后自动关掉（首次扫码流程用）；
                          False = 一直留着（用户手动点「生成/刷新二维码」时用，
                                  否则会被 poll_status 立刻关掉，看起来就像「生成不出来」）
        """
        try:
            self.show_tip("二维码已生成！", fg="#55ff55")
            with LOCK:
                STATE["qr_url"] = qr_url
            self._qr_auto_hide = bool(auto_hide)
            self._build_qr_window(qr_url, hint)
        except Exception as e:
            try:
                with LOCK:
                    STATE["last_error"] = "二维码弹窗失败: %s" % e
            except Exception:
                pass
            try:
                messagebox.showerror("二维码生成失败", "出错了：%s" % e)
            except Exception:
                pass

    def _build_qr_window(self, qr_url, hint=None):
        if self._qr_window is not None:
            try: self._qr_window.destroy()
            except: pass
            
        self._qr_window = tk.Toplevel(self.root)
        self._qr_window.title("扫码连接（局域网）")
        self._qr_window.attributes("-topmost", True)
        self._qr_window.configure(bg="#ffffff")
        self._qr_window.geometry("400x450+100+100")
        
        def on_close():
            if self._qr_window:
                self._qr_window.destroy()
                self._qr_window = None
        self._qr_window.protocol("WM_DELETE_WINDOW", on_close)
        
        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        photo = ImageTk.PhotoImage(img.get_image())
        lbl = tk.Label(self._qr_window, image=photo, bg="#ffffff")
        lbl.image = photo
        lbl.pack(padx=10, pady=10)
        
        tk.Label(self._qr_window, text=hint or "请用 DG-Lab App 扫描二维码\n（需在同一个Wi-Fi下）", bg="#ffffff", fg="#333333", font=("Microsoft YaHei", 10), justify="center", wraplength=370).pack(padx=6)
        
        def copy_link():
            self._qr_window.clipboard_clear()
            self._qr_window.clipboard_append(qr_url)
            self.show_tip("链接已复制！", fg="#55ff55")
        
        tk.Button(self._qr_window, text="复制连接链接", command=copy_link, bg="#10b981", fg="white", font=("Microsoft YaHei", 10)).pack(pady=15)
        # 兜底：若二维码图渲染不出来或用户没连上，至少把地址露出来，手动复制也能用
        try:
            tk.Label(self._qr_window, text=qr_url, font=("Consolas", 8), bg="#ffffff", fg="#888888", wraplength=370, justify="center").pack(padx=8, pady=(0, 4))
        except Exception:
            pass


    def _sync_params_to_ui(self):
        try:
            self.mode_var.set(PARAMS.get("mode", "continuous"))
            self.interval_var.set(PARAMS.get("interval_sec", 3.0))
            self.on_mode()
        except Exception: pass

    def refresh_region_labels(self):
        with LOCK:
            ar, hp = STATE["armor_region"], STATE["health_region"]
        self.armor_region_label.config(text=f"护甲区域: {ar}" if ar else "护甲区域: 未设置")
        self.health_region_label.config(text=f"血量区域: {hp}" if hp else "血量区域: 未设置")

    def show_tip(self, text, duration=4000, fg="#ff5555"):
        self.tip_var.set(text)
        self.tip_label.config(fg=fg)
        if self._tip_after_id:
            try: self.root.after_cancel(self._tip_after_id)
            except Exception: pass
        self._tip_after_id = self.root.after(duration, lambda: self.tip_var.set(""))

    def create_overlay(self):
        self.overlay = tk.Toplevel(self.root); self.overlay.withdraw()
        self.overlay.overrideredirect(True); self.overlay.attributes("-topmost", True)
        try: self.overlay.attributes("-transparentcolor", "#010101")
        except Exception: self.overlay.attributes("-alpha", 0.88)
        self.overlay.configure(bg="#010101"); self.overlay.geometry("+20+20")
        box = tk.Frame(self.overlay, bg="#010101"); box.pack(padx=6, pady=6)
        self.ov_ar = tk.Label(box, text="护甲: --.-%", font=("Consolas", 18, "bold"), bg="#010101", fg="#666666", anchor="w"); self.ov_ar.pack(anchor="w")
        self.ov_hp = tk.Label(box, text="血量: --.-%", font=("Consolas", 18, "bold"), bg="#010101", fg="#666666", anchor="w"); self.ov_hp.pack(anchor="w")
        self.ov_color = tk.Label(box, text="", font=("Microsoft YaHei", 11), bg="#010101", fg="#ffaa55", anchor="w"); self.ov_color.pack(anchor="w")
        self.ov_intensity = tk.Label(box, text="", font=("Microsoft YaHei", 11, "bold"), bg="#010101", fg="#ff55ff", anchor="w"); self.ov_intensity.pack(anchor="w")
        self.ov_trigger = tk.Label(box, text="", font=("Microsoft YaHei", 9), bg="#010101", fg="#ffaa55", anchor="w"); self.ov_trigger.pack(anchor="w")
        self.ov_mode = tk.Label(box, text="", font=("Microsoft YaHei", 10), bg="#010101", fg="#888888", anchor="w"); self.ov_mode.pack(anchor="w")
        for w in (self.ov_hp, self.ov_ar, self.ov_color, self.ov_intensity, self.ov_trigger, self.ov_mode, box):
            w.bind("<ButtonPress-1>", self._ov_drag_start); w.bind("<B1-Motion>", self._ov_drag)
        self.overlay.deiconify()

    def _ov_drag_start(self, event):
        self._ov_x = event.x; self._ov_y = event.y

    def _ov_drag(self, event):
        x = self.overlay.winfo_x() + event.x - self._ov_x; y = self.overlay.winfo_y() + event.y - self._ov_y
        self.overlay.geometry(f"+{x}+{y}")

    def _slider(self, parent, label, lo, hi, default, callback):
        frame = tk.Frame(parent, bg="#1e1e1e"); frame.pack(fill="x", padx=10, pady=2)
        tk.Label(frame, text=label, font=("Microsoft YaHei", 10), bg="#1e1e1e", fg="#cccccc", width=14, anchor="w").pack(side="left")
        var = tk.DoubleVar(value=default)
        tk.Scale(frame, from_=lo, to=hi, resolution=1, orient="horizontal", variable=var, bg="#1e1e1e", fg="#cccccc", highlightthickness=0, command=lambda v: callback(v)).pack(side="left", fill="x", expand=True)

    def on_mode(self):
        PARAMS["mode"] = self.mode_var.get()
        if PARAMS["mode"] == "continuous": self.interval_frame.pack_forget()
        else: self.interval_frame.pack(fill="x", padx=10, pady=2)

    def select_region(self, which):
        self.root.withdraw()
        if self.color_win: self.color_win.withdraw()
        try:
            import ctypes
            try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception: pass
            
            user32 = ctypes.windll.user32
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            
            overlay = tk.Toplevel()
            overlay.withdraw()
            overlay.overrideredirect(True)
            overlay.geometry(f"{sw}x{sh}+0+0")
            overlay.attributes("-topmost", True)
            overlay.configure(cursor="cross", bg="#010101")
            try: overlay.attributes("-transparentcolor", "#010101")
            except Exception: pass
            overlay.update_idletasks()
            overlay.deiconify()
            
            canvas = tk.Canvas(overlay, width=sw, height=sh, bg="#010101", highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            overlay.after(300, lambda: self._do_capture(overlay, canvas, sw, sh, which))
        except Exception as e:
            self.root.deiconify(); self.show_tip(f"框选失败: {e}")

    def _do_capture(self, overlay, canvas, sw, sh, which):
        try:
            from PIL import ImageGrab, ImageTk
            import numpy as np
            img = ImageGrab.grab()
            if img.size != (sw, sh): img = img.resize((sw, sh))
            small = img.resize((80, 45)); arr = np.array(small)
            mostly_black = arr.mean() < 5
            start, rect = [None], [None]
            overlay.attributes("-alpha", 0.06)
            if not mostly_black:
                photo = ImageTk.PhotoImage(img)
                canvas.create_image(0, 0, anchor="nw", image=photo)
                canvas.photo = photo; overlay.attributes("-alpha", 1.0)
            label = "护甲" if which == "armor" else "血量"
            color = "#00ffff" if which == "armor" else "#ffff00"
            canvas.create_rectangle(10, 10, 770, 150, fill="#0b0b0b", outline=color, width=3)
            extra = ("护甲条请框紧整条（左右边界贴近条本身），程序按蓝3/紫4/金5/红6格逐格识别。" if which == "armor" else "只框血条本身，不要包含数字和图标。")
            canvas.create_text(22, 22, anchor="nw", fill=color, font=("Microsoft YaHei", 14, "bold"), text=f"现在框选：{label}条\n{extra}\n右键取消重画，Esc 取消。", width=740)

            def on_press(e):
                start[0] = (e.x, e.y)
                rect[0] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ff00", width=4)
            def on_drag(e):
                if rect[0] and start[0]: canvas.coords(rect[0], start[0][0], start[0][1], e.x, e.y)
            def on_release(e):
                if start[0] is None: return
                x1, y1 = min(start[0][0], e.x), min(start[0][1], e.y)
                x2, y2 = max(start[0][0], e.x), max(start[0][1], e.y)
                start[0], rect[0] = None, None
                if x2 - x1 < 10 or y2 - y1 < 3: return
                overlay.attributes("-alpha", 1.0)
                with LOCK:
                    if which == "armor": STATE["armor_region"] = (x1, y1, x2, y2)
                    else: STATE["health_region"] = (x1, y1, x2, y2)
                save_config()
                canvas.delete("all")
                canvas.create_rectangle(x1, y1, x2, y2, outline="#00ff00", width=4)
                canvas.create_text(x1, max(y1 - 8, 4), anchor="sw", fill="#00ff00", font=("Microsoft YaHei", 14, "bold"), text=f"{label} {x2-x1}x{y2-y1}，即将确认...")
                overlay.after(700, finish)
            def finish(e=None):
                overlay.destroy(); self.root.deiconify()
                if self.color_win: self.color_win.deiconify()
                self.refresh_region_labels()
                with LOCK:
                    ar, hp = STATE["armor_region"], STATE["health_region"]
                if ar is not None and hp is not None and not STATE["running"]:
                    STATE["running"] = True; STATE["status"] = "监测中"; self.start_btn.config(text="停止监测", bg="#dc2626")
            canvas.bind("<ButtonPress-1>", on_press); canvas.bind("<B1-Motion>", on_drag); canvas.bind("<ButtonRelease-1>", on_release)
            canvas.bind("<Button-3>", lambda e: finish()); overlay.bind("<Escape>", lambda e: finish())
        except Exception as e:
            try: overlay.destroy()
            except Exception: pass
            self.root.deiconify()
            if self.color_win: self.color_win.deiconify()
            self.show_tip(f"截图失败: {e}")

    def toggle_run(self):
        with LOCK:
            running = STATE["running"]; ar = STATE["armor_region"]; hp = STATE["health_region"]
        if running:
            with LOCK: STATE["running"] = False; STATE["status"] = "已停止"; STATE["shocking"] = False
            self.start_btn.config(text="开始监测", bg="#16a34a"); return
        if ar is None or hp is None: self.show_tip("请先分别『框选护甲』和『框选血量』"); return
        
        with LOCK: STATE["running"] = True; STATE["status"] = "监测中"
        self.start_btn.config(text="停止监测", bg="#dc2626")

    def poll_status(self):
        try:
            with LOCK:
                status = STATE["status"]; bound = STATE["bound"]; hp = STATE["hp"]; ar = STATE["ar"]
                color = STATE["ar_color"]; running = STATE["running"]; cur_int = STATE["current_intensity"]
                trig = STATE["triggered_by"]; health_visible = STATE["health_visible"]
                health_debug = dict(STATE["health_debug"]); armor_debug = dict(STATE["armor_debug"])
            
            self.status_label.config(text=status)
            
            if bound:
                self.bind_label.config(text="✅ 已连接设备", fg="#55ff55")
                if self._qr_window is not None and getattr(self, "_qr_auto_hide", True):
                    try:
                        self._qr_window.destroy()
                        self._qr_window = None
                    except: pass
            else:
                self.bind_label.config(text="未连接设备", fg="#ffaa55")
            
            hp_thr, ar_thr = PARAMS["health_threshold"], PARAMS["armor_threshold"]
            lines = []
            if STATE.get("last_error"):
                lines.append("⚠ " + str(STATE["last_error"])[:60])
            if running:
                if health_debug:
                    lines.append(f"血量白像素={health_debug.get('white',0)}")
                if armor_debug:
                    counts = armor_debug.get("counts", {})
                    lines.append(f"护甲像素: 蓝={counts.get('blue',0)} 紫={counts.get('purple',0)} 金={counts.get('gold',0)} 红={counts.get('red',0)}")
            self.debug_label.config(text="\n".join(lines), fg="#888888")

            if not running:
                self.ov_ar.config(text="护甲: --.-%", fg="#666666"); self.ov_hp.config(text="血量: --.-%", fg="#666666")
                self.ov_color.config(text=""); self.ov_intensity.config(text=""); self.ov_trigger.config(text=""); self.ov_mode.config(text="")
            elif not health_visible:
                self.ov_ar.config(text="护甲: --.-%", fg="#666666"); self.ov_hp.config(text="血量: --.-%", fg="#666666")
                self.ov_color.config(text=""); self.ov_intensity.config(text=""); self.ov_trigger.config(text=""); self.ov_mode.config(text="")
            else:
                if color is not None and ar >= 0:
                    ar_color_disp = "#55ff55" if ar >= ar_thr else "#55aaff"
                    self.ov_ar.config(text=f"护甲: {ar:5.1f}%", fg=ar_color_disp); self.ov_color.config(text=COLOR_CN.get(color, ""))
                else:
                    self.ov_ar.config(text="护甲: --.-%", fg="#666666"); self.ov_color.config(text="")
                if hp < 0: self.ov_hp.config(text="血量: --.-%", fg="#666666")
                else:
                    hp_color = "#55ff55" if hp >= hp_thr else "#ff5555"
                    self.ov_hp.config(text=f"血量: {hp:5.1f}%", fg=hp_color)
                
                if cur_int > 0: self.ov_intensity.config(text=f"强度: {cur_int}", fg="#ff55ff")
                else: self.ov_intensity.config(text="")
                self.ov_trigger.config(text=f"触发: {trig}" if trig else "")
                
                if bound:
                    self.ov_mode.config(text="电击模式", fg="#55ff55")
                else:
                    self.ov_mode.config(text="仅预览（未连接）", fg="#888888")
        except Exception: pass
        
        # 【核心修改】UI 刷新率改为 50ms（1秒20次）
        try: self.root.after(50, self.poll_status)
        except Exception: pass

    def on_quit(self):
        try: save_config()
        except Exception: pass
        with LOCK: STATE["running"] = False
        _cleanup_all()                      # 杀隧道子进程树 + 删 pid 文件
        try: self.overlay.destroy()
        except Exception: pass
        try: self.root.destroy()
        except Exception: pass
        os._exit(0)

if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
