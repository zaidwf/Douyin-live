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


# ---------------------------------------------------------------------------
# 代理（**只作用于控制面**：4 处 HTTP + WebSocket）
#
# 为什么只给控制面：444 风控打在「直播页 HTML / enter / ranklist」这些端点上
# （DEPLOY.md 第七节实测：数据中心 IP 15 主播 @10s 就有 88 次 444）。而 FLV 是 CDN 的
# 另一套域名，通常不受同一套风控；且一路媒体流 ~6.2 Mbps，5 路 ≈ 31 Mbps，全程穿隧道
# 既计费又可能撞隧道带宽上限 —— 没必要就不走。
#
# 若实测发现 CDN 也被 IP 拦截（现象：控制面正常、但 `MediaCapture` 起不来 ffmpeg），
# 单独设 `DOUYIN_STREAM_PROXY` 给 ffmpeg 开（见 `MediaCapture._proxy_args`），
# **不要**顺手把控制面这份也改掉。
#
# 配置优先级：DOUYIN_PROXY > http_proxy / HTTP_PROXY > 无（无 = 直连，行为与改动前一致）
#
# ★ 2026-10-16：**WebSocket 默认不再走隧道**（单独开关见 `WS_PROXY_ENV`）。
# 上面那句「444 也打在 WS 建连上」是 2026-10-15 的结论，但那天**所有**控制面流量都在
# 隧道里，把「隧道掐长连接」误当成了「WS 被 444」。三组对照实测（同一台服务器、同一
# 时刻、同一批在播主播，唯一变量是走不走隧道）推翻了它：
#
#   | 组     | 条数 | 结果                                              |
#   |--------|------|---------------------------------------------------|
#   | 走隧道 | 2    | 389.1s / 456.5s **双双被掐**（`Connection to     |
#   |        |      | remote host was lost.`，死前还在收帧 702/870）    |
#   | 直连   | 2    | 1051s **仍活着**                                   |
#   | 直连   | 10   | **9 条活满 1200s 上限、零 ERROR**；第 10 条   |
#   |        |      | 574.6s **正常下播**（`offline`，不是被掐）    |
#   | 直连   | 2    | 活满 900s 上限，2231/2310 帧，**零 ERROR**         |
#
# 同期生产（走隧道）70 分钟里 108 次 `WebSocket错误`，而 14 条直连 WS **一次 444 都没有**。
# 隧道在 ~6.5 分钟处掐断长连接、每条各自死（85 条 `ws_closed` 里同秒死亡 **0** 次、
# ≤2 秒成批 **0** 组；并发 10 路与 1–2 路的中位存活同为 ~375s）。
#
# 后果不只是「少采一点」，而是**一场真实直播被切成多个「完整」碎片场次**
# （实测同一主播 30 分钟内 4 个场次目录）—— 台账里的「完整」是碎片完整。
#
# 所以 WS 走直连、HTTP 仍走隧道：怕 444 的是页面/enter/ranklist 这些**短请求**
# （直连实测确实会 444，那才是当初加代理的原因），而**长连接**恰恰是隧道唯一搞不定的东西。
# 实测直连的短请求也没被 444 打 —— 但那是隧道在替它挡，所以**不动** `proxy_for_requests()`。
#
# 要退回「WS 也走隧道」（隧道换供应商后想 A/B）：设 `DOUYIN_WS_PROXY=1`，不用改代码。
# ---------------------------------------------------------------------------
PROXY_ENV_KEYS = ("DOUYIN_PROXY", "http_proxy", "HTTP_PROXY")

# WS 是否走隧道。**默认关**（= 直连），设成非空且不是下面这些假值才开。
WS_PROXY_ENV = "DOUYIN_WS_PROXY"
_WS_PROXY_OFF = ("", "0", "no", "off", "false")


def parse_proxy(url):
    """把 `http://<用户>:<密码>@<主机>:<端口>` 拆成各处能直接用的形状；空/None 返回 None

    :return: `{"url", "host", "port", "auth"}`，`auth` 是 `(user, pass)` 或 None
    :raises ValueError: 非 http/https，或没有主机名（**明确报错，不静默降级成直连**）

    ⚠️ 只支持 http/https。socks5 需要 PySocks，且 websocket-client 走的是
    `proxy_type='socks5'` 另一条分支 —— 本项目当前用不到，宁可报错也不静默走错。
    """
    if not url or not str(url).strip():
        return None
    u = str(url).strip()
    if "://" not in u:
        u = "http://" + u          # 容忍省略 scheme 的 `host:port` / `user:pass@host:port`
    sp = urllib.parse.urlsplit(u)
    if sp.scheme not in ("http", "https"):
        raise ValueError(f"只支持 http/https 代理，收到 scheme={sp.scheme!r}（{url!r}）")
    if not sp.hostname:
        raise ValueError(f"代理地址里没有主机名: {url!r}")
    # 用户名/密码要 unquote：凭据里出现 %xx 是常态（本次这个恰好是纯 hex，但不能假设）
    auth = None
    if sp.username is not None:
        auth = (urllib.parse.unquote(sp.username),
                urllib.parse.unquote(sp.password or ""))
    return {"url": u, "host": sp.hostname,
            "port": sp.port or (443 if sp.scheme == "https" else 80),
            "auth": auth}


_PROXY_CFG = None         # 进程内解析一次
_PROXY_RESOLVED = False   # 测试钩子：置回 False 可让下次调用重新读环境


def _proxy_cfg():
    """惰性解析一次并缓存 —— 代理是进程级配置，中途不会变，不必每请求读环境"""
    global _PROXY_CFG, _PROXY_RESOLVED
    if not _PROXY_RESOLVED:
        url = None
        for k in PROXY_ENV_KEYS:
            v = os.environ.get(k)
            if v and v.strip():
                url = v.strip()
                break
        _PROXY_CFG = parse_proxy(url)
        _PROXY_RESOLVED = True
    return _PROXY_CFG


def proxy_for_requests():
    """`requests.get(..., proxies=proxy_for_requests())` 的实参

    未配置时返回 **空 dict**（= 确定直连）。⚠️ 不能返回 `None` —— `None` 会让 requests
    回落到自己读 `http_proxy` 环境变量，而那几个变量上面已经读过一遍了；传 `{}` 才能让
    「走不走代理」只由 `_proxy_cfg()` 这一处决定，行为可复现。
    """
    c = _proxy_cfg()
    return {"http": c["url"], "https": c["url"]} if c else {}


def ws_proxy_enabled():
    """WS 要不要走隧道。**默认 False**（= 直连），见 `WS_PROXY_ENV` 上面那段实测

    读环境**不缓存**：`ws_proxy_enabled` 决定的是「新建的连接往哪走」，
    进程活着的这几小时里隧道可能被换掉/掐死，让运维能改 env 后立刻生效
    （`proxy_for_ws()` 每条连接才调一次，这点开销可忽略）。
    """
    v = (os.environ.get(WS_PROXY_ENV) or "").strip().lower()
    return v not in _WS_PROXY_OFF


