#!/usr/bin/python
# coding:utf-8 -*-
"""并发承压测试：验证采集链路在主播规模扩大时的承受能力。

三种模式（`--mode`）：

| 模式 | 测什么 | 要网络吗 |
|---|---|---|
| `pump` | N 个**合成**主播按真实事件比例灌进真 `DataRecorder`，测线程/内存/磁盘/CPU 扩展性 + 数据完整性 | 否（离线，无风控风险） |
| `v8` | MiniRacer V8 上下文「每次新建 vs 单例缓存」的内存/耗时差，**两条签名路径分开测** | 否 |
| `network` | N 个**真实** fetcher 连真实在播房间，测资源上限 / 错误率 | 是 |

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
"""
import argparse
import codecs
import gc
import io
import json
import os
import random
import sys
import threading
import time
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

    def __init__(self, args, start_one, total, stagger=True):
        """
        :param start_one: 可调用对象，签名 `start_one(index) -> bool`（启动第 index 个）
        :param total: 目标总数
        :param stagger: 是否在启动之间错峰。**只有 network 需要**（避免瞬时并发触发
                        风控）；pump/v8 是本地线程/纯计算，错峰纯属白等 —— 旧版 pump
                        没有错峰逻辑，这次重构成公共类时别把它传染过去。
        """
        self.args = args
        self.start_one = start_one
        self.total = total
        self.stagger = stagger
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
        for _ in range(n):
            if self.started >= self.total:
                return
            if self.t_first is None:
                self.t_first = time.time()
            self.start_one(self.started)
            self.started += 1
            self._stagger()
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
    from liveMan import DouyinLiveWebFetcher

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
        if "444" in msg:
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
    launcher = RampLauncher(args, start_one, args.anchors, stagger=True)
    t_first = launcher.run(stop)
    startup = launcher.startup_seconds

    # duration **从第一条连接启动起算**（旧版从「全部启动完」起算，于是 --stagger 与
    # ramp 会让实际运行时长被悄悄拉长到 duration + 启动耗时）
    try:
        remain = max(0, args.duration - (time.time() - (t_first or time.time())))
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
        "startup_seconds": startup, "steady_seconds": args.duration,
        "errors": err, "warnings": warn, "error_samples": err_samples,
        "recorders": counters["recorders"],
        "data_dir": data_dir if args.record else None,
        "resource": summary,
        "caveat": "本机结论 = 代码容量，不含网络/IP 风控维度；444 仅记账",
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
    ap = argparse.ArgumentParser(description="并发承压测试（pump / v8 / network）")
    ap.add_argument("--mode", choices=["pump", "v8", "network"], default="pump")
    ap.add_argument("--anchors", type=int, default=50,
                    help="network/pump：并发连接数；v8：每路径循环次数")
    ap.add_argument("--duration", type=int, default=600,
                    help="稳态运行时长（秒）。**从第一条连接启动起算**，不含启动耗时")
    ap.add_argument("--rate", type=int, default=10, help="pump：每主播事件速率（事件/秒）")
    ap.add_argument("--ramp", nargs=2, type=int, metavar=("STEP", "INTERVAL"),
                    help="每 INTERVAL 秒加 STEP 路，逐步加压（三种模式都支持）")
    ap.add_argument("--stagger", type=float, default=0.0,
                    help="连接/worker 之间的间隔秒数；0 = 用 1-3s 随机抖动（默认）")
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
    args = ap.parse_args()

    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    except Exception:
        pass

    {"pump": run_pump, "v8": run_v8, "network": run_network}[args.mode](args)


if __name__ == "__main__":
    main()
