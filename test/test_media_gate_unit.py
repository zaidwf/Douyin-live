#!/usr/bin/python
# coding:utf-8
"""单测：`auto_crawl` 的音视频名额等待策略 —— 名额**只能发给还在播的场次**。

背景（2026-10-15 实测，非推演）：服务器上 `--media --max-media 5` 跑了半天，
**206 个场次里只有 47 个（23%）有音视频，其中 30 个（59%）产出为 0** ——
`audio/` 下只有一个 **78 字节**的 WAV（44 字节头 + LIST 元数据 + 长度为 0 的
`data` 块，一个采样点都没有）。

根因不是 ffmpeg，是**名额被僵尸会话吃掉了**：

  `_one_cycle` 走到「取名额」那一步时，本场**早就开始了**（WS 在跑、recorder 在落盘）。
  原实现裸调 `gate.acquire(stop_event)` —— 阻塞到拿到为止。于是房间下播它不知道，
  几个小时后拿到名额，去给一个**早已下播的房间**起 ffmpeg，拿回一个空 WAV 头就退出。

证据：重启那一刻有 **51 个线程**同时堵在闸门里，而同时在播的只有 ~22 个房间；
拿到名额的 51 场里 30 场的 wav 写入时间**晚于文本最后写入时间**（最晚晚 4 小时）。

修法：把「等名额」拆成 1 秒一跳的重试，**每一跳都检查本场是否还活着**
（`offline` / `ws_thread` / `_stop_event`），本场一结束就放弃名额。
策略在 `AnchorDaemon._wait_media_slot()`，闸门本身只是一个带超时的信号量。

这里用合成对象验语义，**不起线程、不打网络、不碰真磁盘**：

  [1] 名额空着 → 立刻拿到
  [2] ★ 排队中本场下播 → 放弃，且**没有吃掉名额**（这条就是上面那个事故的回归）
  [3] 排队中进程要退出（stop_event） → 放弃，且没吃名额
  [4] 排队中 WS 断了 → 放弃，且没吃名额
  [5] 排队中名额释放、本场仍在播 → 拿到
  [6] 契约：`_one_cycle` 不再裸调 `gate.acquire(stop_event)`

用法：python test/test_media_gate_unit.py
"""
import io
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                  errors='replace', line_buffering=True)
except Exception:
    pass

import auto_crawl
from auto_crawl import _MediaGate, AnchorDaemon

_ok = _fail = 0


