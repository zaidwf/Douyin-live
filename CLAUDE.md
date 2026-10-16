# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

抖音直播间实时数据采集工具，用于 MEMF-GEF 直播销量预测研究的多模态数据采集。通过连接抖音 WebSocket 服务器，捕获弹幕、点赞、用户进出、关注/分享、粉丝团、商品成交以及直播间统计信息。`liveMan.py` 仍保留基于 Tkinter 的图形化界面（按类别分组的日志面板），但当前主用途是**无人值守采集链路**。

## 运行方式

```bash
pip install -r requirements.txt

python collect_full.py 56697889278 600   # 全模态单场采集（文本/数值/图像/音频）
python auto_crawl.py anchor.xlsx 60      # 生产守护：多主播轮播长跑（默认不含音视频）
python auto_crawl.py anchor.xlsx 60 --media --max-media 5   # 同上 + 音视频，同时最多 5 路 ffmpeg
python auto_crawl.py documents/huitun_10_100w_1015_final.xlsx 60 \
    --media --max-media 5 --min-free-gb 5                   # 小规模全模态验证
python auto_crawl.py documents/huitun_10_100w_1015_final.xlsx 60 \
    --media --max-sessions 10 --max-media 10 --min-free-gb 5  # ★ 服务器生产：只并发采 10 个
python auto_crawl.py anchor.xlsx 60 --ws-reconnect 0    # 关掉同场次重连（退回「断一次=一场」）
python auto_crawl.py documents/并发测试.xlsx 60 --sheet 40  # 用并发测试名单的第 40 档
python test/validate_fullmodal.py        # 校验采集产物真实性（默认取最新 data/**/live_*）
python liveMan.py                        # 图形界面（调试用）

python load_test.py --mode v8 --anchors 100            # 签名路径开销（离线）
python load_test.py --mode pump --anchors 100 --duration 600   # 落盘链路承压（离线）
python test/run_ladder.py --duration 600               # 20/40/60/80/100 阶梯真实并发
python test/run_ladder.py --mode media --tiers 1 2 4 8 16 --steady 90   # 音视频阶梯（带宽是瓶颈）
python test/summarize_ladder.py --mode media            # 出音视频汇总表 + 可持续上限判定
python test/test_media_stop_race_unit.py                # 音视频收尾不得漏 ffmpeg 孤儿 + 合并命令形状
python test/test_proxy_unit.py                          # 控制面代理的解析与注入形状（离线）
python test/test_disk_guard_unit.py                     # 全模态磁盘保护：放行/拦住/fail-open（离线）
python test/test_health_check_unit.py                   # 产出体检判据：down/stale/quiet（离线）

python service/health_check.py                          # 体检一次（退出码 0 健康 / 1 down|stale）
python service/health_check.py --probe 12 --stale-min 60  # 探 12 个房间、产出阈值 60 分钟
python test/test_risk444_unit.py                        # 444 风控判定：真命中认得、假阳性排除（离线）

python service/live_ledger.py                           # 开播台账：探全部主播 + 出迁移清单 xlsx
python service/live_ledger.py --no-probe                # 只重建表格，不探测（改标注时用）
python service/live_ledger.py --limit 10                # 只探前 10 个（冒烟）
python service/live_ledger.py --no-probe --matrix-days 7  # 开播矩阵出最近 7 天（0 = 关）
# ⚠️ 手动跑要带 `--max-sessions 10`（与 timer 同值），否则 `★疑似漏采` 假报，见下
python service/live_ledger.py --no-probe --delete-incomplete           # 干跑：只打印要删什么
python service/live_ledger.py --no-probe --delete-incomplete --apply   # 真删（默认只删无音视频的）
python service/live_ledger.py --no-probe --delete-migrated --apply     # 真删已迁走的
python service/live_ledger.py --usable-csv ''                         # 不生成待迁移清单 CSV
python test/test_stall_watchdog_unit.py                 # WS 僵死判据 + session_meta.json（离线）
python test/test_ws_reconnect_unit.py                   # WS 断线同场次重连 + 默认关不变量（离线）
python test/test_session_gate_unit.py                   # 会话闸门：抢不到名额就完全不采（离线）
python test/test_ledger_unit.py                         # 台账：完整性判定/人工列/删除保护（离线）

python service/pull_sessions.py --dry-run               # 自动传输：只说要拉什么/能删什么（不归档/不拉/不删）
python service/pull_sessions.py --selftest              # 自检（含远端 --dump-ledger 可用性；不过就不该开删）
python service/pull_sessions.py --keep-remote --limit 3 # 小范围试：拉回来但**不删**服务器那份
python service/pull_sessions.py --limit 0               # 拉完积压 + 真删（文本→本机；整场此前已到 COS）
python service/pull_sessions.py --no-cos --limit 0      # 本轮不让服务器归档（服务器自己挂 timer 时用）
python test/test_pull_sessions_unit.py                  # 选场/三道门禁/校验拒绝删除/manifest 幂等（离线）

# ↓ 下面五条在**服务器上**跑（`cd /opt/douyin-live`，加 `sudo -n`）；单测在本地跑
./venv/bin/python service/cos_archive.py --selftest     # ★ 归档到 COS，启用前必做（真发一个对象）
./venv/bin/python service/cos_archive.py --dry-run      # 只看要传什么（走内网，18 GB 约 6 分钟）
./venv/bin/python service/cos_archive.py                # 全部打包上传（**不删**服务器那份）
./venv/bin/python service/cos_archive.py --delete-after-upload   # ★传完即删服务器那份（日常不用）
./venv/bin/python service/cos_archive.py --dump-ledger -         # ★只读：账本 + 逐条活 HEAD → object_present
python test/test_cos_archive_unit.py                    # 签名/凭据不回显/选场/tar 校验/契约（离线）

python service/reclaim_check.py                        # 回收链路体检一次（0 健康 / 1 有告警）
python service/reclaim_check.py --min-free-gb 15       # 换磁盘告警线（默认 10 GB）
python test/test_reclaim_check_unit.py                 # 三条判据的真值表 + 心跳契约（离线）

# ↓ 三条自动触发（2026-10-16 起，见「两条腿」那节的 ★）
sudo cp /opt/douyin-live/service/douyin-archive.{service,timer} /etc/systemd/system/
sudo cp /opt/douyin-live/service/douyin-reclaimcheck.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now douyin-archive.timer douyin-reclaimcheck.timer
schtasks /Create /TN "DouyinPull" /SC MINUTE /MO 30 /F /TR "\"<python.exe>\" \"<repo>\service\pull_sessions.py\" --no-cos --quiet"
#    ↑ 笔记本这条**必须带 `--no-cos`**（否则和服务器 timer 抢锁空转），
#      且装完必须关掉电池限制（默认「用电池时不启动」，笔记本一拔电源链路就断）

bash service/pack.sh                                    # 按 service/MANIFEST 打服务器部署包
python service/run_5anchors.py --anchors 5 --duration 600   # 5 主播并发全模态实测编排器
```

