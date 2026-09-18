# تحديث — إصلاح فشل البناء (Build image failed)

## اللي حصل
لما ضفت ملف `Dockerfile` في جذر المشروع، Railway وقف عن استخدام الـ builder
التلقائي (Railpack) وحوّل يبني **Docker image** بدل كده — والبناء ده فشل.

قبل ما تضيف الـ Dockerfile كان البناء بينجح عادي (الخطأ كان في مرحلة التشغيل بس).

## الحل
**امسح ملف `Dockerfile` من الريبو.** إنت مش محتاجه على Railway إطلاقًا.
لو عايز تحتفظ بيه للتوثيق، سمّيه `Dockerfile.txt` أو حطه في مجلد `docs/`.

## الملفات في المجلد ده (نسخة نهائية بدون Dockerfile)
- `Procfile`
- `railway.json`
- `wsgi.py`
- `main.py`
- `requirements.txt`  ← عدّلت `psycopg2-binary` لـ `2.9.13` (أحدث إصدار، فيه wheels جاهزة لـ Python 3.13)
- `gunicorn.conf.py`
- `.python-version`
- `.gitignore`

## تشيك ليست قبل الـ Deploy الجاي
1. ❌ مفيش `Dockerfile` في جذر المشروع.
2. ✅ `Procfile` و `main.py` و `wsgi.py` موجودين **في الجذر** مش جوه مجلد.
3. ✅ Settings → Deploy → Custom Start Command = **فاضية**.
4. ✅ Variables: `SECRET_KEY` و `DATABASE_URL` و `SESSION_COOKIE_SECURE=1`.
