#!/usr/bin/python
# coding:utf-8
"""把服务器上采完的场次自动拉回笔记本 `D:\\shock\\Documents\\data`（结构与服务器 `data/` 一致）。

## 它解决什么

在这之前，场次是**人工迁移**的：人眼看台账 xlsx / `usable_pending.csv`，手工挑场次目录，
手工 rsync。服务器磁盘因此长期压着几十 GB（实测全模态 ≈ **68.6 GB/天**，40G 盘约 8.4 小时
就撞 `--min-free-gb 5`、之后降级为纯文本）。这个工具把那一步自动化，并让「传完即删」
成为日常 —— 于是服务器磁盘被持续回收，而不是靠人去发现。

## 四个已定的决策（用户拍的，不要再改）

| 项 | 决定 |
|---|---|
| 谁发起 | **笔记本定时拉取**（服务器在公网、笔记本在 NAT 后，只能笔记本主动连）|
| 传完服务器那份 | **自动删**（`--keep-remote` 是退路）|
| 传哪些 | **完整的传进 `data/`，不完整的也传但放 `_incomplete/`** |
| 多久一次 | 每 10 分钟（Windows 计划任务，见文件末尾）|

## ★ 安全模型（照这个顺序读）

1. **判定不在本地重写。** 「完整 / 不完整 / 采集中 / 空」全部由**服务器**用
   `live_ledger.classify_session()` 算好后随 `--dump-facts` 送过来。笔记本只**读**结论。
   判据在第二台机器上再写一份必然漂移（本仓库的口径改过多次）。
2. **本地数据永不自动删。** 它是研究语料，不是缓存。这个工具**任何路径都不会删本地文件**
   （连 `.staging` 里的半截目录也只报告、不清理）。本地空间不够时的行为是
   **停止拉取 + 告警**，不是删旧的腾地方。
3. **删除的前置条件是「本轮校验通过」。** 服务器那份是唯一副本，所以：
   拉 → **再取一次**服务器清单 → 本地重算 → 逐项相等 → 才把它交出去删。
   `pulled_sessions.jsonl` **只作审计，不作「已拉取」的判据** —— 它一旦不准（崩溃、
   人工编辑），以它为准就会**删掉本地根本没有副本的场次**。
4. **服务器侧的删除只走 `live_ledger.py --delete-list`**，它内部复用 `delete_sessions()`，
   于是墓碑账本（`deleted_sessions.jsonl`）与两道硬保护（`safe_to_delete`、
   在采中永不删）一处不改地继承下来。**不新开任何绕过保护的删除路径。**
   （2026-10-15 实测过：裸删 281 个场次让台账的 `★疑似漏采` 从 0 跳到 10，全是假阳性。）

## ⚠️ 传输用的 ssh / rsync 必须来自**同一个 MSYS2 前缀**

`C:\\Users\\shock\\bin\\rsync` **不是二进制，是一个 261 字节的 bash shim**，真身在
`D:\\code\\msys2-rsync\\usr\\bin\\rsync.exe`。用户为此写的 `D:\\code\\msys2-rsync\\README.md`
记着三个坑，其中第三个是硬约束：

- 调 **Git 自带**的 `ssh.exe` → `dup() in/out/err failed`（两个 MSYS 运行时的管道不能互递）
- 调 **Windows 原生** `C:\\Windows\\System32\\OpenSSH\\ssh.exe` →
  `safe_read failed to read 4 bytes: Connection reset by peer`
- 只有「rsync 和 ssh 同前缀」能跑通

所以这里**不调 shim、不做 PATH 查找**：每次调用都把 `MSYS_BIN` 插到子进程 `PATH` 最前，
并用**绝对路径**调 `rsync.exe` / `ssh.exe`。shim 那句 `PATH=...` **绝不能**进系统 PATH
（会顶掉 Git 的 ssh，`git push` 会坏）—— 我们只影响自己的子进程。

`etc/fstab` 提供 cygdrive 映射 → **本地路径一律走 `to_msys()` 转成 `/d/...`**，
写 `D:/...` 会报 `change_dir ... failed`。`etc/nsswitch.conf` 的 `db_home: /c/Users/%U`
（已配好）→ 前缀自带的 ssh 才认 `~/.ssh/config` 里的 `42.192.51.77` 别名与密钥。

## ⚠️ 已知边界：笔记本拉不动时，服务器会退回纯文本

服务器磁盘能回收的**前提是本工具真的在拉**。本地空间不足而停拉、或笔记本连续离线超过
约 8 小时，服务器就会重新撞 `--min-free-gb 5` 并降级为纯文本（数据不丢，只是没有音视频）。

## 用法

    python service/pull_sessions.py --selftest          # 启用前必做：环境自检
    python service/pull_sessions.py --dry-run           # 只看计划，不动任何东西
    python service/pull_sessions.py --limit 3 --keep-remote   # 先拉 3 场、不删服务器
    python service/pull_sessions.py                     # 正式：拉 + 校验 + 删服务器那份
    python service/pull_sessions.py --only data/20261016/live_x_20261016_020000
    python service/pull_sessions.py --min-free-gb 20    # 本地空间低于 20G 就停拉并告警
"""
import argparse
import io
import json
import os
import subprocess
import sys
from datetime import datetime

# ── 复用服务器那份「清单 / 判定」实现（同一份代码 → 两侧算出来必然一致）─────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_ledger as ll          # noqa: E402  （dir_manifest / manifest_digest / 正则）
import cos_archive as ca          # noqa: E402  （session_key：COS 对象键**唯一**的口径）

# ⚠️ 与 `live_ledger.py` / `health_check.py` 同一条约定：幂等包装，各用各的标记。
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
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, 'service', 'dist')
MANIFEST = os.path.join(DIST, 'pulled_sessions.jsonl')     # 审计用，**不是**判据
LOCK = os.path.join(DIST, 'pull_sessions.lock')
LOG = os.path.join(DIST, 'pull_sessions.log')

DEFAULT_HOST = '42.192.51.77'
REMOTE_ROOT = '/opt/douyin-live'
DEFAULT_DEST = 'D:/shock/Documents/data'

# MSYS2 自洽前缀 —— 见模块顶部「必须来自同一个 MSYS2 前缀」
MSYS_PREFIX = r'D:\code\msys2-rsync'
MSYS_BIN = MSYS_PREFIX + r'\usr\bin'
RSYNC_EXE = MSYS_BIN + r'\rsync.exe'
SSH_EXE = MSYS_BIN + r'\ssh.exe'

# 半截数据先去这里，校验通过后再**原子改名**进正式目录 —— 于是中断留下的残缺目录
# 永远不会混进研究语料。必须在**同一个卷**上（否则 os.replace 不是原子的）。
STAGING = '_staging'
INCOMPLETE = '_incomplete'          # 下划线前缀 = 非生产，与 data/_load_test/ 同一约定

# ★ 顶层「文本/数值」文件的**已知集合**（2026-10-16 全库 82 场实测去重）。
#   一个场次目录顶层只有 `frames/` 与 `audio/` 两个子目录，其余全是下面这 8 种文件。
#   用途是 **fail-closed**：顶层冒出第 9 种名字 = 采集端加了新产物，而本工具
#   「顶层 = 文本、子目录 = 媒体」这条分界线可能已经不成立 → 那一场**不进删除清单**。
KNOWN_TOP_FILES = frozenset({
    'room.json', 'danmaku.jsonl', 'events.jsonl', 'series.json', 'series.jsonl',
    'fansclub_edges.jsonl', 'session_meta.json', 'ffmpeg_media.log',
})

# 本工具在**本地**场次目录里留的指针：这一场的媒体（frames/ + audio/）在 COS 上的位置。
# 双重避让（`_local_` 前缀 + 下划线开头）：将来服务器端若真产出同名文件也不会撞车，
# 而 `text_manifest()` 在**两侧**都会把它滤掉。
LOCAL_MARKER = '_local_cos_media.json'

