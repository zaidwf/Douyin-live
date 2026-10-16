#!/usr/bin/python
# coding:utf-8 -*-
"""抖音直播自动全程采集（守护模式，轻量：弹幕/事件/商品讲解）

从 anchor.xlsx 读取主播列表（抖音号），长期守护运行：
- 轮询检测每个主播是否开播（解析 room_id + 直播间状态）
- 检测到开播 → 自动启动 WebSocket 采集（弹幕/事件/商品 promotion_id）
- 主播下播（ControlMessage status==3）→ 自动停止并落盘（room.json/events.jsonl/...）
- 继续盯守，等主播下一次开播再采，循环往复直到手动停止

用法：
  python auto_crawl.py [xlsx_path] [check_interval_seconds]
"""
import io
import random
import sys
import threading
import time
from datetime import datetime

from liveMan import DouyinLiveWebFetcher
from data_recorder import DataRecorder

CHECK_INTERVAL = 60  # 默认开播检测间隔（秒）


def load_anchors(xlsx_path):
    """从 anchor.xlsx 读取主播列表，返回 [(name, douyin_id)]

    依赖列：第 2 列=博主名，第 9 列=抖音号（用于 live.douyin.com/{抖音号}）
    """
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    anchors = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or all(c is None or str(c).strip() == '' for c in r):
            continue
        name = str(r[1]).strip() if r[1] is not None else ''
        raw_id = r[8]  # 抖音号列（第 9 列，可能为纯数字）
        if isinstance(raw_id, float) and raw_id.is_integer():
            douyin_id = str(int(raw_id))
        else:
            douyin_id = str(raw_id).strip() if raw_id is not None else ''
        if name and douyin_id and douyin_id.lower() != 'none':
            anchors.append((name, douyin_id))
    return anchors


class AnchorDaemon(threading.Thread):
    """单个主播的守护线程：检测开播 → 全程采集 → 落盘 → 循环"""

    def __init__(self, name, douyin_id, check_interval=CHECK_INTERVAL):
        super().__init__(name=name)  # 非 daemon：保证优雅退出时 close 完成
        self.anchor_name = name
        self.douyin_id = douyin_id
        self.check_interval = check_interval
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        # 随机初始延迟：避免多个线程同时首次请求，触发抖音风控（HTTP 444）
        init_delay = random.uniform(0, self.check_interval)
        print(f"[{self.anchor_name}] 守护启动（抖音号 {self.douyin_id}，检测间隔 {self.check_interval}s，初始延迟 {init_delay:.1f}s）")
        self._stop_event.wait(init_delay)
        while not self._stop_event.is_set():
            try:
                self._one_cycle()
            except Exception as e:
                print(f"[{self.anchor_name}] 循环异常: {e}")
            # 加随机抖动，避免所有线程同步轮询造成突发并发
            jitter = random.uniform(0.5, 1.5)
            self._stop_event.wait(self.check_interval * jitter)

    def _one_cycle(self):
        """一轮检测：若开播则采集到自然下播"""
        fetcher = DouyinLiveWebFetcher(self.douyin_id, None)

        # 1. 解析 room_id（抖音号有效即可解析；未开播时返回历史房间号，开播判断靠下一步）
        try:
            rid = fetcher.room_id
        except Exception:
            rid = None
        if not rid:
            return  # 抖音号无效，跳过本轮

        # 2. 确认直播状态 + 主播信息
        try:
            success, status, nickname, user_id = fetcher.get_room_status()
        except Exception:
            return
        if not success or status != '正在直播':
            return

        start_ts = datetime.now()
        print(f"[{self.anchor_name}] {start_ts.strftime('%H:%M:%S')} 检测到开播：{nickname}（{user_id}），开始全程采集")

        # 3. 启动采集（WebSocket 弹幕 + 结构化落盘）
        offline = threading.Event()
        fetcher.on_offline = offline.set

        recorder = DataRecorder(fetcher.room_id, self.douyin_id)
        recorder.set_room_info({
            'title': nickname, 'anchor_nickname': nickname,
            'anchor_id': user_id, 'room_status': status,
        })
        fetcher.data_callback = recorder.on_event

        ws_thread = threading.Thread(target=fetcher.start, daemon=True)
        ws_thread.start()

        # 4. 等待下播（ControlMessage status==3 触发 on_offline）
        while not offline.is_set() and not self._stop_event.is_set():
            if not ws_thread.is_alive():
                break  # ws 意外断开（网络问题），也结束本场
            offline.wait(timeout=self.check_interval)

        # 5. 停止并落盘
        fetcher.stop()
        recorder.close()
        dur = (datetime.now() - start_ts).total_seconds()
        print(f"[{self.anchor_name}] 本场结束（时长 {dur / 60:.1f} 分钟），数据落盘：{recorder.path}")


def main():
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass
    xlsx_path = sys.argv[1] if len(sys.argv) > 1 else "anchor.xlsx"
    check_interval = int(sys.argv[2]) if len(sys.argv) > 2 else CHECK_INTERVAL

    anchors = load_anchors(xlsx_path)
    if not anchors:
        print(f"错误：未从 {xlsx_path} 读取到主播列表")
        return
    print(f"加载主播列表 {len(anchors)} 个（来自 {xlsx_path}，检测间隔 {check_interval}s）：")
    for name, did in anchors:
        print(f"  - {name}（抖音号 {did}）")
    print("=" * 70)

    daemons = [AnchorDaemon(name, did, check_interval) for name, did in anchors]
    for d in daemons:
        d.start()

    print("守护模式运行中，按 Ctrl+C 停止...")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n收到停止信号，正在优雅关闭...")
        for d in daemons:
            d.stop()
        for d in daemons:
            d.join(timeout=30)
        print("已全部停止，退出。")


if __name__ == "__main__":
    main()
