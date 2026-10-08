#!/usr/bin/python
# coding:utf-8

import codecs
import glob
import gzip
import hashlib
import os
import random
import re
import string
import subprocess
import threading
import time
import urllib.parse
from contextlib import contextmanager
from unittest.mock import patch

import requests
import websocket
import json
from py_mini_racer import MiniRacer
from protobuf.douyin import *

# GUI 依赖惰性导入：无 tkinter 的 Linux 服务器也能 import 本模块（仅 GUI 功能不可用）
try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox, simpledialog
except ImportError:
    tk = ttk = scrolledtext = messagebox = simpledialog = None

# 完整浏览器请求头：抖音风控对缺失浏览器特征头的请求返回 HTTP 444（数据中心 IP 尤其严格）
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


@contextmanager
def patched_popen_encoding(encoding='utf-8'):
    original_popen_init = subprocess.Popen.__init__

    def new_popen_init(self, *args, **kwargs):
        kwargs['encoding'] = encoding
        original_popen_init(self, *args, **kwargs)

    with patch.object(subprocess.Popen, '__init__', new_popen_init):
        yield


def generateSignature(wss, script_file='sign.js'):
    """
    出现gbk编码问题则修改 python模块subprocess.py的源码中Popen类的__init__函数参数encoding值为 "utf-8"
    """
    params = ("live_id,aid,version_code,webcast_sdk_version,"
              "room_id,sub_room_id,sub_channel_id,did_rule,"
              "user_unique_id,device_platform,device_type,ac,"
              "identity").split(',')
    wss_params = urllib.parse.urlparse(wss).query.split('&')
    wss_maps = {i.split('=')[0]: i.split("=")[-1] for i in wss_params}
    tpl_params = [f"{i}={wss_maps.get(i, '')}" for i in params]
    param = ','.join(tpl_params)
    md5 = hashlib.md5()
    md5.update(param.encode())
    md5_param = md5.hexdigest()

    with codecs.open(script_file, 'r', encoding='utf8') as f:
        script = f.read()

    ctx = MiniRacer()
    ctx.eval(script)

    try:
        signature = ctx.call("get_sign", md5_param)
        return signature
    except Exception as e:
        print(e)

    # 以下代码对应js脚本为sign_v0.js
    # context = execjs.compile(script)
    # with patched_popen_encoding(encoding='utf-8'):
    #     ret = context.call('getSign', {'X-MS-STUB': md5_param})
    # return ret.get('X-Bogus')


def generate_a_bogus(params_str, user_agent, script_file='lib/reverse/douyin_old_algo_ref.js'):
    """
    通过 py_mini_racer 调用 douyin_old_algo_ref.js 中的 sign_datail 生成 a_bogus
    :param params_str: URL 查询参数字符串
    :param user_agent: User-Agent 字符串
    :param script_file: JS 签名脚本路径
    :return: a_bogus 签名字符串
    """
    with codecs.open(script_file, 'r', encoding='utf8') as f:
        script = f.read()
    ctx = MiniRacer()
    ctx.eval(script)
    return ctx.call("sign_datail", params_str, user_agent)


def generateMsToken(length=107):
    """
    产生请求头部cookie中的msToken字段，其实为随机的107位字符
    :param length:字符位数
    :return:msToken
    """
    random_str = ''
    base_str = string.ascii_letters + string.digits + '=_'
    _len = len(base_str) - 1
    for _ in range(length):
        random_str += base_str[random.randint(0, _len)]
    return random_str


# —— FansclubMessage.content（明文公告）的模板 ——
# 只有 type=1/2 有 content，占全部 FansclubMessage 的 ~5%（其余 ~95% 是 type=6 静默消息）。
# ⚠️ 两个模板里的「名字」含义**不同**，混用会串：
#   type=2 的 `爱生活`   = **粉丝团名**（真团名，带货房间里它是唯一来源）
#   type=1 的 `与辉同行` = **主播名**，不是团名 —— 同一个 anchor_id 会给出两个不同的名字
#     （实测 与辉同行 anchor_id=3250600708947220：type=2 给「爱生活」、type=1 给「与辉同行」）
# 故 **只有 type=2 的捕获组 3 可以作为 fc_club_name**；type=1 的方括号内容不作团名，只取 Lv。
#
# 团名与昵称都可能是含空格的任意文本，故用非贪婪 + 锚定首尾，而不是按空格切分。
FANSCLUB_JOIN_RE = re.compile(r'^恭喜\s+(?P<nick>.+?)\s+成为第(?P<seq>\d+)名(?P<club>.+?)成员$')
FANSCLUB_UPGRADE_RE = re.compile(r'^(?P<nick>.+?)\s+刚刚升级至【(?P<room>.+?)】粉丝团\s*Lv(?P<level>\d+)$')


def parseFansclubContent(fc_type, content):
    """从 `FansclubMessage.content` 明文公告里解析结构化字段（best-effort，不匹配则返回空 dict）

    实测模板（2026-09-29，`data/` 全部 4 条 type=1/2 事件）：
      type=2「恭喜 敏敏～A 成为第1597966名爱生活成员」
        → `fc_join_seq`=1597966, `fc_club_name`='爱生活'
        **`fc_join_seq` 是该主播粉丝团的**服务端权威累计加入序号** —— 单调递增，且**不受
        我们采样影响**：两条 join 事件的序号差 = 期间**真实**新增成员数（不是我们观测到的数）。
        这是全链路里唯一的无偏累计计数器，也是 type=6 缺的那个性质。代价是 type=2 极稀疏
        （~0.1 条/分），差分分辨率低。
      type=1「致橡树 刚刚升级至【与辉同行】粉丝团 Lv14」
        → `fc_upgrade_to_level`=14（`fc_club_name` **不取**，见上面的模板注释）

    与 `User.FansClub` 快照的关系：快照给状态与等级，本函数给**变化的时刻与结果**。
    返回的 `fc_parse_ok` 用于暴露模板缺口 —— 出现新模板时它会计数下降而不是无声丢失。
    """
    text = (content or '').strip()
    if not text:
        return {}
    if fc_type == 2:
        m = FANSCLUB_JOIN_RE.match(text)
        if m:
            return {'fc_parse_ok': True,
                    'fc_join_seq': int(m.group('seq')),
                    'fc_club_name': m.group('club')}
    elif fc_type == 1:
        m = FANSCLUB_UPGRADE_RE.match(text)
        if m:
            return {'fc_parse_ok': True,
                    'fc_upgrade_to_level': int(m.group('level'))}
    return {'fc_parse_ok': False}


# ---------------------------------------------------------------------------
# 音视频模态采集（2026-09-29 从 collect_full.py 迁入本模块）
#
# 迁移原因：原先只有 collect_full（单场入口）能采音视频，auto_crawl（生产长跑）拿不到，
# 多模态数据集缺这一块。迁到 liveMan 后两个入口共用同一套代码。
#
# ★ 同时去掉了 Playwright（见下方 extract_stream_urls_from_html 的说明与
#   test/probe_stream_url.py 的实测）：原实现起 headless Chromium 只为读一次页面 HTML，
#   而本模块的 room_id 请求本来就拉了同一份 HTML。100 路并发下这一项省 15-30GB 内存。
# ---------------------------------------------------------------------------

