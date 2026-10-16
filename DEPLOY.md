# 抖音直播自动采集 — Linux 部署指南

将 `auto_crawl.py` 守护采集程序部署到 Linux 服务器（裸机 venv + systemd）。

## 概述

- **部署方式**：裸机 `venv` 虚拟环境 + `systemd` 守护服务
- **采集能力**：弹幕 / 事件 / 商品讲解（纯 WebSocket，无浏览器）
- **运行模式**：长期守护，检测到开播自动全程采集，下播自动落盘，循环往复
- **关键依赖**：`mini_racer`（内嵌 V8 执行签名 JS，**无需 Node.js**）

## 前置条件

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux（建议国内服务器，需能访问抖音） |
| Python | 3.8 ~ 3.11（`mini_racer` 有预编译 wheel） |
| 网络 | 能访问 `live.douyin.com` 及 `webcast` WebSocket |

## 上传文件清单

**清单的唯一真相是 [`service/MANIFEST`](service/MANIFEST)**（一行一个路径，带「为什么传 /
为什么不传」的注释），不要再照抄本文档手工维护第二份 —— 那正是漏传的来源。要传的文件：

| 文件 | 作用 | 备注 |
|---|---|---|
| `auto_crawl.py` | 守护采集主程序 | |
| `liveMan.py` | WebSocket 采集核心（签名/解析）+ 控制面代理 | |
| `data_recorder.py` | 结构化数据落盘 | |
| `sign.js` | WebSocket 签名脚本 | 492KB |
| `lib/reverse/douyin_old_algo_ref.js` | HTTP a_bogus 签名脚本 | ⚠️ 被 `.gitignore` 排除 |
| `protobuf/__init__.py`、`protobuf/douyin.py` | protobuf 生成代码 | 无需 protoc |
| `anchor.xlsx` | 主播列表 | ⚠️ 被 `.gitignore` 排除 |
| `documents/huitun_10_100w_1015_final.xlsx` | 生产名单（157 人） | ⚠️ 被 `.gitignore` 排除 |
| `requirements.txt` | Python 依赖清单 | |
| `service/douyin-crawl.service` | systemd 服务配置 | |
| `service/health_check.py` | **产出体检**（判断「在干活」而不是「进程活着」） | 配 `.timer` 用 |
| `service/douyin-healthcheck.service`、`.timer` | 每 10 分钟跑一次体检 | |
| `service/live_ledger.py` | **开播台账**（探全部主播 + 每场完整性 → 迁移清单 xlsx） | 配 `.timer` 用 |
| `service/douyin-ledger.service`、`.timer` | 每 10 分钟跑一次台账（2026-10-16 从 30 分钟改过来：守护只采 10 个主播，台账是其余人的唯一开播记录）| |
| `service/cos_archive.py` | **归档到 COS**：把采完的场次打包成 tar 传到腾讯云（走内网，59 MB/s） | 见「四之五」，需另建 `config/cos.env` |
| `metrics.py`、`test/validate_fullmodal.py` | 仅 5 路测试用 | 纯守护跑不需要 |

> ⚠️ **重要**：`lib/`、`anchor.xlsx`、`documents/` 在 `.gitignore` 中被忽略，**不能用
> git 推送**，必须打包上传，否则 a_bogus 签名脚本和主播列表会丢失。

> ℹ️ `service/pull_sessions.py` **不在上表里，且不要部署** —— 它是**笔记本侧**的拉取工具
> （见「四之四」），服务器只需要 `live_ledger.py` 的三个机器模式（`--dump-facts` /
> `--manifest` / `--delete-list`）。同 `pack.sh`：`service/` 下不是每个文件都进包。

## 一、本地打包上传（Windows）

在项目根目录的 Git Bash 执行：

```bash
bash service/pack.sh        # → service/dist/douyin-live-<日期>.tar.gz
scp service/dist/douyin-live-*.tar.gz root@你的服务器IP:/opt/
```

`pack.sh` 按 `service/MANIFEST` 打包，并做两件保护：**清单里少一个文件就报错退出**
（不静默漏传），以及**扫描包里有没有带账密的 URL**（防止代理凭据被顺手写进某个配置
文件带出去）。少传的典型后果是 a_bogus 签名脚本没了 —— 现象是「WS 连上了但一条数据
都没有」，很难往「少传了一个文件」上想。

## 二、服务器部署

```bash
ssh root@你的服务器IP

# 1. 解压到 /opt/douyin-live
mkdir -p /opt/douyin-live
tar xzf /opt/douyin-live-*.tar.gz -C /opt/douyin-live
cd /opt/douyin-live

# 2. 时区设为上海（否则时间戳差 8 小时）
timedatectl set-timezone Asia/Shanghai

# 3. 建虚拟环境 + 装依赖
python3 --version            # 确认 3.8~3.11
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> 若 `pip install mini_racer` 报编译错误，先装编译工具再重试：
> ```bash
> apt install -y build-essential python3-dev
> ```

### 配代理（**上服务器必做**）

服务器是**数据中心 IP**，444 风控对这类 IP 拒绝率很高（见第七节实测），**加机器解决
不了**，只能换出口 IP。控制面（4 处 `requests` + WebSocket）走隧道代理：

```bash
# 先验代理本身通不通 —— 这一步不过，后面全不用谈
curl -x 'http://<用户>:<密码>@<主机>:<端口>' http://myip.ipip.net
#   出口 IP 应该是代理的，不是服务器的

mkdir -p /opt/douyin-live/config
printf 'DOUYIN_PROXY=http://<用户>:<密码>@<主机>:<端口>\n' \
    > /opt/douyin-live/config/proxy.env
chmod 600 /opt/douyin-live/config/proxy.env     # 凭据只落这一个文件
```

`config/` 在 `.gitignore` 里，凭据不会进仓库。systemd 通过 unit 文件里的
`EnvironmentFile=-/opt/douyin-live/config/proxy.env` 读它（行首 `-` = **文件不存在也不
报错**，所以不配代理服务照样起得来，只是直连）。手跑时 `set -a; . config/proxy.env; set +a`。

配置优先级 **`DOUYIN_PROXY` > `http_proxy` / `HTTP_PROXY` > 无（直连）**，进程启动时
解析一次。全部细节（含媒体流代理开关）见 [`service/README.md`](service/README.md) 第三节。

## 三、手动测试

挂 systemd 前先手动跑一遍，确认能连上抖音：

```bash
cd /opt/douyin-live
./venv/bin/python auto_crawl.py anchor.xlsx 60
```

看到类似输出即成功：

```
加载主播列表 15 个（来自 anchor.xlsx，检测间隔 60s）：
  - 与辉同行（抖音号 56697889278）
  ...
