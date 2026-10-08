#!/usr/bin/python
# coding:utf-8 -*-
"""结构化数据落盘模块 - 将直播间采集数据写入 JSON/JSONL 供 MEMF-GEF 模型训练"""
import json
import os
from collections import Counter
from datetime import datetime


def new_date_dir(base_dir="data", ts=None):
    """返回**按日期**分组的落盘目录：`data/<YYYYMMDD>/`（不存在则创建）

    所有场次按**开播日期**归入同一天目录，避免 `data/` 下几十上百个 `live_*` 平铺
    （2026-09-29 前有 101 个）。

    ⚠️ **由入口在「确认开播」之后调用**（`collect_full.py` 单场 / `auto_crawl.py` 长跑），
    把返回值当 `base_dir` 传给 `DataRecorder`。**不要在 `DataRecorder` 内部建** ——
    那是另一层（每个 `DataRecorder` 一个 `live_*` 目录），写方法里建会把两个层级混在一起。

    **并发安全，无需加锁**：多个主播线程同时调用得到的是**同一个**目录名，
    `os.makedirs(exist_ok=True)` 本身幂等（EEXIST 被吞掉）。

    ⚠️ **日期在调用时刻解析一次，只在开播时调**：一场 23:50 开播的直播落在
    `data/20260929/` 就一直写到底，跨零点也不换目录，直到下播；00:05 开播的另一场
    则落在 `data/20260930/`。这是「一场直播不停播就不动文件夹」的保证 ——
    场次目录由 `DataRecorder` 构造时定死，日期目录只影响**此后新开**的场次。

    :param base_dir: 数据根目录，默认 `data`
    :param ts: 用哪个时刻定日期（默认现在），单测用
    """
    path = os.path.join(base_dir, (ts or datetime.now()).strftime('%Y%m%d'))
    os.makedirs(path, exist_ok=True)
    return path


