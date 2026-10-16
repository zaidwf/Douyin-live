#!/usr/bin/python
# coding:utf-8 -*-
"""并发承压测试：验证采集链路在主播规模扩大时的承受能力。

四种模式（`--mode`）：

| 模式 | 测什么 | 要网络吗 |
|---|---|---|
| `pump` | N 个**合成**主播按真实事件比例灌进真 `DataRecorder`，测线程/内存/磁盘/CPU 扩展性 + 数据完整性 | 否（离线，无风控风险） |
| `v8` | MiniRacer V8 上下文「每次新建 vs 单例缓存」的内存/耗时差，**两条签名路径分开测** | 否 |
| `network` | N 个**真实** fetcher 连真实在播房间，测资源上限 / 错误率 | 是 |
| `media` | N 路 `MediaCapture`（音频切片 + 抽帧）并发，测**音视频子系统的可持续上限** | 是 |

## media 模式（音视频并发，2026-10-14 新增）

**不建 WS**，每个 fetcher 只用来 `fetch_live_html()` 拿流地址 —— 音视频的成本全在
**ffmpeg 子进程**里，与 WS/落盘链路是两回事，混测无法归因。

    python load_test.py --mode media --anchors 8 --rooms-file live3.txt --duration 90
    python load_test.py --mode media --anchors 1 --url-index 3 --duration 60   # 低画质单路

判据不是「进程还活着」，而是**音频实时倍率**：16kHz 单声道 s16 = 32 KB/s，
倍率 = 实测写入速率 ÷ 32000。**< 1 = 解码跟不上直播实时**，数据已经在丢 —— CPU 打到
100% 而倍率 0.7 时进程"在跑"，但采到的是坏数据。另附抽帧达成率与 watchdog 续采次数。

⚠️ 用 `--mode network` 的结论不能替代本模式：音视频吃的是 CPU/带宽，WS 吃的是
线程/句柄/磁盘，量级完全不同。

## network 模式的两种口径（都由本脚本承担）

**口径 A —— 复用少数真实开播房间凑满 N 路**（回答「单机能带几路」）
N 路连接按 `i % len(rooms)` 轮流指向 M 个真实房间（M 取 1-3 个当前在播的）。
确定性压满，与实际开播率无关：

    python load_test.py --mode network --anchors 100 --rooms-file live3.txt --duration 300 --record

**口径 B —— 按真名单跑**（回答「真实负载 + 开播率下的表现」）
名单本身就是要连的房间，一路一个，含 60s 轮询检测的完整生产形态：

    python load_test.py --mode network --anchors 40 --roster documents/并发测试.xlsx --sheet 40 --duration 600

> 两者共用一条代码路径：**连接数 = `--anchors`，房间按 `rooms[i % M]` 循环**。
> 口径 A 让 M ≪ N（复用），口径 B 让 M == N（一一对应），不需要额外的参数。

## ⚠️ 结论外推边界

本机测出的是**代码**能承受多少并发，**不是网络**能承受多少并发。后者受 IP 类型支配 ——
同样的频率，数据中心 IP（如腾讯云）会被抖音直接 444 拒掉，住宅 IP 则正常。故
**444 只记账、不当失败**，本机结论不能直接外推到部署环境，须在目标环境单独测。

## 依赖

`psutil`（资源采样）、`openpyxl`（`--roster` 读 xlsx）、`py_mini_racer`（v8 模式）。

用法示例：
  python load_test.py --mode pump    --anchors 100 --duration 600 --rate 10
  python load_test.py --mode pump    --anchors 100 --duration 900 --ramp 10 60
  python load_test.py --mode v8      --anchors 100
  python load_test.py --mode network --anchors 100 --rooms-file live3.txt --duration 300 --record
  python load_test.py --mode media   --anchors 8 --rooms-file live3.txt --duration 90
"""
import argparse
import codecs
import gc
import glob
import io
import json
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from data_recorder import DataRecorder
from metrics import ResourceSampler

# 合成事件权重：参考与辉同行 1h 实测的相对比例（enter/chat 占大头）
EVENT_WEIGHTS = {
    "chat": 30, "like": 20, "enter": 25, "stats": 8,
    "follow": 4, "social": 4, "purchase": 4, "shopping": 5,
}


# ============================================================================
# 公共
# ============================================================================

def _count_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return -1


