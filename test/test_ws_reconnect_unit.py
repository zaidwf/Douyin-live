#!/usr/bin/python
# coding:utf-8
"""单测：WS 断线**同场次内自动重连**（`liveMan.DouyinLiveWebFetcher.start`）。

## 这条改动防的是什么（2026-10-16 定案）

WS 一断，`auto_crawl._one_cycle` 就把本场收尾、60 秒后**另开一个场次目录**。于是
**一场真实直播被切成多个「完整」碎片场次**（实测同一主播 30 分钟内 4 个场次目录）——
台账里标的「完整」是**碎片完整**，任何按场次聚合的研究特征（时长、峰值人气、整场弹幕量、
留存曲线）都会**在碎片边界上被切断**。

改法是把重连放进 `start()` 内部，让它对外保持「`start()` 返回 == 本场结束」的语义 ——
于是 `auto_crawl` 里所有「`ws_thread.is_alive()` == 本场还在采」的判断自动继续成立
（`_wait_media_slot` 与 `_one_cycle` 的等待循环**一行都不用改**）。

## 这个文件重点钉住什么

重连循环有四个**只要写错一个就完全失效**的细节，每个都有独立断言：

  A. `start()` 每次连接前必须置 `running = True`（`_wsOnOpen` 自己不置位）
     —— 漏了它，重连后握手成功但**心跳永不发出**，连接被服务端当僵尸掐掉。
  B. `_prepare_reconnect` 必须重置 `last_frame_ts` —— 不重置的话新连接刚建立
     就被 `auto_crawl` 的 300 秒僵死判据判死，重连等于白做。
  C. `_teardown_ws` 必须先 `self.ws = None` 再 join 心跳线程 —— `_sendHeartbeat`
     读的是实例属性 `self.ws`，重连后它已指向**新** socket，旧线程会往新连接发 ping
     且永不退出（`stop()` 只 join 得到最后一个 → 线程**累积**）。
  D. `_wsOnClose` 必须只认「当前这条连接」—— 否则旧 socket 的迟到回调会把新连接的
     `running` 清掉。

还有一条**反向**要求：`max_reconnects <= 0`（默认）时行为必须与改动前**逐字节等价** ——
`load_test.py` 拿 `f.running = False` 当**唯一**的停止手段，重连会把它吞掉。

用法：python test/test_ws_reconnect_unit.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                  errors='replace', line_buffering=True)
except Exception:
    pass

import auto_crawl
from auto_crawl import AnchorDaemon, WS_RECONNECT
from liveMan import DouyinLiveWebFetcher, next_reconnect_state

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


class _FakeWS:
    """占位 socket：让 `_teardown_ws` 的 `ws.close()` 有东西可调，并能验证它真的被调了"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _mk_fetcher():
    """真 fetcher 实例（`__init__` 不发请求、不建线程），但 `_connectWebSocket` 会被换掉"""
    return DouyinLiveWebFetcher('000', None)


def _stub_connect(seq, on_call=None):
    """把 `_connectWebSocket` 换成脚本化的假实现（可记录调用、可自定义行为）"""
    def _fake(self):
        seq.append(time.time())
        if on_call is not None:
            on_call(self, len(seq))
    return _fake


def _run(f, max_reconnects, wait=0.01):
    """跑一遍 `start()`，把 `_connectWebSocket` 换成 `f._fake_connect`"""
    f._connectWebSocket = types.MethodType(f._fake_connect, f)
    f.start(max_reconnects=max_reconnects, reconnect_wait=wait)