守护模式运行中，按 Ctrl+C 停止...
[与辉同行] 检测到开播：与辉同行（...），开始全程采集
```

按 `Ctrl+C` 退出。

## 四、配置 systemd 守护

```bash
cp service/douyin-crawl.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now douyin-crawl
systemctl status douyin-crawl
```

service 文件默认假设：

- 项目路径：`/opt/douyin-live`
- 运行用户：`root`
- 日志：**journald**（`StandardOutput=journal`，自带轮转）。**不要改回
  `append:/opt/douyin-live/crawl.log`** —— 守护进程按 60s 一轮持续打印，
  append 模式永不轮转，跑几个月能把磁盘写满。

若路径或用户不同，编辑 `service/douyin-crawl.service` 里的 `User=`、`WorkingDirectory=`、
`ExecStart=` 后再复制。

> ⚠️ **三处路径必须一致**：`WorkingDirectory=`、`ExecStart=` 里的 python 与脚本路径、
> 以及 `auto_crawl.py` 后面的 xlsx 路径。此前 service 写 `/opt/douyin-live` 而本文档写
> `/opt/tiktok-live` —— 照文档做会 `WorkingDirectory` 不存在直接起不来。
> **现已统一为 `/opt/douyin-live`（以 service 文件为准）。**

### 停止与重启是**优雅**的

`systemd` 的 `stop` / `restart` 发 **SIGTERM**，`auto_crawl.py` 已注册处理函数：
停止信号会先让**当前所有在采场次落盘**（`recorder.close()`）再退出，所以正在采的场次
不会留下残缺目录。`TimeoutStopSec=60` 是收尾预算。

```bash
systemctl restart douyin-crawl   # 安全：当前场次先落盘再重启
```

> 若同时要换主播名单（`anchor.xlsx`），**必须 restart 才生效** —— 名单只在启动时读一次。

### 四之二、装产出体检（**别跳过**）

```bash
cp service/douyin-healthcheck.service service/douyin-healthcheck.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now douyin-healthcheck.timer

# 立刻跑一次看结果
systemctl start douyin-healthcheck.service
journalctl -u douyin-healthcheck -n 20 --no-pager
systemctl list-timers douyin-healthcheck.timer
```

**为什么必须有**：`systemctl is-active douyin-crawl` 显示 `active` **不代表在采数据**。
2026-10-15 实测踩到 —— 隧道端点变成 `Connection refused` 后，守护进程每条请求都
ProxyError、一条数据都采不到，进程状态却一切正常，空转了几个小时没人知道。
体检看的是**产出**：探几个房间（走和守护进程完全相同的代码路径，覆盖 ttwid 首页 /
enter / 隧道认证）再与 `data/` 的新鲜度交叉，**退出码即判定**（0 健康 / 1 `down`|`stale`）。

于是告警面就是原生的：

```bash
systemctl --failed                # down/stale 会直接列在这里
systemctl status douyin-healthcheck.service   # 看判定详情
cat /opt/douyin-live/service/dist/health.json # 机器可读的那份
```

判定的四种结果、以及「为什么不能只看 data/ 有没有新文件」（半夜没人开播时没有新数据是
正常的，纯 mtime 判据会每晚误报、报几次就没人看了），见 `service/health_check.py` 顶部注释。

> ⚠️ **不要把体检单元改成 `Restart=always` / `Restart=on-failure`** —— 失败正是要留下的
> 信号，重试会把退出码冲成成功，`systemctl --failed` 就看不见了。

### 四之三、装开播台账（迁移清单 + 漏采哨兵）

```bash
cp service/douyin-ledger.service service/douyin-ledger.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now douyin-ledger.timer

# 立刻跑一次（全量探 157 人约 50 秒 + 扫 data/）
systemctl start douyin-ledger.service
journalctl -u douyin-ledger -n 20 --no-pager
systemctl list-timers douyin-ledger.timer

