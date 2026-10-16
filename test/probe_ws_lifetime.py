# -*- coding: utf-8 -*-
"""一次性探针：WebSocket 连接**能活多久**（走隧道 vs 直连，1 条 vs N 条并发）。

## 为什么要这个

2026-10-16 实测：服务器上 99 个场次里 **85 个以 `ws_closed` 收尾**（86%），
时长中位 **375 秒**，只有 3 场是正常 `offline`。后果不是「少采一点」，而是
**一场真实直播被切成多个「完整」碎片场次**（同一主播 30 分钟内 4 个场次），
于是台账里「完整」标的是「碎片完整」而不是「一场直播完整」。

要修它，先得知道是谁掐断的。两个候选解释：

  (a) **隧道/抖音对长连接有寿命上限** —— 每条连接从自己建立那刻起各有一个计时器，
      到点各自死。表现：并发开的连接**死在不同时刻**，且单条独跑也照样死。
  (b) **隧道在并发下被压垮**（代理账号的连接数/带宽配额）—— 表现：并发开的连接
      **几乎同时死**，而单条独跑能活很久。

本探针就是把这个区分做出来：同时开 N 条到**在播**主播的 WS，逐秒记录存活，
把每条连接的「建立时刻 / 死亡时刻 / 死因 / 最后一条错误」打出来。

## 用法（在**服务器**上跑）

⚠️ 2026-10-16 起 **WS 默认直连**（`liveMan.proxy_for_ws()` 返回 `{}`），所以
「走隧道」那一组**必须显式加 `--ws-proxy`** —— 不加的话两组都是直连，实验没有变量。

    # 1) 从最近一轮 live_rounds 里自动挑 N 个在播主播，**走隧道**
    #    （`probe_ws_lifetime_tunnel.sh` 负责读 root:root 600 的 proxy.env 再转交，
    #      避开 `sudo -n bash -c "... &"` 那种引号/cwd 全是坑的写法）
    sudo -n setsid nohup bash /opt/douyin-live/test/probe_ws_lifetime_tunnel.sh \
        --pick 10 --ws-proxy --duration 1200 > /tmp/pt.log 2>&1 < /dev/null &

    # 2) 同样 N 个主播，**直连**（对照组；--no-proxy 只是显式一点，不加也一样）
    cd /opt/douyin-live && setsid nohup ./venv/bin/python -u test/probe_ws_lifetime.py \
        --pick 10 --duration 1200 > /tmp/pd.log 2>&1 < /dev/null &

（`setsid` + `</dev/null` 是为了让 ssh 断开后进程还能活着；日志里可能有 NUL
补位，读的时候用 `tr -d "\0" < 日志`。）

## ★ 2026-10-16 的实测结论（这个探针就是为此写的）

| 组 | 跑在哪 | 条数 | 结果 |
|---|---|---|---|
| 隧道 | 服务器 | 2 | **389.1s / 456.5s 双双被掐**，错误 `Connection to remote host was lost.`，死前还在收帧（702 / 870） |
| 隧道 | 服务器 | 10（`--pick 10`） | 未跑（10 路那条已由直连组占了并发；隧道行为在 2 条上已定案）|
| 直连 | 服务器 | 2 | **1051s 仍活着**（实验结束时没死） |
| **直连** | 服务器 | **10** | **9 条活满 1200s 上限**（帧数 2186–3717，**零 ERROR**）；
|        |        |      | 第 10 条 574.6s **正常下播**（`offline`，不是被掐）|
| 直连 | **笔记本** | 2 | **活满 900s 上限**，收到 2231 / 2310 帧，零 ERROR |

→ **隧道是掐断长连接的那一方，且每条连接各自死**（不是并发压垮，见
`probe_ws_death_cluster.py`：85 条 `ws_closed` 里**没有一次同秒/同两秒成批死亡**，
并发 10 路与并发 1–2 路的中位存活时长同为 ~370s）。

## ⚠️ 凭据纪律

本探针**绝不打印代理账密**：走 `liveMan.proxy_for_ws()` 拿参数，只把
`host:port` 打进日志（`http_proxy_auth` 那一项一个字节都不输出）。
`--no-proxy` 把三个代理环境变量**清掉**再开始，所以那一组是确定的直连。
`--ws-proxy` 设 `DOUYIN_WS_PROXY=1` 让 WS 重新走隧道（2026-10-16 起的 A/B 用）。

## ⚠️ 这是探针不是单测

会真的连抖音、真的走代理、真的花掉隧道流量。不进 `test_*_unit.py`，
也不要在采集正在跑的时候开一大堆 —— N 要按「代理账号的并发配额」量力而行。
"""
import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import liveMan as ll  # noqa: E402

