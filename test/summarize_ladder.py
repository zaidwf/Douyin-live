#!/usr/bin/python
# coding:utf-8 -*-
"""汇总口径 A 阶梯压测的各档 `report.json`，输出「并发数 vs 资源」对照表。

只读，不重跑。用于阶梯跑完后一次性出结论表 —— 报告本来就是为了能机器比对才落盘的，
手工从五份 JSON 里抠数字既慢又容易抄错。

用法：
  python test/summarize_ladder.py                 # 扫 load_test/A*/report.json
  python test/summarize_ladder.py --glob "load_test/**/report.json"
  python test/summarize_ladder.py --json out.json # 额外导出一份汇总
"""
import argparse
import glob
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_reports(pattern, mode="network"):
    rows = []
    for path in sorted(glob.glob(os.path.join(ROOT, pattern), recursive=True)):
        try:
            with open(path, encoding="utf-8") as f:
                r = json.load(f)
        except Exception as e:
            print(f"  跳过 {path}: {type(e).__name__}: {e}")
            continue
        if r.get("mode") != mode:
            continue
        rows.append((path, r))
    return rows


def _media_arm_name(merged):
    """把 report 里的 `merged` 字段翻译成 arm 名（老报告没有这个字段 = 改动前）"""
    if merged is True:
        return "合并单进程（改动后）"
    if merged is False:
        return "音视双进程（对照）"
    return "旧（无 merged 字段，改动前）"


def _media_table(rows, title):
    """打印单个 arm 的阶梯表，返回 (table, first_bad)"""
    # ⚠️ 必须按 **并发数** 排序，不能按路径：glob 出来是 M1/M10/M12/M16/M2/M20/M4/M6/M8
    # 的字符串序，直接打印会把阶梯显示成乱的，「首个超标档」也会取错。
    rows = sorted(rows, key=lambda pr: (pr[1].get("streams_requested") or 0))

    print("=" * 122)
    print(title)
    print("=" * 122)
    print(f"{'并发':>4} {'起流':>5} {'ffmpeg':>6} {'ffmpegCPU':>9} {'系统CPU峰':>8} "
          f"{'内存峰值MB':>10} {'带宽MB/s':>9} {'带宽/路MB':>9} {'实时倍率min':>10} "
          f"{'抽帧达成':>8} {'续采':>4} {'守卫':>4}")
    print("-" * 122)
    table, first_bad = [], None
    for path, r in rows:
        s = r.get("resource_steady") or {}
        n = r.get("streams_requested", 0)
        started = r.get("streams_started", 0)
        cpu = s.get("peak_children_cpu")
        bw = r.get("net_recv_mb_s")
        bw_per = r.get("net_recv_mb_s_per_stream")
        rt = r.get("realtime_factor_min")
        fa = r.get("frame_achieved_min")
        # 起流数 0 = 压根没采起来（本次是抖音页面端点下发验证码中间页），
        # 不能当成「该档安全」—— 它没测到任何东西，结论不成立
        nostart = started == 0
        bad = (isinstance(rt, (int, float)) and rt < 1.0) or \
              (isinstance(s.get("peak_sys_cpu"), (int, float)) and s["peak_sys_cpu"] >= 97)
        if bad and first_bad is None:
            first_bad = n
        row = {"anchors": n, "started": started, "ffmpeg": s.get("peak_children"),
               "ffmpeg_cpu_peak": cpu, "sys_cpu_peak": s.get("peak_sys_cpu"),
               "children_rss_mb": s.get("peak_children_rss_mb"),
               "net_mb_s": bw, "net_mb_s_per_stream": bw_per,
               "realtime_factor_min": rt, "frame_achieved_min": fa,
               "restarts": r.get("total_restarts"), "guard_hit": r.get("guard_hit"),
               "steady_seconds": r.get("steady_seconds"),
               "url_index": r.get("url_index"), "rooms": r.get("distinct_rooms"),
               "merged": r.get("merged"),
               "report": os.path.relpath(path, ROOT),
               "verdict": "未起流" if nostart else ("超标" if bad else "")}
        table.append(row)
        print(f"{n:>4} {started:>5} {str(s.get('peak_children')):>6} {str(cpu):>9} "
              f"{str(s.get('peak_sys_cpu')):>8} {str(s.get('peak_children_rss_mb')):>10} "
              f"{str(bw):>9} {str(bw_per):>9} {str(rt):>10} {str(fa):>8} "
              f"{str(r.get('total_restarts')):>4} "
              f"{'是' if r.get('guard_hit') else '否':>4}")
    print("-" * 122)
    nostart = [t["anchors"] for t in table if t["verdict"] == "未起流"]
    if first_bad is not None:
        safe = [t["anchors"] for t in table if t["verdict"] == "" and t["anchors"] < first_bad]
        print(f"\n⚠️ 首个**超标**档位: {first_bad} 路"
              f"（实时倍率 < 1 或整机 CPU ≥ 97% → 数据已在丢/机器已饱和）")
        print(f"   → 本机可持续上限 ≈ {max(safe) if safe else '?'} 路音视频")
    else:
        print("\n✅ 各档实时倍率均 ≥ 1、CPU 未饱和 —— 上限未触及，可继续加档")
    if nostart:
        print(f"\n⚠️ 起流数为 0 的档位: {nostart} —— **不是**「该档安全」，是压根没采起来"
              f"\n   （本次原因：抖音直播页端点被下发验证码中间页，取不到流地址）")
    return table, first_bad


