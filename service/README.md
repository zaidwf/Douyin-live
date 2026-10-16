# service/ —— 服务器部署包

这个文件夹是**「要传到服务器的东西」的唯一入口**。`MANIFEST` 列出要传哪些文件，
`pack.sh` 按它打成一个 tar 包。

## 为什么不把源码复制一份进来

`service/` 里**没有** `liveMan.py` / `auto_crawl.py` 的副本 —— 只有清单和脚本。
副本会漂移：改了根目录那份，`service/` 那份还是旧的，而服务器上跑的是旧的那份，
现象是「本地明明修好了，服务器上还是坏的」。清单方案下源码永远只有一份。

```
service/
  MANIFEST              ★ 要传哪些文件的清单（唯一真相）
  pack.sh               按清单打包 → dist/douyin-live-<日期>.tar.gz
  run_5anchors.py       5 主播并发全模态测试的编排器
  douyin-crawl.service  systemd 守护配置（含代理 EnvironmentFile）
  proxy.env.example     代理配置模板（无凭据，可安全提交）
  dist/                 产物（tar 包与报告），已在 .gitignore 里
```

## 一、打包与上传

```bash
bash service/pack.sh                       # → service/dist/douyin-live-<日期>.tar.gz
scp service/dist/douyin-live-*.tar.gz root@<服务器IP>:/opt/
```

`pack.sh` 会做两件保护：**清单里少一个文件就报错退出**（不静默漏传），以及
**扫描包里有没有带账密的 URL**（防止代理凭据被顺手写进某个配置文件带出去）。
漏传的典型后果是 a_bogus 签名脚本没了 —— 现象是「WS 连上了但一条数据都没有」，
极难往「少传了一个文件」上想。

> 为什么不能用 `git push` 代替：`lib/` 与名单在 `.gitignore` 里，git 传不过去。
> 所以哪怕是 `git clone` 到服务器，也还得再补这些文件 —— 不如走这一个 tar 包。

## 二、服务器初始化

```bash
ssh root@<服务器IP>
mkdir -p /opt/douyin-live
tar xzf /opt/douyin-live-*.tar.gz -C /opt/douyin-live
cd /opt/douyin-live

timedatectl set-timezone Asia/Shanghai     # 不设的话目录名与时间戳差 8 小时
python3 --version                          # 需 3.8~3.11
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
# 若 mini_racer 编译失败：apt install -y build-essential python3-dev 后重试
```

## 三、配代理（**上服务器必做**）

服务器是**数据中心 IP**，抖音风控对这类 IP 的 444 拒绝率很高：实测腾讯云
「15 主播 @10s 就有 88 次 444」，而本机住宅 IP 175 路并发是 `444 = 0`。
这不是代码容量问题，**加机器解决不了**，只能换出口 IP。

```bash
# 先验代理本身通不通（这一步不过，后面全不用谈）
curl -x 'http://<用户>:<密码>@<主机>:<端口>' http://myip.ipip.net
#   出口 IP 应该是代理的，不是服务器的

mkdir -p /opt/douyin-live/config
printf 'DOUYIN_PROXY=http://<用户>:<密码>@<主机>:<端口>\n' \
    > /opt/douyin-live/config/proxy.env
chmod 600 /opt/douyin-live/config/proxy.env
```

`config/` 在 `.gitignore` 里，凭据不会进仓库；systemd 通过 service 文件里的
`EnvironmentFile=-/opt/douyin-live/config/proxy.env` 读它（行首 `-` = 文件不存在
也不报错，所以**不配代理服务照样起得来**，只是直连）。手跑时：

```bash
set -a; . config/proxy.env; set +a
```

**代理只覆盖控制面的 HTTP**（4 处 `requests`）—— 444 打在页面/enter/ranklist 这些
端点上。ffmpeg 拉 FLV 默认**直连**：那是 CDN 的另一套域名，且一路 ~6.2 Mbps，
5 路 ≈ 31 Mbps 全程穿隧道既计费又可能撞隧道带宽上限。

### ⚠️ ★ WebSocket **默认不走隧道**（2026-10-16 改，别改回去）

隧道在 **~6.5 分钟**处掐断长连接，而且**每条各自死**（不是并发压垮的）。三组对照实测
（同一台服务器、同一时刻、同一批在播主播，唯一变量是走不走隧道）：

| 组 | 条数 | 结果 |
|---|---|---|
| 走隧道 | 2 | **389.1s / 456.5s 双双被掐**，`Connection to remote host was lost.`，死前还在收帧 702/870 |
| 直连 | 2 | **1051s 仍活着** |
| **直连** | **10**（= 生产并发） | **9 条活满 1200s 上限、零 ERROR**（帧数 2186–3717）；第 10 条 574.6s **正常下播**（`offline`，不是被掐）|
| 直连 | 2（笔记本） | 活满 900s 上限，2231/2310 帧，**零 ERROR** |

同期生产（走隧道）70 分钟里 **108 次** `WebSocket错误`，而 14 条直连 WS **一次 444 都没有**。
代价不只是「少采一点」：**一场真实直播被切成多个「完整」碎片场次**
（实测同一主播 30 分钟内 4 个场次目录）—— 台账里的「完整」是**碎片完整**。

所以：WS 直连，怕 444 的**短请求**（页面/enter/ranklist）仍走隧道。
要退回「WS 也走隧道」（换供应商后 A/B）：在 `config/proxy.env` 里加一行 `DOUYIN_WS_PROXY=1`，
**不用改代码**。`test/test_proxy_unit.py` 的 `[4]` 有断言钉住「配了隧道但 WS 仍直连」这条不变量。

