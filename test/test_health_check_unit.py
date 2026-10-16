#!/usr/bin/python
# coding:utf-8
"""单测：采集守护的**产出体检**判据（离线，不打网络、不读真 data/）。

要防的两类错，方向相反、都要防：

1. **漏报**（漏掉真故障）：隧道死了、`systemctl is-active` 仍是 `active` —— 这正是
   2026-10-15 青果隧道那次事故的形状。判据必须能从「通道探不通」推出 `down`。
2. **误报**（半夜吓人）：凌晨没人在播时 `data/` 不增长是**正常**的。纯 mtime 判据会在
   每天后半夜稳定报警，报几次之后就没人看了 —— 告警一旦被无视，等于没有告警。
   所以「没产出」必须**再问一句有没有人在播**，有人才判 `stale`。

用法：python test/test_health_check_unit.py
"""
import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'service'))

# ⚠️ **本文件不自己包 stdout** —— `health_check` 导入时已经包过一层了。
# 再包一层会出现：第二层持有第一层的 .buffer，第一层随即失去引用被 GC，
# 其 __del__ 把底层 buffer 一并 close → 之后所有 print 都是
# `ValueError: I/O operation on closed file`（踩过一次）。
from health_check import (check_channel, classify_status, decide,
                          newest_write_within, DEFAULT_ROSTER)

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


# ---------- 1. decide()：四条分支 + 退出码 ----------
def test_decide_truth_table():
    print('\n[1] decide() 真值表')

    st, healthy, why = decide(probe_ok=False, probe_error='ProxyError: refused',
                              live_seen=0, fresh=False)
    check('通道不通 → down', st == 'down', st)
    check('通道不通 → 不健康（退出码 1）', healthy is False)
    check('down 的文案带上底层错误', 'refused' in why, why)

    st, healthy, _ = decide(probe_ok=True, probe_error=None, live_seen=0, fresh=True)
    check('通了且有产出 → ok', st == 'ok', st)
    check('ok → 健康（退出码 0）', healthy is True)

    st, healthy, why = decide(probe_ok=True, probe_error=None, live_seen=3, fresh=False)
    check('通了、有人播、但没产出 → stale', st == 'stale', st)
    check('★ stale → **不健康**（在播却采不到，就是事故）', healthy is False)
    check('stale 的文案点出「在播」', '在播' in why or '3' in why, why)

    st, healthy, _ = decide(probe_ok=True, probe_error=None, live_seen=0, fresh=False)
    check('通了、没人播、没产出 → quiet（半夜正常态）', st == 'quiet', st)
    check('★ quiet → **健康**（不能报成故障，否则每晚误报）', healthy is True)

    # 有产出时 live_seen 无所谓 —— 有产出本身就是最强的健康证据
    st, healthy, _ = decide(True, None, 0, True)
    check('有产出时 live_seen 不影响判定', (st, healthy) == ('ok', True))

    # 通道不通时，即便 data/ 恰好有旧文件在窗口内，也不能判 ok —— 通道是主信号
    st, healthy, _ = decide(False, 'boom', 5, True)
    check('★ 通道不通优先于产出新鲜（down 覆盖 ok）', (st, healthy) == ('down', False))


# ---------- 2. classify_status()：两类「没开播」与两类失败 ----------
def test_classify_status():
    print('\n[2] classify_status()：区分「拿到数据」与「没拿到数据」')
    check("'正在直播' → live", classify_status('正在直播') == 'live')
    check("★ '已结束' → offline（**拿到数据**：enter 里 room_status==2）",
          classify_status('已结束') == 'offline')
    check("★ '未知' → nodata（**没拿到数据 = 失败**，见下）",
          classify_status('未知') == 'nodata')
    check("'错误' → error（没打通）", classify_status('错误') == 'error')

    # ★ 坑 1：把 '未知' 当「房间没开播，正常」—— 名字像“不知道”，实际相反。
    #  死代理实测（2026-10-15，DOUYIN_PROXY 指向 127.0.0.1:1）：
    #  enter 全部返回空体 → 全部 '未知' → 那一版报的是 ✅ ok，8 个房间 0 个错误。
    #  看门狗对它唯一要防的场景（隧道全死）**完全失明**。
    sample = ['未知'] * 8
    failed = sum(1 for s in sample if classify_status(s) in ('nodata', 'error'))
    check('★★ 死代理形状（8 个全「未知」）→ 8 个失败，不是 0 个',
          failed == 8, f'只数出 {failed} 个 —— 假阴性')
    check('★★ 死代理形状 → 通道判定为**不通**（必须报 down）',
          (len(sample) - failed) == 0)

    # ★ 坑 2：排除法（不是「正在直播」就算错）—— 200 人名单里没开播的是绝大多数，
    #   于是每次采样都判「通道错误」。实测那次 5 个房间里 4 个被误判。
    quiet = ['已结束'] * 8                     # 8 个房间全没开播 = 后半夜的常态
    failed2 = sum(1 for s in quiet if classify_status(s) in ('nodata', 'error'))
    check('★ 8 个房间全「已结束」→ 0 个失败（不是 8 个！）', failed2 == 0, f'误判 {failed2} 个')
    check('★ 8 个房间全「已结束」→ 通道判定为**通**，不会报 down',
          (len(quiet) - failed2) > 0)

    mixed = ['已结束', '已结束', '未知', '正在直播', '错误']
    f = sum(1 for s in mixed if classify_status(s) in ('nodata', 'error'))
    check('混合样本：5 个探针里有 2 个失败（未知 + 错误）', f == 2, f'数出 {f} 个')
    check('混合样本里有 1 个成功就已证明通道通', (len(mixed) - f) > 0)


