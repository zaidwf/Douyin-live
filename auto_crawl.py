#!/usr/bin/python
# coding:utf-8 -*-
"""抖音直播自动全程采集（守护模式：弹幕/事件/商品讲解，可选音视频）

从 xlsx 读取主播列表（抖音号），长期守护运行：
- 轮询检测每个主播是否开播（解析 room_id + 直播间状态）
- 检测到开播 → 自动启动 WebSocket 采集（弹幕/事件/商品 promotion_id）
- 主播下播（ControlMessage status==3）→ 自动停止并落盘（room.json/events.jsonl/...）
- 继续盯守，等主播下一次开播再采，循环往复直到手动停止

**音视频默认关闭**：`--media` 打开后每场额外起 ffmpeg 抽帧 + 切片（走
`liveMan.MediaCapture`，与 `collect_full.py` 同源）。默认关是因为音视频的开销是
**带宽 + 内存**主导、无法线性外推到 100 并发（实测单路：**1 个 ffmpeg、0.023 核 /
162 MB / 6.2 Mbps**；2 核 2G 约 10 路，本机 Wi-Fi 约 5 路），
只适合小规模全模态验证。

用法：
  python auto_crawl.py [xlsx_path] [check_interval_seconds] [--sheet N] [--media] [--max-media N]
  python auto_crawl.py documents/并发测试.xlsx 60 --sheet 40    # 取 40 并发档名单
  python auto_crawl.py anchor.xlsx 60 --media --max-media 5    # 最多 5 路音视频并行
  python auto_crawl.py documents/huitun_10_100w_1015_final.xlsx 60 \
      --media --max-sessions 10 --max-media 10 --min-free-gb 5  # 生产：只并发采 10 个

**并发会话上限（`--max-sessions`，生产用 10；0 = 不限 = 改动前行为）**：进程级
**非阻塞**闸门 —— 「同时在采的主播数」钉在 N，**抢不到名额的主播这一轮完全不采**：
不建场次目录、不连 WS、不落任何数据，只保留开播状态探测（那由 `douyin-ledger` 记录）。
语义与 `--max-media` **相反**：`--max-media` 抢不到名额是**排队等**（等到就采，最坏降级
成纯文本）；`--max-sessions` 抢不到是**直接跳过**。见 `_MediaGate` 的说明。
两者都设成同一个数时 `--max-sessions` 先生效，`--max-media` 就永远不会阻塞了。

**磁盘保护（`--min-free-gb`，默认 5）**：`--media` 时剩余空间低于阈值就**不再起新的
ffmpeg**，那一场降级为纯文本/数值，空间回收后自动恢复。全模态约 4.9 GB/天/路，而文本
每场只占 ~200 KB —— 写满磁盘比丢掉图像/音频模态糟得多（daemon 会开始报错、强杀时留下
残缺场次）。0 = 关闭。
"""
import argparse
import io
import json
import os
import random
import shutil
import signal
import sys
import threading
import time
from datetime import datetime

from liveMan import DouyinLiveWebFetcher, MediaCapture
from data_recorder import DataRecorder, new_date_dir

CHECK_INTERVAL = 60  # 默认开播检测间隔（秒）
MEDIA_MAX_SECONDS = 8 * 3600  # 音视频的防僵尸上限（真正的停止是 stop_media()）

# WS 僵死阈值（秒）：连接活着但**不再下发任何数据**多久算这一场结束。
#
# 为什么必须有这个：下播是**事件驱动**的（`ControlMessage status==3` → `on_offline`），
# 而失效的那一半恰恰不发这个事件 —— TCP 连接还在、抖音不再送数据，于是
# `offline` 永不置位、`ws_thread.is_alive()` 恒为真，线程**永久卡在等待循环里**。
# 后果是双层的：这一场从挂死那刻起不再落盘，**且这个主播永远回不到探测循环，
# 之后每一次开播整场漏掉**。2026-10-15 实测：名单 157 人一轮探到 36 个在播，
# 而守护进程只有 21 个在写 —— 16 个房间（44%）没在采，其中挂死最久的已 90 分钟。
#
# 阈值依据（实测，不是拍的）：健康场次 `events.jsonl` 相邻 `ts` 的**最大**间隔只有
# 2–16 秒（20 个在写场次逐个量），而挂死场次是 1500+ 秒 —— 中间两个数量级余量。
# 判据是「距最后一帧」（见 `DouyinLiveWebFetcher.frame_age`）：心跳的 `need_ack`
# 也走 `_wsOnMessage`，健康连接每 ~5 秒刷新一次，比弹幕频率高得多也更可靠。
STALL_SECONDS = 300

# WS 断线后**连续重试**多少次才放弃本场（`liveMan.next_reconnect_state` 用的是
# 「连续」而不是累计：连接活过 `RECONNECT_GRACE` 就清零，所以「每 10 分钟断一次」
# 的长直播能一直重连下去，只有「一连就挂」才计数耗尽 —— 那通常意味着房间已下播）。
#
# 为什么需要重连：WS 一断，本场就收尾、60 秒后**另开一个场次目录** —— 一场真实直播
# 被切成多个「完整」碎片场次（实测同一主播 30 分钟内 4 个），台账标的「完整」变成
# **碎片完整**，任何按场次聚合的研究特征都会在碎片边界上被切断。重连让
# 「一场直播 = 一个场次目录」重新成立。
#
# 10 是怎么定的：连续 10 次 × `RECONNECT_WAIT` 3 秒 + 握手 ≈ 1~2 分钟就放弃，
# 秒级抖动绰绰有余，而真下播 / 被风控的房间不会拖着一场空转十分钟。
# 0 = 关闭（回到改动前行为：断一次就结束本场）。
WS_RECONNECT = 10

# 「会话名额已满、本轮跳过」这条日志的**每主播**节流间隔（秒）。
# 为什么需要节流：名额只有 N 个，而名单里同时可能有一二十个在播 —— 抢不到名额的那批
# 主播**每轮（60 秒）**都会走到这个分支。不节流的话 157 人名单会被这一行刷屏，
# 把真正要看的信息（开播/下播/异常）淹掉。节流后只在**第一次**和每 30 分钟提醒一次。
SESSION_FULL_LOG_INTERVAL = 1800.0

DATA_ROOT = 'data'  # 与 `new_date_dir()` 的默认 base_dir 一致（相对 cwd；systemd 里 = WorkingDirectory）