**★ 但直连只修掉了「隧道掐」这一个断因 —— 断线本身还在，所以另有「同场次内自动重连」。**
抖音自己、网络抖动、机房侧 RST 一样会断 WS，而原来的处置是**结束本场** → 60 秒后另开一个
场次目录 → 上面那个「一场直播被切成 4 个碎片场次」的问题会以别的形式重现。
现在重连在 `DouyinLiveWebFetcher.start(max_reconnects=N)` **内部**做：断线时重开连接、
继续往**同一个场次目录**写，`start()` 返回才等于本场结束 —— 所以 `auto_crawl` 里那些
「`ws_thread.is_alive()` == 本场还在采」的判断一行都没改。
`auto_crawl.WS_RECONNECT` 默认 **10**（**连续**失败；某条连接活过 15 秒即清零重计），
**不用在 service 文件里写**；要退回旧行为在 ExecStart 里加 `--ws-reconnect 0`。
⚠️ **默认对压测入口是关的**（`collect_full.py` / `load_test.py` / GUI 都不传参）——
「断了就失败」的语义必须保留，否则阶梯测试的失败计数会被掩盖。
判据与实测见 `test/test_ws_reconnect_unit.py` 顶部。

若实测发现**控制面正常但与音视频起不来**（CDN 也被按 IP 拦了），再单独开：

```bash
echo 'DOUYIN_STREAM_PROXY=http://<用户>:<密码>@<主机>:<端口>' \
    >> /opt/douyin-live/config/proxy.env
```

## 四、跑起来

**先手动跑几分钟确认 444 为 0**（`DEPLOY.md` 第七节的上线前 IP 体检），再挂 systemd：

```bash
./venv/bin/python auto_crawl.py anchor.xlsx 60
```

```bash
cp service/douyin-crawl.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now douyin-crawl
journalctl -u douyin-crawl -f
```

### ⚠️ 并发只采 10 个主播（2026-10-16 起的策略）

**这是当前生产的核心策略，改任何配置前先读完这一段。**

157 人名单里同时可能有一二十个在播，而 2C2G 这台机**内存只够 10 路 ffmpeg**
（单路 111 MB，满载时 `available` 只剩 536 MB，无 swap，超了就 OOM）。所以守护进程
用 `--max-sessions 10` 把「**同时在采的主播数**」钉在 10：

| | 抢不到名额时 | 结果 |
|---|---|---|
| `--max-media 10` | **排队等** | 等到就采，最坏降级成纯文本 |
| **`--max-sessions 10`** | **直接跳过** | **本轮完全不采**：不建场次目录、不连 WS、不落任何数据 |

名额**先到先得**，空出来补给下一个在播的（每轮 60 秒重探）。抢不到的那些主播**只剩
开播状态探测**，日志里会打（节流到每 30 分钟一条）：

```
[某某主播] 会话名额已满（10 路）—— 开始跳过本轮（只记开播状态，本轮不落任何数据）
```

两个配套后果，**都必须知道**：

1. **`douyin-ledger` 每 10 分钟成了其余 147 人唯一的开播记录**（这也是 timer 从 30 分钟
   改到 10 分钟的原因）。别的模态一律没有 —— 这是有意为之，不是漏采。
2. **台账的 `★疑似漏采` 口径跟着改了**：名额已满的轮次**不计漏采**（否则这一列会恒等于
   「在播数 − 10」、退出码恒为 1，把 `systemctl --failed` 这个告警面淹掉）。
   它现在报的是「**名额没占满，却有人在播而没在采**」—— 那才是真出事。
   ⚠️ 所以 `douyin-crawl.service` 和 `douyin-ledger.service` 里的 `--max-sessions`
   **必须填同一个值**，不一致会让这一列误报（`test/test_session_gate_unit.py` 有断言钉住）。

**这个策略不降低磁盘/内存压力** —— 还是 10 路媒体在写，实测仍 **≈ 68.6 GB/天**
（见 `douyin-crawl.service` 的 `--min-free-gb` 注释），40G 盘约 **8.4 小时**撞 5 GB 阈值。
要减少磁盘就得降 `--max-sessions`（少采几个），那又是另一个取舍。

**同时装上产出体检**（`douyin-crawl` 显示 `active` 不等于在采数据 —— 2026-10-15 青果隧道
那次就是进程一切正常、一条数据都采不到，空转了几个小时没人发现）：

```bash
cp service/douyin-healthcheck.service service/douyin-healthcheck.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now douyin-healthcheck.timer
systemctl start douyin-healthcheck.service          # 立刻跑一次看结果
journalctl -u douyin-healthcheck -n 20 --no-pager
systemctl --failed                                  # down / stale 会直接列在这里
```

判据（通道探查 × 产出新鲜度交叉，退出码 0 健康 / 1 `down`|`stale`）和「为什么磁盘低
只告警不改退出码」见 `service/health_check.py` 顶部注释。

**再装上开播台账**（每 10 分钟探一轮名单里**全部**主播，汇总成一张迁移清单）：

```bash
cp service/douyin-ledger.service service/douyin-ledger.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now douyin-ledger.timer
systemctl start douyin-ledger.service               # 立刻跑一次
journalctl -u douyin-ledger -n 20 --no-pager
systemctl --failed                                  # 探测全失败 / 有疑似漏采 会列在这里
```

产出在 `service/dist/`，**四份**（同一批事实的几个视图，口径同源）：

