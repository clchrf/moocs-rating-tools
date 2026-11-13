"""
磨課師 AI 課程評分核心函式庫（moocs_core）

相依關係：opencv / numpy / whisper 僅由前處理相關函式使用，改為函式內延遲載入，
因此 rate_courses、build_* 等工具不需安裝這些套件，也不會被打包進 exe。
修正：UnicodeDecodeError、結尾mp3損壞、英文語速異常節奏判斷
新增：三個平行 Claude Agent 自動分析，取代人工貼JSON步驟
"""

import json
import os
import re
import time
import csv
import glob
import asyncio
import base64
import subprocess
from pathlib import Path
from collections import Counter
from dotenv import load_dotenv

# Gemini 為舊版備援路徑，未安裝也能跑（目前走 MODEL_PRIORITY 的 Claude）
try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

import moocs_config as mc

# 載入 .env（專案目錄或執行檔旁；未找到則退回搜尋當前工作目錄）
_ENV_LOCAL = Path(__file__).resolve().parent / ".env"
if _ENV_LOCAL.exists():
    load_dotenv(_ENV_LOCAL)
else:
    load_dotenv()

# ==========================================
# SSL：moocs.moe.edu.tw 用 TWCA 政府根憑證（不在 certifi 內，但 Windows 信任）
# 用 truststore 改走 Windows 系統憑證庫，讓影片下載通過 TLS 驗證。
# conda 等沒裝 truststore 的環境會靜默略過，改用 download_video 內的 verify=False 備案。
# ==========================================
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