def proxy_for_ws():
    """`self.ws.run_forever(**proxy_for_ws())` 的实参

    ★ **默认返回 `{}`（直连），即使配了 `DOUYIN_PROXY`** —— 隧道会掐长连接，
    见 `WS_PROXY_ENV` 上面那段的实测。要恢复「WS 也走隧道」设 `DOUYIN_WS_PROXY=1`。

    ⚠️ 真要传代理参数时，websocket-client **不认** `http_proxy` 环境变量
    （requests 认、它不认），必须显式传这些参数，否则 WS 会静默绕过代理直连。

    ⚠️ **`proxy_type` 必须显式给**，否则必报
    `Only http, socks4, socks5 proxy protocols are supported`（实测踩过）：
    `run_forever` 的默认签名是 `proxy_type=None`，而它把该值**原样**塞进 options，
    下游 `proxy_info` 用的是 `options.get("proxy_type", "http")` —— key 存在但值为 None
    时 `.get` 的默认值**不生效**，于是 `None not in [http, socks4, ...]` 直接抛错。
    传 `"http"` 才是那个「默认值」真正生效的写法。
    """
    c = _proxy_cfg()
    if not c or not ws_proxy_enabled():
        return {}
    return {"http_proxy_host": c["host"], "http_proxy_port": c["port"],
            "http_proxy_auth": c["auth"], "proxy_type": "http"}


RISK_444_TAG = '[风控444]'          # 稳定标记，打在所有被 444 的日志行上
_444_LEGACY = '444 Client Error'    # requests 的报错措辞（旧口径，仅用于兼容历史日志）


def is_risk_control_444(text):
    """该日志行/消息是否是抖音的 **HTTP 444 风控命中**

    ⚠️ **不要退回子串匹配 `"444" in text`**。实测（2026-10-15）：那样会把 user_id、
    时间戳里恰好含 `444` 的行全算成风控命中 —— 一个干净窗口里 naive 口径数出 **50** 行，
    而真 444 只有 **0** 行，凭空造出一个「≈12% 残余风控」的假结论。

    两个来源都认：
      1. `RISK_444_TAG` —— 本模块按 `err.response.status_code == 444` 打的标记（**首选**，
         不依赖 requests 的英文措辞，语言/版本/时区都改不动它）
      2. `_444_LEGACY` —— 历史日志里 requests 抛出的原文，用于回溯旧数据
    """
    if RISK_444_TAG in text:
        return True
    return _444_LEGACY in text


def err_text(err):
    """把请求异常渲染成日志文本；**是 444 就附上稳定标记**

    按 `status_code` 判、不按文本判：444 在 HTTP 里没有 reason phrase，requests 会拼成
    `444 Client Error:  for url: …`（中间两个空格，出自空 phrase）—— 依赖这个措辞等于把
    「风控统计」绑死在 requests 的英文格式上。按状态码判，语言/版本/时区都改不动它。
    """
    if getattr(getattr(err, 'response', None), 'status_code', None) == 444:
        return f"{err} {RISK_444_TAG}"
    return str(err)


def stream_proxy_url():
    """ffmpeg 拉 FLV 用的代理 URL；默认 None = **直连**（理由见上方注释块）

    刻意不与控制面共用 `DOUYIN_PROXY`：两者该不该走代理是**独立**的问题，
    绑在一起会导致「为了修 CDN 而把控制面也塞进隧道」这类误操作。
    """
    v = os.environ.get("DOUYIN_STREAM_PROXY")
    return v.strip() if v and v.strip() else None


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


def extract_stream_urls_from_enter(data):
    """从 `_enter_api()` 的返回值解析 flv_pull_url（多画质 FLV 流）

    与 `extract_stream_urls_from_html` 是**同一条数据的两个来源**：结构都是
    `stream_url.flv_pull_url`（字典，键 FULL_HD1/HD1/SD1/SD2），都按 `flv.values()`
    取值，所以**画质顺序一致**，`MediaCapture.url_index` 在两条路径上语义相同。

    存在的理由（2026-10-14 实测）：直播间**页面端点**（`https://live.douyin.com/<id>`）
    在密集请求后会下发**验证码中间页** —— HTTP 200、6297 字节、含
    `sec_sdk_build/3.5.2/captcha/index.js`，**没有** `flv_pull_url`/`roomId`。
    实测是 **IP 级**限制（换新 ttwid 无效、所有房间同时中招），且**只针对页面端点**：
    enter 接口与 WS 全程正常。此时页面路径返回 `[]`，音视频采集直接归零（生产实测
    `collect_full.py` 图像 0 帧 / 音频 0 段）。此函数即该情况下的回退来源。

    :param data: `_enter_api()` 的返回值（响应里的 `data` 对象）
    :return: 流地址列表（多个画质，顺序同 HTML 路径），解析不到返回 []
    """
    try:
        rooms = (data or {}).get('data') or []
        flv = ((rooms[0].get('stream_url') or {}).get('flv_pull_url') or {})
        return [u for u in flv.values() if u]
    except Exception:
        return []


# 长会话看护：FLV 流地址会过期，检查间隔与最多续采次数
MEDIA_WATCHDOG_INTERVAL = 30
MEDIA_MAX_RESTARTS = 20

# **停滞判据**（2026-10-17 实测）：长会话在**整 8 小时**处 CDN 停推 —— 三场长直播的
# 帧数都恰好 **5760**（= 480 分钟 × 12 帧/分）、音频段 960（= 480 × 2），最后一帧都在
# 开场后 **8h00m05s / 06s / 07s**；而 ffmpeg 进程**仍存活**（CPU 仍占 1.4–1.7%），
# 只是再也不写文件。原判据只看 `poll() is not None`（进程退出），这种「活着但不出数」
# **永远发现不了** —— 既不会续采，也没有任何日志。
#
# 阈值取 300s：健康会话的帧每 5s 一个、音频段每 30s 一段，300s 是**两个数量级**的余量
# （与 WS 僵死的 300s 同一口径）；而 8 小时处断流每多等一分钟就永久缺一分钟媒体。
MEDIA_STALL_SECONDS = 300


def media_output_progress(count, prev_count, prev_ts, now, threshold):
    """某一模态本轮的产物计数 → `(是否停滞, 新的基线时间戳)`

    `count != prev_count` 说明有新产物，基线时间戳前移到 `now`；否则距上次变化
    ≥ `threshold` 秒判停滞（时间戳**保持不动**，于是日志里的停摆时长会持续增长）。

    计数用**非空文件个数**而不是 mtime：`frame_*.jpg` 每 5 秒一个新文件、
    `segment_*.wav` 每 30 秒一段，个数只会单调增加；mtime 会被正在写的那个文件的
    系统缓冲影响。抽成模块级纯函数是为了**可单测**（同 `next_reconnect_state`）。
    """
    if count != prev_count:
        return False, now
    return (now - prev_ts) >= threshold, prev_ts


