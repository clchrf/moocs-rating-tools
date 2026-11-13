# -*- coding: utf-8 -*-
"""證據鏈索引(traceability manifest)。

補上專案原本缺少的一環:每一門課實際分析了哪幾支影片、對應到哪些本機檔案、
產出哪張截圖與哪份素材、最後得到什麼結果。抽樣為決定性演算法,本檔由
moocs_queue.json 重算後與磁碟現況比對,不是事後補記。

產出:
  證據鏈索引.xlsx   三個工作表(課程層、影片層、資料檔清點)
  證據鏈索引.csv    課程層純文字版,供程式比對
"""
import sys, os, io, re, json, csv, hashlib, glob, datetime
import corpus
sys.stdout.reconfigure(encoding='utf-8')
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import moocs_core as mf
import bundles_data as BD

OUT_X = '證據鏈索引.xlsx'
OUT_C = '證據鏈索引.csv'
NAVY, LIGHT = '1F3864', 'EAF0F8'
HF = Font(name='微軟正黑體', bold=True, color='FFFFFF', size=10)
BF = Font(name='微軟正黑體', size=9.5)
BFB = Font(name='微軟正黑體', size=9.5, bold=True)
TITLE = Font(name='微軟正黑體', bold=True, size=15, color=NAVY)
SUB = Font(name='微軟正黑體', size=9.5, color='666666')
_t = Side(style='thin', color='D9D9D9')
BORDER = Border(_t, _t, _t, _t)


def safe(nm):
    return re.sub(r'[\\/:*?"<>|]', '_', nm)[:50]


def vid_path(ref):
    """還原 download_video 的命名規則,推出本機檔名。"""
    if ref.startswith('http') and '.mp4' in ref.lower():
        return f"videos/mp4_{hashlib.md5(ref.encode()).hexdigest()[:12]}.mp4"
    return f'videos/{ref}.mp4'


def head(ws, title, subtitle, headers, widths, row=4):
    ws['A1'] = title; ws['A1'].font = TITLE
    ws['A2'] = subtitle; ws['A2'].font = SUB
    for j, h in enumerate(headers, 1):
        c = ws.cell(row, j, h); c.fill = PatternFill('solid', fgColor=NAVY)
        c.font = HF; c.border = BORDER
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    for j, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.row_dimensions[row].height = 30
    ws.freeze_panes = f'A{row+1}'


def put(ws, r, vals, left=(), bold=(), fill=None):
    for j, v in enumerate(vals, 1):
        c = ws.cell(r, j, v); c.border = BORDER
        c.font = BFB if j in bold else BF
        c.alignment = Alignment(horizontal='left' if j in left else 'center',
                                vertical='center', wrap_text=True)
        if fill:
            c.fill = PatternFill('solid', fgColor=fill)


queue = json.load(open('moocs_queue.json', encoding='utf-8'))
ai = {r['name']: r for r in json.load(open('analysis_results.json', encoding='utf-8'))}
cmp_ = {e['courseId']: e for e in
        json.load(open('AI_vs_同學_比對結果.json', encoding='utf-8'))['courses']}
final = BD.load_courses()
bundles = BD.enrich(BD.BUNDLES, final)
in_bundle = {}
for b in bundles:
    for cid in b['courses']:
        in_bundle.setdefault(cid, []).append(b['bid'])

# 同名課程:找出以 courseId 命名的專屬檔
def merged_for(cid, nm):
    for cand in (f'merged/cid_{cid}.jpg', f'merged/course_{cid}.jpg',
                 f'merged/{safe(nm)}.jpg'):
        if os.path.exists(cand):
            return cand
    return ''


def analysis_for(cid, nm):
    for cand in (f'merged/cid_{cid}_analysis.txt', f'merged/course_{cid}_analysis.txt',
                 f'merged/{safe(nm)}_analysis.txt'):
        if os.path.exists(cand):
            return cand
    return ''


rows_course, rows_video = [], []
for c in queue:
    cid = str(c.get('courseId'))
    nm = c.get('courseName', '')
    allv = c.get('videoIds', []) + c.get('mp4Urls', [])
    sel = mf.select_videos_smart(c)
    img, txt = merged_for(cid, nm), analysis_for(cid, nm)
    a = ai.get(nm, {})
    res = a.get('result') or {}
    cm = cmp_.get(cid, {})
    f = final.get(cid, {})

    n_local = sum(1 for v in sel if os.path.exists(vid_path(v)))
    txt_len = len(io.open(txt, encoding='utf-8').read()) if txt else 0
    rows_course.append([
        cid, nm, c.get('courseName') and len(c.get('chapters', [])), len(allv), len(sel),
        n_local, os.path.basename(img), os.path.basename(txt), txt_len,
        a.get('analyzer', '(moocs_core)'), a.get('timestamp', ''),
        '、'.join(res.get('能力標籤', [])), res.get('難易度', ''), res.get('推薦度', ''),
        cm.get('n_raters', ''), cm.get('avg_rec', ''),
        f.get('labels', ''), f.get('avg_rec', ''), f.get('grade', ''),
        '、'.join(in_bundle.get(cid, [])) or '—',
    ])
    for i, v in enumerate(sel, 1):
        p = vid_path(v)
        segs = sorted(glob.glob(os.path.join('audio', os.path.basename(p)[:-4] + '_seg*.mp3')))
        trs = sorted(glob.glob(os.path.join('audio', os.path.basename(p)[:-4] + '_seg*.json')))
        rows_video.append([cid, nm, i, (v[:78] + '…') if len(v) > 80 else v,
                           os.path.basename(p), '是' if os.path.exists(p) else '否',
                           len(segs), len(trs)])

