# 說明文件：使用預編譯 exe（Windows 11）

本文件適用「只想跑評分與儀表板、不想裝 Python」的使用者。

> **先決定走哪條路**
>
> | 想做的事 | 走哪條 |
> |---|---|
> | 已有課程資料與前處理結果，只要評分與儀表板 | **exe 版（本文件）** |
> | 要從平臺抓課程、下載影片、轉逐字稿 | Python 版（[example-full-run.md](example-full-run.md)） |
>
> 前處理（`preprocess_courses`）與平臺抓取（`fetch_courses`）需要 `ffmpeg` 與
> Playwright 瀏覽器核心，**無法打包成單檔 exe**，故交付包不含這兩支。

---

## 一、環境設定

1. 建立一個專案目錄存放本工具，例如 `C:\rating-tools`。
2. 解壓縮 `release-win.zip` 到此目錄（下載連結見 [release.md](../release/release.md)），
   會得到類似下面的結構：

```text
C:\rating-tools
├─ rate_courses.exe          --> LLM 評分工具
├─ build_bundles.exe         --> 分級與主題套裝工具
├─ build_dashboard.exe       --> 互動儀表板產生工具
├─ dashboard_template.html   --> 儀表板版面樣板
├─ .env.example              --> 環境變數範例
└─ config\
   ├─ prompt.txt             --> 評分規準（可自行編修）
   ├─ fields.json            --> 輸出欄位定義
   ├─ pipeline.json          --> 參數與門檻
   └─ course_ids.txt         --> 課程 ID 清單
```

> `config\` 資料夾必須與 exe 放在**同一層**。
> 程式會依序找：環境變數 `MOOCS_CONFIG_DIR` → exe 旁的 `config\` → 當前目錄的 `config\`。

3. 把前處理產物放進 `merged\` 資料夾（每門課一對檔案）：

```text
C:\rating-tools\merged\
├─ 機器學習基礎理論與實作.jpg            --> 合併接觸表（模型的視覺輸入）
└─ 機器學習基礎理論與實作_analysis.txt   --> 課名＋簡介＋語速＋逐字稿（文字輸入）
```

4. 把課程佇列 `moocs_queue.json` 放在 `C:\rating-tools\`。

---

## 二、設定 API 金鑰

1. 向模型供應商申請 API 帳戶並取得 API Key
   （這個 key 通常只會顯示一次，必須妥善保存，**不可公開**）。
2. 在命令列設定環境變數：

```bash
set ANTHROPIC_API_KEY=sk-ant-...（您的 API key）
```

或把 `.env.example` 複製成 `.env` 後填入 —— `.env` 不會進版控。

多把金鑰可用逗號分隔，多次獨立呼叫會輪流使用：

```bash
set ANTHROPIC_API_KEY=sk-ant-aaa,sk-ant-bbb,sk-ant-ccc
```

---

## 三、LLM 評分（rate_courses）

讀 `merged\` 下已備妥的輸入，對每門課做 N 次獨立呼叫，再以 Python 規則整合。

```text
merged\*.jpg（視覺輸入）         ──┐
merged\*_analysis.txt（文字輸入）──┤
config\prompt.txt（評分規準）    ──┼──> rate_courses.exe ──> analysis_results.json
config\fields.json（欄位定義）   ──┤                         rating.csv
moocs_queue.json（課程清單）     ──┘                         failed.csv
```

指令格式：

```bash
usage: rate_courses [-h] [--queue QUEUE] [--merged-dir MERGED_DIR]
                    [--results RESULTS] [--output OUTPUT] [--failed FAILED]
                    [--agents AGENTS] [--model MODEL] [--cost-cap COST_CAP]
                    [--limit LIMIT] [--only ONLY] [--retry] [--dry-run]

用 LLM 依 config/prompt.txt 的規準為課程評分（多次獨立呼叫＋規則整合）

