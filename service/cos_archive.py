#!/usr/bin/python
# coding:utf-8
"""把服务器上采完的场次**打包上传到腾讯云 COS** —— 走内网，免费且不限速。

## 它解决什么

服务器出口被轻量实例的带宽包硬卡在 **≈0.33 MB/s（2.76 Mbps）**，而全模态流入实测
**≈68.6 GB/天**（要排完得 59.5 小时/天）—— 「笔记本公网 rsync 拉」这条路**永远追不上**，
服务器会反复撞 `--min-free-gb 5` 然后降级成纯文本。

2026-10-16 实测发现**内网出口根本没被卡**（同一个 bucket、同一个进程、同一时刻）：

    单个 200 MB 对象            58.68 MB/s      ← 公网那条路的 178 倍
    100 × 1 MB                   9.12 MB/s
    400 × 256 KB（≈一帧的尺寸）    2.83 MB/s      ← 小文件被「每请求 ≈90 ms 往返」拖住
    一个真实场次 469.8 MB 打 tar 后   59.4 MB/s      ← 打包本身 157 MB/s，不是瓶颈

所以本工具**把每场打成一个 tar 再传** —— 一个大对象吃满 59 MB/s。逐文件上传只有
2.83 MB/s，慢 21 倍，纯粹输在每个 PUT 的往返开销上（58 MB/s 的链路传 256 KB 只要 4.4 ms）。

## COS 上的目录约定

    <bucket>/data/<8位日期>/<场次名>.tar            场次本体（tar，**不压缩**）
    <bucket>/data/<8位日期>/<场次名>.tar.partNNN     >4 GiB 的场次拆成的独立对象
    <bucket>/data/<8位日期>/<场次名>.manifest.json   逐文件清单（存档那一刻的真相）

>4 GiB 的 tar **不是 multipart**，而是多个普通对象（当前子账号没有分片上传权限）：
`cat live_x.tar.part000 live_x.tar.part001 > live_x.tar` 即还原。账本里的 `key` 仍是
规范 tar 键（笔记本门禁按它比对），实际对象在 `parts` 里。

`.manifest.json` 是刻意存的：服务器那份一旦删掉，「这场到底该有哪些文件」的权威记录就
只剩它了。以后笔记本从 COS 拉回来时，解 tar → `live_ledger.dir_manifest()` → 与它逐项比对，
**不需要服务器在线**，也不需要两台机器跑同一份代码才能对得上。

不压缩的理由：语料是 JPEG 帧与 WAV，压不动；而 tar 的 157 MB/s 远高于链路的 59 MB/s，
zstd 只会把 CPU 变成新瓶颈（还让以后每次取一个文件都得先解压）。

## 安全模型（照这个顺序读）

1. **完整性判定不在本工具里重写。** 「完整 / 不完整 / 采集中 / 空」全部由
   `live_ledger.classify_session()` 给出（同一份代码、唯一实现）。**采集中永不碰** ——
   正在写的目录打出来的 tar 是残缺的，而它看起来又完全合法。
2. **默认不删服务器那份。** 上传成功只是「COS 上有了一份」，删服务器是**另一个决定**，
   必须显式 `--delete-after-upload`。非默认的理由：这一步没有回头路，而在「笔记本从 COS
   取」那一段还没打通之前，服务器那份是唯一能直接用的副本。
3. 真要删时**走 `live_ledger.py --delete-list`**（内部复用 `delete_sessions()`），
   于是墓碑账本与两道硬保护（`safe_to_delete`、在采中永不删）自动继承，
   不新开任何绕过保护的删除路径。墓碑 label 用 `已归档COS`，与自动传输的
   `已传输`、人工的 `已迁移` 在 `dist/deleted_sessions.jsonl` 里一眼可分。
4. **凭据只从 `config/cos.env` 读**（服务器上 `root:root 600`），任何路径都不打印、
   不回显、不写进日志。缺文件时直接报错退出，不去猜默认值。

## 零依赖（刻意的）

**不用 `cos-python-sdk-v5`。** COS V5 的签名就是 sha1 + HMAC-SHA1，`hashlib` / `hmac`
足够，HTTP 用 venv 里已有的 `requests`。好处是**部署仍然只是拷文件**（`pack.sh`），
服务器上不需要 `pip install`（少一步就会有人漏掉，而症状是「任务全失败」）；
以后笔记本侧要直接从 COS 取时，也不用为了下载一个 tar 装一整套 SDK。

## 用法

    ./venv/bin/python service/cos_archive.py --selftest      # 启用前必做：真发一个对象再删掉
    ./venv/bin/python service/cos_archive.py --dry-run       # 只看要传什么
    ./venv/bin/python service/cos_archive.py --limit 3       # 先传 3 场
    ./venv/bin/python service/cos_archive.py                 # 全部（**不删**服务器那份）
    ./venv/bin/python service/cos_archive.py --delete-after-upload   # ★传完即删（想清楚再开）
"""
import argparse
import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from urllib.parse import quote

# ── 复用服务器那份「判定 / 清单」实现（同一份代码 → 口径必然一致）─────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_ledger as ll          # noqa: E402

# ⚠️ 与 `live_ledger.py` / `health_check.py` / `pull_sessions.py` 同一条约定：幂等包装。
#    两个模块都包 stdout 时，先包的那个会被 GC 掉并关掉底层 buffer。
if not getattr(sys.stdout, '_douyin_utf8', False):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                      errors='replace', line_buffering=True)
        sys.stdout._douyin_utf8 = True
    except Exception:
        pass
if not getattr(sys.stderr, '_douyin_utf8', False):
    try:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                                      errors='replace', line_buffering=True)
        sys.stderr._douyin_utf8 = True
    except Exception:
        pass

# ================================================================ 常量
ROOT = ll.ROOT
DIST = os.path.join(ROOT, 'service', 'dist')
LEDGER = os.path.join(DIST, 'cos_archive.jsonl')      # 审计 + 「已归档」判据
LOCK = os.path.join(DIST, 'cos_archive.lock')
LOG = os.path.join(DIST, 'cos_archive.log')
# 归档心跳：**只在「这一轮真的启动了」时写**，给 `reclaim_check.py` 判「归档腿在不在动」。
# 与 LOCK 的区别：锁表示「此刻有谁在跑」（跑完就删），心跳要**留在原地** —— 归档早就跑完
# 退出了，体检仍然要知道「它两小时前跑过」。
BEAT = os.path.join(DIST, 'cos_archive.beat')
TMP_DIR = os.path.join(DIST, '_cos_tmp')              # tar 临时文件（**同卷**，见 pack_tar）
COS_ENV = os.path.join(ROOT, 'config', 'cos.env')

