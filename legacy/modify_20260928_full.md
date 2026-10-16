# 修改记录

## 2026-08-03：补全 HTTP API 的 a_bogus 签名

### 背景

抖音对 `/webcast/room/web/enter/` 和 `/webcast/ranklist/audience/` 等 AJAX 接口强制要求 `a_bogus` 签名参数。原代码中 `a_bogus=` 为空字符串，导致 API 返回 HTTP 200 但响应体为空（抖音的反爬静默拦截）。

原项目中的 `sign.js`（v1.0.0.53）仅用于 WebSocket 签名（通过 `get_sign(md5)` 生成 `signature` 参数），不适配 HTTP 接口的 `a_bogus` 签名。

### 问题分析

1. **WebSocket 签名**：`generateSignature()` 提取 WebSocket URL 参数 → MD5 → `get_sign()` → `signature`，此机制正常工作。

2. **HTTP a_bogus**：当前抖音使用 SM3 哈希 + RC4 加密 + 自定义 Base64 的签名算法（不同于旧的 MD5 方案）。`sign.js` 中的 `get_sign()` 返回的是 `X-Bogus`，但输入参数格式不对（需传入 URL query string + User-Agent，而非 MD5 hash）。

3. 尝试了三种 a_bogus 生成方案：
   - 方法 v1（模仿 generateSignature 的参数拼接）→ 失败
   - 方法 v2（排序后的 query string MD5）→ 失败
   - 方法 v3（完整 query string MD5）→ 失败
   - 原因：sign.js 版本过旧，当前抖音要求 SM3 而非 MD5

### 调研过程

1. 搜索了 2025-2026 年抖音 a_bogus 签名算法的公开资料
2. 发现 `mafqla/douyin-api` 项目提供了两种方案：
   - **纯 Python 查表法**（abogus_pure.py）：需 time_mapping_full.json（>5MB，未提交到仓库）
   - **旧算法 JS + py_mini_racer**（douyin_old_algo_ref.js）：完整 SM3 + RC4 实现，通过 `sign_datail()` 函数生成
3. 尝试了 `bdms.js` 补环境方案（lib/runtime/bdms/）：生成的值无效，注释标明"需要 webmssdk.js 才能完整工作"

### 采用方案

使用 `douyin_old_algo_ref.js` + `py_mini_racer`，通过 `sign_datail(query_string, user_agent)` 生成 a_bogus。

**选择理由**：
- 项目已使用 `py_mini_racer` 执行 JS，零额外依赖
- 该 JS 文件包含完整 SM3 + RC4 实现，无需额外数据文件
- 实测有效（HTTP 200 + 43KB JSON 响应）

### 修改的文件

#### 1. `liveMan.py` — 核心改动

**新增 `generate_a_bogus()` 函数**（第 73-87 行）

```python
def generate_a_bogus(params_str, user_agent, script_file='lib/reverse/douyin_old_algo_ref.js'):
    with codecs.open(script_file, 'r', encoding='utf8') as f:
        script = f.read()
    ctx = MiniRacer()
    ctx.eval(script)
    return ctx.call("sign_datail", params_str, user_agent)
```

**重写 `get_room_status()` 方法**（第 189-222 行）

- 参数改为字典形式，通过 `requests.get(url, params=params)` 传递
- 添加 `msToken` 参数生成
- 调用 `generate_a_bogus()` 生成有效签名
- Cookie 中补充 `msToken` 和 `__ac_nonce`
- 添加 `Referer` 请求头

**重写 `get_audience_ranklist()` 方法**（第 244-286 行）

- 同样改为字典参数 + a_bogus 签名
- 移除无效的"VIP 路线"重试逻辑（两个 URL 完全相同，无实际意义）
- 统一使用 `self.user_agent`

**修复消息分发逻辑**（第 399-415 行）

```python
# 旧代码：dict.get(method)(msg.payload)  → 未知 method 返回 None，调用 None() 报错
# 新代码：先获取 handler，判断非 None 再调用
handler = {...}.get(method)
if handler:
    handler(msg.payload)
```

**修复 `_parseMemberMsg()` 性别解析**（第 454 行）

```python
# 旧代码：gender = ["女", "男"][message.user.gender]  → gender 越界报 IndexError
# 新代码：增加边界检查，越界时显示"未知"
gender = ["女", "男"][message.user.gender] if message.user.gender in (0, 1) else "未知"
```

#### 2. 新增文件

| 文件 | 大小 | 来源 | 说明 |
|---|---|---|---|
| `lib/reverse/douyin_old_algo_ref.js` | 15.7 KB | mafqla/douyin-api | SM3+RC4 签名算法（433行），导出 `sign_datail()` 和 `sign_reply()` |
| `lib/runtime/sign_cli.js` | 2.0 KB | mafqla/douyin-api | Node.js a_bogus 签名命令行入口（备用） |
| `lib/runtime/bdms/bdms.js` | 348 KB | mafqla/douyin-api | JSVMP 补环境模块 v1.0.1.19（研究中，当前不可用） |
| `lib/runtime/bdms/env.js` | 52 KB | mafqla/douyin-api | 浏览器环境模拟 |
| `lib/runtime/bdms/index.js` | 7.1 KB | mafqla/douyin-api | bdms 入口，导出 `get_a_bogus()` |
| `lib/runtime/bdms/collect_traces.js` | 2.2 KB | mafqla/douyin-api | 轨迹收集工具 |
| `lib/runtime/bdms/instrument.js` | 2.0 KB | mafqla/douyin-api | 代码插桩工具 |
| `test_live.py` | - | 新建 | 直播间数据获取测试脚本 |
| `CLAUDE.md` | - | 新建 | Claude Code 项目指导文件（中文） |

### 测试结果

- **直播间**：`https://live.douyin.com/646454278948`（与辉同行）
- **时间**：2026-08-03 13:10

| 测试项 | 状态 | 结果 |
|---|---|---|
| 获取 ttwid cookie | 正常 | 成功 |
| 提取 room_id | 正常 | `7669563139070970659` |
| 获取直播间状态 | 正常 | 主播：与辉同行，直播中 |
| 获取观众排行榜 | 正常 | 获取 200 个观众数据 |
| WebSocket 连接 | 正常 | 连接成功，收到实时数据 |
| 聊天消息 (CHAT) | 正常 | 实时推送 |
| 进场消息 (ENTER) | 正常 | 含性别标签 |
| 关注消息 (FOLLOW) | 正常 | 实时推送 |
| 点赞消息 (LIKE) | 正常 | 实时推送 |
| 统计信息 (STATS) | 正常 | 在线人数 / 累计观看 |
| 未知消息类型 | 正常 | 静默跳过，不再报 NoneType 错误 |

### 遗留问题

1. **bdms 补环境方案不可用**：`index.js` 中注释说明需配合 `webmssdk.js` 才能工作，当前 `get_a_bogus()` 生成的值无法通过服务端校验。已保留作为备用，待后续研究。

2. **纯 Python 查表方案不可用**：`abogus_pure.py` 依赖 `time_mapping_full.json`（>5MB），该文件因体积过大被 gitignore，仓库中仅有 100 条样本的 `time_mapping_sample.json`（覆盖 2024-01-01 ~ 2024-01-02）。如需使用需运行 CDP 采样工具生成完整映射表。

3. **douyin_old_algo_ref.js 是旧算法**：可能在未来被抖音升级替换，届时需重新逆向新算法。

---

## 2026-08-16：并发测试脚本与日志存储改造

### 背景

单直播间抓取（`test_live.py`）已验证可用后，需要支持多个直播间并发抓取，并为每个直播间生成独立的日志文件，最后汇总统计。

### 新增 `test_concurrent.py` 并发测试脚本

实现 10 个直播间同时抓取，核心设计：

- 每个直播间一个 `RoomWorker` 实例 + 独立线程，互不干扰（`DouyinLiveWebFetcher` 的 `ttwid`/`room_id`/`ws`/`heartbeat_thread` 均为实例属性，天然隔离）
- 每个 worker 持有独立日志文件句柄 + `threading.Lock` 锁，保证 heartbeat 线程与消息回调线程并发写入安全
- `log_callback` 回调内完成两件事：写日志文件 + 解析消息做统计

### 关键问题与修复

**问题 1：`fetcher.start()` 阻塞导致线程卡死**

`_connectWebSocket()` 内部调用 `websocket.WebSocketApp.run_forever()`，这是一个**阻塞**的事件循环。若在 worker 线程内直接调用 `fetcher.start()`，线程会永久卡在事件循环上，无法执行后续的 `stop()` 和统计。

**修复**：将 `fetcher.start()` 放到独立子线程，由 worker 线程负责计时和停止：

```python
monitor_thread = threading.Thread(target=self.fetcher.start)
monitor_thread.daemon = True
monitor_thread.start()
self.ws_connected = True
time.sleep(MONITOR_SECONDS)
self.fetcher.stop()
```

**问题 2：stdout 缓冲导致看不到输出**

`sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 默认块缓冲，piped 到 `tee` 时输出不实时刷新。

**修复**：加 `line_buffering=True`，并用 `python -u` 运行。

### 日志存储方式改造

**改造前**：所有日志文件平铺在 `test_result/` 下，多次运行会混杂。

**改造后**：每次运行创建独立子目录，格式 `test_result/YYYYMMDD_HHMMSS/`，本次运行的所有文件都保存在该目录内：

```
test_result/
  20260816_185257/
    与辉同行_646454278948.log
    交个朋友_168465302284.log
    ...
    summary.log          # 本次运行的总结报告
