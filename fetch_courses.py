# -*- coding: utf-8 -*-
"""
fetch_courses.py
----------------
① 自磨課師平臺取得課程清單與影片位址 → moocs_queue.json

每門課取出：課名、課程簡介、章節結構、YouTube 影片 ID、平臺 MP4 位址。
章節結構是後續「章節感知抽樣」的依據，不可省略。

關鍵 API：/api/v1/course-material-list?course_id={id}&return_part=false
學習頁面：/#/learning/{course_id}

需要平臺登入狀態（session.json）；可中斷續跑（已取得影片的課程自動跳過）。
課程 ID 清單放在 config/course_ids.txt，換一批課程只需改這份檔。

使用方式:
    python save_login.py                      # 先登入一次，產生 session.json
    python fetch_courses.py
    python fetch_courses.py --ids config/course_ids.txt --output moocs_queue.json
    python fetch_courses.py --headless --limit 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright, Page

import moocs_config as mc

BASE = "https://moocs.moe.edu.tw"
YT_RE = re.compile(r'(?:youtube\.com/(?:embed/|watch\?v=|v/)|youtu\.be/)([a-zA-Z0-9_-]{11})')
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def load_course_ids(path: Path) -> list[int]:
    """讀取課程 ID 清單（一行一個，# 為註解）。"""
    if not path.exists():
        print(f"[錯誤] 找不到課程 ID 清單 {path}", file=sys.stderr)
        sys.exit(1)
    ids: list[int] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.isdigit():
            ids.append(int(line))
    if not ids:
        print(f"[錯誤] {path} 沒有任何課程 ID", file=sys.stderr)
        sys.exit(1)
    return ids


# ==========================================
# 以下抓取邏輯沿用既有實作（已處理平臺的分頁、章節與 MP4 取址行為）
# ==========================================
def extract_from_items(items: list, seen_yt: set, seen_mp4: set) -> list:
    """遞迴提取 items（含子 items），回傳章節列表"""
    chapters = []
    for it in items:
        href = (it.get('href') or '').strip()
        name = it.get('text') or ''
        sub_items = it.get('items') or []

        yt_ids = []
        mp4_urls = []

        # YouTube
        m = YT_RE.search(href)
        if m and m.group(1) not in seen_yt:
            seen_yt.add(m.group(1))
            yt_ids.append(m.group(1))

        # MP4：直接用整個 href（URL 可能含中文和空格，不用 regex）
        if href and '.mp4' in href.lower() and href not in seen_mp4:
            seen_mp4.add(href)
            mp4_urls.append(href)

        # 子 items（遞迴）
        sub_chapters = extract_from_items(sub_items, seen_yt, seen_mp4)

        if yt_ids or mp4_urls:
            chapters.append({'name': name, 'videoIds': yt_ids, 'mp4Urls': mp4_urls})
        if sub_chapters:
            # 如果本身是章節容器（無影片），把子章節攤開，並加上章節名稱前綴
            if not yt_ids and not mp4_urls and name:
                for sc in sub_chapters:
                    sc['chapter'] = name
            chapters.extend(sub_chapters)

    return chapters


def fetch_course(page: Page, course_id: int) -> dict:
    """直接呼叫 API 取得課程所有影片，不依賴頁面導覽"""
    result = {
        'courseId': str(course_id),
        'courseName': '', 'courseDesc': '',
        'chapters': [], 'videoIds': [], 'mp4Urls': [],
    }

    # 1. 取課程基本資訊（名稱、簡介）
    try:
        info = page.evaluate(f"""
        async () => {{
            const r = await fetch('/api/v1/courses/{course_id}/info');
            return await r.json();
        }}""")
        d = (info.get('data') or {}).get('introduce') or info.get('data') or {}
        result['courseName'] = d.get('name') or info.get('data', {}).get('name', '')
        desc = d.get('text') or d.get('introduction') or ''
        result['courseDesc'] = re.sub(r'<[^>]+>', '', desc).strip()[:500]
    except Exception:
        pass

    if not result['courseName']:
        try:
            meta = page.evaluate(f"""
            async () => {{
                const r = await fetch('/api/v1/courses/{course_id}?majored=true');
                return await r.json();
            }}""")
            d = meta.get('data') or {}
            result['courseName'] = d.get('name', '')
            result['courseDesc'] = d.get('introduction', '')[:500]
        except Exception:
            pass

    # 2. 報名課程（忽略錯誤）
    try:
        sr = page.evaluate(f"""
        async () => {{
            const r = await fetch('/api/v1/courses/{course_id}/sign-up',
                {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'}});
            return r.status;
        }}""")
        if sr == 200:
            print("(已報名)", end=' ')
        elif sr == 403:
            print("(已是學員)", end=' ')
        else:
            print(f"(報名:{sr})", end=' ')
    except Exception:
        pass

    # 3. 呼叫 course-material-list API（關鍵！）
    try:
        materials = page.evaluate(f"""
        async () => {{
            const r = await fetch('/api/v1/course-material-list?course_id={course_id}&return_part=false');
            if (!r.ok) return {{error: r.status}};
            return await r.json();
        }}""")

        if 'error' in materials:
            print(f"(material API:{materials['error']})", end=' ')
        else:
            items = (materials.get('data') or {}).get('items') or []
            seen_yt: set = set()
            seen_mp4: set = set()
            chapters = extract_from_items(items, seen_yt, seen_mp4)
            result['chapters'] = chapters
            result['videoIds'] = list(seen_yt)
            result['mp4Urls'] = list(seen_mp4)

    except Exception as e:
        print(f"(material err:{e})", end=' ')

    # 4. 如果 material list 沒有結果，試導覽到學習頁面攔截
    if not result['videoIds']:
        try:
            intercepted = {}
            def on_resp(r):
                if 'course-material-list' in r.url or 'material' in r.url:
                    try:
                        intercepted[r.url] = r.text()
                    except Exception:
                        pass
            page.on('response', on_resp)

            page.goto(f"{BASE}/moocs/#/learning/{course_id}", timeout=20000)
            time.sleep(3)
            page.remove_listener('response', on_resp)

            for url, body in intercepted.items():
                try:
                    d = json.loads(body)
                    items = (d.get('data') or {}).get('items') or []
                    for it in items:
                        m = YT_RE.search(it.get('href', ''))
                        if m:
                            vid_id = m.group(1)
                            if vid_id not in result['videoIds']:
                                result['videoIds'].append(vid_id)
                                result['chapters'].append({
                                    'name': it.get('text', ''),
                                    'videoIds': [vid_id]
                                })
                except Exception:
                    pass

            # 也從 DOM 補充
            dom = page.content()
            for vid_id in YT_RE.findall(dom):
                if vid_id not in result['videoIds']:
                    result['videoIds'].append(vid_id)

            # 導回首頁（避免下一門課受影響）
            page.goto(f"{BASE}/moocs/#/home", timeout=10000)
            time.sleep(1)
        except Exception as e:
            print(f"(nav err:{e})", end=' ')
            try:
                page.goto(f"{BASE}/moocs/#/home", timeout=10000)
            except Exception:
                pass

    return result