# vhost 形式。**实测这个公网域名在腾讯云内网解析到 169.254.0.47**（内网魔法 IP），
# 所以走它就已经是内网，不需要另配 `cos-internal.` 域名（两者解析结果相同）。
ENDPOINT = 'https://{bucket}.cos.{region}.myqcloud.com'
DEFAULT_REGION = 'ap-shanghai'

PREFIX = 'data'                                       # COS 上的顶层前缀
SIGN_EXPIRE = 3600                                    # 签名有效期（秒），单次请求够用
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 1800                                   # 1.5 GB / 59 MB/s ≈ 27 s，给足余量

# COS 单 PUT 上限 **5 GiB**。超过它的 tar 必须拆成多个对象 —— 2026-10-19 实测一场
# 5,399,797,760 B（5.03 GiB）的场次连续两次在上传末段报 SSLEOFError，COS 上不留对象。
# 阈值取 4 GiB，给「接近上限但还没超」的场次留余量。
#
# ⚠️ 为什么不是**分片上传（multipart）**：2026-10-20 用当前 `cos.env` 的凭据实测，
# `POST ?uploads`（InitiateMultipartUpload）恒返回 **403 AccessDenied**，而同一把 key
# 对同一前缀的普通 `PUT` 是 200 —— 这把子账号只有 PutObject 权限，没有分片接口权限。
# 所以走「**每个 ≤4 GiB 的独立对象**」：`<场次>.tar.part000`、`.part001` ……
# 每个都用普通 PUT（同一把 key 已证明可用），拼回来就是 `cat part* > live_x.tar`。
# 账本里仍保留逻辑键 `key`（规范 tar 键，笔记本门禁按它比对），真正的对象列在 `parts`。
SHARD_THRESHOLD = 4 * 1024 ** 3
SHARD_SIZE = 4 * 1024 ** 3                            # 4 GiB/片；5.03 GiB → 2 片

EXIT_OK, EXIT_FAIL, EXIT_ENV, EXIT_LOCKED = 0, 1, 4, 3


