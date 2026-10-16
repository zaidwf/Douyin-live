# -*- coding: utf-8 -*-
"""一次性：ws_closed 场次的时长分布 —— 看有没有「硬上限」特征（一次性探针，非单测）"""
import glob
import json
import os
from collections import Counter

DATA = '/opt/douyin-live/data'
rows = []
for p in glob.glob(os.path.join(DATA, '*', 'live_*', 'session_meta.json')):
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        continue
    rows.append({
        'dir': os.path.basename(os.path.dirname(p)),
        'date': os.path.basename(os.path.dirname(os.path.dirname(p))),
        'dur': d.get('duration_s'),
        'reason': d.get('stop_reason'),
        'started': d.get('started_at', ''),
    })

print('总场次 %d' % len(rows))
byr = Counter(r['reason'] for r in rows)
print('stop_reason:', dict(byr))

for reason in ('ws_closed', 'offline', 'signal', 'stall'):
    ds = sorted(r['dur'] for r in rows if r['reason'] == reason and r['dur'])
    if not ds:
        continue
    n = len(ds)
    print('\n== %s  n=%d  min=%d  p25=%d  中位=%d  p75=%d  max=%d =='
          % (reason, n, ds[0], ds[n // 4], ds[n // 2], ds[3 * n // 4], ds[-1]))
    # 60 秒分桶，看是否有硬边
    c = Counter(int(d // 60) for d in ds)
    for k in sorted(c):
        print('  %3d-%3d 分 %s %d' % (k, k + 1, '#' * min(c[k], 60), c[k]))

# ws_closed 是否与「同一主播的其他场次」同时发生（并发相关性）
print('\n== ws_closed 的 started_at 精确到分钟的重合度 ==')
st = Counter(r['started'][:16] for r in rows if r['reason'] == 'ws_closed')
for k, v in sorted(st.items())[-40:]:
    if v > 1:
        print('  %s  同分钟开播 %d 场' % (k, v))
