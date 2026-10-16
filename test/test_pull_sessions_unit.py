#!/usr/bin/python
# coding:utf-8
"""单测：笔记本侧拉取工具（`service/pull_sessions.py`）—— 全离线，不打网络、不起 rsync。

## 这个工具最要紧的一条安全断言

    服务器那份**只在本地校验通过之后**才允许删。

因为「传完自动删」意味着**本地立刻成为唯一副本**。这条一旦错，丢的是研究语料，
而且**丢得静默** —— 服务器删了、本地不全，下一轮两头都没有。所以本文件的重心不是
「选场对不对」，而是**每一条失败路径上交给 `--delete-list` 的清单都是空的**。

下面所有副作用（ssh / rsync / 删除 / 空间查询 / 日志 / 账本）**全部打桩**，
只有 `_cycle()` 本身的控制流是真的 —— 被测的正是那段控制流。
`verify()` 与 `ll.dir_manifest()` 用真的（喂合成文件），因为「算得对不对」也不该靠假设。

  [1] 路径映射：完整 → `data/`，不完整 → `_incomplete/`，且 `_incomplete` 不被当日期目录
  [2] 选场规则真值表（完整/不完整/采集中/已拉取/全空）
  [3] ★ 校验失败 → **一条都不删**（6 种失败形态逐个走一遍）
  [4] ★ 只有校验通过的才进删除清单（含「已在本地」那一路）
  [5] rsync 退出码非 0 → 不记入、不删除
  [6] 空间闸门：低于阈值 / 量不出来 → 不拉不删，且**连事实都不去取**
  [7] 账本往返幂等（同一轮跑两次，第二次待拉为空）
  [8] 锁：占住不放 / 过期自愈 / 释放
  [9] 契约：复用 `live_ledger.dir_manifest`、ssh/rsync **不过 PATH 查找**、
      **不删本地数据**、不在 `service/MANIFEST` 里、`--dump-facts`/`--delete-list` 存在
     且 `--dump-facts` 在 `load_anchors` 之前短路

用法：python test/test_pull_sessions_unit.py

⚠️ 本文件**不要**再包一层 `sys.stdout` —— `pull_sessions` 导入 `live_ledger` 时已经包过，
双层会让内层被 GC 掉、之后所有 print 报 `I/O operation on closed file`
（`test_health_check_unit.py` / `test_ledger_unit.py` 顶部同款注释）。
"""
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'service'))

import live_ledger as ll
import pull_sessions as ps

_ok = _fail = 0


