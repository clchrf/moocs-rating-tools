# 完整流程範例：用 Python 從頭跑一遍

以本計畫的 59 門課為例，從平臺抓取一路跑到互動儀表板。
Windows / macOS / Linux 皆適用（指令以 Windows 為例，其他平台把 `python` 換成 `python3`）。

---

## 0. 安裝

```bash
git clone https://github.com/<你的帳號>/moocs-rating-tools.git
cd moocs-rating-tools
pip install -e .
playwright install chromium
```

外部工具（前處理需要）：

```bash
# Windows（winget）
winget install Gyan.FFmpeg
winget install yt-dlp.yt-dlp

# macOS（Homebrew）
brew install ffmpeg yt-dlp
```

設定金鑰：

```bash
copy .env.example .env      # macOS/Linux: cp .env.example .env
# 用編輯器打開 .env，填入 ANTHROPIC_API_KEY
```

確認環境就緒：

```bash
python tools/test_llm.py
```

```text
── 設定檔 ──
[OK]   prompt.txt　　4231 字
[OK]   fields.json　 19 欄（必填 10 欄）
       數值欄位：趣味度、內容深度、實作程度、結構清晰度、應用廣度、推薦度
       判斷原因：7 項
[OK]   pipeline.json 模型 ['claude-sonnet-4-6']　呼叫 3 次　上限 $9.5

── 整合規則（不呼叫模型）──
[OK]   整合 3 筆 → 19 欄位
       推薦度 4/2/4 → 平均取整 3
       難易度 進階/進階/基礎 → 多數決 進階
       分歧標記：['推薦度⚠️分歧大']
[OK]   驗證器正確攔下超出範圍的分數

── 總結 ──
[OK]   設定檔
[OK]   整合規則

全部通過，可以開始跑整批分析。
```

加上 `--live` 會實際呼叫一次模型做端對端測試（需要 `merged/` 已有素材）。

---

## 1. 決定要盤點哪些課程

編輯 `config/course_ids.txt`，一行一個課程 ID，`#` 開頭為註解：

```text
# 要盤點的課程 ID，一行一個。# 開頭為註解。
# 本清單為教育部磨課師平臺 AI 專區課程（2026-05-12 版本）。
10000022
10000034
10000041
...
```

本計畫的清單共 59 門，已內附。

---

## 2. 登入平臺

```bash
python save_login.py
```

會開啟瀏覽器請你登入一次，成功後把登入狀態存成 `session.json`。

> `session.json` 是登入憑證，已列入 `.gitignore`，**不得外流**。

---

## 3. 取課程清單與影片位址（fetch_courses）

```bash
python fetch_courses.py
```

```text
課程 ID 清單：config/course_ids.txt（59 門）
已有影片 0 門 | 待抓 59 門

[1/59] 10000022 ... YT:0 MP4:12 | 機器學習與物聯網資料分析
[2/59] 10000034 ... YT:8 MP4:0 | 人工智慧與深度學習
...

課程 59 門 | YouTube 270 支 | 平臺 MP4 549 支 | 合計 819 支
已輸出：moocs_queue.json
下一步：python preprocess_courses.py
```

每門課取出課名、簡介、**章節結構**、YouTube 影片 ID 與平臺 MP4 位址。
章節結構是後續「章節感知抽樣」的依據，不可省略。

可中斷續跑：已取得影片的課程會自動跳過。平臺改版導致抓不到時，
程式會把該課留空並在最後列出，請人工確認後再排除。

---

## 4. 前處理（preprocess_courses）—— 最耗時但不花錢

```bash
python preprocess_courses.py
```

```text
課程佇列：moocs_queue.json（59 門）
輸出目錄：merged
抽幀：每支 3 幀 | 音訊：3 段 × 2 分鐘 | 逐字稿截斷：5000 字
載入 Whisper base 模型（本機執行，不耗 API 額度）...
開始批次前處理

[1/59] 機器學習與物聯網資料分析
  抽樣 8 支影片（全部 12 支，章節 4）
  完成：截圖 24 幀、逐字稿 7,412 字、語言 zh
...

前處理完成：本次處理 59 門、已存在跳過 0 門、無法處理 0 門
耗時 614.2 分鐘
下一步：python rate_courses.py
```