# ==========================================
# 找出可用的 ffmpeg/ffprobe（避開 conda 那個壞掉的 4.3.1）
# ==========================================
def _find_working_ffmpeg():
    """測試各候選 ffmpeg 能否真的編碼，回傳 (ffmpeg, ffprobe) 路徑"""
    candidates = []
    # 1. WinGet 系統版（已知正常）
    winget = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe")
    if os.path.exists(winget):
        candidates.append(winget)
    # 2. PATH 中非 conda 的 ffmpeg
    import shutil
    for p in (os.environ.get("PATH", "")).split(os.pathsep):
        cand = os.path.join(p, "ffmpeg.exe")
        if os.path.exists(cand) and "conda" not in cand.lower() and cand not in candidates:
            candidates.append(cand)
    # 3. 最後才用 PATH 預設（可能是 conda 的）
    candidates.append("ffmpeg")

    for ff in candidates:
        try:
            # 用 lavfi 產生 1 秒正弦波測試編碼成 mp3
            r = subprocess.run(
                [ff, '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                 '-ar', '16000', '-ac', '1', '-b:a', '32k', '-f', 'mp3', '-'],
                capture_output=True, timeout=30
            )
            if r.returncode == 0 and len(r.stdout) > 1000:
                # 推導同目錄的 ffprobe
                if ff == "ffmpeg":
                    fp = "ffprobe"
                else:
                    fp = os.path.join(os.path.dirname(ff), "ffprobe.exe")
                    if not os.path.exists(fp):
                        fp = "ffprobe"
                return ff, fp
        except Exception:
            continue
    return "ffmpeg", "ffprobe"   # 都失敗就用預設

FFMPEG, FFPROBE = _find_working_ffmpeg()

# ==========================================
# 設定區：全部來自 config/pipeline.json
# ==========================================
_CFG = mc.load_pipeline()

COOKIES_FILE          = _CFG["files"]["cookies"]
QUEUE_FILE            = _CFG["files"]["queue"]
RESULTS_JSON          = _CFG["files"]["results"]
OUTPUT_CSV            = _CFG["files"]["output_csv"]
MERGED_DIR            = _CFG["files"]["merged_dir"]
VIDEOS_DIR            = _CFG["files"]["videos_dir"]
AUDIO_DIR             = _CFG["files"]["audio_dir"]

VIDEOS_PER_COURSE     = _CFG["sampling"]["videos_per_course"]
MAX_SAMPLE            = _CFG["sampling"]["max_sample"]
FRAMES_PER_VIDEO      = _CFG["frames"]["frames_per_video"]
SKIP_SECONDS          = _CFG["frames"]["skip_seconds"]
MERGE_COLS            = _CFG["frames"]["merge_cols"]
AUDIO_SEGMENTS        = _CFG["audio"]["segments"]
AUDIO_SEGMENT_MINUTES = _CFG["audio"]["segment_minutes"]
WHISPER_MODEL         = _CFG["transcript"]["whisper_model"]
TRANSCRIPT_CHARS      = _CFG["transcript"]["truncate_chars"]

MODEL_PRIORITY        = _CFG["llm"]["model_priority"]
NUM_AGENTS            = _CFG["llm"]["num_agents"]
MAX_TOKENS            = _CFG["llm"]["max_tokens"]
COST_CAP              = float(os.getenv("COST_CAP", _CFG["llm"]["cost_cap_usd"]))
DAILY_LIMIT           = _CFG["limits"]["daily_limit"]

# 評分規準：外部設定檔 config/prompt.txt（換領域改這份檔，不需改程式碼）
SYSTEM_PROMPT = mc.load_prompt()

# ==========================================
# 下載影片（含重試）：支援 YouTube ID 和磨課師平台 MP4 URL
# ==========================================
def download_video(video_ref, out_dir, max_retries=3):
    """
    video_ref: YouTube ID（11碼）或磨課師 MP4 完整 URL
    """
    import urllib.parse, hashlib, requests as _req

    is_mp4_url = video_ref.startswith('http') and '.mp4' in video_ref.lower()

    if is_mp4_url:
        # 用 URL hash 當檔名（避免中文路徑問題）
        safe_name = hashlib.md5(video_ref.encode()).hexdigest()[:12]
        out_path = out_dir / f"mp4_{safe_name}.mp4"
        label = urllib.parse.unquote(video_ref.split('/')[-1])[:40]
    else:
        out_path = out_dir / f"{video_ref}.mp4"
        label = video_ref

    # 快取檔也要驗證完整性（壞檔就刪掉重抓）
    if out_path.exists() and out_path.stat().st_size > 10000:
        ok, _, _, why = verify_video(out_path)
        if ok:
            print(f"    ✅ 已存在：{label}")
            return str(out_path)
        print(f"    ⚠️ 快取損壞（{why}），重新下載：{label}")
        out_path.unlink(missing_ok=True)

    for attempt in range(max_retries):
        print(f"    ⬇️  下載：{label}（第{attempt+1}次）")

        if is_mp4_url:
            # 從 session.json 讀 cookies 做認證下載
            try:
                session_file = Path(r"C:\Users\User\OneDrive\Desktop\moocs\session.json")
                cookies = {}
                if session_file.exists():
                    with open(session_file, encoding='utf-8') as f:
                        sess = json.load(f)
                    for ck in sess.get('cookies', []):
                        cookies[ck['name']] = ck['value']

                # 需帶瀏覽器 User-Agent，否則 Apache 會擋掉 python-requests 預設 UA 回 403
                _get_kw = dict(cookies=cookies, stream=True, timeout=60,
                               headers={'Referer': 'https://moocs.moe.edu.tw/moocs/',
                                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                                                      'Chrome/120.0.0.0 Safari/537.36',
                                        'Accept': '*/*', 'Accept-Language': 'zh-TW,zh;q=0.9'})
                try:
                    r = _req.get(video_ref, **_get_kw)
                except _req.exceptions.SSLError:
                    # truststore 不在時的備案：moocs 用 TWCA 政府憑證，關閉驗證重抓
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    print("    ⚠️ TLS 驗證失敗（TWCA 根憑證），改用 verify=False 重抓")
                    r = _req.get(video_ref, verify=False, **_get_kw)
                if r.status_code == 200:
                    with open(out_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            f.write(chunk)
                else:
                    print(f"    ⚠️ HTTP {r.status_code}")
            except Exception as e:
                print(f"    ⚠️ 下載失敗：{e}")
        else:
            cmd = (
                f'yt-dlp.exe --cookies "{COOKIES_FILE}" '
                f'-f "best[height<=360]" '
                f'--no-warnings --no-progress '
                f'-o "{out_path}" '
                f'"https://www.youtube.com/watch?v={video_ref}"'
            )
            os.system(cmd)

        # 下載後驗證：確認不是壞檔才回傳
        if out_path.exists():
            ok, has_a, dur, why = verify_video(out_path)
            if ok:
                a_note = "" if has_a else "（無音軌，音訊將用備案）"
                print(f"    ✅ 下載完成（{dur:.0f}s）{a_note}")
                return str(out_path)
            print(f"    ⚠️ 下載的檔案有問題：{why}")
            out_path.unlink(missing_ok=True)

        print(f"    ⚠️ 失敗，{'重試...' if attempt < max_retries-1 else '放棄'}")
        time.sleep(3)
    print(f"    ❌ 無法取得可用影片：{label}")
    return None

# ==========================================
# 影片完整性驗證（避免分析到壞檔）
# ==========================================
def verify_video(video_path, min_duration=10):
    """檢查影片是否可用。回傳 (ok, has_audio, duration, 問題說明)"""
    p = Path(video_path)
    if not p.exists():
        return False, False, 0, "檔案不存在"
    if p.stat().st_size < 50 * 1024:
        return False, False, 0, f"檔案過小({p.stat().st_size//1024}KB)"
    try:
        r = subprocess.run(
            [FFPROBE, '-v', 'quiet', '-print_format', 'json',
             '-show_format', '-show_streams', str(p.resolve())],
            capture_output=True, encoding='utf-8', errors='ignore', timeout=30
        )
        d = json.loads(r.stdout)
        streams = d.get('streams', [])
        has_v = any(s.get('codec_type') == 'video' for s in streams)
        has_a = any(s.get('codec_type') == 'audio' for s in streams)
        dur = float(d.get('format', {}).get('duration', 0) or 0)
        if not has_v:
            return False, has_a, dur, "無視訊軌"
        if dur and dur < min_duration:
            return False, has_a, dur, f"影片過短({dur:.0f}s)"
        # 有視訊軌、長度足夠就算可用（無音軌仍可分析畫面，音訊另有備案）
        return True, has_a, dur, "" if has_a else "無音軌(將用備案取音訊)"
    except Exception as e:
        return False, False, 0, f"無法解析({str(e)[:40]})"

# ==========================================
# 取得影片長度（修正 UnicodeDecodeError）
# ==========================================
def get_video_duration(video_path):
    """取得影片總秒數，修正 Windows cp950 編碼問題"""
    try:
        result = subprocess.run(
            [FFPROBE, '-v', 'quiet', '-print_format', 'json',
             '-show_format', str(Path(video_path).resolve())],
            capture_output=True,
            encoding='utf-8',      # 強制 utf-8
            errors='ignore',       # 忽略無法解碼的字元
            timeout=30
        )
        info = json.loads(result.stdout)
        return float(info['format']['duration'])
    except Exception as e:
        print(f"    ⚠️ 無法取得影片長度：{e}，使用預設600秒")
        return 600.0

# ==========================================
# 平均抽樣音訊（修正結尾段損壞問題）
# ==========================================
def extract_audio_segments(video_path, out_dir, segment_minutes=2,
                           num_segments=3, skip_seconds=10):
    video_id = Path(video_path).stem
    total_seconds = get_video_duration(video_path)
    segment_duration = segment_minutes * 60

    # 計算可用範圍
    usable_start = skip_seconds
    usable_end = total_seconds - skip_seconds
    usable_duration = usable_end - usable_start

    # 影片太短時直接用全段
    if usable_duration <= segment_duration:
        usable_start = 0
        usable_end = total_seconds
        usable_duration = total_seconds

    # 計算每段起始時間，確保不超出影片長度
    start_times = []
    for i in range(num_segments):
        start = usable_start + (usable_duration * i / num_segments)
        # 確保這段音訊不會超出影片結尾
        if start + segment_duration > total_seconds:
            start = max(0, total_seconds - segment_duration)
        start_times.append(int(start))

    audio_paths = []
    labels = ['start', 'mid', 'end']

    # === 準備音源檔（切片段之前先確保有一個含音訊的來源）===
    # 先檢查影片檔本身有無音軌；無音軌（YouTube 純視訊格式）就下載完整音訊
    yt_id = Path(video_path).stem
    source_audio = str(Path(video_path).resolve())  # 預設用影片檔
    if not _has_audio_stream(video_path) and len(yt_id) == 11:
        full_audio = out_dir / f"{yt_id}_full_audio.mp3"
        if not (full_audio.exists() and full_audio.stat().st_size > 10000):
            print(f"    ⬇️ 下載完整音訊（影片無音軌）...")
            dl_cmd = (
                f'yt-dlp.exe --cookies "{COOKIES_FILE}" '
                f'-f bestaudio --extract-audio --audio-format mp3 '
                f'--no-warnings --no-progress '
                f'-o "{full_audio}" '
                f'"https://www.youtube.com/watch?v={yt_id}"'
            )
            os.system(dl_cmd)
        if full_audio.exists() and full_audio.stat().st_size > 10000:
            source_audio = str(full_audio.resolve())
        else:
            print(f"    ❌ 無法取得音訊，跳過此影片音訊分析")
            return audio_paths

    for idx, start in enumerate(start_times):
        label = labels[idx] if idx < 3 else f'seg{idx+1}'
        audio_path = out_dir / f"{video_id}_seg{idx}_{label}.mp3"

        # 快取檢查（同時確認檔案不是空的）
        if audio_path.exists():
            if audio_path.stat().st_size > 1024:  # 至少 1KB
                print(f"    ✅ 音訊快取：{label}段")
                audio_paths.append(str(audio_path))
                continue
            else:
                audio_path.unlink()

        # 計算實際可提取的秒數（不超出影片）
        actual_duration = min(segment_duration, total_seconds - start)
        if actual_duration < 10:
            print(f"    ⚠️ 影片剩餘不足10秒，跳過{label}段")
            continue

        print(f"    🎵 提取音訊：{label}（{start}s 起，{int(actual_duration)}s）")

        # 兩種 seek 方式都試：先 accurate seek（v4 可靠做法），再 fast seek
        seek_variants = [
            # accurate seek：-i 在 -ss 前（解碼到指定點，最可靠）
            [FFMPEG, '-i', source_audio, '-ss', str(start),
             '-t', str(actual_duration), '-vn',
             '-ar', '16000', '-ac', '1', '-b:a', '32k', '-y',
             str(audio_path.resolve())],
            # fast seek：-ss 在 -i 前（快，但少數檔案會空）
            [FFMPEG, '-ss', str(start), '-i', source_audio,
             '-t', str(actual_duration), '-vn',
             '-ar', '16000', '-ac', '1', '-b:a', '32k', '-y',
             str(audio_path.resolve())],
        ]

        success = False
        last_err = ''
        # 重試 3 輪（OneDrive 同步等暫時性鎖檔，幾秒後通常解除）
        for retry in range(3):
            for cmd in seek_variants:
                proc = subprocess.run(cmd, capture_output=True, encoding='utf-8',
                                      errors='ignore', timeout=120)
                if audio_path.exists() and audio_path.stat().st_size > 1024:
                    size_kb = audio_path.stat().st_size // 1024
                    tag = '✅ 完成' if retry == 0 else f'✅ 完成(重試{retry})'
                    print(f"    {tag}（{size_kb}KB）")
                    audio_paths.append(str(audio_path))
                    success = True
                    break
                if audio_path.exists():
                    audio_path.unlink()
                last_err = (proc.stderr or '')[-150:].strip().split('\n')[-1]
            if success:
                break
            time.sleep(2)  # 等暫時性鎖檔解除再重試

        if not success:
            # === 第二方案 A：把整條音軌完整解碼成一個檔，再從那檔切 ===
            # 只要影片真的有音軌，完整解碼絕不會「nothing encoded」，可解決 seek 失敗
            local_full = out_dir / f"{video_id}_fulltrack.mp3"
            if not (local_full.exists() and local_full.stat().st_size > 10000):
                print(f"    🔧 第二方案：完整解碼音軌...")
                subprocess.run(
                    [FFMPEG, '-i', source_audio, '-vn',
                     '-ar', '16000', '-ac', '1', '-b:a', '32k', '-y',
                     str(local_full.resolve())],
                    capture_output=True, encoding='utf-8', errors='ignore', timeout=300
                )
            if local_full.exists() and local_full.stat().st_size > 10000:
                subprocess.run(
                    [FFMPEG, '-i', str(local_full.resolve()), '-ss', str(start),
                     '-t', str(actual_duration), '-ar', '16000', '-ac', '1',
                     '-b:a', '32k', '-y', str(audio_path.resolve())],
                    capture_output=True, encoding='utf-8', errors='ignore', timeout=120
                )
                if audio_path.exists() and audio_path.stat().st_size > 1024:
                    print(f"    ✅ 第二方案完成（{audio_path.stat().st_size//1024}KB）")
                    audio_paths.append(str(audio_path))
                    success = True

        if not success:
            # === 第二方案 B：YouTube 影片改用 yt-dlp 重抓音訊 ===
            if len(yt_id) == 11:
                full_audio = out_dir / f"{yt_id}_full_audio.mp3"
                if not (full_audio.exists() and full_audio.stat().st_size > 10000):
                    print(f"    ⬇️ 第二方案：yt-dlp 重抓音訊...")
                    os.system(
                        f'yt-dlp.exe --cookies "{COOKIES_FILE}" '
                        f'-f bestaudio --extract-audio --audio-format mp3 '
                        f'--no-warnings --no-progress -o "{full_audio}" '
                        f'"https://www.youtube.com/watch?v={yt_id}"'
                    )
                if full_audio.exists() and full_audio.stat().st_size > 10000:
                    subprocess.run(
                        [FFMPEG, '-i', str(full_audio.resolve()), '-ss', str(start),
                         '-t', str(actual_duration), '-vn', '-ar', '16000', '-ac', '1',
                         '-b:a', '32k', '-y', str(audio_path.resolve())],
                        capture_output=True, encoding='utf-8', errors='ignore', timeout=120
                    )
                    if audio_path.exists() and audio_path.stat().st_size > 1024:
                        print(f"    ✅ 第二方案完成（{audio_path.stat().st_size//1024}KB）")
                        audio_paths.append(str(audio_path))
                        success = True

        if not success:
            print(f"    ❌ 提取失敗（所有方案都試過）：{last_err}")

    return audio_paths


def _has_audio_stream(video_path):
    """用 ffprobe 檢查影片是否含音軌"""
    try:
        r = subprocess.run(
            [FFPROBE, '-v', 'quiet', '-select_streams', 'a',
             '-show_entries', 'stream=index', '-of', 'csv=p=0',
             str(Path(video_path).resolve())],
            capture_output=True, encoding='utf-8', errors='ignore', timeout=30
        )
        return bool((r.stdout or '').strip())
    except Exception:
        return False

# ==========================================
# Whisper 辨識（自動偵測語言）
# ==========================================
def transcribe_audio(audio_path, model):
    if not audio_path:
        return "", {}, "zh"

    cache_path = Path(audio_path).with_suffix('.json')
    if cache_path.exists():
        try:
            with open(str(cache_path), 'r', encoding='utf-8') as f:
                cached = json.load(f)
            lang = cached.get('language', 'zh')
            print(f"    ✅ 讀取快取（{lang}）")
            return cached.get('text', ''), cached.get('stats', {}), lang
        except:
            pass

    print(f"    🎤 Whisper 辨識（自動偵測語言）...")
    try:
        detect_result = model.transcribe(
            audio_path, language=None, verbose=False, fp16=False
        )
        detected_lang = detect_result.get('language', 'zh')
        print(f"    🌐 偵測語言：{detected_lang}")

        if detected_lang != 'zh':
            result = model.transcribe(
                audio_path, language=detected_lang, verbose=False, fp16=False
            )
        else:
            result = detect_result

        text = result['text'].strip()
        segments = result.get('segments', [])
        stats = analyze_speech(segments, text, detected_lang)
        print(f"    ✅ {len(text)}字，語速{stats.get('wpm', 0):.0f}字/分（{detected_lang}）")

        try:
            with open(str(cache_path), 'w', encoding='utf-8') as f:
                json.dump({
                    'text': text, 'stats': stats, 'language': detected_lang
                }, f, ensure_ascii=False)
        except:
            pass

        return text, stats, detected_lang
    except Exception as e:
        print(f"    ❌ 辨識失敗：{e}")
        return "", {}, "zh"

# ==========================================
# 語速分析（中英文分開，修正異常判斷）
# ==========================================
def analyze_speech(segments, text, language='zh'):
    if not segments:
        return {}

    total_time = segments[-1]['end'] if segments else 0
    char_count = len(text.replace(' ', ''))
    wpm = (char_count / total_time * 60) if total_time > 0 else 0

    pauses = [
        segments[i]['start'] - segments[i-1]['end']
        for i in range(1, len(segments))
        if segments[i]['start'] - segments[i-1]['end'] > 2.0
    ]
    pause_count = len(pauses)
    pause_ratio = sum(pauses) / total_time if total_time > 0 else 0

    # 異常偵測（英文放寬到500字/分）
    is_abnormal = False
    if language == 'zh' and (wpm > 400 or wpm < 30):
        is_abnormal = True
    if language == 'en' and (wpm > 500 or wpm < 20):
        is_abnormal = True

    # 節奏判斷
    if is_abnormal:
        # 英文異常不亂猜，直接標記需人工確認
        rhythm = "需人工確認"
    elif language == 'zh':
        if wpm < 120 and pause_ratio * 100 > 40:
            rhythm = "拖泥帶水"
        elif wpm < 200 or pause_ratio * 100 > 20:
            rhythm = "需開倍速"
        else:
            rhythm = "精煉流暢"
    else:  # 英文
        if wpm < 80 and pause_ratio * 100 > 40:
            rhythm = "拖泥帶水"
        elif wpm < 120 or pause_ratio * 100 > 20:
            rhythm = "需開倍速"
        else:
            rhythm = "精煉流暢"

    return {
        'wpm': wpm,
        'rhythm': rhythm,
        'pause_count': pause_count,
        'pause_ratio': round(pause_ratio * 100, 1),
        'total_seconds': round(total_time, 1),
        'char_count': char_count,
        'language': language,
        'is_abnormal': is_abnormal
    }

# ==========================================
# OpenCV 抽幀（跳過片頭片尾、黑畫面）
# ==========================================
def extract_frames(video_path, num_frames, skip_seconds=10):
    import cv2   # 延遲載入：只有前處理需要 opencv
    frames = []
    cap = cv2.VideoCapture(str(Path(video_path).resolve()))
    if not cap.isOpened():
        return frames

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    skip_frames = int(skip_seconds * fps)
    usable_start = skip_frames
    usable_end = total - skip_frames
    usable_range = usable_end - usable_start

    if usable_range <= 0:
        usable_start, usable_end, usable_range = 0, total, total

    positions = [
        usable_start + int(usable_range * i / (num_frames + 1))
        for i in range(1, num_frames + 1)
    ]

    prev = None
    for pos in positions:
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ret, frame = cap.read()
        if not ret:
            continue
        # 跳過黑畫面
        if cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean() < 10:
            continue
        if prev is not None:
            diff = cv2.absdiff(
                cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
            ).mean() / 255.0
            if diff < 0.05:
                for offset in range(1, 30):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, pos + offset * 15)
                    ret2, frame2 = cap.read()
                    if not ret2:
                        break
                    if cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY).mean() < 10:
                        continue
                    if cv2.absdiff(
                        cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY),
                        cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
                    ).mean() / 255.0 > 0.05:
                        frame = frame2
                        break
        frames.append(frame)
        prev = frame

    cap.release()
    return frames