def check(desc, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {desc}')
    else:
        _fail += 1
        print(f'  ❌ {desc}' + (f'  ← {detail}' if detail else ''))


# ================================================================ 合成数据
def _fact(rel, completeness='完整', modal='音视频', nbytes=1000, files=3,
          started='2026-10-16T00:00:00', audio=1, frames=1, danmaku=10, events=5):
    """一条 `--dump-facts` 口径的场次事实（键名照抄 `facts_public()`）。"""
    date, sess = rel.split('/')[1], rel.split('/')[2]
    return {'dir': rel, 'douyin_id': sess.split('_')[1], 'name': '主播',
            'date': date, 'started': started, 'ended': '', 'last_write': started,
            'inflight': completeness == '采集中', 'has_room_json': True,
            'danmaku_lines': danmaku, 'event_lines': events,
            'audio_segs': audio, 'frames': frames,
            'completeness': completeness, 'reason': '', 'modal': modal,
            'bytes': nbytes, 'files': files}


def _mkfiles(root, spec):
    """按 `{相对路径: 内容}` 造真文件，返回根目录（`dir_manifest` 要真读）。"""
    for rel, data in spec.items():
        p = os.path.join(root, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as fh:
            fh.write(data if isinstance(data, bytes) else data.encode('utf-8'))
    return root


def _m(rel, spec, hash_max=None):
    """给一个目录造清单（服务器侧那份用同一函数造 —— 两边算法同源）。"""
    with tempfile.TemporaryDirectory() as td:
        _mkfiles(td, spec)
        man = ll.dir_manifest(td)
    if hash_max is not None:
        man['hash_max_bytes'] = hash_max
    man['files'] = [dict(e, p=e['p']) for e in man['files']]
    return man


def _make(rel, spec):
    """→ `(服务器清单, 服务器真实内容)`。

    清单与内容必须成对给：打桩的 `rsync_pull` 要**照着真实内容**写出 staging，
    否则正对照会因「字节数对、内容不对」而挂在 sha256 上（那不是要测的东西）。
    """
    return _m(rel, spec), dict(spec)


# ================================================================ 1. 路径映射
def test_dest_rel():
    print('\n[1] 路径映射：完整 → data/，不完整 → _incomplete/')
    REL = 'data/20261016/live_123_20261016_001046'
    check('完整 → <dest>/20261016/live_…',
          ps.dest_rel('完整', REL) == '20261016/live_123_20261016_001046',
          ps.dest_rel('完整', REL))
    check('不完整 → <dest>/_incomplete/20261016/live_…',
          ps.dest_rel('不完整', REL) == '_incomplete/20261016/live_123_20261016_001046',
          ps.dest_rel('不完整', REL))
    check('★ `_incomplete` **不**被日期目录正则匹配（服务器扫描不会当生产目录）',
          not ll.DATE_DIR_RE.match(ps.INCOMPLETE))
    check('★ `_staging` 同样不匹配（半截目录不会被当「已在本地」）',
          not ll.DATE_DIR_RE.match(ps.STAGING))
    check('日期目录本身仍匹配（正对照，证明上面两条不是因为正则坏掉）',
          bool(ll.DATE_DIR_RE.match('20261016')))
    for bad in ('live_123_20261016_001046', 'data/20261016', 'x/y/z',
                'data/20261016/live_1/x'):
        try:
            ps.dest_rel('完整', bad)
            check(f'非法输入应报错：{bad}', False, '竟然通过了')
        except ValueError:
            check(f'非法输入报错：{bad}', True)

    print('  · MSYS2 路径转换（rsync 只认 /d/… 形式）')
    check('`D:/x` → `/d/x`', ps.to_msys('D:/x') == '/d/x', ps.to_msys('D:/x'))
    check('`D:\\x` → `/d/x`', ps.to_msys('D:\\x') == '/d/x', ps.to_msys('D:\\x'))
    check('已经是 `/d/x` 的原样返回', ps.to_msys('/d/x') == '/d/x')


# ================================================================ 2. 选场规则
def test_plan_cycle():
    print('\n[2] 选场规则真值表（★ 白名单：**只收「完整」**）')
    A = _fact('data/20261016/live_a_20261016_001000', '完整', started='2026-10-16T00:10:00')
    B = _fact('data/20261016/live_b_20261016_002000', '不完整', modal='仅文本数值',
              audio=0, frames=0, started='2026-10-16T00:20:00')
    C = _fact('data/20261016/live_c_20261016_003000', '采集中', started='2026-10-16T00:30:00')
    D = _fact('data/20261016/live_d_20261016_004000', '不完整', modal='空',
              audio=0, frames=0, danmaku=0, events=0, nbytes=0, files=0,
              started='2026-10-16T00:40:00')
    E = _fact('data/20261016/live_e_20261016_005000', '完整', started='2026-10-16T00:50:00')

    facts = [A, B, C, D, E]
    inv = {'data/20261016/live_e_20261016_005000': '/local/e'}
    todo, verify, skip = ps.plan_cycle(facts, inv)

    check('待拉 = 只有完整的 A', [f['dir'] for f in todo] == [A['dir']],
          [f['dir'] for f in todo])
    check('★ 不完整 B **不进待拉**（本工具只收完整场次）',
          B['dir'] not in [f['dir'] for f in todo])
    check('采集中 C 不进待拉', C['dir'] not in [f['dir'] for f in todo])
    check('全空 D 不进待拉（它在 classify_session 里就是 `不完整`）',
          D['dir'] not in [f['dir'] for f in todo])
    check('已在本地 E 进「待校验删除」而不是待拉',
          [f['dir'] for f in verify] == [E['dir']], [f['dir'] for f in verify])
    whys = dict(skip)
    check('跳过原因写得出来：C=采集中', '采集中' in whys.get(C['dir'], ''), whys.get(C['dir']))
    check('★ 跳过原因写明「只收完整场次」+ 服务器给的原因',
          '只收「完整」场次' in whys.get(B['dir'], ''), whys.get(B['dir']))
    check('  全空 D 的理由也走同一条（不再有单独的「全空」分支）',
          '只收「完整」场次' in whys.get(D['dir'], ''), whys.get(D['dir']))
    check('★ 排序按开播时间正序（先拉旧的，避免最新那场反复插队）',
          [f['started'] for f in todo] == sorted(f['started'] for f in todo))
    check('`modal == 空` 判为全空（`is_empty` 仍是「空场次」的**唯一**口径，'
          '只是 `plan_cycle` 不再单独用它 —— 白名单已经覆盖）',
          ps.is_empty(D) and not ps.is_empty(B))

    todo2, _, _ = ps.plan_cycle(facts, inv, only=A['dir'])
    check('`--only` 只处理那一场', [f['dir'] for f in todo2] == [A['dir']])

    todo3, v3, _ = ps.plan_cycle(facts, {})
    check('本地什么都没有时，两场完整的都进待拉（不完整的不进）',
          [f['dir'] for f in todo3] == [A['dir'], E['dir']] and not v3,
          [f['dir'] for f in todo3])


# ================================================================ 3/4/5. 管线：失败绝不删
class _Args:
    """`_cycle()` 需要的全套参数（全部默认值，逐个用例再覆盖）。"""
    host = 'fake-host'
    dest = ''
    inflight_min = 15
    min_free_gb = 10.0
    bwlimit = '3m'
    limit = 0
    only = ''
    keep_remote = False
    cos = True
    text_max_mb = 64.0
    dry_run = False
    quiet = True
    lock_min = 90
    rsync_timeout = 60


def _cosrec(fact, present=True, **over):
    """一条 `--dump-ledger` 口径的账本记录。**默认能通过 `cos_ok()`** ——
    要测门禁就显式传 `present=False` 或 `**over` 覆盖某一项。"""
    rec = {'ts': '2026-10-16T03:00:00', 'dir': fact['dir'],
           'key': ps.ca.session_key(fact['dir']),
           'tar_bytes': fact['bytes'] + 1024, 'tar_sha256': 'd' * 64,
           'bytes': fact['bytes'], 'files': fact['files'],
           'digest': 'e' * 64, 'etag': '"x"', 'verify': 'ok',
           'object_present': present}
    rec.update(over)
    return rec


class _Env:
    """把 `_cycle` 的全部副作用替换掉，只留控制流是真的。"""

    def __init__(self, dest, facts, inv, remote_mans, specs=None, rsync_rc=0,
                 rsync_mangle=None, free=100.0, cos_ledger=None):
        self.dest = dest
        self.facts = facts
        self.inv = inv
        self.remote_mans = remote_mans      # {rel: 服务器清单}
        self.specs = specs or {}            # {rel: {相对路径: 真实字节}}（打桩 rsync 照它写）
        self.rsync_rc = rsync_rc
        self.rsync_mangle = rsync_mangle or (lambda rel, staging: None)
        self.free = free
        # ★ 默认账本按 **facts 本身**造一份「全过」的 —— 这样「正对照」才是真的正对照。
        #   传 `cos_ledger={}` 或带 `present=False` 的记录就能测门禁拦截。
        if cos_ledger is None:
            cos_ledger = {f['dir']: _cosrec(f) for f in facts}
        self.cos_ledger = cos_ledger
        self.deleted = []                   # 每次 remote_delete 收到的 **rel 部分**
        self.deleted_raw = []               # 原样（含 \t 期望列）—— 契约断言要看它
        self.pulled = []
        self.rsync_calls = []               # [(rel, names, list_path)]
        self.cos_runs = 0
        self.ledger_calls = 0
        self.logs = []
        self._saved = {}

    def __enter__(self):
        def _rsync_pull(host, rel, staging, bwlimit, timeout, names, list_path):
            self.rsync_calls.append((rel, list(names), list_path))
            # 真的把清单写出来（走**生产**那个函数）—— 这样 NUL 分隔与每轮清理都被真跑一遍
            ps.write_file_list(list_path, names)
            os.makedirs(staging, exist_ok=True)
            # 照「服务器真实内容」写出来，但**只写 names 点名的那些** —— 真实 rsync 也只拉
            # 这些；打桩不这么裁的话，正对照会因为本地凭空多出媒体文件而失败。
            spec = {k: v for k, v in self.specs.get(rel, {}).items() if k in names}
            _mkfiles(staging, spec)
            self.rsync_mangle(rel, staging)
            self.pulled.append(rel)
            if self.rsync_rc:
                return self.rsync_rc, '', '假的 rsync 失败'
            return 0, '', ''

        def _remote_delete(host, lines, inflight_min, apply=True, label='已传输+COS'):
            self.deleted_raw.append((list(lines), label))
            self.deleted.append([ln.split('\t')[0] for ln in lines])
            return 0, f'（假删 {len(lines)} 场）', ''

        def _remote_cos_ledger(host):
            self.ledger_calls += 1
            return dict(self.cos_ledger)

        def _run_cos_archive(host):
            self.cos_runs += 1
            return 0, '（假归档）', ''

        patches = {
            'free_gb': lambda p: self.free,
            'remote_facts': lambda h, m: list(self.facts),
            'inventory_local': lambda d: dict(self.inv),
            'remote_manifest': lambda h, rel: self.remote_mans[rel],
            'rsync_pull': _rsync_pull,
            'remote_delete': _remote_delete,
            'remote_cos_ledger': _remote_cos_ledger,
            'run_cos_archive': _run_cos_archive,
            'append_manifest': lambda rec: None,
            'log': lambda msg: self.logs.append(msg),
        }
        for k, v in patches.items():
            self._saved[k] = getattr(ps, k)
            setattr(ps, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            setattr(ps, k, v)

    @property
    def delete_list(self):
        """本轮实际交给服务器的场次目录（合并所有调用）。"""
        out = []
        for b in self.deleted:
            out += b
        return out

    @property
    def delete_lines(self):
        """本轮实际交给服务器的**原始清单行**（含 `\\t字节\\t文件数` 期望列）。"""
        out = []
        for lines, _label in self.deleted_raw:
            out += lines
        return out

    def says(self, needle):
        return [m for m in self.logs if needle in m]


def _func_src(src, name):
    """抠出 `def name(...)` 到下一个顶层 `def`/`# ===` 的源码。

    契约里要断言的令牌多半是**字符串字面量**（`'-r'`、`'--from0'`），
    而 `_strip_comments()` 会把字符串抹掉 —— 所以那几条必须看原文。
    """
    m = re.search(rf'^def {re.escape(name)}\(', src, re.M)
    if not m:
        return ''
    rest = src[m.end():]
    nxt = re.search(r'^(?:def |# =)', rest, re.M)
    return src[m.start(): m.end() + (nxt.start() if nxt else len(rest))]


# ================================================================ 2b~2e. 本轮新增的纯函数
def test_text_manifest():
    print('\n[2b] `text_manifest()`：只留顶层 8 种已知文本，且**两侧用同一个**过滤')
    REL = 'data/20261016/live_a_20261016_001000'
    keep = {'room.json': b'{"a":1}', 'danmaku.jsonl': b'hello', 'events.jsonl': b'x',
            'series.json': b'{}', 'series.jsonl': b'', 'fansclub_edges.jsonl': b'',
            'session_meta.json': b'{}', 'ffmpeg_media.log': b'log'}
    spec = dict(keep)
    spec.update({
        'frames/frame_0001.jpg': b'JPEG' * 100,      # 媒体 → 走 COS，不进本机
        'audio/seg_00001.wav': b'RIFF' * 100,
        ps.LOCAL_MARKER: b'{"key":"x"}',             # 本机自己的指针 → 必须滤掉
        'mystery_new_artifact.bin': b'??',           # 未知 → fail-closed（另由 unknown 报出）
    })
    rm = _m(REL, spec)
    tm = ps.text_manifest(rm)
    names = {e['p'] for e in tm['files']}

    check('只留顶层那 8 种（一个不多一个不少）', names == set(ps.KNOWN_TOP_FILES),
          sorted(names))
    check('★ `frames/` `audio/` 全被丢掉（媒体在 COS，公网这条腿只拉文本）',
          not any('/' in n for n in names))
    check('★ `_local_cos_media.json` 被滤掉（否则第二轮 `to_verify` 恒判「本地多 1 个文件」）',
          ps.LOCAL_MARKER not in names)
    check('未知顶层文件也被滤掉（靠 `unknown_top_files()` 单独拦，不靠这里）',
          'mystery_new_artifact.bin' not in names)
    check('count/total 按**过滤后**重算',
          tm['count'] == len(tm['files']) == 8
          and tm['total'] == sum(e['n'] for e in tm['files']), (tm['count'], tm['total']))
    check('★ `hash_max_bytes` **原样保留**（退化成 None 会让 `verify()` 的版本守卫'
          '变成 `None == None` 而静默失效）',
          tm['hash_max_bytes'] == rm['hash_max_bytes'] == ll.MANIFEST_HASH_MAX,
          (tm['hash_max_bytes'], rm['hash_max_bytes']))
    check('★ `digest` 用 `ll.manifest_digest` **重算**（本机没有第二份哈希实现）',
          tm['digest'] == ll.manifest_digest(tm['files']))
    check('顶层 partial_dirs 原样保留、媒体侧的不进（后者的整段丢失不算文本腿的事）',
          all('/' not in d for d in tm['partial_dirs']), tm['partial_dirs'])

    check('`unknown_top_files()` 报出那个未知文件',
          ps.unknown_top_files(rm) == ['mystery_new_artifact.bin'],
          ps.unknown_top_files(rm))
    check('★ `_local_cos_media.json` **不算**未知文件（是本机自己的指针，不是采集端新产物）',
          ps.unknown_top_files(_m(REL, {ps.LOCAL_MARKER: b'{}'})) == [])
    check('全是已知文件时 `unknown_top_files()` 为空（正对照）',
          ps.unknown_top_files(_m(REL, keep)) == [])

    # ★ 对称性：本机那份多出指针文件时，两侧过滤后必须仍然相等 —— 这就是第二轮能过校验的原因
    with tempfile.TemporaryDirectory() as td:
        _mkfiles(td, keep)
        _mkfiles(td, {ps.LOCAL_MARKER: b'{"key":"x"}'})
        lm = ll.dir_manifest(td)
        ok, probs = ps.verify(ps.text_manifest(rm), ps.text_manifest(lm))
        check('★ 本机多一个 `_local_cos_media.json` → 过滤后仍一致（第二轮不会恒判不一致）',
              ok, probs)


def test_cos_gate():
    print('\n[2c] `cos_ok()` 真值表（缺记录 / verify / 键 / ★object_present / bytes·files）')
    REL = 'data/20261016/live_a_20261016_001000'
    f = _fact(REL, nbytes=1000, files=3)
    good = _cosrec(f)
    check('全等 → 通过（且 `cos_why` 给空串）',
          ps.cos_ok(REL, good, f) and ps.cos_why(REL, good, f) == '',
          ps.cos_why(REL, good, f))
    check('★ `cos_ok` 就是 `cos_why` 的投影（判定只有一份实现）',
          ps.cos_ok(REL, good, f) == (ps.cos_why(REL, good, f) == ''))

    cases = [
        ('账本里没有这一场', None),
        ('verify 不是 ok（只是**过去**的回执）', dict(good, verify='fail')),
        ('★ object_present 为假（账本说传过、本轮 HEAD 没命中）',
         dict(good, object_present=False)),
        ('★ object_present 是 None（压根没 HEAD 过）', dict(good, object_present=None)),
        ('对象键不符（账本张冠李戴）', dict(good, key='other/session.tar')),
        ('bytes 与服务器此刻不符', dict(good, bytes=f['bytes'] + 1)),
        ('files 与服务器此刻不符', dict(good, files=f['files'] + 1)),
    ]
    for name, rec in cases:
        why = ps.cos_why(REL, rec, f)
        check(f'{name} → 拒绝', not ps.cos_ok(REL, rec, f), why)
        check('   且给出**中文原因**（能直接写进日志给人看）', bool(why), why)


def test_gate_truth():
    print('\n[2d] `gate()`：三条门禁全过才产生删除行，且行里带**期望列**')
    REL = 'data/20261016/live_a_20261016_001000'
    f = _fact(REL, nbytes=1000, files=3)
    fby = {REL: f}

    lines, keep = ps.gate([REL], fby, {REL: _cosrec(f)})
    check('★ 三条全过 → 一行，形如 `rel\\t字节\\t文件数`（服务器删前据此复核 TOCTOU）',
          lines == [f'{REL}\t{f["bytes"]}\t{f["files"]}'], lines)
    check('  且 `pulled_only` 为空', keep == [], keep)

    for name, led in [
        ('COS 账本里没有这一场', {}),
        ('账本有但 object_present 为假', {REL: _cosrec(f, present=False)}),
        ('账本有但 verify 不是 ok', {REL: _cosrec(f, verify='fail')}),
        ('账本记的 bytes 与服务器此刻不符', {REL: _cosrec(f, bytes=f['bytes'] + 1)}),
        ('账本记的 files 与服务器此刻不符', {REL: _cosrec(f, files=f['files'] + 1)}),
    ]:
        l2, k2 = ps.gate([REL], fby, led)
        check(f'★ {name} → **不产生删除行**', l2 == [], l2)
        check('   且归到 `pulled_only` 并说明原因',
              len(k2) == 1 and k2[0][0] == REL and bool(k2[0][1]), k2)

    l3, k3 = ps.gate([REL], {}, {REL: _cosrec(f)})
    check('★ 服务器上已找不到这一场（被别处删了）→ 不产生删除行',
          l3 == [] and len(k3) == 1, (l3, k3))
    check('多场混合：只有全过的那些产生删除行（其余进 pulled_only）',
          ps.gate([REL], fby, {REL: _cosrec(f)})[0] == [f'{REL}\t1000\t3'])


def test_file_list_nul():
    print('\n[2e] `--files-from` 清单：NUL 分隔、**没有 CR**（Windows 文本模式会埋 `\\r`）')
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, '_staging', '_lists', 'x.txt')
        ps.write_file_list(p, ['room.json', 'danmaku.jsonl'])
        raw = open(p, 'rb').read()
        check('★ NUL 分隔（配合 rsync `--from0`）',
              raw == b'room.json\x00danmaku.jsonl\x00', raw)
        check('★ 一个 `\\r` 都没有 —— 有的话 rsync 会把它当成文件名的一部分：'
              '轻则 rc 23，重则**静默少传**', b'\r' not in raw, raw)
        check('  目录不存在时会自己建（`_staging/_lists/` 是临时目录）',
              os.path.isdir(os.path.dirname(p)))
        check('空清单写成空文件（不报错）',
              ps.write_file_list(p, []) is None and open(p, 'rb').read() == b'')


def test_cleanup_lists():
    print('\n[2f] 清单清理：只删 `_staging/_lists/` 里的临时文件，缺目录也不报错')
    with tempfile.TemporaryDirectory() as td:
        d = os.path.join(td, ps.STAGING, ps.LISTS)
        check('目录不存在时不报错（收尾动作失败绝不影响「删没删」的判定）',
              ps.cleanup_lists(d) is None)
        os.makedirs(d)
        ps.write_file_list(os.path.join(d, 'a.txt'), ['x'])
        ps.write_file_list(os.path.join(d, 'b.txt'), ['y'])
        ps.cleanup_lists(d)
        check('存在的都被清掉', os.listdir(d) == [], os.listdir(d))


def _pipeline(tmp, facts, inv, mans, specs=None, **kw):
    """跑一次 `_cycle`，返回 `(返回码, _Env)`。"""
    args = _Args()
    args.dest = tmp
    env = _Env(tmp, facts, inv, mans, specs, **kw)
    with env:
        rc = ps._cycle(args)
    return rc, env


def test_never_delete_on_failure():
    print('\n[3] ★ 校验失败 → 一条都不删（这是本工具最要紧的断言）')

    # ---- 3.1 新拉一场，但校验不过：6 种失败形态 ----
    REL = 'data/20261016/live_a_20261016_001000'
    good_spec = {'room.json': b'{"a":1}', 'danmaku.jsonl': b'hello'}
    good_man, good_src = _make(REL, good_spec)

    def _run(mangle):
        td = tempfile.mkdtemp()
        try:
            rc, env = _pipeline(td, [_fact(REL)], {}, {REL: good_man},
                               {REL: good_src}, rsync_mangle=mangle)
            return rc, env
        finally:
            shutil.rmtree(td, ignore_errors=True)

    # (a) 少一个文件
    rc, env = _run(lambda rel, st: os.remove(os.path.join(st, 'danmaku.jsonl')))
    check('本地少一个文件 → 不删服务器那份', env.delete_list == [], env.delete_list)
    check('  且记为失败（返回码非 0）', rc != ps.EXIT_OK, rc)

    # (b) 多一个残留文件，同时把另一个文件截短 —— **总字节数一模一样**，
    #     专治「只比总大小」的实现（文件数、路径集合各自都不对，但总量对得上）
    #     ⚠️ 这个残留文件必须用**已知**文件名（`series.json`）：`text_manifest()` 会把
    #     未知名字从两侧一起滤掉，用生僻名会让这一条悄悄退化成「什么都测不到」。
    def _swap(rel, st):
        open(os.path.join(st, 'series.json'), 'wb').write(b'hello')
        with open(os.path.join(st, 'room.json'), 'wb') as fh:
            fh.write(b'{"')                     # 7 → 2 字节，正好抵消多出来的 5 字节
    rc, env = _run(_swap)
    check('★ 多一个残留 + 另一个截短（**总字节数相同**）→ 不删',
          env.delete_list == [], env.delete_list)

    # (c) 同长度但内容不同（专治「只比 size」）
    def _corrupt(rel, st):
        with open(os.path.join(st, 'room.json'), 'wb') as fh:
            fh.write(b'{"a":2}')      # 与服务器同为 7 字节，内容不同
    rc, env = _run(_corrupt)
    check('★ 同长度不同内容（size 一样）→ 不删', env.delete_list == [], env.delete_list)

    # (d) 残留 .rsync-partial（半截数据的信号）
    def _partial(rel, st):
        os.makedirs(os.path.join(st, ll.PARTIAL_DIR), exist_ok=True)
        open(os.path.join(st, ll.PARTIAL_DIR, 'room.json'), 'wb').write(b'{"a":1}')
    rc, env = _run(_partial)
    check('★ 残留 .rsync-partial → 不删', env.delete_list == [], env.delete_list)

    # (e) 服务器那份在传输期间变了（僵尸 appender / 别处删了）
    rc, env = _run(lambda rel, st: open(os.path.join(st, 'danmaku.jsonl'), 'ab').write(b'!'))
    check('★ 传输期间服务器又追加了（清单取在传输之后）→ 不删',
          env.delete_list == [], env.delete_list)

    # (f) 本地代码是旧的（hash 阈值不一致）
    td = tempfile.mkdtemp()
    try:
        stale = dict(good_man)
        stale['hash_max_bytes'] = 1          # 假装本地那份是旧算法
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: stale}, {REL: good_src})
        check('★ 两侧 hash 阈值不一致（拉到旧 checkout）→ 不删',
              env.delete_list == [], env.delete_list)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # ---- 正对照：一切都对的时候，**确实会**进删除清单 ----
    td = tempfile.mkdtemp()
    try:
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: good_man}, {REL: good_src})
        check('★ 正对照：本地与服务器一致 → 进删除清单（证明上面 6 条不是因为流程压根不删）',
              env.delete_list == [REL], env.delete_list)
        check('  正对照返回 0', rc == ps.EXIT_OK, rc)
        check('  落地目录在 <dest>/20261016/…（完整口径）',
              os.path.isdir(os.path.join(td, '20261016', 'live_a_20261016_001000')),
              os.listdir(td))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_cos_gate_blocks_delete():
    print('\n[4b] ★★ 端到端：COS 门禁没过 → 服务器那份**留着**（三种合成场景）')
    REL = 'data/20261016/live_a_20261016_001000'
    spec = {'room.json': b'{"a":1}', 'danmaku.jsonl': b'hello'}
    man, src = _make(REL, spec)

    mk = {'cos_ledger': {}}
    scen = [
        ('① 文本拉到了，但 COS 账本里没有这一场', {}, mk),
        ('② 账本有，但 object_present 为假（HEAD 没命中）', {},
         {'cos_ledger': {REL: _cosrec(_fact(REL), present=False)}}),
        # ★ 这一条就是「两条路径各写一份判断」会漏掉的那个坑：
        #   上一轮以 --keep-remote 落地 → 本地只有文本 → 本轮走 to_verify。
        ('③ ★ 第二轮：本地**只有文本**、账本里没有这一场', 'local', mk),
    ]
    for name, inv_kind, kw in scen:
        td = tempfile.mkdtemp()
        try:
            inv = {}
            if inv_kind == 'local':
                local = os.path.join(td, '20261016', 'live_a_20261016_001000')
                _mkfiles(local, spec)
                inv = {REL: local}
            rc, env = _pipeline(td, [_fact(REL)], inv, {REL: man}, {REL: src}, **kw)
            check(f'★ {name} → **一条都不删**', env.delete_list == [], env.delete_list)
            check('   且日志点明卡在 COS 门禁',
                  any('COS 门禁没过' in m for m in env.logs), env.logs[-4:])
            check('   服务器那份没被碰过（`remote_delete` 压根没被调用）',
                  env.deleted_raw == [], env.deleted_raw)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    # 正对照：账本齐全时**确实会删** —— 证明上面三条不是因为流程压根不删
    td = tempfile.mkdtemp()
    try:
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src})
        check('★ 正对照：文本腿 + COS 腿都过 → 进删除清单',
              env.delete_list == [REL], env.delete_list)
        check('  删除行**带期望列**（服务器据此在删除那一刻复核 bytes/files）',
              env.delete_lines == [f'{REL}\t{_fact(REL)["bytes"]}\t{_fact(REL)["files"]}'],
              env.delete_lines)
        check('  墓碑 label 是 `已传输+COS`（与人工的 `已迁移` 分得开）',
              env.deleted_raw[0][1] == '已传输+COS', env.deleted_raw[0][1])
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_unknown_file_blocks():
    print('\n[4c] ★ 顶层未知文件 → fail-closed（不拉也不删）')
    REL = 'data/20261016/live_a_20261016_001000'
    man, src = _make(REL, {'room.json': b'{"a":1}', 'brand_new_artifact.bin': b'??'})
    td = tempfile.mkdtemp()
    try:
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src})
        check('★ 不进删除清单', env.delete_list == [], env.delete_list)
        check('  而且**压根没拉**（拉回来也只是半份，还不如让服务器留着整份）',
              env.pulled == [], env.pulled)
        check('  日志点名是哪个文件、并给出正确反应（升级 KNOWN_TOP_FILES）',
              any('未知文件' in m and 'brand_new_artifact' in m for m in env.logs),
              env.logs[-3:])
        check('  记为失败（返回码非 0，等人看一眼）', rc != ps.EXIT_OK, rc)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_flags():
    print('\n[4d] `--cos` / `--no-cos` / `--text-max-mb` / `--limit` 的语义')
    REL = 'data/20261016/live_a_20261016_001000'
    man, src = _make(REL, {'room.json': b'{"a":1}'})

    def _run(**kw):
        td = tempfile.mkdtemp()
        try:
            a = _Args()
            a.dest = td
            for k, v in kw.items():
                setattr(a, k, v)
            env = _Env(td, [_fact(REL)], {}, {REL: man}, {REL: src})
            with env:
                rc = ps._cycle(a)
            return rc, env
        finally:
            shutil.rmtree(td, ignore_errors=True)

    rc, env = _run()
    check('默认 `--cos`：本轮先让服务器归档一次',
          env.cos_runs == 1 and env.ledger_calls == 1, (env.cos_runs, env.ledger_calls))
    rc, env = _run(cos=False)
    check('★ `--no-cos`：不调 cos_archive，但**仍然读账本**做门禁'
          '（不读账本就等于把 COS 腿关掉，绝对不能删）',
          env.cos_runs == 0 and env.ledger_calls == 1, (env.cos_runs, env.ledger_calls))
    check('  且日志写明本轮不归档', any('--no-cos' in m for m in env.logs), env.logs[:3])

    rc, env = _run(dry_run=True)
    check('★★ `--dry-run` **不触发归档**（归档是写动作，与「什么都不做」的约定冲突），'
          '但仍读账本 —— 代价是它的「可删」数只是**保守下限**',
          env.cos_runs == 0 and env.ledger_calls == 1 and env.pulled == [],
          (env.cos_runs, env.ledger_calls, env.pulled))
    check('  且日志写明这一点（免得把保守下限当成真数）',
          any('保守下限' in m for m in env.logs), env.logs[:3])

    rc, env = _run(text_max_mb=0.000001)      # 12 字节 > 1e-6 MB
    check('★ 文本超 `--text-max-mb`：**只告警、照拉照删**'
          '（拦住会让这场在服务器上永远删不掉）',
          env.pulled == [REL] and env.delete_list == [REL]
          and any('text-max-mb' in m for m in env.logs),
          (env.pulled, env.delete_list, env.logs[-3:]))
    check('  告警文案说清「仍照拉」', any('仍照拉' in m for m in env.logs), env.logs[-3:])


