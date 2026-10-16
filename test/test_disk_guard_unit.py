#!/usr/bin/python
# coding:utf-8
"""单测：`auto_crawl._DiskGuard` —— 全模态的磁盘保护（2026-10-15 上服务器前加）。

背景（实测数字，不是估算）：服务器 2C2G / 40G 盘，全模态单路一场 600s = **34 MB**
（帧 14 MB + 音频 20 MB）→ 约 **4.9 GB/天/路**，`--max-media 5` 就是 ~24 GB/天；
而**文本+数值每场只占 ~200 KB**（1012 KB / 5 场）。所以「剩余不足 → 本场只采文本」
几乎不损失研究价值，却能让 daemon 长期活下去。反过来磁盘写满会让整个 daemon 报错、
被强杀时留下没有 `room.json` 的残缺场次。

这里用**合成数据**验语义（monkeypatch `shutil.disk_usage`），不打网络、不碰真磁盘：
  1. 够用 → 放行
  2. 不足 → 拦住（且**不**占用并发闸门名额）
  3. 度量失败 → **放行**（fail-open，只告警一次）
  4. 路径不存在 → 向上找到最近的祖先再量（`data/` 还没建时也要能量）
  5. 受阻提示节流：不给它刷屏的机会，但空间恢复后再受阻要立刻报
  6. `--min-free-gb 0` = 关闭；负数被 argparse 拒掉

用法：python test/test_disk_guard_unit.py
"""
import io
import os
import shutil
import sys
import tempfile
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                  errors='replace', line_buffering=True)
except Exception:
    pass

import auto_crawl
from auto_crawl import _DiskGuard, AnchorDaemon

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


_Usage = namedtuple('usage', 'total used free')


class fake_disk:
    """上下文管理器：把 shutil.disk_usage 换成可编程的假实现

    `free` 传数字 = 固定剩余字节；传 'raise' = 抛 OSError（模拟挂载点消失）。
    `seen` 记录被问过的路径，用来验「路径不存在时向上找祖先」。
    """

    def __init__(self, free, total=40 << 30):
        self.free = free
        self.total = total
        self.seen = []

    def __enter__(self):
        self._orig = shutil.disk_usage

        def fake(path):
            self.seen.append(path)
            if self.free == 'raise':
                raise OSError(2, 'No such file or directory')
            return _Usage(self.total, self.total - self.free, self.free)

        shutil.disk_usage = fake
        return self

    def __exit__(self, *a):
        shutil.disk_usage = self._orig


GB = 1 << 30


# ---------- 1. 够用 → 放行 ----------
def test_allow_when_enough():
    print('\n[1] 剩余充足 → 放行')
    with fake_disk(20 * GB):
        g = _DiskGuard(5.0, path=tempfile.gettempdir())
        ok, free = g.check()
    check('20 GB >= 5 GB 阈值 → True', ok is True, f'got {ok}')
    check('剩余数值如实返回（20.0 GB）', abs(free - 20.0) < 1e-6, f'got {free}')


# ---------- 2. 不足 → 拦住 ----------
def test_block_when_low():
    print('\n[2] 剩余不足 → 拦住')
    with fake_disk(3 * GB):
        g = _DiskGuard(5.0, path=tempfile.gettempdir())
        ok, free = g.check()
    check('3 GB < 5 GB 阈值 → False', ok is False, f'got {ok}')
    check('剩余数值如实返回（3.0 GB）', abs(free - 3.0) < 1e-6, f'got {free}')


def test_boundary_exact():
    print('\n[2b] 边界：恰好等于阈值')
    with fake_disk(5 * GB):
        ok, _ = _DiskGuard(5.0, path=tempfile.gettempdir()).check()
    check('剩余 5.0 GB == 阈值 5.0 → 放行', ok is True, f'got {ok}')


