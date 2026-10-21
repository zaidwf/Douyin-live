#!/usr/bin/python
# coding:utf-8
"""单测：媒体**停滞**判据 —— ffmpeg 活着，但再也不往盘上写东西。

## 这条判据防的是什么（2026-10-17 实测，非推演）

三场长直播的媒体在**整 8 小时**处静默停摆：

| 场次 | 开场 | 最后帧 | 帧/音频段 | 停摆时长 |
|---|---|---|---|---|
| 1637665557  | 03:14:34 | 11:14:39 | **5760 / 960** | 4.4 h |
| 21518711476 | 05:55:10 | 13:55:16 | **5760 / 961** | 1.7 h |
| 35596105925 | 06:32:43 | 14:32:50 | **5760 / 961** | 1.1 h |

帧数**一模一样 5760**（= 480 分钟 × 12 帧/分）、音频段 960（= 480 × 2），最后一帧都
在开场后 **8h00m05s / 06s / 07s** —— 是抖音 CDN 在 8 小时处停推。

而 `_watchdog` 原来只判 `proc.poll() is not None`（**进程退出**）：实测这三个 ffmpeg
**都还活着**（存活 12:25 / 09:45 / 09:07，CPU 仍占 1.4–1.7%），于是既没触发续采、
也没有任何日志 —— 这一场剩下的媒体**永久缺失**，而所有指标看起来都正常。

新判据：某模态的**产物文件计数**在阈值内一次没涨 → 判停滞 → 强杀 + 重解析地址续采。
计数口径选**非空文件个数**而不是 mtime：`frame_*.jpg` 每 5 秒一个新文件、
`segment_*.wav` 每 30 秒一段，个数只会单调增加。

## 2026-10-18 23:00 的第二次断流：停止键是我们自己按的

部署后当晚三场 15:00 开播的场次正好撞上 8 小时线：

- `26915370405`：ffmpeg **退出**（不是停摆）→ 「提前退出」路径续采成功，
  帧数 5760 → 5856 继续涨；
- `21518711476` / `49822721018`：同样停在 5760 帧 / 960 段，但**一声没吭** ——
  `_watchdog` 在 `max_seconds`（8h）到点处 `return` 了，连「进程退出」都判不到。
  两个 ffmpeg 变成 `<defunct>` 僵尸（PPID 是守护进程，死亡时刻正好是开场 + 8h00m）。

根因不是 CDN：**`-t 28800` 是我们自己加的单进程防僵尸上限**，健康流会准时正常退出。
第 3 场能续上只是恰好赶上守卫 `return` 前的一轮。修法是删掉那句 `return`，让
`-t` 到点走正常的「进程退出 → 续采」路径；用例 `[3b]` 钉住它。

## 用例（合成对象，不打网络、不起真 ffmpeg）

  [1] `media_output_progress()` 真值表（涨了/没涨/恰好到阈值）
  [2] `_stalled_modalities()`：建基线 / 判停滞 / 涨了不算 / 死进程不归它管 /
      阈值随段长与帧间隔放宽 / `media_stall=0` 关闭
  [3] watchdog 集成：停滞 → **先杀后 spawn**、`stalls`/`restarts` 各加一、有日志
  [3b] watchdog **活过 `max_seconds`**：`-t` 到点的正常退出也必须续采（10-18 事故）
  [4] `stop_media()` 的返回值带 `stalls`
  [5] 契约：阈值 300、判据挂在 `_watchdog`、`session_meta` 记 `stalls`、
      续采解析失败不再静默、watchdog 里没有 `max_seconds` 的提前 return

用法：python test/test_media_stall_unit.py
"""
import io
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                  errors='replace', line_buffering=True)
except Exception:
    pass

import liveMan
from liveMan import MEDIA_STALL_SECONDS, MediaCapture, media_output_progress

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


class _FakeProc:
    """够 `_graceful_stop` / `_kill_procs` 走完的假进程（不吃 q，直接 terminate）"""

    def __init__(self, alive=True):
        self.alive = alive
        self.stdin = None          # `_graceful_stop` 里 `.write` 会 AttributeError → 被吞
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated += 1
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed += 1
        self.alive = False


