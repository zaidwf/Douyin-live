#!/usr/bin/python
# coding:utf-8
"""全模态单场采集入口（当前版）

采集四个代码侧模态 + 一个第三方模态：

| 模态 | 内容 | 来源 |
|---|---|---|
| 文本 | 弹幕流（chat/emoji） | WebSocket（`liveMan.py`） |
| 数值 | 时序指标 + 事件流 + 粉丝团群体级特征 | WebSocket → `data_recorder.py` |
| 图像 | 直播画面抽帧（jpg，原始分辨率纯画面） | ffmpeg 从流 URL 直接解码（`liveMan.MediaCapture`） |
| 音频 | 音频片段（WAV 16kHz 单声道，30s/段连续） | HTML 解析 flv_pull_url → ffmpeg 连续切片（`liveMan.MediaCapture`） |
| 商品 | 商品列表全部字段 | **灰豚第三方平台（直播结束后下载）** —— 代码不采集 |

设计要点：
  - 文本/数值走当前核心 `liveMan` + `data_recorder`（与 `auto_crawl.py` 生产链路同源）
  - **音视频：统一走 `liveMan.MediaCapture`**（2026-09-29 起）。原先本模块自带的
    Playwright + ffmpeg 实现已删除 —— 实测 `requests` 拿到的直播间 HTML 里就含
    `flv_pull_url`（见 `test/probe_stream_url.py`），**不需要起 Chromium**。
    现在单场与 `auto_crawl --media` 共用同一套代码。
  - 商品已改灰豚，**不再做 Playwright 面板提取**（实测面板点击打不开，见 legacy/README.md）
  - 采集结束后自动做全模态真实性校验（`test/validate_fullmodal.py` 的逻辑）

与其他入口的分工：
  - `auto_crawl.py`  —— 生产守护：多主播轮播、长跑；音视频经 `--media` 开关（默认关）
  - `collect_full.py` —— 本脚本：单场全模态、含音视频、用于数据集构建与验证

用法：
  python collect_full.py [抖音号] [时长秒] [抽帧间隔秒] [音频段长秒]
  python collect_full.py 56697889278 600
"""
import io
import sys
import threading
import time
from datetime import datetime

from liveMan import DouyinLiveWebFetcher, MediaCapture
from data_recorder import DataRecorder, new_date_dir

DEFAULT_LIVE_ID = "56697889278"   # 与辉同行


def collect_full(live_id: str = DEFAULT_LIVE_ID, duration: int = 600,
                 frame_interval: int = 10, audio_segment: int = 30) -> dict:
    """单场全模态采集：主播信息 → WS（文本/数值）→ 音视频（MediaCapture）"""
    start = datetime.now()
    print(f"[{start.strftime('%H:%M:%S')}] 全模态采集 直播间={live_id} 时长={duration}s")
    print("=" * 74)

    # ---------- 1. 主播信息 ----------
    print("\n[1/4] 主播信息（enter API）...")
    fetcher = DouyinLiveWebFetcher(live_id, None)
    ok, status, nickname, anchor_id = fetcher.get_room_status()
    if not ok:
        print(f"  ❌ 无法采集：{status}")
        return {"ok": False, "error": status}
    print(f"  {nickname} | 主播ID={anchor_id} | room_id={fetcher.room_id} | {status}")

    # ---------- 2. 结构化落盘 + WS 采集 ----------
    # 落盘目录：`data/<当天日期>/`（见 data_recorder.new_date_dir）。在**确认开播之后**
    # 才解析 —— 若在开播检测前就解析，每次对已下播房间的空跑都会留下一个空目录，反而更乱。
    date_dir = new_date_dir()
    print(f"  数据保存到：{date_dir}/")
    recorder = DataRecorder(fetcher.room_id, live_id, base_dir=date_dir)
    room_info = dict(fetcher.room_detail)
    room_info.update({"title": nickname, "anchor_nickname": nickname,
                      "anchor_id": anchor_id, "room_status": status})
    recorder.set_room_info(room_info)
    fetcher.data_callback = recorder.on_event

    print("\n[2/4] WebSocket 采集（弹幕 / 事件 / 粉丝团时序）...")
    ws_thread = threading.Thread(target=fetcher.start, daemon=True)
    ws_thread.start()

    # ---------- 3. 图像 + 音频（统一走 liveMan.MediaCapture）----------
    print("[3/4] 图像抽帧 + 音频切片（ffmpeg，MediaCapture）...")
    # max_seconds 只是**防僵尸上限**，主停止手段是 stop_media()；这里给 duration+30 留出收尾余量
    capture = MediaCapture(live_id, recorder.path, fetcher=fetcher,
                           audio_segment=audio_segment, frame_interval=frame_interval,
                           max_seconds=duration + 30)
    capture.start_media()
    time.sleep(duration)                     # 定长单场：到点自己停
    media = capture.stop_media()

    # ---------- 4. 收尾 ----------
    print("\n[4/4] 收尾落盘...")
    fetcher.stop()
    ws_thread.join(timeout=5)
    recorder.set_promotion_order(fetcher.product_refresh_order)
    recorder.close()

    elapsed = (datetime.now() - start).total_seconds()
    print("\n" + "=" * 74)
    print("全模态采集汇总")
    print("=" * 74)
    print(f"  主播        : {nickname} ({anchor_id})")
    print(f"  输出目录    : {recorder.path}")
    print(f"  文本/数值   : danmaku.jsonl / events.jsonl / series.jsonl / fansclub_edges.jsonl")
    print(f"  图像        : {len(media['frames'])} 帧")
    print(f"  音频        : {len(media['audio'])} 段")
    if media['restarts']:
        print(f"  音视频续采  : {media['restarts']} 次（流地址过期后重解析）")
    print(f"  商品        : 由灰豚提供（直播结束后下载），代码侧不采集")
    print(f"  总时长      : {elapsed / 60:.1f} 分钟")
    print("=" * 74)
    print(f"\n校验产物真实性：python test/validate_fullmodal.py {recorder.path}")

    return {"ok": True, "data_dir": recorder.path, "anchor": nickname,
            "media": media, "duration": elapsed}


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                  errors='replace', line_buffering=True)
    live_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIVE_ID
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    frame_interval = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    audio_segment = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    collect_full(live_id, duration, frame_interval, audio_segment)
