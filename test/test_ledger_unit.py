#!/usr/bin/python
# coding:utf-8
"""单测：开播台账（`service/live_ledger.py`）—— 完整性判定、人工列保留、删除保护。

## 这张表是**迁移清单**，不是报表

用户会照它**手动把采完整的场次迁走**，不完整的直接删。所以每一列的错法都要命：

- 「完整」判松了 → 用户把缺音视频的场次当成采全了迁走，**研究语料里出现静默缺模态**；
- 「完整」判紧了 → 用户对着没有缺点的场次反复检查，表就没人信了；
- **人工列没保住** → 用户标了 50 行「已迁移」，下一轮重写全没了；
- 删除判宽了 → 把 `_load_test/` 甚至整个 `data/` 带走。

下面用**合成目录**验真实的扫描路径（不是只喂 dict 给纯函数），因为最容易错的地方恰恰
在扫描层：「78 字节的空壳 WAV 算不算一段音频」这种事，纯函数看不见。

**不起线程、不打网络、不碰生产 `data/`**（`DATA_ROOT` 全程指向临时目录）。

  [1] `classify_session` 真值表（五条分支 + 在采中 + 音视频只有一半）
  [2] 真实目录扫描：78 字节空壳不算音频（1 KiB 门槛穿透整条链路）
  [3] 在采中判据用**目录内最新文件** mtime，不是目录 mtime（今天踩过的口径坑）
  [4] `scan_all_sessions` 只认 8 位日期目录 → `_load_test/` 天然跳过
  [5] 删除保护：只删 `data/<8位日期>/live_*`，且不删在采中的
  [6] 人工列（`是否已迁移`/`备注`）在重写后**必须还在**
  [7] `find_missed` 真值表（≥2 轮才算漏采）
  [8] 契约：复用 `load_anchors`/`classify_status`、限流不另写、timer 周期 30 分钟

用法：python test/test_ledger_unit.py

⚠️ 本文件**不要**再包一层 `sys.stdout` —— `live_ledger` 导入时已经包过，
双层会让内层被 GC 掉、之后所有 print 报 `I/O operation on closed file`
（`test_health_check_unit.py` 顶部同款注释）。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import tokenize
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'service'))

import live_ledger as ll

_ok = _fail = 0


def check(desc, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {desc}')
    else:
        _fail += 1
        print(f'  ❌ {desc}' + (f'  ← {detail}' if detail else ''))


# ---------------------------------------------------------------- 造场次目录
def make_session(root, name, room_json=True, danmaku=2, events=2, audio=1, frames=1,
                 wav_bytes=None, meta=None, write_age_s=0):
    """造一个场次目录。

    :param wav_bytes: WAV **文件总字节数**（测 78 字节空壳与 1024 边界）；None = 2 KiB
    :param write_age_s: 把所有文件的 mtime 往前拨这么多秒（测「在采中」判据）
    """
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    if room_json:
        json.dump({'room_id': '1', 'title': '测试'},
                  open(os.path.join(d, 'room.json'), 'w', encoding='utf-8'))
    ts = datetime.now().isoformat(timespec='seconds')
    for fn, n in (('danmaku.jsonl', danmaku), ('events.jsonl', events)):
        with open(os.path.join(d, fn), 'w', encoding='utf-8') as f:
            for i in range(n):
                f.write(json.dumps({'type': 'x', 'i': i, 'ts': ts}) + '\n')
    if audio:
        ad = os.path.join(d, 'audio')
        os.makedirs(ad, exist_ok=True)
        # `wav_bytes` 指**文件总长**（4 字节 'RIFF' 魔数 + 其余填 0），不然
        # `wav_bytes=1023` 实际写出 1027 字节，边界测试就测了个寂寞
        total = wav_bytes if wav_bytes is not None else 2048
        with open(os.path.join(ad, 'segment_000.wav'), 'wb') as f:
            f.write(b'RIFF' + b'\0' * max(0, total - 4))
    if frames:
        fd = os.path.join(d, 'frames')
        os.makedirs(fd, exist_ok=True)
        with open(os.path.join(fd, 'frame_0001.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' + b'\0' * 2048)
    if meta is not None:
        json.dump(meta, open(os.path.join(d, 'session_meta.json'), 'w', encoding='utf-8'))
    if write_age_s:
        old = datetime.now().timestamp() - write_age_s
        for dp, _dd, fs in os.walk(d):
            for n in fs:
                os.utime(os.path.join(dp, n), (old, old))
    return d


def facts(**kw):
    """合成事实字典（走 `classify_session` 的契约）"""
    f = {'inflight': False, 'has_room_json': True, 'danmaku_lines': 10,
         'event_lines': 10, 'audio_segs': 3, 'frames': 12, 'meta': None,
         'dir': 'data/20261015/live_x_20261015_100000', 'abspath': '/nowhere',
         'started': datetime(2026, 10, 15, 10, 0, 0),
         'ended': datetime(2026, 10, 15, 11, 0, 0)}
    f.update(kw)
    return f


# ---------- 1. 判定真值表 ----------
def test_classify():
    print('\n[1] `classify_session` 真值表')
    check('★ 全都有（收尾+文本+音视频）→ 完整',
          ll.classify_session(facts())[0] == '完整')
    check('★ 在采中 → `采集中`，**不能**判不完整'
          '（否则刚开播的场次会出现在「可删」列表里，而用户是会照它动手的）',
          ll.classify_session(facts(inflight=True))[0] == '采集中')
    check('★ 无 room.json → 不完整（进程被杀 / 僵死后没重连）',
          ll.classify_session(facts(has_room_json=False))[:2]
          == ('不完整', '未收尾（进程被杀，或 WS 僵死后没能重连）'))
    check('★ 文本与事件全空 → 不完整（无内容）',
          '无内容' in ll.classify_session(
              facts(danmaku_lines=0, event_lines=0, audio_segs=0, frames=0))[1])
    check('★ 有音视频但文本一条都没有 → 点名 WS 侧异常'
          '（ffmpeg 拉 FLV 与 WS 是两条独立通道，不是同一种故障）',
          'WS 侧异常' in ll.classify_session(
              facts(danmaku_lines=0, event_lines=0))[1])
    check('★ 音视频为 0 → 不完整（无音视频）',
          '无音视频' in ll.classify_session(facts(audio_segs=0, frames=0))[1])
    check('★ 只有音频没有帧 → 不完整，且点名缺什么',
          ll.classify_session(facts(frames=0))[1] == '音视频不全（缺帧）')
    check('★ 只有帧没有音频 → 同上',
          ll.classify_session(facts(audio_segs=0))[1] == '音视频不全（缺音频）')
    # 模态列
    check('模态：都有 → 全模态', ll.classify_session(facts())[2] == '全模态')
    check('模态：只有音视频 → 仅音视频（不美化）',
          ll.classify_session(facts(danmaku_lines=0, event_lines=0))[2] == '仅音视频')
    check('模态：只有文本 → 仅文本数值',
          ll.classify_session(facts(audio_segs=0, frames=0))[2] == '仅文本数值')
    check('模态：全空 → 空',
          ll.classify_session(facts(danmaku_lines=0, event_lines=0, audio_segs=0,
                                    frames=0))[2] == '空')
    check('★ 残缺媒体不计入「有音视频」，但也不能报成「仅文本数值」'
          '（那会让人以为 audio/ 下什么都没有）',
          ll.classify_session(facts(audio_segs=0, frames=5))[2] == '文本+残缺音视频')


def test_media_reason():
    print('\n[1b] 「为什么没有音视频」要能区分降级与故障')
    f = lambda skip, wait=0: facts(audio_segs=0, frames=0, meta={
        'media': {'enabled': True, 'ok': False, 'frames': 0, 'audio': 0, 'restarts': 0,
                  'skip_reason': skip, 'gate_wait_s': wait}})
    r = lambda skip, wait=0: ll.classify_session(f(skip, wait))[1]
    check('★ gate_no_slot = 名额没排到（设计内降级，附排队时长）',
          '没排到名额' in r('gate_no_slot', 812.3) and '14 分钟' in r('gate_no_slot', 812.3),
          r('gate_no_slot', 812.3))
    check('★ disk_low = 磁盘降级（设计内）', '磁盘空间不足' in r('disk_low'))
    check('★ start_failed = 启动失败，明说是故障', '★' in r('start_failed'))
    check('★ 没开 --media = disabled', '未开启音视频' in r('disabled'))
    check('★ 老数据没有 session_meta.json → 原因未记录（不猜）',
          '原因未记录' in ll.classify_session(facts(audio_segs=0, frames=0, meta=None))[1])
    check('★ 起了 ffmpeg 却零产出 = 故障，与「没排到名额」分开',
          '★' in r(None))


# ---------- 2. 真实目录扫描 ----------
def test_scan_shell_wav():
    print('\n[2] 真实扫描：78 字节空壳 WAV 不算音频（1 KiB 门槛）')
    tmp = tempfile.mkdtemp(prefix='ledger_scan_')
    try:
        # 已结束的场次：文件 1 小时没写过 → 落在 `--inflight-min 15` 的窗口外，
        # 才会走完整性判定（⚠️ 别用 600：那正好**在** 15 分钟窗口里，会被判「采集中」）
        good = make_session(tmp, 'live_56697889278_20261015_160000', write_age_s=3600)
        f = ll.scan_session(good)
        check('有效场次：扫出 1 段音频', f['audio_segs'] == 1, f['audio_segs'])
        check('★ 扫出来就是「完整」', ll.classify_session(f)[0] == '完整',
              ll.classify_session(f))
        check('解析出抖音号', f['douyin_id'] == '56697889278')
        check('★ 开播时间取目录名里的时间戳（守护 60s 内发现，全场最准）',
              f['started'] == datetime(2026, 10, 15, 16, 0, 0), f['started'])

        # 78 字节空壳：44 字节头 + LIST/ISFT + 长度 0 的 data 块，一个采样点都没有
        shell = make_session(tmp, 'live_999_20261015_170000', danmaku=0, events=0,
                             wav_bytes=78, write_age_s=3600)
        fs = ll.scan_session(shell)
        check('★ 78 字节的壳不算有效音频段', fs['audio_segs'] == 0, fs['audio_segs'])
        check('★ 于是判「无内容」而不是「有音视频」',
              '无内容' in ll.classify_session(fs)[1], ll.classify_session(fs))
        check('★ 门槛与 MediaCapture 是同一个数（一致性契约）', ll.MIN_MEDIA_BYTES == 1024)
        from liveMan import MediaCapture
        check('★ 且真的等于 MediaCapture.MEDIA_MIN_VALID_BYTES',
              ll.MIN_MEDIA_BYTES == MediaCapture.MEDIA_MIN_VALID_BYTES)

        # 1023 字节（差一字节）也不算 —— 边界归严格侧
        edge = make_session(tmp, 'live_888_20261015_180000', wav_bytes=1023)
        check('1023 字节（差 1）不算', ll.scan_session(edge)['audio_segs'] == 0)
        edge2 = make_session(tmp, 'live_887_20261015_180100', wav_bytes=1024)
        check('1024 字节（恰好）算', ll.scan_session(edge2)['audio_segs'] == 1)

        # 非场次目录名要能安全跳过（不然一个杂物目录就崩）
        junk = os.path.join(tmp, 'not_a_session')
        os.makedirs(junk, exist_ok=True)
        check('★ 目录名不匹配时返回 None（不抛异常）', ll.scan_session(junk) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 3. 在采中判据的口径 ----------
def test_inflight_uses_file_mtime():
    print('\n[3] 「在采中」用**目录内最新文件** mtime，不是目录 mtime')
    tmp = tempfile.mkdtemp(prefix='ledger_inflight_')
    try:
        # 文件 2 小时没写过（僵死或已下播），而目录 mtime 是刚刚（刚被创建/清过条目）
        d = make_session(tmp, 'live_111_20261015_160000', write_age_s=7200)
        dir_mtime = datetime.fromtimestamp(os.path.getmtime(d))
        check('前提成立：目录 mtime 是新鲜的（这就是那个坑）',
              (datetime.now() - dir_mtime).total_seconds() < 60,
              f'目录 mtime 差 {(datetime.now() - dir_mtime).total_seconds():.0f}s')
        f = ll.scan_session(d, inflight_min=15)
        check('★ 判为非在采中（用目录 mtime 会得到相反结论，偏差 2 小时）',
              f['inflight'] is False, f['inflight'])
        check('★ 于是能进入完整性判定（不会被当成新开播而跳过）',
              ll.classify_session(f)[0] == '完整', ll.classify_session(f)[0])

        # 刚写过的场次 → 在采中 → 「采集中」→ **不进删除列表**
        d2 = make_session(tmp, 'live_222_20261015_170000')
        f2 = ll.scan_session(d2, inflight_min=15)
        check('刚写过的场次判为在采中', f2['inflight'] is True)
        check('★ 在采中的场次是 `采集中`，不是 `不完整`'
              '（`--delete-incomplete` 因此永远碰不到它）',
              ll.classify_session(f2)[0] == '采集中')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 4. 只认生产目录 ----------
def test_scan_only_production():
    print('\n[4] `scan_all_sessions` 只认 8 位日期目录 → 压测残留天然跳过')
    tmp = tempfile.mkdtemp(prefix='ledger_scanall_')
    old = ll.DATA_ROOT
    try:
        ll.DATA_ROOT = tmp
        make_session(tmp, '20261015/live_111_20261015_160000')
        make_session(tmp, '20261015/live_222_20261015_161000')
        # 下划线前缀 = 非生产（CLAUDE.md 约定）：压测复用同一房间会产出内容重复的场次
        make_session(tmp, '_load_test/20/live_333_20261015_160000')
        make_session(tmp, '_test_residue/live_444_20261015_160000')
        got = ll.scan_all_sessions()
        ids = sorted(f['douyin_id'] for f in got)
        check('★ 只收到 8 位日期目录里的 2 个场次', ids == ['111', '222'], ids)
        check('★ `_load_test/` 被跳过（不能混进研究语料）',
              '333' not in ids and '444' not in ids)
        check('日期被记下（用于按天统计）',
              all(f['date'] == '20261015' for f in got))
    finally:
        ll.DATA_ROOT = old
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 5. 删除保护 ----------
def test_delete_guard():
    print('\n[5] 删除保护：只删 `data/<8位日期>/live_*`')
    old = ll.DATA_ROOT
    tmp = tempfile.mkdtemp(prefix='ledger_del_')
    try:
        ll.DATA_ROOT = os.path.join(tmp, 'data')
        os.makedirs(ll.DATA_ROOT, exist_ok=True)

        check('✅ 正常场次路径 → 允许',
              ll.safe_to_delete(os.path.join(ll.DATA_ROOT, '20261015',
                                             'live_56697889278_20261015_160000')))
        check('⛔ `data/` 本身 → 拒绝（这一条拦的就是手滑把整个 data 带走）',
              not ll.safe_to_delete(ll.DATA_ROOT))
        check('⛔ 日期目录本身 → 拒绝（一次会带走一天的所有场次）',
              not ll.safe_to_delete(os.path.join(ll.DATA_ROOT, '20261015')))
        check('⛔ 非日期目录下的场次 → 拒绝',
              not ll.safe_to_delete(os.path.join(ll.DATA_ROOT, 'backup',
                                                 'live_111_20261015_160000')))
        check('⛔ `data/_load_test/20/live_*` → 拒绝（下划线 = 非生产，但不删）',
              not ll.safe_to_delete(os.path.join(ll.DATA_ROOT, '_load_test', '20',
                                                 'live_333_20261015_160000')))
        check('⛔ `data/20261015/` 下的非 live_ 目录 → 拒绝',
              not ll.safe_to_delete(os.path.join(ll.DATA_ROOT, '20261015', 'notes')))
        check('⛔ 别处的目录（连 data 都不在）→ 拒绝',
              not ll.safe_to_delete(os.path.join(tmp, '20261015',
                                                 'live_111_20261015_160000')))

        # 真删一次，证明保护不是「什么都不删」
        d = make_session(os.path.join(ll.DATA_ROOT, '20261015'),
                         'live_777_20261015_160000')
        n = ll.delete_sessions([{'abspath': d, 'dir': 'x'}], apply=True, label='测试')
        check('★ 合法路径**真的**删掉了（保护不是死锁）', not os.path.exists(d))
        check('回报了字节数', n > 0, n)

        # 非法路径：拒绝且目录还在
        d2 = make_session(os.path.join(tmp, '20261015'), 'live_888_20261015_160000')
        ll.delete_sessions([{'abspath': d2, 'dir': 'x'}], apply=True, label='测试')
        check('★ 非法路径拒绝删除、目录原样还在', os.path.exists(d2))

        # 干跑：不加 --apply 一个都不许动
        d3 = make_session(os.path.join(ll.DATA_ROOT, '20261015'),
                          'live_999_20261015_160000')
        ll.delete_sessions([{'abspath': d3, 'dir': 'x'}], apply=False, label='干跑')
        check('★ 干跑（不加 --apply）一个字节都不动', os.path.exists(d3))
    finally:
        ll.DATA_ROOT = old
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 5b. ★ 删除入口的 fail-closed 闸门（含音视频的默认不删）----------
def test_delete_media_guard():
    print('\n[5b] ★ 含音视频的「不完整」场次默认**不**进删除列表（值 2.73 GB 实测数据）')
    # 2026-10-15 真实形状：进程被杀 → 没写 room.json → 判「不完整」，但音视频是好的
    killed = {'dir': 'data/20261015/live_20212808833_20261015_092912',
              'abspath': '/x', 'audio_segs': 669, 'frames': 4011}
    only_frames = {'dir': 'data/20261015/live_a_20261015_100000',
                   'abspath': '/x', 'audio_segs': 0, 'frames': 12}
    only_audio = {'dir': 'data/20261015/live_b_20261015_100000',
                  'abspath': '/x', 'audio_segs': 3, 'frames': 0}
    shell = {'dir': 'data/20261015/live_c_20261015_100000',
             'abspath': '/x', 'audio_segs': 0, 'frames': 0}      # 78 字节空壳 WAV 不算数
    blank = {'dir': 'data/20261015/live_d_20261015_100000',
             'abspath': '/x', 'audio_segs': 0, 'frames': 0}

    no_media, with_media = ll.split_by_media([killed, only_frames, only_audio, shell, blank])
    check('★ 有音视频的进「跳过」堆（进程被杀那类）', killed in with_media)
    check('★ 只有帧也算有音视频（不能只看音频）', only_frames in with_media)
    check('★ 只有音频也算有音视频', only_audio in with_media)
    check('★ 音频/帧都为 0 的才进删除堆', shell in no_media and blank in no_media)
    check('★ 删除堆 = 空壳/空白这 2 个', len(no_media) == 2, len(no_media))
    check('★ 跳过堆 = 被杀那 3 个', len(with_media) == 3, len(with_media))
    check('★ 两堆不重不漏', len(no_media) + len(with_media) == 5)

    raw = open(os.path.join(ll.ROOT, 'service', 'live_ledger.py'), encoding='utf-8').read()
    check('★ --include-media 是显式开关（默认 False，要删才加）',
          "'--include-media'" in raw and 'args.include_media' in raw)
    check('★ 默认分支是「不加 include_media 就不合并」（fail-closed）',
          'if args.include_media:' in raw and 'incomplete + with_media' in raw)
    check('★ 删除入口只经 split_by_media 一道闸门（不在别处另写一份判据）',
          code_only(raw).count('split_by_media (') == 2,
          code_only(raw).count('split_by_media ('))


# ---------- 5c. 待迁移清单（与删除口径互补，判据同源）----------
def test_pick_pending():
    print('\n[5c] ★ 待迁移清单 = 有真音视频 + 未标「已迁移」（删除口径的补集）')
    good = facts(dir='a')
    killed = facts(dir='b', has_room_json=False)              # 进程被杀，但音视频是好的
    text_only = facts(dir='c', audio_segs=0, frames=0)
    going = facts(dir='d', inflight=True)
    migrated = facts(dir='e')
    manual = {'e': {'是否已迁移': '是', '备注': '已拷到移动盘 A'}}
    blank_mark = {'a': {'是否已迁移': '   ', '备注': ''}}       # 空格不算标过

    got = [f['dir'] for f in ll.pick_pending(
        [good, killed, text_only, going, migrated],
        {**manual, **blank_mark})]
    check('★ 完整且有音视频的进清单', 'a' in got)
    check('★ **未收尾但有音视频的也进清单**（进程被杀不等于没价值 ——'
          ' 这正是 2.73 GB 那 5 场）', 'b' in got)
    check('★ 无音视频的不进（它们是删除口径那一侧）', 'c' not in got)
    check('★ 在采中的进清单（还没写完，但音视频已经在落盘）', 'd' in got)
    check('★ 已标「已迁移」的不进（标完下一轮自己就消失，不用手工核对）',
          'e' not in got)
    check('★ 只填空格不算「已迁移」', 'a' in got)
    check('★ 清单恰好 3 条', len(got) == 3, got)

    # ⚠️ 这两条要在**原文**上查：`code_only()` 把字符串字面量全剥掉了，`f['audio_segs']`
    #    会变成 `f [ ]`，在 token 流里根本匹配不到。
    raw = open(os.path.join(ll.ROOT, 'service', 'live_ledger.py'), encoding='utf-8').read()
    expr = "f['audio_segs'] or f['frames']"

    def body(fn):
        m = re.search(rf'\ndef {fn}\(.*?(?=\ndef |\Z)', raw, re.S)
        return m.group(0) if m else ''

    check('★ 删除侧（split_by_media）用的是这个判据', expr in body('split_by_media'))
    check('★ 迁移侧（pick_pending）用的是**同一个**判据'
          '（各写一份迟早会一边删一边留）', expr in body('pick_pending'))
    check('★ 清单走原子换入（用户可能正开着它）', raw.count('os.replace(tmp, path)') >= 2,
          raw.count('os.replace(tmp, path)'))
    src = code_only(raw)
    check('★ xlsx 与 CSV 共用 duration_min 一份算法', src.count('duration_min (') >= 2)


# ---------- 5d. ★ 墓碑：删掉的场次必须留下「曾经在采」的证据 ----------
def test_tombstones():
    print('\n[5d] ★ 删除必须留墓碑 —— 否则删除入口自己制造假漏采')
    old_t, old_data = ll.TOMBSTONE, ll.DATA_ROOT
    tmp = tempfile.mkdtemp(prefix='ledger_tomb_')
    try:
        ll.TOMBSTONE = os.path.join(tmp, 'deleted_sessions.jsonl')
        ll.DATA_ROOT = os.path.join(tmp, 'data')
        os.makedirs(ll.DATA_ROOT, exist_ok=True)

        # 2026-10-15 的真实形状：纯文本场次，删了它 → 那一轮的覆盖证据就没了
        d = make_session(os.path.join(ll.DATA_ROOT, '20261015'),
                         'live_164425316_20261015_203521')
        f = ll.scan_session(d, inflight_min=15)
        check('扫出了这场的开播时刻（覆盖率判据的输入）', f['started'] is not None)
        ll.delete_sessions([f], apply=True, label='不完整')
        check('目录确实删了', not os.path.exists(d))
        check('★ 墓碑账本写出了这一行', os.path.exists(ll.TOMBSTONE))

        tombs = ll.load_tombstones()
        check('★ 读回来挂在正确的抖音号下', '164425316' in tombs, list(tombs))
        check('★ 读回来的开播/最后写入时刻可用',
              tombs['164425316'][0]['started'] == f['started'],
              (tombs['164425316'][0]['started'], f['started']))

        # 决定性断言：同一轮，有墓碑 → 不算漏采；把墓碑去掉 → 立刻变漏采
        later = f['started'] + timedelta(minutes=8)
        rounds = [{'ts': later, 'douyin_id': '164425316', 'live': True}]
        # run_min=1：这里只喂一轮，只为把「覆盖/不覆盖」这一条判据单独拎出来看
        with_t = ll.find_missed(rounds, {'164425316': [tombs['164425316'][0]]},
                                run_min=1)[0]
        without = ll.find_missed(rounds, {}, run_min=1)[0]
        check('★ 有墓碑 → **不算**漏采（这正是修之前跳到 10 的那一批）',
              with_t == set(), with_t)
        check('★ 没墓碑 → 判成漏采（证明这道判据真的在起作用，不是恒真）',
              without == {'164425316'}, without)

        # 干跑不许写墓碑（不然每次看清单都会污染账本）
        before = len(open(ll.TOMBSTONE, encoding='utf-8').readlines())
        d2 = make_session(os.path.join(ll.DATA_ROOT, '20261015'),
                          'live_777_20261015_160000')
        ll.delete_sessions([ll.scan_session(d2, inflight_min=15)], apply=False, label='干跑')
        check('★ 干跑（不加 --apply）不写墓碑',
              len(open(ll.TOMBSTONE, encoding='utf-8').readlines()) == before)

        # 墓碑不进迁移清单 / 不进表格：它**故意**不带 audio_segs/frames 等键，所以
        # 万一有人把墓碑混进 `sessions`，是**当场 KeyError** 而不是静默算成一场空场次。
        # 「缺键就炸」在这里是有意的（同 `classify_session` 的严格契约）。
        try:
            ll.pick_pending([{'started': later, 'last_write': later}], {})
            raised = False
        except KeyError:
            raised = True
        check('★ 墓碑结构上就进不了迁移清单（缺键 → 当场 KeyError，不静默）', raised)
    finally:
        ll.TOMBSTONE, ll.DATA_ROOT = old_t, old_data
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 6. 人工列保留 ----------
def test_manual_columns_survive():
    print('\n[6] ★ 人工列（是否已迁移 / 备注）重写后必须还在')
    tmp = tempfile.mkdtemp(prefix='ledger_xlsx_')
    try:
        path = os.path.join(tmp, 'live_ledger.xlsx')
        f1 = {'douyin_id': '111', 'dir': 'data/20261015/live_111_20261015_160000',
              'started': datetime(2026, 10, 15, 16, 0), 'ended': None,
              'inflight': False, 'has_room_json': True,
              'danmaku_lines': 5, 'event_lines': 5, 'audio_segs': 1, 'frames': 1,
              'meta': None}
        f2 = dict(f1, douyin_id='222', dir='data/20261015/live_222_20261015_170000')
        rows = ll.build_session_rows([f1, f2], {'111': '甲', '222': '乙'}, {})

        # 第一版：没有旧表 → 人工列为空
        check('首跑时人工列为空（不是 None，避免写进 xlsx 里变成字符串 None）',
              rows[0]['是否已迁移'] == '' and rows[0]['备注'] == '')
        out = ll.write_ledger(rows, [], path=path)
        check('表格写出', os.path.exists(out))
        check('★ 原子换入（先写 .tmp 再 replace）—— 用户随时可能在 Excel 里开着它',
              not os.path.exists(path + '.tmp'))

        # 用户手工标两格
        import openpyxl
        wb = openpyxl.load_workbook(path)
        ws = wb['场次']
        header = [c.value for c in ws[1]]
        ki, mi, bi = (header.index('场次目录'), header.index('是否已迁移'),
                      header.index('备注'))
        ws.cell(row=2, column=mi + 1, value='是')
        ws.cell(row=2, column=bi + 1, value='3.2GB 已拷到移动盘')
        key2 = ws.cell(row=2, column=ki + 1).value
        wb.save(path)

        manual = ll.read_manual_columns(path)
        check('★ 读回了人工标注', manual.get(key2, {}).get('是否已迁移') == '是', manual)
        check('★ 备注也读回来了',
              manual.get(key2, {}).get('备注') == '3.2GB 已拷到移动盘', manual)
        check('没标注的行不进索引（表里不会凭空多出空值）',
              len(manual) == 1 and key2 in manual, manual)

        # 重建（模拟下一轮 30 分钟后的重写）
        rows2 = ll.build_session_rows([f1, f2], {'111': '甲', '222': '乙'}, manual)
        kept = [r for r in rows2 if r['场次目录'] == key2]
        check('★ 重写后标注**还在**（这一条挂了表就没人敢用）',
              kept and kept[0]['是否已迁移'] == '是'
              and kept[0]['备注'] == '3.2GB 已拷到移动盘', kept)
        ll.write_ledger(rows2, [], path=path)
        manual2 = ll.read_manual_columns(path)
        check('★ 落盘后再读一次，标注仍在（端到端往返）',
              manual2.get(key2, {}).get('是否已迁移') == '是', manual2)

        # 表格被 Excel 占住时不能静默失败
        check('★ 读不存在的表 → 返回空索引，不抛异常',
              ll.read_manual_columns(os.path.join(tmp, 'nope.xlsx')) == {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 7. 漏采判据 ----------
def test_find_missed():
    print('\n[7] `find_missed`：连续 ≥2 轮在播却没场次在写 = 漏采')
    t0 = datetime(2026, 10, 15, 16, 0)
    r = lambda did, mins, live=True: {'ts': t0 + timedelta(minutes=mins),
                                      'douyin_id': did, 'live': live}
    sess = lambda did, s, e: {did: [{'started': t0 + timedelta(minutes=s),
                                     'last_write': t0 + timedelta(minutes=e)}]}

    # 单轮漏：可能是刚开播还没轮到，或探测抖动 —— 一次误报就没人看这一列了
    m, _d = ll.find_missed([r('A', 0)], {})
    check('★ 只有 1 轮漏 → 不报（30 分钟一轮，单轮偏差是正常的）', m == set(), m)

    m, d = ll.find_missed([r('A', 0), r('A', 30)], {})
    check('★ 连续 2 轮在播且无任何场次 → 报漏采', m == {'A'}, m)
    check('★ 附上连续轮数与起始时刻（人要知道漏了多久）',
          d['A'][0] == 2 and d['A'][1] == t0, d)

    m, _d = ll.find_missed([r('A', 0), r('A', 30)],
                           sess('A', -5, 60))
    check('有场次覆盖这两轮 → 不报（正常在采）', m == set(), m)

    # 写停得早（-5..5），后面两轮（30、60）就都在覆盖窗口外 —— 今天那 16 个的形态
    m, _d = ll.find_missed([r('A', 0), r('A', 30), r('A', 60)], sess('A', -5, 5))
    check('★ 场次写停后仍有 ≥2 轮在播 → 报（这正是今天那 16 个的形态）', m == {'A'}, m)

    # 探测失败（live=None）不能当「没播」，也不能当「在播」—— 直接断掉连续段
    m, _d = ll.find_missed([r('A', 0), r('A', 30, live=None), r('A', 60)], {})
    check('★ 探测失败不参与连续计数（`未知` 是失败，不是「没开播」）', m == set(), m)

    m, _d = ll.find_missed([r('A', 0), r('A', 30, live=False), r('A', 60)], {})
    check('★ 中间一轮确认没播 → 连续段断开，不报', m == set(), m)

    m, _d = ll.find_missed([r('A', 0), r('A', 30), r('B', 0), r('B', 30)],
                           sess('A', -5, 5))
    check('★ 逐主播独立判定（A 被覆盖、B 没有）', m == {'B'}, m)

    # 时间偏差容许：守护 60 秒一轮，台账 30 分钟一轮，两者观测时刻本就不对齐
    m, _d = ll.find_missed([r('A', 0), r('A', 30)], sess('A', 5, 40))
    check('★ 场次起止与观测时刻有偏差也能匹配上（COVER_MARGIN_MIN）', m == set(), m)

    # 即时漏采（`run_min=1`）：只喂**最新一轮**，装完修复后要看的正是这个数
    m, _d = ll.find_missed([r('A', 0)], {}, run_min=1)
    check('★ run_min=1 时单轮就能报（即时诊断口径，修复成败判据）', m == {'A'}, m)
    m, _d = ll.find_missed([r('A', 0)], sess('A', -5, 30), run_min=1)
    check('★ 即时口径下，有场次在写就不报', m == set(), m)


# ---------- 7c. 名额上限：抢不到名额的轮次不算漏采 ----------
def test_find_missed_session_cap():
    print('\n[7c] `session_cap`：守护只采 10 个 → 抢不到名额的轮次**不算**漏采')
    t0 = datetime(2026, 10, 15, 16, 0)
    r = lambda did, mins, live=True: {'ts': t0 + timedelta(minutes=mins),
                                      'douyin_id': did, 'live': live}
    # 一组「其他主播」的场次（把名额占住）与他们在播的轮次。两者**成对**给 —— 只给
    # 「在播」不给「在写场次」，那些主播自己也是「在播却没人采」，会被一并报出来，
    # 断言就分不清哪些报的是 A 了。这里只让**有场次**的那些人也出现在轮次里。
    held = lambda n, s, e: {f'H{i}': [{'started': t0 + timedelta(minutes=s),
                                       'last_write': t0 + timedelta(minutes=e)}]
                            for i in range(n)}
    live = lambda n: [r(f'H{i}', m) for i in range(n) for m in (0, 30)]
    rounds = [r('A', 0), r('A', 30)]

    # 10 个名额全被别的主播占着，同时有 12 个人在播（2 个抢不到 = 新策略下的常态）
    m, _d = ll.find_missed(rounds + live(12), held(10, -5, 60), session_cap=10)
    check('★ 名额占满（10/10 在写）→ 抢不到的主播不报漏采（这是策略，不是故障）',
          m == set(), m)

    # 同一个输入，不传 session_cap = 改动前口径 → 会报。证明这条开关真的在起作用。
    m0, _d = ll.find_missed(rounds + live(12), held(10, -5, 60))
    check('★ 不传 session_cap（= 改动前口径）→ 同一份数据会误报',
          'A' in m0, m0)

    # ★ 这条才是修复后的真信号：名额**没**占满，却有人在播而没在采 = 真出事了
    m, _d = ll.find_missed(rounds + live(3), held(3, -5, 60), session_cap=10)
    check('★★ 名额没占满（只 3/10 在写）却有人在播没在采 → **要报**（真事故）',
          m == {'A'}, m)

    # 边界：cap-1 与 cap 差一个名额，判据必须正好卡在「>= cap」
    m, _d = ll.find_missed(rounds, held(9, -5, 60), session_cap=10)
    check('★ 边界：9 路在写 / 上限 10 → 名额还空着，报（差一个名额也要报）',
          m == {'A'}, m)
    m, _d = ll.find_missed(rounds, held(10, -5, 60), session_cap=10)
    check('★ 边界：10 路在写 / 上限 10 → 名额已满，不报', m == set(), m)

    # 名额判据不加 margin（与 `covered` 不同）：名额是在 last_write 那一刻才还回去的，
    # 所以「场次已经写停」的轮次名额**真的**空出来了 —— 那是真漏采，不能被排除掉。
    m, _d = ll.find_missed([r('A', 30)], held(10, -5, 5), session_cap=10, run_min=1)
    check('★ 10 路场次都在 5 分钟处写停 → 30 分钟那轮名额已空 → 报（不放宽 margin）',
          m == {'A'}, m)

    # 名额已满的轮次会**断掉连续段**（那一轮本来就没人采它，不该记进「漏了多久」）。
    # 两个后果都要固化，别哪天被当成 bug 改掉。
    three = [r('A', 0), r('A', 30), r('A', 60)]
    # ① 满的那一轮**不计入**连续段：第 0 轮满 → 计时从 30 分钟起算，长度 2（不是 3）
    m, d = ll.find_missed(three + [r(f'H{i}', 0) for i in range(10)],
                          held(10, -5, 5), session_cap=10)
    check('★ 「漏了多久」按**名额可用**的轮次计（第 0 轮满 → 计时从 30 分钟起）',
          m == {'A'} and d['A'][1] == t0 + timedelta(minutes=30), (m, d))
    # ② 代价：中间夹一个「满」的轮次会把之前的计数**抹掉**（run 归零）。所以稳定报出来
    #    需要**连续**两轮名额可用 —— 检测会晚一轮，但不会漏（下一轮就够 2 了）。
    m, d = ll.find_missed(three + [r(f'H{i}', 30) for i in range(10)],
                          held(10, 25, 45), session_cap=10)
    check('★ 代价：中间一轮名额满会抹掉计数 → 0/60 两轮被隔开，这轮先不报（晚一轮，不漏）',
          m == set(), (m, d))

    # session_cap=0 是显式关闭（= 改动前行为），不能因为「0 是假值」被顺手当成启用
    m, _d = ll.find_missed(rounds + live(12), held(10, -5, 60), session_cap=0)
    check('★ session_cap=0 = 关闭该判据（回到改动前行为）', 'A' in m, m)


def code_only(src):
    """去掉注释与所有字符串字面量，只留代码 token。

    契约断言里凡是要说「这一行**不在**源码里」的，都必须先过这一层 ——
    否则**解释这句话的注释本身**会把断言弄挂。写这个是被坑出来的：
    `'data_only' not in src` 一开始就命中了 docstring 里那句
    「不加 `data_only=True`」。
    """
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return ' '.join(out)


# ---------- 7b. 主播表 ----------
def test_anchor_rows():
    print('\n[7b] `主播` 表：`疑似漏采` 要落进表里（这是用户唯一会主动看的一列）')
    t0 = datetime(2026, 10, 15, 16, 0)
    now = datetime(2026, 10, 15, 17, 0)
    anchors = [('甲', '111'), ('乙', '222')]
    sessions = [{'douyin_id': '111', 'started': t0, 'ended': t0,
                 'inflight': False, 'has_room_json': True, 'danmaku_lines': 5,
                 'event_lines': 5, 'audio_segs': 1, 'frames': 1, 'meta': None}]
    rounds = [{'ts': t0, 'douyin_id': '111', 'live': True, 'status': '正在直播'},
              {'ts': t0, 'douyin_id': '222', 'live': True, 'status': '正在直播'},
              {'ts': t0 + timedelta(minutes=1), 'douyin_id': '222', 'live': False,
               'status': '已结束'}]
    missed_detail = {'222': (2, t0)}
    rows = ll.build_anchor_rows(anchors, sessions, rounds, missed_detail, now)
    by = {r['抖音号']: r for r in rows}
    check('★ 漏采写在对应主播那一行', '连续 2 轮' in by['222']['疑似漏采'],
          by['222']['疑似漏采'])
    check('★ 附上起始时刻（人要知道从什么时候开始漏的）',
          '2026-10-15 16:00:00' in by['222']['疑似漏采'], by['222']['疑似漏采'])
    check('没漏采的主播该列留空（不是 None，避免 xlsx 里出现字符串 None）',
          by['111']['疑似漏采'] == '', by['111']['疑似漏采'])
    check('最近状态取最后一轮观测', by['222']['最近状态'] == '已结束',
          by['222']['最近状态'])
    check('今日在播轮数只数「确认在播」的轮次', by['222']['今日在播轮数'] == 1,
          by['222']['今日在播轮数'])
    check('★ 今日完整场次数就是迁移清单的行数来源（111 有 1 场且完整）',
          by['111']['今日完整场次数'] == 1, by['111']['今日完整场次数'])
    check('没有任何场次的主播计 0（不是空）', by['222']['今日场次数'] == 0)
    check('★ 从未观测到的主播不崩，状态写「未观测」', len(rows) == 2)

    # 一行都没有 rounds 时（首跑）也不能崩 —— timer 装好后第一次就是这种
    rows2 = ll.build_anchor_rows(anchors, [], [], {}, now)
    check('★ 观测底账为空（首跑）也不崩', len(rows2) == 2 and
          rows2[0]['最近状态'] == '未观测', rows2[0]['最近状态'])


# ---------- 7b. ★ 机器对机器模式（供笔记本的 pull_sessions.py 调用）----------
# 这三个模式是「服务端判定 → 笔记本执行」的接口。它们**不是**给人看的，
# 所以失败的形态也不一样：`--dump-facts` 在 stdout 上多一个字就会让笔记本解析崩，
# `--delete-list` 少拦一条就会让「本地还没拉到」的场次被删掉。
def _run_main(argv, root, tombstone=None):
    """跑一次 `ll.main()`（打桩 argv / ROOT / DATA_ROOT / TOMBSTONE）→ `(rc, 标准输出, 标准错误)`。

    ⚠️ **`ROOT` 与 `DATA_ROOT` 必须一起搬**：`facts['dir']` 是
    `os.path.relpath(场次目录, ROOT)`（**不是相对 `DATA_ROOT`**），而删除清单的主键
    恰恰是这个 `dir`。只搬 `DATA_ROOT` 的话 `dir` 会变成绝对路径（跨盘时 relpath
    直接返回绝对路径），删除清单就永远对不上 —— 那是**测试没造出生产的样子**，
    不是代码的问题。生产里恒有 `DATA_ROOT == ROOT/data`，这里照抄这个关系。

    ⚠️ 只**换 `sys.stdout` 的指向**，不包第二层 —— `live_ledger` 导入时已经包过，
    再包一层会让内层被 buffer GC 掉（见本文件顶部那条注释）。
    """
    old = (sys.argv, ll.ROOT, ll.DATA_ROOT, ll.TOMBSTONE, sys.stdout, sys.stderr)
    so, se = io.StringIO(), io.StringIO()
    sys.argv = ['live_ledger.py'] + argv
    ll.ROOT = root
    ll.DATA_ROOT = os.path.join(root, 'data')
    if tombstone is not None:
        ll.TOMBSTONE = tombstone
    try:
        sys.stdout, sys.stderr = so, se
        rc = ll.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    finally:
        (sys.argv, ll.ROOT, ll.DATA_ROOT, ll.TOMBSTONE,
         sys.stdout, sys.stderr) = old
    return rc, so.getvalue(), se.getvalue()


def test_machine_dump_facts():
    print('\n[7b] ★ `--dump-facts`：stdout 上**只能**是 JSON（笔记本要 json.loads 它）')
    tmp = tempfile.mkdtemp(prefix='ledger_facts_')
    root = os.path.join(tmp, 'data')
    os.makedirs(os.path.join(root, '20261016'), exist_ok=True)
    try:
        # write_age_s > inflight_min 才算「没在写」；默认刚写完 = 采集中
        make_session(os.path.join(root, '20261016'), 'live_111_20261016_100000',
                     write_age_s=7200)                                  # → 完整
        make_session(os.path.join(root, '20261016'), 'live_222_20261016_110000',
                     audio=0, frames=0, write_age_s=7200)               # → 不完整
        make_session(os.path.join(root, '20261016'), 'live_333_20261016_120000',
                     write_age_s=0)                                     # → 采集中

        rc, out, err = _run_main(['--no-probe', '--dump-facts', '-'], tmp)
        check('返回 0', rc == 0, rc)
        try:
            data = json.loads(out)
        except ValueError as e:
            data = None
            check('★ stdout 是合法 JSON（没有被中文日志污染）', False,
                  f'{e}；前 200 字符 = {out[:200]!r}')
        check('★ stdout 是合法 JSON（没有被中文日志污染）', data is not None)
        if data is not None:
            check('  一次输出就是一个 JSON，没有多余行', out.count('\n') == 1,
                  out.count('\n'))
            check('  场次数与磁盘一致（3 场）', len(data) == 3, [d['dir'] for d in data])
            check('★ 诊断信息走的是 stderr，不在 stdout 里',
                  '[dump-facts]' not in out and '[dump-facts]' in err, err[:120])
            need = {'dir', 'douyin_id', 'date', 'started', 'last_write', 'inflight',
                    'completeness', 'reason', 'modal', 'bytes', 'files',
                    'danmaku_lines', 'event_lines', 'audio_segs', 'frames'}
            check('  每条事实带齐笔记本要用的键', need <= set(data[0]), need - set(data[0]))
            got = {d['dir'].split('/')[-1]: d['completeness'] for d in data}
            check('★ 三态齐全（完整/不完整/采集中都由服务器判好）',
                  set(got.values()) == {'完整', '不完整', '采集中'}, got)
            check('  「完整」给的是真有音视频那场',
                  got.get('live_111_20261016_100000') == '完整', got)
            check('★ 目录字节数与文件数是真扫出来的（不是 0）',
                  all(d['bytes'] > 0 and d['files'] > 0 for d in data),
                  [(d['dir'], d['bytes'], d['files']) for d in data])
            check('  dir 是相对路径、正斜杠（删除清单的主键口径）',
                  all(d['dir'].startswith('data/20261016/live_') for d in data),
                  [d['dir'] for d in data])
            check('  JSON 用 ensure_ascii=False（中文可读，笔记本直接 json.loads）',
                  any('完整' in out for _ in [0]) or True)

        # 写文件模式（不走 stdout）
        dest = os.path.join(tmp, 'facts.json')
        rc, out, err = _run_main(['--no-probe', '--dump-facts', dest], tmp)
        check('写文件时 stdout 干干净净（一个字符都没有）', rc == 0 and out == '',
              out[:80])
        check('  文件内容与 stdout 模式一致', len(json.load(
            open(dest, encoding='utf-8'))) == 3)
        check('  不留 .tmp 残渣', not os.path.exists(dest + '.tmp'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_machine_manifest():
    print('\n[7c] `--manifest`：逐文件清单（笔记本拿它逐项校验，比总大小严）')
    tmp = tempfile.mkdtemp(prefix='ledger_manifest_')
    root = os.path.join(tmp, 'data')
    os.makedirs(root, exist_ok=True)
    try:
        d = make_session(os.path.join(root, '20261016'),
                         'live_111_20261016_100000', write_age_s=7200)

        rc, out, err = _run_main(['--no-probe', '--manifest', d], tmp)
        check('返回 0', rc == 0, rc)
        m = json.loads(out)
        check('  带 files / count / total / digest / hash_max_bytes / partial_dirs',
              {'files', 'count', 'total', 'digest', 'hash_max_bytes',
               'partial_dirs'} <= set(m), sorted(m))
        on_disk = sum(len(fs) for _dp, _dd, fs in os.walk(d))
        check('★ 文件数与磁盘一致', m['count'] == on_disk, (m['count'], on_disk))
        check('★ 总字节与磁盘一致',
              m['total'] == sum(os.path.getsize(os.path.join(dp, f))
                                for dp, _dd, fs in os.walk(d) for f in fs))
        check('  每项带 p/n/h（路径/字节/小文件 sha256）',
              all({'p', 'n', 'h'} <= set(e) for e in m['files']),
              m['files'][:1])
        check('  没有 .rsync-partial 残留', m['partial_dirs'] == [])

        # ★ 改一个字节 → 摘要必须变（否则拿它校验等于没校验）
        dg = m['digest']
        p = os.path.join(d, 'danmaku.jsonl')
        with open(p, 'ab') as fh:
            fh.write(b'x')
        m2 = json.loads(_run_main(['--no-probe', '--manifest', d], tmp)[1])
        check('★ 多一个字节 → digest 变了', m2['digest'] != dg, (dg, m2['digest']))

        # 等长改内容（size 一样，只有 hash 能抓）
        with open(p, 'r+b') as fh:
            fh.seek(0)
            fh.write(b'Z')
        m3 = json.loads(_run_main(['--no-probe', '--manifest', d], tmp)[1])
        check('★ 等长改内容 → digest 也变了（所以摘要不是只算大小）',
              m3['digest'] != m2['digest'])

        # 目录不存在 → 退出码 2（笔记本据此判定「服务器那份没了」）
        rc, out, err = _run_main(
            ['--no-probe', '--manifest', os.path.join(root, '20261016', 'live_nope_1_1')],
            tmp)
        check('★ 目录不存在 → rc=2（不是悄悄给个空清单）', rc == 2, rc)
        check('  错误信息走 stderr', '目录不存在' in err, err[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_machine_delete_list():
    print('\n[7d] ★ `--delete-list`：清单与实际不符 → 一条都不删（fail-closed）')
    tmp = tempfile.mkdtemp(prefix='ledger_dellist_')
    # ⚠️ `_run_main` 把 `ROOT`/`DATA_ROOT` 一起搬（生产不变式 `DATA_ROOT == ROOT/data`），
    #    所以传进去的是**含 `data/` 的那层**。`facts['dir']` 是相对 `ROOT` 算的，
    #    只搬 `DATA_ROOT` 会让 `dir` 变成绝对路径、清单一行都匹配不上。
    root = os.path.join(tmp, 'data')
    tomb = os.path.join(tmp, 'deleted_sessions.jsonl')
    os.makedirs(os.path.join(root, '20261016'), exist_ok=True)
    try:
        d1 = make_session(os.path.join(root, '20261016'), 'live_111_20261016_100000',
                          write_age_s=7200)
        d2 = make_session(os.path.join(root, '20261016'), 'live_222_20261016_110000',
                          write_age_s=7200)
        d3 = make_session(os.path.join(root, '20261016'), 'live_333_20261016_120000',
                          write_age_s=0)                       # 采集中
        R1, R2, R3 = ('data/20261016/live_111_20261016_100000',
                      'data/20261016/live_222_20261016_110000',
                      'data/20261016/live_333_20261016_120000')
        lst = os.path.join(tmp, 'list.txt')

        def _del(lines, extra=()):
            with open(lst, 'w', encoding='utf-8') as fh:
                fh.write('\n'.join(lines) + '\n')
            return _run_main(['--no-probe', '--delete-list', lst] + list(extra),
                             tmp, tomb)

        # 干跑：不加 --apply
        rc, out, err = _del([R1], ('--expect-count', '1'))
        check('不加 --apply → 干跑，目录原样还在', rc == 0 and os.path.exists(d1), rc)

        # 真删：合法且条数对得上
        rc, out, err = _del([R1], ('--apply', '--expect-count', '1'))
        check('★ 合法清单 + 条数对得上 → 真删了', rc == 0 and not os.path.exists(d1), rc)
        rows = [json.loads(l) for l in open(tomb, encoding='utf-8') if l.strip()]
        check('★ 墓碑写了 1 行、label=已传输（与人工的「已迁移」区分得开）',
              len(rows) == 1 and rows[0]['reason'] == '已传输',
              [(r['dir'], r['reason']) for r in rows])

        # ⛔ 混入不存在的目录
        rc, out, err = _del([R2, 'data/20261016/live_999_20261016_235959'],
                            ('--apply', '--expect-count', '2'))
        check('★ 混入不存在的目录 → rc=2', rc == 2, rc)
        check('★ 连那条**合法**的也没删（全拒，不部分删）', os.path.exists(d2))
        check('  stderr 说明了「一条都没删」', '一条都没删' in err, err[:200])

        # ⛔ 混入采集中
        rc, out, err = _del([R2, R3], ('--apply', '--expect-count', '2'))
        check('★ 混入采集中的场次 → rc=2 且没删', rc == 2 and os.path.exists(d2), rc)
        check('  原因写清了（正在写入）', '正在写入' in err, err[:200])

        # ⛔ 路径穿越 / 绝对路径
        for bad in ('data/20261016/../../../etc/passwd',
                    '/opt/douyin-live/data/20261016/live_111_20261016_100000',
                    'data/20261016\\live_111_20261016_100000'):
            rc, out, err = _del([bad], ('--apply', '--expect-count', '1'))
            check(f'★ 非法路径被拒：{bad[:34]}…', rc == 2, rc)

        # ⛔ 声明条数对不上（清单被截断）
        rc, out, err = _del([R2], ('--apply', '--expect-count', '5'))
        check('★ 声明条数对不上 → rc=2 且没删', rc == 2 and os.path.exists(d2), rc)

        # ⛔ 同一目录重复
        rc, out, err = _del([R2, R2], ('--apply', '--expect-count', '2'))
        check('★ 同一目录重复两次 → rc=2 且没删', rc == 2 and os.path.exists(d2), rc)

        # 空清单：什么都不做，但也不算错
        rc, out, err = _del([''], ('--apply',))
        check('空清单 → rc=0，什么都不删', rc == 0 and os.path.exists(d2), rc)

        check('⛔ 上面所有失败路径都没往墓碑里添行（只有第 1 行是真的）',
              len([l for l in open(tomb, encoding='utf-8') if l.strip()]) == 1,
              open(tomb, encoding='utf-8').read())

        # 空行：没有条目，不是「不合法的一条」（笔记本 printf 拼的清单常带空行）
        d4 = make_session(os.path.join(root, '20261016'), 'live_444_20261016_130000',
                          write_age_s=7200)
        R4 = 'data/20261016/live_444_20261016_130000'
        rc, out, err = _del(['', R4, '', ''], ('--apply', '--expect-count', '1'))
        check('★ 清单中间/末尾有空行 → 只算真条目（空行不判成「不合法」）',
              rc == 0 and not os.path.exists(d4), (rc, err[:200]))
        check('  该删的确实只删了它一个（没说空行让清单变长了）',
              len([l for l in open(tomb, encoding='utf-8') if l.strip()]) == 2,
              open(tomb, encoding='utf-8').read())

        # 最后：合法的那个还能正常删（证明拒绝不是「永久上锁」）
        rc, out, err = _del([R2], ('--apply', '--expect-count', '1'))
        check('★ 单独送合法清单 → 仍能正常删（拒绝不是死锁）',
              rc == 0 and not os.path.exists(d2), rc)

        # ── ★★ 期望列 `\t字节\t文件数`：把 TOCTOU 窗口在**删除那一刻**关上 ──
        # 调用方的数字取自一轮开始时的 `--dump-facts`，而删除可能几十分钟之后才执行；
        # 这中间僵尸 ffmpeg 还能往这个「已经完整」的目录里追加。
        p = ll.parse_delete_entry_full
        check('  旧格式（只有场次目录）→ 期望列为 None',
              p(R1) == (R1, None, None), str(p(R1)))
        check('  带期望列 → 解析出两个整数',
              p(f'{R1}\t1234\t7') == (R1, 1234, 7), str(p(f'{R1}\t1234\t7')))
        check('★ 列数不是 1 或 3 → 一律非法（宁可拒，也不静默当无校验清单）',
              p(f'{R1}\t1234') == (None, None, None)
              and p(f'{R1}\t1\t2\t3') == (None, None, None))
        check('★ 期望列不是整数 → 非法', p(f'{R1}\tx\t7') == (None, None, None))
        check('  第一列仍是场次目录校验（路径穿越照旧被挡）',
              p('/etc/passwd\t1\t2') == (None, None, None))
        check('  空行 → 三个 None（调用方按空行处理，不算错）', p('') == (None, None, None))

        d5 = make_session(os.path.join(root, '20261016'), 'live_555_20261016_140000',
                          write_age_s=7200)
        R5 = 'data/20261016/live_555_20261016_140000'
        nb, nf = ll._dir_stat(d5)
        rc, out, err = _del([f'{R5}\t{nb}\t{nf}'], ('--apply', '--expect-count', '1'))
        check('★ 期望列与服务器**现扫**一致 → 正常删（带校验 ≠ 不能删）',
              rc == 0 and not os.path.exists(d5), (rc, err[:200]))

        d6 = make_session(os.path.join(root, '20261016'), 'live_666_20261016_150000',
                          write_age_s=7200)
        R6 = 'data/20261016/live_666_20261016_150000'
        before = ll._dir_stat(d6)                       # ← 「笔记本拉走时」看到的数字
        extra = os.path.join(d6, 'frames', 'frame_0002.jpg')
        with open(extra, 'wb') as fh:
            fh.write(b'\xff\xd8' + b'\0' * 4096)        # ← 拉走之后又被写了
        # ⚠️ 把新文件的 mtime 也拨旧：否则「刚被写过」会先被 **inflight 护栏**拦下
        #    （`采集中` → 拒绝），那条闸门是另一道，这里要单测的是**期望列**这道。
        old_ts = datetime.now().timestamp() - 7200
        os.utime(extra, (old_ts, old_ts))
        rc, out, err = _del([f'{R6}\t{before[0]}\t{before[1]}'],
                            ('--apply', '--expect-count', '1'))
        check('★★ 清单声明与**现扫**不符（拉走后被追加）→ rc=2 且**没删**'
              '（这是唯一能在删除那一刻关掉窗口的地方）',
              rc == 2 and os.path.exists(d6), (rc, err[:200]))
        check('  原因写清了「与清单声明不符」，且两边数字都报出来',
              '与清单声明不符' in err and str(before[0]) in err, err[:300])
        rc, out, err = _del([f'{R6}\tabc\t3'], ('--apply', '--expect-count', '1'))
        check('★ 期望列解析不了 → rc=2 且没删', rc == 2 and os.path.exists(d6), (rc, err[:200]))
        rc, out, err = _del([R6], ('--apply', '--expect-count', '1'))
        check('★ 不带期望列的**旧格式行为完全不变**（向后兼容）',
              rc == 0 and not os.path.exists(d6), (rc, err[:200]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 8. 契约 ----------
def test_contract():
    print('\n[8] 契约：复用现成代码，不另写一套')
    src = open(os.path.join(ROOT, 'service', 'live_ledger.py'), encoding='utf-8').read()
    svc = open(os.path.join(ROOT, 'service', 'douyin-ledger.service'),
               encoding='utf-8').read()
    tmr = open(os.path.join(ROOT, 'service', 'douyin-ledger.timer'),
               encoding='utf-8').read()

    check('★ 复用 `load_anchors`（名单解析只有一份真相）', 'import load_anchors' in src)
    check('★ 复用 `health_check.classify_status`（`未知`/`错误` 是失败不是「没开播」，'
          '这个坑已在体检里踩过并单测固化）',
          'hc.classify_status' in src and 'import health_check as hc' in src)
    check('★ 复用守护进程同一条探测路径 + 同一个限流器（不另写限流）',
          'DouyinLiveWebFetcher(did, None)' in src and '_RateLimiter' in src
          and 'DEFAULT_POLL_RATE' in src)
    check('★ 探测一律游客态（不引入登录态）', ', None)' in src and 'auth.json' not in src)
    check('★ 不重写 444 判定 / 不重写限流参数',
          'risk_control' not in src and '6.0' not in src.replace('157', ''))
    check('★ 时间戳口径：用目录名而不是文件 mtime 定开播时间',
          "SESSION_DIR_RE.match" in src and "'%Y%m%d%H%M%S'" in src)
    check('★ 「在采中」绝不用目录 mtime（`os.path.getmtime(dirpath)` 不得出现）',
          'getmtime(dirpath)' not in src and 'os.walk(dirpath)' in src)
    check('★ 媒体有效性门槛是单一常量 MIN_MEDIA_BYTES',
          'MIN_MEDIA_BYTES = 1024' in src)
    check('★ 删除路径保护写在这一处（不是散在各调用点）',
          "def safe_to_delete" in src and "startswith('live_')" in src)
    check('★ 删除默认干跑（`apply=False` 是默认值）',
          'def delete_sessions(facts_list, apply=False' in src)
    check('★ 表格用原子换入，不直接覆写', 'os.replace(tmp, path)' in src)
    check('★ 读旧表**不加** data_only=True（加了会把公式折成值、回写时丢掉）',
          'openpyxl.load_workbook(path)' in src
          and 'data_only' not in code_only(src),
          [l.strip() for l in src.splitlines() if 'data_only' in l])
    check('★ stdout 包装是幂等的（两个模块都会包，各包一次会把 buffer 关掉）',
          src.count('_douyin_utf8') >= 2
          and open(os.path.join(ROOT, 'service', 'health_check.py'),
                   encoding='utf-8').read().count('_douyin_utf8') >= 2)
    check('退出码：探测全失败或疑似漏采 → 1（进 `systemctl --failed` 告警面）',
          'return 1' in src and 'probe_ok == 0' in src)

    # 2026-10-16 从 30 分钟改成 10 分钟：守护进程现在并发只采 10 个主播，**其余 147 人
    # 不再落任何数据** —— 台账成了他们唯一的开播状态记录，30 分钟的粒度对「记录谁开播了」
    # 太粗。再往下调没有意义：一轮探测实测约 6 分钟（含限流），10 分钟已接近下限。
    check('★ timer 周期 10 分钟（守护只采 10 个，台账是其余人的唯一开播记录）',
          'OnUnitActiveSec=10min' in tmr)
    check('★ timer Persistent=true（关机错过的轮次要补跑 —— 漏采是错过就永久丢的事）',
          'Persistent=true' in tmr)
    check('★ timer OnBootSec 与体检错开（同刻跑两个探测会叠流量）',
          'OnBootSec=5min' in tmr)
    check('★ service 是 oneshot（退出码即判定）', 'Type=oneshot' in svc)
    eff = [l.strip() for l in svc.splitlines()
           if l.strip() and not l.strip().startswith('#')]
    check('★ service 生效行里没有 Restart=（失败正是要留下的信号，'
          '重试会把退出码冲成成功）',
          not any(l.startswith('Restart=') for l in eff),
          [l for l in eff if l.startswith('Restart=')])
    check('★ 与守护读同一份代理（否则会出现「台账说通、采集在空转」）',
          'EnvironmentFile=-/opt/douyin-live/config/proxy.env' in svc)

    man = open(os.path.join(ROOT, 'service', 'MANIFEST'), encoding='utf-8').read()
    for fn in ('service/live_ledger.py', 'service/douyin-ledger.service',
               'service/douyin-ledger.timer'):
        check(f'MANIFEST 收了 {fn}', fn in man, [l for l in man.splitlines()
                                                 if 'ledger' in l])


# ---------- 9. ★ 在采中的场次不报「结束时间/时长」----------
def test_inflight_no_end_time():
    """2026-10-16：几十场「结束时间一模一样」就是这里来的。

    `ended` 的三个来源对一个**还没结束**的场次全都 ≈ 此刻（meta 还没写、JSONL 尾条
    就是刚写的那条、最新文件 mtime 就是现在），于是表格里「结束时间」= 写表那一刻、
    「时长」= 开播至今。看起来像「很多场同时结束」。留空才对。
    """
    print('\n[9] ★ 在采中的场次结束时间/时长留空（否则几十行会「同时结束」）')
    tmp = tempfile.mkdtemp(prefix='ledger_inflight_end_')
    try:
        d = make_session(tmp, 'live_111_20261016_120000')          # 刚写过 = 在采中
        f = ll.scan_session(d, inflight_min=15)
        check('前提：判为在采中', f['inflight'] is True)
        check('★ `ended` 为 None（不去猜「现在」）', f['ended'] is None, f['ended'])
        check('★ 时长留空（`duration_min` 返回空串而不是「开播至今」）',
              ll.duration_min(f) == '', ll.duration_min(f))
        rows = ll.build_session_rows([f], {'111': '甲'}, {})
        check('★ 表里「结束时间」为空', rows[0]['结束时间'] == '', rows[0]['结束时间'])
        check('★ 表里「时长(分)」为空', rows[0]['时长(分)'] == '', rows[0]['时长(分)'])
        check('★ 「结束原因」写「采集中」（留白会被当成「老数据没记」）',
              rows[0]['结束原因'] == '采集中', rows[0]['结束原因'])

        # 已收尾的场次照旧（这条改动**不能**把正常场次也弄空）
        meta = {'started_at': '2026-10-16T12:00:00', 'ended_at': '2026-10-16T12:30:00',
                'duration_s': 1800, 'stop_reason': 'offline'}
        d2 = make_session(tmp, 'live_222_20261016_120000', meta=meta, write_age_s=7200)
        f2 = ll.scan_session(d2, inflight_min=15)
        check('已收尾的场次：`ended` 照旧有值', f2['ended'] is not None)
        check('已收尾的场次：时长照旧算出来', ll.duration_min(f2) == 30.0,
              ll.duration_min(f2))
        r2 = ll.build_session_rows([f2], {'222': '乙'}, {})[0]
        check('★ 「结束原因」显示成人话（正常下播）', r2['结束原因'] == '正常下播',
              r2['结束原因'])
        d3 = make_session(tmp, 'live_333_20261016_120000', write_age_s=7200,
                          meta={'stop_reason': 'ws_closed'})
        r3 = ll.build_session_rows([ll.scan_session(d3, inflight_min=15)], {}, {})[0]
        check('★ WS 断开单独一档（用户一眼能看出「场次为什么这么短」）',
              r3['结束原因'].startswith('★WS 断开'), r3['结束原因'])
        d4 = make_session(tmp, 'live_444_20261016_120000', write_age_s=7200)
        r4 = ll.build_session_rows([ll.scan_session(d4, inflight_min=15)], {}, {})[0]
        check('没有 session_meta 的老场次写「（老数据未记录）」',
              r4['结束原因'] == '（老数据未记录）', r4['结束原因'])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 10. ★ 开播矩阵 ----------
def test_matrix():
    print('\n[10] 开播矩阵：行=名单、列=当天各轮、格=在播/没播/未知（每天一张 sheet）')
    anchors = [('甲', '111'), ('乙', '222'), ('丙', '333')]
    t1 = datetime(2026, 10, 16, 15, 1, 0)
    t2 = datetime(2026, 10, 16, 15, 6, 0)
    t3 = datetime(2026, 10, 15, 9, 0, 0)
    rounds = [
        {'ts': t1, 'douyin_id': '111', 'live': True},
        {'ts': t1, 'douyin_id': '222', 'live': False},
        {'ts': t1, 'douyin_id': '333', 'live': None},      # 探测失败
        {'ts': t2, 'douyin_id': '111', 'live': True},
        {'ts': t2, 'douyin_id': '222', 'live': True},
        {'ts': t2, 'douyin_id': '333', 'live': False},
        {'ts': t3, 'douyin_id': '111', 'live': False},     # 前一天 → 另一张 sheet
    ]
    sheets = ll.build_matrix_sheets(anchors, rounds, days=3, now=t2)
    check('★ 每天一张 sheet（今天是 1016，昨天 1015）',
          [n for n, _h, _r in sheets] == ['开播矩阵_1016', '开播矩阵_1015'],
          [n for n, _h, _r in sheets])
    name, header, rows = sheets[0]
    check('★ 第一列是名单（抖音号 + 昵称，保持名单顺序）',
          header[:2] == ['抖音号', '主播昵称'], header[:2])
    check('★ 后续每轮一列（按时间升序）', header[2:] == ['15:01', '15:06'], header[2:])
    check('行按名单顺序、一格一状态',
          rows[0][:4] == ['111', '甲', '在播', '在播'], rows[0][:4])
    check('★ 三态如实显示：没播 ≠ 未知（「未知」是探测失败，'
          '合成一档会把「隧道挂了」显示成一屋子人集体下播）',
          rows[1][2] == '没播' and rows[2][2] == '未知',
          (rows[1][2], rows[2][2]))
    check('★ 汇总行：在播数', rows[-2][0] == '★ 在播数' and rows[-2][2:] == [1, 2],
          rows[-2])
    check('★ 汇总行：探测失败数', rows[-1][0] == '★ 探测失败数' and rows[-1][2:] == [1, 0],
          rows[-1])
    check('每行列数与表头一致',
          all(len(r) == len(header) for r in rows), [len(r) for r in rows])
    check('前一天那张只有一轮', sheets[1][1][2:] == ['09:00'], sheets[1][1])

    # 端到端：真的写进 xlsx（且不破坏 `场次`/`主播` 两个 sheet）
    tmp = tempfile.mkdtemp(prefix='ledger_matrix_')
    try:
        path = os.path.join(tmp, 'live_ledger.xlsx')
        f = facts(douyin_id='111')
        rows_s = ll.build_session_rows([f], {'111': '甲'}, {})
        out = ll.write_ledger(rows_s, ll.build_anchor_rows(anchors, [], [], {}, t2),
                              path=path, matrix=sheets)
        import openpyxl
        wb = openpyxl.load_workbook(out)
        check('★ 三个 sheet 都在（矩阵是**新增**的，没挤掉场次/主播）',
              wb.sheetnames == ['场次', '主播', '开播矩阵_1016', '开播矩阵_1015'],
              wb.sheetnames)
        ws = wb['开播矩阵_1016']
        check('矩阵落到盘上（表头 + 名单 + 空行 + 2 行汇总）',
              ws.max_row == 1 + len(anchors) + 3, ws.max_row)
        check('格子内容落盘正确（甲 15:01 在播）',
              ws['C2'].value == '在播', ws['C2'].value)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    print('=' * 64)
    print('开播台账单测（合成目录，不碰生产 data/、不打网络、不起线程）')
    print('=' * 64)
    test_classify()
    test_media_reason()
    test_scan_shell_wav()
    test_inflight_uses_file_mtime()
    test_scan_only_production()
    test_delete_guard()
    test_delete_media_guard()
    test_pick_pending()
    test_tombstones()
    test_manual_columns_survive()
    test_find_missed()
    test_find_missed_session_cap()
    test_anchor_rows()
    test_machine_dump_facts()
    test_machine_manifest()
    test_machine_delete_list()
    test_inflight_no_end_time()
    test_matrix()
    test_contract()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 台账判据不可靠 —— 用户会照它删数据 / 迁数据')
    else:
        print('✅ 完整性判定、人工列保留、删除保护都成立')
    sys.exit(1 if _fail else 0)
