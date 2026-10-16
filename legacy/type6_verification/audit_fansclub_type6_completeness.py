#!/usr/bin/python
# coding:utf-8
"""type=6 事件获取**完整性**审计（离线，不连网）

要回答的问题：我们收到的 type=6 事件，是「直播间真实发生的全部」还是「一个抽样」？
这决定了**能否把 type=6 的条数当作特征**（绝对计数 vs 只能当存在性/相对信号）。

方法（不依赖任何外部真值）：

  **非循环的翻转见证检验。** 每个用户在任何消息里都带 `user_fans_club_status` 快照。
  若我们在同一场次内看到某用户
      t1 时 status=1（未点亮） → t2 时 status=2（已点亮）
  则**在这两点之间必然发生过一次 1→2 翻转**，因此**必然有一条对应的 type=6**。
  于是检查 (t1, t2) 之间是否真的抓到过该用户的 type=6：

  - 抓到  → 该次翻转被完整捕获
  - 没抓到 → **漏采**（因为我们已知本场次 WS 连线没有断：先决条件检验会打印空档）

  关键的非循环性：**只采用「非 fansclub 消息」构成的翻转对**（enter/chat/like/social）。
  若 t2 本身就是那条 type=6，则「有 type=6」是自证，不构成证据，必须排除。

先决条件（脚本会自己验证）：
  1. 同一场次内消息流连续（最大空档远小于翻转对的时间跨度）→ 漏采只能来自服务端抽样
  2. 翻转对的两点必须落在**同一场次目录**内（跨场次时缺口里可能发生翻转，无法归因）

用法：
  python test/audit_fansclub_type6_completeness.py
  python test/audit_fansclub_type6_completeness.py --max-span 1800
"""
import glob
import io
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAP_ALARM = 30          # 场次内空档超过此值 → 警告连线可能中断
NON_FC_KINDS = {'enter', 'chat', 'like', 'social', 'follow'}


