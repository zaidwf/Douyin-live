#!/usr/bin/python
# coding:utf-8 -*-
"""对拍实验：同一娱乐直播间，游客态 vs 登录态，礼物消息是否下发

背景：娱乐主播 568544961053 直播 10 分钟收到 600+ 弹幕/点赞/进场，却 0 条
WebcastGiftMessage，且 _unhandled_methods 中也无任何礼物相关方法。

本脚本**同时**开两条 WebSocket 连同一个房间（消除「房间恰好没礼物」这个变量）：
  A 游客态：仅 ttwid（现网默认行为）
  B 登录态：config/auth.json 的完整 douyin cookie 集
比较两者收到的 method 集合，特别是 WebcastGiftMessage。

用法：
  python probe_gift_auth.py --id 568544961053 --duration 600
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

from liveMan import DouyinLiveWebFetcher, generateMsToken

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_FILE = os.path.join(ROOT, "config", "auth.json")


def load_login_cookie():
    """从 config/auth.json 提取 .douyin.com 域的 cookie，拼成请求 Cookie 串"""
    if not os.path.exists(AUTH_FILE):
        return None, 0
    state = json.load(open(AUTH_FILE, encoding='utf-8'))
    pairs, names = [], []
    for c in state.get('cookies', []):
        dom = c.get('domain', '')
        name = c.get('name') or ''
        val = c.get('value')
        if not name or val is None:
            continue
        if 'douyin.com' in dom or 'jinritemai' in dom:
            pairs.append(f"{name}={val}")
            names.append(name)
    if 'ttwid' not in names:
        return None, 0
    return "; ".join(pairs), len(pairs)


class Probe:
    """一条连接的采集探针"""

    def __init__(self, label, live_id, cookie_string):
        self.label = label
        self.live_id = live_id
        self.cookie_string = cookie_string
        self.counters = Counter()
        self.gift_samples = []
        self.lock = threading.Lock()
        self.fetcher = None
        self.room_id = None

    def log_cb(self, log_type, message):
        if log_type in ('GIFT', 'WEBSOCKET', 'FRAME', 'ERROR'):
            print(f"[{self.label}][{datetime.now().strftime('%H:%M:%S')}][{log_type}] "
                  f"{message[:300]}", flush=True)

    def run(self, duration):
        f = DouyinLiveWebFetcher(self.live_id, log_callback=self.log_cb)
        self.fetcher = f
        if self.cookie_string:
            f.cookie_string = self.cookie_string
        # 复用同一次 room_id 解析结果，避免两条连接拿到不同房间
        if self.room_id:
            f._DouyinLiveWebFetcher__room_id = self.room_id
        status = f.get_room_status()
        self.room_id = f.room_id
        print(f"[{self.label}] status={status} room_id={self.room_id} "
              f"cookie={'登录态' if self.cookie_string else '游客态'}", flush=True)

        def data_cb(event_type, data):
            with self.lock:
                self.counters[event_type] += 1
            if event_type == 'gift':
                with self.lock:
                    self.gift_samples.append(data)
                print(f"[{self.label}][GIFT] {data.get('nickname')} -> "
                      f"{data.get('gift_name')} x{data.get('gift_count')} "
                      f"单价={data.get('diamond_value')} repeat_end={data.get('repeat_end')} "
                      f"group={data.get('group_id')}", flush=True)

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

    cookie_string, n_cookie = load_login_cookie()
    print(f"[cookie] config/auth.json 提取到 {n_cookie} 条 douyin cookie: "
          f"{'成功' if cookie_string else '失败（缺失，B 组退化为游客态）'}", flush=True)
    if not cookie_string:
        return

    # 先解析一次 room_id，两条连接共用（确保对拍的是同一个房间）
    probe_a = Probe("A游客", args.id, None)
    probe_b = Probe("B登录", args.id, cookie_string)
    f0 = DouyinLiveWebFetcher(args.id, None)
    shared_room = f0.room_id
    probe_a.room_id = probe_b.room_id = shared_room
    print(f"[对拍] 目标房间 room_id={shared_room}，时长 {args.duration}s\n", flush=True)

    ta = threading.Thread(target=probe_a.run, args=(args.duration,), daemon=True)
    tb = threading.Thread(target=probe_b.run, args=(args.duration,), daemon=True)
    ta.start()
    time.sleep(2)  # 错峰建连，避免瞬时并发触发风控
    tb.start()
    ta.join()
    tb.join()

    print("\n" + "=" * 72, flush=True)
    print("对拍结果", flush=True)
    print("=" * 72, flush=True)
    for p in (probe_a, probe_b):
        seen = sorted(getattr(p.fetcher, '_seen_methods', set()))
        gift_seen = [m for m in seen if 'Gift' in m]
        print(f"\n[{p.label}] 事件分布={dict(p.counters)}", flush=True)
        print(f"  礼物事件数={p.counters.get('gift', 0)}  礼物 method={gift_seen}", flush=True)
        print(f"  收到 method 总数={len(seen)}", flush=True)
        print(f"  全部 method={seen}", flush=True)
        if p.gift_samples:
            print(f"  礼物样例={p.gift_samples[0]}", flush=True)

    a_gift = probe_a.counters.get('gift', 0)
    b_gift = probe_b.counters.get('gift', 0)
    print("\n" + "-" * 72, flush=True)
    if a_gift == 0 and b_gift > 0:
        print("✅ 结论：礼物仅登录态下发 —— 游客态 WS 被抖音过滤了礼物消息", flush=True)
    elif a_gift == 0 and b_gift == 0:
        print("➖ 结论：两种态都收不到礼物 —— 非登录态问题，需查 method/协议", flush=True)
    else:
        print(f"❓ 结论：游客态也收到礼物（A={a_gift} B={b_gift}）", flush=True)


if __name__ == "__main__":
    main()