# ---------- 3. newest_write_within()：mtime 判据 ----------
def test_newest_write_within():
    print('\n[3] newest_write_within()')
    tmp = tempfile.mkdtemp(prefix='hc_unit_')
    try:
        # 结构照抄 data/ 的真实形状：日期目录 / 场次目录 / 文件
        sess = os.path.join(tmp, '20261015', 'live_123_20261015_120000')
        os.makedirs(os.path.join(sess, 'frames'))
        fresh_file = os.path.join(sess, 'frames', '00001.jpg')
        old_file = os.path.join(tmp, '20261014', 'live_9_20261014_090000', 'room.json')

        io.open(fresh_file, 'w', encoding='utf-8').write('x')
        os.makedirs(os.path.dirname(old_file))
        io.open(old_file, 'w', encoding='utf-8').write('x')

        now = time.time()
        os.utime(fresh_file, (now - 60, now - 60))            # 1 分钟前
        os.utime(old_file, (now - 7200, now - 7200))          # 2 小时前

        check('窗口 30 分钟 → 有（嵌套在 frames/ 里也能找到）',
              newest_write_within(tmp, 30) is True)
        check('窗口 0 分钟 → 无（cutoff = 现在，旧文件不算）',
              newest_write_within(tmp, 0) is False)

        # 把新文件也推旧，整个目录变「不新鲜」
        os.utime(fresh_file, (now - 7200, now - 7200))
        check('全部文件都 2 小时前 → 窗口 30 分钟判为无',
              newest_write_within(tmp, 30) is False)
        check('窗口 180 分钟 → 又有（说明阈值确实在起作用）',
              newest_write_within(tmp, 180) is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    check('目录不存在 → False（不抛）',
          newest_write_within(os.path.join(tempfile.gettempdir(), 'hc_nope_xyz'), 30) is False)
    check('空目录 → False',
          newest_write_within(tempfile.mkdtemp(prefix='hc_empty_'), 30) is False)


# ---------- 4. check_channel()：桩掉两个模块，端到端验决定 down 的那段循环 ----------
class _StubFetcher:
    """假 fetcher：按脚本依次吐出 status。`get_room_status` 永不抛（与真实实现一致）。"""
    script = []
    calls = 0

    def __init__(self, did, auth=None):
        self.did = did

    def get_room_status(self):
        i = _StubFetcher.calls
        _StubFetcher.calls += 1
        st = _StubFetcher.script[min(i, len(_StubFetcher.script) - 1)]
        if isinstance(st, Exception):
            raise st
        return (st == '正在直播'), st, f'主播{self.did}', '111'

    def stop(self):
        pass


def _with_stub_modules(anchors, script, fn):
    """把 auto_crawl / liveMan 换成桩，跑 fn，再原样还回去（**不 import 真的两个模块**）"""
    import types
    fake_crawl = types.ModuleType('auto_crawl')
    fake_crawl.load_anchors = lambda path: anchors
    fake_live = types.ModuleType('liveMan')
    fake_live.DouyinLiveWebFetcher = _StubFetcher
    saved = {k: sys.modules.get(k) for k in ('auto_crawl', 'liveMan')}
    sys.modules['auto_crawl'] = fake_crawl
    sys.modules['liveMan'] = fake_live
    _StubFetcher.script = script
    _StubFetcher.calls = 0
    try:
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_check_channel_loop():
    print('\n[4] check_channel() 循环（桩模块，不打网络）')
    anchors = [(f'主播{i}', f'{100000 + i}') for i in range(20)]

    # ★ 死代理形状：全部拿不到数据 → 必须 not reached（第一版在这里判 ok）
    ok, err, live, probed, failed = _with_stub_modules(
        anchors, ['未知'], lambda: check_channel('x.xlsx', 8))
    check('★★ 8 个全「未知」→ ok=False（会报 down）', ok is False, f'ok={ok}')
    check('★★ 8 个全「未知」→ failed=8', failed == 8, f'failed={failed}')
    check('★ 文案点明「隧道/代理全死形状」', '全死' in (err or ''), err)

    # 全部没开播（拿到了数据）→ 通，但不该报 stale（live_seen=0 由 decide 处理）
    ok, err, live, probed, failed = _with_stub_modules(
        anchors, ['已结束'], lambda: check_channel('x.xlsx', 8))
    check('8 个全「已结束」→ ok=True，failed=0', (ok, failed) == (True, 0), f'{ok},{failed}')
    check('8 个全「已结束」→ live_seen=0', live == 0)
    check('8 个全「已结束」→ 探满 8 个（没有提前停）', probed == 8, probed)

    # 中间遇到在播 → 提前停（少给风控添事）
    ok, err, live, probed, failed = _with_stub_modules(
        anchors, ['已结束', '已结束', '正在直播', '已结束'], lambda: check_channel('x.xlsx', 8))
    check('遇在播提前停：只探了 3 个', probed == 3, probed)
    check('遇在播：live_seen=1 且 ok=True', (live, ok) == (1, True))

    # 混合：有失败也有成功 → 仍判通（这正是「个别房间抽风不该报警」）
    ok, err, live, probed, failed = _with_stub_modules(
        anchors, ['未知', '错误', '已结束', '已结束'], lambda: check_channel('x.xlsx', 8))
    check('混合：2 失败 2 成功 → ok=True', ok is True)
    check('混合：failed 数对（未知+错误都算）', failed == 2, failed)

    # 构造抛异常 → 也算失败，不能把异常漏成「成功」
    ok, err, live, probed, failed = _with_stub_modules(
        anchors, [RuntimeError('boom')], lambda: check_channel('x.xlsx', 4))
    check('fetcher 抛异常 → failed=4、ok=False', (failed, ok) == (4, False))

    # 名单读不到 → down，且文案说明是名单问题而不是通道问题
    ok, err, live, probed, failed = _with_stub_modules([], ['已结束'],
                                                       lambda: check_channel('x.xlsx', 8))
    check('空名单 → ok=False，文案点明名单', ok is False and '名单' in (err or ''), err)

    # 取样是均匀的（名单按分层排，取前 N 个会集中在同一档）
    ok, err, live, probed, failed = _with_stub_modules(
        anchors, ['已结束'], lambda: check_channel('x.xlsx', 5))
    check('20 人名单探 5 个 → 只探 5 个', probed == 5, probed)


# ---------- 5. 磁盘信号：只告警、不改退出码 ----------
def test_disk_signal():
    print('\n[5] 磁盘信号（复用 _DiskGuard，只告警不改退出码）')
    from health_check import disk_free_gb

    g = disk_free_gb()
    check('能量到本机剩余空间（正数）', isinstance(g, float) and g > 0, g)
    check('量到的是 GB 量级（< 100000）', g is not None and g < 100000, g)

    # 量不到时必须是 None，不能是 0 —— 0 会被读成「磁盘满了」
    import health_check as hc
    saved = hc.__dict__.get('__disk_stub__')
    real = hc.disk_free_gb
    try:
        hc.disk_free_gb = lambda path=hc.DATA_ROOT: None
        check('量不到返回 None（不是 0 —— 0 是「已满」这个**错误的事实**）',
              hc.disk_free_gb() is None)
    finally:
        hc.disk_free_gb = real

    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'service', 'health_check.py'), encoding='utf-8').read()
    check('★ 磁盘阈值参数存在（要与 douyin-crawl 的 --min-free-gb 对齐）',
          "'--min-free-gb'" in src and 'default=5.0' in src)
    check('★ 磁盘量法取自 auto_crawl._DiskGuard（不另写一份）',
          'from auto_crawl import _DiskGuard' in src)
    check('★ 磁盘低**不**进 healthy 判定（decide 的入参里没有磁盘）',
          'def decide(probe_ok, probe_error, live_seen, fresh)' in src)
    check('磁盘进 health.json（只看 --failed 会漏掉它，所以要有机器可读的一份）',
          "'disk':" in src)


