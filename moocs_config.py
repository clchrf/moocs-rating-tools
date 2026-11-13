# -*- coding: utf-8 -*-
"""
moocs_config.py
---------------
設定載入層：把評分規準、欄位定義與管線參數從程式碼抽出成外部設定檔。

舊版（moocs_final.py）把 SYSTEM_PROMPT 與 19 個欄位名稱寫死在三個地方
（規準、merge_agent_results、parse_and_validate_json），換一個領域必須同步改三處。
本模組改為：

    config/prompt.txt     評分規準（LLM system prompt）
    config/fields.json    19 欄位的型別、整合規則、驗證規則
    config/pipeline.json  抽樣／抽幀／音訊／模型／分級門檻

整合與驗證函式改由 fields.json 驅動（見 merge_results / validate_result），
換領域時只需改設定檔，不需改程式碼。

設定檔尋找順序：
    1. 環境變數 MOOCS_CONFIG_DIR
    2. 執行檔／腳本所在目錄下的 config/
    3. 當前工作目錄下的 config/
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

__all__ = [
    "config_dir", "load_prompt", "load_fields", "load_pipeline",
    "merge_results", "validate_result", "score_fields", "reason_keys",
]


# ==========================================
# 設定檔位置
# ==========================================
def _base_dir() -> Path:
    """腳本或 PyInstaller 執行檔所在目錄。

    打包成 exe 後設定檔放在 exe 旁邊（而非塞進 exe 內），使用者才能自行編修。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_dir() -> Path:
    """回傳設定檔目錄；找不到時回傳預設位置（由呼叫端報錯）。"""
    env = os.getenv("MOOCS_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()

    for cand in (_base_dir() / "config", Path.cwd() / "config"):
        if cand.is_dir():
            return cand

    return _base_dir() / "config"


def _read_text(name: str) -> str:
    path = config_dir() / name
    if not path.exists():
        raise SystemExit(
            f"[錯誤] 找不到設定檔 {path}\n"
            f"       請確認 config/ 目錄與執行檔（或腳本）在同一層，"
            f"或設定環境變數 MOOCS_CONFIG_DIR。"
        )
    return path.read_text(encoding="utf-8")


def _strip_comments(obj):
    """移除設定檔中以 _ 開頭的註解鍵，讓 JSON 可以寫說明又不影響程式。"""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


_cache: dict[str, object] = {}


def load_prompt() -> str:
    """評分規準（LLM system prompt）。"""
    if "prompt" not in _cache:
        _cache["prompt"] = _read_text("prompt.txt")
    return _cache["prompt"]


def load_fields() -> dict:
    """欄位定義與整合／驗證規則。"""
    if "fields" not in _cache:
        _cache["fields"] = _strip_comments(json.loads(_read_text("fields.json")))
    return _cache["fields"]


def load_pipeline() -> dict:
    """管線參數。"""
    if "pipeline" not in _cache:
        _cache["pipeline"] = _strip_comments(json.loads(_read_text("pipeline.json")))
    return _cache["pipeline"]


def _specs() -> list[dict]:
    return load_fields()["fields"]


def _spec(name: str) -> dict:
    for s in _specs():
        if s["name"] == name:
            return s
    raise KeyError(name)


def score_fields() -> list[str]:
    """數值型欄位名稱（五大面向＋推薦度）。"""
    return [s["name"] for s in _specs() if s["type"] == "number"]


def reason_keys() -> list[str]:
    """判斷原因的子項名稱。"""
    return list(_spec("判斷原因").get("keys", []))


# ==========================================
# 單次結果驗證（取代 parse_and_validate_json 的規則部分）
# ==========================================
def _normalize_list(values, spec):
    """依 normalize 規則整理列表值。"""
    out = []
    for v in values or []:
        if not isinstance(v, str):
            continue
        item = v.strip()
        if spec.get("normalize") == "leading_letter":
            # 寬容處理：「C-2-IV-1」這類代碼抽出開頭字母
            item = item[:1].upper()
        allowed = spec.get("allowed")
        if allowed and item not in allowed:
            continue
        if item and item not in out:
            out.append(item)
    return out


def validate_result(raw_text: str, verbose: bool = True):
    """解析單一次模型回應並依 fields.json 驗證。格式不合回傳 None（整筆作廢重跑）。"""
    def warn(msg):
        if verbose:
            print(f"  ⚠️ {msg}")

    try:
        text = re.sub(r"```json\n?", "", raw_text or "").replace("```", "").strip()
        result = json.loads(text)
    except json.JSONDecodeError as e:
        if verbose:
            print(f"  ❌ JSON 解析失敗：{e}")
        return None

    if not isinstance(result, dict):
        warn("回應不是 JSON 物件")
        return None

    # 必填欄位
    missing = [s["name"] for s in _specs() if s.get("required") and s["name"] not in result]
    if missing:
        warn(f"缺少欄位：{missing}")
        return None

    lo, hi = load_fields().get("score_range", [1, 5])

    for spec in _specs():
        name, ftype = spec["name"], spec["type"]
        if name not in result:
            continue
        val = result[name]

        if ftype == "number":
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not lo <= val <= hi:
                warn(f"{name} 應為 {lo}-{hi} 的數值，得到 {val!r}")
                return None

        elif ftype == "choice":
            if val not in spec["allowed"]:
                warn(f"{name} 應為 {spec['allowed']} 之一，得到 {val!r}")
                return None

        elif ftype == "list":
            norm = _normalize_list(val, spec)
            if spec.get("required") and not norm:
                warn(f"{name} 格式錯誤（無法解析）：{val!r}")
                return None
            result[name] = norm

        elif ftype == "bool":
            result[name] = bool(val)

    return result


# ==========================================
# 多次結果整合（取代 merge_agent_results 的規則部分）
# ==========================================
def _merge_mean(values, lo=1, hi=5):
    """平均後取整（與舊版 merge_numeric 相同：round 到整數，不保留小數）。"""
    valid = [v for v in values
             if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi]
    if not valid:
        return None, "⚠️無有效值"
    avg = round(sum(valid) / len(valid))
    return avg, "⚠️分歧大" if max(valid) - min(valid) >= 2 else ""


def _merge_majority(values, allowed):
    """多數決；無任一選項取得 2 票以上即標記分歧（與舊版 merge_choice 相同）。"""
    valid = [v for v in values if v in allowed]
    if not valid:
        return None, "⚠️無有效值"
    top, n = Counter(valid).most_common(1)[0]
    return top, "" if n >= 2 else "⚠️三者分歧"


def _merge_vote(lists, min_votes):
    """票數門檻。票數 = 提到該項目的呼叫次數（同一次內重複只算一票）。

    註：舊版 merge_list 未在單次內去重，同一次列兩遍會自己湊到 2 票。
    此處改為每次只算一票，票數語意才正確；差異見 README「與舊版的差異」。
    """
    counts: Counter = Counter()
    order: list[str] = []
    for lst in lists:
        for item in dict.fromkeys(lst or []):
            counts[item] += 1
            if item not in order:
                order.append(item)
    return [i for i in order if counts[i] >= min_votes]


def _to_text(t):
    """把 dict（逐面向原因）攤平成可讀字串，其他型別轉字串。"""
    if t is None:
        return ""
    if isinstance(t, dict):
        return "\n".join(f"{k}：{_to_text(v)}" for k, v in t.items())
    if isinstance(t, (list, tuple)):
        return "；".join(_to_text(x) for x in t)
    return str(t)


def _merge_longest(texts, min_len=10):
    """取最長的一份文字；過短（≤min_len 字）視為無效，與舊版 merge_text 相同。"""
    valid = [s for s in (_to_text(t) for t in texts) if s and len(s) > min_len]
    return max(valid, key=len) if valid else ""


def merge_results(parsed: list[dict]):
    """把多次獨立呼叫的結果依 fields.json 合併，回傳 (merged, diverge_fields)。

    純 Python 規則（平均／多數決／票數門檻／任一即真／取最長），**不呼叫模型**。
    """
    if not parsed:
        return None, []
    if len(parsed) == 1:
        return parsed[0], ["⚠️僅一次呼叫成功"]

    n = len(parsed)
    lo, hi = load_fields().get("score_range", [1, 5])
    merged: dict = {}
    diverge: list[str] = []

    for spec in _specs():
        name, ftype = spec["name"], spec["type"]
        vals = [r.get(name) for r in parsed]

        if ftype == "number":
            merged[name], flag = _merge_mean(vals, lo, hi)
            if flag:
                diverge.append(f"{name}{flag}")

        elif ftype == "choice":
            merged[name], flag = _merge_majority(vals, spec["allowed"])
            if flag:
                diverge.append(f"{name}{flag}")

        elif ftype == "bool":
            merged[name] = any(bool(v) for v in vals)

        elif ftype == "list":
            min_votes = spec.get("min_votes", 1)
            if n < 3 and "min_votes_when_few_agents" in spec:
                min_votes = spec["min_votes_when_few_agents"]
            out = _merge_vote(vals, min_votes)
            if not out and spec.get("fallback_first_agent"):
                out = _normalize_list(parsed[0].get(name, []), spec)
            merged[name] = out

        elif ftype == "text":
            merged[name] = _merge_longest(vals)

        elif ftype == "dict":
            merged[name] = {
                k: _merge_longest([r.get(name, {}).get(k) for r in parsed])
                for k in spec.get("keys", [])
            }

    # 衍生規則（例：實作程度≥3 必為技術實作型）
    _OPS = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
            "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
            "==": lambda a, b: a == b}
    for rule in load_fields().get("derived_rules", []):
        cond, then = rule["if"], rule["then"]
        cur = merged.get(cond["field"])
        op = _OPS.get(cond["op"])
        if op and isinstance(cur, (int, float)) and not isinstance(cur, bool):
            if op(cur, cond["value"]):
                merged[then["field"]] = then["value"]

    return merged, diverge
