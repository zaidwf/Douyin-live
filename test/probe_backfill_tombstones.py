"""一次性工具：从 journald **回填**墓碑账本（`dist/deleted_sessions.jsonl`）。

背景见 `live_ledger.append_tombstones()`：`find_missed()` 靠场次目录判断「有没有在采」，
而删除入口删的就是场次目录 —— 不记墓碑就会长出假漏采。

2026-10-15 那 281 个场次是在墓碑机制**之前**删的，所以没记录。但 journald 里有权威数据：

    [金山银山] 本场结束（时长 225.8 分钟），数据落盘：data/20261015/live_164425316_20261015_164820

「结束时刻 − 时长 = 开始时刻」，场次目录名里也有开始时刻，两处可以互相校验。缺
`session_meta.json` 的老场次没有更准的来源，这个精度足够覆盖率判定（判据本来就有
`COVER_MARGIN_MIN` 的余量）。

幂等：按 `场次目录` 去重，重复跑不会写重。

    ./venv/bin/python test/probe_backfill_tombstones.py [--since '2026-10-15 00:00'] [--dry]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOMBSTONE = os.path.join(ROOT, 'service', 'dist', 'deleted_sessions.jsonl')

# [昵称] 本场结束（时长 225.8 分钟），数据落盘：data/20261015/live_164425316_20261015_164820
# 同时认 `-o short-iso`（2026-10-15T22:55:49+0800）与默认输出（Oct 15 22:55:49）
END_RE = re.compile(
    r'(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}|\w{3} +\d{1,2} \d{2}:\d{2}:\d{2})'
    r'.*?本场结束（时长 (?P<dur>[\d.]+) 分钟）'
    r'，数据落盘：(?P<dir>data/\d{8}/live_\S+)')
SESSION_RE = re.compile(r'^live_(?P<did>.+)_(?P<d>\d{8})_(?P<t>\d{6})$')


def journal_ends(since):
    """从 journald 抽「本场结束」行 → [{dir, douyin_id, started, last_write}]"""
    out = []
    try:
        txt = subprocess.run(
            ['journalctl', '-u', 'douyin-crawl', '--since', since, '--no-pager',
             '-o', 'short-iso'],
            capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f'读 journald 失败：{e}')
        return out
    for line in txt.splitlines():
        m = END_RE.search(line)
        if not m:
            continue
        sm = SESSION_RE.match(os.path.basename(m.group('dir')))
        if not sm:
            continue
        try:
            ended = datetime.strptime(m.group('ts'), '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            try:
                ended = datetime.strptime(m.group('ts'), '%b %d %H:%M:%S').replace(
                    year=datetime.now().year)
            except ValueError:
                continue
        started = datetime.strptime(sm.group('d') + sm.group('t'), '%Y%m%d%H%M%S')
        # 目录名里的开始时刻是权威的；日志里的结束时刻减时长只用来交叉校验
        by_dur = ended - timedelta(minutes=float(m.group('dur')))
        out.append({
            'ts': datetime.now().isoformat(timespec='seconds'),
            'reason': 'backfill-journald',
            'dir': m.group('dir'),
            'douyin_id': sm.group('did'),
            'started': started.isoformat(timespec='seconds'),
            'last_write': ended.isoformat(timespec='seconds'),
            'skew_s': round(abs((by_dur - started).total_seconds())),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-10-15 00:00')
    ap.add_argument('--dry', action='store_true')
    args = ap.parse_args()

    recs = journal_ends(args.since)
    print(f'journald 里抽出「本场结束」记录 {len(recs)} 条（since {args.since}）')
    if not recs:
        return

    known = set()
    if os.path.exists(TOMBSTONE):
        with open(TOMBSTONE, encoding='utf-8') as fh:
            for line in fh:
                try:
                    known.add(json.loads(line)['dir'])
                except (ValueError, KeyError):
                    pass
    fresh = [r for r in recs if r['dir'] not in known]
    print(f'其中 {len(recs) - len(fresh)} 条已在账本里（跳过），新增 {len(fresh)} 条')
    # 只回填**现在真的不存在**的目录 —— 还在的说明没被删，不该记墓碑
    fresh = [r for r in fresh if not os.path.exists(os.path.join(ROOT, r['dir']))]
    print(f'再滤掉仍存在的目录 → 实际回填 {len(fresh)} 条')

    skew = [r['skew_s'] for r in fresh if r['skew_s'] > 60]
    if skew:
        print(f'⚠️ {len(skew)} 条的「结束−时长」与目录名开始时刻差 >60s '
              f'（最大 {max(skew)}s）—— 以目录名为准，仅作提示')
    if args.dry:
        for r in fresh[:5]:
            print('  ·', r['dir'], r['started'], '→', r['last_write'])
        print('（--dry，未写入）')
        return
    os.makedirs(os.path.dirname(TOMBSTONE), exist_ok=True)
    with open(TOMBSTONE, 'a', encoding='utf-8') as fh:
        for r in fresh:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(f'已写入 {TOMBSTONE}')


if __name__ == '__main__':
    main()