ls -l /opt/douyin-live/service/dist/live_ledger.xlsx   # 下载到本地用 Excel 看
```

**它和体检是两个不同的失效模式，两个都要装**：

| | 体检（10 分钟） | 台账（10 分钟） |
|---|---|---|
| 探几个房间 | 8 个（取样） | **全部 157 人** |
| 判什么 | 守护进程还活着吗（通道 × 产出新鲜度）| 有没有**整场漏采** + 每场采全了没有 |
| 抓不到的 | 「大部分正常、少数几个房间僵死」 | 隧道整体死了（要等一轮，且它报的是 `疑似漏采`）|

`live_ledger.xlsx` 里 `开播矩阵_MMDD` 是**每天一张**的开播矩阵：第一列是名单，
**之后每一轮探测新增一列**，格子里 `在播`/`没播`/`未知`，末尾两行是 `★ 在播数` /
`★ 探测失败数` —— 看「开播数量随时间的走势」用它。**它只读 `live_rounds` 底账重建**
（纯读，不探测），历史某天随时调得出来（`--matrix-days 7`，`0` = 关）。
⚠️ `未知` 是**探测失败**（隧道挂了 / enter 被拦），不是「没播」。

2026-10-15 那次就是前者：**16 个在播房间（占在播的 44%）实际没在采**，而体检报「健康」。
根因是 WS 僵死（TCP 活着但抖音不再下发数据 → 线程永久卡在等待循环），已由
`auto_crawl.STALL_SECONDS` 修复；台账的 `疑似漏采` 列是它的**长期哨兵**，正常情况下
应长期贴近 0。

**⚠️ 2026-10-16 起守护进程并发只采 10 个主播（`--max-sessions 10`），所以 10 分钟一轮的
台账成了其余 147 人唯一的开播记录。**`疑似漏采` 的口径也随之改了 —— 详见第五节。

### 四之四、自动传输（**装在笔记本上，服务器不用装任何东西**）

★ **2026-10-16 起是两条腿**（原因见本节末）：

| 数据 | 去哪 | 通路 |
|---|---|---|
| 顶层文本/数值（8 种文件，约 1 MB/场） | 笔记本 `D:\shock\Documents\data` | 公网 ssh+rsync |
| **整场**（含音视频，每场一个 tar） | COS（见四之五） | 内网 59 MB/s、**免费** |

方向是**笔记本主动拉**，所以工具 `service/pull_sessions.py` **不在服务器的部署包里**
（同 `pack.sh`）—— 服务器永远不执行它，服务器侧只需要 `live_ledger.py` 与 `cos_archive.py`。

笔记本上的落地布局：日期目录下是**完整场次**，里面只有**顶层 8 种文本** +
`_local_cos_media.json`（指向 COS 上那一场 tar 的指针：key / tar_bytes / tar_sha256 /
etag / bytes / files / digest，**不含任何凭据**）。**不再与服务器逐字节一致** ——
媒体在 COS 上，按指针里的 `key` 取（下载 → sha256 比对 → `tar -xf` → 与本地目录合并）。
**采集中 / 不完整 / 全空**的场次**不拉也不删**。
（旧文档里「不完整的进 `_incomplete\<日期>\`」**已取消**：本工具只收「完整」场次，
全空场次的回收归 `live_ledger --delete-incomplete` 的既有职责。）

操作顺序 —— **先服务器、后笔记本**（笔记本要靠服务器的 `--dump-ledger`，
服务器没部署就会抛 `unrecognized arguments`）：

```bash
# 笔记本上，先小范围试（拉回来但不删服务器那份）
python service/pull_sessions.py --selftest                # 自检；★ 远端 --dump-ledger 不通就绝不能开删
python service/pull_sessions.py --dry-run                 # 只说要拉什么、能删什么（不归档/不拉/不删）
python service/pull_sessions.py --keep-remote --limit 3   # 真拉，但不删
python service/pull_sessions.py --limit 0                 # 拉完积压 + 真删
```

**三道删除门禁**（缺一不可，详解见 `service/README.md` 第四节）：
① **文本腿** —— 两侧「顶层 8 种文件」的清单逐项相等（复用 `ll.manifest_digest()`）；
② **COS 腿的活证据** —— `object_present` 为真 **且** `bytes/files` 与**此刻**磁盘相等
（**只读账本不算**：`verify` 只是过去某次上传的回执，`cos_archive` 自己都不信它）；
③ **TOCTOU** —— 删前**重取一次事实**重算 ②，且清单每行带期望列 `rel\t字节\t文件数`，
由服务器**自己重扫后复核**，不符**整批拒绝**。
另有 **顶层出现未知文件名 = fail-closed**（该场不拉也不删）。
墓碑 label 是 **`已传输+COS`**（复用 `delete_sessions()`，**不会制造假漏采**）。

四条不可动摇的约定：**本地数据永不自动删**、**校验不过绝不删服务器那份**、
**清单有一条不符就整体拒绝**、**本地空间低于阈值就停拉 + 告警**。

### ★ 自动触发（2026-10-16 起，方案「A 双机分工」已装）

分工不是随便定的：链路三步里**只有第 ① 步必须在服务器上做**（内网 59 MB/s vs 公网
0.33 MB/s，差 178 倍），而**删除必须由笔记本发起** —— 三道门禁只在笔记本上，代码里
`assert '--delete-after-upload' not in cmd` 明确禁止服务器自删。于是自动化也照这个分工，
拆成两半：

| 跑在哪 | 装什么 | 周期 | 覆盖哪一步 |
|---|---|---|---|
| 服务器 | `douyin-archive.timer`（**在部署包里**） | 1 小时 | ① 归档到 COS（**不删**） |
| 笔记本 | Windows 计划任务 `DouyinPull` | 30 分钟 | ② 拉顶层文本 + ③ 门禁通过后删 |

```bash
# 服务器侧：pack.sh 后解压即得，两条命令启用
sudo cp /opt/douyin-live/service/douyin-archive.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now douyin-archive.timer
```

```cmd
:: 笔记本侧：/tr 直接起 python.exe，不经 bash —— Python 自己会设 MSYS2 的 PATH
schtasks /Create /TN "DouyinPull" /SC MINUTE /MO 30 /F ^
  /TR "\"D:\code\anaconda3\python.exe\" \"D:\CodeFile\GithubProjects\Tiktok-live\service\pull_sessions.py\" --no-cos --quiet"