| 产出 | 内容 | 怎么用 |
|---|---|---|
| `live_ledger.xlsx` → `场次` | 一行一场直播：时长、弹幕数、音频段、帧数、模态、**完整性**、**结束原因**、原因、`名单内`、`是否已迁移`、`备注` | 总表。**迁移后在 `是否已迁移` 里标一下** |
| `live_ledger.xlsx` → `主播` | 一行一个主播：最近状态、今日在播轮数、今日场次数、今日完整场次数、**疑似漏采** | 看名单里谁漏采了 |
| `live_ledger.xlsx` → `开播矩阵_MMDD` | **每天一张**：第一列是名单，之后**每一轮探测新增一列**，格子里是 `在播`/`没播`/`未知`；末尾两行是 `★ 在播数` 和 `★ 探测失败数` | 看**开播数量随时间怎么变**、谁在哪个点开的播 |
| **`usable_pending.csv`** | **待迁移清单** = 有真音视频 **且** 未标「已迁移」，按「完整优先、体积倒序」 | 手动迁移照它拷。标完 `是否已迁移`，下一轮它自己就少一行 |

**⚠️ 开播矩阵只吃 `live_rounds` 底账，是纯读重建**（不探测、不新增采集），所以历史
某天随时能调出来：`--matrix-days 7` 出最近 7 天、`--matrix-days 0` 关掉。矩阵里**三态
分得很清** —— `未知` 是**探测失败**（隧道挂了、enter 被拦），不是「没播」；合成一档会把
「隧道全死」显示成「一屋子人集体下播」，这正是 `get_room_status()` 那个反直觉返回值
踩过的坑。

**⚠️ 在采中的场次，「结束时间」与「时长」是空的，「结束原因」写「采集中」。** 这是
2026-10-16 修的：`ended` 的三个来源（`session_meta` / JSONL 尾条 / 最新文件 mtime）对
一个还没结束的场次全都 ≈ 此刻，于是表里几十行的「结束时间」都等于写表那一刻、时长恒为
「开播至今」，看起来像「很多场直播同时结束」。**别把 `采集中` 当成缺数据** —— 它们还在写。

**⚠️ `是否已迁移` / `备注` 是人工列，请填文本、不要用公式。** 表格每 10 分钟整份重写，
这两列按 `场次目录` 搬回来（读旧表时故意不加 `data_only=True`，否则公式会被折成值）；
填公式会在下一次重写时变成静态值。

**⚠️ 台账与体检不是二选一，是两个不同的失效模式**：体检探 8 个房间看「守护还活着吗」
（快、取样），台账探全部 157 人看「有没有**整场**漏采」（慢 50 秒、全量）。
2026-10-15 实测的那次漏采是 16 个房间（占在播的 44%）—— 体检当时报的是「健康」。

清理（**默认只打印，加 `--apply` 才真删**）：

```bash
./venv/bin/python service/live_ledger.py --no-probe --delete-incomplete          # 干跑
./venv/bin/python service/live_ledger.py --no-probe --delete-incomplete --apply  # 真删
./venv/bin/python service/live_ledger.py --no-probe --delete-migrated --apply    # 回收已迁走的
```

删的粒度是整个场次目录。**三道**硬保护：只删 `data/<8位日期>/live_*`（`_load_test/`、
`_test_residue/` 天然跳过）；**在采中的场次永远不进删除列表**（它们判的是
`采集中`，不是 `不完整`）；以及 **`--delete-incomplete` 默认跳过含音视频的场次**。

**⚠️ 第三道最要紧 —— `不完整` ≠ 「数据没价值」。** 含音视频却判不完整的，几乎都是
**进程被杀**（V8 崩溃 / OOM / 重启）没来得及写 `room.json`，音视频本体是好的。
2026-10-15 实测这类有 **5 场 / 2.73 GB**，其中一场 **1.3 GB 是当天最大的场次**
（669 段音频、4011 帧、522 KB 弹幕）。按裸 `不完整` 口径删，总量会从 **221 MB 变成
2.95 GB**，且不可逆。所以默认只删「音频段与帧数都为 0」的场次；被跳过的会**单独列出来**
（含段数/帧数），确认不要了才加 `--include-media`：

```bash
./venv/bin/python service/live_ledger.py --no-probe --delete-incomplete --include-media --apply
```

**⚠️ 删掉的场次会留一条「墓碑」（`dist/deleted_sessions.jsonl`），别删这个文件。**
`★疑似漏采` 判的是「这一轮探到在播、但那一刻没有场次在写」（**且名额当时没满**，
见第四节）—— 它拿**场次目录**当证据，
而删除入口删的就是场次目录。不留墓碑的话，每删一次，那些本来被纯文本场次覆盖的时间段
就变成「没在采」，`主播` 表长出**假漏采**、退出码变 1、`systemctl --failed` 被污染。
2026-10-15 实测踩到：删完 281 个场次后 `★疑似漏采` 从 **0 跳到 10**，逐个对 journald
核对，那 10 个主播当时**都在采**，全是假阳性。墓碑只用来证明「这场曾经在采」，
不进表格、不进迁移清单、也不会被再删一次。

要开音视频得改 `ExecStart` 并**务必带 `--max-media`** —— 不带的话名单里有几个在播
就同时起几路 ffmpeg（`auto_crawl` 是「一主播一线程」，没有天然上限）。生产线上还要带
`--max-sessions`（见上一节），此时 `--max-media` 是**第二道闸**：

```bash
./venv/bin/python auto_crawl.py anchor.xlsx 60 \
    --media --max-sessions 10 --max-media 10 --min-free-gb 5
```

