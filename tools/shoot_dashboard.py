# -*- coding: utf-8 -*-
"""
tools/shoot_dashboard.py
------------------------
為 README 產生儀表板截圖（references/*.jpg）。

用 Playwright 開本機產生的 dashboard.html，逐個分頁截圖。
每次改版重跑即可，截圖與實際輸出不會對不上。

使用方式:
    python tools/shoot_dashboard.py
    python tools/shoot_dashboard.py --input data/dashboard.html --out references
    python tools/shoot_dashboard.py --width 1100 --dark
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]

# (輸出檔名, 分頁文字, 捲動到的位置, 截圖高度)
SHOTS = [
    ("1-overview.jpg",  None,              0,    900),
    ("2-courses.jpg",   "課程總覽",         620,  760),
    ("3-charts.jpg",    "分布圖表",         420,  720),
    ("4-agreement.jpg", "AI 與真人一致性",  430,  820),
    ("5-bundles.jpg",   "主題套裝",         430,  780),
]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="為 README 產生儀表板截圖")
    ap.add_argument("--input", default="data/dashboard.html", help="要截圖的儀表板 HTML")
    ap.add_argument("--out", default="references", help="輸出目錄")
    ap.add_argument("--width", type=int, default=1120, help="視窗寬度")
    ap.add_argument("--scale", type=float, default=1.25, help="裝置像素比（越大越清晰）")
    ap.add_argument("--dark", action="store_true", help="截深色模式")
    ap.add_argument("--quality", type=int, default=82, help="JPEG 品質")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    src = (ROOT / args.input).resolve()
    if not src.exists():
        print(f"[錯誤] 找不到 {src}\n"
              f"       請先執行 build_dashboard.py 產生儀表板。", file=sys.stderr)
        sys.exit(1)

    out_dir = (ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[錯誤] 缺少 playwright，請安裝：pip install -e .[fetch]", file=sys.stderr)
        sys.exit(1)

    print(f"來源：{src}")
    print(f"輸出：{out_dir}　寬度 {args.width}　"
          f"{'深色' if args.dark else '淺色'}模式\n")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(
            viewport={"width": args.width, "height": 900},
            device_scale_factor=args.scale,
            color_scheme="dark" if args.dark else "light",
        ).new_page()
        page.goto(src.as_uri())
        page.wait_for_selector(".tile")

        for filename, tab, scroll_y, height in SHOTS:
            if tab:
                page.click(f'nav.tabs button:text-is("{tab}")')
                page.wait_for_timeout(260)       # 等圖表畫完
            else:
                page.click('nav.tabs button:text-is("課程總覽")')
            page.evaluate(f"window.scrollTo(0, {scroll_y})")
            page.wait_for_timeout(160)

            path = out_dir / filename
            page.screenshot(path=str(path), type="jpeg", quality=args.quality,
                            clip={"x": 0, "y": 0, "width": args.width, "height": height})
            print(f"  {filename:<18} {path.stat().st_size // 1024:>4} KB"
                  + (f"　分頁「{tab}」" if tab else "　頁首"))

        browser.close()

    print(f"\n完成：{len(SHOTS)} 張截圖已更新。")


if __name__ == "__main__":
    main()
