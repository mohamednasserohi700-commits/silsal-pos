"""
ProERP - WSGI Entry Point
يُستخدم مع gunicorn (Linux/Server) أو waitress (Windows)
"""
import os
from app import app, init_db

# تهيئة قاعدة البيانات عند بدء التشغيل
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7500))
    app.run(host="0.0.0.0", port=port)
