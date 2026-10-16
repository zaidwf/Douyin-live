#!/usr/bin/python
# coding:utf-8 -*-
"""礼物口径对拍：Web 画像 vs PC 客户端画像（同一房间同时连接）

已确认事实（2026-09-28）：
  - 娱乐主播 568544961053 连 10min：600+ 事件、17 种 method，0 条 WebcastGiftMessage
  - 与辉同行 1h：同样 0 条；/webcast/im/fetch/ HTTP 通道近期窗口（63 条）也 0 条
  - 游客态与登录态（cookie 已过期）method 集合完全一致 → 不是登录态问题
  - _unhandled_methods 中无任何礼物相关方法 → 不是解析/注册问题

本脚本对拍两种协议画像，定位礼物是否只在 PC 客户端画像下下发：
  W (Web)  ：Chrome UA + 仅 ttwid + need_persist_msg_count=15（现网行为 + ACK 修正）
  P (PC)   ：awemePcClient UA + X-AWEME-* 头 + PC 固定 cookie + need_persist_msg_count=0

用法：
  python probe_gift_arms.py --id 568544961053 --duration 600
"""
import argparse
import hashlib
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher

# --- PC 客户端画像常量（对齐 douyinLive protocol_profile.go，抓包抖音桌面版 8.5.1） ---
PC_APP_VERSION = "8.5.1"
PC_BUILD_ID = "469669512"
PC_CHROME = "136.0.7103.59"
PC_ELECTRON = "36.4.0-rs.31.release.pgo.7"
PC_UA = (f"Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
         f"douyin/{PC_APP_VERSION} Chrome/{PC_CHROME} Electron/{PC_ELECTRON} "
         f"TTElectron/{PC_ELECTRON} Safari/537.36 awemePcClient/{PC_APP_VERSION} "
         f"buildId/{PC_BUILD_ID} osName/Windows")

PC_COOKIE_SEEDS = [
    ("client_push", '{"live_push_record":""}'),
    ("enter_pc_once", "1"),
    ("__live_version__", '"1.1.5.5741"'),
    ("has_avx2", "true"),
    ("device_web_cpu_core", "10"),
    ("device_web_memory_size", "5"),
    ("is_support_rtm_web_ts", "0"),
    ("live_can_add_dy_2_desktop", '"0"'),
    ("live_use_vvc", '"true"'),
    ("home_can_add_dy_2_desktop", '"0"'),
    ("webcast_local_quality", "null"),
    ("is_dash_user", "1"),
    ("live_private_user", "0"),
]


def pc_device_identity(seed):
    """按抓包长度派生 PC 设备标识：DeviceName 32 位 hex，GUID 128 位 hex"""
    name = hashlib.md5(("aweme-pc-device-name:" + seed).encode()).hexdigest()
    guid = hashlib.sha512(("aweme-pc-device-guid:" + seed).encode()).hexdigest()
    return name, guid


