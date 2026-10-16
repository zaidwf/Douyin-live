#!/usr/bin/python
# coding:utf-8 -*-
"""直播数据抓取 - 15分钟监控 | 直播间: 646454278948"""
import io
import sys
import threading
import time
from datetime import datetime

from liveMan import DouyinLiveWebFetcher

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

LIVE_ID = "646454278948"
MONITOR_SECONDS = 15 * 60  # 15分钟


def main():
    start_time = datetime.now()
    print(f"[{start_time.strftime('%H:%M:%S')}] 开始监控 直播间={LIVE_ID}  时长={MONITOR_SECONDS//60}分钟")
    print("=" * 60)

    fetcher = DouyinLiveWebFetcher(LIVE_ID)

    ttwid = fetcher.ttwid
    room_id = fetcher.room_id
    print(f"[INFO] ttwid={ttwid[:40]}...")
    print(f"[INFO] room_id={room_id}")

    # 获取直播间状态
    print(f"\n[INFO] 获取直播间状态...")
    success, status, nickname, user_id = fetcher.get_room_status()
    if not success:
        print(f"[ERROR] 获取直播间状态失败")
        return
    print(f"[INFO] 主播={nickname}  ID={user_id}  状态={status}")

    # 获取观众排行榜
    print(f"\n[INFO] 获取观众排行榜...")
    accounts = fetcher.get_audience_ranklist(user_id)
    if accounts:
        print(f"[INFO] 获取到 {len(accounts)} 个观众")
        for i, acc in enumerate(accounts[:10]):
            print(f"       {i+1}. [{acc['id']}] {acc['nickname']} (抖音号: {acc['display_id']})")
    else:
        print(f"[WARN] 未获取到观众数据")

    # 启动 WebSocket 监控
    print(f"\n[INFO] 启动 WebSocket 监控 ({MONITOR_SECONDS//60}分钟)...")
    print("-" * 60)

    monitor_thread = threading.Thread(target=fetcher.start)
    monitor_thread.daemon = True
    monitor_thread.start()

    # 每30秒输出一次状态
    try:
        elapsed = 0
        while elapsed < MONITOR_SECONDS:
            time.sleep(30)
            elapsed += 30
            remaining = MONITOR_SECONDS - elapsed
            now = datetime.now()
            print(f"\n  [{now.strftime('%H:%M:%S')}] 已运行 {elapsed//60}分{elapsed%60}秒  |  剩余 {remaining//60}分{remaining%60}秒")
            print("  " + "-" * 50)
    except KeyboardInterrupt:
        print(f"\n[INFO] 用户中断")

    fetcher.stop()

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    print(f"\n[INFO] 监控结束")
    print(f"[INFO] 开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[INFO] 结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[INFO] 总时长: {duration/60:.1f} 分钟")
    print("=" * 60)


if __name__ == '__main__':
    main()