options:
  -h, --help            show this help message and exit
  --queue QUEUE         課程佇列 JSON
  --merged-dir MERGED_DIR
                        前處理輸出目錄（讀 *.jpg 與 *_analysis.txt）
  --results RESULTS     結果 JSON（續跑依據）
  --output OUTPUT       輸出的扁平化評分表 CSV
  --failed FAILED       未成功評分的課程清單 CSV
  --agents AGENTS       同一輸入的獨立呼叫次數
  --model MODEL         分析用模型（預設 claude-sonnet-4-6）
  --cost-cap COST_CAP   估算花費上限（USD），達到即停止，可續跑
  --limit LIMIT         本次最多分析幾門課
  --only ONLY           只分析課名包含此字串的課程
  --retry               重跑已有結果的課程（預設跳過）
  --dry-run             只檢查前處理是否備齊與估算規模，不呼叫模型
```

### 建議的執行順序

**第一步，先空跑確認備齊狀況**（不呼叫模型、不花錢）：

```bash
c:\rating-tools>rate_courses --dry-run

規準：C:\rating-tools\config\prompt.txt（4231 字）
欄位：C:\rating-tools\config\fields.json（19 欄）
模型：claude-sonnet-4-6 × 3 次獨立呼叫
課程：59 門 | 已完成 0 門 | 花費上限 $9.5

[dry-run] 前處理已備齊：59 門
[dry-run] 尚未備齊　　：0 門
[dry-run] 本次將分析　：59 門 × 3 次呼叫 = 177 次
[dry-run] 未呼叫模型，未產生任何輸出
```

**第二步，先跑一門課確認輸出正常**：

```bash
c:\rating-tools>rate_courses --limit 1
```

**第三步，跑整批**（設花費上限保護）：

```bash
c:\rating-tools>rate_courses --cost-cap 10
```

執行中可隨時以 `Ctrl+C` 中斷，已完成的課程會寫進 `analysis_results.json`；
直接重跑即可續做，不會重複計費。

### 功能重點

- **可中斷續跑**：已有結果的課程自動跳過
- **花費上限**：達到 `--cost-cap` 即停止，避免額度爆掉
- **格式不合整筆作廢**：驗證不過的回應不做局部修補，整筆重跑
- **失敗清單**：`failed.csv` 列出尚未成功的課程，沒有失敗時只含表頭
- **分歧標記**：多次呼叫結果差距大時，在 `rating.csv` 的「分歧欄位」標記

### `rating.csv` 主要欄位

- 課程名稱、評級、推薦均值
- 能力標籤、難易度、課程節奏
- 趣味度、內容深度、實作程度、結構清晰度、應用廣度、推薦度
- 通識素養型、跨域應用型、技術實作型
- 關鍵字、細項代碼、先備知識、對應職務角色、對應職場技能、課程摘要
- 7 項判斷理由（能力標籤原因、難易度原因、修課對象原因、評分原因、
  推薦度原因、課程節奏原因、先備職場原因）
- 分歧欄位、分析模型、分析時間

---

## 四、分級與主題套裝（build_bundles）

```bash
usage: build_bundles [-h] [--source SOURCE] [--results RESULTS]
                     [--output OUTPUT] [--grades-csv GRADES_CSV]
                     [--a-threshold A_THRESHOLD]
                     [--aplus-threshold APLUS_THRESHOLD] [--strict]
```

本工具有**兩種模式**，差別在推薦均值由幾票算出來：

| 模式 | 指令 | 推薦均值 | 用途 |
|---|---|---|---|
| 初評 | `build_bundles --results analysis_results.json` | 只有 AI 一票 | 尚無真人標註時的初步排序 |
| 正式 | `build_bundles --source 定稿標籤表.xlsx` | AI ＋ 同學兩票 | 交付用的最終評級 |

```bash
c:\rating-tools>build_bundles --results analysis_results.json

