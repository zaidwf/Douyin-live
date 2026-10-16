#!/usr/bin/python
# coding:utf-8
"""以抖音官方网页客户端为基准，抓它自己的 WebSocket 消息，核对 method 集合

背景：栋梁(福袋间)/与辉同行(带货)/爱乐-L(娱乐，粉丝团活跃) 三个房间的直连 WS 都收不到
`WebcastGiftMessage`，且 `_unhandled_methods` 里也没有任何礼物相关方法名。
需要判定：是「Web 通道本就不下发礼物」还是「我们的连接参数与真实客户端不同」。

做法：用 Playwright 打开真实直播间页面，拦截页面自己建立的 WebSocket 二进制帧，
按 PushFrame→Response 解出 method 集合，与本项目直连的结果逐项比对。

用法：
  python probe_page_ws.py --id 731918444565 --duration 120 [--auth]
"""
import argparse
import base64
import gzip
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright
from protobuf.douyin import PushFrame, Response

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH = os.path.join(ROOT, "config", "auth.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="731918444565")
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--auth", action="store_true", help="加载 config/auth.json 登录态")
    args = ap.parse_args()

    methods = Counter()
    frames = Counter()
    errors = Counter()
    urls = set()
    lock = threading.Lock()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--autoplay-policy=no-user-gesture-required"],
        )
        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if args.auth and os.path.exists(AUTH):
            ctx_kwargs["storage_state"] = AUTH
        ctx = browser.new_context(**ctx_kwargs)

        def on_ws(ws):
            with lock:
                urls.add(ws.url.split('?')[0])
            print(f"[WS] {ws.url[:110]}", flush=True)

            def on_recv(payload):
                frames['total'] += 1
                raw = None
                if isinstance(payload, bytes):
                    raw = payload
                    frames['binary'] += 1
                elif isinstance(payload, str):
                    # Playwright 对二进制帧可能给 base64 字符串（视版本而定）
                    frames['text'] += 1
                    try:
                        raw = base64.b64decode(payload)
                    except Exception:
                        return
                if not raw:
                    return
                try:
                    frame = PushFrame().parse(raw)
                    if frame.payload_type != 'msg':
                        frames[f'pt:{frame.payload_type}'] += 1
                        return
                    body = frame.payload
                    if body[:2] == b'\x1f\x8b':
                        body = gzip.decompress(body)
                    resp = Response().parse(body)
                    for m in resp.messages_list:
                        methods[m.method] += 1
                except Exception as e:
                    errors[type(e).__name__] += 1

            ws.on("framereceived", on_recv)

        ctx.on("websocket", on_ws)
        page = ctx.new_page()
        url = f"https://live.douyin.com/{args.id}"
        print(f"[打开] {url}  auth={args.auth}", flush=True)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # 等播放器起来并开始收消息
        time.sleep(args.duration)
        browser.close()

    print("\n" + "=" * 72, flush=True)
    print(f"页面自身 WS 抓取结果（{args.duration}s，auth={args.auth}）", flush=True)
    print("=" * 72, flush=True)
    print(f"WS 端点: {sorted(urls)}", flush=True)
    print(f"帧统计: {dict(frames)}   解码异常: {dict(errors)}", flush=True)
    print(f"\n页面收到的 method 共 {len(methods)} 种:", flush=True)
    for m, c in methods.most_common():
        flag = '  ← 礼物' if 'Gift' in m else ''
        print(f"  {c:6d}  {m}{flag}", flush=True)
    gift = [m for m in methods if 'Gift' in m]
    print(f"\n礼物相关 method = {gift}", flush=True)
    print(f"\n与本项目直连对比: 本项目 17 种（含 WebcastChatMessage 等），礼物 method = []", flush=True)


if __name__ == "__main__":
    main()
