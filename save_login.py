"""
執行一次：在瀏覽器登入磨課師，把 cookie 存成 session.json
之後爬蟲自動載入，不需要再登入
"""
from playwright.sync_api import sync_playwright
import json

SESSION_FILE = "session.json"
BASE = "https://moocs.moe.edu.tw"

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=[
            '--start-maximized',
            '--disable-blink-features=AutomationControlled',
            '--no-first-run',
            '--no-default-browser-check',
        ]
    )
    ctx = browser.new_context(
        viewport={'width': 1280, 'height': 900},
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        locale='zh-TW',
    )
    page = ctx.new_page()

    # 覆蓋 window.alert/confirm/prompt，讓它們靜默返回，不觸發 dialog 事件
    ctx.add_init_script("window.alert = () => {}; window.confirm = () => true; window.prompt = () => '';")

    import time as _t
    print("="*50)
    print("  請在跳出的瀏覽器用 Google 帳號完成登入")
    print("  偵測到登入成功會【自動存檔】，不需要回來按 Enter")
    print("  （最多等候 5 分鐘）")
    print("="*50)
    page.goto(f"{BASE}/moocs/#/login", timeout=30000)

    # 偵測登入：以 id_token cookie 是否出現為準（check-auth API 需 Bearer token，
    # 用 plain fetch 會誤判未登入，故改看 context cookie，登入完成立即存檔）
    name = None
    deadline = _t.time() + 300
    while _t.time() < deadline:
        names = {c['name'] for c in ctx.cookies()}
        if 'id_token' in names:
            try:
                auth = page.evaluate("""
                async () => { const r = await fetch('/api/v1/check-auth'); return await r.json(); }""")
                if auth.get('data'):
                    name = auth['data'].get('name') or auth['data'].get('email') or '成功'
            except Exception:
                pass
            name = name or '已取得 id_token'
            break
        _t.sleep(2)

    if name:
        print(f"✅ 偵測到登入：{name}")
    else:
        print("⚠️ 等候逾時仍未偵測到 id_token，仍儲存目前 session（可能無效，請重跑）")

    # 儲存 session（cookies + localStorage）
    state = ctx.storage_state()
    with open(SESSION_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"✅ Session 已存到 {SESSION_FILE}")
    print("   之後直接跑 moocs_scraper.py，不需要再登入！")

    browser.close()