# enter 接口探测的**进程级全局限流**（默认限速，`--poll-rate 0` 关闭）。
# 为什么需要：每主播一线程、每轮一次 `get_room_status`（enter 接口），只有随机 jitter
# 「散布」而没有「并发硬上限」—— 627 主播 uniform(0,60) 随机聚类会瞬时突破 enter 接口
# 风控阈值。2026-10-09 实测：~6 req/s 完全干净，~18 req/s 被 444 拦下 85%。
# 注意这是「enter 探测接口」的风控，与 WS 连接（实测 175 并发 444=0）是两回事。
DEFAULT_POLL_RATE = 6.0  # 每秒最多打几个 enter 探测请求（保守安全值，按 IP 体检可上调）


# 表头候选名：不同名单文件措辞不同，按名字定位列而不是写死列号。
# 仓库里有两份结构不同的名单：
#   anchor.xlsx             10 列：分层 | 博主 | 9月粉丝 | … | 抖音号 | 直播间链接
#   documents/并发测试.xlsx   5 列：序号 | 昵称 | 抖音号 | 粉丝数 | 主要类目
# 旧实现硬编码 `r[8]`（第 9 列）取抖音号，对后者会**静默返回 0 个主播**（5 列文件里
# r[8] 恒为 None），排查起来很费时间 —— 故改为表头驱动，找不到列就明确报错。
ID_HEADER_CANDIDATES = ('抖音号', '抖音id', '抖音 ID', '账号', 'live_id')
NAME_HEADER_CANDIDATES = ('昵称', '博主', '博主名', '主播', '主播名', '名字', 'name')


def _norm_cell(v):
    """单元格值归一化为去空白的字符串（None → ''）"""
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _find_col(header, candidates):
    """在表头里找候选列名，返回列索引；找不到返回 None（做子串匹配以容忍空格/前缀）"""
    for i, h in enumerate(header):
        hs = _norm_cell(h).lower()
        if not hs:
            continue
        for cand in candidates:
            c = cand.lower()
            if hs == c or c in hs:
                return i
    return None


def load_anchors(xlsx_path, sheet=None):
    """读取主播名单，返回 [(name, douyin_id)]

    **按表头名定位列**，不写死列号（见 ID_HEADER_CANDIDATES 的说明）。

    :param sheet: sheet 名，如 `'40'` 取 `并发测试.xlsx` 的 40 并发档名单；
                  默认取第一个 sheet。分档 sheet 与 sheet1 列布局相同但没有表头行，
                  此时用 `sheet1` 解析列位置，并自动跳过目标 sheet 的首行（若它是表头）。
    :raises ValueError: 找不到抖音号列或昵称列（明确报错，不静默返回空列表）
    """
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)

    # 表头来源：优先 sheet1（并发测试.xlsx 的分档表没有表头），否则用目标/活动 sheet
    header_ws = wb['sheet1'] if 'sheet1' in wb.sheetnames else (
        wb[str(sheet)] if sheet is not None and str(sheet) in wb.sheetnames else wb.active)
    header = [c for c in next(header_ws.iter_rows(min_row=1, max_row=1, values_only=True))]

    id_idx = _find_col(header, ID_HEADER_CANDIDATES)
    name_idx = _find_col(header, NAME_HEADER_CANDIDATES)
    if id_idx is None:
        raise ValueError(f"{xlsx_path} 的表头 {header} 里找不到抖音号列"
                         f"（候选：{ID_HEADER_CANDIDATES}）")
    if name_idx is None:
        raise ValueError(f"{xlsx_path} 的表头 {header} 里找不到昵称/博主列"
                         f"（候选：{NAME_HEADER_CANDIDATES}）")

    ws = wb[str(sheet)] if sheet is not None else wb.active
    rows = list(ws.iter_rows(values_only=True))
    # 首行是否表头：抖音号列的值命中候选名就是表头（分档 sheet 首行即数据，需保留）
    start = 1 if rows and _norm_cell(rows[0][id_idx]).lower() in \
        [c.lower() for c in ID_HEADER_CANDIDATES] else 0

    anchors = []
    for r in rows[start:]:
        if not r or all(c is None or str(c).strip() == '' for c in r):
            continue
        name = _norm_cell(r[name_idx]) if name_idx < len(r) else ''
        douyin_id = _norm_cell(r[id_idx]) if id_idx < len(r) else ''
        # 抖音号可能是纯数字（int/float）或字母数字串，_norm_cell 已统一处理
        if name and douyin_id and douyin_id.lower() != 'none':
            anchors.append((name, douyin_id))
    return anchors


class _RateLimiter:
    """进程级令牌桶：enter 接口探测限流（见 DEFAULT_POLL_RATE 注释）

    所有主播线程共享一个实例，`acquire()` 阻塞到「允许发下一个请求」为止。
    令牌以 `rate`/秒 匀速补充，`capacity` 是允许的瞬时突发上限（默认 = rate，
    即最多一个「秒配额」的请求可同时放行）。

    `acquire(stop_event)`：stop_event 置位后**提前返回 False**（不阻塞），
    让 `_one_cycle` 在优雅退出时能立刻放弃本轮，而不是卡在限流队列里。
    """

    def __init__(self, rate, capacity=None):
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate))
        self.tokens = self.capacity
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def acquire(self, stop_event=None):
        """拿到一个令牌（可发一个请求）返回 True；stop_event 置位则返回 False"""
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                wait = (1.0 - self.tokens) / self.rate
            # 锁外睡，别的线程也能进来补令牌/取令牌；最多睡 0.5s 以保证对 stop_event 响应快
            time.sleep(max(0.02, min(wait, 0.5)))


