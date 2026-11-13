# -*- coding: utf-8 -*-
"""
compare_students.py
-------------------
④ AI 評分 vs 真人標註 交叉比對（純統計，不呼叫任何 API）。

每門課由不同同學獨立填同一組欄位，與 AI 的結果逐欄比對一致率，
並以「同學兩人之間的一致率」作為對照基準（偶然一致的參考線）。
差距大的課程列入「待主持人裁決」。


★ 規則(老師指示):
  - 本程式對 AI 端(analysis_results.json)與份1 三份交付物一律「唯讀」,絕不覆寫。
  - 比對結果只寫到全新檔案:AI_vs_同學_比對報告.xlsx(不與份1 任何檔案混合)。
  - 原本的 compare_ai_vs_students.py 保留不動(老師要看),本檔是另寫的 v2。
  - 純統計,完全不呼叫任何 API。

★ 對應關係:用 courseId 當主鍵。
  - AI 端 59 門皆可解析出唯一 courseId(7 門同名課自帶 courseId,其餘 52 門用
    moocs_queue.json 的「課名→courseId」對應)——已驗證 59/59 全部對到。
  - 同學表最左欄即 courseId,直接對。

★ 可比對欄位(兩邊都有):
  - 類別:能力標籤 A/B/C/D(逐標籤二元 Kappa)、難易度、課程節奏、
          修課對象 通識素養型/跨域應用型/技術實作型(布林 Kappa)
  - 數值:趣味度、推薦度、內容深度、實作程度、結構清晰度、應用廣度(六面向,ICC(2,1)+Gwet AC2)
  - 同學表單自 2026-07 起六面向全評;仍有 5 門課只完成部分面向(第二輪或第一輪缺新增4面向),
    本次先排除這 5 門(PENDING_CIDS),待補齊後再納入重跑。

★ 統計方法(對應計畫書/委員回覆):
  - 類別變項 → Cohen's Kappa;數值變項 → ICC(2,1);門檻 0.70。
  - 差距≥2 或 類別標籤不同 → 列入「待主持人裁決」。

用法:
  python compare_students.py --inspect                 # 只印出同學表偵測到的欄位（校正欄位對應）
  python compare_students.py                           # 跑比對 + 產出報告
  python compare_students.py --students 檔名.xlsx       # 指定同學標註表路徑
  python compare_students.py --official 課程清單.xlsx   # 指定官方課程清單（補課名用）
"""
import sys, json, argparse, glob, datetime
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
import numpy as np
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ---------- 路徑/常數 ----------
# 以下路徑可由命令列參數覆寫（見 main()）
AI_RESULTS   = 'analysis_results.json'
QUEUE_JSON   = 'moocs_queue.json'
OFFICIAL_XLSX= 'data/官方課程清單.xlsx'        # 僅用於補空白課名,缺檔不影響比對
OUT_XLSX     = 'AI_vs_同學_比對報告.xlsx'     # 全新獨立檔,對 AI 端唯讀

NUMERIC   = ['趣味度', '推薦度', '內容深度', '實作程度', '結構清晰度', '應用廣度']   # 同學表單六面向皆評(2026-07起)
CAT_SINGLE= ['難易度', '課程節奏']
# 尚未完成六面向完整評分的課程 → 先排除,待補齊後重跑。2026-07-11:59/59 全部完成,故留空。
PENDING_CIDS = set()
CAP_LABELS= ['A', 'B', 'C', 'D']
# 課程節奏為「序位」三級:精煉流暢(最好) → 需開倍速(還可接受,開倍速即可) → 拖泥帶水(最差,開倍速仍超慢)。
# 相鄰一級(如 流暢↔需開倍速)視為可接受;僅差兩級(流暢↔拖泥帶水)才算真分歧。
PACE_RANK = {'精煉流暢': 0, '需開倍速': 1, '拖泥帶水': 2}
AUD_FLAGS = ['通識素養型', '跨域應用型', '技術實作型']
KAPPA_THRESHOLD = 0.70
ICC_THRESHOLD   = 0.70

NAVY='1F3864'; BLUE='2E6DB4'; GREEN='C6EFCE'; YELLOW='FFEB9C'; RED='FFC7CE'

# ====================================================================
# 統計核心(格式無關,已就緒)
# ====================================================================
def cohen_kappa(a, b):
    """兩位評分者的 Cohen's Kappa(類別)。a、b 等長標籤序列。"""
    a, b = list(a), list(b)
    n = len(a)
    if n == 0:
        return None
    cats = sorted(set(a) | set(b))
    idx = {c: i for i, c in enumerate(cats)}
    m = np.zeros((len(cats), len(cats)))
    for x, y in zip(a, b):
        m[idx[x], idx[y]] += 1
    po = np.trace(m) / n
    pe = sum((m[i, :].sum() / n) * (m[:, i].sum() / n) for i in range(len(cats)))
    if pe == 1:
        return 1.0
    return (po - pe) / (1 - pe)

def icc_2_1(ratings):
    """ICC(2,1):雙向隨機、單一評分者一致性。ratings: 每列一門課的各評分者分數。"""
    rows = [r for r in ratings if len(r) == len(ratings[0])]
    if len(rows) < 2:
        return None
    M = np.array(rows, dtype=float)
    n, k = M.shape
    if n < 2 or k < 2:
        return None
    grand = M.mean()
    row_means = M.mean(axis=1)
    col_means = M.mean(axis=0)
    SST = ((M - grand) ** 2).sum()
    SSR = k * ((row_means - grand) ** 2).sum()
    SSC = n * ((col_means - grand) ** 2).sum()
    SSE = SST - SSR - SSC
    MSR = SSR / (n - 1)
    MSC = SSC / (k - 1)
    MSE = SSE / ((n - 1) * (k - 1))
    denom = MSR + (k - 1) * MSE + k * (MSC - MSE) / n
    if denom == 0:
        return None
    return (MSR - MSE) / denom