# ---------- 1. 判据本身：纯函数真值表 ----------
def test_truth_table():
    print('\n[1] `next_reconnect_state()` 真值表')
    # (连了多久, 之前的连续失败数, 上限, 期望的重连与否, 期望的新失败数)
    cases = [
        (600.0, 0, 10, True, 0),     # 长连接 → 额度恢复、继续重连
        (600.0, 7, 10, True, 0),     # ★ 长连接把之前的失败计数**清零**
        (2.0, 0, 10, True, 1),       # 秒断 → 计数 +1
        (2.0, 9, 10, True, 10),      # 刚好用完
        (2.0, 10, 10, False, 11),    # ★ 超过上限 → 放弃
        (2.0, 0, 2, True, 1),        # 小额度：第 1 次
        (2.0, 2, 2, False, 3),       # 小额度：第 3 次放弃
        (600.0, 0, 0, False, 0),     # ★ 默认关：长连接也不重连
        (2.0, 0, 0, False, 0),       # ★ 默认关：短连接也不重连
        (0.0, 0, 10, True, 1),       # 0 秒 = 失败
        (15.0, 5, 10, True, 0),      # 恰好等于 grace → 算「活过」（`>=`）
        (14.9, 5, 10, True, 6),      # 差一点 → 仍算失败
    ]
    for lived, fails, mx, want_keep, want_fails in cases:
        got = next_reconnect_state(lived, fails, mx)
        check(f'lived={lived}s fails={fails} max={mx} → {got}',
              got == (want_keep, want_fails), f'期望 {(want_keep, want_fails)}')
    # 负额度也必须是「关」，不能变成「无限重连」
    check('★ max_reconnects=-1 也是关闭（不是无上限）',
          next_reconnect_state(2.0, 0, -1) == (False, 0))


# ---------- 2. 掉线 → 重连 → 下播 ----------
def test_reconnect_then_offline():
    print('\n[2] 掉线 → 重连 → 主播下播（`status==3` 走 stop()）')
    f = _mk_fetcher()
    seq = []
    logs = []
    f.log_callback = lambda t, m: logs.append((t, m))

    def on_call(self, n):
        if n == 2:
            self.stop()      # 模拟 ControlMessage status==3 → on_offline + stop()
    f._fake_connect = _stub_connect(seq, on_call)
    _run(f, max_reconnects=5)

    check('总共连了 2 次（第 1 次掉线、第 2 次下播）', len(seq) == 2, len(seq))
    check('★ 重连计数为 1', f.reconnect_count == 1, f.reconnect_count)
    check('★ 下播后 `_intentional_stop` 为真（= 主动停止，不该再重连）',
          f._intentional_stop is True)
    check('★ 打了「第 N 次重连」的日志（现场可观测，不是静默的）',
          any('第 1 次重连' in m for _, m in logs), logs[:3])
    check('★ 重连等待的秒数被记下（写进 session_meta 的 ws_gap_s）',
          f.reconnect_gap_s > 0, f.reconnect_gap_s)


# ---------- 3. 额度用尽 ----------
def test_exhausted():
    print('\n[3] 连续失败到上限 → 放弃并结束本场')
    f = _mk_fetcher()
    seq = []
    logs = []
    f.log_callback = lambda t, m: logs.append((t, m))
    f._fake_connect = _stub_connect(seq)     # 每次都立刻返回 = 次次秒断
    _run(f, max_reconnects=2)

    check('★ 恰好重连 2 次后放弃（`max_reconnects=2` = 重连 2 次，不是 3 次）',
          f.reconnect_count == 2, f.reconnect_count)
    check('总共连了 3 次（1 次原始 + 2 次重连）', len(seq) == 3, len(seq))
    check('★ `_intentional_stop` 仍为 False —— 这个区别决定 `auto_crawl` 会记 '
          '`ws_closed`', f._intentional_stop is False)
    check('★ 打了「重连额度用尽」的日志',
          any('额度用尽' in m for _, m in logs), logs[-2:])


# ---------- 4. ★ last_frame_ts 必须重置 ----------
def test_frame_age_reset():
    print('\n[4] ★ 重连前重置 `last_frame_ts`（否则新连接刚建立就被判僵死）')
    f = _mk_fetcher()
    ages = []

    def on_call(self, n):
        if n == 1:
            # 第 1 条连接「断了很久才返回」：把最后一帧时刻拨到 400 秒前。
            # （模拟真实场景：socket 早就死了，300 秒僵死判据已经该判它了）
            self.last_frame_ts -= 400
            ages.append(self.frame_age())
        else:
            ages.append(self.frame_age())   # 第 2 条连接刚建立 —— 应已被重置
            self.stop()
    f._fake_connect = _stub_connect([], on_call)
    _run(f, max_reconnects=5)

    check('第 1 次连接时 frame_age ≈ 400（人为拨的）',
          len(ages) >= 2 and 399 <= ages[0] <= 403, ages)
    check('★★ 第 2 次连接时 frame_age < 5（重连前被重置）—— 不重置的话'
          'auto_crawl 的 300 秒僵死判据会立刻把新连接判死',
          len(ages) >= 2 and ages[1] < 5.0, ages)
    check('判据串起来：重连后不再算僵死',
          AnchorDaemon('测试主播', '000')._stalled(f) is False)


