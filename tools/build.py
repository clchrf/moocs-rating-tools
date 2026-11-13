# -*- coding: utf-8 -*-
"""
tools/build.py
--------------
把工具打包成 Windows 單檔 exe，輸出到 release/，並另外產生 release/release-win.zip。

只打包「不需要外部執行檔」的三支工具：

    rate_courses.exe      LLM 評分（需 API 金鑰，不需 ffmpeg）
    build_bundles.exe     分級與主題套裝
    build_dashboard.exe   產生互動儀表板

前處理（preprocess_courses）與平臺抓取（fetch_courses）需要 ffmpeg／ffprobe
與 Playwright 瀏覽器核心，無法塞進單檔 exe，交付時請走 Python 路徑。

設定檔（config/、dashboard_template.html）**放在 exe 旁邊而不塞進 exe**，
使用者才能自行編修評分規準與參數。

使用方式:
    pip install -e .[build]
    python tools/build.py
    python tools/build.py --release-dir dist
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT_DIR / "release"
ZIP_NAME = "release-win.zip"

# PyInstaller 的暫存目錄放在系統 temp，不放專案內。
# 專案若位於 OneDrive／Dropbox 等同步資料夾，同步程式會在打包過程中鎖住
# localpycs 等暫存檔，造成 PermissionError: [WinError 5]。
_TAG = hashlib.sha1(str(ROOT_DIR).encode("utf-8")).hexdigest()[:8]
BUILD_ROOT = Path(tempfile.gettempdir()) / f"moocs-rating-build-{_TAG}"

TARGETS = (
    ("rate_courses.py", "rate_courses"),
    ("build_bundles.py", "build_bundles"),
    ("build_dashboard.py", "build_dashboard"),
)

# 與 exe 一同打包、但不塞進 exe 的檔案（使用者需要能編修）
PACKAGE_FILES = (
    "dashboard_template.html",
    ".env.example",
)
PACKAGE_DIRS = (
    "config",
)

# 被 exe import、但 PyInstaller 可能漏掉的本地模組
HIDDEN_IMPORTS = ("moocs_config", "moocs_core", "bundles_data")

# PyInstaller 會靜態掃到「函式內」的 import，因此 moocs_core 延遲載入的
# whisper / cv2 仍會被整包拉進來（whisper → torch，單檔就超過 380 MB）。
# 打包的三支工具不會走到抽幀與轉逐字稿的程式路徑，故明確排除。
# 排除後若誤呼叫到那些函式，會在執行時以 ImportError 明確報錯，不會靜默出錯。
EXCLUDES = (
    "whisper", "torch", "torchaudio", "torchvision",
    "cv2", "google", "google.genai",
    "triton", "numba", "llvmlite",
    "matplotlib", "scipy", "pandas",
    "tkinter", "PyQt5", "PySide2", "IPython", "notebook",
)


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "找不到 PyInstaller。請先安裝打包依賴：pip install -e .[build]"
        ) from exc


def run_pyinstaller(script: Path, exe_name: str, release_dir: Path,
                    build_root: Path = BUILD_ROOT) -> None:
    work_dir = build_root / exe_name / "work"
    spec_dir = build_root / exe_name / "spec"
    for d in (work_dir, spec_dir, release_dir):
        d.mkdir(parents=True, exist_ok=True)

    exe_path = release_dir / f"{exe_name}.exe"
    if exe_path.exists():
        exe_path.unlink()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--clean", "--noconfirm",
        "--name", exe_name,
        "--distpath", str(release_dir),
        "--workpath", str(work_dir),
        "--specpath", str(spec_dir),
        "--paths", str(ROOT_DIR),
    ]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(str(script))

    print(f"\n=== 打包 {exe_name} ===")
    subprocess.run(cmd, check=True)

    if not exe_path.exists():
        raise SystemExit(f"打包結束但找不到 {exe_path}")
    print(f"完成：{exe_path}（{exe_path.stat().st_size // 1024 // 1024} MB）")


def build_zip(release_dir: Path) -> Path:
    zip_path = release_dir / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()

    items: list[tuple[Path, str]] = []
    for _, exe_name in TARGETS:
        items.append((release_dir / f"{exe_name}.exe", f"{exe_name}.exe"))
    for name in PACKAGE_FILES:
        items.append((ROOT_DIR / name, name))
    for dname in PACKAGE_DIRS:
        d = ROOT_DIR / dname
        for f in sorted(d.rglob("*")):
            if f.is_file():
                items.append((f, str(Path(dname) / f.relative_to(d))))

    missing = [str(p) for p, _ in items if not p.exists()]
    if missing:
        raise SystemExit("打包交付包時缺少檔案：\n" + "\n".join(missing))

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path, arc in items:
            z.write(path, arcname=arc)

    print(f"\n交付包：{zip_path}（{zip_path.stat().st_size // 1024} KB，"
          f"{len(items)} 個檔案）")
    return zip_path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="打包 Windows exe 與交付 zip")
    ap.add_argument("--release-dir", type=Path, default=RELEASE_DIR,
                    help="exe 與 zip 的輸出目錄")
    ap.add_argument("--skip-exe", action="store_true",
                    help="只重新打包 zip，不重建 exe")
    ap.add_argument("--work-dir", type=Path, default=BUILD_ROOT,
                    help="PyInstaller 暫存目錄（預設在系統 temp，避開雲端同步資料夾）")
    ap.add_argument("--keep-build", action="store_true",
                    help="保留暫存目錄")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    if not args.skip_exe:
        ensure_pyinstaller()
        print(f"暫存目錄：{args.work_dir}")
        for script_name, exe_name in TARGETS:
            script = ROOT_DIR / script_name
            if not script.exists():
                raise SystemExit(f"找不到原始程式：{script}")
            run_pyinstaller(script, exe_name, args.release_dir, args.work_dir)

    build_zip(args.release_dir)

    if not args.keep_build and args.work_dir.exists():
        shutil.rmtree(args.work_dir, ignore_errors=True)

    print("\n打包完成。交付前請確認 zip 內不含 .env、cookies.txt、session.json。")


if __name__ == "__main__":
    main()
