#!/usr/bin/python
# coding:utf-8
"""type=6 事件**获取完整性**实测探针（游客态，与辉同行 10min）

要回答的问题：我们收到的 type=6 事件，是直播间真实发生的**全部**，还是一个**抽样**？
这决定 type=6 的条数能否当特征用（绝对计数 vs 只能当存在性/相对强弱）。

**非循环的翻转见证检验**
  每个 `User` 快照都带 `user_fans_club_status`（字段 24 → 1 → 3）。若同一用户被判为
      t1: status=1（未点亮） → t2: status=2（已点亮）
  则 (t1, t2) 之间**必然发生过一次 1→2 翻转**，因而**必然有一条对应的 type=6**。
  于是只要检查 (t1, t2) 之间有没有抓到该用户的 type=6，就能判出这次翻转是否被完整捕获。

  非循环性：见证用的快照**只取非 `WebcastFansclubMessage` 来源** —— 若 t2 本身就是那条
  type=6，则「有 type=6」是自证，不构成证据。

**快照来源（本次新增关键一路）**
  - `WebcastRoomRankMessage.ranks_list[].user` —— **批量**快照，同批用户被反复推送，
    是「同一用户多次观测」的主要来源。注意 `liveMan._parseRankMsg` **只打日志、
    不 emit 结构化数据**，所以 `data/` 语料从未记录排行榜用户 → 历史语料在此处是盲的。
  - 常规事件里的 User（enter / chat / like / social / follow），经 `data_callback`
  - 兜底：对**所有** method 的 payload 递归扫描「User 形状」的嵌套消息（严格校验，
    见 `_looks_like_user`），以最大化快照密度

用法：
  python test/probe_fansclub_completeness.py [抖音号] [时长秒]
  python test/probe_fansclub_completeness.py 56697889278 600
"""
import io
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace', line_buffering=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from liveMan import DouyinLiveWebFetcher                       # noqa: E402
from protobuf.douyin import FansclubMessage, RoomRankMessage   # noqa: E402

DEFAULT_LIVE_ID = "56697889278"     # 与辉同行
GAP_ALARM = 30                      # 场次内空档 > 此值 → 连线可疑中断
NON_FC_SOURCES = ('rank', 'enter', 'chat', 'like', 'social', 'follow', 'gift', 'other')
OUT_DIR = os.path.join(ROOT, "test", "_fc_audit")


# ---------------- 最小 protobuf wire 解码 ----------------

def _read_varint(b, i):
    r = s = 0
    while i < len(b):
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        if not (x & 0x80):
            return r, i
        s += 7
    raise IndexError('varint 越界')


def parse_fields(b):
    out = defaultdict(list)
    i = 0
    while i < len(b):
        try:
            tag, i = _read_varint(b, i)
        except IndexError:
            break
        fnum, wt = tag >> 3, tag & 7
        try:
            if wt == 0:
                v, i = _read_varint(b, i)
                out[fnum].append(('varint', v))
            elif wt == 2:
                ln, i = _read_varint(b, i)
                out[fnum].append(('bytes', b[i:i + ln]))
                i += ln
            elif wt == 5:
                out[fnum].append(('i32', b[i:i + 4])); i += 4
            elif wt == 1:
                out[fnum].append(('i64', b[i:i + 8])); i += 8
            else:
                break
        except IndexError:
            break
    return out


def _bv(fields, num):
    for wt, v in fields.get(num, []):
        if wt == 'bytes':
            return v
    return None


def _vv(fields, num):
    for wt, v in fields.get(num, []):
        if wt == 'varint':
            return v
    return None


def _text(b):
    if not b or len(b) > 64:
        return None
    try:
        s = b.decode('utf-8')
    except Exception:
        return None
    return s if s and all(c.isprintable() or c in '\n\t' for c in s) else None


# ---------------- User 形状识别（严格校验，防误报） ----------------

def _looks_like_user(b, anchor_id):
    """User: id=1(nonzero varint) / nick_name=3(可打印) / fans_club=24 → data=1
       FansClubData: level=2 / user_fans_club_status=3 / anchorId=6
       额外强约束：anchor_id ∈ {0, 本房间主播 id}（实测会员非零、非会员为 0）

    ⚠️ **本函数只认「成员」**：protobuf3 不下发默认值，非成员 `User` 的字段 24 要么
    整个缺失、要么子消息为空 → 这里直接 return None。因此**本审计的总体是「成员」**，
    未点亮占比等分母都不含非成员；非成员本来也不可能发生 1→2 翻转，不影响主检验。"""
    f = parse_fields(b)
    uid = _vv(f, 1)
    if not uid:
        return None
    nick = _text(_bv(f, 3))
    if nick is None:
        return None
    fc_b = _bv(f, 24)
    if fc_b is None:
        return None
    fc = parse_fields(fc_b)
    data_b = _bv(fc, 1)
    if data_b is None:
        return None
    d = parse_fields(data_b)
    lv = _vv(d, 2)
    st = _vv(d, 3)
    aid = _vv(d, 6)
    if lv is None or st is None or lv > 100 or st not in (0, 1, 2):
        return None
    if aid is not None and aid not in (0, anchor_id):
        return None
    return str(uid), lv, st, (aid or 0)


