# -*- coding: utf-8 -*-
"""
lan_map.py —— DGLab 「内网映射」模块
======================================================================
把开源 DG-Lab 生态里的「地址映射 / 内网映射」那套逻辑扒出来，做成可以直接嫁给
本项目的独立模块（只用标准库，不新增依赖，PyInstaller 直接能打包）。

扒取来源（GitHub 开源）
----------------------------------------------------------------------
1. Ljzd-PRO/pydglab-ws  ·  pydglab_ws/utils.py :: dg_lab_client_qrcode()
   官方二维码格式： https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#ws://IP:PORT/<clientId>
   pydglab_ws/client/ws.py :: DGLabWSClient.get_qrcode()（原来本项目是把 127.0.0.1
   字符串替换掉，非常脆，这里改成「先决定映射地址，再拼二维码」）
2. AngelcoMilk/DGLabPunish · DGLabSocketServer.GetSocketBaseUri / GetAdvertiseHost /
   GetLocalIPv4Addresses · ModConfig（LocalServer / PublicSocket / RemoteServer / RelayCode
   四种连接模式 + AdvertiseHost + PublishSocketUri，这就是它的「内网映射」核心）
3. dungeonlab-open/dglab-websocket-server · v3-server（自建 relay，公网映射落地用）
4. dungeonlab-open/dglab-kit · 4.0 二维码格式 https://dungeon-lab.cn/s/?v=1&action=socket&url=

模块能力
----------------------------------------------------------------------
* list_ipv4_candidates()  多网卡枚举 + 虚拟网卡识别 + 打分排序（扒 DGLabPunish 的选卡规则）
* pick_lan_ip()           自动挑「手机同一 Wi-Fi 能访问的」那个 IPv4
* build_socket_uri()/build_qrcode_url()  地址 -> Socket URI -> 官方二维码
* check_tcp()             地址可达性自检（本机能不能连上这个映射地址）
* UPnP IGD               SSDP 发现路由器 + AddPortMapping/DeletePortMapping（真·内网映射）
* netsh portproxy        Windows 本机端口代理映射（备用方案）
* ensure_firewall_rule()  Windows 防火墙放行（二维码扫了连不上，九成是这个）
* IntranetMapper         把上面这些串成一个「映射会话」对象，给 GUI 用
* cloudflared 隧道        有 cloudflared 时可以直接开公网隧道（内网穿透）

设计原则
----------------------------------------------------------------------
* 纯标准库；所有外部命令都 best-effort，失败只回报原因，不抛到 UI。
* 任何映射失败都不影响原本的「局域网直连」用法（降级即可用）。
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

__all__ = [
    "IS_WINDOWS",
    "QR_PREFIX_V3",
    "QR_TEMPLATE_V4",
    "list_ipv4_candidates",
    "pick_lan_ip",
    "default_route_ip",
    "normalize_base_uri",
    "build_socket_uri",
    "build_qrcode_url",
    "build_qrcode_url_v4",
    "check_tcp",
    "ensure_firewall_rule",
    "netsh_portproxy_add",
    "netsh_portproxy_delete",
    "netsh_portproxy_show",
    "ssdp_discover_gateways",
    "upnp_get_external_ip",
    "upnp_map_port",
    "upnp_unmap_port",
    "find_cloudflared",
    "IntranetMapper",
]

IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# 官方 V3 二维码前缀（pydglab_ws.utils.dg_lab_client_qrcode）
QR_PREFIX_V3 = "https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#"
# 官方 V4 二维码（dglab-websocket-server / dglab-kit 文档）
QR_TEMPLATE_V4 = "https://dungeon-lab.cn/s/?v=1&action=socket&url={url}"

UPNP_SSDP_ADDR = ("239.255.255.250", 1900)
UPNP_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
WAN_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)

# 虚拟 / 隧道 / 代理网卡关键字（DGLabPunish README 里点名的那几类：198.18.x.x、Tailscale、
# VMware、Hyper-V、WSL、Radmin、加速器……）
VIRTUAL_HINTS = (
    "vmware", "vmnet", "virtualbox", "vbox", "hyper-v", "vethernet", "default switch",
    "loopback", "npcap", "wintun", "tap-windows", "openvpn", "wireguard", "tailscale",
    "zerotier", "radmin", "hamachi", "docker", "wsl", "bluetooth", "sangfor", "easyconnect",
    "anyconnect", "forticlient", "clash", "mihomo", "v2ray", "singbox", "sing-box",
    "proxy", "vpn", "tunnel", "虚拟", "加速", "环回", "隧道", "代理",
)


def _no_window_kwargs() -> dict:
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NO_WINDOW}
    return {}


def _run(cmd, timeout=8):
    """跑一条外部命令，永远不抛异常。"""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="ignore", shell=isinstance(cmd, str),
            **_no_window_kwargs(),
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", "命令不存在: %s" % (cmd if isinstance(cmd, str) else cmd[0])
    except subprocess.TimeoutExpired:
        return -2, "", "命令超时"
    except Exception as e:  # noqa: BLE001
        return -3, "", str(e)


# ======================================================================
# 1. 网卡枚举 / 智能选卡  —— 扒自 DGLabPunish 的 GetLocalIPv4Addresses + README 选卡规则
# ======================================================================

def default_route_ip():
    """默认路由出口 IP：UDP connect 一下就知道，最靠谱的「我现在的内网 IP」。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.4)
        s.connect(("223.5.5.5", 53))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return None
    finally:
        s.close()


