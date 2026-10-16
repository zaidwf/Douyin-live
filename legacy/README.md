# legacy/ —— 已归档的旧采集逻辑

> **平时不用看这个目录。** 这里的代码都已被根目录的当前链路取代，仅作历史留档。
> 需要考证"某个字段/逻辑当初是怎么写的"时再回来翻。

归档时间：**2026-09-28**。当前链路见根目录 `README.md` 与 `DEPLOY.md`。

## 当前链路（对照用）

| 模态 | 当前文件 |
|---|---|
| 文本（弹幕）/ 数值 / 粉丝团 | `liveMan.py` + `data_recorder.py` |
| 守护式多主播采集（生产入口） | `auto_crawl.py`（音视频走 `--media`，默认关） |
| 全模态单场采集 | `collect_full.py` |
| 图像 / 音频 | **`liveMan.py` 的 `MediaCapture`**（ffmpeg 原始分辨率抽帧 + ffmpeg 单进程 `segment` 切片）；`collect_full.py` 与 `auto_crawl.py --media` 共用 |
| 登录态 | `save_auth.py` |
| 资源指标 | `metrics.py` |
| 并发压测 | `load_test.py`（2026-09-29 从本目录恢复到根目录） |
| 商品 | **灰豚第三方平台**（直播结束后下载，代码不采集） |

当前根目录**只有 7 个 .py**：`liveMan.py` / `data_recorder.py` / `auto_crawl.py` /
`collect_full.py` / `metrics.py` / `save_auth.py` / `load_test.py`。
音视频逻辑集中在 `liveMan.MediaCapture`，**不再有独立的媒体采集模块**，也**不需要 Playwright**。

## 归档清单

### 被取代的采集入口

| 文件 | 原用途 | 归档原因 |
|---|---|---|
| `collect_all.py` | 单场全模态采集（主播+商品+音视频+弹幕） | 被 `collect_full.py` 取代：商品改灰豚后其商品提取逻辑已废，且内嵌了 `_parse_products_from_text` 等约 100 行死代码 |
| `collect_concurrent.py` | 多直播间并发采集 | 被 `auto_crawl.py`（守护模式）取代；且内置主播表**有错**——与辉同行写成 `646454278948`，正确值是 `56697889278` |
| `product_extractor.py` | Playwright 提取商品橱窗 | 商品信息改用灰豚（直播结束后下载）。实测面板**点击打不开**：`[data-e2e="yellowCart-container"]` 能定位，但点击后 `运费险`/`带货口碑`/`去抢购` 等面板关键词一个都不出现，10 分钟采集 `commodities=0` |
| `media_collector.py` | 音视频采集类（`MediaCollector.capture()` + `capture_media()`） | 其唯一仍在用的方法 `_extract_stream_urls_from_html` 与常量 `STREAM_URL_KEYWORDS` 先迁入 `collect_full.py`，**2026-09-29 又统一迁到 `liveMan.py`**（现名 `extract_stream_urls_from_html`，`MediaCapture` 调用）。**该类入口本身有两个问题**：① 音频逐段重连、每轮 `await proc.communicate()` 阻塞使 `elapsed` 与墙钟脱钩，**音频时间轴覆盖率仅约 52%**（每轮丢 ~18s）；② 每段独立重连导致时间轴不连续。单进程 `segment` muxer 方案已解决这两点（段间无缝、覆盖率 ≈100%）。音频口径为 **16kHz 单声道无损 WAV**（`pcm_s16le`） |

### 历史备份（纯冗余）

| 文件 | 归档原因 |
|---|---|
| `liveMan_before_server.py` | 服务器部署改造前的 `liveMan.py` 快照，与当前版本**差 393 行** |
| `auto_crawl_before_server.py` | `auto_crawl.py` 的快照，与当前版本**仅差 4 行**，纯冗余 |

### 一次性排查脚本

| 文件 | 原用途 | 归档原因 |
|---|---|---|
| `capture_purchase.py` | 购买通知原始结构观测（2026-08-30） | Bug 2（成交 promotion_id 字段）排查期的一次性脚本，结论已定案并落进 `data_recorder.py`/`modify.md` |
| `observe_purchase.py` | 同上，观测 variant | 同上 |
| `verify_0922.py` | 校验 2026-09-22 采集数据口径 | 一次性数据核查脚本，结论已落进 `modify.md` |