**⚠️ 会话并发（`--max-sessions`，2026-10-16 起的生产策略，默认 0 = 不限）**：
「**同时在采的主播数**」钉在 N，抢不到名额的主播**这一轮完全不采** —— 不建场次目录、
不连 WS、不落任何数据，只保留开播状态探测。名额**先到先得**，空出来补给下一个在播的。
**语义与 `--max-media` 相反**（那个是排队等），两者共用 `_MediaGate`，策略在调用方。
闸门卡在**确认开播之后**（没开播的不占名额）且**卡在 `DataRecorder(` 之前**（否则
「完全不采」会退化成「采一个空目录」），名额在**整场收尾之后**的 `finally` 里才还。
生产用 `--max-sessions 10 --max-media 10`，此时 `--max-media` 永远轮不到阻塞，
留着只是「有人把 `--max-sessions` 调大」时的兜底。上限由**内存**定（新机单路 111 MB，
10 路满载 `available` 536 MB，**无 swap**）。两个配套后果：台账的 `★疑似漏采` 口径
跟着改了（见下），且 `douyin-ledger` 每 10 分钟成了其余主播**唯一的开播记录**
（这也是 timer 从 30 分钟改到 10 分钟的原因）。
⚠️ `douyin-crawl.service` 与 `douyin-ledger.service` 的 `--max-sessions` **必须同值**
（`test/test_session_gate_unit.py` 有断言钉住）。

**⚠️ `--media` 必须配 `--max-media`**：`auto_crawl.py` 是「一主播一线程」，媒体在主播线程内
联锁采完整场，**没有天然上限** —— 名单里有几个在播就同时起几路 ffmpeg（157 人名单上量
时会瞬间起满）。`--max-media N` 是进程级信号量，**阻塞等待**而非跳过（跳过会让场次静默
降级成纯文本）。释放点在 `capture.stop_media()` **之后**（ffmpeg 是在那里才收掉的）。
不传 `--max-media` = 不限流 = 改动前行为，只适合 ≤10 人名单。

**⚠️ `--media` 还要配 `--min-free-gb`（默认 5）**：全模态是**磁盘大头** —— 实测单路一场
600s = 34 MB（帧 14 MB + 音频 20 MB）→ **≈ 4.9 GB/天/路**，40G 盘一两天写满；
而**文本+数值每场只占 ~200 KB**。所以剩余空间低于阈值就不起新 ffmpeg、那场降级为
纯文本，空间回收后自动恢复。两道闸门语义不同：并发闸门是**排队等**（阻塞），磁盘闸门是
**直接不起**（降级）；磁盘检查在**取并发名额之前**，避免白占名额。度量失败时 **fail-open**
（放行 + 告警）—— 「因为量不出来而永久停采」比「磁盘可能写满」更难发现。0 = 关闭。

**⚠️ 磁盘实测量级：≈ 68.6 GB/天（2026-10-16 新机实测，比早期估值高一个档）。**
演进过程值得记一下，因为**每一版都比上一版更悲观**：早期按单房间 34 MB/600s 外推得
4.9 GB/天/路（5 路 ≈ 24 GB/天）→ 旧机实测 **≈ 33 GB/天**（1.4~1.5 GB/小时，`du`/`df`
双窗口核对）→ **新机再测 ≈ 2.86 GB/小时 ≈ 68.6 GB/天**（5 分钟窗口 244 MB）。
差异来自**实测 265 KB/帧**（原始分辨率 `-q:v 2` JPEG），而文档里一直按 25 KB 估 —— 
**低估了 10 倍**。构成：音频 ≈ 1.18 GB/时（30 秒段 × 32 KB/s），帧 ≈ 1.7 GB/时。
运营含义：40G 盘从 29 GB 可用掉到 5 GB 阈值只有 **约 8.4 小时**，之后守护进程自动降级为
纯文本。**`--min-free-gb` 只能保证「不写满」，不能保证「能一直采全模态」** —— 要长期
采全模态得清 `data/`、降帧率/画质，或扩盘。体检会把剩余空间报出来（不改退出码，理由见
`service/health_check.py` 顶部）。

