#!/usr/bin/python
# coding:utf-8 -*-
"""多主播并发采集测试：验证数据口径（gift/like/follow/social/purchase 字段）

从 anchor.xlsx 读取主播 → 探测开播状态 → 对前 N 个在播主播并发采集 → 汇总校验口径。

用法：
  python test_field_concurrent.py --probe                          # 只探测开播状态
  python test_field_concurrent.py --anchors 5 --duration 600       # 并发采集并校验
  python test_field_concurrent.py --anchors 3 --duration 600 --ids 56697889278 dongfangzhenxuan beili881
"""
import argparse
import io
import json
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime

# 脚本位于 test/ 子目录，需把项目根加入 sys.path 才能 import liveMan / data_recorder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher
from data_recorder import DataRecorder

XLSX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "anchor.xlsx")


def load_anchors(xlsx_path):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    anchors = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or all(c is None or str(c).strip() == '' for c in r):
            continue
        name = str(r[1]).strip() if r[1] is not None else ''
        raw_id = r[8]
        if isinstance(raw_id, float) and raw_id.is_integer():
            did = str(int(raw_id))
        else:
            did = str(raw_id).strip() if raw_id is not None else ''
        if name and did and did.lower() != 'none':
            anchors.append((name, did))
    return anchors


def probe_live(anchors, delay=0.8):
    live = []
    print(f"探测 {len(anchors)} 个主播开播状态...")
    for name, lid in anchors:
        f = DouyinLiveWebFetcher(lid, None)
        try:
            success, status, nickname, uid = f.get_room_status()
            is_live = success and status == '正在直播'
            print(f"  {'[在播]' if is_live else '[未播]'} {name}({lid}) -> {nickname}")
            if is_live:
                live.append((name, lid, nickname, uid))
        except Exception as e:
            print(f"  [错误] {name}({lid}) -> {e}")
        time.sleep(delay)
    return live


class RoomWorker(threading.Thread):
    def __init__(self, name, lid, nickname, uid, duration, counters):
        super().__init__(daemon=True, name=name)
        self.name = name
        self.lid = lid
        self.nickname = nickname
        self.uid = uid
        self.duration = duration
        self.counters = counters
        self.fetcher = None
        self.recorder = None

    def run(self):
        self.fetcher = DouyinLiveWebFetcher(self.lid, None)
        self.recorder = DataRecorder(self.fetcher.room_id or self.lid, self.lid)
        self.recorder.set_room_info({
            "title": self.nickname, "anchor_nickname": self.nickname,
            "anchor_id": self.uid, "room_status": "正在直播",
        })

        def data_cb(event_type, data):
            self.counters[self.name][event_type] += 1
            self.recorder.on_event(event_type, data)

        self.fetcher.data_callback = data_cb
        t = threading.Thread(target=self.fetcher.start, daemon=True)
        t.start()
        time.sleep(self.duration)
        self.fetcher.stop()
        self.recorder.close()


def verify_events(recorder):
    """校验 events.jsonl 口径：follow/social action 分布 + gift 字段 + purchase promotion_id 格式"""
    action_dist = Counter()
    gift_sample = None
    gift_bad = 0
    purchase_ids = []
    ep = os.path.join(recorder.path, "events.jsonl")
    if not os.path.exists(ep):
        return {"action_dist": {}, "gift_sample": None, "purchase_pid_ok": None}
    with open(ep, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                x = json.loads(line)
            except Exception:
                continue
            t = x.get("type")
            if t in ("follow", "social"):
                action_dist[x.get("action")] += 1
            elif t == "gift":
                if gift_sample is None:
                    gift_sample = {k: x.get(k) for k in ("gift_id", "gift_name", "gift_count", "diamond_value", "gift_type")}
                if not x.get("gift_id") or not x.get("gift_name"):
                    gift_bad += 1
            elif t == "purchase":
                purchase_ids.extend(x.get("ids", []))
    # promotion_id 格式：19 位数字
    pid_ok = all(str(p).isdigit() and len(str(p)) == 19 for p in purchase_ids)
    return {"action_dist": dict(action_dist), "gift_sample": gift_sample, "gift_bad": gift_bad,
            "purchase_count": len(purchase_ids), "purchase_pid_ok": pid_ok}


def summarize(workers):
    print("\n" + "=" * 70)
    print("并发采集汇总（口径校验）")
    print("=" * 70)
    for w in workers:
        c = w.counters[w.name]
        rd_path = os.path.join(w.recorder.path, "room.json")
        rd = json.load(open(rd_path, encoding="utf-8")) if os.path.exists(rd_path) else {}
        print(f"\n[{w.name}] 事件={dict(c)}")
        print(f"  like_total={rd.get('like_total')}  follow_count={rd.get('follow_count')}  "
              f"shopping_down_count={rd.get('shopping_down_count')}")
        # 礼物聚合字段已移除（Web IM 通道不承载 WebcastGiftMessage，恒 0 假特征），不再展示
        promos = rd.get("promotions", [])
        sold = [p for p in promos if p.get("sale_count", 0) > 0]
        print(f"  商品总数={len(promos)}  成交商品数={len(sold)}")
        v = verify_events(w.recorder)
        print(f"  follow/social action 分布={v['action_dist']}  "
              f"gift 样例={v['gift_sample']}  gift 缺字段={v['gift_bad']}")
        print(f"  purchase 次数={v['purchase_count']}  promotion_id 均 19 位={v['purchase_pid_ok']}")
        print(f"  输出目录: {w.recorder.path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchors", type=int, default=5, help="并发主播数")
    ap.add_argument("--duration", type=int, default=600, help="采集时长（秒）")
    ap.add_argument("--probe", action="store_true", help="只探测开播状态")
    ap.add_argument("--ids", nargs="*", default=None, help="指定抖音号（跳过探测直接采集）")
    args = ap.parse_args()

    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

    if args.ids:
        live = [(lid, lid, lid, "") for lid in args.ids]
    else:
        anchors = load_anchors(XLSX)
        live = probe_live(anchors)

    if args.probe:
        print(f"\n在播主播共 {len(live)} 个")
        return

    if not live:
        print("错误：当前无在播主播，无法采集")
        return

    picked = live[:args.anchors]
    print(f"\n开始并发采集 {len(picked)} 个主播，时长 {args.duration}s：")
    for name, lid, nickname, uid in picked:
        print(f"  - {name}({lid})")

    counters = {name: Counter() for name, _, _, _ in picked}
    workers = [RoomWorker(name, lid, nickname, uid, args.duration, counters)
               for name, lid, nickname, uid in picked]
    for w in workers:
        w.start()

    start = time.time()
    next_report = 60
    while any(w.is_alive() for w in workers):
        time.sleep(5)
        elapsed = int(time.time() - start)
        if elapsed >= next_report:
            print(f"[进度 {elapsed}s] " + " | ".join(f"{w.name}:{sum(counters[w.name].values())}" for w in workers))
            next_report += 60

    summarize(workers)


if __name__ == "__main__":
    main()
