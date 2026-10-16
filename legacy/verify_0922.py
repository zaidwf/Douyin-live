#!/usr/bin/python
# coding:utf-8 -*-
"""校验 2026-09-22 爬取数据口径。

遍历 data/live_*_20260922_* 目录，检查核心字段语义：
  - room.json: like_total / follow_count / gift_* / shopping_down_count / promotions(成交)
  - events.jsonl: follow/social action 分布、purchase promotion_id 19 位、gift 字段完整
"""
import glob
import io
import json
import os
import sys
from collections import Counter


def load_json(p):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def is_19digit(pid):
    s = str(pid)
    return s.isdigit() and len(s) == 19


def main():
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

    dirs = sorted(glob.glob('data/live_*_20260922_*'))
    print(f"共 {len(dirs)} 个 20260922 数据目录\n")

    anomalies = []
    for d in dirs:
        name = os.path.basename(d)
        rd = load_json(os.path.join(d, 'room.json'))
        if rd is None:
            print(f"[{name}] 缺 room.json（采集未正常收尾）")
            anomalies.append(f"{name}: 缺 room.json")
            continue

        # ---- room.json 口径 ----
        # 礼物聚合字段已于 2026-09-28 移除（Web IM 通道不承载 WebcastGiftMessage）
        has_gift = 'gift_total_count' in rd  # 旧 schema 残留检测（新数据应为 False）
        like_total = rd.get('like_total')
        follow_count = rd.get('follow_count')
        shop_down = rd.get('shopping_down_count')
        promos = rd.get('promotions', [])
        sale_sum = sum(p.get('sale_count', 0) for p in promos)

        # ---- events.jsonl 口径 ----
        ec = Counter()
        action = Counter()
        pids = []
        gift_bad = 0
        gift_sample = None
        ep = os.path.join(d, 'events.jsonl')
        if os.path.exists(ep):
            with open(ep, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        x = json.loads(line)
                    except Exception:
                        continue
                    t = x.get('type')
                    ec[t] += 1
                    if t in ('follow', 'social'):
                        action[x.get('action')] += 1
                    elif t == 'purchase':
                        pids.extend(x.get('ids', []))
                    elif t == 'gift':
                        if not x.get('gift_id') or not x.get('gift_name'):
                            gift_bad += 1
                        if gift_sample is None:
                            gift_sample = {k: x.get(k) for k in ('gift_id', 'gift_name', 'gift_count', 'diamond_value')}

        pid_ok = (all(is_19digit(p) for p in pids) if pids else '-')
        total_events = sum(ec.values())

        print(f"[{name}]")
        print(f"  room.json: like_total={like_total} follow_count={follow_count} "
              f"shop_down={shop_down} 商品={len(promos)} 成交商品={sum(1 for p in promos if p.get('sale_count',0)>0)} "
              f"成交次数={sale_sum}")
        print(f"  gift: 旧聚合字段残留={has_gift}（2026-09-28 起已从口径移除，新数据应为 False）")
        print(f"  events={dict(ec)}  合计={total_events}")
        print(f"  follow/social action={dict(action)}  purchase_ids 19位={pid_ok}  gift样例={gift_sample}  gift缺字段={gift_bad}")

        # ---- 异常判定 ----
        if action.get(2):  # action=2 取关：此前从未观测，出现需复核
            anomalies.append(f"{name}: follow/social 出现 action=2 x{action[2]}")
        if pid_ok is False:
            anomalies.append(f"{name}: 存在非 19 位 promotion_id")
        if gift_bad:
            anomalies.append(f"{name}: {gift_bad} 条 gift 缺 gift_id/name")
        if has_gift:
            anomalies.append(f"{name}: room.json 仍含 gift_* 字段（旧 schema 数据，礼物聚合已废弃）")
        if like_total == 0:
            anomalies.append(f"{name}: like_total=0（可能未收到点赞）")
        print()

    print("=" * 70)
    print(f"异常汇总（{len(anomalies)} 条）")
    for a in anomalies:
        print(f"  - {a}")


if __name__ == "__main__":
    main()
