# -*- coding: utf-8 -*-
"""
rate_courses.py
---------------
③ LLM 評分：讀已備妥的 merged/*.jpg ＋ *_analysis.txt，對每門課做
   N 次獨立呼叫（預設 3 次），再以 Python 規則整合成一筆結果。

重點：
  - 規準來自 config/prompt.txt，欄位與整合規則來自 config/fields.json
  - 整合是純 Python 規則（平均／多數決／票數門檻），**不呼叫第二個模型複評**
  - 格式不合的回應整筆作廢，不做局部修補
  - 可中斷續跑（已完成的課自動跳過）、有花費上限保護
  - 失敗課程另寫 failed.csv，方便單獨重跑

輸出：
    analysis_results.json   完整結果（含判斷理由、分歧標記）
    rating.csv              扁平化評分表（供人工檢核與儀表板）
    failed.csv              尚未成功評分的課程

使用方式:
    python rate_courses.py
    python rate_courses.py --limit 10 --cost-cap 3.0
    python rate_courses.py --dry-run
    python rate_courses.py --input failed.csv --retry
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import moocs_config as mc
import moocs_core as mf


def safe_name(name: str, limit: int = 50) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:limit] or name


def parse_speech(text: str) -> dict:
    """從 *_analysis.txt 取回語速統計（前處理階段算好的）。"""
    wpm = re.search(r"平均語速：(\d+)", text)
    rhythm = re.search(r"建議節奏：(\S+)", text)
    return {
        "wpm": int(wpm.group(1)) if wpm else 0,
        "rhythm": rhythm.group(1) if rhythm else "",
        "language": "zh",
        "is_abnormal": False,
    }


# ==========================================
# 輸出：rating.csv（扁平化，供人工檢核與儀表板）
# ==========================================
def rating_columns() -> list[str]:
    cols = ["課程名稱", "評級", "推薦均值"]
    for spec in mc.load_fields()["fields"]:
        if spec["name"] == "判斷原因":
            continue
        cols.append(spec["name"])
    cols.extend(mc.reason_keys())
    cols.extend(["分歧欄位", "分析模型", "分析時間"])
    return cols


def flatten(record: dict) -> dict:
    """把一筆結果攤平成 rating.csv 的一列。"""
    res = record.get("result") or {}
    row = {"課程名稱": record.get("name", "")}

    rec_score = res.get("推薦度")
    row["推薦均值"] = rec_score if isinstance(rec_score, (int, float)) else ""
    row["評級"] = ""   # 由 build_bundles.py 併入同學評分後才定案

    for spec in mc.load_fields()["fields"]:
        name = spec["name"]
        if name == "判斷原因":
            continue
        val = res.get(name)
        if isinstance(val, list):
            val = "、".join(map(str, val))
        elif isinstance(val, bool):
            val = "V" if val else ""
        row[name] = "" if val is None else val

    reasons = res.get("判斷原因") or {}
    for key in mc.reason_keys():
        row[key] = (reasons.get(key) or "").replace("\n", " ")

    row["分歧欄位"] = "；".join(record.get("diverge_fields") or [])
    row["分析模型"] = record.get("analyzer", "")
    row["分析時間"] = record.get("timestamp", "")
    return row


def write_rating_csv(path: Path, records: list[dict]) -> None:
    cols = rating_columns()
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for rec in records:
            if rec.get("result"):
                w.writerow(flatten(rec))


def write_failed_csv(path: Path, pending: list[tuple[str, str]]) -> None:
    """未成功評分的課程；沒有失敗時仍寫表頭，方便單獨重跑。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["課程名稱", "原因"])
        w.writerows(pending)


