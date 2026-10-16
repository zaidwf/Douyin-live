#!/usr/bin/python
# coding:utf-8 -*-
"""商品橱窗采集模块 - 基于 Playwright 从直播间商品面板提取商品信息

复用自 D:/CodeFile/aicode/douyin 项目的 product_collector.py / test_product_extract.py。
商品信息（名称/价格/销量/图片）从浏览器渲染后的 DOM 直接提取，无需逆向商城 API。
"""
import asyncio
import re
from typing import List, Dict, Optional

from playwright.async_api import async_playwright

# 商品橱窗按钮选择器（点击打开商品面板）
TRAY_SELECTORS = [
    '[data-e2e="yellowCart-container"]',
    '[class*="productTray"]',
    '[class*="shopBtn"]',
    '[class*="shoppingCart"]',
    'span:has-text("商品")',
    'div:has-text("全部商品")',
]

# 服务标签（过滤用，不是商品名）
SERVICE_KEYWORDS = [
    "运费险", "7天无理由退货", "顺丰包邮", "顺丰配送",
    "券满", "券后价", "领券抢购", "先鉴后发",
    "预售", "最晚", "发货",
]

# 营销按钮文本（过滤用）
MARKETING_TEXTS = {
    "去抢购", "领券抢购", "- 已抢光 -", "券后价",
    "点击抽奖", "起", "讲解中",
}

SALES_PATTERN = re.compile(r"(?:已售|销量)\s*(\d+\.?\d*[万wW]?)")


def _parse_count(text: str) -> int:
    """解析销量（支持"万"单位）"""
    text = text.strip().replace(",", "").replace(" ", "")
    if not text:
        return 0
    try:
        if "万" in text or "w" in text.lower():
            num = float(re.sub(r"[万wW万]", "", text))
            return int(num * 10000)
        return int(float(text))
    except ValueError:
        return 0


def _is_index_line(line: str) -> bool:
    """是否为商品序号行（1-999）"""
    return bool(re.match(r"^\d{1,3}$", line.strip()))


def _is_price_line(line: str) -> bool:
    """是否含价格（¥ 符号 + 数字）"""
    return bool(re.search(r"[\xa5￥]\s*[\d.]+", line))


def _is_service_line(line: str) -> bool:
    """是否为服务/徽章标签"""
    return any(kw in line for kw in SERVICE_KEYWORDS)


def _is_marketing_line(line: str) -> bool:
    """是否为按钮/营销文本"""
    l = line.strip()
    if l in MARKETING_TEXTS:
        return True
    if len(l) <= 2:
        return True
    if re.match(r"^(在线观众|高等级用户|带货口碑|小时榜|人气榜|关注|充值|更多|标清|分享|举报)$", l):
        return True
    return False


async def _open_product_panel(page) -> bool:
    """点击商品橱窗按钮，打开商品面板（force click + 重试）"""
    btn = None
    for selector in TRAY_SELECTORS:
        try:
            btn = await page.query_selector(selector)
            if btn:
                break
        except Exception:
            continue
    if not btn:
        return False

    for attempt in range(5):
        try:
            await btn.click(timeout=5000, force=True)
            await asyncio.sleep(8)
            body = await page.inner_text("body")
            if any(kw in body for kw in ("运费险", "带货口碑", "查看价格", "去抢购", "店铺口碑")):
                return True
        except Exception:
            pass
        # 重试前等待并重新定位按钮
        await asyncio.sleep(3)
        try:
            btn = await page.query_selector(TRAY_SELECTORS[0])
        except Exception:
            pass

    return False


async def _extract_image_urls(page) -> List[str]:
    """从页面提取商品图片 URL"""
    urls = []
    try:
        imgs = await page.query_selector_all("img")
        for img in imgs[:20]:
            src = await img.get_attribute("src")
            if src and ("douyin" in src or "douyincdn" in src):
                if src.startswith("//"):
                    src = "https:" + src
                if src not in urls:
                    urls.append(src)
    except Exception:
        pass
    return urls


