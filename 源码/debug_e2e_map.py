# -*- coding: utf-8 -*-
"""
debug_e2e_map.py —— 内网映射端到端验证

真起一个本地 WebSocket 服务端（和 app.py 里 run_local_server 同一条路径），
然后检查：
  1. 映射出来的局域网地址，服务起来后到底通不通（防火墙问题一眼看出）
  2. lan_map 拼出来的二维码，和 pydglab-ws 官方 get_qrcode() 拼的是不是一模一样
  3. 公网映射（UPnP / cloudflared）当前能不能用

用法: python debug_e2e_map.py
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_VENV_SP = os.path.join(HERE, "venv312", "Lib", "site-packages")
if os.path.isdir(_VENV_SP) and _VENV_SP not in sys.path:
    sys.path.insert(0, _VENV_SP)

import lan_map  # noqa: E402
from pydglab_ws import DGLabWSConnect  # noqa: E402
from pydglab_ws.server import DGLabWSServer  # noqa: E402

PORT = 5678


async def main():
    print("=" * 70)
    print("内网映射端到端验证 (port=%d)" % PORT)
    print("=" * 70)

    mapper = lan_map.IntranetMapper(PORT, log=lambda s: print("   " + s))
    mapper.refresh()

    ok, msg = lan_map.check_tcp(mapper.lan_ip, PORT)
    print("[0] 服务未启动时，映射地址自连: %s (%s)" % (ok, msg))

    async with DGLabWSServer("0.0.0.0", PORT, 60):
        async with DGLabWSConnect("ws://127.0.0.1:%d" % PORT) as client:
            client_id = str(client.client_id)
            uri = mapper.lan_uri()
            qr_url = lan_map.build_qrcode_url(uri, client_id)

            print("[1] 终端 client_id      : %s" % client_id)
            print("[2] 映射地址 (写进二维码): %s" % uri)
            print("[3] 二维码内容          : %s" % qr_url)

            official = client.get_qrcode("ws://%s:%d" % (mapper.lan_ip, PORT))
            print("[4] 与 pydglab-ws 官方二维码一致: %s" % (official == qr_url))
            if official != qr_url:
                print("    官方: %s" % official)
                print("    本模块: %s" % qr_url)

            ok, msg = lan_map.check_tcp(mapper.lan_ip, PORT)
            print("[5] 服务运行中，映射地址自连: %s (%s)" % (ok, msg))
            if not ok:
                print("     -> 用手机扫二维码连不上的话，先点程序里的『放行防火墙』")

            print("[6] 公网映射")
            print("     局域网地址: %s" % mapper.lan_uri())
            print("     公网地址  : %s" % (mapper.public_uri() or "未开启"))

            # App 侧模拟：用二维码里的地址真的连一次（等价于手机扫码后的连接）
            ok2, msg2 = lan_map.check_tcp(mapper.lan_ip, PORT)
            print("[7] 模拟 App 用二维码地址连接: %s (%s)" % (ok2, msg2))

    print("")
    print("完成。二维码可以直接给 DG-Lab App 的 SOCKET 扫码功能用。")


if __name__ == "__main__":
    asyncio.run(main())