# ── WS 断线自动重连（2026-10-16，用在 `DouyinLiveWebFetcher.start`）──────────
#
# 为什么要有它：WS 一断，`auto_crawl` 就把本场收尾、60 秒后**另开一个场次目录** ——
# 于是一场真实直播被切成多个「完整」碎片场次（实测同一主播 30 分钟内 4 个），
# 台账标的「完整」变成**碎片完整**，任何按场次聚合的研究特征（时长、峰值人气、
# 整场弹幕量、留存）都会在碎片边界上被切断。重连让「一场直播 = 一个场次目录」
# 重新成立，断线只丢几秒数据而不是把一场变成两场。
#
# ★ **默认不重连**（`start(max_reconnects=0)`）：`collect_full.py` / GUI /
#   `load_test.py` / `run_ladder.py` 都依赖「WS 线程死了 = 这场完了」的语义，
#   改了会把压测的失败计数掩盖掉（`load_test` 更是拿 `f.running = False` 当**唯一**
#   的停止手段，重连会把它吞掉）。只有 `auto_crawl`（生产守护）显式打开。
RECONNECT_WAIT = 3.0            # 每次重连前的退避等待（秒）
RECONNECT_GRACE = 15.0          # 连接活过这么久 = 「真连上并采到数据了」，连续失败计数清零
HEARTBEAT_JOIN_TIMEOUT = 6.0    # ★ 必须 > `_sendHeartbeat` 的 time.sleep(5)，否则 join 恒超时