日常运维、故障排查、容量与风控的完整说明见仓库根目录的 **`DEPLOY.md`**
（本包也带了一份）。

### 自动传输（`service/pull_sessions.py` —— ★ **这个文件不在本包里**）

★ **2026-10-16 起是两条腿**（原因见本节末「产能问题」）：

| 数据 | 去哪 | 通路 | 实测量级 |
|---|---|---|---|
| 顶层文本/数值（8 种文件） | 笔记本 `D:\shock\Documents\data` | 公网 ssh+rsync | 顶层只占全库 **0.5%**（约 1 MB/场），85 MB 全量 ≈ **4.3 分钟** |
| **整场**（文本 **+** 音视频，每场一个 tar） | COS | 内网 | 59 MB/s、**免费**（见下一节） |

两条腿**都只处理 `completeness == '完整'` 的场次**（用户指定）。**采集中 / 不完整 /
全空**的场次**既不拉也不删** —— 归既有的 `live_ledger --delete-incomplete` /
`--delete-migrated` / 人工处置。（`不完整` 里含音视频的那批尤其别裸删，见 CLAUDE.md。）

笔记本上的布局：

```
D:\shock\Documents\data\
  20261016\live_<...>_20261016_101500\
      room.json  danmaku.jsonl  events.jsonl  series.json  series.jsonl
      fansclub_edges.jsonl  session_meta.json  ffmpeg_media.log    ← 顶层 8 种，全在这
      _local_cos_media.json      ← ★ 指针：整场在 COS 上的 key/校验值/字节数（无凭据）
  _staging\                      ← 传输中，校验通过才原子改名搬走
  _staging\_lists\<场次>.txt      ← rsync --files-from 的清单，每轮结束清理
```

**笔记本上的场次目录不再与服务器逐字节一致**：只有顶层文本 + 指针。
要媒体就按指针里的 `key` 从 COS 取（下载 tar → `tar -xf` → 与本地目录合并）。
指针**故意放进场次目录** —— 用户会「满了自己挪盘」，指针必须跟着目录走。
（下划线前缀，避免与服务器将来可能产出的同名文件撞车；`text_manifest()` 会把它
从**两侧**一起滤掉，所以它不会让校验恒判不一致。）

**方向是「笔记本拉」，服务器上不需要任何东西** —— 所以 `pull_sessions.py`
**不进 `MANIFEST`**（同 `pack.sh`：`service/` 下不是每个文件都进包）。它每轮：

1. `ssh` 让服务器跑 `cos_archive.py` —— **把未归档的整场打包传 COS**（`--no-cos` 可关）。
   归档是**写动作**，所以 `--dry-run` 会跳过它。
2. 取**逐场次事实**（`live_ledger.py --dump-facts -`）与 **COS 账本 + 逐条活 HEAD**
   （`cos_archive.py --dump-ledger -` → `object_present`）。
   完整性只有服务器那一份 `classify_session()` 判定，笔记本**不重写一份**。
3. 只挑 `完整` 且本地还没有的 → `rsync --files-from`（**只那 8 种顶层文件**）到 `_staging`；
   本地已有的走另一条路，但**本轮一样要重新校验**。
4. **校验**：`text_manifest(服务器清单)` 与 `text_manifest(本地目录)` 逐项比对
   （相对路径集合 + 每文件字节 + 总字节 + 文件数四项全等），哈希复用
   `live_ledger.manifest_digest()`，**不在笔记本上另写一份**。
5. 校验通过才写指针、`os.replace` 原子改名落地 —— 正式目录从出现的第一刻就是终态。
6. 过**三道删除门禁**（见下）→ 通过 stdin 把清单交给
   `live_ledger.py --delete-list - --apply`，由它复用 `delete_sessions()` 删掉服务器那份
   （**墓碑 label = `已传输+COS`**）。

```bash
python service/pull_sessions.py --dry-run               # 只说要拉什么、能删什么（不归档/不拉/不删）
python service/pull_sessions.py --keep-remote --limit 3 # 小范围试：拉回来但**不删**服务器那份
python service/pull_sessions.py --limit 0               # 拉完积压 + 真删
python service/pull_sessions.py --no-cos --limit 0      # 本轮不让服务器归档（服务器自己挂 timer 时用）
```

（`--selftest` 会顺带验远端 `--dump-ledger` 可用且是合法 JSON —— **未通过就绝不允许开删**。
所以部署顺序是**先服务器、后笔记本**。`--limit` **只截 `to_pull`，不截 `to_verify`**：
首轮 `--limit 3` 仍可能触发大量 `to_verify` 删除，都符合门禁，但要知道
`--limit` **不是**「最多删 3 场」。）

#### ★ 三道删除门禁（缺一不可）

删除是**唯一**破坏性动作，且它 `rmtree` 掉的是**整个场次目录**。所以：

**① 文本腿**：`text_manifest(服务器清单)` == `text_manifest(本地清单)`（上面第 4 步）。

**② COS 腿 —— 必须是活证据，不是历史回执**：

```
账本里有这场 and verify == 'ok'
and object_present is True          # ★ 本轮 --dump-ledger 刚 HEAD 过，不是账本自述
and bytes/files 与「此刻」的服务器磁盘完全相等
and key == cos_archive.session_key(rel)
```

★ **为什么不能只读账本**：`cos_archive.py` 自己就**不信账本** —— 它在轮里专门再 HEAD
一遍，理由写在注释里：账本一旦与实际不符（桶被清空 / 生命周期规则 / 传错桶 / 有人手工
删过对象），配上删除就是**静默丢失**。`verify` 只是**过去某次上传的回执**，从来没被
重新确认过。82 场 ≈ 8 秒，可以忽略。