def scan_users(b, anchor_id, depth=0, out=None, seen=None):
    """递归扫描嵌套消息里的 User 形状对象"""
    if out is None:
        out = []
    if seen is None:
        seen = set()
    if depth > 3 or len(out) > 500:
        return out
    f = parse_fields(b)
    for num, vals in f.items():
        for wt, v in vals:
            if wt != 'bytes' or not isinstance(v, bytes) or len(v) < 4:
                continue
            u = _looks_like_user(v, anchor_id)
            if u:
                out.append(u)
                continue
            if depth < 3 and v not in seen:
                seen.add(v)
                scan_users(v, anchor_id, depth + 1, out, seen)
    return out


# ---------------- 采集 ----------------

snapshots = []          # (ts, uid, level, status, source)
type6 = []              # (ts, uid, level, status, payload_len)
for_method = Counter()  # 快照来源 method 计数
raw_events = []         # 全部消息的 (ts, method)，用于连续性检验


def main():
    live_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIVE_ID
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    os.makedirs(OUT_DIR, exist_ok=True)

    print("=" * 78)
    print(f"type=6 完整性探针（游客态）  live_id={live_id}  时长={duration}s")
    print("=" * 78)

    fetcher = DouyinLiveWebFetcher(live_id, None)
    ok, status, nickname, anchor_id = fetcher.get_room_status()
    if not ok:
        print(f"❌ 无法连接：{status}")
        return
    print(f"\n{nickname} | 主播ID={anchor_id} | room_id={fetcher.room_id} | {status}\n")

    def on_raw(method, payload, msg):
        ts = datetime.now()
        raw_events.append((ts, method))
        if method == 'WebcastFansclubMessage':
            m = FansclubMessage().parse(payload)
            uid = str(m.user.id) if m.user else None
            lv = m.user.fans_club.data.level if m.user else None
            st = m.user.fans_club.data.user_fans_club_status if m.user else None
            if m.type == 6:
                type6.append((ts, uid, lv, st, len(payload)))
                # ★ type=6 本身**不作见证**，故不进 snapshots
            else:
                # type=1/2 是独立的里程碑事件，其里的 User 可作见证
                if uid:
                    snapshots.append((ts, uid, lv, st, 'fansclub_other'))
            return

        src = method[7:-7] if method.startswith('Webcast') and method.endswith('Message') \
            else method
        if method == 'WebcastRoomRankMessage':
            src = 'rank'
            try:
                for rk in RoomRankMessage().parse(payload).ranks_list:
                    u = rk.user
                    if u and u.id:
                        d = u.fans_club.data
                        snapshots.append((ts, str(u.id), d.level,
                                          d.user_fans_club_status, 'rank'))
            except Exception:
                pass
            return
        # 兜底：递归扫描（其余 method 一律尝试），src 记真实 method 便于核对来源
        got = scan_users(payload, anchor_id)
        for uid, lv, st, _aid in got:
            snapshots.append((ts, uid, lv, st, src))
        for_method[method] += len(got)

    def on_data(event_type, data):
        uid = data.get('user_id')
        if not uid or uid == '111111':
            return
        # ★ 非循环性红线：**type=6 自身不得作见证**。若翻转对的 t2 就是那条 type=6，
        #   则「中间抓到 type=6」是自证。data_callback 对 type=6 也会回调，
        #   这里必须显式剔掉（on_raw 侧已剔，但两条通路是独立的，都要堵）。
        if data.get('fc_event_type') == 6:
            return
        src = 'fansclub_other' if event_type == 'fansclub' else event_type
        snapshots.append((datetime.now(), str(uid), data.get('fansclub_level'),
                          data.get('fansclub_status'), src))

    fetcher.raw_callback = on_raw
    fetcher.data_callback = on_data
    t = threading.Thread(target=fetcher.start, daemon=True)
    t.start()
    t0 = time.time()
    try:
        while time.time() - t0 < duration:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    fetcher.stop()
    t.join(timeout=5)

    dump_and_report()


