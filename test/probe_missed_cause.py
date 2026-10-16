"""一次性诊断：`★疑似漏采` 是真漏采，还是**删除入口自己把证据删掉了**？

`find_missed()` 判的是「这一轮探到在播，但那一刻没有场次在写」。它靠**场次目录**当证据。
而 `--delete-incomplete` / `--delete-migrated` 删的就是场次目录 —— 于是删完之后，那些
本来被纯文本场次覆盖的时间段会**变成「没在采」**，凭空长出漏采告警。

这个脚本按轮次逐条算覆盖率，把「哪几轮没过」直接摆出来，再与该主播**现存**的场次对账。
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'service'))

import live_ledger as ll                                            # noqa: E402


def main():
    targets = sys.argv[1:] or ['164425316', '37682323632', '40625844102', '49822721018',
                               '547426aok', '59382298966', '60183981003', '75224268496',
                               'wzbx999', 'xu94941111']
    rounds = ll.load_rounds(days=3)
    sessions = ll.scan_all_sessions(inflight_min=15, now=datetime.now())
    spans = {}
    for f in sessions:
        spans.setdefault(f['douyin_id'], []).append(f)

    ts_all = sorted({r['ts'] for r in rounds})
    recent = ts_all[-6:]
    print(f'最近 6 轮：{[(t.strftime("%H:%M"), ) for t in recent]}')
    print()

    margin = timedelta(minutes=ll.COVER_MARGIN_MIN)
    for did in targets:
        obs = {r['ts']: r for r in rounds if r['douyin_id'] == did}
        mine = spans.get(did, [])
        print(f'===== {did}   现存场次 {len(mine)} 个 =====')
        for f in sorted(mine, key=lambda x: x['started']):
            print(f'    {f["started"]:%H:%M:%S} → {(f["last_write"] or f["started"]):%H:%M:%S}'
                  f'  音频{f["audio_segs"]:>4} 帧{f["frames"]:>5}  {f["dir"]}')
        for t in recent:
            o = obs.get(t)
            if not o:
                print(f'    {t:%H:%M}  (无观测)')
                continue
            if not o['live']:
                print(f'    {t:%H:%M}  {o["status"]} → 不算在播，**断掉连续段**')
                continue
            hit = [f for f in mine
                   if f['started'] - margin <= t <= (f['last_write'] or f['started']) + margin]
            print(f'    {t:%H:%M}  在播 → "覆盖场次": '
                  + (', '.join(f'{h["started"]:%H:%M}' for h in hit) if hit else '**没有**'))
        print()


if __name__ == '__main__':
    main()
