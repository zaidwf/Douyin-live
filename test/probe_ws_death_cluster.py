# -*- coding: utf-8 -*-
"""一次性：WS 断线**是不是成批发生**（第二级分辨率）+ 断线那一刻有几路并发在采。

判据：
  - 断线时刻按秒聚类。若「隧道在并发下被压垮」，同一秒/同一两秒内会有多条连接
    一起死（因为隧道是同一个账号、同一个出口）。
  - 若「每条长连接各有寿命上限」，则即便并发 10 路，它们的死因时刻是各自
    从自己建立时刻起算的，**与别人建立时刻无关** —— 聚不成批。
"""
import glob
import json
import os
import datetime as dt
from collections import Counter, defaultdict

DATA = '/opt/douyin-live/data'

def parse(ts):
    try:
        return dt.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S')
    except Exception:
        try:
            return dt.datetime.fromisoformat(ts)
        except Exception:
            return None

rows = []
for p in glob.glob(os.path.join(DATA, '*', 'live_*', 'session_meta.json')):
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        continue
    st, en = parse(d.get('started_at', '')), parse(d.get('ended_at', ''))
    if not st or not en:
        continue
    rows.append({'dir': os.path.basename(os.path.dirname(p)), 'st': st, 'en': en,
                 'reason': d.get('stop_reason'), 'dur': d.get('duration_s')})

print('可解析起止的场次 %d' % len(rows))
ws = [r for r in rows if r['reason'] == 'ws_closed']
print('其中 ws_closed %d' % len(ws))

# ① 断线时刻按秒聚类：同一秒有几条连接死
sec = Counter(r['en'].strftime('%H:%M:%S') for r in ws)
multi = {k: v for k, v in sec.items() if v > 1}
print('\n① 同一秒内有 ≥2 条 WS 死亡的时刻：%d 个，涉及 %d 条连接'
      % (len(multi), sum(multi.values())))
for k in sorted(multi, key=lambda k: -multi[k])[:25]:
    print('   %s  x%d' % (k, multi[k]))

# ② 更宽容：2 秒窗内
win = defaultdict(list)
for r in ws:
    b = r['en'].replace(microsecond=0)
    win[b].append(r)
print('\n② 相邻秒合并（把 T 和 T+1s 算一组）后，成批组：')
keys = sorted(sec)
i = 0
groups = []
while i < len(keys):
    t = dt.datetime.strptime(keys[i], '%H:%M:%S')
    grp = [keys[i]]
    while i + 1 < len(keys) and (dt.datetime.strptime(keys[i + 1], '%H:%M:%S') - t).seconds <= 1:
        i += 1
        grp.append(keys[i])
    n = sum(sec[k] for k in grp)
    if n >= 3:
        groups.append((grp[0], n, len(grp)))
    i += 1
print('   成批（≥3 条死在 ≤2 秒内）共 %d 组' % len(groups))
for k, n, lv in groups[:25]:
    print('   %s 起 %d 条（跨 %d 秒）' % (k, n, lv))

# ③ 断线那一刻有几个「别的场次」在采（并发度）
print('\n③ 每条 ws_closed 死亡时刻的并发在采场次数分布：')
conc = Counter()
for r in ws:
    c = sum(1 for o in rows if o['st'] <= r['en'] <= o['en'])
    conc[c] += 1
for k in sorted(conc):
    print('   并发 %2d 路 : %s %d' % (k, '#' * min(conc[k], 50), conc[k]))

# ④ 存活时长 vs 并发度（并发越高是不是越短？）
print('\n④ 按死亡时刻并发度分组的存活时长中位数：')
byc = defaultdict(list)
for r in ws:
    c = sum(1 for o in rows if o['st'] <= r['en'] <= o['en'])
    if r['dur']:
        byc[c // 3 * 3].append(r['dur'])
for k in sorted(byc):
    v = sorted(byc[k])
    print('   并发 %2d-%2d 路 : n=%3d 中位 %4d 秒' % (k, k + 2, len(v), v[len(v) // 2]))