async def extract_products(room_id: str, headless: bool = True, auth_file: Optional[str] = None,
                           cdp_port: Optional[int] = None) -> List[Dict]:
    """提取直播间商品橱窗列表

    :param room_id: 直播间 ID（web_rid）
    :param headless: 是否无头模式（CDP 模式忽略此参数）
    :param auth_file: 登录态文件路径（可选，config/auth.json；CDP 模式忽略）
    :param cdp_port: CDP 端口（如 9222），连接已运行的真实 Chrome（反检测最强）
    :return: 商品列表，每项含 name/price/price_original/sales_count/sold_out 等
    """
    room_url = f"https://live.douyin.com/{room_id}"
    products = []
    image_urls = []

    async with async_playwright() as p:
        if cdp_port:
            # CDP 模式：连接真实 Chrome（复用其持久化 profile 的登录态）
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            # 独立浏览器模式
            browser = await p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--window-size=1400,900"],
            )
            context = await browser.new_context(
                viewport={"width": 1400, "height": 850},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                storage_state=auth_file,
            )
        page = await context.new_page()

        try:
            await page.goto(room_url, wait_until="commit", timeout=60000)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await asyncio.sleep(10)

            # 打开商品橱窗面板
            opened = await _open_product_panel(page)
            if not opened:
                return products  # 关闭交给 finally 处理，避免 CDP 模式误关 Chrome

            # 滚动商品面板，加载全部商品（商品列表是懒加载的）
            # 只滚动包含商品特征的容器，避免影响直播画面等其他滚动区域
            for _ in range(15):
                await page.evaluate('''() => {
                    const containers = document.querySelectorAll('div, ul');
                    for (const el of containers) {
                        if (el.scrollHeight > el.clientHeight + 100) {
                            const text = el.innerText || '';
                            if (text.includes('运费险') || text.includes('去抢购') ||
                                text.includes('查看价格') || text.includes('带货口碑') ||
                                text.includes('店铺口碑')) {
                                el.scrollTop = el.scrollHeight;
                            }
                        }
                    }
                }''')
                await asyncio.sleep(1.5)

            # 提取页面文本，按行解析
            body = await page.inner_text("body")
            lines = [l.strip() for l in body.split("\n") if l.strip()]

            # 收集图片 URL
            image_urls = await _extract_image_urls(page)

            # 定位商品面板区域：从"带货口碑/店铺口碑/讲解中"到"在线观众"
            start = end = -1
            for i, line in enumerate(lines):
                if start < 0 and ("带货口碑" in line or "店铺口碑" in line or "讲解中" in line):
                    start = i
                if start >= 0 and "在线观众" in line:
                    end = i
                    break
            panel_lines = lines[start:end if end > 0 else len(lines)] if start >= 0 else lines

            # 在商品面板区域内找序号行
            index_positions = []
            for i, line in enumerate(panel_lines):
                if _is_index_line(line):
                    num = int(line)
                    if not index_positions or (1 <= num <= index_positions[-1][1] + 10):
                        index_positions.append((i, num))

            seen_names = set()

            # 基于序号定位商品，逐个提取
            for idx, (index_i, num) in enumerate(index_positions):
                # 商品范围：到下一个序号为止
                end_i = index_positions[idx + 1][0] if idx + 1 < len(index_positions) else len(panel_lines)

                # 商品名 = 序号后第一行
                if index_i + 1 >= end_i:
                    continue
                label = panel_lines[index_i + 1]
                if not label or len(label) < 2 or len(label) > 120:
                    continue
                if _is_service_line(label) or _is_marketing_line(label):
                    continue
                if label in seen_names:
                    continue
                seen_names.add(label)

                # 在商品范围内提取：副标题、价格、销量、售罄状态
                subtitle = ""
                price_current = 0.0
                price_original = 0.0
                sales_count = 0
                is_sold_out = False
                is_coupon_price = False

                for j in range(index_i + 2, end_i):
                    l = panel_lines[j]
                    # 价格：¥ 数字 或 "查看价格"
                    if _is_price_line(l):
                        prices = [float(x) for x in re.findall(r"([\d.]+)", l)]
                        if prices and not price_current:
                            price_current = min(prices)
                            price_original = max(prices) if len(prices) >= 2 else 0.0
                    elif "查看价格" in l and not price_current:
                        # 已抢光商品价格隐藏
                        price_current = -1  # 标记为"查看价格"
                    # 副标题（商品名后、服务标签前）
                    if not subtitle and l and l != label and not _is_service_line(l) and not _is_marketing_line(l):
                        subtitle = l
                    # 销量
                    sm = SALES_PATTERN.search(l)
                    if sm:
                        sales_count = _parse_count(sm.group(1))
                    # 售罄
                    if "已抢光" in l:
                        is_sold_out = True
                    if "券后价" in l:
                        is_coupon_price = True

                products.append({
                    "num": num,
                    "name": label,
                    "subtitle": subtitle,
                    "price": price_current if price_current > 0 else 0.0,
                    "price_original": price_original,
                    "sales_count": sales_count,
                    "sold_out": is_sold_out,
                    "coupon_price": is_coupon_price,
                })

        except Exception as e:
            print(f"商品提取错误: {e}")
        finally:
            if cdp_port:
                # CDP 模式只关闭 page，保持 Chrome 连接
                try:
                    await page.close()
                except Exception:
                    pass
            else:
                await browser.close()

    # 附上图片 URL（图片与商品顺序不一定对应，作为整体列表）
    if image_urls:
        for i, prod in enumerate(products):
            if i < len(image_urls):
                prod["image_url"] = image_urls[i]
    return products


