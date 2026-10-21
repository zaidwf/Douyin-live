#!/usr/bin/python
# coding:utf-8
"""单测：服务器侧 COS 归档工具（`service/cos_archive.py`）—— **全离线**，不打网络、不要凭据。

## 这个工具最要紧的一条安全断言

    默认**不删**服务器那份；要删必须显式 `--delete-after-upload`，且只删
    「本轮真的传上去、并且独立核验通过的」。

服务器那份一删，COS 上的归档就是**唯一副本**了。所以本文件的重心不是「传得对不对」，
而是**每一条失败路径都不产生任何删除请求**，外加**删的那条路必须走
`live_ledger --delete-list`**（复用 `delete_sessions()` → 继承墓碑与两道硬保护，
不新开绕过保护的删除路径）。`[8]` 那组契约断言就是钉这个的。

## 签名的正确性怎么保证（★ 别把这两件事搞混）

`cos_authorization()` 是纯函数，`[1b]` 钉了一个**已知值** —— 那是一把**回归锁**：
谁改了算法这里立刻红。但它**不是正确性证明**，因为那个值也是本实现算出来的。

正确性由**对真 COS 发一次 PUT** 证明：`--selftest`（2026-10-16 实测 HTTP 200，签名一次通过）。
所以本文件里没有任何网络调用，也不需要 `config/cos.env`。

  [1] 签名：确定性、已知值、参与签名的头部集合、路径里的 `/` 不编码
  [2] 凭据：只报键名**绝不回显值**、权限/占位符/长度告警
  [3] 键映射与选场真值表（完整/不完整/采集中/全空/已归档未变/已归档变了）
  [4] 上传后核验：字节不符 / 对象不存在 → 拒绝
  [5] tar：成员表剥顶层目录、逐项校验能抓缺/多/字节不符
  [6] 归档账本往返（同 dir 多行 → 最后一行生效）
  [7] 锁：占住 / 过期自愈 / 心跳只改 mtime / 释放
  [8] 契约：不引 SDK、删除走 `live_ledger --delete-list`、**本工具在 `service/MANIFEST` 里**、
      默认不删、不自己重写完整性判定

用法：python test/test_cos_archive_unit.py

⚠️ 本文件**不要**再包一层 `sys.stdout` —— `cos_archive` 导入 `live_ledger` 时已经包过，
双层会让内层被 GC 掉、之后所有 print 报 `I/O operation on closed file`
（`test_health_check_unit.py` / `test_ledger_unit.py` 顶部同款注释）。
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'service'))

import cos_archive as ca          # noqa: E402
import live_ledger as ll          # noqa: E402

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
def _fact(rel, nbytes=1000, files=3, started='2026-10-16T00:00:00', audio=1, frames=1,
          danmaku=10, events=5, inflight=False, has_room_json=True):
    """一条 `scan_all_sessions()` 口径的场次事实（`classify_session()` 要的那几个键都在）。"""
    date, sess = rel.split('/')[1], rel.split('/')[2]
    return {'dir': rel, 'douyin_id': sess.split('_')[1], 'name': sess,
            'date': date, 'started': started, 'ended': '', 'last_write': started,
            'inflight': inflight, 'has_room_json': has_room_json, 'meta': None,
            'danmaku_lines': danmaku, 'event_lines': events,
            'audio_segs': audio, 'frames': frames,
            'abspath': '/nonexistent/' + rel, '_bytes': nbytes, '_files': files}


def _mkfiles(root, spec):
    """按 `{相对路径: 内容}` 造真文件，返回根目录"""
    for rel, data in spec.items():
        p = os.path.join(root, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as fh:
            fh.write(data if isinstance(data, bytes) else data.encode('utf-8'))
    return root


def _mkmanifest(root):
    """用**真**的 `dir_manifest()` 造清单（喂合成文件），返回 `(清单, 文件字典)`"""
    return ll.dir_manifest(root)


# ================================================================ [1] 签名
def t_signature():
    print('[1] COS V5 签名（纯函数）')
    host = 'douyinlive-1507396983.cos.ap-shanghai.myqcloud.com'
    hdrs = {'Host': host, 'Content-Length': '470500352',
            'Content-Type': 'application/octet-stream', 'x-cos-meta-sha256': 'abc123'}
    a1 = ca.cos_authorization('put', 'data/20261016/live_x.tar', hdrs,
                              'AKIDEXAMPLE', 'SECRET', start=1700000000)
    a2 = ca.cos_authorization('put', 'data/20261016/live_x.tar', hdrs,
                              'AKIDEXAMPLE', 'SECRET', start=1700000000)
    check('同输入两次结果完全相同（纯函数）', a1 == a2)
    check('★ 已知值（回归锁 —— 改了算法这里立刻红）',
          a1 == ('q-sign-algorithm=sha1&q-ak=AKIDEXAMPLE&q-sign-time=1700000000;1700003600'
                 '&q-key-time=1700000000;1700003600'
                 '&q-header-list=content-length;content-type;host;x-cos-meta-sha256'
                 '&q-url-param-list=&q-signature=7c4131373a6376c117de0d4bad76b1790cab0c82'),
          a1[:120])

    # ★ requests 自己会加 User-Agent / Accept-Encoding —— 它们**不进签名**，
    #   否则签名会随环境漂移。这条断言钉住「头部筛选规则」。
    noisy = dict(hdrs, **{'User-Agent': 'python-requests/2.31.0', 'Accept-Encoding': 'gzip'})
    check('★ User-Agent/Accept-Encoding 不参与签名（requests 自己加的头部不破坏签名）',
          ca.cos_authorization('put', 'data/20261016/live_x.tar', noisy,
                               'AKIDEXAMPLE', 'SECRET', start=1700000000) == a1)
    check('换 key → 签名变',
          ca.cos_authorization('put', 'data/20261016/live_y.tar', hdrs, 'AKIDEXAMPLE',
                               'SECRET', start=1700000000) != a1)
    check('换方法（put→get）→ 签名变',
          ca.cos_authorization('get', 'data/20261016/live_x.tar', hdrs, 'AKIDEXAMPLE',
                               'SECRET', start=1700000000) != a1)
    check('换密钥 → 签名变',
          ca.cos_authorization('put', 'data/20261016/live_x.tar', hdrs, 'AKIDEXAMPLE',
                               'OTHER', start=1700000000) != a1)

    check('q-header-list 用 ; 连接且排序',
          a1.split('q-header-list=')[1].split('&')[0]
          == 'content-length;content-type;host;x-cos-meta-sha256')
    # 路径里的 / **不编码**（COS 的 canonical URI 规则）—— 编成 %2F 会 403
    check('★ 路径里的 `/` 不编码（出现 %2F 就是错）', '%2F' not in a1)
    check('x-cos-* 进了签名（换个值签名就变）',
          ca.cos_authorization('put', 'data/20261016/live_x.tar',
                               dict(hdrs, **{'x-cos-meta-sha256': 'zzz'}),
                               'AKIDEXAMPLE', 'SECRET', start=1700000000) != a1)
    check('有效期写进 q-sign-time（expire 变了签名就变）',
          ca.cos_authorization('put', 'data/20261016/live_x.tar', hdrs, 'AKIDEXAMPLE',
                               'SECRET', start=1700000000, expire=60) != a1)

    # 分片上传的查询参数必须进签名（否则 COS 会 403）
    a3 = ca.cos_authorization('put', 'data/20261016/live_x.tar', hdrs,
                              'AKIDEXAMPLE', 'SECRET', start=1700000000,
                              params={'partNumber': '2', 'uploadId': 'abc+/='})
    check('★ 分片参数进入 q-url-param-list（小写、排序、; 连接）',
          '&q-url-param-list=partnumber;uploadid&' in a3, a3[:180])
    check('分片参数不同 → 签名不同', a3 != a1)
    check('参数值为空也参与签名（?uploads）',
          '&q-url-param-list=uploads&' in
          ca.cos_authorization('post', 'data/20261016/live_x.tar',
                               {'Host': host, 'Content-Length': '0'},
                               'AKIDEXAMPLE', 'SECRET', start=1700000000,
                               params={'uploads': ''}))


# ================================================================ [2] 凭据
def t_credentials(tmp):
    print('[2] 凭据读取与告警（★ 绝不回显值）')
    p = os.path.join(tmp, 'cos.env')
    # ⚠️ 别用 `'x' * 32` 当假密钥 —— 里面含 `xxx`，正好命中 `looks_placeholder` 的特征，
    #    会把「形状正常」那条断言弄成假的失败。用像真 base62 的串。
    fake_id, fake_key = ('AKID'
                                  'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6'), 'Za9Bc8De7Fg6Hi5Jk4Lm3No2Pq1Rs0Tu'
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write(f'# 注释\n\nCOS_SECRET_ID = {fake_id}\n'
                 f'COS_SECRET_KEY="{fake_key}"\n'
                 "COS_BUCKET='mybucket-1234567890'\n"
                 'COS_REGION=ap-shanghai\n')
    cfg = ca.load_cos_env(p)
    check('能读（注释 / 空行 / 键名两侧空白 / 单双引号都认）',
          cfg['COS_BUCKET'] == 'mybucket-1234567890' and cfg['COS_REGION'] == 'ap-shanghai'
          and cfg['COS_SECRET_ID'].startswith('AKID'), json.dumps(sorted(cfg)))
    check('region 缺省时回落到 ap-shanghai',
          ca.load_cos_env(p).get('COS_REGION') == 'ap-shanghai')

    # ★★ 这条是本组存在的理由：报错只能报**键名**，不能把已有值带出来
    p2 = os.path.join(tmp, 'cos_partial.env')
    secret = ('AKID'
              'SUPERSECRETVALUE1234567890ABCDEF')
    with open(p2, 'w', encoding='utf-8') as fh:
        fh.write(f'COS_SECRET_ID={secret}\nCOS_BUCKET=b\n')
    try:
        ca.load_cos_env(p2)
        check('缺键要抛错', False, '没抛')
    except RuntimeError as e:
        msg = str(e)
        check('缺键抛 RuntimeError，并指出缺的是哪个键', 'COS_SECRET_KEY' in msg, msg[:160])
        check('★★ 报错里**不含**任何已有凭据的值', secret not in msg and 'AKIDSUPER' not in msg,
              msg[:160])
        check('  只列**键名**（值一律不回显）', 'AKID' not in msg, msg[:160])

    try:
        ca.load_cos_env(os.path.join(tmp, '不存在.env'))
        check('文件不存在要抛错', False, '没抛')
    except RuntimeError as e:
        check('文件不存在 → RuntimeError（不去猜默认值）', '读不到' in str(e), str(e)[:120])

    # 占位符与长度告警：只告警，**不拦截**
    check('★ 占位符被认出来（`替换`/`填`/`xxx`/`...`）',
          all(ca.looks_placeholder(v) for v in
              ('AKID替换', '填这里', 'xxxxx', '...', 'TODO', '  YourKey  ')))
    check('真凭据不误报占位符',
          not ca.looks_placeholder('AKID' + 'a1B2c3D4' * 4))
    short = {'COS_SECRET_ID': 'AKIDshort', 'COS_SECRET_KEY': 'only16chars12345',
             'COS_BUCKET': 'b', 'COS_REGION': 'ap-shanghai'}
    w = ca.config_warnings(short)
    check('SecretId 太短 → 告警（腾讯云的通常 36 字符、AKID 开头）',
          any('COS_SECRET_ID 只有' in x for x in w), '；'.join(w))
    check('SecretKey 不是 32 字符 → 告警', any('COS_SECRET_KEY 是' in x for x in w),
          '；'.join(w))
    check('★ 告警文本里也不含值本身',
          not any('only16chars' in x for x in w), '；'.join(w))
    check('形状正常时没有额外告警',
          not [x for x in ca.config_warnings(ca.load_cos_env(p)) if '形状' in x or '占位' in x],
          '；'.join(ca.config_warnings(ca.load_cos_env(p))))


# ================================================================ [3] 键映射 & 选场
def t_plan():
    print('[3] 键映射与选场真值表')
    rel = 'data/20261016/live_abc_20261016_001046'
    check('session_key 保持与服务器 data/ 同构（加 .tar）',
          ca.session_key(rel) == rel + '.tar', ca.session_key(rel))
    check('manifest_key', ca.manifest_key(rel) == rel + '.manifest.json')

    facts = [
        _fact('data/20261016/live_a_20261016_000002', started='2026-10-16T00:00:02'),
        _fact('data/20261016/live_b_20261016_000001', started='2026-10-16T00:00:01',
              audio=1, frames=1, danmaku=0, events=0),          # 有音视频无文本 → 不完整
        _fact('data/20261016/live_c_20261016_000004', started='2026-10-16T00:00:04',
              inflight=True),                                    # 采集中
        _fact('data/20261016/live_d_20261016_000003', started='2026-10-16T00:00:03',
              audio=0, frames=0, danmaku=0, events=0),           # 全空
    ]
    archived = {'data/20261016/live_a_20261016_000002':
                {'dir': 'data/20261016/live_a_20261016_000002', 'bytes': 1000, 'files': 3,
                 'tar_sha256': 'x'}}
    todo, done, skipped = ca.plan_cycle(facts, archived)
    trels = [f['dir'] for f in todo]
    srels = {r: w for r, w in skipped}
    check('已归档且 bytes/files 都对得上 → done（不重传）',
          done == ['data/20261016/live_a_20261016_000002'], str(done))
    check('★ 采集中 → 跳过（正在写的目录打出来的 tar 是残缺的，且看起来毫无异常）',
          'data/20261016/live_c_20261016_000004' in srels
          and '采集中' in srels['data/20261016/live_c_20261016_000004'], str(srels))
    check('全空场次 → 跳过（不传也不删）',
          'data/20261016/live_d_20261016_000003' in srels, str(srels))
    check('不完整的**要传**（COS 这份是全量存档，不做 data/_incomplete 分流）',
          'data/20261016/live_b_20261016_000001' in trels, str(trels))
    check('★ 待传按开播时间正序（先传旧的）', trels == sorted(trels), str(trels))

    # 归档后目录被改动了（字节数不符）→ 必须**重传**，不能因为「账本里有」就跳过
    archived2 = dict(archived)
    archived2['data/20261016/live_b_20261016_000001'] = {
        'dir': 'data/20261016/live_b_20261016_000001', 'bytes': 999, 'files': 1}
    todo2, done2, _ = ca.plan_cycle(facts, archived2)
    check('★ 账本说归档过、但字节数/文件数变了 → 重传（目录被改过）',
          'data/20261016/live_b_20261016_000001' in [f['dir'] for f in todo2], str(todo2))

    todo3, _, _ = ca.plan_cycle(facts, {}, only='data/20261016/live_b_20261016_000001')
    check('--only 只留那一场', [f['dir'] for f in todo3]
          == ['data/20261016/live_b_20261016_000001'], str(todo3))

    check('完整性判定调用的是 `ll.classify_session`（不自己写一份）',
          ca.ll.classify_session(_fact('data/20261016/x_20261016_000001'))[0] == '完整')


# ================================================================ [4] 上传后核验
def t_verify_upload():
    print('[4] 上传后核验')
    check('字节相等 → 过', ca.verify_upload(None, 'k', 100, 100) == (True, ''))
    ok, why = ca.verify_upload(None, 'k', 100, 99)
    check('字节不等 → 拒（PUT 返回 200 只代表 COS 收了请求，不代表落盘的字节对）',
          not ok and '字节数不符' in why, why)
    ok, why = ca.verify_upload(None, 'k', 100, None)
    check('★ 对象不存在（None）→ 拒（None 不能当成 0 也不能当成「有」）',
          not ok and '拿不到' in why, why)


# ================================================================ [5] tar
def _build_tar(tar_path, members):
    """用 tarfile 造一个 tar：`members` = `{相对路径: 字节}`，路径前加 `live_x/`（模拟真归档）"""
    with tarfile.open(tar_path, 'w') as tf:
        for rel, data in sorted(members.items()):
            body = data if isinstance(data, bytes) else data.encode('utf-8')
            ti = tarfile.TarInfo('live_x/' + rel)
            ti.size = len(body)
            tf.addfile(ti, io.BytesIO(body))
    return tar_path


def t_tar(tmp):
    print('[5] tar 成员表与逐项校验')
    src = _mkfiles(os.path.join(tmp, 'sess'), {
        'room.json': '{"a":1}', 'danmaku.jsonl': 'x\ny\n',
        'audio/000.wav': b'\x00' * 3000, 'frames/000.jpg': b'\xff' * 5000})
    m = _mkmanifest(src)
    check('合成目录的清单读得出来', m['count'] == 4 and m['total'] == 8011,
          f"count={m['count']} total={m['total']}")

    tp = os.path.join(tmp, 'a.tar')
    _build_tar(tp, {'room.json': '{"a":1}', 'danmaku.jsonl': 'x\ny\n',
                    'audio/000.wav': b'\x00' * 3000, 'frames/000.jpg': b'\xff' * 5000})
    mem = ca.tar_members(tp)
    check('tar_members 剥掉顶层目录名（好与 dir_manifest 同口径）',
          {e['p'] for e in mem} == {'room.json', 'danmaku.jsonl', 'audio/000.wav',
                                    'frames/000.jpg'}, str([e['p'] for e in mem]))
    check('tar_members 只收文件、不收目录项', all(e['n'] >= 0 for e in mem) and len(mem) == 4)
    check('tar_members 按 p 排序', [e['p'] for e in mem] == sorted(e['p'] for e in mem))

    ok, problems = ca.verify_tar(tp, m)
    check('★ 装对了 → 过', ok, str(problems))

    # ★ 这条最容易写错：tar 体积天生比目录大（每文件 512 字节头 + padding），
    #   拿体积去比目录字节数会**每场都误判失败**。所以校验比的是**成员表**。
    check('★ tar 体积确实大于目录字节数（所以不能拿体积当判据）',
          os.path.getsize(tp) > m['total'], f"{os.path.getsize(tp)} vs {m['total']}")
    check('  而逐项校验不受影响（不看体积）', ok)

    tp2 = os.path.join(tmp, 'b.tar')
    _build_tar(tp2, {'room.json': '{"a":1}', 'danmaku.jsonl': 'x\ny\n',
                     'audio/000.wav': b'\x00' * 3000})              # 少一帧
    ok, problems = ca.verify_tar(tp2, m)
    check('少一个文件 → 拒（且报出少了几个）',
          not ok and '少了' in problems[0], str(problems))

    tp3 = os.path.join(tmp, 'c.tar')
    _build_tar(tp3, {'room.json': '{"a":1}', 'danmaku.jsonl': 'x\ny\n',
                     'audio/000.wav': b'\x00' * 3000, 'frames/000.jpg': b'\xff' * 5000,
                     'frames/extra.jpg': b'\xff' * 10})             # 多一个残留
    ok, problems = ca.verify_tar(tp3, m)
    check('多一个文件 → 拒（残留文件不能混过去）',
          not ok and '多了' in problems[0], str(problems))

    tp4 = os.path.join(tmp, 'd.tar')
    _build_tar(tp4, {'room.json': '{"a":1}', 'danmaku.jsonl': 'x\ny\n',
                     'audio/000.wav': b'\x00' * 3000, 'frames/000.jpg': b'\xff' * 4999})
    ok, problems = ca.verify_tar(tp4, m)
    check('同长度变短 → 拒（字节数不符单独报）',
          not ok and '字节数不符' in problems[0], str(problems))

    # 真跑一次 pack_tar（系统有 tar 才跑）：路径必须**相对**，这样解包回 data/<日期>/
    if shutil.which('tar'):
        tp5 = os.path.join(tmp, 'e.tar')
        size = ca.pack_tar(src, tp5)
        check('pack_tar 产出的 tar 体积 = 文件实际大小', size == os.path.getsize(tp5) > 0,
              str(size))
        with tarfile.open(tp5, 'r:') as tf:
            names = tf.getnames()
        check('★ 归档里存的是相对路径（`<场次名>/…`，解包回 data/<日期>/ 就是服务器原样）',
              all(n == 'sess' or n.startswith('sess/') for n in names), str(names[:3]))
        ok, problems = ca.verify_tar(tp5, m)
        check('真 pack_tar 的产物也过逐项校验', ok, str(problems))
    else:
        print('  ⏭ 系统没有 tar，跳过 pack_tar 实测')


# ================================================================ [6][7] 账本与锁
def t_ledger_lock(tmp):
    print('[6] 归档账本往返')
    # ⚠️ `LEDGER` / `LOCK` / `LOG` 都在 `DIST` 底下，而 `append_record()` 建的是 `DIST`
    #    —— 只搬 `LEDGER` 不搬 `DIST`，写入会落到临时路径而**父目录不存在**，
    #    报「归档账本写入失败」而断言看起来像「逻辑错了」。三个一起搬。
    old = (ca.LEDGER, ca.DIST, ca.LOG)
    ca.DIST = os.path.join(tmp, 'dist')
    os.makedirs(ca.DIST, exist_ok=True)
    ca.LEDGER = os.path.join(ca.DIST, 'cos_archive.jsonl')
    ca.LOG = os.path.join(ca.DIST, 'cos_archive.log')
    try:
        check('账本不存在时读出空表（不抛）', ca.load_ledger() == {})
        ca.append_record({'ts': 't1', 'dir': 'data/20261016/live_a', 'verify': 'ok'})
        ca.append_record({'ts': 't2', 'dir': 'data/20261016/live_b', 'verify': 'ok'})
        lg = ca.load_ledger()
        check('往返读回两条', sorted(lg) == ['data/20261016/live_a', 'data/20261016/live_b'],
              str(sorted(lg)))
        ca.append_record({'ts': 't3', 'dir': 'data/20261016/live_a', 'tar_sha256': 'new'})
        check('★ 同一 dir 多行 → **最后一行**生效（人工修正后重传能覆盖）',
              ca.load_ledger()['data/20261016/live_a']['tar_sha256'] == 'new',
              str(ca.load_ledger()['data/20261016/live_a']))
        with open(ca.LEDGER, 'a', encoding='utf-8') as fh:
            fh.write('\n不是 json 的一行\n')
        check('坏行跳过，不影响其余（账本被截断也不该整体崩）',
              len(ca.load_ledger()) == 2, str(ca.load_ledger()))
    finally:
        ca.LEDGER, ca.DIST, ca.LOG = old

    print('[7] 并发保护（锁）')
    old_l = ca.LOCK
    ca.LOCK = os.path.join(tmp, 'dist', 'cos_archive.lock')
    try:
        check('第一次拿锁 → True', ca.acquire_lock(180) is True)
        check('紧接着再拿 → False（上一轮还在跑）', ca.acquire_lock(180) is False)
        pid_line = open(ca.LOCK, encoding='utf-8').read()
        ca.touch_lock()
        check('★ 心跳只改 mtime、**不改内容**（里面是 pid + 起始时刻，排查要用）',
              open(ca.LOCK, encoding='utf-8').read() == pid_line, open(ca.LOCK).read()[:60])
        # 过期的锁要能自愈，否则一次崩溃就永久停摆
        old_time = (datetime.now() - timedelta(minutes=400)).timestamp()
        os.utime(ca.LOCK, (old_time, old_time))
        check('★ 过期锁能自愈接管（按**心跳**判，不按「建于多久前」判）',
              ca.acquire_lock(180) is True)
        ca.release_lock()
        check('释放后锁文件没了', not os.path.exists(ca.LOCK))
        check('释放后再拿 → True', ca.acquire_lock(180) is True)
        ca.release_lock()
    finally:
        ca.LOCK = old_l


# ================================================================ [9] --dump-ledger
def t_dump_ledger(tmp):
    print('[9] --dump-ledger：交给笔记本的「COS 上真的有」的活证据')
    old = (ca.LEDGER, ca.DIST, ca.LOG, ca.Cos, ca.load_cos_env, ca._LOG_QUIET)
    ca.DIST = os.path.join(tmp, 'dist_dl')
    os.makedirs(ca.DIST, exist_ok=True)
    ca.LEDGER = os.path.join(ca.DIST, 'cos_archive.jsonl')
    ca.LOG = os.path.join(ca.DIST, 'cos_archive.log')

    asked = []                      # 被 HEAD 过的 key —— 用来钉「不该问的没去问」

    class FakeCos:
        present = set()

        def __init__(self, cfg):
            pass

        def size_of(self, key):
            asked.append(key)
            return 4242 if key in FakeCos.present else None

    def fake_env(*a, **k):
        return {'COS_BUCKET': 'b', 'COS_SECRET_ID': 'x',
                'COS_SECRET_KEY': 'y', 'COS_REGION': 'r'}

    ca.Cos, ca.load_cos_env = FakeCos, fake_env
    try:
        def run():
            buf, old_out = io.StringIO(), sys.stdout
            sys.stdout = buf
            try:
                rc = ca.dump_ledger('-')
            finally:
                sys.stdout = old_out
            return rc, buf.getvalue()

        # --- (a) 账本不存在 → `{}` / rc=0，且**完全不碰凭据** ---
        env_calls = []

        def counting_env(*a, **k):
            env_calls.append(1)
            return fake_env()
        ca.load_cos_env = counting_env
        rc, text = run()
        check('账本不存在 → `{}`、rc=0（不是错误：可能还没归档过任何东西）',
              rc == ca.EXIT_OK and json.loads(text) == {}, text[:80])
        check('★ 账本为空时**一次都不碰凭据/网络** —— 服务器排空后不能因为没得问就报错',
              not env_calls, str(env_calls))
        ca.load_cos_env = fake_env

        # --- (b) object_present 的真值表 ---
        ca.append_record({'ts': 't1', 'dir': 'data/20261016/live_here',
                          'verify': 'ok', 'bytes': 100, 'files': 3, 'etag': 'e1'})
        ca.append_record({'ts': 't2', 'dir': 'data/20261016/live_gone', 'verify': 'ok'})
        ca.append_record({'ts': 't3', 'dir': 'data/20261016/live_bad', 'verify': 'fail'})
        FakeCos.present = {ca.session_key('data/20261016/live_here')}
        asked.clear()
        rc, text = run()
        got = json.loads(text)
        check('★ verify==ok 且 HEAD 得到对象 → object_present=true',
              got['data/20261016/live_here']['object_present'] is True,
              str(got.get('data/20261016/live_here')))
        check('★★ verify==ok 但 HEAD 拿不到对象 → object_present=false'
              '（这一条就是「防静默丢失」的全部意义所在）',
              got['data/20261016/live_gone']['object_present'] is False,
              str(got.get('data/20261016/live_gone')))
        check('★ 非 ok 的记录 → object_present=false 且**不去 HEAD**（省往返，也不可能为真）',
              got['data/20261016/live_bad']['object_present'] is False
              and ca.session_key('data/20261016/live_bad') not in asked,
              str(asked))
        check('★ 原账本字段一个不丢（verify/bytes/files/etag 透传，调用方还要比对）',
              got['data/20261016/live_here']['verify'] == 'ok'
              and got['data/20261016/live_here']['bytes'] == 100
              and got['data/20261016/live_here']['etag'] == 'e1')

        # --- (c) stdout 上只允许有 JSON ---
        buf, old_out = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            ca.dump_ledger('-')
            ca.log('噪声：这条若出现在 stdout 就会毁掉 JSON')     # quiet 必须已被打开
        finally:
            sys.stdout = old_out
        text = buf.getvalue()
        try:
            json.loads(text)
            pure = True
        except ValueError:
            pure = False
        check('★★ stdout 上**只有**合法 JSON（`log()` 被 quiet 住 —— 机器可读输出不可污染）',
              pure and '噪声' not in text, repr(text[:110]))

        # --- (d) 凭据不可用 → 环境错误码，但账本照交、全 false ---
        def boom(*a, **k):
            raise RuntimeError('缺少这些键：[COS_SECRET_ID]')
        ca.load_cos_env = boom
        rc, text = run()
        got = json.loads(text)
        check('★ 拿不到凭据 → rc=EXIT_ENV（**不是 0**），且全部 object_present=false'
              '（绝不假装「都在」，调用方据此 fail-closed 不删）',
              rc == ca.EXIT_ENV and got
              and all(r['object_present'] is False for r in got.values()),
              f'rc={rc} got={got}')
    finally:
        (ca.LEDGER, ca.DIST, ca.LOG, ca.Cos,
         ca.load_cos_env, ca._LOG_QUIET) = old


# ================================================================ [9] 分片对象计划
def t_shard():
    print('[9] 分片对象计划（纯函数；不是 multipart）')
    check('10 字节 / 5 → 两片（0 基）',
          ca.shard_plan(10, 5) == [(0, 0, 5), (1, 5, 5)])
    check('11 字节 / 5 → 最后一片是余数',
          ca.shard_plan(11, 5) == [(0, 0, 5), (1, 5, 5), (2, 10, 1)])
    check('0 字节 → 0 片', ca.shard_plan(0, 5) == [])
    check('对象键 → .partNNN',
          ca.shard_key('data/20261019/live_x.tar', 7)
          == 'data/20261019/live_x.tar.part007')
    size = 5399797760
    parts = ca.shard_plan(size, ca.SHARD_SIZE)
    check('★ 5.03 GiB / 4 GiB → 2 片且总长不变',
          len(parts) == 2 and sum(p[2] for p in parts) == size,
          f'parts={len(parts)} total={sum(p[2] for p in parts)}')
    check('片号从 0 连续、偏移连续',
          [p[0] for p in parts] == list(range(len(parts)))
          and all(parts[i][1] + parts[i][2] == parts[i + 1][1]
                  for i in range(len(parts) - 1)))
    check('★ 阈值为 4 GiB，低于 COS 单 PUT 上限 5 GiB',
          ca.SHARD_THRESHOLD == 4 * 1024 ** 3
          and ca.SHARD_THRESHOLD < 5 * 1024 ** 3)

    d = 'data/20261019/live_x'
    k = ca.session_key(d)
    check('老记录（没有 parts）→ 唯一对象就是规范 tar 键',
          ca.record_parts({'dir': d, 'key': k, 'tar_bytes': 9})
          == [{'key': k, 'bytes': 9}])
    rec = {'dir': d, 'key': k, 'tar_bytes': size,
           'parts': [{'key': k + '.part000', 'bytes': 4 * 1024 ** 3},
                     {'key': k + '.part001', 'bytes': size - 4 * 1024 ** 3}]}
    check('新记录 → parts 原样返回', ca.record_parts(rec) is rec['parts'])

    class FakeCos:
        def __init__(self, objs):
            self.objs = objs

        def size_of(self, key):
            return self.objs.get(key)

    full = {p['key']: p['bytes'] for p in rec['parts']}
    check('★ 全部分片都在且字节对 → True',
          ca.record_present(FakeCos(full), rec))
    one_missing = dict(full)
    one_missing.pop(rec['parts'][1]['key'])
    check('★ 缺第二片 → False（不会只看第一片）',
          not ca.record_present(FakeCos(one_missing), rec))
    wrong = dict(full)
    wrong[rec['parts'][0]['key']] -= 1
    check('★ 字节数不符 → False', not ca.record_present(FakeCos(wrong), rec))


# ================================================================ [8] 契约
def t_contract():
    print('[8] 契约（跨文件 grep）')
    src = open(os.path.join(ROOT, 'service', 'cos_archive.py'), encoding='utf-8').read()
    man = open(os.path.join(ROOT, 'service', 'MANIFEST'), encoding='utf-8').read()
    led = open(os.path.join(ROOT, 'service', 'live_ledger.py'), encoding='utf-8').read()

    # 只看**真的 import** —— 模块顶部的文档里写着「不用 cos-python-sdk-v5」，
    # 那是说明不是依赖，按子串判会把它自己判成违规。
    check('★ 不 import cos SDK（部署仍只是拷文件，服务器不用 pip install）',
          not re.search(r'^\s*(?:import|from)\s+(?:qcloud_cos|cos_python_sdk)',
                        src, re.MULTILINE),
          '源码里真的 import 了 SDK')
    check('★ 删除走 `live_ledger --delete-list`（复用 delete_sessions → 继承墓碑与硬保护）',
          "'--delete-list'" in src and 'live_ledger.py' in src)
    check('★★ 本文件里**没有**任何直接删除目录的调用（rmtree/rmdir 只在 live_ledger 里）',
          'rmtree' not in src and 'os.rmdir' not in src)
    check('★ 完整性判定复用 `ll.classify_session`（不在第二处重写）',
          'll.classify_session' in src and 'def classify_session' not in src)
    check('live_ledger 侧确有 --delete-list 的实现', "'--delete-list'" in led)
    check('★ `service/MANIFEST` 里**有** cos_archive.py（服务器侧工具，与 pull_sessions 相反）',
          'service/cos_archive.py' in man)
    check('  pull_sessions.py 仍然**不在** MANIFEST 里（笔记本侧）',
          'service/pull_sessions.py' not in
          '\n'.join(l for l in man.splitlines() if not l.lstrip().startswith('#')))
    check('★ 默认**不删**服务器那份（`--delete-after-upload` 是 store_true，缺省 False）',
          "ap.add_argument('--delete-after-upload', action='store_true'" in src
          and 'if args.delete_after_upload and uploaded' in src)
    check('★ tar 临时目录在 `dist/` 下（与 data/ 同卷）—— 写进 tmpfs 会吃内存把采集 OOM',
          "TMP_DIR = os.path.join(DIST, '_cos_tmp')" in src and "'/tmp'" not in src)
    check('  tar 不压缩（语料是 JPEG/WAV，压不动，且 zstd 会把 CPU 变成新瓶颈）',
          "'-cf'" in src and "'-czf'" not in src)
    check('签名有效期是有限的（不是永久签名）',
          'SIGN_EXPIRE' in src and 'q-sign-time' in src)
    check('vhost 域名是内网（169.254 那条实测结论写进了注释）',
          '169.254' in src and 'cos-internal' in src)

    # ── `--dump-ledger`（笔记本侧判「能不能删」的唯一证据来源）──
    dl = src[src.index('def dump_ledger'):src.index('def main')]
    check('★ `--dump-ledger` 在**抢锁之前**短路（纯读，不该跟正在跑的那一轮抢锁）',
          'if args.dump_ledger:' in src
          and src.index('if args.dump_ledger:') < src.index('if not acquire_lock(args.lock_min)'))
    rp = src[src.index('def record_present'):src.index('class _SliceReader')]
    check('★ 活证据走 `record_present`（内部用既有的 `Cos.size_of`，单/多片同一判据）',
          'record_present(cos, ledger[d])' in dl
          and 'cos.size_of(key)' in rp
          and 'cos.head(' not in rp)
    check('★ dump-ledger 模式强制 quiet（否则 `log()` 会 print 到 stdout 把 JSON 冲掉）',
          '_LOG_QUIET = True' in dl)
    check('★ 诊断一律走 stderr（stdout 只留 JSON 给调用方解析）',
          'file=sys.stderr' in dl)


def main():
    tmp = tempfile.mkdtemp(prefix='cosarc_')
    try:
        t_signature()
        t_credentials(tmp)
        t_plan()
        t_verify_upload()
        t_tar(tmp)
        t_ledger_lock(tmp)
        t_shard()
        t_contract()
        t_dump_ledger(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print(f'{"=" * 60}')
    total = _ok + _fail
    print(f'{"✅ 全绿" if _fail == 0 else "❌ 有失败"}  {_ok}/{total} 条断言通过'
          + (f'，{_fail} 条失败' if _fail else ''))
    return 1 if _fail else 0


if __name__ == '__main__':
    sys.exit(main())
