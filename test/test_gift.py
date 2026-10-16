#!/usr/bin/python
# coding:utf-8 -*-
"""娱乐主播礼物口径专项测试（纯 WebSocket）

针对娱乐/才艺主播（礼物高频），验证 gift 事件的字段真实性：
  gift_id / gift_name / gift_type / gift_count(combo_count) / repeat_count /
  total_count / diamond_value / repeat_end / group_id
以及 data_recorder 的连击收尾（repeat_end && group_id>0）聚合跳过逻辑。

背景（2026-09-28）：多种协议画像/连接上下文对拍后，`WebcastGiftMessage` 仍未收到过。
已知 `568544961053`（栋梁）是福袋/红包活动间，`56697889278`（与辉同行）是带货间——
两者都可能天然少礼物。故本脚本支持批量探测娱乐/才艺主播，对**在播**的几个并发采集，
用于捕捉真实礼物事件做口径验证。

用法：
  python test_gift.py                                  # 默认批量探测娱乐主播名单
  python test_gift.py --ids 568544961053 --duration 300
  python test_gift.py --probe                          # 只探测开播状态
"""
import argparse
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher
from data_recorder import DataRecorder

# 娱乐/才艺类主播（唱歌/音乐人，礼物高频），来源：公开主播资料检索（2026-09-28）。
# 注意：这类主播多在晚间 20:00-24:00 开播，白天探测常为未播。
ENTERTAINMENT_ANCHORS = [
    ("蓝万", "lanwan"),
    ("DOU来好歌会", "doulaimusic"),
    ("嫄嫄全能唱将", "yuanyuan.6789"),
    ("音乐浪子", "jtxd888"),
    ("媛小小", "wanan669"),
    ("娟妹", "59239524067"),
    ("怪才杨五", "44415627794"),
    ("栋梁(福袋红包间)", "568544961053"),
]


class GiftProbe(threading.Thread):
    def __init__(self, name, live_id, nickname, duration):
        super().__init__(daemon=True, name=name)
        self.name = name
        self.live_id = live_id
        self.nickname = nickname
        self.duration = duration
        self.counters = Counter()
        self.gift_rows = []
        self.fetcher = None
        self.recorder = None

    def run(self):
        self.fetcher = DouyinLiveWebFetcher(self.live_id, None)
        self.recorder = DataRecorder(self.fetcher.room_id, self.live_id)
        self.recorder.set_room_info({
            "title": self.nickname, "anchor_nickname": self.nickname,
            "anchor_id": "", "room_status": "正在直播",
        })

        def data_cb(event_type, data):
            self.counters[event_type] += 1
            self.recorder.on_event(event_type, data)
            if event_type == 'gift':
                self.gift_rows.append(data)
                print(f"[{self.name}][礼物] {data.get('nickname')} -> {data.get('gift_name')} "
                      f"x{data.get('gift_count')} (id={data.get('gift_id')}, "
                      f"type={data.get('gift_type')}, 单价={data.get('diamond_value')}, "
                      f"repeat={data.get('repeat_count')}, total={data.get('total_count')}, "
                      f"repeat_end={data.get('repeat_end')}, group_id={data.get('group_id')})",
                      flush=True)

        self.fetcher.data_callback = data_cb
        t = threading.Thread(target=self.fetcher.start, daemon=True)
        t.start()
        time.sleep(self.duration)
        self.fetcher.stop()
        self.recorder.close()


