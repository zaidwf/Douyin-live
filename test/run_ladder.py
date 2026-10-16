#!/usr/bin/python
# coding:utf-8 -*-
"""阶梯压测驱动器：网络档（20→40→60→80→100）与媒体档（1→2→4→…→20），逐档串行。

`--mode network`（默认）：N 路真实 WS 连接，见下方说明。
`--mode media`：N 路 `MediaCapture`（音频切片 + 抽帧），回答「单机能带几路音视频」。
  与 network 的区别：不启 WS、**不每档重探整份名单**（627 人限流扫一遍要 105s，
  每档都扫不起）—— 改为开跑前取一份在播池，每档从池里限流复探前 N 个仍在播的。

**必须串行**：并行跑两档会让 CPU/内存数字互相污染，得到的峰值没有意义。

每档开跑前从**完整名单按顺序重探**，凑够该档所需路数即停止探测，然后跑这一档；
下一档再从名单头重新探一遍。理由有两条，都是实测出来的：

  1. 一轮阶梯要跑约半小时，中途主播下播很正常 —— 拿已下播的房间去压，该档实际
     连接数会缩水（连接建得起来，但服务端一条消息都不发，落盘全是空壳）。
  2. 名单本身也在衰减：实测在播名单的**半衰期约 2-3 小时**（09:41 扫出的 142 人，
     到 13:16 只剩 40% 还在播）。所以必须用**开跑那一刻**仍在播的人。

凑够即停的意义：名单是按粉丝量降序排的，前面的都是大主播，命中率随位置下降；
探到够用就停可以省掉大部分探测时间。

用法：
  python test/run_ladder.py --roster documents/筛选结果_10w-100w_指定类目.xlsx --sheet Sheet1
  python test/run_ladder.py --roster ... --tiers 20 40 --steady 300
  python test/run_ladder.py                 # 不带 --roster → 退回内置 3 个候选（口径 A 复用）

音视频档（本轮新增）：
  python test/run_ladder.py --mode media \
      --roster documents/huitun_10_100w_list.xlsx --steady 90
  默认档位 1 2 4 6 8 10 12 16 20；每档稳态 90s，整条约 22-25 分钟。
  想快：`--tiers 1 2 4 8 16 --steady 60`（约 8 分钟）。

高并发档（接着上一轮 100 继续往上）：
  python test/run_ladder.py --roster ... --tiers 125 150 175 200 \
      --prev 100 --stop-below-prev
  加了 `--stop-below-prev` 时，每档先**扫完整份名单**取总在播数；总在播数 < 上一档已
  完成的并发数就停止整条阶梯 —— 名单的在播数就是这一轮的天花板，它没比上一档更多时，
  更高并发只会在同一批人上复用，再往上跑没有意义。
"""
import argparse
import io
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_CANDIDATES = [
    ("金山银山", "164425316"),
    ("壮家阿媛", "570722959"),
    ("DACC服饰穿搭", "95687589924"),
]
DEFAULT_TIERS = {"network": [20, 40, 60, 80, 100],
                 "media": [1, 2, 4, 6, 8, 10, 12, 16, 20]}
DEFAULT_STEADY = {"network": 300, "media": 90}


def _probe_one(candidate):
    """探一个主播是否在播。返回 (name, live_id, is_live)"""
    name, did = candidate
    from liveMan import DouyinLiveWebFetcher
    try:
        f = DouyinLiveWebFetcher(did, None)
        ok, status, _, _ = f.get_room_status()
        return name, did, bool(ok and status == "正在直播")
    except Exception:
        return name, did, False


def probe_rooms(candidates, want, concurrency=8, full=False):
    """从候选池**按顺序**探测出当前在播的房间。

    并发探测但**结果按名单顺序**返回（分块提交、块内并发、块间串行）—— 阶梯取的是
    「名单里前 N 个在播的」，顺序必须可复现，否则每档用到的人是随机的。

    :param full: `False`（默认）凑够 `want` 即停止探测；`True` 则**扫完整份名单**，
                 返回全部在播的（此时 `want` 无意义，`len(返回值)` 就是总在播数）。
                 需要总数来判断「这一档还值不值得跑」时必须用 `full=True`。
    """
    live = []
    total = len(candidates)
    real_out = sys.stdout
    n_chunks = 0
    for start in range(0, total, concurrency):
        if not full and len(live) >= want:
            break
        chunk = candidates[start:start + concurrency]
        # 探测期间吞掉 fetcher 的 [STATUS] 打印（1684 人会淹没结果），进度另行输出
        sink = io.StringIO()
        sys.stdout = sink
        try:
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                results = list(ex.map(_probe_one, chunk))
        finally:
            sys.stdout = real_out
        n_chunks += 1
        for name, did, ok in results:
            if not ok:
                continue
            live.append((name, did))
            if not full:
                print(f"    [在播] {len(live):>3}/{want}  {name} ({did})")
            if not full and len(live) >= want:
                break
        # 进度每 10 块打一次：扫全量时逐块打会把结果冲掉
        if n_chunks % 10 == 0:
            print(f"    … 已探 {min(start + concurrency, total)}/{total}，"
                  f"在播 {len(live)}")
    return live


