#!/usr/bin/env python3
"""
Van Cleef & Arpels 补货监控

判断逻辑：
  - 页面 HTML 里"有货按钮"和"没货按钮"一直同时存在，所以不能只搜文字，
    要用 Playwright 判断哪个按钮真正"可见"。
  - 有货按钮可见            -> 有货：Bark 紧急警报 + Gmail 邮件，退出码 0
  - 只有没货按钮可见        -> 没货，退出码 0
  - 两个按钮都看不到        -> 保存截图 screenshot.png，退出码 1
  - 有货但通知发送失败      -> 退出码 2

用法：
  python monitor.py               正常检测
  python monitor.py --test-email  只发一封测试邮件
  python monitor.py --test-bark   只发一条测试 Bark 警报

所有密码 / key 都从环境变量读取（在 GitHub Actions 里由 Secrets 注入）：
  BARK_KEY, GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO
可选：
  BARK_SERVER        默认 https://api.day.app
  IN_STOCK_TEXT      默认 ADD TO BAG
  OUT_OF_STOCK_TEXT  默认 ORDER BY PHONE
  PRODUCT_URL        默认下面的手链链接
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

DEFAULT_URL = (
    "https://www.vancleefarpels.com/gb/en/collections/jewelry/alhambra/"
    "vcard35600---vintage-alhambra-bracelet-5-motifs.html"
)
SCREENSHOT = "screenshot.png"
WAIT_SECONDS = 40  # 最多等多久让按钮渲染出来


def cfg(name: str, default: str = "") -> str:
    """读环境变量；空字符串也当作没设置。"""
    return (os.environ.get(name) or "").strip() or default


def need(name: str) -> str:
    value = cfg(name)
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}（请在 GitHub Secrets 里添加）")
    return value


PRODUCT_URL = cfg("PRODUCT_URL", DEFAULT_URL)
IN_STOCK_TEXT = cfg("IN_STOCK_TEXT", "ADD TO BAG")
OUT_STOCK_TEXT = cfg("OUT_OF_STOCK_TEXT", "ORDER BY PHONE")


# ---------------------------------------------------------------- 检测

def text_pattern(text: str) -> re.Pattern:
    # 忽略大小写、忽略多余空格："Add  to bag" 也能匹配 "ADD TO BAG"
    words = [re.escape(w) for w in text.split()]
    return re.compile(r"\s+".join(words), re.IGNORECASE)


def button_visible(page, text: str) -> bool:
    """页面上是否有一个包含该文字、并且真正可见的按钮。"""
    candidates = page.locator("button, a, [role='button']").filter(
        has_text=text_pattern(text)
    )
    for i in range(candidates.count()):
        el = candidates.nth(i)
        try:
            # is_visible：排除 display:none、visibility:hidden、尺寸为 0 的元素
            if not el.is_visible():
                continue
            # 额外排除透明度为 0 的"隐形"按钮
            opacity = el.evaluate("e => getComputedStyle(e).opacity")
            if opacity == "0":
                continue
            return True
        except Exception:
            continue  # 元素在检查过程中被移除之类，跳过
    return False


def check_stock(url: str) -> str:
    """返回 'IN' / 'OUT' / 'UNKNOWN'。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-GB",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass  # 有些网站一直有后台请求，等不到 networkidle 也没关系

            deadline = time.time() + WAIT_SECONDS
            while time.time() < deadline:
                if button_visible(page, IN_STOCK_TEXT):
                    return "IN"
                if button_visible(page, OUT_STOCK_TEXT):
                    # 再等 3 秒复查一次，防止页面先显示没货再切换成有货
                    page.wait_for_timeout(3_000)
                    return "IN" if button_visible(page, IN_STOCK_TEXT) else "OUT"
                page.wait_for_timeout(1_000)

            page.screenshot(path=SCREENSHOT, full_page=True)
            print(f"页面标题：{page.title()!r}")
            return "UNKNOWN"
        except Exception as e:
            print(f"打开页面出错：{e}")
            try:
                page.screenshot(path=SCREENSHOT, full_page=True)
            except Exception:
                pass
            return "UNKNOWN"
        finally:
            browser.close()


# ---------------------------------------------------------------- 通知

def send_bark(title: str, body: str, open_url: str) -> None:
    key = need("BARK_KEY")
    server = cfg("BARK_SERVER", "https://api.day.app").rstrip("/")
    params = {
        "level": "critical",  # 紧急通知：静音、专注/睡眠模式下也会响
        "call": "1",          # 铃声重复播放约 30 秒
        "volume": "10",       # 紧急通知音量（0-10）
        "group": "VCA补货",
        "url": open_url,      # 点通知直接打开商品页
    }
    q = urllib.parse.quote
    full_url = f"{server}/{key}/{q(title, safe='')}/{q(body, safe='')}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full_url, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 200:
        raise RuntimeError(f"Bark 返回异常：{data}")
    print("Bark 警报已发送")


def send_email(subject: str, body: str) -> None:
    user = need("GMAIL_USER")
    password = need("GMAIL_APP_PASSWORD").replace(" ", "")
    to_addr = need("MAIL_TO")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, [to_addr], msg.as_string())
    print(f"邮件已发送到 {to_addr}")


# ---------------------------------------------------------------- 主程序

def main() -> int:
    parser = argparse.ArgumentParser(description="VCA 补货监控")
    parser.add_argument("--test-email", action="store_true", help="只发送一封测试邮件")
    parser.add_argument("--test-bark", action="store_true", help="只发送一条测试 Bark 警报")
    parser.add_argument("--url", default=PRODUCT_URL, help="商品链接")
    args = parser.parse_args()

    if args.test_email:
        send_email(
            "【测试】VCA 补货监控邮件",
            "收到这封邮件，说明 Gmail 发信设置正确。\n\n监控商品：\n" + args.url,
        )
        return 0

    if args.test_bark:
        send_bark("【测试】VCA 补货监控", "收到并且响铃，说明 Bark 设置正确", args.url)
        return 0

    print(f"检测：{args.url}")
    print(f"有货按钮：{IN_STOCK_TEXT!r}  没货按钮：{OUT_STOCK_TEXT!r}")
    status = check_stock(args.url)

    if status == "IN":
        print("✅ 有货！正在发送通知……")
        title = "VCA 手链补货了！"
        body = "Vintage Alhambra 5 motifs 现在可以 Add to bag，快去下单"
        errors = []
        try:
            send_bark(title, body, args.url)
        except Exception as e:
            errors.append(f"Bark 失败：{e}")
        try:
            send_email(title, f"{body}\n\n直接打开：\n{args.url}")
        except Exception as e:
            errors.append(f"邮件失败：{e}")
        for err in errors:
            print(err)
        return 2 if errors else 0

    if status == "OUT":
        print("暂时没货。")
        return 0

    print(f"❌ 两个按钮都看不到，已保存截图 {SCREENSHOT}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
