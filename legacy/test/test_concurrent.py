#!/usr/bin/python
# coding:utf-8 -*-
"""多直播间并发爬取测试 - 每个直播间独立日志文件 + 总结文件"""
import io
import os
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime

from liveMan import DouyinLiveWebFetcher

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# 直播间列表
ROOMS = [
    ("与辉同行", "646454278948"),
    ("交个朋友", "168465302284"),
    ("小北珠宝", "916546228414"),
    ("贾乃亮", "516466932480"),
    ("老贝bay", "172783499850"),
    ("朱瓜瓜", "468784815081"),
    ("冰冰是福星", "669270973388"),
    ("小明正能量", "37831440846"),
    ("玉美人菲菲", "584446477656"),
    ("瑛瑛服饰", "198176747712"),
]

MONITOR_SECONDS = 900  # 并发测试 15 分钟
RUN_TS = datetime.now().strftime('%Y%m%d_%H%M%S')  # 本次运行目录名
RESULT_DIR = os.path.join("test_result", RUN_TS)  # 本次运行结果保存到独立子目录


class RoomWorker:
    """单个直播间的抓取工作线程，写独立日志文件并收集统计"""

    def __init__(self, name, live_id):
        self.name = name
        self.live_id = live_id
        self.fetcher = None
        self.log_file = None
        self.log_lock = threading.Lock()

        # 基本信息
        self.status = "未启动"
        self.anchor = "未知"
        self.anchor_id = "未知"
        self.ws_connected = False
        self.start_time = None
        self.end_time = None

        # 统计信息
        self.message_counts = Counter()
        self.error_count = 0
        self.enter_users = set()          # 进场用户 ID
        self.chat_users = Counter()       # 用户 ID -> 发言次数
        self.like_users = set()           # 点赞用户昵称
        self.like_total = 0               # 总点赞数
        self.follow_users = set()         # 关注用户 ID
        self.gift_users = Counter()       # 用户 ID -> 礼物贡献(钻)
        self.gift_total = 0               # 总礼物价值（钻）
        self.viewer_counts = []           # 在线人数序列

    # ---------- 日志写入 ----------

    def write_log(self, log_type, message):
        """写入带时间戳的日志行"""
        ts = datetime.now().strftime('%H:%M:%S')
        line = f"[{ts}] [{log_type}] {message}\n"
        with self.log_lock:
            try:
                self.log_file.write(line)
                self.log_file.flush()
            except Exception:
                pass

    def log_callback(self, log_type, message):
        """供 fetcher 回调：写日志 + 统计"""
        self.write_log(log_type, message)

        if log_type == "ERROR":
            self.error_count += 1
            return
        self.message_counts[log_type] += 1

        # 解析各类消息，提取详细数据
        if log_type == "ENTER":
            m = re.search(r'\[(\d+)\]', message)
            if m:
                self.enter_users.add(m.group(1))

        elif log_type == "CHAT":
            m = re.search(r'\[(\d+)\]', message)
            if m:
                self.chat_users[m.group(1)] += 1

        elif log_type == "LIKE":
            m = re.search(r'(.+?) 点了(\d+)个赞', message)
            if m:
                self.like_users.add(m.group(1))
                self.like_total += int(m.group(2))

        elif log_type == "FOLLOW":
            m = re.search(r'\[(\d+)\]', message)
            if m:
                self.follow_users.add(m.group(1))

        elif log_type == "GIFT":
            m = re.search(r'\[(\d+)\].+?共(\d+)钻', message)
            if m:
                self.gift_users[m.group(1)] += int(m.group(2))
                self.gift_total += int(m.group(2))

        elif log_type == "STATS":
            m = re.search(r'当前观看人数: ([\d,]+)', message)
            if m:
                self.viewer_counts.append(int(m.group(1).replace(',', '')))

    # ---------- 主流程 ----------

    def run(self):
        self.start_time = datetime.now()
        log_path = os.path.join(RESULT_DIR, f"{self.name}_{self.live_id}.log")
        self.log_file = open(log_path, 'w', encoding='utf-8')

        self.write_log("INFO", f"开始监控 直播间={self.live_id} ({self.name})  时长={MONITOR_SECONDS // 60}分钟")
        self.write_log("INFO", "=" * 60)

        self.fetcher = DouyinLiveWebFetcher(self.live_id, self.log_callback)

        # 基础信息
        self.write_log("INFO", f"ttwid={self.fetcher.ttwid[:40] if self.fetcher.ttwid else 'N/A'}...")
        self.write_log("INFO", f"room_id={self.fetcher.room_id}")

        # 获取直播间状态
        self.write_log("INFO", "获取直播间状态...")
        try:
            success, status, nickname, user_id = self.fetcher.get_room_status()
            if success:
                self.status = status
                self.anchor = nickname
                self.anchor_id = user_id
            else:
                self.status = "获取状态失败"
            self.write_log("INFO", f"主播={nickname}  ID={user_id}  状态={status}")
        except Exception as e:
            self.status = f"异常: {e}"
            self.write_log("ERROR", f"获取直播间状态异常: {e}")

        # 获取观众排行榜
        if self.status == "正在直播":
            self.write_log("INFO", "获取观众排行榜...")
            try:
                accounts = self.fetcher.get_audience_ranklist(self.anchor_id)
                if accounts:
                    self.write_log("INFO", f"获取到 {len(accounts)} 个观众")
                    for i, acc in enumerate(accounts[:3]):
                        self.write_log("INFO", f"  {i + 1}. [{acc['id']}] {acc['nickname']} (抖音号: {acc['display_id']})")
                else:
                    self.write_log("WARN", "未获取到观众数据")
            except Exception as e:
                self.write_log("ERROR", f"获取观众排行榜异常: {e}")

        # 启动 WebSocket 监控
        self.write_log("INFO", "-" * 60)
        if self.status == "正在直播":
            self.write_log("INFO", f"启动 WebSocket 监控 ({MONITOR_SECONDS // 60}分钟)...")
            monitor_thread = threading.Thread(target=self.fetcher.start)
            monitor_thread.daemon = True
            monitor_thread.start()
            self.ws_connected = True
            time.sleep(MONITOR_SECONDS)
            self.fetcher.stop()
        else:
            self.write_log("WARN", f"直播间状态为「{self.status}」，跳过 WebSocket 监控")
            time.sleep(MONITOR_SECONDS)

        self.end_time = datetime.now()
        duration = (self.end_time - self.start_time).total_seconds()
        self.write_log("INFO", "-" * 60)
        self.write_log("INFO", "监控结束")
        self.write_log("INFO", f"开始时间: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.write_log("INFO", f"结束时间: {self.end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.write_log("INFO", f"总时长: {duration / 60:.1f} 分钟")
        self.write_log("INFO", "=" * 60)

        self.log_file.close()


# ---------- 汇总报告 ----------

def format_room_detail(w):
    """生成单个直播间的详细报告文本"""
    duration = (w.end_time - w.start_time).total_seconds() if w.end_time else 0
    lines = []
    lines.append(f"【{w.name}】(ID: {w.live_id})")
    lines.append(f"  主播: {w.anchor} (主播ID: {w.anchor_id})")
    lines.append(f"  状态: {w.status} | WebSocket: {'已连接' if w.ws_connected else '未连接'} | 时长: {duration:.0f}s | 错误: {w.error_count}")

    if w.message_counts:
        lines.append(f"  消息: " + "  ".join(f"{t}:{c}" for t, c in w.message_counts.most_common()))

    lines.append(f"  用户: 进场去重 {len(w.enter_users)} | 发言去重 {len(w.chat_users)} | "
                 f"点赞去重 {len(w.like_users)} | 关注去重 {len(w.follow_users)}")

    if w.viewer_counts:
        lines.append(f"  在线人数: 最大 {max(w.viewer_counts):,} | 最小 {min(w.viewer_counts):,} | "
                     f"平均 {sum(w.viewer_counts) // len(w.viewer_counts):,} (采样 {len(w.viewer_counts)} 次)")

    if w.chat_users:
        top = w.chat_users.most_common(5)
        lines.append(f"  发言 Top5: " + ", ".join(f"[{uid}]{cnt}条" for uid, cnt in top))

    if w.like_total > 0:
        lines.append(f"  点赞: 共 {w.like_total} 次 | 点赞用户 {len(w.like_users)} 人")

    if w.gift_total > 0:
        top = w.gift_users.most_common(5)
        lines.append(f"  礼物贡献: 共 {w.gift_total} 钻 | Top5: " + ", ".join(f"[{uid}]{v}钻" for uid, v in top))
    else:
        lines.append(f"  礼物贡献: 无（监控期间未捕获礼物）")

    return "\n".join(lines)


def main():
    os.makedirs(RESULT_DIR, exist_ok=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始 {len(ROOMS)} 个直播间并发测试")
    print(f"监控时长: {MONITOR_SECONDS // 60} 分钟 | 结果目录: {RESULT_DIR} | 时间戳: {RUN_TS}")
    print("=" * 70)

    workers = [RoomWorker(name, lid) for name, lid in ROOMS]
    threads = []

    for w in workers:
        t = threading.Thread(target=w.run, name=w.name)
        t.daemon = True
        t.start()
        threads.append(t)
        print(f"  启动线程: {w.name} ({w.live_id})")
        time.sleep(0.5)

    print("\n等待监控完成...\n")

    for t in threads:
        t.join()

    # 生成总结文件
    summary_path = os.path.join(RESULT_DIR, "summary.log")
    total_messages = Counter()
    success_rooms = 0
    total_enter = 0
    total_chat = 0
    total_like = 0
    total_follow = 0
    total_gift = 0

    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] 并发测试总结报告\n")
        f.write(f"时间戳: {RUN_TS}\n")
        f.write(f"直播间数: {len(workers)} | 监控时长: {MONITOR_SECONDS // 60} 分钟\n")
        f.write("=" * 70 + "\n\n")

        for w in workers:
            total_messages.update(w.message_counts)
            total_enter += len(w.enter_users)
            total_chat += sum(w.chat_users.values())
            total_like += w.like_total
            total_follow += len(w.follow_users)
            total_gift += w.gift_total
            if w.ws_connected:
                success_rooms += 1

            f.write(format_room_detail(w) + "\n\n")

        f.write("=" * 70 + "\n")
        f.write("全量汇总\n")
        f.write("=" * 70 + "\n")
        f.write(f"成功连接: {success_rooms}/{len(workers)} 个直播间\n")
        f.write(f"进场用户(去重): {total_enter}\n")
        f.write(f"发言总次数: {total_chat}\n")
        f.write(f"点赞总次数: {total_like}\n")
        f.write(f"关注用户(去重): {total_follow}\n")
        f.write(f"礼物贡献总值: {total_gift} 钻\n\n")
        f.write("消息类型分布:\n")
        for msg_type, count in total_messages.most_common():
            f.write(f"  [{msg_type}]: {count}\n")

    # 终端输出
    print("\n" + "=" * 70)
    print(f"并发测试完成: {success_rooms}/{len(workers)} 个直播间成功连接")
    print("=" * 70)
    for w in workers:
        print()
        print(format_room_detail(w))

    print(f"\n每个直播间日志文件已保存到 {RESULT_DIR}/ 目录")
    print(f"总结文件: {summary_path}")
    print("\n测试完成!")


if __name__ == '__main__':
    main()
