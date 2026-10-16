#!/usr/bin/python
# coding:utf-8 -*-
"""采集 3 分钟 purchase 事件（LivePopMessage 简短版 + 完整版）

保存所有 LivePopMessage 的完整字段到 purchase_events.jsonl，方便查看简短版/完整版字段。
"""
import io
import sys
import gzip
import json
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import websocket
from liveMan import DouyinLiveWebFetcher
from protobuf.douyin import PushFrame, Response, LiveEcomGeneralMessage

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)


def parse_fields(data, depth=0, max_depth=5):
    """递归解析 protobuf 字段，返回结构化 dict"""
    fields = {}
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

        if wt == 0:
            val = 0
            sh = 0
            while i < len(data):
                b = data[i]
                i += 1
                val |= (b & 0x7f) << sh
                sh += 7
                if not (b & 0x80):
                    break
            fields[f'f{fn}'] = val
        elif wt == 2:
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
                    fields[f'f{fn}'] = s
                    continue
            except Exception:
                pass
            fields[f'f{fn}'] = parse_fields(chunk, depth + 1, max_depth)
        elif wt == 1:
            i += 8
            fields[f'f{fn}'] = '(64bit)'
        elif wt == 5:
            i += 4
            fields[f'f{fn}'] = '(32bit)'
        else:
            break
    return fields


class CapturePurchase(DouyinLiveWebFetcher):
    def __init__(self, live_id, output_file):
        super().__init__(live_id, None)
        self.output_file = open(output_file, 'w', encoding='utf-8')
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
            fields = parse_fields(e.biz_content)
            record = {
                'size': biz_len,
                'type': '简短版' if biz_len <= 15 else '完整版',
                'timestamp': e.timestamp,
                'ts': datetime.now().isoformat(timespec='seconds'),
                'fields': fields,
            }
            self.output_file.write(json.dumps(record, ensure_ascii=False) + '\n')
            self.output_file.flush()

    def close_output(self):
        self.output_file.close()


def main():
    room_id = sys.argv[1] if len(sys.argv) > 1 else "646454278948"
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    output_path = Path('data') / f'purchase_events_{room_id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.jsonl'

    print(f"采集直播间 {room_id}，时长 {duration} 秒")
    print(f"输出文件: {output_path}")
    print("=" * 70)

    fetcher = CapturePurchase(room_id, str(output_path))
    th = threading.Thread(target=fetcher.start, daemon=True)
    th.start()

    time.sleep(duration)
    fetcher.stop()
    fetcher.close_output()

    print("\n" + "=" * 70)
    print("purchase 事件大小分布:")
    for size, cnt in fetcher.size_counter.most_common():
        label = "简短版" if size <= 15 else "完整版"
        print(f"  {size}B: {cnt} 条 ({label})")
    print(f"总计: {sum(fetcher.size_counter.values())} 条")
    print(f"\n数据已保存到: {output_path}")


if __name__ == '__main__':
    main()