**⚠️ ★ 服务器磁盘靠「文本→笔记本 + 整场→COS」两条腿回收（2026-10-16 定案并实测）。**
公网出口被腾讯云轻量带宽包硬卡在 **≈ 0.33 MiB/s（2.76 Mbps）**：并发**不叠加**
（2/4/8 路总量一样），而**入口 4.2 MiB/s、同一条链路同一时刻快 12 倍**（排除网卡/load/CPU）。
产能 **≈ 28 GiB/天 vs 流入 68.6 GB/天 → 整场走公网永远追不上（需 59.5 小时/天）**。
但**顶层文本只占全库 0.5%**（85.2 MB / 17 GB）→ 约 **0.35 GB/天 ≈ 18 分钟/天**，公网**拉得动**。
所以按形态分开走：**顶层文本 → 笔记本（公网 rsync）**、**整场打包 → COS（内网）**。
拆分的依据就是这个实测构成：**frames 61% / audio 38% / 文本+数值 0.6%**。
`--bwlimit` 仍是兜底（从来没起过作用，瓶颈在带宽包不在它）；传输**不影响采集**（采集全是入向）。
详见 `service/README.md` 第四节末尾与 `service/pull_sessions.py` 的 `DEFAULT_BWLIMIT` 注释。

**★ 2026-10-16：带宽包只卡公网，走内网的 COS 完全没被卡 —— `service/cos_archive.py`。**
同一个 bucket、同一进程、同一时刻：内网单对象 **58.68 MB/s**（公网的 **178 倍**）。
但**小文件只有 2.83 MB/s**（256 KB 的 PUT 有 ≈90 ms 往返开销，瓶颈是**请求数**不是带宽），
所以工具**把每场打成一个 tar 再传** —— 真场次 469.8 MB 实测 **59.4 MB/s**，18 GB 积压
**约 5 分钟**（公网那条路要 13 小时）。**零依赖**：COS V5 签名用 `hashlib`/`hmac` 手写，
部署仍然只是拷文件（**别改成引 `cos-python-sdk-v5`**，单测有断言钉住）。
**`--dump-ledger -`** 是**只读**模式：账本 + **逐条活 HEAD** → `object_present`，
是笔记本允许删除的**唯一正向证据**（⚠️ **`cos_archive` 自己的退出码不可采信**：
`--only` 打错、该场被跳过、`EXIT_LOCKED(3)`（上一轮还在跑、什么都没做）**都静默返回 0**，
而且它的 HEAD 复查在 `if not todo: return` **之后** —— 恰恰在「全都已归档」这个最需要
确认对象还在的场景里一次 HEAD 都不做）。默认**不删**服务器那份；日常删除由**笔记本
门禁通过后**发起（`--delete-after-upload` 反而要显式加，且别和本工具同跑）。

**★ 两条腿的分工与三道删除门禁（2026-10-16 实测：一轮删 91 场 / 18.21 GB，
根盘可用 14G→31G、`data/` 18G→1.2G）**：`service/pull_sessions.py`（**笔记本侧，
不进部署包**）只处理 `completeness == '完整'` 的场次 —— 采集中/不完整/全空**不拉也不删**
（全空场次的回收归 `live_ledger --delete-incomplete` 的既有职责）。落地目录**只有顶层 8 种
文本 + `_local_cos_media.json` 指针**（媒体留在 COS，按指针里的 `key` 取）；
`rsync` 用 **`--files-from --from0 --no-relative` 且无 `-r`**（不加 `--files-from` 的 `-r`
会把 `audio/` 拉进来，真机验过）。删除要**同时**过三道：
① **文本腿** `text_manifest(服务器清单) == text_manifest(本地目录)`
（复用 `ll.manifest_digest()`，**不在笔记本上另写一份哈希**）；
② **COS 腿的活证据**：`object_present is True` **且** `bytes/files` 与**此刻**磁盘相等
**且** key 相符（只读账本不行 —— `verify` 只是**过去某次上传的回执**）；
③ **TOCTOU**：删前**重取一次 `--dump-facts`** 重算 ②，任何一场对不上就**整批不删**；
且删除清单每行带期望列 `rel\t字节\t文件数`，服务器**自己重扫后复核**，不符**整批拒绝**。
**顶层出现未知文件名 = fail-closed**（该场不拉不删）。⚠️ **绝不**把未知文件从**两侧清单
一起滤掉** —— 那样文本腿与 COS 腿都会通过，服务器那份就**带着它被删了**。
删除走 `live_ledger --delete-list`（墓碑 label **`已传输+COS`**）→ 不制造假漏采。
⚠️ **「笔记本 → COS 自动取媒体」那一段还没做**：现在是「服务器 → COS」单向通，
笔记本只拿顶层文本 + 指针。
⚠️ **手动跑 `live_ledger.py` 时必须带上 timer 的同款参数**（`--max-sessions 10`）——
漏了它 `session_cap` 变 0，名额占满的轮次不再被排除，`★疑似漏采` 会**假报**一批
（2026-10-16 实测：漏参数报 17、补上报 0）。

