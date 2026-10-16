#!/usr/bin/python
# coding:utf-8
"""粉丝团身份标识可获取性实测

`FansclubMessage` 目前只打日志、不产出结构化数据（`_parseFansclubMsg` 仅取 content）。
本脚本实测三类来源能拿到什么：

  A. `WebcastFansclubMessage`（升级/加入事件）—— type(1升级/2加入) + content + user.fans_club
  B. 其他消息里每个用户的 `user.fans_club.data`（club_name / level / user_fans_club_status / anchor_id）
     —— 这是"身份标识"能否**随消息长期携带**的关键
  C. `FansClub.prefer_data`（map<int32, FansClubData>）是否比 data 更丰富

用法：
  python probe_fansclub.py --id 731918444565 --duration 180
"""
import argparse
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher
from protobuf.douyin import (FansclubMessage, ChatMessage, MemberMessage, LikeMessage,
                             SocialMessage, GiftMessage, EmojiChatMessage)

WATCH = {
    'WebcastFansclubMessage': FansclubMessage,
    'WebcastChatMessage': ChatMessage,
    'WebcastMemberMessage': MemberMessage,
    'WebcastLikeMessage': LikeMessage,
    'WebcastSocialMessage': SocialMessage,
    'WebcastEmojiChatMessage': EmojiChatMessage,
    'WebcastGiftMessage': GiftMessage,
}


def club_of(user):
    """返回 (club_name, level, status, anchor_id, prefer_keys) 或 None"""
    if user is None:
        return None
    fc = getattr(user, 'fans_club', None)
    if fc is None:
        return None
    d = getattr(fc, 'data', None)
    name = getattr(d, 'club_name', '') if d else ''
    lvl = getattr(d, 'level', 0) if d else 0
    st = getattr(d, 'user_fans_club_status', 0) if d else 0
    aid = getattr(d, 'anchor_id', 0) if d else 0
    pref = getattr(fc, 'prefer_data', None) or {}
    return (name, lvl, st, aid, tuple(sorted(pref.keys())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="731918444565")
    ap.add_argument("--duration", type=int, default=180)
    args = ap.parse_args()

    f = DouyinLiveWebFetcher(args.id, None)
    st = f.get_room_status()
    print(f"[STATUS] {st[1]} {st[2]}  room_id={f.room_id}", flush=True)
    if not st[0]:
        return

    # 按 method 统计：总消息数 / 带 fans_club 的用户数 / club_name 非空数 / level>0 数
    stat = defaultdict(lambda: {'n': 0, 'has_fc': 0, 'name_nonempty': 0, 'lvl_pos': 0})
    club_names = Counter()
    levels = Counter()
    statuses = Counter()
    prefer_keys = Counter()
    anchor_ids = Counter()
    fc_type = Counter()
    fc_samples = []
    prefer_samples = []

    lock = threading.Lock()

    def raw_cb(method, payload, msg):
        cls = WATCH.get(method)
        if cls is None:
            return
        try:
            m = cls().parse(payload)
        except Exception:
            return
        if method == 'WebcastFansclubMessage':
            fc_type[m.type] += 1
            if len(fc_samples) < 5:
                fc_samples.append((m.type, m.content, club_of(m.user)))
            u = m.user
        else:
            u = getattr(m, 'user', None)

        s = stat[method]
        s['n'] += 1
        info = club_of(u)
        if info:
            s['has_fc'] += 1
            name, lvl, stt, aid, pref = info
            if name:
                s['name_nonempty'] += 1
                club_names[name] += 1
            if lvl:
                s['lvl_pos'] += 1
                levels[lvl] += 1
            statuses[stt] += 1
            anchor_ids[aid] += 1
            prefer_keys[pref] += 1
            if pref and len(prefer_samples) < 3:
                prefer_samples.append((method, name, lvl, pref))

    f.raw_callback = raw_cb
    t = threading.Thread(target=f.start, daemon=True)
    t.start()
    print(f"[采集] {args.duration}s ...", flush=True)
    time.sleep(args.duration)
    f.stop()

    print("\n" + "=" * 76, flush=True)
    print("粉丝团身份标识可获取性", flush=True)
    print("=" * 76, flush=True)
    print(f"{'method':<34}{'消息数':>8}{'带fans_club':>12}{'club_name非空':>14}{'level>0':>9}", flush=True)
    for m, s in sorted(stat.items(), key=lambda x: -x[1]['n']):
        print(f"{m:<34}{s['n']:>8}{s['has_fc']:>12}{s['name_nonempty']:>14}{s['lvl_pos']:>9}",
              flush=True)

    print(f"\n[A] FansclubMessage.type 分布 (1=升级, 2=加入) = {dict(fc_type)}", flush=True)
    for typ, content, info in fc_samples:
        print(f"      type={typ} content={content!r}\n        user.fans_club={info}", flush=True)

    print(f"\n[B] 观测到的粉丝团名称 ({len(club_names)} 种): {dict(club_names.most_common(8))}", flush=True)
    print(f"    粉丝团等级分布: {dict(sorted(levels.items())[:20])}", flush=True)
    print(f"    user_fans_club_status 分布: {dict(statuses)}", flush=True)
    print(f"    FansClubData.anchor_id 分布: {dict(anchor_ids.most_common(5))}", flush=True)
    print(f"    prefer_data 键组合: {dict(prefer_keys.most_common(5))}", flush=True)


if __name__ == "__main__":
    main()