★ **也绝不采信 `cos_archive.py` 的退出码**：`--only` 打错字、该场被跳过、甚至
`EXIT_LOCKED(3)`（上一轮还在跑，**什么都没做**）**都会静默返回 0**；而且它的 HEAD 复查
在 `if not todo: return` **之后**，恰恰在「全都已归档」这个**最需要确认对象还在**的场景里
一次 HEAD 都不做。**HEAD 直接把这个问题消掉了 —— HEAD 是活的，退出码不是。**

**③ TOCTOU 复核**：第 2 步取的事实到删除时已过去几十分钟。所以删之前**立刻重取一次**
`--dump-facts` 重算 ②；任何一场对不上 → **整批不删**。再加一道服务器侧的：删除清单
每行带期望列 `rel \t 字节 \t 文件数`，`resolve_delete_list()` 在**它自己重扫**之后用
`_dir_stat()` 复核，对不上就 fail-closed **整批拒绝** —— 这是唯一能在删除那一刻关掉
窗口的改法（向后兼容：不带列的旧清单照旧）。

**④ 未知文件名 = fail-closed**：顶层出现**不在那 8 种已知名字里**的文件 → 该场
**不拉也不删**。这比尺寸阈值更能抓住「未来版本的产物」。
⚠️ 绝**不**把未知文件从**两侧清单一起滤掉** —— 那样文本腿与 COS 腿都会通过，
服务器那份就**带着它被删了**。

**四条不可动摇的约定**（都有单测钉住，改之前先读 `test/test_pull_sessions_unit.py`）：

| 约定 | 为什么 |
|---|---|
| **本地数据永不自动删** | 那是研究语料，不是缓存。工具里没有 `rmtree`（只 `os.remove` 锁文件与 `_staging/_lists` 里的清单） |
| **校验不过就绝不删服务器那份** | 传完即删 ⇒ 本地成为唯一副本；宁可在服务器上多留一份，也不能删了才发现传残了 |
| **清单里有一条与实际不符就整体拒绝** | 部分删比不删危险得多（fail-closed） |
| **本地空间低于阈值就停拉 + 告警** | 不硬写满。用户已明确「满了我自己挪盘」，工具只负责停下来喊人 |

**与 `usable_pending.csv` / `是否已迁移` 的分工**：日常排水由本工具自动完成，
那一列不必再人工维护；它还留着是为「我另外归档到别处了」这类工具管不着的场景
（`--delete-migrated` 继续可用）。墓碑里三种 label 一眼分得开：
**`已传输+COS` = 本工具删的**（文本在笔记本、整场在 COS）、`已归档COS` =
`cos_archive --delete-after-upload` 删的、`已迁移` = 人工标了那一列删的。
⚠️ `live_ledger.load_tombstones()` 只读 `douyin_id/started/last_write`，**`reason` 不作
判据** —— 所以改 label 是安全的，不会影响 `★疑似漏采` 的抑制。

**⚠️ 传输校验与服务器侧的删除是两个独立的机器模式**，由 `live_ledger.py` 提供
（`--dump-facts` / `--manifest` / `--delete-list`），都在 `from auto_crawl import load_anchors`
**之前**短路 —— 拉取和清单都不需要名单，不该被名单解析拖累。三个模式的 **stdout 只能有
JSON**（诊断信息一律 stderr），这是最隐蔽的失败点，单测有断言。
（`cos_archive.py --dump-ledger -` 同理：该模式下强制 `_LOG_QUIET`，stdout 只有 JSON。）

#### ⚠️ ★ 公网这条路的产能问题 —— 以及为什么现在只在它上面跑「文本」

服务器**出口**被腾讯云轻量带宽包硬卡在 **≈ 0.33 MiB/s（2.76 Mbps）**：

- 单流：裸 `ssh` 0.32、`rsync` 0.30 MiB/s；
- **并发不叠加**：2/4/8 路并行，总量**一样是 0.32 MiB/s**（不是「多开几路就快」）；
- 对照面排除了一切其它解释：**入口 4.2 MiB/s（33.6 Mbps）—— 同一条链路同一时刻快 12 倍**；
  RTT 11 ms、服务器 load 0.48、rsync 只占 0.1% 核、100 Mbps 网卡只用了 2.4%。

**产能 ≈ 28 GiB/天，而流入实测 68.6 GB/天 → 整场走公网永远拉不完（需 59.5 小时/天）。**
但**顶层文本只占全库 0.5%（85.2 MB / 17 GB）** → 按 68.6 GB/天算约 **0.35 GB/天
≈ 18 分钟/天**，公网这条路**终于拉得动了**。所以 2026-10-16 起把数据按形态分开走：

- **媒体 + 整场 → COS**（内网 59 MB/s、免费，见下一节）—— 这就是「整场打包上 COS」的理由；
- **顶层文本 → 笔记本**（公网 rsync）—— 本工具**唯一的传输内容**。

三条仍然成立的推论：① **`--bwlimit` 是兜底、不是有效约束**（限流值从来没起过作用，
瓶颈在带宽包，它先到）；② **传输不影响采集**（采集全是入向，与出向不抢带宽）；
③ **传完即删现在真的能回收磁盘**了 —— 2026-10-16 实测一轮删掉 **91 场 / 18.21 GB**，
根盘可用 **14G → 31G**、`data/` **18G → 1.2G**。（拆分之前这句话是**不成立**的：
公网拉不动 ⇒ 服务器那份删不掉 ⇒ 磁盘很快重新撞 `--min-free-gb 5` 降级为纯文本。
`frames 61% / audio 38% / 文本+数值 0.6%` 这个构成就是拆分的依据。）

