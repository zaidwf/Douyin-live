#!/usr/bin/python
# coding:utf-8
"""单测：**会话并发闸门** `--max-sessions` —— 抢不到名额就「完全不采」。

## 这条策略是什么（2026-10-16 用户指定的调整）

原来守护进程是「名单里有几个在播就采几个」（音视频另由 `--max-media` 排队限流）。
新策略改成：**同时只采 10 个主播**，抢不到名额的那些**这一轮一点数据都不落** ——
不建场次目录、不连 WebSocket、不写任何文件，只保留开播状态探测（那由
`douyin-ledger` 每 10 分钟记一次）。名额先到先得，空出来补给下一个在播的。

与 `--max-media` 的**语义差别**是这次改动的核心，也是最容易写错的地方：

  |            | 抢不到名额时 | 结果                     |
  |------------|--------------|--------------------------|
  | --max-media| **排队等**   | 等到就采，最坏降级成纯文本 |
  | --max-sessions | **直接跳过** | 本轮完全不采，什么都不落 |

## 与台账的耦合（不测这条会在生产上炸）

`live_ledger` 的 `★疑似漏采` 判的是「探到在播、但那一刻没有场次在写」。10 个名额配
一二十个在播，**几乎每个在播主播都会命中这一条** —— 不处理的话这一列会恒等于
「在播数 − 10」、退出码恒为 1，把 `systemctl --failed` 这个告警面彻底淹掉。
所以 `find_missed()` 多了 `session_cap`：名额已满的轮次不计漏采。

用合成对象验语义，**不起线程、不打网络、不碰真磁盘**：

  [1] 名额满 → 不建场次目录、不连 WS，且**不吃掉**名额
  [2] 名额空 → 正常采，且名额在**整场收尾之后**才归还
  [3] 优雅退出（stop_event 置位）→ 不采、不占名额，且**不误报「名额已满」**
  [4] 「名额已满」日志有节流（否则 157 人名单会被刷屏）
  [5] 契约：闸门卡在 `DataRecorder(` 之前、两个 unit 的 `--max-sessions` 值一致、
        `.timer` 周期是 10 分钟、`find_missed` 接受 `session_cap`

用法：python test/test_session_gate_unit.py
"""
import io
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                  errors='replace', line_buffering=True)
except Exception:
    pass

import auto_crawl
from auto_crawl import AnchorDaemon, _MediaGate

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(rel):
    with io.open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


# ================================================================ 桩
# 「谁先谁后」要跨桩比较（recorder.close() vs gate.release），所以都往**同一个**
# 序列里追加 —— 分两个列表各记各的，`index()` 之间没有可比性。
SEQ = []


class _StubFetcher:
    """只实现 `_one_cycle` 走到的那几个接口；`start()` 立即触发下播（本场即刻结束）。"""

    last = None

    def __init__(self, douyin_id=None, cookie=None, status='正在直播', rid='123456'):
        self.douyin_id = douyin_id
        self._status = status
        self.room_id = rid
        self.room_detail = {}
        self.product_refresh_order = None
        self.on_offline = None
        self.data_callback = None
        self.started = False
        self.stopped = False
        _StubFetcher.last = self

    def get_room_status(self):
        return True, self._status, '测试主播', '999'

    def start(self, max_reconnects=0, reconnect_wait=0.0):
        """★ 签名必须与 `DouyinLiveWebFetcher.start` 一致 —— `_one_cycle` 是按关键字
        `max_reconnects=...` 传的（WS 断线同场次重连，见 `auto_crawl.WS_RECONNECT`），
        签名对不上会在线程里抛 TypeError，表现为「什么都没采」而不是明显的报错。
        """
        self.started = True
        self.max_reconnects = max_reconnects
        if self.on_offline:
            self.on_offline()       # 立刻下播 → 等待循环马上退出

    def stop(self):
        self.stopped = True

    def frame_age(self):
        return 0.0                  # 不僵死


class _StubRecorder:
    """只记录「有没有被造出来」与调用顺序 —— 造出来就等于开了场次目录。"""

    made = 0

    def __init__(self, rid, douyin_id, base_dir=None):
        _StubRecorder.made += 1
        SEQ.append('recorder_created')
        self.path = os.path.join(base_dir or '.', 'live_stub_%d' % _StubRecorder.made)
        os.makedirs(self.path, exist_ok=True)

    def set_room_info(self, d):
        pass

    def on_event(self, e):
        pass

    def set_promotion_order(self, x):
        pass

    def close(self):
        SEQ.append('recorder_closed')


