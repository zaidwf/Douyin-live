#!/usr/bin/python
# coding:utf-8
"""5 主播并发全模态测试（服务器侧编排器）

**采集逻辑一行不写** —— 全部交给生产 `auto_crawl.py`（`--media --max-media N`），
本脚本只做四件事：筛出 N 个在播的、起进程、量资源、收尾出报告。

为什么这样编排（而不是自己起 fetcher/DataRecorder/MediaCapture）：
  要测的就是**真实生产路径**。自己再写一遍采集循环，测出来的数字对
  `auto_crawl` 在服务器上跑成什么样没有解释力，还会多一套会漂移的代码。

流程：
  1. 读名单（`auto_crawl.load_anchors`，表头驱动定位列）
  2. 按顺序限流探测，凑够 N 个在播的（可复现：取名单里**前 N 个**在播的）
  3. 写一份只含这 N 行的临时 xlsx
  4. `auto_crawl.py <tmp.xlsx> 30 --media --max-media N --poll-rate 6`
  5. `metrics.ResourceSampler(children=True, io=True)` 采**子进程树**（auto_crawl → ffmpeg）
  6. 预热 → 稳态窗口（默认 600s）→ SIGTERM（优雅落盘）
  7. 逐场统计 + 跑 `validate_fullmodal.py` 校验产物真实性，写 report.json

用法：
  python service/run_5anchors.py                                    # 默认 5 路 / 600s
  python service/run_5anchors.py --anchors 2 --duration 90           # 上服务器前先小规模预跑
  python service/run_5anchors.py --roster documents/xxx.xlsx --check-interval 30
"""
import argparse
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import liveMan   # 只用它的 `is_risk_control_444`（判据单一来源，见该函数注释）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_ROSTER = os.path.join("documents", "huitun_10_100w_1015_final.xlsx")


def _resolve_path(p):
    """相对路径按 `ROOT` 解析 —— 本文件里**其它所有路径**（`auto_crawl.py`、`data/`、
    `validate_fullmodal.py`、`dist/`）都是 `ROOT` 相对的，roster 不能是唯一按 cwd 解析的
    那个：那会让「换个 cwd 跑」变成「候选池找不到」，而报错完全指不到 cwd 上去。

    仍然兼容 cwd 相对的写法：`ROOT` 下找不到就按 cwd 找，都找不到才把 `ROOT` 下那个
    路径交回去（报错信息里由调用方给出两个候选路径）。
    """
    if os.path.isabs(p):
        return p
    cand = os.path.join(ROOT, p)
    return cand if os.path.exists(cand) else (p if os.path.exists(p) else cand)
# enter 探测限流：实测 6 req/s 干净、18 req/s 被 444 拦下 85%（见 modify.md 2026-10-10）。
# 不限流的话被 444 的房间会被误判成「没播」，在播池凭空缩水。
POLL_RATE = 6.0
WAV_BYTES_PER_SEC = 32000.0     # 16kHz 单声道 s16le = 32 KB/s，实时倍率的分母


# --------------------------------------------------------------------------
# 1. 在播探测（与 test/run_ladder.py 的 probe_rooms 同构 —— 这里刻意**不 import test/**，
#    那会把 load_test.py / summarize_ladder.py 一整串压测依赖拖进服务器部署包）
# --------------------------------------------------------------------------
def _probe_one(candidate):
    """探一个主播是否在播。返回 (name, did, is_live)"""
    name, did = candidate
    from liveMan import DouyinLiveWebFetcher
    try:
        f = DouyinLiveWebFetcher(did, None)
        ok, status, _, _ = f.get_room_status()
        return name, did, bool(ok and status == "正在直播")
    except Exception:
        return name, did, False


