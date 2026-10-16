#!/usr/bin/python
# coding:utf-8
"""FansclubMessage type=6 语义标定探针（v3：纯游客态，不依赖登录）

**已定标结论（2026-09-29，本探针 + 历史数据交叉验证）：**
  type=6 = **粉丝团「灯牌点亮」的静默状态同步消息**
  - 静默：顶层**不含 f3**（content 字段本身不下发）、Common 无 f8、f6 仅 4B
    （type=1/2 的 f6 为 210B 且含 f25 公告富载荷）
  - 它标记 `user_fans_club_status` 由 **1 → 2** 的翻转时刻（有前序观测的样本均如此，
    等级全程不变）
  - `user_fans_club_status`：0=非成员（level=0 时恒为 0），1=成员·灯牌未点亮(4%)，
    2=成员·灯牌已点亮(96%)
  - 非用户场内互动触发（±30s 内无该用户弹幕/点赞/进场），是服务端按观看时长等
    因素自主触发的

已排除的假设：
  - ✗ 升级：跨会话 0 次伴随等级上升（type=1 唯一一条就是上升）
  - ✗ 跟随互动触发：无邻近行为

**v3 关键改动：去掉登录态依赖。** 生产链路（`collect_full.py` / `auto_crawl.py`）本来就用
`DouyinLiveWebFetcher(live_id, None)` 的**游客态**（仅 ttwid），`data/` 全量语料也是游客态采到的
—— 所以标定没有理由需要登录。v2 曾借 `D:/CodeFile/aicode/douyin/config/auth.json` 只是为了
「原始 wire dump 能不能看到更多字段」；v3 验证游客态下 wire 结构完全一致（见 `modify.md`）。

做法：
  1. **游客态**连 WS（不读任何 auth 文件）
  2. `raw_callback` 手写 protobuf wire 解码，逐层剥到 User(4) → FansClub(24) → data(1)
  3. **原始 payload 落盘** `test/_fc_raw/type{N}_{ts}.bin`，供离线复现 diff
  4. `data_callback` 记录逐用户观测时间线（level/status/行为），**在本轮内**实时检验三条判据：
     ① status 翻转 ② 等级不变 ③ ±30s 无邻近行为

用法：
  python test/probe_fansclub_type6.py [抖音号] [时长秒]
  python test/probe_fansclub_type6.py 56697889278 600
"""
import io
import os
import shutil
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
from protobuf.douyin import FansclubMessage                    # noqa: E402

RAW_DIR = os.path.join(ROOT, "test", "_fc_raw")
DEFAULT_LIVE_ID = "56697889278"   # 与辉同行
NEIGHBOR_WINDOW = 30              # ±30s 邻近行为窗口（与历史数据检验口径一致）
BEHAVIOR_KINDS = {'chat', 'like', 'enter', 'social', 'follow'}


# ---------------- 最小 protobuf wire 解码（不依赖 proto 声明） ----------------

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
    """解成 {字段号: [值, ...]}；varint→int，bytes→bytes（调用方决定是否递归）"""
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
                out[fnum].append(('i32', b[i:i + 4]))
                i += 4
            elif wt == 1:
                out[fnum].append(('i64', b[i:i + 8]))
                i += 8
            else:
                break
        except IndexError:
            break
    return out


def first_bytes(fields, num):
    for wt, v in fields.get(num, []):
        if wt == 'bytes':
            return v
    return None


def first_varint(fields, num):
    for wt, v in fields.get(num, []):
        if wt == 'varint':
            return v
    return None


def decode_text(b):
    try:
        s = b.decode('utf-8')
        return s if s and all(c.isprintable() or c in '\n\t' for c in s) else None
    except Exception:
        return None


# ---------------- 采集状态 ----------------

seen = []                                  # 每条 FansclubMessage 的 wire 记录
timeline = defaultdict(list)               # uid → [(ts, kind, level, status), ...]
name_of = {}                               # uid → 昵称


