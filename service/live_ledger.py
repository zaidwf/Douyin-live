#!/usr/bin/python
# coding:utf-8
"""开播台账：每轮探全部主播的开播状态，并把 `data/` 的场次汇成**一张迁移清单**。

## 为什么要有它

两件事各占一半：

**一、知道「谁在播」** —— 每 30 分钟探一轮名单里全部主播，原始观测追加进
`service/dist/live_rounds_<YYYYMM>.jsonl`（一行一轮一主播）。这是「持续记录所有博主
开播信息」的底账，表只是它的视图。

**二、知道「哪一场采全了」** —— 用户会**手动把采完整的场次迁走**，不完整的直接删。
所以 `场次` sheet 不是报表，是**迁移清单**：一行一场，标出完整性、标出原因、
留出 `是否已迁移` / `备注` 两列给人工填。**重写表格时会按主键把这两列原值搬回来**，
所以标注不会被打架（见 `read_manual_columns`）。

## ★ 最重要的那一列：`疑似漏采`

台账探到「某主播在播」，而 `data/` 里那一刻**没有**它在写的场次 —— 这就是采集漏了。

2026-10-15 首次实测：名单 157 人探到 **36 个在播**，守护进程只有 **21 个在写**
→ **16 个房间（44%）没在采**。根因是 WS 僵死（连接活着但抖音不再送数据），
线程永久卡在等待循环里，**该主播之后每次开播整场漏采**。已由
`auto_crawl._stalled()` 修复（见 `STALL_SECONDS`），这一列就是它的长期哨兵。

判据要求**连续 ≥2 轮**都漏才算（单轮可能是刚开播还没轮到，或探测抖动）——
一次误报就没人看这一列了，和 `health_check.py` 里 `quiet`/`stale` 的取舍同理。

## 「完整」的口径（用户定的：**必须含音视频**）

| 条件 | 完整性 | 原因 |
|---|---|---|
| 还在写 | `采集中` | 不判定（新开播的正常中间态）|
| 无 `room.json` | `不完整` | `未收尾`（进程被杀 / 僵死后未重连）|
| 文本与事件都空 | `不完整` | `无内容` |
| 音视频有效段为 0（或只有一半）| `不完整` | `无音视频…`（带原因细分）|
| 其余 | `完整` | |

⚠️ **音视频的「有效段」按 `MediaCapture.MEDIA_MIN_VALID_BYTES`（1 KiB）过滤** ——
78 字节的空壳 WAV（44 字节头 + 长度 0 的 `data` 块）**不算数**。2026-10-15 就是它
把「0 帧 / 1 段音频」报成了有音视频。

「为什么没有音视频」读的是场次目录里的 `session_meta.json`（`auto_crawl` 落盘）：
`gate_no_slot` 名额没排到 / `disk_low` 磁盘降级 / `start_failed` 启动失败 /
`disabled` 没开音视频。**区分这四种是关键** —— 前两种是设计内的降级，后两种才是故障，
混成一个「没有音视频」会让真故障淹没在噪声里。老数据没有这个文件 → `原因未记录`。

## 用法

    python service/live_ledger.py                     # 探一轮 + 重建表格
    python service/live_ledger.py --no-probe          # 只重建表格（不探测）
    python service/live_ledger.py --limit 10          # 只探前 10 个（冒烟）
    python service/live_ledger.py --delete-incomplete            # **只打印**要删什么
    python service/live_ledger.py --delete-incomplete --apply    # 真删（默认只删无音视频的）
    python service/live_ledger.py --delete-migrated --apply      # 删已迁走的（回收空间）

## 用法（机器对机器 —— 给笔记本的 `service/pull_sessions.py` 用）

    # 逐场次事实（JSON 到 stdout）—— 笔记本据此决定拉哪些
    python service/live_ledger.py --dump-facts -
    # 某场次的逐文件清单 —— 笔记本据此校验传输结果
    python service/live_ledger.py --manifest data/20261016/live_xxx_20261016_020000
    # 按笔记本给的清单删（清单走 **stdin**，服务器上不落临时文件）
    cat list.txt | python service/live_ledger.py --delete-list - --expect-count 12 --apply

⚠️ 这三个模式**必须只往 stdout 写 JSON**（诊断一律走 stderr），而且要**在
`from auto_crawl import load_anchors` 之前短路** —— 它们只要扫盘，**不需要名单**。

⚠️ **`--delete-migrated` 在自动拉取上线后应当停用**：它按人工列 `是否已迁移` 删，而
「人工标了已迁移」不等于「自动拉取已经把副本拿到本地了」—— 两者并用会删掉**本地根本
没有副本**的场次。日常排水改由自动拉取（删除入口 `--delete-list`，墓碑标记 `已传输`）。
## ⚠️ 删除入口：默认**永不删含音视频的场次**（除非显式 `--include-media`）

`不完整` 这个判定**不等于「数据没价值」**。它有三种完全不同的成因：

| 成因 | 内容 | 该不该删 |
|---|---|---|
| `无音视频`（绝大多数）| 名额只有个位数而同时在播几十人，**天生拿不到媒体名额** | 该删（对多模态研究没用）|
| `无内容` / `空` | 连上了但一条弹幕都没收到 | 该删 |
| **`未收尾` 却有音视频** | 进程被杀（V8 崩溃 / OOM / 重启），`room.json` 没写成 | **不该删 —— 内容是真的** |

2026-10-15 实测：第三类有 **5 场 / 2.73 GB**，其中一场 **1.3 GB 是当天最大的场次**
（669 段音频、4011 帧、522 KB 弹幕，只是 15:04 那次 `py_mini_racer` V8 崩溃把它截断了）。
按裸 `不完整` 口径删会连它一起删掉，**总量从 221 MB 变成 2.95 GB，且不可逆**。

所以 `--delete-incomplete` 的候选里，**含音视频的（`音频段 > 0` 或 `帧数 > 0`）默认被
跳过并单独报出来**，要删得再显式加 `--include-media`。这是 fail-closed：默认保住好数据。

## ⚠️ 删除必须留墓碑，否则删除入口会自己制造假漏采

`疑似漏采` 判的是「这一轮探到在播，但那一刻**没有场次在写**」—— 它拿**场次目录**当证据。
而删除入口删的就是场次目录。所以每删一次，那些本来被纯文本场次覆盖的时间段就会**变成
「没在采」**，`主播` 表长出假漏采、退出码变 1、`systemctl --failed` 被污染
（正是本文件开头警告过的「报几次就没人看」）。

2026-10-15 实测踩到：删完 281 个场次后 `★疑似漏采` **从 0 跳到 10**，逐个对 journald
核对 —— 那 10 个主播当时**都在采**，场次一条不缺，全是删出来的假阳性。

所以 `delete_sessions(apply=True)` 在 `rmtree` **之前**把每个场次记进
`dist/deleted_sessions.jsonl`（`append_tombstones`），`main()` 再把它们并进覆盖率判定的
`spans`。墓碑不进表格、不进迁移清单、也不会被再删一次 —— 它只证明「这场曾经在采」。
回填历史墓碑用 `test/probe_backfill_tombstones.py`（从 journald 的「本场结束」行）。

## ⚠️ 会话名额已满的轮次不算漏采（2026-10-16 起的口径）

守护进程现在按 `auto_crawl --max-sessions 10` 跑：**同时在采的主播最多 10 个**，名额
先到先得，抢不到的在场主播**本来就一点数据都不落**（那是策略，不是故障）。

所以 `find_missed()` 多了一个 `session_cap` 参数（由 `--max-sessions` 传入）：某一刻
「在采场次数 ≥ 上限」的轮次**不计漏采**。不排除的话，10 个名额配一二十个在播，这一列
会恒等于「在播数 − 10」、退出码恒为 1，把 `systemctl --failed` 彻底淹掉。

排除之后这一列**仍然有用，而且判据更准**：它现在报的是「**名额没占满，却有人在播而没
在采**」—— 那才是真出了事（WS 僵死、线程卡住、进程半死不活）。这是 2026-10-15 那个
44% 漏采事故的哨兵，不要因为换了口径就把它删掉。

由 `douyin-ledger.timer` 每 10 分钟跑一次。退出码：0 正常；1 = 探测通道全失败或
有 `疑似漏采`（进 `systemctl --failed` 告警面）。
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # 供 import health_check
# ⚠️ 幂等包装（同 `health_check.py` 里那段注释）：两个模块都会包 stdout，
# 各包一次的话先包的那个会被 GC 掉并关掉底层 buffer。
if not getattr(sys.stdout, '_douyin_utf8', False):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                      errors='replace', line_buffering=True)
        sys.stdout._douyin_utf8 = True
    except Exception:
        pass
# stderr 也要包 —— `--dump-facts` / `--delete-list` 这些**机器对机器**模式把诊断信息
# 全部推到 stderr（stdout 只留 JSON），而 Windows 上 stderr 默认是 GBK，中文会变成 `��`。
# 与 stdout 的包装互不影响（不同流），各用各的幂等标记。
if not getattr(sys.stderr, '_douyin_utf8', False):
    try:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                                      errors='replace', line_buffering=True)
        sys.stderr._douyin_utf8 = True
    except Exception:
        pass

import health_check as hc     # 复用 classify_status（'未知'/'错误' 是**失败**不是「没开播」）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.path.join(ROOT, 'data')
DIST = os.path.join(ROOT, 'service', 'dist')
DEFAULT_ROSTER = os.path.join(ROOT, 'documents', 'huitun_10_100w_1015_final.xlsx')
LEDGER_XLSX = os.path.join(DIST, 'live_ledger.xlsx')
JSON_OUT = os.path.join(DIST, 'ledger.json')
USABLE_CSV = os.path.join(DIST, 'usable_pending.csv')
# 墓碑账本：已删场次的「存在过」记录，只喂覆盖率判定，见 `append_tombstones()`
TOMBSTONE = os.path.join(DIST, 'deleted_sessions.jsonl')

# 人工列：**重写表格时按主键搬回来**，所以这两个名字是契约（README 里也写了）
MANUAL_COLS = ('是否已迁移', '备注')
KEY_COL = '场次目录'

# 开播矩阵：**每天一张 sheet**，行 = 名单里的主播（保持名单顺序），列 = 当天各轮探测。
# 用户要看的正是「开播数量随时间怎么变」—— 所以列头用轮次时刻，另在末尾压两行汇总。
# 默认出最近 3 天（与 `--history-days` 一致），`--matrix-days 0` 关掉。
MATRIX_SHEET_DAYS = 3
# `live_rounds` 里的三态（True 在播 / False 确认没播 / None 探测失败）→ 单元格文本。
# ★ 必须**三态**：把 `None` 显示成「没播」会把「隧道挂了」看成一屋子人集体下播 ——
#   这正是 `get_room_status()` 那个反直觉返回值踩过的坑（见 CLAUDE.md）。
LIVE_TEXT = {True: '在播', False: '没播', None: '未知'}

# `session_meta.json` 的 `stop_reason` → 人话。★ 这一列是为了**一眼看出场次为什么这么短**：
# 2026-10-16 实测 99 场里 85 场是 `ws_closed`（WS 被掐断），只有 3 场是正常下播。
STOP_REASON_TEXT = {
    'offline': '正常下播',
    # ⚠️ 同一个词在新旧数据里含义不同，读这一列时要记得分界点（2026-10-16 起）：
    #    旧 = 「WS 断过一次就收尾」，新 = 「**同场次内自动重连的额度用尽**了」
    #    （见 `auto_crawl.WS_RECONNECT`）。两种都确实是「这条连接废了」，所以主干文案不变。
    'ws_closed': '★WS 断开（重连额度用尽）',
    'stall': '★WS 僵死（连着但没数据）',
    'signal': '收到停止信号（重启）',
}

# 待迁移清单（CSV）的列 —— 比 xlsx 少几列：「原因」（口径写在 docstring 里）与
# 「名单内」（用户按体积/时长挑，不按名单）。`★完整性` 加星是为了在 Excel 里好筛。
USABLE_COLS = ['抖音号', '主播昵称', '开播时间', '结束时间', '时长(分)', '音频段', '帧数',
               '模态', '★完整性', '体积MB', KEY_COL, '是否已迁移']

SESSION_COLS = ['抖音号', '主播昵称', '开播时间', '结束时间', '时长(分)', KEY_COL,
                '弹幕数', '事件数', '音频段', '帧数', '模态', '完整性', '结束原因', '原因',
                '名单内', '是否已迁移', '备注']
ANCHOR_COLS = ['抖音号', '主播昵称', '最近状态', '最近观测时间', '今日在播轮数',
               '今日场次数', '今日完整场次数', '疑似漏采', '备注']

DATE_DIR_RE = re.compile(r'^\d{8}$')
SESSION_DIR_RE = re.compile(r'^live_(.+)_(\d{8})_(\d{6})$')

# 与 MediaCapture.MEDIA_MIN_VALID_BYTES 一致：小于这个字节数的产物当残片，不算有效音视频
MIN_MEDIA_BYTES = 1024

# `dir_manifest()` 里**多大以下才算 sha256**（见该函数注释）。
# 语料本体是那些 JSONL / room.json，每个几百 KB，哈希几乎免费；而 1.3 GB 的帧序列在
# 2C2G 且正与 10 路 ffmpeg 抢 CPU 的机器上逐张 sha256 是纯浪费 —— 那部分只比字节数，
# 传输完整性由 rsync 协议自己的逐块校验和保证。
# ⚠️ 这个阈值会随 JSON 一起回显（`hash_max_bytes`），笔记本发现两边不一致会**拒绝**，
# 防止两侧算法悄悄漂移成「各算各的」。
MANIFEST_HASH_MAX = 8 * 1024 * 1024

# 「疑似漏采」要连续这么多轮才报（单轮可能是刚开播还没轮到，或探测抖动）
MISSED_RUN_MIN = 2
# 判定「那一刻有场次在写」时允许的时间偏差（分钟）—— 台账 30 分钟一轮，
# 而守护进程 60 秒一轮，两者的观测时刻本就不对齐
COVER_MARGIN_MIN = 10


# ================================================================ 纯逻辑（可单测）
def parse_session_dir(name):
    """`live_<抖音号>_<YYYYMMDD>_<HHMMSS>` → (抖音号, 开播 datetime)；不匹配返回 (None, None)

    开播时间取**目录名里的时间戳**，不用文件 mtime：目录名是守护进程在「确认开播」
    那一刻定的（60 秒内发现），是全场最准的开播时刻来源。
    """
    m = SESSION_DIR_RE.match(name)
    if not m:
        return None, None
    douyin_id, date_str, time_str = m.groups()
    try:
        return douyin_id, datetime.strptime(date_str + time_str, '%Y%m%d%H%M%S')
    except ValueError:
        return None, None


def _fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S') if dt else ''


def duration_min(f):
    """场次时长（分钟）。xlsx 与迁移清单 CSV 共用**一份**算法，避免两处对不上"""
    if f['started'] and f['ended']:
        return round((f['ended'] - f['started']).total_seconds() / 60, 1)
    return ''


def classify_session(f):
    """场次事实 → `(完整性, 原因, 模态)`。**纯函数**，单测直接喂合成 dict。

    :param f: `scan_session()` 产出的事实字典
    """
    audio, frames = f['audio_segs'], f['frames']
    # 模态如实写「这场到底有什么」，不美化：残缺的媒体**不计入**「有音视频」，
    # 但也不能因此把它报成「仅文本数值」—— 那会让人以为 `audio/` 里什么都没有。
    has_text = bool(f['danmaku_lines'] or f['event_lines'])
    has_av = bool(audio and frames)
    has_any = bool(audio or frames)
    if has_av and has_text:
        modal = '全模态'
    elif has_av:
        modal = '仅音视频'
    elif has_any and has_text:
        modal = '文本+残缺音视频'
    elif has_any:
        modal = '仅残缺音视频'
    elif has_text:
        modal = '仅文本数值'
    else:
        modal = '空'

    if f['inflight']:
        # 正在写 = 新开播的正常中间态。**不能判不完整** —— 否则每次刚开播的场次
        # 都会出现在「可删」列表里，而用户是会照这个列表动手的。
        return '采集中', '本场正在写入，暂不判定', modal

    if not f['has_room_json']:
        return '不完整', '未收尾（进程被杀，或 WS 僵死后没能重连）', modal

    if not f['danmaku_lines'] and not f['event_lines']:
        # 因果要说清：ffmpeg 拉的是 FLV 流，和 WS 是**两条独立的通道**。所以
        # 「有音视频但一条文本都没有」= WS 侧从头就没通，和「两边都空」是两种故障。
        if audio or frames:
            return '不完整', '无内容（★ 音视频有产出但文本通道一条都没收到 —— WS 侧异常）', modal
        return '不完整', '无内容（连上了但一条文本/数值都没收到）', modal

    if not audio and not frames:
        return '不完整', '无音视频' + _media_reason(f['meta']), modal
    if not (audio and frames):
        missing = '缺音频' if not audio else '缺帧'
        return '不完整', f'音视频不全（{missing}）', modal
    return '完整', '', modal


def _media_reason(meta):
    """从 `session_meta.json` 读「为什么没产出音视频」，翻成中文附在原因后面"""
    if not meta:
        return '（老数据，原因未记录）'
    m = meta.get('media') or {}
    if not m.get('enabled'):
        return '（本场未开启音视频）'
    why = m.get('skip_reason')
    waited = m.get('gate_wait_s') or 0
    if why == 'gate_no_slot':
        return f'（没排到名额，排队 {waited / 60:.0f} 分钟后本场已结束）'
    if why == 'disk_low':
        return '（磁盘空间不足，主动降级为纯文本）'
    if why == 'start_failed':
        return '（★ 音视频启动失败 —— 这是故障，不是降级）'
    if why == 'disabled':
        return '（本场未开启音视频）'
    if m.get('frames') is None and m.get('audio') is None:
        return '（原因未记录）'
    return '（★ 起了 ffmpeg 却一段有效产物都没有 —— 这是故障）'


def find_missed(rounds, sessions, run_min=MISSED_RUN_MIN, margin_min=COVER_MARGIN_MIN,
                session_cap=0):
    """找出「台账探到在播、但那一刻没有场次在写」的主播。

    :param rounds:   `[{'ts': datetime, 'douyin_id': str, 'live': bool|None}, ...]`
                     `live=None` 表示这一轮探测失败（**不能据此下结论**，直接跳过）
    :param sessions: `{抖音号: [{'started': datetime, 'last_write': datetime}, ...]}`
    :param session_cap: 守护进程的**会话并发上限**（`auto_crawl --max-sessions`）。
        0 = 不限（= 改动前行为）。
    :return: `(漏采的主播集合, {抖音号: (连续漏采轮数, 首个漏采时刻)})`

    为什么要求**连续 ≥2 轮**：单轮漏可能是「刚开播、守护还没轮到」（守护 60 秒一轮，
    而台账 10 分钟一轮，天然有偏差），也可能是探测抖动。一次误报就没人看这一列了。

    ★ **名额已满的轮次不算漏采**（`session_cap > 0` 时生效）。2026-10-16 起守护进程按
    `--max-sessions 10` 跑：名额先到先得，**抢不到的在场主播本来就一点数据都不落**
    —— 那是策略，不是故障。不排除的话，10 个名额配一二十个在播，这一列会恒等于
    「在播数 − 10」、退出码恒为 1，把 `systemctl --failed` 这个告警面彻底淹掉。

    排除之后这一列仍然有用，而且判据更准了：它现在报的是「**名额没占满，却有人在播
    而没在采**」—— 那才是真的出事了（WS 僵死、线程卡住、进程半死不活）。
    """
    by_anchor = {}
    for r in rounds:
        by_anchor.setdefault(r['douyin_id'], []).append(r)
    for v in by_anchor.values():
        v.sort(key=lambda r: r['ts'])

    # 「这一刻在采的场次数」。所有主播的场次合在一起算 —— 名额是**进程级**的，
    # 占着它的是别的房间，所以不能只看本主播自己的场次。
    #
    # ⚠️ 这里**不加 `margin`**（与上面的 `covered` 判据不同）：`covered` 容忍 ±10 分钟
    # 是因为「守护 60 秒一轮 + 台账 10 分钟一轮」天然有偏差，差几分钟算「在采」；
    # 而名额占用是**精确**的 —— 名额恰恰是在 `last_write` 那一刻（整场收尾之后）
    # 才还回去的（见 auto_crawl._one_cycle 的 finally）。放宽反而会把「名额其实
    # 已经空出来了」的轮次误判成「名额已满」，从而漏报真事故。
    all_spans = [s for v in sessions.values() for s in v]
    _inflight = {}

    def inflight_at(ts):
        n = _inflight.get(ts)
        if n is None:
            n = sum(1 for s in all_spans if s['started'] <= ts <= s['last_write'])
            _inflight[ts] = n
        return n

    margin = timedelta(minutes=margin_min)
    missed, detail = set(), {}
    for did, rs in by_anchor.items():
        spans = sessions.get(did, [])
        best_run, best_first = 0, None
        run, first = 0, None
        for r in rs:
            if not r['live']:
                run, first = 0, None        # 非「确认在播」→ 断掉（含探测失败）
                continue
            covered = any(s['started'] - margin <= r['ts'] <= s['last_write'] + margin
                          for s in spans)
            if covered:
                run, first = 0, None
            elif session_cap and inflight_at(r['ts']) >= session_cap:
                # 名额已满 → 「没轮到」是策略，断掉连续计数（不计漏采）
                run, first = 0, None
            else:
                run += 1
                first = first or r['ts']
                if run > best_run:
                    best_run, best_first = run, first
        if best_run >= run_min:
            missed.add(did)
            detail[did] = (best_run, best_first)
    return missed, detail


# ================================================================ 扫盘
def _tail_ts(path):
    """读 jsonl 最后一条可解析的 `ts`（→ datetime）；拿不到返回 None

    用**最后一条记录的 ts** 而不是文件 mtime 当结束时刻：mtime 受缓冲区刷写影响，
    而且跨机拷贝后会全部变成拷贝时刻。
    """
    last = None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    t = json.loads(line).get('ts')
                    if t:
                        last = datetime.fromisoformat(t)
                except (ValueError, AttributeError):
                    continue
    except OSError:
        pass
    return last


def _count_lines(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _count_media(dirpath, pattern):
    """数**有效**产物个数（按 MIN_MEDIA_BYTES 过滤掉残片）"""
    if not os.path.isdir(dirpath):
        return 0
    n = 0
    for name in os.listdir(dirpath):
        if not re.match(pattern, name):
            continue
        try:
            if os.path.getsize(os.path.join(dirpath, name)) >= MIN_MEDIA_BYTES:
                n += 1
        except OSError:
            pass
    return n


def _newest_write(dirpath):
    """目录内**最新文件**的 mtime；没有文件返回 None

    ⚠️ **不能用目录 mtime**：目录 mtime 只在新增/删除条目时更新，往已有文件里写内容
    不动它。2026-10-15 用错口径把「在采场次数」量成了 72（真值 22，差 3 倍）。
    """
    newest = None
    for dirpath_, _dirs, files in os.walk(dirpath):
        for name in files:
            try:
                m = os.path.getmtime(os.path.join(dirpath_, name))
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return datetime.fromtimestamp(newest) if newest else None


def scan_session(dirpath, inflight_min=15, now=None):
    """扫一个场次目录，产出事实字典（喂给 `classify_session`）"""
    now = now or datetime.now()
    name = os.path.basename(dirpath)
    douyin_id, started = parse_session_dir(name)
    if douyin_id is None:
        return None

    meta = None
    meta_path = os.path.join(dirpath, 'session_meta.json')
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding='utf-8') as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = None

    last_write = _newest_write(dirpath)
    inflight = bool(last_write and
                    (now - last_write) <= timedelta(minutes=inflight_min))

    # ★ **在采中的场次不报结束时间**（2026-10-16 修）。
    #   原因：`ended` 的三个来源 —— `session_meta.json` 的 `ended_at`（收尾时才写，此刻还没有）、
    #   JSONL 尾条 `ts`、最新文件 mtime —— 对**还没结束**的场次全都 ≈ 此刻。于是表格里
    #   几十场的「结束时间」都等于「写表那一刻」、时长恒为「开播至今」，看起来像
    #   「很多场直播结束时间一模一样」，实际是**它们根本还没结束**。
    #   留空 + 「结束原因」列写「采集中」才是如实的。删除门禁**不看这两列**
    #   （只看 `completeness`/`bytes`/`files`），所以这个改动不会动到任何删除判据。
    ended = None
    if not inflight:
        if meta and meta.get('ended_at'):
            try:
                ended = datetime.fromisoformat(meta['ended_at'])
            except ValueError:
                ended = None
        if ended is None:
            for fn in ('events.jsonl', 'danmaku.jsonl', 'series.jsonl'):
                ended = _tail_ts(os.path.join(dirpath, fn))
                if ended:
                    break
        if ended is None:
            ended = last_write

    try:
        # 相对路径更好读（表格里 `data/20261015/live_...` 一眼能认出来）
        rel = os.path.relpath(dirpath, ROOT).replace('\\', '/')
    except ValueError:
        # 跨盘符（Windows 上单测把临时目录放 C: 而仓库在 D:）→ 退回绝对路径。
        # 服务器上不会发生，但让一个只读脚本因为路径写法崩掉没有道理。
        rel = dirpath.replace('\\', '/')

    return {
        'douyin_id': douyin_id,
        'name': name,
        'dir': rel,
        'abspath': dirpath,
        'started': started,
        'ended': ended,
        'last_write': last_write,
        'inflight': inflight,
        'has_room_json': os.path.exists(os.path.join(dirpath, 'room.json')),
        'danmaku_lines': _count_lines(os.path.join(dirpath, 'danmaku.jsonl')),
        'event_lines': _count_lines(os.path.join(dirpath, 'events.jsonl')),
        'audio_segs': _count_media(os.path.join(dirpath, 'audio'), r'^segment_.*\.wav$'),
        'frames': _count_media(os.path.join(dirpath, 'frames'), r'^frame_.*\.jpg$'),
        'meta': meta,
    }


def scan_all_sessions(inflight_min=15, now=None):
    """扫 `data/<8位日期>/live_*` 下的全部场次。

    ⚠️ 只认**纯 8 位数字**的日期目录，于是 `data/_load_test/`、`data/_test_residue/`
    这些下划线前缀的非生产目录天然被跳过（与 CLAUDE.md 的约定一致）。
    """
    out = []
    if not os.path.isdir(DATA_ROOT):
        return out
    for date_dir in sorted(os.listdir(DATA_ROOT)):
        if not DATE_DIR_RE.match(date_dir):
            continue
        dpath = os.path.join(DATA_ROOT, date_dir)
        if not os.path.isdir(dpath):
            continue
        for name in sorted(os.listdir(dpath)):
            if not name.startswith('live_'):
                continue
            spath = os.path.join(dpath, name)
            if not os.path.isdir(spath):
                continue
            facts = scan_session(spath, inflight_min=inflight_min, now=now)
            if facts:
                facts['date'] = date_dir
                out.append(facts)
    return out


# ================================================================ 观测底账
def rounds_path(now=None):
    return os.path.join(DIST, 'live_rounds_%s.jsonl' % (now or datetime.now()).strftime('%Y%m'))


def load_rounds(days=3, now=None):
    """读最近若干天的观测（跨月要读两个文件）"""
    now = now or datetime.now()
    since = now - timedelta(days=days)
    rows = []
    for delta in (0, 1):
        m = (now.replace(day=1) - timedelta(days=delta * 30)).strftime('%Y%m')
        p = os.path.join(DIST, 'live_rounds_%s.jsonl' % m)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        r['ts'] = datetime.fromisoformat(r['ts'])
                    except (ValueError, KeyError, AttributeError):
                        continue
                    if r['ts'] >= since:
                        rows.append(r)
        except OSError:
            continue
    return rows


def probe_round(anchors, log=print):
    """探一轮全部主播，返回观测行列表。

    **复用守护进程同一条代码路径**（`DouyinLiveWebFetcher.get_room_status()` 走 enter
    接口 + `_RateLimiter` 限流）—— 这是 `health_check.py` 那条教训的直接应用：
    探测必须走和采集一样的路，否则会出现「体检说通、采集在空转」这种最误导人的组合。

    实测：157 人一轮 **50 秒、0 失败、0 个 444**（6 req/s 限流下），30 分钟一轮毫无压力。
    """
    from auto_crawl import _RateLimiter, DEFAULT_POLL_RATE
    from liveMan import DouyinLiveWebFetcher

    limiter = _RateLimiter(DEFAULT_POLL_RATE)
    round_ts = datetime.now().replace(microsecond=0)
    rows = []
    for i, (name, did) in enumerate(anchors, 1):
        limiter.acquire()
        ok, status, nick, uid = False, '', None, None
        try:
            ok, status, nick, uid = DouyinLiveWebFetcher(did, None).get_room_status()
        except Exception as e:
            status = f'异常:{type(e).__name__}'
        kinds = hc.classify_status(status)
        rows.append({
            'ts': round_ts.isoformat(timespec='seconds'),
            'douyin_id': str(did),
            'name': name,
            'success': bool(ok),
            'status': status,
            # live 三态：True 在播 / False 确认没播 / None 探测失败（不能下结论）
            'live': (kinds == 'live') if kinds in ('live', 'offline') else None,
            'nickname': nick,
            'user_id': uid,
        })
        if i % 50 == 0:
            log(f'  已探 {i}/{len(anchors)}…')
    return round_ts, rows


def append_rounds(rows, now=None):
    os.makedirs(DIST, exist_ok=True)
    path = rounds_path(now)
    with open(path, 'a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    return path


# ================================================================ 表格
def read_manual_columns(path=LEDGER_XLSX):
    """读**上一版**表格里的人工列，返回 `{场次目录: {列名: 值}}`

    ★ 这是本仓库的**新约定**（此前没有「读回已有 xlsx 保留人工列」的先例）：
    表格每 30 分钟整份重写，人工标注必须按主键搬回来，否则用户的标注会无声消失。

    ⚠️ 用 `load_workbook` **不加 `data_only=True`** —— 加了会把公式折成缓存值，
    回写时公式就丢了。代价是**人工列请填文本，不要用公式**（README 里也写了）。
    """
    if not os.path.exists(path):
        return {}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path)
    except Exception as e:
        print(f'⚠️ 读不回上一版表格的人工列（{e}）—— 本次标注将为空')
        return {}
    if '场次' not in wb.sheetnames:
        return {}
    ws = wb['场次']
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {}
    header = [str(c) if c is not None else '' for c in rows[0]]
    if KEY_COL not in header:
        return {}
    ki = header.index(KEY_COL)
    idx = {c: header.index(c) for c in MANUAL_COLS if c in header}

    out = {}
    for r in rows[1:]:
        if ki >= len(r) or not r[ki]:
            continue
        vals = {}
        for col, i in idx.items():
            v = r[i] if i < len(r) else None
            if v is not None and str(v).strip() != '':
                vals[col] = v
        if vals:
            out[str(r[ki])] = vals
    return out


def build_matrix_sheets(anchors, rounds, days=MATRIX_SHEET_DAYS, now=None):
    """按天生成「开播矩阵」：`[(sheet 名, 表头, 行), ...]`（**纯函数**）

    用户要的能力：**第一列是博主名单，之后每一轮探测新增一列**，格子里是在播状态。

    形状（每天一张 sheet，列 = 当天各轮）：

        抖音号 | 主播昵称 | 15:01 | 15:06 | 15:11 | …
        123    | 张三     | 在播  | 在播  | 没播  | …
        …
        （空行）
        ★ 在播数     |  | 5 | 7 | 4 | …
        ★ 探测失败数 |  | 0 | 1 | 0 | …

    ★ 三种状态原样保留（在播/没播/未知）：**「未知」是探测失败，不是「没播」** ——
    合成一档会把「隧道挂了」显示成「一屋子人集体下播」。汇总行里单列「探测失败数」
    就是为了让这种东西一眼可见。

    ★ 数据源只有 `live_rounds` 底账（**纯读**）：这里不探测、不新增任何采集，
      按天重建即可，所以历史某天随时能用 `--matrix-days` 调出来。
    """
    now = now or datetime.now()
    by_day = {}
    for r in rounds:
        ts = r.get('ts')
        did = r.get('douyin_id')
        if not ts or did is None:
            continue
        d = ts.strftime('%Y%m%d')
        b = by_day.setdefault(d, {'ts': set(), 'cell': {}})
        b['ts'].add(ts)
        b['cell'][(str(did), ts)] = r.get('live')

    out = []
    for d in sorted(by_day, reverse=True)[:days]:
        ts_list = sorted(by_day[d]['ts'])
        cell = by_day[d]['cell']
        header = ['抖音号', '主播昵称'] + [t.strftime('%H:%M') for t in ts_list]
        rows = []
        for name, did in anchors:
            did = str(did)
            rows.append([did, name] +
                        [LIVE_TEXT.get(cell.get((did, t)), '') for t in ts_list])
        live_cnt = [0] * len(ts_list)
        unknown_cnt = [0] * len(ts_list)
        for i, t in enumerate(ts_list):
            for _name, did in anchors:
                v = cell.get((str(did), t))
                if v is True:
                    live_cnt[i] += 1
                elif v is None:
                    unknown_cnt[i] += 1
        rows.append([''] * len(header))
        rows.append(['★ 在播数'] + [''] + live_cnt)
        rows.append(['★ 探测失败数'] + [''] + unknown_cnt)
        out.append(('开播矩阵_%s' % d[4:], header, rows))
    return out


def write_ledger(session_rows, anchor_rows, path=LEDGER_XLSX, matrix=None):
    """写 sheet 的 xlsx：`场次` / `主播` / 每天一张 `开播矩阵_MMDD`。
    先写 `.tmp` 再 `os.replace`（原子换入）"""
    import openpyxl
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '场次'
    ws.append(SESSION_COLS)
    for r in session_rows:
        ws.append([r.get(c, '') for c in SESSION_COLS])
    ws2 = wb.create_sheet('主播')
    ws2.append(ANCHOR_COLS)
    for r in anchor_rows:
        ws2.append([r.get(c, '') for c in ANCHOR_COLS])
    for sheet_name, header, rows in (matrix or []):
        ws3 = wb.create_sheet(sheet_name)
        ws3.append(header)
        for r in rows:
            ws3.append(r)

    tmp = path + '.tmp'
    wb.save(tmp)
    try:
        os.replace(tmp, path)
    except PermissionError:
        # 用户在 Excel 里开着这个文件 → Windows 不允许替换。**保留 tmp 并说清楚**，
        # 不要静默失败（那样用户会一直盯着一个不再更新的旧表）。
        keep = path + '.new'
        try:
            os.replace(tmp, keep)
        except OSError:
            keep = tmp
        print(f'⚠️ 表格被占用（Excel 里开着？）—— 新表已存到 {keep}，关掉后重跑即可')
        return keep
    return path


def stop_reason_text(f):
    """`session_meta.json` 的 `stop_reason` → 表格里那一列的文本（**纯函数**）

    在采中的场次没有 `stop_reason`（收尾时才写），写「采集中」而不是留白 ——
    留白会被当成「老数据没记」，而这两件事的处置完全不同。
    """
    if f['inflight']:
        return '采集中'
    meta = f.get('meta') or {}
    why = meta.get('stop_reason')
    if not why:
        return '（老数据未记录）'
    return STOP_REASON_TEXT.get(why, why)


def build_session_rows(sessions, anchors_by_id, manual):
    """场次事实 → 表格行（新场次在上）"""
    rows = []
    for f in sorted(sessions, key=lambda x: x['started'], reverse=True):
        completeness, reason, modal = classify_session(f)
        m = manual.get(f['dir'], {})
        dur = duration_min(f)
        row = {
            '抖音号': f['douyin_id'],
            '主播昵称': anchors_by_id.get(f['douyin_id'], ''),
            '开播时间': _fmt(f['started']),
            '结束时间': _fmt(f['ended']),
            '时长(分)': dur,
            KEY_COL: f['dir'],
            '弹幕数': f['danmaku_lines'],
            '事件数': f['event_lines'],
            '音频段': f['audio_segs'],
            '帧数': f['frames'],
            '模态': modal,
            '完整性': completeness,
            '结束原因': stop_reason_text(f),
            '原因': reason,
            '名单内': '是' if f['douyin_id'] in anchors_by_id else '否',
        }
        for c in MANUAL_COLS:
            row[c] = m.get(c, '')
        rows.append(row)
    return rows


def build_anchor_rows(anchors, sessions, rounds, missed_detail, now=None):
    """名单级状态表（一行一主播）"""
    now = now or datetime.now()
    today = now.strftime('%Y%m%d')
    by_id = {}
    for f in sessions:
        by_id.setdefault(f['douyin_id'], []).append(f)
    last_obs, live_rounds = {}, {}
    for r in rounds:
        did = r['douyin_id']
        if not last_obs.get(did) or r['ts'] > last_obs[did]['ts']:
            last_obs[did] = r
        if r.get('live') and r['ts'].strftime('%Y%m%d') == today:
            live_rounds[did] = live_rounds.get(did, 0) + 1

    rows = []
    for name, did in anchors:
        did = str(did)
        today_sessions = [f for f in by_id.get(did, []) if f['started'].strftime('%Y%m%d') == today]
        complete = sum(1 for f in today_sessions if classify_session(f)[0] == '完整')
        ob = last_obs.get(did)
        rows.append({
            '抖音号': did,
            '主播昵称': name,
            '最近状态': (ob['status'] if ob else '未观测'),
            '最近观测时间': _fmt(ob['ts']) if ob else '',
            '今日在播轮数': live_rounds.get(did, 0),
            '今日场次数': len(today_sessions),
            '今日完整场次数': complete,
            '疑似漏采': (f"★ 连续 {missed_detail[did][0]} 轮"
                         f"（{_fmt(missed_detail[did][1])} 起）"
                         if did in missed_detail else ''),
            '备注': '',
        })
    return rows


# ================================================================ 删除（默认 dry-run）
def safe_to_delete(dirpath):
    """硬保护：只允许删 `data/<8位日期>/live_*` 这一种路径

    宁可报错也不删 —— 这个函数的唯一职责就是**拦住手滑**：
    `--delete-*` 是唯一会破坏数据的入口，路径判据写松一点就可能把整个 `data/` 带走。
    """
    ap = os.path.abspath(dirpath)
    parent = os.path.dirname(ap)
    gparent = os.path.dirname(parent)
    if os.path.abspath(DATA_ROOT) != gparent:
        return False
    if not DATE_DIR_RE.match(os.path.basename(parent)):
        return False
    return os.path.basename(ap).startswith('live_')


def _dir_stat(dirpath):
    """`(总字节, 文件数)`。走一遍 `os.walk`，量不出来就跳过 —— 不能因此中断清单生成"""
    total = 0
    count = 0
    for dp, _d, files in os.walk(dirpath):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(dp, n))
                count += 1
            except OSError:
                pass
    return total, count


def _dir_bytes(dirpath):
    """场次目录的总字节数（`_dir_stat` 的薄包装，只关心大小时用它）"""
    return _dir_stat(dirpath)[0]


# 断点续传的残留目录名。**它本身就是「这一份没传完」的信号** —— 用
# `--partial-dir` 之后，中断产物落在这里，而不是以正确文件名留在原地被误认为完整文件。
# （早先在本地残留里看到过 `…/.segment_164.wav.4sMx41`，那正是**没有** `--partial-dir`
#   时 rsync 留下的临时文件 —— 目录里出现这种文件，说明那次传输没走完。）
PARTIAL_DIR = '.rsync-partial'


def dir_manifest(dirpath, hash_max_bytes=MANIFEST_HASH_MAX):
    """一个目录的**逐文件清单**（服务端与笔记本**共用这一个实现**）。

    :return: `{'files': [{'p':相对路径, 'n':字节, 'h':sha256|None}, …],  # 按 p 排序
               'count': int, 'total': int, 'digest': str,
               'hash_max_bytes': int, 'partial_dirs': [str, …]}`

    ## 为什么大文件不比 sha256

    语料本体（`room.json` / `danmaku.jsonl` / `events.jsonl` / `series.json*` /
    `fansclub_edges.jsonl` / `session_meta.json`）都很小，哈希几乎免费，而且**它们才是
    研究真正要读的东西**；`audio/*.wav` 与 `frames/*.jpg` 实测 **265 KB/帧**，一场能到
    1.3 GB —— 在正跑着 10 路 ffmpeg 的机器上逐张算哈希是纯浪费。
    传输完整性由 **rsync 协议自己的逐块校验和**保证（`--checksum` 那条注释有详述），
    这一层清单要抓的是 rsync 抓不到的错：**文件集合不对**（漏了一整个子目录、
    多了残留文件、按错了日期/场次目录）。

    ⚠️ **只看总大小不够**，这是本函数存在的全部理由：
    1. 等长方差 —— 两个文件换成一个等总长的，总数不变；
    2. 缺一个 + 多一个 —— 两边字节数可能恰好相等；
    3. 同长度变长 —— 内容被覆盖成同样字节数的错值，size 完全看不出来。
    `count` + 相对路径集合 + 逐文件字节 + 小文件哈希，才把「集合正确」与「内容正确」**分开**验证。
    """
    entries = []
    partial = []
    for dp, dirnames, files in os.walk(dirpath):
        for d in dirnames:
            if d == PARTIAL_DIR:
                partial.append(os.path.relpath(os.path.join(dp, d), dirpath))
        for n in files:
            full = os.path.join(dp, n)
            rel = os.path.relpath(full, dirpath).replace(os.sep, '/')
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            h = None
            if size <= hash_max_bytes:
                h = _file_sha256(full)
            entries.append({'p': rel, 'n': size, 'h': h})
    entries.sort(key=lambda e: e['p'])
    return {
        'files': entries,
        'count': len(entries),
        'total': sum(e['n'] for e in entries),
        'digest': manifest_digest(entries),
        'hash_max_bytes': hash_max_bytes,
        'partial_dirs': sorted(partial),
    }


def _file_sha256(path):
    """文件的 sha256；读不出来返回 None（**不能**返回空串 —— 那会与「算过且为空」混淆）"""
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b''):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def manifest_digest(entries):
    """`[{'p','n','h'}, …]` → 一个摘要字符串（**两侧必须跑同一份代码**才算得一致）"""
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda e: e['p']):
        h.update(('%s\t%d\t%s\n' % (e['p'], e['n'], e.get('h') or '')).encode('utf-8'))
    return h.hexdigest()


def pick_pending(sessions, manual):
    """挑出**待迁移**的场次：有真音视频 **且** 还没标「已迁移」。

    这是 `split_by_media()` 的**补集用法**（那边选没音视频的删掉，这边选有音视频的迁走），
    判据同源就不会出现「删了该留的 / 留了该删的」。

    「已在 xlsx 里标了 `是否已迁移`」的直接排除 —— 用户标完这一格，下一轮 CSV 就少一行，
    不用再手工核对。按「完整优先、体积倒序」排：迁移时从最值钱的开始拷。
    """
    out = []
    for f in sessions:
        if not (f['audio_segs'] or f['frames']):
            continue
        if str(manual.get(f['dir'], {}).get('是否已迁移', '')).strip():
            continue
        out.append(f)
    return sorted(out, key=lambda f: (classify_session(f)[0] != '完整',
                                      -_dir_bytes(f['abspath'])))


def write_usable_csv(sessions, manual, anchors_by_id, path=USABLE_CSV):
    """把「待迁移」清单写成 CSV（用户手动迁移时照它拷）。

    与 xlsx 一样**先写 .tmp 再换入** —— 用户随时可能在 Excel / WPS 里开着它。
    """
    rows = pick_pending(sessions, manual)
    total = 0
    tmp = path + '.tmp'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.writer(fh)
            w.writerow(USABLE_COLS)
            for f in rows:
                size = _dir_bytes(f['abspath'])
                total += size
                completeness, _why, modal = classify_session(f)
                w.writerow([
                    f['douyin_id'], anchors_by_id.get(f['douyin_id'], ''),
                    _fmt(f['started']), _fmt(f['ended']), duration_min(f),
                    f['audio_segs'], f['frames'], modal,
                    completeness, round(size / 1e6, 1),
                    f['dir'], '',
                ])
        os.replace(tmp, path)
    except OSError as e:
        print(f'⚠️ 迁移清单写出失败：{e}（tmp 留在 {tmp}）')
        return None
    print(f'  迁移清单：{out_safe(path)}  {len(rows)} 场，共 {total / 1e9:.2f} GB')
    return path


def out_safe(p):
    try:
        return os.path.relpath(p, ROOT).replace('\\', '/')
    except ValueError:
        return p.replace('\\', '/')


def split_by_media(facts_list):
    """把「不完整」场次按**有没有音视频**分成两堆 —— 删除入口的 fail-closed 闸门

    `不完整` 有三类成因，只有前两类该删（见模块顶部「删除入口」）。含音视频却判不完整的，
    几乎都是**进程被杀**（V8 崩溃 / OOM / 重启）没写成 `room.json`，音视频本体是好的：
    `--delete-incomplete` 默认只吃第一堆，第二堆单独报出来，要删得显式 `--include-media`。

    纯函数（喂合成 dict），单测直接覆盖 —— 这条保护值 2.73 GB 实测数据，不能只靠肉眼看。
    """
    no_media = [f for f in facts_list if not (f['audio_segs'] or f['frames'])]
    with_media = [f for f in facts_list if f['audio_segs'] or f['frames']]
    return no_media, with_media


def append_tombstones(facts_list, label=''):
    """把要删的场次记进墓碑账本（`dist/deleted_sessions.jsonl`）。

    **为什么非记不可**：`find_missed()` 判断「这一轮有没有在采」靠的是**场次目录**，
    而删除入口删的就是场次目录 —— 不记墓碑的话，删完之后那些本来被纯文本场次覆盖的
    时间段会凭空变成「没在采」，`主播` 表长出假漏采、退出码变 1、把
    `systemctl --failed` 这个告警面污染掉。而这恰恰是**设计好的日常动作**
    （`--delete-migrated --apply` 就是排水口），所以不修的话第一次迁移就会中招。

    2026-10-15 实测踩到：删完 281 个场次后 `★疑似漏采` 从 **0 跳到 10**，逐个对
    journald 核对，那 10 个主播当时**都在采**（场次一条不缺），全是删出来的假阳性。

    写在 `rmtree` **之前** —— 中途崩了也比没记强。
    """
    if not facts_list:
        return
    path = TOMBSTONE
    try:
        os.makedirs(DIST, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            for f in facts_list:
                fh.write(json.dumps({
                    'ts': datetime.now().isoformat(timespec='seconds'),
                    'reason': label,
                    'dir': f.get('dir', ''),
                    'douyin_id': f.get('douyin_id', ''),
                    'started': _iso(f.get('started')),
                    'last_write': _iso(f.get('last_write') or f.get('started')),
                }, ensure_ascii=False) + '\n')
    except (OSError, TypeError) as e:
        print(f'⚠️ 墓碑账本写入失败：{e}（下一轮的漏采判定会把它算成漏采）')


def _iso(dt):
    return dt.isoformat(timespec='seconds') if dt else None


def load_tombstones(days=30):
    """读墓碑账本 → `{抖音号: [{'started','last_write'}]}`（喂给覆盖率判定）。

    `days` 给足（默认 30）是故意的：墓碑只是**很轻的一行**（几十字节），而覆盖率的
    历史窗口只有 `--history-days`（默认 3 天）。留宽一点，免得改窗口时对不上。
    """
    path = TOMBSTONE
    cutoff = datetime.now() - timedelta(days=days)
    out = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                try:
                    started = datetime.fromisoformat(rec['started'])
                except (KeyError, TypeError, ValueError):
                    continue
                last = started
                try:
                    last = datetime.fromisoformat(rec['last_write']) or started
                except (KeyError, TypeError, ValueError):
                    pass
                if last < cutoff:
                    continue
                out.setdefault(rec.get('douyin_id', ''), []).append(
                    {'started': started, 'last_write': last})
    except OSError:
        pass
    return out


def facts_public(facts):
    """场次事实 → 可 JSON 化的投影（给笔记本 `--dump-facts` 用）。

    ★ **`completeness/reason/modal` 由服务器用 `classify_session()` 算好塞进来** ——
    笔记本**永远不自己判「完整」**。判据只有这一份实现：台账哨兵、
    `usable_pending.csv`、删除入口、以及现在的自动拉取，全都要同一个口径，
    在第二台机器上重写一份必然漂移（本仓库的口径改过多次）。
    """
    out = []
    for f in facts:
        completeness, reason, modal = classify_session(f)
        total, count = _dir_stat(f['abspath'])
        out.append({
            'dir': f['dir'],                       # 相对 ROOT，正斜杠 —— 删除清单的主键
            'douyin_id': f['douyin_id'],
            'name': f['name'],
            'date': f['date'],
            'started': _fmt(f['started']),
            'ended': _fmt(f['ended']),
            'last_write': _fmt(f['last_write']),
            'inflight': f['inflight'],
            'has_room_json': f['has_room_json'],
            'danmaku_lines': f['danmaku_lines'],
            'event_lines': f['event_lines'],
            'audio_segs': f['audio_segs'],
            'frames': f['frames'],
            'completeness': completeness,
            'reason': reason,
            'modal': modal,
            'bytes': total,
            'files': count,
        })
    return out


def parse_delete_entry(line):
    """删除清单的一行 → 相对场次目录；不合法返回 `None`。

    **复用既有的两个正则**（`DATE_DIR_RE` / `SESSION_DIR_RE`），不另写第三份口径 ——
    `SESSION_DIR_RE` 那个 `.+` 的抖音号本身就允许字母（`live_dyuvnpez772a62_…`）。

    这里挡的是**路径穿越**（`/abs`、`..`、反斜杠），不是 shell 注入 —— 清单经 **stdin**
    传进来，从不拼进命令行。真正的硬保护仍由 `safe_to_delete()` 兜第二道。
    """
    s = (line or '').strip()
    if not s or s.startswith('/') or '\\' in s or '..' in s:
        return None
    parts = s.split('/')
    if len(parts) != 3 or parts[0] != 'data':
        return None
    if not DATE_DIR_RE.match(parts[1]) or not SESSION_DIR_RE.match(parts[2]):
        return None
    return s


def parse_delete_entry_full(line):
    """删除清单的一行 → `(相对场次目录, 期望字节|None, 期望文件数|None)`；不合法 → 三个 `None`。

    一行有**两种写法**（第二种是可选的，为的是关掉 TOCTOU 窗口）：

        data/20261016/live_x                  ← 只声明「删它」（旧格式，行为完全不变）
        data/20261016/live_x\\t1234\\t7        ← 还声明「我拉走时它是 1234 字节 / 7 个文件」

    为什么需要第二种：调用方（笔记本）的数字取自**一轮开始时**的 `--dump-facts`，而
    `--delete-list` 可能几十分钟之后才到 —— 这中间**僵尸 ffmpeg 还能往已「完整」的场次里
    追加**。只凭场次目录判断的话，服务器删掉的是「多了几百兆新帧」的那个目录，而那些帧
    从未被任何副本覆盖过。带上期望列，服务器就能用**自己重扫出来的**数字复核
    （见 `resolve_delete_list`），这是唯一能在那**删除那一刻**关掉窗口的改法。

    格式写错（列数不是 1 或 3、数字解析不了）→ 返回非法。**宁可整批拒，也不静默忽略一列**
    —— 把「本意是带校验的清单」当成无校验的清单处理，正是这里最危险的失败方式。
    """
    s = (line or '').strip()
    if not s:
        return None, None, None
    cols = s.split('\t')
    rel = parse_delete_entry(cols[0])
    if rel is None:
        return None, None, None
    if len(cols) == 1:
        return rel, None, None
    if len(cols) != 3:
        return None, None, None
    try:
        return rel, int(cols[1]), int(cols[2])
    except (TypeError, ValueError):
        return None, None, None


def resolve_delete_list(lines, facts_by_dir, expect_count=0):
    """删除清单 → `(待删 facts, 错误列表)`。

    ★ **fail-closed：`errors` 非空 ⇒ 返回的 facts 必须是空的**（全拒，绝不部分删）。
    清单与实际不符说明「笔记本那边的校验前提已经不作数了」，此时删一半比不删危险得多。

    每行可以只写场次目录，也可以**再带两个期望列**（`\\t字节\\t文件数`，见
    `parse_delete_entry_full`）。带列时本函数会用**自己现扫的** `_dir_stat()` 复核 ——
    这是唯一能在**删除那一刻**关掉 TOCTOU 窗口的地方（调用方的数字取自一轮开始时的
    `--dump-facts`，而删除可能几十分钟后才执行）。

    :param facts_by_dir: `{相对场次目录: fact}`，由 `scan_all_sessions()` 建
    :param expect_count: 笔记本声明的条目数；>0 时对不上即拒（防清单被截断/误编辑）
    """
    errors = []
    seen = []
    dup = set()
    expected = {}          # rel → (期望字节, 期望文件数)；不带期望列的清单不进这里
    for ln, raw in enumerate(lines, 1):
        if not raw.strip():
            # 空行 = 没有条目，**不是**「不合法的一条」。清单是笔记本用
            # `printf '%s\n'` 拼的，末尾/中间的换行很常见；把它判成错误会让每个
            # 空闲轮次都报「清单与实际不符」。删零个永远是安全的，所以这里放宽。
            continue
        rel, exp_bytes, exp_files = parse_delete_entry_full(raw)
        if rel is None:
            errors.append(f'第 {ln} 行不合法（认 `data/<8位日期>/live_*`'
                          f'，可选的期望列写作 `\\t字节\\t文件数`）：{raw!r}')
            continue
        if rel in dup:
            errors.append(f'第 {ln} 行重复：{rel}')
            continue
        dup.add(rel)
        seen.append(rel)
        if exp_bytes is not None:
            expected[rel] = (exp_bytes, exp_files)

    if expect_count > 0 and len(seen) != expect_count:
        errors.append(f'清单条目数不符：收到 {len(seen)} 条，声明 {expect_count} 条')

    facts = []
    for rel in seen:
        f = facts_by_dir.get(rel)
        if f is None:
            # 磁盘上找不到 = 我们对服务器的模型已陈旧（被别处删了/改名了）
            errors.append(f'磁盘上找不到该场次：{rel}')
            continue
        completeness = classify_session(f)[0]
        if completeness == '采集中':
            # 与既有硬保护同一条：在采中的场次永不删
            errors.append(f'该场次正在写入（采集中），拒绝删除：{rel}')
            continue
        if not safe_to_delete(f['abspath']):
            errors.append(f'路径不像生产场次，拒绝删除：{rel}')
            continue
        # ★ TOCTOU 复核（清单带期望列时才做）：清单声明的是**调用方拉走时**的数字，
        #   而删除可能几十分钟之后才执行 —— 这中间僵尸 ffmpeg 还能往这个「已完整」的
        #   目录里追加。用**这里现扫的**数字复核，对不上就整批拒（fail-closed）。
        exp = expected.get(rel)
        if exp is not None:
            now = _dir_stat(f['abspath'])
            if now != exp:
                errors.append(
                    f'场次内容与清单声明不符（清单 {exp[0]} 字节 / {exp[1]} 个文件，'
                    f'现在 {now[0]} 字节 / {now[1]} 个文件）→ 拒绝删除：{rel}')
                continue
        facts.append(f)

    if errors:
        return [], errors
    return facts, []


def delete_sessions(facts_list, apply=False, label=''):
    """删场次目录。`apply=False` 时**只打印**"""
    total = 0
    done = 0
    victims = []
    for f in facts_list:
        if not safe_to_delete(f['abspath']):
            print(f'  ⛔ 拒绝删除（路径不像生产场次）：{f["dir"]}')
            continue
        size = 0
        for dp, _d, files in os.walk(f['abspath']):
            for n in files:
                try:
                    size += os.path.getsize(os.path.join(dp, n))
                except OSError:
                    pass
        total += size
        done += 1            # 只数**真的过了保护**的，被拒的不进总数
        victims.append(f)
        if apply:
            shutil.rmtree(f['abspath'], ignore_errors=True)
            print(f'  🗑 已删 {f["dir"]}（{size / 1e6:.1f} MB）')
        else:
            print(f'  · 将删 {f["dir"]}（{size / 1e6:.1f} MB）[{label}]')
    if apply:
        append_tombstones(victims, label=label)
    verb = '已删除' if apply else '将删除'
    print(f'  {verb} {done} 个场次，共 {total / 1e9:.2f} GB'
          + ('' if done else '（没有可删的）')
          + ('' if apply else '（**干跑**，加 --apply 才真删）'))
    return total


# ================================================================ 机器对机器模式
def _emit(obj, dest):
    """JSON 写到 `dest`；`-` = stdout。写文件时**先写 .tmp 再换入**（同表格）"""
    if dest == '-':
        json.dump(obj, sys.stdout, ensure_ascii=False)
        sys.stdout.write('\n')
        sys.stdout.flush()
        return
    tmp = dest + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, dest)


def _read_lines(path):
    """`-` = stdin，否则读文件。删除清单走 **stdin** 而不是命令行 —— 不落服务器磁盘、
    也不拼进 shell，所以既没有临时文件要清理，也没有注入面。"""
    if path == '-':
        return sys.stdin.read().splitlines()
    with open(path, encoding='utf-8') as fh:
        return fh.read().splitlines()


def machine_main(args):
    """`--dump-facts` / `--manifest` / `--delete-list` 的实现。

    ★ **诊断信息一律走 stderr。** stdout 是给笔记本解析的 JSON 通道，
    多一个字符（一句中文日志）就会让它解析失败 —— 这是本功能最隐蔽的失败点，
    2026-10-16 的约定是：只要走了这三个模式之一，stdout 上就**只能是** JSON。
    `test/test_ledger_unit.py` 有断言钉住。
    """
    if args.dump_facts:
        facts = scan_all_sessions(inflight_min=args.inflight_min)
        data = facts_public(facts)
        _emit(data, args.dump_facts)
        print(f'[dump-facts] {len(data)} 场', file=sys.stderr)
        return 0

    if args.manifest:
        path = args.manifest
        if not os.path.isabs(path):
            path = os.path.join(ROOT, path)
        if not os.path.isdir(path):
            print(f'❌ 目录不存在：{path}', file=sys.stderr)
            return 2
        _emit(dir_manifest(path), '-')
        print(f'[manifest] {path}', file=sys.stderr)
        return 0

    if args.delete_list:
        try:
            lines = _read_lines(args.delete_list)
        except OSError as e:
            print(f'❌ 读不到清单：{e}', file=sys.stderr)
            return 2
        facts = scan_all_sessions(inflight_min=args.inflight_min)
        by_dir = {f['dir']: f for f in facts}
        victims, errors = resolve_delete_list(lines, by_dir, args.expect_count)
        for e in errors:
            print(f'  ❌ {e}', file=sys.stderr)
        if errors:
            # fail-closed：有一条不符就**一条都不删**。理由见 `resolve_delete_list`。
            print(f'❌ 清单与实际不符（{len(errors)} 条）→ **一条都没删**', file=sys.stderr)
            return 2
        if not victims:
            print('清单为空，无可删', file=sys.stderr)
            return 0
        print(f'清单 {len(victims)} 场（墓碑标记 label={args.label}）', file=sys.stderr)
        delete_sessions(victims, apply=args.apply, label=args.label)
        return 0

    return 0


# ================================================================ main
def main():
    ap = argparse.ArgumentParser(description='开播台账：探开播状态 + 场次完整性标注（迁移清单）')
    ap.add_argument('--roster', default=DEFAULT_ROSTER)
    ap.add_argument('--no-probe', action='store_true', help='不探测，只重建表格')
    ap.add_argument('--limit', type=int, default=0, help='只探前 N 个主播（冒烟用，0 = 全部）')
    ap.add_argument('--inflight-min', type=int, default=15,
                    help='目录内最新文件在这个分钟内被写过 → 视为在采中（不判完整性）')
    ap.add_argument('--history-days', type=int, default=3, help='读几天的观测底账')
    ap.add_argument('--matrix-days', type=int, default=MATRIX_SHEET_DAYS,
                    help='开播矩阵出最近几天（每天一张 sheet，行=名单、列=当天各轮）；'
                         '0 = 不生成')
    ap.add_argument('--max-sessions', type=int, default=0, metavar='N',
                    help='守护进程的**会话并发上限**（= auto_crawl 的 --max-sessions），'
                         '用来排除「名额已满所以没在采」的假漏采。0 = 不限（= 改动前口径）')
    ap.add_argument('--xlsx', default=LEDGER_XLSX)
    ap.add_argument('--json-out', default=JSON_OUT)
    ap.add_argument('--usable-csv', default=USABLE_CSV,
                    help='「待迁移」清单 CSV 的路径（默认 dist/usable_pending.csv，'
                         '传空字符串则不生成）')
    ap.add_argument('--delete-incomplete', action='store_true',
                    help='删「不完整」的场次（默认只打印，加 --apply 才真删；'
                         '**默认跳过含音视频的**，见 --include-media）')
    ap.add_argument('--delete-migrated', action='store_true',
                    help='删已在表里标注「已迁移」的场次（默认只打印，加 --apply 才真删）')
    ap.add_argument('--include-media', action='store_true',
                    help='★危险：--delete-incomplete 时连含音视频的场次一起删。'
                         '那些场次判「不完整」多半是进程被杀，内容是真的（实测单场 1.3 GB）')
    ap.add_argument('--apply', action='store_true', help='真的执行删除（不加则干跑）')
    # ---- 下面三个是「机器对机器」模式，供笔记本的 pull_sessions.py 调用 ----
    ap.add_argument('--dump-facts', default='', metavar='PATH',
                    help='把**逐场次事实**输出成 JSON（`-` = stdout），然后直接退出。'
                         '纯读：不探测、不写表格、不写 ledger.json。给自动拉取用')
    ap.add_argument('--manifest', default='', metavar='DIR',
                    help='输出某个场次目录的**逐文件清单** JSON（`-` = stdout）。'
                         '笔记本用它做传输后的逐项校验')
    ap.add_argument('--delete-list', default='', metavar='FILE',
                    help='按显式清单删场次（一行一个 `data/<8位日期>/live_*`；'
                         '`-` = 从 stdin 读）。**清单与实际不符即整体拒绝**。'
                         '默认只打印，加 --apply 才真删')
    ap.add_argument('--expect-count', type=int, default=0,
                    help='配合 --delete-list：声明清单条目数，对不上即拒（防清单被截断）')
    ap.add_argument('--label', default='已传输',
                    help="配合 --delete-list：写进墓碑账本的来源标记（默认「已传输」)"
                         "（人工那条路径用「已迁移」，两者在 deleted_sessions.jsonl 里好区分）")
    args = ap.parse_args()

    # ★ 三个机器模式必须在这里短路 —— 在 `from auto_crawl import load_anchors` **之前**。
    #   它们只要扫盘，**根本不需要名单**；落到后面的话，名单缺失/换路径就会让笔记本的
    #   拉取直接崩，而它跟名单毫无关系。
    if args.dump_facts or args.manifest or args.delete_list:
        return machine_main(args)

    from auto_crawl import load_anchors
    anchors = load_anchors(args.roster)
    anchors_by_id = {str(did): name for name, did in anchors}
    print(f'名单 {len(anchors)} 人（{os.path.basename(args.roster)}）')

    # ---- 1. 探一轮 ----
    probe_ok = probe_total = 0
    if not args.no_probe:
        use = anchors[:args.limit] if args.limit > 0 else anchors
        t0 = datetime.now()
        print(f'探测 {len(use)} 个主播（限流 6 req/s，实测约 {len(use) / 3.1:.0f} 秒）…')
        round_ts, rows = probe_round(use)
        probe_total = len(rows)
        probe_ok = sum(1 for r in rows if r['success'])
        path = append_rounds(rows)
        live_n = sum(1 for r in rows if r['live'])
        print(f'  完成：{probe_ok}/{probe_total} 成功，{live_n} 个在播，'
              f'耗时 {(datetime.now() - t0).total_seconds():.0f}s → {path}')
    rounds = load_rounds(days=args.history_days)

    # ---- 2. 扫场次 + 判定 ----
    now = datetime.now()
    sessions = scan_all_sessions(inflight_min=args.inflight_min, now=now)
    spans = {}
    for f in sessions:
        spans.setdefault(f['douyin_id'], []).append(
            {'started': f['started'], 'last_write': f['last_write'] or f['started']})
    # ★ 把已删（往往是已迁移）的场次也算进覆盖率 —— 否则删除入口会亲手制造假漏采。
    #   见 `append_tombstones()` 的注释：2026-10-15 实测删完 281 个场次后
    #   `★疑似漏采` 从 0 跳到 10，逐个对 journald 核对**全是假阳性**。
    tombstones = load_tombstones()
    n_tomb = 0
    for did, spans_list in tombstones.items():
        spans.setdefault(did, []).extend(spans_list)
        n_tomb += len(spans_list)
    missed, missed_detail = find_missed(rounds, spans, session_cap=args.max_sessions)

    counts = {}
    for f in sessions:
        counts[classify_session(f)[0]] = counts.get(classify_session(f)[0], 0) + 1

    # ---- 2b. 本轮即时漏采（诊断用，不参与告警）----
    # `疑似漏采` 要求连续 ≥2 轮（避免误报），代价是「刚开播就漏」要等一轮才看得出。
    # 这里用**同一张判据**只喂最新一轮，把「此刻探到在播、但没有场次在写」直接报出来 ——
    # 装完修复后立刻要看的正是这个数：它应该等于 0，而修复前实测是 16。
    latest_ts = max((r['ts'] for r in rounds), default=None)
    instant, instant_detail = find_missed(
        [r for r in rounds if r['ts'] == latest_ts], spans, run_min=1,
        session_cap=args.max_sessions)
    live_now = sum(1 for r in rounds if r['ts'] == latest_ts and r['live'])

    # ---- 3. 重建表格（人工列按主键搬回来）----
    manual = read_manual_columns(args.xlsx)
    session_rows = build_session_rows(sessions, anchors_by_id, manual)
    anchor_rows = build_anchor_rows(anchors, sessions, rounds, missed_detail, now)
    # 开播矩阵：**纯读底账**重建（不探测），所以历史某天随时能调出来看。
    # 多读一份 rounds 只在「矩阵要的天数 > 疑似漏采要的天数」时才发生。
    matrix = None
    if args.matrix_days > 0:
        mrounds = (rounds if args.matrix_days == args.history_days
                   else load_rounds(days=args.matrix_days))
        matrix = build_matrix_sheets(anchors, mrounds, days=args.matrix_days, now=now)
    out = write_ledger(session_rows, anchor_rows, path=args.xlsx, matrix=matrix)
    print(f'表格已更新：{out}')
    if matrix:
        print('  开播矩阵：' + '，'.join('%s（%d 轮）' % (n, len(h) - 2) for n, h, _ in matrix)
              + '  ← 列 = 当天各轮，格 = 在播/没播/未知')
    print('  场次分布：' + '，'.join(f'{k} {v}' for k, v in sorted(counts.items())))
    if manual:
        print(f'  保留了 {len(manual)} 行人工标注（{"/".join(MANUAL_COLS)}）')
    if n_tomb:
        print(f'  覆盖率另计入 {n_tomb} 个已删场次（墓碑账本，防止删除入口自己制造假漏采）')
    if args.usable_csv:
        write_usable_csv(sessions, manual, anchors_by_id, path=args.usable_csv)

    # ---- 4. 删除入口 ----
    incomplete = [f for f in sessions if classify_session(f)[0] == '不完整']
    # ★ fail-closed：`不完整` ≠ 「没价值」。含音视频却判不完整的，几乎都是**进程被杀**
    #   （没写成 room.json），音视频本体是好的 —— 2026-10-15 实测 5 场 / 2.73 GB，
    #   其中一场 1.3 GB 是当天最大的。默认跳过，要删得显式 `--include-media`。
    incomplete, with_media = split_by_media(incomplete)
    if args.include_media:
        incomplete = incomplete + with_media          # 显式放开：连音视频一起删
    migrated = [f for f in sessions
                if str(manual.get(f['dir'], {}).get('是否已迁移', '')).strip()]
    if args.delete_incomplete:
        print(f'\n不完整场次 {len(incomplete)} 个（**不含**在采中的）：')
        delete_sessions(incomplete, apply=args.apply, label='不完整')
    if with_media and args.delete_incomplete and not args.include_media:
        msize = 0
        for f in with_media:
            for dp, _d, files in os.walk(f['abspath']):
                for n in files:
                    try:
                        msize += os.path.getsize(os.path.join(dp, n))
                    except OSError:
                        pass
        print(f'  ⛔ **跳过** {len(with_media)} 个含音视频的「不完整」场次（{msize / 1e9:.2f} GB）'
              f' —— 它们多半是进程被杀没写 room.json，音视频本体是完整的。')
        print(f'     确认不要了再加 `--include-media`。最像样的几个：')
        for f in sorted(with_media, key=lambda x: -(x['audio_segs'] + x['frames']))[:5]:
            print(f'       · {f["dir"]}（音频 {f["audio_segs"]} 段 / 帧 {f["frames"]} 张）')
    if args.delete_migrated:
        print(f'\n已标注「已迁移」的场次 {len(migrated)} 个：')
        delete_sessions(migrated, apply=args.apply, label='已迁移')

    # ---- 5. 摘要 + 退出码 ----
    summary = {
        'ts': now.isoformat(timespec='seconds'),
        'probe': {'total': probe_total, 'ok': probe_ok},
        'sessions': len(sessions),
        'by_completeness': counts,
        'incomplete': len(incomplete),
        'incomplete_with_media_skipped': 0 if args.include_media else len(with_media),
        'live_now': live_now,
        'live_now_without_session': sorted(instant),
        'missed_anchors': sorted(missed),
        'session_cap': args.max_sessions,
        'disk_free_gb': round(hc.disk_free_gb(), 1),
    }
    try:
        os.makedirs(DIST, exist_ok=True)
        with open(args.json_out, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f'⚠️ 状态文件写入失败：{e}')

    # 即时漏采：这是**修复的成败判据**（2026-10-15 修复前实测 36 在播 / 16 个没在采）
    if latest_ts:
        cap_note = ''
        if args.max_sessions:
            now_inflight = sum(1 for s in spans.values() for x in s
                               if x['started'] <= latest_ts <= x['last_write'])
            cap_note = (f'（并发上限 {args.max_sessions}，此刻在采 {now_inflight} 个'
                        + ('，**名额已满** → 没轮到的不算漏采）' if now_inflight >= args.max_sessions
                           else '，名额还有空）'))
        print(f'\n本轮（{_fmt(latest_ts)}）探到在播 {live_now} 个，'
              f'其中 **{len(instant)} 个没有场次在写**{cap_note}'
              + ('：' + '、'.join(sorted(instant)) if instant else '（全部在采 ✅）'))
    print(f'★ 疑似漏采（连续 ≥2 轮）{len(missed)} 个主播'
          + ('：' + '、'.join(sorted(missed)[:10]) if missed else '（正常）'))

    if probe_total and probe_ok == 0:
        print('\n❌ 探测通道全失败 —— 隧道/代理已失效（守护进程此刻多半也在空转）')
        return 1
    if missed:
        print('\n❌ 有主播在播却没在采 —— 见表格的「疑似漏采」列')
        return 1
    print('\n✅ 台账已更新')
    return 0


if __name__ == '__main__':
    sys.exit(main())
