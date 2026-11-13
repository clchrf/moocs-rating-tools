# -*- coding: utf-8 -*-
"""
build_bundles.py
----------------
⑤ 分級與主題套裝：算推薦均值 → 切 A+／A／C → 組出主題套裝推薦清單。

推薦均值的定義（重點，常被誤解）：

    推薦均值 = mean(AI 推薦度, 同學1 推薦度, 同學2 推薦度)

**推薦分數是算出來的，不是 AI 講出來的** —— AI 只佔三票之一。
因此本步驟有兩種模式：

    --source <定稿標籤表.xlsx>   三票併入後的正式模式（交付用）
    --results analysis_results.json  只有 AI 一票的初評模式（尚無同學標註時）

初評模式的均值僅反映 AI 單方判斷，輸出會明確標記，不可當成最終評級。

分級門檻與套裝定義：
    config/pipeline.json  grading.a_threshold / grading.aplus_threshold
    bundles_data.py       套裝主題、課程組合、能力架構對應（領域設定）

輸出：
    bundles.json          套裝與課程評級（供 build_dashboard.py）
    course_grades.csv     逐課評級表

使用方式:
    python build_bundles.py --source "磨課師AI課程能力分類標籤表_定稿(AI+同學).xlsx"
    python build_bundles.py --results analysis_results.json
    python build_bundles.py --source 定稿.xlsx --strict
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import moocs_config as mc


def grade_of(avg: float, a_th: float, aplus_th: float) -> str:
    if avg >= aplus_th:
        return "A+"
    return "A" if avg >= a_th else "C"


# ==========================================
# 初評模式：只有 AI 一票
# ==========================================
def courses_from_results(path: Path, a_th: float, aplus_th: float) -> dict:
    records = json.loads(path.read_text(encoding="utf-8"))
    courses = {}
    for i, rec in enumerate(records, 1):
        res = rec.get("result")
        if not res:
            continue
        avg = res.get("推薦度")
        if not isinstance(avg, (int, float)):
            continue
        cid = str(rec.get("courseId") or i)
        courses[cid] = {
            "cid": cid,
            "name": rec.get("name", ""),
            "avg_rec": round(float(avg), 2),
            "grade": grade_of(avg, a_th, aplus_th),
            "labels": "、".join(res.get("能力標籤", [])),
            "codes": "、".join(res.get("細項代碼", [])),
            "diff": res.get("難易度", ""),
            "hours": 0,
            "votes": 1,
            "vote_source": "僅 AI（尚無同學標註）",
        }
    return courses


# ==========================================
def parse_args() -> argparse.Namespace:
    cfg = mc.load_pipeline()
    ap = argparse.ArgumentParser(
        description="依推薦均值分級並組出主題套裝推薦清單")
    ap.add_argument("--source", default=None,
                    help="定稿標籤表 .xlsx（AI＋同學三票併入後的正式來源）")
    ap.add_argument("--results", default=cfg["files"]["results"],
                    help="AI 評分結果 JSON（無定稿標籤表時的初評來源）")
    ap.add_argument("--output", default="bundles.json",
                    help="輸出的套裝與評級 JSON（供儀表板）")
    ap.add_argument("--grades-csv", default="course_grades.csv",
                    help="輸出的逐課評級 CSV")
    ap.add_argument("--a-threshold", type=float, default=cfg["grading"]["a_threshold"],
                    help="A 級門檻（納入套裝）")
    ap.add_argument("--aplus-threshold", type=float, default=cfg["grading"]["aplus_threshold"],
                    help="A+ 級門檻（得重複納入不同套裝）")
    ap.add_argument("--strict", action="store_true",
                    help="套裝自我檢核未通過時以非零碼結束（交付前把關用）")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    a_th, aplus_th = args.a_threshold, args.aplus_threshold

    print(f"分級門檻：A ≥ {a_th} ｜ A+ ≥ {aplus_th}")

    bundles = []
    errors: list[str] = []

    if args.source and Path(args.source).exists():
        # ---- 正式模式：三票併入後的定稿標籤表 ----
        import bundles_data as bd
        bd.A_TH, bd.APLUS_TH = a_th, aplus_th
        print(f"來源：{args.source}（AI＋同學，三票併入）")
        courses = bd.load_courses(args.source)
        for c in courses.values():
            c.setdefault("votes", 3)
            c.setdefault("vote_source", "AI＋同學兩輪")
        bundles = bd.enrich(bd.BUNDLES, courses)
        errors = bd.validate(bundles, courses)
        for b in bundles:
            b["track"] = bd.TRACK.get(b["key"], "")
            b["short_target"] = bd.SHORT_TARGET.get(b["key"], "")
        mode = "正式（三票）"
    else:
        # ---- 初評模式：只有 AI 一票 ----
        rpath = Path(args.results)
        if not rpath.exists():
            print(f"[錯誤] 找不到 {args.source or rpath}。\n"
                  f"       請用 --source 指定定稿標籤表，或先執行 rate_courses.py。",
                  file=sys.stderr)
            sys.exit(1)
        if args.source:
            print(f"[警告] 找不到定稿標籤表 {args.source}，改用初評模式。", file=sys.stderr)
        print(f"來源：{rpath}（僅 AI 一票，初評）")
        courses = courses_from_results(rpath, a_th, aplus_th)
        mode = "初評（僅 AI）"

    if not courses:
        print("[錯誤] 沒有可分級的課程", file=sys.stderr)
        sys.exit(1)

    # ---- 統計 ----
    n_aplus = sum(1 for c in courses.values() if c["grade"] == "A+")
    n_a = sum(1 for c in courses.values() if c["grade"] == "A")
    n_c = sum(1 for c in courses.values() if c["grade"] == "C")
    print(f"\n課程母體 {len(courses)} 門 → A+ {n_aplus} 門 / A {n_a} 門 / C {n_c} 門")

    if bundles:
        used = {cid for b in bundles for cid in b["courses"]}
        slots = sum(b["n"] for b in bundles)
        print(f"主題套裝 {len(bundles)} 組 | 納入席次 {slots}（去重後 {len(used)} 門）")
        for b in bundles:
            print(f"  {b['bid']}  {b['name']:<22} {b['n']} 門 "
                  f"{b['hours']:>3} 小時  均值 {b['avg_rec']}  {b.get('track', '')}")
    else:
        print("（初評模式不組套裝：套裝需以三票併入後的評級為準）")

    if errors:
        print(f"\n[套裝自我檢核] 未通過 {len(errors)} 項：")
        for e in errors:
            print(f"  - {e}")
    elif bundles:
        print("\n[套裝自我檢核] 全部通過（課程數 2–5 門、不納入 C 級、"
              "重複納入僅限 A+、A 級全數納入）")

    # ---- 輸出 ----
    payload = {
        "mode": mode,
        "thresholds": {"a": a_th, "aplus": aplus_th},
        "summary": {"total": len(courses), "A+": n_aplus, "A": n_a, "C": n_c},
        "courses": list(courses.values()),
        "bundles": [
            {k: v for k, v in b.items() if k != "course_objs"} | {
                "course_names": [courses[cid]["name"] for cid in b["courses"]]}
            for b in bundles
        ],
        "validation_errors": errors,
    }
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with open(args.grades_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["課號", "課程名稱", "推薦均值", "評級", "票數來源",
                    "能力標籤", "難易度", "時數"])
        for c in sorted(courses.values(), key=lambda x: -x["avg_rec"]):
            w.writerow([c["cid"], c["name"], c["avg_rec"], c["grade"],
                        c.get("vote_source", ""), c.get("labels", ""),
                        c.get("diff", ""), c.get("hours", "")])

    print(f"\n已輸出：{args.output}、{args.grades_csv}")
    print("下一步：python build_dashboard.py")

    if errors and args.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