:: ★ 装完必须关掉电池限制 —— 默认「用电池时不启动」，笔记本一拔电源链路就断
powershell -NoProfile -Command "$s=(Get-ScheduledTask -TaskName 'DouyinPull').Settings; $s.DisallowStartIfOnBatteries=$false; $s.StopIfGoingOnBatteries=$false; Set-ScheduledTask -TaskName 'DouyinPull' -Settings $s"
```

⚠️ 笔记本这条**必须带 `--no-cos`**：服务器已经自己挂了归档 timer，再让笔记本每 30 分钟
去触发一次，两边会抢同一个 `cos_archive` 锁（撞锁返回 `EXIT_LOCKED(3)`，那一轮白白空转）。
`--no-cos` 只关掉「写」（归档），**COS 账本照读**，删除门禁不受影响。

⚠️ 三个已知边界：
1. **笔记本侧是 `Interactive only`**（`schtasks` 默认），所以**机器重启后要有人登录**任务
   才会跑。改成「不管是否登录都跑」得存密码（`/RU ... /RP ...`）或 S4U —— 但 S4U 读不到
   用户目录下的 SSH 密钥，反而更糟。**保持登录会话是前提**。
2. **笔记本关机期间服务器磁盘照涨**：归档那条腿照跑（数据进 COS，不会丢），但删除那条腿
   停在笔记本上，`data/` 会堆。长假期回来要手动补一次 `pull_sessions.py --limit 0`。
3. **不要用退出码做告警**：`cos_archive` 的 `0` 不证明数据在 COS（只有 `--dump-ledger -`
   的活 HEAD 是证据），`pull_sessions` 的 `3`（撞锁）与 `1`（有场次失败）在正常轮次里
   也会出现。笔记本侧看 `service/dist/pull_sessions.log` 的「本轮结束：成功 N、失败 M」；
   服务器侧看 `journalctl -u douyin-archive` 与 `systemctl --failed`。

⏪ 回退：`schtasks /Delete /TN "DouyinPull" /F`；
`sudo systemctl disable --now douyin-archive.timer`。两条互不影响，可以只停一条。

> ⚠️ 本地 `rsync`/`ssh` **必须同源**：都取 `D:\code\msys2-rsync\usr\bin` 下那一对
> （调 Git 或 Windows 自带的 ssh 都有确定报错），且本地路径要写 MSYS2 的 `/d/...` 形式。
> 工具内部把这条关系钉死了（私有 helper + 绝对路径），**不要**把那个 `usr\bin` 加进系统
> PATH —— 会顶掉 Git 的 ssh，`git push` 会坏。详见 `D:\code\msys2-rsync\README.md`。

**⚠️ ★ 公网这条路的产能（2026-10-16 实测）**：服务器出口被腾讯云轻量带宽包硬卡在
**≈ 0.33 MiB/s（2.76 Mbps）**，**并发不叠加**（2/4/8 路总量一样），而入口 4.2 MiB/s
（同链路同时刻快 12 倍，排除网卡/load/CPU）。**产能 ≈ 28 GiB/天 vs 流入 68.6 GB/天 →
整场走公网永远追不上**。所以只让它跑**文本**：顶层仅占全库 **0.5%**，约 **0.35 GB/天
≈ 18 分钟/天**，绰绰有余。媒体走内网 COS。`--bwlimit` 仍是兜底（从没起过作用）；
传输**不影响采集**（采集全是入向）。实测构成 **frames 61% / audio 38% / 文本+数值 0.6%**
就是这次拆分的依据。

**表格用法**（`场次` sheet 就是**迁移清单**）：

1. 筛 `完整性 = 完整` → 这些是能迁走的；
2. 迁完在 `是否已迁移` 里写个标记（**填文本，别填公式**——每 10 分钟整份重写，
   这两列是按 `场次目录` 搬回来的；填公式会在下次重写时变成静态值）；
3. 需要腾空间时 `--delete-migrated --apply` 一条命令回收。

`完整性` 的判据是**必须含音视频**（用户口径）：优雅收尾（有 `room.json`）+ 弹幕/事件非空
+ **有效**音频段与帧都 > 0。⚠️ 「有效」按 1 KiB 过滤 —— 78 字节的空壳 WAV
（44 字节头 + 长度 0 的 `data` 块）**不算数**，2026-10-15 就是它把「一段音频」报成了有音视频。
`原因` 列会把「没排到名额」/「磁盘降级」（设计内）与「启动失败」/「起了 ffmpeg 零产出」
（故障）分开 —— 前两种不用管，后两种要查。

### 四之五、归档到 COS（**装在服务器上**，凭据另建）

**为什么需要它**：服务器出口被轻量带宽包卡在 **0.33 MB/s**，而全模态流入 **68.6 GB/天**
—— 笔记本走公网 rsync **永远追不上**（需 59.5 小时/天）。而**内网出口没被卡**：
同一个 bucket、同一进程、同一时刻，内网单对象 **58.68 MB/s**（**178 倍**）。
所以场次改从内网归档到 COS。详见 `service/README.md` 第四节。

**1) 建凭据（在服务器上做，`root:root 600`）** —— 密钥**不进 git、不进发布包**：

```bash
sudo mkdir -p /opt/douyin-live/config
sudo tee /opt/douyin-live/config/cos.env >/dev/null <<'EOF'
COS_SECRET_ID=<腾讯云 SecretId，36 字符、AKID 开头>
COS_SECRET_KEY=<腾讯云 SecretKey，32 字符>
COS_BUCKET=douyinlive-1507396983
COS_REGION=ap-shanghai
EOF
sudo chown root:root /opt/douyin-live/config/cos.env
sudo chmod 600 /opt/douyin-live/config/cos.env
```

> ⚠️ **用 CAM 子账号，只授这个 bucket 的读写**，不要用主账号密钥。
> **在上面那条命令里直接替换 `<...>` —— 不要把密钥贴进对话、issue 或任何文件。**

**2) 自检（必做）** —— 它会**真的发一个对象、读回来、再删掉**：

```bash
cd /opt/douyin-live && sudo -n ./venv/bin/python service/cos_archive.py --selftest
```

全绿才继续。`★ 走的是内网（169.254.x.x）` 那条尤其要看 —— 解析到公网 IP 就会撞带宽包。

**3) 先干跑，再小范围，再全量**：

```bash
sudo -n ./venv/bin/python service/cos_archive.py --dry-run      # 只看要传什么
sudo -n ./venv/bin/python service/cos_archive.py --limit 3      # 先传 3 场
sudo -n ./venv/bin/python service/cos_archive.py                # 全部，**不删**服务器那份
```

**4) `--dump-ledger -`（★ 笔记本侧删除的**唯一**正向证据，只读）**：

```bash
sudo -n ./venv/bin/python service/cos_archive.py --dump-ledger -   # 账本 + 逐条活 HEAD → object_present
```

`service/pull_sessions.py` 每轮都调它，拿到的 `object_present` 是**活的**（当场 HEAD），
不是账本的自述。⚠️ 这条命令**必须带 `sudo -n`**：`config/cos.env` 是 `root:root 600`，
不给 sudo 就读不到凭据 → 工具 fail-closed 地把**全部对象报成「不在」**（`EXIT_ENV`），
于是**一场都删不掉**。

**5) 「要腾服务器磁盘」的日常做法不是 `--delete-after-upload`，而是让笔记本跑
`pull_sessions.py`** —— 删除由笔记本的**三道门禁**（文本腿 + COS 活证据 + TOCTOU 复核）
通过后发起，墓碑 label **`已传输+COS`**。`--delete-after-upload` 只会看 COS 那一侧，
少了文本腿，**别和笔记本那条链一起用**（同跑会有锁竞争，也会绕过文本校验）。
2026-10-16 实测那一轮：**删 91 场 / 18.21 GB**，根盘可用 **14G → 31G**、`data/` **18G → 1.2G**。

**不需要**在服务器上 `pip install` 任何东西 —— COS V5 签名是手写的
（`cos_authorization()`，只用 `hashlib`/`hmac`），**别改成引 `cos-python-sdk-v5`**，
那会让部署多出一步手工操作（少一步的症状是「任务全失败」）。单测有断言钉住。

## 五、日常运维

```bash
# 查看状态
systemctl status douyin-crawl

# 实时看采集日志（日志走 journald，没有 crawl.log 文件了）
journalctl -u douyin-crawl -f
journalctl -u douyin-crawl --since "1 hour ago" | grep -E "风控444|444 Client Error|\[ERROR\]"

# 重启 / 停止（优雅：当前场次先落盘）
systemctl restart douyin-crawl
systemctl stop douyin-crawl

