#!/usr/bin/python
# coding:utf-8 -*-
"""观察所有 purchase 事件（LivePopMessage 简短版 + 完整版）

采集 WebSocket 的 WebcastLiveEcomGeneralMessage（type='LivePopMessage'），
完整 dump 简短版（12B）和完整版（50B）的字段，用于判断简短版是否含购买信息。
"""
import io
import sys
import gzip
import threading
import time
from collections import Counter

import websocket
from liveMan import DouyinLiveWebFetcher
from protobuf.douyin import PushFrame, Response, LiveEcomGeneralMessage

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)


def dump_fields(data, indent="", depth=0, max_depth=5):
    """递归 dump protobuf 字段"""
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


class ObservePurchase(DouyinLiveWebFetcher):
    def __init__(self, live_id, max_dump=6):
        super().__init__(live_id, None)
        self.max_dump = max_dump
        self.dumped = 0
        self.size_counter = Counter()

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
            if msg.method != 'WebcastLiveEcomGeneralMessage':
                continue
            e = LiveEcomGeneralMessage().parse(msg.payload)
            if e.type != 'LivePopMessage':
                continue

            biz_len = len(e.biz_content)
            self.size_counter[biz_len] += 1

            if self.dumped < self.max_dump:
                self.dumped += 1
                print(f"\n===== LivePopMessage #{self.dumped} (biz_content {biz_len}B, ts={e.timestamp}) =====")
                for line in dump_fields(e.biz_content):
                    print(line)


def main():
    room_id = "646454278948"
    print(f"观察直播间 {room_id}，采集所有 LivePopMessage（90 秒）...")
    print("=" * 70)

    fetcher = ObservePurchase(room_id, max_dump=8)
    th = threading.Thread(target=fetcher.start, daemon=True)
    th.start()

    time.sleep(90)
    fetcher.stop()

    print("\n" + "=" * 70)
    print("LivePopMessage 大小分布（简短版 vs 完整版）:")
    for size, cnt in fetcher.size_counter.most_common():
        label = "简短版" if size <= 15 else "完整版"
        print(f"  {size}B: {cnt} 条 ({label})")
    print(f"总计: {sum(fetcher.size_counter.values())} 条")


if __name__ == '__main__':
    main()