def extract_stream_urls_from_html(html):
    """从直播间 HTML 的 `self.__pace_f.push` 数据解析 flv_pull_url（多画质 FLV 流）

    解析路径：`state.roomStore.roomInfo.room.stream_url.flv_pull_url`
    （字典，键为 FULL_HD1/HD1/SD1/SD2 等画质）。逻辑与 `legacy/media_collector.py`
    的原始实现一致，未改动。

    ★ **不需要浏览器**（2026-09-29 实测，`test/probe_stream_url.py`）：
      `DouyinLiveWebFetcher.fetch_live_html()` 那条 `requests.get` 拿到的 HTML 里
      **就含 flv_pull_url**（实测 1,130,959 字节 HTML 中出现 8 次，解析出 4 个流地址，
      截取首个 URL 喂 ffmpeg **真的抽出了 72067 字节的 jpg** —— 是真能解码的地址，
      不是占位串）。Playwright 起 Chromium 读同一份页面纯属多余。

    :param html: 直播间页面 HTML 文本
    :return: 流地址列表（多个画质），解析不到返回 []
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


# 长会话看护：FLV 流地址会过期，检查间隔与最多续采次数
MEDIA_WATCHDOG_INTERVAL = 30
MEDIA_MAX_RESTARTS = 20


class MediaCapture:
    """一次直播会话的**音视频模态**采集（音频切片 + 图像抽帧）

    起停语义（定长单场与「开播到下播」共用同一套）：

        cap = MediaCapture(live_id, out_dir, fetcher=fetcher)
        cap.start_media()        # 解析流地址 + 起 ffmpeg；失败返回 False（不抛）
        ...                      # 跑多久由**调用方**决定
        result = cap.stop_media()  # 停进程 + 回捞产物 → {"audio": [...], "frames": [...]}

    区别只在**谁调 `stop_media()`**：
      - `collect_full.py`（定长单场）：`time.sleep(duration)` 后自己调
      - `auto_crawl.py`（开播到下播，时长未知）：等 `on_offline` 事件后调

    口径（与迁入前完全一致，**勿改**）：
      - 音频 `audio/segment_%03d.wav`：**16kHz 单声道无损 WAV**（`pcm_s16le`），
        `-f segment` 单进程一次连接连续切片、段间无间隙。供 Wav2Vec 2.0 /
        librosa-OpenSMILE 直接使用；**不要改回 mp3**。
      - 图像 `frames/frame_%04d.jpg`：ffmpeg 直解 FLV，**原始分辨率、无 scale**、
        无浏览器缩放/黑边/UI 叠加 —— 这是相对 Playwright `video.screenshot()` 的本质改善。

    ⚠️ **并发成本**：一路主播 = 2 个 ffmpeg 进程，视频软解约 0.5-1 核，
    **100 路 ≈ 50-100 核**、磁盘约 20-30 GB/小时。单机带不动 100 路音视频，
    故 `auto_crawl` 侧是显式开关且**默认关**。
    """

    def __init__(self, live_id, out_dir, *, fetcher=None, want_audio=True,
                 want_frames=True, audio_segment=30, frame_interval=10,
                 max_seconds=None, log=print):
        """
        :param fetcher: `DouyinLiveWebFetcher` 实例，用于复用其 ttwid/cookie 与页面请求
        :param out_dir: 落盘目录（传 `DataRecorder.path`），内部再建 `audio/` `frames/`
        :param max_seconds: **防僵尸安全上限**（`-t`），不是主停止手段；None 表示不加
        """
        self.live_id = live_id
        self.out_dir = str(out_dir)
        self.fetcher = fetcher
        self.want_audio = want_audio
        self.want_frames = want_frames
        self.audio_segment = audio_segment
        self.frame_interval = frame_interval
        self.max_seconds = max_seconds
        self.log = log
        self.audio_dir = os.path.join(self.out_dir, 'audio')
        self.frames_dir = os.path.join(self.out_dir, 'frames')
        # ffmpeg 的 stderr 落到文件而不是 DEVNULL：`-loglevel error` 平时几乎无输出，
        # 但长会话续采排查（`_watchdog` 为什么重启）全靠这里。也不能用 PIPE ——
        # 几小时跑下来不排空会写满管道缓冲把 ffmpeg 卡死。
        self.audio_log = os.path.join(self.audio_dir, 'ffmpeg.log')
        self.frame_log = os.path.join(self.frames_dir, 'ffmpeg.log')
        self.stream_url = None
        self.audio_proc = None
        self.frame_proc = None
        self.restarts = 0
        self._stop_event = threading.Event()
        self._watchdog_thread = None
        self._t0 = None

    # ---------------- 流地址 ----------------

    def _resolve_stream_url(self):
        """取当前可用流地址；**不需要浏览器**（见 extract_stream_urls_from_html）"""
        if self.fetcher is None:
            self.log('[media] 未提供 fetcher，无法解析流地址')
            return None
        html = self.fetcher.fetch_live_html()
        if not html:
            return None
        urls = extract_stream_urls_from_html(html)
        return urls[0] if urls else None

    # ---------------- 命令构造 ----------------

    def _time_cap(self):
        """`-t` 参数（防僵尸上限）。注意**不是**主停止手段，主停止是 stop_media()"""
        return ["-t", str(int(self.max_seconds))] if self.max_seconds else []

    def _audio_cmd(self, url, start_number=0):
        # **刻意不加 `-nostdin`**：停采时要往 stdin 写 `q` 让 ffmpeg 优雅退出并给
        # 当前分段收尾（见 stop_media）。子进程的 stdin 是 PIPE，不会抢终端输入。
        return (["ffmpeg", "-y", "-loglevel", "error", "-i", url]
                + self._time_cap()
                + ["-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
                   "-f", "segment", "-segment_time", str(self.audio_segment),
                   "-segment_start_number", str(start_number),
                   "-reset_timestamps", "1",
                   os.path.join(self.audio_dir, "segment_%03d.wav")])

    def _frame_cmd(self, url, start_number=0):
        # `-vf fps=1/interval`：ffmpeg 直解 FLV 抽帧，原始分辨率
        return (["ffmpeg", "-y", "-loglevel", "error", "-i", url]
                + self._time_cap()
                + ["-vf", f"fps={1.0 / self.frame_interval}", "-q:v", "2",
                   "-start_number", str(start_number),
                   os.path.join(self.frames_dir, "frame_%04d.jpg")])

    @staticmethod
    def _count_files(pattern):
        return len([p for p in glob.glob(pattern) if os.path.getsize(p) > 0])

    # ---------------- 起停 ----------------

    def _spawn(self):
        """起（或续接）ffmpeg 进程。

        ⚠️ 序号必须**续接**已有的产物：`segment_%03d.wav` / `frame_%04d.jpg` 若从 0
        重开，配合 `-y` 会**覆盖**上一段的产物。首次为 0，续采时按已落盘文件数接着排。
        """
        if self.want_audio and self.audio_proc is None:
            n = self._count_files(os.path.join(self.audio_dir, "segment_*.wav"))
            self.audio_proc = subprocess.Popen(
                self._audio_cmd(self.stream_url, n),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=open(self.audio_log, 'ab'))
        if self.want_frames and self.frame_proc is None:
            n = self._count_files(os.path.join(self.frames_dir, "frame_*.jpg"))
            self.frame_proc = subprocess.Popen(
                self._frame_cmd(self.stream_url, n),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=open(self.frame_log, 'ab'))

    def start_media(self):
        """解析流地址并起 ffmpeg 进程。返回是否**至少起了一个**进程（失败不抛异常）"""
        url = self._resolve_stream_url()
        if not url:
            self.log('[media] 未解析到流地址（未开播 / 页面无 flv_pull_url），本场音视频缺失')
            return False
        self.stream_url = url
        os.makedirs(self.audio_dir, exist_ok=True)
        os.makedirs(self.frames_dir, exist_ok=True)
        self._t0 = time.time()
        self._stop_event.clear()
        self._spawn()
        self._watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        self._watchdog_thread.start()
        self.log(f'[media] 已启动音视频采集（音频={self.want_audio} 抽帧={self.want_frames} '
                 f'间隔={self.frame_interval}s 段长={self.audio_segment}s）')
        return True

    @staticmethod
    def _graceful_stop(proc):
        """先礼后兵地停 ffmpeg：**写 `q` 让它优雅退出**，再 terminate，最后 kill。

        ⚠️ 为什么不能直接 `terminate()`：Windows 上它是 `TerminateProcess`，ffmpeg
        来不及给正在写的分段收尾，会留下 **0 字节的残片**（实测）。写 `q` 走 ffmpeg
        自己的退出路径，会 flush 并封口当前 `segment`/`frame` 文件 —— 实测最后那段
        7.66s 的音频被完整保留，而不是白白丢掉。
        """
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.stdin.write(b'q')
                proc.stdin.flush()
                proc.stdin.close()
                proc.wait(timeout=10)
            except Exception:
                pass
        if proc.poll() is None:          # 优雅退出没生效（如 stdin 已断）
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

    @staticmethod
    def _prune_empty(*patterns):
        """删掉 0 字节残片（硬杀留下的空壳），返回存活文件（大小 > 0）"""
        alive = []
        for pattern in patterns:
            for p in glob.glob(pattern):
                if os.path.getsize(p) > 0:
                    alive.append(p)
                else:
                    try:
                        os.remove(p)      # 空壳是纯噪声，留着会污染数据集与校验
                    except OSError:
                        pass
        return sorted(alive)

    def stop_media(self):
        """停所有 ffmpeg 进程并回捞产物（优雅退出 + 剔除 0 字节残片）

        :return: {"audio": [路径...], "frames": [路径...], "restarts": int, "ok": bool}
        """
        self._stop_event.set()
        self._graceful_stop(self.audio_proc)
        self._graceful_stop(self.frame_proc)
        self.audio_proc = self.frame_proc = None
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2)
        audio = self._prune_empty(os.path.join(self.audio_dir, "segment_*.wav"))
        frames = self._prune_empty(os.path.join(self.frames_dir, "frame_*.jpg"))
        return {'audio': audio, 'frames': frames, 'restarts': self.restarts,
                'ok': bool(audio or frames)}

    def stop(self):
        """`stop_media` 的别名（便于挂成 on_offline 之类的回调）"""
        return self.stop_media()

    # ---------------- 长会话看护 ----------------

    def _watchdog(self):
        """流地址会过期 → ffmpeg 可能中途早退。早退则重解析地址并**续号**续采。

        ⚠️ 本机制**尚未经长会话实测**（需一场 ≥1 小时的真实直播验证：确认过期后能续上、
        且 `segment_*.wav` 时间连续无缺口）。若实测发现问题，可先靠 `max_seconds` 兜底。
        """
        while not self._stop_event.wait(MEDIA_WATCHDOG_INTERVAL):
            # `-t` 到点属于**正常结束**，不能当早退去续采
            if self.max_seconds and (time.time() - self._t0) >= self.max_seconds:
                return
            dead_audio = (self.want_audio and self.audio_proc is not None
                          and self.audio_proc.poll() is not None)
            dead_frame = (self.want_frames and self.frame_proc is not None
                          and self.frame_proc.poll() is not None)
            if not (dead_audio or dead_frame):
                continue
            if self.restarts >= MEDIA_MAX_RESTARTS:
                self.log(f'[media] 续采已达上限 {MEDIA_MAX_RESTARTS} 次，放弃')
                return
            url = self._resolve_stream_url()
            if not url:
                continue
            self.stream_url = url
            self.restarts += 1
            self.log(f'[media] ffmpeg 提前退出（音频={dead_audio} 抽帧={dead_frame}），'
                     f'用新流地址续采（第 {self.restarts} 次）')
            if dead_audio:
                self.audio_proc = None
            if dead_frame:
                self.frame_proc = None
            self._spawn()


class DouyinLiveWebFetcher:

    def __init__(self, live_id, log_callback=None, data_callback=None):
        """
        直播间弹幕抓取对象
        :param live_id: 直播间的直播id，打开直播间web首页的链接如：https://live.douyin.com/261378947940  ，
                        其中的261378947940即是live_id
        :param log_callback: 日志回调函数
        :param data_callback: 结构化数据回调函数 data_callback(event_type, data_dict)
        """
        self.__ttwid = None
        self.__room_id = None
        self.live_id = live_id
        self.live_url = "https://live.douyin.com/"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " \
                          "Chrome/120.0.0.0 Safari/537.36"
        self.log_callback = log_callback
        self.data_callback = data_callback
        self.ws = None
        self.heartbeat_thread = None
        self.running = False
        self.product_ids = set()  # 缓存直播间出现的商品 promotion_id
        self.product_refresh_order = []  # 商品列表刷新顺序（best-effort 对齐面板商品 num）
        self.on_offline = None  # 下播回调（ControlMessage status==3 时触发）
        self.room_detail = {}  # enter API 完整解析结果（主播/直播间富字段，供 MEMF-GEF 节点用）
        self.live_page_html = None  # 最近一次拉取的直播间页面 HTML（流地址来源，见 fetch_live_html）
        self._seen_methods = set()  # 实际收到的 WebSocket method 名（诊断用）
        self._unhandled_methods = set()  # 未注册处理器的 method 名（诊断 ProductChangeMessage 等为何未捕获）
        self._room_data_sync_dumped = False  # RoomDataSync 结构是否已 dump（只 dump 首次）
        self._frame_header_diag = set()  # 已打印过的 PushFrame 头组合（诊断 gzip 判定依据）
        self.cookie_string = None  # 可选：完整 Cookie 串（登录态）。None 时仅用 ttwid（游客态）
        self.extra_ws_headers = {}  # 可选：WS 握手附加头（PC 客户端画像的 X-AWEME-* 等）
        self.raw_callback = None  # 可选：raw_callback(method, payload, msg)，收到每条消息的原始字节
        self.persist_msg_count = "15"  # WS 查询串 need_persist_msg_count（Web 端 15，PC 客户端 0）
        self.user_unique_id = "7319483754668557238"  # WS 查询串 user_unique_id / did
        # WS 连接上下文：默认用历史抓包硬编码值，可通过 im/fetch 预取新鲜值覆盖（见 set_ws_context）
        self.ws_push_host = "webcast100-ws-web-lq.douyin.com"
        self.ws_cursor = "d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
        self.ws_internal_ext = None  # None = 按 room_id 现算默认值（见 _default_internal_ext）

    def log(self, log_type, message):
        """记录日志"""
        if self.log_callback:
            self.log_callback(log_type, message)
        else:
            print(f"[{log_type}] {message}")

    def _emit_data(self, event_type, data):
        """输出结构化数据（若提供 data_callback）"""
        if self.data_callback:
            try:
                self.data_callback(event_type, data)
            except Exception:
                pass

    def start(self):
        self.running = True
        self._connectWebSocket()

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            self.heartbeat_thread.join(timeout=1.0)

    @property
    def ttwid(self):
        """
        产生请求头部cookie中的ttwid字段，访问抖音网页版直播间首页可以获取到响应cookie中的ttwid
        :return: ttwid
        """
        if self.__ttwid:
            return self.__ttwid
        headers = dict(COMMON_HEADERS)
        try:
            # 加 timeout：同 room_id property 那条，原实现无超时，挂死的连接会永久占住调用线程
            response = requests.get(self.live_url, headers=headers, timeout=30)
            response.raise_for_status()
        except Exception as err:
            self.log("ERROR", f"请求直播URL错误: {err}")
        else:
            self.__ttwid = response.cookies.get('ttwid')
            return self.__ttwid

    def fetch_live_html(self):
        """GET 直播间页面并返回 HTML 文本（请求失败返回 None）

        ★ 这份 HTML 是**流地址的唯一来源** —— 实测其中就含 `flv_pull_url`
        （`test/probe_stream_url.py`：1,130,959 字节 HTML 里出现 8 次，解析出 4 个流地址，
        截首个喂 ffmpeg 真的抽出 72067 字节的 jpg）。故 `MediaCapture` 复用它即可，
        **不需要 Playwright 另起一个 Chromium 去读同一份页面**。
        原实现在 `room_id` 里只用正则抠了 roomId，就把 `response.text` 丢掉了。

        用完整浏览器头 + `ttwid`/`msToken`/`__ac_nonce` cookie，与取 room_id 时完全一致。
        每次调用都是**新鲜请求**（流地址带签名令牌会过期，长会话续采需要重取）。

        :return: HTML 文本；失败返回 None
        """
        url = self.live_url + self.live_id
        headers = {**COMMON_HEADERS,
                   "cookie": f"ttwid={self.ttwid}&msToken={generateMsToken()}; __ac_nonce=0123407cc00a9e438deb4"}
        try:
            # 加 timeout：原实现没有超时，100 并发下一条挂死的请求会永久占住一个线程
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
        except Exception as err:
            self.log("ERROR", f"请求直播间URL错误: {err}")
            return None
        self.live_page_html = response.text
        return self.live_page_html

    @property
    def room_id(self):
        """
        根据直播间的地址获取到真正的直播间roomId，有时会有错误，可以重试请求解决
        :return:room_id
        """
        if self.__room_id:
            return self.__room_id
        html = self.fetch_live_html()
        if not html:
            return None
        match = re.search(r'roomId\\":\\"(\d+)\\"', html)
        if match is None or len(match.groups()) < 1:
            self.log("ERROR", "未找到匹配的roomId（可能未开播）")
            return None

        self.__room_id = match.group(1)
        return self.__room_id

    @staticmethod
    def _get_avatar_url(img):
        """从抖音 Image 结构提取头像 URL（兼容 url_list / url_list_list 两种形态）"""
        if not isinstance(img, dict):
            return None
        # JSON 形态：url_list: ["https://...", ...]
        for key in ('url_list', 'urlList'):
            urls = img.get(key)
            if isinstance(urls, list) and urls and urls[0]:
                return urls[0]
        # protobuf 形态：url_list_list: [[url, ...], ...]
        for key in ('url_list_list', 'urlListList'):
            ll = img.get(key)
            if isinstance(ll, list) and ll and isinstance(ll[0], list) and ll[0] and ll[0][0]:
                return ll[0][0]
        return None

    @staticmethod
    def _deep_get(mapping, *paths):
        """按多个候选路径依次取值，返回第一个非空值（容错解析 API JSON 的字段命名差异）"""
        for path in paths:
            val = mapping
            ok = True
            for key in path:
                if not isinstance(val, dict):
                    ok = False
                    break
                val = val.get(key)
            if ok and val not in (None, '', [], {}):
                return val
        return None

    def get_room_status(self):
        """
        获取直播间开播状态:
        room_status: 2 直播已结束
        room_status: 0 直播进行中
        """
        ms_token = generateMsToken()
        params = {
            'aid': '6383',
            'app_name': 'douyin_web',
            'live_id': '1',
            'device_platform': 'web',
            'language': 'zh-CN',
            'enter_from': 'web_live',
            'cookie_enabled': 'true',
            'screen_width': '1536',
            'screen_height': '864',
            'browser_language': 'zh-CN',
            'browser_platform': 'Win32',
            'browser_name': 'Edge',
            'browser_version': '133.0.0.0',
            'web_rid': self.live_id,
            'room_id_str': self.room_id,
            'enter_source': '',
            'is_need_double_stream': 'false',
            'insert_task_id': '',
            'live_reason': '',
            'msToken': ms_token,
        }
        query_str = '&'.join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        a_bogus = generate_a_bogus(query_str, self.user_agent)
        params['a_bogus'] = a_bogus

        url = 'https://live.douyin.com/webcast/room/web/enter/'
        try:
            resp = requests.get(url, params=params, headers={
                **COMMON_HEADERS,
                'Cookie': f'ttwid={self.ttwid}; msToken={ms_token}; __ac_nonce=0123407cc00a9e438deb4',
                'Referer': f'https://live.douyin.com/{self.live_id}',
            }, timeout=30)
            resp.raise_for_status()
            data = resp.json().get('data')
            if data:
                room_status = data.get('room_status')
                user = data.get('user') or {}
                room = data.get('room') or {}
                user_id = user.get('id_str') or str(user.get('id') or '')
                nickname = user.get('nickname') or ''
                status = '正在直播' if room_status == 0 else '已结束'

                # 富字段解析（enter API 一次返回，供 MEMF-GEF 主播/直播间节点使用）
                follow_info = self._deep_get(user, ('follow_info',), ('followInfo',)) or {}
                self.room_detail = {
                    'anchor_id': user_id,
                    'anchor_sec_uid': user.get('sec_uid') or user.get('secUid'),
                    'anchor_short_id': str(user.get('short_id') or user.get('shortId') or ''),
                    'anchor_nickname': nickname,
                    'anchor_signature': user.get('signature') or '',
                    'follower_count': follow_info.get('follower_count') or follow_info.get('followerCount'),
                    'following_count': follow_info.get('following_count') or follow_info.get('followingCount'),
                    'total_favorited': user.get('total_favorited') or user.get('totalFavorited'),
                    'aweme_count': user.get('aweme_count') or user.get('awemeCount'),
                    'gender': user.get('gender'),
                    'city': user.get('city'),
                    'level': self._deep_get(user, ('pay_grade', 'level'), ('payGrade', 'level')),
                    'avatar_thumb': self._get_avatar_url(user.get('avatar_thumb') or user.get('avatarThumb')),
                    'avatar_medium': self._get_avatar_url(user.get('avatar_medium') or user.get('avatarMedium')),
                    'avatar_large': self._get_avatar_url(user.get('avatar_large') or user.get('avatarLarge')),
                    'room_title': room.get('title') or '',
                    'room_status': room_status,
                    'user_count_str': room.get('user_count_str') or room.get('userCountStr') or '',
                    'room_stats': room.get('stats') or {},
                }
                self.log("STATUS", f"【{nickname}】[{user_id}]直播间：{status}.")
                return True, status, nickname, user_id
            else:
                self.log("ERROR", "获取直播间状态失败，返回数据为空")
                return False, "未知", "未知", "未知"
        except Exception as e:
            self.log("ERROR", f"获取直播间状态时出错: {str(e)}")
            return False, "错误", "未知", "未知"

    def get_audience_ranklist(self, anchor_id):
        """
        获取直播间观众用户数据
        """
        ms_token = generateMsToken()
        params = {
            'aid': '6383',
            'app_name': 'douyin_web',
            'webcast_sdk_version': '2450',
            'room_id': self.room_id,
            'anchor_id': anchor_id,
            'rank_type': '30',
            'msToken': ms_token,
        }
        query_str = '&'.join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        a_bogus = generate_a_bogus(query_str, self.user_agent)
        params['a_bogus'] = a_bogus

        headers = {
            'User-Agent': self.user_agent,
            'Cookie': f'ttwid={self.ttwid}; msToken={ms_token}; __ac_nonce=0123407cc00a9e438deb4',
            'Referer': f'https://live.douyin.com/{self.live_id}',
        }

        self.log("RANK", f"获取观众用户数据数据中.....")

        try:
            response = requests.get(
                'https://live.douyin.com/webcast/ranklist/audience/',
                params=params,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = json.loads(response.text)

            if 'data' not in data or 'ranks' not in data['data']:
                self.log("ERROR", "未获取到排名数据，请检查输入的房间ID和主播ID是否正确")
                return []

            ranks = data['data']['ranks']
            account_list = []
            for rank in ranks:
                if isinstance(rank, dict) and isinstance(rank.get('user'), dict):
                    user = rank['user']
                    account_info = {
                        'id': str(user.get('id', '未知')),
                        'nickname': user.get('nickname', '未知昵称'),
                        'display_id': user.get('display_id', ''),
                        'sec_uid': user.get('sec_uid', ''),
                        'rank': rank.get('rank'),
                        'rank_score': rank.get('score'),
                        'follower_count': user.get('follower_count') or user.get('followerCount'),
                        'avatar_thumb': self._get_avatar_url(user.get('avatar_thumb') or user.get('avatarThumb')),
                    }
                    account_list.append(account_info)
                else:
                    rank_no = rank.get('rank', '未知') if isinstance(rank, dict) else '?'
                    self.log("WARN", f"警告：第{rank_no}位用户数据缺失")

            self.log("RANK", f"成功获取到 {len(account_list)} 个账号信息")
            return account_list
        except Exception as e:
            self.log("ERROR", f"获取观众用户数据时出错: {str(e)}")
            return []

    def get_product_detail(self, promotion_id):
        """
        获取商品详情（标题/价格/图片）。
        注意：抖音商城商品详情 JSON API 需进一步逆向（含签名），本方法仅返回详情页 URL。

        **商品信息已统一改用灰豚**（直播结束后下载，字段完整权威）——不要再走
        Playwright 渲染详情页/商品面板的路线（原 `product_extractor.py` 已归档到
        `legacy/`，实测面板点击打不开）。本方法仅保留 URL 供人工查阅。
        """
        return {
            'promotion_id': str(promotion_id),
            'detail_url': f'https://haohuo.jinritemai.com/views/product/detail?id={promotion_id}',
            'title': None,
            'price': None,
            'image_url': None,
        }

    def get_product_ids(self):
        """返回直播间采集到的商品 promotion_id 列表"""
        return sorted(self.product_ids)

    def _default_internal_ext(self):
        """构造默认 internal_ext（历史抓包模板，时间戳为 2024 固定值）"""
        return ("internal_src:dim|wss_push_room_id:{room}|wss_push_did:{did}"
                "|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|"
                "wss_info:0-1721106114633-0-0|wrds_v:7392094459690748497"
                ).format(room=self.room_id, did=self.user_unique_id)

    def set_ws_context(self, cursor=None, internal_ext=None, push_host=None,
                       user_unique_id=None):
        """用 /webcast/im/fetch/ 预取的**新鲜**连接上下文覆盖硬编码兜底值

        douyinLive（Go）在正式建连前会先请求 im/fetch，拿到服务端下发的 cursor、
        internal_ext、push_server_v2 再拼 WS URL；本方法用于复现该流程。

        :param cursor: Response.cursor（如 t-1790576617885_r-1_d-1_u-1）
        :param internal_ext: Response.internal_ext
        :param push_host: Response.push_server_v2 的主机名（含 wss:// 时自动剥离）
        """
        if cursor:
            self.ws_cursor = cursor
        if internal_ext:
            self.ws_internal_ext = internal_ext
        if push_host:
            host = push_host.strip().rstrip('/')
            for scheme in ('wss://', 'ws://', 'https://', 'http://'):
                if host.startswith(scheme):
                    host = host[len(scheme):]
                    break
            # 去掉可能自带的 /webcast/im/push/v2 路径，只保留主机名
            self.ws_push_host = host.split('/')[0]
        if user_unique_id:
            self.user_unique_id = str(user_unique_id)
        self.log("WEBSOCKET", f"连接上下文已覆盖: host={self.ws_push_host} "
                              f"cursor={self.ws_cursor[:40]} ext_len={len(self.ws_internal_ext or '')}")

    def _connectWebSocket(self):
        """
        连接抖音直播间websocket服务器，请求直播间数据
        """
        if not self.room_id:
            self.log("ERROR", "无法获取room_id，无法连接WebSocket")
            return

        # push host / cursor / internal_ext 可被 im/fetch 预取的**新鲜值**覆盖（见 set_ws_context）。
        # 硬编码兜底值是历史抓包，时间戳停留在 2024，服务端可能据此判定客户端状态陈旧。
        internal_ext = self.ws_internal_ext or self._default_internal_ext()
        wss = (f"wss://{self.ws_push_host}/webcast/im/push/v2/?app_name=douyin_web"
               "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
               "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
               "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
               "&browser_name=Mozilla"
               "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
               "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
               "&browser_online=true&tz_name=Asia/Shanghai"
               f"&cursor={self.ws_cursor}"
               f"&internal_ext={internal_ext}"
               f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
               f"&user_unique_id={self.user_unique_id}&im_path=/webcast/im/fetch/&identity=audience"
               f"&need_persist_msg_count={self.persist_msg_count}&insert_task_id=&live_reason=&room_id={self.room_id}&heartbeatDuration=0")

        signature = generateSignature(wss)
        wss += f"&signature={signature}"

        headers = {
            "cookie": self.cookie_string or f"ttwid={self.ttwid}",
            'user-agent': self.user_agent,
        }
        headers.update(self.extra_ws_headers)

        self.log("WEBSOCKET", f"正在连接WebSocket: {wss[:100]}...")
        self.log("WEBSOCKET", f"Cookie 模式: {'登录态' if self.cookie_string else '游客态(仅 ttwid)'} "
                              f"len={len(headers['cookie'])}")

        try:
            self.ws = websocket.WebSocketApp(wss,
                                             header=headers,
                                             on_open=self._wsOnOpen,
                                             on_message=self._wsOnMessage,
                                             on_error=self._wsOnError,
                                             on_close=self._wsOnClose)
            self.ws.run_forever()
        except Exception as e:
            self.log("ERROR", f"WebSocket连接错误: {str(e)}")
            self.stop()

    def _sendHeartbeat(self):
        """
        发送心跳包
        """
        while self.running:
            try:
                if self.ws and self.ws.sock and self.ws.sock.connected:
                    heartbeat = PushFrame(payload_type='hb').SerializeToString()
                    self.ws.send(heartbeat, websocket.ABNF.OPCODE_PING)
                    self.log("HEARTBEAT", "发送心跳包...")
                else:
                    self.log("WARN", "WebSocket未连接，停止发送心跳")
                    break
            except Exception as e:
                self.log("ERROR", f"发送心跳包时出错: {str(e)}")
                break
            else:
                time.sleep(5)

    def _wsOnOpen(self, ws):
        """
        连接建立成功
        """
        self.log("WEBSOCKET", "WebSocket连接成功.")
        self.heartbeat_thread = threading.Thread(target=self._sendHeartbeat)
        self.heartbeat_thread.daemon = True
        self.heartbeat_thread.start()

    def _decodeFramePayload(self, package):
        """解出 PushFrame.payload 的 Response 字节（按实际压缩标志判断，不硬编码 gzip）

        抖音 PushFrame 头部会下发 compress_type=gzip，但并非所有帧都压缩（心跳/短帧常见明文）。
        这里以 **gzip 魔数 0x1f 0x8b** 为准：是压缩体就解压，否则按原文解析。同时把头部组合
        打印一次，用于核对服务端声明的压缩方式（诊断礼物等消息是否被解压失败吞掉）。
        """
        payload = package.payload
        hdr = {h.key: h.value for h in package.headers_list}
        is_gzip = payload[:2] == b'\x1f\x8b'

        sig = (package.payload_type, hdr.get('compress_type', ''), str(is_gzip))
        if sig not in self._frame_header_diag:
            self._frame_header_diag.add(sig)
            self.log("FRAME", f"PushFrame payloadType={package.payload_type!r} "
                              f"headers={hdr} payload_len={len(payload)} gzip_magic={is_gzip}")

        if is_gzip:
            return gzip.decompress(payload)
        # 声明 gzip 但无 gzip 魔数：按明文解析（短帧常见，日志已在上面的 FRAME 行体现）
        return payload

    def _wsOnMessage(self, ws, message):
        """
        接收到数据
        :param ws: websocket实例
        :param message: 数据
        """

        # 根据proto结构体解析对象
        package = PushFrame().parse(message)
        response = Response().parse(self._decodeFramePayload(package))

        # 返回直播间服务器链接存活确认消息，便于持续获取数据。
        # 客户端回显的是**下行 PushFrame 头部 im-internal_ext 的原值**，不是 Response 内部的
        # internal_ext 字段（抓包实测两者 first_req_ms/seq 存在系统性差异，见 douyinLive
        # message_decode.go 注释）。ACK 内容错会让服务端认为客户端 seq 不同步。
        hdr_ext = next((h.value for h in package.headers_list if h.key == 'im-internal_ext'), '')
        ack_ext = hdr_ext or response.internal_ext
        if ack_ext:
            try:
                ack = PushFrame(log_id=package.log_id,
                                payload_type='ack',
                                payload=ack_ext.encode('utf-8')
                                ).SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                self.log("ERROR", f"发送ACK时出错: {str(e)}")

        # 根据消息类别解析消息体
        for msg in response.messages_list:
            method = msg.method
            self._seen_methods.add(method)
            try:
                handler = {
                    'WebcastChatMessage': self._parseChatMsg,
                    'WebcastGiftMessage': self._parseGiftMsg,
                    'WebcastLikeMessage': self._parseLikeMsg,
                    'WebcastMemberMessage': self._parseMemberMsg,
                    'WebcastSocialMessage': self._parseSocialMsg,
                    'WebcastRoomUserSeqMessage': self._parseRoomUserSeqMsg,
                    'WebcastFansclubMessage': self._parseFansclubMsg,
                    'WebcastControlMessage': self._parseControlMsg,
                    'WebcastEmojiChatMessage': self._parseEmojiChatMsg,
                    'WebcastRoomStatsMessage': self._parseRoomStatsMsg,
                    'WebcastRoomMessage': self._parseRoomMsg,
                    'WebcastRoomRankMessage': self._parseRankMsg,
                    'WebcastRoomStreamAdaptationMessage': self._parseRoomStreamAdaptationMsg,
                    'WebcastLiveShoppingMessage': self._parseLiveShoppingMsg,
                    'WebcastLiveEcomGeneralMessage': self._parseLiveEcomGeneralMsg,
                    'WebcastProductChangeMessage': self._parseProductChangeMsg,
                    'WebcastAwemeShopExplainMessage': self._parseAwemeShopExplainMsg,
                    'WebcastRoomDataSyncMessage': self._parseRoomDataSyncMsg,
                }.get(method)
                if self.raw_callback:
                    # 原始 payload 旁路（口径标定用）：可在不改 proto 的前提下 dump 全部 wire 字段
                    self.raw_callback(method, msg.payload, msg)
                if handler:
                    handler(msg.payload)
                elif method not in self._unhandled_methods:
                    # 记录未注册的 method（诊断 ProductChangeMessage 等消息为何未捕获）
                    self._unhandled_methods.add(method)
                    self.log("WARN", f"未注册处理器的消息类型: {method}")
            except Exception as e:
                self.log("ERROR", f"尝试解析消息可能出错: {str(e)}")

    def _wsOnError(self, ws, error):
        self.log("ERROR", f"WebSocket错误: {str(error)}")

    def _wsOnClose(self, ws, *args):
        self.log("WEBSOCKET", "WebSocket连接已关闭.")
        self.running = False

    def _parseChatMsg(self, payload):
        """聊天消息"""
        message = ChatMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        content = message.content
        self.log("CHAT", f"[{user_id}]{user_name}: {content}")
        data = {"user_id": str(user_id), "nickname": user_name, "content": content}
        fc = self._fansclub_of(message.user)
        if fc:
            data.update(fc)
        self._emit_data("chat", data)

    def _parseGiftMsg(self, payload):
        """礼物消息（gift 事件：单条礼物通知 + 补全 gift_id/gift_type/连击字段）

        字段口径（对齐 douyinlive-proto + DouyinBarrageGrab）：
          combo_count(6)   = 本批连击数量（本次增量件数，聚合用这个）
          repeat_count(5)  = 连击累计次数
          total_count(29)  = 该用户在房内累计送出数（含历史）
          diamond_count(12)= 单个礼物价值（钻石/抖币），单价
          repeat_end(9)    = 连击结束标记（1 表示连击串收尾，不再代表新增量）
          group_id(11)     = 连击分组 id（单发礼物为 0）
        """
        message = GiftMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        gift_id = message.gift.id            # 礼物类型ID（GiftStruct.id，稳定标识）
        gift_name = message.gift.name
        gift_type = message.gift.type
        gift_cnt = message.combo_count        # 本批连击数量
        repeat_count = message.repeat_count   # 连击累计次数
        total_count = message.total_count     # 累计送出数（含历史）
        diamond_count = message.gift.diamond_count  # 单个礼物价值（钻石/抖币）
        repeat_end = message.repeat_end       # 连击结束标记
        group_id = message.group_id           # 连击分组 id
        self.log("GIFT", f"{user_name} 送出了 {gift_name}x{gift_cnt}（价值 {diamond_count}/个，"
                         f"连击结束={repeat_end}, group_id={group_id}）")
        self._emit_data("gift", {
            "user_id": str(user_id), "nickname": user_name,
            "gift_id": str(gift_id), "gift_name": gift_name, "gift_type": gift_type,
            "gift_count": gift_cnt, "repeat_count": repeat_count, "total_count": total_count,
            "diamond_value": diamond_count, "repeat_end": repeat_end, "group_id": group_id,
        })

    def _parseLikeMsg(self, payload):
        '''点赞消息（total 为直播间累计点赞，count 为本次增量）'''
        message = LikeMessage().parse(payload)
        user_name = message.user.nick_name
        count = message.count
        total = message.total
        self.log("LIKE", f"{user_name} 点了{count}个赞，累计 {total}")
        data = {"nickname": user_name, "count": count, "total": total}
        # 点赞消息原先不带 user_id；补上以便粉丝团聚合能按用户去重
        if message.user and message.user.id:
            data["user_id"] = str(message.user.id)
        fc = self._fansclub_of(message.user)
        if fc:
            data.update(fc)
        self._emit_data("like", data)

    def _parseMemberMsg(self, payload):
        '''进入直播间消息'''
        message = MemberMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        gender = ["女", "男"][message.user.gender] if message.user.gender in (0, 1) else "未知"
        self.log("ENTER", f"[{user_id}][{gender}]{user_name} 进入了直播间")
        data = {"user_id": str(user_id), "nickname": user_name, "gender": gender}
        fc = self._fansclub_of(message.user)
        if fc:
            data.update(fc)
        self._emit_data("enter", data)

    def _parseSocialMsg(self, payload):
        '''社交消息（关注/分享，按 action 区分）

        action 语义（与辉同行 1h 实测 2026-09-17 定案，09-24 复测修订）：
          1 = 关注（follow_count 为**主播实时粉丝总数快照**，取最新值）
          3 = 分享（follow_count 恒为 0，因为分享不改变粉丝数；share_type/share_target 区分分享渠道）
          2（取消关注）在多次整段观测中均未出现，属罕见事件。

        注意 follow_count 不具备单调性：09-24 与辉同行 1h 内 486 次关注事件中 72 次
        出现下降（-1 ~ -63），系取关导致总数回落。它反映的是"当前粉丝总数"这一时刻量，
        不是累计量，取值时应取最新快照而非最大值/累加。
        '''
        message = SocialMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        action = message.action
        follow_count = message.follow_count
        share_type = message.share_type
        share_target = message.share_target
        fc = self._fansclub_of(message.user)
        base = {"user_id": str(user_id), "nickname": user_name,
                "action": action, "follow_count": follow_count}
        if fc:
            base.update(fc)
        if action == 1:
            self.log("FOLLOW", f"[{user_id}]{user_name} 关注了主播 (粉丝数 {follow_count})")
            self._emit_data("follow", base)
        else:
            self.log("SOCIAL", f"[{user_id}]{user_name} action={action}(分享), share_type={share_type}, "
                               f"share_target={share_target!r}, 粉丝数={follow_count}")
            base.update({"share_type": share_type, "share_target": share_target})
            self._emit_data("social", base)

    def _parseRoomUserSeqMsg(self, payload):
        '''直播间统计'''
        message = RoomUserSeqMessage().parse(payload)
        current = message.total
        total = message.total_pv_for_anchor
        self.log("STATS", f"当前观看人数: {current}, 累计观看人数: {total}")
        self._emit_data("stats", {"viewer_count": current, "total_pv": total})

    @staticmethod
    def _fansclub_of(user):
        """提取用户粉丝团标识（`User.FansClub` 字段 24 → `data` 字段 1）

        粉丝团标识不是独立消息，而是**嵌在每个 User 对象里**的快照，任意携带 User 的消息
        （Chat/Member/Like/Social/Fansclub）都可读到。被动读取，无额外请求。

        字段口径（2026-09-28 实测，爱乐-L + 与辉同行，见 modify.md 续5/续6）：
          level(2)     粉丝团等级。`level > 0` 与 `anchor_id != 0` **完全等价**（交叉表零例外），
                       判「是否成员」用 `level > 0` 即可。
          anchor_id(6) 粉丝团**归属主播 ID** —— 「哪个粉丝团」的标识，覆盖率 ~100%（娱乐/带货均如此）。
                       爱乐-L（团播）实测 2 个不同值，与辉同行 1 个。
          status(3)    user_fans_club_status。0 = 非成员（与 anchor_id==0 等价，交叉表零例外）；
                       1 与 2 的语义**未定标**（与辉同行 status=2 占 1827、status=1 仅 53，
                       与爱乐-L 分布不同，成因未定），**只记录原始值，不作特征**。
                       （旧注释写「跨房间反转」——那是被基数误导的判断，不成立。）
          club_name(1) 粉丝团名称。覆盖率低且房间相关（娱乐团播 4~8%，带货 **0%**），
                       不作为标识使用；团名兜底来源是 FansclubMessage.content 的明文文本。

        :return: dict（含 level/anchor_id/status，club_name 非空时才带），无 FansClub 结构时返回 None
        """
        fc = getattr(user, 'fans_club', None)
        d = getattr(fc, 'data', None) if fc else None
        if d is None:
            return None
        info = {
            "fansclub_level": int(d.level or 0),
            "fansclub_anchor_id": int(d.anchor_id or 0),
            "fansclub_status": int(d.user_fans_club_status or 0),
        }
        if d.club_name:
            info["fansclub_name"] = d.club_name
        return info

    def _parseFansclubMsg(self, payload):
        '''粉丝团消息（加入/升级**事件**，是粉丝团身份变化的精确时间戳来源）

        `FansclubMessage`：commonInfo(1) / type(2) / content(3) / user(4)

        `type` 实测值：**1 = 升级，2 = 加入** —— 均带明文 `content` 公告，只占全部
        `FansclubMessage` 的 ~5%。**其余 ~95% 是 type=6**：静默消息，`content` 恒为空
        （wire 层面 f3 字段本身不下发）。

        type=6 的语义曾定标为「灯牌点亮」，但**已决定不采用**：其获取完整性不可测
        （非循环翻转见证检验在本语料上取不到样本），且到达高度爆发（单场 40 倍方差）。
        详见 `modify.md` 2026-09-29（续）的「舍弃决定」节。此处**不按 type 作语义解释**，
        只把 `fc_event_type` 原样传出，由 `data_recorder` 的全量计数 + `event_type_dist`
        原始分布记录。

        `content` 是明文中文，例如：
          type=1 「丫*** 刚刚升级至【爱乐-L】粉丝团 Lv6」（Lv6 与 user.fans_club.level=6 一致）
          type=2 「恭喜 姥山的姥 成为第1597964名爱生活成员」← **团名的唯一兜底来源**
        （`user.fans_club.data.club_name` 在带货房间恒为空，只能从这段文本里取团名）

        与 `User.FansClub` 快照互补：快照给状态，本事件给**变化时刻**。

        content 的结构化解析见模块级 `parseFansclubContent()` —— 它把 type=2 的「第 N 名」
        提成 `fc_join_seq`（**服务端权威累计加入序号**，无采样偏差）与 `fc_club_name`（真团名），
        把 type=1 的 Lv 提成 `fc_upgrade_to_level`。**type=1 方括号里是主播名、不是团名**，
        该函数按此约定刻意不产出 `fc_club_name`。
        '''
        message = FansclubMessage().parse(payload)
        user = message.user
        data = {"fc_event_type": message.type, "content": message.content}
        # 明文公告 → 结构化字段（best-effort；fc_parse_ok=False 表示模板未命中，需回看 content）
        data.update(parseFansclubContent(message.type, message.content))
        if user:
            data["user_id"] = str(user.id)
            data["nickname"] = user.nick_name
        fc = self._fansclub_of(user)
        if fc:
            data.update(fc)
        self.log("FANSCLUB", f"type={message.type}({message.content})")
        self._emit_data("fansclub", data)

    def _parseLiveShoppingMsg(self, payload):
        '''商品成交/状态消息（含 promotion_id）

        msg_type 语义（经数据实证）：
          2 = 成交/下单（与 LiveEcomGeneralMessage 的 LivePopMessage 购买通知一一对应）
          3 = 下架/讲解结束（语义待完全确认）
          10 = 其他
        真正的讲解开始/结束在 ProductChangeMessage（explainType）。
        注意：LiveShoppingMessage.promotionId（字段3）经灰豚验证并非真正的 promotion_id，
        成交商品的 promotion_id 以 LiveEcomGeneralMessage 的 LivePopMessage（f6.f2）为准。
        '''
        message = LiveShoppingMessage().parse(payload)
        msg_type = message.msg_type
        promotion_id = message.promotion_id
        if promotion_id:
            self.product_ids.add(promotion_id)
        type_desc = {2: "成交/下单", 3: "下架/讲解结束", 10: "其他"}.get(msg_type, str(msg_type))
        self.log("SHOPPING", f"商品状态: type={msg_type}({type_desc}), promotion_id={promotion_id}")
        self._emit_data("shopping", {"msg_type": msg_type, "promotion_id": str(promotion_id)})
        if promotion_id:
            self._emit_data("product", {"promotion_id": str(promotion_id)})

    def _parseLiveEcomGeneralMsg(self, payload):
        '''电商通用消息（购买通知 / 商品列表刷新）'''
        message = LiveEcomGeneralMessage().parse(payload)
        msg_type = message.type
        timestamp = message.timestamp
        # 递归解析 biz_content，得到结构化字段列表
        fields = self._parse_protobuf_fields(message.biz_content)

        if msg_type == 'ProductRefreshMessage':
            # 商品列表刷新：提取所有 19 位 varint 作为商品 ID（按出现顺序记录，用于对齐面板商品）
            for fn, wt, val, sub in fields:
                if wt == 0 and len(str(val)) >= 19:
                    if val not in self.product_ids:
                        self.product_refresh_order.append(val)
                    self.product_ids.add(val)
                    self._emit_data("product", {"promotion_id": str(val)})
        elif msg_type == 'LivePopMessage':
            # 购买通知：购买 ID 在嵌套消息 f6.f2 中。
            # Bug 2 已定案（2026-09-17 1h 实测）：f6 三元组结构为
            #   f6.f1 = LiveShoppingMessage.promotionId（字段3，与 shopping 消息同一 ID 体系）
            #   f6.f2 = 真正的 promotion_id（灰豚 pId/id 命中，讲解成交 80%）
            #   f6.f3 = unix 时间戳
            # 同一笔成交，shopping type=2 与 LivePopMessage 各报一次，329 次一一对应但两个 ID 不同。
            # 仅「成交」类 LivePopMessage 带 f6；其余（点赞榜/进场等）f6 为空。
            purchase_ids = []
            f6_dump = []
            for fn, wt, val, sub in fields:
                if fn == 6 and sub:
                    for sfn, swt, sval, _ in sub:
                        f6_dump.append((sfn, swt, sval))
                        if sfn == 2 and swt == 0 and len(str(sval)) >= 19:
                            purchase_ids.append(sval)
            self.log("PURCHASE", f"type={msg_type}, ts={timestamp}, purchase_ids={purchase_ids}, f6={f6_dump}")
            if purchase_ids:
                self._emit_data("purchase", {"msg_type": msg_type, "ids": [str(i) for i in purchase_ids]})

    def _parseProductChangeMsg(self, payload):
        '''商品变化消息（含讲解状态 explainType）

        ProductChangeMessage 是商品讲解状态变化的真正来源：
          updateProductInfoList: 商品列表（promotionId + explainType + index）
          updateToast: 提示文案（如"讲解中"/"已讲解"），可直接辅助确认语义
        explainType 语义待实测确认（初步假设：0=未讲解, 1=讲解中, 2=已讲解），
        本方法只记录原始值，不做解释。
        '''
        message = ProductChangeMessage().parse(payload)
        toast = message.update_toast
        prods = []
        for pinfo in message.update_product_info_list:
            pid = pinfo.promotion_id
            et = pinfo.explain_type
            idx = pinfo.index
            if pid:
                self.product_ids.add(pid)
                prods.append((pid, et, idx))
        self.log("PRODUCT", f"商品讲解变化: toast={toast!r}, 商品数={len(prods)}, 明细={[(p, e) for p, e, _ in prods]}")
        for pid, et, idx in prods:
            self._emit_data("explain", {"promotion_id": str(pid), "explain_type": et, "index": idx})

    def _parseAwemeShopExplainMsg(self, payload):
        '''讲解状态消息（WebcastAwemeShopExplainMessage）

        AwemeShopExplainMessage.Extra.active（bool）直接表示"该商品是否正在讲解"，
        是比 ProductChangeMessage.explainType 更明确的讲解信号（explainType 全网无枚举定义）。

        讲解口径：
          active 上升沿（False→True）= 开始讲解 → 讲解次数 explain_count +1
          active 下降沿（True→False）= 讲解结束
        （上升沿计数在 data_recorder._record_explain_active 里实现，这里只发原始信号。）
        '''
        message = AwemeShopExplainMessage().parse(payload)
        extra = message.extra
        pid = extra.promotion_id
        active = extra.active
        self.log("EXPLAIN", f"商品 {pid} 讲解状态 active={active}")
        if pid:
            self._emit_data("explain", {"promotion_id": str(pid), "active": active})

    def _parseRoomDataSyncMsg(self, payload):
        '''直播间组件数据同步（WebcastRoomDataSyncMessage）

        实测 2026-09-22（与辉同行）：这是"组件级"同步通道，f3 为同步类型字符串，
        已观测到 InputPanelComponentSyncData / PreviewFeaturedChatSyncData 两种，
        均非商品讲解同步。讲解开始信号 Web 端 WebSocket 拿不到
        （WebcastProductChangeMessage 全程缺席）。
        此处只记录同步类型（诊断：观察是否会出现商品/讲解类 SyncData），
        不做 promotion_id 提取——消息内 19 位 ID 是 room_id/author_id/msg_id，
        误提取会污染 product_ids。
        '''
        fields = self._parse_protobuf_fields(payload)
        sync_type = None
        for fn, wt, val, sub in fields:
            if fn == 3 and wt == 2 and isinstance(val, bytes):
                try:
                    s = val.decode('utf-8')
                    if 'SyncData' in s:
                        sync_type = s
                except Exception:
                    pass
        self.log("ROOM", f"RoomDataSync 同步类型 = {sync_type!r}")
        if not self._room_data_sync_dumped:
            self._room_data_sync_dumped = True
            for fn, wt, val, sub in fields:
                if wt == 0:
                    self.log("ROOM", f"    f{fn} varint = {val}")
                elif sub:
                    self.log("ROOM", f"    f{fn} nested ({len(sub)} 子字段)")
                else:
                    self.log("ROOM", f"    f{fn} bytes ({len(val or b'')}B)")

    @staticmethod
    def _parse_protobuf_fields(data, depth=0):
        """递归解析 protobuf bytes，返回 [(field_num, wire_type, value, sub_fields), ...]

        wire_type: 0=varint, 2=length-delimited(bytes/嵌套消息), 1=64bit, 5=32bit
        嵌套消息会递归解析并填入 sub_fields。
        """
        fields = []
        i = 0
        while i < len(data):
            tag = 0
            shift = 0
            while i < len(data):
                b = data[i]
                i += 1
                tag |= (b & 0x7f) << shift
                shift += 7
                if not (b & 0x80):
                    break
            fn = tag >> 3
            wt = tag & 0x07

            if wt == 0:  # varint
                val = 0
                sh = 0
                while i < len(data):
                    b = data[i]
                    i += 1
                    val |= (b & 0x7f) << sh
                    sh += 7
                    if not (b & 0x80):
                        break
                fields.append((fn, wt, val, None))

            elif wt == 2:  # length-delimited
                length = 0
                sh = 0
                while i < len(data):
                    b = data[i]
                    i += 1
                    length |= (b & 0x7f) << sh
                    sh += 7
                    if not (b & 0x80):
                        break
                chunk = data[i:i + length]
                i += length
                # 尝试作为嵌套消息递归解析
                sub = None
                if depth < 4 and len(chunk) >= 2:
                    try:
                        sub = DouyinLiveWebFetcher._parse_protobuf_fields(chunk, depth + 1)
                    except Exception:
                        sub = None
                fields.append((fn, wt, chunk, sub))

            elif wt == 1:
                i += 8
                fields.append((fn, wt, None, None))
            elif wt == 5:
                i += 4
                fields.append((fn, wt, None, None))
            else:
                break
        return fields

    def _parseEmojiChatMsg(self, payload):
        '''聊天表情包消息'''
        message = EmojiChatMessage().parse(payload)
        emoji_id = message.emoji_id
        user = message.user
        common = message.common
        default_content = message.default_content
        self.log("EMOJI", f"表情包ID: {emoji_id}, 用户: {user}, 内容: {default_content}")

    def _parseRoomMsg(self, payload):
        message = RoomMessage().parse(payload)
        common = message.common
        room_id = common.room_id
        self.log("ROOM", f"直播间ID: {room_id}")

    def _parseRoomStatsMsg(self, payload):
        message = RoomStatsMessage().parse(payload)
        display_long = message.display_long
        self.log("STATS", display_long)

    def _parseRankMsg(self, payload):
        message = RoomRankMessage().parse(payload)
        ranks_list = message.ranks_list
        # ⚠️ 这里**不要**打印 `ranks_list` 本体：每个 rank 都内嵌 avatar_thumb/
        # avatar_medium/avatar_large/pay_grade... 数十个嵌套 Image，`repr` 一次就是
        # 十几 KB 纯文本，而它在**调用点就求值**（无论有没有 log_callback，见 log()），
        # 于是每条 RoomRankMessage 都白造一份巨大字符串。100 并发下这是实打实的 CPU
        # 与 stdout 锁竞争开销。只留有界摘要 —— RANK 本来也不落盘（只走 log）。
        heads = ', '.join(u.nick_name for u in
                          (r.user for r in ranks_list[:5]) if u is not None)
        self.log("RANK", f"榜单 {len(ranks_list)} 人"
                         + (f"，前 5：{heads}" if heads else ""))

    def _parseControlMsg(self, payload):
        '''直播间状态消息'''
        message = ControlMessage().parse(payload)
        if message.status == 3:
            self.log("STATUS", "直播间已结束")
            if self.on_offline:
                self.on_offline()
            self.stop()

    def _parseRoomStreamAdaptationMsg(self, payload):
        message = RoomStreamAdaptationMessage().parse(payload)
        adaptationType = message.adaptation_type
        self.log('ADAPTATION', f'直播间adaptation: {adaptationType}')


class DouyinLiveApp:
    def __init__(self, root):
        self.root = root
        self.root.title("抖音直播间监控工具")
        self.root.geometry("1200x800")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 创建日志类型字典
        self.log_types = {
            "CHAT": "聊天消息",
            "GIFT": "礼物消息",
            "LIKE": "点赞消息",
            "ENTER": "进场消息",
            "FOLLOW": "关注消息",
            "STATS": "统计信息",
            "FANSCLUB": "粉丝团消息",
            "EMOJI": "表情消息",
            "ROOM": "房间信息",
            "RANK": "用户数据信息",
            "ADAPTATION": "流配置",
            "STATUS": "房间状态",
            "WEBSOCKET": "连接状态",
            "HEARTBEAT": "心跳检测",
            "ERROR": "错误信息",
            "WARN": "警告信息"
        }

        # 创建UI
        self.create_widgets()

        # 直播监控器实例
        self.fetcher = None
        self.live_id = ""
        self.anchor_id = ""

    def create_widgets(self):
        # 创建顶部控制面板
        control_frame = ttk.Frame(self.root, padding="10")
        control_frame.grid(row=0, column=0, columnspan=3, sticky="ew")

        # 直播间ID输入
        ttk.Label(control_frame, text="直播间ID:").grid(row=0, column=0, padx=5, sticky="w")
        self.live_id_entry = ttk.Entry(control_frame, width=30)
        self.live_id_entry.grid(row=0, column=1, padx=5)

        # 主播ID输入
        ttk.Label(control_frame, text="主播ID:").grid(row=0, column=2, padx=5, sticky="w")
        self.anchor_id_entry = ttk.Entry(control_frame, width=30)
        self.anchor_id_entry.grid(row=0, column=3, padx=5)

        # 按钮区域
        button_frame = ttk.Frame(control_frame)
        button_frame.grid(row=0, column=4, padx=10)

        ttk.Button(button_frame, text="获取直播间状态", command=self.get_status).grid(row=0, column=0, padx=5)
        ttk.Button(button_frame, text="获取用户数据", command=self.get_ranklist).grid(row=0, column=1, padx=5)
        ttk.Button(button_frame, text="开始直播间数据监控", command=self.start_monitor).grid(row=0, column=2, padx=5)
        ttk.Button(button_frame, text="停止监控", command=self.stop_monitor).grid(row=0, column=3, padx=5)
        ttk.Button(button_frame, text="清空日志", command=self.clear_logs).grid(row=0, column=4, padx=5)

        # 创建4x3网格的日志框
        self.log_frames = {}
        self.log_texts = {}

        # 定义日志框的位置和类型
        log_positions = [
            (1, 0, "CHAT"),  # 聊天消息
            (1, 1, "GIFT"),  # 礼物消息
            (1, 2, "ENTER"),  # 进场消息
            (2, 0, "LIKE"),  # 点赞消息
            (2, 1, "FOLLOW"),  # 关注消息
            (2, 2, "FANSCLUB"),  # 粉丝团消息
            (3, 0, "STATS"),  # 统计信息
            (3, 1, "STATUS"),  # 房间状态
            (3, 2, "RANK"),  # 用户数据信息
            (4, 0, "ROOM"),  # 房间信息
            (4, 1, "ADAPTATION"),  # 流配置
            (4, 2, "ERROR")  # 错误信息
        ]

        for row, col, log_type in log_positions:
            frame = ttk.LabelFrame(self.root, text=self.log_types[log_type])
            frame.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")

            # 创建带滚动条的文本框
            text_area = scrolledtext.ScrolledText(
                frame,
                wrap=tk.WORD,
                width=40,
                height=10,
                state='disabled'
            )
            text_area.pack(fill="both", expand=True)

            self.log_frames[log_type] = frame
            self.log_texts[log_type] = text_area

        # 配置网格行列权重
        for i in range(1, 5):
            self.root.rowconfigure(i, weight=1)
        for i in range(3):
            self.root.columnconfigure(i, weight=1)

    def log_message(self, log_type, message):
        """记录日志到对应的文本框"""
        if log_type in self.log_texts:
            text_area = self.log_texts[log_type]
            text_area.config(state='normal')
            text_area.insert(tk.END, message + "\n")
            text_area.see(tk.END)  # 滚动到底部
            text_area.config(state='disabled')

    def get_status(self):
        """获取直播间状态"""
        self.live_id = self.live_id_entry.get().strip()
        if not self.live_id:
            messagebox.showerror("错误", "请输入直播间ID")
            return

        if not self.fetcher or self.fetcher.live_id != self.live_id:
            self.fetcher = DouyinLiveWebFetcher(self.live_id, self.log_message)

        success, status, nickname, user_id = self.fetcher.get_room_status()
        if success:
            messagebox.showinfo("直播间状态", f"主播: {nickname}\nID: {user_id}\n状态: {status}")
        else:
            messagebox.showerror("错误", "无法获取直播间状态")

    def get_ranklist(self):
        """获取观众用户数据"""
        self.live_id = self.live_id_entry.get().strip()
        self.anchor_id = self.anchor_id_entry.get().strip()

        if not self.live_id:
            messagebox.showerror("错误", "请输入直播间ID")
            return

        if not self.anchor_id:
            messagebox.showerror("错误", "请输入主播ID")
            return

        if not self.fetcher or self.fetcher.live_id != self.live_id:
            self.fetcher = DouyinLiveWebFetcher(self.live_id, self.log_message)

        accounts = self.fetcher.get_audience_ranklist(self.anchor_id)

        # 显示用户数据结果
        if accounts:
            rank_window = tk.Toplevel(self.root)
            rank_window.title("直播间观众用户数据")
            rank_window.geometry("600x400")

            # 创建树形视图
            tree = ttk.Treeview(rank_window, columns=("ID", "昵称", "抖音号"), show="headings")
            tree.heading("ID", text="ID")
            tree.heading("昵称", text="昵称")
            tree.heading("抖音号", text="抖音号")

            tree.column("ID", width=100)
            tree.column("昵称", width=200)
            tree.column("抖音号", width=200)

            # 添加滚动条
            scrollbar = ttk.Scrollbar(rank_window, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scrollbar.set)

            scrollbar.pack(side="right", fill="y")
            tree.pack(fill="both", expand=True)

            # 添加数据
            for i, account in enumerate(accounts, 1):
                tree.insert("", "end", values=(account['id'], account['nickname'], account['display_id']))
        else:
            messagebox.showinfo("提示", "未获取到观众用户数据数据")

    def start_monitor(self):
        """开始监控直播间"""
        self.live_id = self.live_id_entry.get().strip()
        if not self.live_id:
            messagebox.showerror("错误", "请输入直播间ID")
            return

        # 检查是否已经有监控器在运行
        if self.fetcher and self.fetcher.running:
            messagebox.showinfo("提示", "监控已在运行中")
            return

        # 创建或更新监控器
        if not self.fetcher or self.fetcher.live_id != self.live_id:
            self.fetcher = DouyinLiveWebFetcher(self.live_id, self.log_message)

        # 先获取房间状态
        success, status, nickname, user_id = self.fetcher.get_room_status()
        if not success:
            messagebox.showerror("错误", "无法获取直播间状态，监控无法启动")
            return

        if status != "正在直播":
            if not messagebox.askyesno("确认", "直播间当前未开播，是否继续监控？"):
                return

        # 启动监控线程
        monitor_thread = threading.Thread(target=self.fetcher.start)
        monitor_thread.daemon = True
        monitor_thread.start()

        self.log_message("STATUS", "直播间监控已启动...")

    def stop_monitor(self):
        """停止监控直播间"""
        if self.fetcher:
            self.fetcher.stop()
            self.log_message("STATUS", "直播间监控已停止")

    def clear_logs(self):
        """清空所有日志"""
        for text_area in self.log_texts.values():
            text_area.config(state='normal')
            text_area.delete(1.0, tk.END)
            text_area.config(state='disabled')
        self.log_message("STATUS", "所有日志已清空")

    def on_closing(self):
        """关闭窗口时的处理"""
        if self.fetcher and self.fetcher.running:
            if messagebox.askokcancel("退出", "监控正在运行，确定要退出吗？"):
                self.fetcher.stop()
                self.root.destroy()
        else:
            self.root.destroy()