def test_rsync_shape():
    print('\n[4e] rsync 的**形状**：只被点名的顶层文件、清单落在 `_staging/_lists/` 且每轮清')
    REL = 'data/20261016/live_a_20261016_001000'
    spec = {'room.json': b'{"a":1}', 'danmaku.jsonl': b'hello',
            'frames/f_1.jpg': b'JPEG', 'audio/s_1.wav': b'RIFF'}
    man, src = _make(REL, spec)
    td = tempfile.mkdtemp()
    try:
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src})
        check('rsync 只被调一次（一场一次）', len(env.rsync_calls) == 1, env.rsync_calls)
        rel_, names, list_path = env.rsync_calls[0]
        check('★ 只被告知**顶层**文件（`frames/` `audio/` 一个都没有 —— 它们在 COS）',
              sorted(names) == ['danmaku.jsonl', 'room.json'] and all('/' not in n for n in names),
              names)
        check('★ 清单落在 `<dest>/_staging/_lists/`',
              os.path.dirname(list_path) == os.path.join(td, ps.STAGING, ps.LISTS), list_path)
        check('★ 清单文件每轮清干净（不留痕、不越攒越多）',
              not os.path.isdir(os.path.join(td, ps.STAGING, ps.LISTS))
              or os.listdir(os.path.join(td, ps.STAGING, ps.LISTS)) == [],
              os.path.join(td, ps.STAGING, ps.LISTS))
        check('★ 正式目录里只有**文本 + 指针**，没有媒体',
              sorted(os.listdir(os.path.join(td, '20261016', 'live_a_20261016_001000')))
              == ['_local_cos_media.json', 'danmaku.jsonl', 'room.json'],
              os.listdir(os.path.join(td, '20261016', 'live_a_20261016_001000')))
        check('★ `_staging/` 下不留半截场次目录',
              not os.path.isdir(os.path.join(td, ps.STAGING, '20261016'))
              or os.listdir(os.path.join(td, ps.STAGING, '20261016')) == [],
              os.path.join(td, ps.STAGING))
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # 指针文件的内容：说明去哪取媒体，且**不含任何凭据**
    td = tempfile.mkdtemp()
    try:
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src})
        mk = os.path.join(td, '20261016', 'live_a_20261016_001000', ps.LOCAL_MARKER)
        got = json.load(open(mk, encoding='utf-8'))
        check('指针记了 COS 对象键（用户靠它去取媒体）',
              got.get('key') == ps.ca.session_key(REL), got)
        check('★ 指针**不含任何凭据**（账本里本来也没有；这里是防回归）',
              not any(k.lower() in ('secret_id', 'secret_key', 'token', 'authorization',
                                    'password', 'key_secret')
                      for k in got), sorted(got))
        check('指针里带一句「怎么取回来」的人话',
              isinstance(got.get('note'), str) and 'COS' in got['note'], got.get('note'))
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # 账本里没有这一场时，指针也要落地 —— 但**必须写明先别删服务器那份**
    td = tempfile.mkdtemp()
    try:
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src}, cos_ledger={})
        mk = os.path.join(td, '20261016', 'live_a_20261016_001000', ps.LOCAL_MARKER)
        got = json.load(open(mk, encoding='utf-8'))
        check('★ 账本没这场时指针仍在，且写明「媒体还没归档、先别删服务器那份」',
              '先别删' in got.get('note', ''), got.get('note'))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_verify_modes():
    print('\n[3b] `verify()` 逐项比对（把「集合对」与「内容对」分开）')
    spec = {'a.txt': b'aaaa', 'b.txt': b'bbbb', 'd/c.txt': b'cccc'}
    rm = _m('data/20261016/live_x_20261016_001000', spec)
    with tempfile.TemporaryDirectory() as td:
        _mkfiles(td, spec)
        lm = ll.dir_manifest(td)
        ok, probs = ps.verify(rm, lm)
        check('完全相同 → 通过', ok and not probs, probs)

    cases = [
        ('缺文件', lambda t: os.remove(os.path.join(t, 'b.txt'))),
        ('多文件', lambda t: open(os.path.join(t, 'z.txt'), 'wb').write(b'zzzz')),
        ('同长度改内容', lambda t: open(os.path.join(t, 'a.txt'), 'wb').write(b'XXXX')),
        ('少一整个子目录', lambda t: shutil.rmtree(os.path.join(t, 'd'))),
    ]
    for name, mangle in cases:
        with tempfile.TemporaryDirectory() as td:
            _mkfiles(td, spec)
            mangle(td)
            lm = ll.dir_manifest(td)
            ok, probs = ps.verify(rm, lm)
            check(f'{name} → 拒绝', not ok, probs)

    # 等总字节、但集合不同 —— 单比总量的经典反例
    with tempfile.TemporaryDirectory() as td:
        _mkfiles(td, {'a.txt': b'aaaa', 'b.txt': b'bbbb', 'c.txt': b'cccc'})
        lm = ll.dir_manifest(td)
        check(f'等总字节（{rm["total"]}）但文件集合不同 → 拒绝（所以不能只比总量）',
              not ps.verify(rm, lm)[0])