分級門檻：A ≥ 3.67 ｜ A+ ≥ 4.33
來源：analysis_results.json（僅 AI 一票，初評）

課程母體 59 門 → A+ 0 門 / A 11 門 / C 48 門
（初評模式不組套裝：套裝需以三票併入後的評級為準）

已輸出：bundles.json、course_grades.csv
```

> **初評模式的 A+ 是 0 門，這不是錯誤。**
> AI 在推薦度從未給過 5 分，A+ 級全部是靠同學的高分推上去的 ——
> 這正是「AI 只佔三票之一」的意義。初評模式的結果不可當成最終評級。

---

## 五、產生互動儀表板（build_dashboard）

```bash
usage: build_dashboard [-h] [--results RESULTS] [--bundles BUNDLES]
                       [--compare COMPARE] [--queue QUEUE]
                       [--template TEMPLATE] [--output OUTPUT] [--title TITLE]
```

```bash
c:\rating-tools>build_dashboard --output 課程評分儀表板.html

樣板：dashboard_template.html
課程 59 門（A+ 9 / A 18）
主題套裝：9 組
一致性指標：14 個欄位

已輸出：課程評分儀表板.html（327 KB，單檔、可離線開啟）
```

產出為**單一 HTML 檔**，所有資料內嵌、無外部連線，可離線開啟、可用隨身碟轉交。
內含五個分頁：

| 分頁 | 內容 |
|---|---|
| 課程總覽 | 逐課卡片，可依評級／能力標籤／難易度／節奏／修課對象篩選與排序，附判斷理由 |
| 分布圖表 | 評級、能力標籤、各評分面向平均、難易度、課程節奏的分布 |
| 主題套裝 | 各套裝的修課路徑、適合對象、先修條件與學習目標 |
| AI 與真人一致性 | 逐欄一致率，並列同學兩人之間的一致率作為對照基準 |
| 資料表 | 完整欄位表格，可排序、可下載 CSV |

缺少的資料來源會自動略過該分頁（例如還沒跑過一致性比對，就不顯示該分頁的圖表）。

---

## 六、自行編修評分規準

`config\prompt.txt` 就是送給模型的評分規準，可用記事本直接編修。
改完建議先用 `--limit 1` 跑一門課確認輸出格式仍然通得過驗證。

若要改**輸出欄位**（新增、刪除或改名），需同步改 `config\fields.json`：

- `fields[]` 每一項定義一個欄位的型別（`number` / `choice` / `list` / `bool` / `text` / `dict`）
- `merge` 決定多次呼叫結果怎麼合併（`mean` / `majority` / `vote` / `any` / `longest`）
- `required: true` 的欄位缺少時，該次回應整筆作廢

兩份檔案要一起改 —— 規準要求模型填的欄位，必須和 `fields.json` 定義的一致。

---

## 七、常見問題

**沒有產生任何評分結果？**
先跑 `rate_courses --dry-run` 看「尚未備齊」幾門。
`merged\` 下每門課必須同時有 `.jpg` 與 `_analysis.txt` 兩個檔案，缺一不可。

**出現「找不到設定檔 config\prompt.txt」？**
`config\` 資料夾要和 exe 放在同一層，或設定 `MOOCS_CONFIG_DIR` 指到正確位置。

**模型回應格式不穩，某些課程沒被解析？**
- 降低 `--agents`（例如先試 1 次呼叫，確認單次輸出正常）
- 檢查 `config\prompt.txt` 結尾是否仍明確要求輸出 JSON
- 看 `failed.csv` 列出哪些課程未成功，可用 `--only` 單獨重跑

**儀表板打不開或資料是空的？**
- 確認 `analysis_results.json` 非空
- 確認 `dashboard_template.html` 存在且佔位符號（`__DATA__` 等）未被破壞

**花費超出預期？**
`--cost-cap` 是**估算值**，依 token 用量推算。建議第一次先 `--limit 5` 跑小批，
用實際花費回推整批規模後再放大。
