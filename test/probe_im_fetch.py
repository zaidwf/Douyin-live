#!/usr/bin/python
# coding:utf-8 -*-
"""诊断探测：/webcast/im/fetch/ 是否下发 WebcastGiftMessage

背景：娱乐主播 568544961053 直播 10 分钟（600+ 事件、5000 在线）未收到任何
WebcastGiftMessage。本脚本走 HTTP 侧 /webcast/im/fetch/ 拉取一批最近的 IM 消息，
看 GIFT 是否出现在 HTTP 通道里 —— 用于区分「房间无礼物」与「WS 通道不下发礼物」。

用法：
  python probe_im_fetch.py --id 568544961053
"""
import argparse
import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from protobuf.douyin import Response
from liveMan import DouyinLiveWebFetcher, generate_a_bogus, generateMsToken

USER_UNIQUE_ID = "7319483754668557238"  # 与 WS URL 中 wss_push_did 一致


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="568544961053")
    args = ap.parse_args()

    f = DouyinLiveWebFetcher(args.id, None)
    status = f.get_room_status()
    print(f"[STATUS] {status}")
    room_id = f.room_id
    print(f"[ROOM] room_id={room_id}")

    ms_token = generateMsToken()
    params = [
        "resp_content_type=protobuf", "did_rule=3", "device_id=",
        "app_name=douyin_web", "endpoint=live_pc", "support_wrds=1",
        f"user_unique_id={USER_UNIQUE_ID}", "identity=audience",
        "need_persist_msg_count=15", "insert_task_id=", "live_reason=",
        f"room_id={room_id}", "version_code=180800", "last_rtt=0",
        "live_id=1", "aid=6383", "fetch_rule=1", "cursor=", "internal_ext=",
        "device_platform=web", "cookie_enabled=true", "screen_width=1920",
        "screen_height=1080", "browser_language=zh-CN", "browser_platform=Win32",
        "browser_name=Mozilla",
        "browser_version=5.0%20(Windows%20NT%2010.0%3B%20Win64%3B%20x64)%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20Chrome%2F120.0.0.0%20Safari%2F537.36",
        "browser_online=true", "tz_name=Asia/Shanghai", f"msToken={ms_token}",
    ]
    query = "&".join(params)
    a_bogus = generate_a_bogus(query, f.user_agent)
    url = f"https://live.douyin.com/webcast/im/fetch/?{query}&a_bogus={a_bogus}"
    print(f"[URL] len={len(url)} a_bogus_len={len(a_bogus or '')}")

    headers = {
        "User-Agent": f.user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": f"https://live.douyin.com/{args.id}",
        "Cookie": f"ttwid={f.ttwid}; msToken={ms_token}; __ac_nonce=0123407cc00a9e438deb4",
    }
    r = requests.get(url, headers=headers, timeout=20)
    print(f"[HTTP] status={r.status_code} content-type={r.headers.get('Content-Type')} len={len(r.content)}")
    if r.status_code != 200 or not r.content:
        print("[FAIL] 空响应/非 200（可能被反爬拦截）")
        return

    resp = Response().parse(r.content)
    print(f"[RESP] messages={len(resp.messages_list)} need_ack={resp.need_ack} "
          f"cursor={resp.cursor!r} heartbeat={resp.heartbeat_duration} "
          f"push_server_v2={resp.push_server!r}")
    print(f"[RESP] internal_ext={resp.internal_ext!r}")

    dist = Counter(m.method for m in resp.messages_list)
    print("\n[方法分布]")
    for m, c in dist.most_common():
        print(f"  {c:5d}  {m}")

    gift_msgs = [m for m in resp.messages_list if 'Gift' in m.method]
    print(f"\n[礼物] 命中 {len(gift_msgs)} 条")
    for m in gift_msgs[:5]:
        print(f"  method={m.method} payload_len={len(m.payload)}")
        print(f"  hex[:80]={m.payload[:80].hex()}")

    # 全部 method 清单，便于核对是否有未注册的礼物相关方法
    print(f"\n[全部 method] {sorted(dist)}")


if __name__ == "__main__":
    main()
