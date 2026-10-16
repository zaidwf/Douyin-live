#!/usr/bin/python
# coding:utf-8 -*-
"""直播间 3 分钟测试 - 结构化数据落盘"""
import io
import sys
import threading
import time
from datetime import datetime

from liveMan import DouyinLiveWebFetcher
from data_recorder import DataRecorder
from product_extractor import extract_products_sync

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

LIVE_ID = "646454278948"
MONITOR_SECONDS = 180  # 3 分钟


def main():
    start_time = datetime.now()
    print(f"[{start_time.strftime('%H:%M:%S')}] 开始测试直播间 {LIVE_ID}，时长 {MONITOR_SECONDS // 60} 分钟")
    print("=" * 60)

    fetcher = DouyinLiveWebFetcher(LIVE_ID, None)
    print(f"room_id = {fetcher.room_id}")

    # 获取直播间状态
    success, status, nickname, user_id = fetcher.get_room_status()
    print(f"主播: {nickname}  ID: {user_id}  状态: {status}")

    # 提取商品橱窗详情（Playwright）
    print("\n提取商品橱窗...")
    try:
        products = extract_products_sync(LIVE_ID, headless=True)
        print(f"提取到 {len(products)} 个商品")
        for p in products[:5]:
            print(f"  #{p['num']} {p['name'][:40]} | ¥{p['price']} | 已售{p['sales_count']}")
    except Exception as e:
        print(f"商品提取失败: {e}")
        products = []

    # 初始化结构化数据落盘
    recorder = DataRecorder(fetcher.room_id, LIVE_ID)
    recorder.set_room_info({
        'title': nickname,
        'anchor_nickname': nickname,
        'anchor_id': user_id,
        'room_status': status,
    })
    recorder.set_products(products)
    fetcher.data_callback = recorder.on_event

    # 启动 WebSocket 监控
    monitor_thread = threading.Thread(target=fetcher.start)
    monitor_thread.daemon = True
    monitor_thread.start()

    # 每 30 秒输出进度
    try:
        elapsed = 0
        while elapsed < MONITOR_SECONDS:
            time.sleep(30)
            elapsed += 30
            remaining = MONITOR_SECONDS - elapsed
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] 已运行 {elapsed // 60}分{elapsed % 60}秒 | 剩余 {remaining // 60}分{remaining % 60}秒")
    except KeyboardInterrupt:
        print("用户中断")

    fetcher.stop()
    recorder.set_promotion_order(fetcher.product_refresh_order)
    recorder.close()

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    product_ids = fetcher.get_product_ids()

    print("\n" + "=" * 60)
    print(f"测试结束，总时长 {duration / 60:.1f} 分钟")
    print(f"采集到商品 ID {len(product_ids)} 个: {product_ids}")
    print(f"结构化数据已保存到: {recorder.path}")
    print("=" * 60)


if __name__ == '__main__':
    main()
