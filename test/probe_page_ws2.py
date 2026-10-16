#!/usr/bin/python
# coding:utf-8
"""在页面内劫持 WebSocket，抓抖音官方网页**真正收到**的消息（含 Worker 内建连）

背景：`context.on('websocket')` 在无头 Chrome 里对抖音直播页观测到 **0 个 WS**，
但页面弹幕正常渲染、礼物面板和致谢弹幕都在 —— 怀疑 WS 建在 Worker 里（Playwright 版本
对 Worker 内 WebSocket 的捕获不可靠）。

做法：用 `add_init_script` 在主文档与所有 Worker 中替换 `WebSocket` 类，记录
每个实例的 URL 与收到的二进制帧（base64），再从 Python 侧解码 PushFrame→Response，
统计 method 分布，重点看 `WebcastGiftMessage`。

用法：
  python probe_page_ws2.py --id 731918444565 --duration 150 [--auth]
"""
import argparse
import base64
import gzip
import os
import sys
import time
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright
from protobuf.douyin import PushFrame, Response

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH = os.path.join(ROOT, "config", "auth.json")

INIT_JS = r"""
(() => {
  try {
    const Orig = WebSocket;
    const store = { urls: [], frames: [], errors: [] };
    Object.defineProperty(window, '__wsHook', { value: store, configurable: true });

    function record(url, data) {
      try {
        let b64 = null;
        if (data instanceof ArrayBuffer) {
          const u8 = new Uint8Array(data);
          let bin = '';
          for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
          b64 = btoa(bin);
        } else if (data instanceof Blob) {
          return;  // 异步读太慢，交由 ArrayBuffer 分支覆盖
        } else if (typeof data === 'string') {
          return;
        }
        if (b64) store.frames.push({ url: String(url), b64: b64 });
        if (store.frames.length > 4000) store.frames.splice(0, 2000);
      } catch (e) { store.errors.push(String(e)); }
    }

    class HookedWS extends Orig {
      constructor(url, protocols) {
        super(url, protocols);
        store.urls.push(String(url));
        this.addEventListener('message', (ev) => record(url, ev.data));
      }
    }
    HookedWS.prototype = Orig.prototype;
    try { Object.assign(HookedWS, Orig); } catch (e) {}
    window.WebSocket = HookedWS;

    if (typeof self !== 'undefined' && self.postMessage) {
      self.addEventListener('message', () => {});
    }
  } catch (e) {
    try { window.__wsHook = { urls: [], frames: [], errors: ['init:' + String(e)] }; } catch (_) {}
  }
})();
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="731918444565")
    ap.add_argument("--duration", type=int, default=150)
    ap.add_argument("--auth", action="store_true")
    args = ap.parse_args()

    methods = Counter()
    bad = Counter()
    total_frames = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        kw = {"viewport": {"width": 1400, "height": 900},
              "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        if args.auth and os.path.exists(AUTH):
            kw["storage_state"] = AUTH
        ctx = browser.new_context(**kw)
        ctx.add_init_script(INIT_JS)
        page = ctx.new_page()
        page.goto(f"https://live.douyin.com/{args.id}", wait_until="domcontentloaded", timeout=60000)

        start = time.time()
        next_report = 30
        while time.time() - start < args.duration:
            time.sleep(3)
            try:
                data = page.evaluate(
                    "(() => { const h = window.__wsHook; if(!h) return null;"
                    " const f = h.frames; h.frames = []; return {urls: h.urls, frames: f, errors: h.errors}; })()")
            except Exception as e:
                bad[f'evaluate:{type(e).__name__}'] += 1
                continue
            if not data:
                bad['no_hook'] += 1
                continue
            for f in data["frames"]:
                total_frames += 1
                try:
                    raw = base64.b64decode(f["b64"])
                    frame = PushFrame().parse(raw)
                    if frame.payload_type != 'msg':
                        bad[f'pt:{frame.payload_type}'] += 1
                        continue
                    body = frame.payload
                    if body[:2] == b'\x1f\x8b':
                        body = gzip.decompress(body)
                    resp = Response().parse(body)
                    for m in resp.messages_list:
                        methods[m.method] += 1
                except Exception as e:
                    bad[type(e).__name__] += 1
            el = int(time.time() - start)
            if el >= next_report:
                g = sum(v for k, v in methods.items() if 'Gift' in k)
                print(f"[进度 {el}s] 帧={total_frames} method {len(methods)} 种 礼物={g} "
                      f"WS url={[u[:60] for u in dict.fromkeys(data['urls'])]}", flush=True)
                next_report += 30
        browser.close()

    print("\n" + "=" * 72, flush=True)
    print(f"页面内 hook 结果（{args.duration}s，auth={args.auth}）", flush=True)
    print("=" * 72, flush=True)
    print(f"帧总数={total_frames}  解码异常={dict(bad)}", flush=True)
    print(f"\nmethod 分布（{len(methods)} 种）:", flush=True)
    for m, c in methods.most_common():
        print(f"  {c:6d}  {m}{'   ← 礼物' if 'Gift' in m else ''}", flush=True)
    g = sum(v for k, v in methods.items() if 'Gift' in k)
    print(f"\n礼物消息 = {g} 条", flush=True)


if __name__ == "__main__":
    main()
