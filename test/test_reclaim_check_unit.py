#!/usr/bin/python
# coding:utf-8
"""单测：回收链路体检的三条判据（离线：不打网络、不连 COS、不读真 data/）。

要防的两类错，方向相反、都要防：

1. **漏报**（链路停了却报健康）。尤其两种难发现的停法：
   * **归档 timer 被停用/没 enable** —— 单元永远不会 failed，`systemctl --failed` 一片
     干净，磁盘照涨；
   * **账本说已归档、COS 上对象没了** —— 唯一副本没了，而 `cos_archive` 自己的 HEAD
     复查排在 `if not todo: return EXIT_OK` **之后**，恰恰在日常的「没有新场次要传」
     场景里一次都不做。
2. **误报**（半夜吓人）。三种形状：
   * 检查跑在归档 timer **前面** → 每次重启报一次「归档心跳不存在」；
   * 「刚结束、还没到点归档」的场次被当成丢失；
   * 对**已删场次**的历史账本记录狂发 HEAD（既慢又不是丢失 —— 删除当时已当场核过）。

告警一旦被无视，等于没有告警 —— 所以两个方向都要钉住。

用法：python test/test_reclaim_check_unit.py
"""
import os
import re
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE = os.path.join(ROOT, 'service')
sys.path.insert(0, ROOT)
sys.path.insert(0, SERVICE)

# ⚠️ 本文件**不自己包 stdout** —— `health_check` 导入时已经包过一层了。
# 再包一层会出现「第二层持有第一层的 .buffer，第一层失去引用被 GC，__del__ 顺手把底层
# buffer close 掉」→ 之后所有 print 都是 `ValueError: I/O operation on closed file`。
# （`test_ledger_unit.py` 一开始就是这么炸的。）
import cos_archive as ca
from reclaim_check import (DEFAULT_MAX_IDLE_MIN, DEFAULT_MIN_FREE_GB,
                           audit_objects, beat_age_min, decide, sessions_on_disk)

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


def src(name):
    with open(os.path.join(SERVICE, name), encoding='utf-8') as fh:
        return fh.read()


def code_only(text):
    """去掉整行注释。

    ⚠️ 这不是洁癖：本仓库的注释里到处引用代码片段（`load_cos_env()`、
    `EnvironmentFile`、`--dump-ledger`…），直接对源码做子串/顺序断言会被**注释本身**
    骗过去 —— 一开始就踩到了：`_cycle` 里那句「位置在 `load_cos_env()` **之前**」的注释
    比 `touch_beat()` 更靠前，于是「心跳在取凭据之前」这条断言假失败。
    """
    return '\n'.join(ln for ln in text.splitlines() if not ln.strip().startswith('#'))


def body(source, func):
    """从 `def func(` 到下一个顶层 `def`/`class` —— 用来把「某函数里有没有某句」钉住。"""
    m = re.search(rf'^def {re.escape(func)}\(', source, re.M)
    if not m:
        return ''
    nxt = re.search(r'^(?:def |class )', source[m.end():], re.M)
    return source[m.end():m.end() + (nxt.start() if nxt else len(source))]


class FakeCos:
    """假 COS：只实现 `size_of`（判据「对象在不在」唯一用到的那一个），并记下问了哪些 key。"""

    def __init__(self, objects):
        self.objects = dict(objects)      # {key: 字节数}
        self.asked = []

    def size_of(self, key):
        self.asked.append(key)
        return self.objects.get(key)


# ================================================================ ① 判据真值表
def test_decide_truth_table():
    print('\n[1] decide() —— 三条判据各自命中、边界、以及「测不到」不许当健康')

    def d(free=25.0, age=12.0, probs=(), creds=''):
        return decide(free, DEFAULT_MIN_FREE_GB, age, DEFAULT_MAX_IDLE_MIN,
                      list(probs), creds)

    # 全绿
    check('全绿 → 无告警', d() == [], str(d()))

    # 边界：严格不等号 —— 正好压在线上不算告警（否则阈值就成了「默认报警」）
    check('磁盘正好等于告警线（10.0）→ 不报', d(free=10.0) == [], str(d(free=10.0)))
    check('磁盘略低于告警线 → 报 disk',
          [k for k, _ in d(free=9.9)] == ['disk'], str(d(free=9.9)))
    check('心跳正好等于阈值（150 分钟）→ 不报', d(age=150.0) == [], str(d(age=150.0)))
    check('心跳略超阈值 → 报 beat',
          [k for k, _ in d(age=150.1)] == ['beat'], str(d(age=150.1)))

    # ★ 「测不到」两个方向都要防：不许静默当健康
    check('★ 磁盘未测到（None）→ **报** disk（不当健康）',
          [k for k, _ in d(free=None)] == ['disk'], str(d(free=None)))
    check('★ 心跳文件不存在（None）→ **报** beat（不当成「刚跑过」）',
          [k for k, _ in d(age=None)] == ['beat'], str(d(age=None)))

    # 凭据读不到：归档与核实都做不了，也是告警（注意与 health_check 对磁盘的 fail-open 相反）
    check('凭据读不到 → 报 cos',
          [k for k, _ in d(creds='config/cos.env 不存在')] == ['cos'],
          str(d(creds='x')))

    # ③ 是逐场次的，几场就几条
    check('两场对象对不上 → 两条 cos',
          [k for k, _ in d(probs=[('a', 'x'), ('b', 'y')])] == ['cos', 'cos'])
    check('三条全中 → 三类都在',
          sorted({k for k, _ in d(free=3.0, age=None, creds='boom')}) ==
          ['beat', 'cos', 'disk'])

    # 人话里要能看见**数字**（说不出「剩多少 / 多久没动」的告警没法处置）
    check('disk 告警文本里带剩余量与阈值',
          '9.9' in d(free=9.9)[0][1] and '10' in d(free=9.9)[0][1], d(free=9.9)[0][1])
    check('beat 告警文本里带小时数',
          '2.5' in d(age=150.1)[0][1], d(age=150.1)[0][1])