def _media_ab(all_arms):
    """两轮配对跑过时，出「合并 vs 双进程」的逐档对照（同一档位、同一在播池）

    ⚠️ 列序**按 `merged` 字段定**，不能按 arm 名排序：中文名按码点排会得到
    「合并…」在「音视…」之前，而表头写的是「拆 / 合并」→ 数字整体错位（踩过）。
    """
    by_n = {}
    for _name, (table, _) in all_arms.items():
        for t in table:
            slot = "merged" if t.get("merged") is True else (
                "split" if t.get("merged") is False else "legacy")
            by_n.setdefault(t["anchors"], {})[slot] = t
    # 只对照「拆分 vs 合并」这一对；旧报告（无 merged 字段）不参与配对
    pairs = [(n, v) for n, v in sorted(by_n.items())
             if v.get("split") and v.get("merged")]
    if not pairs:
        return
    print("\n" + "=" * 122)
    print("配对对照：音视双进程（拆）→ 合并单进程（合），同档位")
    print("=" * 122)
    print(f"{'并发':>4} | {'带宽MB/s 拆':>12} {'合':>7} {'合/拆':>7} | "
          f"{'ffmpeg 拆':>10} {'合':>4} | {'CPU 拆':>8} {'合':>8} | "
          f"{'RSSMB 拆':>9} {'合':>7} | {'实时倍率 拆/合':>16}")
    print("-" * 122)
    for n, v in pairs:
        a, b = v["split"], v["merged"]

        def rat(k, lo=0.001):
            x, y = a.get(k), b.get(k)
            return (y / x) if (isinstance(x, (int, float)) and isinstance(y, (int, float))
                               and abs(x) > lo) else None
        rb, rc, rr = rat("net_mb_s"), rat("ffmpeg_cpu_peak"), rat("children_rss_mb")
        fmt = lambda x: f"×{x:.2f}" if x is not None else "?"
        print(f"{n:>4} | {str(a.get('net_mb_s')):>12} {str(b.get('net_mb_s')):>7} "
              f"{fmt(rb):>7} | "
              f"{str(a.get('ffmpeg')):>10} {str(b.get('ffmpeg')):>4} | "
              f"{str(a.get('ffmpeg_cpu_peak')):>8} {str(b.get('ffmpeg_cpu_peak')):>8} | "
              f"{str(a.get('children_rss_mb')):>9} {str(b.get('children_rss_mb')):>7} | "
              f"{str(a.get('realtime_factor_min')):>7}/{str(b.get('realtime_factor_min')):<8}")
    print("-" * 122)
    print("（合/拆 < 1 表示合并更省。带宽是**全机接收**，所以无论哪一档，"
          "两列都会一起撞到同一条链路的天花板\n"
          "  —— 达到天花板后「省带宽」不再体现为总带宽更小，而是**同一带宽下能带更多路**，"
          "看「实时倍率」与\n  各自的「首个超标档」才是真结论）")
    print("⚠️ **CPU/RSS 两列只在两边都健康的档位才可比**：一旦某臂超标，它的 ffmpeg 就"
          "在干等 socket（实时倍率 < 1 = 大半天没数据可解），\n   饿死的进程自然 CPU/RSS 都低。"
          "此时「超标臂的数字更小」是**饿死的假象**，不是它更省。请按各自的\n   「可持续上限」"
          "档位（本表上方的首个超标档前一档）比单路成本。")