def main():
    live_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIVE_ID
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 600

    # 清掉上一轮（尤其是 v2 登录态跑的）残留，避免与本次混检
    if os.path.isdir(RAW_DIR):
        shutil.rmtree(RAW_DIR)
    os.makedirs(RAW_DIR, exist_ok=True)

    print("=" * 78)
    print(f"FansclubMessage type 差异分析探针 v3（游客态）  live_id={live_id}  时长={duration}s")
    print(f"原始 payload 落盘: {RAW_DIR}")
    print("=" * 78)

    fetcher = DouyinLiveWebFetcher(live_id, None)
    ok, status, nickname, anchor_id = fetcher.get_room_status()
    if not ok:
        print(f"❌ 无法连接：{status}")
        return
    print(f"\n{nickname} | 主播ID={anchor_id} | room_id={fetcher.room_id} | {status}\n")
    # 不设置 fetcher.cookie_string —— 保持 None 即游客态（仅 ttwid）

    # ---- 钩子 1：原始 wire（只关心 FansclubMessage）----
    def on_raw(method, payload, msg):
        if method != 'WebcastFansclubMessage':
            return
        ts = datetime.now()
        top = parse_fields(payload)
        m = FansclubMessage().parse(payload)
        ptype = m.type
        uid = str(m.user.id) if m.user else None
        nick = m.user.nick_name if m.user else None

        # 逐层剥：顶层(4)=User → User(24)=FansClub → FansClub(1)=data
        user_b = first_bytes(top, 4)
        uf = parse_fields(user_b) if user_b else {}
        fc_b = first_bytes(uf, 24)
        fcf = parse_fields(fc_b) if fc_b else {}
        data_b = first_bytes(fcf, 1)
        df = parse_fields(data_b) if data_b else {}

        # FansClubData 字段号（见 protobuf/douyin.proto）：
        #   clubName=1  level=2  userFansClubStatus=3  badge=4  availableGiftIds=5  anchorId=6
        rec = {
            'ts': ts, 'type': ptype, 'user_id': uid, 'nickname': nick,
            'payload_len': len(payload),
            'top_fields': sorted(top.keys()),
            'user_fields': sorted(uf.keys()),
            'fc_fields': sorted(fcf.keys()),
            'data_fields': sorted(df.keys()),
            'has_f3_content': 3 in top,
            'content': m.content,
            'club_name': (decode_text(first_bytes(df, 1)) or ''),
            'level': first_varint(df, 2),
            'status': first_varint(df, 3),
            'anchor_id': first_varint(df, 6),
        }
        seen.append(rec)
        if uid:
            timeline[uid].append((ts, 'fansclub', rec['level'], rec['status']))
            name_of[uid] = nick

        fn = os.path.join(RAW_DIR, f"type{ptype}_{ts.strftime('%H%M%S_%f')}.bin")
        with open(fn, 'wb') as fh:
            fh.write(payload)

        print(f"[{ts.strftime('%H:%M:%S')}] type={ptype} {len(payload)}B "
              f"user={nick}({uid}) level={rec['level']} status={rec['status']} "
              f"club={rec['club_name']!r} content={rec['content']!r}")
        print(f"      顶层字段={rec['top_fields']}  User字段={rec['user_fields']}")
        print(f"      FansClub(24)字段={rec['fc_fields']}  data(1)字段={rec['data_fields']}")

    # ---- 钩子 2：结构化事件（离线的邻近行为/翻转判据在游客态下同样可算）----
    def on_data(event_type, data):
        uid = data.get('user_id')
        if not uid or uid == '111111':
            return
        timeline[str(uid)].append((datetime.now(), event_type,
                                   data.get('fansclub_level'),
                                   data.get('fansclub_status')))
        if data.get('nickname'):
            name_of.setdefault(str(uid), data['nickname'])

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

    report()