class _SpyGate(_MediaGate):
    """记录 release 发生在什么时候（用来断言「收尾之后才还名额」）"""

    def release(self):
        SEQ.append('gate_release')
        super().release()


class _TmpCwd:
    def __enter__(self):
        self._old = os.getcwd()
        self._dir = tempfile.mkdtemp(prefix='sg_unit_')
        os.chdir(self._dir)
        return self._dir

    def __exit__(self, *a):
        os.chdir(self._old)
        shutil.rmtree(self._dir, ignore_errors=True)


def _patch(recorder_cls=_StubRecorder):
    """把 `_one_cycle` 里的三个外部依赖换成桩：fetcher / recorder / 日期目录"""
    auto_crawl.DouyinLiveWebFetcher = _StubFetcher
    auto_crawl.DataRecorder = recorder_cls
    auto_crawl.new_date_dir = lambda base_dir='data': (os.makedirs(base_dir, exist_ok=True)
                                                       or base_dir)


_PATCHED = False


def _ensure_patched():
    global _PATCHED
    if not _PATCHED:
        _patch()
        _PATCHED = True


# ================================================================ [1] 名额满
def test_full_skips():
    print('\n[1] 名额已满 → **完全不采**（不建目录、不连 WS、不吃名额）')
    _ensure_patched()
    with _TmpCwd():
        gate = _MediaGate(1)
        assert gate.acquire(timeout=0)          # 先把唯一的名额占掉
        _StubRecorder.made = 0
        SEQ.clear()
        d = AnchorDaemon('测试主播', '000', session_gate=gate)
        d._one_cycle()
        f = _StubFetcher.last
        check('不建场次目录（DataRecorder 没被造出来）', _StubRecorder.made == 0,
              'made=%d' % _StubRecorder.made)
        check('不连 WebSocket（fetcher.start 没被调）', f.started is False)
        check('不吃掉名额（闸门仍是满的）', gate.acquire(timeout=0) is False)
        check('data/ 下没留下任何场次目录',
              not os.path.exists(os.path.join('data', 'live_stub_1')))


# ================================================================ [2] 名额空
def test_slot_free_runs():
    print('\n[2] 名额有空 → 正常采，且名额在**整场收尾之后**才归还')
    _ensure_patched()
    with _TmpCwd():
        _StubRecorder.made = 0
        SEQ.clear()
        gate = _SpyGate(1)
        d = AnchorDaemon('测试主播', '000', session_gate=gate)
        d._one_cycle()
        f = _StubFetcher.last
        check('建了场次目录', _StubRecorder.made == 1)
        check('连了 WebSocket', f.started is True)
        # ★ 生产守护必须真的开了「同场次内自动重连」——不传就是断一次切一个碎片场次
        check('★ WS 断线重连已开启（`max_reconnects` 传到了 fetcher.start）',
              f.max_reconnects == auto_crawl.WS_RECONNECT, f.max_reconnects)
        check('本场正常收尾（fetcher.stop + recorder.close）',
              f.stopped and 'recorder_closed' in SEQ)
        check('整场结束后名额已归还（能再拿到）', gate.acquire(timeout=0) is True)
        check('★ release 发生在 recorder.close() **之后**（不能在落盘时就还名额）',
              'recorder_closed' in SEQ and 'gate_release' in SEQ
              and SEQ.index('recorder_closed') < SEQ.index('gate_release'),
              'SEQ=%s' % SEQ)


# ================================================================ [3] 优雅退出
def test_stop_event_no_gate():
    print('\n[3] 优雅退出（stop_event 置位）→ 不采、不占名额、**不误报「名额已满」**')
    _ensure_patched()
    with _TmpCwd():
        _StubRecorder.made = 0
        gate = _MediaGate(1)
        d = AnchorDaemon('测试主播', '000', session_gate=gate)
        d.stop()                                # 模拟 systemctl stop
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            d._one_cycle()
        finally:
            sys.stdout = old
        check('不建场次目录', _StubRecorder.made == 0)
        check('不吃掉名额', gate.acquire(timeout=0) is True)
        check('★ 日志里没有误导性的「会话名额已满」', '会话名额已满' not in buf.getvalue(),
              buf.getvalue()[:120])


