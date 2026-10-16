#!/usr/bin/python
# coding:utf-8
"""讲解时序语义标定：dump LiveShoppingMessage 的**全部 wire 字段**（含本 proto 未定义字段）

背景（2026-09-28 实测）：
  - 88 个数据目录中 explain 事件 = 0：`WebcastAwemeShopExplainMessage` 与
    `WebcastProductChangeMessage` 从未下发过 → 现成的 active/explainType 信号拿不到。
  - 但 `WebcastLiveShoppingMessage` 大量下发，且在本项目 proto 里只有 3 个字段
    （common/msgType/promotionId），参考 new_douyin.proto 有 ~36 个字段未解析。
  - 已验证：`msg_type==2` 与 `LiveEcomGeneralMessage` 的成交（LivePopMessage）
    4037/4067 秒级对齐 → msg_type=2 = 成交。
  - 疑点：`msg_type==3` 在同一 promotion_id 上以 ~12s 周期连发 10 分钟以上，
    不可能代表「下架/讲解结束」，语义需重新标定。

本脚本用 raw_callback 拿到 payload 原始字节，用通用 protobuf wire 解码器 dump 所有字段，
落盘 JSONL 供事后与「商品切换 / 成交 / 讲解切换」对齐，从而标定 msg_type 与各字段语义。

用法：
  python probe_explain_raw.py --id 56697889278 --duration 900
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liveMan import DouyinLiveWebFetcher

WATCH = {
    'WebcastLiveShoppingMessage',
    'WebcastLiveEcomGeneralMessage',
    'WebcastProductChangeMessage',
    'WebcastAwemeShopExplainMessage',
}


# ---------- 通用 protobuf wire 解码（无依赖） ----------
def _varint(b, i):
    r = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        if not (x & 0x80):
            break
        s += 7
    return r, i


def _looks_nested(raw):
    """粗判 bytes 段是否为可解析的子消息（排除纯文本）"""
    if len(raw) < 2:
        return False
    try:
        return _decode(raw, depth=1,
                       maxdepth=0) is not None and raw[0] not in range(0x20, 0x7F)
    except Exception:
        return False


def _decode(b, depth=0, maxdepth=1):
    out = []
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        fn, wt = key >> 3, key & 7
        if fn == 0:
            return None
        if wt == 0:
            v, i = _varint(b, i)
            out.append({'f': fn, 't': 'varint', 'v': v})
        elif wt == 2:
            ln, i = _varint(b, i)
            raw = b[i:i + ln]
            if len(raw) != ln:
                return None
            i += ln
            entry = {'f': fn, 't': 'bytes', 'len': ln}
            txt = None
            try:
                s = raw.decode('utf-8')
                if s.isprintable():
                    txt = s
            except Exception:
                pass
            if txt is not None:
                entry['s'] = txt[:120]
            if depth < maxdepth and txt is None:
                sub = _decode(raw, depth + 1, maxdepth)
                if sub:
                    entry['sub'] = sub
            elif txt is None:
                entry['hex'] = raw[:48].hex()
            out.append(entry)
        elif wt == 5:
            out.append({'f': fn, 't': 'fixed32', 'hex': b[i:i + 4].hex()})
            i += 4
        elif wt == 1:
            out.append({'f': fn, 't': 'fixed64', 'hex': b[i:i + 8].hex()})
            i += 8
        else:
            return None  # 3/4 组已废弃
    return out or None


def decode_message(payload):
    """返回 (顶层字段列表, 人读字符串)"""
    try:
        fields = _decode(payload, maxdepth=2)
    except Exception:
        fields = None
    if fields is None:
        return None, 'DECODE_FAILED'
    return fields, fmt(fields)


def fmt(fields, indent=0):
    lines = []
    for e in fields:
        f = e['f']
        if e['t'] == 'varint':
            lines.append(f"{'  ' * indent}f{f}(varint)={e['v']}")
        elif e['t'] == 'bytes':
            if 's' in e:
                lines.append(f"{'  ' * indent}f{f}(str,len{e['len']})={e['s']!r}")
            elif 'sub' in e:
                lines.append(f"{'  ' * indent}f{f}(msg,len{e['len']}){{")
                lines.extend(fmt(e['sub'], indent + 1))
                lines.append(f"{'  ' * indent}}}")
            else:
                lines.append(f"{'  ' * indent}f{f}(bytes,len{e['len']})={e.get('hex', '')}")
        else:
            lines.append(f"{'  ' * indent}f{f}({e['t']})={e['hex']}")
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="56697889278")
    ap.add_argument("--duration", type=int, default=900)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # 输出到 data/_test_residue/ —— 探针产物是一次性的，不该混进 data/ 的批次目录层
    out_path = args.out or os.path.join(
        "data", "_test_residue",
        f"raw_shopping_{args.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    f = DouyinLiveWebFetcher(args.id, None)
    st = f.get_room_status()
    print(f"[STATUS] {st}", flush=True)
    if not st[0]:
        return

    counts = Counter()
    shapes = {}          # 每种 (method, msg_type) 的字段形状（只留首例）
    fh = open(out_path, 'w', encoding='utf-8')

    def raw_cb(method, payload, msg):
        if method not in WATCH:
            return
        fields, text = decode_message(payload)
        counts[method] += 1
        # 提取顶层 f2（msgType）作为分组键
        f2 = next((e['v'] for e in (fields or []) if e['f'] == 2 and e['t'] == 'varint'), None)
        key = (method, f2)
        if key not in shapes:
            shapes[key] = text
            print(f"\n[新形状] method={method} msg_type(f2)={f2} 顶层字段数={len(fields or [])}\n"
                  f"{text}\n", flush=True)
        fh.write(json.dumps({
            'ts': datetime.now().isoformat(timespec='milliseconds'),
            'method': method, 'msg_type': f2, 'text': text, 'fields': fields,
        }, ensure_ascii=False) + '\n')
        fh.flush()

    f.raw_callback = raw_cb
    t = threading.Thread(target=f.start, daemon=True)
    t.start()
    print(f"[采集] {args.duration}s，落盘 {out_path}", flush=True)
    time.sleep(args.duration)
    f.stop()
    fh.close()

    print("\n" + "=" * 72, flush=True)
    print("method 计数:", dict(counts), flush=True)
    print(f"落盘: {out_path}", flush=True)
    print("\n各 (method, msg_type) 字段形状:", flush=True)
    for (m, mt), txt in shapes.items():
        print(f"\n--- {m}  msg_type={mt} ---\n{txt}", flush=True)


if __name__ == "__main__":
    main()