def probe_live(anchors, want, rate=POLL_RATE, concurrency=4):
    """从名单**按顺序**限流探测，凑够 `want` 个在播的

    **按名单顺序**返回而不是「谁先探到算谁」：否则每次跑用到的人是随机的，
    测试不可复现。分块提交、块内并发、块间串行（`ex.map` 保序）。
    """
    from auto_crawl import _RateLimiter
    limiter = _RateLimiter(rate)
    live = []
    real_out = sys.stdout
    i = 0
    while i < len(anchors) and len(live) < want:
        chunk = anchors[i:i + concurrency]
        i += concurrency

        def one(c):
            limiter.acquire()
            return _probe_one(c)

        # 吞掉 fetcher 的 [STATUS] 刷屏（会淹没结果），进度另行输出
        sink = io.StringIO()
        sys.stdout = sink
        try:
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                results = list(ex.map(one, chunk))
        finally:
            sys.stdout = real_out
        for name, did, ok in results:
            if ok and len(live) < want:
                live.append((name, did))
                print(f"    [在播] {len(live):>2}/{want}  {name} ({did})")
    return live


def write_roster_xlsx(path, pairs):
    """把筛选结果写成 auto_crawl 能读的 xlsx

    ⚠️ sheet 名必须叫 **`sheet1`**、且**带表头行**：`load_anchors` 会优先用
    `sheet1` 定位表头（分档 sheet 没有表头，靠它兜），首行命中「抖音号」候选名时
    按表头处理。名字/表头任一处不对，列就定位不到，直接 ValueError。
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "sheet1"
    ws.append(["昵称", "抖音号"])
    for name, did in pairs:
        ws.append([name, did])
    wb.save(path)


# --------------------------------------------------------------------------
# 2. 产物统计
# --------------------------------------------------------------------------
def _count_files(d, suffix):
    if not os.path.isdir(d):
        return 0
    return len([f for f in os.listdir(d)
                if f.endswith(suffix) and os.path.getsize(os.path.join(d, f)) > 0])


def dir_stats(path):
    """单场目录的模态统计（用于稳态窗口前后各取一次，差分出速率）"""
    au = os.path.join(path, "audio")
    ab = 0
    if os.path.isdir(au):
        for f in os.listdir(au):
            if f.endswith(".wav"):
                try:
                    ab += os.path.getsize(os.path.join(au, f))
                except OSError:
                    pass
    return {"audio_bytes": ab,
            "audio_segs": _count_files(au, ".wav"),
            "frames": _count_files(os.path.join(path, "frames"), ".jpg"),
            "danmaku": _count_jsonl(os.path.join(path, "danmaku.jsonl")),
            "events": _count_jsonl(os.path.join(path, "events.jsonl"))}


def _count_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def diff_stats(a, b):
    return {k: (b.get(k, 0) - a.get(k, 0)) for k in b}


# --------------------------------------------------------------------------
# 3. 主流程
# --------------------------------------------------------------------------
def main():
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="5 主播并发全模态测试编排器")
    ap.add_argument("--roster", default=DEFAULT_ROSTER, help="候选池 xlsx")
    ap.add_argument("--sheet", default=None, help="候选池的 sheet 名")
    ap.add_argument("--anchors", type=int, default=5, help="并发路数（= 筛几个在播的）")
    ap.add_argument("--duration", type=int, default=600, help="稳态采样时长（秒）")
    ap.add_argument("--warmup", type=int, default=75,
                    help="预热秒数：等全部 ffmpeg 起来再开稳态窗口。"
                         "必须 > check-interval（默认 30s）+ 初始随机延迟，否则"
                         "稳态窗口里会掺进「还没起流」的空档")
    ap.add_argument("--check-interval", type=int, default=30,
                    help="传给 auto_crawl 的开播检测间隔")
    ap.add_argument("--out", default=None, help="report.json 路径（默认 service/dist/）")
    ap.add_argument("--skip-validate", action="store_true", help="跳过 validate_fullmodal")
    args = ap.parse_args()

    if args.anchors < 1:
        ap.error("--anchors 必须 >= 1")

    from auto_crawl import load_anchors
    roster = _resolve_path(args.roster)
    if not os.path.exists(roster):
        ap.error(f"候选池找不到：{args.roster}\n"
                 f"  按仓库根目录解析为 {roster}\n"
                 f"  按当前目录解析为 {os.path.abspath(args.roster)}\n"
                 f"  部署包带了它（service/MANIFEST），解压后应从服务器上的 "
                 f"/opt/douyin-live 下运行；或用 --roster 给绝对路径。")
    anchors = load_anchors(roster, sheet=args.sheet)
    print(f"候选池：{roster} → {len(anchors)} 人")
    print(f"筛选 {args.anchors} 个在播（限流 {POLL_RATE:.0f} req/s，按名单顺序取前 N 个）...")
    live = probe_live(anchors, args.anchors)
    if len(live) < args.anchors:
        print(f"❌ 只筛到 {len(live)}/{args.anchors} 个在播的 —— 池子不够或探测被风控拦了。"
              f"\n   先确认服务器能正常访问抖音（curl https://live.douyin.com/），"
              f"或调小 --anchors")
        return 1
    print(f"✅ 确定的 {len(live)} 路：")
    for name, did in live:
        print(f"     {name} ({did})")

    # 起跑前先记下已有的场次目录，跑完用差集认出本次的产物
    data_root = os.path.join(ROOT, "data")
    before = set()
    for d in os.listdir(data_root) if os.path.isdir(data_root) else []:
        sub = os.path.join(data_root, d)
        if os.path.isdir(sub):
            before.update(os.path.join(sub, x) for x in os.listdir(sub)
                          if x.startswith("live_"))

    tmpdir = tempfile.mkdtemp(prefix="run5_")
    xlsx = os.path.join(tmpdir, "anchors.xlsx")
    write_roster_xlsx(xlsx, live)
    log_path = os.path.join(tmpdir, "auto_crawl.log")

    cmd = [sys.executable, os.path.join(ROOT, "auto_crawl.py"), xlsx,
           str(args.check_interval), "--media", "--max-media", str(len(live)),
           "--poll-rate", str(POLL_RATE)]
    print("\n" + "=" * 78)
    print("启动生产守护进程（子进程）：")
    print("  " + " ".join(cmd))
    print("=" * 78)

    from metrics import ResourceSampler
    sampler = ResourceSampler(os.path.join(tmpdir, "samples.jsonl"), interval=5.0,
                              label="run5", children=True, io=True).start()

    # stdout 要**同时**落文件（事后查 444 / 开播日志）、打屏（长跑要看）、
    # 并统计 444 次数 —— 所以起个读取线程，不用 communicate（那会憋到进程结束才给）
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace",
                            start_new_session=True)   # 自成进程组，SIGTERM 只打它

    state = {"444": 0, "started": [], "lines": 0}

    def pump():
        with open(log_path, "w", encoding="utf-8") as f:
            for line in proc.stdout:
                f.write(line)
                state["lines"] += 1
                # ⚠️ 判据在 `liveMan.is_risk_control_444`（单一来源）。**不要**退回
                # 子串 `"444" in line` —— 实测（2026-10-15）那样会把 user_id / 时间戳里
                # 恰好含 "444" 的行全算成风控命中：干净窗口里 naive 口径数出 **50** 行，
                # 而真 444 是 **0** 行，凭空造出一个「≈12% 残余风控」的假结论。
                if liveMan.is_risk_control_444(line):
                    state["444"] += 1
                if "检测到开播" in line:
                    state["started"].append(line.strip())
                    print("  " + line.rstrip())

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()

    print(f"\n预热 {args.warmup}s（等全部 ffmpeg 起流）...")
    time.sleep(args.warmup)
    # ⚠️ `_t0` 是 `time.monotonic()`（metrics.py:105），稳态起点必须用**同一个时钟**算 ——
    # 混 `time.time()` 会得到一个量级完全不对的 since，`summary()` 过滤后样本为空，
    # 报告里就只剩一个空 dict（本项目踩过一次，见 modify.md）
    t_warm = time.monotonic() - _sampler_t0(sampler)
    warm = {p: dir_stats(p) for p in _live_dirs(data_root, before)}
    print(f"预热结束，已起流 {len(warm)}/{len(live)} 场")

    print(f"稳态采样 {args.duration}s ...")
    t0 = time.time()
    while time.time() - t0 < args.duration:
        time.sleep(5)
        el = int(time.time() - t0)
        if el % 30 < 5:
            print(f"  … {el}/{args.duration}s  444 计数={state['444']}")
    steady_dt = time.time() - t0
    after = {p: dir_stats(p) for p in _live_dirs(data_root, before)}

    print("\n停子进程...")
    _stop_child(proc)
    pump_thread.join(timeout=5)
    sampler.stop()

    # ---- 汇总 ----
    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "platform": sys.platform,
        # Windows 上子进程是被 taskkill /T /F 硬杀的：没有 recorder.close()（因此**没有
        # room.json**，validate_fullmodal 的文本/数值模态必判 ❌），产物只在「硬杀那一刻」
        # 之前有效。读报告的人要能一眼分辨这是平台限制还是真缺陷。
        "graceful_shutdown": os.name != "nt",
        "roster": roster,          # 解析后的绝对路径（复现时不用猜 cwd）
        "anchors_requested": args.anchors,
        "anchors_live": [{"name": n, "did": d} for n, d in live],
        "check_interval": args.check_interval,
        "warmup_seconds": args.warmup,
        "steady_seconds": round(steady_dt, 1),
        "exit_code": proc.returncode,
        "http_444_lines": state["444"],   # 只计 `444 Client Error`（真风控命中），不含子串假阳性
        "started_events": len(state["started"]),
        "resource_steady": sampler.summary(since=t_warm),
        "per_anchor": {},
        "validation": {},
    }
    print("\n" + "=" * 78)
    print("逐场统计（稳态窗口内）")
    print("=" * 78)
    print(f"{'主播/目录':<34} {'帧':>6} {'音频段':>7} {'音频KB':>8} {'实时倍率':>9} "
          f"{'弹幕':>7} {'事件':>7}")
    print("-" * 78)
    for p in sorted(after):
        d = diff_stats(warm.get(p, {}), after[p])
        rt = d["audio_bytes"] / steady_dt / WAV_BYTES_PER_SEC
        name = os.path.basename(p)
        report["per_anchor"][name] = dict(d, realtime_factor=round(rt, 2),
                                          audio_kb=round(d["audio_bytes"] / 1024))
        print(f"{name:<34} {d['frames']:>6} {d['audio_segs']:>7} "
              f"{d['audio_bytes'] / 1024:>8.0f} {rt:>9.2f} {d['danmaku']:>7} {d['events']:>7}")
    print("-" * 78)
    rts = [v["realtime_factor"] for v in report["per_anchor"].values()]
    if rts:
        print(f"实时倍率最低 {min(rts):.2f} —— **< 1.0 表示解码跟不上直播实时，数据已在丢**")
    print(f"\n444 计数：{state['444']} 次（只计 `444 Client Error`，真风控命中；"
          f">0 说明 IP 仍被风控，代理没生效或需换出口）")
    res = report["resource_steady"]
    # ⚠️ `peak_children` 数的是**采样器的全部后代**（`metrics` 里 `psutil.Process()`
    # 指的是 run_5anchors 自己 → `children(recursive=True)`），而子进程树是
    # **auto_crawl + N 个 ffmpeg** —— 所以这里要**减掉 auto_crawl 本身**才是 ffmpeg 路数。
    # 不减就会显示成「--max-media 5 却起了 6 个」，看起来像闸门漏了（2026-10-14
    # 服务器实测踩过：报告写 6，`pgrep -c ffmpeg` 实为 5）。
    n_ff = res.get("peak_children")
    n_ff = (n_ff - 1) if isinstance(n_ff, int) and n_ff > 0 else n_ff
    report["resource_steady"]["peak_ffmpeg"] = n_ff
    print(f"资源（稳态）：ffmpeg 峰值 {n_ff} 个（= 子进程 {res.get('peak_children')} − 1 个 "
          f"auto_crawl 本身）/ {res.get('peak_children_cpu')}% CPU / "
          f"{res.get('peak_children_rss_mb')} MB RSS（整个子进程树，含 auto_crawl）/ "
          f"接收 {res.get('peak_net_recv_mb_s')} MB/s 峰值")

    if not args.skip_validate:
        print("\n产物真实性校验（validate_fullmodal）...")
        vp = os.path.join(ROOT, "test", "validate_fullmodal.py")
        for p in sorted(after):
            try:
                r = subprocess.run([sys.executable, vp, p], capture_output=True,
                                   text=True, timeout=180, encoding="utf-8",
                                   errors="replace")
                ok = "❌" not in (r.stdout or "") and r.returncode == 0
                report["validation"][os.path.basename(p)] = {
                    "ok": ok, "returncode": r.returncode,
                    "tail": (r.stdout or "").strip().splitlines()[-3:]}
                print(f"  {'✅' if ok else '❌'} {os.path.basename(p)}")
            except Exception as e:
                report["validation"][os.path.basename(p)] = {"ok": False, "error": str(e)}
                print(f"  ❌ {os.path.basename(p)}: {e}")
        if os.name == "nt":
            print("\n  ⚠️ 以上 ❌ 是 **Windows 预跑的预期结果**，不是编排器缺陷：Windows 上\n"
                  "     子进程被 `taskkill /T /F` 硬杀，`recorder.close()` 不跑 → **没有\n"
                  "     room.json** → 文本/数值模态必判 ❌。图像/音频模态不受影响（ffmpeg\n"
                  "     退出前落下的段是有效的）。**服务器（Linux）上是真正的 SIGTERM 优雅\n"
                  "     路径**，五模态应全 ✅ —— 那两个数才是这次实测要看的。")

    out = args.out or os.path.join(ROOT, "service", "dist",
                                   f"report_5anchors_{datetime.now():%Y%m%d_%H%M%S}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已存：{out}")
    print(f"原始日志：{log_path}")
    print(f"采样明细：{os.path.join(tmpdir, 'samples.jsonl')}")
    return 0


def _stop_child(proc, grace=120):
    """停掉 auto_crawl 子进程（**连它的 ffmpeg 树一起**）

    POSIX：SIGTERM → `auto_crawl` 的 `_install_signal_handlers` 会让每场
    `recorder.close()` + `capture.stop_media()` 落盘再退出，**ffmpeg 在 `stop_media()`
    里被收掉**，不会漏。这是服务器上的路径。

    ⚠️ **Windows 没有优雅可言**：Windows 的 `SIGTERM` 就是 `TerminateProcess`（硬杀），
    信号处理器根本不跑 —— `recorder.close()` 不执行（**产不出 `room.json`**，
    `validate_fullmodal` 因此判文本/数值模态 ❌），而且 `stop_media()` 不跑意味着
    **ffmpeg 变孤儿继续往盘里写**，一直写到被发现为止（本地预跑实测漏了 2 个 ffmpeg，
    音频多写出 150s）。所以 Windows 上直接 `taskkill /T /F` 收整棵树，
    不要假装它有优雅收尾。

    Windows 预跑的这两个后果（无 `room.json`、无优雅落盘）是**平台限制**，
    服务器（Linux）上是真正的 SIGTERM 优雅路径 —— 见 `service/README.md`。
    """
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        print(f"  已强杀进程树（Windows 无优雅收尾），返回码 {proc.returncode}")
        if r.returncode not in (0, 128):     # 128 = 进程本就没了
            print(f"  ⚠️ taskkill 返回 {r.returncode}: {(r.stderr or '').strip()}")
        return

    print(f"  发 SIGTERM，等优雅落盘（当前场次先 recorder.close + stop_media，{grace}s 预算）...")
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        print(f"  已退出，返回码 {proc.returncode}")
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ {grace}s 未退出，强杀（产物可能不完整）")
        proc.kill()
        proc.wait(timeout=10)


def _sampler_t0(sampler, timeout=10.0):
    """等采样线程写下时间锚点再取（`_t0` 是在采样线程里首次 `_snapshot()` 时赋的）

    不等就可能拿到 None，而 `summary(since=None)` 会**静默**退化成「统计全部样本」——
    等于把预热期的启动尖峰算进稳态，数字偏高却看不出来。
    """
    end = time.monotonic() + timeout
    while sampler._t0 is None and time.monotonic() < end:
        time.sleep(0.05)
    if sampler._t0 is None:
        raise RuntimeError("采样器没起来（psutil 未安装？）—— 拿不到稳态起点")
    return sampler._t0


def _live_dirs(data_root, before):
    """本次新产生的场次目录（与起跑前的快照做差集）"""
    now = set()
    for d in os.listdir(data_root) if os.path.isdir(data_root) else []:
        sub = os.path.join(data_root, d)
        if os.path.isdir(sub):
            now.update(os.path.join(sub, x) for x in os.listdir(sub)
                       if x.startswith("live_"))
    return sorted(now - before)


if __name__ == "__main__":
    sys.exit(main())
