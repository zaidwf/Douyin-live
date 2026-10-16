"""保存抖音登录态到 config/auth.json（从 CDP Chrome 提取）

Usage:
  1. 启动 Chrome（带 remote-debugging）:
     chrome.exe --remote-debugging-port=9222 --user-data-dir=./data/chrome_profile
  2. 在该 Chrome 窗口中登录 https://live.douyin.com/
  3. 运行: python save_auth.py
"""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

AUTH_FILE = Path("config/auth.json")


async def main():
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            print(f"ERROR: 无法连接 Chrome CDP: {e}")
            print("\n请先启动 Chrome:")
            print(r'  chrome.exe --remote-debugging-port=9222 --user-data-dir=./data/chrome_profile')
            return

        contexts = browser.contexts
        if not contexts:
            print("未找到浏览器 context，请先在 Chrome 中打开一个标签页。")
            return

        state = await contexts[0].storage_state()
        AUTH_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        cookies = state.get("cookies", [])
        douyin = [c for c in cookies if "douyin" in c.get("domain", "")]
        print(f"已保存 {len(cookies)} 个 cookies（其中 {len(douyin)} 个 douyin）到 {AUTH_FILE}")
        print("完成。测试脚本会自动加载此登录态。")


if __name__ == "__main__":
    asyncio.run(main())