# 服务器侧归档工具（在 `service/MANIFEST` 里）。**绝不带 `--delete-after-upload`** ——
# 带了就等于让服务器自行删场次，绕过笔记本这边「文本已经回本机了」那道门禁
# （单测有断言钉住这一点）。
COS_ARCHIVE = 'service/cos_archive.py'
COS_ARCHIVE_CMD = f'sudo -n ./venv/bin/python {COS_ARCHIVE}'
COS_RUN_TIMEOUT = 3600              # 一轮归档实测 18 GB ≈ 5 分钟，给足余量
COS_LEDGER_TIMEOUT = 300            # `--dump-ledger` 要逐条 HEAD，82 场 ≈ 8 秒
DEFAULT_TEXT_MAX_MB = 64.0          # 单场顶层文本合计的告警线（正常每场约 1 MB）
LISTS = '_lists'                    # `_staging/_lists/<场次>.txt`：rsync --files-from 的清单
                                    # （下划线前缀 + 每轮清理，不进 data/）

# ★ `--bwlimit` 是**兜底**，不是有效约束 —— 2026-10-16 实测它从来没起过作用。
#
# 真相是**服务器出口被带宽包硬卡在 ≈0.32 MB/s（2.56 Mbps）**：
# 裸 ssh 单流 0.32、rsync 0.30、并发 2/4/8 流的总量**一样是 0.32**（叠加不了），
# 而服务器 load 0.48、rsync 只占 0.1% 核、100 Mbps 网卡只用了 2.4% —— 不是 CPU、
# 不是网卡，是**实例的峰值带宽包**（腾讯云轻量 3 Mbps 档的典型值）。
#
# 另外这条链路**不与采集争带宽**：采集全是**入向**（ffmpeg 拉 FLV + WS 下行），
# 我们这次是**出向**，两个方向各算各的。所以「限速以免影响采集」这个理由不成立。
# 3m（3 MB/s ≈ 24 Mbps）留在代码里只当作「万一换了不限额的机器」的兜底。
#
# ⚠️ 由此得到的**产能结论**：0.32 MB/s ≈ **27.6 GB/天**，而流入实测 **68.6 GB/天**
# —— 这条链路**拉不完**（需 59.5 小时/天）。要跟上必须升级带宽包或减少传输量，
# 详见 `service/README.md`。这决定了「自动传输」只能是「尽力而为地排水」。
DEFAULT_BWLIMIT = '3m'
# 本地剩余空间的**地板**：低于它就停拉并告警。D: 盘很紧（实测只剩 49 GB），
# 所以这一步是常态而非异常路径。
DEFAULT_MIN_FREE_GB = 10.0

# rsync 的 ssh 选项。BatchMode=yes 很关键：让任何交互请求**失败退出而不是挂住** ——
# 否则计划任务会一直挂到锁超时，看起来像死锁。
SSH_OPTS = ('ssh -o BatchMode=yes -o ConnectTimeout=15 '
            '-o ServerAliveInterval=30 -o ServerAliveCountMax=4')

# rsync 退出码 → 我们怎么处理。**只有 0 可接受。**
RSYNC_RC = {
    0: '成功',
    24: '★源文件在传输中消失了（说明这场的写入还没真正settle，需要调大 --inflight-min）',
    23: '部分传输（有文件没传完）',
    10: 'socket I/O 错误', 11: '文件 I/O 错误', 12: 'rsync 协议错误',
    30: '超时', 31: '数据超时',
    255: 'ssh 连接/鉴权失败（含 BatchMode 下的交互请求）',
}

EXIT_OK, EXIT_FAIL, EXIT_NOSPACE, EXIT_LOCKED, EXIT_ENV = 0, 1, 2, 3, 4


# ================================================================ 纯逻辑（可单测）
def to_msys(p):
    """Windows 路径 → MSYS2 形式（`D:/x` / `D:\\x` → `/d/x`）。

    必须转：前缀的 `etc/fstab` 提供的是 cygdrive 映射，rsync 收到 `D:/...` 会报
    `change_dir ... failed: No such file or directory`。已经是 `/d/x` 的原样返回。
    """
    s = str(p).replace('\\', '/')
    if s.startswith('/'):
        return s
    if len(s) >= 2 and s[1] == ':':
        return '/' + s[0].lower() + s[2:]
    return s


def dest_rel(completeness, rel_dir):
    """服务器相对场次目录 → 本地相对目录。

    `('完整',   'data/20261016/live_x')` → `'20261016/live_x'`
    `('不完整', 'data/20261016/live_x')` → `'_incomplete/20261016/live_x'`

    ⚠️ `_incomplete` 以 `_` 开头，**不会被 `DATE_DIR_RE`（`^\\d{8}$`）匹配** ——
    与 `data/_load_test/`、`data/_test_residue/` 是同一约定，所以它既不会被服务器
    的 `scan_all_sessions()` 当成日期目录，也不会被本工具的本地清点当成年份目录。
    """
    parts = rel_dir.split('/')
    if len(parts) != 3 or parts[0] != 'data':
        raise ValueError(f'不是合法的场次相对目录：{rel_dir!r}')
    date, session = parts[1], parts[2]
    if completeness == '完整':
        return f'{date}/{session}'
    return f'{INCOMPLETE}/{date}/{session}'


def is_empty(fact):
    """全空场次：一条弹幕一个事件都没有、也没有音视频（`modal == '空'`，服务器给的）。

    这种场次**不传也不删** —— 搬过去只是 ~200 KB 的壳子，还污染语料。
    服务器上它们归既有的 `--delete-incomplete` 管（默认只吃「音频段与帧数都为 0」）。
    """
    return fact.get('modal') == '空'


def verify(remote_m, local_m, max_report=8):
    """服务器清单 vs 本地清单 → `(是否一致, 问题列表)`。

    ★ **不能只比总大小**（见 `live_ledger.dir_manifest` 的注释）：等长方差、缺一个多一个、
    同长度变长，都能骗过单一的总量指标。这里逐项比 **文件数 + 相对路径集合 +
    每文件字节 + 小文件 sha256 + 总字节 + 有无 .rsync-partial 残留**，
    把「集合正确」与「内容正确」**分开**验证。

    `hash_max_bytes` 两边必须一致 —— 它防的是**笔记本这份代码是旧的**（拉的是旧 checkout），
    那样两侧算法会悄悄各算各的。
    """
    problems = []
    if remote_m.get('hash_max_bytes') != local_m.get('hash_max_bytes'):
        problems.append(f"两侧 hash 阈值不一致（服务器 {remote_m.get('hash_max_bytes')} / "
                        f"本地 {local_m.get('hash_max_bytes')}）—— 本地代码可能是旧的")
        return False, problems

    if local_m.get('partial_dirs'):
        # 残留的 .rsync-partial 目录**本身就是「这份没传完」的信号**
        problems.append(f"本地残留未传完的目录：{local_m['partial_dirs'][:3]}")

    rfiles = {e['p']: e for e in remote_m['files']}
    lfiles = {e['p']: e for e in local_m['files']}
    if remote_m['count'] != local_m['count']:
        problems.append(f"文件数不符：服务器 {remote_m['count']} / 本地 {local_m['count']}")
    missing = sorted(set(rfiles) - set(lfiles))
    extra = sorted(set(lfiles) - set(rfiles))
    if missing:
        problems.append(f"本地缺 {len(missing)} 个文件，如 {missing[:3]}")
    if extra:
        problems.append(f"本地多 {len(extra)} 个文件（残留？），如 {extra[:3]}")
    if remote_m['total'] != local_m['total']:
        problems.append(f"总字节不符：服务器 {remote_m['total']} / 本地 {local_m['total']}")

    size_bad = [p for p in set(rfiles) & set(lfiles)
                if rfiles[p]['n'] != lfiles[p]['n']]
    if size_bad:
        problems.append(f"{len(size_bad)} 个文件字节数不符，如 "
                        f"{[(p, rfiles[p]['n'], lfiles[p]['n']) for p in size_bad[:3]]}")

    hash_bad = [p for p in set(rfiles) & set(lfiles)
                if rfiles[p]['h'] and rfiles[p]['h'] != lfiles[p]['h']]
    if hash_bad:
        problems.append(f"★ {len(hash_bad)} 个小文件内容不符（sha256），如 {hash_bad[:3]}")

    if remote_m['digest'] != local_m['digest'] and not problems:
        problems.append('摘要不符（但逐项都对？说明两侧算法漂移了，请核对代码版本）')
    return (not problems), problems[:max_report]