# ================================================================ [4] 日志节流
def test_log_throttle():
    print('\n[4] 「名额已满」日志节流（否则 157 人名单每 60 秒刷一次屏）')
    _ensure_patched()
    gate = _MediaGate(1)
    d = AnchorDaemon('测试主播', '000', session_gate=gate)
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        for _ in range(50):
            d._note_session_full()
    finally:
        sys.stdout = old
    n = buf.getvalue().count('会话名额已满')
    check('连续 50 次只打 1 行', n == 1, '实际 %d 行' % n)
    check('首次那条说「开始跳过本轮」', '开始跳过本轮' in buf.getvalue())
    # 手动把节流窗口推过期 → 再打一条，且这次措辞是「仍在」
    d._last_full_log -= auto_crawl.SESSION_FULL_LOG_INTERVAL + 1
    buf2 = io.StringIO()
    sys.stdout = buf2
    try:
        d._note_session_full()
    finally:
        sys.stdout = old
    check('窗口过期后再打一条，措辞改成「仍在跳过」', '仍在跳过本轮' in buf2.getvalue(),
          buf2.getvalue()[:120])


# ================================================================ [5] 契约
def test_contract():
    print('\n[5] 契约：闸门位置、两个 unit 的值一致、台账口径跟着改')
    src = _read('auto_crawl.py')

    check('有 --max-sessions 参数', "'--max-sessions'" in src)
    check('main() 建了闸门并传给 AnchorDaemon',
          '_MediaGate(args.max_sessions)' in src and 'session_gate=session_gate' in src)
    check('闸门用**非阻塞** acquire（timeout=0）',
          'self.session_gate.acquire(self._stop_event, timeout=0)' in src)

    # ★ 位置：闸门必须在 `DataRecorder(` 之前 —— 否则目录已经建出来了，
    #   「完全不采」就退化成「采一个空目录」。
    i_gate = src.index('self.session_gate.acquire(self._stop_event')
    i_rec = src.index('recorder = DataRecorder(')
    check('★ 闸门卡在 `DataRecorder(...)` **之前**（不采就不该建目录）', i_gate < i_rec,
          '闸门 @%d，建目录 @%d' % (i_gate, i_rec))
    i_probe = src.index('success, status, nickname, user_id = fetcher.get_room_status()')
    check('闸门卡在**确认开播之后**（没开播的不该占名额）', i_probe < i_gate)

    check('_note_session_full 存在且被调用', 'def _note_session_full' in src)
    check('SESSION_FULL_LOG_INTERVAL 存在', 'SESSION_FULL_LOG_INTERVAL =' in src)

    # ★★ 两个 unit 的 --max-sessions 必须一致：台账靠它区分「名额满了没轮到」
    #    和「真漏采了」。不一致 → `疑似漏采` 误报 → 退出码恒为 1 → 告警面失效。
    crawl = _read('service/douyin-crawl.service')
    ledger = _read('service/douyin-ledger.service')
    m1 = re.search(r'--max-sessions\s+(\d+)', crawl)
    m2 = re.search(r'--max-sessions\s+(\d+)', ledger)
    check('crawl.service 里配了 --max-sessions', bool(m1))
    check('ledger.service 里配了 --max-sessions', bool(m2))
    check('★★ 两处值一致', bool(m1) and bool(m2) and m1.group(1) == m2.group(1),
          'crawl=%s ledger=%s' % (m1 and m1.group(1), m2 and m2.group(1)))

    timer = _read('service/douyin-ledger.timer')
    check('ledger.timer 周期是 10 分钟', 'OnUnitActiveSec=10min' in timer)
    check('ledger.timer 仍保留 Persistent=true', 'Persistent=true' in timer)

    lsrc = _read('service/live_ledger.py')
    check('find_missed 接受 session_cap', 'session_cap=0' in lsrc)
    check('main() 把 --max-sessions 传进 find_missed',
          'session_cap=args.max_sessions' in lsrc)
    check('★ 名额已满的轮次不计漏采（排除假漏采）',
          'inflight_at(r[\'ts\']) >= session_cap' in lsrc)


if __name__ == '__main__':
    test_full_skips()
    test_slot_free_runs()
    test_stop_event_no_gate()
    test_log_throttle()
    test_contract()
    print(f'\n结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        sys.exit(1)
    print('✅ 会话闸门：抢不到名额就完全不采，且不会污染台账的漏采告警')