### 归档到 COS（`service/cos_archive.py` —— ★ **这个文件在包里**）

带宽包只卡**公网**出口。同一个 bucket、同一个进程、同一时刻，**内网出口根本没被卡**：

| 测什么 | 速率 | 对比公网 0.33 MB/s |
|---|---|---|
| 单个 200 MB 对象 | **58.68 MB/s** | 178 倍 |
| 100 × 1 MB | 9.12 MB/s（8.7 个/s） | 28 倍 |
| 400 × 256 KB（≈一帧） | 2.83 MB/s（10.8 个/s） | 8.6 倍 |
| **一个真场次 469.8 MB 打 tar 后** | **59.4 MB/s** | **180 倍** |

第三行是关键：小文件只有 2.83 MB/s，因为 58 MB/s 的链路传 256 KB 只要 4.4 ms，
而**每个 PUT 有 ≈90 ms 的往返开销** —— 瓶颈是请求数，不是带宽。所以本工具
**把每场打成一个 tar 再传**，一个大对象吃满 59 MB/s（打包本身 157 MB/s，不是瓶颈）。
18 GB 积压实测**约 5 分钟**（公网那条路要 13 小时）。

```bash
./venv/bin/python service/cos_archive.py --selftest   # 启用前必做：真发一个对象再删掉
./venv/bin/python service/cos_archive.py --dry-run    # 只看要传什么
./venv/bin/python service/cos_archive.py --limit 3    # 先传 3 场
./venv/bin/python service/cos_archive.py              # 全部（**不删**服务器那份）
./venv/bin/python service/cos_archive.py --delete-after-upload   # ★传完即删（想清楚再开；日常不用，删由笔记本门禁发起）
./venv/bin/python service/cos_archive.py --dump-ledger -         # ★只读：账本 + 逐条活 HEAD → object_present
```

COS 上的布局，与服务器 `data/` 同构：

```
<bucket>/data/<8位日期>/<场次名>.tar             场次本体（tar，**不压缩**）
<bucket>/data/<8位日期>/<场次名>.manifest.json    逐文件清单（存档那一刻的真相）
```

`.manifest.json` 是刻意存的：服务器那份一删，「这场该有哪些文件」的权威记录就只剩它了。
以后笔记本从 COS 取时，解 tar → `live_ledger.dir_manifest()` → 与它逐项比对，
**不需要服务器在线**。（2026-10-16 实测过一次完整往返：下载 → sha256 与账本一致 →
解包 → `dir_manifest` 的 digest 与服务器目录**逐字节相同**。）

**`--dump-ledger -`（笔记本的唯一正向证据）**：输出 `{dir: rec}`，其中每条 `verify=='ok'`
的记录**多一个 `object_present`** —— 用既有的 `Cos.size_of(session_key(rel)) is not None`，
与轮里那步判「对象还在不在」用的是**同一个判据**，不另造一个。它**不抢锁**（纯读），
但**需要凭据**（要 HEAD）—— 所以在 `load_cos_env()` 之后、`acquire_lock()` 之前短路。
⚠️ 因此调用它的那条 ssh **必须带 `sudo -n`**：`config/cos.env` 是 `root:root 600`，
不给 sudo 就读不到凭据，工具会 fail-closed 地把**全部对象报成「不在」**（`EXIT_ENV`），
于是**一场都删不掉**。账本不存在 → 输出 `{}` 返回 0（**不报错**）。

三条不能破的约定：

1. **完整性判定复用 `live_ledger.classify_session()`** —— 采集中**永不碰**（正在写的目录
   打出来的 tar 是残缺的，而且看起来毫无异常）。
2. **默认不删服务器那份。** 要删得显式 `--delete-after-upload`，且**只删本轮真的传上去
   并独立核验通过的**；真删走 `live_ledger.py --delete-list`，于是墓碑与两道硬保护
   （`safe_to_delete`、在采中永不删）自动继承。墓碑 label 是 `已归档COS`
   —— 与 `已传输+COS`（笔记本门禁后发起的）、`已迁移`（人工标的）在
   `deleted_sessions.jsonl` 里可分。
3. **零依赖**：COS V5 的签名就是 sha1 + HMAC-SHA1，用 `hashlib`/`hmac` 手写
   （`cos_authorization()`），HTTP 走 venv 里已有的 `requests`。**不要改成引
   `cos-python-sdk-v5`** —— 那样服务器就得 `pip install`（少一步就会有人漏掉，症状是
   「任务全失败」），而部署本该只是拷文件。单测 `[8]` 有断言钉住这一条。

**成本**：内网上传**免费**（不计公网流量）。要花钱的是**下行** —— 笔记本从 COS 取时
0.5 元/GB，按 68.6 GB/天算 ≈ **34 元/天 ≈ 1030 元/月**。腾讯云有下行流量包，批量买略便宜。
另一条要价比的路是**直接给轻量实例加带宽包**（68.6 GB/天 折合只有 **6.35 Mbps** 的持续
需求，留缓冲加到 20 Mbps 就够）—— 若月租比 1030 元低，那条路更省，而且**零代码改动**。