# 查看采集的数据（每个主播一场直播一个目录）
ls /opt/douyin-live/data/
```

`Restart=always` 已配置：进程崩溃会在 30 秒后自动重启；崩溃前的数据因 `series.jsonl` 边采边落盘而不丢。

> **journald 默认可能只存在内存里**（`/run/log/journal`），重启即失。要查历史日志先开持久化：
> ```bash
> mkdir -p /var/log/journal && systemd-tmpfiles --create --prefix /var/log/journal && systemctl restart systemd-journald
> ```
> 再给个上限，别让日志本身成为磁盘问题：编辑 `/etc/systemd/journald.conf` 设 `SystemMaxUse=1G`。

**要盯的五个量**（守护进程长跑必须看）：

```bash
# 0. 体检判定（最快的一眼：有没有在干活）
systemctl --failed                          # down / stale / 疑似漏采 都会列在这里
journalctl -u douyin-healthcheck -n 5 --no-pager --output=cat

# 0b. ★ 疑似漏采（台账）：正常应长期为 0。不为 0 = **名额没占满却有人在播没被采**，
#     那是**整场**数据在丢，比磁盘满更值得立刻处理。
#     ⚠️ 2026-10-16 起判据多了「名额没满」这个前提（守护只并发采 10 个，抢不到的
#     本来就不落数据，那是策略不是故障 —— 见第五节）。
journalctl -u douyin-ledger -n 20 --no-pager --output=cat | grep -E "疑似漏采|探测通道全失败"
journalctl -u douyin-ledger -n 5 --no-pager --output=cat | grep "场次分布"
#     `场次分布` 那行给出 完整/不完整/采集中 各多少 —— 迁移清单的规模一眼可见

