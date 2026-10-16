#!/usr/bin/python
# coding:utf-8 -*-
"""多直播间并发统一采集 - 每个直播间同时采集主播、商品、音视频、弹幕

架构：
- 主线程：一个 Playwright 会话，多个 page 用 asyncio.gather 并发采集（商品 + 音视频）
- 子线程：每个直播间一个 WebSocket 弹幕采集线程

用法：
  python collect_concurrent.py [duration_seconds]
"""
import asyncio
import io
import sys
import threading
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from liveMan import DouyinLiveWebFetcher
from data_recorder import DataRecorder
from product_extractor import (
    _open_product_panel, _is_index_line, _is_price_line,
    _is_service_line, _is_marketing_line, _parse_count, SALES_PATTERN,
)
from media_collector import MediaCollector, STREAM_URL_KEYWORDS

ROOMS = [
    ("与辉同行", "646454278948"),
    ("交个朋友", "168465302284"),
]
PRODUCT_SCROLL_TIMES = 15


def _parse_products_from_text(body: str) -> list:
    """从页面文本解析商品列表（复用 collect_all 逻辑）"""
    lines = [l.strip() for l in body.split("\n") if l.strip()]
    start = end = -1
    for i, line in enumerate(lines):
        if start < 0 and ("带货口碑" in line or "店铺口碑" in line or "讲解中" in line):
            start = i
        if start >= 0 and "在线观众" in line:
            end = i
            break
    panel = lines[start:end if end > 0 else len(lines)] if start >= 0 else lines

    index_positions = []
    for i, line in enumerate(panel):
        if _is_index_line(line):
            num = int(line)
            if not index_positions or (1 <= num <= index_positions[-1][1] + 10):
                index_positions.append((i, num))

    seen = set()
    products = []
    for idx, (index_i, num) in enumerate(index_positions):
        end_i = index_positions[idx + 1][0] if idx + 1 < len(index_positions) else len(panel)
        if index_i + 1 >= end_i:
            continue
        label = panel[index_i + 1]
        if not label or len(label) < 2 or len(label) > 120:
            continue
        if _is_service_line(label) or _is_marketing_line(label):
            continue
        if label in seen:
            continue
        seen.add(label)

        subtitle, price, price_original, sales, sold_out, coupon = "", 0.0, 0.0, 0, False, False
        for j in range(index_i + 2, end_i):
            l = panel[j]
            if _is_price_line(l):
                prices = [float(x) for x in __import__('re').findall(r"([\d.]+)", l)]
                if prices and not price:
                    price = min(prices)
                    price_original = max(prices) if len(prices) >= 2 else 0.0
            elif "查看价格" in l and not price:
                price = -1
            if not subtitle and l and l != label and not _is_service_line(l) and not _is_marketing_line(l):
                subtitle = l
            sm = SALES_PATTERN.search(l)
            if sm:
                sales = _parse_count(sm.group(1))
            if "已抢光" in l:
                sold_out = True
            if "券后价" in l:
                coupon = True
        products.append({
            "num": num, "name": label, "subtitle": subtitle,
            "price": price if price > 0 else 0.0, "price_original": price_original,
            "sales_count": sales, "sold_out": sold_out, "coupon_price": coupon,
        })
    return products


async def _collect_one_room(page, room_id, name, duration, frame_interval,
                            audio_segment, audio_dir, frames_dir) -> dict:
    """在给定 page 上采集单个直播间的商品 + 音视频"""
    room_url = f"https://live.douyin.com/{room_id}"
    products, stream_urls, audio_segments, frames = [], [], [], []

    def on_response(response):
        url = response.url
        if any(kw in url for kw in STREAM_URL_KEYWORDS):
            if response.status in (200, 302) and url not in stream_urls:
                stream_urls.append(url)
    page.on("response", on_response)

    try:
        await page.goto(room_url, wait_until="commit", timeout=60000)
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await asyncio.sleep(8)

        # HTML 解析流 URL（无需登录态）
        html = await page.content()
        parsed = MediaCollector._extract_stream_urls_from_html(html)
        if parsed:
            stream_urls[:] = parsed
        else:
            await asyncio.sleep(5)

        # 启动持续 ffmpeg 抽帧（一次连接，持续输出原始帧，支持高频 1 秒/帧）
        frame_extractor = None
        if stream_urls:
            fps = 1.0 / frame_interval
            output_pattern = frames_dir / "frame_%04d.jpg"
            cmd = ["ffmpeg", "-i", stream_urls[0], "-vf", f"fps={fps}",
                   "-q:v", "2", str(output_pattern), "-loglevel", "error", "-y"]
            try:
                frame_extractor = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            except Exception:
                frame_extractor = None

        # 音频采集循环（截图由持续 ffmpeg 负责）
        elapsed, audio_idx = 0, 0
        while elapsed < duration:
            if stream_urls and elapsed % audio_segment < frame_interval:
                out = audio_dir / f"segment_{audio_idx:03d}.mp3"
                for url in stream_urls:
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "ffmpeg", "-i", url, "-t", str(audio_segment),
                            "-vn", "-acodec", "libmp3lame", "-ab", "64k",
                            "-ar", "22050", "-f", "mp3", str(out),
                            "-loglevel", "error", "-y",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await asyncio.wait_for(proc.communicate(), timeout=audio_segment + 15)
                        if out.exists() and out.stat().st_size > 0:
                            audio_segments.append(str(out))
                            audio_idx += 1
                            break
                    except Exception:
                        continue

            await asyncio.sleep(frame_interval)
            elapsed += frame_interval

        # 停止持续 ffmpeg，收集生成的帧
        if frame_extractor:
            frame_extractor.terminate()
            try:
                await asyncio.wait_for(frame_extractor.wait(), timeout=5)
            except Exception:
                pass
        frame_files = sorted(frames_dir.glob("frame_*.jpg"))
        frames = [str(f) for f in frame_files]

    except Exception as e:
        print(f"[{name}] 浏览器采集错误: {e}")

    return {
        "room_id": room_id, "name": name, "products": products,
        "stream_urls": stream_urls, "audio_segments": audio_segments, "frames": frames,
    }