# ==========================================
# 合併截圖
# ==========================================
def merge_frames(frames, output_path, cols=5):
    import cv2          # 延遲載入：只有前處理需要
    import numpy as np  # 同上
    if not frames:
        return False
    resized = [cv2.resize(f, (384, 216)) for f in frames]
    while len(resized) % cols != 0:
        resized.append(np.zeros_like(resized[0]))
    rows = [np.hstack(resized[i:i+cols]) for i in range(0, len(resized), cols)]
    grid = np.vstack(rows)
    _, buf = cv2.imencode('.jpg', grid, [cv2.IMWRITE_JPEG_QUALITY, 85])
    with open(str(output_path), 'wb') as f:
        f.write(buf.tobytes())
    return Path(output_path).exists()

# ==========================================
# Claude API：三個平行 Agent
# ==========================================
# Anthropic Claude（見 MODEL_PRIORITY）；三個獨立 agent = 對同一輸入呼叫三次
import anthropic as _anthropic

_anthropic_client = None
_exhausted = set()            # 已停用的 (key, model) 組合（餘額用完時）
# 累計用量，用來估算花費（盯住 $10 預算）
_usage_acc = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
# Sonnet 4.6 單價（USD / 百萬 token）：input 3、output 15、cache 讀 0.3、cache 寫 3.75
_PRICE = _CFG["llm"]["price_per_mtok"]