def load_live_pool(path):
    """从 `test_result/live_now_<date>.json` 读在播池（按在线人数降序）

    比每档重探整份名单便宜得多：627 人即使限流到 6 req/s 也要 105s，而 media 阶梯有
    9 档。池里的房间可能已经下播，所以每档仍要**复探一次**（见 probe_live_pool）。
    """
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    pool = [(r.get("nick") or r.get("did"), str(r.get("did")).strip())
            for r in d.get("live", []) if r.get("did")]
    return pool


def probe_live_pool(pool, want, rate=6.0, concurrency=4):
    """从池里**按顺序**限流复探，凑够 `want` 个仍在播的。

    ⚠️ 必须限流：enter 接口实测 6 req/s 干净、18 req/s 被 444 拦下 85%
    （见 modify.md 2026-10-10）。不限流的话被 444 的房间会被误判成「没播」，
    在播池凭空缩水，阶梯就会退化成复用。`concurrency` 只是把等待摊到多个线程，
    真正的速率由令牌桶卡住。
    """
    from auto_crawl import _RateLimiter
    limiter = _RateLimiter(rate)
    live = []
    real_out = sys.stdout
    i = 0
    while i < len(pool) and len(live) < want:
        chunk = pool[i:i + concurrency]
        i += concurrency

        def one(c):
            limiter.acquire()
            return _probe_one(c)

        # 吞掉 fetcher 的 [STATUS] 刷屏（同 probe_rooms），阶梯一共要探 9 档
        sink = io.StringIO()
        sys.stdout = sink
        try:
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                results = list(ex.map(one, chunk))   # ex.map 保序
        finally:
            sys.stdout = real_out
        for name, did, ok in results:
            if ok and len(live) < want:
                live.append((name, did))
                print(f"    [在播] {len(live):>3}/{want}  {name} ({did})")
    return live


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["network", "media"], default="network",
                    help="network：N 路真实 WS 连接（默认）；media：N 路音视频采集")
    ap.add_argument("--roster", default=None,
                    help="候选池 xlsx（完整名单）。给了就每档从它按顺序重探；"
                         "不给则退回内置 3 个候选房间（口径 A，复用）")
    ap.add_argument("--sheet", default=None, help="--roster 的标签页名，如 Sheet1 / 20")
    ap.add_argument("--live-json", default=None,
                    help=f"media：在播池 JSON（默认 test_result/live_now_<今天>.json）。"
                         f"没有就先跑一次全名单探测生成它")
    ap.add_argument("--tiers", type=int, nargs="*", default=None,
                    help=f"逐档并发数。默认 network {DEFAULT_TIERS['network']} / "
                         f"media {DEFAULT_TIERS['media']}")
    ap.add_argument("--prev", type=int, default=None,
                    help="上一轮**已完成**的并发数（本轮的初始基准）。配合 "
                         "--stop-below-prev 使用，例如接着上一轮 100 的阶梯跑 125/150/…")
    ap.add_argument("--stop-below-prev", action="store_true",
                    help="每档开跑前**扫完整份名单**取总在播数；若总在播数 < 上一档已完成的"
                         "并发数，立即停止整条阶梯。理由：名单的在播数就是这一轮的天花板，"
                         "它没比上一档更多时，更高并发只会在同一批人上复用（降级为口径 A），"
                         "再往上跑没有意义。总在播数 < 本档所需但 ≥ 上一档时仍会跑，"
                         "只是会复用房间，届时会显式告警")
    ap.add_argument("--steady", type=int, default=None,
                    help=f"每档**稳态**时长（秒）。默认 network {DEFAULT_STEADY['network']} / "
                         f"media {DEFAULT_STEADY['media']}")
    ap.add_argument("--warmup", type=int, default=20, help="media：预热秒数（透传）")
    ap.add_argument("--url-index", type=int, default=0,
                    help="media：画质档位（0=最高画质，即生产口径；透传）")
    ap.add_argument("--media-no-merge", dest="merged", action="store_false",
                    help="media：拆回音视双进程（对比用；默认合并单进程）")
    ap.add_argument("--out-tag", default="",
                    help="输出目录标签后缀，如 'split' —— 前后对照时区分两轮（透传）")
    ap.add_argument("--stagger", type=float, default=0.5,
                    help="连接提交间隔秒（用于估算启动耗时，并透传给 load_test）")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="启动与探测的并发度（透传给 load_test）")
    ap.add_argument("--data-root", default=os.path.join(ROOT, "data", "_load_test"),
                    help="落盘根目录，各档落在其下的 <N>/（media 为 media_<N>/）子目录")
    ap.add_argument("--no-record", dest="record", action="store_false",
                    help="network：不落盘（默认落盘到 data/_load_test/<N>/）")
    ap.add_argument("--out-root", default=os.path.join(ROOT, "load_test"))
    args = ap.parse_args()
    if args.tiers is None:
        args.tiers = DEFAULT_TIERS[args.mode]
    if args.steady is None:
        args.steady = DEFAULT_STEADY[args.mode]

    # 候选池
    if args.mode == "media":
        live_json = args.live_json or os.path.join(
            ROOT, "test_result", f"live_now_{datetime.now().strftime('%Y%m%d')}.json")
        if not os.path.exists(live_json):
            print(f"media：在播池 {live_json} 不存在。先跑一次全名单探测生成它"
                  f"（参考 test_result/live_now_*.json 的生成方式），或 --live-json 指定")
            return
        candidates = load_live_pool(live_json)
        pool_src = live_json
        tag = "M"
        mode = "音视频档：每档从在播池限流复探，凑够 N 路 1:1"
    elif args.roster:
        from load_test import _load_rooms_roster
        candidates = _load_rooms_roster(args.roster, args.sheet)
        pool_src = args.roster + (f" sheet={args.sheet}" if args.sheet else "")
        tag = "B"
        mode = "口径 B：每档从完整名单按顺序重探，凑够 N 个在播即停"
    else:
        candidates = DEFAULT_CANDIDATES
        pool_src = "（内置 3 个）"
        tag = "A"
        mode = "口径 A：复用内置的 3 个候选房间凑满 N 路"
    if not candidates:
        print(f"候选池为空：{pool_src}")
        return

    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    except Exception:
        pass

    print("=" * 74)
    print(f"阶梯压测：{args.tiers}，每档稳态 {args.steady}s（{args.steady / 60:.0f} 分钟）")
    print(mode)
    print(f"候选池 {len(candidates)} 人：{pool_src}")
    print(f"起始时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.mode == "media":
        print(f"音视频落盘: {args.data_root}/media_<N>/  预热 {args.warmup}s  "
              f"画质档 url_index={args.url_index}（0=最高画质/生产口径）")
    else:
        print(f"落盘: {args.data_root}/<N>/"
              f"{'' if args.record else '（--no-record，不落盘）'}")
    print("=" * 74)

    summary = []
    prev_best = args.prev or 0     # 上一档已完成的并发数（停止规则的基准）
    for n in args.tiers:
        print(f"\n{'#' * 74}\n# 档位 {n} 路 —— {datetime.now().strftime('%H:%M:%S')}\n{'#' * 74}")
        t_probe = time.time()
        if args.mode == "media":
            # 限流复探：enter 接口 6 req/s 是安全线，超了会被 444 把在播房间误判成没播
            print(f"[在播池] 限流复探（6 req/s，concurrency {args.concurrency}），"
                  f"凑 {n} 个仍在播的…")
            live = probe_live_pool(candidates, want=n, concurrency=args.concurrency)
        elif args.stop_below_prev:
            print(f"[候选池] **扫完整份 {len(candidates)} 人名单**取总在播数"
                  f"（停止条件：总在播 < 上一档 {prev_best}）…")
            live = probe_rooms(candidates, want=n, concurrency=args.concurrency, full=True)
            print(f"  总在播数 {len(live)}")
            if len(live) < prev_best:
                print(f"  [停止] 总在播 {len(live)} < 上一档 {prev_best} —— 名单没有比上一档"
                      f"更多，再往上跑只会在同一批人上复用（降级为口径 A），停止阶梯")
                summary.append((n, f"stop: 总在播 {len(live)} < 上一档 {prev_best}"))
                break
        else:
            print(f"[候选池] 从 {len(candidates)} 人名单按顺序重探，凑 {n} 个在播的…")
            live = probe_rooms(candidates, want=n, concurrency=args.concurrency)
        probe_s = time.time() - t_probe
        if not live:
            print("  ❌ 没有在播的，跳过本档（可换候选池）")
            summary.append((n, "skip: 无在播"))
            continue
        print(f"  探测完成：{len(live)} 个在播（用时 {probe_s:.0f}s）")
        if args.mode != "media" and args.stop_below_prev:
            # 全量扫返回的是**全部**在播（按名单顺序），本档只需前 n 个
            live = live[:n]
        if len(live) < n:
            # 候选池不够 → load_test 会 rooms[i % len(rooms)] 复用，本档退化为口径 A
            print(f"  ⚠️ 只找到 {len(live)} 个（< {n} 路），本档会复用房间凑满 → 退化为口径 A")

        # 房间文件落在**本档自己的输出目录**里：报告因此自带「这一档用的是哪几个人」，
        # 否则半小时后回看 report.json 无法判断当时压的是谁（主播会轮换下播）
        out = os.path.join(args.out_root, f"{tag}{n}{args.out_tag}")
        os.makedirs(out, exist_ok=True)
        rooms_file = os.path.join(out, "rooms.txt")
        with open(rooms_file, "w", encoding="utf-8") as f:
            f.write("\n".join(f"{nm} {did}" for nm, did in live) + "\n")

        if args.mode == "media":
            # media 的 duration 从**预热结束**起算，不含启动耗时，所以不用加预算
            print(f"  本档 {n} 路 → {len(live)} 个房间"
                  + ("（1:1）" if len(live) == n else f"（复用，每房间 {n / len(live):.1f} 路）")
                  + f"，--duration {args.steady} + 预热 {args.warmup}（画质档 {args.url_index}）")
            cmd = [sys.executable, os.path.join(ROOT, "load_test.py"),
                   "--mode", "media", "--anchors", str(n),
                   "--rooms-file", rooms_file, "--out", out,
                   "--duration", str(args.steady), "--warmup", str(args.warmup),
                   "--url-index", str(args.url_index), "--stagger", str(args.stagger),
                   "--data-dir", os.path.join(args.data_root, f"media_{n}")]
            if not args.merged:
                cmd.append("--media-no-merge")
        else:
            # duration 从**第一条连接**起算，所以必须把启动耗时加进预算，否则稳态会被吃掉
            startup_est = int(n * max(args.stagger, 0.0)) + 5
            dur = args.steady + startup_est
            print(f"  本档 {n} 路 → {len(live)} 个房间"
                  + ("（1:1）" if len(live) == n else f"（复用，每房间 {n / len(live):.1f} 路）")
                  + f"，--duration {dur}（= 稳态 {args.steady} + 预估启动 {startup_est}）")
            cmd = [sys.executable, os.path.join(ROOT, "load_test.py"),
                   "--mode", "network", "--anchors", str(n),
                   "--rooms-file", rooms_file, "--duration", str(dur),
                   "--stagger", str(args.stagger), "--out", out,
                   "--concurrency", str(args.concurrency),
                   # 数据目录按**档位号**命名（`data/_load_test/<N>/`），报告目录才带口径前缀
                   "--data-dir", os.path.join(args.data_root, str(n))]
            if args.record:
                cmd.append("--record")
        t0 = time.time()
        r = subprocess.run(cmd, cwd=ROOT)
        dt = time.time() - t0
        rc = "ok" if r.returncode == 0 else f"exit={r.returncode}"
        print(f"\n[档位 {n}] 完成（{dt / 60:.1f} 分钟，{rc}），报告 {out}/report.json")
        summary.append((n, f"{rc}, {dt / 60:.1f} min, 在播 {len(live)}/{n}, "
                           f"探测 {probe_s:.0f}s"))
        # 本档成为下一档的停止基准（用户口径：与**上一档已完成的并发数**比）
        prev_best = n

        # 档间必须确认连接真的断了：下一档的资源峰值不能被上一档的残留连接污染
        time.sleep(5)
        try:
            ps = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                                capture_output=True, text=True, timeout=30)
            left = max(0, len(ps.stdout.strip().splitlines()) - 1)
        except Exception:
            left = -1
        print(f"[档位 {n}] 断连核验 —— 残留 python 进程数: {left}")

    print("\n" + "=" * 74)
    print("阶梯压测总览")
    print("=" * 74)
    for n, note in summary:
        print(f"  档位 {n:>4}: {note}")
    print(f"\n结束时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.mode == "media":
        print(f"下一步：python test/summarize_ladder.py --glob \"load_test/{tag}*/report.json\"")
    else:
        print("比对各档 report.json 的 resource.peak_rss_mb / peak_threads / peak_connections")


if __name__ == "__main__":
    main()