**★ 2026-10-16 起整条回收链路自动跑（方案「A 双机分工」）**。此前三步全手动，15:13 之后
没人碰，**4 小时就积压 180 场 / 12.6 GB**，根盘只剩 19 G 可用（约 6.6 小时触到
`--min-free-gb 5` 的降级线）。自动化按**必须在哪台机器上做**拆成两半：

| 跑在哪 | 装什么 | 周期 | 覆盖 |
|---|---|---|---|
| 服务器 | `douyin-archive.timer`（**在部署包里**） | 1 小时 | ① 归档到 COS（**不删**） |
| 笔记本 | Windows 计划任务 `DouyinPull` | 30 分钟 | ② 拉顶层文本 + ③ 门禁通过后删 |
| 服务器 | `douyin-reclaimcheck.timer`（**在部署包里**） | 30 分钟 | **体检上面两条腿**（见下） |

**分工不是随便定的**：① 走内网 59 MB/s、公网只有 0.33 MB/s，**只有它必须在服务器上做**；
而**删除必须由笔记本发起**（三道门禁只在笔记本上，`pull_sessions.py` 里
`assert '--delete-after-upload' not in cmd` 明令禁止服务器自删）。
两个后果要记住：**归档 timer 跑得再勤也不省磁盘**（它不删，磁盘回收天然依赖笔记本那台
还开着，笔记本关机期间 `data/` 照涨 —— 长假期回来要手动补一次
`pull_sessions.py --limit 0`）；**笔记本那条必须带 `--no-cos`**，否则它和服务器 timer
抢同一个 `cos_archive` 锁、每轮撞锁空转（`EXIT_LOCKED(3)`）—— `--no-cos` 只关「写」
（归档），**COS 账本照读**，删除门禁一位不少。
⚠️ **笔记本侧是 `Interactive only`**（`schtasks` 默认），**重启后要有人登录任务才会跑**；
改成「不管是否登录都跑」得存密码或 S4U，而 S4U 读不到用户目录下的 SSH 密钥，反而更糟。
⚠️ **仍然不要拿退出码做告警**：`cos_archive` 的 `0` 不证明数据在 COS（只有
`--dump-ledger -` 的逐条活 HEAD 是证据），`pull_sessions` 的 `3`（撞锁）与 `1`（有场次
失败）在正常轮次里也会出现 —— 看 `service/dist/pull_sessions.log` 的
「本轮结束：成功 N、失败 M」和 `journalctl -u douyin-archive`。
首次两轮实测：服务器首轮归档 4 场 / 0 失败 / 26 秒；笔记本首轮「成功 5、失败 0」，
其中**1 场被 COS 门禁正确拦下**（20:04 归档之后才结束的场次，COS 里还没有它 → 保留
服务器那份）—— 门禁在真实轮次里做了一次正确拦截，链路与保护都验过了。

**★ 回收链路体检 `service/reclaim_check.py` + `douyin-reclaimcheck.timer`（30 分钟）**：
自动化的危险形态不是「报错」而是「**每一步都看起来正常**」—— 磁盘一路涨到
`--min-free-gb 5` 悄悄把全模态降级成纯文本，没有任何东西会出声。已有的告警面各漏一块：
体检（探通道+产出）看不到「数据进没进 COS」；`★疑似漏采` 看不到；归档单元自己的退出码
**看不见「timer 压根没跑」**（被停用/没 enable 时单元永远不会 failed，`--failed` 一片干净）。
三条判据补这些（任一命中 → 退出 1；`systemctl --failed` 就是告警面）：

| 判据 | 抓的是什么 | 默认阈值 |
|---|---|---|
| ① 根盘可用空间 | **笔记本那条腿停了**（不删了）→ 磁盘只涨不落。它是两条腿的**联合**指标 —— 删除只在笔记本上做，服务器看不到它的死活，但「不删」的直接后果就是磁盘不降 | 10 GB |
| ② 归档心跳 `service/dist/cos_archive.beat` 的年龄 | 归档 timer **根本没在跑** | 150 分钟（连丢两轮） |
| ③ 账本说已归档、COS 上对象**此刻还在不在** | ★ **静默丢失**（唯一副本没了） | 无（二元） |

⚠️ **③ 非做不可**：`cos_archive` 的 HEAD 复查排在 `if not todo: return EXIT_OK` **之后**
—— 在日常的「没有新场次要传」场景里服务器**一次 HEAD 都不做**就返回 0（见上面那条）。
③ 只核对「**服务器上还在**」的场次（账本会累积，全量 HEAD 随天数线性变重；已删场次在删除
当时已当场核过）。判「对象在不在」用的是**同一个** `Cos.size_of()`，不另写一份。
⚠️ **心跳只由 `_cycle` 写**（在 `load_cos_env()` 之前，答的是「跑起来没」不是「成功没」），
`--dump-ledger` / `--selftest` **不写** —— 笔记本每 30 分钟就调一次 `--dump-ledger`，
那也写心跳的话 timer 早死了也看不出来（单测钉住）。
⚠️ 它**不替代** `--dump-ledger -`（那个是「允不允许删」的当场正向证据，fail-closed + TOCTOU），
也**不证明数据内容对**（逐字节核验在归档轮与删除门禁里）。
⚠️ 体检 timer 的 `OnBootSec=20min` **必须晚于归档的 10min**：早于它跑，每次重启都会假报
「归档心跳不存在」，报几次就没人看了（单测钉住这个次序）。