這一步做四件事，**完全不呼叫 LLM**：

1. 下載影片（YouTube 走 yt-dlp、平臺 MP4 走 session cookie）
2. 章節感知抽樣：819 支 → 258 支（31.5%）
3. 抽幀 → 去黑畫面 → 去重複 → 合併成 5 欄接觸表
4. 切音訊（開頭／中間／結尾各 2 分鐘）→ Whisper 轉逐字稿 → 算語速與停頓比例

耗時以小時計。建議分批跑：

```bash
python preprocess_courses.py --limit 10      # 一次跑 10 門
```

可隨時中斷，已完成的課程（`.jpg` 與 `_analysis.txt` 都在）會自動跳過。

單獨重跑某一門：

```bash
python preprocess_courses.py --only "機器學習基礎理論與實作" --force
```

---

## 5. LLM 評分（rate_courses）

先空跑確認備齊狀況：

```bash
python rate_courses.py --dry-run
```

```text
[dry-run] 前處理已備齊：59 門
[dry-run] 尚未備齊　　：0 門
[dry-run] 本次將分析　：59 門 × 3 次呼叫 = 177 次
[dry-run] 未呼叫模型，未產生任何輸出
```

先跑一門確認輸出正常，再跑整批：

```bash
python rate_courses.py --limit 1
python rate_courses.py --cost-cap 10
```

```text
[1/59] 機器學習與物聯網資料分析
    Claude（claude-sonnet-4-6）分析中（3 agent）...
    ✅ Agent 1 解析成功
    ✅ Agent 2 解析成功
    ✅ Agent 3 解析成功
  能力['C'] 難易度進階 實作4 推薦4 節奏精煉流暢
...

本次分析 59 門 | 累計完成 59/59 門 | 估算花費 $8.43
已輸出：analysis_results.json、rating.csv、failed.csv
下一步：python compare_students.py（有同學標註）或 python build_bundles.py
```

這一步的設計重點：

- **多次獨立呼叫**：同一組輸入呼叫 N 次（預設 3），抓隨機波動
- **Python 規則整合**：平均／多數決／票數門檻，**不呼叫第二個模型複評**
- **格式不合整筆作廢**：驗證不過不做局部修補
- **可續跑**：已完成的課程自動跳過
- **花費上限**：達到即停，重跑續做

---

## 6. 與真人標註交叉比對（compare_students）

這一步需要同學標註表（Google 試算表下載成 `.xlsx`）。
先校正欄位對應：

```bash
python compare_students.py --students 同學標註.xlsx --inspect
```

確認欄位對得上之後跑比對：

```bash
python compare_students.py --students 同學標註.xlsx
```

```text
同學標註來源:同學標註.xlsx
同學表課程列數:59;其中有整合標註:59
可比對:59 門;兩輪都完成:59 門;待裁決:0 筆

=== 一致性指標(摘要)===
趣味度        ICC(2,1)   0.294  一致率 81.4%  AC1=+0.83
推薦度        ICC(2,1)   ...
...
✔ 已輸出獨立報告:AI_vs_同學_比對報告.xlsx
✔ 已輸出去識別化結果:AI_vs_同學_比對結果.json
```

產出兩份：

| 檔案 | 內容 | 可否對外 |
|---|---|---|
| `AI_vs_同學_比對報告.xlsx` | 4 工作表：總覽／一致性指標／逐課差異／待主持人裁決 | 內部用 |
| `AI_vs_同學_比對結果.json` | 去識別化（無姓名），供報告與儀表板共用 | **可對外** |

> 本程式對 AI 端的結果**一律唯讀，絕不覆寫**；比對結果只寫到全新檔案。
> 同學標註原始表含姓名與自由填答，標示「請勿外流」，不納入交付。

統計方法：類別變項用 Cohen's Kappa，數值變項用 ICC(2,1)，門檻 0.70；
另計 Gwet's AC1/AC2 以處理判定分布極度集中時 Kappa 偏低的問題。
差距 ≥ 2 或類別標籤不同者，列入「待主持人裁決」。

---

## 7. 分級與主題套裝（build_bundles）

有了同學標註之後，用定稿標籤表跑正式模式：

```bash
python build_bundles.py --source "data/磨課師AI課程能力分類標籤表_定稿(AI+同學).xlsx"
```

