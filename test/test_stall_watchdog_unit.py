#!/usr/bin/python
# coding:utf-8
"""单测：WS 僵死检测 + `session_meta.json`（场次自描述）。

## 这条判据防的是什么（2026-10-15 实测，非推演）

守护进程的下播判定是**事件驱动**的：`ControlMessage status==3` → `on_offline`。
可失效的那一半恰恰不发这个事件 —— **TCP 连接还在，抖音不再下发任何数据**。
此时 `offline` 永不置位、`ws_thread.is_alive()` 恒为真，等待循环
`while not offline.is_set() and not stop_event.is_set(): if not ws.is_alive(): break`
**永远不会退出**。后果是双层的：

1. 这一场从挂死那刻起不再落盘；
2. **这个主播永远回不到探测循环 → 之后每一次开播整场漏采。**

实测证据：名单 157 人探一轮拿到 **36 个在播**，而守护进程只有 **21 个在写** →
16 个房间（44%）没在采。逐个查写停时刻（房间当时都还在播）：
`1637665557` 停在 15:15（挂死 90 分钟）、`26915370405` 停在 15:19（85 分钟）、
`liuxiongxiong` 停在 15:42（62 分钟）、`110154347` 停在 15:57（47 分钟）。

## 阈值为什么取 300 秒（同样是实测）

20 个在写场次逐个量 `events.jsonl` 相邻 `ts` 的**最大**间隔：**2–16 秒**。
挂死场次是 1500+ 秒。中间两个数量级余量 —— 300 秒不可能误杀。

用合成对象验语义，**不起线程、不打网络**：

  [1] 僵死 → `_stalled` 为真；健康 → 为假；恰好等于阈值 → 为真（`<=`）
  [2] `stall_seconds=0` 关闭检测 → 恒为假（回到改动前行为）
  [3] `DouyinLiveWebFetcher.frame_age()` 的真实语义（构造时置位、能随时间增长）
  [4] `_write_session_meta` 真的写对内容（含未产出的原因与排队时长）
  [5] 契约：判据挂在 `_wsOnMessage`（唯一收口）、是被 `_one_cycle` 用的、
        有 `--stall-sec` 开关、`STALL_SECONDS == 300`

用法：python test/test_stall_watchdog_unit.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                  errors='replace', line_buffering=True)
except Exception:
    pass

import auto_crawl
from auto_crawl import STALL_SECONDS, AnchorDaemon
from liveMan import DouyinLiveWebFetcher

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


class _StubFetcher:
    """只有 `frame_age()` 的桩 —— 不连网络、不建线程"""

    def __init__(self, age):
        self._age = age

    def frame_age(self):
        return self._age


def _daemon(stall_seconds=STALL_SECONDS):
    return AnchorDaemon('测试主播', '000', stall_seconds=stall_seconds)


# ---------- 1. 判据本身 ----------
def test_stalled_truth_table():
    print('\n[1] 僵死判据真值表')
    d = _daemon()
    check('刚收到帧（0.5 秒前）→ 不判僵死', d._stalled(_StubFetcher(0.5)) is False)
    check('16 秒（实测健康场次的最坏情况）→ 不判僵死',
          d._stalled(_StubFetcher(16)) is False)
    check('299 秒（阈值内）→ 不判僵死', d._stalled(_StubFetcher(299)) is False)
    check('★ 300 秒（恰好等于阈值）→ 判僵死（用 `<=`，边界归僵死侧）',
          d._stalled(_StubFetcher(300)) is True)
    check('★ 1500 秒（实测的挂死量级）→ 判僵死',
          d._stalled(_StubFetcher(1500)) is True)
    check('阈值可调（构造参数生效）',
          _daemon(60)._stalled(_StubFetcher(61)) is True
          and _daemon(60)._stalled(_StubFetcher(59)) is False)


# ---------- 2. 关闭开关 ----------
def test_disabled():
    print('\n[2] `stall_seconds=0` = 关闭检测（回到改动前行为）')
    d = _daemon(0)
    check('★ 关闭后即使 24 小时没收到帧也不判僵死',
          d._stalled(_StubFetcher(86400)) is False)
    check('★ 关闭是真的关（0 < 0 为假）—— 负值也不会误开',
          _daemon(-1)._stalled(_StubFetcher(86400)) is False)


# ---------- 3. fetcher 侧的真实语义 ----------
def test_frame_age_real():
    print('\n[3] `DouyinLiveWebFetcher.frame_age()` 的真实语义')
    f = DouyinLiveWebFetcher('000', None)      # __init__ 不发请求、不建线程
    check('★ 构造时就置位 —— 否则「还没收到第一帧」会被算成「很久没收到帧」，'
          '刚连上就被判僵死', f.frame_age() < 5.0, f'{f.frame_age()}s')
    check('确实是「距最后一帧」的秒数', hasattr(f, 'last_frame_ts'))
    # 把时刻往前拨 400 秒，等价于「400 秒没收到任何帧」
    f.last_frame_ts -= 400
    check('★ 400 秒没收到帧 → frame_age 约 400',
          399 <= f.frame_age() <= 402, f'{f.frame_age()}s')
    check('★ 并据此判僵死（与判据串起来）', _daemon()._stalled(f) is True)
    # 模拟「收到一帧」→ 立刻恢复健康
    f.last_frame_ts = time.time()
    check('收到一帧后立刻不再是僵死（判据是可自愈的，不是一次性的）',
          _daemon()._stalled(f) is False)


# ---------- 4. session_meta.json ----------
def test_session_meta():
    print('\n[4] `session_meta.json` 真的写对内容')
    tmp = tempfile.mkdtemp(prefix='stall_meta_')
    try:
        d = _daemon()
        start = datetime.now() - timedelta(minutes=42, seconds=7)
        d._write_session_meta(
            tmp, start, 2527.0, 'stall', 'gate_no_slot', 812.3,
            {'ok': False, 'frames': [], 'audio': [], 'restarts': 0})
        p = os.path.join(tmp, 'session_meta.json')
        check('文件已写出', os.path.exists(p))
        meta = json.load(open(p, encoding='utf-8'))
        check('★ stop_reason 记的是 stall（这就是排查时要的那一项）',
              meta['stop_reason'] == 'stall', meta.get('stop_reason'))
        check('时长被记下', abs(meta['duration_s'] - 2527.0) < 1, meta.get('duration_s'))
        check('起止时间都在', bool(meta['started_at']) and bool(meta['ended_at']))
        check('★ 没产出的原因记的是 gate_no_slot（与「启动失败」区分开）',
              meta['media']['skip_reason'] == 'gate_no_slot', meta['media'])
        check('★ 排队时长被记下（用于判断名额是不是长期不够）',
              abs(meta['media']['gate_wait_s'] - 812.3) < 0.1, meta['media'])
        check('音视频产出计数为 0', meta['media']['frames'] == 0
              and meta['media']['audio'] == 0)
        # WS 断线重连统计（见 test_ws_reconnect_unit.py）：不传 fetcher 时也要在，
        # 否则老调用点写出来的 meta 缺键，读方得为「文件格式有两个版本」做兼容。
        check('★ 重连统计字段始终存在（WS 直连的正常场次就是 0，不是缺键）',
              meta.get('ws_reconnects') == 0 and meta.get('ws_gap_s') == 0.0,
              (meta.get('ws_reconnects'), meta.get('ws_gap_s')))

        # 成功产出的那一场：skip_reason 必须是 None，否则台账会把好场次判成不完整
        d._write_session_meta(tmp, start, 60.0, 'offline', None, 0.0,
                             {'ok': True, 'frames': [1, 2], 'audio': [1], 'restarts': 2})
        meta2 = json.load(open(p, encoding='utf-8'))
        check('★ 正常产出的场次 skip_reason 为 None（不能标成「有原因 = 不好」）',
              meta2['media']['skip_reason'] is None, meta2['media'])
        check('产出计数如实', meta2['media']['frames'] == 2 and meta2['media']['audio'] == 1
              and meta2['media']['restarts'] == 2 and meta2['media']['ok'] is True)

        # 写不进去也不能抛（磁盘满 / 权限）—— 不能把一场已采完的数据带崩
        bad = os.path.join(tmp, 'nonexistent', 'x')
        try:
            d._write_session_meta(bad, start, 1.0, 'offline', None, 0.0, None)
            check('★ 目录不存在时只打日志、不抛异常', True)
        except Exception as e:
            check('★ 目录不存在时只打日志、不抛异常', False, repr(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 5. 契约 ----------
def test_contract():
    print('\n[5] 契约：判据挂在唯一收口上，且可关闭')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    crawl = open(os.path.join(root, 'auto_crawl.py'), encoding='utf-8').read()
    man = open(os.path.join(root, 'liveMan.py'), encoding='utf-8').read()

    check('★ 阈值是 300 秒', STALL_SECONDS == 300, STALL_SECONDS)
    # `__init__` 里也有一行同样的赋值（构造时置位），所以不能拿「第一次出现」比位置 ——
    # 必须真的切出 `_wsOnMessage` 的函数体再看它在不在里面。
    body = man[man.index('def _wsOnMessage'):]
    body = body[:body.index('\n    def ')]          # 截到同缩进的下一个方法为止
    check('★ 最后一帧时刻记在 `_wsOnMessage` 的函数体里'
          '（所有下行帧的唯一必经之路，心跳 need_ack 也走这里）',
          'self.last_frame_ts = time.time()' in body, body[:200])
    check('★ `frame_age()` 是 fetcher 的方法（不是别处算的）',
          'def frame_age(self):' in man and 'return time.time() - self.last_frame_ts' in man)
    check('★ 判据用「距最后一帧」而不是「距最后一条弹幕」'
          '（安静房间会几分钟没弹幕，用弹幕会误杀）',
          'fetcher.frame_age()' in crawl and 'last_danmaku' not in crawl)
    check('★ `_one_cycle` 真的用了这个判据', 'if self._stalled(fetcher):' in crawl)
    check('★ 僵死时的 stop_reason 是 stall', "stop_reason = 'stall'" in crawl)
    check('★ 脚本里有 --stall-sec 开关（0 = 关闭）',
          "'--stall-sec'" in crawl and 'default=STALL_SECONDS' in crawl)
    check('★ --stall-sec 负数会报错（而不是被当成关闭）',
          "'--stall-sec 不能为负" in crawl)
    check('★ 僵死结束走的是**已有**收尾路径（不另写一套清理逻辑）',
          "stop_reason = 'stall'" in crawl and 'recorder.close()' in crawl)
    check('★ 收尾时写了 session_meta.json',
          '_write_session_meta(recorder.path' in crawl
          and "'session_meta.json'" in crawl)


if __name__ == '__main__':
    print('=' * 64)
    print('WS 僵死检测 + session_meta.json 单测（合成数据，不起线程/不打网络）')
    print('=' * 64)
    test_stalled_truth_table()
    test_disabled()
    test_frame_age_real()
    test_session_meta()
    test_contract()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 僵死仍会卡住线程 —— 该主播之后每次开播都会整场漏采')
        sys.exit(1)
    print('✅ 僵死会被识别成本场结束，线程回到探测循环重连')