**控制面代理**（`DOUYIN_PROXY`，服务器上必配）：只覆盖控制面的 **HTTP**（4 处 `requests`），
444 打在页面/enter/ranklist 这些端点上；ffmpeg 拉 FLV **默认直连**（CDN 是另一套
域名，且 5 路 ≈ 31 Mbps 穿隧道既贵又可能撞带宽上限），留 `DOUYIN_STREAM_PROXY` 单独开。
优先级 `DOUYIN_PROXY` > `http_proxy`/`HTTP_PROXY` > 无。凭据只落 `.gitignore` 里的
`config/proxy.env`，**不进 git**。

**★ WebSocket 默认不走隧道**（2026-10-16 起，`liveMan.WS_PROXY_ENV`）：隧道在
**~6.5 分钟**处掐断长连接、**每条各自死** —— 三组对照实测（同一台服务器、同一时刻、
同一批在播主播，唯一变量是走不走隧道）：走隧道 2 条 **389.1s / 456.5s 双双被掐**
（`Connection to remote host was lost.`，死前还在收帧 702/870）；直连 2 条 **1051s 仍活**；
直连 10 条（= 生产并发）**9 条活满 1200s 上限、零 ERROR**（帧数 2186–3717），第 10 条 574.6s **正常下播**（`offline`，不是被掐）；笔记本直连 2 条活满 900s、**零 ERROR**。
同期生产（走隧道）70 分钟 **108 次** `WebSocket错误`，而 14 条直连 WS **一次 444 都没有**。
后果不只是「少采一点」，而是**一场真实直播被切成多个「完整」碎片场次**
（实测同一主播 30 分钟内 4 个场次目录）—— 台账里的「完整」是**碎片完整**。
退回旧行为：`config/proxy.env` 里加 `DOUYIN_WS_PROXY=1`，不用改代码。
（旧口径「444 也打在 WS 建连上」是 2026-10-15 的误判 —— 那天所有控制面流量都在隧道里，
把「隧道掐长连接」当成了风控。**长连接才是隧道唯一搞不定的东西**，短请求照样走它。）
两个坑仍写在 `proxy_for_ws()` 注释里：websocket-client **不认** env 代理必须显式传参，
且 1.7.0 的 `run_forever` 必须显式给 `proxy_type="http"`。

**★ WS 断线在「同一场次内」自动重连**（2026-10-16 起，`auto_crawl.WS_RECONNECT` 默认 10）：
直连只修掉了「隧道掐」**这一个**断因 —— 抖音自己、网络抖动、机房侧 RST 一样会断，而原来的
处置是**结束本场**，于是**断一次 = 一场真实直播被切成两个碎片场次**（正是上面那条的后果）。
现在重连逻辑放在 `DouyinLiveWebFetcher.start(max_reconnects=N)` **内部**：断线时重开一条
连接、继续往**同一个场次目录**写，`start()` 返回才等于本场结束 —— 于是 `auto_crawl` 里所有
「`ws_thread.is_alive()` == 本场还在采」的判断（含 `_wait_media_slot`、主等待循环）
**一行都不用改**，语义反而更准。额度是**连续**失败：某条连接活过 15 秒即清零重计
（`RECONNECT_GRACE`），所以断断续续的长直播能一直自愈，只有「一连就挂」才耗尽 ——
那通常意味着房间已下播 / 被风控。
⚠️ **默认是关的**（`max_reconnects=0`）：`collect_full.py` / GUI / `load_test.py` /
`run_ladder.py` **一行没动**，压测「断了就失败」的语义必须原样保留（`load_test` 更是拿
`f.running = False` 当**唯一**的停止手段，重连会把它吞掉）。生产由 `auto_crawl` 显式打开；
回退 `--ws-reconnect 0` 不用改代码。现场是否真在重连看 `session_meta.json` 的
`ws_reconnects`（WS 直连后实测长期为 0；持续 > 0 是**新断因**的告警信号）。

⚠️ **三个必须做对的细节**（每个都有单测钉住，写错任一个功能就整个失效）：① `start()`
**每次**连接前都要置 `running = True` —— `_wsOnOpen` 自己不置位（只起心跳线程），而
`_teardown_ws` 会把它置 False，漏了新连接握手成功但**心跳永不发出**；② 重连前必须重置
`last_frame_ts`，否则新连接刚建立就被 300 秒僵死判据判死，重连等于白做；③ `_teardown_ws`
要**先摘 `self.ws` 再 join 心跳线程** —— `_sendHeartbeat` 读的是实例属性 `self.ws`，重连后
它已指向**新** socket，旧线程醒来会往新连接发 ping 且永不退出（`stop()` 只 join 得到最后
一个 → 线程累积）。另外 `_wsOnClose` 加了 `ws is self.ws` 守卫，防旧 socket 的迟到回调把
新连接的 `running` 清掉（默认不重连路径下条件恒真，行为不变）。
**`stop_reason` 的四个值因此变了一个语义**：`ws_closed` 不再是「断过一次」，而是
**「重连额度用尽」** —— 比原来信息量大得多。

