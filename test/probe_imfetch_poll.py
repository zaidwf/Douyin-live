#!/usr/bin/python
# coding:utf-8
"""轮询 /webcast/im/fetch/ 统计 method 分布 —— 判定礼物是否走 HTTP 通道

背景：抖音官方网页在无头浏览器里**不开 WebSocket**（页面零 WS），但弹幕正常渲染，
且页面上能看到"感谢公主的嘉年华"这类礼物致谢 —— 说明网页的消息传输不是直连 WS。
`/webcast/im/fetch/` 是已知可用（需完整 douyin cookie）的 HTTP IM 端点。

本脚本按固定间隔反复请求 im/fetch（游标留空 = 取最近窗口），累计 method 分布，
重点看是否出现 `WebcastGiftMessage`。

用法：
  python probe_imfetch_poll.py --id 731918444565 --duration 180 --interval 3
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from protobuf.douyin import Response
from liveMan import DouyinLiveWebFetcher, generate_a_bogus, generateMsToken

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH = os.path.join(ROOT, "config", "auth.json")


def load_cookie():
    if not os.path.exists(AUTH):
        return None
    st = json.load(open(AUTH, encoding='utf-8'))
    parts = [f"{c['name']}={c['value']}" for c in st.get('cookies', [])
             if c.get('name') and c.get('value') is not None
             and ('douyin.com' in c.get('domain', '') or 'jinritemai' in c.get('domain', ''))]
    return "; ".join(parts) or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="731918444565")
    ap.add_argument("--duration", type=int, default=180)
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    cookie = load_cookie()
    if not cookie:
        print("缺少 config/auth.json，im/fetch 会返回空体")
        return

    f = DouyinLiveWebFetcher(args.id, None)
    st = f.get_room_status()
    print(f"[STATUS] {st[1]} {st[2]}  room_id={f.room_id}", flush=True)
    if not st[0]:
        return

    methods = Counter()
    gift_rows = []
    rounds = ok = empty = 0
    start = time.time()
    next_report = 30

    while time.time() - start < args.duration:
        rounds += 1
        ms = generateMsToken()
        q = '&'.join([
            'resp_content_type=protobuf', 'did_rule=3', 'device_id=',
            'app_name=douyin_web', 'endpoint=live_pc', 'support_wrds=1',
            f'user_unique_id={f.user_unique_id}', 'identity=audience',
            'need_persist_msg_count=15', 'insert_task_id=', 'live_reason=',
            f'room_id={f.room_id}', 'version_code=180800', 'last_rtt=0',
            'live_id=1', 'aid=6383', 'fetch_rule=1', 'cursor=', 'internal_ext=',
            'device_platform=web', 'cookie_enabled=true', 'screen_width=1920',
            'screen_height=1080', 'browser_language=zh-CN', 'browser_platform=Win32',
            'browser_name=Mozilla',
            'browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36',
            'browser_online=true', 'tz_name=Asia/Shanghai', f'msToken={ms}',
        ])
        ab = generate_a_bogus(q, f.user_agent)
        hdr = {'User-Agent': f.user_agent, 'Accept': '*/*', 'Accept-Encoding': 'identity',
               'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
               'Referer': f'https://live.douyin.com/{args.id}', 'Cookie': cookie}
        try:
            r = requests.get(f'https://live.douyin.com/webcast/im/fetch/?{q}&a_bogus={ab}',
                             headers=hdr, timeout=15)
            if not r.content:
                empty += 1
            else:
                ok += 1
                resp = Response().parse(r.content)
                for m in resp.messages_list:
                    methods[m.method] += 1
                    if 'Gift' in m.method:
                        gift_rows.append((datetime.now().isoformat(timespec='seconds'),
                                          m.method, len(m.payload)))
        except Exception as e:
            print(f"  [轮次 {rounds}] 异常 {e}", flush=True)

        el = int(time.time() - start)
        if el >= next_report:
            g = sum(v for k, v in methods.items() if 'Gift' in k)
            print(f"[进度 {el}s] 轮次={rounds} 成功={ok} 空={empty} | "
                  f"method {len(methods)} 种 | 礼物={g} | 总消息={sum(methods.values())}", flush=True)
            next_report += 30
        time.sleep(args.interval)

    print("\n" + "=" * 72, flush=True)
    print(f"im/fetch 轮询结果（{int(time.time()-start)}s，{rounds} 轮）", flush=True)
    print("=" * 72, flush=True)
    print(f"成功={ok} 空响应={empty}  累计消息={sum(methods.values())}", flush=True)
    print(f"\nmethod 分布（{len(methods)} 种）:", flush=True)
    for m, c in methods.most_common():
        print(f"  {c:6d}  {m}{'   ← 礼物' if 'Gift' in m else ''}", flush=True)
    g = sum(v for k, v in methods.items() if 'Gift' in k)
    print(f"\n礼物消息 = {g} 条", flush=True)
    if gift_rows:
        print("礼物样例:", flush=True)
        for row in gift_rows[:5]:
            print("   ", row, flush=True)


if __name__ == "__main__":
    main()