class _MediaGate:
    """进程级并发闸门（一个带超时的信号量）：名单可以任意大，同时在跑的场次/ffmpeg 钉在 N

    两个用途，**语义相反，差别全在调用方**（本类只提供信号量，不含策略）：
      * **媒体闸门**（`--max-media`）：抢不到要**排队等** → 走 `_wait_media_slot()`
      * **会话闸门**（`--max-sessions`）：抢不到要**跳过本轮** → `timeout=0` 试一次就返回，
        这一年该主播**完全不采**（不建目录、不连 WS），只保留开播状态探测

    为什么需要：`AnchorDaemon` 是「一主播一线程」且**一路采集占满该线程全程**，
    名单里有几个在播就同时起几路 —— 157 人名单开 `--media` 会按名单起满
    （`modify.md` 里 141 在播直接崩就是这个缺口）。

    **媒体闸门的语义是「排队等待」，不是跳过。** 跳过会让排到后面的场次静默降级成纯文本，
    而守护进程的目标恰恰是「每场都采全」。闸门打满时日志会明说，据此决定
    「这台机加人还是加机器」，比悄悄丢模态好。
    （**会话闸门**反过来 —— 名额是有意稀缺的「只采 10 个」，抢不到就跳过；
    两种语义各自的理由都写在调用点上，改之前先读。）

    ⚠️ **「等」的策略不在这里，在 `AnchorDaemon._wait_media_slot()`。** 本类只是
    一个带超时的信号量；**裸的 `acquire(stop_event)` 是错的** —— 它会一直等到拿到
    名额为止，而调用它的场次可能早就下播了（详见 `_wait_media_slot` 的说明：
    2026-10-15 实测有 51 个僵尸线程堵在这里，吃掉了 59% 的名额）。
    把「以本场结束为界」这条策略放在调用方，是因为只有调用方拿得到 `offline`
    与 `ws_thread` 这两个「本场还活着吗」的判据。

    `acquire(timeout=...)` 用**分片 wait**：既不忙等，也保证对 stop_event 响应快。
    """

    def __init__(self, n):
        self.n = int(n)
        self._sem = threading.BoundedSemaphore(self.n)

    def acquire(self, stop_event=None, timeout=0.5):
        """**试一次**：拿到名额返回 True；`timeout` 秒内没拿到、或 stop_event 已置位 → False

        ⚠️ 与 `_RateLimiter.acquire` 的语义**不同**：这里返回 False **不等于**「进程要退出」，
        它只表示「这一轮没排到，稍后再试」。原来把两件事塞进同一个返回值（False 只能来自
        stop_event，于是内部 while 一直转到拿到为止）—— 那正是僵尸名额的成因：
        调用方无从在等待中途放弃。现在拆开了，由 `_wait_media_slot` 负责重试与放弃。

        调用方返回 False 时**没有**拿到名额，不要再调 `release()`。
        """
        if stop_event is not None and stop_event.is_set():
            return False
        return self._sem.acquire(timeout=timeout)

    def release(self):
        self._sem.release()


class _DiskGuard:
    """音视频的磁盘闸门：剩余空间低于阈值时**不再起新的 ffmpeg**（本场降级为纯文本/数值）。

    为什么需要：**全模态是磁盘大头，文本不是**。2026-10-14 服务器实测单路一场 600s =
    **34 MB**（帧 14 MB + 音频 20 MB）→ 约 **4.9 GB/天/路**，`--max-media 5` 就是 ~24 GB/天；
    而**文本+数值每场只占 ~200 KB**（1012 KB / 5 场）。所以空间告急时降级到纯文本几乎不损失
    什么，却能让守护进程无限期活下去 —— 反过来，磁盘写满会让整个 daemon 开始报错、
    并在进程被杀时留下半截场次。

    **只拦「起新媒体」，不打断已在采的场次**：一场最多再涨几百 MB，从「刚好达标」起也吃不穿
    阈值。中途掐断反而会把一场好数据切成残缺场次。

    **度量失败时放行（fail-open）**：`disk_usage` 抛异常（路径诡异、挂载点消失）就当作「够用」，
    只打一条告警。理由是「因为量不出来而永久停采音视频」比「磁盘可能被写满」更难发现
    —— 前者只有日志里一行字，后者至少会真的报错。宁可采，不可因度量失败静默降级。
    """

    def __init__(self, min_free_gb, path=DATA_ROOT, log_interval=1800):
        self.min_free_gb = float(min_free_gb)
        self.path = path
        self.log_interval = float(log_interval)   # 持续不足时，最多这么久提醒一次（防刷屏）
        self._last_log = 0.0
        self._warned_unmeasurable = False

    def free_gb(self):
        """路径所在文件系统的剩余 GB；路径还不存在就向上找到最近的存在祖先。拿不到返回 None"""
        p = os.path.abspath(self.path)
        while not os.path.exists(p):
            parent = os.path.dirname(p)
            if parent == p:      # 到根了还是不存在 —— 交给 disk_usage 去抛
                break
            p = parent
        try:
            return shutil.disk_usage(p).free / (1 << 30)
        except OSError:
            return None

    def check(self):
        """-> (是否允许起新媒体, 剩余 GB 或 None)"""
        free = self.free_gb()
        if free is None:
            if not self._warned_unmeasurable:
                self._warned_unmeasurable = True
                print(f"⚠️ 磁盘剩余空间量不出来（{self.path}）—— **放行**音视频，请手工 df 确认")
            return True, free
        self._warned_unmeasurable = False
        return free >= self.min_free_gb, free

    def should_report_block(self):
        """受阻提示的**节流器**：状态恢复为「够用」后立刻重新可报（见 check 的调用方）"""
        now = time.monotonic()
        if self._last_log == 0.0 or now - self._last_log >= self.log_interval:
            self._last_log = now
            return True
        return False

    def reset_report(self):
        self._last_log = 0.0     # 空间恢复后再受阻 → 立刻报，不用等节流窗口


