# -*- coding: utf-8 -*-
"""定稿分析語料的界定與凍結。

為什麼需要這個模組
------------------
`audio/` 與 `videos/` 會持續累積後續作業(抽樣代表性驗證等)所產生的檔案,
但各項交付文件所載之語料規模一律指**定稿分析當時所用者**。
若直接 glob 目錄加總,數字會隨後續作業一路往上跑,交出去的文件即失準。

此外,目錄中原本就混有開發期的測試殘檔(`_test_mid`、`test_cut`、
`*_fulltrack`、`*_full_audio`),不屬於分析語料,亦不應計入。

界定方式
--------
定稿語料 = 凍結時點(FREEZE_BEFORE)之前產生、且檔名符合
`{影片主幹}_seg{n}_{位置}` 格式者。凍結時點為絕對時間,故重跑 --freeze
結果固定不變,不受執行時間影響。

凡文件需引用語料規模,一律呼叫本模組,不得自行 glob 目錄。
"""
import os, re, sys, glob, json, hashlib, datetime

MANIFEST = 'corpus_manifest.json'
QUEUE = 'moocs_queue.json'
# 凍結時點:定稿分析結束、抽樣代表性驗證開始之前
FREEZE_BEFORE = datetime.datetime(2026, 8, 22, 15, 40)
SEG_JSON = re.compile(r'^(.+)_seg\d+_\w+\.json$')
SEG_MP3 = re.compile(r'^(.+)_seg\d+_\w+\.mp3$')


def video_stem(ref):
    """影片位址 → 本機檔名主幹,與 moocs_final.download_video() 一致。"""
    if ref.startswith('http') and '.mp4' in ref.lower():
        return 'mp4_' + hashlib.md5(ref.encode()).hexdigest()[:12]
    return ref


def _sampled_total(queue=None):
    """逐課加總之抽樣影片支數(與影片總數 819 同一計法)。"""
    import moocs_final as mf
    queue = queue or json.load(open(QUEUE, encoding='utf-8'))
    return sum(len(mf.select_videos_smart(c)) for c in queue)


def sampled_stems(queue=None):
    """定稿分析所抽出之影片檔名主幹集合。"""
    import moocs_final as mf
    queue = queue or json.load(open(QUEUE, encoding='utf-8'))
    return {video_stem(r) for c in queue for r in mf.select_videos_smart(c)}


def _load():
    if not os.path.exists(MANIFEST):
        raise FileNotFoundError(
            f'找不到 {MANIFEST}。請先執行 python corpus.py --freeze。')
    return json.load(open(MANIFEST, encoding='utf-8'))


def transcript_stats():
    """(逐字稿段數, 總字數)。僅計定稿語料。"""
    m = _load()
    return m['n_segments'], m['n_chars']


def audio_segment_count():
    """定稿語料之音訊片段數(與逐字稿一一對應)。"""
    return _load()['n_audio']


def sampled_video_count():
    """定稿分析所抽出之影片支數(逐課加總,與 819 支同一計法)。

    註:其中有數支影片為同名課程共用,故相異檔案數少於本數,
    見 manifest 之 n_sampled_distinct。
    """
    return _load()['n_sampled_videos']


def files():
    """定稿語料之逐字稿檔名清單。"""
    return _load()['files']


def sampled_all_present():
    """258 支抽樣影片是否全數在檔。回傳 (是否全在, 缺漏清單)。"""
    want = sampled_stems()
    have = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob('videos/*.mp4')}
    miss = sorted(want - have)
    return (not miss), miss


def freeze():
    """就凍結時點之前的檔案產生清單。可重複執行,結果固定。"""
    cut = FREEZE_BEFORE.timestamp()
    fs, chars, later, junk = [], 0, 0, 0
    for p in sorted(glob.glob('audio/*.json')):
        base = os.path.basename(p)
        if os.path.getmtime(p) > cut:
            later += 1
            continue
        if not SEG_JSON.match(base):
            junk += 1
            continue
        fs.append(base)
        chars += len(json.load(open(p, encoding='utf-8')).get('text', ''))
    keep = {f[:-5] for f in fs}
    n_audio = sum(1 for p in glob.glob('audio/*.mp3')
                  if os.path.getmtime(p) <= cut
                  and SEG_MP3.match(os.path.basename(p))
                  and os.path.basename(p)[:-4] in keep)
    data = {
        'frozen_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'freeze_before': FREEZE_BEFORE.strftime('%Y-%m-%d %H:%M:%S'),
        'note': '定稿分析所用之語料。凍結時點之後的檔案(抽樣代表性驗證等後續作業)'
                '與開發期測試殘檔一律不計入,俾使交付文件所載數字固定不變。',
        'n_segments': len(fs), 'n_chars': chars, 'n_audio': n_audio,
        'n_videos': len({SEG_JSON.match(f).group(1) for f in fs}),
        'n_sampled_videos': _sampled_total(),
        'n_sampled_distinct': len(sampled_stems()),
        'excluded_after_freeze': later, 'excluded_junk': junk,
        'files': fs,
    }
    with open(MANIFEST, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return data


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--freeze' in sys.argv:
        d = freeze()
        print(f'已凍結 {MANIFEST}(凍結時點 {d["freeze_before"]})')
    d = _load()
    ok, miss = sampled_all_present()
    print(f'定稿語料　逐字稿 {d["n_segments"]} 段、{d["n_chars"]:,} 字'
          f'｜音訊 {d["n_audio"]} 段｜來源影片 {d["n_videos"]} 支')
    print(f'抽樣影片 {d["n_sampled_videos"]} 支(逐課加總)、'
          f'相異檔案 {d["n_sampled_distinct"]} 個(同名課程共用數支),'
          f'{"全數在檔" if ok else f"缺 {len(miss)} 個:{miss[:5]}"}')
    print(f'排除:凍結時點之後 {d["excluded_after_freeze"]} 個、'
          f'測試殘檔 {d["excluded_junk"]} 個')
    print(f'目前磁碟 audio/ {len(glob.glob("audio/*.json"))} 個 json、'
          f'videos/ {len(glob.glob("videos/*.mp4"))} 個 mp4')