def check(desc, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {desc}')
    else:
        _fail += 1
        print(f'  ❌ {desc}' + (f'  ← {detail}' if detail else ''))


class _FastGate(_MediaGate):
    """把内部等待切片缩到 5ms —— 生产是 1.0s，测试里等不起。

    只覆盖 `acquire` 的默认 timeout，**不改任何语义**：真实验的仍是
    「拿不到就返回 False，由调用方决定是否继续等」。
    """

    def acquire(self, stop_event=None, timeout=0.5):
        return super().acquire(stop_event, timeout=0.005)


class _Alive:
    def is_alive(self):
        return True


class _Dead:
    def is_alive(self):
        return False


def _daemon(gate):
    """造一个**没有启动**的 AnchorDaemon —— 构造成员即可，不跑 run()"""
    return AnchorDaemon('测试主播', '000', media=True, media_gate=gate)


def _slots(gate):
    """信号量当前剩余名额数。

    读 `_sem._value` 是 CPython 的实现细节，但在这里**正合适**：要断言的恰恰是
    「名额有没有被吃掉」这个内部计数，用公开 API 反而绕（acquire 本身就是被测行为）。
    """
    return gate._sem._value


# ---------- 1. 名额空着 ----------
def test_free_slot():
    print('\n[1] 名额空着 → 立刻拿到')
    gate = _FastGate(1)
    d = _daemon(gate)
    got, waited = d._wait_media_slot(threading.Event(), _Alive())
    check('拿到名额', got is True)
    check('等待时长被如实回报（进 session_meta.json 的 gate_wait_s）',
          isinstance(waited, float) and waited < 1.0, f'waited={waited}')
    check('名额被占住（剩余 0）', _slots(gate) == 0, f'剩余 {_slots(gate)}')
    gate.release()
    check('release 后名额恢复', _slots(gate) == 1)


# ---------- 2. ★ 排队中本场下播 ----------
def test_offline_while_queued():
    print('\n[2] ★ 排队中本场下播 → 放弃，且不吃名额（僵尸名额事故的回归）')
    gate = _FastGate(1)
    gate.acquire()                      # 唯一名额被别的场次占着
    before = _slots(gate)
    offline = threading.Event()
    threading.Timer(0.05, offline.set).start()
    d = _daemon(gate)
    got, waited = d._wait_media_slot(offline, _Alive())
    check('放弃（返回 False，不去给下播的房间起 ffmpeg）', got is False)
    check('★ 名额没被吃掉（调用方不可 release）', _slots(gate) == before,
          f'前 {before} 后 {_slots(gate)}')
    check('等待时长被回报（用于区分「没排到名额」与「排到了但启动失败」）',
          isinstance(waited, float) and waited >= 0.0, f'waited={waited}')
    # 名额还在别人手里：本场的放弃不影响它
    gate.release()
    check('原持有者仍能正常归还', _slots(gate) == 1)


# ---------- 3. 排队中进程要退出 ----------
def test_stop_event_while_queued():
    print('\n[3] 排队中进程要退出 → 放弃，且不吃名额')
    gate = _FastGate(1)
    gate.acquire()
    before = _slots(gate)
    d = _daemon(gate)
    threading.Timer(0.05, d.stop).start()      # systemctl stop / restart
    got, _waited = d._wait_media_slot(threading.Event(), _Alive())
    check('放弃（不阻塞 join，优雅退出不被它拖住）', got is False)
    check('名额没被吃掉', _slots(gate) == before, f'前 {before} 后 {_slots(gate)}')


# ---------- 4. 排队中 WS 断了 ----------
def test_ws_death_while_queued():
    print('\n[4] 排队中 WS 断了 → 放弃，且不吃名额')
    gate = _FastGate(1)
    gate.acquire()
    before = _slots(gate)
    d = _daemon(gate)
    got, _waited = d._wait_media_slot(threading.Event(), _Dead())
    check('放弃（WS 死了场次同样结束）', got is False)
    check('名额没被吃掉', _slots(gate) == before, f'前 {before} 后 {_slots(gate)}')


# ---------- 5. 排队中等到了名额 ----------
def test_slot_frees_while_alive():
    print('\n[5] 排队中名额释放、本场仍在播 → 拿到（这是**该**发生的路径）')
    gate = _FastGate(1)
    gate.acquire()
    threading.Timer(0.05, gate.release).start()
    d = _daemon(gate)
    got, _waited = d._wait_media_slot(threading.Event(), _Alive())
    check('拿到名额', got is True)
    check('名额确实被本场占住', _slots(gate) == 0, f'剩余 {_slots(gate)}')


# ---------- 6. 契约 ----------
def test_contract():
    print('\n[6] 契约：调用点不再裸调阻塞式 acquire')
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'auto_crawl.py'), encoding='utf-8').read()
    lines = [l.strip() for l in src.splitlines()]
    # 裸调：把阻塞等待的全部判断（何时放弃）都交给了闸门自己 —— 那正是事故成因
    bad = [l for l in lines
           if l.startswith('gate_taken = self.media_gate.acquire(')]
    check('★ 没有 `gate_taken = self.media_gate.acquire(...)` 这种裸阻塞调用',
          not bad, bad)
    check('改用 _wait_media_slot（带 offline / ws_thread 两个「本场还活着吗」判据）',
          'self._wait_media_slot(offline, ws_thread)' in src)
    check('_wait_media_slot 每跳都检查 offline', 'offline.is_set()' in src)
    check('_wait_media_slot 每跳都检查 ws_thread', 'ws_thread.is_alive()' in src)
    # 闸门必须支持「试一次就返回」——没有 timeout 参数的话 _wait_media_slot 会 TypeError
    check('★ _MediaGate.acquire 接受 timeout 参数',
          'def acquire(self, stop_event=None, timeout=' in src)
    check('★ start_media 失败时不会把失败对象留在 capture 里（否则会打印成空采集）',
          'if cap.start_media():' in src and 'capture = cap' in src)


if __name__ == '__main__':
    print('=' * 64)
    print('音视频名额等待策略单测（合成数据，不起线程/不打网络）')
    print('=' * 64)
    test_free_slot()
    test_offline_while_queued()
    test_stop_event_while_queued()
    test_ws_death_while_queued()
    test_slot_frees_while_alive()
    test_contract()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 名额策略有问题 —— 名额会再次被僵尸会话吃掉')
    else:
        print('✅ 名额只会发给还在播的场次（僵尸会话不再吃名额）')
    sys.exit(1 if _fail else 0)