def text_manifest(m):
    """完整清单 → **只含顶层文本文件**的清单（媒体那部分被丢掉）。

    「顶层 = 文本」这条分界是实测出来的：场次目录顶层只有 `frames/` 与 `audio/` 两个
    子目录（全库 82 场去重），其余 8 种全是小文件（`KNOWN_TOP_FILES`）—— 顶层**只占
    全库体积的 0.5%**（85 MB / 17 GB），而公网出口实测只有 **0.33 MB/s**。于是
    「媒体走内网 COS（59 MB/s）、文本走公网回本机」这个二分既够快、又不会误伤。

    ★ 这一个函数**同时过滤两侧**（服务器的完整清单、本地目录清单）—— 必须对称。
    否则第二轮 `to_verify` 会因为本地多出一个 `_local_cos_media.json` 而恒判不一致，
    每一轮都报「本地多 1 个文件」，服务器那份**永远删不掉**。

    ★ `hash_max_bytes` **原样保留**：`verify()` 拿它判「本地这份代码是不是旧的」，
    这里若退化成 `None`，那个守卫会变成 `None == None` 而**静默失效**。
    """
    files = [e for e in m.get('files', [])
             if '/' not in e['p'] and e['p'] != LOCAL_MARKER
             and e['p'] in KNOWN_TOP_FILES]
    out = {
        'files': files,
        'count': len(files),
        'total': sum(e['n'] for e in files),
        'hash_max_bytes': m.get('hash_max_bytes'),
        # 顶层残留的未传完目录；`frames/.rsync-partial` 那种整段丢掉（属于媒体侧）
        'partial_dirs': [d for d in (m.get('partial_dirs') or []) if '/' not in d],
    }
    out['digest'] = ll.manifest_digest(out['files'])
    return out


def unknown_top_files(m):
    """顶层里**不在** `KNOWN_TOP_FILES` 的文件名（媒体子目录里的不算）。

    非空 = 采集端加了新产物 → 这一场**不拉也不删**（见 `_cycle`）。
    ⚠️ 正确的反应是**升级 `KNOWN_TOP_FILES` 并确认新产物该走哪条腿**，而不是把未知文件
    从**两侧**清单一起滤掉 —— 那样 `verify()` 与 `cos_ok()` 都会通过，服务器那份就带着
    这个文件被删了，而它从没被任何副本覆盖过。
    """
    bad = set()
    for e in m.get('files', []):
        p = e['p']
        if '/' in p or p == LOCAL_MARKER or p in KNOWN_TOP_FILES:
            continue
        bad.add(p)
    return sorted(bad)


def cos_why(rel, rec, fact):
    """COS 那份**是否已被证实**存在、且就是这一场 —— 删除的第二道门禁（媒体腿）。

    返回**中文原因**；全部成立时返回空串（= 放行）。`cos_ok()` 只是它的布尔投影 ——
    **判定逻辑只有这一份**，不会再漂移出第二个实现。

    必须**全部**成立才放行：

    - `verify == 'ok'` —— 曾经传成功过。但这只是**过去的回执**，见下一条。
    - **`object_present is True`** —— `--dump-ledger` **本轮刚 HEAD 过**。这条是灵魂：
      `verify=='ok'` 之后桶可能被清空、被生命周期规则删、被人手工删过，账本照样写着 ok。
      `cos_archive.py` 自己就**不信账本**（`_cycle` 的 1b 步要再 HEAD 一遍），而且那个
      1b 步在「本轮没有新东西要传」时**根本不执行**就返回 0 —— 所以它的退出码什么都
      证明不了。活证据只有 HEAD。
    - `key` 与 `session_key(rel)` 相等 —— 防账本张冠李戴
    - `bytes` / `files` 与**服务器此刻**的事实相等 —— 防「归档之后目录又变了」
    """
    if not rec:
        return 'COS 账本里没有这一场'
    if rec.get('verify') != 'ok':
        return f"账本该场 verify={rec.get('verify')!r}（不是 'ok'）"
    if rec.get('key') != ca.session_key(rel):
        return f"对象键不符：账本 {rec.get('key')!r} / 应为 {ca.session_key(rel)!r}"
    if rec.get('object_present') is not True:
        return '★ COS 上**找不到这个对象**（账本说传过，本轮 HEAD 却没命中）'
    if rec.get('bytes') != fact.get('bytes') or rec.get('files') != fact.get('files'):
        return (f"与服务器**此刻**不符：COS 记 {rec.get('bytes')}B/{rec.get('files')} 文件、"
                f"磁盘现在 {fact.get('bytes')}B/{fact.get('files')} 文件")
    return ''


def cos_ok(rel, rec, fact):
    """`cos_why()` 的布尔投影（空串 = 通过）。保留这个名字因为调用点读起来更清楚。"""
    return cos_why(rel, rec, fact) == ''


def marker_payload(rec):
    """`_local_cos_media.json` 的内容 —— 媒体在 COS 上的位置。

    **不含任何凭据**（账本里本来也没有）：它只说明去哪取那场 tar；取的时候用的是
    服务器上的 `config/cos.env`。
    """
    return {
        'key': rec.get('key', ''),
        'tar_bytes': rec.get('tar_bytes'),
        'tar_sha256': rec.get('tar_sha256'),
        'etag': rec.get('etag'),
        'bytes': rec.get('bytes'),
        'files': rec.get('files'),
        'digest': rec.get('digest'),
        'archived_ts': rec.get('ts'),
    }


