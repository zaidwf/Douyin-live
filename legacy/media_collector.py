#!/usr/bin/python
# coding:utf-8 -*-
"""抖音直播音视频采集模块 - 复用 Playwright 页面采集音频片段和视频帧

流 URL 提取参考 douyin_paqu 项目（HTML 解析 flv_pull_url）：
- 从直播间 HTML 的 self.__pace_f.push 数据解析 flv_pull_url（多画质 FLV 流地址）
- 音频：ffmpeg 从 FLV 流切 **16kHz 单声道无损 WAV** 片段（供 Wav2Vec 2.0 / librosa-OpenSMILE）
- 视频帧：定期用 Playwright 截图（JPEG）
"""
import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from playwright.async_api import async_playwright

# 流 URL 关键词（网络拦截 fallback 用）
STREAM_URL_KEYWORDS = ["pull-flv", ".flv", ".m3u8", "live.douyin.com/stream"]


class MediaCollector:
    """抖音直播音视频采集器"""

    def __init__(self, room_id: str, output_dir: str = "data"):
        self.room_id = room_id
        self.output_dir = Path(output_dir)
        self.stream_urls: List[str] = []
        self.audio_dir: Optional[Path] = None
        self.frames_dir: Optional[Path] = None

    def _setup_dirs(self, session_ts: str):
        """创建输出目录结构 data/live_{room_id}_{ts}/{audio,frames}/"""
        base = self.output_dir / f"live_{self.room_id}_{session_ts}"
        self.audio_dir = base / "audio"
        self.frames_dir = base / "frames"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        return base

    @staticmethod
    def _extract_stream_urls_from_html(html: str) -> List[str]:
        """从直播间 HTML 的 self.__pace_f.push 数据解析 flv_pull_url（多画质 FLV 流）

        参考 douyin_paqu / douyin 抖音直播录制的解析路径：
        state.roomStore.roomInfo.room.stream_url.flv_pull_url（字典：FULL_HD1/HD1/SD1/SD2）
        """
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        for sc in scripts:
            if 'flv' not in sc or 'status' not in sc or 'h265' not in sc:
                continue
            finds = re.findall(r'self\.__pace_f\.push\(\[1,(.*?)\]\)', sc)
            if not finds:
                continue
            try:
                json_obj = json.loads(finds[0])
                json_obj = json.loads(json_obj[2:])
                room = json_obj[3]['state']['roomStore']['roomInfo']['room']
                flv = room.get('stream_url', {}).get('flv_pull_url', {})
                urls = [u for u in flv.values() if u]
                if urls:
                    return urls
            except Exception:
                continue
        return []

    def _on_response(self, response):
        """网络拦截 fallback：捕获流 URL（HTML 解析失败时使用）"""
        url = response.url
        if any(kw in url for kw in STREAM_URL_KEYWORDS):
            if response.status in (200, 302):
                if url not in self.stream_urls:
                    self.stream_urls.append(url)

    async def _capture_audio_segment(self, index: int, duration: int) -> Optional[str]:
        """用 ffmpeg 从流 URL 切一段音频（逐个尝试捕获到的流 URL）

        音频口径（2026-09-28 与 `collect_full.py` 统一）：**16kHz 单声道无损 WAV**
        （`pcm_s16le`）—— Wav2Vec 2.0 要求 16kHz 输入，且 mp3 有损会伤害 LiveLens 的
        音调/情绪专家特征。原口径为 `22050Hz mp3 64k`。
        """
        if not self.stream_urls:
            return None

        output = self.audio_dir / f"segment_{index:03d}.wav"

        # 逐个尝试流 URL（流 URL 可能过期，第一个失败则试下一个）
        for url in self.stream_urls:
            cmd = [
                "ffmpeg", "-nostdin",   # 不读父进程 stdin（否则长跑可能吞掉终端输入）
                "-i", url,
                "-t", str(duration),
                "-vn",  # 去掉视频
                "-ac", "1",
                "-ar", "16000",
                "-acodec", "pcm_s16le",
                "-f", "wav",
                str(output),
                "-loglevel", "error",
                "-y",
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=duration + 15)
                if output.exists() and output.stat().st_size > 0:
                    return str(output)
            except asyncio.TimeoutError:
                continue
            except FileNotFoundError:
                print("ffmpeg 未找到，请安装 ffmpeg 并加入 PATH")
                return None
            except Exception:
                continue
        return None

    async def _capture_frame(self, page, index: int) -> Optional[str]:
        """提取一帧直播画面（优先 ffmpeg 从流 URL 提取原始帧，fallback video 截图）

        ffmpeg 提取原始帧：直接从 FLV 流解码，得到原始分辨率（如 540×960）的纯画面，
        无浏览器缩放黑边。video.screenshot() 截取的是浏览器渲染容器（1080×762），可能有黑边。
        """
        output = self.frames_dir / f"frame_{index:04d}.jpg"

        # 方案 1：ffmpeg 从流 URL 提取原始帧
        for url in self.stream_urls:
            cmd = ["ffmpeg", "-i", url,
                   "-frames:v", "1",  # 只提取 1 帧
                   "-q:v", "2",       # 高质量 JPEG
                   str(output), "-loglevel", "error", "-y"]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await asyncio.wait_for(proc.communicate(), timeout=15)
                if output.exists() and output.stat().st_size > 0:
                    return str(output)
            except Exception:
                continue

        # 方案 2：fallback 用 Playwright 截图（video 元素）
        try:
            video = await page.query_selector('video')
            if video:
                await video.screenshot(path=str(output), type="jpeg", quality=70, timeout=10000)
            else:
                await page.screenshot(path=str(output), type="jpeg", quality=70, timeout=10000)
            return str(output)
        except Exception:
            return None

    async def _start_frame_extractor(self, fps: float):
        """启动持续 ffmpeg 进程抽帧（一次连接，持续输出帧）

        支持高频（如 fps=1 每秒一帧），避免每次重新连接流。
        输出到 self.frames_dir/frame_%04d.jpg。
        """
        output_pattern = self.frames_dir / "frame_%04d.jpg"
        cmd = ["ffmpeg", "-i", self.stream_urls[0], "-vf", f"fps={fps}",
               "-q:v", "2", str(output_pattern), "-loglevel", "error", "-y"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            return proc
        except Exception:
            return None

    async def capture(self, duration: int = 60, frame_interval: int = 10,
                      audio_segment: int = 30, cdp_port: Optional[int] = None,
                      auth_file: Optional[str] = None) -> dict:
        """采集指定时长内的音视频

        :param duration: 总采集时长（秒）
        :param frame_interval: 截图间隔（秒）
        :param audio_segment: 每段音频时长（秒）
        :param cdp_port: CDP 端口（连接真实 Chrome）
        :param auth_file: 登录态文件（独立浏览器模式）
        :return: 采集结果统计
        """
        room_url = f"https://live.douyin.com/{self.room_id}"
        session_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        base = self._setup_dirs(session_ts)

        result = {
            "room_id": self.room_id,
            "duration": duration,
            "audio_segments": [],
            "frames": [],
            "stream_urls": [],
            "output_dir": str(base),
        }

        async with async_playwright() as p:
            if cdp_port:
                browser = await p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
            else:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    storage_state=auth_file,
                )

            page = await context.new_page()
            page.on("response", self._on_response)  # 保留网络拦截作为 fallback

            try:
                await page.goto(room_url, wait_until="commit", timeout=60000)
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await asyncio.sleep(8)

                # 主策略：从页面 HTML 解析 flv_pull_url（多画质 FLV 流）
                html = await page.content()
                self.stream_urls = self._extract_stream_urls_from_html(html)
                # fallback：网络拦截（HTML 解析失败时，等待响应捕获）
                if not self.stream_urls:
                    await asyncio.sleep(5)
                result["stream_urls"] = self.stream_urls[:5]

                # 启动持续 ffmpeg 抽帧（一次连接，持续输出帧，支持高频 1 秒/帧）
                frame_extractor = None
                if self.stream_urls:
                    fps = 1.0 / frame_interval
                    frame_extractor = await self._start_frame_extractor(fps)

                # 音频采集循环（截图由持续 ffmpeg 负责）
                elapsed = 0
                audio_idx = 0
                while elapsed < duration:
                    if self.stream_urls and elapsed % audio_segment < frame_interval:
                        seg_path = await self._capture_audio_segment(audio_idx, audio_segment)
                        if seg_path:
                            result["audio_segments"].append(seg_path)
                            audio_idx += 1
                    await asyncio.sleep(frame_interval)
                    elapsed += frame_interval

                # 停止持续 ffmpeg，收集生成的帧
                if frame_extractor:
                    frame_extractor.terminate()
                    try:
                        await asyncio.wait_for(frame_extractor.wait(), timeout=5)
                    except Exception:
                        pass
                frame_files = sorted(self.frames_dir.glob("frame_*.jpg"))
                result["frames"] = [str(f) for f in frame_files]

            except Exception as e:
                print(f"音视频采集错误: {e}")
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                if not cdp_port:
                    await browser.close()

        result["audio_count"] = len(result["audio_segments"])
        result["frame_count"] = len(result["frames"])
        return result


def capture_media(room_id: str, duration: int = 60, frame_interval: int = 10,
                  audio_segment: int = 30, cdp_port: Optional[int] = None,
                  auth_file: Optional[str] = None) -> dict:
    """同步包装，采集直播音视频"""
    collector = MediaCollector(room_id)
    return asyncio.run(collector.capture(
        duration, frame_interval, audio_segment, cdp_port, auth_file
    ))
