# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

抖音直播间实时数据采集工具，用于 MEMF-GEF 直播销量预测研究的多模态数据采集。通过连接抖音 WebSocket 服务器，捕获弹幕、点赞、用户进出、关注/分享、粉丝团、商品成交以及直播间统计信息。`liveMan.py` 仍保留基于 Tkinter 的图形化界面（按类别分组的日志面板），但当前主用途是**无人值守采集链路**。

## 运行方式

```bash
pip install -r requirements.txt

python collect_full.py 56697889278 600   # 全模态单场采集（文本/数值/图像/音频）
python auto_crawl.py anchor.xlsx 60      # 生产守护：多主播轮播长跑（默认不含音视频）
python auto_crawl.py anchor.xlsx 60 --media   # 同上，另加音视频（仅 5-10 主播规模）
python auto_crawl.py documents/并发测试.xlsx 60 --sheet 40  # 用并发测试名单的第 40 档
python test/validate_fullmodal.py        # 校验采集产物真实性（默认取最新 data/**/live_*）
python liveMan.py                        # 图形界面（调试用）

python load_test.py --mode v8 --anchors 100            # 签名路径开销（离线）
python load_test.py --mode pump --anchors 100 --duration 600   # 落盘链路承压（离线）
python test/run_ladder.py --duration 600               # 20/40/60/80/100 阶梯真实并发
```

需要 Python 3.8+、Node.js v18+、`ffmpeg`（音视频模态；不开 `--media` 时不需要）以及 `protoc` 25.1（protobuf 编译器）。protobuf 编译生成的 `protobuf/douyin.py` 已生成并提交到仓库中（无需重新编译，`protoc.exe` 也在 `protobuf/` 下）。

**不需要 Playwright** —— 音视频所需的流地址来自 `requests` 拉到的直播间 HTML（`liveMan.fetch_live_html()` 里就有 `flv_pull_url`），实测可直接喂 ffmpeg 解码，见 `test/probe_stream_url.py`。

## 文件布局（当前链路）

| 模态 | 文件 |
|---|---|
| 文本（弹幕）/ 数值 / 粉丝团 | `liveMan.py` + `data_recorder.py`（核心，活跃维护） |
| 图像 / 音频 | **`liveMan.py` 的 `MediaCapture`**（ffmpeg 直解 FLV 原始分辨率抽帧 + 单进程 `segment` 切片）；`collect_full.py` 与 `auto_crawl.py --media` 共用 |
| 全模态单场入口 | `collect_full.py` |
| 守护式多主播入口 | `auto_crawl.py` |
| 登录态 | `save_auth.py`（`config/auth.json`） |
| 资源指标 | `metrics.py` |
| 并发压测 | `load_test.py`（`pump` 离线 / `v8` 签名 / `network` 真实连接）+ `test/run_ladder.py`（20-100 阶梯驱动器） |
| **商品** | **灰豚第三方平台**（直播结束后下载）——**代码侧不采集商品** |

根目录**只有 7 个 .py**（上表全部）。音频口径统一为 **16kHz 单声道无损 WAV**（`pcm_s16le`），
供 Wav2Vec 2.0 / librosa-OpenSMILE 直接使用；**不要再用 mp3 口径**。

- `legacy/` —— 已归档的旧采集逻辑（`collect_all.py`/`collect_concurrent.py`/`product_extractor.py` 及各历史备份）。**平时不用看**，见 `legacy/README.md`。
- `test/` —— 探针与单元验证脚本（`probe_*` 为一次性实测工具，`test_*_unit.py` 为可重复的合成数据单测）。
- `documents/` —— 研究文档；特征口径以 `documents/特征体系与数据来源.md` 为准。
- `modify.md` —— 逐次改动与实测结论的完整记录（**排查口径问题前先查这里**），已在 `.gitignore` 中。

## 数据目录布局（`data/`，2026-09-29 起）

`data/` 下按 **日期** 分组（`YYYYMMDD`，无时分秒），避免几十上百个场次目录平铺：

```
data/
  20260929/                        # 日期目录 = 这一天的所有场次（含存量归档与新采集）
    live_56697889278_20260929_160000/   # 单场（每次 DataRecorder 一个）
      room.json  danmaku.jsonl  events.jsonl
      series.json  series.jsonl  fansclub_edges.jsonl
      audio/ frames/                # 仅 collect_full.py（全模态）
    live_19733746_20260929_201503/      # 同一主播多次开播 = 多个场次目录
  20260930/
    live_56697889278_20260930_001200/   # 跨零点后**新开**的场次落次日目录
  20260915/                        # ← 2026-09-29 归档的存量数据，格式相同
    live_56697889278_20260915_081353/
  _load_test/                      # ← 压测数据，**按并发档位**分开（下划线前缀 = 非生产）
    20/  live_164425316_20260929_1713*×7 …    # 20 路并发那一轮的全部场次
    40/  60/  80/  100/                       # 各档同理
  _test_residue/                   # 一次性试跑产物（旧探针输出等）
```

- **日期目录由入口在「确认开播之后」解析，不是 `DataRecorder` 自建**。
  单场见 `collect_full.py`，长跑见 `auto_crawl.py` 各主播线程；构造器是
  `data_recorder.new_date_dir()`。检测前就解析会让每次对已下播房间的空跑留下空目录。