# ---------- 5. ★ stop() 能打断重连退避等待 ----------
def test_stop_interrupts_wait():
    print('\n[5] ★ `stop()` 打断重连退避等待（否则 systemctl stop 要白等几十秒）')
    f = _mk_fetcher()
    f._fake_connect = _stub_connect([])      # 每次都秒断 → 立刻进退避等待
    f._connectWebSocket = types.MethodType(f._fake_connect, f)

    t = threading.Thread(target=lambda: f.start(max_reconnects=50, reconnect_wait=30.0),
                         daemon=True)
    t0 = time.time()
    t.start()
    time.sleep(0.5)
    f.stop()
    t.join(timeout=5)
    elapsed = time.time() - t0

    check('★ `start()` 在 5 秒内返回（而不是等满 30 秒 × 50 次）',
          not t.is_alive(), f'仍活着 {elapsed:.1f}s')
    check('★ 确实是被 stop() 打断的（远早于 30 秒的退避窗口）',
          elapsed < 5.0, f'{elapsed:.2f}s')
    check('打断后 `_intentional_stop` 为真（收尾时不会当成故障）',
          f._intentional_stop is True)


# ---------- 6. ★ 心跳线程不累积 ----------
def test_heartbeat_not_leaked():
    print('\n[6] ★ 重连不泄漏心跳线程（旧线程必须被 join 掉）')
    f = _mk_fetcher()
    socks = []
    hbs = []
    calls = []

    def on_call(self, n):
        calls.append(n)
        if n >= 4:
            self.stop()
            return
        # 模拟 `_wsOnOpen`：起一条**活得比退避窗口久**的心跳线程
        # （真线程因为要等 `time.sleep(5)` 退出，这里用 0.3 秒等价地慢）
        ws = _FakeWS()
        socks.append(ws)
        self.ws = ws
        th = threading.Thread(target=lambda: time.sleep(0.3), daemon=True)
        th.start()
        hbs.append(th)
        self.heartbeat_thread = th

    f._fake_connect = _stub_connect([], on_call)
    _run(f, max_reconnects=10, wait=0.01)

    check('起了 3 条心跳线程（3 次握手各一条）', len(hbs) == 3, len(hbs))
    check('★ 每次重连后旧心跳线程都已退出 —— 不 join 的话 3 条会**并发**'
          '往同一个 socket 发 ping',
          all(not th.is_alive() for th in hbs),
          [th.is_alive() for th in hbs])
    check('★ 旧 socket 都被 close 掉了', all(ws.closed for ws in socks),
          [ws.closed for ws in socks])
    check('★ `heartbeat_thread` 没留下悬空引用（`_teardown_ws` 置了 None）',
          f.heartbeat_thread is None, f.heartbeat_thread)


# ---------- 7. ★ _wsOnClose 只认当前连接 ----------
def test_onclose_guard():
    print('\n[7] ★ `_wsOnClose` 只认「当前这条连接」（防迟到回调误杀新连接）')
    f = _mk_fetcher()
    old_ws = _FakeWS()
    new_ws = _FakeWS()
    f.ws = new_ws
    f.running = True

    f._wsOnClose(old_ws, None, None)
    check('★★ 旧 socket 的迟到回调**不会**清掉新连接的 `running`',
          f.running is True, f.running)

    f._wsOnClose(new_ws, None, None)
    check('当前 socket 关闭仍然会置 False（默认路径行为不变）',
          f.running is False)


