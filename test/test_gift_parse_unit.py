#!/usr/bin/python
# coding:utf-8
"""礼物解析口径单元验证（合成 GiftMessage，不依赖服务端下发）

背景：2026-09-28 定案 —— `WebcastGiftMessage` 不由 Web IM 推送通道承载（抖音官方网页
自身的 WS 亦收不到），礼物事件恒为 0。因此**礼物聚合特征已从特征口径中移除**
（`room.json` 不再输出 gift_total_count / gift_total_diamond / gift_user_count）。
`liveMan._parseGiftMsg` 解析器保留，通道若恢复可直接启用。

本脚本用合成 protobuf 验证当前口径：

  1. `_parseGiftMsg` 字段映射正确（gift_id/gift_name/gift_type/diamond_value/
     gift_count=combo_count/repeat_count/total_count/repeat_end/group_id）
  2. gift 事件正常写入 events.jsonl（保留原始事件流，便于将来重算）
  3. `room.json` **不再包含**任何礼物聚合字段（防止恒 0 假特征回流）

注：连击收尾的聚合规则（`repeat_end==1 && group_id>0` 跳过、单发 `group_id==0` 照常计入）
现记录在 `data_recorder.on_event` 的 gift 分支注释中，供将来恢复聚合时参照。

用法：
  python test/test_gift_parse_unit.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher
from data_recorder import DataRecorder
from protobuf.douyin import GiftMessage, GiftStruct, User

ROOM_ID = "unit-room"
LIVE_ID = "unit-anchor"
PASS, FAIL = [], []


def check(label, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(label)
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")


def make_payload(gift_id, name, diamond, combo, repeat, total,
                 repeat_end, group_id, user_id, nickname, gift_type=1):
    msg = GiftMessage(
        combo_count=combo,
        repeat_count=repeat,
        total_count=total,
        repeat_end=repeat_end,
        group_id=group_id,
        user=User(id=user_id, nick_name=nickname),
        gift=GiftStruct(id=gift_id, name=name, diamond_count=diamond, type=gift_type),
    )
    return bytes(msg)


def main():
    events = []
    f = DouyinLiveWebFetcher(LIVE_ID, None)
    f.data_callback = lambda event_type, data: events.append((event_type, data))

    out_dir = tempfile.mkdtemp(prefix="gift_unit_")
    try:
        rec = DataRecorder(ROOM_ID, LIVE_ID, base_dir=out_dir)

        # ---- 用例 1：连击串（小心心，单价 1 抖币）----
        # 连击过程 3 条增量 + 1 条收尾（repeat_end=1, group_id=987654321）
        print("\n[用例 1] 连击串：3 条增量 + 1 条收尾（group_id>0）")
        combo_seq = [
            # (combo, repeat, total, repeat_end)
            (1, 1, 1, 0),
            (1, 2, 2, 0),
            (1, 3, 3, 0),
            (0, 3, 3, 1),   # 收尾：combo_count 归 0，仅作连击结束信号
        ]
        for i, (combo, repeat, total, end) in enumerate(combo_seq):
            payload = make_payload(1001, "小心心", 1, combo, repeat, total,
                                   end, 987654321, 555000111, "连击用户")
            f._parseGiftMsg(payload)
            rec.on_event("gift", events[-1][1])

        gift_events = [d for t, d in events if t == 'gift']
        check("连击串事件数", len(gift_events), 4)
        # 字段映射（以第 2 条为例）
        e = gift_events[1]
        check("gift_id", e['gift_id'], "1001")
        check("gift_name", e['gift_name'], "小心心")
        check("gift_type", e['gift_type'], 1)
        check("diamond_value(单价)", e['diamond_value'], 1)
        check("gift_count=combo_count", e['gift_count'], 1)
        check("repeat_count(连击累计)", e['repeat_count'], 2)
        check("total_count(房内累计)", e['total_count'], 2)
        check("group_id", e['group_id'], 987654321)
        check("repeat_end(收尾标记)", gift_events[3]['repeat_end'], 1)
        check("user_id", e['user_id'], "555000111")
        check("nickname", e['nickname'], "连击用户")
        # 聚合字段已移除：DataRecorder 上不应再存在这些属性
        for attr in ('gift_total_count', 'gift_total_diamond', 'gift_user_ids'):
            check(f"聚合属性已移除: {attr}", hasattr(rec, attr), False)

        # ---- 用例 2：连击收尾 + 单发礼物仍应逐条写入 events.jsonl ----
        print("\n[用例 2] 收尾消息与单发礼物都应写入 events.jsonl（原始流不丢）")
        payload = make_payload(2002, "玫瑰", 10, 5, 1, 5, 1, 0, 555000222, "单发用户")
        f._parseGiftMsg(payload)
        rec.on_event("gift", events[-1][1])
        payload = make_payload(1001, "小心心", 1, 3, 3, 3, 1, 987654321, 555000111, "连击用户")
        f._parseGiftMsg(payload)
        rec.on_event("gift", events[-1][1])
        check("gift 事件累计 6 条", len([1 for t, _ in events if t == 'gift']), 6)

        # ---- 用例 3：落盘正确性 + 确认礼物聚合字段已从 room.json 消失 ----
        print("\n[用例 3] events.jsonl / room.json 落盘")
        rec.close()
        ep = os.path.join(rec.path, "events.jsonl")
        with open(ep, encoding='utf-8') as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        check("events.jsonl 行数(4+1+1)", len(lines), 6)
        check("落盘 gift 事件带 ts", all('ts' in l for l in lines), True)
        check("落盘均为 gift 类型", all(l.get('type') == 'gift' for l in lines), True)
        with open(os.path.join(rec.path, "room.json"), encoding='utf-8') as fh:
            rd = json.load(fh)
        for f_ in ('gift_total_count', 'gift_total_diamond', 'gift_user_count'):
            check(f"room.json 已移除 {f_}", f_ in rd, False)
        # 其余特征字段应仍在
        check("room.json 保留 like_total", 'like_total' in rd, True)
        check("room.json 保留 follow_count", 'follow_count' in rd, True)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    print("\n" + "=" * 64)
    print(f"结果：通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        sys.exit(1)
    print("✅ 礼物解析口径通过 + 聚合字段已彻底移除（合成数据）")


if __name__ == "__main__":
    main()
