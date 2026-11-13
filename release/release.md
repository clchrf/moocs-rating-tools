# Release

## v1.0.0（2026-11-13）

磨課師 AI 課程評分工具首次發布。

### 下載

- `release-win.zip` —— Windows 免安裝執行包

內含三支工具與可編修的設定檔：

```text
rate_courses.exe          LLM 評分
build_bundles.exe         分級與主題套裝
build_dashboard.exe       互動儀表板
dashboard_template.html   儀表板版面樣板
.env.example              環境變數範例
config/prompt.txt         評分規準（4,231 字）
config/fields.json        19 欄位定義與整合／驗證規則
config/pipeline.json      抽樣／抽幀／音訊／模型／門檻
config/course_ids.txt     課程 ID 清單
```

設定檔放在 exe **旁邊**而非塞進 exe，使用者可自行編修評分規準與參數。

### 不含在執行包內

`fetch_courses`（平臺抓取）與 `preprocess_courses`（影音前處理）需要
`ffmpeg`、`ffprobe` 與 Playwright 瀏覽器核心，無法打包成單檔 exe。
這兩步請走 Python 路徑，見 [example-full-run.md](../doc/example-full-run.md)。

### 安裝與使用說明

- 使用預編譯 exe + Windows：[doc/document.md](../doc/document.md)
- 使用 Python 從頭跑完整流程：[doc/example-full-run.md](../doc/example-full-run.md)

### 自行打包

```bash
pip install -e .[build]
python tools/build.py
```

### 本版重點

- 六支命令列工具取代原本 100 多支一次性腳本
- 評分規準、欄位定義、管線參數、課程 ID 全部外部化成設定檔
- 整合與驗證改由 `config/fields.json` 驅動，換領域不需改程式碼
- 新增單檔互動儀表板（可篩選、排序、看逐課判斷理由，可離線開啟）
- 新增 `failed.csv` 失敗清單與 `--dry-run` 空跑模式
- 憑證檔一律排除於版控之外

行為一致性：整合規則重構後，以 59 門課真實結果 × 4 種擾動（236 組）× 19 欄位
與舊版逐欄比對，差異 0 處；抽樣結果重現舊版數字（819 支 → 258 支 / 31.5%）。