# 1. 磁盘：data/ 的增速（视频帧/音频只有开 --media 才有；纯文本量级很小）
df -h / && du -sh /opt/douyin-live/data/
#    剩余低于 --min-free-gb（默认 5G）会自动降级为纯文本，日志里会打「只采文本/数值」
# 2. 444 率：风控的唯一早期信号
#    ⚠️ 必须用**这个**模式，不能 `grep -c 444` —— 日志里到处都是恰好含 444 的
#    user_id / 时间戳（`[ENTER] [3544429557987695]`、`ts=1792027354448`），
#    裸子串会把它们全计成风控命中（实测同一窗口：裸子串 46 行、真 444 是 0 行）。
journalctl -u douyin-crawl --since today | grep -cE '风控444|444 Client Error'
# 3. 句柄：接近 LimitNOFILE 就要警惕（每场直播占 4 个）
ls /proc/$(systemctl show -p MainPID --value douyin-crawl)/fd | wc -l
```

### ⚠️ 并发只采 10 个主播（2026-10-16 起的策略）

**157 人名单里同时可能有一二十个在播，而 2C2G 只够 10 路 ffmpeg**（单路 111 MB，
满载 `available` 剩 536 MB，**无 swap**，超了直接 OOM）。所以守护进程用
`--max-sessions 10` 把「同时在采的主播数」钉在 10：

- **抢不到名额的主播这一轮完全不采** —— 不建场次目录、不连 WS、不落任何数据。
  日志里会打（节流到每 30 分钟一条）：
  `[某某主播] 会话名额已满（10 路）—— 开始跳过本轮（只记开播状态，本轮不落任何数据）`
- 名额**先到先得**，空出来补给下一个在播的（每轮 60 秒重探）。
- 语义与 `--max-media` **相反**：那个抢不到是**排队等**（等到就采），这个抢不到是
  **直接跳过**。两个都设成 10 时 `--max-media` **永远轮不到阻塞** —— 它现在只是
  「万一有人把 `--max-sessions` 调大」时的一道兜底保护。

两个配套后果，**都必须知道**：

1. **`douyin-ledger` 每 10 分钟成了其余 147 人唯一的开播记录**（这是 timer 从 30 分钟
   改到 10 分钟的原因）。别的模态一律没有 —— 有意为之，不是漏采。
2. **`疑似漏采` 口径跟着改了**：名额已满的轮次**不计漏采**。不排除的话，10 个名额配
   一二十个在播，这一列会恒等于「在播数 − 10」、退出码恒为 1，把 `systemctl --failed`
   这个告警面彻底淹掉。它现在报的是「**名额没占满，却有人在播而没在采**」—— 真出事。
   ⚠️ `douyin-crawl.service` 与 `douyin-ledger.service` 的 `--max-sessions`
   **必须是同一个值**（有两处注释互相点名，`test/test_session_gate_unit.py` 有断言钉住）。
   ⚠️ 名额已满的轮次会**断掉连续计数**，所以一次真漏采最迟 20 分钟（两轮）报出来。

**这个策略不降低磁盘/内存压力** —— 仍是 10 路媒体在写，仍 **≈ 68.6 GB/天**（见下）。
要真正减负只能调小 `--max-sessions`（少采几个主播），那是另一个取舍。

想要全部在播的都采（回到改动前行为）：去掉 `--max-sessions` 即可，
但**必须同时确认内存**，并相应调大 `--max-media`。

## 六、故障排查

| 现象 | 排查 |
|---|---|
| `pip install mini_racer` 报错 | `apt install -y build-essential python3-dev` 后重试 |
| 连不上抖音 | 确认服务器能 `curl https://live.douyin.com/`；海外服务器可能不通，换国内 |
| `ModuleNotFoundError: tkinter` | 已做惰性导入，理论上不会出现；若出现说明 `liveMan.py` 版本过旧 |
| 时间戳差 8 小时 | `timedatectl set-timezone Asia/Shanghai` |
| 采集目录为空 | 确认主播列表里有人开播；`tail -f crawl.log` 看检测日志 |
| 报 `ffmpeg 未找到` | 默认部署不带音视频（`auto_crawl.py` 不开 `--media` 时不用 ffmpeg）；若已开 `--media` 或跑 `collect_full.py`，需先装 ffmpeg |
| 大量 `444 Client Error` | 抖音风控静默拒绝（nginx 无响应状态码）。**注意根因通常是 IP 类型**：数据中心 IP 会被直接拒（实测腾讯云服务器 15 主播 @10s 就有 88 次 444，而本机同样频率正常）。先确认检测间隔用 60s 而非 10s；确认已上传新版 `liveMan.py`（带完整浏览器头）和 `auto_crawl.py`（错峰 + 抖动）。若仍 444，**配控制面代理**（第二节「配代理」）—— 卡的是 IP 类型，加机器无效 |
| 重启后当前场次目录缺 `room.json`/`series.json` | 说明 SIGTERM 没被优雅处理。确认服务器上的 `auto_crawl.py` 是**带 `_install_signal_handlers` 的版本**，且 service 里 `TimeoutStopSec` 够长（默认 60s） |
| 加量后全部连接失败 / 一直拿不到数据，但 444 计数是 0 | 怀疑 **ttwid 缓存失效**。`liveMan.py` 的进程级 ttwid 带 30 分钟 TTL 会自动重取；若日志里出现「请求直播URL错误」，看重取是否成功。极端情况下 `systemctl restart` 可直接复位 |
| 加量后越来越慢 / `Address already in use` / 打不开文件 | 句柄不够，见 service 的 `LimitNOFILE=65535`；用上面的 `ls /proc/<pid>/fd | wc -l` 核对 |
| **台账报「疑似漏采」，某个主播在播却没有场次目录** | 该主播的采集线程卡住了。历史根因是 **WS 僵死**（TCP 活着但抖音不再下发数据 → `offline` 永不置位、`ws_thread.is_alive()` 恒为真 → 等待循环永不退出）；`auto_crawl.STALL_SECONDS`（默认 300s）已能识别并结束本场，**下一轮探测会重开一个新场次目录**。若仍在报：`journalctl -u douyin-crawl -f` 看有没有 `⚠️ WS 僵死`，没有就是别的原因（例如线程死在别处）。**漏采丢掉的是整场，不是尾部**，所以别放着不管 |
| 某场次被台账标成 `不完整 / 未收尾` | 该场次目录里没有 `room.json`（`DataRecorder.close()` 没跑成）= 进程被杀或僵死后重启。`session_meta.json` 的 `stop_reason` 会说明是哪种：`offline` 正常下播 / `ws_closed` WS 断开 / `stall` 僵死 / `signal` 收到 SIGTERM。**没有 `session_meta.json` 的是 2026-10-15 之前的老场次**，那时还没这个文件 |
| 某场次被标成 `不完整 / 无音视频`，原因写「没排到名额」 | 这是**设计内降级**，不是故障：这一场排不到**音视频**名额时不采音视频，只采文本/数值。⚠️ 2026-10-16 起生产跑的是 `--max-sessions 10 --max-media 10`，场次数被前者钉在 10、每场最多一路 ffmpeg，所以后者**正常永远不会阻塞** —— 真出现这一条，说明有人把 `--max-sessions` 调大了而没同步 `--max-media`。要采全请先确认内存（服务器**无 swap**，超了直接 OOM），再调 `--max-media` |
| 某个在播主播**一直没有场次目录、连空目录都没有**，但日志有「会话名额已满」 | **正常**，这是 2026-10-16 起的策略：并发只采 10 个，抢不到的完全没轮到（见「并发只采 10 个主播」一节）。这一列在台账里会显示成「在播但没场次」——只有**名额没占满**时报的才叫 `疑似漏采`，两者别混 |
| 某场次被标成 `不完整 / 无音视频`，原因带 ★ | 是**故障**，要查：`start_failed` = ffmpeg 没起来（`ffmpeg -version` 通不通、`flv_pull_url` 拿没拿到）；`起了 ffmpeg 却零产出` = 流地址拿到了但拉不到流（多为页面端点的验证码中间页，见 `liveMan.py` 的「页面优先 + enter 兜底」）|
| **笔记本上拉取一直很慢（≈ 0.3 MiB/s）** | **不是故障，是带宽包**（见「四之四」）：服务器出口被硬卡在 2.76 Mbps，**并发不叠加**。别去调 `--bwlimit`（它从来没生效过）或多开几路 —— 都没用 |
| 拉取日志里出现「清单记录已拉取，但本地不存在」 | 文件被**人工挪走**了（用户说过「满了我自己挪盘」）。这行**不改状态、不重下**，只是让「服务器那份也删了、本地也不见了」这种**唯一副本丢失**被看见 |
| 拉取日志里出现「服务器拒绝删除（rc=2）」 | 清单与服务器实际不符（场次被别处删了、正在写入、路径不合规，或条数对不上）。**一条都没删**，服务器那份还在，下一轮会重新校验重试 —— 这是 **fail-closed 的正常表现**，不是要修的 bug |
| 本地空间告警停拉 | 阈值默认 10 GB（`--min-free-gb`）。**工具永不自动删本地数据**，停拉是设计行为 —— 挪走旧场次后自动恢复 |
| 定时任务没跑 / 日志不动 | 看 `service/dist/pull_sessions.log`；锁文件 `pull_sessions.lock` 卡住时，只要**超过 `--lock-min`（默认 180 分钟）没有心跳**会被下一轮自动接管（心跳 = 每采完一场刷新一次锁的 mtime） |
| COS 归档报 `HTTP 403 AccessDenied` | 密钥错 / 子账号没授这个 bucket 的权。先用 `--selftest` 看 `权限 600 且凭据形状正常` 那条 —— 如果报「看起来还是模板占位符」，就是 `cos.env` 里那行还是 `<...>` 没换 |
| COS 归档报 `HTTP 404 NoSuchBucket` | `COS_BUCKET` 或 `COS_REGION` 写错（bucket 名是 `名字-appid`，region 形如 `ap-shanghai`） |
| COS 归档**很慢**（每秒只有几 MB） | 先看 `--selftest` 里 `★ 走的是内网（169.254.x.x）` 那条 —— 解析到公网 IP 就会撞带宽包。若内网正常还慢，是**小对象多**（每 PUT ≈90 ms 往返），而不是带宽；本工具已经用「整场打一个 tar」规避了这一点，别改回逐文件上传 |
| COS 归档日志里出现「tar 与清单不符 → 不传」 | 打包期间目录被改动了（有进程还在往里追加）。判据是**成员表逐项比对**，不是体积 —— 这一场会在下一轮重试，不是 bug |
| COS 归档日志里出现「账本记录已归档，但 COS 上找不到对象」 | 对象被删过（或账本是从别的 bucket 搬来的）。**会重传** —— 这一步是防「服务器那份也删了、COS 上也没有」的静默丢失，别去掉 |
| 服务器磁盘还是满的，明明已经归档到 COS | **归档默认不删服务器那份**（故意的，见 `cos_archive.py` 模块顶部）。要腾空间加 `--delete-after-upload` |

## 七、上线前须知：容量与风控

这一节是 2026-10-08 两轮阶梯压测（20→175 并发，全档 `http_444 = 0`）的结论，
**上线前务必先读**。

### ⚠️ 175 并发 ≠ 能带 1684 个主播

