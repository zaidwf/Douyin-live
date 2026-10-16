#!/usr/bin/python
# coding:utf-8
"""回收链路体检：**数据真的进 COS 了吗、磁盘真的在回收吗**。

## 为什么要这个

2026-10-16 起整条回收链路自动跑了（服务器 `douyin-archive.timer` 每小时归档、笔记本的
计划任务每 30 分钟拉文本 + 门禁通过后删）。但**自动化只是让链路自己动，没有让它在停摆时
出声** —— 而这恰好是最危险的形态：每一步都「看起来正常」，磁盘一路涨到
`--min-free-gb 5` 悄悄把全模态降级成纯文本，而且**没有任何东西会报错**。

已有的告警面各自漏掉一块：

| 已有 | 覆盖 | 漏掉 |
|---|---|---|
| `douyin-healthcheck` | 采集**通道**通不通、`data/` 有没有产出 | 数据**进没进 COS**（采集全绿、归档全死，它照样报 ok） |
| `douyin-ledger` 的 `★疑似漏采` | 「在播却没在采」 | 同上 |
| `douyin-archive` 单元自己的退出码 | 归档**跑起来之后**失败（非 0 → `--failed`） | timer **压根没跑**（被停用/没 enable → 单元永远不会 failed） |

本工具补这三块。

## 三条判据（任一命中 → 退出 1）

| # | 判据 | 抓的是什么 | 默认阈值 |
|---|---|---|---|
| ① | 根盘可用空间 | **笔记本那条腿停了**（不删了）→ 磁盘只涨不落。它是两条腿的**联合**指标 | 10 GB |
| ② | 归档心跳 `cos_archive.beat` 的年龄 | 归档 timer **根本没在跑** | 150 分钟 |
| ③ | 账本说已归档、COS 上**对象此刻还在不在** | ★ **静默丢失**（那是唯一副本） | 无（二元） |

**为什么 ① 能替笔记本那条腿说话**：删除只在笔记本上做（三道门禁只在那边），服务器看不到
它的死活。但「不删」的直接后果就是**磁盘只涨不落** —— 于是一条磁盘判据同时覆盖两条腿，
不必在服务器上去猜笔记本的状态。代价是**慢**：从 25 GB 掉到 10 GB 大约 5 小时
（实测流入 2.86 GB/小时），降级线 5 GB 之前约 1.7 小时。

**为什么 ③ 非做不可**：`cos_archive` 的 HEAD 复查排在 `if not todo: return EXIT_OK`
**之后** —— 也就是「所有场次都已归档、没有新东西要传」这个**恰恰最需要确认对象还在**的
日常场景里，服务器**一次 HEAD 都不做**就返回 0。桶被清空、生命周期规则到期、有人手工删过
对象：账本照旧写着 `verify='ok'`，`plan_cycle()` 照旧把它们判成 `done` 并**永远跳过**，
直到笔记本那侧的门禁在**准备删它**的那一刻才发现。本判据把这个发现时机从「下次要删它」
提前到「每 30 分钟」。

⚠️ **③ 只核对「服务器上还在的场次」**。账本是**累积**的（笔记本删掉一场，那条记录仍然
留着），全量 HEAD 会随天数线性变重（一天一百多场 → 一个月几千次 HEAD）。而已经删掉的那些，
**删除当时已经用同一条判据当场核过**，再查一遍没有新增信息。所以过滤是**正确性**要求
（不查无关的），也是**成本**要求。

## 本判据**不**证明什么（别把它当数据安全的全部）

- 它证明的是「对象**在**、字节数与账本对得上」。**内容**对不对不在它范围内 ——
  逐字节核验在归档轮（`verify_tar` 对 manifest）与笔记本的删除门禁（文本腿 manifest）里。
- 它**不是** `--dump-ledger -` 的替代，两者分工不同：那个是「**允不允许删**」的当场正向
  证据（fail-closed、逐批 TOCTOU 重取、整批拒绝），本工具是「**有没有出事先吱一声**」的
  巡检。但两者判定「对象在不在」用的是**同一个** `Cos.size_of()`，不另写一份。
- 磁盘那一条**不是**「能一直采全模态」的保证：`--min-free-gb` 只保证「不写满」。

## 退出码

`0` 健康 / `1` 有告警。**没有第三个码** —— 「磁盘测不到」「凭据读不到」也一律归 1：它们
同样是「**没有证据说明链路健康**」（注意这与 `health_check` 对磁盘的 fail-open 处理方向
相反，那里的取舍是「别因为量不出来就停采」，这里是「别因为量不出来就当没事」）。
systemd 只看零/非零，`systemctl --failed` 就是唯一的告警面 —— 仓库里没有邮件/钉钉通道，
**故意不造**（多一个通道就多一个没人看的东西）。

    python service/reclaim_check.py                  # 体检一次
    python service/reclaim_check.py --min-free-gb 15 # 换磁盘告警线
    python service/reclaim_check.py --json-out ''    # 不写状态文件

由 `douyin-reclaimcheck.timer` 每 30 分钟跑一次（见同目录 `.service` / `.timer`）。
"""
import argparse
import io
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # 供 import health_check / cos_archive