**⚠️ `active` 不等于「在干活」**（2026-10-15 青果隧道事故的教训）：隧道端点变成
`Connection refused` 后，`douyin-crawl` 每条请求都 ProxyError、一条数据都采不到，
而 `systemctl is-active` 照样 `active`。所以服务器上装了
**`service/health_check.py` + `douyin-healthcheck.timer`（每 10 分钟）**：用**和守护进程
同一条代码路径**探几个房间（覆盖 ttwid 首页 / enter / 隧道认证三条腿），再与 `data/`
的产出新鲜度交叉，退出码即判定（0 健康 / 1 `down`|`stale`），于是
`systemctl --failed` 就是告警面。

两条信号**必须交叉**：只看「有没有新产出」会在后半夜稳定误报（没人开播时没有新数据是
正常的），报几次之后就没人看了。所以「没产出」还要再问一句「采样里有没有人在播」——
有人才判 `stale`（在播却采不到 = 事故），没人判 `quiet`（正常）。

**⚠️ 体检抓不到「少数房间僵死」，得靠台账**：体检查的是「守护进程整体还活着吗」，
取样 8 个房间，所以「大部分正常、个别主播整场没在采」它会报 `ok`。2026-10-15 实测正是
这样：**16 个在播房间（占在播的 44%）实际没在采**，体检当时报健康。所以另有
**`service/live_ledger.py` + `douyin-ledger.timer`（每 10 分钟）**：探**全部** 157 人
（实测 50 秒、0 个 444），出 `dist/usable_pending.csv`（**待迁移清单** = 有真音视频且
未标「已迁移」，按完整优先/体积倒序 —— 删除口径的**补集**，判据同源）与
`dist/live_ledger.xlsx` 三组 sheet —— `场次` 是**迁移清单**
（一行一场，标完整性 + **结束原因** + 留 `是否已迁移`/`备注` 人工列，**重写时按
`场次目录` 搬回来**，所以那两列**要填文本不要填公式**）；
`开播矩阵_MMDD` 是**每天一张**的开播矩阵 —— 第一列是名单，**之后每一轮探测新增一列**，
格子里 `在播`/`没播`/`未知`，末尾两行是 `★ 在播数` 与 `★ 探测失败数`（用户要的
「每隔一段时间检测开播数量」就是它）。**矩阵只读 `live_rounds` 底账重建**（不探测），
所以历史某天随时调得出来：`--matrix-days 7` / `--matrix-days 0` 关掉。
⚠️ 矩阵里 `未知` 是**探测失败**不是「没播」，**别把三态合成两态** —— 那会把「隧道全死」
显示成「一屋子人集体下播」。
**2026-10-16 从 30 分钟改成 10 分钟**：守护进程现在并发只采 10 个主播，**其余 147 人不再
落任何数据**，台账成了他们唯一的开播状态记录，30 分钟粒度太粗。一轮探测实测约 6 分钟
（含限流），所以**不要再往下调**。
`主播` 表的 `★疑似漏采` 判的是「探到在播、但那一刻没有场次在写」——**且名额当时没满**
（**连续 ≥2 轮**才报，单轮可能是刚开播还没轮到）。**正常情况下它应长期贴近 0**。

**⚠️ 在采中的场次「结束时间/时长」是空的（2026-10-16 修的口径）**：`ended` 的三个来源
（`session_meta` / JSONL 尾条 / 最新文件 mtime）对一个**还没结束**的场次全都 ≈ 此刻，于是
表里几十行的「结束时间」都等于写表那一刻、时长恒为「开播至今」，看起来像「很多场直播
同时结束」—— 实际是它们还在写。「结束原因」那一列会写 `采集中` 而不是留白（留白会被
当成「老数据没记」，两件事的处置完全不同）。**删除门禁不看这两列**（只看
`completeness`/`bytes`/`files`），所以这个改动动不到任何删除判据。
删除入口 `--delete-incomplete` / `--delete-migrated` 默认干跑（加 `--apply`
才真删），只删 `data/<8位日期>/live_*`，**在采中的场次永不进删除列表**（判的是
`采集中`，不是 `不完整`）。

**⚠️ `★疑似漏采` 的口径跟着 10 路名额改了（2026-10-16）**：只有 10 个名额配一二十个
在播时，原判据（「探到在播但没有场次在写」）会**恒等于「在播数 − 10」**、退出码恒为 1，
把 `systemctl --failed` 这个告警面彻底淹掉。所以 `find_missed()` 多了 `session_cap`
参数（= `auto_crawl --max-sessions` 的同一个值）：`session_cap>0` 时**名额已满的轮次不计
漏采** —— 那是策略不是故障。排除之后这一列**仍然有用且判据更准**：它现在报的是
「**名额没占满，却有人在播而没在采**」，那才是真出事（WS 僵死、线程卡住、进程半死不活）。
两个已固化的细节（`test/test_ledger_unit.py` 的 `[7c]`）：① 名额是否占满按**精确**时刻
判，**不加** `COVER_MARGIN_MIN` —— 名额恰恰在 `last_write` 那一刻才还回去（见
`_one_cycle` 的 `finally`），放宽会把「名额其实空出来了」误判成「满了」而**漏报真事故**；
② 名额已满的轮次会**断掉连续计数**，所以一次真漏采最迟 20 分钟（两轮）报出来。
⚠️ 两个 service 的 `--max-sessions` **必须同值**，不一致会让这一列误报。

