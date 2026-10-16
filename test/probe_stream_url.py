#!/usr/bin/python
# coding:utf-8
"""一次性探针：验证「能否去掉 Playwright 拿直播流地址」

## ✅ 验证已完成（2026-09-29），结论：**分支一可行，Playwright 已移除**

实测（金山银山 `164425316`，room_id `7690870763229842214`）：

| 路径 | HTML 大小 | `flv_pull_url` 出现 | 解析出流地址 |
|---|---|---|---|
| `requests`（本探针 [1]） | 1,130,959 字节 | 8 次 | **4 个** |
| Playwright（本探针 [2] 对照） | 1,112,624 字节 | — | 4 个 |

**决定性判据 [3]**：把 `requests` 解析出的首个 URL 直接喂 ffmpeg，
**真的抽出了 72067 字节的 jpg** —— 是真能解码的地址，不是占位/过期串。

两路 URL 集合不完全相同，是签名令牌（`k=` / `t=`）短时有效的正常差异，
不是结构差异（两边解析数量一致，且 requests 那路能解码）。

**据此已落地**：`extract_stream_urls_from_html` 与音视频采集迁入 `liveMan.py`
（`MediaCapture`），**不再引入 playwright**；`collect_full.py` 自带的 Playwright
实现已删除。

---

背景：`collect_full.py` 原用 Playwright 起一个 headless Chromium，**只为读一次
直播间页面 HTML** 来解析 `flv_pull_url`；而 `liveMan.py` 的 `room_id` property
本来就用带完整浏览器头 + cookie 的 `requests.get` 拉了**同一份 HTML**，只正则抠了
roomId 就把 `response.text` 丢掉。那份 HTML 里就有 `flv_pull_url`，于是 100 路并发
可以整块省掉 Chromium（每实例 150-300MB，100 路 = 15-30GB）。

本探针保留作为**该结论的可复现证据**与「抖音改版后是否回退」的回归工具。

三步验证：
  [1] requests 拿 HTML → extract_stream_urls_from_html，看能否解析出流地址
  [2] 对照：同一房间用 Playwright 拿 HTML 再解析，比对是否一致
      （排除「requests 拿到的是精简版页面」这种假阳性）
  [3] ★ 把解析出的 URL 直接喂 ffmpeg 抽 1 帧，确认**可解码**
      —— 只验「非空字符串」不算数，URL 可能是过期/占位的

用法：
  python test/probe_stream_url.py                      # 自动找一个在播房间
  python test/probe_stream_url.py 56697889278          # 指定抖音号
  python test/probe_stream_url.py --scan 30            # 扫名单前 30 个找在播的
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests                                     # noqa: E402
from liveMan import (COMMON_HEADERS, DouyinLiveWebFetcher, generateMsToken,  # noqa: E402
                     extract_stream_urls_from_html)


def fetch_html_via_requests(fetcher, live_id):
    """复刻 liveMan `room_id` property 的请求（同样 headers + cookie），但**保留 HTML**

    这是本探针的核心：liveMan 已经做了这个请求，只是把 response.text 丢了。
    """
    url = f"https://live.douyin.com/{live_id}"
    headers = {**COMMON_HEADERS,
               "cookie": f"ttwid={fetcher.ttwid}&msToken={generateMsToken()}; __ac_nonce=0123407cc00a9e438deb4"}
    r = requests.get(url, headers=headers, timeout=30)
    return r.status_code, r.text


def fetch_html_via_playwright(live_id, auth_file):
    """**对照路径**：Playwright 导航后取 page.content()

    `collect_full.py` 原先走这条路（已删除）。保留在此仅作对照，用于排除
    「requests 拿到的是精简版页面」这种假阳性。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=auth_file if os.path.exists(auth_file) else None)
        page = ctx.new_page()
        try:
            page.goto(f"https://live.douyin.com/{live_id}",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
            html = page.content()
        finally:
            try:
                page.close()
            except Exception:
                pass
            browser.close()
    return html


def try_decode_frame(url, tag, seconds=25):
    """把流 URL 喂 ffmpeg 抽 1 帧，验证**真的能解码**

    只检查「URL 非空」是不够的：占位/过期地址同样是非空字符串。
    这里要求 ffmpeg 真的产出一张非空 jpg。
    """
    out = os.path.join(tempfile.gettempdir(), f"probe_frame_{tag}.jpg")
    if os.path.exists(out):
        os.remove(out)
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
           "-rw_timeout", "15000000",              # 15s 读超时，避免挂死
           "-i", url, "-frames:v", "1", "-q:v", "2", out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 15)
    except subprocess.TimeoutExpired:
        return False, None, "ffmpeg 超时（流不可读）"
    if os.path.exists(out) and os.path.getsize(out) > 1024:
        return True, os.path.getsize(out), (r.stderr or '').strip()[:200]
    return False, None, (r.stderr or '').strip()[:300] or f"exit={r.returncode}"