# ================================================================ 凭据
def load_cos_env(path=COS_ENV):
    """读 `config/cos.env` → `{'COS_BUCKET':…, 'COS_SECRET_ID':…, …}`。

    ⚠️ **绝不要把这个 dict 打印出来**（`print(cfg)` 会把密钥写进日志和 systemd journal）。
    要报错只报**缺了哪个键**，不报值。
    """
    need = ('COS_SECRET_ID', 'COS_SECRET_KEY', 'COS_BUCKET')
    cfg = {}
    try:
        with open(path, encoding='utf-8') as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith('#') or '=' not in ln:
                    continue
                k, v = ln.split('=', 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError as e:
        raise RuntimeError(f'读不到 COS 凭据 {path}：{e}（见 DEPLOY.md，需 root:root 600）')
    missing = [k for k in need if not cfg.get(k)]
    if missing:
        # 只报键名 —— 值可能是占位符（`AKID替换` 这种），但绝不回显
        raise RuntimeError(f'{path} 缺少这些键：{missing}（当前有 {sorted(cfg)}）')
    cfg.setdefault('COS_REGION', DEFAULT_REGION)
    return cfg


def config_mode_warning(path=COS_ENV):
    """凭据文件权限不是 600 → 返回一句告警（**不改权限**，那是运维的事）。"""
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return ''
    return '' if mode == 0o600 else f'⚠️ {path} 权限是 {oct(mode)}，应为 600'


_PLACEHOLDER_HINTS = ('替换', '填', '改成', '你的', 'your', 'xxx', '...', 'todo')


def looks_placeholder(v):
    """值看起来还是**模板占位符**，不是真凭据（`AKID替换` / `填这里` / `xxx`）。

    只是**告警**不是判据 —— 真凭据也可能碰巧含这些子串，所以它绝不拦截，
    只把「403 AccessDenied」这种含糊报错换成人一眼能懂的「你那一行还是模板」。
    （2026-10-16 上一版 `cos.env` 里就真的是 `COS_SECRET_KEY=替换`，当时是靠
    长度 + 占位符特征手工判出来的。）
    """
    low = str(v).lower()
    return any(h in low for h in _PLACEHOLDER_HINTS)


def config_warnings(cfg):
    """凭据的形状告警（**只报键名，绝不回显值**）"""
    out = [config_mode_warning()]
    for k in ('COS_SECRET_ID', 'COS_SECRET_KEY'):
        if looks_placeholder(cfg.get(k, '')):
            out.append(f'⚠️ {k} 看起来还是模板占位符 —— 请确认已换成真实值')
    if cfg.get('COS_SECRET_ID') and len(cfg['COS_SECRET_ID']) < 20:
        out.append(f"⚠️ COS_SECRET_ID 只有 {len(cfg['COS_SECRET_ID'])} 字符"
                   f"（腾讯云 SecretId 通常 36 字符、以 AKID 开头）")
    if cfg.get('COS_SECRET_KEY') and len(cfg['COS_SECRET_KEY']) != 32:
        out.append(f"⚠️ COS_SECRET_KEY 是 {len(cfg['COS_SECRET_KEY'])} 字符（通常 32）")
    return [w for w in out if w]


# ================================================================ COS V5 签名（纯函数）
def _q(s):
    """URL 编码。COS 要的是 RFC3986 —— `quote(s, '')` 保留 `-_.~`，正好一致。"""
    return quote(str(s), safe='')


def cos_authorization(method, key, headers, secret_id, secret_key,
                      start=None, expire=SIGN_EXPIRE, params=None):
    """算 COS V5 的 `Authorization` 头。**纯函数**（给了 `start` 就完全确定，可离线单测）。

    ```
    KeyTime      = <start>;<start+expire>
    SignKey      = hex(HMAC-SHA1(SecretKey, KeyTime))
    HttpString   = method\\n uri \\n url-params \\n headers \\n
    StringToSign = "sha1\\n" + KeyTime + "\\n" + hex(SHA1(HttpString)) + "\\n"
    Signature    = hex(HMAC-SHA1(SignKey, StringToSign))
    ```

    ⚠️ 三个容易记错的地方，都按 SDK 的 `cos_auth.py` 核过：
    1. `SignKey` 是 **hex 字符串**，第二次 HMAC 拿它当 key（不是原始字节）；
    2. 规范化头是 `key=value` 用 **`&`** 连接（而 `q-header-list` 里的键用 **`;`** 连接），
       两处都按**键名排序**；
    3. 参与签名的头只认三类：`host`、`content-type`/`content-length`、`x-cos-*`。
       其余（如 `User-Agent`）即使发出去也不进签名 —— 所以 requests 自己加的头部
       不会破坏签名。
    4. 查询参数（分片上传的 `uploads` / `partNumber` / `uploadId`）也必须参与签名；
       没有参数时 `canon_params` 与 `q-url-param-list` 都为空 —— 与改动前的单 PUT
       逐字节一致（`[1b]` 的已知值回归锁不受影响）。
    """
    if start is None:
        start = int(datetime.now().timestamp())
    key_time = f'{start};{start + expire}'
    sign_key = hmac.new(secret_key.encode('utf-8'), key_time.encode('utf-8'),
                        hashlib.sha1).hexdigest()

    low = {k.lower(): v for k, v in headers.items()}
    signed = {k: v for k, v in low.items()
              if k in ('content-type', 'content-length') or k.startswith('x-cos-')}
    signed['host'] = low['host']                      # host 必须签

    header_list = ';'.join(sorted(signed))
    canon_headers = '&'.join(f'{_q(k)}={_q(v)}' for k, v in sorted(signed.items()))
    uri = '/' + _q(key).replace('%2F', '/')           # 路径里的 `/` 不编码
    # 查询参数也必须进签名（分片上传的 ?uploads / ?partNumber=…&uploadId=…）：
    # COS 的 canonical query string 按键名排序、`&` 连接；q-url-param-list 用 `;`。
    params = {str(k).lower(): str(v) for k, v in (params or {}).items()}
    param_items = sorted(params.items())
    canon_params = '&'.join(f'{_q(k)}={_q(v)}' for k, v in param_items)
    param_list = ';'.join(k for k, _ in param_items)
    http_string = f'{method.lower()}\n{uri}\n{canon_params}\n{canon_headers}\n'
    string_to_sign = ('sha1\n' + key_time + '\n'
                      + hashlib.sha1(http_string.encode('utf-8')).hexdigest() + '\n')
    signature = hmac.new(sign_key.encode('utf-8'), string_to_sign.encode('utf-8'),
                         hashlib.sha1).hexdigest()

    return ('q-sign-algorithm=sha1'
            f'&q-ak={secret_id}'
            f'&q-sign-time={key_time}'
            f'&q-key-time={key_time}'
            f'&q-header-list={header_list}'
            f'&q-url-param-list={param_list}'
            f'&q-signature={signature}')


# ================================================================ COS 侧副作用
def shard_plan(size, shard_size=SHARD_SIZE):
    """`size` 字节按 `shard_size` 切片 → `[(index, offset, length), …]`。

    index 从 **0** 开始（对象名后缀用）。最后一片是余数；空文件返回空列表。
    **纯函数**，单测直接喂边界值，不需要网络。

    与 multipart 的 partNumber 无关：这是**多个独立对象**，每个都走普通 PUT。
    """
    if size <= 0 or shard_size <= 0:
        return []
    return [(i, i * shard_size, min(shard_size, size - i * shard_size))
            for i in range((size + shard_size - 1) // shard_size)]


def shard_key(key, index):
    """逻辑键 `….tar` → 实际对象键 `….tar.part000`（**逻辑键本身不会存在**）。"""
    return f'{key}.part{index:03d}'


def record_parts(rec):
    """账本记录 → 实际的 COS 对象列表 `[{'key', 'bytes'}, …]`。

    老记录（分片方案之前）没有 `parts` → 唯一对象就是规范 tar 键。
    """
    parts = rec.get('parts')
    if parts:
        return parts
    return [{'key': rec.get('key') or session_key(rec.get('dir', '')),
             'bytes': rec.get('tar_bytes')}]


def record_present(cos, rec):
    """账本说已归档的那份数据，**此刻**是否完整躺在 COS 上。

    分片记录要求**每一片都在、且字节数与账本一致** —— 只查第一片会让「第二片被生命周期
    规则删掉」这种静默丢失漏过去，而那正是这个函数存在的理由。
    """
    for p in record_parts(rec):
        key = p.get('key')
        if not key:
            return False
        remote = cos.size_of(key)
        if remote is None:
            return False
        want = p.get('bytes')
        if want is not None and remote != want:
            return False
    return True


class _SliceReader:
    """把已 seek 到 offset 的文件句柄**只读 length 字节**交给 requests（不进内存）。

    4 GiB 一片直接 `fh.read()` 会把整片读进内存 —— 这台机器 2 GB 内存、无 swap，
    等于自杀。requests 流式读它时每次只拿 8 KiB 左右，所以 `__len__` 必须给出**剩余**
    字节数（它会被用来填 Content-Length；我们也显式设了该头，双保险）。
    """

    def __init__(self, fh, length):
        self._fh = fh
        self._left = length

    def read(self, n=-1):
        if self._left <= 0:
            return b''
        if n is None or n < 0:
            n = self._left
        data = self._fh.read(min(n, self._left))
        self._left -= len(data)
        return data

    def __len__(self):
        return self._left


class Cos:
    """极简 COS 客户端：只需 PUT / HEAD / DELETE 三个动作，所以不引 SDK。"""

    def __init__(self, cfg):
        import requests                    # 延迟导入：单测（纯函数）不需要它
        self.cfg = cfg
        self.bucket = cfg['COS_BUCKET']
        self.region = cfg['COS_REGION']
        self.host = ENDPOINT.format(bucket=self.bucket, region=self.region).split('://', 1)[1]
        self.base = ENDPOINT.format(bucket=self.bucket, region=self.region)
        self.s = requests.Session()

    def url(self, key):
        return f'{self.base}/{key}'

    def _headers(self, method, key, extra=None, params=None):
        h = {'Host': self.host}
        h.update(extra or {})
        h['Authorization'] = cos_authorization(
            method, key, h, self.cfg['COS_SECRET_ID'], self.cfg['COS_SECRET_KEY'],
            params=params)
        return h

    def put_file(self, key, path, meta=None):
        """从一个**文件**上传（流式，不进内存）。返回 `(状态码, 响应头, 文本片段)`。

        只用于 ≤4 GiB 的对象（tar 统一走 `put_sharded()`；manifest 很小）。
        超过 COS 单 PUT 上限的 tar 由 `put_sharded()` 拆成多个独立对象。
        """
        size = os.path.getsize(path)
        if size > SHARD_THRESHOLD:
            raise RuntimeError(
                f'{path} 有 {size} 字节，超过单对象阈值 {SHARD_THRESHOLD}；'
                f'必须用 put_sharded()（当前凭据没有分片上传权限）')
        extra = {'Content-Length': str(size), 'Content-Type': 'application/octet-stream'}
        for k, v in (meta or {}).items():
            extra['x-cos-meta-' + k] = str(v)
        headers = self._headers('put', key, extra)
        with open(path, 'rb') as fh:
            r = self.s.put(self.url(key), data=fh, headers=headers,
                           timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        return r.status_code, dict(r.headers), r.text[:300]

    def put_shard(self, key, path, offset, length, meta=None):
        """把文件的一个**字节区间**作为一个独立对象上传（流式，不进内存）。"""
        extra = {'Content-Length': str(length), 'Content-Type': 'application/octet-stream'}
        for k, v in (meta or {}).items():
            extra['x-cos-meta-' + k] = str(v)
        headers = self._headers('put', key, extra)
        with open(path, 'rb') as fh:
            fh.seek(offset)
            r = self.s.put(self.url(key), data=_SliceReader(fh, length),
                           headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        return r.status_code, dict(r.headers), r.text[:300]

    def put_sharded(self, key, path, meta=None, shard_size=SHARD_SIZE):
        """把一个大文件传成**一组独立对象** `key` 或 `key.part000…`，返回 `parts` 列表。

        - ≤4 GiB：普通单对象，`parts` 只有一项（键就是 `key`）。
        - >4 GiB：按 `shard_size` 切片，每片是一个独立对象；**不合并**，拼回来是
          `cat live_x.tar.part000 live_x.tar.part001 > live_x.tar`。
        - 每一片传完都当场 HEAD 比字节数；任何一片失败 → 删掉本轮已传的片再抛异常，
          避免桶里留下一组「看起来有、其实不全」的半成品。

        为什么不用 COS 的 multipart：InitiateMultipartUpload 在**当前子账号**上是 403
        （见模块顶部 SHARD_* 注释），普通 `PUT` 才是这把 key 被允许的动作。
        """
        size = os.path.getsize(path)
        plan = shard_plan(size, shard_size)
        if len(plan) <= 1:
            code, hdrs, body = self.put_file(key, path, meta=meta)
            if code != 200:
                raise RuntimeError(f'上传失败 HTTP {code}：{body[:200]}')
            remote = self.size_of(key)
            if remote != size:
                self.delete(key)
                raise RuntimeError(f'核验不过：本地 {size} / COS 上 {remote}')
            return [{'key': key, 'bytes': size,
                     'etag': hdrs.get('ETag', '').strip('"')}]

        parts = []
        try:
            for index, offset, length in plan:
                pkey = shard_key(key, index)
                code, hdrs, body = self.put_shard(pkey, path, offset, length, meta=meta)
                if code != 200:
                    raise RuntimeError(
                        f'第 {index} 片（{length} 字节）上传失败 HTTP {code}：{body[:200]}')
                remote = self.size_of(pkey)
                if remote != length:
                    raise RuntimeError(
                        f'第 {index} 片核验不过：本地 {length} / COS 上 {remote}')
                parts.append({'key': pkey, 'bytes': length,
                              'etag': hdrs.get('ETag', '').strip('"')})
        except Exception:
            for p in parts:                 # 本轮已传的片清掉，重传时不留半成品
                try:
                    self.delete(p['key'])
                except Exception:
                    pass
            raise
        return parts

    def head(self, key):
        """返回 `(状态码, 响应头 dict)`。404 → `(404, {})`，不抛异常。"""
        try:
            r = self.s.head(self.url(key), headers=self._headers('head', key),
                            timeout=(CONNECT_TIMEOUT, 60))
        except Exception as e:                      # 网络/SSL 失败也算「量不到」
            return -1, {'error': f'{type(e).__name__}: {e}'}
        return r.status_code, dict(r.headers)

    def delete(self, key):
        r = self.s.delete(self.url(key), headers=self._headers('delete', key),
                          timeout=(CONNECT_TIMEOUT, 60))
        return r.status_code

    def size_of(self, key):
        """对象字节数；不存在或问不到 → `None`（**不是** 0）。"""
        code, h = self.head(key)
        if code != 200:
            return None
        try:
            return int(h.get('Content-Length', -1))
        except (TypeError, ValueError):
            return None


# ================================================================ 纯逻辑
def session_key(rel_dir):
    """`data/20261016/live_x_20261016_020000` → `data/20261016/live_x_20261016_020000.tar`

    保持与服务器 `data/` 同构 —— 光看对象键就知道它从哪来、属于哪天。
    """
    return f'{rel_dir}.tar'


def manifest_key(rel_dir):
    return f'{rel_dir}.manifest.json'


def is_empty(f):
    """全空场次（弹幕/事件/音频/帧全 0）：只有 ~200 KB 壳子，**不传也不删**。

    与 `pull_sessions.is_empty()` 同一口径（都读 `classify_session()` 的 `modal`）。
    """
    return ll.classify_session(f)[2] == '空'


def plan_cycle(facts, archived, only=''):
    """逐场次事实 + 归档账本 → `(todo, done, skipped)`。

    - `采集中` → 跳过（**绝不碰正在写的**：打出来的 tar 是残缺的，且看起来毫无异常）
    - `空` → 跳过（只污染语料，服务器上归 `--delete-incomplete` 管）
    - 账本里已有 **且** 字节数/文件数都对得上 → `done`（不重传）
    - `only` 非空时只处理那一场（手工排查用）
    """
    todo, done, skipped = [], [], []
    for f in sorted(facts, key=lambda x: x.get('started') or ''):
        rel = f['dir']
        if only and rel != only:
            continue
        completeness = ll.classify_session(f)[0]
        if completeness == '采集中':
            skipped.append((rel, '采集中（正在写，绝不碰）'))
            continue
        if is_empty(f):
            skipped.append((rel, '全空场次（弹幕/事件/音频/帧都是 0）'))
            continue
        rec = archived.get(rel)
        if rec and rec.get('bytes') == f.get('_bytes') and rec.get('files') == f.get('_files'):
            done.append(rel)
        else:
            todo.append(f)
    return todo, done, skipped


def verify_upload(cos, key, local_size, remote_size):
    """上传后的**独立**核验：再 HEAD 一次，比对字节数。

    为什么不只看 PUT 的返回码：PUT 是原子的（失败不会留下半截对象），但返回 200 只代表
    COS 收了这份请求，不能证明落盘的就是我们要的那些字节。一次 HEAD 很便宜，
    而这一步的产物以后是**唯一副本**。

    :return: `(是否通过, 问题描述)`
    """
    if remote_size is None:
        return False, f'HEAD 拿不到对象 {key}（上传后不存在？）'
    if remote_size != local_size:
        return False, f'字节数不符：本地 tar {local_size} / COS 上 {remote_size}'
    return True, ''


# ── 打包与哈希 ──
def pack_tar(session_abs, tar_path):
    """把场次目录打成 tar（**不压缩**，理由见模块顶部）。

    归档里存**相对路径**（`-C <父目录>` + 场次名）→ 解包时 `tar -C data/<日期>` 就还原
    成服务器上的样子，与 `pull_sessions.py` 的落地布局天然一致。

    ⚠️ `tar_path` 必须与 `data/` 在**同一个卷**上（本工具放在 `dist/_cos_tmp/`，同卷）。
    写进 `/tmp` 若它恰好是 tmpfs，一个 1.6 GB 的 tar 就是 1.6 GB 内存 —— 这台机器
    **没有 swap**，会直接把采集进程一起 OOM 掉。
    """
    os.makedirs(os.path.dirname(tar_path), exist_ok=True)
    parent = os.path.dirname(session_abs)
    name = os.path.basename(session_abs)
    p = subprocess.run(['tar', '-C', parent, '-cf', tar_path, name],
                       capture_output=True, timeout=1800)
    if p.returncode != 0:
        raise RuntimeError(f'tar 失败 rc={p.returncode}：{p.stderr.decode("utf-8", "replace")[:300]}')
    return os.path.getsize(tar_path)


def tar_members(tar_path):
    """读 tar 的**成员表**（只取名字与字节数，不读内容）→ `[{'p','n'}, …]`，按 p 排序。

    tar 里存的是 `<场次名>/<相对路径>`，这里剥掉第一段，好与 `dir_manifest()` 同口径。
    目录项跳过 —— `dir_manifest()` 只收文件。
    """
    out = []
    with tarfile.open(tar_path, 'r:') as tf:
        for m in tf:
            if m.isdir():
                continue
            rel = m.name.split('/', 1)[1] if '/' in m.name else m.name
            out.append({'p': rel, 'n': m.size})
    out.sort(key=lambda e: e['p'])
    return out


def verify_tar(tar_path, manifest, max_report=5):
    """tar 里到底装了什么？与打包前的清单**逐项**比对 → `(是否一致, 问题列表)`。

    ★ 这**不是**「比一下体积」那种弱检查。tar 每个文件加 512 字节头部、尾部还有
    1024 字节 + padding，所以它的体积天生就比目录大（一场 836 个文件要多出 400 多 KB）
    —— 拿体积跟目录字节数比是**必然失败**的错判；而反过来「体积对得上」也说明不了什么。

    真正要证明的是：归档里的**文件集合与每个文件的字节数**，等于打包前那一刻的目录状态。
    这也顺带抓住「打包期间目录被改动」（有进程还在往里追加）——那种情况打出来的 tar
    看起来完全正常，只有这一比才会露馅。
    """
    want = {e['p']: e['n'] for e in manifest['files']}
    got = {e['p']: e['n'] for e in tar_members(tar_path)}
    problems = []
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    if missing:
        problems.append(f'tar 里少了 {len(missing)} 个文件，如 {missing[:3]}')
    if extra:
        problems.append(f'tar 里多了 {len(extra)} 个文件，如 {extra[:3]}')
    bad = [p for p in set(want) & set(got) if want[p] != got[p]]
    if bad:
        problems.append(f'{len(bad)} 个文件字节数不符，如 '
                        f'{[(p, want[p], got[p]) for p in bad[:3]]}')
    return (not problems), problems[:max_report]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b''):
            h.update(chunk)
    return h.hexdigest()


def append_record(rec):
    try:
        os.makedirs(DIST, exist_ok=True)
        with open(LEDGER, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError as e:
        log(f'⚠️ 归档账本写入失败：{e}')


def load_ledger():
    """读账本 → `{相对场次目录: 记录}`（**最后一次**记录生效，便于人工修正后重传）"""
    out = {}
    try:
        with open(LEDGER, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get('dir'):
                    out[rec['dir']] = rec
    except OSError:
        pass
    return out


_LOG_QUIET = False


def log(msg):
    line = f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}'
    if not _LOG_QUIET:
        print(line)
    try:
        os.makedirs(DIST, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


# ================================================================ 并发保护
def acquire_lock(max_runtime_min=180):
    """拿锁 → True；上一轮还在跑 → False。**过期的锁要能自愈**，否则一次崩溃就永久停摆。

    ⚠️「过期」按**心跳**判（锁文件多久没被更新），不按「建于多久前」判 —— 与
    `pull_sessions.acquire_lock()` 同一条理由：一轮要传十几 GB，锁的存活时间本该
    由「有没有进展」定义，而不是由「开了多久」定义。
    """
    os.makedirs(DIST, exist_ok=True)
    if os.path.exists(LOCK):
        try:
            age_min = (datetime.now().timestamp() - os.path.getmtime(LOCK)) / 60
        except OSError:
            age_min = 0
        if age_min < max_runtime_min:
            return False
        log(f'⚠️ 发现过期锁（{age_min:.0f} 分钟没有心跳 > {max_runtime_min}）→ 接管')
    try:
        with open(LOCK, 'w', encoding='utf-8') as fh:
            fh.write(f'{os.getpid()}\n{datetime.now().isoformat(timespec="seconds")}\n')
    except OSError:
        return True                     # 锁文件写不了不该拦住工作
    return True


def touch_lock():
    """心跳：只刷 mtime，**不改内容**（里面是持有者的 pid 与起始时刻，排查要用）"""
    try:
        os.utime(LOCK, None)
    except OSError:
        pass


def release_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def touch_beat():
    """写归档心跳（`BEAT`）：**只表示这一轮启动过，不代表成功**。

    为什么体检不该改用 `LOG` 的 mtime 代替它：`log()` 到处都是，将来随手加一行日志
    就会让「归档腿在不在动」这条判据**静默失效**（判据失效的方向还是「看起来一切正常」，
    最难发现）。心跳只有一处写，改不动。

    `--dump-ledger` / `--selftest` 两条**短路路径不写**它 —— 它们不是「归档跑了一轮」，
    而笔记本每 30 分钟就会调一次 `--dump-ledger`：要是那也写心跳，体检就永远看不出
    timer 早死了。这一条有单测钉住。
    """
    try:
        os.makedirs(DIST, exist_ok=True)
        with open(BEAT, 'w', encoding='utf-8') as fh:
            fh.write(f'{datetime.now().isoformat(timespec="seconds")}\n')
    except OSError:
        pass


# ================================================================ 自检
def selftest():
    """启用前必做：**真发一个对象、再读回来、再删掉**。只有全绿才该拿去跑全量。"""
    ok = True

    def chk(name, cond, extra=''):
        nonlocal ok
        print(('  ✅ ' if cond else '  ❌ ') + name + (f'  {extra}' if extra else ''))
        if not cond:
            ok = False

    print(f'1) 凭据 {COS_ENV}')
    try:
        cfg = load_cos_env()
    except RuntimeError as e:
        chk('读凭据', False, str(e)[:300])
        return False
    chk('读凭据', True, f"bucket={cfg['COS_BUCKET']} region={cfg['COS_REGION']} "
                        f"（密钥不回显：id {len(cfg['COS_SECRET_ID'])} 字符 / key "
                        f"{len(cfg['COS_SECRET_KEY'])} 字符）")
    warns = config_warnings(cfg)
    chk('权限 600 且凭据形状正常', not warns, '；'.join(warns))

    print('2) 解析与内网')
    cos = Cos(cfg)
    import socket
    try:
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(cos.host, 443)})
    except Exception as e:
        ips = []
        chk('域名可解析', False, str(e)[:200])
    if ips:
        chk('域名可解析', True, f'{cos.host} → {", ".join(ips)}')
        # 169.254.x.x 是腾讯云内网魔法 IP：走它 = 不计公网流量、不撞带宽包
        internal = all(i.startswith('169.254.') for i in ips)
        chk('★ 走的是内网（169.254.x.x）而非公网出口', internal,
            '' if internal else '解析到了公网 IP → 会撞带宽包，又慢又计费')

    print('3) 真发一个对象 → HEAD → DELETE（这一步才算通）')
    key = f'{PREFIX}/_selftest/probe.txt'
    body = b'douyin-live cos_archive selftest\n'
    try:
        extra = {'Content-Length': str(len(body)), 'Content-Type': 'application/octet-stream'}
        headers = cos._headers('put', key, extra)
        r = cos.s.put(cos.url(key), data=body, headers=headers,
                      timeout=(CONNECT_TIMEOUT, 60))
        chk('PUT 成功（签名与权限都对）', r.status_code == 200,
            f'HTTP {r.status_code} {r.text[:200]}')
        if r.status_code == 200:
            chk('HEAD 读得回来且字节数对', cos.size_of(key) == len(body),
                f'{cos.size_of(key)} / {len(body)}')
            chk('DELETE 成功', cos.delete(key) in (200, 204))
    except Exception as e:
        chk('往返测试', False, f'{type(e).__name__}: {e}')

    print('4) 分片对象（>4 GiB 场次走这条路；不是 multipart）')
    skey = f'{PREFIX}/_selftest/shard.bin'
    spath = os.path.join(TMP_DIR, f'_selftest_shard_{os.getpid()}.bin')
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        size = 10 * 1024 * 1024 + 123
        with open(spath, 'wb') as fh:
            fh.write(os.urandom(size))
        parts = cos.put_sharded(skey, spath, meta={'probe': 'shard'},
                                shard_size=4 * 1024 * 1024)
        chk('★ 10 MiB / 4 MiB → 3 个独立对象', len(parts) == 3, f'{len(parts)} 片')
        chk('★ 每片 HEAD 字节数都对',
            all(cos.size_of(p['key']) == p['bytes'] for p in parts))
        fake = {'key': skey, 'tar_bytes': size, 'parts': parts}
        chk('★ record_present 认全部分片', record_present(cos, fake))
        for p in parts:
            cos.delete(p['key'])
        chk('★ 缺任一分片 → record_present 为假（不会只看第一片）',
            not record_present(cos, fake))
    except Exception as e:
        chk('分片对象往返', False, f'{type(e).__name__}: {e}')
    finally:
        try:
            os.remove(spath)
        except OSError:
            pass

    print('5) 本地前提')
    chk('tar 可执行', shutil.which('tar') is not None, shutil.which('tar') or '')
    st = os.statvfs(DIST if os.path.isdir(DIST) else ROOT)
    free_gb = st.f_bavail * st.f_frsize / 1e9
    chk('临时目录所在卷剩余 > 3 GB（最大场次 tar 1.6 GB）', free_gb > 3, f'{free_gb:.1f} GB')
    return ok


def _emit(obj, dest):
    """把 JSON 写到 dest（`-` = stdout）。**stdout 上只允许有它** —— 调用方按 JSON 解析。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2) + '\n'
    if dest == '-':
        sys.stdout.write(text)
        sys.stdout.flush()
    else:
        with open(dest, 'w', encoding='utf-8') as fh:
            fh.write(text)


def dump_ledger(dest):
    """把归档账本交给调用方（笔记本侧），每条 `verify=='ok'` 的记录**附 `object_present`**。

    为什么必须是**当场 HEAD**，而不是直接把 `rec['verify']` 交出去：
    `verify=='ok'` 只是**过去某一次上传的回执**，写下之后再没被任何东西重新确认过 ——
    桶被清空、生命周期规则到期、账本是从别的桶搬来的、有人手工删过对象，账本都照样
    写着 `ok`。本工具自己在 `_cycle()` 的 1b 步就**不信账本**（那里也要再 HEAD 一遍），
    调用方自然更不该信。

    ⚠️ 更硬的一条：1b 步在 `if not todo: return EXIT_OK` **之后**才执行 —— 也就是
    「所有场次都已归档、本轮没有新东西要传」这个**恰恰最需要确认对象还在**的场景里，
    服务器**一次 HEAD 都不做**就返回 0。所以本工具的退出码只说明「我跑完了」，
    **不能**当成「COS 上确实有那份数据」的证据。要证据，只有这个 HEAD。

    不抢锁（纯读，与正在跑的那一轮不冲突）。账本不存在 → 输出 `{}` 返回 0 —— 那不是错误，
    只是还没归档过任何东西。

    ⚠️ `dest == '-'` 时 **stdout 只允许有 JSON**：此处强制 quiet，因为 `log()` 非 quiet 时
    会 `print` 到 stdout 把 JSON 冲掉。诊断一律走 stderr。
    """
    global _LOG_QUIET
    _LOG_QUIET = True

    ledger = load_ledger()
    out = {d: dict(rec) for d, rec in ledger.items()}
    # 保守默认：只有下面真正 HEAD 成功证实的才翻成 true
    for rec in out.values():
        rec['object_present'] = False

    pending = [d for d, r in ledger.items() if r.get('verify') == 'ok']
    if pending:
        try:
            cos = Cos(load_cos_env())
        except RuntimeError as e:
            # 拿不到凭据 = 给不出活证据。**不静默降级成「都在」** —— 照实交出去
            # （全部 object_present=false），调用方据此 fail-closed 不删。
            print(f'⚠️ {e}', file=sys.stderr)
            print('⚠️ 无法核实对象是否还在，全部按 object_present=false 输出（调用方将不删）',
                  file=sys.stderr)
            _emit(out, dest)
            return EXIT_ENV

        gone = []
        for d in pending:
            present = record_present(cos, ledger[d])
            out[d]['object_present'] = present
            if not present:
                gone.append(d)
        if gone:
            print(f'⚠️ {len(gone)} 场账本说「已归档」、COS 上却找不到对象：'
                  f'{gone[:5]}{" …" if len(gone) > 5 else ""}', file=sys.stderr)

    _emit(out, dest)
    return EXIT_OK


# ================================================================ main
def main():
    ap = argparse.ArgumentParser(
        description='把服务器上采完的场次打包上传到 COS（走内网，见模块顶部实测）')
    ap.add_argument('--inflight-min', type=int, default=15,
                    help='目录内最新文件在这个分钟内被写过 → 视为采集中（不传）')
    ap.add_argument('--limit', type=int, default=0, help='本轮最多传几场（0 = 不限）')
    ap.add_argument('--only', default='', help='只处理这一场（相对场次目录）')
    ap.add_argument('--delete-after-upload', action='store_true',
                    help='★ 传完并校验通过后，让服务器删掉场次目录（默认**不删**，见模块顶部）')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划，不传也不删')
    ap.add_argument('--selftest', action='store_true', help='环境自检后退出')
    ap.add_argument('--dump-ledger', default=None, metavar='PATH',
                    help='把归档账本打成 JSON 写到 PATH（`-` = stdout）后退出；'
                         '每条 verify==ok 的记录附 `object_present`（当场 HEAD 核实）。'
                         '不抢锁、不传不删 —— 供笔记本侧判断「能不能删服务器那份」')
    ap.add_argument('--quiet', action='store_true', help='不往 stdout 打（仍写日志文件）')
    ap.add_argument('--lock-min', type=int, default=180,
                    help='锁多少分钟没心跳才算过期（据此接管）')
    ap.add_argument('--keep-manifest', action='store_true', default=True,
                    help='同时上传 .manifest.json（默认开，存档的真相）')
    args = ap.parse_args()

    global _LOG_QUIET
    _LOG_QUIET = args.quiet

    if args.selftest:
        return EXIT_OK if selftest() else EXIT_ENV

    # 纯读模式，**不抢锁** —— 它只 HEAD 不写，跟正在跑的那一轮不冲突。
    if args.dump_ledger:
        return dump_ledger(args.dump_ledger)

    if not acquire_lock(args.lock_min):
        log('上一轮还在跑（锁未过期）→ 本轮跳过')
        return EXIT_LOCKED
    try:
        return _cycle(args)
    finally:
        release_lock()


def _cycle(args):
    # ---- 0. 心跳 ----
    # 位置在 `load_cos_env()` **之前**，故意的：它答的是「timer 有没有把这一轮跑起来」，
    # 而不是「这一轮成功了没」。凭据坏掉 / COS 连不上时**照写** —— 那些情况由归档单元
    # 自己的退出码（EXIT_ENV / EXIT_FAIL，都是非 0 → `systemctl --failed` 直接亮）和
    # `reclaim_check.py` 查账本对象那一条覆盖，不需要心跳也来插一脚。
    touch_beat()

    try:
        cfg = load_cos_env()
    except RuntimeError as e:
        log(f'❌ {e}')
        return EXIT_ENV
    for w in config_warnings(cfg):
        log(w)

    # ---- 1. 取事实（判定只有 live_ledger 一份实现）----
    facts = ll.scan_all_sessions(inflight_min=args.inflight_min)
    for f in facts:                       # scan_session 不直接给 size/count，这里补一次
        f['_bytes'], f['_files'] = ll._dir_stat(f['abspath'])

    archived = load_ledger()
    todo, done, skipped = plan_cycle(facts, archived, args.only)
    if args.limit > 0:
        todo = todo[:args.limit]

    total_bytes = sum(f['_bytes'] for f in todo)
    log(f'服务器 {len(facts)} 场；待归档 {len(todo)}（{total_bytes / 1e9:.2f} GB）、'
        f'已归档 {len(done)}、跳过 {len(skipped)}')
    for rel, why in skipped:
        log(f'  · 跳过 {rel}：{why}')

    if args.dry_run:
        for f in todo:
            log(f'  · 将传 {f["dir"]}（{ll.classify_session(f)[0]}，'
                f'{f["_bytes"] / 1e6:.1f} MB，{f["_files"]} 文件）')
        log('（--dry-run，什么都没做）')
        return EXIT_OK
    if not todo:
        log('没有要传的场次')
        return EXIT_OK

    cos = Cos(cfg)
    os.makedirs(TMP_DIR, exist_ok=True)

    # ---- 1b. 账本说「已归档」的，再 HEAD 一次确认对象**真的还在** ----
    # 这不是洁癖：账本一旦与实际不符（桶被清空、账本是从别的桶搬来的、有人手工删过
    # 对象），`plan_cycle()` 会**永远跳过**那些场次 —— 配上 `--delete-after-upload`
    # 就是「服务器那份也删了、COS 上也没有」的静默丢失。82 场 ≈ 8 秒，相对传输可忽略。
    if done:
        by_dir = {f['dir']: f for f in facts}
        missing = [rel for rel in done if not record_present(cos, archived[rel])]
        if missing:
            log(f'⚠️ 账本记录已归档，但 COS 上找不到对象 → 本轮重传 '
                f'{missing[:5]}' + (f' 等 {len(missing)} 场' if len(missing) > 5 else ''))
            todo.extend(by_dir[r] for r in missing if r in by_dir)
            done = [r for r in done if r not in missing]

    # ---- 2. 逐场：取清单 → 打包 → 校验 tar → 上传 → 核验 → 记账 ----
    uploaded, failed = [], []
    for f in todo:
        rel = f['dir']
        key = session_key(rel)
        tar_path = os.path.join(TMP_DIR, f'{f["name"]}.tar')
        try:
            # ★ 清单必须取在**打包之前**：它就是「这场该有什么」的真相 ——
            #   下面拿它核 tar 装对了没有，也随 tar 一起存进 COS（那张 .manifest.json）。
            lm = ll.dir_manifest(f['abspath'])
            size = pack_tar(f['abspath'], tar_path)
            good, problems = verify_tar(tar_path, lm)
            if not good:
                log('  ❌ tar 与清单不符 → 不传（打包期间目录被改了？）：')
                for p in problems:
                    log(f'       - {p}')
                failed.append(rel)
                continue
            digest = sha256_file(tar_path)
            log(f'↑ {rel}（{size / 1e6:.1f} MB，{lm["count"]} 文件）')

            parts = cos.put_sharded(key, tar_path, meta={
                'sha256': digest, 'files': lm['count'], 'bytes': lm['total'],
                'dir': rel, 'manifest-digest': lm['digest'],
            })
            if len(parts) > 1:
                log(f'  · 已拆成 {len(parts)} 个对象（每片 ≤ '
                    f'{SHARD_SIZE / 2 ** 30:.0f} GiB）：'
                    + '、'.join(os.path.basename(p['key']) for p in parts[:3])
                    + (' …' if len(parts) > 3 else ''))

            if args.keep_manifest:
                mkey = manifest_key(rel)
                mpath = os.path.join(TMP_DIR, f'{f["name"]}.manifest.json')
                with open(mpath, 'w', encoding='utf-8') as fh:
                    json.dump(lm, fh, ensure_ascii=False)
                mc, mbody, _ = cos.put_file(mkey, mpath,
                                            meta={'dir': rel, 'digest': lm['digest']})
                os.remove(mpath)
                if mc != 200:
                    # 清单没上去 = 「服务器删了以后就再也无从核验」，所以算失败，
                    # 但**不删**那个已经传好的 tar（重传一次清单比重传 1.6 GB 便宜）。
                    log(f'  ❌ 清单上传失败 HTTP {mc}：{mbody[:200]} → 下一轮只重传清单')
                    failed.append(rel)
                    continue

            rec = {'ts': datetime.now().isoformat(timespec='seconds'), 'dir': rel,
                   'key': key, 'tar_bytes': size, 'tar_sha256': digest,
                   'bytes': lm['total'], 'files': lm['count'],
                   'digest': lm['digest'],
                   'etag': parts[0].get('etag', '') if len(parts) == 1 else '',
                   'verify': 'ok'}
            if len(parts) > 1:
                rec['parts'] = parts
                rec['sharded'] = True
            append_record(rec)
            log(f'  ✅ 已归档 cos://{cfg["COS_BUCKET"]}/{key}（{size / 1e6:.1f} MB'
                + (f'，{len(parts)} 个分片对象' if len(parts) > 1 else '') + '）')
            uploaded.append(rel)
        except Exception as e:
            log(f'  ❌ {rel} 出错：{type(e).__name__}: {e}')
            failed.append(rel)
        finally:
            try:
                os.remove(tar_path)        # 临时 tar 不留在盘上（1.6 GB × N 场）
            except OSError:
                pass
            touch_lock()                   # 每场都刷心跳（见 acquire_lock）

    # ---- 3.（可选）传完即删：**只交本轮真的传成功的**，且走 live_ledger 的删除入口 ----
    if args.delete_after_upload and uploaded:
        rc, out, err = ll_delete(args.inflight_min, uploaded, label='已归档COS')
        for ln in (out + err).strip().splitlines():
            log(f'    [{rc}] {ln}')
        if rc != 0:
            log(f'  ❌ 服务器拒绝删除（rc={rc}）→ 服务器那份保留，下一轮重新核验后再试')
        else:
            log(f'🗑 服务器已删 {len(uploaded)} 场（墓碑标记 已归档COS）')
    elif uploaded:
        log(f'本轮传了 {len(uploaded)} 场，**服务器那份保留**'
            f'（要删加 --delete-after-upload）')

    log(f'本轮结束：成功 {len(uploaded)}、失败 {len(failed)}'
        + (f'（{failed[:5]}）' if failed else ''))
    return EXIT_FAIL if failed else EXIT_OK


def ll_delete(inflight_min, rels, label='已归档COS'):
    """走 `live_ledger.py --delete-list -`（复用 `delete_sessions()`）—— 见模块顶部第 3 条。

    清单走 **stdin**：不落临时文件，也不拼进命令行。
    """
    cmd = [sys.executable, os.path.join(ROOT, 'service', 'live_ledger.py'),
           '--delete-list', '-', '--inflight-min', str(inflight_min),
           '--expect-count', str(len(rels)), '--label', label, '--apply']
    p = subprocess.run(cmd, input=('\n'.join(rels) + '\n').encode('utf-8'),
                       capture_output=True, timeout=600)
    return (p.returncode,
            p.stdout.decode('utf-8', 'replace'),
            p.stderr.decode('utf-8', 'replace'))


if __name__ == '__main__':
    sys.exit(main())
