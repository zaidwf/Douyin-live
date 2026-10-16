#!/usr/bin/python
# coding:utf-8 -*-
"""与辉同行短测：dump 6 个未注册 method 的原始 protobuf 字段（各限次）

目标：定位 WebcastRoomDataSyncMessage 的讲解字段（item 6），
     并摸清其余 5 个 method 的真实字段结构。
"""
import io
import os
import sys
import gzip
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websocket
from liveMan import DouyinLiveWebFetcher
from protobuf.douyin import PushFrame, Response

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

LIVE_ID = "56697889278"
DURATION = 90  # 秒
# RoomDataSync 是增量同步，可能多次变化，dump 2 次；其余各 1 次
MAX_DUMP = {'WebcastRoomDataSyncMessage': 2}
LINE_CAP = 150  # 单条消息最多打印行数（防 RoomRank 式刷屏）


def dump_fields(data, indent="", depth=0, max_depth=6):
    result = []
    i = 0
    while i < len(data) and depth < max_depth:
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
            result.append(f"{indent}f{fn}(varint)={val}")
        elif wt == 2:  # length-delimited
            ln = 0
            sh = 0
            while i < len(data):
                b = data[i]
                i += 1
                ln |= (b & 0x7f) << sh
                sh += 7
                if not (b & 0x80):
                    break
            chunk = data[i:i + ln]
            i += ln
            try:
                s = chunk.decode('utf-8')
                if s and all(32 <= ord(c) < 127 for c in s):
                    result.append(f"{indent}f{fn}(str)=\"{s}\"")
                    continue
            except Exception:
                pass
            result.append(f"{indent}f{fn}(msg {ln}B):")
            result.extend(dump_fields(chunk, indent + "  ", depth + 1, max_depth))
        elif wt == 1:
            i += 8
            result.append(f"{indent}f{fn}(64bit)")
        elif wt == 5:
            i += 4
            result.append(f"{indent}f{fn}(32bit)")
        else:
            break
    return result


class DumpFetcher(DouyinLiveWebFetcher):
    def __init__(self, live_id):
        super().__init__(live_id, None)
        self.dumped = Counter()

    def _wsOnMessage(self, ws, message):
        package = PushFrame().parse(message)
        response = Response().parse(gzip.decompress(package.payload))
        if response.need_ack:
            try:
                ack = PushFrame(log_id=package.log_id, payload_type='ack',
                                payload=response.internal_ext.encode('utf-8')).SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)
            except Exception:
                pass
        for msg in response.messages_list:
            m = msg.method
            if m in TARGETS and self.dumped[m] < MAX_DUMP.get(m, 1):
                self.dumped[m] += 1
                print(f"\n===== {m} #{self.dumped[m]} ({len(msg.payload)}B) =====")
                lines = dump_fields(msg.payload, "", 0, 6)
                for line in lines[:LINE_CAP]:
                    print(line)
                if len(lines) > LINE_CAP:
                    print(f"    ... 截断（共 {len(lines)} 行）")


TARGETS = {
    'WebcastRoomDataSyncMessage', 'WebcastChatLikeMessage', 'WebcastResidentGuestMessage',
    'WebcastInRoomBannerMessage', 'WebcastRanklistHourEntranceMessage', 'WebcastDataLifeLiveMessage',
}


def main():
    fetcher = DumpFetcher(LIVE_ID)
    try:
        success, status, nickname, user_id = fetcher.get_room_status()
        print(f"主播={nickname}  状态={status}")
    except Exception:
        pass
    print(f"dump 6 个 method（{LIVE_ID}，{DURATION}s）...")
    print("=" * 60)
    t = threading.Thread(target=fetcher.start, daemon=True)
    t.start()
    time.sleep(DURATION)
    fetcher.stop()
    print("\n" + "=" * 60)
    print("已 dump 的 method:")
    for m, c in sorted(fetcher.dumped.items()):
        print(f"    {m}: {c} 次")
    missing = TARGETS - set(fetcher.dumped)
    if missing:
        print(f"未出现（{DURATION}s 内）: {sorted(missing)}")


if __name__ == '__main__':
    main()