### type=6 口径验证（一次性探针，2026-09-29）

`type6_verification/` 下 4 个脚本 + 2 个产物目录，是为验证 `FansclubMessage.type=6`
口径而写的一次性工具。**结论：type=6 已决定不采用**（获取完整性不可测 + 到达高度爆发），
定标与舍弃理由的全过程保留在根目录 `modify.md` 2026-09-29（续）一节。

| 文件 | 原用途 |
|---|---|
| `type6_verification/probe_fansclub_type6.py` | 游客态实连，dump `WebcastFansclubMessage` 原始 wire（→ `_fc_raw/`），并记录逐 uid 时间线 |
| `type6_verification/verify_fansclub_type6_corpus.py` | 离线复核 `data/` 语料的三条判据（content 静默 / status 翻转 / ±30s 无邻近行为） |
| `type6_verification/audit_fansclub_type6_completeness.py` | 离线审计：非循环的 1→2 翻转见证检验，估计 type=6 捕获率。**结论：0 个可用翻转对，方法在本语料上无检验力** |
| `type6_verification/probe_fansclub_completeness.py` | 游客态 10min 实连完整性探针（含 `WebcastRoomRankMessage.ranks_list[].user` 批量快照） |
| `type6_verification/_fc_raw/` | 探针 dump 的原始 wire 二进制 —— **type=6 顶层无 `f3`、`f6` 仅 4B 的原始字节证据** |
| `type6_verification/_fc_audit/` | 探针产出的 `snapshots.jsonl` / `type6.jsonl` —— 与辉同行 10min 的 7093 条用户快照（含 rank 批量快照），是「0 个可用翻转对」的原始数据 |

> 本批按本目录惯例**未修改代码内容**。`ROOT` 常量是按 `test/` 的深度写的
> （`dirname(dirname(__file__))`），移到本目录后需自行补一层；脚本内的输出目录常量
> 也仍指向 `test/_fc_audit` / `test/_fc_raw`。原始语料已随脚本移入本目录。

### 文档留档

| 文件 | 说明 |
|---|---|
| `modify_20260928_full.md` | 2026-09-28 当日的**逐次完整记录**（13 次迭代、636 行）。根目录 `modify.md` 已将其精简为「当日汇总」一节（127 行）——保留全部结论、关键数据与待办，砍掉过程叙述与重复表格。**需要查当日某个中间数据/原始过程时看这份** |
| `modify_20260929_full.md` | 2026-09-29 `type=6` 章节的**逐次完整记录**（4 轮迭代、271 行）。根目录 `modify.md` 已将其精简为「精简汇总」一节（120 行）——保留全部结论、关键数据与**舍弃依据**，砍掉过程叙述与重复表格。**需要查原始 wire dump 对照、探针日志或完整翻转明细时看这份** |

### 非采集逻辑

| 文件 | 原用途 | 归档原因 |
|---|---|---|
| `load_test.py` | 并发压测（pump/v8/network 三模式） | ~~属压测线而非采集线~~ → **2026-09-29 已恢复到仓库根目录**：主播规模要从 15 扩到 100，测「单机可带几路」成了当务之急，且它依赖的 `data_recorder` / `metrics` 都在根目录（放 `legacy/` 下 `from data_recorder import ...` 必然 `ModuleNotFoundError`，根本跑不起来）。恢复时修掉了 v8/network 的若干实测性缺陷，重写程度较大，**以根目录那份为准** |
| `test/test_live.py` | 早期无界面抓取测试（2026-08-03） | 被 `test/test_field_1h.py`、`test/test_gift.py` 等取代 |
| `test/test_observe.py` | 早期观测测试（2026-08-18） | 同上 |
| `test/test_concurrent.py` | 早期并发测试（2026-08-16） | 被 `test/test_field_concurrent.py` 取代 |
| `test/test_record.py` | 早期落盘测试（2026-09-14） | 被 `test/test_fansclub_unit.py`、`test/test_gift_parse_unit.py` 等合成单测取代 |

## 注意事项

归档脚本内的 `from liveMan import ...` 等导入是**相对仓库根目录**写的。在本目录下直接运行会 `ModuleNotFoundError`，需自行补 `sys.path`：

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

归档时**未修改任何代码内容**，保持原样以便考证。