# ---------- 8. ★ 默认路径等价性（最要紧的一条） ----------
def test_default_path_unchanged():
    print('\n[8] ★ 默认（`max_reconnects=0`）行为必须与改动前逐字节等价')
    f = _mk_fetcher()
    seq = []
    logs = []
    prep = []
    f.log_callback = lambda t, m: logs.append((t, m))
    f._fake_connect = _stub_connect(seq)
    # spy `_prepare_reconnect`：默认路径一次都不该进重连分支
    f._prepare_reconnect = types.MethodType(lambda self, w: prep.append(w) or True, f)
    _run(f, max_reconnects=0)

    check('★ 只连了 1 次（循环体只跑一轮）', len(seq) == 1, len(seq))
    check('★ 没有发生任何重连', f.reconnect_count == 0, f.reconnect_count)
    check('★★ `_prepare_reconnect()` 一次都没被调用', len(prep) == 0, prep)
    check('★ 没打「额度用尽」的日志（0 是「配置成不重连」，不是「额度用尽」）',
          not any('额度用尽' in m for _, m in logs), logs)
    check('★ 没打「第 N 次重连」的日志', not any('次重连' in m for _, m in logs), logs)
    # `load_test.py` 靠这个置位来停：只要没重连分支，它就一定生效
    check('★ `load_test` 的停止手段（`running=False`）不会被吞掉：'
          '整个默认路径只把 running 写成 True 一次',
          len(seq) == 1 and f.reconnect_count == 0)


def test_start_after_stop_reusable():
    print('\n[8b] GUI 复用同一个实例：`stop()` 之后再 `start()` 必须能正常工作')
    f = _mk_fetcher()
    f.stop()      # 留下 `_intentional_stop = True`
    check('`stop()` 置位了 `_intentional_stop`', f._intentional_stop is True)
    seq = []
    f._fake_connect = _stub_connect(seq)
    _run(f, max_reconnects=0)
    check('★ 复位后能正常连上（不复位的话第一次连接返回就立刻 break，'
          'GUI 点了开始却什么都没采）', len(seq) == 1, len(seq))


