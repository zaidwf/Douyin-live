#!/usr/bin/python
# coding:utf-8 -*-
"""观察房间数据同步消息，定位商品橱窗列表"""
import io
import sys
import gzip
import threading
import time
from collections import Counter

import websocket
from liveMan import DouyinLiveWebFetcher
from protobuf.douyin import PushFrame, Response

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

LIVE_ID = "646454278948"


def dump_fields(data, indent="", max_depth=5):
    result = []
    i = 0
    while i < len(data) and indent.count('  ') < max_depth:
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
        if wt == 0:
            v = 0
            sh = 0
            while i < len(data):
                b = data[i]; i += 1
                v |= (b & 0x7f) << sh
                sh += 7
                if not (b & 0x80): break
            result.append(f"{indent}f{fn}(varint)={v}")
        elif wt == 2:
            ln = 0
            sh = 0
            while i < len(data):
                b = data[i]; i += 1
                ln |= (b & 0x7f) << sh
                sh += 7
                if not (b & 0x80): break
            chunk = data[i:i+ln]
            i += ln
            try:
                s = chunk.decode('utf-8')
                if s and all(32 <= ord(c) < 127 or c in '\n\r\t' for c in s):
                    result.append(f"{indent}f{fn}(str)='{s}'")
                    continue
            except Exception:
                pass
            sub = dump_fields(chunk, indent + "  ", max_depth)
            result.append(f"{indent}f{fn}(msg {ln}B):")
            result.extend(sub)
        else:
            i += 8 if wt == 1 else (4 if wt == 5 else 0)
            result.append(f"{indent}f{fn}(wire{wt})")
    return result


class ObserveFetcher(DouyinLiveWebFetcher):
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
            if m in ('WebcastRoomDataSyncMessage', 'WebcastProductChangeMessage') and self.dumped[m] < 1:
                self.dumped[m] += 1
                print(f"\n===== {m} ({len(msg.payload)}B) =====")
                for line in dump_fields(msg.payload, "", 5):
                    print(line)
                print()


def main():
    print(f"观察 {LIVE_ID}，120 秒")
    fetcher = ObserveFetcher(LIVE_ID)
    t = threading.Thread(target=fetcher.start, daemon=True)
    t.start()
    time.sleep(120)
    fetcher.stop()
    print("\n完成")


if __name__ == '__main__':
    main()