def _media_perstream(rows_by_arm):
    """在**各臂最后一个健康档**上折算单路成本 —— 跨臂唯一公平的比法

    横向比不同 N 的 CPU/RSS 没意义（档位不同），而在超标档上比更是假的（那一臂的
    ffmpeg 在干等 socket）。只在各自「不超标且无续采」的最后一档上除路数。

    ⚠️ `peak_children_cpu` 的单位是**百分比**（`psutil.cpu_percent()` 求和，可 > 100），
    不是 CPU 秒。14.3 = 0.143 核。**不是**拿窗口时长去除。
    """
    lines = []
    for name, (table, _) in rows_by_arm.items():
        ok = [t for t in table if t["verdict"] == "" and t["started"]]
        if not ok:
            continue
        t = max(ok, key=lambda x: x["anchors"])
        n = t["started"]

        def per(v, scale=1.0):
            return round(v / n * scale, 3) if isinstance(v, (int, float)) else None
        lines.append((name, n, per(t["net_mb_s"]), per(t["ffmpeg_cpu_peak"], 0.01),
                      per(t["children_rss_mb"]), per(t["ffmpeg"])))
    if not lines:
        return
    print("\n" + "=" * 122)
    print("各臂**最后一个健康档**的单路成本（跨臂唯一公平的比法：两边都在正常干活）")
    print("=" * 122)
    print(f"{'arm':<28} {'健康档':>6} {'带宽MB/s·路':>12} {'CPU核·路':>10} "
          f"{'RSS MB·路':>10} {'ffmpeg·路':>10} {'实时倍率min':>11}")
    print("-" * 122)
    arms = list(rows_by_arm.items())
    for name, n, bw, cpu, rss, nf in lines:
        t = max([x for x in rows_by_arm[name][0]
                 if x["verdict"] == "" and x["started"]], key=lambda x: x["anchors"])
        print(f"{name:<28} {n:>6} {str(bw):>12} {str(cpu):>10} {str(rss):>10} "
              f"{str(nf):>10} {str(t.get('realtime_factor_min')):>11}")
    print("-" * 122)


