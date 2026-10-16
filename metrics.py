#!/usr/bin/python
# coding:utf-8 -*-
"""进程/系统资源采样器（psutil），用于并发承压测试与生产监控。

ResourceSampler 以固定间隔采集当前进程与整机的资源指标，逐行写 JSONL，
结束时输出峰值/均值摘要。可后台线程运行（start/stop），也可手动 snapshot()。

依赖：psutil（pip install psutil）。未安装时降级为「只写 error 样本」，不抛异常。
"""
import json
import threading
import time
from datetime import datetime

try:
    import psutil
except ImportError:
    psutil = None


class ResourceSampler:
    """周期采样当前进程 + 整机资源指标

    :param children: 是否聚合**本进程的子进程**（`children(recursive=True)`）的
        CPU/RSS/进程数。默认 False。音视频采集的全部成本在 **ffmpeg 子进程**里
        （一路 = 2 个 ffmpeg），不开这项等于对音视频开销完全失明 —— 而本进程自己
        几乎不耗 CPU。测音视频必须开。
    :param io: 是否采网络/磁盘吞吐（累计字节 → 相邻样本算 MB/s）。带宽是媒体采集的
        硬约束之一，不开这项就只能看 CPU。默认 False（`p.connections()` 之外再加
        两次计数器读取，对离线模式是纯浪费）。
    """

    def __init__(self, out_path, interval=5.0, label="", children=False, io=False):
        self.out_path = out_path
        self.interval = interval
        self.label = label
        self.children = children
        self.io = io
        self._stop = threading.Event()
        self._thread = None
        self._samples = []
        self._proc = psutil.Process() if psutil else None
        # `{pid: Process}`：cpu_percent() 是**跨调用差值**，必须复用同一个 Process
        # 实例跨样本保温，否则每次新建实例都返回 0.0（首次调用恒为 0）。
        self._child_procs = {}
        self._prev_io = None      # (t, net_recv, net_sent, disk_write) 上一次的累计值
        self._t0 = None

    def _children_sample(self, now):
        """聚合子进程：进程数 / CPU 合计（逻辑核，单进程可 >100）/ RSS 合计"""
        out = {"children": 0, "children_cpu": 0.0, "children_rss_mb": 0.0}
        try:
            alive = self._proc.children(recursive=True)
        except Exception:
            return out
        seen = set()
        for c in alive:
            seen.add(c.pid)
            prev = self._child_procs.get(c.pid)
            if prev is None:
                self._child_procs[c.pid] = c
                prev = c
                try:
                    prev.cpu_percent(interval=None)   # 保温：首次恒为 0，丢掉
                except Exception:
                    continue
            try:
                out["children_cpu"] += prev.cpu_percent(interval=None)
                out["children_rss_mb"] += prev.memory_info().rss / 1048576
                out["children"] += 1
            except Exception:                       # 采样间隙进程已退出
                continue
        for dead in set(self._child_procs) - seen:   # 别让字典无限增长
            self._child_procs.pop(dead, None)
        out["children_cpu"] = round(out["children_cpu"], 1)
        out["children_rss_mb"] = round(out["children_rss_mb"], 1)
        return out

    def _io_sample(self, now):
        """网络/磁盘**速率**（MB/s）。累计计数器的差值 ÷ 实际间隔"""
        out = {"net_recv_mb_s": None, "net_sent_mb_s": None, "disk_write_mb_s": None,
               "net_recv_total_mb": None, "disk_write_total_mb": None}
        try:
            net = psutil.net_io_counters()
            disk = psutil.disk_io_counters()
        except Exception:
            return out
        cur = (now, net.bytes_recv, net.bytes_sent,
               disk.write_bytes if disk else 0)
        out["net_recv_total_mb"] = round(net.bytes_recv / 1048576, 1)
        out["disk_write_total_mb"] = round(cur[3] / 1048576, 1)
        prev = self._prev_io
        self._prev_io = cur
        if prev is None:
            return out
        dt = now - prev[0]
        if dt <= 0:
            return out
        out["net_recv_mb_s"] = round((cur[1] - prev[1]) / 1048576 / dt, 2)
        out["net_sent_mb_s"] = round((cur[2] - prev[2]) / 1048576 / dt, 2)
        out["disk_write_mb_s"] = round((cur[3] - prev[3]) / 1048576 / dt, 2)
        return out

    def _snapshot(self):
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        s = {"ts": datetime.now().isoformat(timespec="seconds"),
             "t": round(now - self._t0, 2)}   # 相对起点的秒数：summary(since=) 要用
        if psutil is None:
            s["error"] = "psutil 未安装"
            return s
        if self.children:
            s.update(self._children_sample(now))
        if self.io:
            s.update(self._io_sample(now))
        p = self._proc
        try:
            mem = p.memory_info()
            s.update({
                "rss_mb": round(mem.rss / 1048576, 1),
                "vms_mb": round(mem.vms / 1048576, 1),
                "cpu_percent": round(p.cpu_percent(interval=None), 1),
                "num_threads": p.num_threads(),
            })
        except Exception as e:
            s["error"] = str(e)
        # Windows 上 num_fds 不存在；connections 可能较慢，都做保护
        try:
            s["num_fds"] = p.num_fds()
        except Exception:
            s["num_fds"] = None
        try:
            s["connections"] = len(p.connections(kind="tcp"))
        except Exception:
            s["connections"] = None
        try:
            s["sys_cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
            s["sys_mem_percent"] = round(psutil.virtual_memory().percent, 1)
        except Exception:
            pass
        return s

    def start(self):
        """启动后台采样线程。未装 psutil 时仅打印警告。"""
        if psutil is None:
            print("[metrics] 警告：psutil 未安装，无法采样（pip install psutil）")
            return self
        self._proc.cpu_percent(interval=None)  # 预热：第一次调用恒为 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            s = self._snapshot()
            self._samples.append(s)
            try:
                with open(self.out_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def summary(self, since=None):
        """返回峰值/均值摘要 dict（无样本时为空）

        :param since: 只统计 `t >= since` 的样本（`t` 是相对采样起点的秒数）。
            用来把**预热期**的进程启动尖峰与**稳态**分开报 —— 否则「N 路稳态跑了
            90s」的数字里混进了启动瞬间的 2N 个 ffmpeg 同时初始化的开销。
        """
        samples = self._samples
        if since is not None:
            samples = [s for s in samples if isinstance(s.get("t"), (int, float))
                       and s["t"] >= since]
        if not samples:
            return {}

        def peak(key):
            vals = [s[key] for s in samples if isinstance(s.get(key), (int, float))]
            return max(vals) if vals else None

        def mean(key):
            vals = [s[key] for s in samples if isinstance(s.get(key), (int, float))]
            return round(sum(vals) / len(vals), 1) if vals else None

        out = {
            "label": self.label,
            "samples": len(samples),
            "window_from": since,
            "peak_rss_mb": peak("rss_mb"),
            "mean_rss_mb": mean("rss_mb"),
            "peak_vms_mb": peak("vms_mb"),
            "peak_threads": peak("num_threads"),
            "peak_fds": peak("num_fds"),
            "peak_connections": peak("connections"),
            "mean_sys_cpu": mean("sys_cpu_percent"),
            "peak_sys_cpu": peak("sys_cpu_percent"),
            "mean_sys_mem": mean("sys_mem_percent"),
        }
        if self.children:
            out.update({
                "peak_children": peak("children"),
                "peak_children_cpu": peak("children_cpu"),
                "mean_children_cpu": mean("children_cpu"),
                "peak_children_rss_mb": peak("children_rss_mb"),
            })
        if self.io:
            out.update({
                "peak_net_recv_mb_s": peak("net_recv_mb_s"),
                "mean_net_recv_mb_s": mean("net_recv_mb_s"),
                "peak_disk_write_mb_s": peak("disk_write_mb_s"),
            })
        return out
