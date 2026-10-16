#!/usr/bin/python
# coding:utf-8 -*-
"""统一采集入口：同时爬取直播间（弹幕/礼物/点赞）、主播、商品、音视频数据

采集四类数据：
  1. 直播间弹幕流（WebSocket：聊天/礼物/点赞/进出/关注/统计）
  2. 主播信息（room/enter API）
  3. 商品橱窗（Playwright 提取名称/价格/销量）
  4. 音视频（ffmpeg 音频片段 + Playwright 视频帧）

商品提取与音视频采集合并到同一个 Playwright 会话，避免重复导航触发反爬限流。

用法：
  python collect_all.py [room_id] [duration_seconds]
"""
import io
import sys
import asyncio
import threading
import time
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from liveMan import DouyinLiveWebFetcher
from data_recorder import DataRecorder
from product_extractor import (
    _open_product_panel, _is_index_line, _is_price_line,
    _is_service_line, _is_marketing_line, _parse_count, SALES_PATTERN,
)
from media_collector import STREAM_URL_KEYWORDS, MediaCollector

# 商品面板滚动加载次数
PRODUCT_SCROLL_TIMES = 15


def _extract_product_block(panel, label_i, end_i):
    """提取从 label_i 到 end_i 之间的商品详情（名称/副标题/价格/销量）"""
    label = panel[label_i]
    subtitle = ""
    price = 0.0
    price_original = 0.0
    sales = 0
    sold_out = False
    coupon = False
    for j in range(label_i + 1, end_i):
        l = panel[j]
        if _is_price_line(l):
            prices = [float(x) for x in re.findall(r"([\d.]+)", l)]
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
    return {
        "name": label, "subtitle": subtitle,
        "price": price if price > 0 else 0.0, "price_original": price_original,
        "sales_count": sales, "sold_out": sold_out, "coupon_price": coupon,
    }


def _parse_products_from_text(body: str) -> list:
    """从页面文本解析商品列表（含"讲解中"的正在讲解商品）"""
    lines = [l.strip() for l in body.split("\n") if l.strip()]

    # 定位商品面板区域
    start = end = -1
    for i, line in enumerate(lines):
        if start < 0 and ("带货口碑" in line or "店铺口碑" in line or "讲解中" in line):
            start = i
        if start >= 0 and "在线观众" in line:
            end = i
            break
    panel = lines[start:end if end > 0 else len(lines)] if start >= 0 else lines

    # 找序号行
    index_positions = []
    for i, line in enumerate(panel):
        if _is_index_line(line):
            num = int(line)
            if not index_positions or (1 <= num <= index_positions[-1][1] + 10):
                index_positions.append((i, num))

    seen = set()
    products = []

    # 1. 提取"讲解中"的商品（正在讲解，通常无序号，紧跟"讲解中"标签）
    for i, line in enumerate(panel):
        if "讲解中" in line:
            # "讲解中"后的第一个非服务/营销行是商品名
            j = i + 1
            while j < len(panel) and (_is_service_line(panel[j]) or _is_marketing_line(panel[j])):
                j += 1
            if j < len(panel) and len(panel[j]) >= 2:
                label = panel[j]
                if label not in seen:
                    seen.add(label)
                    # 商品范围到下一个序号或"在线观众"
                    next_idx = next((p for p, _ in index_positions if p > j), len(panel))
                    block = _extract_product_block(panel, j, next_idx)
                    block["num"] = 1
                    block["is_current"] = True
                    products.append(block)
            break

    # 2. 提取序号商品
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

        block = _extract_product_block(panel, index_i + 1, end_i)
        block["num"] = num
        block["is_current"] = False
        products.append(block)
    return products


async def _browser_collect(room_id: str, duration: int, frame_interval: int,
                           audio_segment: int, cdp_port: int, auth_file: str,
                           audio_dir: Path, frames_dir: Path,
                           collect_products: bool = False) -> dict:
    """一次 Playwright 会话完成商品提取 + 音视频采集"""
    room_url = f"https://live.douyin.com/{room_id}"
    products = []
    stream_urls = []
    audio_segments = []
    frames = []

    async with async_playwright() as p:
        if cdp_port:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720}, storage_state=auth_file,
            )

        page = await context.new_page()

        # 拦截流 URL
        def on_response(response):
            url = response.url
            if any(kw in url for kw in STREAM_URL_KEYWORDS):
                if response.status in (200, 302) and url not in stream_urls:
                    stream_urls.append(url)
        page.on("response", on_response)

        try:
            # 导航直播间（一次）
            await page.goto(room_url, wait_until="commit", timeout=60000)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await asyncio.sleep(8)

            # 主策略：从 HTML 解析 flv_pull_url（多画质 FLV 流）
            html = await page.content()
            parsed_urls = MediaCollector._extract_stream_urls_from_html(html)
            if parsed_urls:
                stream_urls[:] = parsed_urls
            else:
                # fallback：等待网络拦截捕获
                await asyncio.sleep(5)

            # ---- 提取商品（默认关闭，商品信息改用灰豚，直播结束后下载）----
            # 2026-09-28 决策：商品信息统一由灰豚商品列表提供（字段完整权威），代码侧不再采集。
            # Playwright 面板实测长期打不开（点击后面板关键词一个都不出现），且 5 次 × 8s 的
            # 重试点击对直播页有反爬风险、还会挤占音视频采集时间。保留代码以便按需回退。
            opened = False
            if collect_products:
                opened = await _open_product_panel(page)
            if opened:
                # 只滚动包含商品特征的容器，避免影响直播画面导致页面错位
                for _ in range(PRODUCT_SCROLL_TIMES):
                    await page.evaluate('''() => {
                        const containers = document.querySelectorAll('div, ul');
                        for (const el of containers) {
                            if (el.scrollHeight > el.clientHeight + 100) {
                                const text = el.innerText || '';
                                if (text.includes('运费险') || text.includes('去抢购') ||
                                    text.includes('查看价格') || text.includes('带货口碑') ||
                                    text.includes('店铺口碑')) {
                                    el.scrollTop = el.scrollHeight;
                                }
                            }
                        }
                    }''')
                    await asyncio.sleep(1.5)
                body = await page.inner_text("body")
                products = _parse_products_from_text(body)

                # 关闭商品面板，恢复直播画面（避免遮挡直播画面导致截图错位）
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(2)
                except Exception:
                    pass

            # ---- 采集音视频 ----
            elapsed = 0
            frame_idx = 0
            audio_idx = 0
            while elapsed < duration:
                # 截图（只截取直播画面 video 元素，不含聊天栏等）
                frame_path = frames_dir / f"frame_{frame_idx:04d}.jpg"
                try:
                    video = await page.query_selector('video')
                    if video:
                        await video.screenshot(path=str(frame_path), type="jpeg", quality=70, timeout=10000)
                    else:
                        await page.screenshot(path=str(frame_path), type="jpeg", quality=70, timeout=10000)
                    frames.append(str(frame_path))
                    frame_idx += 1
                except Exception:
                    pass

                # 音频（ffmpeg 从流 URL 切片段）
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

        except Exception as e:
            print(f"浏览器采集错误: {e}")
        finally:
            try:
                await page.close()
            except Exception:
                pass
            if not cdp_port:
                await browser.close()

    return {
        "products": products,
        "stream_urls": stream_urls,
        "audio_segments": audio_segments,
        "frames": frames,
    }