# ================================================================ ② 心跳年龄
def test_beat_age():
    print('\n[2] beat_age_min() —— **文件不存在返回 None，不是 0**')

    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, 'cos_archive.beat')
        check('文件不存在 → None（0 会被读成「刚跑过」）', beat_age_min(p) is None)
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('2026-10-16T20:00:00\n')
        os.utime(p, (time.time() - 3600, time.time() - 3600))
        age = beat_age_min(p)
        check('一小时前写的 → ≈60 分钟', age is not None and 59 < age < 61, str(age))
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ================================================================ ③ 对象核对
def test_audit_objects():
    print('\n[3] audit_objects() —— 只核「账本说已归档 + 还在磁盘上」的')

    ledger = {
        'data/20261016/live_a': {'verify': 'ok', 'tar_bytes': 100},
        'data/20261016/live_b': {'verify': 'ok', 'tar_bytes': 200},
        'data/20261016/live_c': {'verify': 'ok', 'tar_bytes': 300},
        'data/20261016/live_d': {'verify': 'fail', 'tar_bytes': 400},   # 传失败过
        'data/20261016/live_e': {'verify': 'ok'},                       # 老账本没记字节数
    }
    on_disk = list(ledger)

    # 都在 → 无问题（live_e 的账本没记字节数，所以这里给它一个**任意**存在的对象）
    objects = {ca.session_key(r): ledger[r].get('tar_bytes') or 500 for r in on_disk}
    cos = FakeCos(objects)
    problems, checked = audit_objects(cos, ledger, on_disk)
    check('对象都在、字节数都对 → 无问题', problems == [], str(problems))
    check('★ 传失败的（verify=fail）**不核**（归归档单元自己的退出码管）',
          ca.session_key('data/20261016/live_d') not in cos.asked, str(cos.asked))

    # 对象没了
    cos = FakeCos({ca.session_key('data/20261016/live_a'): 100})
    problems, _ = audit_objects(cos, ledger, on_disk)
    got = {r: w for r, w in problems}
    check('★ COS 上对象没了 → 报（唯一副本丢失）',
          'data/20261016/live_b' in got and '找不到' in got['data/20261016/live_b'],
          str(problems))
    check('字节数相符的那场**不**报', 'data/20261016/live_a' not in got)

    # 字节数不符（对象在、但短了/长了 —— 半截上传或被人覆盖）
    cos = FakeCos({ca.session_key('data/20261016/live_a'): 99})
    problems, _ = audit_objects(cos, ledger, on_disk)
    check('★ 字节数不符 → 报，且**两个数字都写出来**',
          problems and problems[0][0] == 'data/20261016/live_a'
          and '99' in problems[0][1] and '100' in problems[0][1], str(problems))

    # 老账本没有 tar_bytes → 只判「在不在」，不瞎报
    cos = FakeCos({ca.session_key('data/20261016/live_e'): 12345})
    problems, checked = audit_objects(cos, ledger, ['data/20261016/live_e'])
    check('账本没记字节数时只判存在性（不拿 None 去比）', problems == [], str(problems))

    # ★ 已删场次（账本里有、磁盘上没有）不许进核对范围
    cos = FakeCos({})
    problems, checked = audit_objects(cos, ledger, [])
    check('★ 已删场次不进核对范围 → 一次 HEAD 都不发', cos.asked == [], str(cos.asked))
    check('   checked 也为空', checked == [], str(checked))