def probe_one(live_id, auth_file, do_playwright=True):
    print("=" * 78)
    print(f"探针目标：{live_id}")
    print("=" * 78)

    f = DouyinLiveWebFetcher(live_id, None)
    try:
        room_id = f.room_id
    except Exception as e:
        print(f"  ❌ room_id 解析异常: {e}")
        return None
    if not room_id:
        print("  ❌ room_id 解析失败（未开播或页面无 roomId）")
        return None

    ok, status, nickname, anchor_id = f.get_room_status()
    print(f"  装修信息: {nickname} | 主播ID={anchor_id} | room_id={room_id} | {status}")
    if not ok or status != '正在直播':
        print(f"  ⚠️ 未在直播（{status}），跳过流地址测试")
        return None

    # ---- [1] requests 分支 ----
    print("\n[1] requests + HTML 直接解析（去 Playwright 的目标路径）")
    try:
        code, html = fetch_html_via_requests(f, live_id)
    except Exception as e:
        print(f"  ❌ 请求异常: {e}")
        return None
    print(f"  HTTP {code} | HTML {len(html)} 字节")
    # 页面里出现 flv_pull_url 关键字没有？先看「有没有」再看「能不能解析」
    for kw in ('flv_pull_url', '__pace_f', 'stream_url', 'roomId'):
        print(f"    含 {kw!r}: {html.count(kw)} 次")
    urls_req = extract_stream_urls_from_html(html)
    print(f"  → 解析出 {len(urls_req)} 个流地址")

    # ---- [2] Playwright 对照 ----
    urls_pw = []
    if do_playwright:
        print("\n[2] Playwright + HTML 解析（现有实现，作对照）")
        try:
            html_pw = fetch_html_via_playwright(live_id, auth_file)
            print(f"  HTML {len(html_pw)} 字节")
            urls_pw = extract_stream_urls_from_html(html_pw)
            print(f"  → 解析出 {len(urls_pw)} 个流地址")
            same = set(urls_req) == set(urls_pw)
            print(f"  两者 URL 集合一致: {same}")
        except Exception as e:
            print(f"  ⚠️ Playwright 对照失败（不影响主结论）: {type(e).__name__}: {e}")

    # ---- [3] ★ 可解码性：这才是决定性判据 ----
    print("\n[3] ★ 把 requests 解析出的 URL 喂 ffmpeg 抽 1 帧（决定性判据）")
    if not urls_req:
        print("  ❌ requests 分支没解析出任何 URL，分支一不可行")
        return False
    print(f"  取样 URL（截断）: {urls_req[0][:120]}...")
    dec_ok, size, err = try_decode_frame(urls_req[0], 'req')
    if dec_ok:
        print(f"  ✅ 可解码！抽出 {size} 字节的 jpg → **requests 分支可行**")
    else:
        print(f"  ❌ 不可解码: {err[:250]}")
        print("     → 退分支二：保留 Playwright，但改为「读一次 HTML 即关浏览器」")
    return dec_ok


def find_live_id(scan):
    """从 documents/并发测试.xlsx 的 sheet1 取前 scan 个抖音号，找第一个在播的"""
    import openpyxl
    path = os.path.join(ROOT, 'documents', '并发测试.xlsx')
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb['sheet1']
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    print(f"[扫描] 名单 {len(rows)} 个，依次探测前 {scan} 个找在播房间...")
    for i, r in enumerate(rows[:scan], 1):
        did = r[2]
        if isinstance(did, float) and did.is_integer():
            did = str(int(did))
        else:
            did = str(did).strip()
        try:
            f = DouyinLiveWebFetcher(did, None)
            ok, status, nick, _ = f.get_room_status()
        except Exception:
            continue
        mark = '← 在播' if (ok and status == '正在直播') else ''
        print(f"  [{i:>2}] {r[1]} ({did}) {status} {mark}")
        if ok and status == '正在直播':
            return did, str(r[1])
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('live_id', nargs='?', default=None, help='抖音号；不给则自动扫名单找在播的')
    ap.add_argument('--scan', type=int, default=20, help='自动扫描名单的前 N 个（默认 20）')
    ap.add_argument('--no-playwright', action='store_true', help='跳过 Playwright 对照')
    ap.add_argument('--auth', default=os.path.join(ROOT, 'config', 'auth.json'))
    args = ap.parse_args()

    live_id = args.live_id
    if not live_id:
        live_id, nick = find_live_id(args.scan)
        if not live_id:
            print(f"\n❌ 前 {args.scan} 个主播都不在播，无法测试。"
                  f"可用 --scan 扩大范围，或直接指定一个正在直播的抖音号。")
            return
        print(f"\n选中在播房间：{nick} ({live_id})\n")

    result = probe_one(live_id, args.auth, do_playwright=not args.no_playwright)

    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    if result is True:
        print("  ✅ 分支一可行：requests 拿到的 HTML 就能解析出可解码的流地址")
        print("     → liveMan 不必引入 playwright/asyncio，只需搬 ffmpeg 部分")
    elif result is False:
        print("  ❌ 分支二：保留 Playwright，但改为「读一次 HTML 即关浏览器」")
    else:
        print("  ⚠️ 未能得出有效结论（目标未在播），换个在播房间重跑")


if __name__ == '__main__':
    main()