def report():
    print("\n" + "=" * 78)
    print("差异汇总")
    print("=" * 78)
    by_type = Counter(r['type'] for r in seen)
    print(f"  共捕获 {len(seen)} 条 | 按 type: "
          f"{dict(sorted(by_type.items(), key=lambda x: str(x[0])))}")
    print(f"  观测到的用户数: {len(timeline)}（含非 fansclub 事件）")

    for tp in sorted(by_type, key=lambda x: str(x)):
        rs = [r for r in seen if r['type'] == tp]
        print(f"\n  ── type={tp}（{len(rs)} 条）──")
        print(f"    payload 长度: {min(r['payload_len'] for r in rs)}"
              f"~{max(r['payload_len'] for r in rs)}B")
        print(f"    含 f3(content): {sum(r['has_f3_content'] for r in rs)}/{len(rs)}")
        for label, key in [('顶层', 'top_fields'), ('User(4)', 'user_fields'),
                           ('FansClub(24)', 'fc_fields'), ('data(1)', 'data_fields')]:
            always, sometimes = set(), set()
            for r in rs:
                s = set(r[key])
                always = s if not always else (always & s)
                sometimes |= s
            print(f"    {label} 字段: 恒有={sorted(always)}  曾出现={sorted(sometimes)}")
        lv = [r['level'] for r in rs if r['level'] is not None]
        st = Counter(r['status'] for r in rs)
        if lv:
            print(f"    level: {min(lv)}~{max(lv)} 均值 {sum(lv) / len(lv):.2f}")
        print(f"    status 分布: {dict(st)}")

    # ---------------- 判据 ★：type=6 相对 type=1/2 的型别特异字段 ----------------
    def always_set(tp, key):
        rs = [r for r in seen if r['type'] == tp]
        if not rs:
            return None
        s = set(rs[0][key])
        for r in rs[1:]:
            s &= set(r[key])
        return s

    t6 = always_set(6, 'top_fields')
    if t6 is not None:
        base = set()
        for other in (1, 2):
            o = always_set(other, 'top_fields')
            if o is not None:
                base |= o
        print(f"\n  ★ type=6 顶层恒有字段: {sorted(t6)}")
        print(f"  ★ type=1/2 顶层恒有字段: {sorted(base) if base else '（本轮未捕获）'}")
        if base:
            only6 = t6 - base
            only_base = base - t6
            print(f"  ★ 仅 type=6 有: {sorted(only6) or '无'}")
            print(f"  ★ 仅 type=1/2 有: {sorted(only_base) or '无'}"
                  f"{'  ← f3 缺失即 content 字段本身不下发' if 3 in only_base else ''}")
            print(f"  ★ 结论: "
                  f"{'type=6 顶层结构与 1/2 相同（仅 f3 缺席）' if not only6 else 'type=6 另有专属字段，见上'}")

    # ---------------- 判据 ★②：status 1→2 翻转 + 判据 ★③：±30s 无邻近行为 ----------------
    t6_events = [r for r in seen if r['type'] == 6 and r['user_id']]
    if not t6_events:
        print("\n  （本轮未捕获 type=6，跳过错位/邻近判据）")
        print(f"\n  原始 payload 已存至 {RAW_DIR}（{len(seen)} 个 .bin）")
        return

    flip_ok = flip_other = flip_none = 0
    lv_same = lv_changed = 0
    flip_detail, prior_missing = [], 0
    near_hits = Counter()

    for ev in t6_events:
        uid, t = ev['user_id'], ev['ts']
        obs = timeline.get(uid, [])
        # 最近一次「严格早于本事件」的观测
        prev = [o for o in obs if o[0] < t]
        if prev:
            p = prev[-1]
            if p[3] is not None and ev['status'] is not None:
                if p[3] == 1 and ev['status'] == 2:
                    flip_ok += 1
                    if p[2] is not None and ev['level'] is not None:
                        (lv_same if p[2] == ev['level'] else lv_changed)
                        lv_same += (p[2] == ev['level'])
                        lv_changed += (p[2] != ev['level'])
                    flip_detail.append(
                        (uid, name_of.get(uid, ''), p[0].strftime('%H:%M:%S'), p[3], p[2],
                         t.strftime('%H:%M:%S'), ev['status'], ev['level']))
                elif p[3] != ev['status']:
                    flip_other += 1
                else:
                    flip_none += 1
        else:
            prior_missing += 1
        # ±30s 邻近行为：**只数行为类**（BEHAVIOR_KINDS）。
        # 不能用「ts 相同就跳过」来排除这条 fansclub 自身——on_raw 与 on_data 各自
        # 调 datetime.now()，时间戳差几微秒永远不相等，会把自身算成邻近行为
        # （v3 首跑就踩了这个坑，报出假的「有邻近行为」）。
        near = {k: 0 for k in BEHAVIOR_KINDS}
        for o in obs:
            if o[1] not in BEHAVIOR_KINDS:
                continue
            if abs((o[0] - t).total_seconds()) <= NEIGHBOR_WINDOW:
                near[o[1]] = near.get(o[1], 0) + 1
        total_near = sum(near.values())
        near_hits['有邻近行为' if total_near else '无邻近行为'] += 1
        if total_near:
            near_hits.update({k: v for k, v in near.items() if v})

    print(f"\n  ★ 判据② status 翻转（对 {len(t6_events)} 条 type=6，"
          f"排除无前序观测的 {prior_missing} 条）:")
    print(f"      1→2 翻转: {flip_ok} 条 | 其它变化: {flip_other} 条 | status 未变: {flip_none} 条")
    if flip_detail:
        print("      翻转明细（uid / 前序 / 本事件）:")
        for uid, nick, t0, s0, l0, t1, s1, l1 in flip_detail:
            print(f"        {uid:<18}{nick:<12} {t0} status={s0} lv={l0}"
                  f"  →  {t1} status={s1} lv={l1}")
        print(f"      其中等级不变 {lv_same} 条 / 等级变化 {lv_changed} 条")

    print(f"\n  ★ 判据③ ±{NEIGHBOR_WINDOW}s 邻近行为（该用户自身）:")
    print(f"      {dict(near_hits)}")
    print("      注：无邻近行为 ⇒ 不是用户在场内互动时触发的")

    print(f"\n  原始 payload 已存至 {RAW_DIR}（{len(seen)} 个 .bin），可离线复现此 diff")


if __name__ == '__main__':
    main()