def _windows_adapters():
    """返回 {ip: adapter_alias}。用 Get-NetIPAddress，失败就返回空表。"""
    if not IS_WINDOWS:
        return {}
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return {}
    # 强制 UTF-8 输出，否则中文网卡名（"以太网" / "WLAN"）会乱码
    cmd = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
           "$OutputEncoding=[System.Text.Encoding]::UTF8;"
           "Get-NetIPAddress -AddressFamily IPv4 | "
           "Select-Object IPAddress,InterfaceAlias,SkipAsSource | ConvertTo-Json -Compress")
    rc, out, _err = _run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd], timeout=10)
    if rc != 0 or not out:
        return {}
    try:
        data = json.loads(out)
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(data, dict):
        data = [data]
    table = {}
    for row in data:
        ip = (row or {}).get("IPAddress")
        if not ip:
            continue
        alias = (row.get("InterfaceAlias") or "").strip()
        if row.get("SkipAsSource"):
            alias = (alias + " [SkipAsSource]").strip()
        table[ip] = alias
    return table


def _fallback_ips():
    ips = set()
    for fn in (lambda: socket.gethostbyname_ex(socket.gethostname())[2],
               lambda: [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]):
        try:
            ips.update(fn())
        except Exception:  # noqa: BLE001
            pass
    return ips