ROUNDS = '/opt/douyin-live/service/dist/live_rounds_{ym}.jsonl'


def pick_live(n, rounds_path=None):
    """从最近一轮 `live_rounds` 里挑 n 个**在播**主播，返回 [(douyin_id, name)]

    取「最后那一轮」而不是「最近的 n 条」—— 一轮里每个主播恰好一行，
    跨轮取会把同一主播重复挑进来。
    """
    if rounds_path is None:
        rounds_path = ROUNDS.format(ym=time.strftime('%Y%m'))
    if not os.path.exists(rounds_path):
        return []
    rounds = {}
    order = []
    with open(rounds_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get('ts')
            if ts not in rounds:
                rounds[ts] = []
                order.append(ts)
            rounds[ts].append(r)
    if not order:
        return []
    last = rounds[order[-1]]
    live = [(r['douyin_id'], r.get('name') or r['douyin_id']) for r in last if r.get('live') is True]
    print('最近一轮 %s：%d 行，其中在播 %d' % (order[-1], len(last), len(live)))
    return live[:n]


def describe_proxy():
    """把生效的代理打印成一句话（**只 host:port，绝不含账密**）"""
    c = ll._proxy_cfg()
    ws = ll.proxy_for_ws()
    if not c:
        return '直连（未配置代理）'
    return 'HTTP 走隧道 %s:%s（auth=%s）；WS %s' % (
        c['host'], c['port'], '有' if c['auth'] else '无',
        '走隧道（DOUYIN_WS_PROXY=1）' if ws else '**直连**（默认）')


class Conn(object):
    """一条 WS 连接 + 它的观测线程"""

    def __init__(self, did, name):
        self.did = did
        self.name = name
        self.logs = []          # [(相对秒, level, text)]
        self.frames = 0         # data_callback 收到的事件条数
        self.t0 = None          # 开始（= 线程起）时刻
        self.t_open = None      # 相对秒：握手成功（看到「WebSocket连接成功.」）
        self.t_end = None
        self.reason = None
        self.note = ''

    def _log(self, level, msg):
        self.logs.append((time.time() - (self.t0 or time.time()), level, msg))

    def _on_data(self, _ev, _d):
        self.frames += 1

    def errors(self, limit=3):
        out = [t for (_, lv, t) in self.logs if lv in ('ERROR', 'WARN')]
        return out[:limit]

    def first_error(self):
        for (_, lv, t) in self.logs:
            if lv == 'ERROR':
                return t
        return ''


def run_one(conn, duration):
    """跑一条连接直到它死、主播下播、或到时限。**阻塞**在调用线程里。"""
    f = ll.DouyinLiveWebFetcher(conn.did, None)
    conn.t0 = time.time()
    f.log_callback = conn._log
    f.data_callback = conn._on_data
    offline = threading.Event()
    f.on_offline = offline.set

    th = threading.Thread(target=f.start, daemon=True)
    th.start()

    while time.time() - conn.t0 < duration:
        for (ts, _lv, text) in conn.logs:
            if conn.t_open is None and 'WebSocket连接成功' in text:
                conn.t_open = ts
        if not th.is_alive():
            conn.reason = 'ws_closed'
            break
        if offline.is_set():
            conn.reason = 'offline（主播真下播）'
            break
        time.sleep(0.3)
    else:
        conn.reason = 'timeout（到时限仍活着）'
    conn.t_end = time.time() - conn.t0

    f.stop()
    th.join(timeout=5)
    if conn.reason == 'ws_closed' and conn.t_open is None:
        conn.reason = '★连接就没建立起来'
    if conn.t_open is None:
        conn.note = '握手未成功'
    return conn


def main():
    ap = argparse.ArgumentParser(description='WebSocket 生命周期探针（一次性，真连抖音）')
    ap.add_argument('--anchors', default='', help='逗号分隔的抖音号（与 --pick 二选一）')
    ap.add_argument('--pick', type=int, default=0,
                    help='从最近一轮 live_rounds 里自动挑 N 个**在播**主播')
    ap.add_argument('--rounds', default=None, help='live_rounds 文件路径（默认当月）')
    ap.add_argument('--duration', type=float, default=900, help='最长观测秒数（默认 900）')
    ap.add_argument('--stagger', type=float, default=0,
                    help='每条连接之间错开多少秒启动（默认 0 = 同时，为了看是否同时死）')
    ap.add_argument('--no-proxy', action='store_true',
                    help='★清掉代理环境变量 = 确定的直连（**现在这是 WS 的默认行为**，'
                         '这个开关只影响 HTTP 那几条腿）')
    ap.add_argument('--ws-proxy', action='store_true',
                    help='★让 WS **走隧道**（设 DOUYIN_WS_PROXY=1）。'
                         '2026-10-16 之后 WS 默认直连，要复现「隧道掐长连接」必须加这个')
    args = ap.parse_args()

    if args.no_proxy:
        for k in ll.PROXY_ENV_KEYS:
            os.environ.pop(k, None)
        ll._PROXY_RESOLVED = False      # 让 _proxy_cfg 重新解析（见 liveMan）
    if args.ws_proxy:
        os.environ[ll.WS_PROXY_ENV] = '1'
    else:
        os.environ.pop(ll.WS_PROXY_ENV, None)

    print('=' * 78)
    print('通路：%s' % describe_proxy())
    print('时限：%gs  错开：%gs' % (args.duration, args.stagger))
    print('=' * 78)

    if args.anchors:
        targets = [(a.strip(), a.strip()) for a in args.anchors.split(',') if a.strip()]
    else:
        n = args.pick or 4
        targets = pick_live(n, args.rounds)
    if not targets:
        print('没有目标主播 —— 用 --anchors 或确认 live_rounds 里有在播的')
        return 2
    print('目标 %d 个：%s' % (len(targets), ', '.join('%s(%s)' % (n, d) for d, n in targets)))

    conns = [Conn(d, n) for d, n in targets]
    threads = []
    for c in conns:
        t = threading.Thread(target=run_one, args=(c, args.duration), daemon=True)
        t.start()
        threads.append(t)
        if args.stagger:
            time.sleep(args.stagger)

    print('\n—— 观测中（每 30s 一行心跳，看谁还活着）——')
    t_start = time.time()
    while any(t.is_alive() for t in threads):
        time.sleep(30)
        alive = []
        for c in conns:
            if c.t_end is None:
                age = time.time() - (c.t0 or time.time())
                alive.append('%.0fs' % age)
            else:
                alive.append('死@%.0fs' % c.t_end)
        print('  +%.0fs  %s' % (time.time() - t_start, '  '.join(alive)))
        if time.time() - t_start > args.duration + 120:
            break
    for t in threads:
        t.join(timeout=10)

    print('\n' + '=' * 78)
    print('%-22s %8s %8s %8s %6s  %s' % ('主播', '握手', '死亡', '存活', '帧数', '死因'))
    print('-' * 78)
    for c in conns:
        print('%-22s %8s %8s %8s %6d  %s' % (
            (c.name or c.did)[:20],
            '%.1f' % c.t_open if c.t_open is not None else '—',
            '%.1f' % c.t_end if c.t_end is not None else '—',
            '%.1f' % ((c.t_end - c.t_open) if (c.t_end is not None and c.t_open is not None) else 0),
            c.frames, c.reason or ''))

    print('\n—— 每条连接的最后一条错误（判死因）——')
    ends = [c.t_end for c in conns if c.t_end is not None]
    for c in conns:
        err = c.first_error()
        print('  %-20s 结束@%7s  首错：%s' % (
            (c.name or c.did)[:20],
            '%.1f' % c.t_end if c.t_end is not None else '—',
            (err or '（无 ERROR）')[:150]))

    if len(ends) >= 2:
        spread = max(ends) - min(ends)
        print('\n★ 死亡时刻跨度 %.1f 秒' % spread)
        print('  < 30s → 指向「隧道在并发下被压垮」(b)；'
              '跨度大且各自随机 → 指向「长连接各自有寿命上限」(a)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
