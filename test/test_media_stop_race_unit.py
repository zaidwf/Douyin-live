#!/usr/bin/python
# coding:utf-8
"""单测：`MediaCapture` 收尾时**不得漏 ffmpeg 孤儿进程**（2026-10-14 压测暴露）。

背景（实测，不是假想）：音视频阶梯跑到 N=16/20 档后，系统里残留了 **4 个 ffmpeg**
一直占着流带宽，其中一个从 M16 档横跨到 M20 档结束。

根因是**收尾竞态**：
  1. watchdog 醒来发现 ffmpeg 早退，进入 `_resolve_stream_url()`（**网络调用**，
     拥塞时数秒）
  2. 主线程此时调 `stop_media()`：置 `_stop_event`、停掉当前那对（早已是死进程，
     空操作）、`join(timeout=2)` —— 但 watchdog 还在网络调用里，join **超时返回**
  3. `stop_media()` 返回，采集"已停"
  4. watchdog 的网络调用返回了，继续 `_spawn()` 出一对**没人收**的 ffmpeg

三处修补（对应下面三个用例）：
  - `_spawn()` 见到停止信号直接 return（用例 1）
  - watchdog 在 `_spawn()` **之后**自查，若期间收到停止信号就自己把这对自己收掉（用例 2）
  - `stop_media()` join 之后再扫一遍（用例 3）

用假进程（`python -c "time.sleep(...)"`）冒充 ffmpeg：`_graceful_stop` 写 `q` 到
stdin 后再 terminate，假进程不吃 `q` 也无所谓。

用法：python test/test_media_stop_race_unit.py
"""
import io
import os
import shutil
import subprocess
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
from liveMan import MediaCapture

SLEEP_CMD = [sys.executable, '-c', 'import time; time.sleep(300)']
FAKE_URL = 'http://127.0.0.1:1/never.flv'     # 不会被真正拉流，只是占位

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


def alive(proc):
    return proc is not None and proc.poll() is None


def make_cap(tmp, merged=False):
    """造一个 MediaCapture，返回的假进程会一直活着（便于观测泄漏）

    默认 `merged=False`（拆成两个假进程）—— 收尾竞态的用例要能**分别**观察两个槽位。
    合并模式的用例见 test_merged_single_process。
    """
    cap = MediaCapture('0000000', tmp, merged=merged, log=lambda *a: None)
    cap.stream_url = FAKE_URL
    cap._audio_cmd = lambda url, n=0: list(SLEEP_CMD)
    cap._frame_cmd = lambda url, n=0: list(SLEEP_CMD)
    cap._merged_cmd = lambda url, an=0, fn=0: list(SLEEP_CMD)
    os.makedirs(cap.audio_dir, exist_ok=True)
    os.makedirs(cap.frames_dir, exist_ok=True)
    return cap


def reap(cap):
    seen = []
    for p in (cap.audio_proc, cap.frame_proc):
        if p is not None and all(p is not q for q in seen):
            seen.append(p)
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)


# ---------------------------------------------------------------- 用例 1
def test_spawn_refuses_after_stop():
    """置位停止信号后 `_spawn()` 必须一个进程都不起"""
    print('\n[1] `_spawn()` 在停止后拒绝起进程')
    tmp = tempfile.mkdtemp(prefix='media_race_')
    try:
        cap = make_cap(tmp)
        cap._stop_event.set()
        cap._spawn()
        check('停止后 _spawn() 未创建音频进程', cap.audio_proc is None)
        check('停止后 _spawn() 未创建抽帧进程', cap.frame_proc is None)
        reap(cap)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例 2