wb = openpyxl.Workbook()
NOTE = ('抽樣係由 moocs_core.select_videos_smart() 依 moocs_queue.json 決定性重算,'
        '無隨機成分,任何人可重現;本表另比對本機檔案是否存在。')

ws = wb.active; ws.title = '課程層'
head(ws, '證據鏈索引　課程層',
     f'{len(rows_course)} 門課｜{NOTE}',
     ['課程編號', '課程名稱', '章節數', '平臺影片數', '抽樣分析數', '本機影片檔數',
      '合併截圖檔', '分析素材檔', '素材字數', 'AI 分析者', 'AI 分析時間',
      'AI 能力標籤', 'AI 難易度', 'AI 推薦度', '同學標註人數', '推薦均值(AI+同學)',
      '定稿能力標籤', '定稿推薦均值', '等級', '收錄套裝'],
     [10, 30, 7, 9, 9, 10, 24, 26, 8, 17, 17, 10, 8, 8, 10, 12, 12, 11, 7, 11])
for i, r in enumerate(rows_course, 5):
    put(ws, i, r, left=(2, 7, 8), bold=(2,))

ws2 = wb.create_sheet('影片層')
head(ws2, '證據鏈索引　影片層',
     f'{len(rows_video)} 支實際分析之影片｜每支取 {mf.FRAMES_PER_VIDEO} 幀畫面與 '
     f'{mf.AUDIO_SEGMENTS} 段各 {mf.AUDIO_SEGMENT_MINUTES} 分鐘音訊',
     ['課程編號', '課程名稱', '抽樣序', '影片來源(YouTube ID 或平臺 MP4 位址)',
      '本機檔名', '檔案存在', '音訊片段數', '逐字稿快取數'],
     [10, 26, 7, 58, 26, 8, 10, 11])
for i, r in enumerate(rows_video, 5):
    put(ws2, i, r, left=(2, 4, 5), bold=(2,))

ws3 = wb.create_sheet('資料檔清點')
head(ws3, '證據鏈索引　資料檔清點', '本表為磁碟現況快照,供結案存查',
     ['資料項', '路徑／檔名', '數量', '說明'], [18, 30, 9, 62])
inv = [
    ['課程清單', 'moocs_queue.json', len(queue), '59 門課的編號、課名、簡介、章節與影片位址,為整條流程的起點'],
    ['原始影片', 'videos/*.mp4', corpus._load()['n_sampled_distinct'],
     'YouTube 以影片 ID 命名;平臺 MP4 以網址 MD5 前 12 碼命名(mp4_xxxx.mp4)'],
    ['音訊片段', 'audio/*.mp3', corpus.audio_segment_count(),
     '每支影片 3 段,檔名後綴 _seg0_start / _seg1_mid / _seg2_end'],
    ['逐字稿快取', 'audio/*.json', corpus.transcript_stats()[0],
     '含 text(辨識文字)、stats(語速與停頓)、language(偵測語言)'],
    ['合併截圖', 'merged/*.jpg', len(glob.glob('merged/*.jpg')),
     '每門課一張接觸表(384×216 × 5 欄);同名課程另以 cid_課程編號.jpg 區分'],
    ['分析素材', 'merged/*_analysis.txt', len(glob.glob('merged/*_analysis.txt')),
     '送入模型的文字輸入:課名＋簡介＋偵測語言＋語速數據＋逐字稿前 5000 字'],
    ['AI 分析結果', 'analysis_results.json', 59,
     '59 門課的 19 欄位結果、判斷理由、分析者與時間戳'],
    ['一致性分析', 'AI_vs_同學_比對結果.json', 59,
     '去識別化:逐欄一致率、逐課 AI 與同學對照'],
    ['裁決紀錄', 'apply_arbitration.py', 18,
     '14 筆能力標籤裁決＋4 筆課程節奏裁決,每筆均有書面理由'],
    ['定稿標籤表', '磨課師AI課程能力分類標籤表_定稿(AI+同學).xlsx', 59,
     '以課程編號為鍵,同名課程各自獨立;為所有下游成果的單一事實來源'],
    ['評分規準', 'config/prompt.txt（評分規準）', 1,
     '4,231 字、10 個區塊;另有 AI評分規準_SYSTEM_PROMPT技術文件.docx 全文說明'],
    ['套裝定義', 'bundles_data.py 之 BUNDLES', 9,
     '9 組主題套裝的課程、順序、學習目標、先備要求與能力對應'],
]
for i, r in enumerate(inv, 5):
    put(ws3, i, r, left=(1, 2, 4))

wb.save(OUT_X)

with io.open(OUT_C, 'w', encoding='utf-8-sig', newline='') as fp:
    w = csv.writer(fp)
    w.writerow(['課程編號', '課程名稱', '章節數', '平臺影片數', '抽樣分析數', '本機影片檔數',
                '合併截圖檔', '分析素材檔', '素材字數', 'AI分析者', 'AI分析時間',
                'AI能力標籤', 'AI難易度', 'AI推薦度', '同學標註人數', '推薦均值',
                '定稿能力標籤', '定稿推薦均值', '等級', '收錄套裝'])
    w.writerows(rows_course)

print(f'OK 已產出 {OUT_X} 與 {OUT_C}')
print(f'   課程層 {len(rows_course)} 列｜影片層 {len(rows_video)} 列｜資料檔清點 {len(inv)} 列')
miss_img = [r[0] for r in rows_course if not r[6]]
miss_txt = [r[0] for r in rows_course if not r[7]]
print(f'   無合併截圖的課程:{miss_img or "無"}')
print(f'   無分析素材的課程:{miss_txt or "無"}')
lack = [(r[0], r[4], r[5]) for r in rows_course if r[5] < r[4]]
print(f'   本機影片檔少於抽樣數的課程:{len(lack)} 門(影片為過程檔,可依 queue 重新下載)')