**⚠️ `不完整` ≠「数据没价值」，删除入口默认跳过含音视频的场次**（2026-10-15 实测的
教训）。含音视频却判不完整的，几乎都是**进程被杀**（V8 崩溃 / OOM / 重启）没写成
`room.json` —— 那天有 **5 场 / 2.73 GB**，其中一场 **1.3 GB 是当天最大场次**（669 段
音频、4011 帧、522 KB 弹幕，只是 15:04 那次 `py_mini_racer` V8 崩溃把它截断了）。
裸删会让删除总量从 **221 MB 变成 2.95 GB**。`--delete-incomplete` 因此只吃
`音频段 == 0 且 帧数 == 0` 的场次，被跳过的单独报出来（含段数/帧数），要连它们一起删
得显式加 `--include-media`。判据在 `split_by_media()`，单测固化。

**⚠️ 删除必须留「墓碑」**：`★疑似漏采` 拿**场次目录**当「那一刻在不在采」的证据，而删除
入口删的就是场次目录 —— 不留墓碑的话，删一次 `主播` 表就长一批**假漏采**、退出码变 1、
把 `systemctl --failed` 这个告警面污染掉，而 `--delete-migrated --apply` 正是**设计好的
日常动作**。2026-10-15 实测：删完 281 个场次后 `★疑似漏采` 从 **0 跳到 10**，逐个对
journald 核对（`本场结束（时长 X 分钟），数据落盘：…`）——那 10 个主播当时**都在采**，
全是假阳性。所以 `delete_sessions(apply=True)` 在 `rmtree` **之前**把场次记进
`dist/deleted_sessions.jsonl`，`main()` 再并进覆盖率判定的 `spans`（`load_tombstones`）。
**这个文件不能删**；历史墓碑可从 journald 回填，见 `test/probe_backfill_tombstones.py`。

**⚠️ WS 僵死：连接活着、抖音不再下发数据**（2026-10-15 找到的根因，比音视频问题严重）：
下播判定是**事件驱动**的（`ControlMessage status==3` → `on_offline`），而这个失效模式
恰恰不发那个事件。此时 `offline` 永不置位、`ws_thread.is_alive()` 恒为真，等待循环
**永远不退出**：这一场从挂死那刻起不再落盘，而且**该主播永远回不到探测循环 → 之后每次
开播整场漏采**。修法是 `liveMan.DouyinLiveWebFetcher.frame_age()`（距**最后一帧**的秒数，
在 `_wsOnMessage` 里刷新 —— 那是所有下行帧的唯一必经之路，心跳 `need_ack` 也走它）配上
`auto_crawl.STALL_SECONDS = 300`（挂死 = 断线重连，走**已有**收尾路径）。阈值取 300 是
实测的：健康场次 `events.jsonl` 相邻 `ts` 的**最大**间隔只有 2–16 秒，挂死是 1500+ 秒，
两个数量级余量。`--stall-sec 0` 关闭。判据必须是**距最后一帧**而不是距最后一条弹幕 ——
安静房间会几分钟没弹幕，用弹幕会误杀。

**每个场次目录里现在有 `session_meta.json`**（`auto_crawl._write_session_meta`，在
`recorder.close()` 之后写）：`started_at`/`ended_at`/`duration_s`/`stop_reason`
（`offline` 正常下播 / `ws_closed` **重连额度用尽** / `stall` 僵死 / `signal` 收到 SIGTERM）+
`ws_reconnects`/`ws_gap_s`（本场重连**次数** + 主动退避等待的累计秒数 —— ⚠️ `ws_gap_s`
**不是**「这场丢了多少秒数据」：断线发生在 `run_forever` 返回**之前**多久是测不出来的，
socket 早已死、`run_forever` 可能过几秒才返回，所以只能测我们自己的等待）+
`media.{enabled,ok,frames,audio,restarts,skip_reason,gate_wait_s}`。**这是为了不用再靠
journald 排查**（日志会轮转掉）。`skip_reason` 是关键：`gate_no_slot`（没排到名额）与
`disk_low`（磁盘降级）是**设计内**的，`start_failed`（ffmpeg 起不来）与「起了却零产出」
才是**故障** —— 混成一个「没有音视频」会让真故障淹没在噪声里。老场次没有这个文件，
读的时候要能优雅降级（`ws_reconnects`/`ws_gap_s` 同理：老场次没有这两个键，
读方要按 0 处理，不能当成「文件格式有两个版本」）。

**`get_room_status()` 的返回值语义反直觉，改判定逻辑前必读**（踩过两次，都只在实跑中
暴露）：请求**成功**时只会返回 `'正在直播'` 或 `'已结束'`；拿不到数据时返回 `'未知'`
（enter 空体，代理挂/风控静默拦截都落这里）或 `'错误'`。**`'未知'` 是失败，不是「房间
没开播」** —— 凭名字猜会以为它是「不知道」，于是把「隧道全死」判成 `✅ ok`。判据见
`health_check.classify_status()` 的注释，单测里两个方向的坑都有断言。
⚠️ `config/proxy.env` 里的值**必须带 `http://` 前缀** —— `parse_proxy` 用 `urlsplit`，
没有 scheme 会把 `user:pass@host:port` 整体误判成 host。隧道会失效/换供应商（2026-10-15
青果那条就已 `Connection refused`，换成快代理才通），换时只需改这个文件再 restart。