def agree_pct(a, b):
    """逐項完全相同的比率(%);類別/二元用。"""
    if not a: return None
    return 100.0 * sum(1 for x, y in zip(a, b) if x == y) / len(a)

def gwet_ac2(a, b, span, kind='quad'):
    """Gwet's AC2:加權版 AC1,用於「順序量表」(課程節奏、趣味度/推薦度等 Likert)。
    既解偏態(同 AC1),又用加權矩陣給「差一級」高分。span=全量表級距(1-5→4、節奏→2)。
    a、b 為等長數值序列(整數/序位);kind='quad' 二次加權(差1級失分極小)、'linear' 線性。"""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    n = len(pairs)
    if n == 0: return None
    cats = sorted(set([x for x, _ in pairs]) | set([y for _, y in pairs]))
    q = len(cats)
    if q == 1: return 1.0
    idx = {c: i for i, c in enumerate(cats)}
    P = np.zeros((q, q))
    for x, y in pairs: P[idx[x], idx[y]] += 1
    P /= n
    W = np.zeros((q, q))
    for i, ci in enumerate(cats):
        for j, cj in enumerate(cats):
            d = abs(ci - cj) / span
            W[i, j] = 1 - d * d if kind == 'quad' else 1 - d
    pa = (W * P).sum()
    pi = (P.sum(axis=1) + P.sum(axis=0)) / 2
    Tw = W.sum()
    pe = (Tw / (q * (q - 1))) * sum(p * (1 - p) for p in pi)
    return (pa - pe) / (1 - pe) if pe < 1 else 1.0

def gwet_ac1(a, b):
    """Gwet's AC1:對「高一致但偏態」穩健的一致性係數(解 Kappa 悖論)。
    pe = (1/(q−1)) Σ πk(1−πk),πk=該類在兩評分者的平均比例。範圍 −1~1。"""
    a, b = list(a), list(b); n = len(a)
    if n == 0: return None
    cats = sorted(set(a) | set(b)); q = len(cats)
    if q == 1: return 1.0
    pa = sum(1 for x, y in zip(a, b) if x == y) / n
    pe = sum(((a.count(k) + b.count(k)) / (2 * n)) * (1 - (a.count(k) + b.count(k)) / (2 * n))
             for k in cats) / (q - 1)
    if pe >= 1: return 1.0
    return (pa - pe) / (1 - pe)

def weighted_kappa(a, b, rank, kind='linear'):
    """序位變項的加權 Kappa(相鄰級距罰較輕)。rank: {類別:序位}。"""
    a, b = list(a), list(b); n = len(a)
    if n == 0: return None
    cats = sorted(set(a) | set(b), key=lambda c: rank.get(str(c), 99))
    k = len(cats)
    if k == 1: return 1.0
    idx = {c: i for i, c in enumerate(cats)}
    rv = [rank.get(str(c), 99) for c in cats]
    span = (max(rank.values()) - min(rank.values())) or 1   # 全量表級距(節奏=2)
    O = np.zeros((k, k))
    for x, y in zip(a, b): O[idx[x], idx[y]] += 1
    O /= n
    E = np.outer(O.sum(axis=1), O.sum(axis=0))
    W = np.zeros((k, k))
    for i in range(k):
        for j in range(k):
            d = abs(rv[i] - rv[j]) / span
            W[i, j] = d if kind == 'linear' else d * d
    denom = (W * E).sum()
    if denom == 0: return 1.0
    return 1 - (W * O).sum() / denom

def pace_accept_pct(ai_seq, st_seq, rank=PACE_RANK):
    """節奏「可接受率」=序位差≤1(相鄰)即視為一致的比率。"""
    pairs = [(x, y) for x, y in zip(ai_seq, st_seq) if str(x) in rank and str(y) in rank]
    if not pairs: return None
    return 100.0 * sum(1 for x, y in pairs if abs(rank[str(x)] - rank[str(y)]) <= 1) / len(pairs)

def interpret(v):
    if v is None: return '無法計算'
    if v >= 0.80: return '幾乎完全一致'
    if v >= 0.70: return '達標(實質一致)'
    if v >= 0.60: return '中等,未達門檻'
    if v >= 0.40: return '尚可,偏低'
    return '一致性差'

# ====================================================================
# 載入 AI 端(唯讀;courseId 對應,沿用份1 build 腳本邏輯)
# ====================================================================
def load_ai_by_cid():
    """回傳 {courseId: result dict}。59/59 已驗證可對到 courseId。"""
    name2id = {}
    try:
        queue = json.load(open(QUEUE_JSON, encoding='utf-8'))
        name2id = {c['courseName']: str(c.get('courseId', '')) for c in queue if c.get('courseName')}
    except FileNotFoundError:
        print(f'⚠ 找不到 {QUEUE_JSON};52 門非同名課將無法用課名對 courseId。')
    data = json.load(open(AI_RESULTS, encoding='utf-8'))
    out, unresolved = {}, []
    for x in data:
        if not x.get('result'):
            continue
        cid = str(x['courseId']) if x.get('courseId') else name2id.get(x['name'], '')
        if cid:
            out[cid] = {'name': x['name'], 'result': x['result']}
        else:
            unresolved.append(x['name'])
    if unresolved:
        print(f'⚠ {len(unresolved)} 門 AI 課程無法解析 courseId:{unresolved}')
    return out

def official_names():
    """官方 xlsx 的 courseId→課名(顯示用,唯讀)。"""
    return {k: v['name'] for k, v in official_meta().items()}

