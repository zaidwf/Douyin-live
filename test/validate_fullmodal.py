#!/usr/bin/python
# coding:utf-8
"""全模态采集产物校验：逐模态验真实性，而不是只数文件个数

五个模态：
  文本   danmaku.jsonl（弹幕）
  数值   events.jsonl / series.jsonl / room.json
  图像   frames/*.jpg
  音频   audio/*.wav（16kHz 单声道无损；旧数据为 *.mp3）
  商品   ← 由灰豚提供（直播结束后下载），**不在代码采集范围**，本脚本只做提示

真实性判据（避免"文件存在但内容是垃圾"）：
  - 帧：md5 去重率（全同 = 画面没变，说明截图的是静止页而非直播流）
  - 音频：ffprobe 时长/码率 + volumedetect 平均音量（静音 = 流地址错误）
  - 弹幕：行数 / 去重用户数 / 用户 ID 是否被打码（111111）
  - 时序：series.jsonl 行数与时间跨度是否覆盖采集时长

用法：
  python test/validate_fullmodal.py [数据目录]      # 默认取最新 live_* 目录
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return (r.stdout or '') + (r.stderr or '')
    except Exception as e:
        return f'<ERR {e}>'


def md5(path, limit=None):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def main():
    if len(sys.argv) > 1:
        d = sys.argv[1]
    else:
        # 场次目录现在嵌在 run 目录下（data/<YYYYMMDD_HHMMSS>/live_*/），故按两层找；
        # 用 `**` 递归以兼容 2026-09-29 归档前直接平铺在 data/ 下的旧目录
        dirs = [x for x in glob.glob(os.path.join(ROOT, 'data', '**', 'live_*'), recursive=True)
                if os.path.isdir(x)]
        if not dirs:
            print("未找到任何 data/**/live_* 采集目录")
            return
        d = max(dirs, key=os.path.getmtime)
    print("=" * 78)
    print(f"全模态采集产物校验：{os.path.basename(d)}")
    print("=" * 78)

    verdict = {}

    # ---------- 1. 文本模态 ----------
    print("\n[1] 文本模态 —— danmaku.jsonl")
    dp = os.path.join(d, 'danmaku.jsonl')
    if os.path.exists(dp):
        rows = [json.loads(l) for l in open(dp, encoding='utf-8') if l.strip()]
        uids = Counter(str(r.get('user_id')) for r in rows)
        masked = uids.get('111111', 0)
        keys = sorted(rows[0].keys()) if rows else []
        fc = sum(1 for r in rows if 'fansclub_level' in r)
        print(f"    弹幕 {len(rows)} 条 | 字段 {keys}")
        print(f"    去重用户 {len(uids)} | 111111 打码 {masked} "
              f"({masked / len(rows) * 100 if rows else 0:.1f}%) | 带粉丝团标识 {fc}")
        ts = [r['ts'] for r in rows if r.get('ts')]
        if ts:
            print(f"    时间跨度 {ts[0]} → {ts[-1]}")
        # ⚠️ 判据不能只看 `user_id` 去重数（原判据是 `len(uids) > 1`）：
        # 打码的房间所有被打码用户都塌进 `111111` **这一个桶**，于是
        #   * 全量打码 → 去重恒等于 1 → 把「这个房间打码」误判成「没采到弹幕」；
        #   * 部分打码 → 去重数**恰好还 > 1**（1 个真 id + 那个桶），侥幸躲过。
        # 两种都不能反映「有几个人在说话」。打码率按房间而异（实测 35~100%），会复现。
        # 正确的算法是**在桶层面数发言人**：每个未打码 id 各算 1，被打码的全体育共算 1。
        # nickname 也打码但保留了前缀（`明***`/`绿***`），区分度更高，取两者较大值。
        real = {u for u in uids if u != '111111'}
        uid_speakers = len(real) + (1 if masked else 0)
        nick_speakers = len({str(r.get('nickname')) for r in rows})
        speakers = max(uid_speakers, nick_speakers)
        if rows and masked:
            print(f"    → user_id 打码 {masked}/{len(rows)}：按桶计 {uid_speakers} 个、"
                  f"按昵称计 {nick_speakers} 个发言人")
        verdict['文本'] = len(rows) > 0 and speakers > 1
    else:
        print("    ❌ 缺 danmaku.jsonl")
        verdict['文本'] = False

    # ---------- 2. 数值模态 ----------
    print("\n[2] 数值模态 —— events.jsonl / series.jsonl / room.json")
    ep = os.path.join(d, 'events.jsonl')
    if os.path.exists(ep):
        c = Counter()
        for l in open(ep, encoding='utf-8'):
            l = l.strip()
            if l:
                c[json.loads(l).get('type')] += 1
        print(f"    events 分布: {dict(c)}")
    sp = os.path.join(d, 'series.jsonl')
    srows = []
    if os.path.exists(sp):
        srows = [json.loads(l) for l in open(sp, encoding='utf-8') if l.strip()]
        print(f"    series {len(srows)} 条 | 字段 {sorted(srows[0].keys()) if srows else []}")
        if srows:
            fs = [r.get('fansclub_member_ratio') for r in srows if r.get('fansclub_member_ratio') is not None]
            if fs:
                print(f"    粉丝团成员占比时序: {fs[0]} → {fs[-1]}（首→末）")
    rp = os.path.join(d, 'room.json')
    rd = {}
    if os.path.exists(rp):
        rd = json.load(open(rp, encoding='utf-8'))
        print(f"    room.json 顶层键: {list(rd.keys())}")
        print(f"    like_total={rd.get('like_total')}  follow_count={rd.get('follow_count')}")
        fck = rd.get('fansclub') or {}
        if fck:
            print(f"    fansclub: 成员 {fck.get('member_count')}/{fck.get('user_count')} "
                  f"占比 {fck.get('member_ratio')} 均值 {fck.get('level_mean')} "
                  f"uid_masked={fck.get('uid_masked')}")
    verdict['数值'] = bool(srows) and bool(rd)

    # ---------- 3. 图像模态 ----------
    print("\n[3] 图像模态 —— frames/*.jpg")
    fdir = os.path.join(d, 'frames')
    frames = sorted(glob.glob(os.path.join(fdir, '*.jpg')))
    if frames:
        hs = [md5(p) for p in frames]
        uniq = len(set(hs))
        sizes = [os.path.getsize(p) for p in frames]
        print(f"    帧数 {len(frames)} | md5 去重后 {uniq} ({uniq / len(frames) * 100:.0f}%) "
              f"| 单帧 {min(sizes)}~{max(sizes)} bytes")
        t0 = datetime.fromtimestamp(os.path.getmtime(frames[0]))
        t1 = datetime.fromtimestamp(os.path.getmtime(frames[-1]))
        print(f"    时间跨度 {t0.strftime('%H:%M:%S')} → {t1.strftime('%H:%M:%S')}")
        verdict['图像'] = uniq > len(frames) * 0.2
        if uniq <= 3:
            print("    ⚠️ 帧几乎全同 → 截图的是静止页而非直播画面")
    else:
        print("    ❌ 无帧")
        verdict['图像'] = False

    # ---------- 4. 音频模态 ----------
    print("\n[4] 音频模态 —— audio/*.wav（16kHz 单声道无损）")
    adir = os.path.join(d, 'audio')
    # 2026-09-28 起音频改为 16kHz 单声道无损 WAV；同时兼容旧数据的 mp3 片段
    segs = sorted(glob.glob(os.path.join(adir, '*.wav'))
                  + glob.glob(os.path.join(adir, '*.mp3')))
    if segs:
        total = 0.0
        silent = 0
        broken = 0
        for p in segs:
            # 0 字节 / 截断的分段是硬杀进程的残片，不是采集缺陷 —— 报出来但要能继续走
            if os.path.getsize(p) == 0:
                print(f"    {os.path.basename(p)}: ⚠️ 0 字节残片（进程被强杀留下的空壳），已跳过")
                broken += 1
                continue
            out = sh(['ffprobe', '-v', 'error', '-show_entries', 'format=duration,bit_rate',
                      '-of', 'default=nw=1:nk=1', p])
            nums = [x for x in out.strip().split('\n') if x]
            try:
                dur = float(nums[0]) if nums else 0.0
            except ValueError:
                print(f"    {os.path.basename(p)}: ⚠️ ffprobe 无法解析（{out.strip()[:80]}），已跳过")
                broken += 1
                continue
            br = nums[1] if len(nums) > 1 else '?'
            total += dur
            vol = sh(['ffmpeg', '-hide_banner', '-i', p, '-af', 'volumedetect', '-f', 'null', '-'])
            m = re.search(r'mean_volume:\s*(-?[\d.]+) dB', vol)
            mean_v = float(m.group(1)) if m else None
            if mean_v is None or mean_v < -60:
                silent += 1
            print(f"    {os.path.basename(p)}: {dur:.1f}s, {br} bps, "
                  f"mean_volume={mean_v} dB {'← 疑似静音' if mean_v is None or mean_v < -60 else ''}")
        print(f"    合计 {len(segs)} 段 / {total:.0f}s，静音段 {silent}"
              + (f"，残片 {broken}" if broken else ""))
        verdict['音频'] = (len(segs) - broken) > 0 and silent < (len(segs) - broken)
    else:
        print("    ❌ 无音频片段（流 URL 未提取到或 ffmpeg 失败）")
        verdict['音频'] = False

    # ---------- 5. 商品模态 ----------
    print("\n[5] 商品模态 —— 由灰豚提供（直播结束后下载）")
    print(f"    room.json commodities = {len(rd.get('commodities') or [])} 个"
          f"（代码侧已停采，非缺陷）")
    print(f"    room.json promotions   = {len(rd.get('promotions') or [])} 个"
          f"（代码采集的 promotion_id + 成交时间分布，用于按 pId 关联灰豚商品）")
    verdict['商品'] = None  # 不参与判定

    # ---------- 汇总 ----------
    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    for k, v in verdict.items():
        mark = '—（改灰豚）' if v is None else ('✅ 通过' if v else '❌ 未通过')
        print(f"    {k}模态: {mark}")
    bad = [k for k, v in verdict.items() if v is False]
    print(f"\n    结论: {'全部通过' if not bad else '未通过: ' + '、'.join(bad)}")


if __name__ == "__main__":
    main()