压测测的是 `load_test --mode network` 的模型：**只对当前在播的房间建连**，不轮询。
`auto_crawl` 是**每主播一个常驻线程**，而且**不管在不在播每轮都要探测**。两者资源画像完全不同：

| | 压测（network） | auto_crawl 守护 |
|---|---|---|
| 线程 | ≈ 2 × 连接数 | **1 × 主播数** |
| 轮询请求 | 无 | **每主播每轮 1 次 HTTP**（`get_room_status`） |

拿 1684 人名单直接算：1684 线程 + 1684 req / 60s ≈ **28 req/s 持续打 `enter` 接口**。
这比压测的探测阶段高一个数量级，**不要一台机一个进程直接扔 1684 人**。建议分组，
每台 50–100 人。

### IP 类型才是真正的天花板

| 环境 | 实测 |
|---|---|
| 本机住宅 IP | 175 路并发，`444 = 0` |
| 腾讯云数据中心 IP | **15 主播 @10s 就有 88 次 444** |

压测测出的是**代码容量**，跟服务器能带多少是两件事。所以：

1. **先做 IP 体检**：目标服务器上只跑 **1–2 个主播**、`check_interval 60`、跑 **24h**，
   统计 444 率（`journalctl -u douyin-crawl --since today | grep -cE '风控444|444 Client Error'`
   —— **不要**用 `grep -c 444`，见第五节的说明）。
2. 444 率高 → **配控制面代理**（第二节「配代理」／[`service/README.md`](service/README.md)
   第三节），**不要靠加机器解决**。
3. 再按上面「分组」的思路逐档加量，**每次只加一档、观察 24h**。

**控制面代理的实测边界（2026-10-14，本机）**：`DOUYIN_PROXY` 一设，三条腿全部走代理且
可用 —— ttwid（`live.douyin.com/` 首页）、`enter` 接口、**WebSocket**（收到 12 个数据帧）。
出口 IP 由直连的 `218.108.191.123`（华数）换成 `39.180.134.208`（移动）。

⚠️ **2026-10-16 修订：WebSocket 已默认不走隧道**（`liveMan.proxy_for_ws()` 返回 `{}`）。
隧道在 ~6.5 分钟处掐断长连接、每条各自死，实测把一场直播切成多个碎片场次。
完整证据与 A/B 开关见 `service/README.md` 第三节的表格。

⚠️ **但直连只修掉了「隧道掐」这一个断因** —— 抖音自己、网络抖动、机房侧 RST 一样会断，
所以另有「**同场次内自动重连**」（`auto_crawl.WS_RECONNECT` 默认 10，**不用写进 service
文件里**）：断线时重开一条连接、继续往**同一个场次目录**写，`start()` 返回才等于本场结束。
没有它的话断一次就又是一个碎片场次。判据、"默认对压测入口是关的"这两件事见
`test/test_ws_reconnect_unit.py` 顶部。退回旧行为：ExecStart 里加 `--ws-reconnect 0`。

**以下那段坑只有 `DOUYIN_WS_PROXY=1` 时才用得上**，但别删 —— 那条路随时可能被重新启用：

⚠️ WS 那条腿有个坑：`websocket-client` **不认** `http_proxy` 环境变量，必须显式传
`http_proxy_host`/`http_proxy_port`/`http_proxy_auth`；且 1.7.0 的 `run_forever` 把
`proxy_type=None` 原样转发下去，下游 `options.get("proxy_type", "http")` 拿不到默认值，
**必须显式给 `"http"`**，否则必报 `Only http, socks4, socks5 proxy protocols are supported`。
两处都已在 `liveMan.proxy_for_ws()` 里处理，不要「简化」掉 `proxy_type`。

### `check_interval` 不要调小

每主播每轮 1 次 HTTP，`60s` 已是底线。调成 `10s` 就是 6 倍请求量 ——
上面腾讯云那 88 次 444 就是 10s 间隔下发生的。

### 资源（按实测内存模型直接算）

```
峰值 RSS ≈ 94.6 MB + 2.01 MB × 并发连接数
```

| 项 | 结论 |
|---|---|
| 内存 | 100 路 ≈ 300 MB，500 路 ≈ 1.1 GB。**不是瓶颈** |
| 线程 | ≈ 2 × 连接数 + 主播线程数 |
| 句柄 | 每场直播 4 个常开句柄 → `LimitNOFILE=65535` |
| 时区 | 必须 `Asia/Shanghai`，否则目录名与时间戳差 8 小时 |
| `--media` | **默认不要开**，只做小规模全模态验证。开的话**必须同时给 `--max-media N`** —— `auto_crawl` 是「一主播一线程」，没有天然上限，名单里有几个在播就同时起几路 ffmpeg。一路实测 ≈ 1 个 ffmpeg / 0.023 核 / 162 MB / 6.2 Mbps（见附录）。且 `_watchdog` 的长会话续采**尚无 ≥1h 真实直播的验证数据** |
| `--max-sessions` | **2026-10-16 起生产用 10**：并发只采 10 个主播，抢不到的**本轮完全不采**（不建目录/不连 WS/不落数据，只记开播状态）。**非阻塞**，语义与 `--max-media` 相反。上限由**内存**定：新机实测单路 ffmpeg **111 MB**，10 路满载 `available` 剩 536 MB，**无 swap**。⚠️ 改了必须同步 `douyin-ledger.service` 的同名参数 |
| `--min-free-gb` | 磁盘保护，**开 `--media` 就必须留着**（默认 5）。剩余空间低于阈值就不再起新 ffmpeg，那一场降级为纯文本/数值，空间回收后自动恢复 |
| `--ws-reconnect` | **2026-10-16 起默认 10**（不用写进 service 文件）：WS 断线后在**同一场次目录**里连续重连几次。口径是**连续**失败 —— 某条连接活过 15 秒即清零重计，所以「每 10 分钟断一次」的长直播能一直自愈，只有「一连就挂」（房间已下播/被风控）才耗尽。0 = 退回「断一次 = 一个场次」。⚠️ 只有 `auto_crawl` 传它；压测入口（`collect_full`/`load_test`/GUI）刻意不传，「断了就失败」的语义必须保留 |
| **磁盘预算** | 全模态是磁盘大头。单路一场 600s = **34 MB**（帧 14 MB + 音频 20 MB）；旧机实测 ≈ 33 GB/天（1.4~1.5 GB/小时）。⚠️ **2026-10-16 新机再测是 ≈ 2.86 GB/小时 ≈ 68.6 GB/天**（5 分钟窗口 244 MB，`du` 量 `data/` 本体）—— 比旧的 33 GB/天又高出一倍多，主要因为**实测 265 KB/帧**（原始分辨率 `-q:v 2` JPEG，文档里估的 25 KB 严重偏低）。**40G 盘从 29 GB 可用到 5 GB 阈值只有约 8.4 小时**。文本+数值每场只占 ~200 KB（≈ 29 MB/天/路），所以降级到纯文本几乎不损失研究价值。**`--min-free-gb` 只保证「不写满」，不保证「能一直采全模态」** —— 要长期全模态得清 `data/`、降帧率/画质，或扩盘 |
| 落盘目录 | `data/` 是生产目录，**不要把 `data/_load_test/` 混进研究语料**（`room_status` 写「压测」、内容按房间复用而重复） |