# ⚠️ 包装要**幂等**：`health_check` 与 `live_ledger` 导入时也会包一层，双方各包一次的话
# 先包的那个 wrapper 会失去引用 → 被 GC → `__del__` 关掉底层 buffer，之后任何 print 都报
# `ValueError: I/O operation on closed file`（在 `test_ledger_unit.py` 里真踩过）。
if not getattr(sys.stdout, '_douyin_utf8', False):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                      errors='replace', line_buffering=True)
        sys.stdout._douyin_utf8 = True
    except Exception:
        pass

import health_check as hc     # 复用 disk_free_gb： 「怎么算剩余空间」只有一份实现
import cos_archive as ca      # 复用账本 / Cos / session_key：「对象在不在」只有一份判据

# 磁盘告警线。取 10 而不是守护进程的降级线 5：**告警要早于降级**，留出处置时间。
# 2.86 GB/小时 → 10 GB 距离 5 GB 的降级线还有约 1.7 小时；从当前 25 GB 掉到 10 GB 约 5 小时。
DEFAULT_MIN_FREE_GB = 10.0

# 归档心跳的容忍年龄。归档 timer 是 1 小时一轮，150 分钟 = **连丢两轮**才报 ——
# 单轮迟到（机器刚重启、上一轮正好卡在打包）不该变成告警。
DEFAULT_MAX_IDLE_MIN = 150

# 每类告警的处置建议。**告警必须自带下一步动作** —— 半夜看到一行红字却不知道该敲什么
# 命令，等于没报。
HINTS = {
    'disk': '先看笔记本那条腿：service/dist/pull_sessions.log 的「本轮结束」，'
            '以及计划任务还在不在（schtasks /Query /TN "DouyinPull" /V /FO LIST）',
    'beat': 'sudo -n systemctl status douyin-archive.timer；'
            'sudo -n journalctl -u douyin-archive -n 50',
    'cos': 'sudo -n ./venv/bin/python service/cos_archive.py --selftest；'
           '再 --dump-ledger - 看逐条活 HEAD',
}


def beat_age_min(path=None):
    """归档心跳距今多少分钟。

    **文件不存在 → None，不是 0** —— 0 会被读成「刚跑过」，正好把「一轮都没跑过」这个
    最该报的情况伪装成最健康的情况。
    """
    try:
        mtime = os.path.getmtime(ca.BEAT if path is None else path)
    except OSError:
        return None
    return (time.time() - mtime) / 60.0


def sessions_on_disk(ledger, root=None):
    """账本里有、**且此刻还在磁盘上**的场次目录（rel）—— 即 ③ 的核对范围。

    这个过滤是**成本**要求（账本累积，全量 HEAD 会随天数线性变重）也是**正确性**要求
    （已删场次在删除当时已经当场核过，再查没有新增信息），所以单独成函数、单测钉住。
    """
    root = ca.ROOT if root is None else root
    return [rel for rel in ledger if os.path.isdir(os.path.join(root, rel))]


def audit_objects(cos, ledger, on_disk):
    """账本里 `verify=='ok'` 的场次，**此刻** COS 上还在不在、字节数对不对。

    :param on_disk: 服务器上**还在**的场次目录（rel）。必须由调用方过滤（账本会累积 ——
                    已删场次的记录留着，理由见模块顶部）。
    :return: `(problems, checked)`，`problems = [(rel, 原因)]`
    """
    problems, checked = [], []
    for rel in sorted(on_disk):
        rec = ledger.get(rel) or {}
        if rec.get('verify') != 'ok':
            continue          # 没传过 / 传失败 → 不归本判据管（归档单元自己的退出码管）
        checked.append(rel)
        got = cos.size_of(ca.session_key(rel))
        want = rec.get('tar_bytes')
        if got is None:
            problems.append((rel, 'COS 上找不到这个对象（账本说已归档）'))
        elif want is not None and got != want:
            problems.append((rel, f'字节数不符：账本 {want} / COS {got}'))
    return problems, checked