# ================================================================ ③ 范围过滤
def test_sessions_on_disk():
    print('\n[4] sessions_on_disk() —— 账本累积，只留还在磁盘上的')

    root = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(root, 'data', '20261016', 'live_a'))
        ledger = {'data/20261016/live_a': {},      # 在
                  'data/20261016/live_b': {}}      # 已被笔记本删掉（账本记录还留着）
        got = sessions_on_disk(ledger, root)
        check('★ 只留下目录还存在的（已删的记录不查 —— 删除当时已当场核过）',
              got == ['data/20261016/live_a'], str(got))
        check('空账本 → 空（不报错）', sessions_on_disk({}, root) == [])
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ================================================================ 契约
def test_contract():
    print('\n[5] 契约：心跳只由归档轮写、检查排在归档之后、清单里有这三个文件')

    ca_src = src('cos_archive.py')
    rc_src = src('reclaim_check.py')
    ca_code = code_only(ca_src)
    rc_code = code_only(rc_src)

    # ★ 心跳只在 _cycle 里写 —— 尤其**不能在 dump_ledger 里**
    #   （笔记本每 30 分钟就调一次 --dump-ledger，那也写心跳的话，timer 早死了也看不出来）
    #   数的是**整行调用**：`def touch_beat():` 也含 `touch_beat()` 这个子串。
    calls = [ln for ln in ca_code.splitlines() if ln.strip() == 'touch_beat()']
    check('★ touch_beat() 全文件只被调用一次', len(calls) == 1, f'calls={calls}')
    check('★ 那一次在 _cycle 里（不在 dump_ledger / selftest 路径）',
          'touch_beat()' in body(ca_code, '_cycle'))
    cyc = body(ca_code, '_cycle')
    check('★ 它在 load_cos_env() **之前**（心跳答的是「跑起来没」，不是「成功没」）',
          cyc.index('touch_beat()') < cyc.index('load_cos_env()'),
          f'beat@{cyc.index("touch_beat()")} vs env@{cyc.index("load_cos_env()")}')
    check('dump_ledger 里没有心跳', 'touch_beat' not in body(ca_code, 'dump_ledger'))
    check('心跳文件在 service/dist 下（与账本/锁/日志同处）',
          re.search(r"^BEAT = os\.path\.join\(DIST, 'cos_archive\.beat'\)", ca_src, re.M)
          is not None)

    # 检查不抢归档的锁、也不写账本 —— 它只是**读**
    check('检查不抢锁（纯读，不与正在跑的那一轮冲突）',
          'acquire_lock' not in rc_code and 'append_record' not in rc_code)

    # 判据只有一份实现：磁盘复用 health_check、对象复用 cos_archive 的 Cos.size_of
    check('磁盘判据复用 health_check.disk_free_gb（不另写一份 statvfs）',
          'import health_check as hc' in rc_code and 'hc.disk_free_gb()' in rc_code
          and 'statvfs' not in rc_code)
    check('对象判据复用 cos_archive 的 Cos/session_key（不另写一份「在不在」）',
          'ca.Cos(cfg)' in rc_code and 'ca.session_key(' in rc_code)

    # 退出码：只有 0/1，systemd 只看零/非零
    check('退出码 = 0 健康 / 1 有告警',
          'return 0 if not items else 1' in rc_code)

    # 阈值默认值
    check(f'磁盘告警线默认 {DEFAULT_MIN_FREE_GB:g} GB（**高于**守护进程的降级线 5）',
          DEFAULT_MIN_FREE_GB == 10.0)
    check(f'心跳阈值默认 {DEFAULT_MAX_IDLE_MIN} 分钟（归档 timer 是 1 小时一轮 → 连丢两轮才报）',
          DEFAULT_MAX_IDLE_MIN == 150)

    # ★ 两个 timer 的先后：检查必须排在归档之后，否则每次重启都假报「心跳不存在」
    def on_boot(name):
        m = re.search(r'^OnBootSec=(\d+)min', src(name), re.M)
        return int(m.group(1)) if m else -1

    check('★ 体检 timer 的开机首跑排在归档之后（否则重启即假报）',
          on_boot('douyin-reclaimcheck.timer') > on_boot('douyin-archive.timer'),
          f'{on_boot("douyin-reclaimcheck.timer")} vs {on_boot("douyin-archive.timer")}')

    # 单元文件的既有约定：oneshot + root + 绝不 Restart
    for f in ('douyin-reclaimcheck.service', 'douyin-reclaimcheck.timer'):
        check(f'{f} 存在', os.path.exists(os.path.join(SERVICE, f)))
    svc = src('douyin-reclaimcheck.service')
    check('service 是 oneshot（跑完即退，退出码即判定）', 'Type=oneshot' in svc)
    check('service 以 root 跑（cos.env 是 root:root 600）', 'User=root' in svc)
    check('★ service **没有** Restart=（会把失败重试成成功，告警面就瞎了）',
          not re.search(r'^Restart=', code_only(svc), re.M))
    check('service 没有 EnvironmentFile（凭据是读文件，不是读环境变量）',
          'EnvironmentFile' not in code_only(svc))

    # 清单：不进 MANIFEST 就传不到服务器上，装了 timer 也没用
    manifest = src('MANIFEST')
    for f in ('service/reclaim_check.py', 'service/douyin-reclaimcheck.service',
              'service/douyin-reclaimcheck.timer'):
        check(f'MANIFEST 列了 {f}', f in manifest)


if __name__ == '__main__':
    print('=' * 64)
    print('回收链路体检判据单测（离线）')
    print('=' * 64)
    test_decide_truth_table()
    test_beat_age()
    test_audit_objects()
    test_sessions_on_disk()
    test_contract()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 回收链路体检判据有误 —— 要么漏报真故障，要么半夜误报')
        sys.exit(1)
    print('✅ 回收链路体检判据正确（漏报与误报两个方向都覆盖）')