def next_reconnect_state(lived_s, fails, max_reconnects, grace=RECONNECT_GRACE):
    """一条 WS 连接结束后该不该再连一条 → `(是否重连, 新的连续失败计数)`

    抽成模块级纯函数是为了**可单测**（同 `auto_crawl._stalled` 的写法）—— 循环本身要喂
    「连了多久、连了几次」这些时间相关的状态，内联在 `start()` 里没法测。

    口径是**连续**失败而不是累计：`lived_s >= grace` 说明这条连接真采到数据了，额度
    **恢复**（fails 清零）。于是「每 10 分钟断一次」的长直播能一直重连下去，而「一连就
    挂」才会计数耗尽 —— 后者通常意味着房间已下播 / 被风控，重试本来也没用。

    `max_reconnects=2` 的行为（每次都是秒断）：重连 2 次后放弃；若中途有一条连接活过
    `grace`，计数清零、重新给满 2 次。
    """
    if max_reconnects <= 0:
        return False, fails          # 不重连 = 改动前行为
    fails = 0 if lived_s >= grace else fails + 1
    return fails <= max_reconnects, fails


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
        **抽帧间隔自 2026-10-14 起由 10s 改为 5s**（`frame_interval=5`）。
        ⚠️ **本仓库 2026-10-14 之前采集的场次都是 10s 一帧**，而 `room.json`
        **不记录抽帧间隔** —— 新旧语料混用时只能靠「帧数 ÷ 场次时长」反推。
        改这个值不影响带宽与 CPU（实测：拉流与解码都不依赖出帧频率，
        `fps` 滤镜只决定解码完的帧写不写盘），只影响帧数量与磁盘。

    ⚠️ **并发成本（2026-10-14 实测，非估算）**：

    默认路径是**合并单进程 + `-skip_frame nokey`**（2026-10-14 起），一路 = 1 个 ffmpeg：
    **0.023 核 CPU / 125 MB RSS / 6.2 Mbps 带宽**。

    三个杠杆的实测依据：
      - **合并单进程**（`merged=True`）：`-vn` **不省带宽** —— 音频进程仍要读完整条复用
        FLV，实测音频档与视频档带宽完全相同（各 6.2 Mbps）。拆两个进程 = 把同一条流
        **读两遍**，一路占 `2 × 流码率`。合并后只读一遍 → 带宽 ÷2、进程 ÷2、RSS ÷2。
      - **`-skip_frame nokey`**（放 `-i` 之前）：只解关键帧，视频 CPU `0.161 → 0.013` 核、
        RSS `209 → 116 MB`，**产出帧数不变**。实测抖音流 **GOP = 2.0s**，密于 5s 抽帧
        间隔，故是「挑帧」不是「补帧」，不会写出重复 JPG（副作用：画面最多偏 ±1s）。
      - **抽帧频率/音频段长不是杠杆**：fps=1/10 与 fps=1/300 的带宽与 CPU **完全相同**
        （0.79 MB/s / 0.166 核）—— CPU 在**解码**上，`fps` 滤镜只决定写不写盘。

    瓶颈**不是 CPU 而是带宽与内存**：合并后按 0.023 核/路，16 逻辑核能带几十路；但一路
    仍要 6.2 Mbps，链路吞吐 ÷ 6.2 Mbps 才是真实路数。实测本机 Wi-Fi（标称 144 Mbps）
    真实可用吞吐仅 ~4 MB/s ≈ 32 Mbps。**2 核 2G 服务器**：内存 1710 MB ÷ 127 ≈ 13 路、
    CPU ≈ 11 路、100 Mbps 口 ≈ 16 路 → **可持续 ≈ 11-13 路**（优化前约 5 路）。
    磁盘不是约束（5s/帧口径下：WAV 32 KB/s + 帧 ~64 KB/s ≈ 0.10 MB/s/路）。
    故 `auto_crawl` 侧仍是显式开关且**默认关**，`--media` 只适合小规模验证。
    完整阶梯与标定见 `modify.md` 的 2026-10-14 条目。
    """

    def __init__(self, live_id, out_dir, *, fetcher=None, url_index=0, want_audio=True,
                 want_frames=True, audio_segment=30, frame_interval=5, merged=True,
                 max_seconds=None, media_stall=MEDIA_STALL_SECONDS, stream_proxy=None,
                 log=print):
        """
        :param fetcher: `DouyinLiveWebFetcher` 实例，用于复用其 ttwid/cookie 与页面请求
        :param out_dir: 落盘目录（传 `DataRecorder.path`），内部再建 `audio/` `frames/`
        :param url_index: 取第几个画质档的流地址（`flv_pull_url` 有 4 档：uhd/hd/ld/sd，
            实测 1080×1920@45fps 6Mbps）。**默认 0 = 最高画质**，即生产口径。
            只给压测用：解码成本由分辨率主导，这是不改口径就能验证的「上限杠杆」——
            用来量化「最高画质 vs 低档」差多少路。越界自动回退 0。
        :param frame_interval: 抽帧间隔（秒）。**5 = 生产口径**（2026-10-14 起，
            此前是 10）。只影响帧数量与磁盘，不影响带宽/CPU（见类 docstring）。
        :param merged: 音视频是否合并到**一个** ffmpeg（默认 True）。合并省一半带宽
            （一条流只读一遍）、一半 RSS、一半进程数，代价是两模态故障域耦合。
            仅在同时要音频与抽帧时生效；只要一路时自动退化。要 A/B 对比时传 False。
        :param max_seconds: **防僵尸安全上限**（`-t`），不是主停止手段，也**不是本场
            时长**：到点 ffmpeg 正常退出后由 `_watchdog` 自动续采；None 表示不加
        :param media_stall: **停滞判据**的阈值（秒）：进程还活着、但该模态的产物计数
            这么久一次没涨 → 强杀并重解析流地址续采。`0` = 关闭（回到只看进程退出的
            旧行为）。默认 300（见 `MEDIA_STALL_SECONDS` 的实测依据）。
        :param stream_proxy: ffmpeg 拉流用的代理 URL。**默认 None = 跟随环境变量**
            `DOUYIN_STREAM_PROXY`（不设就是直连 = 生产口径，与改动前逐字节等价）；
            传空串 `''` 可强制直连。与控制面的 `DOUYIN_PROXY` **刻意分开**——
            444 打在控制面端点，FLV CDN 是另一套域名，不该被顺带塞进隧道
            （一路 6.2 Mbps，5 路 ≈ 31 Mbps，走隧道既计费又可能撞带宽上限）。
        """
        self.live_id = live_id
        self.out_dir = str(out_dir)
        self.fetcher = fetcher
        self.url_index = url_index
        self.want_audio = want_audio
        self.want_frames = want_frames
        self.audio_segment = audio_segment
        self.frame_interval = frame_interval
        # 只有两个模态都要时才谈得上合并（任一为 False 就没有第二个输出可合）
        self.merged = bool(merged) and want_audio and want_frames
        self.max_seconds = max_seconds
        self.media_stall = media_stall
        self.stalls = 0             # 「活着但不出数」→ 强杀续采的次数（与 restarts 分开记）
        # 模态 → (上次产物计数, 该计数最后一次变化的时间)。起/续起进程时由 `_spawn` 建基线
        self._progress = {}
        self._resolve_fails = 0     # 续采时连续解析不到流地址的次数（日志限流用）
        # None 表示「跟随 DOUYIN_STREAM_PROXY」，空串表示「强制直连」——用 is None 区分
        self.stream_proxy = stream_proxy_url() if stream_proxy is None else (stream_proxy or None)
        self.log = log
        self.audio_dir = os.path.join(self.out_dir, 'audio')
        self.frames_dir = os.path.join(self.out_dir, 'frames')
        # ffmpeg 的 stderr 落到文件而不是 DEVNULL：`-loglevel error` 平时几乎无输出，
        # 但长会话续采排查（`_watchdog` 为什么重启）全靠这里。也不能用 PIPE ——
        # 几小时跑下来不排空会写满管道缓冲把 ffmpeg 卡死。
        self.audio_log = os.path.join(self.audio_dir, 'ffmpeg.log')
        self.frame_log = os.path.join(self.frames_dir, 'ffmpeg.log')
        self.merged_log = os.path.join(self.out_dir, 'ffmpeg_media.log')
        self.stream_url = None
        self.audio_proc = None
        self.frame_proc = None
        self.restarts = 0
        self._stop_event = threading.Event()
        self._watchdog_thread = None
        self._t0 = None

    # ---------------- 流地址 ----------------

    def _resolve_stream_url(self):
        """取当前可用流地址；**不需要浏览器**（见 extract_stream_urls_from_html）

        两条来源，页面优先、enter 兜底：
          1. 直播页 HTML —— 原路径，正常情况下够用
          2. enter 接口 —— 页面端点被下发**验证码中间页**时（HTTP 200 但无 flv_pull_url，
             IP 级、可长时间持续，实测会让音视频采集整体归零），退回这条
        两条来源的 flv_pull_url 结构与画质顺序一致，所以 url_index 语义不变。
        """
        if self.fetcher is None:
            self.log('[media] 未提供 fetcher，无法解析流地址')
            return None
        urls = extract_stream_urls_from_html(self.fetcher.fetch_live_html() or '')
        if not urls:
            # 页面拿不到（最常见原因：验证码中间页）→ 退到 enter 接口
            try:
                data = self.fetcher._enter_api('')
            except Exception as e:
                data = None
                self.log("ERROR", "[media] enter 接口回退失败: %s" % e)
            urls = extract_stream_urls_from_enter(data)
            if urls:
                self.log('[media] 页面未取到流地址（疑验证码中间页），已用 enter 接口回退')
        if not urls:
            return None
        # url_index 越界（如某房间只有 1 档）回退 0，不因压测参数把生产路径弄挂
        return urls[self.url_index] if self.url_index < len(urls) else urls[0]

    # ---------------- 命令构造 ----------------

    def _time_cap(self):
        """`-t` 参数（防僵尸上限）。注意**不是**主停止手段，主停止是 stop_media()

        ⚠️ 到点 ffmpeg 会**正常退出**，但**这也不是本场结束**：`_watchdog` 会重解析
        地址、带 `-start_number` 续采（2026-10-18 起；此前 watchdog 自己也到点 `return`，
        于是健康流的媒体就静默断在 8 小时处 —— 见 `_watchdog` 的说明）。

        ⚠️ 调用方必须把它放在 `-i` **之前**。放在 `-i` 之后它是**输出选项**，只作用于
        紧接着的那个输出 —— 合并模式（一条命令两个输出）下第二个输出就完全没有上限了
        （实测踩过：探针里视频输出不受 `-t` 约束，靠发 `q` 才停）。
        """
        return ["-t", str(int(self.max_seconds))] if self.max_seconds else []

    def _proxy_args(self):
        """ffmpeg 的 `-http_proxy` 参数（**默认空**，即生产口径直连）

        ⚠️ 与 `-t` / `-skip_frame` 一样是**输入前置选项**，必须放在 `-i` **之前**。
        放在 `-i` 之后会变成输出选项，对已建好的连接无效（`_time_cap` 踩过同款坑）。
        """
        return ["-http_proxy", self.stream_proxy] if self.stream_proxy else []

    def _audio_args(self, start_number=0):
        """音频输出段（选项 + 输出路径），供单路与合并两种命令共用。不含 `-map`"""
        return ["-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
                "-f", "segment", "-segment_time", str(self.audio_segment),
                "-segment_start_number", str(start_number),
                "-reset_timestamps", "1",
                os.path.join(self.audio_dir, "segment_%03d.wav")]

    def _frame_args(self, start_number=0):
        """抽帧输出段（选项 + 输出路径），供单路与合并两种命令共用。不含 `-map`

        `-vf fps=1/interval`：ffmpeg 直解 FLV 抽帧，**原始分辨率、无 scale**（口径勿改）。
        """
        return ["-vf", f"fps={1.0 / self.frame_interval}", "-q:v", "2",
                "-start_number", str(start_number),
                os.path.join(self.frames_dir, "frame_%04d.jpg")]

    def _audio_cmd(self, url, start_number=0):
        # **刻意不加 `-nostdin`**：停采时要往 stdin 写 `q` 让 ffmpeg 优雅退出并给
        # 当前分段收尾（见 stop_media）。子进程的 stdin 是 PIPE，不会抢终端输入。
        return (["ffmpeg", "-y", "-loglevel", "error"] + self._time_cap()
                + self._proxy_args() + ["-i", url] + self._audio_args(start_number))

    def _frame_cmd(self, url, start_number=0):
        return (["ffmpeg", "-y", "-loglevel", "error"] + self._time_cap()
                + self._proxy_args() + ["-skip_frame", "nokey", "-i", url]
                + self._frame_args(start_number))

    def _merged_cmd(self, url, audio_n=0, frame_n=0):
        """合并单进程：**一条 input、两组 `-map` 输出**（音频 WAV 切片 + 原始分辨率抽帧）

        为什么要合并（2026-10-14 实测）：`-vn` **不省带宽** —— 音频进程仍要读完整条
        复用 FLV，实测音频档与视频档带宽完全相同（各 6.2 Mbps）。所以拆两个进程 =
        **把同一条流读两遍**，一路占 `2 × 流码率`。合并成一条 input 后只读一遍，
        带宽 ÷2、进程数 ÷2、RSS ÷2（实测 0.791 MB/s 出齐音频+帧，与单跑视频档同带宽）。

        代价：两个模态**故障域耦合** —— 任一输出报错会整个进程退出、两路同时断。
        `_watchdog` 会续采，所以只是中断粒度变粗，不会丢数据。要拆回两进程用
        `merged=False`。

        `-skip_frame nokey`（只解关键帧）是另一个杠杆：视频 CPU `0.161 → 0.013` 核、
        RSS `209 → 116 MB`，**产出帧数不变**（`fps` 滤镜本来只写解码完的 I 帧，跳掉的
        B/P 帧从来也不写）。可用性取决于关键帧间隔 —— 实测抖音这条流 **GOP = 2.0s**，
        密于 5s 的抽帧间隔，所以是「挑帧」而非「补帧」，不会写出重复 JPG。
        ⚠️ 副作用：帧内容对齐到关键帧（2s 整数倍），5s 网格上的画面最多偏 ±1s。
        """
        cmd = ["ffmpeg", "-y", "-loglevel", "error"] + self._time_cap()
        if self.want_frames:
            cmd += ["-skip_frame", "nokey"]
        cmd += self._proxy_args()
        cmd += ["-i", url]
        # `-map` 在合并模式下**必须有**：不给的话每个输出各自做默认选流，抽帧输出会把
        # 音频流也选进去，而 jpg muxer 装不下音频（`-vf` 对音频无效）→ 直接报错退出。
        if self.want_audio:
            cmd += ["-map", "0:a"] + self._audio_args(audio_n)
        if self.want_frames:
            cmd += ["-map", "0:v"] + self._frame_args(frame_n)
        return cmd

    @staticmethod
    def _count_files(pattern):
        return len([p for p in glob.glob(pattern) if os.path.getsize(p) > 0])

    # ---------------- 起停 ----------------

    def _spawn(self):
        """起（或续接）ffmpeg 进程。

        ⚠️ 序号必须**续接**已有的产物：`segment_%03d.wav` / `frame_%04d.jpg` 若从 0
        重开，配合 `-y` 会**覆盖**上一段的产物。首次为 0，续采时按已落盘文件数接着排。
        """
        # ⚠️ 已收到停止信号就**绝不再起进程**。watchdog 的续采路径会调到这里，而
        # `stop_media()` 无法中断正在 `_resolve_stream_url()`（网络调用）里的 watchdog
        # —— 它 join 超时返回后，watchdog 仍会走到这里起一对**没人收**的 ffmpeg，
        # 孤儿进程会一直占着一条流的带宽。实测 N=20 档漏了 4 个。
        if self._stop_event.is_set():
            return
        if self.merged:
            # 合并模式：**一个进程**同时喂两个字段，判一个就够（两者恒为同一对象）
            if self.audio_proc is None:
                an = self._count_files(os.path.join(self.audio_dir, "segment_*.wav"))
                fn = self._count_files(os.path.join(self.frames_dir, "frame_*.jpg"))
                p = subprocess.Popen(
                    self._merged_cmd(self.stream_url, an, fn),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=open(self.merged_log, 'ab'))
                self.audio_proc = self.frame_proc = p
                # 停滞判据的基线：**从进程起来这一刻算起**。不建基线的话 watchdog 会拿
                # 上一轮（已强杀的那个进程）的旧时间戳去比，刚续采就判停滞、无限重启。
                now = time.time()
                self._progress['audio'] = (an, now)
                self._progress['frames'] = (fn, now)
            return
        if self.want_audio and self.audio_proc is None:
            n = self._count_files(os.path.join(self.audio_dir, "segment_*.wav"))
            self.audio_proc = subprocess.Popen(
                self._audio_cmd(self.stream_url, n),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=open(self.audio_log, 'ab'))
            self._progress['audio'] = (n, time.time())
        if self.want_frames and self.frame_proc is None:
            n = self._count_files(os.path.join(self.frames_dir, "frame_*.jpg"))
            self.frame_proc = subprocess.Popen(
                self._frame_cmd(self.stream_url, n),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=open(self.frame_log, 'ab'))
            self._progress['frames'] = (n, time.time())

    def start_media(self):
        """解析流地址并起 ffmpeg 进程。返回是否**至少起了一个**进程（失败不抛异常）"""
        self.stalls = 0
        self._progress.clear()
        self._resolve_fails = 0
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
                 f'{"合并单进程" if self.merged else "音视双进程"} '
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

    def _kill_procs(self):
        """优雅停掉当前所有 ffmpeg 并清空引用（去重）

        ⚠️ **合并模式下 `audio_proc` 与 `frame_proc` 是同一个对象**，必须按对象去重，
        否则会对同一进程走两遍停流程（第二遍因 `poll()` 非空而空转 —— 虽无害，但语义
        混乱，且掩盖「其实只有一个进程」这个事实）。这里是唯一的收停入口。
        """
        procs = []
        for p in (self.audio_proc, self.frame_proc):
            if p is not None and all(p is not q for q in procs):
                procs.append(p)
        for p in procs:
            self._graceful_stop(p)
        self.audio_proc = self.frame_proc = None

    # 小于这个字节数的产物一律当**残片**删掉。
    #
    # 为什么不是「> 0 字节」就留下（原判据）：ffmpeg 给一个**已经下播的房间**拉流时，
    # 会成功地写出**一个完整合法的文件头、但一个采样点都没有**就退出 —— 实测
    # 2026-10-15 的产物是 **78 字节**（`RIFF....WAVEfmt ` + `LIST/ISFT` + 长度 0 的
    # `data` 块），ffprobe 连时长都读不出来。旧判据只删「0 字节」，于是这个空壳被
    # 当成**一段有效音频**：日志里那些「0 帧 / 1 段音频」的场次就是这么来的，
    # 而且它会被写进 `stop_media()` 的返回值、进下游统计、进研究语料。
    #
    # 门槛取 1 KiB：78 字节的文件头远在下面；而 16kHz 单声道下 1 KiB ≈ 32 毫秒，
    # 正常封口的**最后一段**最短也有几十毫秒（实测一次 7.66s 的正常收尾），
    # 不会被误删。
    MEDIA_MIN_VALID_BYTES = 1024

    @staticmethod
    def _prune_empty(*patterns):
        """删掉残片（0 字节硬杀空壳，或小到不可能含内容的），返回存活文件"""
        alive = []
        for pattern in patterns:
            for p in glob.glob(pattern):
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size >= MediaCapture.MEDIA_MIN_VALID_BYTES:
                    alive.append(p)
                else:
                    try:
                        # 残片是纯噪声：留着会污染数据集、校验脚本与统计口径
                        os.remove(p)
                    except OSError:
                        pass
        return sorted(alive)

    def stop_media(self):
        """停所有 ffmpeg 进程并回捞产物（优雅退出 + 剔除 0 字节残片）

        :return: {"audio": [路径...], "frames": [路径...], "restarts": int, "ok": bool}
        """
        self._stop_event.set()
        self._kill_procs()
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2)
        # ⚠️ 兜底再扫一遍：上面 join 只等 2s，而 watchdog 可能正卡在
        # `_resolve_stream_url()` 的网络请求里（拥塞时数秒）→ join 超时返回，
        # 但它随后还会 _spawn()。`_spawn()` 自身已有停止守卫、watchdog 尾部也有
        # 自收，这里再扫一次是为了**不让任何一条路径漏掉**孤儿 ffmpeg。
        self._kill_procs()
        audio = self._prune_empty(os.path.join(self.audio_dir, "segment_*.wav"))
        frames = self._prune_empty(os.path.join(self.frames_dir, "frame_*.jpg"))
        return {'audio': audio, 'frames': frames, 'restarts': self.restarts,
                'stalls': self.stalls, 'ok': bool(audio or frames)}

    def stop(self):
        """`stop_media` 的别名（便于挂成 on_offline 之类的回调）"""
        return self.stop_media()

    # ---------------- 长会话看护 ----------------

    def _stalled_modalities(self, now):
        """还活着、但产物计数超过阈值没涨的模态 → `{模态: 已停摆秒数}`

        只看**进程还活着**的那些：进程退出由原来的 `poll()` 判据负责，两条判据不重叠。

        ⚠️ 阈值按模态放宽：音频的计数每 `audio_segment` 秒才涨一次、帧每
        `frame_interval` 秒一个，所以实际阈值取 `max(media_stall, 2 × 该模态间隔)` ——
        否则有人把段长调到 600s 时，正常会话也会被判成停滞。
        """
        if self.media_stall <= 0:
            return {}
        pairs = []
        if self.want_audio:
            pairs.append(('audio', self.audio_proc,
                          os.path.join(self.audio_dir, 'segment_*.wav'),
                          max(self.media_stall, 2 * self.audio_segment)))
        if self.want_frames:
            pairs.append(('frames', self.frame_proc,
                          os.path.join(self.frames_dir, 'frame_*.jpg'),
                          max(self.media_stall, 2 * self.frame_interval)))
        stalled = {}
        for kind, proc, pattern, threshold in pairs:
            if proc is None or proc.poll() is not None:
                continue                     # 已退出的交给 poll() 判据
            count = self._count_files(pattern)
            prev = self._progress.get(kind)
            if prev is None:
                self._progress[kind] = (count, now)   # 首次观测只建基线
                continue
            stopped, ts = media_output_progress(count, prev[0], prev[1], now, threshold)
            self._progress[kind] = (count, ts)
            if stopped:
                stalled[kind] = int(now - ts)
        return stalled

    def _watchdog(self):
        """两条判据，处置相同（重解析地址 + **续号**续采）：

        1. **进程退出**：流地址过期 / `-t` 到点 → ffmpeg 中途退出。
        2. **停滞**：进程**还活着**，但该模态的产物计数 `media_stall` 秒内一次没涨
           —— 2026-10-17 实测：长会话在**整 8 小时**处停推（ffmpeg 仍存活、CPU 仍占
           1.4–1.7%），但再也不写文件。只看进程退出的话这种静默停摆**永远发现不了**。

        ⚠️ **不能因为 `-t` 到点就退出本循环**（2026-10-18 实测的第二次静默断流）：
        `auto_crawl` 传 `max_seconds=8h`，ffmpeg 命令行带 `-t 28800`，**健康流**也会在
        整 8 小时处**正常退出**。原实现里有一句「`-t` 到点属于正常结束，直接 return」，
        于是 8 小时后的这次退出**没人续采**，媒体静默停在 **5760 帧 / 960 段**
        （= 480 分钟 × 12 帧/分、× 2 段/分），文本照常、所有指标看着都正常。
        当晚 3 场长直播里 2 场就这么断了；第 3 场只是**恰好**赶在守卫 return 之前的一轮
        被续采到（帧数从 5760 继续涨到 5856，ffmpeg 带 `-start_number 5760` 重拉）——
        证明**流本身没停**，是我们自己按了停止键。
        `-t` 只是**单进程防僵尸上限**，不是「本场结束」；本场结束的唯一信号是
        `_stop_event`（`stop_media()` 置位）。

        ⚠️ 停滞路径必须**先杀进程再 spawn**：`_spawn()` 见到引用非 None 会直接返回，
        不杀的话新进程根本起不来，旧进程继续占着一条流的带宽不出数。
        """
        while not self._stop_event.wait(MEDIA_WATCHDOG_INTERVAL):
            now = time.time()
            dead_audio = (self.want_audio and self.audio_proc is not None
                          and self.audio_proc.poll() is not None)
            dead_frame = (self.want_frames and self.frame_proc is not None
                          and self.frame_proc.poll() is not None)
            stalled = self._stalled_modalities(now)
            if not (dead_audio or dead_frame or stalled):
                continue
            if self.restarts >= MEDIA_MAX_RESTARTS:
                self.log(f'[media] 续采已达上限 {MEDIA_MAX_RESTARTS} 次，放弃')
                return
            url = self._resolve_stream_url()
            if not url:
                # ⚠️ 这条路径原来是**完全静默**的（只等下一轮）—— 媒体确实停了，却不留
                # 任何线索。限流打印：第 1 次 + 其后每 10 次（按 30s 间隔 ≈ 5 分钟一条）。
                self._resolve_fails += 1
                if self._resolve_fails == 1 or self._resolve_fails % 10 == 0:
                    self.log(f'[media] 续采失败：解析不到流地址'
                             f'（连续第 {self._resolve_fails} 次），稍后再试')
                continue
            self._resolve_fails = 0
            self.stream_url = url
            self.restarts += 1
            if stalled:
                # 停滞路径：进程还活着 → 必须先收掉（`_kill_procs` 按对象去重，
                # 合并模式下两个字段指向同一进程，只停一次）。
                # 拆进程模式下若只有一路停滞，另一路健康也会被一起重启 —— 代价是
                # 最多一个音频段（30s）的缺口，换来「不会漏掉任何一路停摆」。
                self.stalls += 1
                who = '、'.join(f'{k}={v}s' for k, v in stalled.items())
                self.log(f'[media] ffmpeg 活着但不出数（{who} 没有任何新产物），'
                         f'强杀后用新流地址续采（第 {self.restarts} 次）')
                self._kill_procs()
            else:
                what = '合并进程' if self.merged else f'音频={dead_audio} 抽帧={dead_frame}'
                self.log(f'[media] ffmpeg 提前退出（{what}），'
                         f'用新流地址续采（第 {self.restarts} 次）')
                if dead_audio:
                    self.audio_proc = None
                if dead_frame:
                    self.frame_proc = None
            self._spawn()
            # 收尾竞态：停止信号可能在 `_spawn()` **之后**才置位（上面的守卫是在
            # `_resolve_stream_url()` 之后判的，而停止也可能恰好挤在这中间）。此时
            # `stop_media` 已经扫过一遍了，只认这一对刚起的进程自己收掉。
            if self._stop_event.is_set():
                self._kill_procs()
                return


# ── ttwid 进程级缓存 ────────────────────────────────────────────────────────
# ttwid 是**访客级**标识（`live.douyin.com` 首页响应的 Set-Cookie），与直播间无关，
# 全进程同一个值。原实现每个 `DouyinLiveWebFetcher` 都重新请求一次首页 ——
# 实测 0.87s/路，100 路就是 87s 的纯重复开销，且每次都重新做 DNS+TCP+TLS 握手。
# 这里改为进程级共享：谁先要谁去取，其余复用。
#
# 锁是**持着做网络请求**的，所以首次获取失败时会最多阻塞其他线程一个 timeout（30s）。
# 这是刻意的取舍：ttwid 拿不到的话整个采集链路本来也走不通，让它显式串行等待
# 好过 142 个线程各自去撞一遍首页（那才是真的把风控招来）。
#
# ★ TTL 是给**长跑守护进程**准备的：`auto_crawl` 一跑几周不重启，如果只缓存不刷新，
#   一旦 ttwid 失效（Cookie 过期 / 风控降级），整个进程后续新建的连接都会**静默**带
#   上一个死值 —— 而每个 fetcher 各自的 `__ttwid` 又会把它固化下来，连重试都没有。
#   压测那种短进程无所谓，守护进程必须有刷新路径。
_TTWID_LOCK = threading.Lock()
_TTWID_CACHE = None
_TTWID_CACHE_TS = 0.0    # 上次成功获取的时刻（TTL 判定用）
_TTWID_TTL = 30 * 60     # 秒。到点强制重取一次 —— 30 分钟 = 48 次/天的首页请求，可忽略


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
        # ★ 最后一帧的到达时刻（见 frame_age）。构造时就置位，否则「还没收到第一帧」
        #   会被算成「已经很久没收到帧」，刚连上就被判僵死。
        self.last_frame_ts = time.time()
        # WS 断线重连状态（见 start / _prepare_reconnect / next_reconnect_state）
        self._intentional_stop = False      # stop() 置位 = 「下播/SIGTERM/外部停」，与「被掐」区分
        self._reconnect_wake = threading.Event()  # 让重连退避等待可被 stop() 打断
        self.reconnect_count = 0            # 本会话的重连**次数**（写进 session_meta）
        self.reconnect_gap_s = 0.0          # 主动退避等待的累计秒数（**不是**丢数据时长）

    def frame_age(self):
        """距**最后一帧**过了多少秒 —— WS 僵死判据（见 auto_crawl._one_cycle）。

        ⚠️ 判据必须用「距最后一帧」而**不是**「距最后一条弹幕」：心跳的 `need_ack`
        也走 `_wsOnMessage`，健康连接每 ~5 秒就刷新一次，而一个安静的小房间可能
        几分钟没有弹幕。用弹幕会把安静房间误判成僵死。

        失效模式：TCP 连接活着，但抖音不再下发任何数据。此时 `on_offline` 永远不来
        （它是 `ControlMessage status==3` 触发的），`ws_thread.is_alive()` 恒为真 ——
        2026-10-15 实测有房间这样挂死 90 分钟，且**该主播之后每次开播全部漏采**。
        """
        return time.time() - self.last_frame_ts

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

    def start(self, max_reconnects=0, reconnect_wait=RECONNECT_WAIT):
        """连上并开始接收；**断线自动重连**，直到本场真正结束才返回。

        对外保持「`start()` 返回 == 本场结束」的语义（见 `next_reconnect_state` 上面那段），
        所以调用方拿 `ws_thread.is_alive()` 当「本场还在采吗」的判据**继续成立** ——
        `auto_crawl._one_cycle` 的等待循环与 `_wait_media_slot` 都因此一行不用改。

        `max_reconnects` 默认 **0 = 不重连**（改动前行为）。只有 `auto_crawl` 显式打开。
        """
        # 每次 start 都是一次新的采集会话：GUI 会**复用同一个 fetcher 实例**
        # （`stop_monitor` 之后再 `start_monitor`），不复位的话残留的
        # `_intentional_stop=True` 会让第一次连接返回后立刻 break。
        self._intentional_stop = False
        self.reconnect_count = 0
        self.reconnect_gap_s = 0.0
        fails = 0
        while True:
            # ★★ 每次连接前都要置位：`_wsOnOpen` 自己不置位（只起心跳线程），而
            #    `_teardown_ws` 会把它置 False —— 漏了这行，重连后握手成功但**心跳永不发出**，
            #    连接会被服务端当僵尸掐掉。
            self.running = True
            t0 = time.time()
            self._connectWebSocket()        # 阻塞到这条连接关闭
            if self._intentional_stop:
                break                       # 下播 / SIGTERM / 外部 stop()
            keep, fails = next_reconnect_state(time.time() - t0, fails, max_reconnects)
            if not keep:
                if max_reconnects > 0:      # 0 = 「配置成不重连」，不是「额度用尽」
                    self.log("WEBSOCKET",
                             f"重连额度用尽（连续 {fails} 次连接都没活过 "
                             f"{RECONNECT_GRACE:.0f} 秒）—— 结束本场")
                break
            self.reconnect_count += 1
            self.log("WEBSOCKET", f"⚠️ 连接断开，第 {self.reconnect_count} 次重连…")
            t_gap = time.time()
            if not self._prepare_reconnect(reconnect_wait):
                break                       # 退避等待期间被 stop() 叫醒
            self.reconnect_gap_s += time.time() - t_gap

    def _teardown_ws(self, join_timeout=1.0, clear_ws=False):
        """收掉当前这条连接：停心跳线程、关 socket。

        三个调用方共用（`stop()` / `_connectWebSocket` 的握手异常 / `_prepare_reconnect`），
        且默认路径必须与改动前**逐字节等价** —— 所以这里就是原 `stop()` 的那三行。

        `clear_ws=True`（重连用）**先**把 `self.ws` 摘成 None：`_sendHeartbeat` 的循环体
        读的是实例属性 `self.ws`，重连后它已指向**新** socket，旧心跳线程醒来会往新连接
        发 ping 且永不退出。摘成 None 让它下一轮必定 `break`。

        `join_timeout` 必须 > `_sendHeartbeat` 的 `time.sleep(5)`，否则 join 恒定超时、
        等于没 join（`stop()` 用 1.0 是历史值，心跳最长睡 5 秒时它本来也会超时返回，
        但那时进程正要退出、留着无所谓；重连是**长期**循环，漏 join 会累积线程）。
        """
        self.running = False
        ws = self.ws
        if clear_ws:
            self.ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        hb = self.heartbeat_thread
        if hb is not None and hb.is_alive() and hb is not threading.current_thread():
            hb.join(timeout=join_timeout)
        self.heartbeat_thread = None

    def stop(self):
        self._intentional_stop = True   # 「下播 / SIGTERM / 外部停」—— 与「被掐」区分开
        self._reconnect_wake.set()      # 打断重连退避等待，否则 systemctl stop 要白等
                                        # 最多 max_reconnects × reconnect_wait 秒
        self._teardown_ws()             # join_timeout=1.0，与改动前完全一致

    def _prepare_reconnect(self, wait):
        """重连前清干净上一轮的残留；返回 False 表示等待期间被 `stop()` 叫醒（别再连了）"""
        self._teardown_ws(join_timeout=HEARTBEAT_JOIN_TIMEOUT, clear_ws=True)
        # ★★ 必须重置：`frame_age()` 用的是它，而 `auto_crawl._stalled` 拿
        #    `frame_age() > 300` 判僵死。不重置的话重连成功的第一秒 frame_age 就等于
        #    「上一帧到现在」，**新连接刚建立就被判僵死**，重连等于白做。
        self.last_frame_ts = time.time()
        self._reconnect_wake.clear()
        return not self._reconnect_wake.wait(wait)

    @property
    def ttwid(self):
        """
        产生请求头部cookie中的ttwid字段，访问抖音网页版直播间首页可以获取到响应cookie中的ttwid
        :return: ttwid
        """
        if self.__ttwid:
            return self.__ttwid
        # 进程级共享（见模块顶部 _TTWID_CACHE 的说明）：96 路 concurrent 下
        # 原实现会并发打 96 次首页，现在只打一次。带 TTL，见 _TTWID_TTL。
        global _TTWID_CACHE, _TTWID_CACHE_TS
        with _TTWID_LOCK:
            now = time.time()
            if _TTWID_CACHE is None or (now - _TTWID_CACHE_TS) > _TTWID_TTL:
                headers = dict(COMMON_HEADERS)
                try:
                    # 加 timeout：同 room_id property 那条，原实现无超时，挂死的连接会永久占住调用线程
                    response = requests.get(self.live_url, headers=headers, timeout=30,
                                            proxies=proxy_for_requests())
                    response.raise_for_status()
                except Exception as err:
                    self.log("ERROR", f"请求直播URL错误: {err_text(err)}")
                    # 取不到时**先降级复用旧值**（刚过期但多半还能用），好过整条链路立刻断；
                    # 旧值也没有才返回 None —— 不把失败固化进缓存，下次调用仍会重试
                    if _TTWID_CACHE is not None:
                        self.__ttwid = _TTWID_CACHE
                        return self.__ttwid
                    return None
                _TTWID_CACHE = response.cookies.get('ttwid')
                _TTWID_CACHE_TS = now
            self.__ttwid = _TTWID_CACHE
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
            response = requests.get(url, headers=headers, timeout=30,
                                    proxies=proxy_for_requests())
            response.raise_for_status()
        except Exception as err:
            self.log("ERROR", f"请求直播间URL错误: {err_text(err)}")
            return None
        self.live_page_html = response.text
        return self.live_page_html

    def _enter_api(self, room_id_str=''):
        """GET `/webcast/room/web/enter/`，返回响应里的 `data` 字典；失败返回 None。

        抽成独立方法的原因：`room_id` 与 `get_room_status` 都要打这条接口，而**两者
        都不需要事先知道真实 room_id** —— 实测 `room_id_str` 传空串照样返回完整数据，
        `data.data[0].id_str` 就是真 room_id（与 HTML 正则抠出来的一致，4/4 房间验证）。

        原实现是反过来的：`get_room_status` 为了填 `room_id_str` 先去调 `self.room_id`，
        而那会下载**整个 1.1MB 直播页 HTML** 再正则抠一个 id —— 等于每次状态请求
        都白下一次整页。实测 JSON 接口 0.51-0.74s vs HTML 1.07-1.29s。
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
            'room_id_str': room_id_str,
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
            }, timeout=30, proxies=proxy_for_requests())
            resp.raise_for_status()
            return resp.json().get('data')
        except Exception as e:
            self.log("ERROR", f"请求直播间状态失败: {err_text(e)}")
            return None

    @property
    def room_id(self):
        """
        根据直播间的地址获取到真正的直播间roomId，有时会有错误，可以重试请求解决
        :return:room_id
        """
        if self.__room_id:
            return self.__room_id
        # 快路径：轻量 JSON 接口（实测 0.5-0.7s）。房间对象在 data.data[0]，
        # 注意不是 data.room —— 后者实测恒为空字典。
        data = self._enter_api('')
        if data:
            rooms = data.get('data') or []
            if rooms and rooms[0].get('id_str'):
                self.__room_id = str(rooms[0]['id_str'])
                return self.__room_id
        # 兜底：走原来的 HTML 正则。接口改版/字段改名时不至于整条链路断掉。
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
        # 复用 _enter_api。**不再传 self.room_id** —— 原实现为它先去下载 1.1MB 的
        # 直播页 HTML，而实测 room_id_str 传空串照样返回完整数据（见 _enter_api 注释）。
        data = self._enter_api(self.__room_id or '')
        try:
            if data:
                room_status = data.get('room_status')
                user = data.get('user') or {}
                # 房间对象在 data.data[0]。data.room 实测**恒为空字典** —— 原实现读的
                # 就是它，所以 room_title / user_count_str / room_stats 一直是空的。
                rooms = data.get('data') or []
                room = rooms[0] if rooms else {}
                # 顺手缓存真 room_id，供 get_audience_ranklist 等后续调用复用
                if room.get('id_str') and not self.__room_id:
                    self.__room_id = str(room['id_str'])
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
            self.log("ERROR", f"获取直播间状态时出错: {err_text(e)}")
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
                proxies=proxy_for_requests(),
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
            # 代理走 `run_forever` 的参数（WebSocketApp 构造不吃这些）：
            # websocket-client 不认 http_proxy 环境变量，必须显式传。未配置时代入 {}，
            # 与改动前的 `run_forever()` 逐字节等价。
            self.ws.run_forever(**proxy_for_ws())
        except Exception as e:
            self.log("ERROR", f"WebSocket连接错误: {str(e)}")
            # 清理动作与原来的 `self.stop()` 逐字节相同，但**不标记** `_intentional_stop`
            # —— 否则「握手就失败」会被当成「主动停止」而**不重连**，重连额度形同虚设。
            self._teardown_ws()

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

        # ★ 这里**是所有下行帧的唯一必经之路**（心跳的 need_ack 也走这里），所以是
        #   「连接还活着吗」最可靠的观测点。放在 parse 之后：解析失败的帧不算有效帧。
        #   用途见 frame_age() 与 auto_crawl 的僵死判据。
        self.last_frame_ts = time.time()

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
        # ★ 只认「当前这条连接」：重连后旧 socket 的延迟回调会把新连接的 `running`
        #   清掉，于是新连上的心跳线程第一轮就退出。默认（不重连）路径下 `self.ws`
        #   就是刚关的那条 → 条件恒真 → 与改动前行为一致。
        if ws is self.ws:
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