**⚠️ 统计风控命中只能按 `liveMan.is_risk_control_444()`，不能用子串 `"444" in line`**：
日志行里到处都是恰好含 `444` 的 user_id 与时间戳（`[ENTER] [3544429557987695]…`、
`ts=1792027354448`），子串口径会把它们全计成风控命中 —— 实测同一窗口裸子串数出 **46** 行、
真 444 是 **0** 行。判据是单一来源：`err_text()` 在 `err.response.status_code == 444` 时
打稳定标记 `[风控444]`（按状态码判，不依赖 requests 拼的英文措辞），
`is_risk_control_444()` 同时认新标记与历史日志里的旧措辞。

**音视频单路成本（2026-10-14 实测，非估算）**：默认路径是**一个 ffmpeg 进程**（合并音视
+ `-skip_frame nokey`），一路约 **0.023 核 / 162 MB RSS / 6.2 Mbps**。改动前是**两个**进程
（0.192 核 / 268 MB / 12.2 Mbps）。三个杠杆：合并（一条流只读一遍 → 带宽 ÷2）、
`-skip_frame nokey`（只解关键帧 → CPU ÷6）、抽帧频率（**不是杠杆**，实测 fps=1/10 与
fps=1/300 的带宽与 CPU 完全相同）。要 A/B 时传 `MediaCapture(merged=False)` 或在阶梯上
加 `--media-no-merge --out-tag _split`。

需要 Python 3.8+、Node.js v18+、`ffmpeg`（音视频模态；不开 `--media` 时不需要）以及 `protoc` 25.1（protobuf 编译器）。protobuf 编译生成的 `protobuf/douyin.py` 已生成并提交到仓库中（无需重新编译，`protoc.exe` 也在 `protobuf/` 下）。

**不需要 Playwright** —— 音视频所需的流地址来自 `requests` 拉到的直播间 HTML（`liveMan.fetch_live_html()` 里就有 `flv_pull_url`），实测可直接喂 ffmpeg 解码，见 `test/probe_stream_url.py`。

⚠️ **但页面端点会被验证码拦截**（2026-10-14 实测）：密集请求后 `https://live.douyin.com/<id>` 会下发**验证码中间页**（HTTP 200、6297 字节、含 `sec_sdk_build/3.5.2/captcha/index.js`、**无** `flv_pull_url`）。它是 **IP 级**的（换新 ttwid 无效、所有房间同时中招），且**只针对页面端点**——enter 接口与 WS 全程正常。因此 `MediaCapture._resolve_stream_url()` 是**页面优先 + enter 兜底**（`extract_stream_urls_from_enter`），两条来源的 `flv_pull_url` 结构与画质顺序一致，`url_index` 语义不变。不要把兜底去掉。

## 文件布局（当前链路）

| 模态 | 文件 |
|---|---|
| 文本（弹幕）/ 数值 / 粉丝团 | `liveMan.py` + `data_recorder.py`（核心，活跃维护） |
| 图像 / 音频 | **`liveMan.py` 的 `MediaCapture`**（**一个 ffmpeg 进程**同时出两路：`-skip_frame nokey` 直解 FLV 原始分辨率抽帧 **5s/帧** + `segment` 切片 **30s/段** 16kHz 单声道 WAV）；`collect_full.py` 与 `auto_crawl.py --media` 共用 |
| 全模态单场入口 | `collect_full.py` |
| 守护式多主播入口 | `auto_crawl.py` |
| 登录态 | `save_auth.py`（`config/auth.json`） |
| 资源指标 | `metrics.py` |
| 并发压测 | `load_test.py`（`pump` 离线 / `v8` 签名 / `network` 真实连接 / `media` 音视频）+ `test/run_ladder.py`（`--mode {network,media}` 阶梯驱动器）+ `test/summarize_ladder.py`（出汇总表与上限判定） |
| **商品** | **灰豚第三方平台**（直播结束后下载）——**代码侧不采集商品** |

根目录**只有 7 个 .py**（上表全部）。音频口径统一为 **16kHz 单声道无损 WAV**（`pcm_s16le`），
供 Wav2Vec 2.0 / librosa-OpenSMILE 直接使用；**不要再用 mp3 口径**。

- `legacy/` —— 已归档的旧采集逻辑（`collect_all.py`/`collect_concurrent.py`/`product_extractor.py` 及各历史备份）。**平时不用看**，见 `legacy/README.md`。
- `service/` —— **服务器部署包**：`MANIFEST`（要传哪些文件的清单，唯一真相）+ `pack.sh`（按清单打包）+ `run_5anchors.py`（5 主播并发全模态实测编排器）+ `health_check.py` + `douyin-crawl.service` + `douyin-healthcheck.{service,timer}` + `live_ledger.py` + `douyin-ledger.{service,timer}` + `cos_archive.py`（整场打包传 COS，**在包里**）+ `proxy.env.example`。**没有源码副本**（副本会漂移，仓库里 `liveMan.py` 永远只有一份），`dist/` 是产物、已 ignore。见 `service/README.md`。
  ⚠️ `pull_sessions.py`（自动传输，**笔记本侧**）**刻意不进包** —— 方向是「笔记本主动拉」，服务器永远不执行它。
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
