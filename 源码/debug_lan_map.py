# -*- coding: utf-8 -*-
"""
debug_lan_map.py —— 内网映射现场诊断

用法:
    python debug_lan_map.py

它会依次打印:
  1. 本机所有 IPv4 + 适配器名 + 可连性评分（看是不是选错网卡了）
  2. SSDP / UPnP 路由器的**原始回包**（判断是路由器没开 UPnP，还是被防火墙拦了）
  3. 这个端口到底有没有在监听、局域网地址能不能自连
  4. 最终二维码里会写成什么地址
"""
import re
import socket
import time

import lan_map

PORT = 5678


def raw_ssdp(timeout=4.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(0.5)
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           "HOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           "ST: ssdp:all\r\n\r\n")
    try:
        s.sendto(msg.encode(), ("239.255.255.250", 1900))
    except Exception as e:
        print("   SSDP 发送失败: %s" % e)
        return []
    replies = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(65507)
        except socket.timeout:
            continue
        except Exception:
            break
        replies.append((addr, data.decode("utf-8", "ignore")))
    s.close()
    return replies


def main():
    print("=" * 70)
    print("DGLab 内网映射诊断")
    print("=" * 70)

    print("")
    print("[1] 网卡与候选地址")
    for c in lan_map.list_ipv4_candidates():
        print("   %-16s %-5s score=%-4d adapter=%s" % (
            c["ip"], "OK" if c["usable"] else "跳过", c["score"], c["adapter"] or "(未知)"))
        print("        %s" % c["note"])
    best = lan_map.pick_lan_ip()
    print("   -> 自动选择: %s" % best)

    replies = raw_ssdp()
    print("")
    print("[2] SSDP 原始回包（收到 %d 个）" % len(replies))
    if not replies:
        print("   路由器没有任何回包 -> UPnP 关着 / 被防火墙拦了 / 当前网络不是家用路由")
    for addr, text in replies:
        first = [l.strip() for l in text.splitlines() if l.strip()][:1]
        loc = re.search(r"(?im)^location:\s*(\S+)", text)
        print("   来自 %s: %s" % (addr[0], first[0] if first else ""))
        if loc:
            print("      LOCATION=%s" % loc.group(1))
    gws = lan_map.ssdp_discover_gateways(timeout=3.0)
    print("   解析出的 IGD: %d 个" % len(gws))
    for gw in gws:
        print("      %s | %s | %s" % (gw.get("friendly_name"), gw["service_type"], gw["control_url"]))

    print("")
    print("[3] 端口监听 / 自连测试 (%s:%d)" % (best, PORT))
    ok, msg = lan_map.check_tcp(best, PORT)
    print("   %s  %s" % ("OK  " if ok else "FAIL", msg))
    if not ok:
        print("   （程序里还没点『生成局域网二维码』时连不上是正常的；")
        print("     点完再跑一次本诊断，若仍失败就是 Windows 防火墙拦了入站）")

    print("")
    print("[4] 二维码地址口径")
    m = lan_map.IntranetMapper(PORT, log=lambda s: print("   " + s))
    m.refresh()
    print("   局域网: %s" % m.qrcode("<CLIENT_ID>"))
    if m.public_uri():
        print("   公网  : %s" % m.public_uri())
    else:
        print("   公网  : 未开启（UPnP 不可用时可装 cloudflared 走隧道）")


if __name__ == "__main__":
    main()