> **笔记本侧现在是「取指针、不取媒体」**：`pull_sessions.py` 只把顶层文本拉回本机，
> 每个落地目录里写一份 `_local_cos_media.json`（`key` / `tar_bytes` / `tar_sha256` /
> `etag` / `bytes` / `files` / `digest` / `archived_ts` / `note`，**不含任何凭据**）。
> 要看媒体时按这个 key 下载 tar（下载 → sha256 与指针比对 → `tar -xf` → 与本地目录合并）。
> ⚠️ **「笔记本 → COS 自动取」那一段仍然没做**（用户 2026-10-16 的决定是
> 「**先将数据保存到 COS，后续再按需修改**」）。

### ★ 自动触发（两台机器，2026-10-16 起 —— 用户选的「方案 A 双机分工」）

在此之前整条链路（服务器归档 / 笔记本拉文本 / 门禁通过后删）**全程手动**：
15:13 之后没人碰，**4 小时就积压 180 场 / 12.6 GB**，根盘只剩 19 G 可用（约 6.6 小时
触到 `--min-free-gb 5` 的降级线）。现在按「**这一步必须在哪台机器上做**」拆成两半：

| 跑在哪 | 装什么 | 周期 | 覆盖哪一步 | 省磁盘？ |
|---|---|---|---|---|
| 服务器 | `douyin-archive.{service,timer}`（**在本包里**） | 1 小时 | ① 归档到 COS | **否**（不删） |
| 笔记本 | Windows 计划任务 `DouyinPull` | 30 分钟 | ② 拉顶层文本 + ③ 门禁通过后删 | 是 |
| 服务器 | `douyin-reclaimcheck.{service,timer}`（**在本包里**） | 30 分钟 | **体检上面两条腿**（见下） | — |

```bash
# 服务器侧（解压本包后就有这四个文件）
sudo cp /opt/douyin-live/service/douyin-archive.{service,timer} /etc/systemd/system/
sudo cp /opt/douyin-live/service/douyin-reclaimcheck.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now douyin-archive.timer douyin-reclaimcheck.timer
journalctl -u douyin-archive -n 50            # 首轮就该有「已传 N 场」
systemctl list-timers douyin-archive.timer douyin-reclaimcheck.timer   # 下一次触发时刻
systemctl start douyin-reclaimcheck.service   # 立刻体检一次（或等它自己跑）
```

⚠️ 装完**顺手跑一轮归档**（`sudo systemctl start douyin-archive.service`）：归档体检的第二条
判据看的是归档心跳 `service/dist/cos_archive.beat`，**这个文件只有归档真的跑过一轮才会出现**。
不先跑一轮的话，接下来 30 分钟里体检会一直报「归档心跳不存在」—— 那是**如实**的报警，不是 bug，
但会让第一次装的人以为装坏了。

```cmd
:: 笔记本侧
schtasks /Create /TN "DouyinPull" /SC MINUTE /MO 30 /F ^
  /TR "\"<python.exe 绝对路径>\" \"<repo>\service\pull_sessions.py\" --no-cos --quiet"
schtasks /Query /TN "DouyinPull" /V /FO LIST | findstr /C:"上次运行结果" /C:"下次运行时间"
```

**四个必须记住的点**：

1. **归档 timer 不省磁盘**（`cos_archive` 默认不删）。它保证的是「数据尽快进 COS」——
   笔记本关机的那段时间数据也不算只躺在服务器上。**磁盘回收天然依赖笔记本那台机器开着。**
2. **笔记本那条必须带 `--no-cos`**：服务器已经挂了归档 timer，再让笔记本每 30 分钟去触发
   一次 `cos_archive`，两边会抢同一把锁（`EXIT_LOCKED(3)`），那一轮白跑。`--no-cos`
   只关「写」（归档），**COS 账本照读**，删除门禁一位不少。
3. **笔记本侧是 `Interactive only`**（`schtasks` 默认）→ **重启后要有人登录才会跑**。
   想改成「不管是否登录都跑」得存密码（`/RU ... /RP ...`）或 S4U，而 S4U 读不到用户目录
   下的 SSH 密钥，反而更糟。**保持登录会话是前提。**
4. **别拿退出码做告警**：`cos_archive` 的 `0` 不证明数据在 COS（只有 `--dump-ledger -`
   的逐条活 HEAD 是证据），`pull_sessions` 的 `3`（撞锁）与 `1`（有场次失败）在正常轮次里
   也会出现。**看人话**：笔记本 `service/dist/pull_sessions.log` 的
   「本轮结束：成功 N、失败 M」；服务器 `journalctl -u douyin-archive` + `systemctl --failed`。

**首次两轮实测**：服务器首轮归档 **4 场 / 0 失败 / 26 秒**（`Result=success`、
`ExecMainStatus=0`、`systemctl --failed` 干净）；笔记本首轮「**成功 5、失败 0**」，
其中 **1 场被 COS 门禁正确拦下**（`live_91430703MA4L17BU_20261016_183413` —— 归档那轮之后
才结束的场次，COS 里还没有它 → **保留服务器那份**）。链路与保护都在真实轮次里验过了。

#### ★ 链路体检（`service/reclaim_check.py`）—— 自动化**自己停摆**时谁出声

自动化的危险形态不是「报错」，是「**每一步都看起来正常**」：磁盘一路涨到
`--min-free-gb 5` 把全模态悄悄降级成纯文本，而没有任何东西报错。已有的告警面各漏一块 ——
`douyin-healthcheck` 看的是**采集通道**（采集全绿、归档全死它照样 ok）、`★疑似漏采` 看的是
「在播却没在采」、归档单元自己的退出码看不见「**timer 压根没跑**」（被停用/没 enable 时
单元永远不会 failed，`systemctl --failed` 一片干净）。三条判据补这些：