### 生产不需要的东西

- **`config/auth.json` 不需要** —— 全程游客态（`DouyinLiveWebFetcher(live_id, None)`），
  `MediaCapture` 的流地址走 fetcher 的 `fetch_live_html`，也不依赖登录态。
- **`test/`（除 `validate_fullmodal.py`）、`load_test.py`、`collect_full.py` 都不需要上传** ——
  它们只用于压测与校验。`metrics.py` 与 `test/validate_fullmodal.py` 平时也不在依赖里，
  但 **5 路测试要用**（量资源 + 验产物真实性），所以 `MANIFEST` 里带了它们；
  纯守护跑不带这两个也完全正常。

## 八、5 主播并发全模态测试

上量前先按 5 路做一次端到端实测（名单 `documents/huitun_10_100w_1015_final.xlsx`，
实测可读到 **157 人**）。采集逻辑全部交给生产的 `auto_crawl --media --max-media 5`，
不另写一套循环（生产还带 `--max-sessions 10`，但 5 路实测时那个闸门不会生效）：

```bash
python service/run_5anchors.py --roster documents/huitun_10_100w_1015_final.xlsx \
    --anchors 5 --duration 600
```

它按名单顺序**限流探测**筛出 5 个在播 → 起 `auto_crawl` 子进程 → 用 `metrics` 采子进程树
（含 ffmpeg）→ 到时 SIGTERM 优雅落盘 → 逐场跑 `validate_fullmodal` 校验 → 写
`service/dist/report_5anchors_<时间>.json`。出报告后**先看这两个数**：

| 看什么 | 判据 |
|---|---|
| **实时倍率** | 稳态窗口内 `音频字节 / 时长 / 32000`。**< 1.0 = 解码跟不上直播实时，数据已在丢** |
| **444 计数** | `> 0` 说明 IP 仍被风控 —— 代理没生效，或需要换出口。**必须按 `风控444\|444 Client Error` 数**，裸 `444` 会把 user_id/时间戳算进来（实测 46 vs 0） |

> ⚠️ 限流不能省：探测 18 req/s 会被 444 拦下 85%，把在播的误判成没播（实测踩过）。

## 附：可选扩展

- **加音视频**：默认部署仅弹幕/事件（轻量）。需要图像/音频时给 `auto_crawl.py` 加 `--media`（或改用单场 `collect_full.py`），两者走**同一套** `liveMan.MediaCapture`：ffmpeg 直接解 FLV 原始分辨率抽帧 + 16kHz 单声道无损 WAV 切片，**不需要 Playwright**（实测 `requests` 拿到的直播间 HTML 里就有 `flv_pull_url`，见 `test/probe_stream_url.py`）。
  ⚠️ **实测单路开销（2026-10-14，端到端驱动生产类 `MediaCapture`）**：一路主播 = **1 个** ffmpeg 进程（音视合并 + `-skip_frame nokey`），约 **0.023 核 CPU / 162 MB RSS / 6.2 Mbps 带宽**。改动前是 **2 个**进程、**0.192 核 / 268 MB / 12.2 Mbps**。
  三个杠杆（都已落地）：**合并单进程**（音频进程即使带 `-vn` 也要读完整条复用 FLV，所以拆两个进程 = 把同一条流读两遍；合并后只读一遍 → **带宽 ÷2**）、**`-skip_frame nokey`**（只解关键帧 → **CPU ÷6**，产出帧数不变）、抽帧频率（**不是杠杆**：实测 fps=1/10 与 fps=1/300 的带宽与 CPU 完全相同）。合计 **带宽 ×0.51 / CPU ×0.12 / RSS ×0.61**。
  **2 核 2G 能带几路**：内存 (2048−250−86) ÷ 164 ≈ **10 路**、CPU ≈ 15 路（vCPU 按桌面的 1/3 估）、100 Mbps 口 ≈ 16 路 → **RAM 单独成为约束，约 10 路**（优化前约 3-5 路，CPU 与 RAM 同时吃紧）。⚠️ **2026-10-16 在新生产机上实测单路是 111 MB（不是这里的 164）**，10 路满载 `available` 只剩 **536 MB** —— 结论仍是「RAM 先撞、上限约 10 路」，但**余量比这张表更薄**（无 swap）。这也是 `--max-sessions 10` 取 10 的原因。
  **瓶颈是带宽与内存，不是 CPU**：本机 Wi-Fi（标称 144 Mbps）实测真实可用吞吐仅 **~4 MB/s ≈ 32 Mbps**，按 6.2 Mbps/路 ≈ 5 路；有线 100 Mbps ≈ 16 路，千兆则不再受带宽约束。磁盘不是约束（5s/帧口径 ≈ 0.10 MB/s/路）。故 `--media` 仍默认关，只做小规模全模态验证。
- **商品数据**：不在代码侧采集，由**灰豚第三方平台**在直播结束后导出。
- **换主播列表**：改单元里 `ExecStart` 的 xlsx 路径（生产指向 `documents/huitun_10_100w_1015_final.xlsx`），
  `systemctl daemon-reload && systemctl restart douyin-crawl`。**必须 restart 才生效** ——
  名单只在进程启动时读一次。`anchor.xlsx` 是 15 人小名单，留作纯文本/调试用。
  列位置**不敏感** —— `load_anchors` 按表头名（`抖音号` / `昵称` 等同义写法）定位列，因此也可以直接用
  `documents/并发测试.xlsx`（5 列，`--sheet 40` 取分档）或 `documents/筛选结果_10w-100w_指定类目.xlsx`
  （1684 人全名单，`--sheet Sheet1`）。
  ⚠️ 人数越多轮询请求量越大（每主播每轮 1 次 HTTP）—— 1684 人 ≈ 28 req/s，**要分组跑，见第七节**。