async def _collect_all_rooms(rooms, duration, frame_interval, audio_segment,
                             base_dir_map) -> list:
    """一个 Playwright 会话并发采集多个直播间（音视频，无需登录态）"""
    async with async_playwright() as p:
        # 方案 B：完全不用账号，独立浏览器（无登录态）
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})

        pages = []
        tasks = []
        for name, room_id in rooms:
            page = await context.new_page()
            pages.append(page)
            audio_dir = base_dir_map[room_id] / "audio"
            frames_dir = base_dir_map[room_id] / "frames"
            audio_dir.mkdir(parents=True, exist_ok=True)
            frames_dir.mkdir(parents=True, exist_ok=True)
            tasks.append(_collect_one_room(
                page, room_id, name, duration, frame_interval,
                audio_segment, audio_dir, frames_dir,
            ))

        results = await asyncio.gather(*tasks)

        for page in pages:
            try:
                await page.close()
            except Exception:
                pass
        await browser.close()

        return results


def collect_all_multi(rooms, duration=180, frame_interval=10, audio_segment=30) -> dict:
    """多直播间并发统一采集（方案 B：完全不用账号）

    采集：主播信息 + 弹幕 + 商品 promotion_id + 音视频（无需登录态）
    """
    start_time = datetime.now()
    print(f"[{start_time.strftime('%H:%M:%S')}] 开始 {len(rooms)} 个直播间并发采集（方案 B，无需账号，时长 {duration}秒）")
    print("=" * 70)

    # ============ 1. 主播信息 + WebSocket 弹幕（子线程，无需登录态） ============
    print("\n[1/3] 获取主播信息 + 启动 WebSocket 弹幕监控...")
    fetchers = []
    for name, room_id in rooms:
        fetcher = DouyinLiveWebFetcher(room_id, None)
        success, status, nickname, user_id = fetcher.get_room_status()
        recorder = DataRecorder(fetcher.room_id, room_id)
        recorder.set_room_info({
            "title": nickname, "anchor_nickname": nickname,
            "anchor_id": user_id, "room_status": status,
        })
        fetcher.data_callback = recorder.on_event
        ws_thread = threading.Thread(target=fetcher.start, daemon=True)
        ws_thread.start()
        fetchers.append((fetcher, recorder, nickname, user_id, status))
        print(f"  {name}: 主播={nickname} 状态={status}")

    # ============ 2. 音视频（主线程 Playwright，多 page 并发，无需登录态） ============
    print(f"\n[2/3] 音视频采集（{len(rooms)} 个直播间并发）...")
    base_dir_map = {}
    for (name, room_id), (fetcher, recorder, nickname, user_id, status) in zip(rooms, fetchers):
        base_dir_map[room_id] = Path(recorder.path)

    browser_results = asyncio.run(_collect_all_rooms(
        rooms, duration, frame_interval, audio_segment, base_dir_map,
    ))

    # ============ 3. 停止 + 汇总 ============
    print("\n[3/3] 停止采集，汇总结果...")
    result_map = {r["room_id"]: r for r in browser_results}
    for (name, room_id), (fetcher, recorder, nickname, user_id, status) in zip(rooms, fetchers):
        fetcher.stop()
        recorder.set_promotion_order(fetcher.product_refresh_order)
        recorder.close()

    end_time = datetime.now()
    total_duration = (end_time - start_time).total_seconds()

    print("\n" + "=" * 70)
    print("并发统一采集汇总（方案 B）")
    print("=" * 70)
    for (name, room_id), (fetcher, recorder, nickname, user_id, status) in zip(rooms, fetchers):
        br = result_map.get(room_id, {})
        print(f"\n【{name}】({room_id})")
        print(f"  主播: {nickname} ({user_id}) | 状态: {status}")
        print(f"  商品 promotion_id: {len(fetcher.product_ids)} 个")
        print(f"  音频片段: {len(br.get('audio_segments', []))} 个")
        print(f"  视频帧: {len(br.get('frames', []))} 张")
        print(f"  流 URL: {len(br.get('stream_urls', []))} 个")
        print(f"  弹幕数据: {recorder.path}")
    print(f"\n总时长: {total_duration / 60:.1f} 分钟")
    print("=" * 70)

    return {"rooms": rooms, "results": browser_results, "duration": total_duration}


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    collect_all_multi(ROOMS, duration=duration)