class AnchorDaemon(threading.Thread):
    """单个主播的守护线程：检测开播 → 全程采集 → 落盘 → 循环"""

    def __init__(self, name, douyin_id, check_interval=CHECK_INTERVAL, media=False,
                 limiter=None, media_gate=None, disk_guard=None,
                 stall_seconds=STALL_SECONDS, session_gate=None,
                 ws_reconnect=WS_RECONNECT):
        super().__init__(name=name)  # 非 daemon：保证优雅退出时 close 完成
        self.anchor_name = name
        self.douyin_id = douyin_id
        self.check_interval = check_interval
        self.media = media          # 是否采音视频（默认关，见模块 docstring）
        self.limiter = limiter      # 进程级 enter 探测限流器（None = 不限流）
        self.media_gate = media_gate  # 进程级音视频并发闸门（None = 不限，即改动前行为）
        self.disk_guard = disk_guard  # 进程级磁盘闸门（None = 不检查，即改动前行为）
        # 进程级**会话**并发闸门（None = 不限，即改动前行为）。与 media_gate 的区别见
        # `_MediaGate` 的 docstring：这个抢不到名额是**跳过本轮**，不是排队等。
        self.session_gate = session_gate
        # WS 僵死阈值（秒），0 = 关闭该判据（回到改动前行为，见 STALL_SECONDS）
        self.stall_seconds = stall_seconds
        # WS 断线连续重连上限，0 = 关闭（回到改动前行为：断一次就结束本场，见 WS_RECONNECT）
        self.ws_reconnect = ws_reconnect
        self._stop_event = threading.Event()
        self._last_full_log = 0.0   # 「名额已满」日志的节流时间戳（见 _note_session_full）

    def stop(self):
        self._stop_event.set()

    def _stalled(self, fetcher):
        """本场的 WS 是否已僵死 —— 连接活着但**不再下发任何数据**。

        抽成独立方法是为了**可单测**（内联在等待循环里没法喂合成值，见
        `test/test_stall_watchdog_unit.py`）。判据是「距最后一帧」而不是「距最后一条
        弹幕」：心跳的 `need_ack` 也走 `_wsOnMessage`，健康连接每 ~5 秒刷新一次。

        `stall_seconds = 0` 表示关闭检测 → 恒返回 False（回到改动前行为）。
        """
        return 0 < self.stall_seconds <= fetcher.frame_age()

    def _media_allowed(self):
        """起 ffmpeg 前的磁盘检查：不够就不起，本场降级为纯文本/数值。

        与闸门一样卡在「已确认开播、即将起 ffmpeg」这一刻 —— 检测开播的开销与磁盘无关。
        """
        if self.disk_guard is None:
            return True
        ok, free = self.disk_guard.check()
        if ok:
            self.disk_guard.reset_report()
            return True
        if self.disk_guard.should_report_block():
            shown = '无法测量' if free is None else f'{free:.1f} GB'
            print(f"[{self.anchor_name}] ⚠️ 磁盘剩余 {shown} < 阈值 "
                  f"{self.disk_guard.min_free_gb:.1f} GB —— 本场**只采文本/数值**"
                  f"（音视频暂停，空间回收后自动恢复）")
        return False

    def _note_session_full(self):
        """会话名额抢不到时的日志（**节流**到每 `SESSION_FULL_LOG_INTERVAL` 秒一条）。

        为什么不无条件打：名额只有 N 个，名单里同时可能有一二十个在播 —— 抢不到的那批
        主播**每轮（默认 60 秒）都会走到这里**。157 人名单不节流就是每分钟一行刷屏，
        把开播/下播/异常这些真正要看的信息淹掉。

        为什么还要打而不是静默：名额长期占满 = 「要么加机器、要么调小名单」这个结论
        必须能从日志里看出来，否则「为什么这个主播一直没数据」会变成考古题。
        """
        now = time.time()
        if now - self._last_full_log < SESSION_FULL_LOG_INTERVAL:
            return
        first = self._last_full_log == 0.0
        self._last_full_log = now
        cap = self.session_gate.n if self.session_gate is not None else '?'
        print(f"[{self.anchor_name}] 会话名额已满（{cap} 路）—— "
              f"{'开始' if first else '仍在'}跳过本轮（只记开播状态，本轮不落任何数据）")

    def _wait_media_slot(self, offline, ws_thread, log_every=300.0):
        """等一个音视频名额，**以本场结束为界**。返回 `(是否拿到, 等待秒数)`。

        ★ 这里必须是「盯着本场等」，不能是裸的 `gate.acquire(stop_event)`。原因：

        走到这一步时**本场早就开始了** —— WebSocket 在跑、`DataRecorder` 在落盘、
        `offline` 事件也挂好了。也就是说「等名额」的这段时间里房间随时可能下播。
        原来的实现直接阻塞在 `acquire()` 上，于是：

          1. 房间下播了它**不知道** —— 既不退出、也不 `recorder.close()`，
             线程连同它的 WebSocket、句柄、内存一起挂在那儿；
          2. 几个小时后名额终于轮到自己，它去给一个**早已下播的房间**起 ffmpeg，
             `_resolve_stream_url()` 拿到废弃地址，ffmpeg 写出一个 78 字节的空 WAV 头
             就退出 —— **一个名额就这么喂给了僵尸会话**；
          3. 真正在播、本该拿到名额的房间排在它后面，继续等。

        2026-10-15 实测：重启那一刻有 **51 个线程**同时堵在闸门里，而同时在播的
        只有 ~22 个房间 —— 多出来的全是这种僵尸。当天拿到名额的 51 场里有 30 场
        （59%）产出为 0，`audio/` 下只有一个 78 字节的空壳。

        现在改成 1 秒一跳地重试，每跳都检查「本场还在吗」：房间下播 / WS 断了 /
        进程要退出，都立刻放弃名额回到正常收尾路径。**名额只会发给还在播的场次。**

        :param offline:   本场的下播事件（`fetcher.on_offline`）
        :param ws_thread: 本场的 WebSocket 线程（死了说明连接断了，场次同样结束）
        :param log_every: 排队中多久提醒一次（秒）。这个提示是**运维信号** ——
                          持续刷说明名额长期不够，该加机器/加名额了
        :return: `(是否拿到名额, 等待秒数)`。等待秒数进 `session_meta.json` ——
                 「这场为什么没有音视频」要靠它区分「没轮到名额」和「名额够了但启动失败」
        """
        t0 = time.monotonic()
        last_log = t0
        while True:
            if self.media_gate.acquire(timeout=1.0):
                waited = time.monotonic() - t0
                if waited >= 60:
                    print(f"[{self.anchor_name}] 排到音视频名额（等了 {waited / 60:.1f} 分钟）")
                return True, waited
            if self._stop_event.is_set() or offline.is_set() or not ws_thread.is_alive():
                waited = time.monotonic() - t0
                if waited >= 60:
                    print(f"[{self.anchor_name}] 本场已结束，放弃等待音视频名额"
                          f"（共等 {waited / 60:.1f} 分钟，本场只有文本/数值）")
                return False, waited
            if time.monotonic() - last_log >= log_every:
                last_log = time.monotonic()
                print(f"[{self.anchor_name}] 音视频名额已满，已等 "
                      f"{(time.monotonic() - t0) / 60:.1f} 分钟"
                      f"（本场先采文本/数值，排到名额后自动补采音视频）")

    def run(self):
        # 随机初始延迟：避免多个线程同时首次请求，触发抖音风控（HTTP 444）
        init_delay = random.uniform(0, self.check_interval)
        print(f"[{self.anchor_name}] 守护启动（抖音号 {self.douyin_id}，检测间隔 {self.check_interval}s，初始延迟 {init_delay:.1f}s）")
        self._stop_event.wait(init_delay)
        while not self._stop_event.is_set():
            try:
                self._one_cycle()
            except Exception as e:
                print(f"[{self.anchor_name}] 循环异常: {e}")
            # 加随机抖动，避免所有线程同步轮询造成突发并发
            jitter = random.uniform(0.5, 1.5)
            self._stop_event.wait(self.check_interval * jitter)

    def _one_cycle(self):
        """一轮检测：若开播则采集到自然下播"""
        fetcher = DouyinLiveWebFetcher(self.douyin_id, None)

        # 1. 确认直播状态 + 主播信息。**这一步顺带完成 room_id 解析** ——
        #    `get_room_status` 走 `/webcast/room/web/enter/`，`room_id_str` 传空串照样
        #    返回完整数据，并会把真 room_id 缓存回 `fetcher.room_id`（见 liveMan）。
        #
        #    原实现在这里**先单独调一次 `fetcher.room_id`**，等于每轮多打一次 HTTP：
        #    旧代码那个调用会下载 1.1MB 整页 HTML 去正则抠 id，删不掉；改成 `_enter_api`
        #    之后它才变成纯冗余。轮询是**每主播每轮**都跑的，这一半在 1684 人名单上
        #    就是 ~28 req/s 的差别 —— 风控看的是请求数，不是字节数。
        try:
            # enter 探测全局限流：先排队领令牌再打请求。stop_event 置位则放弃本轮
            # （优雅退出时不卡在限流队列里，见 _RateLimiter.acquire）
            if self.limiter is not None and not self.limiter.acquire(self._stop_event):
                return
            success, status, nickname, user_id = fetcher.get_room_status()
        except Exception:
            return
        if not success or status != '正在直播':
            return

        # 2. 取 room_id（正常流程已由 get_room_status 缓存，这里只是防空值进下游）
        rid = fetcher.room_id
        if not rid:
            return

        # ★ 会话并发闸门（`--max-sessions`，**非阻塞**）：抢不到名额就**这一轮完全不采**。
        #   和媒体闸门（`--max-media`）的区别是语义 —— 那个抢不到是「排队等」（等到就采，
        #   最坏降级成纯文本），这个抢不到是「直接跳过」：不建场次目录、不连 WS、不落任何
        #   数据，只保留开播状态探测（那由 `douyin-ledger` 每 10 分钟记一次）。
        #
        #   ⚠️ 必须卡在**确认开播之后**：卡在探测之前会让没开播的主播也占着名额。
        #   ⚠️ 必须卡在 `DataRecorder(...)` **之前**：否则目录已经建出来了，
        #   「完全不采」就变成「采一个空目录」。
        session_taken = False
        if self.session_gate is not None:
            session_taken = self.session_gate.acquire(self._stop_event, timeout=0)
            if not session_taken:
                # ⚠️ `stop_event` 置位时 acquire 也返回 False（见 _MediaGate.acquire）——
                #    那不是「名额满」，打「名额已满」会误导（systemctl stop 时每轮都走这里）。
                if not self._stop_event.is_set():
                    self._note_session_full()
                return
        try:
            start_ts = datetime.now()
            print(f"[{self.anchor_name}] {start_ts.strftime('%H:%M:%S')} 检测到开播：{nickname}（{user_id}），开始全程采集")

            # 3. 启动采集（WebSocket 弹幕 + 结构化落盘）
            offline = threading.Event()
            fetcher.on_offline = offline.set

            # 落盘目录：`data/<当天日期>/`（见 data_recorder.new_date_dir）。在**确认开播之后**
            # 才解析 —— 若在检测前就解析，一次没人开播的轮询也会建出一个空目录，反而更乱。
            # 日期在这一刻定死：本场跨零点也继续写同一目录，直到下播，不清场不换文件夹。
            recorder = DataRecorder(rid, self.douyin_id, base_dir=new_date_dir())
            room_info = dict(fetcher.room_detail)  # enter API 富字段（头像/简介/粉丝数/作品数等）
            room_info.update({
                'title': nickname, 'anchor_nickname': nickname,
                'anchor_id': user_id, 'room_status': status,
            })
            recorder.set_room_info(room_info)
            fetcher.data_callback = recorder.on_event

            # ★ WS 断线**自动重连**（见 WS_RECONNECT）：断线时不再结束本场，而是在
            #   **同一个场次目录**里重开一条连接 —— 于是 `ws_thread` 在整个场次期间恒活，
            #   下面拿 `ws_thread.is_alive()` 当「本场还在采吗」的判据（含 `_wait_media_slot`）
            #   自动继续成立，一行都不用改。传 0 时 `start()` 与改动前逐字节一致。
            ws_thread = threading.Thread(
                target=fetcher.start, daemon=True,
                kwargs={'max_reconnects': self.ws_reconnect})
            ws_thread.start()

            # 3.5 音视频（默认关）：与 collect_full 共用 MediaCapture，
            # 区别只在**谁调 stop_media()** —— 这里挂到「下播」这一自然终点。
            #
            # 闸门卡在**这一刻**（已确认开播、即将起 ffmpeg），不是卡在 _one_cycle 入口：
            # 卡入口会把「检测开播」也一并阻塞，而 enter 探测的开销与媒体完全无关。
            capture = None
            gate_taken = False
            media = None
            # 本场为什么没有音视频 / 为什么结束 —— 两个都进 session_meta.json（见 _write_session_meta）。
            # 「无音视频」的原因必须细分：名额没排到（设计内的排队）、磁盘降级（设计内的保护）、
            # 启动失败（真故障）、压根没开（配置）。混成一个「没有音视频」会让真故障淹没在噪声里。
            media_skip_reason = 'disabled' if not self.media else None
            gate_wait_s = 0.0
            stop_reason = 'signal'
            try:
                # 磁盘检查在**取闸门之前**：空间不足时不该白占一个名额（占着等于把别人也堵住）。
                # 两道闸门语义不同 —— 并发闸门是「排队等」（阻塞），磁盘闸门是「直接不起」（降级）。
                if self.media and self._media_allowed():
                    if self.media_gate is None:
                        gate_taken = True      # 无闸门 = 不限流，等价于「已获名额」
                    else:
                        # ★ 等名额**以本场结束为界**（见 _wait_media_slot 的说明）——
                        #   裸的 gate.acquire() 会把名额喂给早已下播的僵尸会话。
                        gate_taken, gate_wait_s = self._wait_media_slot(offline, ws_thread)
                        if not gate_taken:
                            media_skip_reason = 'gate_no_slot'
                    if gate_taken:
                        # ⚠️ 先存到临时变量 `cap`，**成功才赋给 `capture`**。直接写
                        #    `capture = MediaCapture(...)` 再判 `start_media()` 失败的话，
                        #    `capture` 仍是非 None，下面的 `stop_media()` 照样会调 ——
                        #    于是一次「启动失败」会被打印成「音视频：0 帧 / 0 段音频」，
                        #    看起来像一次成功的空采集（实测 2026-10-15 有 14 场如此）。
                        cap = MediaCapture(self.douyin_id, recorder.path, fetcher=fetcher,
                                           max_seconds=MEDIA_MAX_SECONDS)
                        if cap.start_media():
                            capture = cap
                            media_skip_reason = None
                        else:
                            print(f"[{self.anchor_name}] ⚠️ 音视频启动失败，本场只采文本/数值")
                            media_skip_reason = 'start_failed'
                            # 没起成就没占着 ffmpeg，立刻还名额 —— 否则队列会白等一整场
                            if self.media_gate is not None:
                                self.media_gate.release()
                            gate_taken = False
                elif self.media:
                    media_skip_reason = 'disk_low'

                # 4. 等待下播（ControlMessage status==3 触发 on_offline）
                # 用 1s 短跳而不是直接等满 check_interval：**下播本身是事件驱动的**
                # （`offline.is_set()` 立刻返回），这个 timeout 只决定「多久查一次 `_stop_event`」。
                # 等满 check_interval(60s) 会让 `systemctl stop` 白等最多 60s —— 而 systemd 默认
                # `TimeoutStopSec=90s`，一旦超时就直接 SIGKILL，当前场次照样保不住。
                stop_reason = 'offline'
                while not offline.is_set() and not self._stop_event.is_set():
                    if not ws_thread.is_alive():
                        stop_reason = 'ws_closed'   # ws 意外断开（网络问题），也结束本场
                        break
                    # ★ WS 僵死：连接活着但**不再下发任何数据**（见 STALL_SECONDS 的实测依据）。
                    #   不判这个的话线程会永远卡在本循环里 —— 这一场不再落盘，而且**这个主播
                    #   之后每次开播全部漏采**（2026-10-15 实测 44% 的在播房间是这么丢的）。
                    #   当作本场结束处理 = 断线重连：收尾后回到探测循环，房间若仍在播就新开一场。
                    if self._stalled(fetcher):
                        stop_reason = 'stall'
                        print(f"[{self.anchor_name}] ⚠️ WS 僵死（{fetcher.frame_age() / 60:.1f} 分钟"
                              f"没有收到任何数据）—— 结束本场并重连")
                        break
                    offline.wait(timeout=1.0)
                if self._stop_event.is_set():
                    stop_reason = 'signal'      # systemctl stop/restart，正常退出路径

                # 5. 停止音视频（先于关 recorder，避免 ffmpeg 还在往目录里写）
                if capture is not None:
                    media = capture.stop_media()
            finally:
                # ⚠️ **必须在 `stop_media()` 之后**才还名额：ffmpeg 是在 stop_media 里才收掉的，
                # 提前 release 会让下一路在上一路 ffmpeg 还没退时就起来，闸门形同虚设。
                # 用 finally 而不是顺序执行：`start_media` / 等待下播 / `stop_media` 任一处抛异常，
                # 顺序执行会**永久漏掉一个名额** —— 5 个名额漏光后这个进程就再也不采音视频了。
                if gate_taken:
                    self.media_gate.release()

            fetcher.stop()
            ws_thread.join(timeout=5)  # 等 WS 线程退出，否则 close 后仍可能有回调写入
            recorder.set_promotion_order(fetcher.product_refresh_order)
            recorder.close()
            if media:
                if media['ok']:
                    print(f"[{self.anchor_name}] 音视频：{len(media['frames'])} 帧 / "
                          f"{len(media['audio'])} 段音频"
                          + (f"，续采 {media['restarts']} 次" if media['restarts'] else "")
                          + (f"（其中停滞强杀 {media['stalls']} 次）"
                             if media.get('stalls') else ""))
                else:
                    # 起了 ffmpeg 却一段有效产物都没有：**不要把这种情况报成一次空采集**，
                    # 它和「本场没轮到名额」是两回事，混在一起会把真正的故障淹掉。
                    print(f"[{self.anchor_name}] ⚠️ 音视频**未产出任何有效数据**"
                          f"（0 帧 / 0 段，续采 {media['restarts']} 次）—— 本场只有文本/数值")
            dur = (datetime.now() - start_ts).total_seconds()
            print(f"[{self.anchor_name}] 本场结束（时长 {dur / 60:.1f} 分钟），数据落盘：{recorder.path}")
            self._write_session_meta(recorder.path, start_ts, dur, stop_reason,
                                     media_skip_reason, gate_wait_s, media, fetcher=fetcher)
        finally:
            # 名额在**整场收尾之后**才还：release 早了，下一个主播会在这一场还在落盘时
            # 就起来，闸门就名不副实。`run()` 会吞掉 `_one_cycle` 的异常继续下一轮，
            # 所以这里漏还一个名额就是**永久少一路**（10 个漏光就再也不采了）。
            if session_taken:
                self.session_gate.release()

    def _write_session_meta(self, path, start_ts, dur, stop_reason,
                            media_skip_reason, gate_wait_s, media, fetcher=None):
        """把「这场为什么是这样」写进场次目录，让数据**自描述**。

        为什么值得单独落一个文件：2026-10-15 排查「为什么没有音视频」花了几小时，
        根因信息全都只存在于 journald 里 —— 而 journald 会轮转，几天后回看一场旧数据
        就再也没法区分「当时是名额没排到」「磁盘降级了」还是「音视频真坏了」。
        写进目录里之后，`service/live_ledger.py` 只需读目录就能判定完整性，不必去解析日志。

        **写失败不能影响主流程**：这是辅助产物，一场已经采完的数据不能因为这个小文件
        写不进去（磁盘满、权限）而被当成失败。
        """
        meta = {
            'started_at': start_ts.isoformat(timespec='seconds'),
            'ended_at': datetime.now().isoformat(timespec='seconds'),
            'duration_s': round(dur, 1),
            'stop_reason': stop_reason,
            # WS 断线重连统计（见 `liveMan.DouyinLiveWebFetcher.start`）。
            # 为什么必须落进目录：重连是**静默**的 —— 不记的话，「这场为什么比上一场
            # 少了 3 分钟数据」就只能去翻 journald，而日志会轮转掉。
            # ⚠️ `ws_gap_s` 是**我们主动退避等待**的累计秒数，**不是**「这场丢了多少
            #    数据」：断线发生在 `run_forever` 返回之前多久是测不出来的（socket 早已
            #    死，`run_forever` 可能过几秒才返回），只能测我们自己的等待。
            # ⚠️ `ws_reconnects == 0` 是常态（WS 直连后实测 0 次 `ws_closed`）——
            #    它一旦持续 > 0，说明出现了**新的断因**，是告警信号而不是噪声。
            'ws_reconnects': int(getattr(fetcher, 'reconnect_count', 0) or 0),
            'ws_gap_s': round(float(getattr(fetcher, 'reconnect_gap_s', 0.0) or 0.0), 1),
            'media': {
                'enabled': bool(self.media),
                'ok': bool(media and media['ok']),
                'frames': len(media['frames']) if media else 0,
                'audio': len(media['audio']) if media else 0,
                'restarts': media['restarts'] if media else 0,
                # 「进程还活着、但产物计数 N 秒没涨」→ 强杀重拉的次数（见
                # `liveMan.MEDIA_STALL_SECONDS`）。与 restarts 分开记：`restarts` 里
                # 混着「进程正常早退」，而停滞是**另一种故障**（2026-10-17 实测的
                # 8 小时 CDN 停推），混着看会把真故障淹在噪声里。
                'stalls': media.get('stalls', 0) if media else 0,
                # None = 本场确实起了音视频且在产出；否则是**没产出的原因**
                'skip_reason': media_skip_reason,
                'gate_wait_s': round(gate_wait_s, 1),
            },
        }
        try:
            with open(os.path.join(path, 'session_meta.json'), 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"[{self.anchor_name}] ⚠️ session_meta.json 写入失败（不影响本场数据）：{e}")