def _classify(ip, alias, route_ip=None):
    """给一个 IP 打分。分越高越可能「手机能连」。返回 (score, note)。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return -100, "非法地址"
    if addr.is_loopback:
        return -100, "环回地址，手机连不上"
    if addr.is_link_local:
        return -100, "169.254 自动私有地址"
    if addr.is_multicast or addr.is_unspecified:
        return -100, "特殊地址"

    text = (alias or "").lower()
    virtual = any(h in text for h in VIRTUAL_HINTS) if text else False
    note = []
    score = 0

    # 私有网段优先级：192.168 > 10 > 172.16-31（家用路由最常见 192.168.x.x）
    if addr in ipaddress.ip_network("192.168.0.0/16"):
        score += 60
        note.append("家用局域网网段 192.168.x.x")
    elif addr in ipaddress.ip_network("10.0.0.0/8"):
        score += 50
        note.append("局域网网段 10.x.x.x")
    elif addr in ipaddress.ip_network("172.16.0.0/12"):
        score += 40
        note.append("局域网网段 172.16-31.x.x")
    elif addr in ipaddress.ip_network("100.64.0.0/10"):
        score -= 60
        note.append("运营商级 NAT / 组网网段，手机多半连不上")
    elif addr in ipaddress.ip_network("198.18.0.0/15"):
        score -= 80
        note.append("198.18/15 虚拟测试网段（代理/加速器/虚拟网卡）")
    else:
        score -= 20
        note.append("非典型内网网段")

    if route_ip is None:
        route_ip = default_route_ip()
    if ip == route_ip:
        score += 15
        note.append("当前默认路由出口")

    if virtual:
        score -= 70
        note.append("疑似虚拟网卡[%s]" % (alias or "?"))
    elif alias:
        score += 10
        note.append("物理网卡[%s]" % alias)

    if "skipassource" in text:
        score -= 40
        note.append("SkipAsSource")

    return score, "；".join(note)


def list_ipv4_candidates(include_unusable=False):
    """
    枚举本机所有 IPv4，带适配器名和可连性评分。

    :return: [{"ip","adapter","score","usable","note"}...]，已按分数从高到低排序
    """
    table = _windows_adapters()
    ips = set(table.keys()) | _fallback_ips()
    route_ip = default_route_ip()
    out = []
    for ip in ips:
        alias = table.get(ip, "")
        score, note = _classify(ip, alias, route_ip)
        out.append({
            "ip": ip,
            "adapter": alias,
            "score": score,
            "usable": score > 0,
            "note": note,
        })
    out.sort(key=lambda x: (-x["score"], x["ip"]))
    if not include_unusable:
        pass  # 仍然返回不可用项，但 usable 标了 False，方便 UI 灰显
    return out


def pick_lan_ip():
    """自动挑一个最适合写进二维码的内网 IP。"""
    for c in list_ipv4_candidates():
        if c["usable"]:
            return c["ip"]
    return default_route_ip() or "127.0.0.1"


# ======================================================================
# 2. 地址 -> Socket URI -> 官方二维码（扒自 pydglab-ws）
# ======================================================================

def normalize_base_uri(uri):
    """规范化 WebSocket 基础地址：带 scheme、不带结尾斜杠。"""
    uri = (uri or "").strip()
    if not uri:
        return ""
    if not re.match(r"^wss?://", uri, re.I):
        uri = "ws://" + uri
    return uri.rstrip("/")


def build_socket_uri(host, port, secure=False, path=""):
    """
    拼 WebSocket 基础地址。

    :param host: IP 或域名，域名会自动走非加密（除非 secure=True）
    :param port: 端口；None/0 表示省略
    :param secure: True -> wss://，False -> ws://
    :param path: 例如 "/v4"（自建 relay 的 PREFIX）
    """
    scheme = "wss" if secure else "ws"
    if not host:
        host = "127.0.0.1"
    host = str(host).strip()
    # IPv6 需要方括号
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    if port:
        uri = "%s://%s:%s" % (scheme, host, int(port))
    else:
        uri = "%s://%s" % (scheme, host)
    if path:
        uri += "/" + str(path).strip("/")
    return uri


def build_qrcode_url(socket_uri, client_id):
    """
    官方 V3 二维码内容（等价 pydglab_ws.utils.dg_lab_client_qrcode）。

    :param socket_uri: 例如 ws://192.168.1.99:5678 （末尾不能有 /）
    :param client_id: 控制端 terminal id（UUID）
    """
    return "%s%s/%s" % (QR_PREFIX_V3, normalize_base_uri(socket_uri), client_id)


def build_qrcode_url_v4(socket_uri, target_id=None):
    """官方 4.0 二维码（dglab-websocket-server 文档里的拼接方式）。"""
    url = normalize_base_uri(socket_uri)
    if target_id:
        url = "%s?tid=%s" % (url, target_id)
    return QR_TEMPLATE_V4.format(url=urllib.parse.quote(url, safe=""))


def parse_share_link(text):
    """
    把「分享链接 / 邀请码」解析成可直接连接的 WebSocket 地址。

    可能输入：
      * 官方 V3 二维码全文： https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#ws://1.2.3.4:5678/abc
      * 官方便捷：#DGLAB-SOCKET#ws://1.2.3.4:5678/abc
      * 纯 ws:// / wss:// 地址（含路径）
      * 裸 IP:端口 / 域名:端口

    返回 (socket_uri, tail)：
      socket_uri = 让 DGLabWSConnect 能直连的地址
      tail       = 末尾 clientId（若有则保留；没有也无妨）
    解析失败返回 (None, None)。不需要它成功也行——解析不出就整串当 url 试连。
    """
    if not text:
        return None, None
    text = (text or "").strip()
    # 带 hash 的官方链接：取 # 后部分里的第一个 ws:// / wss:// 段
    if "#" in text:
        frag = text.split("#", 1)[1]
        m = re.search(r"(wss?://[^\s#/]+(?:\d+)?[^\s#]*[^\s#/])", frag)
        if m:
            tail_idx = frag.find(m.group(1)) + len(m.group(1))
            tail = frag[tail_idx:].strip().strip("/")
            return m.group(1).rstrip("/"), tail
        # 官方 app-download 前缀且无 hash 段 ws → 兜底整串
    # 纯地址
    m = re.match(r"^(wss?://[^\s]+)", text)
    if m:
        base = m.group(1)
        idx = text.find(base) + len(base)
        tail = text[idx:].strip().strip("/")
        return base.rstrip("/"), tail
    # 裸 host[:port]（可能带 /clientId）
    m = re.match(r"^([^\s/]+)(/.*)?$", text)
    if m:
        host = m.group(1)
        tail = (m.group(2) or "").strip("/")
        uri = normalize_base_uri(host) if "://" in host else "ws://%s" % host
        return uri, tail
    return None, None


# ======================================================================
# 3. 可达性自检
# ======================================================================

def check_tcp(host, port, timeout=1.5):
    """TCP 连一下，确认这个映射地址真的通。返回 (ok, msg)。"""
    host = str(host).strip().strip("[]")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, "TCP 可达 %s:%s" % (host, port)
    except Exception as e:  # noqa: BLE001
        return False, "TCP 不可达 %s:%s（%s）" % (host, port, e)


# ======================================================================
# 4. Windows 防火墙放行（二维码扫完连不上，九成是这个）
# ======================================================================

def ensure_firewall_rule(port, name="DGLab Socket Server", log=None):
    """
    加一条入站放行规则（需要管理员权限）。已存在同名规则时 netsh 会报错，按已存在处理。
    """
    if not IS_WINDOWS:
        return {"ok": False, "msg": "非 Windows，跳过"}
    rc, out, err = _run([
        "netsh", "advfirewall", "firewall", "add", "rule",
        "name=%s" % name, "dir=in", "action=allow",
        "protocol=TCP", "localport=%d" % int(port),
    ], timeout=10)
    text = (out + " " + err).strip()
    ok = rc == 0 or "already exists" in text or "已存在" in text
    if log:
        log("[防火墙] %s" % (text or ("rc=%s" % rc)))
    return {"ok": ok, "msg": text or ("rc=%s" % rc)}


def delete_firewall_rule(name="DGLab Socket Server"):
    return _run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=%s" % name], timeout=10)[:2]


# ======================================================================
# 5. Windows 端口代理映射（netsh portproxy）：把 0.0.0.0:外网端口 -> 127.0.0.1:内网端口
# ======================================================================

def netsh_portproxy_add(listen_port, connect_port, listen_address="0.0.0.0", connect_address="127.0.0.1"):
    """本机端口映射（需要管理员）。用于把服务暴露到本机所有网卡/其它网段。"""
    if not IS_WINDOWS:
        return {"ok": False, "msg": "非 Windows，跳过"}
    rc, out, err = _run([
        "netsh", "interface", "portproxy", "add", "v4tov4",
        "listenport=%d" % int(listen_port), "listenaddress=%s" % listen_address,
        "connectport=%d" % int(connect_port), "connectaddress=%s" % connect_address,
    ], timeout=10)
    return {"ok": rc == 0, "msg": (out + " " + err).strip() or ("rc=%s" % rc)}


def netsh_portproxy_delete(listen_port, listen_address="0.0.0.0"):
    if not IS_WINDOWS:
        return {"ok": False, "msg": "非 Windows，跳过"}
    rc, out, err = _run([
        "netsh", "interface", "portproxy", "delete", "v4tov4",
        "listenport=%d" % int(listen_port), "listenaddress=%s" % listen_address,
    ], timeout=10)
    return {"ok": rc == 0, "msg": (out + " " + err).strip() or ("rc=%s" % rc)}


def netsh_portproxy_show():
    if not IS_WINDOWS:
        return ""
    return _run(["netsh", "interface", "portproxy", "show", "v4tov4"], timeout=10)[1]


# ======================================================================
# 6. UPnP IGD —— 真正的「内网映射」：自动在路由器上开端口映射
# ======================================================================

def _http_get(url, timeout=4):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", ""):
        return ""
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        conn.request("GET", parts.path or "/", headers={"Connection": "close", "User-Agent": "dglab-lan-map/1.0"})
        resp = conn.getresponse()
        return resp.read().decode("utf-8", "ignore")
    finally:
        conn.close()


def _http_soap(control_url, service_type, action, arg_xml, timeout=5):
    parts = urllib.parse.urlsplit(control_url)
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body><u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>'
    ) % (action, service_type, arg_xml, action)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        conn.request("POST", parts.path or "/", body=body.encode("utf-8"), headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"%s#%s"' % (service_type, action),
            "Connection": "close",
            "User-Agent": "dglab-lan-map/1.0",
        })
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "ignore")
    finally:
        conn.close()


def _tag(xml, name):
    m = re.search(r"<[^>]*%s[^>]*>(.*?)</[^>]*%s>" % (name, name), xml or "", re.I | re.S)
    return m.group(1).strip() if m else ""


def _parse_igd(location, log=None):
    """解析 IGD 描述文件，找出 WAN 连接的 controlURL。"""
    xml = _http_get(location)
    if not xml:
        return None
    base = location
    found = None
    for svc in re.finditer(r"<service>(.*?)</service>", xml, re.I | re.S):
        block = svc.group(1)
        stype = _tag(block, "serviceType")
        if stype in WAN_SERVICE_TYPES:
            ctrl = _tag(block, "controlURL")
            if ctrl:
                found = {
                    "location": location,
                    "service_type": stype,
                    "control_url": urllib.parse.urljoin(base, ctrl),
                    "friendly_name": _tag(xml, "friendlyName"),
                }
                break
    if not found and log:
        log("[UPnP] %s 没找到 WANIPConnection/WANPPPConnection 服务" % location)
    return found


def ssdp_discover_gateways(timeout=3.0, log=None):
    """SSDP 找路由器 IGD。返回 [{location,service_type,control_url,friendly_name}...]。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    gateways, locations = [], []
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:  # noqa: BLE001
            pass
        sock.settimeout(0.4)

        def _msearch(st):
            return ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                    'MAN: "ssdp:discover"\r\nMX: 2\r\nST: %s\r\n\r\n') % st

        # 一次不够：不同 ST + 发多轮，很多家用路由只回其中某一种
        search_targets = (UPNP_ST, "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
                          "upnp:rootdevice", "ssdp:all")
        deadline = time.time() + timeout
        sent_at = 0.0
        round_no = 0
        while time.time() < deadline:
            now = time.time()
            if now - sent_at > 0.9 and round_no < 4:
                st = search_targets[round_no % len(search_targets)]
                round_no += 1
                sent_at = now
                for dest in (UPNP_SSDP_ADDR, ("239.255.255.250", 1900)):
                    try:
                        sock.sendto(_msearch(st).encode(), dest)
                        break
                    except Exception as e:  # noqa: BLE001
                        if log and round_no == 1:
                            log("[UPnP] SSDP 发送失败(%s): %s" % (dest, e))
            try:
                data, _addr = sock.recvfrom(65507)
            except socket.timeout:
                continue
            except Exception:  # noqa: BLE001
                break
            m = re.search(r"(?im)^location:\s*(\S+)", data.decode("utf-8", "ignore"))
            if m:
                loc = m.group(1).strip()
                if loc not in locations:
                    locations.append(loc)
    finally:
        sock.close()
    for loc in locations:
        try:
            gw = _parse_igd(loc, log)
        except Exception as e:  # noqa: BLE001
            if log:
                log("[UPnP] 解析 %s 失败: %s" % (loc, e))
            continue
        if gw:
            gateways.append(gw)
    if log:
        log("[UPnP] 发现 %d 个 IGD（%d 个候选描述）" % (len(gateways), len(locations)))
    return gateways