class Arm:
    def __init__(self, label, live_id, profile):
        self.label = label
        self.live_id = live_id
        self.profile = profile  # 'web' | 'pc'
        self.counters = Counter()
        self.gift_samples = []
        self.lock = threading.Lock()
        self.fetcher = None
        self.room_id = None

    def log_cb(self, log_type, message):
        if log_type in ('GIFT', 'ERROR', 'WARN'):
            print(f"[{self.label}][{datetime.now().strftime('%H:%M:%S')}][{log_type}] "
                  f"{message[:260]}", flush=True)

    def run(self, duration):
        f = DouyinLiveWebFetcher(self.live_id, log_callback=self.log_cb)
        self.fetcher = f
        if self.room_id:
            f._DouyinLiveWebFetcher__room_id = self.room_id

        if self.profile == 'pc':
            f.user_agent = PC_UA
            f.persist_msg_count = "0"
            dev_name, dev_guid = pc_device_identity(f"probe-{self.live_id}")
            f.extra_ws_headers = {
                "X-AWEME-CLIENTVERSION": PC_APP_VERSION,
                "X-AWEME-DEVICEMANUFACTURER": "Microsoft Corporation",
                "X-AWEME-DEVICEMODEL": "Virtual Machine",
                "X-AWEME-DEVICENAME": dev_name,
                "X-AWEME-DEVICEOS": "Windows 10",
                "X-AWEME-GUID": dev_guid,
            }
            f.cookie_string = None  # ttwid 在 get_room_status 后补
        else:
            f.persist_msg_count = "15"

        status = f.get_room_status()
        self.room_id = f.room_id
        if self.profile == 'pc':
            parts = [f"ttwid={f.ttwid}"] + [f"{k}={v}" for k, v in PC_COOKIE_SEEDS]
            f.cookie_string = "; ".join(parts)
        print(f"[{self.label}] profile={self.profile} status={status[1]} room={self.room_id} "
              f"ua={'PC' if self.profile == 'pc' else 'Chrome'}", flush=True)

        def data_cb(event_type, data):
            with self.lock:
                self.counters[event_type] += 1
            if event_type == 'gift':
                with self.lock:
                    self.gift_samples.append(data)
                print(f"[{self.label}][GIFT] {data.get('nickname')} -> {data.get('gift_name')} "
                      f"x{data.get('gift_count')} 单价={data.get('diamond_value')} "
                      f"repeat_count={data.get('repeat_count')} repeat_end={data.get('repeat_end')} "
                      f"group_id={data.get('group_id')}", flush=True)

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
    ap.add_argument("--arms", nargs="*", default=["web", "pc"])
    args = ap.parse_args()

    f0 = DouyinLiveWebFetcher(args.id, None)
    st = f0.get_room_status()
    if not st[0]:
        print(f"未开播：{st}")
        return
    shared_room = f0.room_id
    print(f"[对拍] {st[2]} room_id={shared_room} 时长={args.duration}s 画像={args.arms}\n", flush=True)

    arms = []
    for prof in args.arms:
        a = Arm("W" if prof == 'web' else "P", args.id, prof)
        a.room_id = shared_room
        arms.append(a)

    threads = []
    for i, a in enumerate(arms):
        t = threading.Thread(target=a.run, args=(args.duration,), daemon=True)
        threads.append(t)
        t.start()
        time.sleep(3)  # 错峰建连

    for t in threads:
        t.join()

    print("\n" + "=" * 74, flush=True)
    print("礼物口径对拍结果", flush=True)
    print("=" * 74, flush=True)
    for a in arms:
        seen = sorted(getattr(a.fetcher, '_seen_methods', set()))
        gift_seen = [m for m in seen if 'Gift' in m]
        print(f"\n[{a.label}/{'PC' if a.profile == 'pc' else 'Web'}] "
              f"事件分布={dict(a.counters)}", flush=True)
        print(f"  礼物事件数={a.counters.get('gift', 0)}  礼物 method={gift_seen}", flush=True)
        print(f"  method 总数={len(seen)}", flush=True)
        print(f"  全部 method={seen}", flush=True)
        if a.gift_samples:
            print(f"  礼物样例={a.gift_samples[0]}", flush=True)

    w = next((a for a in arms if a.profile == 'web'), None)
    p = next((a for a in arms if a.profile == 'pc'), None)
    if w and p:
        print("\n" + "-" * 74, flush=True)
        if p.counters.get('gift', 0) > w.counters.get('gift', 0):
            print("✅ PC 画像收到礼物而 Web 未收到 → 礼物仅在 PC 客户端画像下下发", flush=True)
        elif w.counters.get('gift', 0) == 0 and p.counters.get('gift', 0) == 0:
            print("➖ 两种画像都无礼物 → 需换房间复核（可能该房间确实无礼物）", flush=True)
        else:
            print(f"❓ 异常：Web={w.counters.get('gift', 0)} PC={p.counters.get('gift', 0)}", flush=True)


if __name__ == "__main__":
    main()
