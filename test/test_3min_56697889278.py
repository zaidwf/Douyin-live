#!/usr/bin/python
# coding:utf-8 -*-
"""与辉同行 3 分钟测试：验证 room_detail 富字段（item 3）+ method 诊断（item 6）"""
import io
import os
import sys
import json
import threading
import time
from collections import Counter
from datetime import datetime

# 引导根目录到 sys.path，使 test/ 子目录下也能 import liveMan
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher
from data_recorder import DataRecorder

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

LIVE_ID = "56697889278"  # 与辉同行
DURATION = 180           # 3 分钟


def main():
    start = datetime.now()
    print(f"[{start.strftime('%H:%M:%S')}] 与辉同行 3min 测试开始（{LIVE_ID}）")
    print("=" * 70)

    fetcher = DouyinLiveWebFetcher(LIVE_ID, None)
    print(f"room_id = {fetcher.room_id}")

    # ---- Item 3：enter API 富字段 ----
    success, status, nickname, user_id = fetcher.get_room_status()
    print(f"\n[item 3] 主播={nickname}  ID={user_id}  状态={status}")
    print(f"[item 3] room_detail:")
    for k, v in fetcher.room_detail.items():
        print(f"    {k} = {v!r}")

    # ---- Item 3：ranklist 富字段 ----
    accounts = fetcher.get_audience_ranklist(user_id)
    print(f"\n[item 3] ranklist 获取 {len(accounts)} 人，前 3 条:")
    for acc in accounts[:3]:
        print(f"    {json.dumps(acc, ensure_ascii=False)}")

    # ---- WebSocket 采集 3 分钟 + 落盘 ----
    recorder = DataRecorder(fetcher.room_id, LIVE_ID)
    room_info = dict(fetcher.room_detail)
    room_info.update({'title': nickname, 'anchor_nickname': nickname,
                      'anchor_id': user_id, 'room_status': status})
    recorder.set_room_info(room_info)
    fetcher.data_callback = recorder.on_event

    print(f"\n[WS] 开始采集 {DURATION}s ...")
    ws_thread = threading.Thread(target=fetcher.start, daemon=True)
    ws_thread.start()

    time.sleep(DURATION)
    fetcher.stop()
    recorder.close()

    # ---- Item 6：method 诊断 ----
    print("\n" + "=" * 70)
    print("[item 6] WebSocket 收到的 method 名:")
    for m in sorted(fetcher._seen_methods):
        print(f"    {m}")
    print(f"[item 6] 未注册处理器的 method: {sorted(fetcher._unhandled_methods) or '（无）'}")
    print(f"[item 6] 是否收到 WebcastProductChangeMessage: "
          f"{'WebcastProductChangeMessage' in fetcher._seen_methods}")

    # 事件类型分布
    ec = Counter()
    ep = os.path.join(recorder.path, 'events.jsonl')
    if os.path.exists(ep):
        with open(ep, encoding='utf-8') as fh:
            for line in fh:
                try:
                    ec[json.loads(line).get('type')] += 1
                except Exception:
                    pass
    print(f"[item 6] 事件类型分布: {dict(ec)}")
    print(f"\n数据目录: {recorder.path}")
    print(f"总耗时 {(datetime.now() - start).total_seconds():.0f}s")


if __name__ == '__main__':
    main()