def collect_all(room_id: str, duration: int = 180, frame_interval: int = 10,
                audio_segment: int = 30, cdp_port: int = None,
                auth_file: str = "config/auth.json",
                collect_products: bool = False) -> dict:
    """同时采集直播间/主播/商品/音视频数据"""
    start_time = datetime.now()
    result = {"room_id": room_id, "start_time": start_time.isoformat(), "duration": duration}

    print(f"[{start_time.strftime('%H:%M:%S')}] 开始统一采集 直播间={room_id} 时长={duration}秒")
    print("=" * 70)

    # ============ 1. 主播信息 ============
    print("\n[1/4] 获取主播信息...")
    fetcher = DouyinLiveWebFetcher(room_id, None)
    success, status, nickname, user_id = fetcher.get_room_status()
    result["anchor"] = {"nickname": nickname, "anchor_id": user_id,
                        "room_status": status, "room_id": fetcher.room_id,
                        **fetcher.room_detail}
    print(f"  主播: {nickname} | ID: {user_id} | 状态: {status}")

    # ============ 初始化结构化落盘（audio/frames 复用同一目录） ============
    recorder = DataRecorder(fetcher.room_id, room_id)
    room_info = dict(fetcher.room_detail)  # enter API 富字段（头像/简介/粉丝数/作品数等）
    room_info.update({"title": nickname, "anchor_nickname": nickname,
                      "anchor_id": user_id, "room_status": status})
    recorder.set_room_info(room_info)
    fetcher.data_callback = recorder.on_event
    base_dir = Path(recorder.path)
    audio_dir = base_dir / "audio"
    frames_dir = base_dir / "frames"
    audio_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    # ============ 2. WebSocket 弹幕监控（子线程） ============
    print("\n[2/4] 启动 WebSocket 弹幕监控...")
    ws_thread = threading.Thread(target=fetcher.start, daemon=True)
    ws_thread.start()

    # ============ 3+4. 商品 + 音视频（主线程 Playwright，避免子线程 EPIPE） ============
    print("[3/4] 商品提取 + 音视频采集（主线程 Playwright 会话）...")
    print("\n" + "-" * 70)
    print(f"采集进行中（{duration}秒）...")
    browser_result = {}
    try:
        browser_result = asyncio.run(_browser_collect(
            room_id, duration, frame_interval, audio_segment,
            cdp_port, auth_file, audio_dir, frames_dir, collect_products,
        ))
    except Exception as e:
        print(f"[ERROR] 浏览器采集异常: {e}")
        import traceback
        traceback.print_exc()

    # ============ 停止采集 ============
    print("\n停止采集...")
    fetcher.stop()
    ws_thread.join(timeout=5)
    products = browser_result.get("products", [])
    recorder.set_products(products)
    recorder.set_promotion_order(fetcher.product_refresh_order)
    recorder.close()

    result["products"] = products
    result["data_dir"] = recorder.path
    result["media"] = {
        "stream_urls": browser_result.get("stream_urls", []),
        "audio_count": len(browser_result.get("audio_segments", [])),
        "frame_count": len(browser_result.get("frames", [])),
        "output_dir": str(base_dir),
    }

    # ============ 汇总 ============
    end_time = datetime.now()
    result["end_time"] = end_time.isoformat()
    total_duration = (end_time - start_time).total_seconds()

    print("\n" + "=" * 70)
    print("采集完成汇总")
    print("=" * 70)
    print(f"主播: {nickname} ({user_id})")
    print(f"商品: {len(products)} 个")
    print(f"弹幕数据目录: {recorder.path}")
    print(f"音频片段: {result['media']['audio_count']} 个")
    print(f"视频帧: {result['media']['frame_count']} 张")
    print(f"流 URL: {len(result['media']['stream_urls'])} 个")
    print(f"总时长: {total_duration / 60:.1f} 分钟")
    print("=" * 70)

    return result


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    room_id = sys.argv[1] if len(sys.argv) > 1 else "646454278948"
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    collect_all(room_id, duration)