class DataRecorder:
    """收集并落盘直播间结构化数据

    输出目录结构:
        data/<YYYYMMDD>/                 # 按开播日期分组（见 new_date_dir）
            live_{live_id}_{date}/       # 单场（每次 DataRecorder 一个）
                room.json       # 直播间/主播/商品静态信息
                danmaku.jsonl   # 弹幕流（逐条）
                events.jsonl    # 点赞/购买/关注/分享/进出/商品事件流
                                # （礼物事件通道不下发，恒为空；见 __init__ 注释）
                series.json     # 时序指标序列（Time-Slice 节点）
                series.jsonl    # 同上，边采边写（中断不丢）
                fansclub_edges.jsonl  # 粉丝团关系边（ν_Au → ν_An，逐用户）
                audio/ frames/       # 音视频模态，走 liveMan.MediaCapture；
                                     # collect_full.py 恒产出，auto_crawl.py 仅带 --media 时产出
                audio/ffmpeg.log     # 该路 ffmpeg 的 stderr（长会话续采排查用）

    单独构造 `DataRecorder(...)`（不传 base_dir）时仍只建 `data/live_*` 一层，
    用于单测；生产入口一律先 `new_date_dir()` 再传进来。
    """

    # 用户 ID 打码占位值：部分房间（如娱乐团播）把全体 user_id 置为该值，按用户维度的特征全部失效
    MASKED_UID = '111111'
    # 判定打码所需的最少观测数（避免小样本误判）
    MASKED_MIN_OBS = 20

    def __init__(self, room_id, live_id, base_dir="data"):
        self.room_id = room_id
        self.live_id = live_id
        date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.output_dir = os.path.join(base_dir, f"live_{live_id}_{date_str}")
        os.makedirs(self.output_dir, exist_ok=True)

        self.danmaku_file = open(os.path.join(self.output_dir, 'danmaku.jsonl'), 'w', encoding='utf-8')
        self.events_file = open(os.path.join(self.output_dir, 'events.jsonl'), 'w', encoding='utf-8')
        self.series_file = open(os.path.join(self.output_dir, 'series.jsonl'), 'w', encoding='utf-8')
        self.fansclub_edges_file = open(os.path.join(self.output_dir, 'fansclub_edges.jsonl'),
                                        'w', encoding='utf-8')

        self.room_info = {}
        self.viewer_series = []       # 观看人数时序（内存，close 时汇总 series.json）
        self.like_total = 0           # 直播间累计点赞（LikeMessage.total，权威累计值）
        self.follow_count = None      # 粉丝总数（SocialMessage.follow_count，非单调，取最新快照）
        # 礼物聚合字段已移除（2026-09-28 定案）：Web IM 推送通道不承载 WebcastGiftMessage，
        # 抖音官方网页自身的 WS 同样收不到（见 modify.md 2026-09-28 续3）。
        # 因此 gift 事件恒为 0，累计件数/价值/送礼人数只会是恒 0 的假特征，不再输出。
        # liveMan 的解析器保留，通道若恢复可再从 events.jsonl 重算。
        self.product_ids = set()
        self.product_times = {}       # promotion_id -> {first_seen, last_seen, sale_count, explain_count}
        self.products = []            # 商品详情列表（含名称/价格/销量/图片）
        self.promotion_order = []     # 商品列表刷新顺序（best-effort 对齐面板商品 num）
        self.shopping_down_count = 0  # 下架/讲解结束事件数（LiveShoppingMessage msg_type=3，promotionId 不可靠仅聚合计数）

        # ---- 粉丝团群体级特征（2026-09-28 接入，见 modify.md 续5/续6）----
        # 标识来源：user.fans_club.data（User 字段 24 → data 字段 1），任意携带 User 的消息都可读。
        # 判「是否成员」用 level > 0（与 anchor_id != 0 完全等价，实测零例外）。
        self.fansclub_users = {}       # user_id -> (level, anchor_id)，取**首次见到**的快照
        self.fansclub_member_count = 0  # level>0 的去重用户数（增量维护）
        self.fansclub_anchor_users = Counter()   # anchor_id -> 成员用户数（关系边聚合）
        self.fansclub_level_dist = Counter()     # level -> 成员用户数（仅 level>0）
        self.fansclub_event_types = Counter()    # FansclubMessage.type -> 次数（原样记录，不作语义解释）
        self.fansclub_event_count = 0            # FansclubMessage 总条数（**全量，不分 type**）
        # ⚠️ 计数口径（2026-09-29 回退后）：`fansclub_event_count` 是**全量**计数，不分 type。
        #    实测其中 ~95% 是 type=6（静默消息、content 恒空），type=1(升级)/2(加入) 只占 ~5%。
        #    type=6 的语义曾定标为「灯牌点亮」，但**已决定不采用**：其获取完整性不可测
        #    （非循环翻转见证检验在本语料上取不到样本），且到达高度爆发（单场 40 倍方差）。
        #    详见 modify.md 2026-09-29（续）的「舍弃决定」节；按 type 的拆分不再单列字段，
        #    需要时从 `event_type_dist` 原始分布里取。

        # ---- 加入粉丝团（2026-09-29 引入）：**明确命名**的分型计数 + 服务端累计序号 ----
        # 上一条「命名必须与口径一一对应」的教训在此落地：下面每个字段名都写明了自己数的是什么。
        # type=2(加入)/type=1(升级) 只占全部 FansclubMessage 的 ~5%（~0.1 条/分，极稀疏），
        # 但二者是**带明文公告的里程碑事件**，语义无需定标（正文自带「成为第 N 名…成员」/
        # 「升级至…Lv N」）。与已舍弃的 type=6 的根本差别：这里没有语义推断，也没有爆发式到达。
        self.fansclub_join_count = 0       # type=2「恭喜…成为第 N 名…成员」条数
        self.fansclub_upgrade_count = 0    # type=1「…刚刚升级至【…】粉丝团 Lv N」条数
        self.fansclub_join_seq_first = None  # 本场首个 join 序号（服务端累计加入序号）
        self.fansclub_join_seq_latest = None  # 本场最新的 join 序号（单调不减；差分得**无偏**加入数）
        self.fansclub_club_names = {}      # anchor_id(str) -> 团名（**只从 type=2 取**，type=1 的方括号是主播名）
        self.fansclub_unparsed = Counter()  # type -> 未命中模板的条数（模板缺口监控，见 room.json）
        self._uid_obs = 0              # 带 user_id 的观测次数
        self._uid_nonmasked = 0        # 其中 user_id 非打码占位值的次数

    def on_event(self, event_type, data):
        """接收结构化事件，分类落盘

        :param event_type: chat / gift / like / enter / follow / social / fansclub /
                           purchase / shopping / stats / product
        :param data: dict，事件数据（不含时间戳，由本方法补充）
        """
        ts = datetime.now().isoformat(timespec='seconds')
        data = dict(data)
        data['ts'] = ts
        # 粉丝团标识随任意携带 User 的消息而来，这里统一汇总（群体级特征原料）
        self._record_fansclub(data, ts)

        if event_type == 'chat':
            self.danmaku_file.write(json.dumps(data, ensure_ascii=False) + '\n')
            self.danmaku_file.flush()
        elif event_type == 'fansclub':
            # 粉丝团消息：全量计数 + type 原始分布 + 加入/升级分型计数，并落 events.jsonl
            et = data.get('fc_event_type')
            self.fansclub_event_types[str(et)] += 1
            self.fansclub_event_count += 1
            if et == 2:
                self.fansclub_join_count += 1
                seq = data.get('fc_join_seq')
                if isinstance(seq, int):
                    if self.fansclub_join_seq_first is None:
                        self.fansclub_join_seq_first = seq
                    # 单调维护：序号理论单调递增，取 max 防止（乱序/异常）回退污染差分
                    prev = self.fansclub_join_seq_latest
                    self.fansclub_join_seq_latest = seq if prev is None else max(prev, seq)
                club = data.get('fc_club_name')
                if club:
                    aid = data.get('fansclub_anchor_id')
                    if aid:
                        self.fansclub_club_names[str(aid)] = club
            elif et == 1:
                self.fansclub_upgrade_count += 1
            # 模板缺口监控：type=1/2 本应有明文公告可解析，未命中即登记（不静默）
            if et in (1, 2) and not data.get('fc_parse_ok'):
                self.fansclub_unparsed[str(et)] += 1
            self.events_file.write(json.dumps({'type': event_type, **data}, ensure_ascii=False) + '\n')
            self.events_file.flush()
        elif event_type == 'stats':
            # 观看人数时序：边采边写 series.jsonl（中断不丢），close 时汇总 series.json
            if 'viewer_count' in data:
                # Time-Slice 节点附带粉丝团群体级快照（随时间漂移；事件计数均为累计值）
                data['fansclub_user_count'] = len(self.fansclub_users)
                data['fansclub_member_count'] = self.fansclub_member_count
                data['fansclub_member_ratio'] = self._fansclub_ratio()
                # 粉丝团消息累计条数（**全量，不分 type**）
                data['fansclub_event_count'] = self.fansclub_event_count
                # 加入/升级分型累计（~5%，稀疏但语义明确）
                data['fansclub_join_count'] = self.fansclub_join_count
                data['fansclub_upgrade_count'] = self.fansclub_upgrade_count
                # 服务端累计加入序号（**无采样偏差**）：沿 series 差分 = 该窗**真实**新增成员数
                data['fansclub_join_seq_latest'] = self.fansclub_join_seq_latest
                self.viewer_series.append(data)
                self.series_file.write(json.dumps(data, ensure_ascii=False) + '\n')
                self.series_file.flush()
        elif event_type == 'product':
            pid = data.get('promotion_id')
            if pid:
                self.product_ids.add(pid)
                self._record_product_time(pid, None, ts)
        elif event_type == 'shopping':
            # 记录商品状态（msg_type=2 成交事件，3 下架）。LiveShoppingMessage.promotionId（字段3）
            # 经灰豚验证并非真正 promotion_id，故这里只记录商品出现时间，不累加 sale_count。
            pid = data.get('promotion_id')
            if pid and pid != '0':
                self.product_ids.add(pid)
                self._record_product_time(pid, None, ts)
            if data.get('msg_type') == 3:
                self.shopping_down_count += 1  # 下架/讲解结束（无法可靠映射到具体商品，仅计数）
            self.events_file.write(json.dumps({'type': event_type, **data}, ensure_ascii=False) + '\n')
            self.events_file.flush()
        elif event_type == 'purchase':
            # 成交事件：ids 为 LivePopMessage f6.f2 提取的 promotion_id（Bug 2 定案后的正确字段）
            # 每个 id 累加一次 sale_count（成交次数）
            for pid in data.get('ids', []):
                pid = str(pid)
                if pid:
                    self.product_ids.add(pid)
                    self._record_product_time(pid, 2, ts)
            self.events_file.write(json.dumps({'type': event_type, **data}, ensure_ascii=False) + '\n')
            self.events_file.flush()
        elif event_type == 'explain':
            # 商品讲解状态：两种来源
            #   - AwemeShopExplainMessage（active bool）：权威"是否正在讲解"信号
            #   - ProductChangeMessage（explainType int）：全网无枚举定义，仅记录原始分布
            pid = data.get('promotion_id')
            if pid:
                self.product_ids.add(pid)
                if 'active' in data:
                    self._record_explain_active(pid, data['active'], ts)
                else:
                    self._record_explain(pid, data.get('explain_type'), ts)
            self.events_file.write(json.dumps({'type': event_type, **data}, ensure_ascii=False) + '\n')
            self.events_file.flush()
        elif event_type == 'like':
            # 点赞：total 为直播间累计点赞（权威），count 为本次增量（被抖音节流，不作为总量）
            total = data.get('total')
            if isinstance(total, (int, float)):
                self.like_total = max(self.like_total, total)
            self.events_file.write(json.dumps({'type': event_type, **data}, ensure_ascii=False) + '\n')
            self.events_file.flush()
        elif event_type == 'gift':
            # 礼物事件只写 events.jsonl（原始事件流），**不再产出聚合特征**。
            # 2026-09-28 定案：Web IM 推送通道不承载 WebcastGiftMessage（官方网页自身 WS 亦收不到），
            # gift 事件恒为 0，任何聚合都只会得到恒 0 的假特征。
            # 保留写入是为了通道若恢复时能直接从原始流重算；届时聚合需注意：
            # 连击收尾消息（repeat_end==1 且 group_id>0）是"连击串结束"的重置信号，不代表新增件数，
            # 聚合时应跳过（参考 DouyinBarrageGrab：repeatEnd==1 && groupId>0 直接丢弃）；
            # 单发礼物（group_id==0）即使 repeat_end 也照常计入。
            self.events_file.write(json.dumps({'type': event_type, **data}, ensure_ascii=False) + '\n')
            self.events_file.flush()
        else:
            # enter / follow / social
            self.events_file.write(json.dumps({'type': event_type, **data}, ensure_ascii=False) + '\n')
            self.events_file.flush()
            if event_type == 'follow':
                fc = data.get('follow_count')
                if isinstance(fc, (int, float)):
                    # 主播实时粉丝总数快照（取最新值）。注意：**非单调递增** —— 实测 1h 内
                    # 486 次关注事件中 72 次出现下降（-1 ~ -63），因取关会拉低总数。
                    # 分享事件（action=3）的 follow_count 恒为 0，故只认 follow 事件，避免清零。
                    self.follow_count = fc

    def _record_fansclub(self, data, ts):
        """汇总用户粉丝团标识（群体级特征 + 关系边的原料）

        标识字段由 `liveMan._fansclub_of()` 从 `User.FansClub`（User 字段 24）提取，
        随 chat/enter/like/follow/social/fansclub 事件一并带来。

        口径：
          - 「是否成员」= `fansclub_level > 0`（与 `fansclub_anchor_id != 0` 完全等价）
          - 聚合按用户**去重**，取**首次见到**的快照（等级会随时间变化，群体级只看构成）
          - `fansclub_status`（0/1/2）**未定标**，只写关系边原始值，不作特征
          - 落盘 `fansclub_edges.jsonl` 时只写**成员**（level>0），非成员不构成 ν_Au → ν_An 边
        """
        uid = data.get('user_id')
        if not uid:
            return
        uid = str(uid)
        self._uid_obs += 1
        if uid != self.MASKED_UID:
            self._uid_nonmasked += 1

        lvl = data.get('fansclub_level')
        if lvl is None:
            return
        lvl = int(lvl)
        aid = int(data.get('fansclub_anchor_id') or 0)
        if uid in self.fansclub_users:
            return
        self.fansclub_users[uid] = (lvl, aid)
        if lvl > 0:
            self.fansclub_member_count += 1
            self.fansclub_level_dist[lvl] += 1
            if aid:
                self.fansclub_anchor_users[aid] += 1
            # 关系边：ν_Au → ν_An（观众 → 主播），带首次出现时间与等级
            self.fansclub_edges_file.write(json.dumps({
                'type': 'fansclub_of',
                'user_id': uid,
                'anchor_id': str(aid),
                'level': lvl,
                'status': data.get('fansclub_status'),  # 未定标，仅留原始值待定标
                'club_name': data.get('fansclub_name', ''),
                'first_seen': ts,
            }, ensure_ascii=False) + '\n')
            self.fansclub_edges_file.flush()

    def _uid_is_masked(self):
        """user_id 是否被打码（部分房间把全体 user_id 置为 111111，按用户维度的特征全部失效）

        实测（2026-09-28）：爱乐-L（娱乐团播）100% 打码、与辉同行（带货）0% 打码。
        判定：观测数达到阈值且**没有见过任何非打码值**。
        """
        return (self._uid_obs >= self.MASKED_MIN_OBS and self._uid_nonmasked == 0)

    def _fansclub_ratio(self):
        """粉丝团成员占比（群体级核心特征，实测与辉同行 48.2%）"""
        n = len(self.fansclub_users)
        return round(self.fansclub_member_count / n, 4) if n else None

    def _record_product_time(self, pid, msg_type, ts):
        """记录商品状态时间：首次/最后出现时间 + 成交次数

        msg_type==2 为成交/下单（与 LivePopMessage 购买通知一一对应），累加到 sale_count。
        explain_count（讲解次数）待接入 ProductChangeMessage（explainType）后再填充，当前恒为 0。
        """
        entry = self.product_times.setdefault(pid, {
            'first_seen': ts, 'last_seen': ts, 'sale_count': 0, 'explain_count': 0,
        })
        entry['last_seen'] = ts
        if msg_type == 2:
            entry['sale_count'] += 1

    def _record_explain(self, pid, explain_type, ts):
        """记录商品讲解状态（ProductChangeMessage 的 explainType）

        explainType 语义待实测确认（初步假设 0=未讲解, 1=讲解中, 2=已讲解），
        此处只记录取值分布到 explain_types，不做语义解释。
        """
        entry = self.product_times.setdefault(pid, {
            'first_seen': ts, 'last_seen': ts, 'sale_count': 0, 'explain_count': 0,
            'explain_types': {},
        })
        entry['last_seen'] = ts
        explain_types = entry.setdefault('explain_types', {})
        et = str(explain_type)
        explain_types[et] = explain_types.get(et, 0) + 1

    def _record_explain_active(self, pid, active, ts):
        """记录讲解状态（AwemeShopExplainMessage.Extra.active，bool）

        讲解次数 explain_count 取 active 上升沿（False→True）计数：每次"开始讲解"计 1 次，
        讲解结束（True→False）只更新状态不计次。原始 true/false 分布存入 explain_active_dist。
        """
        entry = self.product_times.setdefault(pid, {
            'first_seen': ts, 'last_seen': ts, 'sale_count': 0, 'explain_count': 0,
        })
        entry['last_seen'] = ts
        active = bool(active)
        prev = entry.get('_explain_active')
        if active and not prev:
            entry['explain_count'] = entry.get('explain_count', 0) + 1
        entry['_explain_active'] = active
        dist = entry.setdefault('explain_active_dist', {})
        k = 'true' if active else 'false'
        dist[k] = dist.get(k, 0) + 1

    def set_promotion_order(self, order):
        """设置商品列表刷新顺序（promotion_id 有序列表，用于 best-effort 对齐面板商品 num）"""
        self.promotion_order = list(order)

    def _promotion_id_order(self):
        """返回用于对齐面板商品的 promotion_id 有序列表

        优先商品列表刷新顺序（ProductRefreshMessage，与橱窗顺序一致）；
        兜底用成交过的商品（sale_count > 0）按首次出现时间排序。
        """
        if self.promotion_order:
            return [str(p) for p in self.promotion_order]
        sold = [(pid, t['first_seen']) for pid, t in self.product_times.items()
                if t.get('sale_count', 0) > 0]
        sold.sort(key=lambda x: x[1])
        return [str(pid) for pid, _ in sold]

    def set_room_info(self, info):
        """设置直播间/主播静态信息（room.json）"""
        self.room_info = info

    def set_products(self, products):
        """设置商品详情列表（名称/价格/销量/图片）"""
        self.products = products

    def close(self):
        """写 room.json 和 series.json，关闭所有文件"""
        # 商品：面板详情（best-effort 附 promotion_id）+ 权威讲解时间 promotions（不丢）
        commodities = []
        pid_order = self._promotion_id_order()
        if self.products:
            products_sorted = sorted(self.products, key=lambda p: p.get('num', 0))
            for i, prod in enumerate(products_sorted):
                item = dict(prod)
                if i < len(pid_order):
                    item['promotion_id'] = pid_order[i]
                commodities.append(item)

        # 权威 promotion_id 讲解时间（始终保留，供灰豚 Excel 等事后关联）
        promotions = []
        for pid in sorted(self.product_ids):
            item = {'promotion_id': str(pid)}
            t = self.product_times.get(pid)
            if t:
                item['first_seen'] = t['first_seen']
                item['last_seen'] = t['last_seen']
                item['sale_count'] = t.get('sale_count', 0)       # 成交次数（msg_type=2 累加）
                item['explain_count'] = t.get('explain_count', 0)  # 讲解次数（AwemeShopExplain active 上升沿计数）
                item['explain_types'] = t.get('explain_types', {})  # explainType 取值分布（ProductChange 原始数据）
                if 'explain_active_dist' in t:
                    item['explain_active_dist'] = t['explain_active_dist']  # 讲解 active true/false 分布
            promotions.append(item)

        # 粉丝团群体级特征（Audience-Group 实体画像 + 关系边聚合）
        members = self.fansclub_member_count
        lv_sum = sum(lv * c for lv, c in self.fansclub_level_dist.items())
        fansclub = {
            # —— 群体级构成（核心特征）——
            'user_count': len(self.fansclub_users),   # 有粉丝团标识的去重用户数（分母）
            'member_count': members,                  # level>0 的去重用户数
            'member_ratio': self._fansclub_ratio(),   # ★ 成员占比（实测与辉同行 0.482）
            'level_mean': round(lv_sum / members, 2) if members else None,  # ★ 成员平均等级
            'level_dist': {str(k): v for k, v in sorted(self.fansclub_level_dist.items())},  # ★ 等级分布
            'anchor_member_counts': dict(self.fansclub_anchor_users.most_common()),  # 每个粉丝团的成员数
            # —— 粉丝团消息事件（时序信号；带精确时间戳，见 events.jsonl 的 fansclub 事件）——
            'event_count': self.fansclub_event_count,  # ★ 全部 FansclubMessage 条数（全量，不分 type）
            # 按 type 的原始分布。需要分型计数时从这里取，不再单列字段：
            # 实测 ~95% 是 type=6（静默、content 恒空），type=1(升级)/2(加入) 只占 ~5%。
            'event_type_dist': dict(self.fansclub_event_types),
            # —— 加入粉丝团（2026-09-29 引入，见 __init__ 注释）——
            'join_count': self.fansclub_join_count,        # ★ type=2 明文加入公告条数
            'upgrade_count': self.fansclub_upgrade_count,  # ★ type=1 明文升级公告条数
            # ★ 服务端权威累计加入序号（「第 N 名…成员」的 N）。单调不减，**不受我们的采样影响**：
            # 两值之差 = 期间**真实**新增成员数（不是我们观测到的数）。代价是 type=2 极稀疏
            # （~0.1 条/分），差分分辨率低。逐窗序列见 series.jsonl 的 fansclub_join_seq_latest。
            'join_seq_first': self.fansclub_join_seq_first,
            'join_seq_latest': self.fansclub_join_seq_latest,
            # 团名映射（**只来自 type=2**；type=1 方括号里是主播名，不是团名）。
            # 这是带货房间团名的唯一来源（user.fans_club.data.club_name 在带货恒为空）。
            'club_names': dict(self.fansclub_club_names),
            # 模板缺口监控：type=1/2 本应有明文公告，未命中已知模板的条数。
            # 出现非 0 说明抖音改了文案模板 —— 此时该场的 join_seq 不可用，需回看 events.jsonl 的 content。
            'unparsed_by_type': dict(self.fansclub_unparsed),
            # —— 可信度标记 ——
            # user_id 被打码的房间（实测娱乐团播 100%）所有按用户去重的指标都退化为 1，特征不可信
            'uid_masked': self._uid_is_masked(),
        }

        room_data = {
            'room_id': self.room_id,
            'live_id': self.live_id,
            **self.room_info,
            'like_total': self.like_total,           # 直播间累计点赞（LikeMessage.total）
            'follow_count': self.follow_count,        # 粉丝总数（SocialMessage.follow_count）
            'shopping_down_count': self.shopping_down_count,  # 下架/讲解结束事件数（msg_type=3）
            # 礼物聚合字段（gift_total_count / gift_total_diamond / gift_user_count）已移除，
            # 原因见 __init__ 注释：Web IM 通道不承载 WebcastGiftMessage，值恒为 0。
            'fansclub': fansclub,                     # 粉丝团群体级特征（见 __init__ 注释）
            'commodities': commodities,
            'promotions': promotions,
        }
        with open(os.path.join(self.output_dir, 'room.json'), 'w', encoding='utf-8') as f:
            json.dump(room_data, f, ensure_ascii=False, indent=2)

        # 写 series.json（时序指标汇总；中断场景见 series.jsonl）
        with open(os.path.join(self.output_dir, 'series.json'), 'w', encoding='utf-8') as f:
            json.dump({'viewer_series': self.viewer_series}, f, ensure_ascii=False, indent=2)

        self.danmaku_file.close()
        self.events_file.close()
        self.series_file.close()
        self.fansclub_edges_file.close()

    @property
    def path(self):
        return self.output_dir
