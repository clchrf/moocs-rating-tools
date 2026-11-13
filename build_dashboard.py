# -*- coding: utf-8 -*-
"""
build_dashboard.py
------------------
⑥ 把評分結果組成單一互動 HTML 儀表板（可篩選、排序、看逐課明細與判斷理由）。

單檔輸出、無外部連線：所有資料內嵌，可離線開啟、可用隨身碟轉交。

資料來源（有就用，沒有就略過該分頁）：
    analysis_results.json        AI 評分結果（必要）
    bundles.json                 評級與主題套裝（build_bundles.py 產生）
    AI_vs_同學_比對結果.json      一致性指標（compare_students.py 產生，已去識別化）
    moocs_queue.json             課號、章節、影片數

去識別化：儀表板只讀 `AI_vs_同學_比對結果.json`（無姓名），
不讀同學標註原始表，因此輸出可對外。

使用方式:
    python build_dashboard.py
    python build_dashboard.py --output 磨課師AI課程評分儀表板.html
    python build_dashboard.py --results analysis_results.json --bundles bundles.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import moocs_config as mc

FACETS = ["趣味度", "內容深度", "實作程度", "結構清晰度", "應用廣度"]
AUD = ["通識素養型", "跨域應用型", "技術實作型"]
CAP_NAMES = {
    "A": "A. 運用AI學習的能力",
    "B": "B. 負責任使用AI的能力",
    "C": "C. 運用AI解決問題的能力",
    "D": "D. AI無法取代的能力",
}
TEMPLATE = "dashboard_template.html"


def load_json(path: Path | None):
    if not path or not Path(path).exists():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[警告] {path} 不是合法 JSON，已略過（{e}）", file=sys.stderr)
        return None


def find_template() -> Path:
    for cand in (Path(TEMPLATE),
                 Path(mc.config_dir()).parent / TEMPLATE,
                 Path(__file__).resolve().parent / TEMPLATE):
        if cand.exists():
            return cand
    print(f"[錯誤] 找不到樣板 {TEMPLATE}（需與執行檔或腳本放在同一層）", file=sys.stderr)
    sys.exit(1)


# ==========================================
#: 同名課在結果中會寫成「課名（10002646）」，以括號內的課號消歧義
_CID_IN_NAME = re.compile(r"[（(](\d{6,})[)）]\s*$")


def split_cid(name: str) -> tuple[str, str]:
    """把「課名（課號）」拆成 (純課名, 課號)；沒有括號課號時課號為空字串。"""
    m = _CID_IN_NAME.search(name or "")
    return (name[:m.start()].strip(), m.group(1)) if m else (name or "", "")


def build_courses(results, bundles_payload, queue, compare) -> list[dict]:
    """把各來源併成儀表板用的課程列表。

    對應以課號優先、課名其次：平臺上有 3 組同名課，分析結果會寫成
    「課名（課號）」來消歧義，只靠課名會對不回佇列而少算影片數。
    """
    by_cid: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for c in (queue or []):
        entry = {
            "cid": str(c.get("courseId") or ""),
            "videos": len(c.get("videoIds", [])) + len(c.get("mp4Urls", [])),
            "chapters": len(c.get("chapters", [])),
        }
        if entry["cid"]:
            by_cid[entry["cid"]] = entry
        name = c.get("courseName") or ""
        if name and name not in by_name:      # 同名課只留第一筆當後備
            by_name[name] = entry

    # 課名 → 課號的後備對照。佇列中有課程抓不到課名（平臺未提供），
    # 其課名是後來自官方清單補上的，只能靠這份對照接回去。
    name_to_cid: dict[str, str] = {}
    for c in ((compare or {}).get("courses") or []):
        if c.get("name") and c.get("courseId"):
            name_to_cid.setdefault(c["name"], str(c["courseId"]))

    def lookup(result_name: str) -> dict:
        plain, cid = split_cid(result_name)
        if cid and cid in by_cid:
            return by_cid[cid]
        hit = by_name.get(result_name) or by_name.get(plain)
        if hit:
            return hit
        alt = name_to_cid.get(result_name) or name_to_cid.get(plain)
        return by_cid.get(alt, {}) if alt else {}
    # 開課單位（來自去識別化比對結果，以課號為主鍵）
    orgs_by_cid, orgs_by_name = {}, {}
    for c in ((compare or {}).get("courses") or []):
        if c.get("courseId"):
            orgs_by_cid[str(c["courseId"])] = c.get("org", "")
        if c.get("name"):
            orgs_by_name.setdefault(c["name"], c.get("org", ""))

    # 評級與推薦均值（三票併入後的結果）
    grades_by_cid, grades_by_name = {}, {}
    for c in ((bundles_payload or {}).get("courses") or []):
        if c.get("cid"):
            grades_by_cid[str(c["cid"])] = c
        if c.get("name"):
            grades_by_name.setdefault(c["name"], c)

    out = []
    for rec in results:
        res = rec.get("result")
        if not res:
            continue
        name = rec.get("name", "")
        plain, name_cid = split_cid(name)
        m = lookup(name)
        cid = name_cid or m.get("cid") or ""

        g = (grades_by_cid.get(cid) or grades_by_name.get(name)
             or grades_by_name.get(plain) or {})
        org = (orgs_by_cid.get(cid) or orgs_by_name.get(name)
               or orgs_by_name.get(plain) or "")

        scores = {f: res.get(f) for f in FACETS}
        out.append({
            "name": name,
            "cid": cid or g.get("cid") or "",
            "org": org,
            "grade": g.get("grade") or "",
            "rec": g.get("avg_rec"),
            "aiRec": res.get("推薦度"),
            "scores": scores,
            "caps": res.get("能力標籤", []),
            "diff": res.get("難易度", ""),
            "pace": res.get("課程節奏", ""),
            "aud": {a: bool(res.get(a)) for a in AUD},
            "hours": g.get("hours") or 0,
            "videos": m.get("videos", 0),
            "summary": res.get("課程摘要", ""),
            "keywords": res.get("關鍵字", []),
            "codes": res.get("細項代碼", []),
            "why": {k: v for k, v in (res.get("判斷原因") or {}).items() if v},
            "diverge": "；".join(rec.get("diverge_fields") or []),
        })
    return out


def build_agreement(compare) -> dict:
    """把比對結果整理成「AI 一致率 vs 同學之間一致率」的對讀表。"""
    if not compare or not compare.get("metrics"):
        return {}
    ai_rows, human = [], {}
    for m in compare["metrics"]:
        field = m.get("欄位", "")
        if m.get("比對對象") == "同學1 vs 同學2":
            human[field] = m.get("一致率")
        elif m.get("比對對象") == "AI vs 同學":
            ai_rows.append(m)

    rows = []
    for m in ai_rows:
        field = m.get("欄位", "")
        rows.append({
            "field": field,
            "type": m.get("類型", ""),
            "metric": m.get("指標", ""),
            "value": m.get("值"),
            "ai": m.get("一致率"),
            "human": human.get(field),
            "n": m.get("n"),
            "interp": m.get("判讀", ""),
        })

    ov = compare.get("overview") or {}
    notice = (
        f"<b>一致性比較的意義</b>　一致率是「兩軌判定相同的課程占比」。"
        f"單看 AI 的一致率無法判斷好壞，須與<b>同學兩人之間的一致率</b>並列對讀 —— "
        f"後者是這份標註工作本身的難度基準。"
        f"可比對 {ov.get('comparable', '—')} 門課，其中兩輪都完成 "
        f"{ov.get('both_rounds', '—')} 門。"
    )
    if ov.get("arbitration_note"):
        notice += f"<br><br>{ov['arbitration_note']}"
    return {"notice": notice, "rows": rows}


def build_bundles_view(bundles_payload) -> list[dict]:
    out = []
    for b in ((bundles_payload or {}).get("bundles") or []):
        out.append({
            "bid": b.get("bid", ""),
            "title": b.get("name", "") or b.get("key", ""),
            "desc": b.get("tagline", ""),
            "path_note": b.get("path_note", ""),
            "prereq": b.get("prereq", ""),
            "goals": b.get("goals", []),
            "n": b.get("n", len(b.get("courses", []))),
            "hours": b.get("hours", 0),
            "avg_rec": b.get("avg_rec", ""),
            "track": b.get("track", ""),
            "target": b.get("target", ""),
            "codes": b.get("codes", []),
            "course_names": b.get("course_names", []),
        })
    return out


def parse_args() -> argparse.Namespace:
    cfg = mc.load_pipeline()
    ap = argparse.ArgumentParser(
        description="把評分結果組成單一互動 HTML 儀表板（單檔、可離線開啟）")
    ap.add_argument("--results", default=cfg["files"]["results"],
                    help="AI 評分結果 JSON")
    ap.add_argument("--bundles", default="bundles.json",
                    help="評級與主題套裝 JSON（build_bundles.py 產生）")
    ap.add_argument("--compare", default="AI_vs_同學_比對結果.json",
                    help="去識別化的一致性比對結果 JSON")
    ap.add_argument("--queue", default=cfg["files"]["queue"],
                    help="課程佇列 JSON（補課號與影片數）")
    ap.add_argument("--template", default=None, help="HTML 樣板路徑")
    ap.add_argument("--output", default="dashboard.html", help="輸出的 HTML 檔")
    ap.add_argument("--title", default="磨課師 AI 課程評分儀表板", help="頁面標題")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    results = load_json(Path(args.results))
    if not results:
        print(f"[錯誤] 找不到或無法讀取 {args.results}\n"
              f"       請先執行：{mc.cmd('rate_courses')}", file=sys.stderr)
        sys.exit(1)

    bundles_payload = load_json(Path(args.bundles))
    compare = load_json(Path(args.compare))
    queue = load_json(Path(args.queue))

    courses = build_courses(results, bundles_payload, queue, compare)
    if not courses:
        print("[錯誤] 沒有任何有效的評分結果", file=sys.stderr)
        sys.exit(1)

    agreement = build_agreement(compare)
    bundles = build_bundles_view(bundles_payload)
    cfg = mc.load_pipeline()
    th = (bundles_payload or {}).get("thresholds") or {
        "a": cfg["grading"]["a_threshold"], "aplus": cfg["grading"]["aplus_threshold"]}

    graded = [c for c in courses if c["grade"]]
    n_aplus = sum(1 for c in courses if c["grade"] == "A+")
    n_a = sum(1 for c in courses if c["grade"] == "A")
    n_videos = sum(c["videos"] for c in courses)
    ai_recs = [c["aiRec"] for c in courses if isinstance(c["aiRec"], (int, float))]

    tiles = [
        {"k": "課程", "v": len(courses), "n": "完成 AI 評分"},
        {"k": "平臺影片", "v": n_videos or "—", "n": "取自課程佇列"},
        {"k": "A＋／A 級", "v": f"{n_aplus}／{n_a}" if graded else "—",
         "n": f"門檻 {th['aplus']}／{th['a']}" if graded else "待併入同學標註"},
        {"k": "AI 推薦度平均",
         "v": f"{sum(ai_recs) / len(ai_recs):.2f}" if ai_recs else "—",
         "n": f"最高 {max(ai_recs)}　最低 {min(ai_recs)}" if ai_recs else ""},
        {"k": "主題套裝", "v": len(bundles) or "—",
         "n": f"納入 {len({n for b in bundles for n in b['course_names']})} 門" if bundles else "尚未組成"},
    ]
    if agreement.get("rows"):
        vals = [r["ai"] for r in agreement["rows"] if isinstance(r.get("ai"), (int, float))]
        if vals:
            tiles.append({"k": "AI 與同學一致率",
                          "v": f"{sum(vals) / len(vals):.0f}%",
                          "n": f"{len(vals)} 個欄位平均"})

    mode = (bundles_payload or {}).get("mode", "初評（僅 AI）")
    notice = (
        "<b>怎麼讀這份儀表板</b>　每門課的分數不是 AI「講出來的推薦」，"
        f"而是 AI 依固定規準（{len(mc.load_prompt())} 字）填完 "
        f"{len(mc.load_fields()['fields'])} 個欄位後，由程式規則算出來的。"
        f"多次獨立呼叫的結果以平均／多數決／票數門檻整合，<b>不呼叫第二個模型複評</b>。"
        f"<br><br><b>評級模式：{mode}</b>　"
    )
    notice += (
        "推薦均值 = mean(AI 推薦度, 同學1 推薦度, 同學2 推薦度)，AI 只佔三票之一。"
        if "三票" in mode else
        "目前僅含 AI 一票，<b>不可當成最終評級</b>；須併入同學標註後重新產生。"
    )
    notice += (
        "<br><br><b>引用時請注意</b>　AI 判讀可能偏差，結果供快速掌握現況之用；"
        "逐課明細附有判斷理由，請以理由與原始課程內容覆核後再引用。"
    )

    payload = {
        "meta": [f"課程 {len(courses)} 門",
                 f"產生日期 {date.today().isoformat()}",
                 f"分析模型 {(results[0] or {}).get('analyzer', cfg['llm']['model_priority'][0])}"
                 + f" × {cfg['llm']['num_agents']} 次獨立呼叫"],
        "notice": notice,
        "footer": (
            "本頁由 <code>build_dashboard</code> 自資料檔產生，所有數字即時計算，未手動填寫。"
            "<br>資料來源：<code>analysis_results.json</code>"
            + ("、<code>bundles.json</code>" if bundles_payload else "")
            + ("、<code>AI_vs_同學_比對結果.json</code>（已去識別化，無姓名）" if compare else "")
            + "<br>同學標註原始表含姓名與自由填答，標示「請勿外流」，不納入本頁。"
        ),
        "cmd": {"bundles": mc.cmd("build_bundles"),
                "compare": mc.cmd("compare_students"),
                "rate": mc.cmd("rate_courses")},
        "tiles": tiles,
        "thresholds": th,
        "capNames": CAP_NAMES,
        "courses": courses,
        "bundles": bundles,
        "agreement": agreement,
    }

    tpl_path = Path(args.template) if args.template else find_template()
    html = tpl_path.read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 避免內嵌 JSON 提前關閉 <script>
    data = data.replace("</", "<\\/")

    for key, val in (("__TITLE__", args.title),
                     ("__SUBTITLE__", f"{len(courses)} 門課的能力標籤、品質評級與主題學習路徑"
                                      f"　·　{mode}"),
                     ("__DATA__", data)):
        html = html.replace(key, val)

    out = Path(args.output)
    out.write_text(html, encoding="utf-8")
    size_kb = out.stat().st_size // 1024

    print(f"樣板：{tpl_path}")
    print(f"課程 {len(courses)} 門"
          + (f"（A+ {n_aplus} / A {n_a}）" if graded else "（尚未分級）"))
    print(f"主題套裝：{len(bundles)} 組" if bundles else "主題套裝：無（執行 build_bundles.py 後可顯示）")
    print(f"一致性指標：{len(agreement.get('rows', []))} 個欄位"
          if agreement.get("rows") else
          "一致性指標：無（執行 compare_students.py 後可顯示）")
    print(f"\n已輸出：{out}（{size_kb} KB，單檔、可離線開啟）")


if __name__ == "__main__":
    main()