def upnp_get_external_ip(control_url, service_type, timeout=5):
    """取路由器 WAN 侧公网 IP。"""
    status, xml = _http_soap(control_url, service_type, "GetExternalIPAddress", "", timeout)
    if status != 200:
        return None
    ip = _tag(xml, "NewExternalIPAddress")
    return ip or None


def upnp_map_port(internal_ip, internal_port, external_port=None,
                  control_url=None, service_type=None, desc="DGLab Socket",
                  lease=0, timeout=6, log=None):
    """
    在路由器上打洞：把 WAN 侧 external_port 映射到 internal_ip:internal_port。

    :return: {"ok","external_ip","external_port","internal","control_url","msg"}
    """
    external_port = int(external_port or internal_port)
    result = {"ok": False, "external_ip": None, "external_port": external_port,
              "internal": "%s:%s" % (internal_ip, internal_port), "control_url": None, "msg": ""}

    pairs = []
    if control_url and service_type:
        pairs.append((control_url, service_type))
    else:
        for gw in ssdp_discover_gateways(log=log):
            pairs.append((gw["control_url"], gw["service_type"]))

    if not pairs:
        result["msg"] = "没有发现支持 UPnP 的路由器（可能未开启 UPnP / 不是家用路由 / 防火墙拦了 SSDP）"
        return result

    last_err = ""
    for ctrl, stype in pairs:
        try:
            ext_ip = upnp_get_external_ip(ctrl, stype, timeout)
            arg = (
                "<NewRemoteHost></NewRemoteHost>"
                "<NewExternalPort>%d</NewExternalPort>"
                "<NewProtocol>TCP</NewProtocol>"
                "<NewInternalPort>%d</NewInternalPort>"
                "<NewInternalClient>%s</NewInternalClient>"
                "<NewEnabled>1</NewEnabled>"
                "<NewPortMappingDescription>%s</NewPortMappingDescription>"
                "<NewLeaseDuration>%d</NewLeaseDuration>"
            ) % (external_port, int(internal_port), internal_ip, desc, int(lease))
            status, xml = _http_soap(ctrl, stype, "AddPortMapping", arg, timeout)
            if status == 200 and "errorCode" not in xml.lower():
                result.update(ok=True, external_ip=ext_ip, control_url=ctrl,
                              msg="UPnP 端口映射成功：%s:%d -> %s:%d" % (ext_ip, external_port, internal_ip, internal_port))
                if log:
                    log("[UPnP] " + result["msg"])
                return result
            code = _tag(xml, "errorCode")
            desc_text = _tag(xml, "errorDescription")
            last_err = "HTTP %s errorCode=%s %s" % (status, code, desc_text)
            if code in ("718", "725"):
                last_err += "（该外网端口被占用/冲突，换个端口试试）"
            elif code == "403":
                last_err += "（路由器 UPnP 只允许本机网段做映射）"
            elif code == "401":
                last_err += "（路由器拒绝，UPnP 未开放或需要手动授权）"
            if log:
                log("[UPnP] %s 失败: %s" % (ctrl, last_err))
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if log:
                log("[UPnP] %s 异常: %s" % (ctrl, e))

    result["msg"] = last_err or "UPnP 映射失败"
    return result