def write_marker(dir_abs, rec):
    """在本地场次目录里写下媒体指针。

    **在 `verify()` 通过之后、`os.replace` 之前调用**（写进 staging）—— 于是正式目录
    从出现的第一刻起就是终态，永远不会存在「已经能看见、但还没有指针」的中间状态。
    """
    payload = marker_payload(rec or {})
    payload['note'] = (
        '本目录只有顶层文本；媒体（frames/ + audio/）在 COS 上的这个对象里。'
        '取回：下载该 tar 后 `tar -xf` 与本地目录合并。' if rec else
        '⚠️ 本轮 COS 账本里没有这一场 —— 媒体可能还没归档，**先别删服务器那份**')
    try:
        with open(os.path.join(dir_abs, LOCAL_MARKER), 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError as e:
        log(f'  ⚠️ 写 {LOCAL_MARKER} 失败（不影响文本落地）：{e}')


def gate(rels, facts_by_dir, cos_ledger):
    """合并后的**唯一**删除门禁 → `(deletable, pulled_only)`。

    ★ 必须**只算一次**、且算在 `to_pull` 与 `to_verify` **两批合并之后** ——
    绝不能在两条路径里各写一份判断。上一轮以 `--keep-remote` 落地的场次本地**只有文本**，
    本轮走 `to_verify`；那条路径若漏了 COS 门禁，它就会被删，而 COS 那份**从没被检查过**
    —— 结果从「本地文本 + 服务器整场 + COS 整场」变成「本地文本 + 一个不知道还在不在的对象」。

    `deletable` 的元素是**删除清单的一行**（`rel\\t字节\\t文件数`）：带期望列，服务器会在
    **它自己重扫之后**复核，对不上即整批拒绝 —— 这是唯一能在「删除那一刻」关掉 TOCTOU
    窗口的地方（计划 §三 ③）。

    :param rels: 文本腿已校验通过的场次目录（`done`，两部分合并）
    :param facts_by_dir: `{rel: fact}` —— ★ **必须是刚取的**（TOCTOU 复核用它的 bytes/files）
    :param cos_ledger: `{rel: rec}`（`--dump-ledger` 产出，含本轮活 HEAD 的 `object_present`）
    """
    deletable, pulled_only = [], []
    for rel in rels:
        f = facts_by_dir.get(rel)
        if f is None:
            pulled_only.append((rel, '服务器上已找不到这一场（被别处删了？）'))
            continue
        why = cos_why(rel, cos_ledger.get(rel), f)
        if why:
            pulled_only.append((rel, why))
            continue
        deletable.append(f'{rel}\t{f["bytes"]}\t{f["files"]}')
    return deletable, pulled_only


def plan_cycle(facts, inv, only=''):
    """逐场次事实 + 本地清点 → `(to_pull, to_verify, skipped)`。

    - `采集中` → 跳过（**绝不碰正在写的场次**）
    - **非「完整」→ 跳过**（★ 用户 2026-10-16 定的口径：本工具只收完整场次）
    - 本地已有同名目录 → 不进 `to_pull`，但要**本轮重新校验**后才能删服务器那份
    - 其余 → `to_pull`，按开播时间**正序**（先拉旧的，避免最新那场反复抢在前面）
    - `only` 非空时只处理那一场（手工排查用）

    ★ **为什么「只收完整」而不是「排除不完整」**：这是白名单，不是黑名单。
    `classify_session()` 将来若多出一种判定（新故障、新模态），白名单会让它**默认
    落在「不拉也不删」**这一侧 —— 而黑名单会把它当「可以拉」处理。删除是唯一
    破坏性动作，默认值必须偏保守。

    ★ 连带后果：**全空场次（`modal == '空'`）不用单独判**。它们在 `classify_session()`
    里走的是「无内容」那条分支 → `不完整`，天然被这道白名单挡下（`is_empty()` 因此
    不再在这里出现）。而空场次的回收本来就是 `live_ledger --delete-incomplete` 的
    既有职责，本工具不越界 —— 见计划 §四「删掉 `--reap-empty`」。

    :param inv: `{相对场次目录: 本地绝对路径}`（`inventory_local()` 产出）
    """
    to_pull, to_verify, skipped = [], [], []
    for f in sorted(facts, key=lambda x: x.get('started') or ''):
        rel = f['dir']
        if only and rel != only:
            continue
        if f['completeness'] == '采集中':
            skipped.append((rel, '采集中（正在写，绝不碰）'))
            continue
        if f['completeness'] != '完整':
            # 理由带上服务器给的原因（`无音视频…` / `未收尾…`），省得再 ssh 查一遍。
            why = f.get('reason') or f['completeness']
            skipped.append((rel, f'{f["completeness"]}（本工具只收「完整」场次：{why}）'))
            continue
        if rel in inv:
            to_verify.append(f)
        else:
            to_pull.append(f)
    return to_pull, to_verify, skipped


def inventory_local(dest):
    """扫本地目标根 → `{相对场次目录: 本地绝对路径}`。

    认两种布局：`<dest>/<8位日期>/live_*` 与 `<dest>/_incomplete/<8位日期>/live_*`。
    `_staging` 与 `_incomplete` 都以 `_` 开头，所以日期目录用 `DATE_DIR_RE` 卡死 ——
    这也保证了 `_staging` 里那些半截目录**不会**被误认为「已经在本地了」。
    """
    inv = {}
    if not os.path.isdir(dest):
        return inv
    for name in sorted(os.listdir(dest)):
        full = os.path.join(dest, name)
        if not os.path.isdir(full):
            continue
        if ll.DATE_DIR_RE.match(name):
            _collect(inv, full, name)
        elif name == INCOMPLETE:
            for d2 in sorted(os.listdir(full)):
                if ll.DATE_DIR_RE.match(d2):
                    _collect(inv, os.path.join(full, d2), d2)
    return inv


def _collect(inv, date_dir, date):
    for sess in sorted(os.listdir(date_dir)):
        p = os.path.join(date_dir, sess)
        if sess.startswith('live_') and os.path.isdir(p):
            inv[f'data/{date}/{sess}'] = p


def free_gb(path):
    """本地剩余空间（GB）；量不到返回 `None`。

    ⚠️ 按 `health_check.disk_free_gb()` 的约定：**量不到是第三种状态** ——
    既不能当 0（会被读成「磁盘满了」），也不能当「充足」而硬拉。
    `--min-free-gb` 那道闸门对 None 的处理是**拒绝拉取并报出来**（fail-closed）。
    """
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        import shutil
        return shutil.disk_usage(probe).free / 1e9
    except OSError:
        return None


# ================================================================ 副作用
def child_env():
    """子进程环境：把 MSYS2 前缀的 bin 插到 PATH **最前**。

    只影响我们起的子进程；**绝不改系统 PATH**（会顶掉 Git 的 ssh）。
    这一步也是「rsync 与 ssh 同前缀」的保证 —— rsync 自己 spawn ssh 时走的就是它。
    """
    env = dict(os.environ)
    env['PATH'] = MSYS_BIN + os.pathsep + env.get('PATH', '')
    return env


def run(argv, timeout=1800, stdin_text=None):
    """跑一个子进程 → `(rc, stdout文本, stderr文本)`。**argv 列表、绝不 shell=True**"""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           env=child_env(),
                           input=(stdin_text.encode('utf-8') if stdin_text is not None else None))
    except subprocess.TimeoutExpired:
        return 124, '', f'超时（{timeout}s）'
    except FileNotFoundError as e:
        return 127, '', f'找不到可执行文件：{e}'
    return (p.returncode,
            p.stdout.decode('utf-8', 'replace'),
            p.stderr.decode('utf-8', 'replace'))


def ssh(host, remote_cmd, stdin_text=None, timeout=300):
    """远程执行。命令作为**单个 argv 元素**交给 ssh，由远端 shell 解释 —— 本地无 shell 参与。

    `/opt/douyin-live` 整树是 root:root，而登录用户是 `ubuntu`，所以**要改动的**命令
    一律 `sudo -n`（免密）；只读命令（dump-facts / manifest / rsync 读）不需要。

    ⚠️ 但「只读」不等于「不要 sudo」：`cos_archive.py --dump-ledger` 虽然不写任何东西，
    却要读 `config/cos.env`（`root:root 600`）去 HEAD 对象 —— 不给 sudo 就**读不到凭据**，
    工具会 fail-closed 地把全部对象报成「不在」（`EXIT_ENV`），于是**一场都删不掉**。
    所以那条命令走 `COS_ARCHIVE_CMD`（带 `sudo -n`）。
    """
    return run([SSH_EXE, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                host, remote_cmd], timeout=timeout, stdin_text=stdin_text)


def remote_facts(host, inflight_min):
    rc, out, err = ssh(host, f'cd {REMOTE_ROOT} && ./venv/bin/python '
                             f'service/live_ledger.py --dump-facts - '
                             f'--inflight-min {inflight_min}')
    if rc != 0:
        raise RuntimeError(f'取场次事实失败（rc={rc}）：{err.strip()[:400]}')
    return json.loads(out)


def remote_manifest(host, rel):
    rc, out, err = ssh(host, f'cd {REMOTE_ROOT} && ./venv/bin/python '
                             f'service/live_ledger.py --manifest {rel}')
    if rc != 0:
        raise RuntimeError(f'取清单失败 {rel}（rc={rc}）：{err.strip()[:400]}')
    return json.loads(out)