def report(p):
    """单个主播的礼物口径校验"""
    n = len(p.gift_rows)
    print(f"\n[{p.name}] 礼物事件数={n}  事件类型分布={dict(p.counters)}", flush=True)
    print(f"  输出目录: {p.recorder.path}", flush=True)
    if n == 0:
        seen = sorted(getattr(p.fetcher, '_seen_methods', set()))
        print(f"  ⚠️ 未收到 WebcastGiftMessage（收到 method {len(seen)} 种，"
              f"礼物相关={[m for m in seen if 'Gift' in m]}）", flush=True)
        return

    bad_id = sum(1 for g in p.gift_rows if not g.get('gift_id') or not g.get('gift_name'))
    skip_rows = [g for g in p.gift_rows if g.get('repeat_end') and (g.get('group_id') or 0) > 0]
    raw_sum = sum(g.get('gift_count') or 0 for g in p.gift_rows)
    agg_sum = sum(g.get('gift_count') or 0 for g in p.gift_rows
                  if not (g.get('repeat_end') and (g.get('group_id') or 0) > 0))
    print(f"  [1] 缺 gift_id/name={bad_id}  {'✅ 全带' if not bad_id else '❌ 有缺失'}", flush=True)
    print(f"  [2] repeat_end 分布={dict(Counter(g.get('repeat_end') for g in p.gift_rows))}", flush=True)
    print(f"  [3] group_id 分布（0=单发）={dict(Counter((g.get('group_id') or 0) for g in p.gift_rows))}", flush=True)
    print(f"  [4] gift_count(combo_count) 分布={dict(Counter(g.get('gift_count') for g in p.gift_rows))}", flush=True)
    print(f"  [5] diamond_value(单价) 分布={dict(Counter(g.get('diamond_value') for g in p.gift_rows))}", flush=True)
    print(f"  [6] 连击收尾消息数（repeat_end&&group_id>0，聚合跳过）={len(skip_rows)}", flush=True)
    if skip_rows:
        print(f"      样例={skip_rows[0]}", flush=True)
    print(f"  [7] 件数（仅供参考，不再落盘）：原始 sum(combo_count)={raw_sum} vs 跳过收尾后={agg_sum}",
          flush=True)
    user_ids = {str(g['user_id']) for g in p.gift_rows if g.get('user_id')}
    diamond_sum = sum((g.get('diamond_value') or 0) * (g.get('gift_count') or 0)
                      for g in p.gift_rows
                      if not (g.get('repeat_end') and (g.get('group_id') or 0) > 0))
    print(f"  [8] 价值（仅供参考，不再落盘）：Σ(diamond×combo)={diamond_sum}  "
          f"送礼人数(去重)={len(user_ids)}", flush=True)
    print(f"      ⚠️ 礼物聚合特征已从口径移除，room.json 不输出；以下仅为本次实测统计", flush=True)
    seen = sorted(getattr(p.fetcher, '_seen_methods', set()))
    print(f"  [9] 礼物 method={[m for m in seen if 'Gift' in m]}  "
          f"未处理 method={sorted(getattr(p.fetcher, '_unhandled_methods', set()))}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*", default=None, help="指定抖音号（跳过探测）")
    ap.add_argument("--duration", type=int, default=600, help="采集时长（秒）")
    ap.add_argument("--probe", action="store_true", help="只探测开播状态")
    ap.add_argument("--max", type=int, default=4, help="最多并发采集几个在播主播")
    args = ap.parse_args()

    if args.ids:
        picked = [(lid, lid, lid) for lid in args.ids]
    else:
        print(f"探测 {len(ENTERTAINMENT_ANCHORS)} 个娱乐/才艺主播开播状态...", flush=True)
        picked = []
        for name, lid in ENTERTAINMENT_ANCHORS:
            f = DouyinLiveWebFetcher(lid, None)
            try:
                ok, status, nick, uid = f.get_room_status()
                is_live = ok and status == '正在直播'
                print(f"  {'[在播]' if is_live else '[未播]'} {name}({lid}) -> {nick}", flush=True)
                if is_live:
                    picked.append((name, lid, nick))
            except Exception as e:
                print(f"  [错误] {name}({lid}) -> {e}", flush=True)
            time.sleep(0.8)

    if args.probe:
        print(f"\n在播主播共 {len(picked)} 个", flush=True)
        return
    if not picked:
        print("\n当前无在播的娱乐/才艺主播（这类主播多在 20:00-24:00 开播）。"
              "可用 --ids 指定其他直播间。", flush=True)
        return

    picked = picked[:args.max]
    print(f"\n开始并发采集 {len(picked)} 个主播，时长 {args.duration}s:", flush=True)
    for name, lid, nick in picked:
        print(f"  - {name}({lid})", flush=True)

    workers = [GiftProbe(name, lid, nick, args.duration) for name, lid, nick in picked]
    for w in workers:
        w.start()
        time.sleep(2)
    for w in workers:
        w.join()

    print("\n" + "=" * 72, flush=True)
    print("礼物口径校验汇总", flush=True)
    print("=" * 72, flush=True)
    for w in workers:
        report(w)

    hit = [w for w in workers if w.gift_rows]
    print("\n" + "-" * 72, flush=True)
    if hit:
        print(f"✅ {len(hit)}/{len(workers)} 个主播收到礼物事件 —— 礼物口径已用真实数据验证", flush=True)
    else:
        print(f"➖ {len(workers)} 个主播均未收到礼物事件 —— 继续排查通道/房间", flush=True)


if __name__ == "__main__":
    main()