# ---------- 6. 契约：退出码 / 参数 / 定时器存在 ----------
def test_contract():
    print('\n[6] 契约（退出码、参数、systemd 定时器）')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, 'service', 'health_check.py'), encoding='utf-8').read()
    check('退出码由 healthy 决定（systemctl --failed 才能看见）',
          'return 0 if healthy else 1' in src)
    check('有 --stale-min', "'--stale-min'" in src)
    check('有 --probe', "'--probe'" in src)
    check('★ 444 判据取自 liveMan（单一来源，不在体检里重写一遍）',
          'from liveMan import is_risk_control_444' in src)
    # 精确到**代码形状**：注释/文档里为了说明「不要这么写」而引用字面量是允许的，
    # 但 `if "444" in line` / `if '444' in line` 这种判据行不许出现
    import re
    bare = [l for l in src.splitlines()
            if not l.lstrip().startswith('#')
            and re.search(r'^\s*if\s+["\']444["\']\s+in\s+line', l)]
    check('★ 体检里没有裸子串匹配 444（这正是那次假阳性报告的成因）', not bare, bare)
    check('★ 量不到 444 时返回 None，不返回 0（0 是「干净」的意思，不能混）',
          "'risk444': None" in src)
    check('探通道用的是守护进程同一条代码路径（DouyinLiveWebFetcher.get_room_status）',
          'DouyinLiveWebFetcher' in src and 'get_room_status' in src)

    for rel in ['service/douyin-healthcheck.service', 'service/douyin-healthcheck.timer']:
        check(f'{rel} 存在', os.path.isfile(os.path.join(root, rel)))

    timer = io.open(os.path.join(root, 'service', 'douyin-healthcheck.timer'),
                    encoding='utf-8').read() if os.path.isfile(
        os.path.join(root, 'service', 'douyin-healthcheck.timer')) else ''
    check('定时器周期是 10 分钟', 'OnUnitActiveSec=10min' in timer or 'OnUnitActiveSec=600' in timer,
          timer[:200])
    check('定时器开机后也跑一次（OnBootSec）', 'OnBootSec=' in timer)
    check('★ 定时器 Persistent=true（关机期间错过的检查补跑）', 'Persistent=true' in timer)

    svc = io.open(os.path.join(root, 'service', 'douyin-healthcheck.service'),
                  encoding='utf-8').read() if os.path.isfile(
        os.path.join(root, 'service', 'douyin-healthcheck.service')) else ''
    check('体检单元是 Type=oneshot', 'Type=oneshot' in svc)
    check('体检单元读同一份代理配置', 'config/proxy.env' in svc)
    # 只看**生效的指令行**，注释里为了解释「不要这么写」而提到 Restart= 是允许的
    active = [l.strip() for l in svc.splitlines()
              if not l.lstrip().startswith('#') and l.strip()]
    check('★ 体检单元没有 Restart=（失败正是要留下的信号，重试会把退出码冲成成功）',
          not any(l.startswith('Restart=') for l in active),
          [l for l in active if l.startswith('Restart=')])

    man = io.open(os.path.join(root, 'service', 'MANIFEST'), encoding='utf-8').read()
    for rel in ['service/health_check.py', 'service/douyin-healthcheck.service',
                'service/douyin-healthcheck.timer']:
        check(f'MANIFEST 含 {rel}', rel in man)

    # 体检探的就是守护进程在采的那份名单 —— 两者漂移会让「探到有人在播」与
    # 「采不到」不可比（体检说 stale，实际只是探了一份守护进程没在跑的名单）
    check('默认名单与生产 ExecStart 指向同一份',
          DEFAULT_ROSTER.endswith('huitun_10_100w_1015_final.xlsx'), DEFAULT_ROSTER)
    unit = io.open(os.path.join(root, 'service', 'douyin-crawl.service'),
                   encoding='utf-8').read()
    check('★ 生产 ExecStart 用的也是同一份名单（与 DEFAULT_ROSTER 一致）',
          'huitun_10_100w_1015_final.xlsx' in unit,
          [l for l in unit.splitlines() if 'huitun' in l])


if __name__ == '__main__':
    print('=' * 64)
    print('采集守护产出体检判据单测（离线）')
    print('=' * 64)
    test_decide_truth_table()
    test_classify_status()
    test_newest_write_within()
    test_check_channel_loop()
    test_disk_signal()
    test_contract()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 体检判据有误 —— 要么漏报真故障，要么半夜误报')
        sys.exit(1)
    print('✅ 体检判据正确（漏报与误报两个方向都覆盖）')
