#!/usr/bin/python
# coding:utf-8 -*-
"""礼物口径对拍：陈旧连接上下文 vs im/fetch 预取的新鲜上下文

已验证：/webcast/im/fetch/ 在**携带完整 douyin cookie 集**时可用（游客仅 ttwid 时返回空体），
且响应里带回 cursor / internal_ext / push_server_v2。本脚本对拍：

  S (Stale)：现网行为 —— 用 2024 年历史抓包的 cursor/internal_ext + lq 边缘节点
  F (Fresh)：先 im/fetch 拿新鲜 cursor/internal_ext/push_server_v2 再建 WS

若 F 收到 WebcastGiftMessage 而 S 没有，则证明礼物缺失源于连接上下文陈旧（服务端据此
把客户端判定为历史/降级会话）。

用法：
  python probe_gift_freshctx.py --id 568544961053 --duration 600
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from protobuf.douyin import Response
from liveMan import DouyinLiveWebFetcher, generate_a_bogus, generateMsToken

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_FILE = os.path.join(ROOT, "config", "auth.json")


def load_login_cookie():
    if not os.path.exists(AUTH_FILE):
        return None
    state = json.load(open(AUTH_FILE, encoding='utf-8'))
    parts = [f"{c['name']}={c['value']}" for c in state.get('cookies', [])
             if c.get('name') and c.get('value') is not None
             and ('douyin.com' in c.get('domain', '') or 'jinritemai' in c.get('domain', ''))]
    return "; ".join(parts) if parts else None


def im_fetch_context(live_id, room_id, cookie):
    """请求 im/fetch，返回 (cursor, internal_ext, push_server_v2, 方法分布)"""
    f = DouyinLiveWebFetcher(live_id, None)
    ms = generateMsToken()
    q = '&'.join([
        'resp_content_type=protobuf', 'did_rule=3', 'device_id=',
        'app_name=douyin_web', 'endpoint=live_pc', 'support_wrds=1',
        f'user_unique_id={f.user_unique_id}', 'identity=audience',
        'need_persist_msg_count=15', 'insert_task_id=', 'live_reason=',
        f'room_id={room_id}', 'version_code=180800', 'last_rtt=0',
        'live_id=1', 'aid=6383', 'fetch_rule=1', 'cursor=', 'internal_ext=',
        'device_platform=web', 'cookie_enabled=true', 'screen_width=1920',
        'screen_height=1080', 'browser_language=zh-CN', 'browser_platform=Win32',
        'browser_name=Mozilla',
        'browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36',
        'browser_online=true', 'tz_name=Asia/Shanghai', f'msToken={ms}',
    ])
    ab = generate_a_bogus(q, f.user_agent)
    hdr = {
        'User-Agent': f.user_agent, 'Accept': '*/*', 'Accept-Encoding': 'identity',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Referer': f'https://live.douyin.com/{live_id}', 'Cookie': cookie,
    }
    r = requests.get(f'https://live.douyin.com/webcast/im/fetch/?{q}&a_bogus={ab}',
                     headers=hdr, timeout=20)
    if r.status_code != 200 or not r.content:
        return None, None, None, Counter()
    resp = Response().parse(r.content)
    return resp.cursor, resp.internal_ext, resp.push_server, Counter(
        m.method for m in resp.messages_list)


class Arm:
    def __init__(self, label, live_id, mode, cookie):
        self.label = label
        self.live_id = live_id
        self.mode = mode  # 'stale' | 'fresh'
        self.cookie = cookie
        self.counters = Counter()
        self.gift_samples = []
        self.lock = threading.Lock()
        self.fetcher = None
        self.room_id = None

    def log_cb(self, log_type, message):
        if log_type in ('GIFT', 'ERROR', 'WEBSOCKET'):
            print(f"[{self.label}][{datetime.now().strftime('%H:%M:%S')}][{log_type}] "
                  f"{message[:240]}", flush=True)

    def run(self, duration):
        f = DouyinLiveWebFetcher(self.live_id, log_callback=self.log_cb)
        self.fetcher = f
        f.cookie_string = self.cookie
        f._DouyinLiveWebFetcher__room_id = self.room_id

        if self.mode == 'fresh':
            cur, ext, push, dist = im_fetch_context(self.live_id, self.room_id, self.cookie)
            print(f"[{self.label}] im/fetch: cursor={cur!r} push={push!r} 近期方法={dict(dist)}",
                  flush=True)
            if not cur:
                print(f"[{self.label}] im/fetch 失败，退化为 stale", flush=True)
            else:
                f.set_ws_context(cursor=cur, internal_ext=ext, push_host=push)

        def data_cb(event_type, data):
            with self.lock:
                self.counters[event_type] += 1
            if event_type == 'gift':
                with self.lock:
                    self.gift_samples.append(data)
                print(f"[{self.label}][GIFT] {data.get('nickname')} -> {data.get('gift_name')} "
                      f"x{data.get('gift_count')} 单价={data.get('diamond_value')} "
                      f"repeat_end={data.get('repeat_end')} group_id={data.get('group_id')}",
                      flush=True)

        f.data_callback = data_cb
        t = threading.Thread(target=f.start, daemon=True)
        t.start()
        time.sleep(duration)
        f.stop()
        return self


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="568544961053")
    ap.add_argument("--duration", type=int, default=600)
    args = ap.parse_args()

    cookie = load_login_cookie()
    print(f"[cookie] {'已加载 config/auth.json' if cookie else '缺失 → 游客态'}")

    f0 = DouyinLiveWebFetcher(args.id, None)
    st = f0.get_room_status()
    if not st[0]:
        print(f"未开播：{st}")
        return
    room_id = f0.room_id
    print(f"[对拍] {st[2]} room_id={room_id} 时长={args.duration}s\n", flush=True)

    arms = [Arm("S", args.id, 'stale', cookie), Arm("F", args.id, 'fresh', cookie)]
    for a in arms:
        a.room_id = room_id

    # fresh 组先取上下文，避免与 stale 组同时建连抢风控额度
    cur, ext, push, dist = im_fetch_context(args.id, room_id, cookie)
    print(f"[im/fetch 预取] GIFT={sum(v for k, v in dist.items() if 'Gift' in k)} "
          f"push={push!r}\n  cursor={cur!r}\n  ext={ext!r}\n  dist={dict(dist)}\n", flush=True)

    threads = []
    for a in arms:
        t = threading.Thread(target=a.run, args=(args.duration,), daemon=True)
        threads.append(t)
        t.start()
        time.sleep(3)

    for t in threads:
        t.join()

    print("\n" + "=" * 74, flush=True)
    print("陈旧 vs 新鲜连接上下文 —— 对拍结果", flush=True)
    print("=" * 74, flush=True)
    for a in arms:
        seen = sorted(getattr(a.fetcher, '_seen_methods', set()))
        print(f"\n[{a.label}/{a.mode}] 事件={dict(a.counters)}", flush=True)
        print(f"  礼物事件={a.counters.get('gift', 0)}  "
              f"礼物 method={[m for m in seen if 'Gift' in m]}", flush=True)
        print(f"  method 总数={len(seen)}", flush=True)
        print(f"  全部 method={seen}", flush=True)
        if a.gift_samples:
            print(f"  礼物样例={a.gift_samples[0]}", flush=True)

    s = arms[0].counters.get('gift', 0)
    fr = arms[1].counters.get('gift', 0)
    print("\n" + "-" * 74, flush=True)
    if fr > s:
        print("✅ 新鲜上下文收到礼物而陈旧没有 → 根因是 WS 连接上下文陈旧", flush=True)
    elif s == 0 and fr == 0:
        print("➖ 两者都无礼物 → 连接上下文不是根因，需换房间复核", flush=True)
    else:
        print(f"❓ S={s} F={fr}", flush=True)


if __name__ == "__main__":
    main()
