#!/usr/bin/python
# coding:utf-8 -*-
"""与辉同行 1h 字段逻辑验证测试（纯 WebSocket，无浏览器/音视频）

验证目标（2026-09-24 更新 → 覆盖全部当前字段口径）：
1. Bug 2：purchase 事件 ids 是否为 promotion_id —— 看 PURCHASE 日志的 f6_dump 完整结构
2. 点赞：LikeMessage.total（累计）是否合理增长 —— 看 LIKE 日志与进度里的 like_total
3. 关注：SocialMessage.action 取值分布 —— 验证 1=关注/3=分享，以及 follow_count 是否被分享清零
4. 礼物：gift 事件是否带全 gift_id/name/repeat_end/group_id（本轮新增字段）
5. 讲解：WebcastAwemeShopExplainMessage 是否下发、explain 事件 active 取值
6. 方法级诊断：结束打印 _seen_methods / _unhandled_methods，核对 method 名
"""
import sys
import time
import json
import os
import threading
from datetime import datetime
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
# 脚本位于 test/ 子目录，需把项目根加入 sys.path 才能 import liveMan / data_recorder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher
from data_recorder import DataRecorder

DURATION = 3600          # 测试时长（秒）
LIVE_ID = "56697889278"  # 与辉同行


def log_cb(log_type, message):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}][{log_type}] {message}", flush=True)


def _progress(elapsed, counters, recorder):
    sale_pids = sum(1 for t in recorder.product_times.values() if t.get('sale_count', 0) > 0)
    sale_total = sum(t.get('sale_count', 0) for t in recorder.product_times.values())
    print(f"[进度 {elapsed // 60}min] 事件={dict(counters)} | 商品={len(recorder.product_ids)} | "
          f"成交商品={sale_pids} 成交次数={sale_total} | 点赞累计={recorder.like_total} | "
          f"粉丝数={recorder.follow_count}", flush=True)


def _verify(recorder, fetcher, counters):
    """扫描 events.jsonl + fetcher 方法集合，做字段真实性校验"""
    ep = os.path.join(recorder.path, 'events.jsonl')
    action_dist = Counter()
    gift_n = 0
    gift_bad = 0
    gift_sample = None
    repeat_end_dist = Counter()
    explain_n = 0
    explain_active_dist = Counter()
    explain_type_dist = Counter()
    purchase_n = 0
    purchase_pid_bad = 0
    follow_fc = None

    if os.path.exists(ep):
        with open(ep, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    x = json.loads(line)
                except Exception:
                    continue
                t = x.get('type')
                if t in ('follow', 'social'):
                    action_dist[x.get('action')] += 1
                    if t == 'follow' and isinstance(x.get('follow_count'), int):
                        follow_fc = x['follow_count']
                elif t == 'gift':
                    gift_n += 1
                    if not x.get('gift_id') or not x.get('gift_name'):
                        gift_bad += 1
                    if gift_sample is None:
                        gift_sample = {k: x.get(k) for k in
                                       ('gift_id', 'gift_name', 'gift_type', 'gift_count',
                                        'diamond_value', 'repeat_end', 'group_id')}
                    repeat_end_dist[x.get('repeat_end')] += 1
                elif t == 'explain':
                    explain_n += 1
                    if 'active' in x:
                        explain_active_dist[x['active']] += 1
                    else:
                        explain_type_dist[str(x.get('explain_type'))] += 1
                elif t == 'purchase':
                    ids = x.get('ids', [])
                    purchase_n += len(ids)
                    for p in ids:
                        s = str(p)
                        if not (s.isdigit() and len(s) == 19):
                            purchase_pid_bad += 1

    print("\n" + "=" * 70)
    print("字段真实性校验", flush=True)
    print("=" * 70)
    print(f"[1] purchase: 成交 id 数={purchase_n}  非19位={purchase_pid_bad}  "
          f"{'✅ 全部为 19 位 promotion_id' if purchase_n and not purchase_pid_bad else '⚠️ 见上'}", flush=True)
    print(f"[2] like_total={recorder.like_total}  事件数={counters.get('like', 0)}", flush=True)
    print(f"[3] follow/social action 分布={dict(action_dist)}  "
          f"follow_count={follow_fc} (room.json={recorder.follow_count})", flush=True)
    if action_dist.get(1) and recorder.follow_count == 0:
        print("    ❌ follow_count 被清零（follow 事件存在但值为 0）", flush=True)
    elif action_dist.get(1):
        print(f"    ✅ follow_count 未被分享清零", flush=True)
    print(f"[4] gift: 事件数={gift_n}  缺 gift_id/name={gift_bad}  "
          f"repeat_end 分布={dict(repeat_end_dist)}", flush=True)
    print(f"    样例={gift_sample}", flush=True)
    print(f"[5] explain: 事件数={explain_n}  active 分布={dict(explain_active_dist)}  "
          f"explainType 分布={dict(explain_type_dist)}", flush=True)

    # [6] 方法级诊断：确认 method 名是否正确、哪些消息未注册处理器
    seen = sorted(getattr(fetcher, '_seen_methods', set()))
    unhandled = sorted(getattr(fetcher, '_unhandled_methods', set()))
    print(f"[6] 收到的方法总数={len(seen)}", flush=True)
    print(f"    _unhandled_methods（未注册处理器）={unhandled}", flush=True)
    gift_seen = [m for m in seen if 'Gift' in m]
    explain_seen = [m for m in seen if 'Explain' in m or 'Product' in m or 'Shop' in m]
    print(f"    礼物相关 method={gift_seen}", flush=True)
    print(f"    讲解/商品相关 method={explain_seen}", flush=True)
    print(f"    全部 method={seen}", flush=True)


def main():
    counters = Counter()
    fetcher = DouyinLiveWebFetcher(LIVE_ID, log_callback=log_cb)
    status = fetcher.get_room_status()
    print(f"[STATUS] {status}", flush=True)

    recorder = DataRecorder(fetcher.room_id, LIVE_ID)
    recorder.set_room_info({
        "title": status[2],
        "anchor_nickname": status[2],
        "anchor_id": status[3],
        "room_status": status[1],
    })

    def data_cb(event_type, data):
        counters[event_type] += 1
        recorder.on_event(event_type, data)

    fetcher.data_callback = data_cb

    print(f"[开始] {datetime.now().strftime('%H:%M:%S')} 采集 {DURATION}s，输出目录 {recorder.path}", flush=True)
    ws_thread = threading.Thread(target=fetcher.start, daemon=True)
    ws_thread.start()

    start = time.time()
    next_report = 300  # 每 5 分钟打印一次进度
    try:
        while time.time() - start < DURATION:
            time.sleep(5)
            elapsed = int(time.time() - start)
            if elapsed >= next_report:
                _progress(elapsed, counters, recorder)
                next_report += 300
    finally:
        fetcher.stop()
        ws_thread.join(timeout=5)
        recorder.close()

    print("\n" + "=" * 70, flush=True)
    print("采集结束汇总", flush=True)
    print("=" * 70, flush=True)
    _progress(int(time.time() - start), counters, recorder)
    print(f"事件类型分布: {dict(counters)}", flush=True)
    print(f"输出目录: {recorder.path}", flush=True)

    _verify(recorder, fetcher, counters)


if __name__ == "__main__":
    main()