def load_dir(d):
    """→ (obs_by_uid, fc_events, ts_all) 或 None（无 schema）"""
    obs, fcs, ts_all, has_schema = defaultdict(list), [], [], False
    for fn, kd in (("events.jsonl", None), ("danmaku.jsonl", "chat")):
        p = os.path.join(d, fn)
        if not os.path.exists(p):
            continue
        with open(p, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if 'fansclub_level' in r:
                    has_schema = True
                ts = r.get('ts')
                if not ts:
                    continue
                try:
                    t = datetime.fromisoformat(ts)
                except Exception:
                    continue
                ts_all.append(t)
                uid = str(r.get('user_id') or '')
                kind = r.get('type') or kd
                if uid and uid != '111111':
                    obs[uid].append((t, kind, r.get('fansclub_level'),
                                     r.get('fansclub_status')))
                if r.get('type') == 'fansclub':
                    fcs.append({**r, '_t': t})
    if not has_schema:
        return None
    for u in obs:
        obs[u].sort(key=lambda o: o[0])
    return obs, fcs, ts_all


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith('--')]
    max_span = 1800.0
    for a in sys.argv[1:]:
        if a.startswith('--max-span'):
            max_span = float(a.split('=')[-1] if '=' in a else
                             sys.argv[sys.argv.index(a) + 1])
    dirs = sorted({d for p in (argv or ["data/live_*"])
                   for d in glob.glob(os.path.join(ROOT, p))})

    print("=" * 78)
    print("type=6 获取完整性审计")
    print("=" * 78)

    # ---------------- 先决条件 1：场次内连线连续性 ----------------
    print("\n【先决条件 1】场次内消息流空档（>%ds 即视为连线可疑中断）" % GAP_ALARM)
    verdict_cont = []
    for d in dirs:
        got = load_dir(d)
        if got is None:
            continue
        _, _, ts_all = got
        if len(ts_all) < 2:
            verdict_cont.append((os.path.basename(d), 0))
            continue
        ts_all.sort()
        gaps = [max((ts_all[i + 1] - ts_all[i]).total_seconds(), 0)
                for i in range(len(ts_all) - 1)]
        worst = max(gaps) if gaps else 0
        over = sum(1 for g in gaps if g > GAP_ALARM)
        verdict_cont.append((os.path.basename(d), worst))
        mark = '✅' if worst <= GAP_ALARM else '⚠️'
        print(f"  {mark} {os.path.basename(d):<34} 跨度"
              f"{(ts_all[-1] - ts_all[0]).total_seconds():>6.0f}s  最大空档{worst:>5.0f}s"
              f"  >{GAP_ALARM}s 空档 {over} 处")
    usable = [n for n, w in verdict_cont if w <= GAP_ALARM]
    print(f"  → 连续可用的场次 {len(usable)}/{len(verdict_cont)}")

    # ---------------- 主检验：非循环的翻转见证 ----------------
    print("\n" + "-" * 78)
    print("【主检验】同一场次内、由**非 fansclub 消息**见证的 1→2 翻转，"
          "是否都抓到了 type=6")
    print("-" * 78)
    tot_pairs = witnessed = missed = 0
    span_of_witnessed, span_of_missed = [], []
    miss_detail = []
    for d in dirs:
        got = load_dir(d)
        if got is None:
            continue
        base = os.path.basename(d)
        if base not in usable:
            continue
        obs, fcs, _ = got
        t6_by_uid = defaultdict(list)
        for e in fcs:
            if e.get('fc_event_type') == 6:
                t6_by_uid[str(e.get('user_id'))].append(e['_t'])
        for uid, lst in obs.items():
            # 只保留 status 已知、且**非 fansclub**（避免自证）的观测
            seen = [(t, s) for t, k, _lv, s in lst
                    if s is not None and k in NON_FC_KINDS and k != 'fansclub']
            for i in range(1, len(seen)):
                (t1, s1), (t2, s2) = seen[i - 1], seen[i]
                if not (s1 == 1 and s2 == 2):
                    continue
                span = (t2 - t1).total_seconds()
                if span <= 0 or span > max_span:
                    continue
                tot_pairs += 1
                hit = any(t1 < x < t2 for x in t6_by_uid.get(uid, []))
                if hit:
                    witnessed += 1
                    span_of_witnessed.append(span)
                else:
                    missed += 1
                    span_of_missed.append(span)
                    miss_detail.append((uid, t1, t2, span, base))

    print(f"  见证到的 1→2 翻转对（非 fansclub、span ≤ {max_span:.0f}s）: {tot_pairs}")
    if tot_pairs:
        print(f"    其中抓到 type=6: {witnessed}  |  **漏采**: {missed}")
        print(f"    → type=6 捕获率 ≈ {witnessed / tot_pairs:.1%}")
    else:
        print("    样本为 0，无法估计（语料中同一用户的多次观测太稀疏）")

    if miss_detail:
        print(f"\n  漏采明细（前 15 条）:")
        for uid, t1, t2, span, base in sorted(miss_detail, key=lambda x: x[3])[:15]:
            print(f"    {uid:<18} {t1.strftime('%m-%d %H:%M:%S')} status=1  →  "
                  f"{t2.strftime('%H:%M:%S')} status=2   跨{span:>5.0f}s  [{base[5:]}]")
        sp = sorted(span_of_missed)
        print(f"    漏采对的 span: 中位数 {sp[len(sp) // 2]:.0f}s  "
              f"最小 {sp[0]:.0f}s  最大 {sp[-1]:.0f}s")
        print("    （span 越小，『中间必然发生过翻转』越确定；span 几分钟的仍成立，"
              "只是翻转时刻不精确）")

    # ---------------- 观测稀疏度：为什么样本这么少 ----------------
    print("\n" + "-" * 78)
    print("【观测稀疏度】我们能见证多少翻转")
    print("-" * 78)
    for d in dirs:
        got = load_dir(d)
        if got is None:
            continue
        base = os.path.basename(d)
        obs, fcs, ts_all = got
        if not ts_all:
            continue
        dur = (max(ts_all) - min(ts_all)).total_seconds()
        n_uid = len(obs)
        once = sum(1 for u, l in obs.items() if len(l) == 1)
        unlit = len({u for u, l in obs.items()
                     if any(o[3] == 1 for o in l)})
        n6 = sum(1 for e in fcs if e.get('fc_event_type') == 6)
        print(f"  {base[5:]:<28} 时长{dur / 60:>5.1f}min 观察到用户{n_uid:>5} "
              f"仅1次的{once / n_uid:>5.1%}  |  观测到未点亮用户 {unlit:>4}  "
              f"|  type=6 抓到 {n6:>3}")

    print("\n" + "=" * 78)
    print("结论口径提示")
    print("=" * 78)
    print("  · 捕获率 < 100% ⇒ **type=6 的绝对条数不是直播间的真实发生量**，只能当")
    print("    「存在性 / 相对强弱」信号用；跨场次比条数会被采样率污染。")
    print("  · 漏采来自**服务端抽样**（先决条件 1 已排除连线中断）。")
    print("  · 前提假设：1→2 翻转**必然**伴随一条 type=6。若存在其它翻转路径，")
    print("    本审计会把那些情形也算作『漏采』，从而**低估**捕获率。")


if __name__ == '__main__':
    main()