def summarize_media(rows):
    """音视频档：并发数 vs 资源 + **可持续上限判定**

    上限判据不是「进程还活着」，而是**音频实时倍率**（< 1 = 解码跟不上直播实时，
    数据已经在丢）与整机 CPU 饱和。两者取先到者。

    多轮（`merged` 不同）时**分 arm 各出一张表**，并追加一张配对对照 —— 混成一张会
    把「合并前」和「合并后」的数字排成一条阶梯，首个超标档也会取错。
    """
    groups = {}
    for path, r in rows:
        groups.setdefault(r.get("merged"), []).append((path, r))
    order = [k for k in (True, False, None, "other") if k in groups]
    all_arms, flat = {}, []
    for key in order:
        arm_rows = groups[key]
        name = _media_arm_name(key)
        title = f"音视频档阶梯汇总 · {name}（{len(arm_rows)} 档）"
        table, _ = _media_table(arm_rows, title)
        all_arms[name] = (table, None)
        flat.extend(table)
        print()
    if len(all_arms) > 1:
        _media_ab(all_arms)
        _media_perstream(all_arms)
    print("\n⚠️ 外推边界: 只测**媒体子系统**（不含 WS/文本与落盘链路）；带宽上限另受"
          "\n   目标机出口带宽约束，按 `带宽/路` × N 对部署环境换算。")
    return flat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["network", "media"], default="network")
    ap.add_argument("--glob", default=None,
                    help="report.json 的 glob。默认 network: load_test/A*/report.json；"
                         "media: load_test/M*/report.json")
    ap.add_argument("--json", default=None, help="把汇总表另存为 JSON")
    args = ap.parse_args()
    if args.glob is None:
        args.glob = "load_test/M*/report.json" if args.mode == "media" \
            else "load_test/A*/report.json"

    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    except Exception:
        pass

    rows = load_reports(args.glob, mode=args.mode)
    if args.mode == "media":
        if not rows:
            print(f"没找到 media 模式的 report.json（{args.glob}）")
            return
        table = summarize_media(rows)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(table, f, ensure_ascii=False, indent=2)
            print(f"\n汇总已存: {args.json}")
        return
    if not rows:
        print(f"没找到 network 模式的 report.json（{args.glob}）")
        return

    # 主对照表：并发数 vs 资源
    print("=" * 108)
    print("口径 A 阶梯压测汇总（并发数 vs 资源）")
    print("=" * 108)
    hdr = (f"{'并发':>5} {'建起WS':>7} {'未连上':>7} {'峰值RSS':>10} {'均值RSS':>10} "
           f"{'MB/路':>7} {'峰值线程':>9} {'峰值连接':>9} {'峰值系统CPU':>11} {'444':>5} {'ERROR':>6}")
    print(hdr)
    print("-" * 108)

    table = []
    for path, r in rows:
        res = r.get("resource") or {}
        n = r.get("connections", 0)
        conn = r.get("ws_connected", 0)
        peak_rss = res.get("peak_rss_mb")
        per = round(peak_rss / conn, 1) if peak_rss and conn else None
        err = r.get("errors") or {}
        err_total = sum(v for k, v in err.items() if k != "http_444")
        row = {
            "anchors": n, "ws_connected": conn,
            "ws_connect_failed": r.get("ws_connect_failed", n - conn),
            "peak_rss_mb": peak_rss, "mean_rss_mb": res.get("mean_rss_mb"),
            "rss_per_conn_mb": per,
            "peak_threads": res.get("peak_threads"),
            "peak_connections": res.get("peak_connections"),
            "peak_sys_cpu": res.get("peak_sys_cpu"),
            "http_444": err.get("http_444", 0),
            "errors_excl_444": err_total,
            "startup_seconds": r.get("startup_seconds"),
            "data_dir": r.get("data_dir"),
            "report": os.path.relpath(path, ROOT),
        }
        table.append(row)
        print(f"{n:>5} {conn:>7} {row['ws_connect_failed']:>7} "
              f"{str(peak_rss):>10} {str(row['mean_rss_mb']):>10} "
              f"{str(per):>7} {str(row['peak_threads']):>9} "
              f"{str(row['peak_connections']):>9} {str(row['peak_sys_cpu']):>11} "
              f"{row['http_444']:>5} {err_total:>6}")

    print("-" * 108)
    ok = [r for r in table if r["ws_connect_failed"] == 0 and r["errors_excl_444"] == 0]
    print(f"\n全部连接成功且无 ERROR 的档位: {[r['anchors'] for r in ok]}")
    bad = [r for r in table if r["ws_connect_failed"] or r["errors_excl_444"]]
    if bad:
        print(f"⚠️ 有失败的档位:")
        for r in bad:
            print(f"   {r['anchors']:>3} 路: 未连上 {r['ws_connect_failed']}, "
                  f"ERROR {r['errors_excl_444']} → 看 {r['report']} 的 error_samples")
    else:
        print("✅ 各档均无连接失败、无 ERROR")

    print("\n⚠️ 外推边界: 本表是**本机住宅 IP** 的结果，衡量的是**代码容量**。"
          "\n   部署环境若是数据中心 IP，444 会成为主约束（属网络容量，需在目标环境另测）。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(table, f, ensure_ascii=False, indent=2)
        print(f"\n汇总已存: {args.json}")


if __name__ == "__main__":
    main()
