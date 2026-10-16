#!/usr/bin/python
# coding:utf-8
"""type=6 口径的**离线语料复核**（不连网、不需登录态）

`data/` 全量语料是游客态采的（`collect_full.py` / `auto_crawl.py` 都构造
`DouyinLiveWebFetcher(live_id, None)`），所以「type=6 是不是灯牌点亮」这件事
**可以在已有语料上复核，完全不需要登录**。本脚本重跑三条判据：

  ① content 静默      —— type=6 的 `content` 是否恒为空（wire 层面的 f3 缺失，
                         在语料里表现为 content == ""）
  ② status 1→2 翻转   —— 每条 type=6 之前，该用户最近一次观测的 status 是什么；
                         翻转是否恰好落在 type=6 上，且**等级不变**
  ③ ±30s 无邻近行为   —— type=6 时刻前后 30s 内该用户有无 弹幕/点赞/进场/关注/分享

另附：语料 schema 覆盖清单（哪些场次根本没有粉丝团字段 → 它们的 0 是缺口不是观测）

用法：
  python test/verify_fansclub_type6_corpus.py
  python test/verify_fansclub_type6_corpus.py --dirs data/live_56697889278_2026092[89]*
"""
import glob
import io
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEIGHBOR_WINDOW = 30
BEHAVIOR_KINDS = {'chat', 'like', 'enter', 'social', 'follow'}


def load_dir(d):
    """读一个采集目录 → (obs_by_uid, fc_events, n_lines)；无 schema 则返回 None"""
    ev_p, dm_p = os.path.join(d, "events.jsonl"), os.path.join(d, "danmaku.jsonl")
    obs, fcs, n = defaultdict(list), [], 0
    has_schema = False
    for path, kind_default in ((ev_p, None), (dm_p, 'chat')):
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                n += 1
                ts = r.get('ts')
                if not ts:
                    continue
                if 'fansclub_level' in r:
                    has_schema = True
                try:
                    t = datetime.fromisoformat(ts)
                except Exception:
                    continue
                uid = str(r.get('user_id') or '')
                if uid and uid != '111111':
                    obs[uid].append((t, r.get('type') or kind_default,
                                     r.get('fansclub_level'), r.get('fansclub_status'),
                                     os.path.basename(d), r.get('nickname')))
                if r.get('type') == 'fansclub':
                    fcs.append({**r, '_t': t, '_dir': os.path.basename(d)})
    if not has_schema:
        return None
    return obs, fcs, n