def _make_out_dir(args, label):
    """输出目录：`--out` 优先，否则 `load_test/<时间戳>_<label>/`"""
    out_dir = args.out or os.path.join(
        "load_test", f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{label}")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _write_report(out_dir, report):
    """落盘 `report.json` —— 只 print 的历史版本让每档结果无法机器比对，必须留档"""
    path = os.path.join(out_dir, "report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"报告: {path}")
    return path


class RampLauncher:
    """按 `--ramp STEP INTERVAL` 分批启动；无 `--ramp` 则一次性全启。

    ⚠️ 旧版把 ramp 只实现在 pump 模式里，network 模式的 `--ramp` 是**静默无效**的。
    这里做成公共类，三种模式共用，network 也真的能分批加压。
    """

    def __init__(self, args, start_one, total, stagger=True, concurrency=1):
        """
        :param start_one: 可调用对象，签名 `start_one(index) -> bool`（启动第 index 个）
        :param total: 目标总数
        :param stagger: 是否在启动之间错峰。**只有 network 需要**（避免瞬时并发触发
                        风控）；pump/v8 是本地线程/纯计算，错峰纯属白等 —— 旧版 pump
                        没有错峰逻辑，这次重构成公共类时别把它传染过去。
        :param concurrency: `start_one` 的并发度。`start_one` 在 network 模式下几乎全是
                        **等网络**（ttwid + room_id 解析），串行执行等于把 N 个 0.6-1.9s
                        的往返首尾相接。实测 8 路并发比串行快 6.2x。
                        ⚠️ 必须有界：风控看的是**瞬时并发量**，不是总连接数 ——
                        不能因为要快就把 142 路一次性全放出去。
        """
        self.args = args
        self.start_one = start_one
        self.total = total
        self.stagger = stagger
        self.concurrency = max(1, int(concurrency))
        self.started = 0
        self.t0 = time.time()
        self.t_first = None      # **第一个**启动的时刻：duration 从这里起算

    def run(self, stop_event=None):
        if self.args.ramp:
            step, interval = self.args.ramp
            while self.started < self.total:
                if stop_event is not None and stop_event.is_set():
                    break
                self.start_batch(step)
                if self.started >= self.total:
                    break
                if stop_event is not None:
                    stop_event.wait(interval)
                else:
                    time.sleep(interval)
        else:
            self.start_batch(self.total)
        return self.t_first

    def start_batch(self, n):
        if self.concurrency <= 1:
            # 串行路径（保留：pump/v8 用它，也便于做「串行 vs 并发」的 A/B）
            for _ in range(n):
                if self.started >= self.total:
                    return
                if self.t_first is None:
                    self.t_first = time.time()
                self.start_one(self.started)
                self.started += 1
                self._stagger()
            print(f"[ramp] 已启动 {self.started}/{self.total}"
                  f"（t={int(time.time() - self.t0)}s）")
            return

        # 并发路径：`_stagger` 仍逐次调用 —— 它现在控制的是**提交节奏**，
        # 而瞬时并发量由 pool 大小封顶，两道闸门合起来才是风控意义上的「温和加压」。
        with ThreadPoolExecutor(max_workers=self.concurrency,
                                thread_name_prefix="ramp") as ex:
            futs = []
            for _ in range(n):
                if self.started >= self.total:
                    break
                if self.t_first is None:
                    self.t_first = time.time()
                futs.append(ex.submit(self.start_one, self.started))
                self.started += 1
                self._stagger()
            # 必须等本批全部落地：否则 `startup_seconds` 与 `units` 会在还没建好时
            # 就被读走，报告里的启动耗时与连接数都会偏小
            for fu in futs:
                fu.result()
        print(f"[ramp] 已启动 {self.started}/{self.total}（t={int(time.time() - self.t0)}s）")

    def _stagger(self):
        """连接间错峰。`--stagger 0` 时用 1-3s 随机抖动（旧行为）。"""
        if not self.stagger or self.started >= self.total:
            return
        d = self.args.stagger
        time.sleep(d if d > 0 else random.uniform(1.0, 3.0))

    @property
    def startup_seconds(self):
        """全部启动完耗时 —— 单列出来，因为它会随 `--stagger` 线性增长，
        不单列就会把「启动慢」误读成「运行慢」。"""
        return round(time.time() - self.t0, 1)


# ============================================================================
# pump：离线合成压测
# ============================================================================

def make_event_gen(seed=0):
    """生成 (event_type, data) 的无限迭代器，事件内容随机但字段与 data_recorder 对齐"""
    rnd = random.Random(seed)
    kinds = list(EVENT_WEIGHTS.keys())
    weights = list(EVENT_WEIGHTS.values())
    i = 0
    while True:
        i += 1
        kind = rnd.choices(kinds, weights=weights)[0]
        uid = str(rnd.randint(10 ** 8, 10 ** 11))
        nick = f"u{i}"
        if kind == "chat":
            yield "chat", {"user_id": uid, "nickname": nick, "content": f"弹幕{i}"}
        elif kind == "like":
            yield "like", {"nickname": nick, "count": rnd.randint(1, 30), "total": 10 ** 6 + i}
        elif kind == "enter":
            yield "enter", {"user_id": uid, "nickname": nick, "gender": "未知"}
        elif kind == "stats":
            yield "stats", {"viewer_count": rnd.randint(1000, 50000), "total_pv": 10 ** 5 + i}
        elif kind == "follow":
            yield "follow", {"user_id": uid, "nickname": nick, "action": 1, "follow_count": 4 * 10 ** 7 + i}
        elif kind == "social":
            yield "social", {"user_id": uid, "nickname": nick, "action": 3, "follow_count": 0,
                             "share_type": 1, "share_target": "微信"}
        elif kind == "purchase":
            yield "purchase", {"msg_type": "LivePopMessage", "ids": [str(10 ** 18 + rnd.randint(0, 10 ** 9))]}
        elif kind == "shopping":
            yield "shopping", {"msg_type": 2, "promotion_id": str(10 ** 18 + rnd.randint(0, 10 ** 9))}


class PumpWorker(threading.Thread):
    """一个合成主播：真实 DataRecorder 落盘 + 按速率 pump 事件"""

    def __init__(self, idx, base_dir, rate, stop_event):
        super().__init__(daemon=True, name=f"pump-{idx}")
        self.idx = idx
        self.recorder = DataRecorder(room_id=f"synth{idx}", live_id=f"synth{idx}", base_dir=base_dir)
        self.recorder.set_room_info({
            "title": f"合成主播{idx}", "anchor_nickname": f"合成主播{idx}",
            "anchor_id": str(idx), "room_status": "正在直播",
        })
        self.gen = make_event_gen(seed=idx)
        self.rate = rate
        self.stop_event = stop_event
        self.counts = {k: 0 for k in EVENT_WEIGHTS}  # 各类型发射计数（含 fallback）
        self.errors = 0

    def run(self):
        interval = 1.0 / self.rate if self.rate and self.rate > 0 else 0
        next_t = time.time()
        try:
            while not self.stop_event.is_set():
                et, data = next(self.gen)
                try:
                    self.recorder.on_event(et, data)
                    self.counts[et] = self.counts.get(et, 0) + 1
                except Exception:
                    self.errors += 1
                next_t += interval
                now = time.time()
                if next_t > now:
                    time.sleep(next_t - now)
        finally:
            self.recorder.close()

    @property
    def path(self):
        return self.recorder.path

    def integrity_ok(self):
        """校验发射事件数 == 落盘 jsonl 行数（chat→danmaku，stats→series，其余→events）"""
        if self.errors:
            return False
        danmaku = _count_lines(os.path.join(self.path, "danmaku.jsonl"))
        series = _count_lines(os.path.join(self.path, "series.jsonl"))
        events = _count_lines(os.path.join(self.path, "events.jsonl"))
        chat = self.counts.get("chat", 0)
        stats = self.counts.get("stats", 0)
        others = sum(v for k, v in self.counts.items() if k not in ("chat", "stats"))
        return danmaku == chat and series == stats and events == others


def run_pump(args):
    out_dir = _make_out_dir(args, f"pump-N{args.anchors}")
    stop = threading.Event()
    sampler = ResourceSampler(os.path.join(out_dir, "metrics.jsonl"), interval=5,
                              label=f"pump-N{args.anchors}")
    sampler.start()

    workers = []
    lock = threading.Lock()

    def start_one(idx):
        w = PumpWorker(idx, out_dir, args.rate, stop)
        w.start()
        with lock:
            workers.append(w)

    print(f"[pump] 输出目录 {out_dir}，目标 {args.anchors} 主播，速率 {args.rate} 事件/s/主播")
    launcher = RampLauncher(args, start_one, args.anchors, stagger=False)
    t_first = launcher.run(stop)
    startup = launcher.startup_seconds

    # 稳态运行到 duration（**从第一个 worker 启动起算**，不是从启动全部之后）
    try:
        remain = max(0, args.duration - (time.time() - (t_first or time.time())))
        stop.wait(remain)
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=5)
        sampler.stop()

    bad = [w.idx for w in workers if not w.integrity_ok()]
    total = sum(sum(w.counts.values()) for w in workers)
    summary = sampler.summary()

    print("\n" + "=" * 60)
    print("pump 结果汇总")
    print("=" * 60)
    print(f"并发主播数: {len(workers)}  总事件: {total}  启动耗时: {startup}s")
    print(f"资源峰值: {json.dumps(summary, ensure_ascii=False)}")
    print(f"数据完整性: {'全部通过' if not bad else f'失败 {len(bad)} 个（idx={bad[:10]}）'}")

    _write_report(out_dir, {
        "mode": "pump", "args": vars(args), "out_dir": out_dir,
        "anchors_started": len(workers), "total_events": total,
        "startup_seconds": startup,
        "events_per_sec": round(total / max(1, args.duration), 1),
        "integrity_bad": bad, "integrity_ok": not bad,
        "resource": summary,
    })


# ============================================================================
# v8：两条签名路径分开测
# ============================================================================

# liveMan 里有**两条独立的签名路径**，每次调用的成本差一个数量级，不能混为一谈：
#   WS 握手  `generateSignature`  → eval `sign.js`（492KB！）+ call `get_sign`    每场一次
#   HTTP 请求 `generate_a_bogus`  → eval `douyin_old_algo_ref.js`（15.7KB）+ call `sign_datail`
#                                                                              每次请求一次
# 旧版只测了 `sign_datail`，而 100 并发下真正吃内存的是 **`get_sign`**（eval 492KB）。
V8_PATHS = [
    {"name": "ws_get_sign", "script": "sign.js", "fn": "get_sign",
     "script_args": ("d41d8cd98f00b204e9800998ecf8427e",),
     "desc": "WS 握手路径（generateSignature → sign.js get_sign）"},
    {"name": "http_sign_datail", "script": "lib/reverse/douyin_old_algo_ref.js", "fn": "sign_datail",
     "script_args": ("aid=6383&live_id=1&test=1", "Mozilla/5.0"),
     "desc": "HTTP 签名路径（generate_a_bogus → douyin_old_algo_ref.js sign_datail）"},
]


def _v8_measure(script, fn, script_args, loops, fresh_ctx):
    """跑 loops 次「eval + call」，返回 (RSS 增量 MB, 耗时 s)。

    `fresh_ctx=True` 模拟**当前 liveMan 行为**（每次调用新建 MiniRacer）；
    `False` 模拟单例缓存（eval 一次，之后复用）。

    ⚠️ 测「每次新建」时必须 `del ctx` + `gc.collect()`：旧版循环里 `ctx` 被反复覆盖
    而不释放，测的其实是「泄漏式堆积」而非 liveMan 的真实行为（liveMan 里 ctx 是函数
    局部变量，函数返回即回收），数字会**虚高**到不能用来做决策。
    """
    import psutil
    from py_mini_racer import MiniRacer
    proc = psutil.Process()

    def rss_mb():
        return proc.memory_info().rss / 1048576

    gc.collect()
    base = rss_mb()
    t0 = time.time()
    if fresh_ctx:
        for _ in range(loops):
            ctx = MiniRacer()
            ctx.eval(script)
            try:
                ctx.call(fn, *script_args)
            except Exception:
                pass
            del ctx            # ← 关键：模拟真实作用域结束
            gc.collect()
    else:
        ctx = MiniRacer()
        ctx.eval(script)
        for _ in range(loops):
            try:
                ctx.call(fn, *script_args)
            except Exception:
                pass
    dt = time.time() - t0
    return round(rss_mb() - base, 1), round(dt, 3)


def run_v8(args):
    import py_mini_racer          # 提前 import：缺依赖时立刻报清楚，而不是跑一半才炸
    _ = py_mini_racer
    loops = args.anchors
    per_path = {}
    print(f"[v8] 每条路径循环 {loops} 次（对应 {loops} 个主播的规模）\n")
    for spec in V8_PATHS:
        path = spec["script"]
        if not os.path.exists(path):
            print(f"[v8] ⚠️ 跳过 {spec['name']}：脚本不存在 {path}\n")
            continue
        with codecs.open(path, "r", encoding="utf8") as f:
            script = f.read()
        print(f"--- {spec['name']}：{spec['desc']} ---")
        print(f"    脚本 {path}（{len(script) // 1024} KB）")
        fresh_mb, fresh_s = _v8_measure(script, spec["fn"], spec["script_args"], loops, fresh_ctx=True)
        single_mb, single_s = _v8_measure(script, spec["fn"], spec["script_args"], loops, fresh_ctx=False)
        print(f"    [每次新建] RSS 增量 {fresh_mb:>8.1f} MB  耗时 {fresh_s:>7.2f}s"
              f"（{fresh_s / loops * 1000:.1f} ms/次）")
        print(f"    [单例缓存] RSS 增量 {single_mb:>8.1f} MB  耗时 {single_s:>7.2f}s"
              f"（{single_s / loops * 1000:.1f} ms/次）")
        print(f"    → 可省内存 {fresh_mb - single_mb:.1f} MB / 提速 {max(0, fresh_s - single_s):.2f}s\n")
        per_path[spec["name"]] = {
            "desc": spec["desc"], "script": path, "script_kb": len(script) // 1024,
            "loops": loops,
            "fresh_rss_mb": fresh_mb, "fresh_seconds": fresh_s,
            "singleton_rss_mb": single_mb, "singleton_seconds": single_s,
            "saved_rss_mb": round(fresh_mb - single_mb, 1),
            "saved_seconds": round(max(0, fresh_s - single_s), 3),
        }

    out_dir = _make_out_dir(args, "v8")
    _write_report(out_dir, {"mode": "v8", "args": vars(args), "out_dir": out_dir, "paths": per_path})


# ============================================================================
# network：真实网络压测（口径 A / B 共用）
# ============================================================================

def _load_rooms_file(path):
    """每行 `名字 抖音号`（名字可省）；取**最后一段**作 live_id"""
    rooms = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            lid = parts[-1]
            name = parts[0] if len(parts) > 1 else lid
            rooms.append((name, lid))
    return rooms


def _load_rooms_roster(xlsx_path, sheet=None):
    """复用 `auto_crawl.load_anchors` 的表头驱动解析（口径 B：名单 = 要连的房间）"""
    from auto_crawl import load_anchors
    return load_anchors(xlsx_path, sheet=sheet)


def _resolve_rooms(args):
    if args.roster:
        return _load_rooms_roster(args.roster, args.sheet)
    if args.rooms_file:
        return _load_rooms_file(args.rooms_file)
    return []


def run_network(args):
    """N 个真实 fetcher 连真实在播房间，记录资源峰值 / 错误分布 / 实际建立 WS 数"""
    from liveMan import DouyinLiveWebFetcher, is_risk_control_444

    rooms = _resolve_rooms(args)
    if not rooms:
        print("[network] 错误：没有可用房间列表。给 `--rooms-file live3.txt`（口径 A：复用少数房间）"
              "或 `--roster documents/并发测试.xlsx --sheet 40`（口径 B：按真名单）")
        return

    reuse = args.anchors / len(rooms)
    mode_desc = ("口径 A：复用少数房间凑满 N 路" if len(rooms) < args.anchors
                 else "口径 B：一路一房间（按真名单）")
    print(f"[network] {mode_desc}")
    print(f"          连接数 {args.anchors}，房间数 {len(rooms)}"
          f"（{'每个房间被复用 ' + format(reuse, '.1f') + ' 次' if reuse > 1 else '不复用'}），"
          f"时长 {args.duration}s，落盘 {args.record}")

    out_dir = _make_out_dir(args, f"network-N{args.anchors}")
    # 落盘目录**按并发档位分开**：`data/_load_test/<N>/`。压测产物是成百上千个
    # `live_*` 场次目录（同一房间被复用 N/M 次 = N/M 份重复流），混进
    # `data/<日期>/` 里会直接淹掉生产数据，所以单独收在带下划线前缀的目录下
    # （与 `data/_test_residue/` 同一约定）。
    data_dir = args.data_dir or os.path.join("data", "_load_test", str(args.anchors))
    if args.record:
        os.makedirs(data_dir, exist_ok=True)
        print(f"[network] 落盘目录 {data_dir}/")
    sampler = ResourceSampler(os.path.join(out_dir, "metrics.jsonl"), interval=5,
                              label=f"network-N{args.anchors}")
    sampler.start()

    # **ERROR 与 WARN 分开计数**。合成一个"错误率"是错的：WARN 里绝大多数是
    # `未注册处理器的消息类型`（正常现象，抖音新增消息类型）和
    # `警告：第N位用户数据缺失`（排行榜不全），都不是失败。
    # 444 则属独立的**网络**维度（IP 风控），按用户要求只记账，不进任何成功率分母。
    err = {"http_444": 0, "http_other": 0, "room_id": 0, "ws": 0, "parse": 0, "other": 0}
    warn = {"other": 0}
    # 每类留前几条**原文**：光有计数无法判断桶里是致命错误还是无害噪声
    err_samples = {}
    lock = threading.Lock()
    counters = {"ws_connected": 0, "ws_closed": 0, "recorders": 0, "records": 0}

    def _classify(msg):
        # ⚠️ 判据在 `liveMan.is_risk_control_444`（单一来源），**不能**用子串 `"444" in msg`：
        # 实测（2026-10-15）那样会把 user_id / 时间戳里的 "444" 全算成风控命中 ——
        # 干净窗口里 naive 数出 50 行，真 444 是 0 行。
        # （必须先于下面的 `请求直播` 分支判断，否则真 444 会被误吞进 http_other。）
        if is_risk_control_444(msg):
            return "http_444"
        if "请求直播" in msg or "请求直播间" in msg or "request" in msg.lower():
            return "http_other"
        if "roomId" in msg or "未开播" in msg or "room_id" in msg:
            return "room_id"
        if "WebSocket" in msg:
            return "ws"
        if "解析" in msg:
            return "parse"
        return "other"

    def log_cb(level, msg):
        if level not in ("ERROR", "WARN"):
            return
        with lock:
            if level == "WARN":
                warn["other"] += 1
                bucket = warn
            else:
                bucket = err
                bucket[_classify(msg)] += 1
            samples = err_samples.setdefault(f"{level}:{_classify(msg)}", [])
            if len(samples) < 3 and msg not in samples:
                samples.append(msg[:160])

    units = []          # [(fetcher, thread, recorder)]
    stop = threading.Event()

    def start_one(idx):
        name, lid = rooms[idx % len(rooms)]
        f = DouyinLiveWebFetcher(lid, log_callback=log_cb)
        _instrument_open(f, counters, lock)
        rec = None
        if args.record:
            # 落盘才是生产形态：data_recorder 每场 4 个常开句柄 + 每条事件 flush()，
            # 这两个正是「能带几路」的潜在瓶颈，不接 recorder 测出来的数字会偏乐观。
            try:
                rid = f.room_id          # 解析失败返回 None（风控/未开播）
                if not rid:
                    # 必须先判空：`DataRecorder(None, ...)` 会建出 `live_None_<ts>`
                    # 这种垃圾目录，压 100 路时能凭空多出几十个
                    print(f"[network] #{idx} room_id 解析失败，本路不落盘")
                else:
                    rec = DataRecorder(rid, lid, base_dir=data_dir)
                    rec.set_room_info({"title": name, "anchor_nickname": name,
                                       "anchor_id": "", "room_status": "压测"})
                    f.data_callback = rec.on_event
                    with lock:
                        counters["recorders"] += 1
            except Exception as e:
                print(f"[network] #{idx} 建 recorder 失败: {type(e).__name__}: {e}")
        t = threading.Thread(target=_run_unit, args=(f, stop, counters, lock), daemon=True,
                             name=f"net-{idx}")
        t.start()
        with lock:
            units.append((f, t, rec))

    print(f"[network] 输出目录 {out_dir}")
    launcher = RampLauncher(args, start_one, args.anchors, stagger=True,
                            concurrency=args.concurrency)
    t_first = launcher.run(stop)
    startup = launcher.startup_seconds

    # duration **从第一条连接启动起算**（旧版从「全部启动完」起算，于是 --stagger 与
    # ramp 会让实际运行时长被悄悄拉长到 duration + 启动耗时）
    steady_actual = 0
    try:
        remain = max(0, args.duration - (time.time() - (t_first or time.time())))
        steady_actual = int(remain)
        print(f"[network] 启动完成（{startup}s，{launcher.started} 路），稳态运行 {int(remain)}s...")
        stop.wait(remain)
    except KeyboardInterrupt:
        print("\n[network] 收到中断，提前收尾...")
    finally:
        stop.set()
        # 收尾必须**并行**：`fetcher.stop()` 里对心跳线程有 `join(timeout=1.0)`，
        # 100 路串行收尾就是白等 100s（会把「停得慢」误记成本次测试的开销）。
        # 先全体置 running=False，再并行走 stop()。
        for f, _, _ in list(units):
            try:
                f.running = False
            except Exception:
                pass
        stoppers = [threading.Thread(target=_safe_stop, args=(f,), daemon=True)
                    for f, _, _ in list(units)]
        for s in stoppers:
            s.start()
        for s in stoppers:
            s.join(timeout=15)
        for _, t, _ in list(units):
            t.join(timeout=10)
        for _, _, rec in list(units):
            if rec is not None:
                try:
                    rec.close()
                except Exception:
                    pass
        sampler.stop()

    summary = sampler.summary()
    print("\n" + "=" * 60)
    print("network 结果汇总")
    print("=" * 60)
    connected = counters["ws_connected"]
    fail = len(units) - connected
    print(f"连接数: {len(units)}（**建起 WS {connected}**，未连上 {fail}）  启动耗时: {startup}s")
    print(f"ERROR 分布: {json.dumps(err, ensure_ascii=False)}")
    print(f"WARN  分布: {json.dumps(warn, ensure_ascii=False)}（正常现象，非失败）")
    for k, v in err_samples.items():
        print(f"    样本 {k}: {v[0] if v else ''}")
    if err["http_444"]:
        print("  ⚠️ 444 是**静默风控**（nginx 无响应状态码）。本机住宅 IP 下通常不出现；"
              "出现则说明该环境被 IP 风控 —— 这是**网络容量**问题，不是代码容量问题。")
    print(f"落盘: recorder {counters['recorders']} 个 → {data_dir}/")
    print(f"资源峰值: {json.dumps(summary, ensure_ascii=False)}")

    _write_report(out_dir, {
        "mode": "network", "args": vars(args), "out_dir": out_dir, "roster_mode": mode_desc,
        "connections": len(units), "distinct_rooms": len(rooms),
        "ws_connected": connected, "ws_connect_failed": fail,
        "ws_closed": counters["ws_closed"],
        # steady_seconds 必须是**实测**窗口（= duration − 启动耗时），不是配置的
        # duration —— 原实现存的是 args.duration，于是「稳态跑满 N 分钟」这件事
        # 无法从报告里核验（报告说 700，实测只有 624）
        "startup_seconds": startup, "steady_seconds": steady_actual,
        "duration_configured": args.duration,
        "errors": err, "warnings": warn, "error_samples": err_samples,
        "recorders": counters["recorders"],
        "data_dir": data_dir if args.record else None,
        "resource": summary,
        "caveat": "本机结论 = 代码容量，不含网络/IP 风控维度；444 仅记账",
    })


# ============================================================================
# media：音视频并发压测（MediaCapture，不启 WS）
# ============================================================================
#
# 为什么单开一个模式：音视频的**全部成本在 ffmpeg 子进程里**，而 network 模式测的是
# WS/落盘链路，两者互不相干。混在一起测无法归因（谁把 CPU 吃掉了？）。
# 本模式**不建 WS**，每个 fetcher 只用来 fetch_live_html() 拿流地址。
#
# ⚠️ 关键：一路 = **2 个 ffmpeg**（音频切片 + 视频抽帧），且 ffmpeg 走 HTTP 拉 FLV 时
# 是**整条复用流全量读入**——`-vn` 只是不解码视频，**网络仍要收全量**。所以一路的
# 带宽 ≈ 2 × 流码率，这是「最大并发」的硬约束之一，必须单独记账。

def _ffprobe_stream(url, timeout=30):
    """用 ffprobe 读直播流的 分辨率/帧率/码率。解码成本主要由分辨率主导，必须记账"""
    cmd = ["ffprobe", "-v", "error", "-rw_timeout", "15000000",
           "-select_streams", "v:0",
           "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,bit_rate",
           "-of", "json", "-i", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if r.returncode != 0:
        return {"error": (r.stderr or '').strip()[:200]}
    try:
        st = (json.loads(r.stdout).get("streams") or [{}])[0]
    except Exception as e:
        return {"error": f"parse: {e}"}
    return {"codec": st.get("codec_name"), "width": st.get("width"),
            "height": st.get("height"),
            "fps": st.get("avg_frame_rate") or st.get("r_frame_rate"),
            "bit_rate": st.get("bit_rate")}


def _count_media_bytes(cap):
    """一路当前已落盘的 (音频字节, 帧数)。稳态前后各取一次，差值才是这一窗口的产出"""
    ab = 0
    for p in glob.glob(os.path.join(cap.audio_dir, "segment_*.wav")):
        try:
            ab += os.path.getsize(p)
        except OSError:
            pass
    return ab, len(glob.glob(os.path.join(cap.frames_dir, "frame_*.jpg")))


def run_media(args):
    """N 路 MediaCapture 并发：测资源占用 + **可持续上限**

    判据不是「进程还活着」而是**音频实时倍率**：16kHz 单声道 s16 = 32 KB/s，
    倍率 = 实测写入速率 ÷ 32000。**< 1 就说明解码跟不上直播实时**，数据已经在丢
    （CPU 打到 100% 但倍率 0.7 时，看起来「在跑」，实际是坏的）。
    """
    import psutil
    from liveMan import DouyinLiveWebFetcher, MediaCapture

    rooms = _resolve_rooms(args)
    if not rooms:
        print("[media] 错误：没有可用房间列表。给 `--rooms-file live3.txt`（口径 A：复用）"
              "或 `--roster documents/huitun_10_100w_list.xlsx`（口径 B：一路一房间）")
        return
    n = args.anchors
    if len(rooms) < n:
        print(f"[media] ⚠️ 只有 {len(rooms)} 个房间、要 {n} 路 → 复用（每房间 {n / len(rooms):.1f} 路）。"
              f"复用不影响 CPU/带宽折算，但少了真实房间之间的码率差异")

    # label 里带合并/拆分：前后对照要能一眼分清哪个目录是哪一版
    out_dir = _make_out_dir(args, f"media-N{n}{'' if args.merged else '-split'}")
    data_dir = args.data_dir or os.path.join("data", "_load_test", f"media_{n}")
    os.makedirs(data_dir, exist_ok=True)
    print(f"[media] {n} 路 MediaCapture（不启 WS），稳态 {args.duration}s，预热 {args.warmup}s，"
          f"画质档 url_index={args.url_index}")
    print(f"[media] 落盘 {data_dir}/  报告 {out_dir}")

    # ---- 每个不同房间的流参数：分辨率决定解码成本，必须记账 ----
    dist, seen = [], set()
    for _, lid in rooms:
        if lid not in seen:
            seen.add(lid)
            dist.append(lid)
    room_profiles = {}
    for lid in dist[:n]:
        name = next(nm for nm, d in rooms if d == lid)
        url, prof = None, None
        try:
            url = MediaCapture(lid, os.path.join(data_dir, "_probe"),
                               fetcher=DouyinLiveWebFetcher(lid, None))._resolve_stream_url()
        except Exception as e:
            prof = {"error": f"{type(e).__name__}: {e}"}
        if prof is None:
            prof = _ffprobe_stream(url) if url else {"error": "无流地址（未开播？）"}
        prof["url_head"] = (url or '')[:70]
        room_profiles[lid] = prof
        print(f"[media]   房间 {name} ({lid}) → {prof.get('width')}x{prof.get('height')} "
              f"{prof.get('fps')} {prof.get('bit_rate')}bps {prof.get('error') or ''}")

    # ---- 起采样器 + 错峰启动 ----
    t_sampler0 = time.monotonic()
    sampler = ResourceSampler(os.path.join(out_dir, "metrics.jsonl"), interval=5,
                              label=f"media-N{n}", children=True, io=True).start()
    caps = []
    for i in range(n):
        name, lid = rooms[i % len(rooms)]
        cap = MediaCapture(lid, os.path.join(data_dir, f"cap_{i:02d}"),
                           fetcher=DouyinLiveWebFetcher(lid, None),
                           url_index=args.url_index, merged=args.merged,
                           log=lambda *_: None)
        ok = cap.start_media()
        caps.append({"idx": i, "name": name, "live_id": lid, "cap": cap, "started": ok})
        if not ok:
            print(f"[media] #{i:02d} {name}({lid}) 未解析到流地址 → 本路失败")
        if args.stagger > 0 and i < n - 1:
            time.sleep(args.stagger)
    started = sum(1 for c in caps if c["started"])
    n_ff = started * (1 if args.merged else 2)
    print(f"[media] 已起 {started}/{n} 路（{n_ff} 个 ffmpeg，"
          f"{'合并单进程' if args.merged else '音视双进程'}），预热 {args.warmup}s...")

    # ---- 预热 → 稳态（带 CPU / 内存守卫）----
    time.sleep(args.warmup)
    base = {c["idx"]: _count_media_bytes(c["cap"]) for c in caps}
    t_steady0 = time.time()
    # ⚠️ 两个时钟不能混：`end` 用 time.time()（纪元秒）走循环，`since` 必须换算到
    # 采样器内部单调时钟的基准上 —— 采样器样本里的 `t` 是 time.monotonic() 差值，
    # 拿纪元秒去减会得到天文数字，`summary(since=)` 直接筛成空集（已踩过一次）。
    since = time.monotonic() - t_sampler0
    guard_hit, over, cpu_now = None, 0, None
    print(f"[media] 稳态采样 {args.duration}s（守卫：CPU ≥ {args.cpu_guard}% 持续 10s / "
          f"可用内存 < {args.mem_guard_gb}GB 则提前收档）...")
    end = t_steady0 + args.duration
    while time.time() < end:
        time.sleep(2)
        try:
            cpu_now = psutil.cpu_percent(interval=None)
            avail_gb = psutil.virtual_memory().available / 1073741824
        except Exception:
            continue
        if avail_gb < args.mem_guard_gb:
            guard_hit = f"可用内存 {avail_gb:.1f}GB < {args.mem_guard_gb}GB"
            break
        if cpu_now >= args.cpu_guard:
            over += 1
            if over >= 5:
                guard_hit = f"整机 CPU {cpu_now:.0f}% ≥ {args.cpu_guard}% 连续 10s"
                break
        else:
            over = 0
    steady = round(time.time() - t_steady0, 1)
    if guard_hit:
        print(f"[media] ⚠️ 守卫触发，本档提前收尾（实际稳态 {steady}s）：{guard_hit}")

    # ---- 收尾：并行 stop_media（串行会把 2N 个进程的 10s 优雅退出首尾相接）----
    print("[media] 收尾：并行 stop_media() ...")
    after = {}

    def _stop_one(c):
        try:
            after[c["idx"]] = c["cap"].stop_media()
        except Exception as e:
            after[c["idx"]] = {"error": f"{type(e).__name__}: {e}"}

    ths = [threading.Thread(target=_stop_one, args=(c,), daemon=True) for c in caps]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=90)
    sampler.stop()

    # ---- 逐路健康 ----
    streams = []
    for c in caps:
        ab0, nf0 = base.get(c["idx"], (0, 0))
        ab1, nf1 = _count_media_bytes(c["cap"])
        m = after.get(c["idx"]) or {}
        d_ab, d_nf = ab1 - ab0, nf1 - nf0
        rt = round(d_ab / max(steady, 1e-6) / 32000, 3)
        exp_frames = steady / 10.0
        streams.append({
            "idx": c["idx"], "name": c["name"], "live_id": c["live_id"],
            "started": c["started"],
            "audio_bytes_in_window": d_ab, "frames_in_window": d_nf,
            "audio_files_total": len(m.get("audio") or []),
            "restarts": m.get("restarts", 0),
            "realtime_factor": rt,
            "expected_frames": round(exp_frames, 1),
            "frame_achieved": round(d_nf / exp_frames, 2) if exp_frames else None,
            "stop_error": m.get("error"),
        })
    started_streams = [s for s in streams if s["started"]]
    rts = [s["realtime_factor"] for s in started_streams] or [0]
    fas = [s["frame_achieved"] for s in started_streams
           if isinstance(s["frame_achieved"], (int, float))] or [0]
    restarts = sum(s["restarts"] for s in streams)

    all_sum, steady_sum = sampler.summary(), sampler.summary(since=since)
    bw = steady_sum.get("mean_net_recv_mb_s")
    bw_per = round(bw / started, 2) if bw and started else None

    print("\n" + "=" * 72)
    print(f"media 结果汇总（{n} 路，稳态 {steady}s）")
    print("=" * 72)
    print(f"起流: {started}/{n} 路（{started * 2} 个 ffmpeg）  续采(restarts): {restarts}")
    print(f"音频实时倍率: min {min(rts):.3f} / mean {sum(rts) / len(rts):.3f}"
          f"   （< 1 = 解码跟不上直播实时，数据在丢）")
    print(f"抽帧达成率  : min {min(fas):.2f}")
    print(f"稳态资源    : {json.dumps(steady_sum, ensure_ascii=False)}")
    print(f"带宽        : {bw} MB/s 全档 → 每路 {bw_per} MB/s"
          f"（≈ {round(bw_per * 8, 1) if bw_per else None} Mbps，对 ffprobe 的码率看是否 ≈2×）")
    if guard_hit:
        print(f"⚠️ 守卫: {guard_hit}")

    _write_report(out_dir, {
        "mode": "media", "args": vars(args), "out_dir": out_dir,
        "streams_requested": n, "distinct_rooms": len(dist), "streams_started": started,
        "warmup_seconds": args.warmup, "steady_seconds": steady,
        "duration_configured": args.duration, "guard_hit": guard_hit,
        "cpu_at_end": cpu_now, "url_index": args.url_index, "merged": args.merged,
        "room_profiles": room_profiles,
        "resource_all": all_sum, "resource_steady": steady_sum,
        "net_recv_mb_s": bw, "net_recv_mb_s_per_stream": bw_per,
        "realtime_factor_min": min(rts), "realtime_factor_mean": round(sum(rts) / len(rts), 3),
        "frame_achieved_min": min(fas), "total_restarts": restarts,
        "streams": streams,
        "caveat": "只测媒体子系统（不含 WS/文本与落盘链路）；本机结论=单机媒体容量。"
                  "带宽上限另受该机出口带宽约束，须按 net_recv_mb_s 对目标环境换算。",
    })


def _safe_stop(fetcher):
    try:
        fetcher.stop()
    except Exception:
        pass


def _instrument_open(fetcher, counters, lock):
    """包一层 `_wsOnOpen`，真实统计**握手成功**数。

    不能用「`start()` 有没有返回」或「耗时多久」之类的启发式 —— 444 被静默拒绝时
    `run_forever` 同样会跑起来再退出，必须看 `on_open` 回调才算数。`ws_connected`
    与 `connections` 的差值就是**没连上的路数**。
    """
    original = fetcher._wsOnOpen

    def counted(ws):
        with lock:
            counters["ws_connected"] += 1
        return original(ws)

    fetcher._wsOnOpen = counted


def _run_unit(fetcher, stop_event, counters, lock):
    """一条连接的线程体：建 WS → 阻塞在 `run_forever` 直到 stop()"""
    try:
        fetcher.start()
    except Exception:
        pass
    finally:
        with lock:
            counters["ws_closed"] += 1


def main():
    ap = argparse.ArgumentParser(description="并发承压测试（pump / v8 / network / media）")
    ap.add_argument("--mode", choices=["pump", "v8", "network", "media"], default="pump")
    ap.add_argument("--anchors", type=int, default=50,
                    help="network/pump/media：并发路数；v8：每路径循环次数")
    ap.add_argument("--duration", type=int, default=600,
                    help="稳态运行时长（秒）。network 从第一条连接启动起算；"
                         "media 从预热结束起算（media 建议 90）")
    ap.add_argument("--rate", type=int, default=10, help="pump：每主播事件速率（事件/秒）")
    ap.add_argument("--ramp", nargs=2, type=int, metavar=("STEP", "INTERVAL"),
                    help="每 INTERVAL 秒加 STEP 路，逐步加压（三种模式都支持）")
    ap.add_argument("--stagger", type=float, default=0.0,
                    help="连接/worker 之间的间隔秒数；0 = 用 1-3s 随机抖动（默认）")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="network：启动阶段的并发度（默认 8）。`start_one` 几乎全在等网络"
                         "（ttwid + room_id 解析），串行等于把 N 个往返首尾相接 —— "
                         "实测 8 路并发比串行快 6.2x。设 1 退回串行（做 A/B 用）。"
                         "⚠️ 别调太大：风控看瞬时并发量")
    ap.add_argument("--rooms-file", default=None,
                    help="network：房间列表文件，每行 `名字 抖音号`（口径 A 用它复用少数房间）")
    ap.add_argument("--roster", default=None,
                    help="network：xlsx 名单（口径 B），列按表头自动定位")
    ap.add_argument("--sheet", default=None,
                    help="配合 --roster：取该 sheet，如 40 取并发测试.xlsx 的 40 档")
    ap.add_argument("--record", action="store_true",
                    help="network：同时挂 DataRecorder 落盘（生产形态，测出的容量才可信）")
    ap.add_argument("--data-dir", default=None,
                    help="network + --record：落盘根目录"
                         "（默认 data/_load_test/<并发数>/，按档位分开存）")
    ap.add_argument("--out", default=None, help="输出目录（默认 load_test/<时间戳>_<label>）")
    # ---- media 模式专用 ----
    ap.add_argument("--warmup", type=int, default=20,
                    help="media：预热秒数。等 2N 个 ffmpeg 进入稳态再开始计稳态 —— "
                         "否则启动瞬间的进程初始化尖峰会污染稳态数字")
    ap.add_argument("--media-no-merge", dest="merged", action="store_false",
                    help="media：拆回**音视双进程**（ffmpeg 数 ×2、带宽 ×2）。默认合并单进程")
    ap.add_argument("--url-index", type=int, default=0,
                    help="media：画质档位（flv_pull_url 一般 4 档，0=最高画质/生产口径）。"
                         "解码成本由分辨率主导，这是不改口径就能验证的「上限杠杆」")
    ap.add_argument("--cpu-guard", type=float, default=97.0,
                    help="media：整机 CPU 连续 10s 超此值即提前收档（防止把机器拖死）")
    ap.add_argument("--mem-guard-gb", type=float, default=3.0,
                    help="media：可用内存低于此值即提前收档")
    args = ap.parse_args()

    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    except Exception:
        pass

    {"pump": run_pump, "v8": run_v8, "network": run_network,
     "media": run_media}[args.mode](args)


if __name__ == "__main__":
    main()
