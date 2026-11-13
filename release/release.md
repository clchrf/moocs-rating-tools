# Release

## v1.0.0（2026-11-13）

磨課師 AI 課程評分工具首次發布。

### 下載

- **[release-win.zip](https://github.com/clchrf/moocs-rating-tools/releases/download/v1.0.0/release-win.zip)**（136 MB）—— Windows 免安裝執行包

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
`config/` 必須與 exe 放在同一層。

### 檔案校驗（SHA-256）

下載後建議核對，確認檔案完整且未被竄改：

```text
99f1e4566fec5f27d65fd7e2a12e4665e1b118440af2c0e67ac6c9aa9ce9c9d8  release-win.zip
dcf2e3396ddbd67586aebe5b843888b276fa5889e12ea5f04d9628a49ff97c43  rate_courses.exe
bc74d6d0fcb5dea8405d76f655895938983441609830c51529aaa6461b99caef  build_bundles.exe
c328256da9b979746f3c5d5f8370a9c846feca966927968c341379b073f9e2cd  build_dashboard.exe
```

Windows 核對方式：

```powershell
Get-FileHash .\release-win.zip -Algorithm SHA256
```

### 執行時的安全性提示

這些 exe **未經數位簽章**，Windows SmartScreen 可能顯示「已保護您的電腦」。
請先以上面的 SHA-256 核對檔案，確認無誤後選「其他資訊 → 仍要執行」。

不放心執行未簽章檔案的單位，請改走 Python 路徑（功能完全相同）：
見 [example-full-run.md](../doc/example-full-run.md)。

### 不含在執行包內

`fetch_courses`（平臺抓取）與 `preprocess_courses`（影音前處理）需要
`ffmpeg`、`ffprobe` 與 Playwright 瀏覽器核心，無法打包成單檔 exe。
這兩步請走 Python 路徑。

### 安裝與使用說明

- 使用預編譯 exe + Windows：[doc/document.md](../doc/document.md)
- 使用 Python 從頭跑完整流程：[doc/example-full-run.md](../doc/example-full-run.md)

### 自行打包

```bash
pip install -e .[build]
python tools/build.py                      # 三支全建
python tools/build.py --only build_dashboard   # 只重建一支
```

### 本版重點

- 六支命令列工具取代原本 100 多支一次性腳本
- 評分規準、欄位定義、管線參數、課程 ID 全部外部化成設定檔
- 整合與驗證改由 `config/fields.json` 驅動，換領域不需改程式碼
- 新增單檔互動儀表板（可篩選、排序、看逐課判斷理由，可離線開啟）
- 新增 `failed.csv` 失敗清單與 `--dry-run` 空跑模式
- 憑證檔一律排除於版控之外

### 驗證

行為一致性：整合規則重構後，以 59 門課真實結果 × 4 種擾動（236 組）× 19 欄位
與舊版逐欄比對，差異 0 處。

本執行包實測可重現下列數字（用 repo 內 `data/` 的資料即可自行驗證）：

| 項目 | 數值 |
|---|---|
| 課程 | 59 門 |
| 平臺影片 | 819 支 → 抽樣分析 258 支（31.5%）|
| 課程評級 | A+ 9 門 / A 18 門 / C 32 門 |
| 主題套裝 | 9 組 · 納入 27 門（自我檢核全通過）|