def make_cap(tmp, **kw):
    """造一个 MediaCapture + 两个有产物的目录 + 活着的假进程"""
    cap = MediaCapture('0000000', tmp, log=lambda *a: None, **kw)
    os.makedirs(cap.audio_dir, exist_ok=True)
    os.makedirs(cap.frames_dir, exist_ok=True)
    p = _FakeProc()
    cap.audio_proc = p
    cap.frame_proc = p if cap.merged else _FakeProc()
    return cap


def touch(path, size=2048):
    with open(path, 'wb') as f:
        f.write(b'x' * size)


def frames_glob(cap):
    return os.path.join(cap.frames_dir, 'frame_*.jpg')


def audio_glob(cap):
    return os.path.join(cap.audio_dir, 'segment_*.wav')


# ---------------------------------------------------------------- 用例 1
def test_progress_truth_table():
    print('\n[1] `media_output_progress()` 真值表')
    check('计数涨了 → 不判停滞，基线前移到 now',
          media_output_progress(5, 4, 100.0, 101.0, 300) == (False, 101.0))
    check('没涨、未到阈值 → 不判停滞，时间戳不动',
          media_output_progress(5, 5, 100.0, 399.0, 300) == (False, 100.0))
    check('没涨、恰好到阈值 → 判停滞（>=）',
          media_output_progress(5, 5, 100.0, 400.0, 300) == (True, 100.0))
    check('没涨、超过阈值 → 判停滞，时间戳保持不动（停摆时长会继续涨）',
          media_output_progress(5, 5, 100.0, 1000.0, 300) == (True, 100.0))