```

关键代码：

```python
RUN_TS = datetime.now().strftime('%Y%m%d_%H%M%S')  # 本次运行目录名
RESULT_DIR = os.path.join("test_result", RUN_TS)   # 独立子目录
```

### 详细统计能力

每个直播间日志文件（带 `[HH:MM:SS]` 时间戳）之外，worker 还会从日志回调中解析并统计：

- **进场用户去重**、**发言用户去重 + 发言频率 Top5**、**点赞用户 + 总次数**
- **关注用户去重**、**礼物贡献值**（钻石数）、**在线人数 min/max/avg**

### 重要发现：抖音用户数据脱敏

并发测试中发现，抖音对**部分直播间**启用了用户数据脱敏：

- 用户 ID 统一替换为占位符 `111111`
- 昵称部分打码为 `X***`（如 `道***`、`沈***`）

**受影响直播间**（本次测试中）：小北珠宝、贾乃亮、老贝bay、朱瓜瓜、冰冰是福星、玉美人菲菲

**未受影响直播间**（真实用户 ID）：与辉同行、交个朋友、小明正能量、瑛瑛服饰

**影响**：脱敏房间无法做个体用户行为轨迹分析（停留时长、发言频率、礼物贡献的按用户统计都会失真，所有用户归并为 `111111`）。

### 测试结果（15 分钟并发，2026-08-16 18:52-19:08）

- **8/10** 直播间成功建立 WebSocket 连接（贾乃亮、玉美人菲菲直播已结束）
- 数据亮点：
  - 小明正能量：发言最活跃（2,495 条聊天、1,470 人去重发言、551 人关注），在线峰值 53,638
  - 老贝bay：点赞最多（11,694 次，244 人去重点赞）
  - 与辉同行：在线人数最稳定（21,825 均值，采样 181 次）
- 礼物贡献：10 个直播间监控期间均未捕获礼物消息

### 涉及文件

| 文件 | 说明 |
|---|---|
| `test_concurrent.py` | 新建：多直播间并发测试脚本（独立日志 + 总结） |
| `test_live.py` | 单直播间 15 分钟监控测试脚本（本次会话调整过监控时长与输出格式） |

---

## 2026-08-18：商品采集、购买通知逆向与结构化数据落盘

### 背景

面向 MEMF-GEF 直播销量预测模型，需补齐三类数据采集能力：购买通知、商品橱窗详情、结构化数据落盘。

### 一、逆向购买通知消息（WebSocket）

通过观察脚本递归解析 WebSocket protobuf，确定两个购物消息：

| Method | 作用 | 关键字段 |
|---|---|---|
| `WebcastLiveShoppingMessage` | 商品讲解状态 | `msg_type`(field 2: 2/3/10) + `promotion_id`(**field 3**) |
| `WebcastLiveEcomGeneralMessage` | 购买通知 + 商品列表刷新 | `type` = `LivePopMessage`（购买弹窗）/ `ProductRefreshMessage`（商品列表） |

**关键修正**：`promotion_id` 实际在 field 3，原 proto 定义为 field 4，已修正 [douyin.proto](protobuf/douyin.proto) 和 [douyin.py](protobuf/douyin.py)。

### 二、商品橱窗采集（Playwright）

商品详情（名称/价格/销量）需从浏览器渲染后的 DOM 提取，无法通过 HTTP API 获取（需签名）。

**参考项目**：`D:/CodeFile/aicode/douyin`（douyin 项目的 product_collector.py / test_product_extract.py）。

**新增 [product_extractor.py](product_extractor.py)**，关键设计：

- **force click + 重试**（5 次）：解决商品橱窗按钮点击不稳定
- **商品面板区域识别**：从"带货口碑/店铺口碑"到"在线观众"，排除观众榜干扰
- **滚动加载**：商品面板懒加载，滚动 15 次触发加载全部商品
- **兼容"查看价格"**：已抢光商品价格隐藏

**新增 WebSocket 消息处理**（[liveMan.py](liveMan.py)）：
- `_parseLiveShoppingMsg` → 记录 promotion_id + 缓存到 `product_ids`
- `_parseLiveEcomGeneralMsg` → 购买通知 + 商品列表刷新（缓存商品 ID）
- `_extract_varints` → 从 protobuf bytes 提取 ID

### 三、结构化数据落盘（DataRecorder）

**新增 [data_recorder.py](data_recorder.py)**，输出到 `data/live_{room_id}_{date}/`：

| 文件 | 内容 | 对应 MEMF-GEF 数据 |
|---|---|---|
| `room.json` | 直播间/主播/商品详情 | Anchor/Room/Commodity 实体 |
| `danmaku.jsonl` | 弹幕流（user_id + 内容 + 时间戳） | 观众弹幕序列 D |
| `events.jsonl` | 礼物/点赞/购买/关注/进出事件 | G_t/L_t/P_t |
| `series.json` | 观看人数时序 | Time-Slice 节点 |

[DouyinLiveWebFetcher](liveMan.py) 增加 `data_callback` 参数，在关键解析方法中输出结构化数据。

### 四、CDP 方案与登录态

商品橱窗**需要登录态**，未登录时点击会弹登录框。

**登录态过期问题**：原 auth.json（7月15日）已过期，需重新登录。

**新增 [save_auth.py](save_auth.py)**（从 douyin 项目复制），从 CDP Chrome 提取登录态到 `config/auth.json`。

**CDP 方案**（反检测最强）：[product_extractor.py](product_extractor.py) 支持 `cdp_port` 参数：

```python
# 需先启动 Chrome: chrome.exe --remote-debugging-port=9222 --user-data-dir=./data/chrome_profile
products = extract_products_sync('646454278948', cdp_port=9222)      # CDP 模式
products = extract_products_sync('646454278948', auth_file='config/auth.json')  # auth 模式
```

### 关键 Bug 修复

1. **`browser.close()` 误关 Chrome**：CDP 模式下，`connect_over_cdp` 的 `browser.close()` 会关闭真实 Chrome。修复：`if not opened` 分支移除 `browser.close()`，统一由 finally 处理（CDP 模式 page.close()，非 CDP 模式 browser.close()）。

2. **`image_urls` 未初始化**：`extract_products` 开头初始化 `image_urls = []`，避免异常时 UnboundLocalError。

### 测试结果

- **商品提取**：滚动加载后提取 **317 个商品**（之前仅 20 个），含名称、价格（¥18.9 ~ ¥4280）、售罄状态
- **登录态刷新**：58 个 douyin cookie 成功提取
- **页面关闭**：测试后 Chrome 保持运行

### 涉及文件

| 文件 | 说明 |
|---|---|
| `product_extractor.py` | 新建：商品橱窗采集模块（Playwright + CDP + 滚动加载） |
| `save_auth.py` | 新建：从 CDP Chrome 提取登录态 |
| `data_recorder.py` | 新建：结构化数据落盘（JSON/JSONL） |
| `test_record.py` | 新建：集成测试脚本（商品采集 + 结构化落盘） |
| `liveMan.py` | 修改：data_callback + 商品消息处理 + 购买通知解析 |
| `protobuf/douyin.proto` | 修改：修正 promotion_id 字段号 + 新增 LiveEcomGeneralMessage |
| `protobuf/douyin.py` | 修改：同上 |
| `config/auth.json` | 新增：抖音登录态（58 个 cookie） |

---

## 2026-08-19：音视频采集、统一采集入口与购买通知修复

### 一、音视频采集模块（media_collector.py）

参考 douyin 项目的 AudioCollector / ScreenshotCollector，实现抖音直播音视频采集：

- **音频**：拦截网络响应捕获 HTTP-FLV 流 URL（关键词 `pull-flv`/`.flv`/`.m3u8`），用 ffmpeg 切 MP3 片段（`-ar 22050 -ab 64k`）
- **视频帧**：Playwright 定期截图（JPEG quality=70）

验证结果（对照 douyin 项目标准）：音频 MP3 15 秒 64kbps、视频帧 JPEG 2560×1330。

### 二、统一采集入口（collect_all.py）

一次运行同时采集四类数据，输出到统一目录：

```
data/live_{room_id}_{date}/
├── room.json        # 主播 + 商品详情
├── danmaku.jsonl    # 弹幕流
├── events.jsonl     # 礼物/点赞/购买/关注/进出事件
├── series.json      # 观看人数时序
├── audio/           # 音频片段
└── frames/          # 视频帧
```

### 关键问题与修复

**问题 1：playwright 子线程 EPIPE 崩溃**

playwright 的 Node.js driver 在子线程运行会触发 `EPIPE: broken pipe`（driver 与 Python 的 pipe 通信在子线程 event loop 销毁后失效）。

**修复**：商品提取 + 音视频采集移到**主线程**（`asyncio.run`），WebSocket 监控放子线程（`fetcher.start` 同步阻塞，适合线程）。

**问题 2：页面错位（滚动逻辑错误）**

原滚动加载把**所有**可滚动容器滚到底（包括直播画面、聊天列表），导致页面错位。

**修复**：只滚动包含商品特征（"运费险"/"去抢购"/"查看价格"/"带货口碑"）的容器。

**问题 3：截图是商品面板**

商品提取后商品面板仍打开，遮挡直播画面。

**修复**：商品提取完成后按 ESC 关闭面板，恢复直播画面。

**问题 4：音视频目录与 DataRecorder 目录不一致**

audio/frames 目录用独立时间戳命名，与 DataRecorder 目录相差数秒。

**修复**：audio/frames 复用 `DataRecorder.path`，统一时间戳。

### 三、购买通知解析修复

**问题**：events.jsonl 里 purchase 事件的 ID 全部相同。

**根因**：`_extract_varints` 扁平提取 biz_content 所有大 varint，把固定的 `f3` 字段（直播间/活动 ID）误当购买 ID。

**结构分析**（递归 dump biz_content）：
- `f3`（外层）：固定值（直播间 ID，每次相同）
- `f6.f2`（嵌套消息）：变化的购买商品 ID
- `f6.f3`：时间戳

**修复**：重写 `_parseLiveEcomGeneralMsg`，用新的 `_parse_protobuf_fields` 递归解析保留字段层级，提取 `f6` 嵌套消息的 `f2` 字段（变化的商品 ID）。简短版（12B，无 f6）自动跳过。

### 涉及文件

| 文件 | 说明 |
|---|---|
| `media_collector.py` | 新建：音视频采集模块（流 URL 拦截 + ffmpeg + 截图） |
| `collect_all.py` | 新建：统一采集入口（主播+商品+音视频+弹幕） |
| `liveMan.py` | 修改：`_parse_protobuf_fields` 递归解析 + 购买通知提取 f6.f2 |
| `product_extractor.py` | 修改：滚动逻辑只滚动商品面板容器 |

---

## 2026-08-20~26：流 URL HTML 解析、视频帧优化、正在讲解商品识别与方案 B 并发

### 一、流 URL 提取改造（HTML 解析）

参考 douyin_paqu 项目，把流 URL 提取从 Playwright 网络拦截改为 **HTML 解析**：

- 从直播间 HTML 的 `self.__pace_f.push` 数据解析 `flv_pull_url`（多画质 FLV 流地址字典）
- 路径：`state.roomStore.roomInfo.room.stream_url.flv_pull_url`（FULL_HD1/HD1/SD1/SD2）
- 一次拿到 4 个画质，比网络拦截更稳定、无需等待视频播放触发请求

[media_collector.py](media_collector.py) 新增 `_extract_stream_urls_from_html` 静态方法，[collect_all.py](collect_all.py) 同步改造（保留网络拦截作 fallback）。

### 二、视频帧只截取直播画面

参考 TiktokLiveVideoRecording 的"定位直播窗口位置"思路，把截图从整页改为**只截取 `<video>` 元素**：

| 方式 | 尺寸 | 内容 |
|---|---|---|
| `page.screenshot()`（旧） | 1400×850 | 含聊天栏/侧边栏 |
| `video.screenshot()`（新） | 1080×762 | 纯直播画面 |

### 三、"正在讲解的商品"识别

商品橱窗里"讲解中"标签后的商品（**无序号**）是正在讲解的商品，原序号解析逻辑会漏掉它。

**修复**：[collect_all.py](collect_all.py) 的 `_parse_products_from_text` 增加"讲解中"识别：

- 找"讲解中"标签，提取其后的商品，标记 `is_current=True`
- 序号商品照常提取，标记 `is_current=False`
- 新增 `_extract_product_block` 辅助函数

**关联关系**：WebSocket 的 `promotion_id`（数字 ID）+ 橱窗的"讲解中"商品（名称/价格），两者对应同一个"正在讲解的商品"。

### 四、方案 B：不用账号的并发采集

**问题**：并发时两个 page 共用同一登录态，抖音检测到"账号已在别处进入直播间"，商品橱窗打开失败。

**探索的"不用账号"路径**（均失败）：
1. HTML 初始数据 `ecomStore` → 内容简单，无商品列表
2. 抖音商城详情页 → SPA（JS 渲染），商品数据需 API 签名
3. WebSocket `ProductRefreshMessage` → 低频（120秒 0条），仅含 ID
4. 商品详情 API → 返回 504/需签名

**结论**：商品详情（名称/价格）必须登录态，但 promotion_id（正在讲解的商品 ID）可通过 WebSocket 无需登录获取。

**方案 B 实现**（[collect_concurrent.py](collect_concurrent.py)）：

- 移除商品橱窗提取（需登录态的部分）
- 独立浏览器（headless，无登录态），多 page 用 asyncio.gather 并发
- 采集：主播信息（room/enter API）+ 弹幕 + promotion_id（WebSocket）+ 音视频（HTML 流 URL + ffmpeg + 截图）

**验证结果**（2 直播间并发，无需账号）：

| 直播间 | promotion_id | 音频 | 视频帧 | 流 URL |
|---|---:|---:|---:|---:|
| 与辉同行 | 3 个 | 3 段 | 6 张 | 4 个 |
| 交个朋友 | 2 个 | 3 段 | 6 张 | 4 个 |

彻底解决"账号已在别处"问题：WebSocket（弹幕/promotion_id）、音视频流、主播信息都只需 ttwid，无需账号。只有商品详情（名称/价格）被放弃，改用 promotion_id 替代。

### 涉及文件

| 文件 | 说明 |
|---|---|
| `collect_concurrent.py` | 新建：多直播间并发采集（方案 B，无需账号） |
| `media_collector.py` | 修改：HTML 解析流 URL + video 元素截图 |
| `collect_all.py` | 修改：HTML 解析流 URL + video 截图 + 讲解中识别 |

---

## 2026-08-26~30：purchase 字段分析、讲解时间记录、ffmpeg 持续抽帧与商品多模态数据探索

### 一、purchase 事件字段分析（简短版 vs 完整版）

LivePopMessage（购买通知）有两种格式，通过 [observe_purchase.py](observe_purchase.py) 完整 dump：

| 格式 | 大小 | 字段 | 是否 emit |
|---|---|---|---|
| 简短版 | 12B | `f1=1` + `f3`（固定直播间/活动 ID） | ❌ 跳过（无商品信息） |
| 完整版 | 50B | `f6.f2`=购买商品 ID + `f6.f3`=时间戳 + `f5.f3`=1000（行为类型） | ✅ emit |

**结论**：简短版不含购买商品信息（只有固定字段），跳过是合理的。完整版的 `f6.f2` 才是购买商品 ID。两者各占约一半（15 条完整版 + 13 条简短版 / 3分钟）。

### 二、商品讲解时间记录

[data_recorder.py](data_recorder.py) 增加 `product_times` 字典，记录每个 promotion_id 的讲解时间：

- `on_event` 里 shopping 事件（msg_type=2 讲解开始）记录时间 + 讲解次数
- `close` 时 room.json 的 commodities 带时间字段：

```json
{"promotion_id": "111", "first_seen": "2026-08-26T21:22:15",
 "last_seen": "2026-08-26T21:22:15", "explain_count": 2}
