"""
ProERP - WSGI Entry Point
gunicorn wsgi:app   (Linux / Railway)
"""
import os
import logging

from werkzeug.middleware.proxy_fix import ProxyFix

from app import app, init_db

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("wsgi")

# Railway/Nginx بيعملوا reverse proxy — لازم Flask يقرأ الهيدرز الصح
# عشان url_for و request.is_secure و IP المستخدم تطلع سليمة
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# كوكي آمن (HTTPS فقط) — فعّله من متغيرات البيئة: SESSION_COOKIE_SECURE=1
if os.environ.get("SESSION_COOKIE_SECURE", "0") == "1":
    app.config["SESSION_COOKIE_SECURE"] = True

# تهيئة قاعدة البيانات عند الإقلاع — لو فشلت لأي سبب، السيرفر يفضل شغال
# والخطأ يظهر كامل في اللوج بدل ما الحاوية تقع وتعمل Restart loop
try:
    init_db()
    _log.info("init_db() completed")
except Exception:
    _log.exception("init_db() failed — the app will still start")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7500))
    app.run(host="0.0.0.0", port=port)