# ---------------------------------------------------------------- 用例 2
def test_stalled_modalities():
    print('\n[2] `_stalled_modalities()` 语义')
    tmp = tempfile.mkdtemp(prefix='media_stall_')
    try:
        cap = make_cap(tmp)
        cap._progress['frames'] = (0, 1000.0)
        cap._progress['audio'] = (0, 1000.0)
        check('未到阈值 → 空', cap._stalled_modalities(1299.0) == {})
        st = cap._stalled_modalities(1300.0)
        check('到阈值 → 两路都判停滞', set(st) == {'frames', 'audio'}, str(st))
        check('停摆秒数 = now - 基线', st.get('frames') == 300, str(st))
        check('时间戳没有前移（下一轮停摆时长继续增长）',
              cap._progress['frames'] == (0, 1000.0), str(cap._progress['frames']))

        # 有新产物 → 不算停滞，基线前移
        touch(os.path.join(cap.frames_dir, 'frame_0001.jpg'))
        st = cap._stalled_modalities(1400.0)
        check('帧计数涨了 → 帧不再判停滞', 'frames' not in st, str(st))
        check('基线前移到本轮 now', cap._progress['frames'] == (1, 1400.0),
              str(cap._progress['frames']))
        check('音频没涨 → 仍判停滞', st.get('audio') == 400, str(st))

        # 死进程不归这条判据管
        cap.frame_proc = _FakeProc(alive=False)
        check('进程已退出 → 跳过（交给 poll() 判据）',
              'frames' not in cap._stalled_modalities(99999.0))

        # 阈值随段长/帧间隔放宽：600s 的音频段不可能 300s 涨一次
        cap2 = make_cap(tmp, audio_segment=600, want_frames=False)
        cap2._progress['audio'] = (0, 1000.0)   # 与磁盘上的实际计数一致（0 个段）
        check('段长 600s → 300s 时不算停滞（阈值放宽到 1200s）',
              cap2._stalled_modalities(1300.0) == {})
        check('段长 600s → 1200s 时判停滞',
              cap2._stalled_modalities(2200.0).get('audio') == 1200)

        # 关闭
        cap3 = make_cap(tmp, media_stall=0)
        cap3._progress['frames'] = (0, 0.0)
        check('media_stall=0 → 恒为空（回到只看进程退出的旧行为）',
              cap3._stalled_modalities(1e9) == {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例 3
def test_watchdog_kills_and_respawns():
    print('\n[3] watchdog 集成：停滞 → 先杀后 spawn')
    tmp = tempfile.mkdtemp(prefix='media_stall_')
    old_interval = liveMan.MEDIA_WATCHDOG_INTERVAL
    try:
        logs = []
        cap = MediaCapture('0000000', tmp, log=logs.append,
                           media_stall=0.15, frame_interval=0.01, audio_segment=0.01)
        os.makedirs(cap.audio_dir, exist_ok=True)
        os.makedirs(cap.frames_dir, exist_ok=True)
        touch(os.path.join(cap.frames_dir, 'frame_0001.jpg'))
        touch(os.path.join(cap.audio_dir, 'segment_000.wav'))
        old_proc = _FakeProc()
        cap.audio_proc = cap.frame_proc = old_proc
        cap._progress['frames'] = (1, time.time())
        cap._progress['audio'] = (1, time.time())
        cap._resolve_stream_url = lambda: 'http://127.0.0.1:1/fake.flv'

        events = []

        def fake_kill():
            events.append('kill')
            liveMan.MediaCapture._kill_procs(cap)
            cap._stop_event.set()     # 只观测**第一次**停滞，避免循环里反复触发

        def fake_spawn():
            events.append('spawn')
            p = _FakeProc()
            cap.audio_proc = cap.frame_proc = p
            now = time.time()
            cap._progress['frames'] = (cap._count_files(frames_glob(cap)), now)
            cap._progress['audio'] = (cap._count_files(audio_glob(cap)), now)

        cap._kill_procs = fake_kill
        cap._spawn = fake_spawn
        liveMan.MEDIA_WATCHDOG_INTERVAL = 0.05

        t = threading.Thread(target=cap._watchdog, daemon=True)
        t.start()
        time.sleep(0.6)
        cap._stop_event.set()
        t.join(timeout=3)

        check('停滞触发了一次', cap.stalls == 1, str(cap.stalls))
        check('restarts 也记了这次', cap.restarts == 1, str(cap.restarts))
        check('旧进程被停掉（合并模式只 stop 一次，按对象去重）',
              old_proc.terminated == 1 and not old_proc.alive,
              f'terminated={old_proc.terminated} alive={old_proc.alive}')
        check('先杀后起（kill → spawn）', events[:2] == ['kill', 'spawn'], str(events))
        check('日志说清了原因（活着但不出数）',
              any('活着但不出数' in s for s in logs), str(logs))
    finally:
        liveMan.MEDIA_WATCHDOG_INTERVAL = old_interval
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例 3b
def test_watchdog_outlives_max_seconds():
    """2026-10-18 事故回归：`-t` 到点后 ffmpeg 正常退出，watchdog **不能跟着退出**

    事故现场：`auto_crawl` 传 `max_seconds=8h`，健康流在整 8 小时处被 `-t` 结束；
    旧 watchdog 在 `max_seconds` 到点时直接 `return`，于是那次退出没人续采 ——
    3 场长直播里 2 场的媒体静默停在 5760 帧 / 960 段。
    """
    print('\n[3b] watchdog 必须活过 `max_seconds`（8 小时断流事故回归）')
    tmp = tempfile.mkdtemp(prefix='media_stall_')
    old_interval = liveMan.MEDIA_WATCHDOG_INTERVAL
    try:
        logs = []
        cap = MediaCapture('0000000', tmp, log=logs.append, max_seconds=0.05,
                           media_stall=0.1, frame_interval=0.01, audio_segment=0.01)
        os.makedirs(cap.audio_dir, exist_ok=True)
        os.makedirs(cap.frames_dir, exist_ok=True)
        touch(os.path.join(cap.frames_dir, 'frame_0001.jpg'))
        touch(os.path.join(cap.audio_dir, 'segment_000.wav'))
        cap._t0 = time.time() - 3600        # 开场 1 小时前 —— 早就过了 max_seconds
        # `-t` 到点：ffmpeg **正常退出**（不是僵死），两个引用都指向退出的进程
        cap.audio_proc = cap.frame_proc = _FakeProc(alive=False)
        cap._progress['frames'] = (1, time.time())
        cap._progress['audio'] = (1, time.time())
        cap._resolve_stream_url = lambda: 'http://127.0.0.1:1/fake.flv'

        def fake_kill():
            cap._stop_event.set()       # 只在收尾竞态那一支被调到

        def fake_spawn():
            cap.audio_proc = cap.frame_proc = _FakeProc()
            now = time.time()
            cap._progress['frames'] = (cap._count_files(frames_glob(cap)), now)
            cap._progress['audio'] = (cap._count_files(audio_glob(cap)), now)
            cap._stop_event.set()       # 只观测**第一次**续采，避免循环里再来一轮

        cap._kill_procs = fake_kill
        cap._spawn = fake_spawn
        liveMan.MEDIA_WATCHDOG_INTERVAL = 0.05

        t = threading.Thread(target=cap._watchdog, daemon=True)
        t.start()
        time.sleep(0.5)
        cap._stop_event.set()
        t.join(timeout=3)

        check('过了 max_seconds 仍在看：进程退出被判到并续采一次',
              cap.restarts == 1, str(cap.restarts))
        check('走的是「提前退出」路径而不是停滞',
              any('提前退出' in s for s in logs) and cap.stalls == 0,
              f'logs={logs} stalls={cap.stalls}')
    finally:
        liveMan.MEDIA_WATCHDOG_INTERVAL = old_interval
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例 4
def test_stop_media_reports_stalls():
    print('\n[4] `stop_media()` 的返回值带 stalls')
    tmp = tempfile.mkdtemp(prefix='media_stall_')
    try:
        cap = make_cap(tmp)
        touch(os.path.join(cap.frames_dir, 'frame_0001.jpg'))
        cap.stalls = 3
        res = cap.stop_media()
        check('返回值里有 stalls=3', res.get('stalls') == 3, str(res))
        check('旧字段仍在（restarts/ok）', 'restarts' in res and 'ok' in res, str(res))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例 5
def test_contract():
    print('\n[5] 契约')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'liveMan.py'), encoding='utf-8') as f:
        lm = f.read()
    with open(os.path.join(root, 'auto_crawl.py'), encoding='utf-8') as f:
        ac = f.read()
    check('MEDIA_STALL_SECONDS == 300', MEDIA_STALL_SECONDS == 300)
    check('_watchdog 里调用了停滞判据', '_stalled_modalities(now)' in lm)
    check('停滞路径先 _kill_procs()', '强杀后用新流地址续采' in lm)
    check('stop_media 返回 stalls', "'stalls': self.stalls" in lm)
    check('`_spawn()` 建停滞基线', "_progress['frames']" in lm)
    check('续采解析失败不再静默（有限流打印）', '_resolve_fails % 10' in lm)
    check('session_meta 记 stalls', "'stalls': media.get('stalls', 0)" in ac)
    check('media_stall 默认就是 MEDIA_STALL_SECONDS',
          'media_stall=MEDIA_STALL_SECONDS' in lm)
    check('0 = 关闭（回到旧行为）', 'if self.media_stall <= 0:' in lm)
    check('watchdog 不因 max_seconds 到点提前 return（2026-10-18 事故回归）',
          'max_seconds and (time.time() - self._t0)' not in lm)


if __name__ == '__main__':
    print('=' * 64)
    print('媒体停滞判据单测（ffmpeg 活着但不出数）')
    print('=' * 64)
    test_progress_truth_table()
    test_stalled_modalities()
    test_watchdog_kills_and_respawns()
    test_watchdog_outlives_max_seconds()
    test_stop_media_reports_stalls()
    test_contract()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 有判据不成立')
        sys.exit(1)
    print('✅ 停滞判据成立')