def _load_api_keys():
    """讀取 ANTHROPIC_API_KEY（單把）。"""
    k = os.getenv("ANTHROPIC_API_KEY")
    if not k or not k.strip():
        raise ValueError("找不到 ANTHROPIC_API_KEY，請在 .env 設定")
    return [k.strip()]

def _get_client(api_key):
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = _anthropic.Anthropic(api_key=api_key)
    return _anthropic_client

def encode_image(image_path):
    with open(image_path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")

def estimated_cost():
    """目前累計的估算花費（USD）。"""
    return sum(_usage_acc[k] * _PRICE[k] for k in _usage_acc) / 1_000_000

def _try_generate(api_key, model, img_bytes, text_content):
    """單次呼叫 Claude；回傳 (text, status)。status: 'ok' | 'exhausted' | 'rate' | 'error'
    SYSTEM_PROMPT 用 prompt 快取（所有呼叫共用、只算一次），大幅省錢。
    本模型不接受 temperature / thinking budget 參數（會回 400），故不帶。"""
    try:
        client = _get_client(api_key)
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": text_content},
            ]}],
        )
        u = resp.usage
        _usage_acc["in"] += getattr(u, "input_tokens", 0) or 0
        _usage_acc["out"] += getattr(u, "output_tokens", 0) or 0
        _usage_acc["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        _usage_acc["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return text, 'ok'
    except _anthropic.RateLimitError as e:
        retry = 30
        try:
            retry = int(e.response.headers.get("retry-after", "30")) + 2
        except Exception:
            pass
        return retry, 'rate'
    except _anthropic.APIStatusError as e:
        s = str(e)
        if 'credit balance' in s.lower() or 'insufficient' in s.lower():
            return None, 'exhausted'   # 餘額用完 → 停
        return s[:120], 'error'
    except Exception as e:
        return str(e)[:120], 'error'

async def call_agent(image_path, text_content, agent_id):
    """每個 agent 優先用自己那把 key；遇到額度用完就換 key/降級模型。"""
    keys = _load_api_keys()
    loop = asyncio.get_event_loop()

    def _call():
        img_bytes = open(image_path, "rb").read()
        # 這個 agent 的 key 優先順序：先自己的，再其他把
        start = (agent_id - 1) % len(keys)
        ordered_keys = keys[start:] + keys[:start]

        # 依 MODEL_PRIORITY 順序嘗試每把 key
        for model in MODEL_PRIORITY:
            for api_key in ordered_keys:
                if (api_key, model) in _exhausted:
                    continue
                for rate_retry in range(4):
                    res, status = _try_generate(api_key, model, img_bytes, text_content)
                    if status == 'ok':
                        return res
                    if status == 'exhausted':
                        _exhausted.add((api_key, model))
                        break  # 換下一把 key
                    if status == 'rate':
                        wait = res
                        print(f"    Agent {agent_id} 限流，等待{wait}秒...")
                        time.sleep(wait)
                        continue
                    # error
                    print(f"    Agent {agent_id} 錯誤：{res}")
                    time.sleep(3)
                    break
        print(f"    Agent {agent_id}：所有 key/模型額度都用完")
        return None

    result_text = await loop.run_in_executor(None, _call)
    return agent_id, result_text

async def run_three_agents(image_path, text_content):
    print(f"    Claude（{MODEL_PRIORITY[0]}）分析中（{NUM_AGENTS} agent）...")
    results = []
    for i in range(1, NUM_AGENTS + 1):
        result = await call_agent(image_path, text_content, i)
        results.append(result)
        if i < NUM_AGENTS:
            time.sleep(2)
    print(f"    💰 累計估算花費約 ${estimated_cost():.2f}（in {_usage_acc['in']} / out {_usage_acc['out']} / cache讀 {_usage_acc['cache_read']}）")
    return results

# ==========================================
# 整合三個 Agent 結果
# ==========================================
def merge_numeric(values):
    valid = [v for v in values if isinstance(v, (int, float)) and 1 <= v <= 5]
    if not valid:
        return None, "⚠️無有效值"
    avg = round(sum(valid) / len(valid))
    diverge = max(valid) - min(valid) >= 2
    return avg, "⚠️分歧大" if diverge else ""

def merge_choice(values, valid_options):
    valid = [v for v in values if v in valid_options]
    if not valid:
        return None, "⚠️無有效值"
    counts = Counter(valid)
    top = counts.most_common(1)[0]
    flag = "" if top[1] >= 2 else "⚠️三者分歧"
    return top[0], flag

def merge_bool(values):
    valid = [v for v in values if isinstance(v, bool)]
    if not valid:
        return False, "⚠️無有效值"
    return sum(valid) >= 2, ""

def merge_list(lists, min_votes=2):
    all_items = [item for lst in lists if lst for item in lst]
    counts = Counter(all_items)
    return [item for item, count in counts.items() if count >= min_votes]

def _to_text(t):
    """把 dict（逐面向原因）攤平成可讀字串，其他型別轉字串"""
    if t is None:
        return ""
    if isinstance(t, dict):
        return '\n'.join(f'{k}：{_to_text(v)}' for k, v in t.items())
    if isinstance(t, (list, tuple)):
        return '；'.join(_to_text(x) for x in t)
    return str(t)

def merge_text(texts):
    norm = [_to_text(t) for t in texts]
    valid = [t for t in norm if t and len(t) > 10]
    if not valid:
        return ""
    return max(valid, key=len)

def merge_agent_results(agent_results):
    """解析並整合多次獨立呼叫的結果，回傳 (merged_result, diverge_fields)。

    整合規則（平均／多數決／票數門檻／任一即真／取最長）定義於 config/fields.json，
    由 moocs_config.merge_results 執行。**純 Python 規則，不呼叫模型。**
    """
    parsed = []
    for agent_id, raw_text in agent_results:
        if isinstance(raw_text, Exception) or raw_text is None:
            print(f"    ⚠️ Agent {agent_id} 無結果，跳過")
            continue
        result = parse_and_validate_json(raw_text)
        if result:
            parsed.append(result)
            print(f"    ✅ Agent {agent_id} 解析成功")
        else:
            print(f"    ❌ Agent {agent_id} JSON 格式錯誤")

    if not parsed:
        print(f"  ❌ {len(agent_results)} 次呼叫均失敗")
        return None, []
    if len(parsed) == 1:
        print("  ⚠️ 只有一次呼叫成功，直接採用")

    return mc.merge_results(parsed)

# ==========================================
# 章節感知智慧抽樣
# ==========================================
def select_videos_smart(course: dict, max_per_chapter: int = None,
                        short_course_threshold: int = None,
                        max_sample: int = None) -> list[str]:
    """
    章節感知抽樣。統一處理 YouTube（videoIds）和 MP4（mp4Urls）。
    短課（≤ threshold 支）全部分析；長課每章節取前 1 + 中 1。
    最終以 max_sample 為硬上限，超過則均勻抽樣。

    三個參數預設皆取自 config/pipeline.json 的 sampling 區塊。
    """
    _s = _CFG["sampling"]
    if max_per_chapter is None:
        max_per_chapter = _s["max_per_chapter"]
    if short_course_threshold is None:
        short_course_threshold = _s["short_course_threshold"]
    if max_sample is None:
        max_sample = _s["max_sample"]
    MAX_SAMPLE = max_sample

    def cap(vids):
        if len(vids) <= MAX_SAMPLE:
            return vids
        step = len(vids) / MAX_SAMPLE
        return [vids[int(i * step)] for i in range(MAX_SAMPLE)]

    yt_ids = course.get('videoIds', [])
    mp4_urls = course.get('mp4Urls', [])
    chapters = course.get('chapters', [])

    # 合併所有影片（YouTube 優先，同一章節 YT+MP4 都用）
    def ch_all(ch):
        return ch.get('videoIds', []) + ch.get('mp4Urls', [])

    all_vids = yt_ids + mp4_urls

    if not all_vids:
        return []

    # 短課：全部分析
    if len(all_vids) <= short_course_threshold:
        return all_vids

    # 有章節結構
    if chapters:
        selected, seen = [], set()
        for ch in chapters:
            ch_vids = ch_all(ch)
            if not ch_vids:
                continue
            picks = []
            if ch_vids[0] not in seen:
                picks.append(ch_vids[0])
                seen.add(ch_vids[0])
            if len(ch_vids) > 2:
                mid = ch_vids[len(ch_vids) // 2]
                if mid not in seen:
                    picks.append(mid)
                    seen.add(mid)
            selected.extend(picks[:max_per_chapter])
        if selected:
            return cap(selected)

    # 無章節結構：均勻抽取
    n = max(5, min(MAX_SAMPLE, len(all_vids) // 3))
    step = len(all_vids) / n
    return cap([all_vids[int(i * step)] for i in range(n)])


def select_videos(video_ids, n):
    """向後相容：moocs_queue.json 只有 videoIds 無章節時使用"""
    if len(video_ids) <= n:
        return video_ids
    step = len(video_ids) // n
    return [video_ids[i * step] for i in range(n)]

# ==========================================
# 存 CSV
# ==========================================
def save_csv(all_results):
    fieldnames = [
        '課程名稱', '課程語言',
        '能力A', '能力B', '能力C', '能力D',
        '難易度', '通識素養型', '跨域應用型', '技術實作型',
        '關鍵字', '細項代碼', '課程摘要',
        '先備知識', '對應職務角色', '對應職場技能',
        '趣味度', '內容深度', '實作程度', '結構清晰度', '應用廣度',
        '推薦度', '評分原因', '推薦度原因',
        '課程節奏', '課程節奏原因',
        '平均語速(字/分)', '停頓次數', '停頓比例(%)', '語速異常',
        '能力標籤原因', '難易度原因', '修課對象原因', '先備職場原因',
        'Agent分歧欄位', '分析時間'
    ]
    with open(OUTPUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_results:
            result = r.get('result')
            speech = r.get('speech_stats', {})
            if not result:
                writer.writerow({
                    '課程名稱': r['name'],
                    '課程語言': speech.get('language', ''),
                    '課程摘要': '（未分析）',
                    'Agent分歧欄位': '、'.join(r.get('diverge_fields', [])),
                    '分析時間': r.get('timestamp', '')
                })
                continue
            tags = result.get('能力標籤', [])
            reasons = result.get('判斷原因', {})
            writer.writerow({
                '課程名稱': r['name'],
                '課程語言': speech.get('language', ''),
                '能力A': '✓' if 'A' in tags else '',
                '能力B': '✓' if 'B' in tags else '',
                '能力C': '✓' if 'C' in tags else '',
                '能力D': '✓' if 'D' in tags else '',
                '難易度': result.get('難易度', ''),
                '通識素養型': '✓' if result.get('通識素養型') else '',
                '跨域應用型': '✓' if result.get('跨域應用型') else '',
                '技術實作型': '✓' if result.get('技術實作型') else '',
                '關鍵字': '、'.join(result.get('關鍵字', [])),
                '細項代碼': '、'.join(result.get('細項代碼', [])),
                '課程摘要': result.get('課程摘要', ''),
                '先備知識': '、'.join(result.get('先備知識', [])),
                '對應職務角色': '、'.join(result.get('對應職務角色', [])),
                '對應職場技能': '、'.join(result.get('對應職場技能', [])),
                '先備職場原因': reasons.get('先備職場原因', ''),
                '趣味度': str(result.get('趣味度', '')),
                '內容深度': str(result.get('內容深度', '')),
                '實作程度': str(result.get('實作程度', '')),
                '結構清晰度': str(result.get('結構清晰度', '')),
                '應用廣度': str(result.get('應用廣度', '')),
                '推薦度': str(result.get('推薦度', '')),
                '評分原因': reasons.get('評分原因', ''),
                '推薦度原因': reasons.get('推薦度原因', ''),
                '課程節奏': result.get('課程節奏', '') or speech.get('rhythm', ''),
                '課程節奏原因': reasons.get('課程節奏原因', ''),
                '平均語速(字/分)': str(round(speech.get('wpm', 0))),
                '停頓次數': str(speech.get('pause_count', '')),
                '停頓比例(%)': str(speech.get('pause_ratio', '')),
                '語速異常': '⚠️需確認' if speech.get('is_abnormal') else '',
                '能力標籤原因': reasons.get('能力標籤原因', ''),
                '難易度原因': reasons.get('難易度原因', ''),
                '修課對象原因': reasons.get('修課對象原因', ''),
                'Agent分歧欄位': '、'.join(r.get('diverge_fields', [])),
                '分析時間': r.get('timestamp', '')
            })
    print(f"  ✅ CSV 已更新：{OUTPUT_CSV}")

# ==========================================
# 驗證 JSON 格式
# ==========================================
def parse_and_validate_json(raw_text, verbose=True):
    """解析並驗證單一次模型回應。規則定義於 config/fields.json。

    格式不合回傳 None —— 整筆作廢重跑，不做局部修補。
    """
    return mc.validate_result(raw_text, verbose=verbose)

# ==========================================
# 主程式
# ==========================================
def main():
    print("🚀 磨課師課程分析程式 Final v5（三Agent自動分析）")
    print("="*50)

    if not Path(QUEUE_FILE).exists():
        print(f"❌ 找不到 {QUEUE_FILE}！請先用插件掃描課程")
        return
    if not Path(COOKIES_FILE).exists():
        print(f"❌ 找不到 {COOKIES_FILE}！")
        return

    vid_dir = Path("videos")
    aud_dir = Path("audio")
    for d in [vid_dir, Path("merged"), aud_dir]:
        d.mkdir(exist_ok=True)

    import whisper   # 延遲載入：只有前處理需要
    print(f"⏳ 載入 Whisper {WHISPER_MODEL} 模型...")
    whisper_model = whisper.load_model(WHISPER_MODEL)
    print(f"✅ Whisper 載入完成\n")

    with open(QUEUE_FILE, 'r', encoding='utf-8') as f:
        queue = json.load(f)
    print(f"✅ 讀取到 {len(queue)} 門課\n")

    # 跨天接續：載入已完成的結果，跳過已分析成功的課
    all_results = []
    done_names = set()
    if Path(RESULTS_JSON).exists():
        try:
            with open(RESULTS_JSON, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
            done_names = {r['name'] for r in all_results if r.get('result')}
            print(f"📂 已完成 {len(done_names)} 門課，本次將接續未完成的\n")
        except Exception:
            all_results = []

    analyzed_today = 0   # 本次新分析的課數

    for i, course in enumerate(queue):
        course_name = course['courseName']

        # 已成功分析過 → 跳過
        if course_name in done_names:
            print(f"[{i+1}/{len(queue)}] ⏭️  已完成，跳過：{course_name}")
            continue

        # 達到每日上限 → 停止
        if DAILY_LIMIT and analyzed_today >= DAILY_LIMIT:
            print(f"\n🛑 已達今日上限 {DAILY_LIMIT} 門，剩下的明天再跑（額度會重置）")
            print(f"   進度：{len(done_names)}/{len(queue)} 門完成")
            break

        course_desc = course.get('courseDesc', '')
        video_ids = course.get('videoIds', [])
        mp4_urls  = course.get('mp4Urls', [])
        all_vids  = video_ids + mp4_urls

        print(f"{'='*50}")
        print(f"[{i+1}/{len(queue)}] 📚 {course_name}")
        if mp4_urls:
            print(f"  📹 YouTube:{len(video_ids)} 支 | MP4:{len(mp4_urls)} 支")

        if not all_vids:
            print(f"  ⚠️ 無影片，跳過")
            all_results.append({
                'name': course_name, 'result': None, 'diverge_fields': [],
                'speech_stats': {}, 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            })
            save_csv(all_results)
            continue

        # 章節感知智慧抽樣
        selected = select_videos_smart(course)
        n_chapters = len(course.get('chapters', []))
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', course_name)[:50]
        analysis_path = Path("merged") / f"{safe_name}_analysis.txt"
        print(f"  全部影片：{len(all_vids)} 支（YT:{len(video_ids)} MP4:{len(mp4_urls)}）| 章節：{n_chapters} | 本次分析：{len(selected)} 支")
        all_frames = []
        all_transcripts = []
        all_speech_stats = []
        detected_languages = []

        for j, vid_id in enumerate(selected):
            print(f"\n  影片 {j+1}/{len(selected)}：{vid_id}")
            vid_path = download_video(vid_id, vid_dir)
            if not vid_path:
                continue

            audio_paths = extract_audio_segments(
                vid_path, aud_dir, AUDIO_SEGMENT_MINUTES,
                AUDIO_SEGMENTS, SKIP_SECONDS
            )

            for audio_path in audio_paths:
                transcript, stats, lang = transcribe_audio(audio_path, whisper_model)
                if transcript:
                    all_transcripts.append(transcript)
                if stats:
                    all_speech_stats.append(stats)
                if lang:
                    detected_languages.append(lang)

            print(f"    🎬 抽幀中...")
            frames = extract_frames(vid_path, FRAMES_PER_VIDEO, SKIP_SECONDS)
            all_frames.extend(frames)
            print(f"    ✅ 抽取 {len(frames)} 幀")

        if not all_frames:
            print(f"  ❌ 沒有截圖，跳過")
            all_results.append({
                'name': course_name, 'result': None, 'diverge_fields': [],
                'speech_stats': {}, 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            })
            continue

        merged_path = Path("merged") / f"{safe_name}.jpg"
        ok = merge_frames(all_frames, merged_path)
        if not ok:
            print(f"  ❌ 合併圖建立失敗")
            all_results.append({
                'name': course_name, 'result': None, 'diverge_fields': [],
                'speech_stats': {}, 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            })
            continue

        size_kb = merged_path.stat().st_size // 1024
        print(f"\n  🖼️  合併完成：{len(all_frames)} 幀（{size_kb}KB）")

        # 計算平均語速
        main_lang = (
            max(set(detected_languages), key=detected_languages.count)
            if detected_languages else 'zh'
        )
        avg_stats = {}
        if all_speech_stats:
            # 優先用正常數據，若全部異常才用全部
            valid_stats = [s for s in all_speech_stats if not s.get('is_abnormal')]
            use_stats = valid_stats if valid_stats else all_speech_stats
            has_abnormal = any(s.get('is_abnormal') for s in all_speech_stats)

            avg_wpm = sum(s.get('wpm', 0) for s in use_stats) / len(use_stats)
            total_pauses = sum(s.get('pause_count', 0) for s in use_stats)
            avg_pause_ratio = sum(s.get('pause_ratio', 0) for s in use_stats) / len(use_stats)

            # 節奏判斷：全部異常時直接標記需人工確認
            if has_abnormal and not valid_stats:
                rhythm = "需人工確認"
            elif main_lang == 'zh':
                if avg_wpm < 120 and avg_pause_ratio > 40:
                    rhythm = "拖泥帶水"
                elif avg_wpm < 200 or avg_pause_ratio > 20:
                    rhythm = "需開倍速"
                else:
                    rhythm = "精煉流暢"
            else:
                if avg_wpm < 80 and avg_pause_ratio > 40:
                    rhythm = "拖泥帶水"
                elif avg_wpm < 120 or avg_pause_ratio > 20:
                    rhythm = "需開倍速"
                else:
                    rhythm = "精煉流暢"

            avg_stats = {
                'wpm': avg_wpm,
                'pause_count': total_pauses,
                'pause_ratio': round(avg_pause_ratio, 1),
                'rhythm': rhythm,
                'language': main_lang,
                'is_abnormal': has_abnormal
            }

        # 存分析素材
        combined_transcript = '\n---\n'.join(all_transcripts)
        with open(str(analysis_path), 'w', encoding='utf-8') as f:
            f.write(f"課程名稱：{course_name}\n")
            f.write(f"課程簡介：{course_desc}\n")
            lang_str = '中文' if main_lang == 'zh' else '英文' if main_lang == 'en' else main_lang
            f.write(f"偵測語言：{lang_str}\n\n")
            if avg_stats:
                f.write(f"【語音分析數據】\n")
                f.write(f"平均語速：{avg_stats.get('wpm', 0):.0f} 字/分鐘\n")
                f.write(f"停頓次數：{avg_stats.get('pause_count', 0)} 次\n")
                f.write(f"停頓比例：{avg_stats.get('pause_ratio', 0)}%\n")
                f.write(f"建議節奏：{avg_stats.get('rhythm', '')}\n")
                if avg_stats.get('is_abnormal'):
                    f.write(f"⚠️ 語速數據異常，請依逐字稿密度判斷節奏\n")
                f.write(f"\n")
            f.write(f"【逐字稿節錄（開頭/中間/結尾）】\n{combined_transcript[:5000]}")

        print(f"  📝 分析素材：{analysis_path}")
        if avg_stats:
            abnormal_flag = '⚠️異常' if avg_stats.get('is_abnormal') else '正常'
            print(f"  📊 語速：{avg_stats.get('wpm', 0):.0f}字/分（{main_lang}，{abnormal_flag}）"
                  f" | 節奏：{avg_stats.get('rhythm', '')}")

        # 讀取分析素材文字
        with open(str(analysis_path), 'r', encoding='utf-8') as f:
            text_content = f.read()

        # 平行呼叫三個 Claude Agent
        agent_raw_results = asyncio.run(run_three_agents(str(merged_path), text_content))
        result, diverge_fields = merge_agent_results(agent_raw_results)

        if result:
            tags = result.get('能力標籤', [])
            print(f"\n  ✅ 分析完成！")
            print(f"  🏷️  標籤：{', '.join(tags)}")
            print(f"  📊 難易度：{result.get('難易度', '')} | "
                  f"趣味度：{result.get('趣味度', '')} | "
                  f"推薦度：{result.get('推薦度', '')}")
            print(f"  🎬 節奏：{result.get('課程節奏', '')}")
            print(f"  🔑 關鍵字：{', '.join(result.get('關鍵字', []))}")
            if diverge_fields:
                print(f"  ⚠️ 分歧欄位：{', '.join(diverge_fields)}")
        else:
            print(f"  ❌ 三個 Agent 均無法取得有效結果，跳過此課程")

        all_results.append({
            'name': course_name,
            'result': result,
            'speech_stats': avg_stats,
            'diverge_fields': diverge_fields,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
        })
        if result:
            done_names.add(course_name)
            analyzed_today += 1
            consecutive_fail = 0
        else:
            consecutive_fail = locals().get('consecutive_fail', 0) + 1

        save_csv(all_results)
        # 存進度（跨天接續用）
        with open(RESULTS_JSON, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)

        # 連續 2 門全失敗（通常是額度耗盡）→ 提早停止，不再浪費時間下載
        if consecutive_fail >= 2:
            print(f"\n🛑 連續 {consecutive_fail} 門分析失敗（多半是額度用完），提早停止")
            print(f"   已完成 {len(done_names)} 門，剩下的等額度重置後再續跑")
            break

    remaining = len(queue) - len(done_names)
    if remaining <= 0:
        print(f"\n🎉 全部 {len(queue)} 門課完成！")
    else:
        print(f"\n✅ 本次完成 {analyzed_today} 門 | 累計 {len(done_names)}/{len(queue)} 門")
        print(f"   還有 {remaining} 門，明天再執行一次即可接續")
    if Path(OUTPUT_CSV).exists():
        os.startfile(OUTPUT_CSV)

if __name__ == "__main__":
    main()