def official_meta():
    """官方 xlsx 的 courseId→{name,org}(顯示用,唯讀)。"""
    meta = {}
    try:
        if not Path(OFFICIAL_XLSX).exists():
            print(f'提示:找不到官方課程清單 {OFFICIAL_XLSX},空白課名將顯示為(未命名)。'
                  ' 可用 --official 指定路徑。')
            return {}
        wb = openpyxl.load_workbook(OFFICIAL_XLSX, data_only=True)
        ws = wb['20260512']
        for r in range(3, ws.max_row + 1):
            cid = ws.cell(r, 1).value
            if cid and str(cid).replace('.0', '').isdigit():
                k = str(cid).replace('.0', '')
                meta[k] = {'name': ws.cell(r, 2).value or '', 'org': ws.cell(r, 11).value or ''}
    except Exception as e:
        print(f'⚠ 官方 xlsx 讀取失敗(僅影響顯示名稱):{e}')
    return meta

# ====================================================================
# 載入同學標註 xlsx(標題關鍵字驅動;對到真檔時於 FIELD_KEYWORDS 微調)
# ====================================================================
# ★ 固定欄位佈局(Google 表單連動模板,穩定):分頁「能力分類標籤表」,表頭第 4 列,資料第 5 列起。
#   結構關鍵:能力標籤A/B/C/D、難易度、修課對象 = 兩位同學各自獨立標註後的「整合(共識)結果」(表單只存整合後一份);
#             趣味度/推薦度/課程節奏 = 第一次(P/Q/R)、第二次(W/X/Y)各一份(逐輪保留)。
SHEET_NAME  = '能力分類標籤表'
HEADER_ROW  = 4
DATA_START  = 5
COL = {
    'courseId': 1,  '課名': 2,  '影片時數': 3,  '開課單位': 4,
    'A': 5, 'B': 6, 'C': 7, 'D': 8,            # 能力標籤(單一)
    '難易度': 9,
    '通識素養型': 10, '跨域應用型': 11, '技術實作型': 12,   # 修課對象(單一)
    '第一次負責同學': 13, '第一次完成狀態': 14, '第一次完成日期': 15,
    '趣味度_1': 16, '推薦度_1': 17, '內容深度_1': 18, '實作程度_1': 19,
    '結構清晰度_1': 20, '應用廣度_1': 21, '課程節奏_1': 22, '細項_1': 23,
    '第二次負責同學': 24, '第二次完成狀態': 25, '第二次完成日期': 26,
    '趣味度_2': 27, '推薦度_2': 28, '內容深度_2': 29, '實作程度_2': 30,
    '結構清晰度_2': 31, '應用廣度_2': 32, '課程節奏_2': 33, '細項_2': 34,
}

def _to_num(v):
    try:
        f = float(v)
        return f if f == f else None   # 過濾 nan
    except (TypeError, ValueError):
        return None

def _checked(v):
    """核取方塊/布林欄:True/有意義文字 → True;False/空/未評/否 → False。"""
    if v in (None, '', 0, '0', False): return False
    if v is True: return True
    s = str(v).strip().lower()
    return s not in ('否', 'false', 'no', '未', '未評', 'x', '0')

def _cat(v):
    return str(v).strip() if v not in (None, '', '未評') and str(v).strip() not in ('未評', '') else None

