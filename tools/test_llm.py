# -*- coding: utf-8 -*-
"""
tools/test_llm.py
-----------------
連線自我檢查：在跑整批分析前，先確認金鑰、模型、規準與輸出格式都通。

檢查項目：
  1. 設定檔是否載得到（prompt.txt / fields.json / pipeline.json）
  2. 金鑰是否存在、是否可呼叫到模型
  3. 模型回應是否通得過 config/fields.json 的驗證器
  4. 多次呼叫的整合規則是否正常運作

只花一門課的額度（預設 1 次呼叫），不寫入任何結果檔。

使用方式:
    python tools/test_llm.py                      # 只檢查設定，不呼叫模型
    python tools/test_llm.py --live               # 實際呼叫一次模型
    python tools/test_llm.py --live --agents 3    # 呼叫三次並測整合
    python tools/test_llm.py --live --course "機器學習基礎理論與實作"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import moocs_config as mc

OK, BAD, WARN = "[OK]  ", "[FAIL]", "[WARN]"


def check_config() -> bool:
    print("── 設定檔 ──")
    ok = True
    try:
        prompt = mc.load_prompt()
        print(f"{OK} prompt.txt　　{len(prompt)} 字　（{mc.config_dir() / 'prompt.txt'}）")
    except SystemExit as e:
        print(f"{BAD} prompt.txt　　{e}")
        return False
    try:
        fields = mc.load_fields()["fields"]
        req = [f["name"] for f in fields if f.get("required")]
        print(f"{OK} fields.json　 {len(fields)} 欄（必填 {len(req)} 欄）")
        print(f"       數值欄位：{'、'.join(mc.score_fields())}")
        print(f"       判斷原因：{len(mc.reason_keys())} 項")
    except (SystemExit, KeyError) as e:
        print(f"{BAD} fields.json　 {e}")
        ok = False
    try:
        cfg = mc.load_pipeline()
        print(f"{OK} pipeline.json 模型 {cfg['llm']['model_priority']}　"
              f"呼叫 {cfg['llm']['num_agents']} 次　"
              f"上限 ${cfg['llm']['cost_cap_usd']}")
    except (SystemExit, KeyError) as e:
        print(f"{BAD} pipeline.json {e}")
        ok = False
    return ok


def check_key() -> bool:
    print("\n── 金鑰 ──")
    raw = os.getenv("ANTHROPIC_API_KEY", "")
    if not raw:
        print(f"{BAD} 找不到環境變數 ANTHROPIC_API_KEY")
        print("       Windows:  set ANTHROPIC_API_KEY=sk-ant-...")
        print("       或複製 .env.example 成 .env 後填入")
        return False
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    print(f"{OK} 找到 {len(keys)} 把金鑰（結尾 "
          f"{'、'.join('…' + k[-4:] for k in keys)}）")
    return True


def check_merge() -> bool:
    """用假資料驗整合規則，不呼叫模型。"""
    print("\n── 整合規則（不呼叫模型）──")
    base = {
        "能力標籤": ["C"], "難易度": "進階", "課程節奏": "精煉流暢",
        "通識素養型": False, "跨域應用型": False, "技術實作型": True,
        "趣味度": 3, "內容深度": 4, "實作程度": 4,
        "結構清晰度": 4, "應用廣度": 3, "推薦度": 4,
        "關鍵字": ["機器學習"], "細項代碼": ["C-2-IV-1"],
        "先備知識": ["Python"], "對應職務角色": ["資料工程師"],
        "對應職場技能": ["模型訓練"],
        "課程摘要": "這是一門測試用的課程摘要，長度需超過十個字才會被採用。",
        "判斷原因": {k: f"{k}的測試理由文字內容" for k in mc.reason_keys()},
    }
    a2 = dict(base, 推薦度=2)                      # 觸發「分歧大」
    a3 = dict(base, 難易度="基礎")                  # 多數決仍為進階
    merged, diverge = mc.merge_results([base, a2, a3])
    if not merged:
        print(f"{BAD} 整合失敗")
        return False
    print(f"{OK} 整合 3 筆 → {len(merged)} 欄位")
    print(f"       推薦度 4/2/4 → 平均取整 {merged['推薦度']}")
    print(f"       難易度 進階/進階/基礎 → 多數決 {merged['難易度']}")
    print(f"       分歧標記：{diverge or '（無）'}")

    bad = dict(base, 推薦度=99)
    if mc.validate_result(json.dumps(bad, ensure_ascii=False), verbose=False) is None:
        print(f"{OK} 驗證器正確攔下超出範圍的分數")
    else:
        print(f"{BAD} 驗證器未攔下超出範圍的分數")
        return False
    return True


def find_sample(merged_dir: Path, want: str | None):
    """找一份已備妥的前處理輸入當測試素材。"""
    if not merged_dir.is_dir():
        return None, None
    for txt in sorted(merged_dir.glob("*_analysis.txt")):
        img = txt.with_name(txt.name.replace("_analysis.txt", ".jpg"))
        if not img.exists():
            continue
        if want and want not in txt.stem:
            continue
        return img, txt
    return None, None


def check_live(agents: int, course: str | None, merged_dir: Path) -> bool:
    print(f"\n── 實際呼叫模型（{agents} 次）──")
    import moocs_core as mf

    img, txt = find_sample(merged_dir, course)
    if not img:
        print(f"{WARN} {merged_dir}/ 下沒有可用的前處理輸入"
              f"（需成對的 *.jpg 與 *_analysis.txt）")
        print("       請先執行：python preprocess_courses.py --limit 1")
        return False

    mf.NUM_AGENTS = agents
    print(f"       素材：{txt.name}")
    print(f"       模型：{mf.MODEL_PRIORITY[0]}")

    text = txt.read_text(encoding="utf-8")
    results = asyncio.run(mf.run_three_agents(str(img), text))
    merged, diverge = mf.merge_agent_results(results)

    if not merged:
        print(f"{BAD} 所有呼叫均失敗或回應格式不合")
        return False

    print(f"{OK} 取得並整合結果，估算花費 ${mf.estimated_cost():.4f}")
    for k in ("能力標籤", "難易度", "課程節奏", "推薦度", "內容深度", "實作程度"):
        print(f"       {k}：{merged.get(k)}")
    print(f"       分歧標記：{diverge or '（無）'}")
    print(f"       課程摘要：{(merged.get('課程摘要') or '')[:40]}…")
    return True


def main() -> None:
    cfg_files = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description="LLM 連線與設定自我檢查")
    ap.add_argument("--live", action="store_true", help="實際呼叫模型（會耗少量額度）")
    ap.add_argument("--agents", type=int, default=1, help="呼叫次數")
    ap.add_argument("--course", default=None, help="指定測試用課程（課名片段）")
    ap.add_argument("--merged-dir", default=None, help="前處理輸出目錄")
    args = ap.parse_args()

    merged_dir = Path(args.merged_dir) if args.merged_dir else \
        cfg_files / mc.load_pipeline()["files"]["merged_dir"]

    results = [("設定檔", check_config()), ("整合規則", check_merge())]
    if args.live:
        results.append(("金鑰", check_key()))
        if results[-1][1]:
            results.append(("模型呼叫", check_live(args.agents, args.course, merged_dir)))
    else:
        print("\n（未加 --live，未呼叫模型。加上 --live 可做端對端測試。）")

    print("\n── 總結 ──")
    for name, ok in results:
        print(f"{OK if ok else BAD} {name}")
    if all(ok for _, ok in results):
        print("\n全部通過，可以開始跑整批分析。")
    else:
        print("\n有項目未通過，請依上面的提示處理後再跑整批分析。")
        sys.exit(1)


if __name__ == "__main__":
    main()
