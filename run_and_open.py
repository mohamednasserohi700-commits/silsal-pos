# -*- coding: utf-8 -*-
"""
تشغيل سيرفر Flask وفتح المتصفح الافتراضي على الصفحة الرئيسية.
تشغيل: python run_and_open.py
أو من المتغيرات: ERP_PORT=8080 python run_and_open.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PORT = int(os.environ.get("ERP_PORT", "7500"))
# للمتصفح نستخدم localhost وليس 0.0.0.0
BROWSER_HOST = os.environ.get("ERP_BROWSER_HOST", "127.0.0.1")


def _open_browser_when_ready() -> None:
    time.sleep(1.8)
    url = f"http://{BROWSER_HOST}:{PORT}/"
    try:
        webbrowser.open(url)
    except Exception:
        print(f"تعذّر فتح المتصفح تلقائياً. افتح يدوياً: {url}")


if __name__ == "__main__":
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    from app import app, init_db

    init_db()
    print(f"السيرفر يعمل — سيتم فتح: http://{BROWSER_HOST}:{PORT}/")
    app.run(debug=True, host="0.0.0.0", port=PORT)
