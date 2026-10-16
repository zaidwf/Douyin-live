#!/usr/bin/python
# coding:utf-8
"""采集守护的**产出体检**：`active` 不等于「在干活」。

## 为什么要这个

2026-10-15 实测踩到：青果隧道端点 `tun-yowmaw.qg.net:23362` 变成 `Connection refused` 之后，
`douyin-crawl` **每一条控制面请求都在报 ProxyError、一条数据都采不到**，而
`systemctl is-active` 照样显示 **`active`**，`systemctl status` 也一片正常。
没有任何机制会告诉你「这个进程已经空转了几个小时」。

**结论：看门狗必须看产出，不能只看进程状态。**

## 怎么判（两条独立信号，缺一不可）

1. **通道探查**（主信号）：用**和守护进程完全相同**的代码路径
   （`DouyinLiveWebFetcher.get_room_status()` → 走 `DOUYIN_PROXY`）随机探几个名单里的房间。
   这一步同时覆盖 ttwid 首页、enter 接口、隧道认证三条腿 —— 隧道死了这里立刻能看出来。
2. **产出新鲜度**（辅信号）：`data/` 下最近有没有文件被写。

**为什么不能只看第 2 条**：凌晨没人在播时「没有新数据」是**完全正常**的，
纯 mtime 判据会在每天后半夜稳定误报，误报几次之后就没人看告警了。
所以两条信号交叉：

| 通道 | 有产出 | 采样中有人在播 | 判定 | 退出码 |
|---|---|---|---|---|
| ❌ 不通 | — | — | `down` 通道断了（隧道/代理/网络） | 1 |
| ✅ 通 | ✅ 有 | — | `ok` | 0 |
| ✅ 通 | ❌ 无 | ✅ 有 | `stale` **在播却采不到** | 1 |
| ✅ 通 | ❌ 无 | ❌ 无 | `quiet` 没人开播，正常 | 0 |

只有 `down` / `stale` 会让单元失败（`systemctl --failed` 能直接看到）。

## 为什么磁盘低**不算** failed（只告警）

体检也会报磁盘剩余（取自 `auto_crawl._DiskGuard`，与守护进程量的是同一个数），但
**磁盘低于阈值不改退出码**，理由是它和 `down`/`stale` 是两种不同的东西：

- `down`/`stale` 是**异常**：坏了，需要人介入，且不会自己好；
- 磁盘低是**设计内的降级**：守护进程按设计停了音视频、只采文本，而且这个状态一旦发生
  **会持续好几天**（实测增速 ≈ 1.4 GB/小时 ≈ 33 GB/天，40G 盘从 19 GB 可用到 5 GB 阈值
  约 10 小时；清盘或扩盘之前一直低）。

如果把一个「会持续几天」的状态做成 `failed`，`systemctl --failed` 就一直挂着一个红项，
几次之后就没人看它了 —— 那真正的 `down` 报出来也会被忽略。**告警的价值在于稀有**，
所以磁盘只打 `⚠️` 行 + 进 `health.json`。代价是「只看 `--failed` 会漏掉磁盘」，
故 §五的运维清单里 `journalctl -u douyin-healthcheck -n 5` 是固定动作。

## 用法

    python service/health_check.py                    # 体检一次
    python service/health_check.py --stale-min 30     # 产出新鲜度阈值改 30 分钟
    python service/health_check.py --probe 12         # 最多探 12 个房间

由 `douyin-healthcheck.timer` 每 10 分钟跑一次（见同目录 `.service` / `.timer`）。
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ⚠️ 包装要**幂等**：`live_ledger.py` 也 import 了本模块，双方各包一次的话，
# 先包的那个 wrapper 会失去引用 → 被 GC → `__del__` 关掉底层 buffer，
# 之后任何 print 都报 `ValueError: I/O operation on closed file`。
# （实测踩到过：`test_ledger_unit.py` 一开始就在这里炸。）
if not getattr(sys.stdout, '_douyin_utf8', False):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                      errors='replace', line_buffering=True)
        sys.stdout._douyin_utf8 = True
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.path.join(ROOT, 'data')
DEFAULT_ROSTER = os.path.join(ROOT, 'documents', 'huitun_10_100w_1015_final.xlsx')
UNIT = 'douyin-crawl'


# ---------------------------------------------------------------- 纯逻辑（可单测）
def decide(probe_ok, probe_error, live_seen, fresh):
    """两条信号 → 判定。**纯函数**，单测直接喂合成值（见 test_health_check_unit.py）

    :param probe_ok:    通道探查里**至少有一个请求成功抵达抖音**（不要求房间在播）
    :param probe_error: 探查失败时的错误摘要（仅用于文案）
    :param live_seen:   采样里**确认在播**的房间数
    :param fresh:       `data/` 在阈值窗口内有过写入
    :return: (status, healthy, reason)  status ∈ {ok, quiet, stale, down}
    """
    if not probe_ok:
        return ('down', False,
                f'控制面通道不通（{probe_error or "未知错误"}）—— 隧道/代理已失效，'
                f'守护进程此刻在空转')
    if fresh:
        return 'ok', True, '通道正常且有新产出'
    if live_seen > 0:
        return ('stale', False,
                f'探到 {live_seen} 个在播房间，但 data/ 在阈值内没有任何写入 —— '
                f'在播却采不到')
    return 'quiet', True, '通道正常，采样里没有在播房间（无人开播时段，正常）'


# ---------------------------------------------------------------- 两个信号
def classify_status(status):
    """把 `get_room_status()` 的返回值归成四类 —— 这是**区分「没打通」和「没在播」的关键**

    `get_room_status()` 把异常**吞掉**了（自己 catch 后返回字符串），调用方拿不到异常，
    只能靠 status 反推。`liveMan.py` 实际只产出这四种，实测对应关系是：

      '正在直播'  → 打通了，且在播            （enter 里 room_status == 0）
      '已结束'    → **打通了**，房间没开播     （enter 里 room_status == 2 —— 名单里绝大多数）
      '未知'      → **没拿到数据** → 失败      （enter 返回空体：风控静默拦截、代理挂、
                                                （网络断，都落到这个分支）
      '错误'      → **没打通** → 失败          （外层 except）

    ⚠️ **关键且反直觉的一条：`'未知'` 是失败，不是「房间没开播」。**
    踩过两次，两次都是实跑才暴露：

    1. 第一版凭直觉把 `'未知'` 当成「没开播 = 正常」，因为名字像\"不知道\"。
       真实情况相反 —— 请求**正常成功**时，没开播的房间返回的是 `'已结束'`（有数据、
       room_status==2），只有**取不到数据**时才会是 `'未知'`。
    2. 第二版改成「排除法」（不是 `'正在直播'` 就算 error），又把 `'已结束'` 归成了错误 ——
       200 人名单里绝大多数房间都没开播，于是几乎每次采样都被判「通道错误」。
       实测那次 5 个房间有 4 个被误判，只是碰巧第 5 个在播才没翻车。

    第三版（当前）用**白名单**：只有前两个是「到了抖音」，后两个都是失败。
    死代理实测：8 个房间全部 `'未知'` → 8 个失败 → `down`（第一版在这里报的是 `✅ ok`，
    也就是说看门狗对「隧道全死」这个**唯一要防的场景**完全失明）。
    """
    if status == '正在直播':
        return 'live'
    if status == '已结束':
        return 'offline'
    if status == '未知':
        return 'nodata'
    return 'error'


def check_channel(roster, probe_n):
    """通道探查：**均匀取样**最多 `probe_n` 个房间，探到在播就提前停

    返回 (ok, error, live_seen, probed, failed)，其中 `failed` = `'未知'` + `'错误'` 之和。

    `ok` 的判据是「**至少有一个房间拿到了数据**」（即在播或已结束）—— 那说明 ttwid 首页
    + enter + 隧道认证这几条腿都通。判据**不能**放宽成「没抛异常就算通」：代理全死时
    每个请求都在内部被 catch 成 `'未知'`，一个异常都不会冒出来。
    """
    try:
        from auto_crawl import load_anchors
        from liveMan import DouyinLiveWebFetcher
    except Exception as e:
        return False, f'导入失败: {e}', 0, 0, 0

    try:
        anchors = load_anchors(roster)
    except Exception as e:
        return False, f'名单读取失败: {e}', 0, 0, 0
    if not anchors:
        return False, f'名单为空: {roster}', 0, 0, 0

    live_seen = probed = failed = nodata = 0
    last_err = None
    # 均匀取样而不是取前 N 个：名单是按分层排的，前 N 个可能集中在同一档
    step = max(1, len(anchors) // max(1, probe_n))
    sample = anchors[::step][:probe_n]

    for name, did in sample:
        probed += 1
        try:
            f = DouyinLiveWebFetcher(did, None)
            _, status, _, _ = f.get_room_status()
            try:
                f.stop()
            except Exception:
                pass
            kind = classify_status(status)
            if kind == 'live':
                live_seen += 1
                break          # 探到在播就够了，不必把采样走完、少给风控添事
            if kind == 'offline':
                continue       # 拿到了数据（房间已结束）—— 这正是「通道通」的证据
            failed += 1
            if kind == 'nodata':
                nodata += 1
                last_err = f'{name}: 未拿到数据'
            else:
                last_err = f'{name}: 通道错误'
        except Exception as e:
            # 兜底：正常路径下 get_room_status 不会抛（它自己吞了），这里防的是构造/其它异常
            failed += 1
            last_err = f'{name}: {e}'

    reached = (probed - failed) > 0        # 至少有一个房间真的拿到了数据
    if not reached and nodata == failed and failed:
        # 全是 '未知'：这是**隧道/代理全死**的招牌形状（enter 全部返回空体），
        # 不是「恰好抽到的房间都没开播」（那种情况会返回 '已结束'）。文案要点明，
        # 否则值班的人会往「名单选得不好」的方向查。
        last_err = f'全部 {failed} 个都被判为「未拿到数据」—— 典型的隧道/代理全死形状'
    return reached, last_err, live_seen, probed, failed


def newest_write_within(path, minutes):
    """`path` 下 `minutes` 分钟内有没有文件被写过（找到第一个就返回，健康时几乎零开销）"""
    cutoff = time.time() - minutes * 60
    if not os.path.isdir(path):
        return False
    for dirpath, dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                if os.path.getmtime(os.path.join(dirpath, fn)) > cutoff:
                    return True
            except OSError:
                continue
    return False


def disk_free_gb(path=DATA_ROOT):
    """剩余空间（GB）；用 `auto_crawl._DiskGuard.free_gb()` 量，**不另写一份**

    「怎么算剩余空间」（含路径不存在时向上找现有祖先）只有一处实现，改也只改那里 ——
    体检和守护进程量的必须是同一个数，否则会出现「守护说降级了、体检说够用」。

    量不到返回 None（与 444 计数同一约定）：`0` 会被读成「磁盘满了」，是**错误的事实**。
    """
    try:
        from auto_crawl import _DiskGuard
    except Exception:
        return None
    try:
        return _DiskGuard(0).free_gb()
    except Exception:
        return None


def journal_risk_count(since_min=60):
    """最近窗口内的**真** 444 与 ProxyError 计数

    444 判据**必须**从 `liveMan.is_risk_control_444` 取，不许在这里重写一遍子串匹配 ——
    这正是 2026-10-15 那份作废报告的成因（naive `"444" in line` 把 user_id / 时间戳
    数成风控命中）。判据只有一份，改也只改 liveMan 一份。

    取不到判据（liveMan 导入失败）时返回 `risk444: None` 而不是 0 —— **0 是「干净」的
    意思**，把「没量到」渲染成「干净」是这类看门狗最危险的失败模式。
    """
    out = {'risk444': None, 'proxy_error': 0, 'error_lines': 0}
    try:
        from liveMan import is_risk_control_444
    except Exception:
        is_risk_control_444 = None
    try:
        r = subprocess.run(
            ['journalctl', '-u', UNIT, '--since', f'-{since_min} min', '--no-pager'],
            capture_output=True, text=True, timeout=60)
        if is_risk_control_444 is not None:
            out['risk444'] = 0
        for line in (r.stdout or '').splitlines():
            if is_risk_control_444 is not None and is_risk_control_444(line):
                out['risk444'] += 1
            if 'ProxyError' in line:
                out['proxy_error'] += 1
            if '[ERROR]' in line:
                out['error_lines'] += 1
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description='采集守护的产出体检（active ≠ 在干活）')
    ap.add_argument('--roster', default=DEFAULT_ROSTER)
    ap.add_argument('--probe', type=int, default=8, help='最多探几个房间（默认 8）')
    ap.add_argument('--stale-min', type=int, default=30,
                    help='data/ 多久没写入算「不新鲜」（默认 30 分钟）')
    ap.add_argument('--min-free-gb', type=float, default=5.0,
                    help='磁盘告警阈值（GB，默认 5，与 douyin-crawl 的 --min-free-gb 保持一致）')
    ap.add_argument('--json-out', default=os.path.join(ROOT, 'service', 'dist', 'health.json'))
    args = ap.parse_args()

    ok, err, live_seen, probed, failed = check_channel(args.roster, args.probe)
    fresh = newest_write_within(DATA_ROOT, args.stale_min)
    status, healthy, reason = decide(ok, err, live_seen, fresh)
    risk = journal_risk_count()
    free_gb = disk_free_gb()

    ffmpeg = 0
    try:
        ffmpeg = int(subprocess.run(['pgrep', '-c', 'ffmpeg'], capture_output=True,
                                    text=True, timeout=15).stdout.strip() or 0)
    except Exception:
        pass

    icon = {'ok': '✅', 'quiet': '✅', 'stale': '❌', 'down': '❌'}[status]
    print(f'{icon} [{status}] {reason}')
    # 打印**失败条数**而不只是最后一条 —— 「1 个房间失败」和「8 个房间全失败」
    # 是完全不同的诊断，只显示 last_err 会把后者伪装成前者
    print(f'    通道探查：{probed} 个房间，{live_seen} 个在播，{failed} 个没拿到数据'
          f'{"（" + err + "）" if err else ""}')
    print(f'    产出：{"有" if fresh else "无"}（窗口 {args.stale_min} 分钟）'
          f' | ffmpeg {ffmpeg} 个')
    # 磁盘**只告警、不改退出码**。说明见文件顶部「为什么磁盘低不算 failed」。
    if free_gb is None:
        print(f'    磁盘：**未测到**剩余空间（守护进程按 fail-open 处理，即放行音视频）')
        disk_low = None
    else:
        disk_low = free_gb < args.min_free_gb
        mark = '⚠️ ' if disk_low else ''
        tail = ('—— 守护进程此刻**已停音视频、只采文本/数值**（这是设计行为，不是故障；'
                '要恢复全模态请清理 data/ 或扩盘）') if disk_low else f'（阈值 {args.min_free_gb:g} GB）'
        print(f'    磁盘：{mark}剩余 {free_gb:.1f} GB {tail}')
    print(f'    近 60 分钟：真444 {risk["risk444"] if risk["risk444"] is not None else "未测到"}'
          f' | ProxyError {risk["proxy_error"]} | [ERROR] {risk["error_lines"]}')

    if args.json_out:
        try:
            os.makedirs(os.path.dirname(args.json_out), exist_ok=True)
            with open(args.json_out, 'w', encoding='utf-8') as f:
                json.dump({'ts': datetime.now().isoformat(timespec='seconds'),
                           'status': status, 'healthy': healthy, 'reason': reason,
                           'probe': {'ok': ok, 'error': err, 'live_seen': live_seen,
                                     'probed': probed, 'failed': failed},
                           'fresh': fresh, 'ffmpeg': ffmpeg,
                           # disk 不影响 healthy：低磁盘是**设计内的降级**，会持续数天，
                           # 做成 failed 会把真正的 down/stale 淹掉（见文件顶部说明）
                           'disk': {'free_gb': None if free_gb is None else round(free_gb, 1),
                                    'min_free_gb': args.min_free_gb, 'low': disk_low},
                           'journal_last_60min': risk},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f'    ⚠️ 状态文件写入失败：{e}')

    return 0 if healthy else 1


if __name__ == '__main__':
    sys.exit(main())