def main():
    pats = [a for a in sys.argv[1:] if not a.startswith('--')] or ["data/live_*"]
    dirs = sorted({d for p in pats for d in glob.glob(os.path.join(ROOT, p))})

    obs_all, fcs_all, schema_dirs, gap_dirs, total_lines = \
        defaultdict(list), [], [], [], 0
    for d in dirs:
        got = load_dir(d)
        if got is None:
            gap_dirs.append(os.path.basename(d))
            continue
        obs, fcs, n = got
        schema_dirs.append(os.path.basename(d))
        total_lines += n
        for uid, lst in obs.items():
            obs_all[uid].extend(lst)
        fcs_all.extend(fcs)

    print("=" * 78)
    print("type=6 口径离线复核（游客态语料，无登录）")
    print("=" * 78)
    print(f"  扫描目录 {len(dirs)} 个 → 含粉丝团 schema 的 {len(schema_dirs)} 个，"
          f"**schema 缺口（早于接入）{len(gap_dirs)} 个**")
    print(f"  观测总行数 {total_lines}，去重用户 {len(obs_all)}，"
          f"fansclub 事件 {len(fcs_all)}")
    if gap_dirs:
        print(f"  ⚠️ 缺口目录（其 0 值不是观测，勿纳入统计）: "
              f"{', '.join(sorted(gap_dirs)[:6])}{' …' if len(gap_dirs) > 6 else ''}")
    if not fcs_all:
        print("  无 fansclub 事件，退出")
        return

    # ---------------- 判据 ①：content 静默 ----------------
    print("\n" + "-" * 78)
    print("① content 静默 —— type=6 是否恒无明文公告")
    print("-" * 78)
    by_t = defaultdict(list)
    for r in fcs_all:
        by_t[r.get('fc_event_type')].append(r)
    for tp in sorted(by_t, key=lambda x: str(x)):
        rs = by_t[tp]
        empty = sum(1 for r in rs if not (r.get('content') or '').strip())
        lv = [r['fansclub_level'] for r in rs if r.get('fansclub_level') is not None]
        st = Counter(r.get('fansclub_status') for r in rs)
        lvs = f"{min(lv)}~{max(lv)}" if lv else '—'
        print(f"  type={tp}: {len(rs):>3} 条 | content 为空 {empty}/{len(rs)}"
              f" | level {lvs} | status 分布 {dict(st)}")
    for tp in sorted(by_t, key=lambda x: str(x)):
        rs = by_t[tp]
        sample = next((r['content'] for r in rs if (r.get('content') or '').strip()), None)
        if sample:
            print(f"  type={tp} 公告样例: {sample!r}")
    t6e = sum(1 for r in by_t.get(6, []) if not (r.get('content') or '').strip())
    print(f"  ★ 判据①: type=6 的 content 空值率 = {t6e}/{len(by_t.get(6, []))}")

    # ---------------- 判据 ②：status 1→2 翻转 ----------------
    print("\n" + "-" * 78)
    print("② status 翻转 —— 翻转是否落在 type=6 上，且等级不变")
    print("-" * 78)
    for uid in obs_all:
        obs_all[uid].sort(key=lambda o: o[0])

    multi = {u: v for u, v in obs_all.items() if len(v) >= 2}
    flippers = {}
    for uid, lst in multi.items():
        seq, prev = [], None
        for o in lst:
            if o[3] is None:
                continue
            if prev is not None and o[3] != prev:
                seq.append(o)
            prev = o[3]
        if seq:
            flippers[uid] = seq
    print(f"  多次观测的用户/成员: {len(multi)} 个；其中 status 发生过翻转的: "
          f"{len(flippers)} 个")

    hit_on_t6 = Counter()
    detail = []
    for ev in fcs_all:
        if ev.get('fc_event_type') != 6:
            continue
        uid = str(ev.get('user_id') or '')
        if not uid:
            continue
        prior = [o for o in obs_all.get(uid, []) if o[0] < ev['_t'] and o[3] is not None]
        if not prior:
            continue
        p = prior[-1]
        cur_s, cur_l = ev.get('fansclub_status'), ev.get('fansclub_level')
        if p[3] == cur_s:
            hit_on_t6['status 未变'] += 1
        elif p[3] == 1 and cur_s == 2:
            hit_on_t6['1→2 翻转'] += 1
            hit_on_t6['  └ 等级不变' if p[2] == cur_l else '  └ 等级变化'] += 1
            detail.append((uid, ev.get('nickname') or (p[5] or ''), p, ev))
        else:
            hit_on_t6[f'{p[3]}→{cur_s} 其它'] += 1
    print(f"  有前序观测的 type=6 事件判据结果: {dict(hit_on_t6)}")
    for uid, nick, p, ev in detail[:10]:
        print(f"    {uid:<18}{nick:<12} {p[0].strftime('%H:%M:%S')} status={p[3]} lv={p[2]}"
              f"  →  {ev['_t'].strftime('%H:%M:%S')} status={ev.get('fansclub_status')}"
              f" lv={ev.get('fansclub_level')}   [{ev['_dir']}]")

    # ---------------- 判据 ③：±30s 无邻近行为 ----------------
    print("\n" + "-" * 78)
    print(f"③ ±{NEIGHBOR_WINDOW}s 邻近行为 —— type=6 是否伴随该用户场内行为")
    print("-" * 78)
    win = timedelta(seconds=NEIGHBOR_WINDOW)
    near_cnt, kinds, no_prior = Counter(), Counter(), 0
    for ev in fcs_all:
        if ev.get('fc_event_type') != 6:
            continue
        uid = str(ev.get('user_id') or '')
        obs = obs_all.get(uid, [])
        if not obs:
            no_prior += 1
        t, n = ev['_t'], 0
        for o in obs:
            # 只数行为类，天然排除这条 fansclub 自身（不靠时间戳相等——那样很脆）
            if o[1] not in BEHAVIOR_KINDS:
                continue
            if abs(o[0] - t) <= win:
                n += 1
                kinds[o[1]] += 1
        near_cnt['有邻近行为' if n else '无邻近行为'] += 1
    print(f"  全部 type=6：{dict(near_cnt)}（语料中无任何观测的用户另计 {no_prior} 条）")
    print(f"  邻近行为类型分布: {dict(kinds)}")
    tot = sum(near_cnt.values())
    if tot:
        print(f"  ★ 判据③: 无邻近行为占比 = {near_cnt['无邻近行为']}/{tot} "
              f"= {near_cnt['无邻近行为'] / tot:.1%}")

    # ---------------- 附：status 总体分布 ----------------
    print("\n" + "-" * 78)
    print("附：user_fans_club_status 总体分布与 level 交叉")
    print("-" * 78)
    cross = defaultdict(Counter)
    for uid, lst in obs_all.items():
        for o in lst:
            if o[2] is None or o[3] is None:
                continue
            cross['level=0' if o[2] == 0 else 'level>0'][o[3]] += 1
    for k, c in cross.items():
        tot_k = sum(c.values())
        print(f"  {k}: {dict(c)}   （合计 {tot_k}）")
    print()


if __name__ == '__main__':
    main()