def dump_and_report():
    with open(os.path.join(OUT_DIR, "snapshots.jsonl"), 'w', encoding='utf-8') as fh:
        for ts, uid, lv, st, src in snapshots:
            fh.write(json.dumps({'ts': ts.isoformat(timespec='milliseconds'),
                                 'user_id': uid, 'level': lv, 'status': st,
                                 'source': src}, ensure_ascii=False) + '\n')
    with open(os.path.join(OUT_DIR, "type6.jsonl"), 'w', encoding='utf-8') as fh:
        for ts, uid, lv, st, n in type6:
            fh.write(json.dumps({'ts': ts.isoformat(timespec='milliseconds'),
                                 'user_id': uid, 'level': lv, 'status': st,
                                 'payload_len': n}, ensure_ascii=False) + '\n')

    print("\n" + "=" * 78)
    print("完整性审计")
    print("=" * 78)

    # ---- 先决条件：连线连续性 ----
    raw_events.sort()
    ts_all = [t for t, _ in raw_events]
    dur = (ts_all[-1] - ts_all[0]).total_seconds() if len(ts_all) > 1 else 0
    gaps = [(ts_all[i + 1] - ts_all[i]).total_seconds() for i in range(len(ts_all) - 1)]
    worst = max(gaps) if gaps else 0
    over = sum(1 for g in gaps if g > GAP_ALARM)
    print(f"\n【先决条件】连线连续性：收到 {len(ts_all)} 条消息，跨度 {dur:.0f}s，"
          f"最大空档 {worst:.0f}s，>{GAP_ALARM}s 空档 {over} 处")
    print(f"  {'✅ 连线连续，漏采只能来自服务端抽样' if worst <= GAP_ALARM else '⚠️ 存在断流，本审计结论不可用'}")

    # ---- 快照密度 ----
    by_uid = defaultdict(list)
    for ts, uid, lv, st, src in snapshots:
        if uid and st is not None:
            by_uid[uid].append((ts, lv, st, src))
    for u in by_uid:
        by_uid[u].sort(key=lambda x: x[0])
    src_cnt = Counter(s for _, _, _, _, s in snapshots)
    multi = {u: v for u, v in by_uid.items() if len(v) >= 2}
    print(f"\n【快照密度】总快照 {len(snapshots)} 条，来源分布 {dict(src_cnt)}")
    print(f"  去重用户 {len(by_uid)}，其中**被观测 ≥2 次**（能做见证）的 {len(multi)} 个 "
          f"({len(multi) / max(len(by_uid), 1):.1%})")
    print(f"  抓到 type=6 {len(type6)} 条")

    # ---- 主检验：1→2 翻转是否都有对应的 type=6 ----
    t6_by_uid = defaultdict(list)
    for ts, uid, _lv, _st, _n in type6:
        if uid:
            t6_by_uid[uid].append(ts)
    pairs = wit = miss = 0
    miss_detail = []
    for uid, lst in by_uid.items():
        seen = [(t, s) for t, _lv, s, _src in lst]
        for i in range(1, len(seen)):
            (t1, s1), (t2, s2) = seen[i - 1], seen[i]
            if not (s1 == 1 and s2 == 2):
                continue
            pairs += 1
            if any(t1 < x < t2 for x in t6_by_uid.get(uid, [])):
                wit += 1
            else:
                miss += 1
                miss_detail.append((uid, t1, t2))
    print(f"\n【主检验】见证到的 1→2 翻转对: {pairs}")
    if pairs:
        print(f"  抓到 type=6: {wit}  |  **漏采**: {miss}")
        print(f"  → **捕获率 ≈ {wit / pairs:.1%}**")
        for uid, t1, t2 in sorted(miss_detail, key=lambda x: (x[2] - x[1]))[:10]:
            print(f"     漏采 {uid:<18} {t1.strftime('%H:%M:%S')} → "
                  f"{t2.strftime('%H:%M:%S')}  跨 {(t2 - t1).total_seconds():.0f}s")
    else:
        print("  样本为 0 —— 本轮没有任何用户被见证到 1→2 翻转，无法估计捕获率")
        print("  （需要同一用户在翻转前后各被独立观测一次；10min 内可能凑不到）")

    # ---- 反向：抓到的 type=6 有多少能被独立见证 ----
    conf = sum(1 for ts, uid, _l, _s, _n in type6
               if uid and any(x[0] < ts and x[2] == 1 for x in by_uid.get(uid, [])))
    print(f"\n【反向】抓到的 {len(type6)} 条 type=6 中，有前序 status=1 独立观测的: {conf}")
    print(f"  → 其余 {len(type6) - conf} 条的点亮者在本轮**从未被独立观测到**"
          f"（房间消息量下我们只看到极小一部分人）")

    print(f"\n  原始快照已存至 {OUT_DIR}/snapshots.jsonl，type=6 存至 type6.jsonl，可离线复核")


if __name__ == '__main__':
    main()