# ---------- 9. session_meta.json 的重连统计 ----------
def test_session_meta_fields():
    print('\n[9] `session_meta.json` 记下重连统计（重连不能是静默的）')
    tmp = tempfile.mkdtemp(prefix='reconnect_meta_')
    try:
        d = AnchorDaemon('测试主播', '000')
        f = _mk_fetcher()
        f.reconnect_count = 3
        f.reconnect_gap_s = 12.34        # 不用 9.35 这种值：round(9.35,1) 因浮点表示得 9.3
        d._write_session_meta(tmp, datetime.now(), 60.0, 'offline', None, 0.0, None,
                              fetcher=f)
        p = os.path.join(tmp, 'session_meta.json')
        meta = json.load(open(p, encoding='utf-8'))
        check('★ `ws_reconnects` 落盘', meta.get('ws_reconnects') == 3, meta.get('ws_reconnects'))
        check('★ `ws_gap_s` 落盘并四舍五入到 0.1 秒',
              abs(meta.get('ws_gap_s', -1) - 12.3) < 0.01, meta.get('ws_gap_s'))

        # 不传 fetcher = 老调用形状（test_stall_watchdog_unit.py 就是这么调的）
        d._write_session_meta(tmp, datetime.now(), 60.0, 'offline', None, 0.0, None)
        meta2 = json.load(open(p, encoding='utf-8'))
        check('★ 不传 fetcher 时两个字段默认 0（向后兼容，老调用点不用改）',
              meta2.get('ws_reconnects') == 0 and meta2.get('ws_gap_s') == 0.0,
              (meta2.get('ws_reconnects'), meta2.get('ws_gap_s')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 10. 契约 ----------
def test_contract():
    print('\n[10] 契约：默认关、参数传对、守卫在位')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    crawl = open(os.path.join(root, 'auto_crawl.py'), encoding='utf-8').read()
    man = open(os.path.join(root, 'liveMan.py'), encoding='utf-8').read()

    check('★ `start()` 的 `max_reconnects` 默认是 0（默认关这条不变量）',
          'def start(self, max_reconnects=0, reconnect_wait=RECONNECT_WAIT):' in man)
    check('★ `auto_crawl` 显式传了 `max_reconnects`（生产才开）',
          "'max_reconnects': self.ws_reconnect" in crawl)

    # `collect_full` / `load_test` 是压测入口，必须**没有**重连 —— 否则失败计数被掩盖
    for fn in ('collect_full.py', 'load_test.py'):
        src = open(os.path.join(root, fn), encoding='utf-8').read()
        check(f'★ `{fn}` 里没有 `max_reconnects`（压测语义：断了就失败）',
              'max_reconnects' not in src)
    # GUI 同理（它靠 `fetcher.running` 判断「还在监控」）。
    # 注意不能拿整个 liveMan.py 去搜 —— `def start(self, max_reconnects=0, ...)`
    # 本身就在这个文件里。要钉的是**调用点**是原样的无参形式。
    check('★ GUI 的采集线程仍是**无参**调用（`running` 语义不能被重连搅乱）',
          'threading.Thread(target=self.fetcher.start)' in man)
    check('★ `liveMan.py` 里没有别处按关键字传 `max_reconnects`（只有 auto_crawl 该传）',
          '.start(max_reconnects' not in man)

    check('★ `--ws-reconnect` 开关存在且默认 = WS_RECONNECT',
          "'--ws-reconnect'" in crawl and 'default=WS_RECONNECT' in crawl)
    check('★ `--ws-reconnect` 负数会报错（而不是被当成关闭）',
          "'--ws-reconnect 不能为负" in crawl)
    check('★ `WS_RECONNECT` 常量被 `AnchorDaemon` 用作默认值',
          'ws_reconnect=WS_RECONNECT' in crawl)

    check('★★ `_wsOnClose` 有 `ws is self.ws` 守卫（防迟到回调误杀新连接）',
          'if ws is self.ws:' in man)
    check('★★ `_teardown_ws` 先摘 `self.ws`（`clear_ws`）再 join 心跳',
          'if clear_ws:' in man and 'self.ws = None' in man
          and 'raise' not in man[man.index('def _teardown_ws'):
                                 man.index('def _teardown_ws') + 2000])
    check('★ 心跳 join 的超时被显式传参（重连用 HEARTBEAT_JOIN_TIMEOUT，'
          '必须 > 心跳循环的 time.sleep(5)）',
          'join_timeout=HEARTBEAT_JOIN_TIMEOUT' in man and 'HEARTBEAT_JOIN_TIMEOUT = 6.0' in man)
    check('★ 心跳线程引用被清空（不留悬空旧线程）',
          'self.heartbeat_thread = None' in man)
    check('★★ `start()` 每次连接前重置 `running = True`'
          '（`_wsOnOpen` 自己不置位，漏了它重连后心跳永不发出）',
          'self.running = True' in man[man.index('def start(self, max_reconnects'):
                                          man.index('def _teardown_ws')])
    check('★ `start()` 复位 `_intentional_stop`（GUI 复用实例）',
          'self._intentional_stop = False' in man)
    check('★ `stop()` 置 `_intentional_stop` 并叫醒退避等待',
          'self._intentional_stop = True' in man and 'self._reconnect_wake.set()' in man)
    check('★ 握手异常不再调 `stop()`（那会被当成主动停止而永不重连）',
          'self._teardown_ws()' in man[man.index('except Exception as e:',
                                                 man.index('def _connectWebSocket')):
                                       man.index('def _sendHeartbeat')])
    check('★ 「额度用尽」日志只在 `max_reconnects > 0` 时打',
          'if max_reconnects > 0:' in man)
    check('★ `_write_session_meta` 写了 `ws_reconnects` / `ws_gap_s`',
          "'ws_reconnects'" in crawl and "'ws_gap_s'" in crawl)
    # 不要动既有的「本场还在采吗」判据 —— 重连正是为了让它们继续成立
    check('★ `auto_crawl` 仍拿 `ws_thread.is_alive()` 当本场结束的判据',
          'not ws_thread.is_alive()' in crawl)
    check('★ `ws_closed` 的收尾路径保留（语义变成「重连额度用尽」）',
          "stop_reason = 'ws_closed'" in crawl)


if __name__ == '__main__':
    print('=' * 66)
    print('WS 断线同场次重连单测（合成数据，不起真连接/不打网络）')
    print('=' * 66)
    test_truth_table()
    test_reconnect_then_offline()
    test_exhausted()
    test_frame_age_reset()
    test_stop_interrupts_wait()
    test_heartbeat_not_leaked()
    test_onclose_guard()
    test_default_path_unchanged()
    test_start_after_stop_reusable()
    test_session_meta_fields()
    test_contract()
    print('\n' + '=' * 66)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 重连有问题 —— 断一次仍会把一场直播切成两个碎片场次')
        sys.exit(1)
    print('✅ 断线在同场次内自愈；默认（不重连）路径与改动前逐字节一致')