```

### 三、ffmpeg 持续抽帧（支持 1 秒/帧）

原 `video.screenshot()` 截取浏览器渲染容器（1080×762，有黑边）。改用 **ffmpeg 从流 URL 提取原始帧**（720×1280 竖屏，无黑边）。

高频率（1 秒/帧）下，每次 `-frames:v 1` 重新连接流有延迟/限流风险。改为**持续 ffmpeg 进程**：

```python
# 一次连接，持续输出帧（fps=1/interval）
ffmpeg -i <flv_url> -vf fps={fps} -q:v 2 frame_%04d.jpg
```

[media_collector.py](media_collector.py) 新增 `_start_frame_extractor`，验证 15 秒生成 16 帧（约 1 帧/秒）。

### 四、商品多模态数据探索（不用账号）

系统探索"不用账号"获取商品详情（标题/价格/图片）的路径：

| 路径 | 结果 |
|---|---|
| HTML 初始数据 `ecomStore` | 无商品列表 |
| App 接口 `reflow/info`（无需登录） | 无商品详情（只有流 URL） |
| 商品详情页 `item2` | 已下线 |
| 促销接口 `/live/promotions/page/get/` | 返回"用户未登录" |

**结论**：商品详情必须登录态。但发现 WebSocket 的 `ProductRefreshMessage`（无需登录）提供**促销接口线索**（`/live/promotions/page/get/` 等 + promotion_id）。

### 五、第三方 Excel 商品目录（灰豚数据关联）

第三方数据平台（灰豚）的 Excel 提供**628 个商品的完整多模态数据**（直播结束后下载）：

- 商品名称、品牌、类目、店铺、价格、销量、图片链接、讲解时长/次数
- 灰豚链接 `pId` / 商品链接 `id` 即 **promotion_id**

解析脚本生成 [data/product_catalog.json](data/product_catalog.json)（628 个商品的 promotion_id 映射）。

**关联验证**：WebSocket 采集的 36 个 promotion_id 中，11 个（31%）能关联到 Excel 商品数据（关联率低是因为 Excel 是 8-25 场次，WebSocket 是 8-26 场次，同场次会接近 100%）。

**方案**（考虑 Excel 事后下载的时序）：

```
实时层（无需登录，可并发）：WebSocket → promotion_id 时序 + 弹幕 + 音视频
关联层（事后，无需登录）：Excel → promotion_id 关联商品多模态数据
```

核心价值：商品多模态数据（名称/价格/图片）通过灰豚 Excel 获取，绕过抖音登录态限制，实现"完全不用账号"。

### 涉及文件

| 文件 | 说明 |
|---|---|
| `observe_purchase.py` | 新建：观察 purchase 事件（简短版/完整版字段） |
| `capture_purchase.py` | 新建：采集 purchase 事件到 JSONL |
| `data_recorder.py` | 修改：product_times 讲解时间记录 |
| `media_collector.py` | 修改：ffmpeg 持续抽帧（1秒/帧） |
| `data/product_catalog.json` | 新增：灰豚 Excel 解析的 628 个商品目录 |

## 2026-09-14：修复 room.json 丢 promotion_id 讲解时间 + series.json 中断丢失

### 背景

1 小时并发测试的统计发现两个数据缺口：

1. **room.json 丢 promotion_id 讲解时间** — `data_recorder.py` 的 `close()` 中 `if self.products:` 分支直接用商品面板解析的完整详情（`num/name/price/...`），把 WebSocket 采集的 `product_ids` / `product_times`（promotion_id + 讲解时间）整个丢弃；且面板详情本身没有 promotion_id，与 `shopping`/`purchase` 事件的 promotion_id 无法关联，只能靠灰豚 Excel 事后对齐。
2. **series.json 中断丢失** — `stats` 事件只 append 到内存 `viewer_series`，`close()` 才写文件；采集被暂停/崩溃时观看人数时序丢失（交个朋友场次因此缺 series.json）。

### 问题分析

- `LiveShoppingMessage`（proto）只有 `msgType` + `promotionId`，无商品名称字段，故 WebSocket 无法直接拿到 promotion_id ↔ 商品名称的映射。
- 实时可靠关联需靠 promotion_id 顺序：`ProductRefreshMessage`（商品列表刷新）的 biz_content 中 19 位 varint 按**橱窗顺序**出现，与商品面板的 `num` 顺序一致，可做 best-effort 对齐。
- 时序数据落盘应在事件到达时即时写盘，而非等到 `close()`。

### 采用方案

1. **不丢 promotion_id 讲解时间** — room.json 新增 `promotions` 字段，始终输出 WebSocket 采集到的权威 promotion_id 讲解时间（`promotion_id` + `first_seen`/`last_seen`/`explain_count`），与 `commodities`（面板详情）并列。
2. **best-effort 关联键** — 利用 `ProductRefreshMessage` 的 promotion_id 出现顺序（与橱窗顺序一致），`close()` 时按 `num` 排序面板商品，把第 i 个 promotion_id 附到第 i 个商品上。优先级：刷新顺序 > 讲解顺序（`explain_count>0` 按 `first_seen` 排序）。
3. **时序边采边落盘** — 新增 `series.jsonl`，`on_event` 收到 `stats` 时立即写一行 + flush；`close()` 仍生成汇总 `series.json`（`series.jsonl` 作为中断兜底）。

### 涉及文件

| 文件 | 说明 |
|---|---|
| `liveMan.py` | 新增 `self.product_refresh_order`，`ProductRefreshMessage` 按出现顺序记录 promotion_id |
| `data_recorder.py` | `close()` 输出 `promotions` + best-effort 附 promotion_id；新增 `series.jsonl` 边采边写、`set_promotion_order()`、`_promotion_id_order()` |
| `collect_all.py` | `close()` 前调用 `set_promotion_order(fetcher.product_refresh_order)` |
| `collect_concurrent.py` | 同上 |
| `test_record.py` | 同上 |

### 注意

- `commodities[].promotion_id` 是启发式对齐（按橱窗顺序），主播跳讲/重复讲时可能错位；权威数据是 `promotions`，精确关联仍建议用灰豚 Excel 的 promotion_id 校正。
- 中断场景统计观看人数时若 `series.json` 缺，可改读 `series.jsonl`。

## 2026-09-15：直播自动全程采集（守护模式）

### 背景

正式批量爬取需要"自动全程"能力：从主播列表自动检测开播、全程采集到下播、自动落盘，无需人工指定 room_id 和时长。主播列表来自 anchor.xlsx（15 个主播，按粉丝量分 5 层）。

经确认需求：**长期守护循环**（持续监控，每场开播自动采到下播，循环往复）+ **仅弹幕/事件/商品**（纯 WebSocket，无浏览器，轻量）。

### 关键发现

- anchor.xlsx 给的是**抖音号**（如 `PPMM88`、`19733746`），不是直播间 `room_id`。`DouyinLiveWebFetcher` 的 `room_id` property 会访问 `live.douyin.com/{抖音号}` 自动解析真实 room_id。
- **room_id 解析到 ≠ 开播**：未开播的主播也会返回历史房间号（实机探测 15 个全部解析到 room_id）。真正的开播判断必须靠 `get_room_status()`（web/enter API，`room_status==0` 为直播中）。

### 采用方案

1. **每主播一个守护线程**（`AnchorDaemon`）：独立生命周期，一个主播出问题不影响其他。
2. **双重开播检测**：先 `room_id` 解析（判断抖音号有效）→ 再 `get_room_status()` 确认 `room_status==0`。
3. **下播自动落盘**：`_parseControlMsg` 的 status==3 触发 `on_offline` 回调 → 守护线程 `recorder.close()` 落盘。
4. **优雅退出**：非 daemon 线程 + Ctrl+C 时 stop + join，保证当前场次 close 完成。

### 涉及文件

| 文件 | 说明 |
|---|---|
| `auto_crawl.py` | 新建：守护模式自动全程采集脚本（load_anchors + AnchorDaemon） |
| `liveMan.py` | 修复 room_id None 保护；新增 on_offline 回调；_parseControlMsg 触发回调 |

### 实机验证

- 读取 anchor.xlsx：15 个主播（含 2 个纯数字抖音号 19733746、56697889278）
- 探测结果：3 个正在直播（卡唯朵扬姐、东方甄选、与辉同行），12 个已结束

### 已知限制

- WebSocket 意外断开（网络抖动，非下播）时当作本场结束落盘，下一轮检测重新发现开播开新场次——单场可能切多段，但不丢数据。断线重连待后续优化。

## 2026-09-16：修正成交语义（Bug 1）+ 接入 ProductChangeMessage 讲解数据（Bug 2 前置 + 缺口）

### 背景

数据 bug 排查发现两个问题 + 一个数据缺口：

1. **Bug 1 — msg_type=2 语义误标**：`LiveShoppingMessage.msgType` 值 2 原被当作「讲解开始」，统计口径错误（经数据实证：msg_type=2 与 LivePopMessage 购买通知 1:1 同秒对应，实为成交/下单）。
2. **Bug 2 — 成交商品 ID 不稳定**：`LivePopMessage` biz_content 的 `f6.f2` 提取的购买 ID 在不同直播间不稳定（部分直播间与 `LiveShoppingMessage.promotionId` 一致，部分完全对不上）。
3. **讲解数据缺口**：真正的讲解状态（讲解中/已讲解）在 `ProductChangeMessage`（`updateProductInfoList` 含 `promotionId`/`explainType`/`index`），原代码未处理。

### 采用方案

1. **msg_type 语义修正**：`type_desc = {2: "成交/下单", 3: "下架/讲解结束", 10: "其他"}`；`data_recorder` 中 msg_type==2 累加 `sale_count`（成交次数），不再累加 `explain_count`；`_promotion_id_order` 兜底改用 `sale_count>0`。
2. **接入 ProductChangeMessage**：新增 `_parseProductChangeMsg`，提取 `promotionId`/`explainType`/`index`，emit `explain` 事件；`data_recorder` 新增 `_record_explain`，`room.json` 输出 `explain_types`（explainType 取值分布，不做语义解释，`updateToast` 文案写入日志辅助定标）。
3. **f6 完整结构 dump**：LivePopMessage 分支把 `f6` 嵌套结构 `f6_dump` 写入 PURCHASE 日志，供后续核对哪个字段才是真正的 promotion_id。

### 涉及文件

| 文件 | 说明 |
|---|---|
| `liveMan.py` | dispatch 新增 `WebcastProductChangeMessage`；新增 `_parseProductChangeMsg`；LivePopMessage 增加 `f6_dump` 日志 |
| `data_recorder.py` | `_record_product_time` 累加 `sale_count`；新增 `_record_explain` + `explain_types` 输出；`_promotion_id_order` 兜底改用 `sale_count>0` |

### 注意

- `explainType` 语义（0/1/2）当时未定标，只记录原始分布，待与第三方数据（灰豚）比对确认。
- Bug 2 当时仅加日志观察，未落地修复（正确字段待实测确认）。

## 2026-09-17：灰豚第三方数据对齐与数据质量校验

### 背景

引入灰豚（huitun）平台导出的 9.15 数据（`documents/9.15/*.xlsx`，11 场，含「商品列表/直播数据/观众画像/观众来源」4 个 sheet）作为 ground truth，校验自研采集质量并定标 Bug 2 / explainType。

### 关键发现

1. **promotion_id 完全对齐**：灰豚链接 `pId=`/`id=` 参数就是 promotion_id，与 WebSocket 抓取的是同一套 19 位 ID，整体交集 543/732 = 74%。
2. **观看人数采集准确**：`RoomStatsMessage.viewer_count` 的峰值/平均在线与灰豚几乎一致（平均在线差 0.9%，峰值 104%），7/9 主播峰值完全一致。
3. **Bug 2 定案（反转）**：成交商品 promotion_id 应取 `LivePopMessage` 的 `f6.f2`（讲解成交命中 80%），而 `LiveShoppingMessage.promotionId`（字段 3）在多数主播根本不是 promotion_id（0~6% 命中）。
4. **静默成交是采集盲区**：灰豚本场成交商品中约 70% 是「挂车自助下单」（讲解次数=0），不触发 WebSocket 购买通知，采集不到。口径结论：我们的成交数据 = 讲解驱动成交（约占 30%），非全部成交，直接用其预测总销量会系统性低估约 70%。
5. **互动指标根因**：
   - 点赞：`LikeMessage` 有 `count`（增量，被抖音节流，实测 count 全为 3 的倍数）与 `total`（累计总量），原代码累加 count 只得 24.5%，应改用 `total`。
   - 关注：`SocialMessage` 是「社交消息」，`action` 字段区分关注/取关/分享，原代码未过滤 action，把取关/分享也当关注（178%），应按 action 过滤或取 `followCount` 净值。

### 产物

- `analysis/灰豚数据对比分析_20260915.md`：完整对比文档（ID 对齐、观看验证、成交校验、以与辉同行为例的六维对比、数据质量总结）。

### 待办（未落地）

| 项 | 说明 |
|---|---|
| Bug 2 修复 | 成交 promotion_id 改用 `purchase` 的 `f6.f2`；`shopping(msg_type=2)` 仅保留成交事件计数，不再绑定 promotion_id |
| 点赞修复 | `_parseLikeMsg` 改用 `LikeMessage.total`（累计）替代累加 `count` |
| 关注修复 | `_parseSocialMsg` 按 `action` 过滤，仅统计关注动作；或记录 `followCount` 算净增 |
| ground-truth 标注表 | 灰豚 1072 个 promotion_id → 讲解次数/时长/销量落 JSON，关联到 room.json |
| explainType 定标 | 重新抓取后用 explainType 状态切换重建讲解次数/时长，与灰豚比对 |

## 2026-09-17：与辉同行 1h 字段逻辑验证（三项修复落地验收）

### 背景

对 2026-09-17 待办的三项修复（Bug 2 / 点赞 / 关注）做 1 小时纯 WebSocket 实测验收。新增 [test_field_1h.py](test_field_1h.py)（`DURATION=3600`、`LIVE_ID=56697889278`，硬编码，每 5 分钟打印进度，结束汇总 action 取值分布）。

### 实测结果（14:00:53 → 15:00:54）

事件分布：`enter 21665 / shopping 769 / product 733 / purchase 329 / chat 1877 / social 585 / like 1202 / follow 499 / stats 834`。

**1. Bug 2 彻底定案 —— f6.f2 才是 promotion_id**

`LivePopMessage`（购买通知）共 627 条，其中：

| 形态 | 条数 | 说明 |
|---|---:|---|
| f6 为空（`f6=[]`） | 298 | 非成交类 pop（点赞榜/进场等），无商品信息，正确跳过 |
| f6 非空 | 329 | 均为 `[(f1,X),(f2,Y),(f3,ts)]` 三元组，结构稳定 |

f6 三元组语义确认：

- **f6.f1** = `LiveShoppingMessage.promotionId`（字段 3）——与 shopping 消息**同一 ID 体系**
- **f6.f2** = 真正的 promotion_id（灰豚 pId/id 命中）
- **f6.f3** = unix 时间戳

最有力证据是**同一笔成交两条消息 329 次一一对应、但 ID 不同**：

| 次数 | shopping type=2 字段3 | LivePopMessage f6.f2 |
|---|---:|---:|
| 175 | 3842118060681068954 | 3842033875136152358 |
| 95 | 3791990859482923054 | 3791988924583379129 |
| 24 | 3829054406574342202 | 3829049246120804805 |
| 20 | 3822388466931597430 | 3822384361051914396 |
| 12 | 3701854400642434367 | 3701852960662356076 |
| 各 1 | 3842790351911911710 等 3 个 | 3840388805941133442 等 3 个 |

shopping 字段3 与 f6.f2 出现次数完全一致（175/95/24/20/12/1/1/1 = 329），但值是两套不同的 19 位 ID。结合灰豚命中率（f6.f2 讲解成交 80%，字段3 仅 0~6%），**成交 promotion_id 取 f6.f2**。

**2. 点赞 —— `total` 累计值验证通过**

`like_total` 从 933542 增长到 **1052614**（1h 净增 ~11.9 万），单调合理，`LikeMessage.total`（字段3）作为累计权威值可用；`count`（字段2）仍被节流为 3 的倍数，只作增量参考。

**3. 关注 —— action 语义定案（修正「2=取关」假设）**

`SocialMessage.action` 分布：`{1: 499, 3: 585}`，**action=2 全程未出现**。

| action | 含义 | follow_count | 证据 |
|---|---:|---|---|
| 1 | 关注 | 真实粉丝数（39454489 → 递增） | FOLLOW 日志带递增粉丝数 |
| 3 | 分享 | 恒为 0 | 分享不改变粉丝数；SocialMessage 有 `share_type`(字段3)/`share_target`(字段5) 分享专用字段 |
| 2 | 取消关注 | — | 整段未出现（罕见事件） |

此前代码注释「2=取消关注」是未经验证的假设，实测无 action=2；action=3 才是主流非关注动作（分享）。

### 落地修改

| 文件 | 改动 |
|---|---|
| `liveMan.py` `_parseSocialMsg` | action 语义注释定案（1=关注/3=分享/2 罕见）；action=3 日志与事件补充 `share_type`/`share_target` |
| `liveMan.py` `_parseLiveEcomGeneralMsg` LivePopMessage 分支 | 注释补充 f6 三元组定案说明（f6.f1=字段3 同体系、f6.f2=promotion_id、f6.f3=时间戳） |
| `test_field_1h.py` | 新建：1h 字段逻辑验证脚本 |

### 遗留

- `explainType` 语义（0/1/2）仍未定标，本场 ProductChangeMessage 讲解事件（explain 事件）可作后续定标原始数据。
- 静默成交（挂车自助下单）盲区仍在：本场 f6.f2 只捕获到讲解驱动成交（约 30%），非全部成交。

## 2026-09-24：9.22 数据口径验证 + 接入讲解信号 + 礼物连击收尾

### 背景
校验 2026-09-22 采集的 12 个目录（并发测试 4 + auto_crawl 守护 8），并对照 `D:/CodeFile/GithubProjects`
下的开源参考项目（DouyinBarrageGrab / douyinlive-proto / douyinLive / live-room-watcher / douyin-live-info 等）核证字段语义。

### 9.22 口径校验结论
- ✅ purchase promotion_id（f6.f2）12 目录全部 19 位，数千条无例外。
- ✅ follow/social action 仅 1(关注)/3(分享)，无 2(取关)；like_total / shopping_down_count / 成交次数均正确。
- 🐛 **follow_count 被分享事件清零**：3/12 目录 follow_count=0（分享事件 follow_count 恒为 0，
  data_recorder 对 follow/social 都读 follow_count 覆盖，分享排在最后即清零）。已修复 + 从 events.jsonl 回填。
- ⚠️ gift=0：全 data/ 历史无任何 gift 事件（带货间基本不送礼，礼物解析从未在真实数据验证过）。
- ⚠️ explain=0：WebcastProductChangeMessage 全程缺席（Web 端 WS 拿不到讲解信号）。

### 参考项目核证（字段口径）
| 字段 | 结论 |
|---|---|
| `Gift.diamond_count`(12) | 单价（抖币），非总额 ✅ 与现实现一致 |
| `Gift.combo_count`(6) | 本次增量件数，聚合用 ✅ |
| 礼物总价值 | `Σ(diamond×combo)` ✅ 与现实现一致 |
| `Gift.repeat_end`(9)/`group_id`(11) | 连击结束标记/连击分组；`repeat_end==1 && group_id>0` 为收尾信号（参考 DouyinBarrageGrab） |
| `Social.share_target`(5) | 分享平台码（112抖音好友/1微信/2朋友圈/3微博/4QQ空间/5QQ），平台在 share_target 不在 share_type ✅ |
| `Social.action` | 1/3 被证实；2=取关 无开源依据（仅推断） |
| `AwemeShopExplainMessage.Extra.active` | 「是否正在讲解」的权威 bool 信号，比 explainType 更直接 |
| `ProductInfo.explain_type`(4) | 全网无枚举定义（0/1/2 无来源） |
| `LiveShopping.msg_type` | 2=成交/3=下架 无开源依据，仅本项目实证 |
| `LiveShopping.promotion_id`(3) | 字段3 与 douyinlive-proto 一致；douyin_paqu 的 =4 是错的（4 已被 target_uid 占用） |

### 落地修改（本次）
| 文件 | 改动 |
|---|---|
| `protobuf/douyin.proto` | 新增 `AwemeShopExplainMessage`（含 `Extra{ promotionId, active }`） |
| `protobuf/douyin.py` | 新增 `AwemeShopExplainMessage` / `AwemeShopExplainMessageExtra` 生成类 |
| `liveMan.py` | 路由加 `WebcastAwemeShopExplainMessage`；新增 `_parseAwemeShopExplainMsg`（发 explain 事件带 active）；`_parseGiftMsg` 补发 `repeat_end`/`group_id` 并注释字段口径 |
| `data_recorder.py` | gift 连击收尾（repeat_end==1 && group_id>0）跳过聚合；explain 事件按 active 记录讲解；room.json promotions 暴露 explain_active_dist；follow_count 仅关注事件更新（修复清零 bug） |

### 讲解口径（新定）
- `explain_count` = 讲解次数 = `active` 上升沿（False→True）计数（每次「开始讲解」计 1）。
- `explain_active_dist` = 原始 true/false 分布（保留供事后核对）。

### 遗留
- `WebcastAwemeShopExplainMessage` 是否经 WebSocket 下发尚未实测（若同 ProductChange 一样缺席，`_seen_methods` 日志可诊断）；实际 method 名以实测为准。
- 礼物 repeat_end/group_id 口径已按参考实现落地，但真实礼物事件仍待捕获实测。
- 静默成交盲区仍在（约 70% 挂车自助下单不触发 WS 通知）。

## 2026-09-28：娱乐主播礼物口径实测 + WS 连接上下文/ACK 修正

### 背景
用户指定娱乐主播 `568544961053`（栋梁，5000 在线）验证礼物口径。此前 1h 与辉同行测试 gift=0，
怀疑是「带货间不送礼」，故换娱乐间复测。

### 实测结论：礼物消息**根本没有下发**（4 种组合对拍一致）

| 组合 | 连接方式 | 礼物事件 | 收到 method 数 |
|---|---|---|---|
| 基线 | Chrome UA + 仅 ttwid | 0 | 17 |
| 登录态 | Chrome UA + config/auth.json 完整 cookie | 0 | 17（与基线**逐条一致**） |
| PC 画像 | awemePcClient UA + `X-AWEME-*` 头 + PC 固定 cookie + `need_persist_msg_count=0` | 0 | 17 |
| 新鲜上下文 | im/fetch 预取 cursor/internal_ext/push_server_v2 后再建连 | 0 | 10 |

- 10 分钟内 600+ 事件（chat 604 / like 229 / enter 518 / stats 229 / follow 90），**0 条 WebcastGiftMessage**。
- `_unhandled_methods` 全程无任何礼物相关方法名，无 ERROR → **不是解析失败、不是路由漏注册**。
- 与辉同行 `/webcast/im/fetch/` 近期窗口 63 条消息同样 0 礼物。
- 对照组（同一房间、同一窗口）游客态与登录态 method 集合**完全相同** → 不是登录态过滤。
- 该房间是**福袋/红包活动间**（弹幕刷屏「偷红包」、`WebcastRoomDataSyncMessage` 携带
  `LotteryInfoSyncData`），enter API 显示 `ecom_room_disable_gift=0`（未禁用礼物）。
  → 房间可能确实近乎无付费礼物，尚**不能**断定是 Web 协议通道整体不下发礼物。

**遗留（待复核）**：需换一个礼物高频娱乐间（才艺/唱歌/PK 类）做同法对拍，才能定论。
礼物解析代码与口径（`diamond_count` 单价 / `combo_count` 增量 / `repeat_end`+`group_id` 收尾）
本身已按参考实现对齐，但**至今未被真实礼物事件验证过**。

### 顺带确认：`/webcast/im/fetch/` 的可达性
- 游客态（仅 ttwid）：HTTP 200 但**响应体为空**（静默反爬）。
- 带 `config/auth.json` 完整 douyin cookie：HTTP 200 / `application/protobuffer` / 28–282 KB
  正常返回，**即使 `sessionid` 已于 2026-09-13 过期**（`ttwid`/`odin_tt`/`s_login` 等仍有效）。
  → im/fetch 可用，可作为 WS 之外的辅助通道与连接上下文来源。

### 落地修改

| 文件 | 改动 |
|---|---|
| `liveMan.py` `_decodeFramePayload`（新） | gzip 解压从「无条件 gzip.decompress」改为**按 gzip 魔数 `0x1f8b` 判定**；新增 FRAME 诊断日志（每个新的 payloadType/compress_type/魔数组合打一次，含 `headers` 全量） |
| `liveMan.py` `_wsOnMessage` ACK | **修正**：回显下行 PushFrame 头 `im-internal_ext` 的**原值**（原为 `Response.internal_ext`，抓包实测两者 seq/first_req_ms 系统性不同）；且改为「非空即 ACK」（原为仅 `need_ack` 时 ACK） |
| `liveMan.py` WS URL 参数化 | 新增 `user_unique_id` / `persist_msg_count` / `ws_push_host` / `ws_cursor` / `ws_internal_ext` 属性，URL 不再内联硬编码；`_default_internal_ext()` 现算兜底值 |
| `liveMan.py` `set_ws_context()`（新） | 用 im/fetch 预取的 cursor/internal_ext/push_server_v2 覆盖硬编码兜底（复现 douyinLive 的预取流程） |
| `liveMan.py` | 新增 `cookie_string`（登录态 Cookie 串）与 `extra_ws_headers`（PC 画像 `X-AWEME-*` 等）注入点 |
| `test/test_gift.py` | 新建：娱乐主播礼物口径专项测试（含 repeat_end/group_id 分布、聚合跳过对比、method 诊断） |
| `test/probe_im_fetch.py` | 新建：探测 `/webcast/im/fetch/` 是否下发礼物 |
| `test/probe_gift_auth.py` | 新建：同房间游客态 vs 登录态对拍 |
| `test/probe_gift_arms.py` | 新建：同房间 Web 画像 vs PC 客户端画像对拍 |
| `test/probe_gift_freshctx.py` | 新建：同房间陈旧 vs 新鲜连接上下文对拍 |

### 参考依据
- `douyinLive`（Go）`message_decode.go`：ACK 回显 `im-internal_ext` 头原值，而非 `Response.internal_ext`。
- `douyinLive` `protocol_profile.go`：PC 客户端画像常量（UA / `X-AWEME-*` / PC 固定 cookie /
  `need_persist_msg_count=0`），抓包自抖音桌面版 8.5.1。
- `douyinLive` `websocket_connection.go`：建连前先 `im/fetch` 预取 cursor/internal_ext/push_server。

### 礼物口径合成数据验证（补足「从未被真实数据执行」的空白）
因服务端不下发礼物，`_parseGiftMsg` 路径从未被真实数据走到。用合成 `GiftMessage` 跑通并断言 22 项：

| 用例 | 断言 |
|---|---|
| 连击串（3 增量 + 1 收尾） | 字段映射全对（gift_id/name/type/单价/combo_count/repeat_count/total_count/group_id/repeat_end）；聚合件数=3（收尾跳过）；价值=Σ(diamond×combo)=3；送礼人去重=1 |
| 单发礼物（`group_id==0` 且 `repeat_end==1`） | **照常计入**（+5 件 / +50 价值）—— 收尾跳过只对 `group_id>0` 生效 |
| 连击收尾（`combo_count` 非 0 的收尾） | 跳过，不计入聚合 |
| 落盘 | events.jsonl 行数、ts 注入、room.json 的 gift_total_count/gift_total_diamond/gift_user_count |

结果：22/22 通过（`test/test_gift_parse_unit.py`）。
即**礼物口径逻辑已被证明正确**，唯一未验证的是「服务端是否/何时下发该消息」。

### 娱乐主播名单（供晚间复测）
`test/test_gift.py` 内置批量探测名单（唱歌/音乐人类，礼物高频），来源公开主播资料：
lanwan(蓝万) / doulaimusic(DOU来好歌会) / yuanyuan.6789(嫄嫄全能唱将) / jtxd888(音乐浪子) /
wanan669(媛小小) / 59239524067(娟妹) / 44415627794(怪才杨五)。
2026-09-28 14:35 探测：**全部未播**（这类主播多在 20:00-24:00 开播）。晚间执行
`python test/test_gift.py` 即可自动捕获在播主播并做礼物口径校验。

## 2026-09-28（续）：讲解时序语义可获取性核查

### 问题
上一个迭代接入的两个讲解信号（`AwemeShopExplainMessage.active`、`ProductChangeMessage.explainType`）
在 Web 端能否拿到？讲解的**时序语义**（哪个商品在什么时间被讲解）能否重建？

### 结论一：现成的两个讲解信号**一次都没下发**
扫描全部 88 个 `data/live_*` 目录：`explain` 事件总数 = **0**。
`WebcastAwemeShopExplainMessage` / `WebcastProductChangeMessage` 从未出现在 `_seen_methods`。
→ 上一轮基于 `active` 上升沿的 `explain_count`、以及 `explain_types` 分布，**在 Web 端恒为空**。

### 结论二：`WebcastLiveShoppingMessage` 才是实际在流的讲解载体（我们只解析了 3/36 字段）
东方甄选实测 10 分钟 21 条，顶层字段号：

| 字段 | 名 | 实测 |
|---|---|---|
| 1 | common | 含 `create_time` = 毫秒时间戳 |
| 2 | msg_type | 2 / 3 / 10 |
| 3 | promotion_id | msg_type=10 时**缺失** |
| 17 | promotions_num | msg_type=10 时出现，实测 **383**（商品总数） |
| 22 | trace_time_metric | `op_timestamp` / `msg_send_timestamp` 毫秒 |
| 30 | hot_atmosphere | msg_type=2 有内容，=3 为空消息 |
| 33 | logical_clock | **实测等于同一毫秒时间戳** |

→ **三个独立毫秒时间戳**（f1.f4 / f22 / f33）可交叉校验，时序精度到毫秒。
→ 本项目 proto 只有 `common/msgType/promotionId`，其余 4 个字段（17/22/30/33）全丢。

### 结论三：`msg_type` 语义需重新标定（现有口径有错）
- `msg_type=2`：与 `LiveEcomGeneralMessage(content_type=LivePopMessage)` 秒级对齐 → 很可能是成交/商品事件。
- `msg_type=3`：**现注释「下架/讲解结束」不成立**。实测与 type=2 成对出现、**同一 promotion_id**、
  间隔约 600ms（东方甄选，先 3 后 2），10 分钟内 9 对，全程同一商品 —— 商品不可能下架 9 次。
  与辉同行 9.16 数据则是 3/2 严格交替（3435/3435）、间隔约 6s，两种房间节奏不同，语义尚未定标。
- `msg_type=10`：带 `promotions_num`（商品总数）、**无 promotion_id** → 是**商品列表数量变化**事件，
  不是讲解切换标记。实测只覆盖 72% 的讲解切换点，且 90% 的 type=10 不在切换点。

### 结论四：讲解时间轴的可用信号 = `promotion_id` 连续段
与辉同行 9.16（12.9h）统计：≥10 条的连续同 pid 段 **118 段**，中位数 **3.6 分钟**，
最长 53.7 分钟，累计讲解 **669 分钟**。段边界即讲解切换。
→ 这是当前**唯一可用的讲解时序近似**，但它是「周期性推送在讲哪个商品」，**不是**服务端明确下发的
「开始讲解 / 结束讲解」事件。

### 结论五：`LiveEcomGeneralMessage.content_type` 我们只处理了 1/3
实测分布：`LivePopMessage`(18) / `ProductRefreshMessage`(16) / `ProductListRefreshMessage`(7)。
- `ProductRefreshMessage`：f6 带商品 id + **状态码 f3**（样例 f3=4）+ 秒级时间戳
- `ProductListRefreshMessage`：f6 直接带中文文案 **「主播更新了商品，已自动刷新」** + 时间戳
→ 这两个是商品/讲解状态变化的直接通知，当前被完全忽略。

### 待办（按价值排序）
1. 扩展 `LiveShoppingMessage` proto：17 `promotions_num` / 22 `trace_time_metric` / 30 `hot_atmosphere`
   / 33 `logical_clock`，尤其 22 与 33 的毫秒时间戳。
2. 处理 `ProductRefreshMessage` / `ProductListRefreshMessage` 两个 content_type。
3. 继续长时段多房间抓取，确认参考 proto 的字段 **19 `updated_commentary_video_info`**（讲解视频信息，
   `{action}`）是否下发 —— 这是唯一可能给出**显式讲解动作**的字段，东方甄选 10 分钟内未出现。
4. 修正 `shopping_down_count` 口径（当前累加的是 msg_type=3，实测语义不是下架）。

### 落地修改
| 文件 | 改动 |
|---|---|
| `liveMan.py` | 新增 `raw_callback`（收到每条消息的原始字节旁路，口径标定用，不改 proto 即可 dump 全部 wire 字段） |
| `test/probe_explain_raw.py` | 新建：通用 protobuf wire 解码器 + LiveShoppingMessage 全字段 dump，落盘 JSONL |

## 2026-09-28（续2）：9.16「讲解数据缺口」处理情况核查

### 缺口状态：**未关闭**，三次尝试两次失效、一次未执行

| 时间 | 动作 | 结果 |
|---|---|---|
| 09-16 | 接入 `ProductChangeMessage.updateProductInfoList`（promotionId/explainType/index），新增 `_parseProductChangeMsg` + `data_recorder._record_explain`，room.json 输出 `explain_types` | 代码在（`liveMan.py:793`），但该消息**从未下发** → `explain_types` 全空 |
| 09-17 | 灰豚对齐，把「explainType 定标（按状态切换重建讲解次数/时长，与灰豚比对）」列入待办 | **未执行** |
| 09-24 | 因 ProductChangeMessage 缺席，改接 `AwemeShopExplainMessage.Extra.active`（bool），新增 `_parseAwemeShopExplainMsg` + `_record_explain_active`（active 上升沿计 `explain_count`） | 代码在（`liveMan.py:816`），同样从未下发 → `explain_active_dist` 全空 |
| 09-28 | 扫描全部目录核查 | `explain` 事件总数 = **0**；`explain_types` 非空条目 = 0；`explain_active_dist` 非空条目 = 0 |

两个解析器与路由（`liveMan.py:609-610`）都还健在，**不是代码坏了，是消息根本不来**。

### 顺带查出的数据污染：`explain_count` 在 09-17 之前是错口径

09-16 之前 `msg_type=2` 被当作「讲解开始」直接累加进 `explain_count`；09-16 的 Bug 1 修正把它改为累加 `sale_count`。
因此 **20260917 之前的 room.json 中，`explain_count` 字段名与语义不符**。

实测证据（逐条目核对 `room.json.explain_count` vs `events.jsonl` 中 `msg_type=2` 计数）：

| 日期 | 目录数 | `explain_count>0` 条目 | 合计 | 与 msg_type=2 计数一致？ |
|---|---|---|---|---|
| 20260915 | 20 | 503 | 17248 | ✅ 与辉同行 9/9、东方甄选 1/1，**零例外** |
| 20260916 | 5 | 50 | 3492 | 当天中途才修正，部分仍为旧口径 |
| 20260917 起 | 23 | 0 | 0 | 新口径（`explain_count` 只来自 explain 事件，而事件不下发 → 恒 0） |

### 对训练数据的影响与处理建议

1. **20260917 之前的 room.json**：`explain_count` 实际是「msg_type=2 计数」，**必须改名或剔除**，否则会被当成讲解次数特征误用（9.15 那批合计 17248 次是虚假讲解数）。
2. **20260917 之后**：`explain_count` 恒为 0，是**空列**，MEMF-GEF 里不能作为讲解特征，应直接从特征集移除或标记缺失。
3. 讲解特征若必须要有，**唯一可用替代**是 `LiveShoppingMessage` 的 `promotion_id` 连续段时长（近似讲解时长），见本文件上一节。

## 2026-09-28（续3）：礼物口径**定案** —— Web IM 通道不承载 WebcastGiftMessage

### 测试对象
用户指定的娱乐主播 `731918444565`（爱乐-L，团播，粉丝团活跃，在播）。
加上此前 2 个房间，共 3 个房间、3 条独立通道交叉验证。

### 决定性证据：抖音官方网页自己的 WebSocket 也收不到礼物

用 Playwright 打开真实直播间页面，**在页面内 hook `WebSocket` 类**抓官方网页真正收到的帧：

| 项 | 结果 |
|---|---|
| 端点 | `wss://webcast100-ws-web-lf.douyin.com/webcast/im/push/v2/`（与本项目同端点族） |
| 规模 | 150 秒 / 292 帧 |
| method | 14 种（Member 654、Chat 190、InRoomBanner 188、RanklistHourEntrance 133、GroupLiveOnCamRecognition 61、Like 50、RoomStats 32、RoomUserSeq 21、Social 10、Fansclub 9、ChatLike 7、RoomMessage 2、ExhibitionChatMessage 2、GroupLiveContainerChange 2） |
| **WebcastGiftMessage** | **0** |

→ **不是我们的连接参数、登录态或解析问题。** 官方客户端同样收不到。

### 三条通道全部零礼物

| 通道 | 规模 | 礼物 |
|---|---|---|
| 本项目直连 WS | 3 房间（栋梁 / 与辉同行 1h / 爱乐-L） | 0 |
| HTTP `/webcast/im/fetch/` 轮询 | 2737 条消息 / 17 种 method / 3 分钟 | 0 |
| **官方网页自身 WS** | 292 帧 / 14 种 method / 150 秒 | **0** |

### 口径逻辑本身已验证正确
`test/test_gift_parse_unit.py` 用合成 `GiftMessage` 断言 22 项全通过：
字段映射（gift_id/name/type/单价/combo_count/repeat_count/total_count/group_id/repeat_end）、
`Σ(diamond×combo)` 累计价值、`repeat_end && group_id>0` 跳过连击收尾、单发礼物（`group_id==0`）不被误跳、
送礼人去重、events.jsonl / room.json 落盘。

### 结论与影响
- **`gift` 事件在现有采集路径上永远采不到**，`gift_total_count` / `gift_total_diamond` /
  `gift_user_count` 恒为 0，MEMF-GEF 的礼物特征**不能来自这条通道**。
- 页面上可见的礼物面板、贡献榜、送礼列表走的是另外的 HTTP 接口，不是这条 WS。
- 要拿礼物数据只能换路：MITM 代理抓抖音 PC 客户端（DouyinBarrageGrab 思路）、
  逆向 HTTP 送礼榜/贡献榜接口、或用第三方（灰豚）。

### 技术备注
`context.on('websocket')` 在无头 Chrome 下对抖音直播页观测为 **0 个 WS** —— WS 建在 Worker 里，
Playwright 该 API 抓不到。必须用 `add_init_script` 在页面内替换 `WebSocket` 类才能拿到 ground truth。
工具：`test/probe_page_ws2.py`（页面内 hook）、`test/probe_imfetch_poll.py`（im/fetch 轮询）。

## 2026-09-28（续4）：礼物相关字段**正式从特征口径移除**

依据续3的定案（Web IM 推送通道不承载 `WebcastGiftMessage`，官方网页自身 WS 亦收不到），
礼物特征已从特征体系中正式移除。**移除范围：特征口径层；`liveMan._parseGiftMsg` 解析器保留**
（通道若恢复可直接启用，届时从 `events.jsonl` 重算即可）。

### 代码改动

| 文件 | 改动 |
|---|---|
| `data_recorder.py` | 删除 `gift_total_count` / `gift_total_diamond` / `gift_user_ids` 三个属性与全部聚合逻辑；`room.json` 不再输出 `gift_total_count` / `gift_total_diamond` / `gift_user_count`；`gift` 分支**保留**（仍逐条写入 `events.jsonl`，保住原始事件流）；连击收尾聚合规则以注释形式记在 gift 分支，供将来恢复聚合时参照 |
| `liveMan.py` | **不动** —— `_parseGiftMsg` 与 `WebcastGiftMessage` 路由保留 |
| `test/test_gift_parse_unit.py` | 改为验证新口径：解析字段映射（12 项）+ 收尾/单发事件均写入 events.jsonl + **断言 `DataRecorder` 上不再有聚合属性、`room.json` 不再含礼物字段** + 其余特征字段仍在 |
| `test/test_gift.py` | `report()` 不再读 `recorder.gift_*`（会 AttributeError），改为本地统计并标注"仅供参考，不再落盘" |
| `test/test_field_1h.py` | 进度行移除礼物件数/价值 |
| `test/test_field_concurrent.py` | 汇总行移除礼物三字段 |
| `verify_0922.py` | 礼物字段改为「旧 schema 残留检测」：新数据应为 `False`，若仍出现则记为异常 |

**验证**：`test/test_gift_parse_unit.py` 24/24 通过；对在播主播 731918444565 实采 70s，
新 `room.json` 顶层键为 `room_id/live_id/title/anchor_nickname/anchor_id/room_status/like_total/
follow_count/shopping_down_count/commodities/promotions` —— **无任何 gift 键**，其余字段正常。

### 文档改动

| 文件 | 改动 |
|---|---|
| `documents/特征体系与数据来源.md` | ① §3.5 Time-Slice 的「礼物数 g_t」标注 ❌ 不可用；② §3.4 行为信号「是否送礼/送礼价值」与 `gifts_to` 边划除；③ 第三层群体聚合的「送礼率」划除；④ 数据约束段与第五节 Audience 行同步；⑤ **新增 §3.8「已排除的特征：礼物」**（含三通道对照证据、已排除假设、解析器正确性说明、页面礼物≠WS 礼物的提醒、将来补数据的 3 条路径）；⑥ 处理状态一览新增第 7 项 ❌ 已排除，图例补 ❌ 标记 |
| `documents/数据爬取.md` | ① 数据源表「礼物通知」行划除；② §2.4.2 标题标注不可用并加说明块（字段结构保留作历史记录）；③ Time-Slice 的 `total_gifts`/`gifts_delta`/`total_gifts_window` 标注恒 0；④ 数据量估算表「礼物通知」行划除 |

### 未改动（需用户决策）

- `documents/MEMF-GEF_Douyin_Systematic_Review.md`（研究综述）**未动**。该文档把 `gifts_to`
  定义为关系边（第 5 类边）并规划了 `w/o gifts_to` 消融实验，另有 9 处提及礼物通知作为可用数据源。
  礼物不可用会导致该边与消融实验失效 —— 这属于**模型设计层面的调整**，不是文档一致性修正，
  需用户决定是「保留边并标注不可用」还是「重新设计该边」。
- `documents/32、MEMF.md` 为参考论文原文，不涉及本项目口径，不动。
- `analysis/analyze_20260915.py` 仍统计 gift 类型（会显示 0），属历史分析脚本，未动。

## 2026-09-28（续5）：粉丝团身份标识可获取性实测

测试房间：`731918444565`（爱乐-L，娱乐团播，粉丝团活跃）。工具：`test/probe_fansclub.py`。

### 结论一：粉丝团字段覆盖率极高，**能获取**
`user.fans_club.data`（`FansClubData`）在每条含 User 的消息上都带：

| method | 消息数 | 带 fans_club | level>0 | club_name 非空 |
|---|---|---|---|---|
| `WebcastMemberMessage` | 892 | 892 (100%) | 652 (73%) | 38 (4%) |
| `WebcastChatMessage` | 397 | 397 (100%) | 347 (87%) | 30 (8%) |
| `WebcastLikeMessage` | 61 | 61 (100%) | 25 | 1 |
| `WebcastSocialMessage` | 6 | 6 (100%) | 1 | 0 |
| `WebcastFansclubMessage` | 6 | 6 (100%) | 6 | 0 |

可用标识字段（实测）：
- `level`（粉丝团等级）：1–18 完整分布，爱乐-L 房间 Lv10 最多（507 次）
- `anchor_id`（**粉丝团归属主播 ID**）：实测 5 个不同值 → 区分「哪个粉丝团」
- `user_fans_club_status`（成员状态）：交叉表**完全可分**
  - `0` ⟺ `anchor_id==0` ⟺ `level==0`（153 次）→ **非粉丝团成员**
  - `1` ⟺ `anchor_id!=0` 且 `level>0`（537 次）→ **在团成员**
  - `2` ⟺ `anchor_id!=0` 且 `level>0`（13 次）→ 与 1 不同但保留等级，**语义未定标**
- `club_name`（粉丝团名称）：**仅 4–8% 的消息带**（实测 '小霖宝' / '小太阳' / '白桃酥'）
- `badge.title` / `badge.icons`：与 club_name 同时非空时才有
- `FansClub.prefer_data`：**实测 1362/1362 全空**，无更丰富的第二来源

`WebcastFansclubMessage` 是**精确时间点的身份变化事件**（可弥补 snapshot 式 level 缺时间戳）：
- `type=1` 升级：`'丫*** 刚刚升级至【爱乐-L】粉丝团 Lv6'`
- `type=2` 加入：`'恭喜 何*** 成为第28858名爱乐L成员'`
- 带 `level` / `user_fans_club_status=1` / `anchor_id`，但 **club_name 为空**，名称只在 `content` 文本里

### 结论二：该房间 **user.id 被完全打码**，身份无法归属到用户
- 爱乐-L：弹幕 293 + 67 条，**不同 user 数 = 1**，`user_id` 全为 `111111`（100%）
- 栋梁 `568544961053`（同日同代码）：弹幕 604 条，**402 个不同 user**，0% 打码
- 与辉同行 `56697889278`：505 次用户出现，**471 个不同 id**，0% 打码

→ 打码是**房间相关**，非普遍规则，也非本项目代码问题。触发条件未知。
**后果**：粉丝团 level/anchor_id 读得到但**塌缩到同一个 `111111`**；更严重的是该房间的弹幕/进场/关注/点赞**全部无法做按用户的分析** —— 弹幕聚类、行为向量、ν_Au→ν_An 关系边、按 user 去重的任何指标在该房间全部失效（去重人数恒为 1）。

**建议**：把打码检测做成采集质量字段（如 `room.json` 记 `user_id_masked: true`），并对已采集数据统计打码率，否则这类房间会静默污染按用户维度的特征。

### 结论三：当前代码**一条粉丝团数据都没落盘**
- `_parseFansclubMsg` 只 `self.log("FANSCLUB", content)`，`type`/`user`/`fans_club` **全部丢弃**
- `_parseChatMsg` / `_parseMemberMsg` / `_parseLikeMsg` / `_parseSocialMsg` **均未提取 `user.fans_club`**
- 落盘实测：`danmaku.jsonl` 字段 = `user_id/nickname/content/ts`；`events[enter]` = `user_id/nickname/gender/ts` —— **无任何 fans club 字段**

→ 粉丝团是**当前可用的、覆盖率远高于礼物**的 ν_Au → ν_An 关系证据（礼物恒 0），但需要新接入。

## 2026-09-28（续6）：与辉同行粉丝团标识 10min 实测 + 加入特征体系的必要性评估

测试：`56697889278`（与辉同行，带货）10 分钟，3900 个用户、4505 条消息。
工具：`test/probe_fansclub_10min.py`，原始聚合数据 `test_result/fansclub_56697889278_*.json`。

### 采集侧结论

| 项 | 结果 |
|---|---|
| 打码 | **0%**（3900 个不同 user_id，`111111` 仅 1 个）→ 可按用户归属 ✅ |
| 字段覆盖率 | Member 3686/3686、Chat 466/466、Like 202/202、Social 141/141 全部携带 `fans_club` |
| 成员占比 | Lv>0 = **1880 / 3900 = 48.2%**（Lv0 非成员 51.8%） |
| 等级分布 | Lv10(337) > Lv11(273) > Lv2(189) > Lv1(180) > Lv9(138) … 最高 Lv16 |
| `anchor_id` | **非零值只有 1 个** = `3250600708947220`（与辉同行自己）→ 单一粉丝团，非团播 |
| `club_name` | **0/3900 = 0%**（带货房间完全拿不到团名；团名只能在 FansclubMessage content 里抠，实测为「**爱生活**」） |
| 加入/升级事件 | 10 次 / 10min = **1.0 次/分钟**；`type` 分布 = **{6: 8, 2: 1, 1: 1}** |
| 多粉丝团用户 | 0/1881（因只有 1 个 anchor_id，**该结论不可推广**） |

**两个未定标项**（落文档前必须解决）：
1. **`user_fans_club_status` 跨房间语义反转**：与辉同行 `anchor_id!=0` 的绝大多数是 **status=2**（1827），status=1 仅 53；爱乐-L 恰好相反（status=1 537、status=2 13）。→ 0=非成员可确认，但 1 与 2 的含义**未定标**，不能直接使用。
2. **`FansclubMessage.type=6`** 占 8/10，而 proto 注释只写了 1=升级 / 2=加入。type=6 带 level、content 为空，语义未知。

### 判别力分析（加入特征体系的核心依据）

成员（1880）vs 非成员（2020）：

| 指标 | 成员 | 非成员 | 倍数 |
|---|---|---|---|
| 人均弹幕 | 0.08 | 0.16 | 0.50 |
| 人均进场 | 1.02 | 0.88 | 1.16 |
| 人均点赞 | 0.06 | 0.04 | 1.39 |
| 发言率 | 1.60% | 9.11% | **0.18** |
| 关注率 | 0.05% | 3.47% | **0.02** |
| 分享率 | 1.91% | 1.58% | 1.21 |

按群体看「成员占比 vs 全体基线 48.2%」更能说明问题：

| 群体 | 人数 | 其中成员 | 成员占比 | 相对基线 |
|---|---|---|---|---|
| 全体（基线） | 3900 | 1880 | 48.2% | — |
| 进场用户 | 3517 | 1807 | 51.4% | +3.2pp |
| 分享用户 | 68 | 36 | 52.9% | +4.7pp |
| 点赞用户 | 63 | 20 | 31.7% | −16.5pp |
| **弹幕用户** | 214 | 30 | **14.0%** | **−34.2pp** |
| **关注用户** | 71 | 1 | **1.4%** | **−46.8pp** |

Lv≥10 高阶成员（730 人）：95.8% 只有进场行为，弹幕 1.1%、关注 0%、点赞 1.8%、分享 2.5%。
**等级与活跃度无单调关系**（Lv1-5 人均弹幕 0.04 / Lv6-10 0.15 / Lv11-15 0.01）。

**解释**：带货房间的粉丝团成员 ≈ **早已关注过的老观众** —— 已关注故不再产生 follow 事件（成员占关注用户仅 1.4%），弹幕贡献也低。level 在此更像「历史沉淀标记」而非「当前参与度」。

**稀疏度警告**：人均消息 1.15 条、中位 1、**92.9% 的用户只出现 1 次** → 个体级行为特征本就不可靠。

### 结论：建议「有选择地加入」

**建议加入（低风险、稳定）**：
1. **群体级占比**：`fansclub_member_ratio`（实测 48.2%）、成员等级均值/分布 → Audience-Group 实体画像。覆盖 100%、不受个体稀疏影响，且直接刻画群体构成。
2. **时序**：成员占比随时间的漂移 + 加入/升级事件率（1.0 次/分钟）→ Time-Slice 特征；加入事件带**精确时间戳**，可作「新粉涌入」的参与度信号。
3. **ν_Au → ν_An 关系边替代**：`fansclub_of(user_id → anchor_id, level)`。礼物边已废（续3定案），这是当前唯一 100% 覆盖的可用关系边 —— 但带货房间只有单一 anchor_id，边的拓扑信息有限。

**不建议加入**：
1. **个体 `level` 作为行为预测特征** —— 判别力方向与直觉相反（成员占弹幕用户仅 14% vs 基线 48.2%），且个体稀疏。
2. **`user_fans_club_status`** —— 跨房间语义反转，未定标。
3. **`club_name`** —— 带货房间覆盖率 0%。
4. **等级→消费金额换算** —— 礼物恒 0，等级只能作序数标签，无法换算金额。

**结构性发现（比结论本身更值得记录）**：能做粉丝团个体分析的房间（带货，未打码）恰好信息量低；信息量可能高的房间（娱乐团播，粉丝团活跃、`club_name` 有值）恰好 **user.id 100% 打码**，做不了按用户分析。→ **当前采集路径下粉丝团的价值主要在群体级，不在个体级。**

## 2026-09-28（续7）：粉丝团群体级三类特征接入

依据续6的评估结论（粉丝团价值在群体级、不在个体级），按现有口径接入三类特征。

### 代码改动

| 文件 | 改动 |
|---|---|
| `liveMan.py` | 新增 `_fansclub_of(user)` 静态方法：从 `User.FansClub`（**字段 24**）→ `data`（字段 1）提取 `fansclub_level/anchor_id/status`（`club_name` 非空时才带），含完整字段口径注释 |
| `liveMan.py` | `_parseFansclubMsg` 从「只打日志」改为**结构化事件**：emit `fansclub` 事件，带 `fc_event_type`(type) / `content` / `user_id` / `nickname` + 粉丝团标识；注释记录 type=1升级/2加入/**6 未定标** 与「content 是团名唯一兜底来源」 |
| `liveMan.py` | `_parseChatMsg` / `_parseMemberMsg` / `_parseLikeMsg` / `_parseSocialMsg` 各追加 `_fansclub_of()` 提取并并入事件数据；**顺便给 `like` 事件补上原先缺失的 `user_id`**（否则按用户去重会漏点赞用户） |
| `data_recorder.py` | 新增 `fansclub_edges.jsonl` 输出文件（第 4 个句柄，沿用逐条 flush 的防丢风格）；新增 `_record_fansclub()` / `_uid_is_masked()` / `_fansclub_ratio()` |
| `data_recorder.py` | `room.json` 新增 `fansclub` 块：`user_count` / `member_count` / `member_ratio` / `level_mean` / `level_dist` / `anchor_member_counts` / `join_count` / `upgrade_count` / `event_type_dist` / **`uid_masked`** |
| `data_recorder.py` | `series.jsonl` 的 `stats` 快照附带粉丝团时序：`fansclub_user_count` / `fansclub_member_count` / `fansclub_member_ratio` / `fansclub_event_count`（累计） |
| `test/test_fansclub_unit.py` | 新建：合成 protobuf 验证 38 项断言 |

### 口径要点

- **判「是否成员」= `fansclub_level > 0`**（与 `anchor_id != 0` 完全等价，实测零例外）
- 聚合按用户**去重**，取**首次见到**的快照（等级会变，群体级只看构成）
- 关系边**只写成员**（level>0），非成员不构成 ν_Au → ν_An 边
- **`status`（0/1/2）未定标** → 只写进关系边留作定标，**不作特征**
- **`club_name`** 覆盖率房间相关（娱乐 4~8%、带货 0%）→ **不用作标识**，用 `anchor_id`

### 验证

**合成数据**：`test/test_fansclub_unit.py` **38/38 通过**（构成聚合、重复出现不改变首次快照、
事件用户纳入聚合、时序快照、打码检测、观测不足不误判）。

**真实端到端**：与辉同行 10min（3886 用户 / 6060 事件）——

| 项 | 实测 |
|---|---|
| `member_ratio` | **0.5067**（探针独立实测 0.482，同一量级） |
| `member_count` / `user_count` | 1969 / 3886 |
| `level_mean` | 7.21 |
| `level_dist` | Lv1~Lv18 完整（Lv10 = 392 最多） |
| `anchor_member_counts` | `{3250600708947220: 1969}`（单一粉丝团） |
| `join_count` / `upgrade_count` | 1 / 0 |
| `event_type_dist` | `{6: 6, 2: 1}` ← 再次出现**未定标 type=6** |
| `uid_masked` | `false` ✅ |
| 时序收敛 | `series.jsonl` 158 条快照，占比 0.0 → 0.506 |
| 关系边 | `fansclub_edges.jsonl` 1969 条 = `member_count` ✅ |

### 文档改动

`documents/特征体系与数据来源.md`：
- §3.4 行为信号表新增「粉丝团归属」行，标注 **`fansclub_of` 边替代 `gifts_to`**
- 第三层群体聚合新增「粉丝团成员占比 / 成员平均等级 / 等级分布」行
- §3.5 Time-Slice 表新增「粉丝团成员占比 g_t + 成员数 + 加入/升级累计」行
- **新增 §3.9「粉丝团特征」**：字段口径表（含号）、三类已接入特征、未纳入个体级的理由、
  `uid_masked` 可信度前提、结构性限制、两项待定标
- 处理状态一览新增第 8 项 ✅ 已接入

### 遗留

- `user_fans_club_status` 的 1 / 2 语义、`FansclubMessage.type=6` 语义 **未定标**（需专门实验）
- 粉丝团关系边在带货房间只有单一 `anchor_id`，拓扑信息有限；待找到「娱乐类但不打码」的房间才能评估团播场景下的边价值

## 2026-09-28（续9）：采集链路重构 —— 归档旧逻辑 + 新建当前版全模态入口

### 背景

`collect_all.py` 的核心其实**不旧**（它 import 的是当前 `liveMan.py`/`data_recorder.py`），
真正过时的是**商品提取那一路**（已改灰豚、面板打不开）。但全模态入口长期分散、旧备份与
一次性脚本混在根目录，且**没有任何单个脚本同时覆盖「当前核心 + 音视频」**
（`auto_crawl.py` 不带音视频，`collect_all.py` 带音视频但含约 100 行已废的商品解析代码）。

### 新建：`collect_full.py`（当前版全模态单场入口）

| 模态 | 来源 |
|---|---|
| 文本（弹幕）/ 数值 / 粉丝团 | `liveMan.py` + `data_recorder.py`（与 `auto_crawl.py` 生产链路同源） |
| 图像 | Playwright 截 `video` 元素 |
| 音频 | HTML 解析 `flv_pull_url` → ffmpeg 定长切片 |
| 商品 | **灰豚**（直播结束后下载），代码侧明确不采集 |

**去掉了商品提取**：不再做面板点击（5 次 × 8s 重试对直播页有反爬风险且挤占采集时间）。
实测同房间 90s 采集，总耗时从 **4.1 分钟降到 3.1 分钟**。

**端到端验证**（与辉同行 90s）：四模态全部通过 ——
弹幕/事件/粉丝团时序正常（占比 0.4375→0.5039）、9 帧 md5 全不同、
3 段音频 30.0s/64.6kbps 且 mean_volume −19.3~−20.2 dB（非静音）。

### 归档：新建 `legacy/` 目录

移到 `legacy/`（含 `legacy/README.md` 逐文件说明归档原因）：

| 类别 | 文件 |
|---|---|
| 被取代的采集入口 | `collect_all.py`、`collect_concurrent.py`、`product_extractor.py` |
| 历史备份（纯冗余） | `liveMan_before_server.py`（与当前差 **393 行**）、`auto_crawl_before_server.py`（仅差 4 行） |
| 一次性排查脚本 | `capture_purchase.py`、`observe_purchase.py`、`verify_0922.py` |
| 非采集逻辑 | `load_test.py`（压测线） |
| 旧测试 | `test/test_live.py`、`test/test_observe.py`、`test/test_concurrent.py`、`test/test_record.py` |

归档为**纯移动，未改任何代码内容**（保持可考证）。README 注明：这些脚本的导入是相对仓库
根写的，在本目录直接运行需自行补 `sys.path`。

> `collect_concurrent.py` 归档时顺带记录：其内置主播表**有错** —— 与辉同行写成
> `646454278948`，正确值是 `56697889278`。

### 文档同步

| 文件 | 改动 |
|---|---|
| `CLAUDE.md` | 运行方式改为 `collect_full.py` / `auto_crawl.py` / `validate_fullmodal.py`（原 `python test_live.py` 已失效——该脚本已归档）；新增「文件布局（当前链路）」小节，含 `legacy/`、`test/`、`documents/`、`modify.md` 的定位说明 |
| `liveMan.py` `get_product_detail` | docstring 注明商品已改灰豚、不要再走 Playwright 路线（原 `product_extractor` 已归档） |
| `legacy/README.md` | 新建：当前链路对照表 + 逐文件归档原因 + 注意事项 |

`.gitignore` **未加** `legacy/` —— 归档代码值得留在仓库（与 `modify.md`/`analysis/` 这类本地工作文档不同）。

### 根目录现状（15 → 6 个 .py）

`liveMan.py` / `data_recorder.py` / `auto_crawl.py` / `collect_full.py` /
`media_collector.py` / `metrics.py` / `save_auth.py`

## 2026-09-28（续10）：音视频采集频率修正 —— 16kHz 无损 WAV + 音频/抽帧解耦

### 背景

结合 MEMF-GEF（VideoMAE / Wav2Vec 2.0）与 LiveLens（语速 Spearman ρ=0.928、音调、
情绪、动作幅度）的研究方向核查 `collect_full.py` 的音视频采集口径，发现两处「喂不进模型」的问题：

1. **音频封装不符**：`-ar 22050` + mp3（64k 有损）。Wav2Vec 2.0 输入为 **16kHz**；且 mp3
   有损会伤害 LiveLens 的**音调/情绪**两个专家特征。
2. **音频与抽帧串行阻塞**：音频 ffmpeg 在采集循环里 `proc.communicate()` 阻塞 ~30s，
   导致抽帧在音频窗口停摆、且 30s 段之间产生 ~20s 空档。语速=词数/时长、音调=时变统计，
   间隙会直接破坏这两个特征。

### 改动（`collect_full.py`）

**`_browser_collect` 重写音频路**：

- 音频改为 **ffmpeg 单进程后台持续切片**：一次连接、`segment` muxer 连续输出 30s 段，
  `-ac 1 -ar 16000 -acodec pcm_s16le`（16kHz 单声道无损 WAV），段间无间隙。
- 抽帧循环独立按 `frame_interval` 进行，不再被音频阻塞；音频进程在循环外启动、
  收尾时 `communicate()` 等它跑满 `duration` 自停。
- 音频段在收尾时按 `audio_dir/segment_*.wav` 回捞（过滤 0 字节），`audio_segments` 返回绝对路径。

**模块 docstring** 同步：`音频片段（mp3 30s/段）` → `WAV 16kHz 单声道，30s/段连续`。

### 保留不变

- `frame_interval=10`（1 帧/10s）默认值未动 —— 这是「图像模态（ResNet50 静态外观）」档，够用。
  视频模态（VideoMAE）需**按时间片抽 8s@2fps 密集 clip**、LiveLens 动作幅度需 1~5fps，属后续可选增强，本次未改。
- `media_collector.py` 的 `capture()`（独立类入口）未改，仍保留原 mp3 口径；如需统一可后续对齐。

### 影响

- 一场 2h：音频 16kHz/16bit 单声道 ≈ 230MB（较原 64k mp3 略大，换取无损 + 语速/音调可用）。
- 抽帧不再漏帧，音频无间隙，两条素材均可直接喂 Wav2Vec 2.0 / librosa-OpenSMILE。

## 2026-09-28（续11）：续10 音频改动的实测核对 + 校验器对齐

对续10 的音频改动做独立实测复核，并修掉该改动遗漏的一处连带影响。

### 实测核对（与辉同行，60s，`audio_segment=20`）

续10 声称的三点**全部证实**：

| 声称 | 实测 |
|---|---|
| 16kHz 单声道无损 WAV | `pcm_s16le / 16000 Hz / 1 ch / 16 bit`，256031 bps（=16000×16×1，理论值）✅ |
| 段间无间隙、定长 | 20.0096 / 20.0107 / 19.9797 s，**目标 20s、误差 ≤ 11ms** ✅ |
| 音频/抽帧解耦，抽帧不再停摆 | 帧间隔 38→43→48→53→**59**→04→09→14→19→24→29→34，仅 1 处 6s（单次截图偏慢），**不再出现原 ~20s 停摆** ✅ |
| 音频非静音 | mean_volume −16.7 ~ −18.1 dB ✅ |

### 排除一个长跑隐患（`stderr=PIPE` 死锁）

初看 `stdout=PIPE, stderr=PIPE` 只在收尾 `communicate()`，担心 2h 运行中 ffmpeg 持续写 stderr
把 64KB 管道缓冲写满而**死锁**。实测：**健康直播流 60s 内 stderr = 0 字节**（`-loglevel error`
已足够安静），需 ~600 条错误行才可能填满 —— 那种情况下音频本身已废。**结论：非隐患，不改。**

### 修掉续10 遗漏的连带影响

| 文件 | 问题 | 修复 |
|---|---|---|
| `test/validate_fullmodal.py` | 音频 glob 仍是 `*.mp3` → 格式改 WAV 后会**误报「❌ 无音频片段」** | 同时 glob `*.wav` + `*.mp3`（兼容旧数据），标题与 docstring 同步 |

两代数据均已验证通过：新 WAV 3 段 / 60s、旧 mp3 3 段 / 90s，静音段均为 0。

### 留待后续（未改）

- `media_collector.py` 的 `capture()` 仍是 mp3/22050 口径（续10 已注明）—— 与 `collect_full.py` 的
  音频口径**不一致**，若将来直接用该类入口会产出不合 Wav2Vec 输入的音频。
- `collect_full.py` 的 ffmpeg 未加 `-nostdin`：`create_subprocess_exec` 默认继承父进程 stdin，
  ffmpeg 会尝试读 stdin 取键盘指令，长跑时可能吞掉终端输入。加一个参数即可硬化（低危）。
- 视频模态仍未按 VideoMAE/LiveLens 需求密集抽帧（续10 已注明）。

## 2026-09-28（续12）：音频口径统一（media_collector）+ ffmpeg stdin 硬化

落实续11 遗留清单的第 1、2 项。

### 改动

| 文件 | 改动 |
|---|---|
| `media_collector.py` `_capture_audio_segment` | 音频口径由 `22050Hz mp3 64k` 统一为 **16kHz 单声道无损 WAV**（`pcm_s16le`），输出 `segment_%03d.wav`；加 `-nostdin`；docstring 注明口径变更原因（Wav2Vec 2.0 需 16kHz、mp3 有损伤 LiveLens 音调/情绪特征） |
| `media_collector.py` 模块 docstring | 音频描述同步为「16kHz 单声道无损 WAV」 |
| `collect_full.py` | ffmpeg 命令加 `-nostdin`（`create_subprocess_exec` 默认继承父进程 stdin，ffmpeg 会读 stdin 取键盘指令，长跑可能吞掉终端输入） |

### 验证：`media_collector.capture()` 新口径实测（与辉同行 60s）

产物格式正确：`pcm_s16le / 16000 Hz / 1 ch / 16 bit`，256031 bps（=16000×16×1 理论值），
mean_volume −17.0 ~ −17.4 dB（非静音），3 段。

### ⚠️ 但发现该入口仍有**续10 修掉的同类问题**，且比预想严重

对两条路的音频做「内容时长 vs 段间 mtime 间隔」对比，暴露 `media_collector.capture()` 的
**时间轴丢采**：

| 入口 | 每段内容时长 | 段间 mtime 间隔 | 时间轴覆盖率 |
|---|---|---|---|
| `collect_full.py`（单进程 `segment` muxer） | 20.01s | **19.99s** | **≈100%（无缝）** |
| `media_collector.capture()`（逐段重连） | 19.88s | **37.99s** | **≈52%（每轮丢 ~18s）** |

**成因**：`capture()` 的采集循环里 `_capture_audio_segment()` 会 `await proc.communicate()` **阻塞**
约 20s，而循环的 `elapsed` 仍是每轮 `+frame_interval` 递增 —— 计数与墙钟脱钩。于是音频调用之间
实际间隔 ~38s，每轮只录到 20s，**近一半音频时间轴从未被采集**。这正是续10 在
`collect_full.py` 中已修复的问题（改为单进程后台持续切片 + 抽帧解耦），`media_collector` 未同步。

**结论**：本次只统一了**封装格式**，未统一**切片机制**。两条路仍然不等价 ——
**建数据集请用 `collect_full.py`**；`media_collector.capture()` 仅适合"只要少量素材、不在意时间轴连续"的场景。
若要彻底对齐，需把 `capture()` 的音频路也改成单进程 `segment` muxer（与续10 同样的改法）。

> 另注：`media_collector.capture()` 是**纯媒体入口**，不做 WS 采集、不写 `room.json`/`danmaku.jsonl`，
> 其产物目录只有 `audio/` 与 `frames/`。

## 2026-09-28（续13）：归档 media_collector.py + 方法迁入 collect_full.py

落实续12 的结论 —— `media_collector.capture()` 那条入口留着就是个陷阱（谁用谁丢约一半音频）。

### 迁移内容

`collect_full.py` 对 `media_collector` 的依赖只有两处，已内联进 `collect_full.py`：

| 迁移项 | 说明 |
|---|---|
| `STREAM_URL_KEYWORDS` | 常量原样迁入 |
| `MediaCollector._extract_stream_urls_from_html()` | 转为模块级函数 `extract_stream_urls_from_html()` —— **代码逻辑未改，仅去掉类包装**，docstring 注明来源 |

`collect_full.py` 的 `from media_collector import ...` 已移除；docstring 的设计要点同步更新
（原来写「复用 media_collector 的流地址解析」，现改为「流地址由本模块解析」）。
`media_collector.py` 整体移入 `legacy/`。

### 验证（与辉同行 60s，`audio_segment=20`）

**四模态全部通过**，且音频保持续10 的无缝特性，证明迁移未破坏任何逻辑：

| 项 | 实测 |
|---|---|
| 音频编码 | `pcm_s16le / 16000 Hz / 1 ch` ✅ |
| 音频连续性 | 内容合计 60.00s，段间 mtime 间隔 `[20.04, 20.01]` → **无缝**（对比 `media_collector` 旧路的 37.99s 间隔 ≈52% 覆盖率） |
| 流地址解析 | 4 个（`extract_stream_urls_from_html` 工作正常）✅ |
| 图像 | 6 帧，md5 全不同 ✅ |
| 文本/数值 | 弹幕/事件/粉丝团时序正常 ✅ |

### 文档同步

| 文件 | 改动 |
|---|---|
| `legacy/README.md` | 归档清单新增 `media_collector.py` 条目（含其两个问题的说明）；「当前链路」表更新为「图像/音频 = `collect_full.py` 内置」；注明根目录只剩 6 个 .py |
| `CLAUDE.md` | 文件布局表更新（去掉 `media_collector.py`，音视频归入 `collect_full.py`）；补音频口径说明「统一 16kHz 单声道无损 WAV，不要再用 mp3」 |

### 根目录现状（6 个 .py）

`liveMan.py` / `data_recorder.py` / `auto_crawl.py` / `collect_full.py` / `metrics.py` / `save_auth.py`
—— 音视频逻辑已全部内联在 `collect_full.py`，**不再有独立的媒体采集模块**。