def _install_signal_handlers(stop_event):
    """把 SIGINT / SIGTERM 都转成「置位 + 走同一条退出路径」

    ★ **必须处理 SIGTERM**：`systemd` 的 `systemctl stop` / `restart` 发的是 SIGTERM，
      而 Python 的默认动作是**直接终止进程**（C 层 exit，不做任何清理）。原来只写
      `except KeyboardInterrupt` 的版本在 systemd 下**根本进不了那个分支** ——
      正在采的场次 `recorder.close()` 不执行，留下没有 `room.json` / `series.json`
      的残缺目录。换句话说：**不修这条，每次重启都在丢当前场次的数据。**

    置位而不是直接在这里 join：信号处理函数在主线程里**可中断**地跑，在里面做
    30s 的 join 会挡住后续信号（第二次 Ctrl+C 想强退也退不掉）。置位后由 main 里
    正常的控制流去收尾，语义清楚也好调试。
    """
    def _handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, 'SIGTERM'):   # Windows 下存在但收不到，set 一次无害
        signal.signal(signal.SIGTERM, _handler)


def main():
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="抖音直播守护式采集（多主播轮播长跑）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument('xlsx_path', nargs='?', default='anchor.xlsx',
                    help='主播名单 xlsx（表头驱动定位「抖音号」/「昵称」列）')
    ap.add_argument('check_interval', nargs='?', type=int, default=CHECK_INTERVAL,
                    help='开播检测间隔（秒）')
    ap.add_argument('--sheet', default=None,
                    help='取该 sheet 的名单，如 40 取 并发测试.xlsx 的 40 并发档；'
                         '默认取第一个 sheet')
    ap.add_argument('--media', action='store_true',
                    help='同时采音视频（默认关；开销无法线性外推到 100 并发，'
                         '只适合 5-10 个主播）')
    ap.add_argument('--poll-rate', type=float, default=DEFAULT_POLL_RATE,
                    help='enter 探测接口的进程级限流（每秒最多请求数）。'
                         '实测 ~6 req/s 安全、~18 req/s 触发 444；传 0 关闭限流')
    ap.add_argument('--max-sessions', type=int, default=None, metavar='N',
                    help='**同时在采的主播数上限**（进程级、**非阻塞**）。抢不到名额的主播'
                         '这一轮**完全不采** —— 不建场次目录、不连 WS、不落任何数据，'
                         '只保留开播状态探测。默认 None = 不限（= 改动前行为：有几个在播采几个）。'
                         '语义与 --max-media 相反（那个是排队等，这个是直接跳过）')
    ap.add_argument('--max-media', type=int, default=None, metavar='N',
                    help='同时在采音视频的主播数上限（仅 --media 时有意义）。'
                         '默认 None = 不限（= 改动前行为）。**名单大于 10 人开 --media 就必须设**，'
                         '否则有几个在播就同时起几路 ffmpeg')
    ap.add_argument('--min-free-gb', type=float, default=5.0, metavar='G',
                    help='剩余磁盘低于该值时**不再起新的 ffmpeg**（本场只采文本/数值，'
                         '空间回收后自动恢复）。0 = 关闭该保护。'
                         '实测全模态约 4.9 GB/天/路，文本约 200 KB/场，故降级几乎不损失什么')
    ap.add_argument('--stall-sec', type=float, default=STALL_SECONDS, metavar='S',
                    help='WS 多久没收到**任何数据**就判定为僵死、结束本场并重连。'
                         '0 = 关闭（回到改动前行为：连接活着但不再送数据时线程会永久卡住，'
                         '该主播之后每次开播全部漏采）')
    ap.add_argument('--ws-reconnect', type=int, default=WS_RECONNECT, metavar='N',
                    help='WS 断线后在**同一场次目录**里最多连续重连几次，'
                         '这样断线只丢几秒而不会把一场直播切成多个碎片场次。'
                         '0 = 关闭（回到改动前行为：断一次就结束本场、另开一个场次目录）。'
                         '「连续」口径：某条连接活过 15 秒即清零重新计')
    args = ap.parse_args()
    if args.max_media is not None and args.max_media < 1:
        ap.error('--max-media 必须 >= 1（不限流请直接不传该参数）')
    if args.max_sessions is not None and args.max_sessions < 1:
        ap.error('--max-sessions 必须 >= 1（不限流请直接不传该参数）')
    if args.min_free_gb < 0:
        ap.error('--min-free-gb 不能为负（0 = 关闭磁盘保护）')
    if args.stall_sec < 0:
        ap.error('--stall-sec 不能为负（0 = 关闭僵死检测）')
    if args.ws_reconnect < 0:
        ap.error('--ws-reconnect 不能为负（0 = 关闭自动重连）')

    anchors = load_anchors(args.xlsx_path, sheet=args.sheet)
    if not anchors:
        print(f"错误：未从 {args.xlsx_path} 读取到主播列表")
        return
    print(f"加载主播列表 {len(anchors)} 个（来自 {args.xlsx_path}"
          f"{'，sheet ' + str(args.sheet) if args.sheet else ''}，"
          f"检测间隔 {args.check_interval}s，音视频 {'开' if args.media else '关'}）：")
    for name, did in anchors:
        print(f"  - {name}（抖音号 {did}）")
    print("=" * 70)

    # 进程级 enter 探测限流（令牌桶）。所有主播线程共享一个 —— 光靠每线程的随机 jitter
    # 只是「散布」没有「硬上限」，大规模会瞬时突破 enter 接口风控阈值（见 DEFAULT_POLL_RATE）。
    limiter = _RateLimiter(args.poll_rate) if args.poll_rate > 0 else None
    if limiter is not None:
        full_cycle = len(anchors) / args.poll_rate
        print(f"enter 探测限流：{args.poll_rate:.1f} req/s"
              f"（{len(anchors)} 人一轮全量探测至少 {full_cycle:.0f}s）")
        if full_cycle > args.check_interval:
            print(f"  ⚠️ 限流后一轮 {full_cycle:.0f}s > 检测间隔 {args.check_interval}s，"
                  f"轮询会积压、实际间隔被拉长到 ~{full_cycle:.0f}s。"
                  f"要么调大 --check-interval，要么分组跑（见 DEPLOY.md 第七节）")

    # 进程级**会话**并发闸门（`--max-sessions`）。同样必须是共享实例。
    # 它比媒体闸门更靠外一层：媒体闸门决定「这一场采不采音视频」，会话闸门决定
    # 「这一场采不采」—— 抢不到名额的主播连场次目录都不会建。
    session_gate = None
    if args.max_sessions is not None:
        session_gate = _MediaGate(args.max_sessions)
        print(f"会话并发闸门：最多同时采 {args.max_sessions} 个主播"
              f"（抢不到名额的**本轮完全不采**，不落任何数据，"
              f"只由台账记录开播状态；名额在整场收尾后才释放）")

    # 进程级音视频并发闸门。**必须是一个共享实例** —— 一路采集占满一个主播线程全程，
    # 只有跨线程共享的信号量才把「同时在跑的 ffmpeg 路数」钉住。
    media_gate = None
    if args.media and args.max_media is not None:
        media_gate = _MediaGate(args.max_media)
        print(f"音视频并发闸门：最多同时 {args.max_media} 路"
              f"（排到名额前该主播只采文本/数值，名额在上一路 ffmpeg 收掉后才释放）")
    elif args.media:
        print(f"⚠️ 未设 --max-media：{len(anchors)} 人中**每个在播的都会同时起一路 ffmpeg**"
              f"（实测单路 0.023 核 / 162 MB / 6.2 Mbps）")

    # 进程级磁盘闸门（仅 --media 时有意义）：全模态是磁盘大头，写满会让 daemon 开始报错。
    # 文本/数值每场只占 ~200 KB，所以「降级到纯文本」是这里代价最小的保命手段。
    disk_guard = None
    if args.media and args.min_free_gb > 0:
        disk_guard = _DiskGuard(args.min_free_gb, path=DATA_ROOT)
        free = disk_guard.free_gb()
        print(f"磁盘保护：剩余 < {args.min_free_gb:.1f} GB 时**停音视频、只留文本**"
              f"（当前剩余 {'无法测量' if free is None else f'{free:.1f} GB'}）")
    elif args.media:
        print("⚠️ --min-free-gb 0：磁盘保护已关闭，写满磁盘会拖垮整个守护进程")

    if args.stall_sec > 0:
        print(f"WS 僵死检测：{args.stall_sec:.0f} 秒收不到任何数据就结束本场"
              f"（下一轮探测会另开新场次；实测健康场次相邻消息最大间隔仅 2–16 秒）")
    else:
        print("⚠️ --stall-sec 0：僵死检测已关闭。连接活着但抖音不再送数据时，"
              "该主播线程会永久卡住，之后每次开播整场漏采")

    if args.ws_reconnect > 0:
        print(f"WS 断线重连：**同一场次目录**里最多连续重连 {args.ws_reconnect} 次"
              f"（每条连接活过 15 秒即清零重计）；断线只丢几秒，不会把一场直播"
              f"切成多个碎片场次")
    else:
        print("⚠️ --ws-reconnect 0：断了就结束本场、另开一个场次目录 —— "
              "一场真实直播会被切成多个「完整」碎片场次，按场次聚合的特征会在碎片边界断掉")

    # 无需共享的目录对象：每个主播在自己开播时各自解析 `data/<当天日期>/`，
    # 同名目录由 os.makedirs(exist_ok=True) 幂等收敛（见 data_recorder.new_date_dir）
    daemons = [AnchorDaemon(name, did, args.check_interval, media=args.media,
                            limiter=limiter, media_gate=media_gate, disk_guard=disk_guard,
                            stall_seconds=args.stall_sec, session_gate=session_gate,
                            ws_reconnect=args.ws_reconnect)
               for name, did in anchors]
    for d in daemons:
        d.start()

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    print("守护模式运行中，按 Ctrl+C 或 systemctl stop 停止...")
    stop_event.wait()   # 事件驱动：信号一到立刻返回，不做 sleep 轮询

    print("\n收到停止信号，正在优雅关闭（当前场次会先落盘）...")
    for d in daemons:
        d.stop()
    for d in daemons:
        d.join(timeout=30)
    alive = [d for d in daemons if d.is_alive()]
    if alive:
        print(f"⚠️ {len(alive)} 个主播线程 30s 内未退出（可能仍在收尾落盘）—— "
              f"进程即将退出，这些场次可能不完整")
    print("已全部停止，退出。")


if __name__ == "__main__":
    main()