def test_already_local_failure():
    print('\n[4] ★「已在本地」那一路：不一致同样不删')
    REL = 'data/20261016/live_e_20261016_005000'
    spec = {'room.json': b'{"a":1}', 'danmaku.jsonl': b'hello'}
    man, src = _make(REL, spec)

    td = tempfile.mkdtemp()
    try:
        local = os.path.join(td, '20261016', 'live_e_20261016_005000')
        _mkfiles(local, spec)
        rc, env = _pipeline(td, [_fact(REL)], {REL: local}, {REL: man}, {REL: src})
        check('本地与服务器一致 → 删服务器那份', env.delete_list == [REL], env.delete_list)

        # 本地被改坏了（模拟「挪盘挪漏了」「磁盘出错」）
        open(os.path.join(local, 'danmaku.jsonl'), 'wb').write(b'HELLO')
        rc, env = _pipeline(td, [_fact(REL)], {REL: local}, {REL: man}, {REL: src})
        check('★ 本地那份与服务器不一致 → 不拉也不删', env.delete_list == [], env.delete_list)
        check('  且记为失败（等人看一眼）', rc != ps.EXIT_OK, rc)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_rsync_failure():
    print('\n[5] rsync 退出码非 0 → 不记账、不删除')
    REL = 'data/20261016/live_a_20261016_001000'
    man, src = _make(REL, {'room.json': b'{"a":1}'})
    td = tempfile.mkdtemp()
    try:
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src}, rsync_rc=24)
        check('rc=24（源在传输中消失）→ 不进删除清单', env.delete_list == [], env.delete_list)
        check('  返回码非 0', rc != ps.EXIT_OK, rc)
        check('  日志写出人话（不是光秃秃一个 24）',
              any('源文件在传输中消失' in m for m in env.logs), env.logs[-3:])
        check('  没留下 target 目录（失败的半截不会混进语料）',
              not os.path.exists(os.path.join(td, '20261016')), os.listdir(td))
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ================================================================ 6. 空间闸门
def test_space_gate():
    print('\n[6] 空间闸门：低于阈值 / 量不出来 → 不拉不删')
    REL = 'data/20261016/live_a_20261016_001000'
    man, src = _make(REL, {'room.json': b'{"a":1}'})

    td = tempfile.mkdtemp()
    try:
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src}, free=1.0)
        check('剩余 1 GB < 阈值 10 GB → 返回「空间不足」码', rc == ps.EXIT_NOSPACE, rc)
        check('  ★ 没删任何东西', env.delete_list == [], env.delete_list)
        check('  ★ 连传输都没开始', env.pulled == [], env.pulled)
        check('  告警里点明「本工具永不自动删本地数据」',
              any('永不自动删本地数据' in m for m in env.logs), env.logs)

        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src}, free=None)
        check('量不出剩余空间 → 返回环境错误码（不当成「充足」硬拉）',
              rc == ps.EXIT_ENV, rc)
        check('  也没删任何东西', env.delete_list == [], env.delete_list)

        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src}, free=10.0)
        check('恰好等于阈值 → 放行（断言的是 `<` 不是 `<=`）', rc == ps.EXIT_OK, rc)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print('  · `--keep-remote` 退路')
    td = tempfile.mkdtemp()
    try:
        args = _Args()
        args.dest = td
        args.keep_remote = True
        env = _Env(td, [_fact(REL)], {}, {REL: man}, {REL: src})
        with env:
            rc = ps._cycle(args)
        check('--keep-remote：仍会拉、仍会校验，但**永不下发删除**',
              env.pulled == [REL] and env.deleted == [], (env.pulled, env.deleted))
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print('  · `--dry-run`')
    td = tempfile.mkdtemp()
    try:
        args = _Args()
        args.dest = td
        args.dry_run = True
        env = _Env(td, [_fact(REL)], {}, {REL: man}, {REL: src})
        with env:
            rc = ps._cycle(args)
        check('--dry-run：什么都不做（不拉不删）',
              env.pulled == [] and env.deleted == [], (env.pulled, env.deleted))
        check('  返回 0', rc == ps.EXIT_OK, rc)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ================================================================ 7. 账本幂等
