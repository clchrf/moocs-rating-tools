# -*- coding: utf-8 -*-
"""
preprocess_courses.py
---------------------
② 前處理：下載影片 → 章節感知抽樣 → 抽幀合併 → 切音訊 → Whisper 轉逐字稿。

**不呼叫 LLM、不耗 API 額度。** 與 ③ rate_courses 刻意分離：
前處理可一次把影片跑完（耗時但免費），模型分析再隨時中斷續跑，不必重抓影片。

輸出（供 ③ 讀取）：
    merged/<課名>.jpg              合併接觸表（模型的視覺輸入）
    merged/<課名>_analysis.txt     課名＋簡介＋語速＋逐字稿節錄（模型的文字輸入）

可中斷續跑：兩個輸出檔都存在的課程會自動跳過。

使用方式:
    python preprocess_courses.py
    python preprocess_courses.py --queue moocs_queue.json --limit 10
    python preprocess_courses.py --only "AI技術與應用" --force
"""

from __future__ import annotations

import argparse
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
    """把課名轉成可當檔名的字串（與舊版規則相同，確保續跑能對上既有檔案）。"""
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:limit]


def write_analysis_text(path: Path, course: dict, transcripts: list[str],
                        stats: list[dict], language: str, truncate: int) -> int:
    """寫出模型的文字輸入，回傳逐字稿總字數。"""
    combined = "\n---\n".join(transcripts)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"課程名稱：{course.get('courseName', '')}\n")
        f.write(f"課程簡介：{course.get('courseDesc', '')}\n")
        f.write(f"偵測語言：{language}\n\n")
        if stats:
            awpm = sum(s.get("wpm", 0) for s in stats) / len(stats)
            f.write(f"【語音分析】平均語速：{awpm:.0f}字/分\n\n")
        f.write(f"【逐字稿節錄】\n{combined[:truncate]}")
    return len(combined)


def parse_args() -> argparse.Namespace:
    cfg = mc.load_pipeline()
    ap = argparse.ArgumentParser(
        description="前處理課程影音為模型輸入（不呼叫 LLM、不耗額度）")
    ap.add_argument("--queue", default=cfg["files"]["queue"],
                    help="課程佇列 JSON（含 videoIds / mp4Urls / chapters）")
    ap.add_argument("--merged-dir", default=cfg["files"]["merged_dir"],
                    help="輸出目錄（合併截圖與逐字稿）")
    ap.add_argument("--videos-dir", default=cfg["files"]["videos_dir"],
                    help="影片下載目錄")
    ap.add_argument("--audio-dir", default=cfg["files"]["audio_dir"],
                    help="音訊片段目錄")
    ap.add_argument("--limit", type=int, default=None,
                    help="本次最多處理幾門課（分批執行用）")
    ap.add_argument("--only", default=None,
                    help="只處理課名包含此字串的課程")
    ap.add_argument("--force", action="store_true",
                    help="已有輸出也重新處理")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cfg = mc.load_pipeline()
    truncate = cfg["transcript"]["truncate_chars"]

    queue_path = Path(args.queue)
    if not queue_path.exists():
        print(f"[錯誤] 找不到課程佇列 {queue_path}\n"
              f"       請先執行：python fetch_courses.py", file=sys.stderr)
        sys.exit(1)

    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if args.only:
        queue = [c for c in queue if args.only in (c.get("courseName") or "")]
        if not queue:
            print(f"[錯誤] 沒有課名包含「{args.only}」的課程", file=sys.stderr)
            sys.exit(1)

    vid_dir = Path(args.videos_dir)
    aud_dir = Path(args.audio_dir)
    out_dir = Path(args.merged_dir)
    for d in (vid_dir, aud_dir, out_dir):
        d.mkdir(parents=True, exist_ok=True)

    print(f"課程佇列：{queue_path}（{len(queue)} 門）")
    print(f"輸出目錄：{out_dir}")
    print(f"抽幀：每支 {mf.FRAMES_PER_VIDEO} 幀 | "
          f"音訊：{mf.AUDIO_SEGMENTS} 段 × {mf.AUDIO_SEGMENT_MINUTES} 分鐘 | "
          f"逐字稿截斷：{truncate} 字")
    print(f"載入 Whisper {mf.WHISPER_MODEL} 模型（本機執行，不耗 API 額度）...")

    import whisper
    wm = whisper.load_model(mf.WHISPER_MODEL)
    print("開始批次前處理\n")

    processed = skipped = failed = 0
    t0 = time.time()

    for idx, course in enumerate(queue, 1):
        name = course.get("courseName") or f"course_{course.get('courseId')}"
        safe = safe_name(name) or str(course.get("courseId"))
        img = out_dir / f"{safe}.jpg"
        txt = out_dir / f"{safe}_analysis.txt"

        if img.exists() and txt.exists() and not args.force:
            skipped += 1
            continue

        if args.limit and processed >= args.limit:
            print(f"\n已達本次上限 {args.limit} 門，停止（可直接重跑續做）")
            break

        print(f"[{idx}/{len(queue)}] {name[:34]}")
        selected = mf.select_videos_smart(course)
        if not selected:
            print("  無影片，跳過")
            failed += 1
            continue
        print(f"  抽樣 {len(selected)} 支影片（全部 "
              f"{len(course.get('videoIds', [])) + len(course.get('mp4Urls', []))} 支，"
              f"章節 {len(course.get('chapters', []))}）")

        frames: list = []
        transcripts: list[str] = []
        stats: list[dict] = []
        langs: list[str] = []

        for vid in selected:
            vpath = mf.download_video(vid, vid_dir)
            if not vpath:
                continue
            for apath in mf.extract_audio_segments(
                    vpath, aud_dir,
                    mf.AUDIO_SEGMENT_MINUTES, mf.AUDIO_SEGMENTS, mf.SKIP_SECONDS):
                tx, st, lg = mf.transcribe_audio(apath, wm)
                if tx:
                    transcripts.append(tx)
                if st:
                    stats.append(st)
                if lg:
                    langs.append(lg)
            frames.extend(mf.extract_frames(vpath, mf.FRAMES_PER_VIDEO, mf.SKIP_SECONDS))

        if not frames:
            print("  無截圖，跳過")
            failed += 1
            continue

        mf.merge_frames(frames, img, cols=mf.MERGE_COLS)
        language = max(set(langs), key=langs.count) if langs else "zh"
        chars = write_analysis_text(txt, course, transcripts, stats, language, truncate)
        processed += 1
        print(f"  完成：截圖 {len(frames)} 幀、逐字稿 {chars} 字、語言 {language}")

    mins = (time.time() - t0) / 60
    print(f"\n前處理完成：本次處理 {processed} 門、已存在跳過 {skipped} 門、無法處理 {failed} 門")
    print(f"耗時 {mins:.1f} 分鐘")
    print(f"下一步：python rate_courses.py")


if __name__ == "__main__":
    main()
