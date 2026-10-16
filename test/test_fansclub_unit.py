#!/usr/bin/python
# coding:utf-8
"""粉丝团群体级特征单元验证（合成 protobuf，不依赖服务端）

验证 2026-09-28 接入的三类群体级特征（口径见 modify.md 续5/续6）：

  1. 群体级构成：member_ratio / level_mean / level_dist / anchor_member_counts
  2. 时序信号：series.jsonl 的 stats 记录附带 fansclub_member_ratio 等快照；加入/升级事件计数
  3. 关系边：fansclub_edges.jsonl（ν_Au → ν_An，仅成员，逐用户一行）
  4. 可信度标记：uid_masked（user_id 全为 111111 时置 True，特征不可信）

用法：
  python test/test_fansclub_unit.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher, parseFansclubContent
from data_recorder import DataRecorder
from protobuf.douyin import (ChatMessage, MemberMessage, FansclubMessage,
                             FansClub, FansClubData, User)

ROOM_ID = "unit-room"
LIVE_ID = "unit-anchor"
PASS, FAIL = [], []


def check(label, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(label)
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")


def chat_payload(uid, nick, content, level, anchor_id, status=1, club_name=''):
    """构造一条带粉丝团标识的 ChatMessage"""
    msg = ChatMessage(
        content=content,
        user=User(id=uid, nick_name=nick,
                  fans_club=FansClub(data=FansClubData(
                      club_name=club_name, level=level,
                      user_fans_club_status=status, anchor_id=anchor_id))),
    )
    return bytes(msg)


def fansclub_payload(uid, nick, fc_type, content, level, anchor_id, status=1):
    msg = FansclubMessage(
        type=fc_type, content=content,
        user=User(id=uid, nick_name=nick,
                  fans_club=FansClub(data=FansClubData(
                      level=level, user_fans_club_status=status, anchor_id=anchor_id))),
    )
    return bytes(msg)


def main():
    events = []
    f = DouyinLiveWebFetcher(LIVE_ID, None)
    f.data_callback = lambda et, d: events.append((et, dict(d)))

    out_dir = tempfile.mkdtemp(prefix="fansclub_unit_")
    try:
        rec = DataRecorder(ROOM_ID, LIVE_ID, base_dir=out_dir)

        print("\n[1] 群体级构成：5 个用户（3 成员 + 2 非成员）")
        # 成员：u1 Lv10 anchor=AAA；u2 Lv5 anchor=AAA；u3 Lv2 anchor=BBB
        # 非成员：u4 Lv0 anchor=0；u5 Lv0 anchor=0
        for p in [
            chat_payload(111, "甲", "a", 10, 900001, club_name="团A"),
            chat_payload(222, "乙", "b", 5, 900001),
            chat_payload(333, "丙", "c", 2, 900002),
            chat_payload(444, "丁", "d", 0, 0),
            chat_payload(555, "戊", "e", 0, 0),
        ]:
            f._parseChatMsg(p)
            rec.on_event("chat", events[-1][1])
        # 同一用户重复出现（u1 再发言 2 次，等级已涨到 12）→ 去重后仍按首次快照 Lv10
        for _ in range(2):
            f._parseChatMsg(chat_payload(111, "甲", "again", 12, 900001))
            rec.on_event("chat", events[-1][1])

        check("去重用户数", len(rec.fansclub_users), 5)
        check("成员数(level>0)", rec.fansclub_member_count, 3)
        check("成员占比(3/5, 重复出现不影响)", rec._fansclub_ratio(), 0.6)
        check("首次快照锁 Lv10（非后来的 Lv12）", rec.fansclub_users['111'][0], 10)
        check("等级分布", dict(rec.fansclub_level_dist), {10: 1, 5: 1, 2: 1})
        check("关系边聚合(每个粉丝团的成员数)",
              dict(rec.fansclub_anchor_users), {900001: 2, 900002: 1})

        print("\n[2] 粉丝团消息事件：type=1 升级、type=2 加入、type=6（静默，不作语义解释）")
        # 公告用**真实模板**：type=1 的【】里是**主播名**（故刻意与下面的团名「团A」不同），
        # type=2 的「成为第 N 名 X 成员」里 X 才是**团名**。
        for p in [
            fansclub_payload(111, "甲", 1, "甲 刚刚升级至【主播甲】粉丝团 Lv10", 10, 900001),
            fansclub_payload(666, "己", 2, "恭喜 己 成为第1名团A成员", 1, 900001),
            fansclub_payload(777, "庚", 6, "", 9, 900001),
            fansclub_payload(888, "辛", 6, "", 8, 900001),
        ]:
            f._parseFansclubMsg(p)
            rec.on_event("fansclub", events[-1][1])
        check("全量消息计数(不分 type)", rec.fansclub_event_count, 4)
        check("type 原始分布", dict(rec.fansclub_event_types), {'1': 1, '2': 1, '6': 2})
        check("fansclub 事件共 4 条", len([1 for et, _ in events if et == 'fansclub']), 4)
        # 事件里的用户也计入分组（666/777/888 是新增成员 → 成员数 3+3=6，用户数 5+3=8）
        check("事件用户也纳入聚合(用户数)", len(rec.fansclub_users), 8)
        check("事件用户也纳入聚合(成员数)", rec.fansclub_member_count, 6)
        # —— 加入粉丝团（2026-09-29 引入）——
        check("加入计数(type=2)", rec.fansclub_join_count, 1)
        check("升级计数(type=1)", rec.fansclub_upgrade_count, 1)
        check("join_seq 首个=最新", rec.fansclub_join_seq_latest, 1)
        check("团名映射只来自 type=2（type=1 的主播名不入表）",
              rec.fansclub_club_names, {'900001': '团A'})
        check("模板全命中(unparsed 为空)", dict(rec.fansclub_unparsed), {})

        print("\n[3] 时序快照：stats 记录附带粉丝团快照")
        f._parseRoomUserSeqMsg  # noqa: B018  (仅提示来源)
        rec.on_event("stats", {"viewer_count": 1000, "total_pv": 5000})
        rec.on_event("stats", {"viewer_count": 1100, "total_pv": 5200})
        check("viewer_series 长度", len(rec.viewer_series), 2)
        s0 = rec.viewer_series[-1]
        check("快照含 fansclub_member_ratio", s0.get('fansclub_member_ratio'),
              round(6 / 8, 4))
        check("快照含 fansclub_member_count", s0.get('fansclub_member_count'), 6)
        check("快照含 fansclub_event_count(全量累计)",
              s0.get('fansclub_event_count'), 4)
        check("快照含 fansclub_join_count", s0.get('fansclub_join_count'), 1)
        check("快照含 fansclub_upgrade_count", s0.get('fansclub_upgrade_count'), 1)
        check("快照含 fansclub_join_seq_latest", s0.get('fansclub_join_seq_latest'), 1)
        check("观看人数原字段仍在", s0.get('viewer_count'), 1100)

        print("\n[4] 落盘：room.json / fansclub_edges.jsonl")
        rec.close()
        rd = json.load(open(os.path.join(rec.path, "room.json"), encoding='utf-8'))
        fc = rd.get('fansclub')
        check("room.json 含 fansclub 块", isinstance(fc, dict), True)
        check("member_ratio", fc['member_ratio'], round(6 / 8, 4))
        check("member_count", fc['member_count'], 6)
        check("user_count", fc['user_count'], 8)
        check("level_dist", fc['level_dist'], {'2': 1, '5': 1, '8': 1, '9': 1, '10': 1, '1': 1})
        # 成员等级 = 甲10 / 乙5 / 丙2（首次快照）+ 己1 / 庚9 / 辛8（fansclub 事件）→ 均值 35/6
        check("level_mean", fc['level_mean'], round((10 + 5 + 2 + 1 + 9 + 8) / 6, 2))
        check("anchor_member_counts", fc['anchor_member_counts'], {'900001': 5, '900002': 1})
        # 全量计数 + type 原始分布。需要分型计数时从 event_type_dist 取：
        # 本例 4 条事件里 2 条是 type=6（静默消息），另 2 条是 type=1/2（加入/升级）
        check("event_count(全量 4 条)", fc['event_count'], 4)
        check("event_type_dist(原始分布)", fc['event_type_dist'], {'1': 1, '2': 1, '6': 2})
        # —— 加入粉丝团 ——
        check("join_count", fc['join_count'], 1)
        check("upgrade_count", fc['upgrade_count'], 1)
        check("join_seq_first", fc['join_seq_first'], 1)
        check("join_seq_latest", fc['join_seq_latest'], 1)
        check("club_names(只来自 type=2)", fc['club_names'], {'900001': '团A'})
        check("unparsed_by_type(模板全命中)", fc['unparsed_by_type'], {})
        check("uid_masked=False（有真实 id）", fc['uid_masked'], False)

        ep = os.path.join(rec.path, "fansclub_edges.jsonl")
        edges = [json.loads(l) for l in open(ep, encoding='utf-8') if l.strip()]
        check("关系边行数 = 成员数", len(edges), 6)
        check("边类型", {e['type'] for e in edges}, {'fansclub_of'})
        check("边不含非成员(无 level=0)", any(e['level'] == 0 for e in edges), False)
        check("边带 anchor_id / level / first_seen",
              sorted(edges[0].keys()),
              ['anchor_id', 'club_name', 'first_seen', 'level', 'status', 'type', 'user_id'])
        e0 = next(e for e in edges if e['user_id'] == '111')
        check("边的 anchor_id", e0['anchor_id'], '900001')
        check("边的 level", e0['level'], 10)
        check("边的 club_name", e0['club_name'], '团A')

        print("\n[5] 打码检测：全部 user_id 为 111111 时 uid_masked=True")
        rec2 = DataRecorder(ROOM_ID, LIVE_ID, base_dir=out_dir)
        for i in range(30):
            f._parseChatMsg(chat_payload(111111, f"用户{i}", "x", 5 if i % 2 else 0, 900001))
            rec2.on_event("chat", events[-1][1])
        check("观测数达标", rec2._uid_obs >= rec2.MASKED_MIN_OBS, True)
        check("uid_masked=True", rec2._uid_is_masked(), True)
        check("去重用户数退化为 1（特征不可信）", len(rec2.fansclub_users), 1)
        rec2.close()
        rd2 = json.load(open(os.path.join(rec2.path, "room.json"), encoding='utf-8'))
        check("room.json 标记 uid_masked", rd2['fansclub']['uid_masked'], True)

        print("\n[6] 观测不足时不应误判为打码")
        rec3 = DataRecorder(ROOM_ID, LIVE_ID, base_dir=out_dir)
        f._parseChatMsg(chat_payload(111111, "甲", "x", 5, 900001))
        rec3.on_event("chat", events[-1][1])
        check("1 次观测 → uid_masked=False", rec3._uid_is_masked(), False)
        rec3.close()

        print("\n[7] content 模板解析（parseFansclubContent，用真实文案）")
        check("type=2 加入 → 序号 + 团名",
              parseFansclubContent(2, "恭喜 敏敏～A 成为第1597966名爱生活成员"),
              {'fc_parse_ok': True, 'fc_join_seq': 1597966, 'fc_club_name': '爱生活'})
        check("type=1 升级 → 只给等级、不给团名（【】里是主播名）",
              parseFansclubContent(1, "致橡树 刚刚升级至【与辉同行】粉丝团 Lv14"),
              {'fc_parse_ok': True, 'fc_upgrade_to_level': 14})
        check("昵称含空格也能解析",
              parseFansclubContent(2, "恭喜 用户 123 成为第7名某某团成员")['fc_join_seq'], 7)
        check("type=6 content 恒空 → 空 dict", parseFansclubContent(6, ""), {})
        check("未命中模板 → fc_parse_ok=False（不静默）",
              parseFansclubContent(2, "欢迎 某某 加入粉丝团"), {'fc_parse_ok': False})
        check("升级文案但 type 报 2 → 不误配", parseFansclubContent(2, "甲 刚刚升级至【X】粉丝团 Lv3"),
              {'fc_parse_ok': False})

        print("\n[8] 序号单调维护 + 模板缺口登记")
        rec4 = DataRecorder(ROOM_ID, LIVE_ID, base_dir=out_dir)
        for seq in (100, 105, 90):  # 90 是异常的序号回退
            f._parseFansclubMsg(fansclub_payload(
                900000 + seq, f"u{seq}", 2, f"恭喜 u{seq} 成为第{seq}名团A成员", 1, 900001))
            rec4.on_event("fansclub", events[-1][1])
        check("seq_first 锁首个", rec4.fansclub_join_seq_first, 100)
        check("seq_latest 取 max（不被 90 回退污染）", rec4.fansclub_join_seq_latest, 105)
        check("join_count 累计 3", rec4.fansclub_join_count, 3)
        # 模板未命中 → 登记缺口
        f._parseFansclubMsg(fansclub_payload(999999, "x", 2, "欢迎 x 加入粉丝团", 1, 900001))
        rec4.on_event("fansclub", events[-1][1])
        check("模板未命中登记到 unparsed", dict(rec4.fansclub_unparsed), {'2': 1})
        check("未命中不影响计数", rec4.fansclub_join_count, 4)
        rec4.close()
        rd4 = json.load(open(os.path.join(rec4.path, "room.json"), encoding='utf-8'))
        check("room.json 暴露 unparsed_by_type",
              rd4['fansclub']['unparsed_by_type'], {'2': 1})
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    print("\n" + "=" * 64)
    print(f"结果：通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        sys.exit(1)
    print("✅ 粉丝团群体级三类特征口径全部通过（合成数据）")


if __name__ == "__main__":
    main()