def test_manifest_roundtrip():
    print('\n[7] 账本往返 + 幂等（同一轮跑两次，第二次待拉为空）')
    saved = ps.MANIFEST
    td = tempfile.mkdtemp()
    try:
        ps.MANIFEST = os.path.join(td, 'pulled_sessions.jsonl')
        check('干净环境下账本为空', ps.load_manifest() == {})
        ps.append_manifest({'ts': 'x', 'dir': 'data/20261016/live_a_20261016_001000',
                            'dest': '/d/a', 'completeness': '完整',
                            'bytes': 10, 'files': 2, 'digest': 'abc', 'rsync_rc': 0,
                            'verify': 'ok'})
        got = ps.load_manifest()
        check('写进去能读回来，键是相对场次目录',
              'data/20261016/live_a_20261016_001000' in got, list(got))

        # 坏行不该让整个账本读不出来（边写边被计划任务读的场景）
        with open(ps.MANIFEST, 'a', encoding='utf-8') as fh:
            fh.write('{这不是 JSON\n\n')
        check('坏行被跳过、好行还在', len(ps.load_manifest()) == 1, ps.load_manifest())
    finally:
        ps.MANIFEST = saved
        shutil.rmtree(td, ignore_errors=True)

    print('  · 幂等：本地已有（含指针）→ 第二轮不再重复拉，且校验**仍然通过**')
    REL = 'data/20261016/live_a_20261016_001000'
    man, src = _make(REL, {'room.json': b'{"a":1}'})
    td = tempfile.mkdtemp()
    try:
        rc, env = _pipeline(td, [_fact(REL)], {}, {REL: man}, {REL: src})
        check('第一轮：拉下来并删服务器那份',
              env.pulled == [REL] and env.delete_list == [REL], (env.pulled, env.delete_list))
        local = os.path.join(td, '20261016', 'live_a_20261016_001000')
        check('  落地目录里有 `_local_cos_media.json`（媒体指针）',
              os.path.isfile(os.path.join(local, ps.LOCAL_MARKER)), os.listdir(local))

        # 第二轮：本地已有（服务器那份已删 → 实际上不会再出现在 facts 里；
        # 但就算它还在（--keep-remote / 没删成的场景），也必须走「校验」而不是「重拉」）
        rc, env = _pipeline(td, [_fact(REL)], {REL: local}, {REL: man}, {REL: src})
        check('第二轮：不重拉（走校验路径）', env.pulled == [], env.pulled)
        check('★★ 第二轮校验**仍然通过**（本机多出的 `_local_cos_media.json` 被对称滤掉）'
              '→ 服务器那份得以删掉',
              env.delete_list == [REL], (env.deleted_raw, env.logs[-4:]))
        check('  没有「本地多 1 个文件」这种噪声告警',
              not any('本地多' in m for m in env.logs),
              [m for m in env.logs if '本地多' in m])
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ================================================================ 8. 锁
def test_lock():
    print('\n[8] 锁：占住不放 / 心跳 / 过期自愈 / 释放（10 分钟周期一定会与长传输重叠）')
    saved_lock, saved_log = ps.LOCK, ps.LOG
    td = tempfile.mkdtemp()
    try:
        ps.LOCK = os.path.join(td, 'pull_sessions.lock')
        ps.LOG = os.path.join(td, 'pull_sessions.log')   # 别污染真的日志文件
        check('第一次拿锁成功', ps.acquire_lock(90) is True)
        check('★ 上一轮还在跑 → 第二次拿不到（不会两个周期同时搬）',
              ps.acquire_lock(90) is False)
        ps.release_lock()
        check('释放后能再拿到', ps.acquire_lock(90) is True)

        # ---- ★ 心跳：长传输不能被「开得久」判成死锁 ----
        # 服务器出口实测只有 0.32 MB/s，一场 1.3 GB 要 ≈70 分钟；若按「建于多久前」判，
        # 跑到 90 分钟就会被第二个实例接管，两边同时往同一个 staging 写同一场。
        old = 200 * 60
        _age = lambda: os.utime(ps.LOCK, (datetime.now().timestamp() - old,
                                         datetime.now().timestamp() - old))
        _age()
        check('（先确认 200 分钟没心跳确实算过期 → 接管）',
              ps.acquire_lock(180) is True)
        body = open(ps.LOCK, encoding='utf-8').read()

        _age()                                            # 又是一场拉了很久
        ps.touch_lock()                                   # 但它有心跳
        check('★ 有心跳就不算过期（第二个实例拿不到锁）',
              ps.acquire_lock(180) is False)
        check('  且心跳**只碰 mtime**，没把「谁在跑」的信息冲掉（pid 行还在）',
              open(ps.LOCK, encoding='utf-8').read().splitlines()[0] == body.splitlines()[0],
              open(ps.LOCK, encoding='utf-8').read())

        _age()
        check('★ 真的没心跳（200 分钟 > 180）→ 接管（否则一次崩溃就永久停摆）',
              ps.acquire_lock(180) is True)
        ps.release_lock()
        check('释放是真的删掉了锁文件', not os.path.exists(ps.LOCK))
        check('没锁时释放也不报错（幂等）', ps.release_lock() is None)
        check('没锁时心跳也不报错（幂等）', ps.touch_lock() is None)
    finally:
        ps.LOCK, ps.LOG = saved_lock, saved_log
        shutil.rmtree(td, ignore_errors=True)