# ==========================================
# 主程式
# ==========================================
def parse_args() -> argparse.Namespace:
    cfg = mc.load_pipeline()
    ap = argparse.ArgumentParser(
        description="自磨課師平臺取得課程清單與影片位址，輸出課程佇列 JSON")
    ap.add_argument("--ids", default=str(mc.config_dir() / "course_ids.txt"),
                    help="課程 ID 清單檔（一行一個，# 為註解）")
    ap.add_argument("--output", default=cfg["files"]["queue"],
                    help="輸出的課程佇列 JSON")
    ap.add_argument("--session", default=cfg["files"]["session"],
                    help="平臺登入狀態檔（由 save_login.py 產生）")
    ap.add_argument("--headless", action="store_true",
                    help="不開視窗執行（平臺改版時較難除錯，預設開視窗）")
    ap.add_argument("--limit", type=int, default=None,
                    help="本次最多抓幾門課")
    ap.add_argument("--refetch", action="store_true",
                    help="已取得影片的課程也重新抓")
    return ap.parse_args()


def print_summary(results: list[dict]) -> None:
    total_yt = sum(len(c.get("videoIds", [])) for c in results)
    total_mp4 = sum(len(c.get("mp4Urls", [])) for c in results)
    empty = [c["courseId"] for c in results
             if not c.get("videoIds") and not c.get("mp4Urls")]
    print(f"\n課程 {len(results)} 門 | YouTube {total_yt} 支 | 平臺 MP4 {total_mp4} 支 "
          f"| 合計 {total_yt + total_mp4} 支")
    if empty:
        print(f"[注意] 仍無影片 {len(empty)} 門：{empty}")
        print("       可能原因：課程已下架、需另行報名、或平臺改版。請人工確認後再排除。")


def main() -> None:
    args = parse_args()
    course_ids = load_course_ids(Path(args.ids))
    output = Path(args.output)

    if not os.path.exists(args.session):
        print(f"[錯誤] 找不到登入狀態檔 {args.session}\n"
              f"       請先執行：python save_login.py", file=sys.stderr)
        sys.exit(1)

    existing: dict[int, dict] = {}
    if output.exists():
        try:
            for c in json.loads(output.read_text(encoding="utf-8")):
                existing[int(c.get("courseId", 0))] = c
        except (json.JSONDecodeError, ValueError):
            print(f"[警告] {output} 格式損壞，將重新建立", file=sys.stderr)

    to_scrape = [cid for cid in course_ids
                 if args.refetch or cid not in existing
                 or (not existing[cid].get("videoIds") and not existing[cid].get("mp4Urls"))]
    if args.limit:
        to_scrape = to_scrape[:args.limit]
    results = list(existing.values())

    print(f"課程 ID 清單：{args.ids}（{len(course_ids)} 門）")
    print(f"已有影片 {len(course_ids) - len(to_scrape)} 門 | 待抓 {len(to_scrape)} 門\n")
    if not to_scrape:
        print("全部已完成。")
        print_summary(results)
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            args=[] if args.headless else ["--start-maximized"])
        ctx = browser.new_context(storage_state=args.session, user_agent=UA)
        ctx.add_init_script(
            "window.alert=()=>{};window.confirm=()=>true;window.prompt=()=>'';")
        page = ctx.new_page()
        page.goto(f"{BASE}/moocs/#/home", timeout=30000)
        time.sleep(2)

        for idx, course_id in enumerate(to_scrape, 1):
            print(f"[{idx}/{len(to_scrape)}] {course_id} ...", end=" ", flush=True)
            try:
                r = fetch_course(page, course_id)
                print(f"YT:{len(r['videoIds'])} MP4:{len(r.get('mp4Urls', []))} "
                      f"| {(r['courseName'] or '')[:22]}")
            except Exception as e:
                print(f"失敗：{e}")
                r = {"courseId": str(course_id), "courseName": "", "courseDesc": "",
                     "chapters": [], "videoIds": [], "mp4Urls": []}
            results = [c for c in results if c["courseId"] != str(course_id)]
            results.append(r)
            output.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            time.sleep(0.5)

        browser.close()

    print_summary(results)
    print(f"已輸出：{output}")
    print(f"下一步：{mc.cmd('preprocess_courses')}")


if __name__ == "__main__":
    main()