```text
分級門檻：A ≥ 3.67 ｜ A+ ≥ 4.33
來源：磨課師AI課程能力分類標籤表_定稿(AI+同學).xlsx（AI＋同學，三票併入）

課程母體 59 門 → A+ 9 門 / A 18 門 / C 32 門
主題套裝 9 組 | 納入席次 31（去重後 27 門）
  P1  AI 素養入門包               3 門  13 小時  均值 4.0   零基礎可修
  P2  AI 職場、商業與決策包        3 門  18 小時  均值 4.0   零基礎可修
  P3  生成式 AI 與數位教學應用包   4 門  19 小時  均值 3.83  零基礎可修
  P4  機器學習實作養成包           4 門  25 小時  均值 4.17  需程式基礎
  P5  深度學習進階包               5 門  33 小時  均值 4.07  需程式基礎
  P6  智慧物聯與電腦視覺包         3 門  20 小時  均值 4.22  需程式基礎
  P7  類神經網路與深度學習理論包   3 門  24 小時  均值 3.89  需程式基礎
  P8  教育資料科學與學習分析包     3 門  18 小時  均值 4.22  特定領域
  P9  醫療健康 AI 應用包           3 門  29 小時  均值 4.11  特定領域

[套裝自我檢核] 全部通過（課程數 2–5 門、不納入 C 級、重複納入僅限 A+、A 級全數納入）

已輸出：bundles.json、course_grades.csv
下一步：python build_dashboard.py
```

交付前把關可加 `--strict`，自我檢核未通過時以非零碼結束，方便接進 CI。

套裝組合原則（寫在 `bundles_data.py` 檔頭，可稽核）：

1. 每組 2–5 門課
2. 主題由課程實際內容歸納，不憑空造題
3. A 級（均值 ≥ 3.67）全數納入；C 級不納入
4. 同一門課原則上只歸一組；A+ 級（≥ 4.33）得重複納入作為共用錨點
5. 課程順序 = 修課路徑：先基礎後進階、先概念後實作、先通用後領域
6. 顯示編號 P1–P9 有意義：先依修課門檻分軌，軌內再依先修關係排列

---

## 8. 產生互動儀表板（build_dashboard）

```bash
python build_dashboard.py --output 磨課師AI課程評分儀表板.html
```

```text
樣板：dashboard_template.html
課程 59 門（A+ 9 / A 18）
主題套裝：9 組
一致性指標：14 個欄位

已輸出：磨課師AI課程評分儀表板.html（327 KB，單檔、可離線開啟）
```

用瀏覽器直接開啟即可，不需架站。

---

## 9. 產生證據鏈索引（build_traceability）

結案稽核用：課程 → 影片 → 檔案 → 結果的逐列對照。

```bash
python build_traceability.py
```

---

## 附錄：用內附資料直接看結果

`data/` 收錄本計畫 59 門課的實際結果，不需跑前面的步驟就能產生儀表板：

```bash
python build_bundles.py --source "data/磨課師AI課程能力分類標籤表_定稿(AI+同學).xlsx" \
    --output data/bundles.json --grades-csv data/course_grades.csv
python build_dashboard.py --results data/analysis_results.json \
    --bundles data/bundles.json \
    --compare "data/AI_vs_同學_比對結果.json" \
    --queue data/moocs_queue.json \
    --output data/dashboard.html
```

打開 `data/dashboard.html` 即可。

---

## 附錄：分批與續跑一覽

| 情境 | 指令 |
|---|---|
| 前處理只跑 10 門 | `python preprocess_courses.py --limit 10` |
| 重跑某一門的前處理 | `python preprocess_courses.py --only "課名片段" --force` |
| 評分只跑 5 門 | `python rate_courses.py --limit 5` |
| 重跑某一門的評分 | `python rate_courses.py --only "課名片段" --retry` |
| 改用別的模型 | `python rate_courses.py --model <model-id>` |
| 只呼叫 1 次（省額度測試）| `python rate_courses.py --agents 1 --limit 1` |
| 看哪些課還沒成功 | 開啟 `failed.csv` |
| 調整分級門檻 | `python build_bundles.py --a-threshold 3.5 --aplus-threshold 4.2` |

所有工具都可用 `--help` 看完整參數。