# ---------- 3. 度量失败 → 放行（fail-open）----------
def test_fail_open():
    print('\n[3] 度量失败 → 放行（fail-open，只告警）')
    with fake_disk('raise'):
        g = _DiskGuard(5.0, path=tempfile.gettempdir())
        ok, free = g.check()
    check('disk_usage 抛 OSError → True（宁可采，不因量不出而停采）', ok is True, f'got {ok}')
    check('剩余返回 None（调用方据此显示「无法测量」）', free is None, f'got {free}')
    check('首次失败告警过、不重复告警（防刷屏）',
          g._warned_unmeasurable is True)


# ---------- 4. 路径不存在 → 向上找祖先 ----------
def test_missing_path_walks_up():
    print('\n[4] 路径不存在 → 向上找最近的祖先')
    missing = os.path.join(tempfile.gettempdir(), '__no_such_dir_42__', 'data')
    check('构造的路径确实不存在', not os.path.exists(missing))
    with fake_disk(10 * GB) as d:
        g = _DiskGuard(5.0, path=missing)
        ok, _ = g.check()
    check('仍能量出剩余（说明向上找了祖先）', ok is True)
    check('量的是**存在的祖先**而不是那个不存在的路径',
          d.seen and not d.seen[0].endswith('__no_such_dir_42__/data'),
          f'seen={d.seen}')
    check('量的是绝对路径', d.seen and os.path.isabs(d.seen[0]), f'seen={d.seen}')


# ---------- 5. 告警节流 ----------
def test_report_throttle():
    print('\n[5] 受阻提示节流')
    with fake_disk(1 * GB):
        g = _DiskGuard(5.0, path=tempfile.gettempdir(), log_interval=1800)
        first = g.should_report_block()      # 第一次必报
        second = g.should_report_block()     # 立刻再问 → 节流掉
        g.reset_report()                     # 空间恢复后再受阻
        third = g.should_report_block()      # → 立刻重新可报
    check('首次受阻 → 报', first is True)
    check('紧接着再受阻 → 不报（节流）', second is False)
    check('reset 后立刻受阻 → 报（不用干等 30 分钟窗口）', third is True)


# ---------- 6. daemon 侧：磁盘闸门先于并发闸门 ----------
def test_daemon_skips_before_gate():
    print('\n[6] daemon：磁盘不足时**不占**并发闸门名额')
    with fake_disk(1 * GB):
        guard = _DiskGuard(5.0, path=tempfile.gettempdir())
        gate = auto_crawl._MediaGate(1)
        d = AnchorDaemon('测试主播', '000', media=True, media_gate=gate, disk_guard=guard)
        allowed = d._media_allowed()
        # 模拟 _one_cycle 的短路：_media_allowed() 为 False 时根本不会走到 acquire
        took_gate = allowed and gate.acquire()
    check('_media_allowed() 在空间不足时返回 False', allowed is False, f'got {allowed}')
    check('并发闸门没被取走（名额没被白占）', took_gate is False)
    check('未配置 guard → 放行（改动前行为不变）',
          AnchorDaemon('x', '0', media=True)._media_allowed() is True)


# ---------- 7. argparse 口径 ----------
def test_cli_contract():
    print('\n[7] 命令行口径')
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'auto_crawl.py'), encoding='utf-8').read()
    check("有 '--min-free-gb' 参数", "'--min-free-gb'" in src)
    check('默认 5.0 GB', "default=5.0" in src)
    check('负数被拒（0 才是关闭语义）', '--min-free-gb 不能为负' in src)
    check('`--media` 分支里 0 会明确提示保护已关',
          '磁盘保护已关闭' in src)


if __name__ == '__main__':
    print('=' * 64)
    print('_DiskGuard 磁盘保护单测（合成数据，不打网络）')
    print('=' * 64)
    test_allow_when_enough()
    test_block_when_low()
    test_boundary_exact()
    test_fail_open()
    test_missing_path_walks_up()
    test_report_throttle()
    test_daemon_skips_before_gate()
    test_cli_contract()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 磁盘保护语义有误')
        sys.exit(1)
    print('✅ 磁盘保护语义正确（放行/拦住/fail-open/节流/不占名额）')