def upnp_unmap_port(external_port, control_url=None, service_type=None, timeout=5, log=None):
    """删除之前打的洞。"""
    pairs = []
    if control_url and service_type:
        pairs.append((control_url, service_type))
    else:
        for gw in ssdp_discover_gateways(timeout=2.0, log=log):
            pairs.append((gw["control_url"], gw["service_type"]))
    ok_any = False
    for ctrl, stype in pairs:
        try:
            arg = ("<NewRemoteHost></NewRemoteHost><NewExternalPort>%d</NewExternalPort>"
                   "<NewProtocol>TCP</NewProtocol>") % int(external_port)
            status, xml = _http_soap(ctrl, stype, "DeletePortMapping", arg, timeout)
            ok_any = ok_any or (status == 200 and "errorCode" not in xml.lower())
        except Exception:  # noqa: BLE001
            continue
    return {"ok": ok_any}


# ======================================================================
# 7. cloudflared 隧道（内网穿透，可选）
# ======================================================================

CLOUDFLARED_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
                   "cloudflared-windows-amd64.exe")

# GitHub 直连在国内经常被重置，所以多准备几条线路，按顺序试。
# （实测：ghfast.top / gh-proxy.com / ghproxy.net 都能下，直连会 Connection reset）
CLOUDFLARED_MIRRORS = (
    "https://ghfast.top/" + CLOUDFLARED_URL,
    "https://gh-proxy.com/" + CLOUDFLARED_URL,
    "https://ghproxy.net/" + CLOUDFLARED_URL,
    "https://github.moeyy.xyz/" + CLOUDFLARED_URL,
    CLOUDFLARED_URL,
)
CLOUDFLARED_MIN_SIZE = 20 * 1024 * 1024      # 正常 50MB 左右，小于 20MB 一律当失败


def _is_pe_file(path, min_size=CLOUDFLARED_MIN_SIZE):
    """是不是一个像样的 exe（MZ 头 + 够大）。"""
    try:
        if os.path.getsize(path) < min_size:
            return False
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except Exception:  # noqa: BLE001
        return False


def _run_exe_quiet(path, args=("--version",), timeout=20):
    try:
        p = subprocess.run([path, *args], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="ignore", **_no_window_kwargs())
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def _download_with_curl(url, dest, log=None, timeout=1800):
    """优先用 Windows 自带的 curl.exe 下载（支持断点、错误信息清楚）。"""
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        for guess in (r"C:\Windows\System32\curl.exe", r"C:\Windows\SysWOW64\curl.exe"):
            if os.path.exists(guess):
                curl = guess
                break
    if not curl:
        return False, "系统里没有 curl"
    try:
        proc = subprocess.Popen(
            [curl, "-L", "--fail", "--silent", "--show-error", "--connect-timeout", "15",
             "--max-time", str(timeout), "-o", dest, url],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="ignore", **_no_window_kwargs())
    except Exception as e:  # noqa: BLE001
        return False, str(e)

    last_mb = 0
    while proc.poll() is None:
        time.sleep(1.5)
        try:
            mb = os.path.getsize(dest) // (1024 * 1024)
        except OSError:
            continue
        if log and mb >= last_mb + 5:
            last_mb = mb
            log("[公网]   已下载 %d MB ..." % mb)
    err = ""
    try:
        err = (proc.stderr.read() or "").strip()
    except Exception:  # noqa: BLE001
        pass
    if proc.returncode == 0 and _is_pe_file(dest):
        return True, ""
    return False, (err or ("curl 退出码 %s" % proc.returncode))[:200]