def write_file_list(path, names):
    """把文件名写成 rsync `--files-from` 的清单：**NUL 分隔** + `newline=''`。

    ★ Windows 上 `open()` 的文本模式会把 `\\n` 写成 `\\r\\n`，而 rsync 会把 `\\r`
    当成**文件名的一部分** —— 轻则 rc 23，重则**静默少传**（少拉几个文件，
    `verify()` 事后能逮住，但那一趟公网白跑）。两个都要：`newline=''` 关掉转换、
    `--from0` 让 rsync 按 NUL 切。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        for n in names:
            fh.write(n + '\x00')


def cleanup_lists(lists_dir):
    """清掉本轮写在 `_staging/_lists/` 的 rsync 清单（一场一个临时文件）。

    不靠 `shutil.rmtree` —— 只在 `os` 层面逐个删，任何一项失败都吞掉：
    这是**收尾动作**，它失败绝不能影响「本轮删没删」的判定。
    """
    try:
        names = os.listdir(lists_dir)
    except OSError:
        return
    for n in names:
        try:
            os.remove(os.path.join(lists_dir, n))
        except OSError:
            pass


def rsync_pull(host, rel, staging_abs, bwlimit, timeout, names, list_path):
    """把 `REMOTE_ROOT/<rel>/` 的**顶层那 8 种文件**拉到本地 staging。

    **一次只搬一场**（要逐场次校验+删），**且只搬 `names` 里点名的文件**。

    ★ 为什么只搬顶层：服务器的媒体在 COS 里（内网 59 MB/s、免费），而公网出口
    实测被带宽包卡在 **0.33 MB/s**。顶层文本只占全库 **0.5%**，所以这条腿才拉得动；
    把 `frames/`/`audio/` 也带上就是 **68.6 GB/天**，永远追不上、也就永远删不掉
    （CLAUDE.md 磁盘段的实测）。

    ★ 三个「不嵌套」的保证，缺一不可（真机 `--dry-run` 验证过）：
      - `--files-from=<清单>` —— 文件名**直接落在目标目录**（`--files-from` 隐含 `-R`）
      - `--no-relative`      —— 显式关掉 `-R`，别指望上一条
      - 清单里只写**裸文件名**，不带 `rel/` 前缀
    对照：不加 `--files-from` 的 `-r` 会把 `audio/` 整个拉进来。所以 `-r` 必须去掉
    （`--files-from` 隐含的是 `-d` 不是 `-r`，列的都是普通文件，不需要递归）。
    """
    os.makedirs(staging_abs, exist_ok=True)
    write_file_list(list_path, names)
    argv = [RSYNC_EXE,
            '-t',                             # 保 mtime。不用 -a（含 -o -g -D，Windows 侧只添噪声）
            '--checksum',                     # 干掉「size+mtime 相同」的假命中（staging 可能有半截残留）
            '--partial', '--partial-dir=' + ll.PARTIAL_DIR,
            '--timeout=120',                  # **单次 I/O 停顿**上限，不是总时长（总时长由 timeout 兜底）
            '--bwlimit=' + bwlimit,
            '--numeric-ids', '--no-motd', '--info=stats1',
            '--from0', '--no-relative',
            '--files-from=' + to_msys(list_path),   # ★ 必须 to_msys()，漏了 rsync 就找不到这个文件
            '-e', SSH_OPTS,
            # 源带尾斜杠 = 拷内容而不是拷目录本身
            f'{host}:{REMOTE_ROOT}/{rel}/',
            to_msys(staging_abs) + '/']
    return run(argv, timeout=timeout)


def remote_cos_ledger(host):
    """ssh → `cos_archive.py --dump-ledger -` → `{相对场次目录: 记录}`。

    ★ **fail-closed**：命令不存在（服务器还没部署新版）、超时、rc≠0、不是合法 JSON、
    不是对象 —— **一律返回 `{}` 并告警**。空账本 ⇒ 没有一场能过 `cos_ok()` ⇒
    **本轮什么都不删**。「查不到 COS 上有没有」和「COS 上没有」在删除这件事上
    必须给出**同一个答案**。

    ★ 记录里的 `object_present` 是**本轮刚 HEAD 出来的活证据**，不是账本自述 ——
    见计划 §三 ②：`cos_archive.py` 自己都不信账本（它在 1b 步重新 HEAD），而且
    那个 1b 步在「所有场次都已归档」时位于 `if not todo: return EXIT_OK` **之后、
    根本不执行** —— 最需要确认对象还在的场景里它一次 HEAD 都不做就返回 0。
    所以它的退出码什么都证明不了，只有这里 HEAD 出来的 `object_present` 算数。
    """
    rc, out, err = ssh(host, f'cd {REMOTE_ROOT} && {COS_ARCHIVE_CMD} --dump-ledger -',
                       timeout=COS_LEDGER_TIMEOUT)
    if rc != 0:
        log(f'⚠️ 取 COS 账本失败（rc={rc}）：{err.strip()[:300]}')
        log('⚠️ 按「还不知道 COS 上有没有」处理 → **本轮不删任何场次**')
        return {}
    try:
        led = json.loads(out)
    except ValueError:
        log(f'⚠️ COS 账本不是合法 JSON（前 200 字）：{out.strip()[:200]}')
        log('⚠️ 同上 → **本轮不删任何场次**')
        return {}
    if not isinstance(led, dict):
        log(f'⚠️ COS 账本不是 JSON 对象（是 {type(led).__name__}）→ **本轮不删任何场次**')
        return {}
    return led


def run_cos_archive(host):
    """让服务器把「已完整、还没归档」的场次打 tar 传进 COS（内网 59 MB/s、免费）。

    ★ **命令里绝不能有 `--delete-after-upload`**：那会让服务器**自己**删掉源目录，
    完全绕过笔记本这边的三道门禁。只有笔记本知道「文本真的回到本机了」——
    服务器不知道。删除一律由笔记本发起（下面那个 assert 不是注释，是硬断言）。

    ★ **不看退出码**：`--only` 打错、该场被 `plan_cycle` 跳过、甚至 `EXIT_LOCKED(3)`
    （上一轮还在跑，**什么都没做**）**全都静默返回 0**。退出码只写进日志，
    「能不能删」一律由随后的 `remote_cos_ledger()`（活 HEAD）说了算。
    """
    cmd = f'cd {REMOTE_ROOT} && {COS_ARCHIVE_CMD}'
    assert '--delete-after-upload' not in cmd, \
        'run_cos_archive 不许带 --delete-after-upload（会让服务器绕过笔记本的门禁自删）'
    return ssh(host, cmd, timeout=COS_RUN_TIMEOUT)


def remote_delete(host, lines, inflight_min, apply=True, label='已传输'):
    """把已校验通过的场次清单交给服务器删（清单走 **stdin**，服务器上不落临时文件）。

    `lines` 每行可以是 `rel`，也可以是 `rel\\t字节\\t文件数`。带期望列时服务器会在
    **它自己重扫之后**用 `_dir_stat()` 复核，对不上即 **fail-closed 整批拒绝**
    （`live_ledger.resolve_delete_list()` 实现）。这是唯一能在**删除那一刻**关掉
    TOCTOU 窗口的地方 —— 我们的数字取自本轮开头的 `--dump-facts`，而删除可能在
    几十分钟之后才执行（见计划 §三 ③）。
    """
    cmd = (f'cd {REMOTE_ROOT} && sudo -n ./venv/bin/python service/live_ledger.py '
           f'--delete-list - --inflight-min {inflight_min} '
           f'--expect-count {len(lines)} --label {label}'
           + (' --apply' if apply else ''))
    return ssh(host, cmd, stdin_text='\n'.join(lines) + '\n')


def load_manifest():
    """读审计账本 → `{相对场次目录: 记录}`。**只作审计**，不作「已拉取」的判据。"""
    out = {}
    try:
        with open(MANIFEST, encoding='utf-8') as fh:
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


def append_manifest(rec):
    try:
        os.makedirs(DIST, exist_ok=True)
        with open(MANIFEST, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError as e:
        log(f'⚠️ 审计账本写入失败：{e}')


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

    ⚠️ 「过期」的判据是**锁文件多久没被更新**（心跳），不是「锁建于多久之前」。
    这不是洁癖 —— 2026-10-16 实测服务器出口被带宽包卡在 **0.32 MB/s**，
    于是一场 1.3 GB 要拉 **≈70 分钟**，全量首拉要**几小时**。若按「建于多久前」判，
    长传输跑到 90 分钟就会被第二个实例判定为「死锁」并接管，
    两个进程同时往**同一个 staging 目录**写同一场 → 半截数据 + 校验失败 + 白拉。
    所以 `touch_lock()` 每完成一场就刷新一次 mtime，「过期」于是变成
    「**连续 N 分钟一场都没拉完**」—— 那才是真死（进程没了或挂在网络上）。

    心跳间隔 = 单场耗时，最坏是最大那场（实测 1.3 GB ≈ 70 分钟）；
    默认 180 分钟给了 2.5 倍余量。
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
    """心跳：每完成一场刷新锁的 mtime。让「过期」=「很久没进展」，而不是「开了很久」。

    ⚠️ 只碰 mtime，**不改内容** —— 里面记的是持有者的 pid 与起始时刻，
    那是排查「谁在跑」用的，被心跳冲掉就没用了。
    """
    try:
        os.utime(LOCK, None)
    except OSError:
        pass


def release_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


# ================================================================ 自检
def selftest(host, dest):
    """启用前必做。**只有全绿才该打开计划任务。**"""
    ok = True

    def chk(name, cond, extra=''):
        nonlocal ok
        print(('  ✅ ' if cond else '  ❌ ') + name + (f'  {extra}' if extra else ''))
        if not cond:
            ok = False

    print('1) 本地 rsync / ssh 与版本')
    rc, out, err = run([RSYNC_EXE, '--version'], timeout=60)
    chk('rsync.exe 可执行', rc == 0, (out or err).splitlines()[0] if (out or err) else '')
    rc2, out2, err2 = run([SSH_EXE, '-V'], timeout=60)
    chk('ssh.exe 可执行', rc2 == 0, (out2 or err2).strip()[:60])
    chk('★ rsync 与 ssh 来自同一个 MSYS2 前缀',
        os.path.dirname(RSYNC_EXE) == os.path.dirname(SSH_EXE), os.path.dirname(RSYNC_EXE))
    chk('本地是 MSYS2 形式 而非 D:/ 形式', to_msys(dest).startswith('/'),
        to_msys(dest))

    print('2) 远端可达 + 免密 + sudo')
    rc, out, err = ssh(host, 'id -un; hostname', timeout=60)
    chk('ssh 免密登录（且别名生效）', rc == 0 and out.strip().splitlines()[0:1] == ['ubuntu'],
        out.strip().replace('\n', ' / ') or err.strip()[:120])
    rc, out, err = ssh(host, 'sudo -n true && echo SUDO_OK', timeout=60)
    chk('sudo -n 免密（删除需要 root）', rc == 0 and 'SUDO_OK' in out, err.strip()[:120])

    print('3) 远端 rsync 与台账版本')
    rc, out, err = ssh(host, '/usr/bin/rsync --version | head -1', timeout=60)
    chk('远端 rsync 可执行', rc == 0, out.strip()[:60])
    rc, out, err = ssh(host, f'cd {REMOTE_ROOT} && ./venv/bin/python '
                             f'service/live_ledger.py --dump-facts - | head -c 1', timeout=120)
    chk('★ 远端 live_ledger 已支持 --dump-facts（否则要重新 pack.sh + 上传）',
        rc == 0 and out.strip() == '[',
        '返回 ' + repr(out[:40]) + ' —— 若报「unrecognized arguments」说明新版还没部署')

    print('4) 真正拉一个文件（这一步才算「能通过 ssh 拉到服务器文件」）')
    try:
        facts = remote_facts(host, 15)
    except Exception as e:
        chk('取场次事实', False, str(e)[:200])
        return ok
    chk('取场次事实', True, f'{len(facts)} 场')
    if not facts:
        chk('至少有一场可测', False, '服务器 data/ 是空的')
        return ok
    probe = facts[0]['dir']
    rm = remote_manifest(host, probe)
    chk('取单场清单', rm.get('count', -1) >= 0,
        f"count={rm.get('count')} total={rm.get('total')}")
    chk('★ 远端清单算法与本地同一份代码（hash 阈值一致）',
        rm.get('hash_max_bytes') == ll.MANIFEST_HASH_MAX,
        f"远端 {rm.get('hash_max_bytes')} / 本地 {ll.MANIFEST_HASH_MAX}")

    print('5) 本地目标根')
    g = free_gb(dest)
    chk('目标根可写', os.access(dest, os.W_OK) or os.access(os.path.dirname(dest), os.W_OK),
        dest)
    chk('剩余空间量得出来', g is not None, f'{g:.1f} GB' if g is not None else '量不到')

    print('6) ★ 远端 COS 账本（--dump-ledger）—— **这一关不过就绝不许开删除**')
    # 部署顺序是「先服务器后笔记本」：笔记本先拿到新版就会在这里报
    # 「unrecognized arguments」，而 `remote_cos_ledger()` 会把异常/空账本
    # fail-closed 成「一场都不删」—— 所以这一关是**功能可用性**的自检，
    # 不是可有可无的体检项。
    rc, out, err = ssh(host, f'cd {REMOTE_ROOT} && {COS_ARCHIVE_CMD} --dump-ledger -',
                       timeout=COS_LEDGER_TIMEOUT)
    chk('★ 远端 cos_archive 已支持 --dump-ledger（否则要重新 pack.sh + 上传）',
        rc == 0, f'rc={rc} ' + (err.strip()[:160] or '')
        + ('  —— 若报「unrecognized arguments」说明新版还没部署到服务器' if rc != 0 else ''))
    led = None
    if rc == 0:
        try:
            led = json.loads(out)
        except ValueError:
            led = None
    chk('★ --dump-ledger 输出是合法 JSON 对象（stdout 不许混日志）',
        isinstance(led, dict), f'{type(led).__name__}，前 120 字：{out.strip()[:120]}')
    if isinstance(led, dict):
        okrec = [d for d, r in led.items() if r.get('verify') == 'ok']
        present = [d for d in okrec if led[d].get('object_present') is True]
        if not okrec:
            # 服务器被排空后账本覆盖度必然为 0 —— **这是正常状态**，不该判失败。
            print(f'  ℹ️ 账本里没有 verify==ok 的场次（{len(led)} 条）；'
                  f'服务器刚起步或被排空都长这样，不算错')
        else:
            chk('★ COS 上逐条 HEAD 都能命中（object_present 全真）',
                len(present) == len(okrec),
                f'{len(present)}/{len(okrec)} 命中'
                + (f'；未命中：{sorted(set(okrec) - set(present))[:5]}' if len(present) != len(okrec)
                   else ''))
        print(f'  ℹ️ 账本 {len(led)} 条，其中 verify==ok {len(okrec)} 条')
    return ok


# ================================================================ main
def main():
    ap = argparse.ArgumentParser(
        description='把服务器上采完的场次拉到笔记本（结构与服务器 data/ 一致）')
    ap.add_argument('--host', default=DEFAULT_HOST)
    ap.add_argument('--dest', default=DEFAULT_DEST, help='本地目标根')
    ap.add_argument('--inflight-min', type=int, default=15,
                    help='服务器侧：目录内最新文件在这个分钟内被写过 → 视为采集中（不拉）')
    ap.add_argument('--min-free-gb', type=float, default=DEFAULT_MIN_FREE_GB,
                    help='本地剩余空间地板；低于它**停止拉取并告警**（本工具永不删本地数据）')
    ap.add_argument('--bwlimit', default=DEFAULT_BWLIMIT,
                    help='rsync 限速（默认 3m ≈ 24 Mbps，见模块顶部实测依据）')
    ap.add_argument('--limit', type=int, default=0,
                    help='本轮最多处理几场（0 = 不限）。首次上线建议 3')
    ap.add_argument('--only', default='', help='只处理这一场（相对场次目录）')
    ap.add_argument('--keep-remote', action='store_true',
                    help='★退路：拉 + 校验，但**永不删**服务器那份')
    ap.add_argument('--cos', dest='cos', action='store_true', default=True,
                    help='本轮开头先让服务器把整场打包传 COS（默认开；内网 59 MB/s、免费）。'
                         '媒体在 COS、文本在本机 —— 两腿都到位才允许删服务器那份')
    ap.add_argument('--no-cos', dest='cos', action='store_false',
                    help='本轮不碰 COS（给「以后服务器自己挂 timer 归档」用）。'
                         '★ 此时 COS 账本仍会被读来做门禁 —— 没有账本/对象不在就一场都不删')
    ap.add_argument('--text-max-mb', type=float, default=DEFAULT_TEXT_MAX_MB,
                    help=f'单场顶层文本合计超过它就**告警**（默认 {DEFAULT_TEXT_MAX_MB:g} MB）。'
                         '⚠️ 只告警、照拉不误：超限只是慢，拦住反而让这场在服务器上'
                         '永远删不掉（媒体在 COS，文本没人拉）')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划，不拉也不删')
    ap.add_argument('--selftest', action='store_true', help='环境自检后退出')
    ap.add_argument('--quiet', action='store_true', help='不往 stdout 打（仍写日志文件）')
    ap.add_argument('--lock-min', type=int, default=180,
                    help='锁多少分钟没心跳才算过期（据此接管）。默认 180 = 单场最坏 '
                         '70 分钟的 2.5 倍；**不要调到单场耗时以下**，否则长传输会被'
                         '第二个实例接管、两边写同一个 staging')
    ap.add_argument('--rsync-timeout', type=int, default=1800, help='单场 rsync 总时长上限（秒）')
    args = ap.parse_args()

    global _LOG_QUIET
    _LOG_QUIET = args.quiet

    if args.selftest:
        print(f'自检：host={args.host}  dest={args.dest}')
        return EXIT_OK if selftest(args.host, args.dest) else EXIT_ENV

    if not acquire_lock(args.lock_min):
        log('上一轮还在跑（锁未过期）→ 本轮跳过')
        return EXIT_LOCKED
    try:
        return _cycle(args)
    finally:
        release_lock()


def _cycle(args):
    # ---- 1. 本地空间闸门：**先看再动手** ----
    # 这条是常态而非异常路径（D: 盘实测只剩 49 GB，而流入 ≈68.6 GB/天）。
    g = free_gb(args.dest)
    if g is None:
        log(f'❌ 量不出 {args.dest} 的剩余空间 → 为安全起见**不拉取、不删除**')
        return EXIT_ENV
    if g < args.min_free_gb:
        log(f'❌ 本地剩余 {g:.1f} GB < 阈值 {args.min_free_gb} GB → '
            f'**停止拉取**（本工具永不自动删本地数据；请自己挪盘/扩盘后再启用）')
        return EXIT_NOSPACE

    # ---- 2. 先让服务器归档（**整场** → COS，内网 59 MB/s、免费）----
    # 顺序不能反：COS 那份是删除的**第二道门禁**，得先存在才有得校验。
    # ★ `--dry-run` 下**跳过这一步** —— 归档是个写动作（要花内网带宽、CPU、写 COS），
    #   而 `--dry-run` 的约定是「什么都不做」。代价：它的「可删」数是**保守下限**，
    #   本轮刚采完、还没归档的场次进不来（要等真跑一轮）。
    if args.dry_run:
        log('--dry-run：**不**让服务器归档（归档是写动作）→ 下面「可删」是保守下限')
    elif args.cos:
        log('→ 让服务器把未归档的场次打包传 COS（内网；工具本身**不删**服务器那份）…')
        rc, out, err = run_cos_archive(args.host)
        # ★ **不看 rc**：`--only` 打错 / 该场被跳过 / `EXIT_LOCKED(3)`（上一轮还在跑，
        #   什么都没做）**全都返回 0** —— 见 `run_cos_archive` 的 docstring。
        #   真伪一律由随后 `--dump-ledger` 的活 HEAD 决定。
        for ln in (err or out).strip().splitlines()[-4:]:
            log(f'    cos_archive[{rc}]: {ln}')
        touch_lock()
    else:
        log('--no-cos：本轮不让服务器归档（仍会读账本做删除门禁）')

    # ---- 3. 取事实 + COS 账本 + 本地清点 ----
    try:
        facts = remote_facts(args.host, args.inflight_min)
    except Exception as e:
        log(f'❌ {e}')
        return EXIT_ENV
    # ★ fail-closed：取不到账本就返回 `{}` → 每一场都过不了 `cos_ok()` → **一场都不删**。
    #   「查不到 COS 上有没有」必须与「COS 上没有」给出同一个答案（计划 §三 ②）。
    cos_ledger = remote_cos_ledger(args.host)
    inv = inventory_local(args.dest)
    fby = {f['dir']: f for f in facts}
    to_pull, to_verify, skipped = plan_cycle(facts, inv, args.only)

    # 审计：账本说拉过、本地却没有（用户挪走了？）—— **不改状态、不重下**，
    # 但要让「服务器那份已删、本地也不见了」这种**唯一副本丢失**被看见。
    audit = load_manifest()
    for rel in list(audit):
        if rel not in inv and rel in fby:
            log(f'⚠️ 账本记录已拉取，但本地不存在：{rel}（是你挪走了吗？）')

    if args.limit > 0:
        # ★ 只截 `to_pull`，**不截 `to_verify`** —— 首轮 `--limit 3` 仍可能触发大量
        #   `to_verify` 删除（符合门禁，但要知道：`--limit` 不是「最多删 3 场」）。
        to_pull = to_pull[:args.limit]
    log(f'服务器 {len(facts)} 场（完整 {sum(1 for f in facts if f["completeness"] == "完整")}）；'
        f'待拉 {len(to_pull)}、本地已有待校验 {len(to_verify)}、跳过 {len(skipped)}；'
        f'本地剩余 {g:.1f} GB；COS 账本 {len(cos_ledger)} 条')
    for rel, why in skipped:
        log(f'  · 跳过 {rel}：{why}')

    if args.dry_run:
        # 用**已经拿到的** facts + 账本算一遍门禁 —— 不再为了 dry-run 逐场 ssh 一遍
        # （那要多 82 次往返）。结论就是「本轮真跑的话会删哪些」。
        would_del, would_keep = gate([f['dir'] for f in to_pull + to_verify], fby, cos_ledger)
        for f in to_pull:
            log(f'  · 将拉 {f["dir"]}（整场 {f["bytes"] / 1e6:.1f} MB，'
                f'只拉其中的顶层文本）')
        for f in to_verify:
            log(f'  · 已在本地，将校验文本后删服务器那份 {f["dir"]}')
        log(f'  → 若真跑：可删 {len(would_del)} 场；文本过了但 **COS 门禁没过、会保留** '
            f'{len(would_keep)} 场'
            + (f'（{would_keep[:5]}）' if would_keep else ''))
        log('（--dry-run，什么都没做）')
        return EXIT_OK

    # ---- 4. 逐场次：取清单 → 只拉顶层文本 → 校验 → 写指针 → 移入 ----
    done, failed = [], []
    lists_dir = os.path.join(args.dest, STAGING, LISTS)
    cleanup_lists(lists_dir)          # 上一轮崩在半路留下的清单，先清掉（自愈）
    for f in to_pull:
        rel = f['dir']
        try:
            local_abs = os.path.join(args.dest, dest_rel(f['completeness'], rel))
            staging = os.path.join(args.dest, STAGING, dest_rel(f['completeness'], rel))
            # ★ 传输**前**先取一次清单：这一次是为了拿到「该拉哪些文件名」，
            #   顺带在动手之前就把「顶层有未知文件」挡下来（fail-closed，F10）。
            rm0 = remote_manifest(args.host, rel)
            unk = unknown_top_files(rm0)
            if unk:
                log(f'  ❌ {rel} 顶层出现未知文件 {unk[:5]} → **不拉也不删**'
                    f'（先确认新产物该走哪条腿，再升级 KNOWN_TOP_FILES）')
                failed.append(rel)
                continue
            tm0 = text_manifest(rm0)
            names = [e['p'] for e in tm0['files']]
            if not names:
                log(f'  ❌ {rel} 顶层没有任何已知文本文件 → 不拉（服务器那份保留）')
                failed.append(rel)
                continue
            mb = tm0['total'] / 1e6
            if mb > args.text_max_mb:
                # ★ 只告警、照拉不误：超限只是慢。拦住反而让这场在服务器上**永远删不掉**
                #   （媒体在 COS，文本没人拉 = 服务器那份是唯一副本）。
                log(f'  ⚠️ {rel} 顶层文本 {mb:.1f} MB > --text-max-mb '
                    f'{args.text_max_mb:g} MB（**仍照拉**）')
            log(f'↓ {rel}（顶层 {len(names)} 文件 / {mb:.1f} MB；整场 {f["bytes"] / 1e6:.1f} MB）')
            rc, out, err = rsync_pull(args.host, rel, staging, args.bwlimit,
                                      args.rsync_timeout, names,
                                      os.path.join(lists_dir, rel.replace('/', '_') + '.txt'))
            if rc != 0:
                log(f'  ❌ rsync 失败 rc={rc}：{RSYNC_RC.get(rc, "未知")}  {err.strip()[:200]}')
                failed.append(rel)
                continue
            # ★ 清单取在**传输之后** —— 这样比对的是「本地 == 服务器**此刻**」。
            #   传输期间目录若被改动（僵尸进程追加 / 被别处删），两边必然对不上 → 拒绝删。
            rm = remote_manifest(args.host, rel)
            unk = unknown_top_files(rm)
            if unk:
                log(f'  ❌ {rel} 传输期间顶层冒出未知文件 {unk[:5]} → 不删服务器那份')
                failed.append(rel)
                continue
            lm = ll.dir_manifest(staging)
            # ★ 两侧用**同一个** `text_manifest()` 过滤 —— 对称。否则第二轮本地多出的
            #   `_local_cos_media.json` 会让校验恒判不一致，服务器那份永远删不掉。
            good, problems = verify(text_manifest(rm), text_manifest(lm))
            if not good:
                log('  ❌ 校验不过，**保留服务器那份**，留待下一轮重试：')
                for p in problems:
                    log(f'       - {p}')
                failed.append(rel)
                continue
            # 原子改名进正式目录（同卷 → os.replace 是原子的）。
            os.makedirs(os.path.dirname(local_abs), exist_ok=True)
            if os.path.exists(local_abs):
                log(f'  ❌ 目标已存在且与预期不符 → **拒绝覆盖**：{local_abs}')
                failed.append(rel)
                continue
            # ★ 指针写进 **staging**（verify 已通过、改名之前）→ 正式目录从出现的第一刻
            #   就是终态，永远不存在「已经能看见、但还没有指针」的中间状态。
            #   `lm` 是在这之前算的，所以刚写的指针不会污染上面那次校验。
            write_marker(staging, cos_ledger.get(rel))
            os.replace(staging, local_abs)
            rec = cos_ledger.get(rel) or {}
            append_manifest({'ts': datetime.now().isoformat(timespec='seconds'),
                             'dir': rel, 'dest': local_abs,
                             'completeness': f['completeness'],
                             'bytes': lm['total'], 'files': lm['count'],
                             'digest': lm['digest'], 'rsync_rc': rc, 'verify': 'ok',
                             'cos_key': rec.get('key', ''),
                             'cos_present': rec.get('object_present') is True})
            log(f'  ✅ 已落地 {local_abs}（{lm["count"]} 文件 / {lm["total"] / 1e6:.1f} MB）')
            done.append(rel)
        except Exception as e:
            log(f'  ❌ {rel} 出错：{e}')
            failed.append(rel)
        finally:
            # 每场结束（无论成败、无论 continue）都刷一次锁的 mtime：
            # 服务器出口实测只有 0.32 MB/s，单场最坏 ≈70 分钟，锁的「过期」必须按
            # **心跳**判而不是按「建于多久前」判（见 `acquire_lock`）。
            touch_lock()

    # ---- 5. 本地已有那些：本轮重新校验**文本**，通过才允许删服务器那份 ----
    for f in to_verify:
        rel = f['dir']
        local_abs = inv[rel]
        try:
            rm = remote_manifest(args.host, rel)
            unk = unknown_top_files(rm)
            if unk:
                log(f'  ❌ {rel} 顶层出现未知文件 {unk[:5]} → 不拉也不删'
                    f'（先确认新产物该走哪条腿，再升级 KNOWN_TOP_FILES）')
                failed.append(rel)
                continue
            lm = ll.dir_manifest(local_abs)
            good, problems = verify(text_manifest(rm), text_manifest(lm))
            if good:
                # 顺手（重）写媒体指针：老工具拉的场次、以及上一轮 `--keep-remote` 落地的
                # 场次，本地都可能还没有 `_local_cos_media.json`。**校验通过之后**才写。
                write_marker(local_abs, cos_ledger.get(rel))
                done.append(rel)
                log(f'  ✅ 本地已有且文本一致 {rel}')
            else:
                log(f'  ⚠️ 本地那份与服务器**不一致** → 不拉也不删，请人工确认 {rel}：')
                for p in problems:
                    log(f'       - {p}')
                failed.append(rel)
        except Exception as e:
            log(f'  ❌ {rel} 校验出错：{e}')
            failed.append(rel)
        finally:
            touch_lock()

    # ---- 6. 两批**合并之后只算一次**门禁，再交清单删 ----
    cleanup_lists(lists_dir)
    if args.keep_remote:
        log(f'--keep-remote：本轮 {len(done)} 场通过校验，但**不删**服务器那份')
    elif not done:
        log('本轮没有通过校验的场次 → 不删服务器那份')
    else:
        # ★ 删之前**重取一次** facts：我们的数字取自本轮开头，而删除可能在几十分钟之后
        #   才执行 —— 期间目录若被改动，`bytes/files` 就对不上（计划 §三 ③ TOCTOU）。
        #   取不到就**一场都不删**（文本都已安全落地，下一轮重新校验再删）。
        fresh = None
        try:
            fresh = remote_facts(args.host, args.inflight_min)
        except Exception as e:
            log(f'❌ 删前重取场次事实失败：{e}')
        if fresh is None:
            log('❌ → **本轮一场都不删**（服务器那份留着，下一轮会重新校验）')
            failed.extend(done)
        else:
            # ★★ 门禁只在这里算一次，`to_pull` 与 `to_verify` 两条路径**共用**它。
            deletable, pulled_only = gate(done, {f['dir']: f for f in fresh}, cos_ledger)
            if pulled_only:
                log(f'⏸ {len(pulled_only)} 场**文本过了、COS 门禁没过** → 保留服务器那份：')
                for rel, why in pulled_only:
                    log(f'    · {rel}：{why}')
            if deletable:
                rc, out, err = remote_delete(args.host, deletable, args.inflight_min,
                                             apply=True, label='已传输+COS')
                for ln in (out + err).strip().splitlines():
                    log(f'    [{rc}] {ln}')
                if rc != 0:
                    log(f'  ❌ 服务器拒绝删除（rc={rc}）→ 清单保留，下一轮会重新校验重试')
                    failed.extend(done)
                else:
                    log(f'🗑 服务器已删 {len(deletable)} 场（墓碑标记 已传输+COS）')
                touch_lock()
            else:
                log('本轮没有可删的场次（都在 COS 门禁前停住了）')

    log(f'本轮结束：成功 {len(done)}、失败 {len(failed)}'
        + (f'（{failed[:5]}）' if failed else ''))
    return EXIT_FAIL if failed else EXIT_OK


if __name__ == '__main__':
    # Windows 计划任务入口（见 service/README.md）：
    #   schtasks /Create /TN "DouyinPull" /SC MINUTE /MO 10 /F ^
    #     /TR "\"<python.exe 绝对路径>\" \"<repo>\service\pull_sessions.py\" --quiet"
    sys.exit(main())