def _get_sheet(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    return wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb[wb.sheetnames[-1]]

def load_students_xlsx(path, inspect=False):
    """讀同學標註 xlsx → {courseId: course_dict}。
    course_dict = {
        'shared': {能力標籤[], 難易度, 通識素養型, 跨域應用型, 技術實作型, 負責同學, 完成狀態},  # 兩位同學整合標註
        'raters': [ {趣味度,推薦度,課程節奏,負責同學,完成狀態,輪次}, ... ]  # 第一/二輪各一(僅填過的)
    }
    inspect=True 時只印表頭與一筆樣本。"""
    ws = _get_sheet(path)
    if inspect:
        print(f'== 工作表:{ws.title} | 維度 {ws.max_row}x{ws.max_column} ==\n-- 第 {HEADER_ROW} 列表頭 --')
        for name, col in COL.items():
            h = ws.cell(HEADER_ROW, col).value
            print(f'  {get_column_letter(col):>2}({col:>2}) {name:12s}: {repr(h)[:40]}')
        print(f'\n-- 第 {DATA_START} 列樣本 --')
        for name, col in COL.items():
            print(f'  {name:12s}: {repr(ws.cell(DATA_START, col).value)[:50]}')
        return None

    def g(r, key):
        return ws.cell(r, COL[key]).value

    def build_pass(r, p):
        suf = '_1' if p == 1 else '_2'
        rater = {f: _to_num(g(r, f + suf)) for f in NUMERIC}
        rater['課程節奏'] = _cat(g(r, '課程節奏' + suf))
        rater['負責同學'] = g(r, f'第{"一" if p==1 else "二"}次負責同學')
        rater['完成狀態'] = g(r, f'第{"一" if p==1 else "二"}次完成狀態')
        rater['輪次'] = p
        return rater

    def pass_filled(rt):
        if any(rt[f] is not None for f in NUMERIC) or rt['課程節奏']: return True
        return str(rt.get('完成狀態') or '').strip() == '已完成'

    out = {}
    for r in range(DATA_START, ws.max_row + 1):
        cidv = g(r, 'courseId')
        if cidv is None: continue
        cid = str(cidv).replace('.0', '')
        if not (cid.isdigit() and len(cid) == 8): continue
        shared = {
            '能力標籤': [lab for lab in CAP_LABELS if _checked(g(r, lab))],
            '難易度':   _cat(g(r, '難易度')),
            '負責同學': g(r, '第一次負責同學'),
            '完成狀態': g(r, '第一次完成狀態'),
        }
        for flag in AUD_FLAGS:
            shared[flag] = _checked(g(r, flag))
        raters = [build_pass(r, p) for p in (1, 2)]
        raters = [rt for rt in raters if pass_filled(rt)]
        out[cid] = {'shared': shared, 'raters': raters}
    return out

def shared_filled(course):
    """整合標註是否有內容(能力標籤/難易度/任一修課對象/第一次已完成)。"""
    s = course['shared']
    if s['能力標籤'] or s['難易度'] or any(s[f] for f in AUD_FLAGS): return True
    return str(s.get('完成狀態') or '').strip() == '已完成'

# ====================================================================
# 比對 + 產出新 Excel
# ====================================================================
def compute_metrics(ai, students):
    """回傳 (cids, both, 指標表 rows, 逐課差異 rows, 待裁決 rows)。

    結構對應:
    - 整合標註(能力標籤/難易度/修課對象):兩位同學各自獨立標後整合的人類共識,AI vs 此共識值。
    - 兩輪欄位(趣味度/推薦度/課程節奏):AI vs 各輪 + 同學間信度(第一輪 vs 第二輪)。
    """
    # 可比對 = AI 有 ∩ 同學整合標註有內容
    cids = [c for c in ai if c in students and shared_filled(students[c])]
    metric_rows = []   # [欄位, 類型, 對象, 指標, 值, 判讀, n, 一致率%, AC1]
    diff_rows   = []   # [courseId, 課名, 欄位, AI, 同學..., 是否分歧]
    pending     = []   # [courseId, 課名, 欄位, AI, 同學, 原因]

    def nm(c): return ai[c]['name']
    def raters(c): return students[c]['raters']
    def shared(c): return students[c]['shared']

    # ---- 數值(順序量表 1-5):主判讀用 Gwet's AC2(加權);ICC 並列為輔 ----
    for f in NUMERIC:
        matrix = []; within1 = 0; ncmp = 0; ai_int = []; st_int = []
        for c in cids:
            aiv = _to_num(ai[c]['result'].get(f))
            svals = [rt[f] for rt in raters(c) if rt[f] is not None]
            rs = ([aiv] if aiv is not None else []) + svals
            if len(rs) >= 2:
                matrix.append(rs)
            if aiv is not None and svals:
                near = min(abs(aiv - sv) for sv in svals)   # 與「最接近的同學」差距
                diff_rows.append([c, nm(c), f, aiv, svals, '分歧(自動採認)' if near >= 2 else ''])
                # 分層:趣味/推薦為主觀數值(AC2 已達標),差異以「取均值」自動採認,不送主持人(僅記錄於逐課差異)
                ncmp += 1
                if abs(aiv - (sum(svals) / len(svals))) <= 1: within1 += 1
                ai_int.append(int(round(aiv)))                    # AI vs 同學第一輪(順序)
                st_int.append(int(round(raters(c)[0][f])) if raters(c)[0][f] is not None else int(round(sum(svals)/len(svals))))
        icc = icc_2_1(matrix) if len(matrix) >= 2 else None
        ac2 = gwet_ac2(ai_int, st_int, span=4)
        ag = (100.0 * within1 / ncmp) if ncmp else None
        metric_rows.append([f, '數值(順序)', 'AI vs 同學', 'ICC(2,1)', icc, interpret(ac2), len(ai_int), ag, ac2])

    # ---- 課程節奏(序位三級):加權 Kappa;裁決只在「差兩級(流暢↔拖泥帶水)」----
    ai_seq, st_seq = [], []
    for c in cids:
        aiv = ai[c]['result'].get('課程節奏')
        rs = raters(c)
        svs = [rt['課程節奏'] for rt in rs if rt['課程節奏']]
        if aiv and rs and rs[0]['課程節奏']:
            ai_seq.append(str(aiv)); st_seq.append(str(rs[0]['課程節奏']))
        if aiv in PACE_RANK and svs:
            gaps = [abs(PACE_RANK[aiv] - PACE_RANK[s]) for s in svs if s in PACE_RANK]
            near = min(gaps) if gaps else 0
            mark = '' if near == 0 else ('分歧' if near >= 2 else '相鄰(可接受)')
            diff_rows.append([c, nm(c), '課程節奏', aiv, svs, mark])
            if near >= 2:   # 僅 流暢↔拖泥帶水 才送人工;相鄰(需開倍速)視為可接受
                pending.append([c, nm(c), '課程節奏', aiv, svs, '節奏差2級(流暢↔拖泥帶水)'])
    kw = weighted_kappa(ai_seq, st_seq, PACE_RANK)
    pace_ac2 = gwet_ac2([PACE_RANK[x] for x in ai_seq], [PACE_RANK[y] for y in st_seq], span=2)
    metric_rows.append(['課程節奏', '類別(序位)', 'AI vs 同學1', '加權Kappa', kw, interpret(pace_ac2), len(ai_seq), pace_accept_pct(ai_seq, st_seq), pace_ac2])

    # ---- 難易度(單一類別):Kappa(AI vs 同學標註) ----
    ai_seq, st_seq = [], []
    for c in cids:
        aiv = ai[c]['result'].get('難易度'); sv = shared(c)['難易度']
        if aiv and sv:
            ai_seq.append(str(aiv)); st_seq.append(str(sv))
            same = str(aiv) == str(sv)
            diff_rows.append([c, nm(c), '難易度', aiv, [sv], '' if same else '分歧(自動採認)'])
            # 分層:難易度為粗分兩級,差異以「同學共識為準」自動採認,不送主持人(僅記錄於逐課差異)
    k = cohen_kappa(ai_seq, st_seq); ac1 = gwet_ac1(ai_seq, st_seq)
    metric_rows.append(['難易度', '類別', 'AI vs 同學', "Cohen's Kappa", k, interpret(ac1), len(ai_seq), agree_pct(ai_seq, st_seq), ac1])

    # ---- 能力標籤(單一):逐 A/B/C/D 二元 Kappa ----
    for lab in CAP_LABELS:
        ai_seq, st_seq = [], []
        for c in cids:
            aih = 1 if lab in (ai[c]['result'].get('能力標籤') or []) else 0
            sth = 1 if lab in (shared(c)['能力標籤'] or []) else 0
            ai_seq.append(aih); st_seq.append(sth)
            if aih != sth:
                diff_rows.append([c, nm(c), f'能力標籤{lab}', '有' if aih else '無',
                                  ['有' if sth else '無'], '分歧'])
        k = cohen_kappa(ai_seq, st_seq); ac1 = gwet_ac1(ai_seq, st_seq)
        metric_rows.append([f'能力標籤 {lab}', '類別(二元)', 'AI vs 同學', "Cohen's Kappa", k, interpret(ac1), len(ai_seq), agree_pct(ai_seq, st_seq), ac1])

    # 能力標籤改以「課程層級」裁決:AI 與同學標籤集合對稱差 ≥2 才需人工(差 1 個視為細微,自動採認)
    for c in cids:
        ai_set = set(ai[c]['result'].get('能力標籤') or [])
        st_set = set(shared(c)['能力標籤'] or [])
        symd = ai_set ^ st_set
        if len(symd) >= 2:
            pending.append([c, nm(c), '能力標籤', ''.join(sorted(ai_set)) or '無',
                            [''.join(sorted(st_set)) or '無'], f'標籤集合差{len(symd)}個'])

    # ---- 修課對象(單一)布林:逐項 Kappa ----
    for flag in AUD_FLAGS:
        ai_seq, st_seq = [], []
        for c in cids:
            aih = 1 if ai[c]['result'].get(flag) else 0
            sth = 1 if shared(c)[flag] else 0
            ai_seq.append(aih); st_seq.append(sth)
        k = cohen_kappa(ai_seq, st_seq); ac1 = gwet_ac1(ai_seq, st_seq)
        metric_rows.append([flag, '類別(二元)', 'AI vs 同學', "Cohen's Kappa", k, interpret(ac1), len(ai_seq), agree_pct(ai_seq, st_seq), ac1])

    # ---- 同學間信度(僅兩輪欄位:趣味度/推薦度/課程節奏,兩輪都填才算) ----
    both = [c for c in cids if len(raters(c)) >= 2]
    if both:
        for f in NUMERIC:
            mat = []; r1 = []; r2 = []
            for c in both:
                vs = [raters(c)[0][f], raters(c)[1][f]]
                if all(v is not None for v in vs):
                    mat.append(vs); r1.append(int(round(vs[0]))); r2.append(int(round(vs[1])))
            within1 = sum(1 for vs in mat if abs(vs[0] - vs[1]) <= 1)
            ag = (100.0 * within1 / len(mat)) if mat else None
            icc = icc_2_1(mat) if len(mat) >= 2 else None
            ac2 = gwet_ac2(r1, r2, span=4)
            metric_rows.append([f, '數值(順序)', '同學1 vs 同學2', 'ICC(2,1)', icc, interpret(ac2), len(r1), ag, ac2])
        a1, a2 = [], []
        for c in both:
            v1, v2 = raters(c)[0]['課程節奏'], raters(c)[1]['課程節奏']
            if v1 and v2: a1.append(str(v1)); a2.append(str(v2))
        kw = weighted_kappa(a1, a2, PACE_RANK)
        pace_ac2 = gwet_ac2([PACE_RANK[x] for x in a1], [PACE_RANK[y] for y in a2], span=2)
        metric_rows.append(['課程節奏', '類別(序位)', '同學1 vs 同學2', '加權Kappa', kw, interpret(pace_ac2), len(a1), pace_accept_pct(a1, a2), pace_ac2])

    return cids, both, metric_rows, diff_rows, pending

def write_excel(ai, students, cids, both, metric_rows, diff_rows, pending):
    wb = openpyxl.Workbook()
    thin = Side(style='thin', color='D9D9D9')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hfill = PatternFill('solid', fgColor=NAVY)
    hfont = Font(color='FFFFFF', bold=True, name='Microsoft JhengHei')
    base  = Font(name='Microsoft JhengHei')
    wrap  = Alignment(wrap_text=True, vertical='top')

    def style_header(ws, ncol, row=1):
        for c in range(1, ncol + 1):
            cell = ws.cell(row, c); cell.fill = hfill; cell.font = hfont
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border

    # ---- 工作表1:總覽 ----
    ws = wb.active; ws.title = '總覽'
    ws['A1'] = 'AI 分析 vs 同學標註 — 交叉比對報告(份2)'
    ws['A1'].font = Font(size=15, bold=True, color=NAVY, name='Microsoft JhengHei')
    info = [
        '',
        f'AI 端課程數(可對 courseId):{len(ai)}',
        f'同學表中有標註的課程數:{sum(1 for c in students if shared_filled(students[c]))}',
        f'可比對課程數(AI∩同學,有整合標註):{len(cids)}',
        f'兩輪都完成、可算同學間信度的課程數:{len(both)}',
        f'待主持人裁決筆數:{len(pending)}',
        '',
        '門檻:Kappa / ICC ≥ 0.70 視為達標(實質一致)。',
        '註:同學表單自 2026-07 起六面向(趣味度/推薦度/內容深度/實作程度/結構清晰度/應用廣度)全評,已全數完成。'
            if not PENDING_CIDS else
            f'註:同學表單自 2026-07 起六面向全評;仍有 {len(PENDING_CIDS)} 門課只完成部分面向,本次先排除,待補齊後再納入重跑。',
        '註:本報告對 AI 端與份1 交付物皆唯讀,為獨立新檔。',
    ]
    for i, line in enumerate(info, start=2):
        ws.cell(i, 1, line).font = base
    ws.column_dimensions['A'].width = 70

    # ---- 工作表2:一致性指標 ----
    ws = wb.create_sheet('一致性指標')
    cols = ['欄位', '類型', '比對對象', '指標', '數值', 'Gwet AC1/AC2', '判讀', 'n(課數)', '一致率']
    ws.append(cols); style_header(ws, len(cols))
    for row in metric_rows:
        field, typ, obj, metric, val, interp, n, ag, ac1 = row
        valcell = '' if val is None else round(val, 3)
        ac1cell = '' if ac1 is None else round(ac1, 3)
        agcell  = '' if ag is None else f'{ag:.0f}%'
        ws.append([field, typ, obj, metric, valcell, ac1cell, interp, n, agcell])
        rr = ws.max_row
        for c in range(1, len(cols) + 1):
            ws.cell(rr, c).font = base; ws.cell(rr, c).border = border
        if val is not None:
            fill = GREEN if val >= 0.70 else (YELLOW if val >= 0.60 else RED)
            ws.cell(rr, 5).fill = PatternFill('solid', fgColor=fill)
        if ac1 is not None:
            fill = GREEN if ac1 >= 0.70 else (YELLOW if ac1 >= 0.60 else RED)
            ws.cell(rr, 6).fill = PatternFill('solid', fgColor=fill)
    ws.append([])
    ws.append(['註:Gwet AC1/AC2 是對「高一致但偏態」穩健的一致性係數,解 Kappa 悖論。類別欄位用 AC1;順序欄位(課程節奏、趣味度/推薦度)用加權的 AC2,均以此為主要判讀。'])
    ws.append(['註:趣味度/推薦度為 1-5 順序量表,以 ICC 當連續量會低估;改用 AC2(二次加權)才適切,ICC 並列為輔。'])
    ws.append(['註:「一致率」類別=逐課完全相同;數值=AI 與同學均值差≤1;節奏=序位差≤1。'])
    widths = [16, 12, 16, 14, 9, 9, 16, 8, 9]
    for i, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = 'A2'

    # ---- 工作表3:逐課逐欄位差異 ----
    ws = wb.create_sheet('逐課差異')
    cols = ['courseId', '課名', '欄位', 'AI', '同學', '是否分歧']
    ws.append(cols); style_header(ws, len(cols))
    for c, name, f, aiv, sv, flag in diff_rows:
        ws.append([c, name, f, str(aiv), '、'.join(str(x) for x in sv), flag])
        rr = ws.max_row
        for cc in range(1, len(cols) + 1):
            ws.cell(rr, cc).font = base; ws.cell(rr, cc).border = border; ws.cell(rr, cc).alignment = wrap
        if flag:
            ws.cell(rr, 6).fill = PatternFill('solid', fgColor=RED)
    for i, w in enumerate([11, 30, 14, 10, 16, 10], 1): ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = 'A2'

    # ---- 工作表4:待主持人裁決 ----
    ws = wb.create_sheet('待主持人裁決')
    cols = ['courseId', '課名', '欄位', 'AI', '同學', '原因']
    ws.append(cols); style_header(ws, len(cols))
    for c, name, f, aiv, sv, why in pending:
        ws.append([c, name, f, str(aiv), '、'.join(str(x) for x in sv), why])
        rr = ws.max_row
        for cc in range(1, len(cols) + 1):
            ws.cell(rr, cc).font = base; ws.cell(rr, cc).border = border; ws.cell(rr, cc).alignment = wrap
    for i, w in enumerate([11, 30, 14, 10, 16, 14], 1): ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = 'A2'

    wb.save(OUT_XLSX)
    print(f'✔ 已輸出獨立報告:{OUT_XLSX}(4 工作表:總覽 / 一致性指標 / 逐課差異 / 待主持人裁決)')

# ====================================================================
# 份1(AI)既有交付物與其它非同學檔,絕不可當成同學表誤讀。
PART1_FILES = {
    OUT_XLSX,
    '磨課師AI課程分類標籤表.xlsx',
    '磨課師AI課程能力分類標籤表.xlsx',
    '磨課師AI課程能力分類標籤表_完整版.xlsx',
    '課程評分雷達圖_測試10門.xlsx',
}

def looks_like_student_sheet(path):
    """結構驗證:有「能力分類標籤表」分頁,且第 4 列表頭含「第一次/第二次負責同學」。"""
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        if SHEET_NAME not in wb.sheetnames:
            return False
        ws = wb[SHEET_NAME]
        h1 = str(ws.cell(HEADER_ROW - 1, COL['第一次負責同學']).value or '')
        h2 = str(ws.cell(HEADER_ROW - 1, COL['第二次負責同學']).value or '')
        return '負責同學' in h1 and '負責同學' in h2
    except Exception:
        return False

def find_student_file(explicit=None):
    if explicit: return explicit
    # 只認名稱帶同學表特徵(請勿外流/同學/標註/Google 試算表)且非份1 檔,再做結構驗證。
    markers = ('請勿外流', '同學', '標註', 'google', '試算表')
    cands = [p for p in glob.glob('*.xlsx')
             if p not in PART1_FILES and any(m in p.lower() for m in markers)]
    for p in cands:
        if looks_like_student_sheet(p):
            return p
    return None

RESULT_JSON = 'AI_vs_同學_比對結果.json'   # ← 去識別化(無姓名),給 PDF/HTML 共用
ZONE_GOOD_TH = 4.0   # 推薦度均值 ≥ 此值 → Zone A(首選)
ZONE_MID_TH  = 3.0   # 推薦度均值 ≥ 此值(但 <ZONE_GOOD_TH)→ Zone B(備選,供配課彈性使用)

def classify_zone(e):
    """Zone 分區(59 門課六面向已全數完成、核心分歧已裁決完畢,依推薦均值三級分層,供配課彈性使用):
    A=推薦均值≥4.0 → 首選,優先組模組;
    B=推薦均值 3.0–4.0 → 備選,Zone A 不足時來此補;
    C=推薦均值<3.0 → 較弱,組模組時不優先。
    推薦均值缺漏 → 暫歸 B(資料不足)。"""
    if e['avg_rec'] is None:
        return 'B'
    if e['avg_rec'] >= ZONE_GOOD_TH: return 'A'
    if e['avg_rec'] >= ZONE_MID_TH: return 'B'
    return 'C'

def export_results_json(ai, students, cids, both, metric_rows, diff_rows, pending, meta):
    """輸出去識別化結果 JSON:總覽 + 指標 + 課程層級彙整(無學生姓名、無逐筆原始標註)。"""
    # 課程層級:衝突來自 pending(courseId, 名, 欄位, AI, [同學], 原因)
    by_course = {}
    for c in cids:
        by_course[c] = {'courseId': c, 'name': ai[c]['name'] or meta.get(c, {}).get('name', ''),
                        'org': meta.get(c, {}).get('org', ''),
                        'n_raters': len(students[c]['raters']), 'conflicts': []}
    for cid, name, field, aiv, svs, why in pending:
        if cid in by_course:
            by_course[cid]['conflicts'].append(
                {'欄位': field, 'AI': aiv, '同學': svs, '原因': why})
    courses = []
    for c in sorted(by_course):
        e = by_course[c]; e['n_conflict'] = len(e['conflicts'])
        e['status'] = 'review' if e['n_conflict'] else 'ok'
        # 可比對欄位的雙方值(透明呈現,無姓名)
        s = students[c]['shared']; rs = students[c]['raters']
        e['compare'] = {
            '能力標籤': {'AI': ai[c]['result'].get('能力標籤') or [], '同學': s['能力標籤'] or []},
            '難易度':   {'AI': ai[c]['result'].get('難易度'), '同學': s['難易度']},
            '課程節奏': {'AI': ai[c]['result'].get('課程節奏'),
                       '同學': [rt['課程節奏'] for rt in rs if rt['課程節奏']]},
        }
        for f in NUMERIC:
            e['compare'][f] = {'AI': ai[c]['result'].get(f),
                               '同學': [rt[f] for rt in rs if rt[f] is not None]}
        # 推薦度均值(AI + 同學各輪)→ 課程「優劣」依據
        recs = [v for v in [_to_num(ai[c]['result'].get('推薦度'))] + e['compare']['推薦度']['同學'] if v is not None]
        e['avg_rec'] = round(sum(recs) / len(recs), 2) if recs else None
        e['zone'] = classify_zone(e)
        courses.append(e)

    n_review = sum(1 for e in courses if e['status'] == 'review')
    zc = {'A': 0, 'B': 0, 'C': 0}
    for e in courses: zc[e['zone']] += 1
    data = {
        'generated': datetime.date.today().isoformat(),
        'note_confidential': '本檔已去識別化:不含學生姓名與逐筆原始標註,可用於公開報告。',
        'overview': {
            'ai_courses': len(ai), 'comparable': len(cids), 'both_rounds': len(both),
            'review_courses': n_review, 'auto_ok_courses': len(cids) - n_review,
            'pending_items': len(pending),
            'zone_A': zc['A'], 'zone_B': zc['B'], 'zone_C': zc['C'], 'zone_good_th': ZONE_GOOD_TH, 'zone_mid_th': ZONE_MID_TH,
        },
        'metrics': [
            {'欄位': f, '類型': t, '比對對象': o, '指標': m,
             '值': (None if v is None else round(v, 3)), '判讀': itp, 'n': n,
             '一致率': (None if ag is None else round(ag, 1)),
             'AC1': (None if ac1 is None else round(ac1, 3))}
            for f, t, o, m, v, itp, n, ag, ac1 in metric_rows
        ],
        'courses': courses,
        'methodology': {
            '數值': 'ICC(2,1),門檻0.70', '類別': "Cohen's Kappa,門檻0.70",
            '課程節奏': '序位三級(精煉流暢>需開倍速>拖泥帶水),用加權Kappa;相鄰一級(需開倍速)視為可接受,僅差兩級(流暢↔拖泥帶水)才送人工',
            '裁決規則': '分層處理。主持人裁決僅限影響分類的核心分歧:①能力標籤集合對稱差≥2;②課程節奏差2級(流暢↔拖泥帶水)。其餘自動採認並記錄(不佔用主持人):難易度以同學共識為準、趣味/推薦(AC2已達標)取均值、能力標籤差1個視為細微。',
            '標註結構': '能力標籤/難易度/修課對象 = 兩位同學各自獨立標註後整合的人類共識,AI 與此共識比對;趣味度/推薦度/課程節奏為兩輪各自分數,另計同學間信度',
            '範圍說明': ('同學表單自 2026-07 起六面向(趣味度/推薦度/內容深度/實作程度/結構清晰度/應用廣度)全評,已全數完成'
                     if not PENDING_CIDS else
                     f'同學表單自 2026-07 起六面向全評;仍有 {len(PENDING_CIDS)} 門課只完成部分面向,本次先排除,待補齊後再納入重跑'),
            '一致率定義': '類別=逐課完全相同比率;數值=AI與同學均值差≤1比率;節奏=序位差≤1可接受比率',
            'Gwet_AC1': '對「高一致但偏態」穩健的一致性係數,pe=(1/(q−1))Σπk(1−πk)。Kappa 在偏態時被低估,故類別欄位以 AC1 為主要判讀、Kappa 為輔。',
            'Gwet_AC2': '加權版 AC1,用於順序量表(課程節奏、趣味度/推薦度 1-5)。pe=(Tw/(q(q−1)))Σπk(1−πk),Tw=加權矩陣總和。給「差一級」高分(二次加權),適合 Likert;趣味度/推薦度改用 AC2 後達 0.81/0.84,ICC 並列為輔(ICC 把順序量表當連續且要求課程間變異大,會低估)。',
            'Zone分區': f'依推薦均值三級分層(核心分歧已全數裁決,供配課彈性使用):A=推薦均值≥{ZONE_GOOD_TH}(首選);B=推薦均值 {ZONE_MID_TH}–{ZONE_GOOD_TH}(備選,Zone A 不足時來此補);C=推薦均值<{ZONE_MID_TH}(較弱,不優先)。',
        },
    }
    json.dump(data, open(RESULT_JSON, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'✔ 已輸出去識別化結果:{RESULT_JSON}(供 PDF/HTML 共用,無姓名)')
    return data

def main():
    global AI_RESULTS, QUEUE_JSON, OFFICIAL_XLSX, OUT_XLSX, RESULT_JSON
    ap = argparse.ArgumentParser(
        description='AI 評分與真人標註的交叉比對（純統計，不呼叫 API）')
    ap.add_argument('--students', help='同學標註 .xlsx 路徑（含姓名,不納入交付）')
    ap.add_argument('--ai-results', default=AI_RESULTS, help='AI 評分結果 JSON（唯讀）')
    ap.add_argument('--queue', default=QUEUE_JSON, help='課程佇列 JSON（課名→courseId 對應）')
    ap.add_argument('--official', default=OFFICIAL_XLSX, help='官方課程清單 .xlsx（補空白課名用）')
    ap.add_argument('--output', default=OUT_XLSX, help='輸出的比對報告 .xlsx')
    ap.add_argument('--result-json', default=RESULT_JSON,
                    help='輸出的去識別化結果 JSON（無姓名,供報告與儀表板共用）')
    ap.add_argument('--inspect', action='store_true', help='只印出同學表偵測欄位')
    args = ap.parse_args()

    AI_RESULTS, QUEUE_JSON = args.ai_results, args.queue
    OFFICIAL_XLSX, OUT_XLSX, RESULT_JSON = args.official, args.output, args.result_json

    spath = find_student_file(args.students)
    if args.inspect:
        if not spath:
            print('找不到同學標註 .xlsx。請用 --students 指定路徑,或把檔案放進本資料夾。')
            return
        print(f'inspect:{spath}\n')
        load_students_xlsx(spath, inspect=True)
        return

    ai = load_ai_by_cid()
    meta = official_meta()
    # 補顯示名稱(空白名取官方課名)
    for cid, e in ai.items():
        if not e['name']:
            e['name'] = meta.get(cid, {}).get('name', '(未命名)')

    if not spath:
        print(f'已載入 AI 端 {len(ai)} 門(皆對到 courseId)。')
        print('同學標註 .xlsx 尚未放入本資料夾 → 統計核心與輸出已就緒,檔案一到即可出報告。')
        print('提示:把 Google 試算表「下載成 .xlsx」放進資料夾,先跑 --inspect 校正欄位對應。')
        return

    if not looks_like_student_sheet(spath):
        print(f'⚠ 「{spath}」看起來不是同學標註表(缺「第一次/第二次負責同學」欄)。')
        print('  為避免誤把份1(AI)檔當成同學資料,已中止。請用 --students 指定正確檔,或先跑 --inspect。')
        return
    print(f'同學標註來源:{spath}')
    students = load_students_xlsx(spath)
    filled = {c: v for c, v in students.items() if shared_filled(v)}
    print(f'同學表課程列數:{len(students)};其中有整合標註:{len(filled)}')

    skip = sorted(c for c in PENDING_CIDS if c in students)
    if skip:
        names = [ai.get(c, {}).get('name') or meta.get(c, {}).get('name', c) for c in skip]
        print(f'⚠ {len(skip)} 門課六面向尚未評完,本次先排除:{list(zip(skip, names))}')
        students = {c: v for c, v in students.items() if c not in PENDING_CIDS}

    cids, both, metric_rows, diff_rows, pending = compute_metrics(ai, students)
    print(f'可比對:{len(cids)} 門;兩輪都完成:{len(both)} 門;待裁決:{len(pending)} 筆\n')
    print('=== 一致性指標(摘要)===')
    for field, typ, obj, metric, val, interp, n, ag, ac1 in metric_rows:
        vs = f'{val:.3f}' if val is not None else '  —  '
        a1s = f'AC1={ac1:+.2f}' if ac1 is not None else '        '
        ags = f'一致率{ag:.0f}%' if ag is not None else ''
        print(f'  [{obj:12s}] {field:12s} {metric:12s}={vs} {a1s} ({interp}, n={n}) {ags}')

    write_excel(ai, students, cids, both, metric_rows, diff_rows, pending)
    export_results_json(ai, students, cids, both, metric_rows, diff_rows, pending, meta)

if __name__ == '__main__':
    main()