def _download_with_urllib(url, dest, log=None, timeout=300):
    """curl 不在时用 urllib 兜底。"""
    try:
        import urllib.request

        def hook(block_num, block_size, total_size):
            if log and total_size > 0:
                done = block_num * block_size / total_size * 100
                if int(done) % 20 == 0:
                    log("[公网]   已下载 %d%%" % min(int(done), 100))

        urllib.request.urlretrieve(url, dest, hook)
        return (_is_pe_file(dest), "下载的文件不完整或不对")
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def cloudflared_local_path():
    r"""默认安装位置：%LOCALAPPDATA%cloudflaredcloudflared.exe（不需要管理员权限）"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "cloudflared", "cloudflared.exe")


def find_cloudflared():
    """找 cloudflared：PATH -> 本程序目录 -> %LOCALAPPDATA% -> Program Files"""
    for name in ("cloudflared", "cloudflared.exe"):
        p = shutil.which(name)
        if p:
            return p
    here = os.path.dirname(os.path.abspath(__file__))
    guesses = [
        os.path.join(here, "cloudflared.exe"),
        cloudflared_local_path(),
        os.path.join(os.path.expanduser("~"), "cloudflared", "cloudflared.exe"),
        r"C:\Program Files\cloudflared\cloudflared.exe",
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    ]
    for guess in guesses:
        if os.path.exists(guess):
            return guess
    return None


def ensure_cloudflared(download=True, log=None, timeout=600):
    r"""
    没有 cloudflared 就自动下载一个（约 50MB，下到 %LOCALAPPDATA%cloudflared）。
    Cloudflare 的 TryCloudflare 隧道是免费的，不用注册账号，等于「帮你找了个公网服务器」。

    :return: (exe路径 或 None, 说明文字)
    """
    exe = find_cloudflared()
    if exe:
        return exe, "已找到 cloudflared: %s" % exe
    if not download:
        return None, "没找到 cloudflared，且未开启自动下载"

    dest = cloudflared_local_path()
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    tmp = dest + ".part"
    if log:
        log("[公网] 没找到 cloudflared，自动下载（共 %d 条线路，会自动换线）" % len(CLOUDFLARED_MIRRORS))

    errors = []
    for idx, url in enumerate(CLOUDFLARED_MIRRORS, 1):
        host = url.split("/")[2]
        if log:
            log("[公网] 线路 %d/%d: %s" % (idx, len(CLOUDFLARED_MIRRORS), host))
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass

        ok, err = _download_with_curl(url, tmp, log)
        if not ok:
            if log:
                log("[公网]   这条线路不行（%s），换下一条" % err)
            ok, err = _download_with_urllib(url, tmp, log)

        if ok:
            if log:
                log("[公网]   下载完成，正在校验 ...")
            if _is_pe_file(tmp):
                rc, out = _run_exe_quiet(tmp)
                if rc == 0 and "cloudflared" in out.lower():
                    try:
                        os.replace(tmp, dest)
                    except Exception as e:  # noqa: BLE001
                        errors.append("%s: 保存失败 %s" % (host, e))
                        continue
                    if log:
                        log("[公网] cloudflared 就绪：%s（%s）" % (dest, out.splitlines()[0][:40]))
                    return dest, "已下载 cloudflared"
                errors.append("%s: 下载到的文件跑不起来" % host)
            else:
                errors.append("%s: 下载到的文件不完整" % host)
        else:
            errors.append("%s: %s" % (host, err))
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass

    msg = ("所有下载线路都失败了（%s）。你的网络可能对 GitHub 及其镜像都有限制。" + chr(10)
           + "手动方案：找个能下的地方拿到 cloudflared.exe，放到这个位置就行：" + chr(10)
           + "  %s" % dest + chr(10)
           + "或者：不装 cloudflared，直接点『免费隧道（不用下载）』。"
           ) % "；".join(errors[:3])
    if log:
        log("[公网] " + msg)
    return None, msg


# ======================================================================
# 7.5 SSH 免费隧道（不用下载任何东西，Windows 自带 ssh.exe）
# ======================================================================

SSH_OPTS = ("-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=NUL",
            "-o", "BatchMode=yes", "-o", "ServerAliveInterval=20",
            "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=15")

SSH_PROVIDERS = {
    "localhost.run": {
        "cmd": ["ssh", *SSH_OPTS, "-R", "80:localhost:{port}", "nokey@localhost.run"],
        "url": r"https://[a-z0-9-]+.lhr.life",
        "note": "免费、免注册；随机子域名，每次启动会变",
    },
    "serveo": {
        "cmd": ["ssh", *SSH_OPTS, "-R", "80:localhost:{port}", "serveo.net"],
        "url": r"https://[a-z0-9-]+.serveo.net",
        "note": "免费；需要注册 SSH key 才能用（没 key 会 Permission denied）",
    },
    "pinggy": {
        "cmd": ["ssh", *SSH_OPTS, "-p", "443", "-R0:localhost:{port}", "a.pinggy.io"],
        "url": r"https://[a-z0-9-]+.(?:a.free.pinggy.link|pinggy.link)",
        "note": "免费；现在多要求登录/带 token",
    },
}


def ssh_available():
    return bool(shutil.which("ssh"))


def start_ssh_tunnel(provider="localhost.run", port=5678, wait=40.0, log=None):
    """
    用系统自带的 ssh 开一条免费隧道（无需下载、无需注册）。

    :return: {"ok","url","msg","proc"}
    """
    spec = SSH_PROVIDERS.get(provider)
    if not spec:
        return {"ok": False, "msg": "未知的隧道服务: %s" % provider}
    if not ssh_available():
        return {"ok": False, "msg": "系统里没有 ssh 命令（Windows 可选功能里装 OpenSSH 客户端即可）"}

    cmd = [c.format(port=port) for c in spec["cmd"]]
    if log:
        log("[免费隧道] 正在连接 %s（不用下载，用系统自带 ssh）..." % provider)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="ignore",
                                **_no_window_kwargs())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": "启动 ssh 失败: %s" % e}

    q = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                q.put(line)
        except Exception:  # noqa: BLE001
            pass
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    url = None
    tail = []
    deadline = time.time() + wait
    while time.time() < deadline and url is None:
        try:
            line = q.get(timeout=1.0)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if line is None:
            break
        text = line.strip()
        if text:
            tail.append(text[:160])
            tail = tail[-8:]
            if ("Permission denied" in text) or ("Operation timed out" in text) or                     ("Connection refused" in text) or ("Could not resolve" in text):
                break
        m = re.search(spec["url"], line)
        if m:
            url = m.group(0)

    if not url:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "msg": "没拿到地址（%s）%s" % (
            spec.get("note", ""), (" | ".join(tail[-3:])[:200] if tail else "")),
            "proc": None}

    if log:
        log("[免费隧道] 成功：%s" % url)
    return {"ok": True, "url": url, "msg": "隧道已建立", "proc": proc, "provider": provider}


# ======================================================================
# 8. 映射会话对象（GUI 直接用它）
# ======================================================================

class IntranetMapper:
    """
    一个「内网映射会话」：管一个本地端口，负责产出口径统一的各种可连接地址。

    用法::

        m = IntranetMapper(5678, log=print)
        m.refresh()
        print(m.lan_uri())            # ws://192.168.1.99:5678
        m.enable_upnp()               # 路由器打洞
        print(m.public_uri())         # ws://公网IP:外网端口
        qr = m.qrcode(client_id)      # 官方二维码内容
    """

    def __init__(self, port=5678, host=None, log=None):
        self.port = int(port)
        self.host = host                 # 指定写进二维码的局域网 IP；None = 自动
        self.log = log or (lambda _m: None)
        self.candidates = []
        self._lan_ip = None
        self._public = None              # {"uri","external_ip","external_port","control_url"}
        self._tunnel = None              # {"proc","url"}
        self._lock = threading.Lock()

    # ---------- 地址 ----------
    def refresh(self):
        self.candidates = list_ipv4_candidates()
        self._lan_ip = self.host or pick_lan_ip()
        self.log("[映射] 局域网映射地址 -> %s:%d" % (self._lan_ip, self.port))
        return self._lan_ip

    @property
    def lan_ip(self):
        if not self._lan_ip:
            self.refresh()
        return self._lan_ip

    def set_host(self, host):
        self.host = (host or "").strip() or None
        self._lan_ip = self.host or pick_lan_ip()
        return self._lan_ip

    def lan_uri(self):
        return build_socket_uri(self.lan_ip, self.port)

    def public_uri(self):
        return self._public["uri"] if self._public else None

    def active_uri(self):
        """优先公网映射地址，没有就局域网地址。"""
        return self.public_uri() or self.lan_uri()

    def qrcode(self, client_id):
        return build_qrcode_url(self.active_uri(), client_id)

    def qrcode_for(self, uri, client_id):
        return build_qrcode_url(uri, client_id)

    def self_test(self, uri=None):
        uri = uri or self.active_uri()
        m = re.match(r"^wss?://(\[[^\]]+\]|[^:/]+)(?::(\d+))?", uri)
        if not m:
            return False, "地址解析失败: %s" % uri
        host = m.group(1)
        port = int(m.group(2) or (443 if uri.startswith("wss") else 80))
        return check_tcp(host, port)

    # ---------- UPnP 映射 ----------
    def enable_upnp(self, external_port=None):
        self.refresh()
        host = self.lan_ip
        if host in ("127.0.0.1", ""):
            return {"ok": False, "msg": "没有可用的局域网 IP，无法做 UPnP 映射"}
        res = upnp_map_port(host, self.port, external_port=external_port, log=self.log)
        if res.get("ok") and res.get("external_ip"):
            with self._lock:
                self._public = {
                    "uri": build_socket_uri(res["external_ip"], res["external_port"]),
                    "external_ip": res["external_ip"],
                    "external_port": res["external_port"],
                    "control_url": res.get("control_url"),
                    "mode": "UPnP",
                }
            self.log("[映射] 公网映射地址 -> %s" % self._public["uri"])
            # 公网 IP 自检（有些内网 IP 6 打洞后仍不可从外网回环，属正常）
            ok, msg = self.self_test(self._public["uri"])
            res["selftest"] = msg
            res["uri"] = self._public["uri"]
            if not ok:
                self.log("[映射] 提示：本机无法回环验证公网地址（%s），需要用手机实测" % msg)
        return res

    def disable_upnp(self):
        with self._lock:
            pub = self._public
            self._public = None
        if pub:
            upnp_unmap_port(pub["external_port"], control_url=pub.get("control_url"), log=self.log)
            self.log("[映射] 已删除路由器上的端口映射 %s:%d" % (pub["external_ip"], pub["external_port"]))
            return {"ok": True}
        return {"ok": False, "msg": "当前没有 UPnP 映射"}

    # ---------- cloudflared 隧道 ----------
    def start_tunnel(self, wait=25.0, auto_download=True):
        exe, note = ensure_cloudflared(download=auto_download, log=self.log)
        if not exe:
            return {"ok": False, "msg": note}

        try:
            proc = subprocess.Popen(
                [exe, "tunnel", "--url", "http://127.0.0.1:%d" % self.port, "--no-autoupdate"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="ignore", **_no_window_kwargs())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": "启动 cloudflared 失败: %s" % e}

        holder = {"url": None}

        def reader():
            for line in proc.stdout:  # type: ignore[union-attr]
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and not holder["url"]:
                    holder["url"] = m.group(0)
                if self.log and ("ERR" in line or "error" in line.lower()):
                    self.log("[隧道] " + line.strip()[:160])

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        deadline = time.time() + wait
        while time.time() < deadline and not holder["url"]:
            time.sleep(0.3)
            if proc.poll() is not None:
                break
        if not holder["url"]:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "msg": "cloudflared 没有返回隧道地址（可能需要登录或网络受限）"}

        with self._lock:
            self._tunnel = {"proc": proc, "url": holder["url"], "kind": "cloudflared"}
            self._public = {
                "uri": self._tunnel["url"].replace("https://", "wss://"),
                "external_ip": urllib.parse.urlsplit(holder["url"]).hostname,
                "external_port": 443,
                "mode": "cloudflared",
            }
        self.log("[映射] 公网隧道 -> %s" % self._public["uri"])
        return {"ok": True, "uri": self._public["uri"], "msg": "隧道已建立"}

    # ---------- 一键开公网：按顺序自动试各种免费方案 ----------
    def start_public(self, prefer=None, wait=45.0, dns_wait=30.0):
        """
        自动开一条公网通道，成功即返回。顺序：
          1) cloudflared（Cloudflare 免费隧道，最稳，但要下载)
          2) localhost.run（系统自带 ssh，不用下载任何东西）
          3) serveo / pinggy（同上，能连上的话）

        :return: {"ok","uri","provider","msg"}
        """
        order = []
        if prefer:
            order.append(prefer)
        for name in ("cloudflared", "localhost.run", "serveo", "pinggy"):
            if name not in order:
                order.append(name)

        tried = []
        for name in order:
            self.log("[公网] === 尝试 %s ===" % name)
            try:
                if name == "cloudflared":
                    res = self.start_tunnel(wait=wait, auto_download=True)
                else:
                    res = start_ssh_tunnel(name, self.port, wait=wait, log=self.log)
                    if res.get("ok"):
                        url = res["url"]
                        with self._lock:
                            self._tunnel = {"proc": res.get("proc"), "url": url, "kind": name}
                            self._public = {
                                "uri": url.replace("https://", "wss://"),
                                "external_ip": urllib.parse.urlsplit(url).hostname,
                                "external_port": 443,
                                "mode": name,
                            }
                        self.log("[公网] 公网地址 -> %s" % self._public["uri"])
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "msg": str(e)}

            if res.get("ok"):
                pub = self.public_uri()
                if pub:
                    host = urllib.parse.urlsplit(pub).hostname
                    if dns_wait and host:
                        self.log("[公网] 等域名生效（本机 DNS 可能要十几秒，手机通常更快）...")
                        for _ in range(int(dns_wait // 2)):
                            try:
                                socket.getaddrinfo(host, 443)
                                self.log("[公网] 域名已生效：%s" % host)
                                break
                            except Exception:  # noqa: BLE001
                                time.sleep(2)
                    return {"ok": True, "uri": pub, "provider": name,
                            "msg": "%s 隧道已建立" % name}
                res = {"ok": False, "msg": "拿到地址但没记住（内部状态异常）"}

            tried.append("%s: %s" % (name, str(res.get("msg"))[:80]))

        msg = "所有免费方案都没成功：" + " | ".join(tried)
        self.log("[公网] " + msg)
        return {"ok": False, "uri": None, "provider": None, "msg": msg}

    def stop_tunnel(self):
        with self._lock:
            tun = self._tunnel
            self._tunnel = None
            if self._public and self._public.get("mode") in ("cloudflared", "localhost.run",
                                                            "serveo", "pinggy"):
                self._public = None
        if tun:
            try:
                proc = tun.get("proc")
                if proc is not None:
                    pid = None
                    try: pid = proc.pid
                    except Exception: pass
                    try: proc.terminate()
                    except Exception: pass
                    if pid:
                        try:
                            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                                           capture_output=True, timeout=10,
                                           creationflags=_CREATE_NO_WINDOW)
                        except Exception:
                            pass
            except Exception:  # noqa: BLE001
                pass
            self.log("[映射] 隧道(%s)已关闭" % tun.get("kind", "?"))
            return {"ok": True}
        return {"ok": False, "msg": "没有正在运行的隧道"}

    def stop(self):
        self.stop_tunnel()
        self.disable_upnp()

    # ---------- 汇总 ----------
    def status_text(self, client_id=None):
        lines = ["局域网映射: %s" % self.lan_uri()]
        if self._public:
            lines.append("公网映射  : %s  [%s]" % (self._public["uri"], self._public.get("mode", "?")))
        else:
            lines.append("公网映射  : 未开启（仅同一 Wi-Fi 可连）")
        if client_id:
            lines.append("二维码内容: %s" % self.qrcode(client_id))
        return "\n".join(lines)


# ======================================================================
# 9. 命令行自检：python lan_map.py
# ======================================================================

def _main():
    print("=" * 68)
    print("DGLab 内网映射自检 (lan_map.py)")
    print("=" * 68)

    print("\n[1] 本机 IPv4 候选（按『手机能不能连』打分排序）")
    for c in list_ipv4_candidates():
        print("   %-16s score=%-4d usable=%-5s %-28s %s" % (
            c["ip"], c["score"], c["usable"], c["adapter"], c["note"]))
    best = pick_lan_ip()
    print("   -> 自动选择: %s" % best)

    m = IntranetMapper(5678, log=lambda s: print("   " + s))
    m.refresh()
    print("\n[2] Socket 地址映射")
    print("   局域网 URI: %s" % m.lan_uri())
    print("   局域网二维码: %s" % m.qrcode("<CLIENT_ID>"))

    print("\n[3] UPnP 路由器发现")
    gws = ssdp_discover_gateways(timeout=3.0, log=lambda s: print("   " + s))
    for gw in gws:
        print("   %s  service=%s  control=%s" % (gw.get("friendly_name"), gw["service_type"], gw["control_url"]))
    if not gws:
        print("   （没发现 IGD：路由器没开 UPnP，或当前网络不是家用路由）")

    print("\n[4] cloudflared 内网穿透")
    exe = find_cloudflared()
    print("   %s" % (exe if exe else "未安装 cloudflared（可选）"))

    print("\n[5] Windows 端口代理当前配置")
    print("   " + (netsh_portproxy_show().replace("\n", "\n   ") or "（无）"))
    print("\n完成。")


if __name__ == "__main__":
    _main()
