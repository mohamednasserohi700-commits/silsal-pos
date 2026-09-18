import os

# Server socket
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# Worker processes
# worker واحد + threads: ده الأنسب للبرنامج ده لأن:
#   - SQLite مع StaticPool ما ينفعش يتشارك بين أكتر من process
#   - init_db() و thread النسخ الاحتياطي يشتغلوا مرة واحدة بس
workers = int(os.environ.get("GUNICORN_WORKERS", 1))
threads = int(os.environ.get("GUNICORN_THREADS", 8))
worker_class = "gthread"
timeout = 120
graceful_timeout = 30
keepalive = 5

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s'

# Process naming
proc_name = "proerp"

# Server mechanics
preload_app = False
daemon = False