def extract_products_sync(room_id: str, headless: bool = True, auth_file: Optional[str] = None,
                          cdp_port: Optional[int] = None) -> List[Dict]:
    """同步包装，供非异步代码调用"""
    return asyncio.run(extract_products(room_id, headless, auth_file, cdp_port))


# 商品详情页 URL（haohuo 商城，客户端渲染 SPA）
DETAIL_URL = "https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id={promotion_id}"


async def _fetch_product_detail_page(page, promotion_id: str) -> Dict:
    """在已有 page 上渲染单个商品详情页，提取标题/价格/销量/店铺等字段"""
    url = DETAIL_URL.format(promotion_id=promotion_id)
    result = {
        "promotion_id": str(promotion_id),
        "detail_url": url,
        "title": None, "price": None, "price_original": None,
        "sales_count": None, "shop_name": None, "brand": None,
    }
    try:
        await page.goto(url, wait_until="commit", timeout=60000)
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await asyncio.sleep(8)  # 等待 SPA 渲染完成

        body = await page.inner_text("body")
        lines = [l.strip() for l in body.split("\n") if l.strip()]

        # 标题：渲染后通常是最早出现的一条较长的商品名
        for l in lines:
            if 5 <= len(l) <= 120 and not _is_service_line(l) and not _is_marketing_line(l):
                result["title"] = l
                break

        for l in lines:
            # 价格（可能登录后才显示，未登录常为"查看价格"）
            if _is_price_line(l) and result["price"] is None:
                prices = [float(x) for x in re.findall(r"([\d.]+)", l)]
                if prices:
                    result["price"] = min(prices)
                    result["price_original"] = max(prices) if len(prices) >= 2 else None
            # 销量
            if result["sales_count"] is None:
                sm = SALES_PATTERN.search(l)
                if sm:
                    result["sales_count"] = _parse_count(sm.group(1))
            # 店铺名（形如 "xx旗舰店" 或 "进店" 上方的店铺名）
            if result["shop_name"] is None and re.search(r"(旗舰店|专卖店|小店|专营店)$", l) and len(l) <= 30:
                result["shop_name"] = l
    except Exception as e:
        print(f"商品详情提取错误（promotion_id={promotion_id}）: {e}")
    return result


async def fetch_product_details(promotion_ids: List[str], headless: bool = True,
                                auth_file: Optional[str] = None,
                                cdp_port: Optional[int] = None) -> List[Dict]:
    """批量渲染商品详情页，复用单个浏览器会话（避免逐商品反复启动浏览器触发风控）

    :param promotion_ids: 商品 promotion_id 列表
    :return: 详情列表，每项含 promotion_id/title/price/sales_count/shop_name 等（缺省字段为 None）
    """
    results = []
    if not promotion_ids:
        return results

    async with async_playwright() as p:
        if cdp_port:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--window-size=1400,900"],
            )
            context = await browser.new_context(
                viewport={"width": 1400, "height": 850},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                storage_state=auth_file,
            )
        page = await context.new_page()
        try:
            for pid in promotion_ids:
                results.append(await _fetch_product_detail_page(page, pid))
                await asyncio.sleep(2)  # 节流，降低风控
        finally:
            if cdp_port:
                try:
                    await page.close()
                except Exception:
                    pass
            else:
                await browser.close()
    return results


def fetch_product_detail(promotion_id: str, headless: bool = True,
                         auth_file: Optional[str] = None,
                         cdp_port: Optional[int] = None) -> Dict:
    """同步包装：渲染单个商品详情页"""
    results = asyncio.run(fetch_product_details([promotion_id], headless, auth_file, cdp_port))
    return results[0] if results else {"promotion_id": str(promotion_id), "detail_url": DETAIL_URL.format(promotion_id=promotion_id)}
