"""
ملف توافق فقط.

بعض أدوات النشر (Railway/Railpack) بتفترض إن نقطة الدخول اسمها main:app،
ولو ملقتهاش بتطلع الخطأ: ModuleNotFoundError: No module named 'main'

الملف ده بيخلي الأوامر دي كلها تشتغل بنفس النتيجة:
    gunicorn main:app
    gunicorn wsgi:app
    gunicorn app:app
"""
from wsgi import app  # noqa: F401

application = app