def test_watchdog_reaps_its_own_spawn_on_race():
    """停止信号在 `_resolve_stream_url()` **期间**到达 —— watchdog 起来后必须自查并收掉

    构造法：让 `_resolve_stream_url` 自己把 `_stop_event` 置位再返回地址，精确复现
    「停止发生在 resolve 内、spawn 在外」这个窗口。
    """
    print('\n[2] watchdog 在 spawn 后自查收尾（停止信号在 resolve 期间到达）')
    tmp = tempfile.mkdtemp(prefix='media_race_')
    try:
        cap = make_cap(tmp)

        def resolve_then_stop():
            cap._stop_event.set()          # ← 停止信号就发生在这里面
            return FAKE_URL

        cap._resolve_stream_url = resolve_then_stop
        cap.audio_proc = None
        cap.frame_proc = None
        # 直接跑一轮 watchdog 的续采体：伪造一个「早退」的进程
        cap.want_audio = cap.want_frames = True
        cap.audio_proc = subprocess.Popen(SLEEP_CMD, stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        cap.frame_proc = subprocess.Popen(SLEEP_CMD, stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        cap.audio_proc.kill(); cap.audio_proc.wait(timeout=5)      # 冒充「早退」
        cap.frame_proc.kill(); cap.frame_proc.wait(timeout=5)
        cap.audio_proc = None       # watchdog 的 dead 判定已在下面手写，这里清引用

        # 手工走一遍 watchdog 的续采分支（`_watchdog` 里的 wait(INTERVAL) 不便单测）
        url = cap._resolve_stream_url()
        assert url == FAKE_URL
        cap.stream_url = url
        cap.restarts += 1
        cap._spawn()                                    # 内部守卫会拦住
        if cap._stop_event.is_set():                    # watchdog 尾部的自查
            cap._kill_procs()
        check('竞态后无存活音频进程', not alive(cap.audio_proc))
        check('竞态后无存活抽帧进程', not alive(cap.frame_proc))
        reap(cap)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例 3
def test_leak_when_watchdog_blocks_in_resolve():
    """端到端：watchdog 卡在网络调用里时 `stop_media()`，不得漏进程

    这是压测现场的真实时序。把 `MEDIA_WATCHDOG_INTERVAL` 临时调到 1s 以免等 30s。
    """
    print('\n[3] 端到端：watchdog 卡在 resolve 时 stop_media() 不漏进程')
    tmp = tempfile.mkdtemp(prefix='media_race_')
    old_interval = liveMan.MEDIA_WATCHDOG_INTERVAL
    try:
        liveMan.MEDIA_WATCHDOG_INTERVAL = 1
        cap = make_cap(tmp)
        # ⚠️ 必须**分次**给命令：第 1 次即时退出（好让 watchdog 判定「早退」进续采），
        # 第 2 次起长活（这样万一漏了，泄漏出来的进程是**活着的**、能被观测到）。
        # 早先两版测试都用一个「即时退出」的命令，漏出来的进程当场就死了，
        # 于是**旧代码也能通过** —— 测试不敏感（已实测踩过）。
        instant = [sys.executable, '-c', 'pass']
        # 音频与抽帧各自计数（同一个 MediaCapture 里两者都会走一次首轮）
        a_calls = {'n': 0}
        f_calls = {'n': 0}

        def audio_cmd(url, n=0):
            a_calls['n'] += 1
            return list(instant if a_calls['n'] == 1 else SLEEP_CMD)

        def frame_cmd(url, n=0):
            f_calls['n'] += 1
            return list(instant if f_calls['n'] == 1 else SLEEP_CMD)
        cap._audio_cmd = audio_cmd
        cap._frame_cmd = frame_cmd
        # resolve 慢 6s，模拟拥塞下的网络请求
        def slow_resolve():
            time.sleep(6)
            return FAKE_URL
        cap._resolve_stream_url = slow_resolve

        cap.start_media()
        time.sleep(4)            # t≈4：watchdog 已在 resolve 里（它 t=1 醒来、要睡到 t=7）
        t_stop = time.time()
        cap.stop_media()         # join(2) 会超时返回
        print(f'     stop_media() 返回耗时 {time.time() - t_stop:.1f}s（>2s 即 join 超时）')
        time.sleep(8)            # 等 watchdog 走完它的 resolve 与 spawn 分支
        check('stop 后无音频进程存活', not alive(cap.audio_proc),
              f'audio_proc={cap.audio_proc}')
        check('stop 后无抽帧进程存活', not alive(cap.frame_proc),
              f'frame_proc={cap.frame_proc}')
        check('watchdog 线程已退出', not (cap._watchdog_thread
                                          and cap._watchdog_thread.is_alive()))
        reap(cap)
    finally:
        liveMan.MEDIA_WATCHDOG_INTERVAL = old_interval
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例 4
def test_merged_single_process():
    """合并模式：一路只有**一个** ffmpeg，两个 slot 指向同一对象，收停去重"""
    print('\n[4] 合并模式 = 单进程（两 slot 别名同一对象，收停不重复停）')
    tmp = tempfile.mkdtemp(prefix='media_merge_')
    try:
        cap = make_cap(tmp, merged=True)
        check('两个模态都要时 merged 生效', cap.merged is True)
        cap._spawn()
        check('只起了一个进程', alive(cap.audio_proc))
        check('audio_proc 与 frame_proc 是同一对象',
              cap.audio_proc is not None and cap.audio_proc is cap.frame_proc)
        pid = cap.audio_proc.pid
        cap._kill_procs()
        check('停止后无存活进程', not alive(cap.audio_proc) and not alive(cap.frame_proc))
        check('停止后引用已清空', cap.audio_proc is None and cap.frame_proc is None)
        check('原 pid 确已退出', not _pid_alive(pid))
        cap._stop_event.set()
        cap._spawn()
        check('停止后 _spawn() 不再起进程', cap.audio_proc is None)
        reap(cap)

        # 只要一个模态时自动退化（没有第二个输出可合）
        check('只要音频时不合并',
              MediaCapture('0', tmp, want_frames=False, log=lambda *a: None).merged is False)
        check('显式 merged=False 时不合并', make_cap(tmp, merged=False).merged is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例 5
def test_merged_cmd_shape():
    """合并命令的**形状**：这是最容易写错、错了又不会立刻报错的地方

    尤其是 `-t` —— 放在 `-i` 之后它就只作用于第一个输出，第二个输出彻底失去防僵尸
    上限（实测踩过：探针里视频输出不受约束，靠发 `q` 才停）。
    """
    print('\n[5] 合并命令形状（`-t`/`-skip_frame` 必须在 `-i` 之前、两个 `-map`）')
    tmp = tempfile.mkdtemp(prefix='media_cmd_')
    try:
        cap = MediaCapture('0', tmp, max_seconds=600, log=lambda *a: None)
        cmd = cap._merged_cmd('http://x/y.flv', 3, 7)
        i = cmd.index('-i')
        check('`-t` 在 `-i` 之前（否则只作用于第一个输出）', cmd.index('-t') < i,
              f'cmd={cmd}')
        check('`-skip_frame nokey` 在 `-i` 之前',
              cmd.index('-skip_frame') < i and cmd[cmd.index('-skip_frame') + 1] == 'nokey')
        check('两个 `-map`（0:a 与 0:v 各一）—— 缺了 jpg muxer 会收到音频而报错',
              cmd.count('-map') == 2
              and cmd[cmd.index('-map') + 1] == '0:a' and '0:v' in cmd)
        check('续号续接：音频段号 3 进了命令',
              cmd[cmd.index('-segment_start_number') + 1] == '3')
        check('续号续接：帧号 7 进了命令', cmd[cmd.index('-start_number') + 1] == '7')
        check('音频口径未变（16kHz 单声道 pcm_s16le）',
              cmd[cmd.index('-ar') + 1] == '16000' and cmd[cmd.index('-ac') + 1] == '1'
              and cmd[cmd.index('-acodec') + 1] == 'pcm_s16le')
        check('抽帧口径未变（fps=1/5 原始分辨率、无 scale）',
              cmd[cmd.index('-vf') + 1] == 'fps=0.2' and '-s' not in cmd
              and 'scale' not in ' '.join(cmd))
        one = cap._frame_cmd('http://x/y.flv', 0)
        check('单路抽帧命令也带 skip_frame（且同样在 `-i` 前）',
              one.index('-skip_frame') < one.index('-i'), f'cmd={one}')
        check('单路抽帧命令仍无 `-map`（单输出走默认选流即可）', '-map' not in one)
        check('`-t` 用 max_seconds 而不是别的值', cmd[cmd.index('-t') + 1] == '600')

        # ---- 代理开关：默认必须是**逐字节不变**的生产口径 ----
        check('默认（未配 DOUYIN_STREAM_PROXY）三条命令都不含 `-http_proxy`',
              '-http_proxy' not in cmd and '-http_proxy' not in one
              and '-http_proxy' not in cap._audio_cmd('http://x/y.flv', 0))
        check('默认 _proxy_args() 为空', cap._proxy_args() == [])

        pcap = MediaCapture('0', tmp, max_seconds=600,
                            stream_proxy='http://u:p@ph:1', log=lambda *a: None)
        pc, pf, pa = (pcap._merged_cmd('http://x/y.flv', 0, 0),
                      pcap._frame_cmd('http://x/y.flv', 0),
                      pcap._audio_cmd('http://x/y.flv', 0))
        # `-http_proxy` 与 `-t`/`-skip_frame` 一样是**输入前置选项**：放 `-i` 之后
        # 就变成输出选项，对已建好的连接无效（`-t` 踩过同款坑）
        for nm, c in (('合并', pc), ('抽帧', pf), ('音频', pa)):
            check(f'{nm}命令里 `-http_proxy` 在 `-i` 之前',
                  '-http_proxy' in c and c.index('-http_proxy') < c.index('-i'), f'cmd={c}')
            check(f'{nm}命令的 `-http_proxy` 值是配的代理 URL',
                  c[c.index('-http_proxy') + 1] == 'http://u:p@ph:1')
        check('空串 stream_proxy = 强制直连（不受环境变量影响）',
              MediaCapture('0', tmp, stream_proxy='',
                           log=lambda *a: None)._proxy_args() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例 6
def test_prune_empty_kills_shells():
    """残片判据：**78 字节的空壳 WAV 必须删掉**，1 KiB 以上的一律留下

    2026-10-15 事故：给早已下播的房间起 ffmpeg，拿回的是 44 字节 RIFF 头 + LIST/ISFT
    元数据 + **长度为 0 的 `data` 块**，共 **78 字节**。旧判据只删「0 字节」，
    于是这些空壳**被当成了有效音频段**（`ffprobe` 连时长都读不出来），
    统计出来的「有音视频的场次」里 59% 实际产出为 0。

    门槛踩在这个真实字节数上：1 KiB 在 78 字节之上（删掉），16kHz 单声道下
    1 KiB ≈ 32ms（不可能是一个 30s 切片的正常大小）。
    """
    print('\n[6] `_prune_empty` 删残片、留真产物（78 字节空壳是回归点）')
    tmp = tempfile.mkdtemp(prefix='media_prune_')
    try:
        cap = MediaCapture('0', tmp, log=lambda *a: None)
        os.makedirs(cap.audio_dir, exist_ok=True)
        cases = {
            'segment_0000.wav': 0,      # 硬杀留下的空文件
            'segment_0001.wav': 78,     # ★ 事故里的空壳（长度 0 的 data 块）
            'segment_0002.wav': 1023,   # 门槛下一字节 —— 仍是残片
            'segment_0003.wav': 1024,   # 门槛本身 —— 留下
            'segment_0004.wav': 960000, # 30s @16kHz 单声道 pcm_s16le = 960 KB
        }
        for name, size in cases.items():
            with open(os.path.join(cap.audio_dir, name), 'wb') as f:
                f.write(b'\x00' * size)

        alive = cap._prune_empty(os.path.join(cap.audio_dir, 'segment_*.wav'))

        check('★ 78 字节的空壳被删掉（旧判据会把它当有效音频段）',
              not os.path.exists(os.path.join(cap.audio_dir, 'segment_0001.wav')))
        check('0 字节被删掉', not os.path.exists(os.path.join(cap.audio_dir, 'segment_0000.wav')))
        check('门槛(1024) 保留', os.path.exists(os.path.join(cap.audio_dir, 'segment_0003.wav')))
        check('正常切片保留', os.path.exists(os.path.join(cap.audio_dir, 'segment_0004.wav')))
        check('返回值只含存活文件、且有序（续号要看最后一个）',
              alive == [os.path.join(cap.audio_dir, n)
                        for n in ('segment_0003.wav', 'segment_0004.wav')], f'{alive}')
        check('门槛是 1 KiB 且是类常量（别再退回「只删 0 字节」）',
              MediaCapture.MEDIA_MIN_VALID_BYTES == 1024)

        # 全是残片时 ok 必须为 False —— 调用方靠它区分「这场真采到了」和「起了个空壳」
        for name in ('segment_0003.wav', 'segment_0004.wav'):
            os.remove(os.path.join(cap.audio_dir, name))
        os.makedirs(cap.frames_dir, exist_ok=True)
        with open(os.path.join(cap.frames_dir, 'frame_0000.jpg'), 'wb') as f:
            f.write(b'\x00' * 78)       # 一个截断的 JPEG 也算残片
        res = cap.stop_media()
        check('全是残片时 ok=False（不能再报成「有音视频」）', res['ok'] is False, f'{res}')
        check('全是残片时回捞列表为空', res['audio'] == [] and res['frames'] == [], f'{res}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _pid_alive(pid):
    """该 pid 是否仍活着（用 psutil，跨平台；没装就退回保守的 True）"""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return True


if __name__ == '__main__':
    print('=' * 64)
    print('MediaCapture 收尾竞态单测（不得漏 ffmpeg 孤儿进程）')
    print('=' * 64)
    test_spawn_refuses_after_stop()
    test_watchdog_reaps_its_own_spawn_on_race()
    test_leak_when_watchdog_blocks_in_resolve()
    test_merged_single_process()
    test_merged_cmd_shape()
    test_prune_empty_kills_shells()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 有泄漏路径未堵住')
        sys.exit(1)
    print('✅ 三条路径均不漏 ffmpeg 孤儿进程')
