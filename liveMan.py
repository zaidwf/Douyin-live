#!/usr/bin/python
# coding:utf-8

import codecs
import gzip
import hashlib
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
            response = requests.get(self.live_url, headers=headers)
            response.raise_for_status()
        except Exception as err:
            self.log("ERROR", f"请求直播URL错误: {err}")
        else:
            self.__ttwid = response.cookies.get('ttwid')
            return self.__ttwid

    @property
    def room_id(self):
        """
        根据直播间的地址获取到真正的直播间roomId，有时会有错误，可以重试请求解决
        :return:room_id
        """
        if self.__room_id:
            return self.__room_id
        url = self.live_url + self.live_id
        headers = {**COMMON_HEADERS,
                   "cookie": f"ttwid={self.ttwid}&msToken={generateMsToken()}; __ac_nonce=0123407cc00a9e438deb4"}
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
        except Exception as err:
            self.log("ERROR", f"请求直播间URL错误: {err}")
        else:
            match = re.search(r'roomId\\":\\"(\d+)\\"', response.text)
            if match is None or len(match.groups()) < 1:
                self.log("ERROR", "未找到匹配的roomId（可能未开播）")
                return None

            self.__room_id = match.group(1)
            return self.__room_id

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
            })
            resp.raise_for_status()
            data = resp.json().get('data')
            if data:
                room_status = data.get('room_status')
                user = data.get('user')
                user_id = user.get('id_str')
                nickname = user.get('nickname')
                status = '正在直播' if room_status == 0 else '已结束'
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
            )
            response.raise_for_status()
            data = json.loads(response.text)

            if 'data' not in data or 'ranks' not in data['data']:
                self.log("ERROR", "未获取到排名数据，请检查输入的房间ID和主播ID是否正确")
                return []

            ranks = data['data']['ranks']
            account_list = []
            for rank in ranks:
                if 'user' in rank and 'id' in rank['user']:
                    user = rank['user']
                    account_info = {
                        'id': user.get('id', '未知'),
                        'nickname': user.get('nickname', '未知昵称'),
                        'display_id': user.get('display_id', '')
                    }
                    account_list.append(account_info)
                else:
                    self.log("WARN", f"警告：第{rank.get('rank', '未知')}位用户数据缺失")

            self.log("RANK", f"成功获取到 {len(account_list)} 个账号信息")
            return account_list
        except Exception as e:
            self.log("ERROR", f"获取观众用户数据时出错: {str(e)}")
            return []

    def get_product_detail(self, promotion_id):
        """
        获取商品详情（标题/价格/图片）。
        注意：抖音商城商品详情 JSON API 需进一步逆向（含签名），
        当前返回商品详情页 URL，供后续增强。
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

    def _connectWebSocket(self):
        """
        连接抖音直播间websocket服务器，请求直播间数据
        """
        if not self.room_id:
            self.log("ERROR", "无法获取room_id，无法连接WebSocket")
            return

        wss = ("wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
               "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
               "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
               "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
               "&browser_name=Mozilla"
               "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
               "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
               "&browser_online=true&tz_name=Asia/Shanghai"
               "&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
               f"&internal_ext=internal_src:dim|wss_push_room_id:{self.room_id}|wss_push_did:7319483754668557238"
               f"|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|wss_info:0-1721106114633-0-0|"
               f"wrds_v:7392094459690748497"
               f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
               f"&user_unique_id=7319483754668557238&im_path=/webcast/im/fetch/&identity=audience"
               f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={self.room_id}&heartbeatDuration=0")

        signature = generateSignature(wss)
        wss += f"&signature={signature}"

        headers = {
            "cookie": f"ttwid={self.ttwid}",
            'user-agent': self.user_agent,
        }

        self.log("WEBSOCKET", f"正在连接WebSocket: {wss[:100]}...")

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

    def _wsOnMessage(self, ws, message):
        """
        接收到数据
        :param ws: websocket实例
        :param message: 数据
        """

        # 根据proto结构体解析对象
        package = PushFrame().parse(message)
        response = Response().parse(gzip.decompress(package.payload))

        # 返回直播间服务器链接存活确认消息，便于持续获取数据
        if response.need_ack:
            try:
                ack = PushFrame(log_id=package.log_id,
                                payload_type='ack',
                                payload=response.internal_ext.encode('utf-8')
                                ).SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                self.log("ERROR", f"发送ACK时出错: {str(e)}")

        # 根据消息类别解析消息体
        for msg in response.messages_list:
            method = msg.method
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
                }.get(method)
                if handler:
                    handler(msg.payload)
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
        self._emit_data("chat", {"user_id": str(user_id), "nickname": user_name, "content": content})

    def _parseGiftMsg(self, payload):
        """礼物消息"""
        message = GiftMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        gift_name = message.gift.name
        gift_cnt = message.combo_count
        diamond_count = message.gift.diamond_count
        self.log("GIFT", f"{user_name} 送出了 {gift_name}x{gift_cnt}")
        self._emit_data("gift", {"user_id": str(user_id), "nickname": user_name,
                                 "gift_name": gift_name, "gift_count": gift_cnt,
                                 "diamond_value": diamond_count})

    def _parseLikeMsg(self, payload):
        '''点赞消息（total 为直播间累计点赞，count 为本次增量）'''
        message = LikeMessage().parse(payload)
        user_name = message.user.nick_name
        count = message.count
        total = message.total
        self.log("LIKE", f"{user_name} 点了{count}个赞，累计 {total}")
        self._emit_data("like", {"nickname": user_name, "count": count, "total": total})

    def _parseMemberMsg(self, payload):
        '''进入直播间消息'''
        message = MemberMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        gender = ["女", "男"][message.user.gender] if message.user.gender in (0, 1) else "未知"
        self.log("ENTER", f"[{user_id}][{gender}]{user_name} 进入了直播间")
        self._emit_data("enter", {"user_id": str(user_id), "nickname": user_name, "gender": gender})

    def _parseSocialMsg(self, payload):
        '''社交消息（关注/分享，按 action 区分）

        action 语义（与辉同行 1h 实测 2026-09-17 定案）：
          1 = 关注（follow_count 为实时粉丝总数，单调递增）
          3 = 分享（follow_count 恒为 0，因为分享不改变粉丝数；share_type/share_target 区分分享渠道）
          2（取消关注）在整段观测中未出现，属罕见事件。
        '''
        message = SocialMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        action = message.action
        follow_count = message.follow_count
        share_type = message.share_type
        share_target = message.share_target
        if action == 1:
            self.log("FOLLOW", f"[{user_id}]{user_name} 关注了主播 (粉丝数 {follow_count})")
            self._emit_data("follow", {"user_id": str(user_id), "nickname": user_name,
                                       "action": action, "follow_count": follow_count})
        else:
            self.log("SOCIAL", f"[{user_id}]{user_name} action={action}(分享), share_type={share_type}, "
                               f"share_target={share_target!r}, 粉丝数={follow_count}")
            self._emit_data("social", {"user_id": str(user_id), "nickname": user_name,
                                       "action": action, "follow_count": follow_count,
                                       "share_type": share_type, "share_target": share_target})

    def _parseRoomUserSeqMsg(self, payload):
        '''直播间统计'''
        message = RoomUserSeqMessage().parse(payload)
        current = message.total
        total = message.total_pv_for_anchor
        self.log("STATS", f"当前观看人数: {current}, 累计观看人数: {total}")
        self._emit_data("stats", {"viewer_count": current, "total_pv": total})

    def _parseFansclubMsg(self, payload):
        '''粉丝团消息'''
        message = FansclubMessage().parse(payload)
        content = message.content
        self.log("FANSCLUB", content)

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
        self.log("RANK", f"用户数据: {ranks_list}")

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


if __name__ == '__main__':
    # 服务器无 GUI/tkinter 时仅打印提示，不启动图形界面（避免 AttributeError: 'NoneType'）
    if tk is None:
        print("未安装 tkinter，无法启动图形界面（Linux 服务器无 GUI 属正常，可用 auto_crawl.py 等脚本采集）。")
    else:
        root = tk.Tk()
        app = DouyinLiveApp(root)
        root.mainloop()