# ==========================================
def parse_args() -> argparse.Namespace:
    cfg = mc.load_pipeline()
    ap = argparse.ArgumentParser(
        description="用 LLM 依 config/prompt.txt 的規準為課程評分（多次獨立呼叫＋規則整合）")
    ap.add_argument("--queue", default=cfg["files"]["queue"],
                    help="課程佇列 JSON")
    ap.add_argument("--merged-dir", default=cfg["files"]["merged_dir"],
                    help="前處理輸出目錄（讀 *.jpg 與 *_analysis.txt）")
    ap.add_argument("--results", default=cfg["files"]["results"],
                    help="結果 JSON（續跑依據）")
    ap.add_argument("--output", default=cfg["files"]["output_csv"],
                    help="輸出的扁平化評分表 CSV")
    ap.add_argument("--failed", default=cfg["files"]["failed_csv"],
                    help="未成功評分的課程清單 CSV")
    ap.add_argument("--agents", type=int, default=cfg["llm"]["num_agents"],
                    help="同一輸入的獨立呼叫次數")
    ap.add_argument("--model", default=None,
                    help=f"分析用模型（預設 {cfg['llm']['model_priority'][0]}）")
    ap.add_argument("--cost-cap", type=float, default=cfg["llm"]["cost_cap_usd"],
                    help="估算花費上限（USD），達到即停止，可續跑")
    ap.add_argument("--limit", type=int, default=None,
                    help="本次最多分析幾門課")
    ap.add_argument("--only", default=None,
                    help="只分析課名包含此字串的課程")
    ap.add_argument("--retry", action="store_true",
                    help="重跑已有結果的課程（預設跳過）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只檢查前處理是否備齊與估算規模，不呼叫模型")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    if args.model:
        mf.MODEL_PRIORITY = [args.model]
    mf.NUM_AGENTS = args.agents

    queue_path = Path(args.queue)
    if not queue_path.exists():
        print(f"[錯誤] 找不到課程佇列 {queue_path}", file=sys.stderr)
        sys.exit(1)

    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if args.only:
        queue = [c for c in queue if args.only in (c.get("courseName") or "")]

    merged_dir = Path(args.merged_dir)
    results_path = Path(args.results)

    records: list[dict] = []
    if results_path.exists():
        try:
            records = json.loads(results_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[警告] {results_path} 格式損壞，將重新建立", file=sys.stderr)
            records = []
    done = {r["name"] for r in records if r.get("result")}

    print(f"規準：{mc.config_dir() / 'prompt.txt'}（{len(mc.load_prompt())} 字）")
    print(f"欄位：{mc.config_dir() / 'fields.json'}（{len(mc.load_fields()['fields'])} 欄）")
    print(f"模型：{mf.MODEL_PRIORITY[0]} × {mf.NUM_AGENTS} 次獨立呼叫")
    print(f"課程：{len(queue)} 門 | 已完成 {len(done)} 門 | 花費上限 ${args.cost_cap}\n")

    # 盤點哪些課備齊了前處理
    ready, not_ready = [], []
    for course in queue:
        name = course.get("courseName") or f"course_{course.get('courseId')}"
        safe = safe_name(name)
        img = merged_dir / f"{safe}.jpg"
        txt = merged_dir / f"{safe}_analysis.txt"
        (ready if (img.exists() and txt.exists()) else not_ready).append((name, img, txt))

    todo = [(n, i, t) for n, i, t in ready if args.retry or n not in done]

    if args.dry_run:
        print(f"[dry-run] 前處理已備齊：{len(ready)} 門")
        print(f"[dry-run] 尚未備齊　　：{len(not_ready)} 門")
        print(f"[dry-run] 本次將分析　：{len(todo)} 門 × {mf.NUM_AGENTS} 次呼叫 "
              f"= {len(todo) * mf.NUM_AGENTS} 次")
        for n, _, _ in not_ready[:10]:
            print(f"           未備齊：{n[:40]}")
        print("[dry-run] 未呼叫模型，未產生任何輸出")
        return

    def save_results() -> None:
        results_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    analyzed = 0
    pending: list[tuple[str, str]] = [(n, "前處理未備齊") for n, _, _ in not_ready]
    stopped = ""

    for i, (name, img, txt) in enumerate(todo, 1):
        if args.limit and analyzed >= args.limit:
            stopped = f"已達本次上限 {args.limit} 門"
            pending.extend((n, "本次未輪到") for n, _, _ in todo[i - 1:])
            break
        if mf.estimated_cost() >= args.cost_cap:
            stopped = f"已達花費上限 ${args.cost_cap}（目前 ${mf.estimated_cost():.2f}）"
            pending.extend((n, "達花費上限未分析") for n, _, _ in todo[i - 1:])
            break

        print(f"[{i}/{len(todo)}] {name[:34]}")
        text_content = txt.read_text(encoding="utf-8")
        agent_results = asyncio.run(mf.run_three_agents(str(img), text_content))
        merged, diverge = mf.merge_agent_results(agent_results)

        if not merged:
            stopped = "所有呼叫均失敗（可能額度用完或金鑰失效），停止以免空轉"
            pending.extend((n, "呼叫失敗") for n, _, _ in todo[i - 1:])
            break

        records = [r for r in records if r.get("name") != name]
        records.append({
            "name": name,
            "result": merged,
            "diverge_fields": diverge,
            "speech_stats": parse_speech(text_content),
            "analyzer": mf.MODEL_PRIORITY[0],
            "agents": mf.NUM_AGENTS,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        save_results()
        analyzed += 1
        print(f"  能力{merged.get('能力標籤')} 難易度{merged.get('難易度')} "
              f"實作{merged.get('實作程度')} 推薦{merged.get('推薦度')} "
              f"節奏{merged.get('課程節奏')}"
              + (f" | 分歧：{'；'.join(diverge)}" if diverge else ""))

    save_results()
    write_rating_csv(Path(args.output), records)
    write_failed_csv(Path(args.failed), pending)

    ok = len([r for r in records if r.get("result")])
    if stopped:
        print(f"\n[停止] {stopped}")
    print(f"\n本次分析 {analyzed} 門 | 累計完成 {ok}/{len(queue)} 門 "
          f"| 估算花費 ${mf.estimated_cost():.2f}")
    print(f"已輸出：{args.results}、{args.output}、{args.failed}")
    if pending:
        print(f"尚有 {len(pending)} 門未完成，見 {args.failed}；直接重跑本程式即可續做")
    print("下一步：python compare_students.py（有同學標註）或 python build_bundles.py")


if __name__ == "__main__":
    main()
