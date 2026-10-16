#!/usr/bin/python
# coding:utf-8
"""与辉同行粉丝团标识 10min 实测 + 判别力分析

目的：判断粉丝团数据**是否值得加入特征体系**。除了覆盖率，还要看它有没有判别力 ——
     粉丝团成员与非成员的行为是否显著不同、等级是否与活跃度相关。

采集：10 分钟内所有含 User 的消息，按 user_id 聚合「粉丝团属性 + 行为计数」。
分析：① 打码检测 ② 字段覆盖率 ③ 等级/anchor_id/status 分布 ④ club_name 非空率
     ⑤ 加入/升级事件 ⑥ 多粉丝团用户 ⑦ 成员 vs 非成员行为对比 ⑧ 等级-活跃度关系

用法：
  python probe_fansclub_10min.py --id 56697889278 --duration 600
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher
from protobuf.douyin import (ChatMessage, MemberMessage, LikeMessage, SocialMessage,
                             FansclubMessage, EmojiChatMessage)

WATCH = {
    'WebcastChatMessage': ChatMessage,
    'WebcastEmojiChatMessage': EmojiChatMessage,
    'WebcastMemberMessage': MemberMessage,
    'WebcastLikeMessage': LikeMessage,
    'WebcastSocialMessage': SocialMessage,
    'WebcastFansclubMessage': FansclubMessage,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="56697889278")
    ap.add_argument("--duration", type=int, default=600)
    args = ap.parse_args()

    f = DouyinLiveWebFetcher(args.id, None)
    st = f.get_room_status()
    print(f"[STATUS] {st[1]} {st[2]}  room_id={f.room_id}", flush=True)
    if not st[0]:
        return

    per_method = defaultdict(Counter)          # method -> {n, has_fc, lvl_pos, name_nonempty}
    user_fc = {}                                # user_id -> (level, anchor_id, status, club_name)
    user_beh = defaultdict(Counter)             # user_id -> {chat, enter, like, follow, social}
    user_anchor_set = defaultdict(set)          # user_id -> {anchor_id}
    lvl_dist = Counter()
    aid_dist = Counter()
    status_dist = Counter()
    cross = Counter()                           # (status, anchor_id非零)
    cname = Counter()
    fc_events = []                              # (ts, type, level, anchor_id, content)
    t0 = time.time()

    def raw_cb(method, payload, msg):
        cls = WATCH.get(method)
        if cls is None:
            return
        try:
            m = cls().parse(payload)
        except Exception:
            return
        s = per_method[method]
        s['n'] += 1

        is_fc_msg = (method == 'WebcastFansclubMessage')
        u = getattr(m, 'user', None)
        if is_fc_msg:
            d = getattr(getattr(u, 'fans_club', None), 'data', None) if u else None
            fc_events.append((
                datetime.now().strftime('%H:%M:%S'), m.type,
                getattr(d, 'level', 0) if d else 0,
                getattr(d, 'anchor_id', 0) if d else 0,
                (m.content or '')[:60]))

        if u is None or not getattr(u, 'id', 0):
            return
        uid = str(u.id)
        fc = getattr(u, 'fans_club', None)
        d = getattr(fc, 'data', None) if fc else None
        if d is None:
            return
        s['has_fc'] += 1
        if d.level:
            s['lvl_pos'] += 1
        if d.club_name:
            s['name_nonempty'] += 1

        # 等级/归属只在首次见到该用户时计数（避免被高频弹幕刷偏）
        if uid not in user_fc:
            user_fc[uid] = (d.level, d.anchor_id, d.user_fans_club_status, d.club_name)
            lvl_dist[d.level] += 1
            aid_dist[d.anchor_id] += 1
            status_dist[d.user_fans_club_status] += 1
            cross[(d.user_fans_club_status, 1 if d.anchor_id else 0)] += 1
            if d.club_name:
                cname[d.club_name] += 1
        if d.anchor_id:
            user_anchor_set[uid].add(d.anchor_id)

        # 行为计数
        if method == 'WebcastChatMessage':
            user_beh[uid]['chat'] += 1
        elif method == 'WebcastEmojiChatMessage':
            user_beh[uid]['emoji'] += 1
        elif method == 'WebcastMemberMessage':
            user_beh[uid]['enter'] += 1
        elif method == 'WebcastLikeMessage':
            user_beh[uid]['like'] += 1
        elif method == 'WebcastSocialMessage':
            act = getattr(m, 'action', 0)
            if act == 1:
                user_beh[uid]['follow'] += 1
            elif act == 3:
                user_beh[uid]['share'] += 1

    f.raw_callback = raw_cb
    t = threading.Thread(target=f.start, daemon=True)
    t.start()

    next_report = 120
    while time.time() - t0 < args.duration:
        time.sleep(5)
        el = int(time.time() - t0)
        if el >= next_report:
            print(f"[进度 {el}s] 用户={len(user_fc)} 粉丝团事件={len(fc_events)} "
                  f"消息={sum(v['n'] for v in per_method.values())}", flush=True)
            next_report += 120
    f.stop()

    # ================= 分析 =================
    total_users = len(user_fc)
    P = print
    P("\n" + "=" * 78)
    P(f"与辉同行 粉丝团 10min 实测分析（{args.duration}s）")
    P("=" * 78)

    # 1 打码
    masked = sum(1 for u in user_fc if u == '111111')
    all_ids = Counter()
    P(f"\n[1] 打码检测")
    P(f"    不同 user_id 数 = {total_users}  其中 111111 = {masked} "
      f"({masked / total_users * 100 if total_users else 0:.1f}%)")
    P(f"    → {'❌ 完全打码，无法归属用户' if total_users and masked / total_users > 0.9 else '✅ 未打码，可按用户归属'}")

    # 2 覆盖率
    P(f"\n[2] 粉丝团字段覆盖率（按 method）")
    P(f"    {'method':<32}{'消息数':>8}{'带fans_club':>13}{'level>0':>9}{'name非空':>10}")
    for m, s in sorted(per_method.items(), key=lambda x: -x[1]['n']):
        P(f"    {m:<32}{s['n']:>8}{s['has_fc']:>13}{s['lvl_pos']:>9}{s['name_nonempty']:>10}")

    # 3 等级分布
    member = [u for u, v in user_fc.items() if v[0] > 0]
    non_member = [u for u, v in user_fc.items() if v[0] == 0]
    P(f"\n[3] 粉丝团等级分布（按**首次出现**去重，共 {total_users} 个用户）")
    P(f"    Lv0(非成员) = {len(non_member)} ({len(non_member) / total_users * 100:.1f}%)")
    P(f"    Lv>0(成员)  = {len(member)} ({len(member) / total_users * 100:.1f}%)")
    mem_lv = Counter(v[0] for v in user_fc.values() if v[0] > 0)
    P(f"    成员等级分布: {dict(sorted(mem_lv.items()))}")

    # 4 anchor_id
    P(f"\n[4] anchor_id 分布（= 粉丝团归属主播）")
    for aid, c in aid_dist.most_common(10):
        P(f"    {aid}: {c} 人")
    P(f"    不同 anchor_id 数 = {len([a for a in aid_dist if a])}")

    # 5 status
    P(f"\n[5] user_fans_club_status 分布与交叉表")
    P(f"    status 分布: {dict(status_dist)}")
    P(f"    (status, anchor_id非零) -> 人数: {dict(cross)}")

    # 6 club_name
    P(f"\n[6] club_name 非空率")
    named = sum(1 for v in user_fc.values() if v[3])
    P(f"    club_name 非空用户 = {named}/{total_users} ({named / total_users * 100 if total_users else 0:.1f}%)")
    P(f"    观测到的粉丝团名称: {dict(cname.most_common(10))}")

    # 7 事件
    P(f"\n[7] WebcastFansclubMessage（加入/升级事件）")
    P(f"    事件数 = {len(fc_events)}   type 分布 = {dict(Counter(e[1] for e in fc_events))}")
    if fc_events:
        P(f"    前 6 条:")
        for e in fc_events[:6]:
            P(f"      {e[0]} type={e[1]} lv={e[2]} anchor={e[3]}  {e[4]!r}")
        # 事件率
        P(f"    速率 = {len(fc_events) / (args.duration / 60):.1f} 次/分钟")

    # 8 多粉丝团
    multi = {u: s for u, s in user_anchor_set.items() if len(s) > 1}
    P(f"\n[8] 多粉丝团用户")
    P(f"    观测到归属的 anchor_id >= 2 个的用户 = {len(multi)}/{len(user_anchor_set)}")
    if multi:
        for u, s in list(multi.items())[:3]:
            P(f"      {u}: {sorted(s)}")

    # 9 行为对比（判别力核心）
    def beh_stats(uids):
        n = len(uids)
        if not n:
            return None
        agg = Counter()
        for u in uids:
            agg.update(user_beh.get(u, {}))
        return {
            'n': n,
            '人均弹幕': agg['chat'] / n,
            '人均进场': agg['enter'] / n,
            '人均点赞': agg['like'] / n,
            '关注率%': sum(1 for u in uids if user_beh.get(u, {}).get('follow')) / n * 100,
            '分享率%': sum(1 for u in uids if user_beh.get(u, {}).get('share')) / n * 100,
            '发言率%': sum(1 for u in uids if user_beh.get(u, {}).get('chat')) / n * 100,
        }

    P(f"\n[9] 判别力：粉丝团成员 vs 非成员 的行为对比")
    a, b = beh_stats(member), beh_stats(non_member)
    if a and b:
        P(f"    {'指标':<12}{'成员':>12}{'非成员':>12}{'倍数':>10}")
        for k in ('n', '人均弹幕', '人均进场', '人均点赞', '发言率%', '关注率%', '分享率%'):
            va, vb = a[k], b[k]
            ratio = (va / vb) if vb else float('inf')
            P(f"    {k:<12}{va:>12.2f}{vb:>12.2f}{ratio:>10.2f}")

    # 10 等级-活跃度
    P(f"\n[10] 等级与活跃度关系（成员按等级分组）")
    P(f"    {'等级段':<10}{'人数':>6}{'人均弹幕':>10}{'人均进场':>10}{'关注率%':>10}")
    buckets = [(1, 5), (6, 10), (11, 15), (16, 25)]
    for lo, hi in buckets:
        us = [u for u, v in user_fc.items() if lo <= v[0] <= hi]
        if not us:
            continue
        agg = Counter()
        for u in us:
            agg.update(user_beh.get(u, {}))
        foll = sum(1 for u in us if user_beh.get(u, {}).get('follow')) / len(us) * 100
        P(f"    Lv{lo}-{hi:<6}{len(us):>6}{agg['chat'] / len(us):>10.2f}"
          f"{agg['enter'] / len(us):>10.2f}{foll:>10.2f}")

    # 落盘
    out = os.path.join("test_result",
                       f"fansclub_{args.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs("test_result", exist_ok=True)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump({
            'anchor': st[2], 'live_id': args.id, 'duration': args.duration,
            'users': {u: {'fc': list(v), 'beh': dict(user_beh.get(u, {}))}
                      for u, v in user_fc.items()},
            'fc_events': fc_events,
            'per_method': {k: dict(v) for k, v in per_method.items()},
        }, fh, ensure_ascii=False, indent=1)
    P(f"\n原始聚合数据已落盘: {out}")


if __name__ == "__main__":
    main()