def decide(free_gb, min_free_gb, age_min, max_idle_min, cos_problems, creds_error):
    """三路观测 → 告警列表 `[(kind, 人话)]`；空列表 = 健康。

    **纯函数**：I/O 全在调用方，单测直接喂合成数据（判据的真值表才是这文件里最该钉住的东西）。
    `free_gb=None` / `age_min=None` / `creds_error` 都算告警 —— 见模块顶部的退出码一节。
    """
    items = []
    if free_gb is None:
        items.append(('disk', '磁盘剩余空间**未测到** —— 没有任何证据说明回收链路在工作'))
    elif free_gb < min_free_gb:
        items.append(('disk', f'根盘只剩 {free_gb:.1f} GB（告警线 {min_free_gb:g} GB）'
                              ' —— 回收没在起作用（笔记本关机？删除那条腿停了？）'))
    if age_min is None:
        items.append(('beat', f'归档心跳不存在（{ca.BEAT}）—— 归档 timer 一轮都没跑过'))
    elif age_min > max_idle_min:
        items.append(('beat', f'归档心跳已 {age_min / 60:.1f} 小时没更新'
                              f'（阈值 {max_idle_min} 分钟）—— 归档 timer 没在跑？'))
    if creds_error:
        items.append(('cos', f'拿不到 COS 凭据：{creds_error} —— 归档与核实都做不了'))
    for rel, why in cos_problems:
        items.append(('cos', f'{rel}：{why}'))
    return items


def main():
    ap = argparse.ArgumentParser(description='回收链路体检：数据进 COS 了吗 / 磁盘在回收吗')
    ap.add_argument('--min-free-gb', type=float, default=DEFAULT_MIN_FREE_GB,
                    help=f'根盘可用空间告警线（GB，默认 {DEFAULT_MIN_FREE_GB:g}）')
    ap.add_argument('--max-idle-min', type=int, default=DEFAULT_MAX_IDLE_MIN,
                    help=f'归档心跳多久没更新算停摆（分钟，默认 {DEFAULT_MAX_IDLE_MIN}）')
    ap.add_argument('--max-report', type=int, default=5,
                    help='逐条列几个问题（默认 5，多出来的只报个数）')
    ap.add_argument('--json-out', default=os.path.join(ca.DIST, 'reclaim.json'),
                    help='状态文件路径（`\'\'` = 不写）')
    args = ap.parse_args()

    free_gb = hc.disk_free_gb()
    age_min = beat_age_min()

    creds_error, problems, checked_n = '', [], 0
    try:
        cfg = ca.load_cos_env()
    except RuntimeError as e:
        creds_error = str(e)
    else:
        ledger = ca.load_ledger()
        # ★ 只核对**还在服务器上**的场次（账本会累积，理由见模块顶部）
        problems, checked = audit_objects(ca.Cos(cfg), ledger, sessions_on_disk(ledger))
        checked_n = len(checked)

    items = decide(free_gb, args.min_free_gb, age_min, args.max_idle_min,
                   problems, creds_error)

    print(f'{"❌ [alarm]" if items else "✅ [ok]"} 回收链路'
          + (f' {len(items)} 项告警' if items else '正常'))
    print('    磁盘：' + ('**未测到**' if free_gb is None
                        else f'剩余 {free_gb:.1f} GB（告警线 {args.min_free_gb:g}）'))
    print('    归档心跳：' + ('**不存在**' if age_min is None
                          else f'{age_min:.0f} 分钟前（阈值 {args.max_idle_min}）'))
    print(f'    COS 对象：核对 {checked_n} 场'
          + ('（凭据读不到，一场都没核）' if creds_error else '')
          + f'，{len(problems)} 项对不上')
    for i, (kind, text) in enumerate(items[:args.max_report], 1):
        print(f'      {i}. [{kind}] {text}')
    if len(items) > args.max_report:
        print(f'      … 另有 {len(items) - args.max_report} 项同类问题')
    for kind in sorted({k for k, _ in items}):
        print(f'    ↳ {HINTS[kind]}')

    if args.json_out:
        try:
            os.makedirs(os.path.dirname(args.json_out), exist_ok=True)
            with open(args.json_out, 'w', encoding='utf-8') as f:
                json.dump({'ts': datetime.now().isoformat(timespec='seconds'),
                           'healthy': not items,
                           'alarms': [{'kind': k, 'detail': t} for k, t in items],
                           'disk': {'free_gb': None if free_gb is None else round(free_gb, 1),
                                    'min_free_gb': args.min_free_gb},
                           'beat': {'age_min': None if age_min is None else round(age_min, 1),
                                    'max_idle_min': args.max_idle_min,
                                    'path': ca.BEAT},
                           'cos': {'checked': checked_n, 'problems': len(problems),
                                   'creds_error': creds_error}},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f'    ⚠️ 状态文件写入失败：{e}')

    return 0 if not items else 1


if __name__ == '__main__':
    sys.exit(main())