| 判据 | 抓的是什么 | 默认阈值 | 命中后先看 |
|---|---|---|---|
| ① 根盘可用空间 | **笔记本那条腿停了**（不删了）→ 磁盘只涨不落 | 10 GB | `service/dist/pull_sessions.log` 的「本轮结束」；计划任务还在不在 |
| ② 归档心跳 `service/dist/cos_archive.beat` 的年龄 | 归档 timer **根本没在跑** | 150 分钟 | `systemctl status douyin-archive.timer` |
| ③ 账本说已归档、COS 上对象**此刻还在不在** | ★ **静默丢失**（唯一副本没了） | —（二元） | `cos_archive.py --selftest`，再 `--dump-ledger -` |

**① 为什么能替笔记本那条腿说话**：删除只在笔记本上做（三道门禁在那边），服务器看不到它的
死活 —— 但「不删」的直接后果就是**磁盘只涨不落**，于是一条磁盘判据覆盖两条腿，不必去猜
笔记本的状态。代价是**慢**：从 25 GB 掉到 10 GB 约 5 小时（实测流入 2.86 GB/小时）。

**③ 为什么非做不可**：`cos_archive` 的 HEAD 复查排在 `if not todo: return EXIT_OK` **之后**
（本文件上面「三条不能破的约定」里那条 `--dump-ledger -` 的说明就是讲这个）—— 在日常的
「没有新场次要传」场景里，服务器**一次 HEAD 都不做**就返回 0。桶被清空、生命周期到期、
有人手工删过对象：账本照旧写着 `verify='ok'`、`plan_cycle()` 照旧判 `done` 并永远跳过，
直到笔记本**准备删它**那一刻才发现。③ 把这个发现时机从「下次要删它」提前到「每 30 分钟」。

```bash
./venv/bin/python service/reclaim_check.py                  # 体检一次（0 健康 / 1 有告警）
./venv/bin/python service/reclaim_check.py --min-free-gb 15 # 换磁盘告警线
cat service/dist/reclaim.json                               # 结构化状态（--json-out '' 可关）
```

三条边界，都是**故意的**：
1. **③ 只核对「服务器上还在」的场次** —— 账本是累积的（删掉一场，记录仍留着），全量 HEAD
   会随天数线性变重；而已经删掉的那些，删除当时已用同一条判据当场核过。
2. **它不替代 `--dump-ledger -`** —— 那个是「**允不允许删**」的当场正向证据（fail-closed、
   逐批 TOCTOU 重取），本工具是「**有没有出事先吱一声**」的巡检；判定「对象在不在」用的是
   **同一个** `Cos.size_of()`，不另写一份。
3. **它不证明数据内容对** —— 逐字节核验在归档轮（`verify_tar` 对 manifest）与笔记本的删除
   门禁（文本腿 manifest）里。它只管「对象在、字节数与账本一致」。

⏪ 回退互不影响，可以只停一条：`sudo systemctl disable --now douyin-archive.timer` /
`sudo systemctl disable --now douyin-reclaimcheck.timer` / `schtasks /Delete /TN "DouyinPull" /F`。

## 五、5 主播并发全模态测试

```bash
python service/run_5anchors.py --roster documents/huitun_10_100w_1015_final.xlsx \
    --anchors 5 --duration 600
```

它做三件事：从名单里**按顺序限流探测**筛出 5 个在播的 → 把采集交给生产
`auto_crawl --media --max-media 5`（**测的就是真生产路径**，不是另写一套）→
用 `metrics` 采子进程树（含 ffmpeg）的资源，到时 SIGTERM 优雅落盘，
逐场统计并跑 `validate_fullmodal` 校验产物真实性，最后写
`service/dist/report_5anchors_<时间>.json`。

出报告后**先看这两个数**：

| 看什么 | 判据 |
|---|---|
| **实时倍率** | 稳态窗口内 `音频字节 / 时长 / 32000`。**< 1.0 = 解码跟不上直播实时，数据已在丢** |
| **444 计数** | `> 0` 说明 IP 仍被风控 —— 代理没生效，或需要换出口 |

上服务器前可先在本地小规模预跑一遍，验证编排链路本身：

```bash
python service/run_5anchors.py --anchors 2 --duration 90
```

> ⚠️ Windows 上 `SIGTERM` 走的是 `TerminateProcess`（硬杀），所以本地预跑看不到
> 「优雅落盘」那一步；服务器（Linux）上才是真正的 SIGTERM → `recorder.close()`。

## 六、这个包里没有的东西

代理凭据（故意的，见第三节），**COS 凭据同理** —— `config/cos.env`（`root:root 600`）
只在服务器上建，`cos_archive.py` 从那里读，包里没有、也不该有。

`metrics.py` 与 `test/validate_fullmodal.py` 平时**不在**生产依赖里 ——
它们只服务于上面的 5 路测试。纯守护跑不带这两个也完全正常。

**`service/pull_sessions.py` 也不在里面**（故意的）：它是**笔记本侧**的拉取工具，
方向是「笔记本主动拉」，服务器永远不需要执行它。同 `pack.sh` —— `service/` 下
不是每个文件都进包，这两个都是开发机侧。详见第四节末尾那一段。

同理，**笔记本上的 Windows 计划任务 `DouyinPull` 也不在服务器上**（它调的就是上面这个
`pull_sessions.py`）—— 服务器侧只装 `douyin-archive.timer`，两半各管一件事，互不依赖，
可以只停一条。⚠️ 反过来说：**`--no-cos` 是笔记本那条的必备参数**，服务器上已经有 timer
在做 ① 了，笔记本再去触发只会抢锁空转（详见第四节末的「自动触发」）。