# ================================================================ 9. 契约
def _strip_comments(src):
    """去掉注释与字符串字面量 —— 契约断言里「这一行**不在**源码里」必须过这一层，
    否则文档里提到 `shell=True` 三个字就会把断言弄红（`live_ledger` 单测同款手法）。

    令牌**不插空格**地拼回去，这样 `ll.dir_manifest(...)` 仍然是
    `ll.dir_manifest(...)`（插空格会变成 `ll . dir_manifest ( ... )`，
    点号匹配全废）。代价是 `def foo` 变成 `deffoo` —— 本文件不按那种形状断言。
    """
    import io
    import tokenize
    out = []
    # f-string 的**字面部分**也要去掉：3.12+ 把它切成 FSTRING_MIDDLE 令牌，
    # 不过滤的话 `f"… sha256 …"` 这种**提示文本**会被当成代码命中。
    str_types = {tokenize.STRING}
    for name in ('FSTRING_MIDDLE', 'FSTRING_START', 'FSTRING_END'):
        t = getattr(tokenize, name, None)
        if t is not None:
            str_types.add(t)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type in str_types:
                out.append('""')
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return src
    return ''.join(out)


def test_contract():
    print('\n[9] 契约：复用同一份判定、不过 PATH 查找、不删本地数据')
    pull_src = open(os.path.join(ROOT, 'service', 'pull_sessions.py'),
                    encoding='utf-8').read()
    led_src = open(os.path.join(ROOT, 'service', 'live_ledger.py'),
                   encoding='utf-8').read()
    pull_code = _strip_comments(pull_src)
    led_code = _strip_comments(led_src)

    # ---- 判定只有一份实现 ----
    check('★ 本地**没有**第二份「完整/不完整」判定（复用服务器给的事实）',
          'classify_session' not in pull_code,
          [l for l in pull_src.splitlines() if 'classify_session' in l])
    check('清单算法两边同源（复用 ll.dir_manifest，不自己写一套）',
          'll.dir_manifest' in pull_code and 'dir_manifest' in led_code)
    check('★ 本工具**不自己算摘要**（不 import hashlib，两侧必然同源）',
          'hashlib' not in pull_code,
          [l for l in pull_src.splitlines() if 'hashlib' in l])
    check('  摘要只**读** `dir_manifest` 给的 `digest` 字段（不自己算）',
          pull_src.count("['digest']") >= 2, pull_src.count("['digest']"))
    check('hash 阈值复用 `ll.MANIFEST_HASH_MAX`（不写死一个数）',
          'll.MANIFEST_HASH_MAX' in pull_code)
    check('残留目录名复用 `ll.PARTIAL_DIR`（与服务器侧同一个约定）',
          'll.PARTIAL_DIR' in pull_code)
    check('日期目录正则复用 `ll.DATE_DIR_RE`', 'll.DATE_DIR_RE' in pull_code)

    # ---- ssh/rsync 必须同前缀、走绝对路径 ----
    check('★ 定义了 MSYS2 前缀常量（同前缀是硬约束，混用会 dup()/safe_read 失败）',
          'MSYS_PREFIX' in pull_code and 'msys2-rsync' in pull_src)
    check('rsync 用绝对路径调（RSYNC_EXE），不靠 PATH',
          'RSYNC_EXE' in pull_code and re.search(r"RSYNC_EXE\s*=\s*MSYS_BIN", pull_code))
    check('ssh 用绝对路径调（SSH_EXE）',
          'SSH_EXE' in pull_code and re.search(r"SSH_EXE\s*=\s*MSYS_BIN", pull_code))
    check('★ 子进程走 `child_env()`（前缀只进子进程，绝不改系统 PATH）',
          'child_env' in pull_code and 'os.environ' in pull_code)
    check('★ 绝不 `shell=True`（argv 列表，路径全 ASCII 才成立）',
          'shell=True' not in pull_code)
    check('ssh 带 BatchMode=yes（交互请求要失败退出，不能挂到锁超时）',
          'BatchMode=yes' in pull_src)
    check('  `ssh()` 也自己拼了一遍 BatchMode（不走 SSH_OPTS 那条路）',
          pull_src.count('BatchMode=yes') >= 2, pull_src.count('BatchMode=yes'))

    # ---- 绝不删本地数据（用户的语料，不是缓存）----
    check('★ 本地侧没有任何 `rmtree`（本工具永不自动删本地数据）',
          'rmtree' not in pull_code)
    removes = re.findall(r'os\.remove\(([^)]*)\)', pull_code)
    check(f'★ `os.remove` 只有两处：锁文件 与 `_staging/_lists` 的临时清单（实际：{removes}）'
          f'—— 都不是数据',
          len(removes) == 2
          and any('LOCK' in r for r in removes)
          and any('lists_dir' in r for r in removes), removes)
    check('目标已存在时**拒绝覆盖**（不拿新数据盖掉旧语料）',
          '拒绝覆盖' in pull_src)
    check('staging + 原子改名（os.replace），半截目录不进正式语料',
          'os.replace' in pull_code and 'STAGING' in pull_code)

    # ---- 服务器侧的三个机器模式 ----
    for flag in ('--dump-facts', '--manifest', '--delete-list'):
        check(f'`live_ledger.py` 有 {flag}', flag in led_src)
    check('★ `--expect-count` 存在（清单条数对不上就整体拒绝）',
          '--expect-count' in led_src)
    check('★ label 默认 `已传输`（与人工的 `已迁移` 在墓碑里分得开）',
          "'已传输'" in led_src and "'已迁移'" in led_src)
    # ⚠️ 不能用 `index()` —— 文档字符串里也提了这句 import，会命中那一处。
    #    真正的代码那处是**最后一次**出现；`machine_main` 的调用点也是唯一一处。
    IMP, CALL = 'from auto_crawl import load_anchors', 'return machine_main(args)'
    check('★ 机器模式在 `from auto_crawl import load_anchors` **之前**短路'
          '（拉取/清单都不需要名单，不该被名单拖累）',
          IMP in led_src and CALL in led_src
          and led_src.index(CALL) < led_src.rindex(IMP),
          (led_src.index(CALL) if CALL in led_src else None,
           led_src.rindex(IMP) if IMP in led_src else None))
    check('★ `--delete-list` 复用 `delete_sessions`（墓碑与两道硬保护自动继承）',
          'delete_sessions' in led_code and 'append_tombstones' in led_code)

    # ---- 部署清单 ----
    man = open(os.path.join(ROOT, 'service', 'MANIFEST'), encoding='utf-8').read()
    # 「不传」的判据看的是**生效行**：注释里提到它是说明，不是清单条目
    # （pack.sh 也只认生效行 —— 两边口径必须一致，否则注释能把文件"混进"包里）。
    eff = [l.strip() for l in man.splitlines()
           if l.strip() and not l.strip().startswith('#')]
    check('★ `pull_sessions.py` **不在** `service/MANIFEST` 的生效行里'
          '（服务器永远不需要它）',
          not any('pull_sessions' in l for l in eff),
          [l for l in eff if 'pull' in l])
    check('  且在「刻意不传的」段里说明了原因（免得下次又纠结一遍）',
          'pull_sessions' in man)
    check('`service/live_ledger.py` 仍在 MANIFEST 里（服务器需要新版本）',
          'service/live_ledger.py' in man)

    # ---- 跨文件：服务器侧拒绝路径确实存在 ----
    check('★ 服务器侧的「清单与实际不符就一条都不删」有实现',
          '一条都没删' in led_src)
    check('服务器侧拒绝「采集中」的场次', '正在写入' in led_src or '采集中' in led_src)
    check('★ 服务器侧 `--delete-list` 会复核期望列（删前 TOCTOU 的唯一关口）',
          'parse_delete_entry_full' in led_code and '_dir_stat' in led_code)

    # ---- ★ 本轮的形状：只拉顶层文本、三道门禁、绝不让服务器自删 ----
    print('  · 本轮的形状（文本走公网 / 媒体走 COS / 三道门禁）')
    rs = _func_src(pull_src, 'rsync_pull')
    check('★ `rsync_pull` 用 `--files-from` 只拉清单里的文件',
          "'--files-from=' + to_msys(list_path)" in rs, rs[-400:])
    check('★ 清单经 `to_msys()`（漏了 rsync 就找不到这个文件）',
          'to_msys(list_path)' in rs)
    check('★ `--from0`（NUL 分隔，免疫 Windows 文本模式的 `\\r`）', "'--from0'" in rs)
    check('★ `--no-relative`（不嵌套由**命令**保证，不靠隐含行为）', "'--no-relative'" in rs)
    check('★★ **没有** `-r` —— 有它就会把 `audio/`/`frames/` 一起拉进来'
          '（那是 68.6 GB/天，公网 0.33 MB/s 永远追不上）',
          "'-r'" not in rs, [l for l in rs.splitlines() if "'-r'" in l])
    check('  源带尾斜杠（拷内容而不是拷目录本身）',
          f'{{host}}:{{REMOTE_ROOT}}/{{rel}}/' in rs, rs[-200:])

    wf = _func_src(pull_src, 'write_file_list')
    check("★ 清单用 `newline=''` 写（关掉 CRLF 转换）", "newline=''" in wf, wf)
    check('★ 清单以 `\\x00` 结尾（配合 `--from0`）', "'\\x00'" in wf, wf)

    cu = _func_src(pull_src, 'run_cos_archive')
    cu_code = _strip_comments(cu)        # 只看**代码**：文档里提到它是说明，不是用法
    check('★★ 发给 `cos_archive` 的**代码**里没有 `--delete-after-upload`'
          '（带了就是让服务器绕过笔记本的门禁自行删源目录）',
          '--delete-after-upload' not in cu_code, cu_code)
    check('   且用**硬断言**钉住（不是靠注释提醒）', 'assert' in cu_code, cu_code)
    check('   （断言文案里确实提到了它 —— 说明是刻意排除，不是忘了）',
          '--delete-after-upload' in cu, cu)

    check('★ 远端归档与账本都走 `COS_ARCHIVE_CMD`（带 `sudo -n`）—— '
          '`cos.env` 是 root:root 600，不给 sudo 读不到凭据，账本会全报「对象不在」',
          'COS_ARCHIVE_CMD' in pull_code and pull_src.count('COS_ARCHIVE_CMD') >= 3,
          pull_src.count('COS_ARCHIVE_CMD'))

    tm = _func_src(pull_src, 'text_manifest')
    check('★ `text_manifest` 用 `ll.manifest_digest` 重算摘要（本机**没有**第二份哈希实现）',
          'll.manifest_digest' in tm, tm[-300:])

    # ⚠️ 必须看**原文**：`_strip_comments` 把令牌**不插空格**地拼回去，`def cos_why`
    #    会变成 `defcos_why`、`return cos_why` 会变成 `returncos_why`（见它的注释）。
    check('★ 门禁只有一份实现：`cos_ok()` 是 `cos_why()` 的投影',
          'def cos_why(' in pull_src and 'return cos_why(' in pull_src)

    cyc = _func_src(pull_src, '_cycle')
    check('★★ `_cycle` 里 `remote_delete` 只有**一处**调用点 —— 门禁在 `to_pull`/`to_verify`'
          '**合并之后**算一次（两条路径各写一份 = 本轮 `to_verify` 绕过 COS 门禁那个坑）',
          cyc.count('remote_delete(') == 1, cyc.count('remote_delete('))
    check('★ 删前**重取** facts（我们的数字取自本轮开头，删除可能几十分钟后才执行）',
          cyc.count('remote_facts(') >= 2, cyc.count('remote_facts('))
    check('★ COS 账本只读一次、两条路径共用', cyc.count('remote_cos_ledger(') == 1,
          cyc.count('remote_cos_ledger('))
    check('★ `gate()` 在合并处被调用', '= gate(' in cyc, cyc.count('= gate('))

    # 「移除」看的是**代码**：文档里提一句「已删掉它」是说明，不是残留。
    check('★ `--reap-empty` 已彻底移除（它的删除不看 `--keep-remote`，语义是坏的）',
          'reap_empty' not in pull_code and 'reap-empty' not in pull_code,
          [l for l in pull_src.splitlines() if 'reap' in l])
    # ⚠️ 字符串字面量在 `pull_code` 里被抹掉了，所以这条要看原文。
    check("★ 只收「完整」是**白名单**写法（`!= '完整'`，不是排除某一类）—— "
          '将来多出一种判定会默认落在「不拉也不删」那一侧',
          "!= '完整'" in pull_src)
    check('★ 墓碑 label 用 `已传输+COS`（与人工的 `已迁移`、老的 `已传输` 都分得开）',
          '已传输+COS' in pull_src)
    check('★ 未知顶层文件是 fail-closed（有 `unknown_top_files` 且进的是 failed）',
          'unknown_top_files(' in pull_code and 'KNOWN_TOP_FILES' in pull_code)
    check('★ 交付账本记下 COS 指针（`cos_key`）与对象是否还在（`cos_present`）',
          'cos_key' in pull_src and 'cos_present' in pull_src)


# ================================================================ main
def main():
    print('=' * 64)
    print('笔记本侧拉取工具单测（全离线：合成数据 + 副作用全打桩）')
    print('=' * 64)
    test_dest_rel()
    test_plan_cycle()
    test_text_manifest()
    test_cos_gate()
    test_gate_truth()
    test_file_list_nul()
    test_cleanup_lists()
    test_never_delete_on_failure()
    test_cos_gate_blocks_delete()
    test_unknown_file_blocks()
    test_flags()
    test_rsync_shape()
    test_verify_modes()
    test_already_local_failure()
    test_rsync_failure()
    test_space_gate()
    test_manifest_roundtrip()
    test_lock()
    test_contract()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 拉取链路的保护不成立 —— 「传完自动删」会静默丢语料')
    else:
        print('✅ 校验通过才删、失败一律留服务器那份、本地数据永不被删')
    return 1 if _fail else 0


if __name__ == '__main__':
    sys.exit(main())