- **日期目录只在开播时解析一次，一场直播中途不换目录**：23:50 开播的场次落在
  `data/20260929/` 就一直写到下播，跨零点也不动；00:05 开播的另一场才落 `data/20260930/`。
  场次目录（`live_*`）由 `DataRecorder` 构造时定死，日期目录只影响**此后新开**的场次。
- **并发安全无需加锁**：100+ 主播线程同时解析得到同一目录名，`os.makedirs(exist_ok=True)`
  本身幂等。（2026-09-29 早先版本用「共享 `RunDir` + 锁 + 按天滚动」，因日期目录天然同名而删除。）
- 引用场次路径的脚本一律用 **`data/**/live_*` 递归查找**。
- **`data/_load_test/`** 是压测产物，**不要混进研究语料**：同一房间被复用 N/M 次会产出
  N/M 份内容重复的场次（100 路压 3 个房间 = 33 份重复流），且 `room_status` 写的是「压测」。
  与 `data/_test_residue/` 同一约定：**下划线前缀 = 非生产**。

## 架构

### 核心数据流

1. `DouyinLiveWebFetcher` 通过访问抖音首页获取 `ttwid` cookie
2. 通过正则表达式从直播页面 HTML 中提取真实的 `room_id`
3. 构建包含所有必要参数的 WebSocket URL，然后调用 `generateSignature()` 生成反爬签名
4. 建立 WebSocket 连接；接收到的消息先经过 gzip 解压，再通过 protobuf 解析（`PushFrame` → `Response`），最后根据消息的 `method` 字符串分发到对应的处理函数
5. 心跳线程每 5 秒发送一次 protobuf 编码的 ping；服务端会回复 `need_ack` 消息，必须对其确认

HTTP 接口（直播间状态、观众排行榜）不经过 WebSocket，而是独立的 AJAX 请求，需要 `a_bogus` 签名（见下）。

### 签名生成

存在两条相互独立的签名路径：

1. **WebSocket `signature`** — [liveMan.py](liveMan.py:38) 中的 `generateSignature()` 从 WebSocket URL 中提取 URL 参数，对特定子集做 MD5 哈希，然后通过 `py_mini_racer`（Python V8 绑定）调用混淆后的 `sign.js` 中的 `get_sign(md5_param)`，产生 WebSocket 端点所需的 `signature` 查询参数。

2. **HTTP `a_bogus`** — [liveMan.py](liveMan.py:73) 中的 `generate_a_bogus(params_str, user_agent)` 调用 `lib/reverse/douyin_old_algo_ref.js` 中的 `sign_datail()`，使用 SM3 + RC4 + 自定义 Base64 算法。抖音对 `/webcast/room/web/enter/` 和 `/webcast/ranklist/audience/` 强制要求 `a_bogus`，缺失或无效时返回 HTTP 200 但响应体为空（静默反爬拦截）。`douyin_old_algo_ref.js` 是"旧算法"，抖音升级后可能失效，届时需重新逆向。

`sign_v0.js` 是 `sign.js` 的 Node.js（`jsdom`）版本变体——当前 Python 代码未使用（原通过 `execjs` 使用该文件的代码路径已被注释掉）。

### `lib/` 目录

- `lib/reverse/douyin_old_algo_ref.js` — 当前生效的 a_bogus 签名脚本，导出 `sign_datail()` 和 `sign_reply()`
- `lib/runtime/sign_cli.js` — Node.js a_bogus 命令行入口（备用）
- `lib/runtime/bdms/` — JSVMP 补环境方案（`bdms.js` / `env.js` / `index.js` 等），研究中的备用路径，当前不可用（需配合 `webmssdk.js` 才能生成通过校验的值）

### Protobuf 消息 (`protobuf/douyin.py`)

基于 `douyin.proto` 使用 `betterproto` 自动生成。关键消息类型：

- **`PushFrame`** — 外层 WebSocket 帧包装（seq_id、log_id、payload_type、payload 字节）
- **`Response`** — gzip 解压后的内层响应；包含 `messages_list`（`Message` 列表）和 `need_ack` 标志
- **`Message`** — 包含 `method`（字符串，如 `WebcastChatMessage`）和 `payload`（需解析为具体消息类型的字节）
- 消息体类型：`ChatMessage`、`GiftMessage`、`LikeMessage`、`MemberMessage`、`SocialMessage`、`RoomUserSeqMessage`、`FansclubMessage`、`ControlMessage`、`EmojiChatMessage`、`RoomStatsMessage`、`RoomMessage`、`RoomRankMessage`、`RoomStreamAdaptationMessage`

### 关键 API 端点

| 用途 | URL |
|---|---|
| WebSocket | `wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/` |
| 直播间状态 | `https://live.douyin.com/webcast/room/web/enter/` |
| 观众排行榜 | `https://live.douyin.com/webcast/ranklist/audience/` |
| 直播首页（获取 ttwid） | `https://live.douyin.com/` |

### 控制流注意事项

- `room_id` 和 `ttwid` 作为实例属性延迟缓存
- `ControlMessage` 的 `status == 3` 表示直播已结束——触发 `self.stop()`
- WebSocket URL 的构造较为脆弱：许多参数值被硬编码（屏幕尺寸、浏览器版本字符串等），抖音更改要求时可能需要更新
- `generateMsToken()` 生成 107 位随机字符作为 cookie token——用于获取 room_id 及 HTTP 接口的请求
- 两个 HTTP 接口（`get_room_status`、`get_audience_ranklist`）都需在请求中带上 `ttwid` + `msToken` + `__ac_nonce` cookie、`Referer` 头，以及 `a_bogus` 参数
