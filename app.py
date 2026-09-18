from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from functools import wraps
import os
import json
from collections import defaultdict
import re
import hashlib
import secrets
import shutil
import threading
import time as time_module
from werkzeug.utils import secure_filename
from sqlalchemy import event
from sqlalchemy.engine.url import make_url
try:
    import openpyxl
except ImportError:
    openpyxl = None

app = Flask(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_INSTANCE_DIR = os.path.join(_BASE_DIR, 'instance')
os.makedirs(_INSTANCE_DIR, exist_ok=True)

# ── Security ──────────────────────────────────────────────
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'erp-secret-key-change-in-production-2024')

# ── Permanent sessions (لا تنتهي الجلسة عند إغلاق المتصفح) ──
app.config['REMEMBER_COOKIE_DURATION'] = None          # لا تنتهي أبداً
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30  # 30 يوم
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True

# ── Database ──────────────────────────────────────────────
def _sqlite_uri_from_json_config():
    cfg = os.path.join(_INSTANCE_DIR, 'database_path.json')
    if not os.path.isfile(cfg):
        return None
    try:
        with open(cfg, encoding='utf-8') as f:
            data = json.load(f)
        raw = (data.get('sqlite_path') or data.get('database_path') or '').strip()
        if not raw:
            return None
        path = os.path.abspath(os.path.expanduser(raw))
        dname = os.path.dirname(path)
        if dname and not os.path.isdir(dname):
            os.makedirs(dname, exist_ok=True)
        return 'sqlite:///' + path.replace('\\', '/')
    except Exception:
        return None


db_url = os.environ.get('DATABASE_URL')
if not db_url:
    db_url = _sqlite_uri_from_json_config()
if not db_url:
    db_url = 'sqlite:///erp.db'
# Heroku/Railway يُرجعون postgres:// — نحوّله لـ postgresql+psycopg://
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif db_url.startswith('postgresql://') and '+' not in db_url.split('://')[0]:
    db_url = db_url.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# قواعد ترخيص منفصلة: سريالات جاهزة | سريالات مُستَخدمة
def _sqlite_bind_uri(name):
    return 'sqlite:///' + os.path.join(_INSTANCE_DIR, name).replace('\\', '/')
app.config['SQLALCHEMY_BINDS'] = {
    'license_pool': _sqlite_bind_uri('license_pool.db'),
    'license_used': _sqlite_bind_uri('license_used.db'),
}

# ── Connection Pool ───────────────────────────────────────
# SQLite لا يدعم pool_size/max_overflow — نفرق بين البيئتين
_is_sqlite   = db_url.startswith('sqlite')
_is_postgres = 'postgresql' in db_url

if _is_sqlite:
    from sqlalchemy.pool import StaticPool
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        # اتصال واحد مشترك — يمنع تضارب الـ threads في SQLite
        'connect_args': {'check_same_thread': False},
        'poolclass': StaticPool,
    }
elif _is_postgres:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_recycle':  280,   # تجديد الاتصال قبل timeout
        'pool_pre_ping': True,  # اختبار الاتصال قبل كل query
        'pool_size':     5,     # اتصالات دائمة (آمن على Railway free tier)
        'max_overflow':  10,    # اتصالات إضافية عند الضغط
        'pool_timeout':  30,
        'connect_args':  {'connect_timeout': 10},
    }
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}


db = SQLAlchemy(app)

# ── SQLite tuning: WAL + busy_timeout ────────────────────────
# WAL بيسمح بالقراءة أثناء الكتابة (بدل ما الملف كله يتقفل وقت الحفظ)،
# وbusy_timeout بيخلي أي عملية تستنى شوية بدل ما ترمي "database is locked"
# فورًا لو اتزاحمت مع عملية تانية بتكتب في نفس اللحظة.
# الاستماع على Engine نفسه (مش db.engine) عشان يشتغل من غير الحاجة لـ app context،
# وبيغطي قاعدة البيانات الرئيسية وقواعد التراخيص (license_pool/license_used) مع بعض.
from sqlalchemy.engine import Engine as _SAEngine


@event.listens_for(_SAEngine, 'connect')
def _set_sqlite_pragma(dbapi_connection, connection_record):
    if 'sqlite3' in type(dbapi_connection).__module__:
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA busy_timeout=15000')  # 15 ثانية انتظار قبل الفشل
        cursor.execute('PRAGMA synchronous=NORMAL')  # أداء أفضل مع WAL، بدون تضحية بأمان البيانات
        cursor.close()

login_manager = LoginManager(app)

# ── Error logging (يظهر الخطأ كاملاً في السجلات) ──────────
import logging, traceback as _tb
logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger(__name__)

@app.errorhandler(500)
def internal_error(e):
    _logger.error("500 ERROR:\n" + _tb.format_exc())
    db.session.rollback()
    return "Internal Server Error — check logs", 500
login_manager.login_view = 'login'
login_manager.login_message = 'يرجى تسجيل الدخول للوصول لهذه الصفحة'

# ===== MODELS =====

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    full_name = db.Column(db.String(120))
    role = db.Column(db.String(20), default='user')  # developer, admin, manager, user
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen = db.Column(db.DateTime, nullable=True)
    last_ip = db.Column(db.String(64), nullable=True)
    last_user_agent = db.Column(db.String(256), nullable=True)
    permissions = db.Column(db.Text)  # JSON list of permission keys; empty = استخدام صلاحيات الدور

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class AppSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.Text)

class Branch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200))
    phone = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True)
    users = db.relationship('User', backref='branch', lazy=True)
    warehouses = db.relationship('Warehouse', backref='branch', lazy=True)

class Warehouse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    manager_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    address = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, default=True)
    manager = db.relationship('User', foreign_keys=[manager_id])

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    children = db.relationship('Category', backref=db.backref('parent', remote_side=[id]))
    products = db.relationship('Product', backref='category', lazy=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    barcode = db.Column(db.String(50))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    unit = db.Column(db.String(20), default='قطعة')
    cost_price = db.Column(db.Float, default=0)
    sell_price = db.Column(db.Float, default=0)
    min_stock = db.Column(db.Float, default=0)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    stock_items = db.relationship('Stock', backref='product', lazy=True)

class Stock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    quantity = db.Column(db.Float, default=0)
    warehouse = db.relationship('Warehouse')
    
    __table_args__ = (db.UniqueConstraint('product_id', 'warehouse_id'),)

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    address = db.Column(db.Text)
    credit_limit = db.Column(db.Float, default=0)
    balance = db.Column(db.Float, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sales = db.relationship('Sale', backref='customer', lazy=True)

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    address = db.Column(db.Text)
    balance = db.Column(db.Float, default=0)
    is_active = db.Column(db.Boolean, default=True)
    purchases = db.relationship('Purchase', backref='supplier', lazy=True)

class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    national_id = db.Column(db.String(30))
    address = db.Column(db.Text)
    photo = db.Column(db.String(300))
    position = db.Column(db.String(100))
    department = db.Column(db.String(100))
    manager_id = db.Column(db.Integer, db.ForeignKey('employee.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    salary = db.Column(db.Float, default=0)
    hire_date = db.Column(db.Date)
    employment_status = db.Column(db.String(30), default='active')
    contract_type = db.Column(db.String(40), default='permanent')
    is_active = db.Column(db.Boolean, default=True)
    branch = db.relationship('Branch')
    manager = db.relationship('Employee', remote_side=[id], foreign_keys=[manager_id])

class InvoiceEditLog(db.Model):
    """سجل تعديلات فواتير البيع/الشراء: من عدّل، متى، وملخص ما تغيّر."""
    id = db.Column(db.Integer, primary_key=True)
    invoice_type = db.Column(db.String(10))  # 'sale' أو 'purchase'
    invoice_id = db.Column(db.Integer)
    invoice_number = db.Column(db.String(50))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    summary = db.Column(db.Text)
    user = db.relationship('User')


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    payment_method = db.Column(db.String(20), default='cash')  # cash, card, transfer, other
    date = db.Column(db.DateTime, default=datetime.utcnow)
    subtotal = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    paid = db.Column(db.Float, default=0)
    remaining = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='completed')
    notes = db.Column(db.Text)
    items = db.relationship('SaleItem', backref='sale', lazy=True, cascade='all, delete-orphan')
    user = db.relationship('User')
    warehouse = db.relationship('Warehouse')

class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    price = db.Column(db.Float)
    discount = db.Column(db.Float, default=0)
    total = db.Column(db.Float)
    product = db.relationship('Product')


class HeldSale(db.Model):
    """فاتورة بيع معلّقة مؤقتاً (Hold/Park Sale) — مسودة قبل السداد، لا تخصم من المخزون
    ولا تُرقّم برقم فاتورة رسمي إلا بعد استرجاعها وحفظها فعلياً."""
    id = db.Column(db.Integer, primary_key=True)
    hold_number = db.Column(db.String(20), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    notes = db.Column(db.Text)
    total_discount = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    items_json = db.Column(db.Text, nullable=False)  # [{product_id, quantity, price, discount}, ...]
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    customer = db.relationship('Customer')
    warehouse = db.relationship('Warehouse')
    user = db.relationship('User')


class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    subtotal = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    withholding_tax = db.Column(db.Float, default=0)  # خصم / تحصيل 1% من المورد (قابل للتعديل)
    total = db.Column(db.Float, default=0)
    paid = db.Column(db.Float, default=0)
    remaining = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='completed')
    notes = db.Column(db.Text)
    items = db.relationship('PurchaseItem', backref='purchase', lazy=True, cascade='all, delete-orphan')
    user = db.relationship('User')
    warehouse = db.relationship('Warehouse')

class PurchaseItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey('purchase.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    price = db.Column(db.Float)
    total = db.Column(db.Float)
    product = db.relationship('Product')

class SaleReturn(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    total = db.Column(db.Float, default=0)
    reason = db.Column(db.Text)
    items = db.relationship('SaleReturnItem', backref='return_order', lazy=True)
    sale = db.relationship('Sale')

class SaleReturnItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(db.Integer, db.ForeignKey('sale_return.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    price = db.Column(db.Float)
    discount = db.Column(db.Float, default=0)
    extra_discount = db.Column(db.Float, default=0)  # خصم إضافي % على سطر المرتجع
    total = db.Column(db.Float)
    product = db.relationship('Product')

class PurchaseReturn(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey('purchase.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    total = db.Column(db.Float, default=0)
    reason = db.Column(db.Text)
    items = db.relationship('PurchaseReturnItem', backref='return_order', lazy=True)
    purchase = db.relationship('Purchase')

class PurchaseReturnItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(db.Integer, db.ForeignKey('purchase_return.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    price = db.Column(db.Float)
    discount = db.Column(db.Float, default=0)
    extra_discount = db.Column(db.Float, default=0)
    total = db.Column(db.Float)
    product = db.relationship('Product')


class StockAdjustmentLog(db.Model):
    """سجل تسويات المخزون اليدوية (للتقرير)."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    old_quantity = db.Column(db.Float, default=0)
    new_quantity = db.Column(db.Float, default=0)
    reason = db.Column(db.String(300))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    product = db.relationship('Product')
    warehouse = db.relationship('Warehouse')
    user = db.relationship('User')


class InventoryMemo(db.Model):
    """مذكرات مخزون: صرف مواد لصالة الإنتاج أو استلام تام من الصالة."""
    id = db.Column(db.Integer, primary_key=True)
    memo_number = db.Column(db.String(50), unique=True)
    memo_type = db.Column(db.String(24), nullable=False)  # issue_production | receive_production
    production_ref = db.Column(db.String(120))  # رقم أمر / صالة الإنتاج
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)
    items = db.relationship('InventoryMemoItem', backref='memo', lazy=True, cascade='all, delete-orphan')
    warehouse = db.relationship('Warehouse')
    user = db.relationship('User')


class InventoryMemoItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    memo_id = db.Column(db.Integer, db.ForeignKey('inventory_memo.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    unit_note = db.Column(db.String(40))  # وحدة الاستلام إن اختلفت عن وحدة الصنف
    product = db.relationship('Product')

class TransferRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_number = db.Column(db.String(50), unique=True)
    from_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    requested_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    approver_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    date_requested = db.Column(db.DateTime, default=datetime.utcnow)
    date_processed = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    notes = db.Column(db.Text)
    rejection_reason = db.Column(db.Text)
    items = db.relationship('TransferItem', backref='transfer', lazy=True, cascade='all, delete-orphan')
    from_warehouse = db.relationship('Warehouse', foreign_keys=[from_warehouse_id])
    to_warehouse = db.relationship('Warehouse', foreign_keys=[to_warehouse_id])
    requester = db.relationship('User', foreign_keys=[requested_by])
    designated_approver = db.relationship('User', foreign_keys=[approver_user_id])
    approver = db.relationship('User', foreign_keys=[approved_by])

class TransferItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey('transfer_request.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    product = db.relationship('Product')

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    category = db.Column(db.String(100))
    description = db.Column(db.Text)
    amount = db.Column(db.Float)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User')
    branch = db.relationship('Branch')

class CustomerPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    amount = db.Column(db.Float)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    customer = db.relationship('Customer')

class SupplierPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))
    amount = db.Column(db.Float)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    supplier = db.relationship('Supplier')


class LicensePoolSerial(db.Model):
    """سريالات غير مستخدمة — قاعدة license_pool.db"""
    __bind_key__ = 'license_pool'
    __tablename__ = 'pool_serial'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(48), unique=True, nullable=False)
    code_norm = db.Column(db.String(40), unique=True, nullable=False, index=True)
    plan = db.Column(db.String(32), nullable=False)
    custom_days = db.Column(db.Integer)
    note = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class LicenseUsedSerial(db.Model):
    """سريالات بعد التفعيل — قاعدة license_used.db"""
    __bind_key__ = 'license_used'
    __tablename__ = 'used_serial'
    id = db.Column(db.Integer, primary_key=True)
    code_hash = db.Column(db.String(64), unique=True, nullable=False)
    code_hint = db.Column(db.String(24))
    plan = db.Column(db.String(32))
    activated_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)

# ===== HELPERS =====
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ['admin', 'manager', 'developer']:
            flash('ليس لديك صلاحية للوصول لهذه الصفحة', 'error')
            return redirect(safe_home_url_for(current_user))
        return f(*args, **kwargs)
    return decorated


# ── صلاحيات مفصّلة (مفاتيح للواجهة و before_request) ─────────────────
PERMISSION_KEYS = [
    ('dashboard', 'لوحة التحكم'),
    ('sales', 'المبيعات'),
    ('purchases', 'المشتريات'),
    ('returns', 'المرتجعات'),
    ('inventory', 'المخزون والجرد'),
    ('transfers', 'تحويلات المخازن'),
    ('transfer_approve', 'الموافقة على تحويلات المخازن'),
    ('adjust_stock', 'تسوية المخزون'),
    ('customers', 'العملاء'),
    ('suppliers', 'الموردون'),
    ('employees', 'الموظفون'),
    ('expenses', 'المصاريف'),
    ('products', 'الأصناف'),
    ('product_add', 'إضافة صنف جديد'),
    ('categories', 'التصنيفات'),
    ('reports', 'التقارير - عام'),
    ('reports_dashboard', 'تقرير/تحليلات لوحة التحكم'),
    ('report_sales', 'تقرير المبيعات'),
    ('report_purchases', 'تقرير المشتريات'),
    ('report_inventory', 'تقرير المخزون'),
    ('report_customers', 'تقرير العملاء'),
    ('report_suppliers', 'تقرير الموردين'),
    ('report_expenses', 'تقرير المصاريف'),
    ('report_profit', 'تقرير الأرباح والخسائر'),
    ('report_low_stock', 'تقرير الأصناف منخفضة المخزون'),
    ('report_stock_adjustments', 'تقرير تسويات المخزون'),
    ('reports_export', 'تصدير التقارير'),
    ('reports_print', 'طباعة التقارير'),
    ('settings', 'الإعدادات (فروع / مخازن / ضريبة البيع)'),
    ('settings_branding', 'إعدادات النظام'),
    ('settings_database', 'إدارة قاعدة البيانات'),
    ('users', 'المستخدمون'),
    ('connected_users', 'المتصلون'),
    ('delete_users', 'حذف مستخدمين'),
    ('record_delete', 'حذف السجلات (عملاء، موردين، أصناف، مصاريف، …)'),
    ('sales_purchases_edit', 'تعديل فواتير البيع والشراء'),
    ('statement_payment_delete', 'حذف دفعات كشف الحساب'),
    ('returns_delete', 'حذف فواتير المرتجعات (مبيعات / مشتريات)'),
    ('backup', 'النسخ الاحتياطي'),
    ('warehouse_purge', 'حذف نهائي للمخزن (خطير)'),
    ('stock_line_delete', 'حذف سطر صنف من المخزن/الجرد (خطير)'),
]

# تظهر في شاشة الصلاحيات للمطوّر فقط — يمنحها للمستخدمين يدوياً
DEVELOPER_ONLY_PERMS = frozenset({'warehouse_purge', 'stock_line_delete'})

DEFAULT_SETTINGS = {
    'company_name': 'System Makers',
    'app_title': 'Silsal POS',
    'app_subtitle': 'نظام إدارة أعمال ومحاسبة',
    'program_label': 'Silsal POS',
    'layout_max_width': '1400px',
    'license_expiry_message': 'انتهى اشتراكك. يرجى التواصل مع المورد لتجديد الترخيص.',
}

# إعدادات إضافية (قابلة للتخزين في AppSetting وللتخصيص حسب الفرع)
EXTRA_APP_SETTINGS_DEFAULTS = {
    'sale_fixed_tax_percent': '0',
    'sale_fixed_tax_enabled': '0',
    'print_mode': 'normal',
    'print_paper_size': 'A4',
    'print_auto_sale': '0',
    'print_auto_purchase': '0',
    'print_auto_sale_return': '0',
    'print_auto_purchase_return': '0',
    'print_auto_copies': '1',
}

GLOBAL_ONLY_SETTING_KEYS = frozenset({
    'installation_license_permanent', 'installation_license_expires', 'license_expiry_message',
})

BRANCH_SCOPED_SETTING_KEYS = frozenset(DEFAULT_SETTINGS.keys()) | frozenset(EXTRA_APP_SETTINGS_DEFAULTS.keys()) - GLOBAL_ONLY_SETTING_KEYS


def _perm_list_from_user(user):
    if not user or not getattr(user, 'permissions', None):
        return None
    try:
        data = json.loads(user.permissions)
        if isinstance(data, list) and len(data) > 0:
            return set(data)
    except (json.JSONDecodeError, TypeError):
        pass
    return None


MANAGER_DEFAULT_DENIED = frozenset({
    'users', 'backup', 'settings_branding', 'settings_database', 'delete_users', 'transfer_approve',
    'record_delete', 'statement_payment_delete', 'returns_delete',
})


def user_can(user, perm: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'role', None) == 'developer':
        return True
    custom = _perm_list_from_user(user)
    if perm in DEVELOPER_ONLY_PERMS:
        if custom is None:
            return False
        return perm in custom
    if custom is not None:
        return perm in custom
    if user.role == 'admin':
        return True
    if user.role == 'manager':
        if perm in MANAGER_DEFAULT_DENIED:
            return False
        return True
    if user.role == 'user':
        return perm in {
            'dashboard', 'sales', 'purchases', 'returns', 'inventory', 'transfers',
            'customers', 'suppliers', 'expenses', 'products', 'product_add', 'categories', 'reports',
        }
    if user.role in ('hr_manager', 'hr_officer', 'payroll_officer', 'department_manager', 'employee'):
        return perm == 'dashboard'
    return False


def user_can_approve_transfers(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'role', None) in ('developer', 'admin'):
        return True
    custom = _perm_list_from_user(user)
    if custom is not None:
        return 'transfer_approve' in custom
    return False


def user_can_delete_users_account(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'role', None) == 'developer':
        return True
    custom = _perm_list_from_user(user)
    if custom is not None:
        return 'delete_users' in custom
    if getattr(user, 'role', None) == 'admin':
        return True
    return False


def default_role_permission_set(role: str) -> set:
    """صلاحيات الدور الافتراضية (بدون JSON يدوي) — للمقارنة وعرض نموذج التعديل."""
    keys_all = {k for k, _ in PERMISSION_KEYS}
    if role == 'developer':
        return set(keys_all)
    if role == 'admin':
        return keys_all - DEVELOPER_ONLY_PERMS
    if role == 'manager':
        return keys_all - MANAGER_DEFAULT_DENIED - DEVELOPER_ONLY_PERMS
    if role == 'user':
        return {
            'dashboard', 'sales', 'purchases', 'returns', 'inventory', 'transfers',
            'customers', 'suppliers', 'expenses', 'products', 'product_add', 'categories', 
            'reports', 'report_sales', 'report_purchases', 'report_inventory', 'report_customers',
            'report_suppliers', 'report_expenses', 'report_profit', 'report_low_stock',
            'report_stock_adjustments', 'reports_dashboard', 'reports_export', 'reports_print',
        }
    if role in ('hr_manager', 'hr_officer', 'payroll_officer', 'department_manager', 'employee'):
        return {'dashboard'}
    return set()


def effective_selected_permissions_for_form(user, keys_visible: frozenset):
    """ما يُعرض مُحدَّداً في خانات الصلاحيات عند التعديل (الدور أو JSON المحفوظ)."""
    custom = _perm_list_from_user(user)
    if custom is not None:
        return sorted(str(k) for k in custom if str(k) in keys_visible)
    base = default_role_permission_set(user.role or 'user')
    return sorted(str(k) for k in base if k in keys_visible)


def _permissions_form_to_stored(perms_list, role: str, keys_visible: frozenset):
    """تحويل ما أُرسل من النموذج إلى JSON أو None إن طابق افتراضيات الدور."""
    if not perms_list:
        return None
    s = set(perms_list) & keys_visible
    if 'dashboard' not in s:
        s.add('dashboard')
    default = {k for k in default_role_permission_set(role) if k in keys_visible}
    if s == default:
        return None
    return sorted(s)


def record_delete_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not user_can(current_user, 'record_delete'):
            flash('ليس لديك صلاحية حذف السجلات. يمنحها مدير النظام يدوياً من صلاحيات المستخدم.', 'error')
            return redirect(safe_home_url_for(current_user))
        return f(*args, **kwargs)
    return decorated


def statement_payment_delete_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not user_can(current_user, 'statement_payment_delete'):
            flash('ليس لديك صلاحية حذف دفعات كشف الحساب. يمنحها مدير النظام من صلاحيات المستخدم.', 'error')
            return redirect(safe_home_url_for(current_user))
        return f(*args, **kwargs)
    return decorated


def returns_delete_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not user_can(current_user, 'returns_delete'):
            flash('ليس لديك صلاحية حذف فواتير المرتجعات. يمنحها مدير النظام من صلاحيات المستخدم.', 'error')
            return redirect(safe_home_url_for(current_user))
        return f(*args, **kwargs)
    return decorated


def product_add_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not user_can(current_user, 'product_add'):
            flash('ليس لديك صلاحية إضافة صنف جديد. يمنحها مدير النظام من صلاحيات المستخدم.', 'error')
            return redirect(safe_home_url_for(current_user))
        return f(*args, **kwargs)
    return decorated


def _invoice_numbers_from_payment_notes(notes):
    text = notes or ''
    if 'دفعة على فواتير:' in text:
        part = text.split('دفعة على فواتير:', 1)[1]
        return [x.strip() for x in part.split(',') if x.strip()]
    # توافق مع الصيغة القديمة لدفعة سريعة على فاتورة واحدة من صفحة الفاتورة نفسها
    if 'دفعة على فاتورة ' in text:
        part = text.split('دفعة على فاتورة ', 1)[1].strip()
        return [part] if part else []
    return []


def _unapply_customer_invoice_payment(customer_id, amount, notes):
    remaining = float(amount or 0)
    if remaining <= 0.0001:
        return
    nums = _invoice_numbers_from_payment_notes(notes)
    if nums:
        sales = Sale.query.filter(Sale.customer_id == customer_id, Sale.invoice_number.in_(nums)).all()
        by_num = {s.invoice_number: s for s in sales}
        for num in reversed(nums):
            sale = by_num.get(num)
            if not sale or remaining <= 0.0001:
                continue
            paid = float(sale.paid or 0)
            if paid <= 0.0001:
                continue
            un = min(paid, remaining)
            sale.paid = paid - un
            sale.remaining = float(sale.remaining or 0) + un
            remaining -= un
    if remaining > 0.0001:
        sales = Sale.query.filter_by(customer_id=customer_id).filter(Sale.paid > 0).order_by(Sale.date.desc()).all()
        for sale in sales:
            if remaining <= 0.0001:
                break
            paid = float(sale.paid or 0)
            un = min(paid, remaining)
            if un <= 0:
                continue
            sale.paid = paid - un
            sale.remaining = float(sale.remaining or 0) + un
            remaining -= un


def _unapply_supplier_invoice_payment(supplier_id, amount, notes):
    remaining = float(amount or 0)
    if remaining <= 0.0001:
        return
    nums = _invoice_numbers_from_payment_notes(notes)
    if nums:
        purchases = Purchase.query.filter(Purchase.supplier_id == supplier_id, Purchase.invoice_number.in_(nums)).all()
        by_num = {p.invoice_number: p for p in purchases}
        for num in reversed(nums):
            purchase = by_num.get(num)
            if not purchase or remaining <= 0.0001:
                continue
            paid = float(purchase.paid or 0)
            if paid <= 0.0001:
                continue
            un = min(paid, remaining)
            purchase.paid = paid - un
            purchase.remaining = float(purchase.remaining or 0) + un
            remaining -= un
    if remaining > 0.0001:
        purchases = Purchase.query.filter_by(supplier_id=supplier_id).filter(Purchase.paid > 0).order_by(Purchase.date.desc()).all()
        for purchase in purchases:
            if remaining <= 0.0001:
                break
            paid = float(purchase.paid or 0)
            un = min(paid, remaining)
            if un <= 0:
                continue
            purchase.paid = paid - un
            purchase.remaining = float(purchase.remaining or 0) + un
            remaining -= un


def _customer_linked_payments_total(customer_id, invoice_number):
    """إجمالي دفعات كشف الحساب (النظام الحالي) المرتبطة تحديداً بفاتورة عميل معيّنة."""
    if not invoice_number:
        return 0.0
    rows = CustomerPayment.query.filter_by(customer_id=customer_id).filter(
        CustomerPayment.notes.like(f'%مرتبطة بفاتورة {invoice_number}')
    ).all()
    return sum(float(p.amount or 0) for p in rows)


def _supplier_linked_payments_total(supplier_id, invoice_number):
    """إجمالي دفعات كشف الحساب (النظام الحالي) المرتبطة تحديداً بفاتورة مورد معيّنة."""
    if not invoice_number:
        return 0.0
    rows = SupplierPayment.query.filter_by(supplier_id=supplier_id).filter(
        SupplierPayment.notes.like(f'%مرتبطة بفاتورة {invoice_number}')
    ).all()
    return sum(float(p.amount or 0) for p in rows)


def _customer_open_invoices(customer_id):
    """فواتير العميل التي ما زال عليها متبقٍ فعلي (لعرضها في قائمة اختيار «تسديد فاتورة»)."""
    sales = Sale.query.filter_by(customer_id=customer_id).order_by(Sale.date.asc()).all()
    result = []
    for s in sales:
        already_paid = _customer_linked_payments_total(customer_id, s.invoice_number)
        actual_remaining = float(s.remaining or 0) - already_paid
        if actual_remaining > 0.0001:
            result.append({'invoice_number': s.invoice_number, 'actual_remaining': actual_remaining, 'total': s.total})
    return result


def _supplier_open_invoices(supplier_id):
    """فواتير المورد التي ما زال عليها متبقٍ فعلي (لعرضها في قائمة اختيار «تسديد فاتورة»)."""
    purchases = Purchase.query.filter_by(supplier_id=supplier_id).order_by(Purchase.date.asc()).all()
    result = []
    for p in purchases:
        already_paid = _supplier_linked_payments_total(supplier_id, p.invoice_number)
        actual_remaining = float(p.remaining or 0) - already_paid
        if actual_remaining > 0.0001:
            result.append({'invoice_number': p.invoice_number, 'actual_remaining': actual_remaining, 'total': p.total})
    return result


def _format_num(v):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return str(v)
    if abs(v - round(v)) < 1e-9:
        return f'{int(round(v)):,}'
    return f'{v:,.2f}'


def _log_invoice_edit(invoice_type, invoice_id, invoice_number, user_id, summary):
    """يسجل عملية تعديل فاتورة (بيع/شراء) في سجل التعديلات: من عدّل، متى، وملخص التغييرات."""
    try:
        log = InvoiceEditLog(
            invoice_type=invoice_type,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            user_id=user_id,
            summary=summary or 'تم الحفظ بدون تغييرات ظاهرة',
        )
        db.session.add(log)
    except Exception:
        pass


def _diff_sale_edit(old, new_lines_merged, product_names, party_label='العميل'):
    """يبني ملخص التغييرات بين حالة الفاتورة (بيع/شراء) قبل وبعد التعديل.
    old/new: dict بالقيم. party_label: 'العميل' أو 'المورد' حسب نوع الفاتورة."""
    changes = []
    if old.get('customer_name') != new_lines_merged.get('customer_name'):
        changes.append(f"{party_label}: {old.get('customer_name') or '-'} ← {new_lines_merged.get('customer_name') or '-'}")
    if old.get('warehouse_name') != new_lines_merged.get('warehouse_name'):
        changes.append(f"المخزن: {old.get('warehouse_name') or '-'} ← {new_lines_merged.get('warehouse_name') or '-'}")
    if abs(float(old.get('discount') or 0) - float(new_lines_merged.get('discount') or 0)) > 0.005:
        changes.append(f"الخصم: {_format_num(old.get('discount'))} ← {_format_num(new_lines_merged.get('discount'))}")
    if abs(float(old.get('tax') or 0) - float(new_lines_merged.get('tax') or 0)) > 0.005:
        changes.append(f"الضريبة: {_format_num(old.get('tax'))} ← {_format_num(new_lines_merged.get('tax'))}")
    if abs(float(old.get('total') or 0) - float(new_lines_merged.get('total') or 0)) > 0.005:
        changes.append(f"الإجمالي: {_format_num(old.get('total'))} ← {_format_num(new_lines_merged.get('total'))}")
    if abs(float(old.get('paid') or 0) - float(new_lines_merged.get('paid') or 0)) > 0.005:
        changes.append(f"المدفوع: {_format_num(old.get('paid'))} ← {_format_num(new_lines_merged.get('paid'))}")
    if (old.get('notes') or '') != (new_lines_merged.get('notes') or ''):
        changes.append('تم تعديل الملاحظات')

    old_items = old.get('items') or {}
    new_items = new_lines_merged.get('items') or {}
    all_pids = set(old_items) | set(new_items)
    for pid in all_pids:
        name = product_names.get(pid, f'#{pid}')
        o = old_items.get(pid)
        n = new_items.get(pid)
        if o and not n:
            changes.append(f'إزالة الصنف «{name}»')
        elif n and not o:
            changes.append(f"إضافة الصنف «{name}» (الكمية {_format_num(n['qty'])} × {_format_num(n['price'])})")
        elif o and n and (abs(o['qty'] - n['qty']) > 0.001 or abs(o['price'] - n['price']) > 0.005 or abs(o.get('disc', 0) - n.get('disc', 0)) > 0.005):
            parts = []
            if abs(o['qty'] - n['qty']) > 0.001:
                parts.append(f"الكمية {_format_num(o['qty'])}←{_format_num(n['qty'])}")
            if abs(o['price'] - n['price']) > 0.005:
                parts.append(f"السعر {_format_num(o['price'])}←{_format_num(n['price'])}")
            if abs(o.get('disc', 0) - n.get('disc', 0)) > 0.005:
                parts.append(f"الخصم% {_format_num(o.get('disc', 0))}←{_format_num(n.get('disc', 0))}")
            changes.append(f"الصنف «{name}»: " + '، '.join(parts))

    return '؛ '.join(changes) if changes else 'تم الحفظ بدون تغييرات ظاهرة'


def _purge_linked_customer_payments(customer_id, invoice_number):
    """عند حذف فاتورة عميل: يحذف حركات كشف الحساب (النظام الحالي) المرتبطة بها تحديداً
    (الدفعات السريعة المسجَّلة من داخل الفاتورة)، ويُرجع إجمالي ما دُفع فعلياً من خلالها،
    حتى يُخصَم من صافي أثر الفاتورة على الرصيد بدلاً من حذفه بالكامل بشكل منفصل."""
    if not invoice_number:
        return 0.0
    linked = CustomerPayment.query.filter_by(customer_id=customer_id).filter(
        CustomerPayment.notes.like(f'%مرتبطة بفاتورة {invoice_number}')
    ).all()
    total = 0.0
    for p in linked:
        total += float(p.amount or 0)
        db.session.delete(p)
    return total


def _purge_linked_supplier_payments(supplier_id, invoice_number):
    """نفس فكرة _purge_linked_customer_payments لكن لفواتير الموردين."""
    if not invoice_number:
        return 0.0
    linked = SupplierPayment.query.filter_by(supplier_id=supplier_id).filter(
        SupplierPayment.notes.like(f'%مرتبطة بفاتورة {invoice_number}')
    ).all()
    total = 0.0
    for p in linked:
        total += float(p.amount or 0)
        db.session.delete(p)
    return total


# ترتيب أول صفحة يُسمح بها عند عدم صلاحية «لوحة التحكم» (تجنب حلقة إعادة التوجيه)
_SAFE_HOME_FALLBACK_ROUTES = [
    ('sales', 'sales'),
    ('purchases', 'purchases'),
    ('returns', 'sale_returns'),
    ('inventory', 'inventory'),
    ('transfers', 'transfers'),
    ('adjust_stock', 'adjust_stock'),
    ('customers', 'customers'),
    ('suppliers', 'suppliers'),
    ('employees', 'employees'),
    ('expenses', 'expenses'),
    ('products', 'products'),
    ('categories', 'categories'),
    ('reports', 'report_dashboard'),
    ('settings', 'branches'),
    ('settings_branding', 'app_settings'),
    ('settings_database', 'database_admin'),
    ('users', 'users'),
    ('backup', 'backup'),
]


def safe_home_url_for(user):
    """أول وجهة آمنة بعد الدخول أو عند رفض صلاحية الصفحة — لا يُعاد التوجيه إلى لوحة تحكم غير مسموحة."""
    if not user or not user.is_authenticated:
        return url_for('login')
    if user_can(user, 'dashboard'):
        return url_for('dashboard')
    for perm, endpoint in _SAFE_HOME_FALLBACK_ROUTES:
        if user_can(user, perm):
            return url_for(endpoint)
    return url_for('access_restricted')


def path_required_permission(path: str):
    """أول بادئة مطابقة تحدد الصلاحية المطلوبة؛ None = يكفي تسجيل الدخول."""
    p = (path or '').rstrip('/') or '/'
    if p.startswith('/static'):
        return None
    if p == '/access-restricted' or p.startswith('/access-restricted/'):
        return None
    rules = [
        ('/settings/users', 'users'),
        ('/settings/connected-users', 'connected_users'),
        ('/settings/backup', 'backup'),
        ('/settings/branches', 'settings'),
        ('/settings/warehouses', 'settings'),
        ('/settings/sale-tax', 'settings'),
        ('/settings/app', 'settings_branding'),
        ('/settings/database', 'settings_database'),
        ('/returns/sale', 'returns'),
        ('/returns/purchase', 'returns'),
        ('/inventory/adjust', 'adjust_stock'),
        ('/inventory/memos', 'inventory'),
        ('/transfers', 'transfers'),
        ('/inventory', 'inventory'),
        ('/customers', 'customers'),
        ('/suppliers', 'suppliers'),
        ('/employees', 'employees'),
        ('/expenses', 'expenses'),
        ('/categories', 'categories'),
        ('/products', 'products'),
        ('/sales', 'sales'),
        ('/purchases', 'purchases'),
        # صلاحيات التقارير بالتفصيل
        ('/reports/dashboard', 'reports_dashboard'),
        ('/reports/sales', 'report_sales'),
        ('/reports/purchases', 'report_purchases'),
        ('/reports/inventory', 'report_inventory'),
        ('/reports/customers', 'report_customers'),
        ('/reports/suppliers', 'report_suppliers'),
        ('/reports/expenses', 'report_expenses'),
        ('/reports/profit', 'report_profit'),
        ('/reports/low-stock', 'report_low_stock'),
        ('/reports/stock-adjustments', 'report_stock_adjustments'),
        ('/reports', 'reports'),
        ('/about', 'dashboard'),
    ]
    for prefix, perm in rules:
        if p == prefix or p.startswith(prefix + '/'):
            return perm
    if p == '/' or p == '':
        return 'dashboard'
    return None


def get_app_settings_dict(branch_id=None):
    out = {**DEFAULT_SETTINGS, **EXTRA_APP_SETTINGS_DEFAULTS}
    try:
        branch_rows = {}
        for row in AppSetting.query.all():
            k = (row.key or '').strip()
            if not k:
                continue
            m = re.match(r'^br(\d+)_(.+)$', k, re.I)
            if m:
                bid = int(m.group(1))
                sub = m.group(2)
                branch_rows.setdefault(bid, {})[sub] = row.value or ''
            else:
                if k in out or k in GLOBAL_ONLY_SETTING_KEYS:
                    out[k] = row.value or ''
        if branch_id and branch_id in branch_rows:
            for sk, sv in branch_rows[branch_id].items():
                if sk in BRANCH_SCOPED_SETTING_KEYS:
                    out[sk] = sv
    except Exception:
        return {**DEFAULT_SETTINGS, **EXTRA_APP_SETTINGS_DEFAULTS}
    return out


BACKUPS_DIR = os.path.join(_INSTANCE_DIR, 'backups')
os.makedirs(BACKUPS_DIR, exist_ok=True)


def permission_keys_for_editor(viewer):
    if not viewer or not getattr(viewer, 'is_authenticated', False):
        return [x for x in PERMISSION_KEYS if x[0] not in DEVELOPER_ONLY_PERMS]
    if getattr(viewer, 'role', None) == 'developer':
        return list(PERMISSION_KEYS)
    return [x for x in PERMISSION_KEYS if x[0] not in DEVELOPER_ONLY_PERMS]


def default_permissions_json_for_editor(viewer):
    keys_visible = frozenset(k for k, _ in permission_keys_for_editor(viewer))
    roles = ['user', 'manager', 'admin']
    if getattr(viewer, 'role', None) == 'developer':
        roles.append('developer')
    return {r: sorted(default_role_permission_set(r) & keys_visible) for r in roles}


def resolve_sqlite_main_path():
    try:
        u = make_url(app.config['SQLALCHEMY_DATABASE_URI'])
        if u.drivername != 'sqlite' or not u.database or u.database == ':memory:':
            return None
        dbn = u.database
        # مسار مطلق
        if os.path.isabs(dbn) or (len(dbn) > 2 and dbn[1] == ':'):
            # لو المسار موجود فعلاً ارجعه، وإلا ارجع None
            return dbn if os.path.isfile(dbn) else None
        path = os.path.abspath(os.path.join(_BASE_DIR, dbn))
        return path
    except Exception:
        return None


def _prune_old_backups(keep=25):
    try:
        files = sorted(
            [os.path.join(BACKUPS_DIR, f) for f in os.listdir(BACKUPS_DIR) if f.endswith('.db')],
            key=os.path.getmtime,
            reverse=True,
        )
        for f in files[keep:]:
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception:
        pass


def get_custom_backup_dir():
    """يرجع المجلد المخصص للنسخ الاحتياطي أو المجلد الافتراضي."""
    try:
        gs = get_app_settings_dict(branch_id=None)
        custom = (gs.get('backup_custom_dir') or '').strip()
        if custom and os.path.isdir(custom):
            return custom
    except Exception:
        pass
    return BACKUPS_DIR


def erp_backup(tag='manual'):
    """نسخ احتياطي يدعم SQLite وPostgreSQL."""
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest_dir = get_custom_backup_dir()
    os.makedirs(dest_dir, exist_ok=True)

    # SQLite
    if 'sqlite' in uri:
        src = resolve_sqlite_main_path()
        # لو المسار من الـ config مش موجود، نحاول المسار الافتراضي
        if not src or not os.path.isfile(src):
            fallback = os.path.join(_INSTANCE_DIR, 'erp.db')
            if os.path.isfile(fallback):
                src = fallback
            else:
                # بحث عن أي ملف .db في مجلد التطبيق
                for candidate in [
                    os.path.join(_BASE_DIR, 'erp.db'),
                    os.path.join(_BASE_DIR, 'instance', 'erp.db'),
                ]:
                    if os.path.isfile(candidate):
                        src = candidate
                        break
        if not src or not os.path.isfile(src):
            return None, f'ملف SQLite غير موجود — تحقق من مسار قاعدة البيانات في الإعدادات (المسار الحالي: {uri})'
        dest = os.path.join(dest_dir, f'erp_{tag}_{ts}.db')
        shutil.copy2(src, dest)
        _prune_old_backups(25)
        return dest, None

    # PostgreSQL
    if 'postgresql' in uri:
        import subprocess
        try:
            from sqlalchemy.engine.url import make_url as _mu
            u = _mu(uri)
            dest = os.path.join(dest_dir, f'erp_{tag}_{ts}.sql')
            env = os.environ.copy()
            if u.password:
                env['PGPASSWORD'] = str(u.password)
            cmd = ['pg_dump',
                   '-h', str(u.host or 'localhost'),
                   '-p', str(u.port or 5432),
                   '-U', str(u.username or 'postgres'),
                   '-F', 'p',  # plain SQL
                   '-f', dest,
                   str(u.database)]
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                return None, result.stderr[:300]
            _prune_old_backups(25)
            return dest, None
        except FileNotFoundError:
            return None, 'أداة pg_dump غير مثبتة على السيرفر'
        except Exception as ex:
            return None, str(ex)[:200]

    return None, 'نوع قاعدة البيانات غير مدعوم للنسخ الاحتياطي'


def run_database_optimize():
    """فحص وإصلاح الأداء: إضافة فهارس مفقودة + تحديث إحصائيات (ANALYZE) + تنظيف المساحة الفارغة (VACUUM).
    آمن 100% على البيانات: لا يحذف ولا يعدّل أي صف موجود — كل ما بيعمله إضافي (فهارس)
    أو تنظيمي بحت (ترتيب الملف واستعادة المساحة الفاضية من عمليات حذف قديمة)."""
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    is_sqlite_db = 'sqlite' in uri
    is_postgres_db = 'postgresql' in uri

    # فهارس على الأعمدة اللي بتتفلتر/تتربط عليها كتير في التقارير والبحث
    index_defs = [
        ('idx_sale_date', 'sale', 'date'),
        ('idx_sale_customer', 'sale', 'customer_id'),
        ('idx_sale_warehouse', 'sale', 'warehouse_id'),
        ('idx_sale_user', 'sale', 'user_id'),
        ('idx_sale_item_sale', 'sale_item', 'sale_id'),
        ('idx_sale_item_product', 'sale_item', 'product_id'),
        ('idx_purchase_date', 'purchase', 'date'),
        ('idx_purchase_supplier', 'purchase', 'supplier_id'),
        ('idx_purchase_warehouse', 'purchase', 'warehouse_id'),
        ('idx_purchase_item_purchase', 'purchase_item', 'purchase_id'),
        ('idx_purchase_item_product', 'purchase_item', 'product_id'),
        ('idx_sale_return_sale', 'sale_return', 'sale_id'),
        ('idx_sale_return_date', 'sale_return', 'date'),
        ('idx_sale_return_item_return', 'sale_return_item', 'return_id'),
        ('idx_sale_return_item_product', 'sale_return_item', 'product_id'),
        ('idx_purchase_return_purchase', 'purchase_return', 'purchase_id'),
        ('idx_purchase_return_date', 'purchase_return', 'date'),
        ('idx_purchase_return_item_return', 'purchase_return_item', 'return_id'),
        ('idx_purchase_return_item_product', 'purchase_return_item', 'product_id'),
        ('idx_stock_product', 'stock', 'product_id'),
        ('idx_stock_warehouse', 'stock', 'warehouse_id'),
        ('idx_stock_adj_product', 'stock_adjustment_log', 'product_id'),
        ('idx_stock_adj_warehouse', 'stock_adjustment_log', 'warehouse_id'),
        ('idx_stock_adj_created', 'stock_adjustment_log', 'created_at'),
        ('idx_inv_memo_warehouse', 'inventory_memo', 'warehouse_id'),
        ('idx_inv_memo_date', 'inventory_memo', 'date'),
        ('idx_inv_memo_item_memo', 'inventory_memo_item', 'memo_id'),
        ('idx_inv_memo_item_product', 'inventory_memo_item', 'product_id'),
        ('idx_transfer_from_wh', 'transfer_request', 'from_warehouse_id'),
        ('idx_transfer_to_wh', 'transfer_request', 'to_warehouse_id'),
        ('idx_transfer_status', 'transfer_request', 'status'),
        ('idx_transfer_date', 'transfer_request', 'date_requested'),
        ('idx_transfer_item_transfer', 'transfer_item', 'transfer_id'),
        ('idx_transfer_item_product', 'transfer_item', 'product_id'),
        ('idx_expense_date', 'expense', 'date'),
        ('idx_expense_branch', 'expense', 'branch_id'),
        ('idx_cust_payment_customer', 'customer_payment', 'customer_id'),
        ('idx_cust_payment_date', 'customer_payment', 'date'),
        ('idx_sup_payment_supplier', 'supplier_payment', 'supplier_id'),
        ('idx_sup_payment_date', 'supplier_payment', 'date'),
        ('idx_product_barcode', 'product', 'barcode'),
        ('idx_product_category', 'product', 'category_id'),
        ('idx_customer_name', 'customer', 'name'),
        ('idx_supplier_name', 'supplier', 'name'),
        ('idx_employee_branch', 'employee', 'branch_id'),
        ('idx_invoice_edit_log_ref', 'invoice_edit_log', 'invoice_id'),
    ]

    size_before = None
    main_path = None
    if is_sqlite_db:
        main_path = resolve_sqlite_main_path()
        if main_path and os.path.isfile(main_path):
            size_before = os.path.getsize(main_path)

    # نقفل أي جلسة/معاملة مفتوحة الأول، عشان VACUUM ما ينفعش يشتغل جوه معاملة
    db.session.remove()

    t0 = time_module.time()
    indexes_created = 0
    indexes_skipped = 0

    raw_conn = db.engine.raw_connection()
    try:
        if is_sqlite_db:
            raw_conn.isolation_level = None  # autocommit — كل أمر بيتنفذ وينحفظ فورًا
        else:
            raw_conn.autocommit = True
        cur = raw_conn.cursor()
        for idx_name, table, col in index_defs:
            try:
                cur.execute(f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({col})')
                indexes_created += 1
            except Exception:
                indexes_skipped += 1
        cur.execute('ANALYZE')
        cur.execute('VACUUM')
        cur.close()
    finally:
        raw_conn.close()

    size_after = None
    if is_sqlite_db and main_path and os.path.isfile(main_path):
        size_after = os.path.getsize(main_path)

    return {
        'indexes_created': indexes_created,
        'indexes_skipped': indexes_skipped,
        'size_before': size_before,
        'size_after': size_after,
        'duration': time_module.time() - t0,
    }


def sqlite_backup_to_folder(tag='manual'):
    """للتوافق مع الكود القديم — يستخدم erp_backup داخلياً."""
    dest, err = erp_backup(tag)
    return dest


def warehouse_has_operations(wh_id):
    if Sale.query.filter_by(warehouse_id=wh_id).first():
        return True
    if Purchase.query.filter_by(warehouse_id=wh_id).first():
        return True
    if InventoryMemo.query.filter_by(warehouse_id=wh_id).first():
        return True
    if TransferRequest.query.filter(
        db.or_(TransferRequest.from_warehouse_id == wh_id, TransferRequest.to_warehouse_id == wh_id)
    ).first():
        return True
    return False


def reset_operational_accounting_data():
    """مسح المبيعات والمشتريات والمرتجعات والتحويلات والمصاريف والدفعات وتصفير الأرصدة والمخزون."""
    db.session.query(InventoryMemoItem).delete(synchronize_session=False)
    db.session.query(InventoryMemo).delete(synchronize_session=False)
    db.session.query(StockAdjustmentLog).delete(synchronize_session=False)
    db.session.query(SaleReturnItem).delete(synchronize_session=False)
    db.session.query(SaleReturn).delete(synchronize_session=False)
    db.session.query(PurchaseReturnItem).delete(synchronize_session=False)
    db.session.query(PurchaseReturn).delete(synchronize_session=False)
    db.session.query(TransferItem).delete(synchronize_session=False)
    db.session.query(TransferRequest).delete(synchronize_session=False)
    db.session.query(SaleItem).delete(synchronize_session=False)
    db.session.query(Sale).delete(synchronize_session=False)
    db.session.query(PurchaseItem).delete(synchronize_session=False)
    db.session.query(Purchase).delete(synchronize_session=False)
    db.session.query(CustomerPayment).delete(synchronize_session=False)
    db.session.query(SupplierPayment).delete(synchronize_session=False)
    db.session.query(Expense).delete(synchronize_session=False)
    db.session.query(Customer).update({Customer.balance: 0}, synchronize_session=False)
    db.session.query(Supplier).update({Supplier.balance: 0}, synchronize_session=False)
    db.session.query(Stock).update({Stock.quantity: 0}, synchronize_session=False)
    db.session.commit()


def ensure_schema():
    from sqlalchemy import inspect, text
    try:
        insp = inspect(db.engine)
        tables = insp.get_table_names()
        if 'user' not in tables:
            return
        cols = {c['name'] for c in insp.get_columns('user')}
        if 'permissions' not in cols:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN permissions TEXT'))
        if 'transfer_request' in tables:
            tcols = {c['name'] for c in insp.get_columns('transfer_request')}
            if 'approver_user_id' not in tcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE transfer_request ADD COLUMN approver_user_id INTEGER'))
        if 'sale_return_item' in tables:
            rcols = {c['name'] for c in insp.get_columns('sale_return_item')}
            if 'discount' not in rcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE sale_return_item ADD COLUMN discount FLOAT DEFAULT 0'))
            if 'extra_discount' not in rcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE sale_return_item ADD COLUMN extra_discount FLOAT DEFAULT 0'))
        if 'purchase_return_item' in tables:
            prcols = {c['name'] for c in insp.get_columns('purchase_return_item')}
            if 'discount' not in prcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE purchase_return_item ADD COLUMN discount FLOAT DEFAULT 0'))
            if 'extra_discount' not in prcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE purchase_return_item ADD COLUMN extra_discount FLOAT DEFAULT 0'))
        if 'purchase' in tables:
            pcols = {c['name'] for c in insp.get_columns('purchase')}
            if 'withholding_tax' not in pcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE purchase ADD COLUMN withholding_tax FLOAT DEFAULT 0'))
        if 'sale' in tables:
            scols = {c['name'] for c in insp.get_columns('sale')}
            if 'payment_method' not in scols:
                with db.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE sale ADD COLUMN payment_method VARCHAR(20) DEFAULT 'cash'"))
        if 'user' in tables:
            ucols = {c['name'] for c in insp.get_columns('user')}
            utbl = '"user"' if insp.bind.dialect.name == 'postgresql' else 'user'
            if 'last_seen' not in ucols:
                ls_sql = 'TIMESTAMP' if insp.bind.dialect.name == 'postgresql' else 'DATETIME'
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {utbl} ADD COLUMN last_seen {ls_sql}'))
            if 'last_ip' not in ucols:
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {utbl} ADD COLUMN last_ip VARCHAR(64)'))
            if 'last_user_agent' not in ucols:
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {utbl} ADD COLUMN last_user_agent VARCHAR(256)'))
        try:
            db.create_all()
        except Exception:
            pass
        tables = insp.get_table_names()

        # ── إزالة نهائية لمخلّفات ميزة "الورديات" المحذوفة (best-effort) ──
        # الأعمدة/الجدول دول مبقاش ليهم أي مرجع في الكود، فبنشيلهم فعليًا من قاعدة
        # البيانات لو الـ driver بيدعم DROP COLUMN (SQLite 3.35+ أو PostgreSQL).
        # كل خطوة في try/except منفصلة عشان لو driver قديم مايدعمش الحذف، الباقي يكمل عادي.
        if 'sale' in tables:
            scols_now = {c['name'] for c in insp.get_columns('sale')}
            if 'shift_id' in scols_now:
                try:
                    with db.engine.begin() as conn:
                        conn.execute(text('ALTER TABLE sale DROP COLUMN shift_id'))
                except Exception:
                    pass
        if 'expense' in tables:
            excols_now = {c['name'] for c in insp.get_columns('expense')}
            if 'shift_id' in excols_now:
                try:
                    with db.engine.begin() as conn:
                        conn.execute(text('ALTER TABLE expense DROP COLUMN shift_id'))
                except Exception:
                    pass
        if 'cash_shift' in tables:
            try:
                with db.engine.begin() as conn:
                    conn.execute(text('DROP TABLE cash_shift'))
            except Exception:
                pass
        tables = insp.get_table_names()

        if 'employee' in tables:
            ecols = {c['name'] for c in insp.get_columns('employee')}
            emp_cols = [
                ('national_id', 'VARCHAR(30)'),
                ('address', 'TEXT'),
                ('photo', 'VARCHAR(300)'),
                ('manager_id', 'INTEGER'),
                ('user_id', 'INTEGER'),
                ('employment_status', "VARCHAR(30) DEFAULT 'active'"),
                ('contract_type', "VARCHAR(40) DEFAULT 'permanent'"),
            ]
            for col, ctype in emp_cols:
                if col not in ecols:
                    with db.engine.begin() as conn:
                        conn.execute(text(f'ALTER TABLE employee ADD COLUMN {col} {ctype}'))
    except Exception:
        pass


def normalize_license_serial(raw):
    return ''.join((raw or '').upper().replace('-', '').split())


def license_serial_hash(code_norm):
    pepper = app.config.get('SECRET_KEY', '')
    return hashlib.sha256((code_norm + '|' + pepper).encode('utf-8')).hexdigest()


def _parse_license_expiry(val):
    val = (val or '').strip()
    if not val:
        return None
    val = val.replace('T', ' ')
    for fmt, n in (('%Y-%m-%d %H:%M:%S', 19), ('%Y-%m-%d', 10)):
        try:
            return datetime.strptime(val[:n], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(val.replace('Z', ''))
    except ValueError:
        return None


def installation_license_valid():
    gs = get_app_settings_dict(branch_id=None)
    if gs.get('installation_license_permanent') == '1':
        return True
    dt = _parse_license_expiry(gs.get('installation_license_expires'))
    if not dt:
        return False
    return datetime.utcnow() < dt


def set_installation_license(permanent, expires_at):
    def _set(k, v):
        row = AppSetting.query.filter_by(key=k).first()
        if not row:
            row = AppSetting(key=k)
            db.session.add(row)
        row.value = v if v is not None else ''
    _set('installation_license_permanent', '1' if permanent else '0')
    if permanent:
        _set('installation_license_expires', '')
    else:
        _set('installation_license_expires', expires_at.strftime('%Y-%m-%d %H:%M:%S') if expires_at else '')


def subscription_status_for_ui():
    if not current_user.is_authenticated:
        return None
    gs = get_app_settings_dict(branch_id=None)
    lic_msg = (gs.get('license_expiry_message') or DEFAULT_SETTINGS.get('license_expiry_message', '') or '').strip()
    if getattr(current_user, 'role', None) == 'developer':
        return {'kind': 'developer', 'line': 'مفعّل — مطوّر', 'client_message': lic_msg}
    if gs.get('installation_license_permanent') == '1':
        return {'kind': 'permanent', 'line': 'ترخيص دائم', 'client_message': lic_msg}
    dt = _parse_license_expiry(gs.get('installation_license_expires'))
    if not dt:
        return {'kind': 'none', 'line': 'لم يُفعَّل', 'client_message': lic_msg}
    left = (dt - datetime.utcnow()).days
    if left < 0:
        return {'kind': 'expired', 'line': 'انتهى الاشتراك', 'client_message': lic_msg}
    if left == 0:
        hrs = (dt - datetime.utcnow()).seconds // 3600
        return {'kind': 'timed', 'line': f'أقل من يوم (~{hrs}س)', 'days': 0, 'expires': gs.get('installation_license_expires'), 'client_message': lic_msg}
    return {'kind': 'timed', 'line': f'متبقي {left} يوم', 'days': left, 'expires': gs.get('installation_license_expires'), 'client_message': lic_msg}


def expires_for_serial_plan(plan, custom_days):
    if plan == 'permanent':
        return None, True
    if plan == 'six_months':
        return datetime.utcnow() + timedelta(days=182), False
    if plan == 'one_year':
        return datetime.utcnow() + timedelta(days=365), False
    if plan == 'custom' and custom_days:
        return datetime.utcnow() + timedelta(days=max(1, int(custom_days))), False
    return datetime.utcnow() + timedelta(days=30), False


def generate_one_serial_string():
    a = secrets.token_hex(3).upper()
    b = secrets.token_hex(3).upper()
    c = secrets.token_hex(3).upper()
    return f'{a}-{b}-{c}'


def developer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or getattr(current_user, 'role', None) != 'developer':
            flash('هذه الشاشة للمطوّر فقط', 'error')
            return redirect(safe_home_url_for(current_user))
        return f(*args, **kwargs)
    return decorated


def get_next_number(prefix, model, field):
    last = db.session.query(model).order_by(db.desc(db.text('id'))).first()
    num = (last.id + 1) if last else 1
    return f"{prefix}{num:06d}"


def allocate_entity_code(prefix: str, model, field_name='code'):
    """كود تلقائي فريد (عملاء C، موردين S، موظفين E، أصناف P، …)."""
    max_id = db.session.query(db.func.max(model.id)).scalar() or 0
    n = max_id + 1
    for _ in range(10000):
        cand = f"{prefix}{n:05d}"
        if not db.session.query(model).filter(getattr(model, field_name) == cand).first():
            return cand
        n += 1
    return f"{prefix}{max_id + 1:05d}x"

# ===== CONTEXT PROCESSOR =====
def _pending_transfers_count_for_user(user):
    if not user or not user.is_authenticated:
        return 0
    q = TransferRequest.query.filter_by(status='pending')
    if user_can_approve_transfers(user):
        q = q.filter(
            db.or_(
                TransferRequest.approver_user_id.is_(None),
                TransferRequest.approver_user_id == user.id,
            )
        )
        return q.count()
    return 0


def visible_pending_transfers_query(user):
    q = TransferRequest.query.filter_by(status='pending')
    parts = [TransferRequest.requested_by == user.id]
    if user_can_approve_transfers(user):
        parts.append(
            db.or_(
                TransferRequest.approver_user_id.is_(None),
                TransferRequest.approver_user_id == user.id,
            )
        )
    return q.filter(db.or_(*parts))


def can_user_act_on_transfer(transfer, user) -> bool:
    if not user or not transfer or transfer.status != 'pending':
        return False
    if not user_can_approve_transfers(user):
        return False
    if transfer.requested_by == user.id:
        return False
    if transfer.approver_user_id and transfer.approver_user_id != user.id:
        return False
    return True


def purchase_line_effective_unit_price(purchase, line) -> float:
    sub = float(purchase.subtotal or 0)
    disc = float(purchase.discount or 0)
    if sub <= 0:
        return float(line.price)
    ratio = max(0.0, (sub - disc) / sub)
    return float(line.price) * ratio


def sale_discount_amount_total(sale):
    """مجموع خصومات السطور (نسبة) + خصم الفاتورة — للتقارير."""
    line_part = sum(
        float(it.quantity or 0) * float(it.price or 0) * float(it.discount or 0) / 100.0
        for it in (sale.items or [])
    )
    return line_part + float(sale.discount or 0)


def sale_returnable_quantity(sale, product_id: int) -> float:
    sold = sum(float(it.quantity or 0) for it in (sale.items or []) if it.product_id == int(product_id))
    back = 0.0
    for r in SaleReturn.query.filter_by(sale_id=sale.id).all():
        for ri in r.items:
            if ri.product_id == int(product_id):
                back += float(ri.quantity or 0)
    return max(0.0, sold - back)


def purchase_returnable_quantity(purchase, product_id: int) -> float:
    bought = sum(float(it.quantity or 0) for it in (purchase.items or []) if it.product_id == int(product_id))
    back = 0.0
    for r in PurchaseReturn.query.filter_by(purchase_id=purchase.id).all():
        for ri in r.items:
            if ri.product_id == int(product_id):
                back += float(ri.quantity or 0)
    return max(0.0, bought - back)


def _maybe_bump_last_seen():
    try:
        now = time_module.time()
        last = float(session.get('_ls_bump_at') or 0)
        if now - last < 50:
            return
        session['_ls_bump_at'] = now
        ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
        ua = (request.headers.get('User-Agent') or '')[:256]
        User.query.filter_by(id=current_user.id).update({
            'last_seen': datetime.utcnow(),
            'last_ip': ip,
            'last_user_agent': ua,
        })
        db.session.commit()
    except Exception:
        db.session.rollback()


@app.context_processor
def inject_globals():
    bid = getattr(current_user, 'branch_id', None) if current_user.is_authenticated else None
    pending = _pending_transfers_count_for_user(current_user) if current_user.is_authenticated else 0
    gs = get_app_settings_dict(branch_id=bid)
    def can(perm):
        return user_can(current_user, perm) if current_user.is_authenticated else False
    return dict(
        pending_transfers_global=pending, pending_transfers_count=pending,
        app_settings=gs, app_brand_title=gs.get('app_title', DEFAULT_SETTINGS['app_title']),
        app_company=gs.get('company_name', DEFAULT_SETTINGS['company_name']),
        app_subtitle_brand=gs.get('app_subtitle', DEFAULT_SETTINGS['app_subtitle']),
        layout_max_width=gs.get('layout_max_width', '1400px'),
        can=can, PERMISSION_KEYS=PERMISSION_KEYS,
        permission_keys_edit=permission_keys_for_editor(current_user) if current_user.is_authenticated else [x for x in PERMISSION_KEYS if x[0] not in DEVELOPER_ONLY_PERMS],
        subscription_status=subscription_status_for_ui() if current_user.is_authenticated else None,
        license_expiry_message_text=(get_app_settings_dict(branch_id=None).get('license_expiry_message') or DEFAULT_SETTINGS.get('license_expiry_message', '')),
        current_branch_id=bid,
        can_delete_users=user_can_delete_users_account(current_user) if current_user.is_authenticated else False,
    )


@app.before_request
def erp_sqlite_autobackup_start():
    if getattr(erp_sqlite_autobackup_start, '_started', False):
        return
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    erp_sqlite_autobackup_start._started = True
    # يدعم SQLite وPostgreSQL
    if ':memory:' in uri:
        return
    if 'sqlite' not in uri and 'postgresql' not in uri:
        return

    def _loop():
        time_module.sleep(60)  # انتظر دقيقة بعد البدء
        while True:
            try:
                with app.app_context():
                    # جلب وقت النسخ الاحتياطي من الإعدادات
                    gs = get_app_settings_dict(branch_id=None)
                    backup_time = (gs.get('backup_daily_time') or '02:00').strip()
                    try:
                        bh, bm = [int(x) for x in backup_time.split(':')]
                    except Exception:
                        bh, bm = 2, 0
                    now = datetime.now()
                    # حساب الوقت التالي للنسخ
                    next_run = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
                    if next_run <= now:
                        next_run = next_run.replace(day=now.day + 1) if now.day < 28 else next_run + timedelta(days=1)
                    wait_secs = (next_run - now).total_seconds()
                time_module.sleep(max(60, wait_secs))
                with app.app_context():
                    erp_backup('auto')
            except Exception:
                time_module.sleep(3600)

    threading.Thread(target=_loop, daemon=True).start()


@app.before_request
def enforce_route_permissions():
    if not getattr(enforce_route_permissions, '_schema_ok', False):
        enforce_route_permissions._schema_ok = True
        try:
            ensure_schema()
        except Exception:
            pass
    if request.endpoint == 'static' or request.endpoint == 'login':
        return
    if not current_user.is_authenticated:
        return
    _maybe_bump_last_seen()
    ep = request.endpoint or ''
    if getattr(current_user, 'role', None) != 'developer':
        if not installation_license_valid() and ep != 'license_activate':
            return redirect(url_for('license_activate'))
    if getattr(current_user, 'role', None) == 'developer':
        return
    p = request.path or ''
    if p.startswith('/api'):
        return
    need = path_required_permission(p)
    if need and not user_can(current_user, need):
        flash('لا صلاحية للوصول لهذه الصفحة', 'error')
        return redirect(safe_home_url_for(current_user))

# ===== AUTH ROUTES =====
@app.route('/access-restricted')
@login_required
def access_restricted():
    return render_template('access_restricted.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(safe_home_url_for(current_user))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']) and user.is_active:
            login_user(user)
            # تسجيل IP واسم الجهاز فور تسجيل الدخول
            try:
                ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
                ua = (request.headers.get('User-Agent') or '')[:256]
                user.last_seen = datetime.utcnow()
                user.last_ip = ip
                user.last_user_agent = ua
                db.session.commit()
            except Exception:
                db.session.rollback()
            return redirect(safe_home_url_for(user))
        flash('اسم المستخدم أو كلمة المرور غير صحيحة', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/license/activate', methods=['GET', 'POST'])
@login_required
def license_activate():
    if getattr(current_user, 'role', None) == 'developer':
        return redirect(safe_home_url_for(current_user))
    if installation_license_valid():
        return redirect(safe_home_url_for(current_user))
    if request.method == 'POST':
        raw = (request.form.get('serial') or '').strip()
        norm = normalize_license_serial(raw)
        if len(norm) < 6:
            flash('أدخل سريالاً صالحاً', 'error')
            return render_template('license_activate.html')
        h = license_serial_hash(norm)
        if LicenseUsedSerial.query.filter_by(code_hash=h).first():
            flash('تم استخدام هذا السريال مسبقاً على هذا النظام أو جهاز آخر', 'error')
            return render_template('license_activate.html')
        pool = LicensePoolSerial.query.filter_by(code_norm=norm).first()
        if not pool:
            flash('السريال غير صالح أو غير موجود في قائمة الترخيص', 'error')
            return render_template('license_activate.html')
        plan = pool.plan or 'one_year'
        custom_days = pool.custom_days
        exp, perm = expires_for_serial_plan(plan, custom_days)
        db.session.delete(pool)
        db.session.add(LicenseUsedSerial(
            code_hash=h,
            code_hint=norm[-10:] if len(norm) >= 10 else norm,
            plan=plan,
            expires_at=exp,
        ))
        set_installation_license(perm, exp)
        db.session.commit()
        flash('تم تفعيل الترخيص بنجاح', 'success')
        return redirect(safe_home_url_for(current_user))
    return render_template('license_activate.html')


@app.route('/license/admin')
@login_required
@developer_required
def license_admin():
    pool = LicensePoolSerial.query.order_by(LicensePoolSerial.created_at.desc()).all()
    used = LicenseUsedSerial.query.order_by(LicenseUsedSerial.activated_at.desc()).all()
    return render_template('license_admin.html', pool=pool, used=used)


@app.route('/license/admin/generate', methods=['POST'])
@login_required
@developer_required
def license_admin_generate():
    try:
        n = min(500, max(1, int(request.form.get('count', 1))))
    except (TypeError, ValueError):
        n = 1
    plan = request.form.get('plan') or 'one_year'
    if plan not in ('six_months', 'one_year', 'permanent', 'custom'):
        plan = 'one_year'
    custom_days = None
    if plan == 'custom':
        try:
            custom_days = max(1, int(request.form.get('custom_days', 30)))
        except (TypeError, ValueError):
            custom_days = 30
    note = (request.form.get('note') or '').strip()[:200]
    created = 0
    for _ in range(n):
        for attempt in range(50):
            s = generate_one_serial_string()
            norm = normalize_license_serial(s)
            if LicensePoolSerial.query.filter_by(code_norm=norm).first():
                continue
            db.session.add(LicensePoolSerial(code=s, code_norm=norm, plan=plan, custom_days=custom_days, note=note or None))
            created += 1
            break
    db.session.commit()
    flash(f'تم إنشاء {created} سريال', 'success')
    return redirect(url_for('license_admin'))


@app.route('/license/admin/message', methods=['POST'])
@login_required
@developer_required
def license_admin_save_message():
    msg = (request.form.get('license_expiry_message') or '').strip()
    row = AppSetting.query.filter_by(key='license_expiry_message').first()
    if not row:
        row = AppSetting(key='license_expiry_message')
        db.session.add(row)
    row.value = msg[:4000]
    db.session.commit()
    flash('تم حفظ رسالة انتهاء الاشتراك للعميل', 'success')
    return redirect(url_for('license_admin'))


@app.route('/license/admin/end-subscription', methods=['POST'])
@login_required
@developer_required
def license_admin_end_subscription():
    # مسح السريال المفعل من قاعدة البيانات — يُطلب سريال جديد عند الدخول
    row = AppSetting.query.filter_by(key='license_serial').first()
    if row:
        db.session.delete(row)
    row2 = AppSetting.query.filter_by(key='license_activated_at').first()
    if row2:
        db.session.delete(row2)
    row3 = AppSetting.query.filter_by(key='license_expires_at').first()
    if row3:
        db.session.delete(row3)
    db.session.commit()
    flash('تم إنهاء الاشتراك الحالي — سيُطلب سريال جديد عند الدخول', 'success')
    return redirect(url_for('license_admin'))


@app.route('/about')
@login_required
def about():
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    return render_template('about.html', settings=gs)

# ===== DASHBOARD =====
@app.route('/')
@login_required
def dashboard():
    today = date.today()
    sales_today = db.session.query(db.func.sum(Sale.total)).filter(
        db.func.date(Sale.date) == today).scalar() or 0
    sale_returns_today = db.session.query(db.func.sum(SaleReturn.total)).filter(
        db.func.date(SaleReturn.date) == today).scalar() or 0
    net_sales_after_returns = float(sales_today) - float(sale_returns_today)
    purchases_today = db.session.query(db.func.sum(Purchase.total)).filter(
        db.func.date(Purchase.date) == today).scalar() or 0
    customers_count = sale_returns_today
    products_count = net_sales_after_returns
    pending_transfers = _pending_transfers_count_for_user(current_user)
    low_stock = db.session.query(Stock).join(Product).filter(
        Stock.quantity <= Product.min_stock, Product.min_stock > 0, Product.is_active == True).count()
    recent_sales = Sale.query.order_by(Sale.date.desc()).limit(5).all()
    recent_transfers = visible_pending_transfers_query(current_user).order_by(
        TransferRequest.date_requested.desc()).limit(5).all()
    return render_template('dashboard.html',
        sales_today=sales_today, purchases_today=purchases_today,
        customers_count=customers_count, products_count=products_count,
        pending_transfers=pending_transfers, low_stock=low_stock,
        recent_sales=recent_sales, recent_transfers=recent_transfers, now=date.today())

# ===== PRODUCTS =====
@app.route('/products')
@login_required
def products():
    q = request.args.get('q', '')
    query = Product.query
    if q:
        query = query.filter(db.or_(Product.name.contains(q), Product.code.contains(q)))
    products = query.filter_by(is_active=True).all()
    categories = Category.query.all()
    return render_template('products.html', products=products, categories=categories, q=q)

@app.route('/products/add', methods=['GET', 'POST'])
@login_required
@product_add_required
def add_product():
    if request.method == 'POST':
        wh_id = request.form.get('warehouse_id')
        if not wh_id:
            flash('اختر المخزن الذي يُسجَّل فيه الصنف', 'error')
            categories = Category.query.all()
            warehouses = Warehouse.query.filter_by(is_active=True).all()
            return render_template('product_form.html', categories=categories, warehouses=warehouses)
        code_input = (request.form.get('code') or '').strip()
        name_val = request.form.get('name')
        if not name_val:
            flash('يرجى إدخال اسم الصنف', 'error')
            categories = Category.query.all()
            warehouses = Warehouse.query.filter_by(is_active=True).all()
            return render_template('product_form.html', categories=categories, warehouses=warehouses)
        try:
            cost_price_val = float(request.form.get('cost_price', 0) or 0)
            sell_price_val = float(request.form.get('sell_price', 0) or 0)
            min_stock_val = float(request.form.get('min_stock', 0) or 0)
            wh_id_int = int(wh_id)
        except (ValueError, TypeError):
            flash('توجد بيانات غير صحيحة (سعر/كمية) — يرجى المراجعة والمحاولة مرة أخرى', 'error')
            categories = Category.query.all()
            warehouses = Warehouse.query.filter_by(is_active=True).all()
            return render_template('product_form.html', categories=categories, warehouses=warehouses)
        barcode_val = request.form.get('barcode')
        category_id_val = request.form.get('category_id') or None
        unit_val = request.form.get('unit', 'قطعة')
        description_val = request.form.get('description')

        # ── حفظ مع إعادة محاولة آمنة عند تعارض الكود (نفس الكود اتاخد قبل ما تحفظ) ──
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            code = code_input or allocate_entity_code('P', Product)
            product = Product(code=code, name=name_val, barcode=barcode_val, category_id=category_id_val,
                               unit=unit_val, cost_price=cost_price_val, sell_price=sell_price_val,
                               min_stock=min_stock_val, description=description_val)
            db.session.add(product)
            try:
                db.session.flush()
                stock = Stock(product_id=product.id, warehouse_id=wh_id_int, quantity=0)
                db.session.add(stock)
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                categories = Category.query.all()
                warehouses = Warehouse.query.filter_by(is_active=True).all()
                if code_input:
                    flash(f'الكود «{code_input}» مستخدم بالفعل لصنف آخر — يرجى اختيار كود مختلف', 'error')
                    suggested = allocate_entity_code('P', Product)
                    return render_template('product_form.html', categories=categories, warehouses=warehouses, suggested_code=suggested)
                if attempt == max_attempts:
                    flash('تعذّر إضافة الصنف بسبب تعارض في ترقيم الأكواد — يرجى المحاولة مرة أخرى', 'error')
                    suggested = allocate_entity_code('P', Product)
                    return render_template('product_form.html', categories=categories, warehouses=warehouses, suggested_code=suggested)
                continue
            except SQLAlchemyError:
                db.session.rollback()
                categories = Category.query.all()
                warehouses = Warehouse.query.filter_by(is_active=True).all()
                flash('حدث خطأ غير متوقع أثناء إضافة الصنف — لم يتم حفظ أي بيانات', 'error')
                suggested = allocate_entity_code('P', Product)
                return render_template('product_form.html', categories=categories, warehouses=warehouses, suggested_code=suggested)

        flash('تم إضافة الصنف بنجاح', 'success')
        return redirect(url_for('products'))
    categories = Category.query.all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    suggested = allocate_entity_code('P', Product)
    return render_template('product_form.html', categories=categories, warehouses=warehouses, suggested_code=suggested)

@app.route('/products/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_product(id):
    product = Product.query.get_or_404(id)
    if request.method == 'POST':
        product.code = request.form['code']
        product.name = request.form['name']
        product.barcode = request.form.get('barcode')
        product.category_id = request.form.get('category_id') or None
        product.unit = request.form.get('unit', 'قطعة')
        product.cost_price = float(request.form.get('cost_price', 0))
        product.sell_price = float(request.form.get('sell_price', 0))
        product.min_stock = float(request.form.get('min_stock', 0))
        product.description = request.form.get('description')
        db.session.commit()
        flash('تم تحديث الصنف بنجاح', 'success')
        return redirect(url_for('products'))
    categories = Category.query.all()
    return render_template('product_form.html', product=product, categories=categories)

@app.route('/products/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_product(id):
    product = Product.query.get_or_404(id)
    product.is_active = False
    Stock.query.filter_by(product_id=product.id).delete()
    db.session.commit()
    flash('تم حذف الصنف وإزالة أرصدته من المخازن', 'success')
    return redirect(url_for('products'))


# ===== PRODUCTS IMPORT (EXCEL) — للمطوّر فقط =====
_PRODUCT_IMPORT_HEADER_ALIASES = {
    'code':    ['كود', 'كود الصنف', 'code'],
    'name':    ['اسم الصنف', 'اسم الصنـف', 'اسم الصنــــف', 'الصنف', 'name'],
    'qty':     ['الرصيد الافتتاحي', 'الرصيد', 'الكمية', 'qty', 'quantity'],
    'warehouse': ['مخزن', 'المخزن', 'اسم المخزن', 'warehouse'],
    'unit':    ['الوحدة', 'وحدة', 'unit'],
    'cost':    ['سعر التكلفة', 'التكلفة', 'cost', 'cost_price'],
    'sell':    ['سعر البيع', 'البيع', 'sell', 'sell_price', 'price'],
    'min_stock': ['الحد الأدنى للمخزون', 'الحد الادنى للمخزون', 'الحد الأدنى', 'min_stock'],
}


def _match_import_columns(header_row):
    """يحدد فهرس كل عمود بمطابقة عناوين الصف الأول مع الأسماء المعروفة.
    ملاحظة: يتجاهل العناوين القصيرة جداً (مثل عمود التسلسل «م») حتى لا تتطابق
    خطأً كجزء نصي من أسماء أعمدة أخرى، ويعتمد فقط على احتواء العنوان الفعلي
    على اسم العمود المعروف (وليس العكس)، ولا يُسند نفس العمود لأكثر من حقل."""
    mapping = {}
    used_idx = set()
    for idx, cell in enumerate(header_row):
        title = (str(cell).strip() if cell is not None else '')
        if len(title) < 3 or idx in used_idx:
            continue
        for key, aliases in _PRODUCT_IMPORT_HEADER_ALIASES.items():
            if key in mapping:
                continue
            matched = False
            for alias in aliases:
                a = alias.strip()
                if len(a) < 3:
                    continue
                if a in title or title in a:
                    mapping[key] = idx
                    used_idx.add(idx)
                    matched = True
                    break
            if matched:
                break
    return mapping


@app.route('/products/import', methods=['GET', 'POST'])
@login_required
@developer_required
def import_products():
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    if request.method == 'GET':
        return render_template('product_import.html', warehouses=warehouses)

    if openpyxl is None:
        flash('تعذّر الاستيراد: مكتبة openpyxl غير مثبّتة على الخادم', 'error')
        return redirect(url_for('import_products'))

    f = request.files.get('file')
    if not f or not f.filename:
        flash('يرجى اختيار ملف إكسيل (.xlsx)', 'error')
        return redirect(url_for('import_products'))
    if not f.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash('امتداد الملف يجب أن يكون .xlsx', 'error')
        return redirect(url_for('import_products'))

    default_wh_id = request.form.get('default_warehouse_id') or None
    default_unit = (request.form.get('default_unit') or 'قطعة').strip() or 'قطعة'
    default_min_stock = request.form.get('default_min_stock', '0')
    try:
        default_min_stock_val = float(default_min_stock or 0)
    except ValueError:
        default_min_stock_val = 0

    tmp = os.path.join(_INSTANCE_DIR, '_products_import_upload.xlsx')
    try:
        f.save(tmp)
        wb = openpyxl.load_workbook(tmp, data_only=True)
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            flash('الملف فارغ', 'error')
            return redirect(url_for('import_products'))
        cols = _match_import_columns(header_row)
        if 'name' not in cols:
            flash('تعذّر التعرّف على عمود «اسم الصنف» في الملف — تأكد من وجود صف عناوين صحيح', 'error')
            return redirect(url_for('import_products'))

        # ذاكرة تخزين مؤقت للمخازن المُنشأة/المطابقة بالاسم أثناء هذا الاستيراد
        warehouse_cache = {}

        def resolve_warehouse(name):
            name = (str(name).strip() if name else '')
            if not name:
                return None
            if name in warehouse_cache:
                return warehouse_cache[name]
            wh = Warehouse.query.filter_by(name=name).first()
            if not wh:
                wh = Warehouse(name=name, is_active=True)
                db.session.add(wh)
                db.session.flush()
            warehouse_cache[name] = wh
            return wh

        existing_products = Product.query.all()
        products_by_code = {p.code: p for p in existing_products}
        products_by_name = {}
        for p in existing_products:
            products_by_name.setdefault((p.name or '').strip(), p)
        seen_codes_this_import = set()
        added = 0
        updated = 0
        skipped = 0
        errors = []

        for row_num, row in enumerate(rows_iter, start=2):
            if row is None or all(v is None or str(v).strip() == '' for v in row):
                continue
            try:
                name_val = row[cols['name']] if cols.get('name') is not None and cols['name'] < len(row) else None
                name_val = (str(name_val).strip() if name_val is not None else '')
                if not name_val:
                    skipped += 1
                    continue

                code_val = None
                if cols.get('code') is not None and cols['code'] < len(row):
                    raw_code = row[cols['code']]
                    if raw_code not in (None, ''):
                        code_val = str(raw_code).strip()
                        try:
                            if float(code_val) == int(float(code_val)):
                                code_val = f"{int(float(code_val)):05d}"
                        except ValueError:
                            pass

                # نبحث عن الصنف الموجود بالفعل لتحديثه بدلاً من تكراره:
                # أولاً بمطابقة الكود، وإن لم يوجد أو كان الكود مستخدَماً بالفعل
                # ضمن هذا الملف نفسه (تعارض داخل الملف)، نطابق باسم الصنف تحديداً.
                product = None
                if code_val and code_val not in seen_codes_this_import:
                    product = products_by_code.get(code_val)
                if product is None:
                    product = products_by_name.get(name_val)

                if not code_val or code_val in seen_codes_this_import:
                    # لا يوجد كود بالملف، أو الكود مكرر داخل نفس الملف لصنف آخر
                    code_val = product.code if product else allocate_entity_code('P', Product)
                seen_codes_this_import.add(code_val)

                unit_val = default_unit
                if cols.get('unit') is not None and cols['unit'] < len(row):
                    raw_unit = row[cols['unit']]
                    if raw_unit not in (None, ''):
                        unit_val = str(raw_unit).strip()

                def _num(key):
                    idx = cols.get(key)
                    if idx is None or idx >= len(row):
                        return 0.0
                    v = row[idx]
                    if v in (None, ''):
                        return 0.0
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        return 0.0

                cost_val = _num('cost')
                sell_val = _num('sell')
                qty_val = _num('qty')
                min_stock_val = _num('min_stock') or default_min_stock_val

                if product:
                    # تحديث الصنف الموجود بدلاً من إنشاء نسخة جديدة مكررة
                    product.name = name_val
                    product.unit = unit_val
                    product.cost_price = cost_val
                    product.sell_price = sell_val
                    product.min_stock = min_stock_val
                    updated += 1
                else:
                    product = Product(code=code_val, name=name_val, unit=unit_val,
                                       cost_price=cost_val, sell_price=sell_val,
                                       min_stock=min_stock_val)
                    db.session.add(product)
                    db.session.flush()
                    added += 1

                products_by_code[product.code] = product
                products_by_name[name_val] = product

                wh = None
                if cols.get('warehouse') is not None and cols['warehouse'] < len(row):
                    wh = resolve_warehouse(row[cols['warehouse']])
                if not wh and default_wh_id:
                    wh = Warehouse.query.get(int(default_wh_id))
                if wh:
                    # نحدّث كمية المخزون الموجودة بدلاً من إضافة سطر جديد لنفس الصنف/المخزن
                    stock = Stock.query.filter_by(product_id=product.id, warehouse_id=wh.id).first()
                    if stock:
                        stock.quantity = qty_val
                    else:
                        stock = Stock(product_id=product.id, warehouse_id=wh.id, quantity=qty_val)
                        db.session.add(stock)
            except Exception as e:
                skipped += 1
                errors.append(f'صف {row_num}: {e}')

        db.session.commit()
        msg = f'تم الاستيراد: {added} صنف جديد'
        if updated:
            msg += f'، وتحديث {updated} صنف موجود مسبقاً'
        if skipped:
            msg += f' — وتم تجاوز {skipped} صف (فارغ أو به خطأ)'
        flash(msg, 'success' if (added or updated) else 'error')
        if errors:
            flash('أول الأخطاء: ' + ' | '.join(errors[:5]), 'error')
        return redirect(url_for('products'))
    except Exception as e:
        db.session.rollback()
        flash(f'فشل استيراد الملف: {e}', 'error')
        return redirect(url_for('import_products'))
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ===== BARCODE LABEL PRINTING (طباعة ملصقات باركود) =====

BARCODE_LABEL_SIZE_PRESETS = {
    '40x30':   {'label': '40×30 مم (عامة)',              'w_mm': 40,   'h_mm': 30,   'cols': 5},
    '50x30':   {'label': '50×30 مم (عامة)',              'w_mm': 50,   'h_mm': 30,   'cols': 4},
    '38x25':   {'label': '38×25 مم (حرارية ضيقة)',       'w_mm': 38,   'h_mm': 25,   'cols': 5},
    '57x32':   {'label': '57×32 مم (حرارية قياسية)',     'w_mm': 57,   'h_mm': 32,   'cols': 3},
    '80x50':   {'label': '80×50 مم (حرارية كبيرة)',      'w_mm': 80,   'h_mm': 50,   'cols': 2},
    '60x40':   {'label': '60×40 مم',                     'w_mm': 60,   'h_mm': 40,   'cols': 3},
    '100x50':  {'label': '100×50 مم (شحن/لوجستيات)',    'w_mm': 100,  'h_mm': 50,   'cols': 2},
    'a4-3col': {'label': 'A4 — 3 أعمدة (63.5×38.1 مم)', 'w_mm': 63.5, 'h_mm': 38.1, 'cols': 3},
    'a4-4col': {'label': 'A4 — 4 أعمدة (48×25 مم)',     'w_mm': 48,   'h_mm': 25,   'cols': 4},
}

# الصيغ المسموح بها
ALLOWED_BARCODE_FORMATS = {'AUTO', 'CODE128', 'EAN13', 'EAN8', 'UPC', 'UPCE', 'CODE39'}

# رمز العملة الافتراضي (يمكن تعديله من الإعدادات العامة)
DEFAULT_CURRENCY = 'ريال'


def _detect_barcode_format(code: str) -> str:
    """
    اكتشاف صيغة الباركود تلقائيًا حسب نوع الكود.
    القاعدة: كل ما لا يناسب EAN/UPC بشكل حصري → CODE128 (الأكثر توافقاً).
    """
    import re
    code = str(code or '').strip()
    if re.fullmatch(r'\d{13}', code): return 'EAN13'
    if re.fullmatch(r'\d{12}', code): return 'UPC'
    if re.fullmatch(r'\d{8}',  code): return 'EAN8'
    # CODE39 فقط للأكواد التي تبدأ بحرف كبير — الأرقام القصيرة مثل 5544 تذهب لـ CODE128
    if re.fullmatch(r'[A-Z][A-Z0-9\-\.\$\/\+\% ]{2,42}', code): return 'CODE39'
    return 'CODE128'


def _calc_label_fonts(preset: dict, show_name: bool, show_price: bool, show_code: bool) -> dict:
    """
    حساب أحجام الخطوط بناءً على أبعاد الملصق.
    يعيد قاموسًا بأحجام البيكسل لكل عنصر.
    """
    h = preset['h_mm']
    w = preset['w_mm']

    # نسبة التحجيم: ملصق صغير = خطوط أصغر
    scale = min(h / 30.0, w / 40.0, 1.3)

    def s(base):
        return round(base * scale, 1)

    return {
        'name_font'     : s(8.5)  if show_name  else 0,
        'bnum_font'     : s(7.0),   # رقم الباركود (دائماً)
        'code_font'     : s(7.0)  if show_code  else 0,
        'price_font'    : s(9.5)  if show_price else 0,
        'currency_font' : s(7.0)  if show_price else 0,
    }


@app.route('/products/barcode-labels')
@login_required
def barcode_labels():
    """صفحة اختيار الأصناف وإعداد طباعة ملصقات الباركود."""
    preselect = None
    pid = request.args.get('product_id', type=int)
    if pid:
        p = Product.query.filter_by(id=pid, is_active=True).first()
        if p:
            preselect = {
                'id': p.id, 'name': p.name, 'code': p.code,
                'barcode': (p.barcode or p.code or ''), 'price': p.sell_price,
            }
    size_options = [{'key': k, **v} for k, v in BARCODE_LABEL_SIZE_PRESETS.items()]
    # رمز العملة من الإعدادات العامة أو الافتراضي
    currency_row = AppSetting.query.filter_by(key='currency_symbol').first()
    default_currency = (currency_row.value if currency_row and currency_row.value else DEFAULT_CURRENCY)
    return render_template(
        'barcode_labels.html',
        preselect=preselect,
        size_options=size_options,
        default_currency=default_currency,
    )


@app.route('/products/barcode-labels/print', methods=['POST'])
@login_required
def barcode_labels_print():
    product_ids  = request.form.getlist('product_id[]')
    quantities   = request.form.getlist('qty[]')
    size_key     = request.form.get('label_size') or '40x30'
    barcode_fmt  = (request.form.get('barcode_fmt') or 'AUTO').upper().strip()
    currency     = (request.form.get('currency') or DEFAULT_CURRENCY).strip()[:15]
    show_name    = request.form.get('show_name')  == '1'
    show_price   = request.form.get('show_price') == '1'
    show_code    = request.form.get('show_code')  == '1'

    if size_key not in BARCODE_LABEL_SIZE_PRESETS:
        size_key = '40x30'
    if barcode_fmt not in ALLOWED_BARCODE_FORMATS:
        barcode_fmt = 'AUTO'

    labels = []
    for i, pid in enumerate(product_ids):
        if not pid:
            continue
        try:
            pid_int = int(pid)
            qty_raw = quantities[i] if i < len(quantities) else '1'
            qty = max(1, min(int(float(qty_raw) if qty_raw not in (None, '') else 1), 500))
        except (ValueError, TypeError, IndexError):
            continue
        product = Product.query.filter_by(id=pid_int, is_active=True).first()
        if not product:
            continue
        code_val = (product.barcode or product.code or '').strip()
        if not code_val:
            continue
        # تحديد الصيغة لكل ملصق
        fmt = _detect_barcode_format(code_val) if barcode_fmt == 'AUTO' else barcode_fmt
        for _ in range(qty):
            labels.append({
                'name'   : product.name,
                'code'   : product.code or '',
                'barcode': code_val,
                'price'  : product.sell_price or 0,
                'fmt'    : fmt,
            })

    if not labels:
        flash('يرجى اختيار صنف واحد على الأقل له كود أو باركود صالح لطباعته', 'error')
        return redirect(url_for('barcode_labels'))

    if len(labels) > 1000:
        labels = labels[:1000]
        flash('تم الاكتفاء بأول 1000 ملصق — يرجى تقسيم الطباعة لدفعات أصغر', 'warning')

    preset    = BARCODE_LABEL_SIZE_PRESETS[size_key]
    fonts     = _calc_label_fonts(preset, show_name, show_price, show_code)
    is_sheet  = preset['cols'] > 1

    return render_template(
        'barcode_labels_print.html',
        labels=labels,
        preset=preset,
        is_sheet=is_sheet,
        show_name=show_name,
        show_price=show_price,
        show_code=show_code,
        currency=currency,
        auto_print=False,
        **fonts,
    )

@app.route('/products/search')
@login_required
def products_search():
    """
    Search endpoint for invoice line picker (lazy search).
    Returns: id, name, barcode, price, stock (+ code/cost/unit for compatibility)
    """
    q = (request.args.get('q') or '').strip()
    warehouse_id = request.args.get('warehouse_id')
    page = request.args.get('page', type=int) or 1
    limit = request.args.get('limit', type=int) or 20
    page = max(page, 1)
    limit = max(1, min(limit, 100))

    query = Product.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(
            Product.name.contains(q),
            Product.code.contains(q),
            Product.barcode.contains(q),
        ))

    total = query.count()
    products = query.order_by(Product.name).offset((page - 1) * limit).limit(limit).all()

    try:
        warehouse_id_int = int(warehouse_id) if warehouse_id not in (None, '', 'null') else None
    except Exception:
        warehouse_id_int = None

    items = []
    for p in products:
        if warehouse_id_int is not None:
            stock = Stock.query.filter_by(product_id=p.id, warehouse_id=warehouse_id_int).first()
            qty = stock.quantity if stock else 0
        else:
            qty = db.session.query(db.func.coalesce(db.func.sum(Stock.quantity), 0)).filter(Stock.product_id == p.id).scalar() or 0
        items.append({
            'id'     : p.id,
            'name'   : p.name,
            'code'   : p.code    or '',
            'barcode': p.barcode or '',   # لا نرجع None — الـ JS يعرضه كـ 0
            'price'  : p.sell_price  or 0,
            'cost'   : p.cost_price  or 0,
            'unit'   : p.unit   or '',
            'stock'  : qty,
            'qty'    : qty,
        })

    return jsonify({
        'items': items,
        'page': page,
        'limit': limit,
        'hasMore': (page * limit) < total,
        'total': total,
    })


@app.route('/api/product/by_barcode')
@login_required
def api_product_by_barcode():
    barcode = (request.args.get('barcode') or '').strip()
    warehouse_id = request.args.get('warehouse_id')
    if not barcode:
        return jsonify({'error': 'barcode_required'}), 400

    p = Product.query.filter_by(is_active=True).filter(
        db.or_(Product.barcode == barcode, Product.code == barcode)
    ).first()
    if not p:
        return jsonify({'error': 'not_found'}), 404

    try:
        warehouse_id_int = int(warehouse_id) if warehouse_id not in (None, '', 'null') else None
    except Exception:
        warehouse_id_int = None

    if warehouse_id_int is not None:
        stock = Stock.query.filter_by(product_id=p.id, warehouse_id=warehouse_id_int).first()
        qty = stock.quantity if stock else 0
    else:
        qty = db.session.query(db.func.coalesce(db.func.sum(Stock.quantity), 0)).filter(Stock.product_id == p.id).scalar() or 0

    return jsonify({
        'id': p.id,
        'code': p.code,
        'barcode': p.barcode,
        'name': p.name,
        'price': p.sell_price,
        'cost': p.cost_price,
        'unit': p.unit,
        'qty': qty,
    })

# ===== CUSTOMERS =====
@app.route('/customers')
@login_required
def customers():
    q = request.args.get('q', '')
    query = Customer.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(Customer.name.contains(q), Customer.phone.contains(q)))
    customers = query.all()
    return render_template('customers.html', customers=customers, q=q)

@app.route('/customers/add', methods=['GET', 'POST'])
@login_required
def add_customer():
    if request.method == 'POST':
        code_input = (request.form.get('code') or '').strip()
        name_val = request.form.get('name')
        if not name_val:
            flash('يرجى إدخال اسم العميل', 'error')
            suggested = allocate_entity_code('C', Customer)
            return render_template('customer_form.html', suggested_code=suggested)
        try:
            credit_limit_val = float(request.form.get('credit_limit', 0) or 0)
        except (ValueError, TypeError):
            flash('حد الائتمان المدخل غير صحيح', 'error')
            suggested = allocate_entity_code('C', Customer)
            return render_template('customer_form.html', suggested_code=suggested)
        phone_val = request.form.get('phone')
        email_val = request.form.get('email')
        address_val = request.form.get('address')

        # ── حفظ مع إعادة محاولة آمنة عند تعارض الكود (نفس الكود اتاخد قبل ما تحفظ) ──
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            code = code_input or allocate_entity_code('C', Customer)
            customer = Customer(code=code, name=name_val, phone=phone_val, email=email_val,
                                 address=address_val, credit_limit=credit_limit_val)
            db.session.add(customer)
            try:
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if code_input:
                    flash(f'الكود «{code_input}» مستخدم بالفعل لعميل آخر — يرجى اختيار كود مختلف', 'error')
                    suggested = allocate_entity_code('C', Customer)
                    return render_template('customer_form.html', suggested_code=suggested)
                if attempt == max_attempts:
                    flash('تعذّر إضافة العميل بسبب تعارض في ترقيم الأكواد — يرجى المحاولة مرة أخرى', 'error')
                    suggested = allocate_entity_code('C', Customer)
                    return render_template('customer_form.html', suggested_code=suggested)
                continue
            except SQLAlchemyError:
                db.session.rollback()
                flash('حدث خطأ غير متوقع أثناء إضافة العميل — لم يتم حفظ أي بيانات', 'error')
                suggested = allocate_entity_code('C', Customer)
                return render_template('customer_form.html', suggested_code=suggested)

        flash('تم إضافة العميل بنجاح', 'success')
        return redirect(url_for('customers'))
    suggested = allocate_entity_code('C', Customer)
    return render_template('customer_form.html', suggested_code=suggested)


# ===== CUSTOMERS IMPORT (EXCEL) =====
_CUSTOMER_IMPORT_HEADER_ALIASES = {
    'code':    ['كود', 'كود العميل', 'code'],
    'name':    ['اسم العميل', 'اسم الزبون', 'العملاء', 'العميل', 'الزبون', 'الاسم', 'name'],
    'phone':   ['الهاتف', 'هاتف', 'تليفون', 'الموبايل', 'موبايل', 'phone'],
    'email':   ['البريد', 'الايميل', 'الإيميل', 'email'],
    'address': ['العنوان', 'عنوان', 'address'],
    'credit_limit': ['حد الائتمان', 'الائتمان', 'credit_limit'],
}


def _match_entity_import_columns(header_row, aliases_map):
    """نفس منطق مطابقة أعمدة استيراد الأصناف، مُعمَّم لأي كيان (عملاء/موردين).
    يتجاهل العناوين القصيرة جداً (كعمود التسلسل «م») حتى لا تتطابق خطأً."""
    mapping = {}
    used_idx = set()
    for idx, cell in enumerate(header_row):
        title = (str(cell).strip() if cell is not None else '')
        if len(title) < 3 or idx in used_idx:
            continue
        for key, aliases in aliases_map.items():
            if key in mapping:
                continue
            matched = False
            for alias in aliases:
                a = alias.strip()
                if len(a) < 3:
                    continue
                if a in title or title in a:
                    mapping[key] = idx
                    used_idx.add(idx)
                    matched = True
                    break
            if matched:
                break
    return mapping


@app.route('/customers/import', methods=['GET', 'POST'])
@login_required
@developer_required
def import_customers():
    if request.method == 'GET':
        return render_template('customer_import.html')

    if openpyxl is None:
        flash('تعذّر الاستيراد: مكتبة openpyxl غير مثبّتة على الخادم', 'error')
        return redirect(url_for('import_customers'))

    f = request.files.get('file')
    if not f or not f.filename:
        flash('يرجى اختيار ملف إكسيل (.xlsx)', 'error')
        return redirect(url_for('import_customers'))
    if not f.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash('امتداد الملف يجب أن يكون .xlsx', 'error')
        return redirect(url_for('import_customers'))

    tmp = os.path.join(_INSTANCE_DIR, '_customers_import_upload.xlsx')
    try:
        f.save(tmp)
        wb = openpyxl.load_workbook(tmp, data_only=True)
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            flash('الملف فارغ', 'error')
            return redirect(url_for('import_customers'))
        cols = _match_entity_import_columns(header_row, _CUSTOMER_IMPORT_HEADER_ALIASES)
        if 'name' not in cols:
            # لم يتم التعرّف على عمود الاسم بالعنوان — نلتقط أوسع عمود نصي غير رقمي بالكامل
            # (يتوافق مع ملفات بسيطة من عمودين: «م» ثم اسم العميل بلا عنوان مطابق).
            candidate_idx = None
            for idx in range(len(header_row)):
                title = (str(header_row[idx]).strip() if header_row[idx] is not None else '')
                if len(title) >= 3 and idx not in cols.values():
                    candidate_idx = idx
                    break
            if candidate_idx is None and len(header_row) >= 2:
                candidate_idx = len(header_row) - 1
            if candidate_idx is None:
                flash('تعذّر التعرّف على عمود «اسم العميل» في الملف — تأكد من وجود صف عناوين صحيح', 'error')
                return redirect(url_for('import_customers'))
            cols['name'] = candidate_idx

        existing_by_name = {}
        for c in Customer.query.all():
            existing_by_name.setdefault((c.name or '').strip(), c)

        added = 0
        skipped = 0
        errors = []
        for row_num, row in enumerate(rows_iter, start=2):
            if row is None or all(v is None or str(v).strip() == '' for v in row):
                continue
            try:
                name_idx = cols['name']
                name_val = row[name_idx] if name_idx < len(row) else None
                name_val = (str(name_val).strip() if name_val is not None else '')
                if not name_val:
                    skipped += 1
                    continue
                if name_val in existing_by_name:
                    skipped += 1
                    continue

                def _txt(key):
                    idx = cols.get(key)
                    if idx is None or idx >= len(row):
                        return None
                    v = row[idx]
                    if v in (None, ''):
                        return None
                    return str(v).strip()

                def _num(key):
                    idx = cols.get(key)
                    if idx is None or idx >= len(row):
                        return 0.0
                    v = row[idx]
                    if v in (None, ''):
                        return 0.0
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        return 0.0

                code_val = _txt('code') or allocate_entity_code('C', Customer)
                customer = Customer(code=code_val, name=name_val, phone=_txt('phone'),
                                     email=_txt('email'), address=_txt('address'),
                                     credit_limit=_num('credit_limit'))
                db.session.add(customer)
                db.session.flush()
                existing_by_name[name_val] = customer
                added += 1
            except Exception as e:
                skipped += 1
                errors.append(f'صف {row_num}: {e}')

        db.session.commit()
        msg = f'تم استيراد {added} عميل جديد'
        if skipped:
            msg += f' — وتم تجاوز {skipped} صف (فارغ أو مكرر أو به خطأ)'
        flash(msg, 'success' if added else 'error')
        if errors:
            flash('أول الأخطاء: ' + ' | '.join(errors[:5]), 'error')
        return redirect(url_for('customers'))
    except Exception as e:
        db.session.rollback()
        flash(f'فشل استيراد الملف: {e}', 'error')
        return redirect(url_for('import_customers'))
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


@app.route('/customers/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_customer(id):
    customer = Customer.query.get_or_404(id)
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if code:
            customer.code = code
        customer.name = request.form['name']
        customer.phone = request.form.get('phone')
        customer.email = request.form.get('email')
        customer.address = request.form.get('address')
        customer.credit_limit = float(request.form.get('credit_limit', 0))
        db.session.commit()
        flash('تم تحديث بيانات العميل', 'success')
        return redirect(url_for('customers'))
    return render_template('customer_form.html', customer=customer)

@app.route('/customers/<int:id>/statement')
@login_required
def customer_statement(id):
    customer = Customer.query.get_or_404(id)
    sales = Sale.query.filter_by(customer_id=id).order_by(Sale.date.desc()).all()
    payments = CustomerPayment.query.filter_by(customer_id=id).order_by(CustomerPayment.date.desc()).all()
    returns = SaleReturn.query.join(Sale).filter(Sale.customer_id == id).all()
    open_invoices = _customer_open_invoices(id)
    return render_template('customer_statement.html', customer=customer, sales=sales, 
                           payments=payments, returns=returns, open_invoices=open_invoices)

@app.route('/customers/<int:id>/statement/detailed')
@login_required
def customer_statement_detailed(id):
    customer = Customer.query.get_or_404(id)
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())

    sale_rows = db.session.query(SaleItem, Sale).join(Sale, SaleItem.sale_id == Sale.id).filter(
        Sale.customer_id == id,
        db.func.date(Sale.date).between(date_from, date_to)
    ).order_by(Sale.date.asc()).all()

    return_rows = db.session.query(SaleReturnItem, SaleReturn, Sale).join(
        SaleReturn, SaleReturnItem.return_id == SaleReturn.id
    ).join(Sale, SaleReturn.sale_id == Sale.id).filter(
        Sale.customer_id == id,
        db.func.date(SaleReturn.date).between(date_from, date_to)
    ).order_by(SaleReturn.date.asc()).all()

    rows = []
    for si, sale in sale_rows:
        rows.append({
            'date': sale.date, 'invoice_number': sale.invoice_number, 'sale_id': sale.id,
            'type': 'sale', 'type_label': 'بيع',
            'product': si.product.name if si.product else '—',
            'unit': si.product.unit if si.product else '',
            'qty': si.quantity or 0, 'price': si.price or 0, 'total': si.total or 0,
        })
    for ri, ret, sale in return_rows:
        rows.append({
            'date': ret.date, 'invoice_number': ret.invoice_number, 'sale_id': sale.id,
            'type': 'return', 'type_label': 'مرتجع بيع',
            'product': ri.product.name if ri.product else '—',
            'unit': ri.product.unit if ri.product else '',
            'qty': ri.quantity or 0, 'price': ri.price or 0, 'total': -(ri.total or 0),
        })
    rows.sort(key=lambda r: r['date'])

    products_summary = defaultdict(lambda: {'qty': 0.0, 'total': 0.0, 'unit': ''})
    for r in rows:
        agg = products_summary[r['product']]
        agg['unit'] = r['unit']
        if r['type'] == 'sale':
            agg['qty'] += r['qty']
        else:
            agg['qty'] -= r['qty']
        agg['total'] += r['total']
    products_summary = dict(sorted(products_summary.items(), key=lambda kv: -kv[1]['total']))

    grand_total = sum(r['total'] for r in rows)
    grand_qty = sum(r['qty'] if r['type'] == 'sale' else -r['qty'] for r in rows)

    return render_template(
        'customer_statement_detailed.html', customer=customer, rows=rows,
        products_summary=products_summary, grand_total=grand_total, grand_qty=grand_qty,
        date_from=date_from, date_to=date_to)

@app.route('/customers/<int:id>/statement/detailed/print')
@login_required
def customer_statement_detailed_print(id):
    customer = Customer.query.get_or_404(id)
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())

    sale_rows = db.session.query(SaleItem, Sale).join(Sale, SaleItem.sale_id == Sale.id).filter(
        Sale.customer_id == id,
        db.func.date(Sale.date).between(date_from, date_to)
    ).order_by(Sale.date.asc()).all()

    return_rows = db.session.query(SaleReturnItem, SaleReturn, Sale).join(
        SaleReturn, SaleReturnItem.return_id == SaleReturn.id
    ).join(Sale, SaleReturn.sale_id == Sale.id).filter(
        Sale.customer_id == id,
        db.func.date(SaleReturn.date).between(date_from, date_to)
    ).order_by(SaleReturn.date.asc()).all()

    rows = []
    for si, sale in sale_rows:
        rows.append({
            'date': sale.date, 'invoice_number': sale.invoice_number,
            'type': 'sale', 'type_label': 'بيع',
            'product': si.product.name if si.product else '—',
            'unit': si.product.unit if si.product else '',
            'qty': si.quantity or 0, 'price': si.price or 0, 'total': si.total or 0,
        })
    for ri, ret, sale in return_rows:
        rows.append({
            'date': ret.date, 'invoice_number': ret.invoice_number,
            'type': 'return', 'type_label': 'مرتجع بيع',
            'product': ri.product.name if ri.product else '—',
            'unit': ri.product.unit if ri.product else '',
            'qty': ri.quantity or 0, 'price': ri.price or 0, 'total': -(ri.total or 0),
        })
    rows.sort(key=lambda r: r['date'])

    products_summary = defaultdict(lambda: {'qty': 0.0, 'total': 0.0, 'unit': ''})
    for r in rows:
        agg = products_summary[r['product']]
        agg['unit'] = r['unit']
        if r['type'] == 'sale':
            agg['qty'] += r['qty']
        else:
            agg['qty'] -= r['qty']
        agg['total'] += r['total']
    products_summary = dict(sorted(products_summary.items(), key=lambda kv: -kv[1]['total']))

    grand_total = sum(r['total'] for r in rows)
    grand_qty = sum(r['qty'] if r['type'] == 'sale' else -r['qty'] for r in rows)

    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'customer_statement_detailed_print.html',
        customer=customer, rows=rows, products_summary=products_summary,
        grand_total=grand_total, grand_qty=grand_qty,
        date_from=date_from, date_to=date_to,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
        printed_at=datetime.now(),
    )

@app.route('/customers/<int:id>/statement/print')
@login_required
def customer_statement_print(id):
    customer = Customer.query.get_or_404(id)
    sales = Sale.query.filter_by(customer_id=id).order_by(Sale.date.desc()).all()
    payments = CustomerPayment.query.filter_by(customer_id=id).order_by(CustomerPayment.date.desc()).all()
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'customer_statement_print.html',
        customer=customer,
        sales=sales,
        payments=payments,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
        printed_at=datetime.now(),
    )

@app.route('/customers/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_customer(id):
    customer = Customer.query.get_or_404(id)
    if customer.balance and abs(customer.balance) > 0.0001:
        flash('لا يمكن حذف العميل طالما يوجد رصيد مستحق أو دائن', 'error')
        return redirect(url_for('customers'))
    customer.is_active = False
    db.session.commit()
    flash('تم حذف العميل', 'success')
    return redirect(url_for('customers'))

@app.route('/customers/<int:id>/payment', methods=['POST'])
@login_required
def customer_payment(id):
    customer = Customer.query.get_or_404(id)
    notes = request.form.get('notes') or ''
    entry_type = (request.form.get('entry_type') or 'payment').strip()
    if entry_type not in ('payment', 'debit', 'credit', 'invoice'):
        entry_type = 'payment'
    try:
        raw_amount = float(request.form.get('amount') or 0)
    except (TypeError, ValueError):
        flash('المبلغ غير صالح', 'error')
        return redirect(url_for('customer_statement', id=id))

    if abs(raw_amount) < 0.0001:
        flash('أدخل مبلغاً أكبر من صفر', 'error')
        return redirect(url_for('customer_statement', id=id))

    # إدخال سالب مثل -1000 = العميل مدين لنا (عليه)
    if raw_amount < 0 and entry_type == 'payment':
        entry_type = 'debit'
    amount = abs(raw_amount)

    if entry_type == 'debit':
        payment = CustomerPayment(
            customer_id=id,
            amount=-amount,
            notes=('إضافة على الحساب — ' + notes) if notes else 'إضافة على الحساب — العميل مدين لنا',
            user_id=current_user.id
        )
        customer.balance += amount
        db.session.add(payment)
        db.session.commit()
        flash('تم إضافة المبلغ على حساب العميل (مدين لنا)', 'success')
        return redirect(url_for('customer_statement', id=id))

    if entry_type == 'credit':
        payment = CustomerPayment(
            customer_id=id,
            amount=amount,
            notes=('رصيد دائن — ' + notes) if notes else 'رصيد دائن — العميل له',
            user_id=current_user.id
        )
        customer.balance -= amount
        db.session.add(payment)
        db.session.commit()
        flash('تم تسجيل رصيد دائن للعميل', 'success')
        return redirect(url_for('customer_statement', id=id))

    if entry_type == 'invoice':
        invoice_number = (request.form.get('invoice_number') or '').strip()
        if not invoice_number:
            flash('يرجى اختيار رقم الفاتورة المراد تسديدها', 'error')
            return redirect(url_for('customer_statement', id=id))
        sale = Sale.query.filter_by(customer_id=id, invoice_number=invoice_number).first()
        if not sale:
            flash('رقم الفاتورة غير موجود لهذا العميل', 'error')
            return redirect(url_for('customer_statement', id=id))
        already_paid = _customer_linked_payments_total(id, invoice_number)
        actual_remaining = float(sale.remaining or 0) - already_paid
        if actual_remaining <= 0.0001:
            flash(f'الفاتورة {invoice_number} مسدَّدة بالكامل بالفعل', 'error')
            return redirect(url_for('customer_statement', id=id))
        if amount > actual_remaining:
            amount = actual_remaining
        # يجب أن ينتهي النص دائماً بـ«مرتبطة بفاتورة {رقم}» بالضبط حتى يُحتسَب مرتبطاً
        # بهذه الفاتورة عند عرضها أو حذفها لاحقاً — لا نسمح لملاحظات المستخدم بكسر هذا الربط.
        linked_note = f'مرتبطة بفاتورة {invoice_number}'
        final_notes = (f'دفعة على حساب العميل — {notes} — {linked_note}'
                       if notes else f'دفعة على حساب العميل — {linked_note}')
        payment = CustomerPayment(
            customer_id=id,
            amount=amount,
            notes=final_notes,
            user_id=current_user.id
        )
        customer.balance -= amount
        db.session.add(payment)
        db.session.commit()
        flash(f'تم تسجيل دفعة {amount:.2f} على الفاتورة {invoice_number}', 'success')
        return redirect(url_for('customer_statement', id=id))

    # ملاحظة: دفعات كشف الحساب لا تُطبَّق على الفواتير نفسها (لا تغيّر sale.paid/sale.remaining)،
    # فقط تُسجَّل كحركة في كشف الحساب وتُحدَّث رصيد العميل الإجمالي، حتى تبقى الفاتورة الأصلية
    # كما صدرت (أجل) عند عرضها لاحقاً.
    payment = CustomerPayment(
        customer_id=id,
        amount=amount,
        notes=notes or 'دفعة عامة',
        user_id=current_user.id
    )
    customer.balance -= amount
    db.session.add(payment)
    db.session.commit()
    flash('تم تسجيل الدفعة بنجاح', 'success')
    return redirect(url_for('customer_statement', id=id))

@app.route('/customers/<int:id>/payment/<int:pay_id>/delete', methods=['POST'])
@login_required
@statement_payment_delete_required
def delete_customer_payment(id, pay_id):
    customer = Customer.query.get_or_404(id)
    payment = CustomerPayment.query.get_or_404(pay_id)
    if payment.customer_id != id:
        flash('الحركة غير مرتبطة بهذا العميل', 'error')
        return redirect(url_for('customer_statement', id=id))
    notes = payment.notes or ''
    stored = float(payment.amount or 0)
    abs_amt = abs(stored)
    try:
        if stored < 0 or 'إضافة على الحساب' in notes:
            customer.balance -= abs_amt
        elif 'رصيد دائن' in notes:
            customer.balance += abs_amt
        else:
            customer.balance += abs_amt
            # توافق مع بيانات قديمة كانت تُطبَّق على الفواتير مباشرة (قبل التعديل الحالي)
            if 'دفعة على فواتير:' in notes or 'دفعة على فاتورة ' in notes:
                _unapply_customer_invoice_payment(id, abs_amt, notes)
        db.session.delete(payment)
        db.session.commit()
        flash('تم حذف الحركة وإلغاء أثرها على الحساب', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'تعذر حذف الحركة: {e}', 'error')
    return redirect(url_for('customer_statement', id=id))

# ===== SUPPLIERS =====
@app.route('/suppliers')
@login_required
def suppliers():
    q = request.args.get('q', '')
    query = Supplier.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(Supplier.name.contains(q), Supplier.phone.contains(q)))
    suppliers = query.all()
    return render_template('suppliers.html', suppliers=suppliers, q=q)

@app.route('/suppliers/add', methods=['GET', 'POST'])
@login_required
def add_supplier():
    if request.method == 'POST':
        code_input = (request.form.get('code') or '').strip()
        name_val = request.form.get('name')
        if not name_val:
            flash('يرجى إدخال اسم المورد', 'error')
            suggested = allocate_entity_code('S', Supplier)
            return render_template('supplier_form.html', suggested_code=suggested)
        phone_val = request.form.get('phone')
        email_val = request.form.get('email')
        address_val = request.form.get('address')

        # ── حفظ مع إعادة محاولة آمنة عند تعارض الكود (نفس الكود اتاخد قبل ما تحفظ) ──
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            code = code_input or allocate_entity_code('S', Supplier)
            supplier = Supplier(code=code, name=name_val, phone=phone_val, email=email_val, address=address_val)
            db.session.add(supplier)
            try:
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if code_input:
                    flash(f'الكود «{code_input}» مستخدم بالفعل لمورد آخر — يرجى اختيار كود مختلف', 'error')
                    suggested = allocate_entity_code('S', Supplier)
                    return render_template('supplier_form.html', suggested_code=suggested)
                if attempt == max_attempts:
                    flash('تعذّر إضافة المورد بسبب تعارض في ترقيم الأكواد — يرجى المحاولة مرة أخرى', 'error')
                    suggested = allocate_entity_code('S', Supplier)
                    return render_template('supplier_form.html', suggested_code=suggested)
                continue
            except SQLAlchemyError:
                db.session.rollback()
                flash('حدث خطأ غير متوقع أثناء إضافة المورد — لم يتم حفظ أي بيانات', 'error')
                suggested = allocate_entity_code('S', Supplier)
                return render_template('supplier_form.html', suggested_code=suggested)

        flash('تم إضافة المورد بنجاح', 'success')
        return redirect(url_for('suppliers'))
    suggested = allocate_entity_code('S', Supplier)
    return render_template('supplier_form.html', suggested_code=suggested)


# ===== SUPPLIERS IMPORT (EXCEL) =====
_SUPPLIER_IMPORT_HEADER_ALIASES = {
    'code':    ['كود', 'كود المورد', 'code'],
    'name':    ['اسم المورد', 'الموردين', 'المورد', 'الاسم', 'name'],
    'phone':   ['الهاتف', 'هاتف', 'تليفون', 'الموبايل', 'موبايل', 'phone'],
    'email':   ['البريد', 'الايميل', 'الإيميل', 'email'],
    'address': ['العنوان', 'عنوان', 'address'],
}


@app.route('/suppliers/import', methods=['GET', 'POST'])
@login_required
@developer_required
def import_suppliers():
    if request.method == 'GET':
        return render_template('supplier_import.html')

    if openpyxl is None:
        flash('تعذّر الاستيراد: مكتبة openpyxl غير مثبّتة على الخادم', 'error')
        return redirect(url_for('import_suppliers'))

    f = request.files.get('file')
    if not f or not f.filename:
        flash('يرجى اختيار ملف إكسيل (.xlsx)', 'error')
        return redirect(url_for('import_suppliers'))
    if not f.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash('امتداد الملف يجب أن يكون .xlsx', 'error')
        return redirect(url_for('import_suppliers'))

    tmp = os.path.join(_INSTANCE_DIR, '_suppliers_import_upload.xlsx')
    try:
        f.save(tmp)
        wb = openpyxl.load_workbook(tmp, data_only=True)
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            flash('الملف فارغ', 'error')
            return redirect(url_for('import_suppliers'))
        cols = _match_entity_import_columns(header_row, _SUPPLIER_IMPORT_HEADER_ALIASES)
        if 'name' not in cols:
            # لم يتم التعرّف على عمود الاسم بالعنوان — نلتقط أوسع عمود نصي غير رقمي بالكامل
            # (يتوافق مع ملفات بسيطة من عمودين: «م» ثم اسم المورد بلا عنوان مطابق).
            candidate_idx = None
            for idx in range(len(header_row)):
                title = (str(header_row[idx]).strip() if header_row[idx] is not None else '')
                if len(title) >= 3 and idx not in cols.values():
                    candidate_idx = idx
                    break
            if candidate_idx is None and len(header_row) >= 2:
                candidate_idx = len(header_row) - 1
            if candidate_idx is None:
                flash('تعذّر التعرّف على عمود «اسم المورد» في الملف — تأكد من وجود صف عناوين صحيح', 'error')
                return redirect(url_for('import_suppliers'))
            cols['name'] = candidate_idx

        existing_by_name = {}
        for s in Supplier.query.all():
            existing_by_name.setdefault((s.name or '').strip(), s)

        added = 0
        skipped = 0
        errors = []
        for row_num, row in enumerate(rows_iter, start=2):
            if row is None or all(v is None or str(v).strip() == '' for v in row):
                continue
            try:
                name_idx = cols['name']
                name_val = row[name_idx] if name_idx < len(row) else None
                name_val = (str(name_val).strip() if name_val is not None else '')
                if not name_val:
                    skipped += 1
                    continue
                if name_val in existing_by_name:
                    skipped += 1
                    continue

                def _txt(key):
                    idx = cols.get(key)
                    if idx is None or idx >= len(row):
                        return None
                    v = row[idx]
                    if v in (None, ''):
                        return None
                    return str(v).strip()

                code_val = _txt('code') or allocate_entity_code('S', Supplier)
                supplier = Supplier(code=code_val, name=name_val, phone=_txt('phone'),
                                     email=_txt('email'), address=_txt('address'))
                db.session.add(supplier)
                db.session.flush()
                existing_by_name[name_val] = supplier
                added += 1
            except Exception as e:
                skipped += 1
                errors.append(f'صف {row_num}: {e}')

        db.session.commit()
        msg = f'تم استيراد {added} مورد جديد'
        if skipped:
            msg += f' — وتم تجاوز {skipped} صف (فارغ أو مكرر أو به خطأ)'
        flash(msg, 'success' if added else 'error')
        if errors:
            flash('أول الأخطاء: ' + ' | '.join(errors[:5]), 'error')
        return redirect(url_for('suppliers'))
    except Exception as e:
        db.session.rollback()
        flash(f'فشل استيراد الملف: {e}', 'error')
        return redirect(url_for('import_suppliers'))
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ===== SALES =====
@app.route('/sales')
@login_required
def sales():
    page = request.args.get('page', 1, type=int)
    sales = Sale.query.order_by(Sale.date.desc()).paginate(page=page, per_page=20)
    returned_ids = {r[0] for r in db.session.query(SaleReturn.sale_id).distinct().all()}
    # فاتورة "مسدَّدة بالكامل" لعرض شارة خضراء جمب رقمها فقط: إما كانت مدفوعة بالكامل عند
    # الإصدار، أو تم تغطية متبقيها بالكامل لاحقاً عبر دفعات كشف الحساب المرتبطة بها تحديداً —
    # دون تغيير عرض عمود «المتبقي» نفسه الذي يبقى كما صدرت الفاتورة.
    fully_settled_sale_ids = set()
    for s in sales.items:
        if (s.remaining or 0) <= 0.0001:
            fully_settled_sale_ids.add(s.id)
        elif s.customer_id:
            paid = _customer_linked_payments_total(s.customer_id, s.invoice_number)
            if paid + 1e-6 >= float(s.remaining or 0):
                fully_settled_sale_ids.add(s.id)
    return render_template('sales.html', sales=sales, returned_sale_ids=returned_ids,
                           fully_settled_sale_ids=fully_settled_sale_ids)

@app.route('/sales/new', methods=['GET', 'POST'])
@login_required
def new_sale():
    if request.method == 'POST':
        customer_id = request.form.get('customer_id') or None
        warehouse_id = request.form.get('warehouse_id') or None
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')
        discounts = request.form.getlist('discount[]')

        # ── Validation: لا حفظ بدون بيانات كاملة ──
        if not warehouse_id:
            flash('يرجى اختيار المخزن قبل الحفظ', 'error')
            return redirect(url_for('new_sale'))

        # ── تحويل آمن للأرقام: أي قيمة غير صالحة (فراغ/نص غير رقمي) توقف
        #    الحفظ برسالة واضحة للمستخدم بدل ما تسبب كراش (500) للكاشير ──
        try:
            warehouse_id = int(warehouse_id)
            paid_val = float(request.form.get('paid', 0) or 0)
            total_discount_val = float(request.form.get('total_discount', 0) or 0)
            tax_form_val = float(request.form.get('tax', 0) or 0)
            lines_raw = []
            for i, pid in enumerate(product_ids):
                if not pid:
                    continue
                qty = float(quantities[i] or 0)
                price = float(prices[i] or 0)
                disc = float(discounts[i]) if i < len(discounts) and (discounts[i] not in (None, '')) else 0.0
                lines_raw.append((int(pid), qty, price, disc))
        except (ValueError, TypeError, IndexError):
            flash('توجد بيانات غير صحيحة في الفاتورة (كمية/سعر/خصم) — يرجى مراجعة الأصناف والحفظ مرة أخرى', 'error')
            return redirect(url_for('new_sale'))

        valid_lines_check = [1 for pid, qty, price, disc in lines_raw if qty > 0 and price > 0]
        if not valid_lines_check:
            flash('يرجى إضافة صنف واحد على الأقل بكمية وسعر صحيحين قبل الحفظ', 'error')
            return redirect(url_for('new_sale'))
        is_ajal = request.form.get('is_ajal') == '1'
        if paid_val <= 0 and not is_ajal:
            flash('يرجى إدخال المبلغ المدفوع أو تحديد "آجل" لحفظ الفاتورة', 'error')
            return redirect(url_for('new_sale'))

        payment_method_val = (request.form.get('payment_method') or 'cash').strip().lower()
        if payment_method_val not in ('cash', 'card', 'transfer', 'other'):
            payment_method_val = 'cash'

        # Merge duplicated products into one line to prevent duplicate items per invoice.
        merged = {}
        for pid, qty, price, disc in lines_raw:
            if qty <= 0 or price <= 0:
                continue
            if pid not in merged:
                merged[pid] = {'qty': 0.0, 'price': price, 'disc': disc}
            merged[pid]['qty'] += qty
            merged[pid]['price'] = price
            merged[pid]['disc'] = disc
        lines = [(pid, v['qty'], v['price'], v['disc']) for pid, v in merged.items()]

        for pid, qty, price, disc in lines:
            stock = Stock.query.filter_by(product_id=pid, warehouse_id=warehouse_id).first()
            avail = float(stock.quantity) if stock else 0.0
            if avail + 1e-9 < qty:
                pname = Product.query.get(pid)
                pname = pname.name if pname else str(pid)
                short = qty - avail
                flash(f'المخزون غير كافٍ للصنف «{pname}»: المتوفر {avail:g} والمطلوب {qty:g} — يوجد عجز {short:g}', 'error')
                return redirect(url_for('new_sale'))

        gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
        notes_val = request.form.get('notes')

        # ── حفظ الفاتورة: خصم المخزون بأمر UPDATE ذري (بيمنع بيع أكتر من
        #    المتوفر فعليًا لو حصلت عملية بيع تانية لنفس الصنف في نفس اللحظة)،
        #    مع إعادة محاولة عند تعارض رقم الفاتورة بين كاشيرين في نفس الثانية ──
        max_attempts = 5
        sale = None
        for attempt in range(1, max_attempts + 1):
            try:
                sale = Sale(
                    invoice_number=get_next_number('INV', Sale, 'invoice_number'),
                    customer_id=customer_id,
                    warehouse_id=warehouse_id,
                    user_id=current_user.id,
                    payment_method=payment_method_val,
                    discount=total_discount_val,
                    tax=0,
                    paid=paid_val,
                    notes=notes_val,
                )
                subtotal = 0
                stock_shortage = None
                for pid, qty, price, disc in lines:
                    item_total = qty * price * (1 - disc / 100)
                    subtotal += item_total
                    item = SaleItem(product_id=pid, quantity=qty, price=price, discount=disc, total=item_total)
                    sale.items.append(item)
                    # UPDATE ذري: يخصم الكمية فقط لو المتاح كافٍ وقت التنفيذ الفعلي
                    result = db.session.execute(
                        db.update(Stock)
                        .where(Stock.product_id == pid, Stock.warehouse_id == warehouse_id,
                               Stock.quantity >= qty - 1e-9)
                        .values(quantity=Stock.quantity - qty)
                    )
                    if result.rowcount == 0:
                        pname = Product.query.get(pid)
                        stock_shortage = pname.name if pname else str(pid)
                        break

                if stock_shortage:
                    db.session.rollback()
                    flash(f'تعذّر إتمام البيع: المخزون المتاح للصنف «{stock_shortage}» تغيّر (بيع آخر تم في نفس اللحظة) — يرجى مراجعة الكمية والمحاولة مرة أخرى', 'error')
                    return redirect(url_for('new_sale'))

                sale.subtotal = subtotal
                if (gs.get('sale_fixed_tax_enabled') or '0').strip() in ('1', 'true', 'on', 'yes'):
                    pct = float(gs.get('sale_fixed_tax_percent') or 0)
                    sale.tax = round(subtotal * (pct / 100.0), 2)
                else:
                    sale.tax = tax_form_val
                sale.total = subtotal - sale.discount + sale.tax
                sale.remaining = sale.total - sale.paid

                if customer_id and sale.remaining > 0:
                    customer = Customer.query.get(customer_id)
                    if customer:
                        customer.balance += sale.remaining

                db.session.add(sale)
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if attempt == max_attempts:
                    flash('تعذّر حفظ الفاتورة بسبب تعارض في ترقيم الفواتير (بيع متزامن من أكتر من كاشير) — يرجى المحاولة مرة أخرى', 'error')
                    return redirect(url_for('new_sale'))
                continue
            except SQLAlchemyError:
                db.session.rollback()
                flash('حدث خطأ غير متوقع أثناء حفظ الفاتورة في قاعدة البيانات — لم يتم حفظ أي بيانات، برجاء المحاولة مرة أخرى', 'error')
                return redirect(url_for('new_sale'))

        flash(f'تم إنشاء الفاتورة {sale.invoice_number} بنجاح', 'success')
        auto_print = (gs.get('print_auto_sale') or '0').strip() in ('1', 'true', 'on', 'yes')
        if auto_print:
            return redirect(url_for('sale_detail', id=sale.id, autoprint='1'))
        return redirect(url_for('sale_detail', id=sale.id))

    customers = Customer.query.filter_by(is_active=True).all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))

    # العميل الأكثر استخداماً
    top_customer = db.session.query(Sale.customer_id, db.func.count(Sale.id).label('cnt'))\
        .filter(Sale.customer_id != None)\
        .group_by(Sale.customer_id).order_by(db.text('cnt DESC')).first()
    default_customer_id = top_customer[0] if top_customer else None

    # المخزن الأكثر استخداماً في المبيعات
    top_warehouse = db.session.query(Sale.warehouse_id, db.func.count(Sale.id).label('cnt'))\
        .filter(Sale.warehouse_id != None)\
        .group_by(Sale.warehouse_id).order_by(db.text('cnt DESC')).first()
    default_warehouse_id = top_warehouse[0] if top_warehouse else (warehouses[0].id if warehouses else None)

    return render_template(
        'sale_form.html',
        customers=customers,
        warehouses=warehouses,
        default_customer_id=default_customer_id,
        default_warehouse_id=default_warehouse_id,
        sale_tax_auto=(gs.get('sale_fixed_tax_enabled') or '0').strip() in ('1', 'true', 'on', 'yes'),
        sale_tax_percent=float(gs.get('sale_fixed_tax_percent') or 0),
    )


# ===== HOLD / PARK SALE (فاتورة معلّقة مؤقتاً) =====

@app.route('/sales/hold', methods=['POST'])
@login_required
def hold_sale():
    """تعليق فاتورة البيع الحالية كمسودة بدون خصم من المخزون وبدون تسجيل دفعة —
    تُستخدم لخدمة زبون تاني بسرعة والرجوع للفاتورة المعلّقة لاحقاً."""
    warehouse_id = request.form.get('warehouse_id') or None
    customer_id = request.form.get('customer_id') or None
    product_ids = request.form.getlist('product_id[]')
    quantities = request.form.getlist('quantity[]')
    prices = request.form.getlist('price[]')
    discounts = request.form.getlist('discount[]')

    if not warehouse_id:
        flash('يرجى اختيار المخزن قبل تعليق الفاتورة', 'error')
        return redirect(url_for('new_sale'))

    try:
        warehouse_id = int(warehouse_id)
        total_discount_val = float(request.form.get('total_discount', 0) or 0)
        tax_val = float(request.form.get('tax', 0) or 0)
        lines = []
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            qty = float(quantities[i] or 0)
            price = float(prices[i] or 0)
            disc = float(discounts[i]) if i < len(discounts) and (discounts[i] not in (None, '')) else 0.0
            if qty > 0 and price > 0:
                lines.append({'product_id': int(pid), 'quantity': qty, 'price': price, 'discount': disc})
    except (ValueError, TypeError, IndexError):
        flash('توجد بيانات غير صحيحة في الفاتورة (كمية/سعر/خصم) — تعذر تعليقها', 'error')
        return redirect(url_for('new_sale'))

    if not lines:
        flash('يرجى إضافة صنف واحد على الأقل بكمية وسعر صحيحين قبل تعليق الفاتورة', 'error')
        return redirect(url_for('new_sale'))

    held = HeldSale(
        hold_number=get_next_number('HOLD', HeldSale, 'hold_number'),
        customer_id=customer_id,
        warehouse_id=warehouse_id,
        user_id=current_user.id,
        branch_id=getattr(current_user, 'branch_id', None),
        notes=request.form.get('notes'),
        total_discount=total_discount_val,
        tax=tax_val,
        items_json=json.dumps(lines),
    )
    db.session.add(held)
    db.session.commit()
    flash(f'تم تعليق الفاتورة برقم {held.hold_number} — استرجعها لاحقاً من زر «الفواتير المعلقة»', 'success')
    return redirect(url_for('new_sale'))


@app.route('/api/sales/held')
@login_required
def api_held_sales():
    """قائمة الفواتير المعلّقة بصيغة JSON لعرضها في المودال."""
    q = HeldSale.query.order_by(HeldSale.created_at.desc())
    if getattr(current_user, 'role', None) not in ('developer', 'admin', 'manager'):
        q = q.filter_by(user_id=current_user.id)
    rows = q.limit(100).all()
    out = []
    for h in rows:
        try:
            items = json.loads(h.items_json or '[]')
        except (json.JSONDecodeError, TypeError):
            items = []
        total = sum((it.get('quantity', 0) or 0) * (it.get('price', 0) or 0) * (1 - (it.get('discount', 0) or 0) / 100) for it in items)
        total = max(0.0, total - (h.total_discount or 0)) + (h.tax or 0)
        out.append({
            'id': h.id,
            'hold_number': h.hold_number,
            'customer_name': h.customer.name if h.customer else 'عميل نقدي',
            'warehouse_name': h.warehouse.name if h.warehouse else '-',
            'items_count': len(items),
            'total': round(total, 2),
            'user_name': (h.user.full_name or h.user.username) if h.user else '-',
            'created_at': h.created_at.strftime('%Y-%m-%d %H:%M') if h.created_at else '',
        })
    return jsonify(out)


@app.route('/api/sales/held/<int:id>/resume', methods=['POST'])
@login_required
def api_resume_held_sale(id):
    """استرجاع فاتورة معلّقة: بترجّع بياناتها للواجهة وتتحذف من قائمة المعلّق فورًا."""
    held = HeldSale.query.get_or_404(id)
    if getattr(current_user, 'role', None) not in ('developer', 'admin', 'manager') and held.user_id != current_user.id:
        return jsonify({'error': 'forbidden'}), 403
    try:
        raw_items = json.loads(held.items_json or '[]')
    except (json.JSONDecodeError, TypeError):
        raw_items = []

    items_out = []
    for it in raw_items:
        product = Product.query.get(it.get('product_id'))
        if not product:
            continue
        stock = Stock.query.filter_by(product_id=product.id, warehouse_id=held.warehouse_id).first()
        items_out.append({
            'id': product.id,
            'name': product.name,
            'code': product.code,
            'barcode': product.barcode or '',
            'stock': float(stock.quantity) if stock else 0.0,
            'qty': it.get('quantity', 0),
            'price': it.get('price', 0),
            'discount': it.get('discount', 0),
        })

    data = {
        'customer_id': held.customer_id or '',
        'warehouse_id': held.warehouse_id or '',
        'notes': held.notes or '',
        'total_discount': held.total_discount or 0,
        'tax': held.tax or 0,
        'items': items_out,
    }
    db.session.delete(held)
    db.session.commit()
    return jsonify(data)


@app.route('/api/sales/held/<int:id>/delete', methods=['POST'])
@login_required
def api_delete_held_sale(id):
    """إلغاء فاتورة معلّقة نهائياً بدون استرجاعها."""
    held = HeldSale.query.get_or_404(id)
    if getattr(current_user, 'role', None) not in ('developer', 'admin', 'manager') and held.user_id != current_user.id:
        return jsonify({'error': 'forbidden'}), 403
    db.session.delete(held)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/sales/<int:id>')
@login_required
def sale_detail(id):
    sale = Sale.query.get_or_404(id)
    sale_has_return = SaleReturn.query.filter_by(sale_id=sale.id).first() is not None
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    # المتبقي الفعلي الذي لم يُسدَّد بعد فعلياً (بعد خصم أي دفعات كشف حساب ارتبطت بهذه الفاتورة)،
    # يُستخدم فقط لضبط نموذج «تسجيل دفعة على الفاتورة» — عرض الفاتورة نفسه يبقى كما صدر (sale.paid/remaining).
    already_paid = _customer_linked_payments_total(sale.customer_id, sale.invoice_number)
    actual_remaining = max(0.0, float(sale.remaining or 0) - already_paid)
    edit_logs = InvoiceEditLog.query.filter_by(invoice_type='sale', invoice_id=sale.id).order_by(InvoiceEditLog.date.desc()).all()
    return render_template(
        'sale_detail.html',
        sale=sale,
        sale_has_return=sale_has_return,
        actual_remaining=actual_remaining,
        edit_logs=edit_logs,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )

@app.route('/sales/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_sale(id):
    sale = Sale.query.get_or_404(id)
    if not user_can(current_user, 'sales_purchases_edit'):
        flash('ليس لديك صلاحية تعديل الفواتير', 'error')
        return redirect(url_for('sale_detail', id=id))
    if SaleReturn.query.filter_by(sale_id=id).first():
        flash('لا يمكن تعديل الفاتورة لوجود مرتجع مرتبط بها — احذف المرتجع أولاً', 'error')
        return redirect(url_for('sale_detail', id=id))

    if request.method == 'POST':
        customer_id = request.form.get('customer_id') or None
        warehouse_id = request.form.get('warehouse_id') or None
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')
        discounts = request.form.getlist('discount[]')

        if not warehouse_id:
            flash('يرجى اختيار المخزن قبل الحفظ', 'error')
            return redirect(url_for('edit_sale', id=id))

        try:
            warehouse_id = int(warehouse_id)
            paid_val = float(request.form.get('paid', 0) or 0)
            total_discount_val = float(request.form.get('total_discount', 0) or 0)
            tax_form_val = float(request.form.get('tax', 0) or 0)
            lines_raw = []
            for i, pid in enumerate(product_ids):
                if not pid:
                    continue
                qty = float(quantities[i] or 0)
                price = float(prices[i] or 0)
                disc = float(discounts[i]) if i < len(discounts) and (discounts[i] not in (None, '')) else 0.0
                lines_raw.append((int(pid), qty, price, disc))
        except (ValueError, TypeError, IndexError):
            flash('توجد بيانات غير صحيحة في الفاتورة (كمية/سعر/خصم) — يرجى مراجعة الأصناف والحفظ مرة أخرى', 'error')
            return redirect(url_for('edit_sale', id=id))

        valid_lines_check = [1 for pid, qty, price, disc in lines_raw if qty > 0 and price > 0]
        if not valid_lines_check:
            flash('يرجى إضافة صنف واحد على الأقل بكمية وسعر صحيحين قبل الحفظ', 'error')
            return redirect(url_for('edit_sale', id=id))
        is_ajal = request.form.get('is_ajal') == '1'
        if paid_val <= 0 and not is_ajal:
            flash('يرجى إدخال المبلغ المدفوع أو تحديد "آجل" لحفظ الفاتورة', 'error')
            return redirect(url_for('edit_sale', id=id))

        merged = {}
        for pid, qty, price, disc in lines_raw:
            if qty <= 0 or price <= 0:
                continue
            if pid not in merged:
                merged[pid] = {'qty': 0.0, 'price': price, 'disc': disc}
            merged[pid]['qty'] += qty
            merged[pid]['price'] = price
            merged[pid]['disc'] = disc
        lines = [(pid, v['qty'], v['price'], v['disc']) for pid, v in merged.items()]

        old_warehouse_id = sale.warehouse_id
        old_items = [(it.product_id, it.quantity) for it in sale.items]
        old_items_full = {it.product_id: {'qty': it.quantity, 'price': it.price, 'disc': it.discount} for it in sale.items}
        old_customer_id = sale.customer_id
        old_remaining = float(sale.remaining or 0)
        old_invoice_number = sale.invoice_number
        old_snapshot = {
            'customer_name': (Customer.query.get(old_customer_id).name if old_customer_id else None),
            'warehouse_name': (Warehouse.query.get(old_warehouse_id).name if old_warehouse_id else None),
            'discount': sale.discount, 'tax': sale.tax, 'total': sale.total,
            'paid': sale.paid, 'notes': sale.notes, 'items': old_items_full,
        }

        try:
            # الخطوة 1: نُرجع كميات الأصناف القديمة إلى مخزنها القديم كأن الفاتورة لم تُنشأ،
            # حتى يُحسب توفر المخزون للأصناف الجديدة بشكل صحيح (سواء كانت نفس الأصناف أو غيرها).
            for pid, qty in old_items:
                stock = Stock.query.filter_by(product_id=pid, warehouse_id=old_warehouse_id).first()
                if stock:
                    stock.quantity += qty
                else:
                    db.session.add(Stock(product_id=pid, warehouse_id=old_warehouse_id, quantity=qty))
            db.session.flush()

            # الخطوة 2: نتحقق من توفر المخزون الكافي للأصناف الجديدة في المخزن الجديد
            for pid, qty, price, disc in lines:
                stock = Stock.query.filter_by(product_id=pid, warehouse_id=warehouse_id).first()
                avail = float(stock.quantity) if stock else 0.0
                if avail + 1e-9 < qty:
                    db.session.rollback()
                    pname = Product.query.get(pid)
                    pname = pname.name if pname else str(pid)
                    short = qty - avail
                    flash(f'المخزون غير كافٍ للصنف «{pname}»: المتوفر {avail:g} والمطلوب {qty:g} — يوجد عجز {short:g}', 'error')
                    return redirect(url_for('edit_sale', id=id))

            # الخطوة 3: نستبدل عناصر الفاتورة ونخصم المخزون الجديد بأمر UPDATE ذري
            sale.items = []
            db.session.flush()

            subtotal = 0
            stock_shortage = None
            for pid, qty, price, disc in lines:
                item_total = qty * price * (1 - disc / 100)
                subtotal += item_total
                item = SaleItem(product_id=pid, quantity=qty, price=price, discount=disc, total=item_total)
                sale.items.append(item)
                result = db.session.execute(
                    db.update(Stock)
                    .where(Stock.product_id == pid, Stock.warehouse_id == warehouse_id,
                           Stock.quantity >= qty - 1e-9)
                    .values(quantity=Stock.quantity - qty)
                )
                if result.rowcount == 0:
                    pname = Product.query.get(pid)
                    stock_shortage = pname.name if pname else str(pid)
                    break

            if stock_shortage:
                db.session.rollback()
                flash(f'تعذّر إتمام التعديل: المخزون المتاح للصنف «{stock_shortage}» تغيّر أثناء الحفظ — يرجى المحاولة مرة أخرى', 'error')
                return redirect(url_for('edit_sale', id=id))

            gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
            sale.subtotal = subtotal
            sale.discount = total_discount_val
            if (gs.get('sale_fixed_tax_enabled') or '0').strip() in ('1', 'true', 'on', 'yes'):
                pct = float(gs.get('sale_fixed_tax_percent') or 0)
                sale.tax = round(subtotal * (pct / 100.0), 2)
            else:
                sale.tax = tax_form_val
            sale.total = subtotal - sale.discount + sale.tax
            sale.paid = paid_val
            pm_val = (request.form.get('payment_method') or sale.payment_method or 'cash').strip().lower()
            sale.payment_method = pm_val if pm_val in ('cash', 'card', 'transfer', 'other') else 'cash'
            new_remaining = sale.total - sale.paid

            # لا يمكن تقليل الفاتورة لأقل مما تم تحصيله فعلاً عبر كشف الحساب (دفعات مرتبطة بهذه
            # الفاتورة تحديداً)، لأن هذه الدفعات تبقى قائمة كما هي ولا تُلغى عند التعديل.
            already_linked_paid = _customer_linked_payments_total(old_customer_id, old_invoice_number) if old_customer_id else 0.0
            if new_remaining + 1e-6 < already_linked_paid:
                db.session.rollback()
                flash(f'لا يمكن تقليل إجمالي الفاتورة لأقل من المبلغ المسدَّد فعلياً عبر كشف الحساب ({already_linked_paid:.2f}) — يرجى تعديل/حذف الدفعات المرتبطة أولاً من كشف الحساب', 'error')
                return redirect(url_for('edit_sale', id=id))

            sale.remaining = new_remaining
            sale.warehouse_id = warehouse_id
            sale.notes = request.form.get('notes')
            new_customer_id = int(customer_id) if customer_id else None

            # تحديث رصيد العميل: نلغي الأثر القديم للفاتورة ونضيف الأثر الجديد، مع الحفاظ على
            # أي دفعات مرتبطة بها في كشف الحساب كما هي (لا تُمس).
            if old_customer_id != new_customer_id:
                if old_customer_id:
                    old_customer = Customer.query.get(old_customer_id)
                    if old_customer:
                        old_customer.balance -= old_remaining
                if new_customer_id:
                    new_customer = Customer.query.get(new_customer_id)
                    if new_customer:
                        new_customer.balance += new_remaining
            else:
                if new_customer_id:
                    customer = Customer.query.get(new_customer_id)
                    if customer:
                        customer.balance += (new_remaining - old_remaining)

            sale.customer_id = new_customer_id

            new_notes = request.form.get('notes')
            new_snapshot = {
                'customer_name': (Customer.query.get(new_customer_id).name if new_customer_id else None),
                'warehouse_name': (Warehouse.query.get(warehouse_id).name if warehouse_id else None),
                'discount': sale.discount, 'tax': sale.tax, 'total': sale.total,
                'paid': sale.paid, 'notes': new_notes,
                'items': {pid: {'qty': v['qty'], 'price': v['price'], 'disc': v['disc']} for pid, v in merged.items()},
            }
            all_pids = set(old_items_full) | set(merged)
            product_names = {pid: (Product.query.get(pid).name if Product.query.get(pid) else str(pid)) for pid in all_pids}
            summary = _diff_sale_edit(old_snapshot, new_snapshot, product_names, party_label='العميل')
            _log_invoice_edit('sale', sale.id, sale.invoice_number, current_user.id, summary)

            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash('حدث خطأ غير متوقع أثناء حفظ التعديل — لم يتم حفظ أي بيانات', 'error')
            return redirect(url_for('edit_sale', id=id))

        flash(f'تم تعديل الفاتورة {sale.invoice_number} بنجاح', 'success')
        return redirect(url_for('sale_detail', id=sale.id))

    # GET: عرض فورم التعديل مع تعبئة بيانات الفاتورة الحالية
    customers = Customer.query.filter_by(is_active=True).all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    edit_items = []
    for it in sale.items:
        product = Product.query.get(it.product_id)
        stock = Stock.query.filter_by(product_id=it.product_id, warehouse_id=sale.warehouse_id).first()
        current_qty = float(stock.quantity) if stock else 0.0
        edit_items.append({
            'id': it.product_id,
            'name': product.name if product else '',
            'code': getattr(product, 'code', '') if product else '',
            'barcode': getattr(product, 'barcode', '') if product else '',
            'unit': getattr(product, 'unit', '') if product else '',
            'qty': it.quantity,
            'price': it.price,
            'discount': it.discount,
            'stock': current_qty + it.quantity,
        })
    return render_template(
        'sale_form.html',
        customers=customers,
        warehouses=warehouses,
        default_customer_id=sale.customer_id,
        default_warehouse_id=sale.warehouse_id,
        sale_tax_auto=(gs.get('sale_fixed_tax_enabled') or '0').strip() in ('1', 'true', 'on', 'yes'),
        sale_tax_percent=float(gs.get('sale_fixed_tax_percent') or 0),
        edit_mode=True,
        sale=sale,
        edit_items=edit_items,
        form_action=url_for('edit_sale', id=sale.id),
    )


@app.route('/sales/<int:id>/delete', methods=['POST'])
@login_required
def delete_sale(id):
    sale = Sale.query.get_or_404(id)
    if not user_can(current_user, 'sales_purchases_delete'):
        flash('ليس لديك صلاحية حذف الفواتير', 'error')
        return redirect(url_for('sales'))
    if SaleReturn.query.filter_by(sale_id=id).first():
        flash('لا يمكن حذف الفاتورة لوجود مرتجع مرتبط بها', 'error')
        return redirect(url_for('sales'))
    for item in sale.items:
        stock = Stock.query.filter_by(product_id=item.product_id, warehouse_id=sale.warehouse_id).first()
        if stock:
            stock.quantity += item.quantity
    if sale.customer_id:
        customer = Customer.query.get(sale.customer_id)
        if customer:
            # نحذف أي دفعات سريعة (كشف حساب) مرتبطة تحديداً بهذه الفاتورة، ونحسب صافي
            # أثرها على الرصيد: المتبقي الأصلي وقت الإصدار ناقص ما دُفع فعلاً من خلالها،
            # حتى لا يتكرر خصم/إضافة نفس المبلغ مرتين على رصيد العميل.
            linked_paid = _purge_linked_customer_payments(customer.id, sale.invoice_number)
            net_effect = float(sale.remaining or 0) - linked_paid
            if abs(net_effect) > 0.0001:
                customer.balance -= net_effect
    db.session.delete(sale)
    db.session.commit()
    flash(f'تم حذف الفاتورة {sale.invoice_number} بنجاح', 'success')
    return redirect(url_for('sales'))

# ===== PURCHASES =====
@app.route('/purchases')
@login_required
def purchases():
    page = request.args.get('page', 1, type=int)
    purchases = Purchase.query.order_by(Purchase.date.desc()).paginate(page=page, per_page=20)
    returned_purchase_ids = {r[0] for r in db.session.query(PurchaseReturn.purchase_id).distinct().all()}
    fully_settled_purchase_ids = set()
    for p in purchases.items:
        if (p.remaining or 0) <= 0.0001:
            fully_settled_purchase_ids.add(p.id)
        elif p.supplier_id:
            paid = _supplier_linked_payments_total(p.supplier_id, p.invoice_number)
            if paid + 1e-6 >= float(p.remaining or 0):
                fully_settled_purchase_ids.add(p.id)
    return render_template('purchases.html', purchases=purchases, returned_purchase_ids=returned_purchase_ids,
                           fully_settled_purchase_ids=fully_settled_purchase_ids)

@app.route('/purchases/new', methods=['GET', 'POST'])
@login_required
def new_purchase():
    if request.method == 'POST':
        supplier_id = request.form.get('supplier_id') or None
        warehouse_id = request.form.get('warehouse_id') or None
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')

        # ── Validation: لا حفظ بدون بيانات كاملة ──
        if not warehouse_id:
            flash('يرجى اختيار المخزن قبل الحفظ', 'error')
            return redirect(url_for('new_purchase'))

        # ── تحويل آمن للأرقام بدل ما أي قيمة غير صالحة تسبب كراش (500) ──
        try:
            warehouse_id = int(warehouse_id)
            paid_val = float(request.form.get('paid', 0) or 0)
            total_discount_val = float(request.form.get('total_discount', 0) or 0)
            tax_val = float(request.form.get('tax', 0) or 0)
            withholding_val = float(request.form.get('withholding_tax', 0) or 0)
            lines = []
            for i, pid in enumerate(product_ids):
                if not pid:
                    continue
                qty = float(quantities[i] or 0)
                price = float(prices[i] or 0)
                lines.append((int(pid), qty, price))
        except (ValueError, TypeError, IndexError):
            flash('توجد بيانات غير صحيحة في فاتورة الشراء (كمية/سعر) — يرجى مراجعة الأصناف والحفظ مرة أخرى', 'error')
            return redirect(url_for('new_purchase'))

        valid_lines = [1 for pid, qty, price in lines if qty > 0 and price > 0]
        if not valid_lines:
            flash('يرجى إضافة صنف واحد على الأقل بكمية وسعر صحيحين قبل الحفظ', 'error')
            return redirect(url_for('new_purchase'))
        is_ajal = request.form.get('is_ajal') == '1'
        if paid_val <= 0 and not is_ajal:
            flash('يرجى إدخال المبلغ المدفوع أو تحديد "آجل" لحفظ الفاتورة', 'error')
            return redirect(url_for('new_purchase'))

        merged = {}
        for pid, qty, price in lines:
            if qty <= 0 or price <= 0:
                continue
            if pid not in merged:
                merged[pid] = {'qty': 0.0, 'price': price}
            merged[pid]['qty'] += qty
            merged[pid]['price'] = price
        notes_val = request.form.get('notes')

        # ── حفظ الفاتورة مع إعادة محاولة آمنة عند تعارض رقم الفاتورة (تزامن) ──
        max_attempts = 5
        purchase = None
        for attempt in range(1, max_attempts + 1):
            try:
                purchase = Purchase(
                    invoice_number=get_next_number('PUR', Purchase, 'invoice_number'),
                    supplier_id=supplier_id,
                    warehouse_id=warehouse_id,
                    user_id=current_user.id,
                    discount=total_discount_val,
                    tax=tax_val,
                    withholding_tax=withholding_val,
                    paid=paid_val,
                    notes=notes_val,
                )
                subtotal = 0
                for pid, line in merged.items():
                    qty = line['qty']
                    price = line['price']
                    item_total = qty * price
                    subtotal += item_total
                    item = PurchaseItem(product_id=int(pid), quantity=qty, price=price, total=item_total)
                    purchase.items.append(item)
                    stock = Stock.query.filter_by(product_id=int(pid), warehouse_id=warehouse_id).first()
                    if stock:
                        stock.quantity += qty
                    else:
                        db.session.add(Stock(product_id=int(pid), warehouse_id=int(warehouse_id), quantity=qty))

                purchase.subtotal = subtotal
                purchase.total = subtotal - purchase.discount + purchase.tax - (purchase.withholding_tax or 0)
                purchase.remaining = purchase.total - purchase.paid

                if supplier_id and purchase.remaining > 0:
                    supplier = Supplier.query.get(supplier_id)
                    if supplier:
                        supplier.balance += purchase.remaining

                db.session.add(purchase)
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if attempt == max_attempts:
                    flash('تعذّر حفظ فاتورة الشراء بسبب تعارض في الترقيم — يرجى المحاولة مرة أخرى', 'error')
                    return redirect(url_for('new_purchase'))
                continue
            except SQLAlchemyError:
                db.session.rollback()
                flash('حدث خطأ غير متوقع أثناء حفظ فاتورة الشراء — لم يتم حفظ أي بيانات، برجاء المحاولة مرة أخرى', 'error')
                return redirect(url_for('new_purchase'))

        flash(f'تم إنشاء فاتورة الشراء {purchase.invoice_number} بنجاح', 'success')
        auto_print = (get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None)).get('print_auto_purchase') or '0').strip() in ('1', 'true', 'on', 'yes')
        if auto_print:
            return redirect(url_for('purchase_detail', id=purchase.id, autoprint='1'))
        return redirect(url_for('purchase_detail', id=purchase.id))
    
    suppliers = Supplier.query.filter_by(is_active=True).all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()

    # المورد الأكثر استخداماً
    top_supplier = db.session.query(Purchase.supplier_id, db.func.count(Purchase.id).label('cnt'))\
        .filter(Purchase.supplier_id != None)\
        .group_by(Purchase.supplier_id).order_by(db.text('cnt DESC')).first()
    default_supplier_id = top_supplier[0] if top_supplier else None

    # المخزن الأكثر استخداماً في المشتريات
    top_wh = db.session.query(Purchase.warehouse_id, db.func.count(Purchase.id).label('cnt'))\
        .filter(Purchase.warehouse_id != None)\
        .group_by(Purchase.warehouse_id).order_by(db.text('cnt DESC')).first()
    default_warehouse_id = top_wh[0] if top_wh else (warehouses[0].id if warehouses else None)

    return render_template('purchase_form.html',
        suppliers=suppliers,
        warehouses=warehouses,
        default_supplier_id=default_supplier_id,
        default_warehouse_id=default_warehouse_id,
    )

@app.route('/purchases/<int:id>')
@login_required
def purchase_detail(id):
    purchase = Purchase.query.get_or_404(id)
    purchase_has_return = PurchaseReturn.query.filter_by(purchase_id=purchase.id).first() is not None
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    already_paid = _supplier_linked_payments_total(purchase.supplier_id, purchase.invoice_number)
    actual_remaining = max(0.0, float(purchase.remaining or 0) - already_paid)
    edit_logs = InvoiceEditLog.query.filter_by(invoice_type='purchase', invoice_id=purchase.id).order_by(InvoiceEditLog.date.desc()).all()
    return render_template(
        'purchase_detail.html',
        purchase=purchase,
        purchase_has_return=purchase_has_return,
        actual_remaining=actual_remaining,
        edit_logs=edit_logs,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )

@app.route('/purchases/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_purchase(id):
    purchase = Purchase.query.get_or_404(id)
    if not user_can(current_user, 'sales_purchases_edit'):
        flash('ليس لديك صلاحية تعديل الفواتير', 'error')
        return redirect(url_for('purchase_detail', id=id))
    if PurchaseReturn.query.filter_by(purchase_id=id).first():
        flash('لا يمكن تعديل الفاتورة لوجود مرتجع مرتبط بها — احذف المرتجع أولاً', 'error')
        return redirect(url_for('purchase_detail', id=id))

    if request.method == 'POST':
        supplier_id = request.form.get('supplier_id') or None
        warehouse_id = request.form.get('warehouse_id') or None
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')

        if not warehouse_id:
            flash('يرجى اختيار المخزن قبل الحفظ', 'error')
            return redirect(url_for('edit_purchase', id=id))

        try:
            warehouse_id = int(warehouse_id)
            paid_val = float(request.form.get('paid', 0) or 0)
            total_discount_val = float(request.form.get('total_discount', 0) or 0)
            tax_val = float(request.form.get('tax', 0) or 0)
            withholding_val = float(request.form.get('withholding_tax', 0) or 0)
            lines = []
            for i, pid in enumerate(product_ids):
                if not pid:
                    continue
                qty = float(quantities[i] or 0)
                price = float(prices[i] or 0)
                lines.append((int(pid), qty, price))
        except (ValueError, TypeError, IndexError):
            flash('توجد بيانات غير صحيحة في فاتورة الشراء (كمية/سعر) — يرجى مراجعة الأصناف والحفظ مرة أخرى', 'error')
            return redirect(url_for('edit_purchase', id=id))

        valid_lines = [1 for pid, qty, price in lines if qty > 0 and price > 0]
        if not valid_lines:
            flash('يرجى إضافة صنف واحد على الأقل بكمية وسعر صحيحين قبل الحفظ', 'error')
            return redirect(url_for('edit_purchase', id=id))
        is_ajal = request.form.get('is_ajal') == '1'
        if paid_val <= 0 and not is_ajal:
            flash('يرجى إدخال المبلغ المدفوع أو تحديد "آجل" لحفظ الفاتورة', 'error')
            return redirect(url_for('edit_purchase', id=id))

        merged = {}
        for pid, qty, price in lines:
            if qty <= 0 or price <= 0:
                continue
            if pid not in merged:
                merged[pid] = {'qty': 0.0, 'price': price}
            merged[pid]['qty'] += qty
            merged[pid]['price'] = price

        old_warehouse_id = purchase.warehouse_id
        old_items = [(it.product_id, it.quantity) for it in purchase.items]
        old_items_full = {it.product_id: {'qty': it.quantity, 'price': it.price, 'disc': 0} for it in purchase.items}
        old_supplier_id = purchase.supplier_id
        old_remaining = float(purchase.remaining or 0)
        old_invoice_number = purchase.invoice_number
        old_snapshot = {
            'customer_name': (Supplier.query.get(old_supplier_id).name if old_supplier_id else None),
            'warehouse_name': (Warehouse.query.get(old_warehouse_id).name if old_warehouse_id else None),
            'discount': purchase.discount, 'tax': purchase.tax, 'total': purchase.total,
            'paid': purchase.paid, 'notes': purchase.notes, 'items': old_items_full,
        }

        try:
            # الخطوة 1: نتأكد إن مخزون كل صنف من هذه الفاتورة لسه موجود بالكامل قبل ما نرجعه
            # (لو جزء منه اتباع/اتحوّل بالفعل، التعديل ممكن يسبب عجز وهمي في المخزون)
            for pid, qty in old_items:
                stock = Stock.query.filter_by(product_id=pid, warehouse_id=old_warehouse_id).first()
                current = float(stock.quantity) if stock else 0.0
                if current + 1e-9 < qty:
                    pname = Product.query.get(pid)
                    pname = pname.name if pname else str(pid)
                    flash(f'تعذّر التعديل: جزء من كمية الصنف «{pname}» المضافة بهذه الفاتورة تم بيعه/تحويله بالفعل من المخزن — لا يمكن التعديل دون التأثير سلباً على دقة المخزون', 'error')
                    return redirect(url_for('edit_purchase', id=id))

            # الخطوة 2: نعكس تأثير الأصناف القديمة على المخزون القديم (كأن الفاتورة لم تُنشأ)
            for pid, qty in old_items:
                stock = Stock.query.filter_by(product_id=pid, warehouse_id=old_warehouse_id).first()
                if stock:
                    stock.quantity -= qty
            db.session.flush()

            # الخطوة 3: نستبدل عناصر الفاتورة ونضيف المخزون الجديد في المخزن (الجديد أو نفس القديم)
            purchase.items = []
            db.session.flush()

            subtotal = 0
            for pid, line in merged.items():
                qty = line['qty']
                price = line['price']
                item_total = qty * price
                subtotal += item_total
                item = PurchaseItem(product_id=int(pid), quantity=qty, price=price, total=item_total)
                purchase.items.append(item)
                stock = Stock.query.filter_by(product_id=int(pid), warehouse_id=warehouse_id).first()
                if stock:
                    stock.quantity += qty
                else:
                    db.session.add(Stock(product_id=int(pid), warehouse_id=int(warehouse_id), quantity=qty))

            purchase.subtotal = subtotal
            purchase.discount = total_discount_val
            purchase.tax = tax_val
            purchase.withholding_tax = withholding_val
            purchase.total = subtotal - purchase.discount + purchase.tax - (purchase.withholding_tax or 0)
            purchase.paid = paid_val
            new_remaining = purchase.total - purchase.paid

            # لا يمكن تقليل الفاتورة لأقل مما تم تحصيله فعلاً عبر كشف الحساب لهذه الفاتورة تحديداً
            already_linked_paid = _supplier_linked_payments_total(old_supplier_id, old_invoice_number) if old_supplier_id else 0.0
            if new_remaining + 1e-6 < already_linked_paid:
                db.session.rollback()
                flash(f'لا يمكن تقليل إجمالي الفاتورة لأقل من المبلغ المسدَّد فعلياً عبر كشف الحساب ({already_linked_paid:.2f}) — يرجى تعديل/حذف الدفعات المرتبطة أولاً من كشف الحساب', 'error')
                return redirect(url_for('edit_purchase', id=id))

            purchase.remaining = new_remaining
            purchase.warehouse_id = warehouse_id
            purchase.notes = request.form.get('notes')
            new_supplier_id = int(supplier_id) if supplier_id else None

            if old_supplier_id != new_supplier_id:
                if old_supplier_id:
                    old_supplier = Supplier.query.get(old_supplier_id)
                    if old_supplier:
                        old_supplier.balance -= old_remaining
                if new_supplier_id:
                    new_supplier = Supplier.query.get(new_supplier_id)
                    if new_supplier:
                        new_supplier.balance += new_remaining
            else:
                if new_supplier_id:
                    supplier = Supplier.query.get(new_supplier_id)
                    if supplier:
                        supplier.balance += (new_remaining - old_remaining)

            purchase.supplier_id = new_supplier_id

            new_notes = request.form.get('notes')
            new_snapshot = {
                'customer_name': (Supplier.query.get(new_supplier_id).name if new_supplier_id else None),
                'warehouse_name': (Warehouse.query.get(warehouse_id).name if warehouse_id else None),
                'discount': purchase.discount, 'tax': purchase.tax, 'total': purchase.total,
                'paid': purchase.paid, 'notes': new_notes,
                'items': {pid: {'qty': v['qty'], 'price': v['price'], 'disc': 0} for pid, v in merged.items()},
            }
            all_pids = set(old_items_full) | set(merged)
            product_names = {pid: (Product.query.get(pid).name if Product.query.get(pid) else str(pid)) for pid in all_pids}
            summary = _diff_sale_edit(old_snapshot, new_snapshot, product_names, party_label='المورد')
            _log_invoice_edit('purchase', purchase.id, purchase.invoice_number, current_user.id, summary)

            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash('حدث خطأ غير متوقع أثناء حفظ التعديل — لم يتم حفظ أي بيانات', 'error')
            return redirect(url_for('edit_purchase', id=id))

        flash(f'تم تعديل فاتورة الشراء {purchase.invoice_number} بنجاح', 'success')
        return redirect(url_for('purchase_detail', id=purchase.id))

    # GET: عرض فورم التعديل مع تعبئة بيانات الفاتورة الحالية
    suppliers = Supplier.query.filter_by(is_active=True).all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    edit_items = []
    for it in purchase.items:
        product = Product.query.get(it.product_id)
        stock = Stock.query.filter_by(product_id=it.product_id, warehouse_id=purchase.warehouse_id).first()
        current_qty = float(stock.quantity) if stock else 0.0
        edit_items.append({
            'id': it.product_id,
            'name': product.name if product else '',
            'code': getattr(product, 'code', '') if product else '',
            'barcode': getattr(product, 'barcode', '') if product else '',
            'unit': getattr(product, 'unit', '') if product else '',
            'qty': it.quantity,
            'price': it.price,
            'discount': 0,
            'stock': current_qty,
        })
    return render_template(
        'purchase_form.html',
        suppliers=suppliers,
        warehouses=warehouses,
        default_supplier_id=purchase.supplier_id,
        default_warehouse_id=purchase.warehouse_id,
        edit_mode=True,
        purchase=purchase,
        edit_items=edit_items,
        form_action=url_for('edit_purchase', id=purchase.id),
    )


@app.route('/purchases/<int:id>/delete', methods=['POST'])
@login_required
def delete_purchase(id):
    purchase = Purchase.query.get_or_404(id)
    # التحقق من الصلاحية
    if not user_can(current_user, 'sales_purchases_delete'):
        flash('ليس لديك صلاحية حذف الفواتير', 'error')
        return redirect(url_for('purchases'))
    # منع الحذف لو فيه مرتجع مرتبط
    if PurchaseReturn.query.filter_by(purchase_id=id).first():
        flash('لا يمكن حذف الفاتورة لوجود مرتجع مرتبط بها', 'error')
        return redirect(url_for('purchases'))
    # عكس تأثير الفاتورة على المخزن والرصيد
    for item in purchase.items:
        stock = Stock.query.filter_by(product_id=item.product_id, warehouse_id=purchase.warehouse_id).first()
        if stock:
            stock.quantity -= item.quantity
    if purchase.supplier_id:
        supplier = Supplier.query.get(purchase.supplier_id)
        if supplier:
            # نفس منطق حذف الفاتورة على جهة العميل: نحذف الدفعات السريعة المرتبطة بها
            # ونحسب صافي أثرها على رصيد المورد بدل الخصم بالكامل بشكل منفصل.
            linked_paid = _purge_linked_supplier_payments(supplier.id, purchase.invoice_number)
            net_effect = float(purchase.remaining or 0) - linked_paid
            if abs(net_effect) > 0.0001:
                supplier.balance -= net_effect
    db.session.delete(purchase)
    db.session.commit()
    flash(f'تم حذف فاتورة الشراء {purchase.invoice_number} بنجاح', 'success')
    return redirect(url_for('purchases'))

# ===== RETURNS =====
@app.route('/returns/sale')
@login_required
def sale_returns():
    returns = SaleReturn.query.order_by(SaleReturn.date.desc()).all()
    return render_template('sale_returns.html', returns=returns)

@app.route('/returns/sale/new', methods=['GET', 'POST'])
@login_required
def new_sale_return():
    if request.method == 'POST':
        sale_id = request.form.get('sale_id') or None
        if not sale_id:
            flash('يرجى اختيار فاتورة البيع قبل الحفظ', 'error')
            return redirect(url_for('new_sale_return'))
        sale = Sale.query.options(joinedload(Sale.items)).get_or_404(sale_id)
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')
        discounts = request.form.getlist('discount[]')
        extra_discounts = request.form.getlist('extra_discount[]')

        # ── تحويل آمن للأرقام بدل ما أي قيمة غير صالحة تسبب كراش (500) ──
        try:
            parsed_items = []
            for i, pid in enumerate(product_ids):
                if not pid:
                    continue
                qty = float(quantities[i] or 0)
                price = float(prices[i] or 0)
                disc = float(discounts[i]) if i < len(discounts) and (discounts[i] not in (None, '')) else 0.0
                extra = float(extra_discounts[i]) if i < len(extra_discounts) and (extra_discounts[i] not in (None, '')) else 0.0
                parsed_items.append((int(pid), qty, price, disc, extra))
        except (ValueError, TypeError, IndexError):
            flash('توجد بيانات غير صحيحة في المرتجع (كمية/سعر/خصم) — يرجى المراجعة والحفظ مرة أخرى', 'error')
            return redirect(url_for('new_sale_return'))

        # ── Validation: لا حفظ بدون أصناف ──
        valid_items_sr = [1 for pid, qty, price, disc, extra in parsed_items if qty > 0]
        if not valid_items_sr:
            flash('يرجى إضافة صنف واحد على الأقل بكمية صحيحة قبل الحفظ', 'error')
            return redirect(url_for('new_sale_return'))

        planned_qty = defaultdict(float)
        for pid, qty, price, disc, extra in parsed_items:
            planned_qty[pid] += qty
        for pid, pq in planned_qty.items():
            max_ret = sale_returnable_quantity(sale, pid)
            if pq > max_ret + 1e-9:
                flash(f'مجموع الكمية المرتجعة للصنف يتجاوز المتاح ({max_ret:g} وفق الفاتورة والمرتجعات السابقة)', 'error')
                return redirect(url_for('new_sale_return'))

        reason_val = request.form.get('reason')

        # ── حفظ المرتجع مع إعادة محاولة آمنة عند تعارض رقم الفاتورة (تزامن) ──
        max_attempts = 5
        ret = None
        for attempt in range(1, max_attempts + 1):
            try:
                ret = SaleReturn(
                    invoice_number=get_next_number('SRT', SaleReturn, 'invoice_number'),
                    sale_id=sale_id, user_id=current_user.id,
                    reason=reason_val,
                )
                total = 0
                for pid, qty, price, disc, extra in parsed_items:
                    base = qty * price * (1 - disc / 100)
                    item_total = round(base * (1 - extra / 100), 4)
                    total += item_total
                    ret.items.append(SaleReturnItem(
                        product_id=pid, quantity=qty, price=price, discount=disc,
                        extra_discount=extra, total=item_total))
                    stock = Stock.query.filter_by(product_id=pid, warehouse_id=sale.warehouse_id).first()
                    if stock:
                        stock.quantity += qty
                ret.total = total
                db.session.add(ret)
                if sale.customer_id:
                    customer = Customer.query.get(sale.customer_id)
                    if customer:
                        customer.balance -= total
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if attempt == max_attempts:
                    flash('تعذّر حفظ مرتجع المبيعات بسبب تعارض في الترقيم — يرجى المحاولة مرة أخرى', 'error')
                    return redirect(url_for('new_sale_return'))
                continue
            except SQLAlchemyError:
                db.session.rollback()
                flash('حدث خطأ غير متوقع أثناء حفظ المرتجع — لم يتم حفظ أي بيانات، برجاء المحاولة مرة أخرى', 'error')
                return redirect(url_for('new_sale_return'))

        flash('تم تسجيل مرتجع المبيعات بنجاح', 'success')
        auto_print = (get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None)).get('print_auto_sale_return') or '0').strip() in ('1', 'true', 'on', 'yes')
        if auto_print:
            return redirect(url_for('sale_return_detail', id=ret.id, autoprint='1'))
        return redirect(url_for('sale_return_detail', id=ret.id))

    sales = Sale.query.options(
        joinedload(Sale.items).joinedload(SaleItem.product),
        joinedload(Sale.customer),
    ).order_by(Sale.date.desc()).limit(100).all()
    sales_json = []
    for s in sales:
        sales_json.append({
            'id': s.id,
            'items': [
                {
                    'product_id': it.product_id,
                    'name': it.product.name if it.product else '',
                    'code': it.product.code if it.product else '',
                    'price': float(it.price),
                    'discount': float(it.discount or 0),
                    'quantity': float(it.quantity or 0),
                }
                for it in s.items if it.product_id
            ],
        })
    return render_template('sale_return_form.html', sales=sales, sales_json=sales_json)

@app.route('/returns/sale/<int:id>/delete', methods=['POST'])
@login_required
@returns_delete_required
def delete_sale_return(id):
    ret = SaleReturn.query.get_or_404(id)
    sale = ret.sale
    warehouse_id = sale.warehouse_id if sale else None
    total = float(ret.total or 0)
    try:
        for item in list(ret.items):
            qty = float(item.quantity or 0)
            if warehouse_id and item.product_id and qty:
                stock = Stock.query.filter_by(product_id=item.product_id, warehouse_id=warehouse_id).first()
                if stock:
                    stock.quantity -= qty
            db.session.delete(item)
        if sale and sale.customer_id and total:
            customer = Customer.query.get(sale.customer_id)
            if customer:
                customer.balance += total
        invoice_number = ret.invoice_number
        db.session.delete(ret)
        db.session.commit()
        flash(f'تم حذف مرتجع المبيعات {invoice_number} وإلغاء أثره', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'تعذر حذف المرتجع: {e}', 'error')
        return redirect(url_for('sale_return_detail', id=id))
    return redirect(url_for('sale_returns'))

# ===== TRANSFERS =====
@app.route('/transfers')
@login_required
def transfers():
    status = request.args.get('status', '')
    query = TransferRequest.query
    if status:
        query = query.filter_by(status=status)
    transfers = query.order_by(TransferRequest.date_requested.desc()).all()
    transfer_can_act = {t.id: can_user_act_on_transfer(t, current_user) for t in transfers}
    return render_template('transfers.html', transfers=transfers, status=status, transfer_can_act=transfer_can_act)

@app.route('/transfers/new', methods=['GET', 'POST'])
@login_required
def new_transfer():
    if request.method == 'POST':
        try:
            from_wh = int(request.form.get('from_warehouse_id') or 0)
            to_wh = int(request.form.get('to_warehouse_id') or 0)
        except (TypeError, ValueError):
            flash('يرجى اختيار المخازن بشكل صحيح', 'error')
            return redirect(url_for('new_transfer'))
        if not from_wh or not to_wh:
            flash('يرجى اختيار المخزن المرسل والمخزن المستقبل', 'error')
            return redirect(url_for('new_transfer'))
        if from_wh == to_wh:
            flash('لا يمكن التحويل من وإلى نفس المخزن', 'error')
            return redirect(url_for('new_transfer'))
        try:
            approver_id = int(request.form.get('approver_user_id') or 0)
        except (TypeError, ValueError):
            approver_id = 0
        if not approver_id:
            flash('يرجى اختيار المستخدم المكلَّف بالموافقة على التحويل', 'error')
            return redirect(url_for('new_transfer'))
        appr = User.query.get(approver_id)
        if not appr or not appr.is_active or not user_can_approve_transfers(appr):
            flash('المستخدم المختار غير مخوّل بالموافقة على التحويلات', 'error')
            return redirect(url_for('new_transfer'))
        if approver_id == current_user.id:
            flash('لا يمكنك تعيين نفسك للموافقة على طلبك', 'error')
            return redirect(url_for('new_transfer'))

        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        try:
            items_data = []
            for i, pid in enumerate(product_ids):
                if not pid:
                    continue
                qty = float(quantities[i] or 0)
                if qty <= 0:
                    continue
                items_data.append((int(pid), qty))
        except (TypeError, ValueError, IndexError):
            flash('توجد كميات غير صحيحة في طلب التحويل — يرجى المراجعة والمحاولة مرة أخرى', 'error')
            return redirect(url_for('new_transfer'))
        if not items_data:
            flash('يرجى إضافة صنف واحد على الأقل بكمية صحيحة', 'error')
            return redirect(url_for('new_transfer'))
        notes_val = request.form.get('notes')

        # ── حفظ مع إعادة محاولة آمنة عند تعارض رقم الطلب (تزامن) ──
        max_attempts = 5
        transfer = None
        for attempt in range(1, max_attempts + 1):
            transfer = TransferRequest(
                request_number=get_next_number('TRF', TransferRequest, 'request_number'),
                from_warehouse_id=from_wh,
                to_warehouse_id=to_wh,
                requested_by=current_user.id,
                approver_user_id=approver_id,
                notes=notes_val,
                status='pending'
            )
            for pid, qty in items_data:
                transfer.items.append(TransferItem(product_id=pid, quantity=qty))
            db.session.add(transfer)
            try:
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if attempt == max_attempts:
                    flash('تعذّر إرسال طلب التحويل بسبب تعارض في الترقيم — يرجى المحاولة مرة أخرى', 'error')
                    return redirect(url_for('new_transfer'))
                continue
            except SQLAlchemyError:
                db.session.rollback()
                flash('حدث خطأ غير متوقع أثناء إرسال طلب التحويل — لم يتم حفظ أي بيانات', 'error')
                return redirect(url_for('new_transfer'))

        flash(f'تم إرسال طلب التحويل {transfer.request_number} بنجاح، في انتظار الموافقة', 'success')
        return redirect(url_for('transfers'))

    warehouses = Warehouse.query.filter_by(is_active=True).all()
    approver_choices = [u for u in User.query.filter_by(is_active=True).order_by(User.full_name, User.username).all()
                        if user_can_approve_transfers(u) and u.id != current_user.id]
    return render_template('transfer_form.html', warehouses=warehouses, approver_choices=approver_choices)

@app.route('/transfers/<int:id>/approve', methods=['POST'])
@login_required
def approve_transfer(id):
    transfer = TransferRequest.query.get_or_404(id)
    if not can_user_act_on_transfer(transfer, current_user):
        flash('لا يمكنك الموافقة على هذا الطلب (لست المكلَّفاً أو أنت صاحب الطلب)', 'error')
        return redirect(url_for('transfer_detail', id=id))
    if transfer.status != 'pending':
        flash('هذا الطلب تم معالجته مسبقاً', 'error')
        return redirect(url_for('transfers'))

    # ── خصم المخزون بأمر UPDATE ذري: يمنع التحويل لو الكمية المتاحة فعليًا وقت
    #    التنفيذ أقل من المطلوب (مثلاً لو اتباعت كمية من المخزن بعد إرسال طلب التحويل) ──
    shortage_product = None
    for item in transfer.items:
        result = db.session.execute(
            db.update(Stock)
            .where(Stock.product_id == item.product_id, Stock.warehouse_id == transfer.from_warehouse_id,
                   Stock.quantity >= item.quantity - 1e-9)
            .values(quantity=Stock.quantity - item.quantity)
        )
        if result.rowcount == 0:
            shortage_product = item.product.name if item.product else str(item.product_id)
            break
        to_stock = Stock.query.filter_by(product_id=item.product_id, warehouse_id=transfer.to_warehouse_id).first()
        if to_stock:
            to_stock.quantity += item.quantity
        else:
            db.session.add(Stock(product_id=item.product_id, warehouse_id=transfer.to_warehouse_id, quantity=item.quantity))

    if shortage_product:
        db.session.rollback()
        flash(f'لا يوجد رصيد كافٍ حالياً للصنف: {shortage_product}', 'error')
        return redirect(url_for('transfers'))
    transfer.status = 'approved'
    transfer.approved_by = current_user.id
    transfer.date_processed = datetime.utcnow()
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash('حدث خطأ غير متوقع أثناء تنفيذ التحويل — لم يتم حفظ أي بيانات', 'error')
        return redirect(url_for('transfers'))
    flash('تم الموافقة على طلب التحويل وتنفيذه بنجاح', 'success')
    return redirect(url_for('transfers'))

@app.route('/transfers/<int:id>/reject', methods=['POST'])
@login_required
def reject_transfer(id):
    transfer = TransferRequest.query.get_or_404(id)
    if not can_user_act_on_transfer(transfer, current_user):
        flash('لا يمكنك رفض هذا الطلب (لست المكلَّفاً أو أنت صاحب الطلب)', 'error')
        return redirect(url_for('transfer_detail', id=id))
    if transfer.status != 'pending':
        flash('هذا الطلب تم معالجته مسبقاً', 'error')
        return redirect(url_for('transfers'))

    transfer.status = 'rejected'
    transfer.approved_by = current_user.id
    transfer.date_processed = datetime.utcnow()
    transfer.rejection_reason = request.form.get('reason', 'لم يذكر سبب')
    db.session.commit()
    flash('تم رفض طلب التحويل', 'success')
    return redirect(url_for('transfers'))

@app.route('/transfers/<int:id>')
@login_required
def transfer_detail(id):
    transfer = TransferRequest.query.get_or_404(id)
    return render_template(
        'transfer_detail.html',
        transfer=transfer,
        can_act_transfer=can_user_act_on_transfer(transfer, current_user),
    )

# ===== INVENTORY =====
@app.route('/inventory')
@login_required
def inventory():
    warehouse_id = request.args.get('warehouse_id')
    q = request.args.get('q', '')
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    query = db.session.query(Stock, Product, Warehouse).join(Product).join(Warehouse)
    if warehouse_id:
        query = query.filter(Stock.warehouse_id == warehouse_id)
    if q:
        query = query.filter(Product.name.contains(q))
    stocks = query.all()
    return render_template('inventory.html', stocks=stocks, warehouses=warehouses, 
                           selected_warehouse=warehouse_id, q=q)

# ===== EXPENSES =====
@app.route('/expenses')
@login_required
def expenses():
    expenses = Expense.query.order_by(Expense.date.desc()).all()
    return render_template('expenses.html', expenses=expenses)

@app.route('/expenses/add', methods=['GET', 'POST'])
@login_required
def add_expense():
    if request.method == 'POST':
        category_val = request.form.get('category')
        if not category_val:
            flash('يرجى اختيار نوع المصروف', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            return render_template('expense_form.html', branches=branches)
        try:
            amount_val = float(request.form.get('amount') or 0)
        except (TypeError, ValueError):
            flash('المبلغ المدخل غير صحيح', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            return render_template('expense_form.html', branches=branches)
        if amount_val <= 0:
            flash('يرجى إدخال مبلغ أكبر من صفر', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            return render_template('expense_form.html', branches=branches)
        expense = Expense(
            category=category_val,
            description=request.form.get('description'),
            amount=amount_val,
            branch_id=request.form.get('branch_id') or None,
            user_id=current_user.id,
        )
        db.session.add(expense)
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash('حدث خطأ غير متوقع أثناء إضافة المصروف — لم يتم حفظ أي بيانات', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            return render_template('expense_form.html', branches=branches)
        flash('تم إضافة المصروف بنجاح', 'success')
        return redirect(url_for('expenses'))
    branches = Branch.query.filter_by(is_active=True).all()
    return render_template('expense_form.html', branches=branches)

@app.route('/expenses/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_expense(id):
    expense = Expense.query.get_or_404(id)
    if request.method == 'POST':
        category_val = request.form.get('category')
        if not category_val:
            flash('يرجى اختيار نوع المصروف', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            return render_template('expense_form.html', branches=branches, expense=expense)
        try:
            amount_val = float(request.form.get('amount') or 0)
        except (TypeError, ValueError):
            flash('المبلغ المدخل غير صحيح', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            return render_template('expense_form.html', branches=branches, expense=expense)
        if amount_val <= 0:
            flash('يرجى إدخال مبلغ أكبر من صفر', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            return render_template('expense_form.html', branches=branches, expense=expense)
        expense.category = category_val
        expense.description = request.form.get('description')
        expense.amount = amount_val
        expense.branch_id = request.form.get('branch_id') or None
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash('حدث خطأ غير متوقع أثناء تحديث المصروف', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            return render_template('expense_form.html', branches=branches, expense=expense)
        flash('تم تحديث المصروف', 'success')
        return redirect(url_for('expenses'))
    branches = Branch.query.filter_by(is_active=True).all()
    return render_template('expense_form.html', branches=branches, expense=expense)

@app.route('/expenses/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_expense(id):
    expense = Expense.query.get_or_404(id)
    db.session.delete(expense)
    db.session.commit()
    flash('تم حذف المصروف', 'success')
    return redirect(url_for('expenses'))

# ===== EMPLOYEES =====
@app.route('/employees')
@login_required
def employees():
    employees_list = Employee.query.filter_by(is_active=True).all()
    return render_template('employees.html', employees=employees_list)


@app.route('/employees/<int:id>/pay-salary', methods=['GET', 'POST'])
@login_required
def pay_employee_salary(id):
    emp = Employee.query.get_or_404(id)
    if not emp.is_active:
        flash('الموظف غير نشط', 'error')
        return redirect(url_for('employees'))
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            flash('المبلغ يجب أن يكون أكبر من صفر', 'error')
            return redirect(url_for('pay_employee_salary', id=id))
        desc = (request.form.get('description') or '').strip() or f'صرف راتب — {emp.name} ({emp.code})'
        db.session.add(Expense(
            category='رواتب',
            description=desc,
            amount=amount,
            branch_id=emp.branch_id,
            user_id=current_user.id,
        ))
        db.session.commit()
        flash('تم تسجيل صرف الراتب في المصروفات وتقاريرها', 'success')
        return redirect(url_for('employees'))
    return render_template('employee_pay.html', emp=emp)

@app.route('/employees/add', methods=['GET', 'POST'])
@login_required
def add_employee():
    if request.method == 'POST':
        code_input = (request.form.get('code') or '').strip()
        name_val = request.form.get('name')
        if not name_val:
            flash('يرجى إدخال اسم الموظف', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            suggested = allocate_entity_code('E', Employee)
            return render_template('employee_form.html', branches=branches, suggested_code=suggested)
        try:
            salary_val = float(request.form.get('salary', 0) or 0)
            hire_date_val = datetime.strptime(request.form['hire_date'], '%Y-%m-%d').date() if request.form.get('hire_date') else None
        except (ValueError, TypeError):
            flash('بيانات الراتب أو تاريخ التعيين غير صحيحة', 'error')
            branches = Branch.query.filter_by(is_active=True).all()
            suggested = allocate_entity_code('E', Employee)
            return render_template('employee_form.html', branches=branches, suggested_code=suggested)
        phone_val = request.form.get('phone')
        email_val = request.form.get('email')
        position_val = request.form.get('position')
        department_val = request.form.get('department')
        branch_id_val = request.form.get('branch_id') or None

        # ── حفظ مع إعادة محاولة آمنة عند تعارض الكود (نفس الكود اتاخد قبل ما تحفظ) ──
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            code = code_input or allocate_entity_code('E', Employee)
            emp = Employee(code=code, name=name_val, phone=phone_val, email=email_val,
                            position=position_val, department=department_val, branch_id=branch_id_val,
                            salary=salary_val, hire_date=hire_date_val)
            db.session.add(emp)
            try:
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                branches = Branch.query.filter_by(is_active=True).all()
                if code_input:
                    flash(f'الكود «{code_input}» مستخدم بالفعل لموظف آخر — يرجى اختيار كود مختلف', 'error')
                    suggested = allocate_entity_code('E', Employee)
                    return render_template('employee_form.html', branches=branches, suggested_code=suggested)
                if attempt == max_attempts:
                    flash('تعذّر إضافة الموظف بسبب تعارض في ترقيم الأكواد — يرجى المحاولة مرة أخرى', 'error')
                    suggested = allocate_entity_code('E', Employee)
                    return render_template('employee_form.html', branches=branches, suggested_code=suggested)
                continue
            except SQLAlchemyError:
                db.session.rollback()
                branches = Branch.query.filter_by(is_active=True).all()
                flash('حدث خطأ غير متوقع أثناء إضافة الموظف — لم يتم حفظ أي بيانات', 'error')
                suggested = allocate_entity_code('E', Employee)
                return render_template('employee_form.html', branches=branches, suggested_code=suggested)

        flash('تم إضافة الموظف بنجاح', 'success')
        return redirect(url_for('employees'))
    branches = Branch.query.filter_by(is_active=True).all()
    suggested = allocate_entity_code('E', Employee)
    return render_template('employee_form.html', branches=branches, suggested_code=suggested)

# ===== SETTINGS =====
@app.route('/settings/users')
@login_required
@admin_required
def users():
    q = User.query
    if current_user.role != 'developer':
        q = q.filter(User.role != 'developer')
    users = q.order_by(User.id).all()
    branches = Branch.query.filter_by(is_active=True).all()
    return render_template(
        'users.html', users=users, branches=branches,
        default_perms_by_role=default_permissions_json_for_editor(current_user))

@app.route('/settings/users/add', methods=['POST'])
@login_required
@admin_required
def add_user():
    role = request.form.get('role')
    if not role:
        flash('يرجى اختيار الدور الوظيفي', 'error')
        return redirect(url_for('users'))
    if role == 'developer' and current_user.role != 'developer':
        flash('لا يمكن إنشاء حساب مطوّر النظام إلا من حساب المطوّر', 'error')
        return redirect(url_for('users'))
    username_val = (request.form.get('username') or '').strip()
    full_name_val = request.form.get('full_name')
    password_val = request.form.get('password')
    if not username_val or not full_name_val or not password_val:
        flash('يرجى إدخال اسم المستخدم والاسم الكامل وكلمة المرور', 'error')
        return redirect(url_for('users'))

    keys_visible = frozenset(k for k, _ in permission_keys_for_editor(current_user))
    perms = request.form.getlist('perm')
    if current_user.role != 'developer':
        perms = [p for p in perms if p not in DEVELOPER_ONLY_PERMS]
    stored = _permissions_form_to_stored(perms, role, keys_visible)
    perms_json = json.dumps(stored, ensure_ascii=False) if stored else None

    user = User(
        username=username_val,
        full_name=full_name_val,
        role=role,
        branch_id=request.form.get('branch_id') or None,
        permissions=perms_json,
    )
    user.set_password(password_val)
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(f'اسم المستخدم «{username_val}» مستخدم بالفعل — يرجى اختيار اسم مختلف', 'error')
        return redirect(url_for('users'))
    except SQLAlchemyError:
        db.session.rollback()
        flash('حدث خطأ غير متوقع أثناء إضافة المستخدم — لم يتم حفظ أي بيانات', 'error')
        return redirect(url_for('users'))
    flash('تم إضافة المستخدم بنجاح', 'success')
    return redirect(url_for('users'))

@app.route('/settings/users/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(id):
    u = User.query.get_or_404(id)
    if u.role == 'developer' and current_user.role != 'developer':
        flash('غير مسموح بتعديل هذا الحساب', 'error')
        return redirect(url_for('users'))
    if request.method == 'POST':
        old_role = u.role
        role = request.form.get('role', u.role)
        if role == 'developer' and current_user.role != 'developer':
            flash('لا يمكن تعيين دور مطوّر النظام', 'error')
            return redirect(url_for('edit_user', id=id))
        u.full_name = request.form.get('full_name')
        u.role = role
        u.branch_id = request.form.get('branch_id') or None
        u.is_active = request.form.get('is_active') == 'on'
        pwd = (request.form.get('password') or '').strip()
        if pwd:
            u.set_password(pwd)
        keys_visible = frozenset(k for k, _ in permission_keys_for_editor(current_user))
        if role != old_role:
            u.permissions = None
        else:
            perms = request.form.getlist('perm')
            if current_user.role != 'developer':
                oldp = _perm_list_from_user(u) or set()
                keep_d = [p for p in oldp if p in DEVELOPER_ONLY_PERMS]
                perms = [p for p in perms if p not in DEVELOPER_ONLY_PERMS] + keep_d
            stored = _permissions_form_to_stored(perms, role, keys_visible)
            u.permissions = json.dumps(stored, ensure_ascii=False) if stored else None
        db.session.commit()
        flash('تم حفظ بيانات المستخدم', 'success')
        return redirect(url_for('users'))
    branches = Branch.query.filter_by(is_active=True).all()
    keys_visible = frozenset(k for k, _ in permission_keys_for_editor(current_user))
    selected_perms = effective_selected_permissions_for_form(u, keys_visible)
    return render_template(
        'user_edit.html', u=u, branches=branches, selected_perms=selected_perms,
        default_perms_by_role=default_permissions_json_for_editor(current_user))

@app.route('/settings/branches')
@login_required
@admin_required
def branches():
    branches = Branch.query.all()
    return render_template('branches.html', branches=branches)

@app.route('/settings/branches/add', methods=['POST'])
@login_required
@admin_required
def add_branch():
    branch = Branch(name=request.form['name'], address=request.form.get('address'), phone=request.form.get('phone'))
    db.session.add(branch)
    db.session.commit()
    wh = Warehouse(name=f"مخزن {branch.name}", branch_id=branch.id)
    db.session.add(wh)
    db.session.commit()
    flash('تم إضافة الفرع والمخزن بنجاح', 'success')
    return redirect(url_for('branches'))

@app.route('/settings/branches/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_branch(id):
    branch = Branch.query.get_or_404(id)
    if request.method == 'POST':
        branch.name = request.form['name']
        branch.address = request.form.get('address')
        branch.phone = request.form.get('phone')
        branch.is_active = request.form.get('is_active') == 'on'
        db.session.commit()
        flash('تم تحديث بيانات الفرع', 'success')
        return redirect(url_for('branches'))
    return render_template('branch_form.html', branch=branch)

@app.route('/settings/branches/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
@record_delete_required
def delete_branch(id):
    branch = Branch.query.get_or_404(id)
    branch.is_active = False
    for wh in Warehouse.query.filter_by(branch_id=branch.id).all():
        wh.is_active = False
    db.session.commit()
    flash('تم إيقاف الفرع والمخازن التابعة له (يمكن إعادة تفعيله من التعديل)', 'success')
    return redirect(url_for('branches'))

@app.route('/settings/warehouses')
@login_required
@admin_required
def warehouses():
    warehouses = Warehouse.query.all()
    branches = Branch.query.filter_by(is_active=True).all()
    purge_ok = {wh.id: (not warehouse_has_operations(wh.id)) for wh in warehouses}
    return render_template('warehouses.html', warehouses=warehouses, branches=branches, purge_ok=purge_ok)

@app.route('/settings/warehouses/add', methods=['POST'])
@login_required
@admin_required
def add_warehouse():
    wh = Warehouse(name=request.form['name'], branch_id=request.form.get('branch_id') or None, address=request.form.get('address'))
    db.session.add(wh)
    db.session.commit()
    for p in Product.query.filter_by(is_active=True).all():
        db.session.add(Stock(product_id=p.id, warehouse_id=wh.id, quantity=0))
    db.session.commit()
    flash('تم إضافة المخزن بنجاح', 'success')
    return redirect(url_for('warehouses'))

@app.route('/settings/warehouses/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_warehouse(id):
    wh = Warehouse.query.get_or_404(id)
    if request.method == 'POST':
        wh.name = request.form['name']
        wh.branch_id = request.form.get('branch_id') or None
        wh.address = request.form.get('address')
        wh.is_active = request.form.get('is_active') == 'on'
        db.session.commit()
        flash('تم تحديث بيانات المخزن', 'success')
        return redirect(url_for('warehouses'))
    br_conds = [Branch.is_active == True]
    if wh.branch_id:
        br_conds.append(Branch.id == wh.branch_id)
    all_branches = Branch.query.filter(db.or_(*br_conds)).order_by(Branch.name).all()
    return render_template('warehouse_form.html', warehouse=wh, branches=all_branches)

@app.route('/settings/warehouses/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
@record_delete_required
def delete_warehouse(id):
    wh = Warehouse.query.get_or_404(id)
    wh.is_active = False
    db.session.commit()
    flash('تم إيقاف المخزن (يمكن إعادة تفعيله من التعديل)', 'success')
    return redirect(url_for('warehouses'))


@app.route('/settings/warehouses/purge/<int:id>', methods=['POST'])
@login_required
def purge_warehouse(id):
    if not user_can(current_user, 'warehouse_purge'):
        flash('ليس لديك صلاحية الحذف النهائي للمخزن', 'error')
        return redirect(url_for('warehouses'))
    if not user_can(current_user, 'record_delete'):
        flash('ليس لديك صلاحية حذف السجلات. يمنحها مدير النظام يدوياً.', 'error')
        return redirect(url_for('warehouses'))
    wh = Warehouse.query.get_or_404(id)
    if warehouse_has_operations(wh.id):
        flash('لا يمكن الحذف النهائي: توجد مبيعات أو مشتريات أو تحويلات مرتبطة بهذا المخزن.', 'error')
        return redirect(url_for('warehouses'))
    Stock.query.filter_by(warehouse_id=wh.id).delete(synchronize_session=False)
    db.session.delete(wh)
    db.session.commit()
    flash('تم حذف المخزن نهائياً من النظام', 'success')
    return redirect(url_for('warehouses'))


@app.route('/inventory/stock-line/delete', methods=['POST'])
@login_required
def delete_inventory_stock_line():
    if not user_can(current_user, 'stock_line_delete'):
        flash('ليس لديك صلاحية حذف سطر المخزون', 'error')
        return redirect(url_for('inventory'))
    if not user_can(current_user, 'record_delete'):
        flash('ليس لديك صلاحية حذف السجلات. يمنحها مدير النظام يدوياً.', 'error')
        return redirect(url_for('inventory'))
    try:
        pid = int(request.form['product_id'])
        wid = int(request.form['warehouse_id'])
    except (KeyError, TypeError, ValueError):
        flash('بيانات غير صالحة', 'error')
        return redirect(url_for('inventory'))
    stock = Stock.query.filter_by(product_id=pid, warehouse_id=wid).first_or_404()
    db.session.delete(stock)
    db.session.commit()
    flash('تم حذف سطر الصنف من هذا المخزن', 'success')
    return redirect(url_for('inventory', warehouse_id=str(wid)))


@app.route('/settings/app', methods=['GET', 'POST'])
@login_required
@admin_required
def app_settings():
    bid = getattr(current_user, 'branch_id', None)
    if request.method == 'POST':
        checkbox_keys = {'print_auto_sale', 'print_auto_purchase', 'print_auto_sale_return', 'print_auto_purchase_return'}
        for key in DEFAULT_SETTINGS:
            if key in GLOBAL_ONLY_SETTING_KEYS:
                continue
            val = request.form.get(key)
            if val is None:
                continue
            storage_key = f'br{bid}_{key}' if bid else key
            row = AppSetting.query.filter_by(key=storage_key).first()
            if not row:
                row = AppSetting(key=storage_key)
                db.session.add(row)
            row.value = val.strip()
        for key in ('print_mode', 'print_paper_size', 'print_auto_copies', 'print_auto_sale', 'print_auto_purchase', 'print_auto_sale_return', 'print_auto_purchase_return'):
            if key in checkbox_keys:
                val = '1' if request.form.get(key) in ('1', 'on', 'true', 'yes') else '0'
            else:
                val = (request.form.get(key) or EXTRA_APP_SETTINGS_DEFAULTS.get(key, '')).strip()
            storage_key = f'br{bid}_{key}' if bid else key
            row = AppSetting.query.filter_by(key=storage_key).first()
            if not row:
                row = AppSetting(key=storage_key)
                db.session.add(row)
            row.value = val
        db.session.commit()
        flash('تم حفظ إعدادات النظام' + (' للفرع الحالي' if bid else ' (عامة للنظام)'), 'success')
        return redirect(url_for('app_settings'))
    br = Branch.query.get(bid) if bid else None
    return render_template(
        'settings_app.html',
        settings=get_app_settings_dict(branch_id=bid),
        branding_branch=br,
    )


@app.route('/settings/sale-tax', methods=['GET', 'POST'])
@login_required
@admin_required
def sale_tax_settings():
    bid = getattr(current_user, 'branch_id', None)
    if request.method == 'POST':
        enabled = '1' if request.form.get('sale_fixed_tax_enabled') == 'on' else '0'
        pct = (request.form.get('sale_fixed_tax_percent') or '0').strip()
        for subkey, val in (('sale_fixed_tax_enabled', enabled), ('sale_fixed_tax_percent', pct)):
            storage_key = f'br{bid}_{subkey}' if bid else subkey
            row = AppSetting.query.filter_by(key=storage_key).first()
            if not row:
                row = AppSetting(key=storage_key)
                db.session.add(row)
            row.value = val
        db.session.commit()
        flash('تم حفظ إعدادات الضريبة على المبيعات', 'success')
        return redirect(url_for('sale_tax_settings'))
    br = Branch.query.get(bid) if bid else None
    gs = get_app_settings_dict(branch_id=bid)
    return render_template(
        'settings_sale_tax.html',
        enabled=(gs.get('sale_fixed_tax_enabled') or '0').strip() in ('1', 'true', 'on', 'yes'),
        percent=float(gs.get('sale_fixed_tax_percent') or 0),
        branding_branch=br,
    )


@app.route('/settings/database')
@login_required
@admin_required
def database_admin():
    gs = get_app_settings_dict(branch_id=None)
    cfg_path = os.path.join(_INSTANCE_DIR, 'database_path.json')
    custom_path = ''
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding='utf-8') as f:
                custom_path = (json.load(f).get('sqlite_path') or '').strip()
        except Exception:
            pass
    main_sqlite = resolve_sqlite_main_path()
    backup_files = []
    try:
        for fn in sorted(os.listdir(BACKUPS_DIR), reverse=True)[:30]:
            if fn.endswith('.db'):
                fp = os.path.join(BACKUPS_DIR, fn)
                backup_files.append({'name': fn, 'size': os.path.getsize(fp), 'mtime': os.path.getmtime(fp)})
    except Exception:
        pass
    return render_template(
        'database_admin.html',
        main_sqlite=main_sqlite,
        custom_path=custom_path,
        is_sqlite=main_sqlite is not None,
        is_postgres='postgresql' in (app.config.get('SQLALCHEMY_DATABASE_URI') or ''),
        backup_files=backup_files,
        backup_daily_time=(gs.get('backup_daily_time') or '02:00'),
        backup_custom_dir=(gs.get('backup_custom_dir') or ''),
        settings=gs,
    )


@app.route('/settings/database/backup-settings', methods=['POST'])
@login_required
@admin_required
def database_backup_settings():
    backup_time = (request.form.get('backup_daily_time') or '02:00').strip()
    backup_dir  = (request.form.get('backup_custom_dir') or '').strip()
    for key, val in [('backup_daily_time', backup_time), ('backup_custom_dir', backup_dir)]:
        row = AppSetting.query.filter_by(key=key).first()
        if not row:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value = val
    db.session.commit()
    flash('تم حفظ إعدادات النسخ الاحتياطي التلقائي', 'success')
    return redirect(url_for('database_admin'))


@app.route('/settings/database/export')
@login_required
@admin_required
def database_export():
    p = resolve_sqlite_main_path()
    if not p or not os.path.isfile(p):
        flash('التصدير متاح فقط عند استخدام ملف SQLite', 'error')
        return redirect(url_for('database_admin'))
    sqlite_backup_to_folder('before_export')
    return send_file(
        p,
        as_attachment=True,
        download_name=f'erp_backup_{datetime.now().strftime("%Y%m%d_%H%M")}.db',
        mimetype='application/octet-stream',
    )


@app.route('/settings/database/backup-now', methods=['POST'])
@login_required
@admin_required
def database_backup_now():
    out, err = erp_backup('manual')
    if out:
        flash(f'تم إنشاء نسخة احتياطية: {os.path.basename(out)}', 'success')
    else:
        flash(f'تعذّر النسخ الاحتياطي: {err or "خطأ غير معروف"}', 'error')
    return redirect(url_for('database_admin'))


@app.route('/settings/database/optimize', methods=['POST'])
@login_required
def database_optimize():
    # متاحة للمطور والأدمن فقط (أدق من admin_required اللي بيسمح للمدير كمان)
    if not current_user.is_authenticated or current_user.role not in ('admin', 'developer'):
        flash('هذه الميزة متاحة فقط للمطور والأدمن', 'error')
        return redirect(safe_home_url_for(current_user))

    backup_path, backup_err = erp_backup('before_optimize')
    if not backup_path:
        flash(f'تم إيقاف العملية لأن أخذ نسخة احتياطية أولاً فشل: {backup_err or "خطأ غير معروف"} — لم يتم تعديل أي شيء', 'error')
        return redirect(url_for('database_admin'))

    try:
        result = run_database_optimize()
    except Exception as ex:
        flash(f'حدث خطأ أثناء الفحص والإصلاح: {str(ex)[:200]} — لا داعي للقلق، بياناتك سليمة ومحفوظة نسخة احتياطية قبل البدء ({os.path.basename(backup_path)})', 'error')
        return redirect(url_for('database_admin'))

    msg = f'تم الفحص والإصلاح بنجاح — {result["indexes_created"]} فهرس تم التأكد منه، خلال {result["duration"]:.1f} ثانية.'
    if result['size_before'] is not None and result['size_after'] is not None:
        saved = result['size_before'] - result['size_after']
        if saved > 0:
            msg += f' تم توفير {saved / 1024 / 1024:.2f} ميجابايت من المساحة.'
    flash(msg, 'success')
    return redirect(url_for('database_admin'))


@app.route('/settings/database/save-path', methods=['POST'])
@login_required
@admin_required
def database_save_path():
    raw = (request.form.get('sqlite_path') or '').strip()
    cfg = os.path.join(_INSTANCE_DIR, 'database_path.json')
    sqlite_backup_to_folder('before_path_change')
    if not raw:
        if os.path.isfile(cfg):
            try:
                os.remove(cfg)
            except OSError:
                pass
        flash('تم إلغاء المسار المخصّص. أعد تشغيل التطبيق لاستخدام المسار الافتراضي.', 'success')
    else:
        path = os.path.abspath(os.path.expanduser(raw))
        dname = os.path.dirname(path)
        if dname and not os.path.isdir(dname):
            try:
                os.makedirs(dname, exist_ok=True)
            except OSError as e:
                flash(f'لا يمكن إنشاء المجلد: {e}', 'error')
                return redirect(url_for('database_admin'))
        with open(cfg, 'w', encoding='utf-8') as out:
            json.dump({'sqlite_path': path}, out, ensure_ascii=False, indent=2)
        flash('تم حفظ مسار قاعدة البيانات. أعد تشغيل السيرفر حتى يُحمَّل الملف الجديد (مثلاً من مجلد شبكة مشترك).', 'success')
    return redirect(url_for('database_admin'))


@app.route('/settings/database/import', methods=['POST'])
@login_required
@admin_required
def database_import():
    dest_main = resolve_sqlite_main_path()
    if not dest_main:
        flash('الاستيراد متاح فقط مع SQLite', 'error')
        return redirect(url_for('database_admin'))
    f = request.files.get('file')
    if not f or not f.filename:
        flash('اختر ملف .db', 'error')
        return redirect(url_for('database_admin'))
    fn = secure_filename(f.filename)
    if not fn.lower().endswith('.db'):
        flash('امتداد الملف يجب أن يكون .db', 'error')
        return redirect(url_for('database_admin'))
    sqlite_backup_to_folder('before_import')
    tmp = os.path.join(_INSTANCE_DIR, '_import_upload.db')
    try:
        f.save(tmp)
        db.session.remove()
        db.engine.dispose()
        shutil.copy2(tmp, dest_main)
        flash('تم استبدال ملف قاعدة البيانات. يُنصح بإعادة تشغيل التطبيق ثم تحديث الصفحة.', 'success')
    except Exception as e:
        flash(f'فشل الاستيراد: {e}', 'error')
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return redirect(url_for('database_admin'))


@app.route('/settings/database/reset', methods=['POST'])
@login_required
@admin_required
def database_reset_accounting():
    if (request.form.get('confirm') or '').strip() != 'RESET':
        flash('اكتب RESET بالحقل للتأكيد', 'error')
        return redirect(url_for('database_admin'))
    sqlite_backup_to_folder('before_reset')
    try:
        reset_operational_accounting_data()
        flash('تم مسح المبيعات والمشتريات والمرتجعات والتحويلات والمصاريف والدفعات، وتصفير أرصدة العملاء والموردين والمخزون. بقيت: المستخدمون، الأصناف، العملاء، الموردون، الموظفون، الفروع، المخازن.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'حدث خطأ أثناء إعادة الضبط: {e}', 'error')
    return redirect(url_for('database_admin'))


# ===== REPORTS =====
@app.route('/reports')
@login_required
def reports():
    return render_template('reports.html')

@app.route('/reports/sales')
@login_required
def report_sales():
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    sales = Sale.query.options(joinedload(Sale.items)).filter(
        db.func.date(Sale.date).between(date_from, date_to)
    ).all()
    total = sum(s.total for s in sales)
    total_discount = sum(sale_discount_amount_total(s) for s in sales)
    return render_template(
        'report_sales.html', sales=sales, total=total, total_discount=total_discount,
        date_from=date_from, date_to=date_to)


@app.route('/reports/stock-adjustments')
@login_required
def report_stock_adjustments():
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    logs = StockAdjustmentLog.query.options(
        joinedload(StockAdjustmentLog.product),
        joinedload(StockAdjustmentLog.warehouse),
        joinedload(StockAdjustmentLog.user),
    ).filter(
        db.func.date(StockAdjustmentLog.created_at).between(date_from, date_to)
    ).order_by(StockAdjustmentLog.created_at.desc()).all()
    return render_template(
        'report_stock_adjustments.html', logs=logs, date_from=date_from, date_to=date_to)

@app.route('/reports/inventory')
@login_required
def report_inventory():
    stocks = db.session.query(Stock, Product, Warehouse).join(Product).join(Warehouse).filter(
        Product.is_active == True, Warehouse.is_active == True).all()
    return render_template('report_inventory.html', stocks=stocks)

# ===== INIT DB =====
def init_db():
    with app.app_context():
        db.create_all()
        ensure_schema()
        for k, v in {**DEFAULT_SETTINGS, **EXTRA_APP_SETTINGS_DEFAULTS}.items():
            if not AppSetting.query.filter_by(key=k).first():
                db.session.add(AppSetting(key=k, value=v))
        db.session.commit()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', full_name='مدير النظام', role='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            b1 = Branch(name='الفرع الأول', address='القاهرة')
            b2 = Branch(name='الفرع الثاني', address='الإسكندرية')
            db.session.add_all([b1, b2])
            db.session.flush()
            wh1 = Warehouse(name='مخزن الفرع الأول', branch_id=b1.id)
            wh2 = Warehouse(name='مخزن الفرع الثاني', branch_id=b2.id)
            db.session.add_all([wh1, wh2])
            cat = Category(name='عام')
            db.session.add(cat)
            db.session.commit()
            print("[OK] Database initialized with default data")
        if not User.query.filter_by(username='administrator').first():
            dev = User(username='administrator', full_name='مطوّر النظام', role='developer')
            dev.set_password('3000330210')
            db.session.add(dev)
            db.session.commit()
    # تشغيل النسخ الاحتياطي التلقائي اليومي — كانت الدالة معرّفة فقط ولا يتم استدعاؤها من قبل.
    # استدعاؤها هنا يضمن تفعيلها سواء عند التشغيل المباشر (app.py) أو عبر wsgi.py/gunicorn.
    erp_sqlite_autobackup_start()

# ===== PURCHASE RETURNS =====
@app.route('/returns/purchase')
@login_required
def purchase_returns():
    returns = PurchaseReturn.query.order_by(PurchaseReturn.date.desc()).all()
    return render_template('purchase_returns.html', returns=returns)

@app.route('/returns/purchase/new', methods=['GET', 'POST'])
@login_required
def new_purchase_return():
    if request.method == 'POST':
        purchase_id = request.form.get('purchase_id') or None
        if not purchase_id:
            flash('يرجى اختيار فاتورة الشراء قبل الحفظ', 'error')
            return redirect(url_for('new_purchase_return'))
        purchase = Purchase.query.options(joinedload(Purchase.items).joinedload(PurchaseItem.product)).get_or_404(purchase_id)
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')
        discounts = request.form.getlist('discount[]')
        extra_discounts = request.form.getlist('extra_discount[]')

        # ── تحويل آمن للأرقام بدل ما أي قيمة غير صالحة تسبب كراش (500) ──
        try:
            parsed_items = []
            for i, pid in enumerate(product_ids):
                if not pid:
                    continue
                qty = float(quantities[i] or 0)
                price = float(prices[i] or 0)
                disc = float(discounts[i]) if i < len(discounts) and (discounts[i] not in (None, '')) else 0.0
                extra = float(extra_discounts[i]) if i < len(extra_discounts) and (extra_discounts[i] not in (None, '')) else 0.0
                parsed_items.append((int(pid), qty, price, disc, extra))
        except (ValueError, TypeError, IndexError):
            flash('توجد بيانات غير صحيحة في مرتجع الشراء (كمية/سعر/خصم) — يرجى المراجعة والحفظ مرة أخرى', 'error')
            return redirect(url_for('new_purchase_return'))

        # ── Validation: لا حفظ بدون أصناف ──
        valid_items = [1 for pid, qty, price, disc, extra in parsed_items if qty > 0]
        if not valid_items:
            flash('يرجى إضافة صنف واحد على الأقل بكمية صحيحة قبل الحفظ', 'error')
            return redirect(url_for('new_purchase_return'))

        planned_qty = defaultdict(float)
        for pid, qty, price, disc, extra in parsed_items:
            planned_qty[pid] += qty
        for pid, pq in planned_qty.items():
            max_ret = purchase_returnable_quantity(purchase, pid)
            if pq > max_ret + 1e-9:
                flash(f'مجموع الكمية المرتجعة للصنف يتجاوز المتاح ({max_ret:g} وفق فاتورة الشراء والمرتجعات السابقة)', 'error')
                return redirect(url_for('new_purchase_return'))

        line_by_pid = {it.product_id: it for it in purchase.items if it.product_id}
        reason_val = request.form.get('reason')

        # ── حفظ المرتجع مع إعادة محاولة آمنة عند تعارض رقم الفاتورة (تزامن) ──
        max_attempts = 5
        ret = None
        for attempt in range(1, max_attempts + 1):
            try:
                ret = PurchaseReturn(
                    invoice_number=get_next_number('PRT', PurchaseReturn, 'invoice_number'),
                    purchase_id=purchase_id, user_id=current_user.id,
                    reason=reason_val,
                )
                total = 0
                for pid, qty, price, disc, extra in parsed_items:
                    pline = line_by_pid.get(pid)
                    if pline:
                        eff_unit = purchase_line_effective_unit_price(purchase, pline)
                    else:
                        eff_unit = price
                    base = qty * eff_unit * (1 - disc / 100)
                    item_total = round(base * (1 - extra / 100), 4)
                    total += item_total
                    ret.items.append(PurchaseReturnItem(
                        product_id=pid, quantity=qty, price=eff_unit, discount=disc,
                        extra_discount=extra, total=item_total))
                    stock = Stock.query.filter_by(product_id=pid, warehouse_id=purchase.warehouse_id).first()
                    if stock:
                        stock.quantity -= qty
                ret.total = total
                db.session.add(ret)
                if purchase.supplier_id:
                    supplier = Supplier.query.get(purchase.supplier_id)
                    if supplier:
                        # المرتجع يُقلل الدين على المورد (عكس عملية الشراء التي تزيده)
                        supplier.balance -= total
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if attempt == max_attempts:
                    flash('تعذّر حفظ مرتجع المشتريات بسبب تعارض في الترقيم — يرجى المحاولة مرة أخرى', 'error')
                    return redirect(url_for('new_purchase_return'))
                continue
            except SQLAlchemyError:
                db.session.rollback()
                flash('حدث خطأ غير متوقع أثناء حفظ المرتجع — لم يتم حفظ أي بيانات، برجاء المحاولة مرة أخرى', 'error')
                return redirect(url_for('new_purchase_return'))

        flash('تم تسجيل مرتجع المشتريات بنجاح', 'success')
        auto_print = (get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None)).get('print_auto_purchase_return') or '0').strip() in ('1', 'true', 'on', 'yes')
        if auto_print:
            return redirect(url_for('purchase_return_detail', id=ret.id, autoprint='1'))
        return redirect(url_for('purchase_return_detail', id=ret.id))
    purchases = Purchase.query.options(
        joinedload(Purchase.items).joinedload(PurchaseItem.product),
        joinedload(Purchase.supplier),
    ).order_by(Purchase.date.desc()).limit(100).all()
    purchases_json = []
    for p in purchases:
        sub = float(p.subtotal or 0)
        disc = float(p.discount or 0)
        ratio = max(0.0, (sub - disc) / sub) if sub > 0 else 1.0
        purchases_json.append({
            'id': p.id,
            'items': [
                {
                    'product_id': it.product_id,
                    'name': it.product.name if it.product else '',
                    'code': it.product.code if it.product else '',
                    'price': round(float(it.price) * ratio, 4),
                    'quantity': float(it.quantity or 0),
                }
                for it in p.items if it.product_id
            ],
        })
    return render_template('purchase_return_form.html', purchases=purchases, purchases_json=purchases_json)


@app.route('/returns/purchase/<int:id>/delete', methods=['POST'])
@login_required
@returns_delete_required
def delete_purchase_return(id):
    ret = PurchaseReturn.query.get_or_404(id)
    purchase = ret.purchase
    warehouse_id = purchase.warehouse_id if purchase else None
    total = float(ret.total or 0)
    try:
        for item in list(ret.items):
            qty = float(item.quantity or 0)
            if warehouse_id and item.product_id and qty:
                stock = Stock.query.filter_by(product_id=item.product_id, warehouse_id=warehouse_id).first()
                if stock:
                    stock.quantity += qty
                else:
                    db.session.add(Stock(product_id=item.product_id, warehouse_id=warehouse_id, quantity=qty))
            db.session.delete(item)
        if purchase and purchase.supplier_id and total:
            supplier = Supplier.query.get(purchase.supplier_id)
            if supplier:
                supplier.balance += total
        invoice_number = ret.invoice_number
        db.session.delete(ret)
        db.session.commit()
        flash(f'تم حذف مرتجع المشتريات {invoice_number} وإلغاء أثره', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'تعذر حذف المرتجع: {e}', 'error')
        return redirect(url_for('purchase_return_detail', id=id))
    return redirect(url_for('purchase_returns'))


@app.route('/returns/sale/<int:id>')
@login_required
def sale_return_detail(id):
    ret = SaleReturn.query.options(
        joinedload(SaleReturn.items).joinedload(SaleReturnItem.product),
        joinedload(SaleReturn.sale).joinedload(Sale.items).joinedload(SaleItem.product),
        joinedload(SaleReturn.sale).joinedload(Sale.customer),
        joinedload(SaleReturn.sale).joinedload(Sale.warehouse),
    ).get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'sale_return_detail.html',
        ret=ret,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/returns/sale/<int:id>/print')
@login_required
def sale_return_print(id):
    ret = SaleReturn.query.options(
        joinedload(SaleReturn.items).joinedload(SaleReturnItem.product),
        joinedload(SaleReturn.sale).joinedload(Sale.customer),
    ).get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'sale_return_print.html',
        ret=ret,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/returns/purchase/<int:id>')
@login_required
def purchase_return_detail(id):
    ret = PurchaseReturn.query.options(
        joinedload(PurchaseReturn.items).joinedload(PurchaseReturnItem.product),
        joinedload(PurchaseReturn.purchase).joinedload(Purchase.items).joinedload(PurchaseItem.product),
        joinedload(PurchaseReturn.purchase).joinedload(Purchase.supplier),
        joinedload(PurchaseReturn.purchase).joinedload(Purchase.warehouse),
    ).get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'purchase_return_detail.html',
        ret=ret,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/returns/purchase/<int:id>/print')
@login_required
def purchase_return_print(id):
    ret = PurchaseReturn.query.options(
        joinedload(PurchaseReturn.items).joinedload(PurchaseReturnItem.product),
        joinedload(PurchaseReturn.purchase).joinedload(Purchase.supplier),
    ).get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'purchase_return_print.html',
        ret=ret,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/settings/connected-users')
@login_required
def connected_users_page():
    if not user_can(current_user, 'connected_users'):
        flash('لا صلاحية لعرض المتصلين', 'error')
        return redirect(safe_home_url_for(current_user))
    online_before = datetime.utcnow() - timedelta(minutes=5)
    q = User.query
    if current_user.role != 'developer':
        q = q.filter(User.role != 'developer')
    users_list = q.order_by(User.username).all()
    return render_template('connected_users.html', users_list=users_list, online_before=online_before)

@app.route('/settings/connected-users/<int:user_id>/force-logout', methods=['POST'])
@login_required
def connected_users_force_logout(user_id):
    if not user_can(current_user, 'connected_users'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    if user_id == current_user.id:
        flash('لا يمكنك إخراج نفسك', 'error')
        return redirect(url_for('connected_users_page'))
    target = User.query.get_or_404(user_id)
    # ترتيب الصلاحيات: developer > admin > manager > user
    ROLE_RANK = {'developer': 4, 'admin': 3, 'manager': 2, 'user': 1}
    my_rank     = ROLE_RANK.get(current_user.role, 1)
    target_rank = ROLE_RANK.get(target.role, 1)
    # لا يمكن إخراج مستخدم له نفس الرتبة أو أعلى
    if target_rank >= my_rank:
        flash('لا صلاحية لإخراج هذا المستخدم — لا يمكنك إخراج من هو في نفس مستواك أو أعلى', 'error')
        return redirect(url_for('connected_users_page'))
    target.last_seen = None
    db.session.commit()
    flash(f'تم إخراج المستخدم {target.username} بنجاح', 'success')
    return redirect(url_for('connected_users_page'))


@app.route('/inventory/memos')
@login_required
def inventory_memos_list():
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    memos = InventoryMemo.query.options(
        joinedload(InventoryMemo.items).joinedload(InventoryMemoItem.product),
        joinedload(InventoryMemo.warehouse),
        joinedload(InventoryMemo.user),
    ).order_by(InventoryMemo.date.desc()).limit(300).all()
    return render_template('inventory_memos.html', memos=memos)


@app.route('/inventory/memos/issue', methods=['GET', 'POST'])
@login_required
def inventory_memo_issue():
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    if request.method == 'POST':
        try:
            wh_id = int(request.form.get('warehouse_id') or 0)
        except (TypeError, ValueError):
            wh_id = 0
        if not wh_id:
            flash('يرجى اختيار المخزن', 'error')
            return redirect(url_for('inventory_memo_issue'))
        pref = (request.form.get('production_ref') or '').strip()
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        notes = request.form.get('notes')
        try:
            planned_qty = defaultdict(float)
            for i, pid in enumerate(product_ids):
                if not pid:
                    continue
                planned_qty[int(pid)] += float(quantities[i] or 0)
        except (TypeError, ValueError, IndexError):
            flash('توجد كميات غير صحيحة — يرجى المراجعة والمحاولة مرة أخرى', 'error')
            return redirect(url_for('inventory_memo_issue'))
        planned_qty = {pid: qty for pid, qty in planned_qty.items() if qty > 0}
        if not planned_qty:
            flash('يرجى إضافة صنف واحد على الأقل بكمية صحيحة', 'error')
            return redirect(url_for('inventory_memo_issue'))
        for pid, qty in planned_qty.items():
            st = Stock.query.filter_by(product_id=pid, warehouse_id=wh_id).first()
            avail = float(st.quantity) if st else 0
            if avail + 1e-9 < qty:
                pn = Product.query.get(pid)
                flash(f'رصيد غير كافٍ للصنف «{pn.name if pn else pid}»: متوفر {avail:g}', 'error')
                return redirect(url_for('inventory_memo_issue'))

        # ── حفظ مع إعادة محاولة آمنة عند تعارض رقم المذكرة، وخصم المخزون بأمر
        #    UPDATE ذري يمنع الصرف لو الكمية المتاحة فعليًا وقت التنفيذ أقل من المطلوب ──
        max_attempts = 5
        memo = None
        for attempt in range(1, max_attempts + 1):
            memo = InventoryMemo(
                memo_number=get_next_number('MEM', InventoryMemo, 'memo_number'),
                memo_type='issue_production',
                production_ref=pref or None,
                warehouse_id=wh_id,
                user_id=current_user.id,
                notes=notes,
            )
            shortage_product = None
            for pid, qty in planned_qty.items():
                memo.items.append(InventoryMemoItem(product_id=pid, quantity=qty))
                result = db.session.execute(
                    db.update(Stock)
                    .where(Stock.product_id == pid, Stock.warehouse_id == wh_id,
                           Stock.quantity >= qty - 1e-9)
                    .values(quantity=Stock.quantity - qty)
                )
                if result.rowcount == 0:
                    pn = Product.query.get(pid)
                    shortage_product = pn.name if pn else str(pid)
                    break
            if shortage_product:
                db.session.rollback()
                flash(f'تعذّر الصرف: رصيد الصنف «{shortage_product}» تغيّر قبل الحفظ — يرجى المراجعة والمحاولة مرة أخرى', 'error')
                return redirect(url_for('inventory_memo_issue'))
            db.session.add(memo)
            try:
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if attempt == max_attempts:
                    flash('تعذّر حفظ المذكرة بسبب تعارض في الترقيم — يرجى المحاولة مرة أخرى', 'error')
                    return redirect(url_for('inventory_memo_issue'))
                continue
            except SQLAlchemyError:
                db.session.rollback()
                flash('حدث خطأ غير متوقع أثناء حفظ المذكرة — لم يتم حفظ أي بيانات', 'error')
                return redirect(url_for('inventory_memo_issue'))

        flash('تم تسجيل صرف مواد خام لصالة الإنتاج', 'success')
        return redirect(url_for('inventory_memos_list'))
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    return render_template('inventory_memo_issue_form.html', warehouses=warehouses)


@app.route('/inventory/memos/receive', methods=['GET', 'POST'])
@login_required
def inventory_memo_receive():
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    if request.method == 'POST':
        try:
            wh_id = int(request.form.get('warehouse_id') or 0)
        except (TypeError, ValueError):
            wh_id = 0
        if not wh_id:
            flash('يرجى اختيار المخزن', 'error')
            return redirect(url_for('inventory_memo_receive'))
        pref = (request.form.get('production_ref') or '').strip()
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        unit_notes = request.form.getlist('unit_note[]')
        notes = request.form.get('notes')
        try:
            items_data = []
            for i, pid in enumerate(product_ids):
                if not pid:
                    continue
                qty = float(quantities[i] or 0)
                if qty <= 0:
                    continue
                raw_u = unit_notes[i] if i < len(unit_notes) else ''
                un_note = (raw_u or '').strip() or None
                items_data.append((int(pid), qty, un_note))
        except (TypeError, ValueError, IndexError):
            flash('توجد كميات غير صحيحة — يرجى المراجعة والمحاولة مرة أخرى', 'error')
            return redirect(url_for('inventory_memo_receive'))
        if not items_data:
            flash('يرجى إضافة صنف واحد على الأقل بكمية صحيحة', 'error')
            return redirect(url_for('inventory_memo_receive'))

        # ── حفظ مع إعادة محاولة آمنة عند تعارض رقم المذكرة (تزامن) ──
        max_attempts = 5
        memo = None
        for attempt in range(1, max_attempts + 1):
            memo = InventoryMemo(
                memo_number=get_next_number('MEM', InventoryMemo, 'memo_number'),
                memo_type='receive_production',
                production_ref=pref or None,
                warehouse_id=wh_id,
                user_id=current_user.id,
                notes=notes,
            )
            for pid, qty, un_note in items_data:
                memo.items.append(InventoryMemoItem(product_id=pid, quantity=qty, unit_note=un_note))
                st = Stock.query.filter_by(product_id=pid, warehouse_id=wh_id).first()
                if st:
                    st.quantity += qty
                else:
                    db.session.add(Stock(product_id=pid, warehouse_id=wh_id, quantity=qty))
            db.session.add(memo)
            try:
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if attempt == max_attempts:
                    flash('تعذّر حفظ المذكرة بسبب تعارض في الترقيم — يرجى المحاولة مرة أخرى', 'error')
                    return redirect(url_for('inventory_memo_receive'))
                continue
            except SQLAlchemyError:
                db.session.rollback()
                flash('حدث خطأ غير متوقع أثناء حفظ المذكرة — لم يتم حفظ أي بيانات', 'error')
                return redirect(url_for('inventory_memo_receive'))

        flash('تم تسجيل استلام منتج تام من الإنتاج', 'success')
        return redirect(url_for('inventory_memos_list'))
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    return render_template('inventory_memo_receive_form.html', warehouses=warehouses)


@app.route('/inventory/memos/<int:id>')
@login_required
def inventory_memo_detail(id):
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    memo = InventoryMemo.query.options(
        joinedload(InventoryMemo.items).joinedload(InventoryMemoItem.product),
        joinedload(InventoryMemo.warehouse),
        joinedload(InventoryMemo.user),
    ).get_or_404(id)
    total_qty = sum(float(it.quantity or 0) for it in memo.items)
    return render_template('inventory_memo_detail.html', memo=memo, total_qty=total_qty)


@app.route('/inventory/memos/<int:id>/print')
@login_required
def inventory_memo_print(id):
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    memo = InventoryMemo.query.options(
        joinedload(InventoryMemo.items).joinedload(InventoryMemoItem.product),
        joinedload(InventoryMemo.warehouse),
        joinedload(InventoryMemo.user),
    ).get_or_404(id)
    total_qty = sum(float(it.quantity or 0) for it in memo.items)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    return render_template(
        'inventory_memo_print.html', memo=memo, total_qty=total_qty,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
    )


@app.route('/inventory/memos/<int:id>/delete', methods=['POST'])
@login_required
def inventory_memo_delete(id):
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    memo = InventoryMemo.query.options(joinedload(InventoryMemo.items)).get_or_404(id)
    wh_id = memo.warehouse_id
    try:
        if memo.memo_type == 'issue_production':
            for it in memo.items:
                q = float(it.quantity or 0)
                st = Stock.query.filter_by(product_id=it.product_id, warehouse_id=wh_id).first()
                if st:
                    st.quantity += q
                else:
                    db.session.add(Stock(product_id=it.product_id, warehouse_id=wh_id, quantity=q))
        elif memo.memo_type == 'receive_production':
            for it in memo.items:
                st = Stock.query.filter_by(product_id=it.product_id, warehouse_id=wh_id).first()
                q = float(it.quantity or 0)
                if not st or float(st.quantity) + 1e-9 < q:
                    pname = it.product.name if it.product else str(it.product_id)
                    flash(f'لا يمكن الحذف: الرصيد الحالي لا يسمح بعكس استلام الصنف «{pname}»', 'error')
                    return redirect(url_for('inventory_memos_list'))
                st.quantity -= q
        else:
            flash('نوع مذكرة غير معروف', 'error')
            return redirect(url_for('inventory_memos_list'))
        db.session.delete(memo)
        db.session.commit()
        flash('تم حذف المذكرة وعكس أثرها على المخزون', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'تعذر الحذف: {e}', 'error')
    return redirect(url_for('inventory_memos_list'))


@app.route('/suppliers/<int:id>/statement')
@login_required
def supplier_statement(id):
    supplier = Supplier.query.get_or_404(id)
    purchases = Purchase.query.filter_by(supplier_id=id).order_by(Purchase.date.desc()).all()
    payments = SupplierPayment.query.filter_by(supplier_id=id).order_by(SupplierPayment.date.desc()).all()
    open_invoices = _supplier_open_invoices(id)
    return render_template('supplier_statement.html', supplier=supplier, purchases=purchases, payments=payments, open_invoices=open_invoices)

@app.route('/suppliers/<int:id>/statement/detailed')
@login_required
def supplier_statement_detailed(id):
    supplier = Supplier.query.get_or_404(id)
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())

    purchase_rows = db.session.query(PurchaseItem, Purchase).join(Purchase, PurchaseItem.purchase_id == Purchase.id).filter(
        Purchase.supplier_id == id,
        db.func.date(Purchase.date).between(date_from, date_to)
    ).order_by(Purchase.date.asc()).all()

    return_rows = db.session.query(PurchaseReturnItem, PurchaseReturn, Purchase).join(
        PurchaseReturn, PurchaseReturnItem.return_id == PurchaseReturn.id
    ).join(Purchase, PurchaseReturn.purchase_id == Purchase.id).filter(
        Purchase.supplier_id == id,
        db.func.date(PurchaseReturn.date).between(date_from, date_to)
    ).order_by(PurchaseReturn.date.asc()).all()

    rows = []
    for pi, purchase in purchase_rows:
        rows.append({
            'date': purchase.date, 'invoice_number': purchase.invoice_number, 'purchase_id': purchase.id,
            'type': 'purchase', 'type_label': 'شراء',
            'product': pi.product.name if pi.product else '—',
            'unit': pi.product.unit if pi.product else '',
            'qty': pi.quantity or 0, 'price': pi.price or 0, 'total': pi.total or 0,
        })
    for ri, ret, purchase in return_rows:
        rows.append({
            'date': ret.date, 'invoice_number': ret.invoice_number, 'purchase_id': purchase.id,
            'type': 'return', 'type_label': 'مرتجع شراء',
            'product': ri.product.name if ri.product else '—',
            'unit': ri.product.unit if ri.product else '',
            'qty': ri.quantity or 0, 'price': ri.price or 0, 'total': -(ri.total or 0),
        })
    rows.sort(key=lambda r: r['date'])

    products_summary = defaultdict(lambda: {'qty': 0.0, 'total': 0.0, 'unit': ''})
    for r in rows:
        agg = products_summary[r['product']]
        agg['unit'] = r['unit']
        if r['type'] == 'purchase':
            agg['qty'] += r['qty']
        else:
            agg['qty'] -= r['qty']
        agg['total'] += r['total']
    products_summary = dict(sorted(products_summary.items(), key=lambda kv: -kv[1]['total']))

    grand_total = sum(r['total'] for r in rows)
    grand_qty = sum(r['qty'] if r['type'] == 'purchase' else -r['qty'] for r in rows)

    return render_template(
        'supplier_statement_detailed.html', supplier=supplier, rows=rows,
        products_summary=products_summary, grand_total=grand_total, grand_qty=grand_qty,
        date_from=date_from, date_to=date_to)

@app.route('/suppliers/<int:id>/statement/detailed/print')
@login_required
def supplier_statement_detailed_print(id):
    supplier = Supplier.query.get_or_404(id)
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())

    purchase_rows = db.session.query(PurchaseItem, Purchase).join(Purchase, PurchaseItem.purchase_id == Purchase.id).filter(
        Purchase.supplier_id == id,
        db.func.date(Purchase.date).between(date_from, date_to)
    ).order_by(Purchase.date.asc()).all()

    return_rows = db.session.query(PurchaseReturnItem, PurchaseReturn, Purchase).join(
        PurchaseReturn, PurchaseReturnItem.return_id == PurchaseReturn.id
    ).join(Purchase, PurchaseReturn.purchase_id == Purchase.id).filter(
        Purchase.supplier_id == id,
        db.func.date(PurchaseReturn.date).between(date_from, date_to)
    ).order_by(PurchaseReturn.date.asc()).all()

    rows = []
    for pi, purchase in purchase_rows:
        rows.append({
            'date': purchase.date, 'invoice_number': purchase.invoice_number,
            'type': 'purchase', 'type_label': 'شراء',
            'product': pi.product.name if pi.product else '—',
            'unit': pi.product.unit if pi.product else '',
            'qty': pi.quantity or 0, 'price': pi.price or 0, 'total': pi.total or 0,
        })
    for ri, ret, purchase in return_rows:
        rows.append({
            'date': ret.date, 'invoice_number': ret.invoice_number,
            'type': 'return', 'type_label': 'مرتجع شراء',
            'product': ri.product.name if ri.product else '—',
            'unit': ri.product.unit if ri.product else '',
            'qty': ri.quantity or 0, 'price': ri.price or 0, 'total': -(ri.total or 0),
        })
    rows.sort(key=lambda r: r['date'])

    products_summary = defaultdict(lambda: {'qty': 0.0, 'total': 0.0, 'unit': ''})
    for r in rows:
        agg = products_summary[r['product']]
        agg['unit'] = r['unit']
        if r['type'] == 'purchase':
            agg['qty'] += r['qty']
        else:
            agg['qty'] -= r['qty']
        agg['total'] += r['total']
    products_summary = dict(sorted(products_summary.items(), key=lambda kv: -kv[1]['total']))

    grand_total = sum(r['total'] for r in rows)
    grand_qty = sum(r['qty'] if r['type'] == 'purchase' else -r['qty'] for r in rows)

    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'supplier_statement_detailed_print.html',
        supplier=supplier, rows=rows, products_summary=products_summary,
        grand_total=grand_total, grand_qty=grand_qty,
        date_from=date_from, date_to=date_to,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
        printed_at=datetime.now(),
    )

@app.route('/suppliers/<int:id>/statement/print')
@login_required
def supplier_statement_print(id):
    supplier = Supplier.query.get_or_404(id)
    purchases = Purchase.query.filter_by(supplier_id=id).order_by(Purchase.date.desc()).all()
    payments = SupplierPayment.query.filter_by(supplier_id=id).order_by(SupplierPayment.date.desc()).all()
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'supplier_statement_print.html',
        supplier=supplier,
        purchases=purchases,
        payments=payments,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
        printed_at=datetime.now(),
    )

@app.route('/suppliers/<int:id>/payment', methods=['POST'])
@login_required
def supplier_payment(id):
    supplier = Supplier.query.get_or_404(id)
    notes = request.form.get('notes') or ''
    entry_type = (request.form.get('entry_type') or 'payment').strip()
    if entry_type not in ('payment', 'debit', 'collect', 'invoice'):
        entry_type = 'payment'
    try:
        raw_amount = float(request.form.get('amount') or 0)
    except (TypeError, ValueError):
        flash('المبلغ غير صالح', 'error')
        return redirect(url_for('supplier_statement', id=id))

    if abs(raw_amount) < 0.0001:
        flash('أدخل مبلغاً أكبر من صفر', 'error')
        return redirect(url_for('supplier_statement', id=id))

    # إدخال سالب مثل -5000 = المورد مدين لنا (عليه)
    if raw_amount < 0 and entry_type == 'payment':
        entry_type = 'debit'
    amount = abs(raw_amount)

    if entry_type == 'debit':
        payment = SupplierPayment(
            supplier_id=id,
            amount=-amount,
            notes=('إضافة على الحساب — ' + notes) if notes else 'إضافة على الحساب — المورد مدين لنا',
            user_id=current_user.id
        )
        supplier.balance -= amount
        db.session.add(payment)
        db.session.commit()
        flash('تم إضافة المبلغ على حساب المورد (مدين لنا)', 'success')
        return redirect(url_for('supplier_statement', id=id))

    if entry_type == 'collect':
        payment = SupplierPayment(
            supplier_id=id,
            amount=amount,
            notes=('تحصيل من المورد — ' + notes) if notes else 'تحصيل من المورد — تسديد ما عليه',
            user_id=current_user.id
        )
        supplier.balance += amount
        db.session.add(payment)
        db.session.commit()
        flash('تم تسجيل التحصيل من المورد', 'success')
        return redirect(url_for('supplier_statement', id=id))

    if entry_type == 'invoice':
        invoice_number = (request.form.get('invoice_number') or '').strip()
        if not invoice_number:
            flash('يرجى اختيار رقم الفاتورة المراد تسديدها', 'error')
            return redirect(url_for('supplier_statement', id=id))
        purchase = Purchase.query.filter_by(supplier_id=id, invoice_number=invoice_number).first()
        if not purchase:
            flash('رقم الفاتورة غير موجود لهذا المورد', 'error')
            return redirect(url_for('supplier_statement', id=id))
        already_paid = _supplier_linked_payments_total(id, invoice_number)
        actual_remaining = float(purchase.remaining or 0) - already_paid
        if actual_remaining <= 0.0001:
            flash(f'الفاتورة {invoice_number} مسدَّدة بالكامل بالفعل', 'error')
            return redirect(url_for('supplier_statement', id=id))
        if amount > actual_remaining:
            amount = actual_remaining
        # يجب أن ينتهي النص دائماً بـ«مرتبطة بفاتورة {رقم}» بالضبط حتى يُحتسَب مرتبطاً
        # بهذه الفاتورة عند عرضها أو حذفها لاحقاً — لا نسمح لملاحظات المستخدم بكسر هذا الربط.
        linked_note = f'مرتبطة بفاتورة {invoice_number}'
        final_notes = (f'دفعة على حساب المورد — {notes} — {linked_note}'
                       if notes else f'دفعة على حساب المورد — {linked_note}')
        payment = SupplierPayment(
            supplier_id=id,
            amount=amount,
            notes=final_notes,
            user_id=current_user.id
        )
        supplier.balance -= amount
        db.session.add(payment)
        db.session.commit()
        flash(f'تم تسجيل دفعة {amount:.2f} على الفاتورة {invoice_number}', 'success')
        return redirect(url_for('supplier_statement', id=id))

    # ملاحظة: دفعات كشف الحساب لا تُطبَّق على الفواتير نفسها (لا تغيّر purchase.paid/purchase.remaining)،
    # فقط تُسجَّل كحركة في كشف الحساب وتُحدَّث رصيد المورد الإجمالي، حتى تبقى الفاتورة الأصلية
    # كما صدرت (أجل) عند عرضها لاحقاً.
    payment = SupplierPayment(
        supplier_id=id,
        amount=amount,
        notes=notes or 'دفعة عامة',
        user_id=current_user.id
    )
    supplier.balance -= amount
    db.session.add(payment)
    db.session.commit()
    flash('تم تسجيل الدفعة بنجاح', 'success')
    return redirect(url_for('supplier_statement', id=id))

@app.route('/suppliers/<int:id>/payment/<int:pay_id>/delete', methods=['POST'])
@login_required
@statement_payment_delete_required
def delete_supplier_payment(id, pay_id):
    supplier = Supplier.query.get_or_404(id)
    payment = SupplierPayment.query.get_or_404(pay_id)
    if payment.supplier_id != id:
        flash('الحركة غير مرتبطة بهذا المورد', 'error')
        return redirect(url_for('supplier_statement', id=id))
    notes = payment.notes or ''
    stored = float(payment.amount or 0)
    abs_amt = abs(stored)
    try:
        if stored < 0 or 'إضافة على الحساب' in notes:
            supplier.balance += abs_amt
        elif 'تحصيل من المورد' in notes:
            supplier.balance -= abs_amt
        else:
            supplier.balance += abs_amt
            # توافق مع بيانات قديمة كانت تُطبَّق على الفواتير مباشرة (قبل التعديل الحالي)
            if 'دفعة على فواتير:' in notes or 'دفعة على فاتورة ' in notes:
                _unapply_supplier_invoice_payment(id, abs_amt, notes)
        db.session.delete(payment)
        db.session.commit()
        flash('تم حذف الحركة وإلغاء أثرها على الحساب', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'تعذر حذف الحركة: {e}', 'error')
    return redirect(url_for('supplier_statement', id=id))

@app.route('/suppliers/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_supplier(id):
    supplier = Supplier.query.get_or_404(id)
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if code:
            supplier.code = code
        supplier.name = request.form['name']
        supplier.phone = request.form.get('phone')
        supplier.email = request.form.get('email')
        supplier.address = request.form.get('address')
        db.session.commit()
        flash('تم تحديث بيانات المورد', 'success')
        return redirect(url_for('suppliers'))
    return render_template('supplier_form.html', supplier=supplier)

@app.route('/suppliers/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_supplier(id):
    supplier = Supplier.query.get_or_404(id)
    if supplier.balance and abs(supplier.balance) > 0.0001:
        flash('لا يمكن حذف المورد طالما يوجد رصيد', 'error')
        return redirect(url_for('suppliers'))
    supplier.is_active = False
    db.session.commit()
    flash('تم حذف المورد', 'success')
    return redirect(url_for('suppliers'))

@app.route('/reports/dashboard')
@login_required
def report_dashboard():
    from sqlalchemy import extract, func
    today = date.today()
    monthly_sales = []
    for i in range(5, -1, -1):
        month = today.month - i; year = today.year
        while month <= 0: month += 12; year -= 1
        total = db.session.query(db.func.sum(Sale.total)).filter(
            extract('month', Sale.date) == month, extract('year', Sale.date) == year
        ).scalar() or 0
        monthly_sales.append({'label': f'{year}/{month:02d}', 'total': round(total, 2)})
    top_products = db.session.query(
        Product.name, func.sum(SaleItem.quantity).label('qty'), func.sum(SaleItem.total).label('revenue')
    ).join(SaleItem).group_by(Product.id).order_by(db.desc('revenue')).limit(5).all()
    top_customers = db.session.query(
        Customer.name, func.sum(Sale.total).label('total')
    ).join(Sale).group_by(Customer.id).order_by(db.desc('total')).limit(5).all()
    total_sales_gross = db.session.query(func.sum(Sale.total)).scalar() or 0
    total_sale_returns = db.session.query(func.sum(SaleReturn.total)).scalar() or 0
    total_sales = float(total_sales_gross) - float(total_sale_returns)

    total_purchases_gross = db.session.query(func.sum(Purchase.total)).scalar() or 0
    total_purchase_returns = db.session.query(func.sum(PurchaseReturn.total)).scalar() or 0
    total_purchases = float(total_purchases_gross) - float(total_purchase_returns)

    total_expenses = db.session.query(func.sum(Expense.amount)).scalar() or 0
    total_receivables = db.session.query(func.sum(Customer.balance)).scalar() or 0
    total_payables = db.session.query(func.sum(Supplier.balance)).scalar() or 0
    line_disc = db.session.query(
        func.coalesce(func.sum(SaleItem.quantity * SaleItem.price * SaleItem.discount / 100.0), 0)
    ).scalar() or 0
    inv_disc = db.session.query(func.coalesce(func.sum(Sale.discount), 0)).scalar() or 0
    total_sales_discounts = float(line_disc) + float(inv_disc)
    return render_template('report_dashboard.html',
        monthly_sales=monthly_sales, top_products=top_products, top_customers=top_customers,
        total_sales=total_sales, total_purchases=total_purchases, total_expenses=total_expenses,
        total_receivables=total_receivables, total_payables=total_payables,
        total_sales_discounts=total_sales_discounts)

@app.route('/reports/profit')
@login_required
def report_profit():
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    sales = Sale.query.filter(db.func.date(Sale.date).between(date_from, date_to)).all()
    expenses = Expense.query.filter(db.func.date(Expense.date).between(date_from, date_to)).all()
    purchases = Purchase.query.filter(db.func.date(Purchase.date).between(date_from, date_to)).all()
    sale_returns = SaleReturn.query.filter(db.func.date(SaleReturn.date).between(date_from, date_to)).all()
    purchase_returns = PurchaseReturn.query.filter(db.func.date(PurchaseReturn.date).between(date_from, date_to)).all()

    total_sales_gross = sum(s.total for s in sales)
    total_sale_returns = sum(r.total for r in sale_returns)
    total_sales = total_sales_gross - total_sale_returns

    total_purchases_gross = sum(p.total for p in purchases)
    total_purchase_returns = sum(r.total for r in purchase_returns)
    total_purchases = total_purchases_gross - total_purchase_returns

    total_expenses = sum(e.amount for e in expenses)
    gross_profit = total_sales - total_purchases
    net_profit = gross_profit - total_expenses
    return render_template('report_profit.html',
        date_from=date_from, date_to=date_to,
        total_sales=total_sales,
        total_sales_gross=total_sales_gross,
        total_sale_returns=total_sale_returns,
        total_purchases=total_purchases,
        total_purchases_gross=total_purchases_gross,
        total_purchase_returns=total_purchase_returns,
        total_expenses=total_expenses,
        gross_profit=gross_profit, net_profit=net_profit,
        sales=sales, expenses=expenses)

@app.route('/reports/customers')
@login_required
def report_customers():
    customers = Customer.query.filter_by(is_active=True).order_by(Customer.balance.desc()).all()
    total_receivable = sum(c.balance for c in customers if c.balance > 0)
    return render_template('report_customers.html', customers=customers, total_receivable=total_receivable)

@app.route('/reports/suppliers')
@login_required
def report_suppliers():
    suppliers = Supplier.query.filter_by(is_active=True).order_by(Supplier.balance.desc()).all()
    total_payable = sum(s.balance for s in suppliers if s.balance > 0)
    return render_template('report_suppliers.html', suppliers=suppliers, total_payable=total_payable)

@app.route('/reports/low-stock')
@login_required
def report_low_stock():
    low_items = db.session.query(Stock, Product, Warehouse).join(Product).join(Warehouse).filter(
        Stock.quantity <= Product.min_stock, Product.min_stock > 0, Product.is_active == True
    ).all()
    return render_template('report_low_stock.html', low_items=low_items)

@app.route('/api/notifications')
@login_required
def api_notifications():
    pending_transfers = _pending_transfers_count_for_user(current_user)
    low_stock = db.session.query(Stock).join(Product).filter(
        Stock.quantity <= Product.min_stock, Product.min_stock > 0, Product.is_active == True
    ).count()
    overdue_customers = Customer.query.filter(Customer.balance > 0).count()
    return jsonify({
        'pending_transfers': pending_transfers,
        'low_stock': low_stock,
        'overdue_customers': overdue_customers,
        'total': pending_transfers + low_stock,
    })

@app.route('/api/dashboard-kpi')
@login_required
def api_dashboard_kpi():
    today = date.today()
    sales_today = db.session.query(db.func.sum(Sale.total)).filter(
        db.func.date(Sale.date) == today).scalar() or 0
    sale_returns_today = db.session.query(db.func.sum(SaleReturn.total)).filter(
        db.func.date(SaleReturn.date) == today).scalar() or 0
    net_sales_after_returns = float(sales_today) - float(sale_returns_today)
    purchases_today = db.session.query(db.func.sum(Purchase.total)).filter(
        db.func.date(Purchase.date) == today).scalar() or 0
    customers_count = sale_returns_today
    products_count = net_sales_after_returns
    pending_transfers = _pending_transfers_count_for_user(current_user)
    low_stock = db.session.query(Stock).join(Product).filter(
        Stock.quantity <= Product.min_stock, Product.min_stock > 0, Product.is_active == True).count()
    return jsonify({
        'sales_today': round(sales_today, 2),
        'purchases_today': round(purchases_today, 2),
        'customers_count': customers_count,
        'products_count': products_count,
        'pending_transfers': pending_transfers,
        'low_stock': low_stock,
    })


@app.route('/api/inventory/stocks')
@login_required
def api_inventory_stocks():
    warehouse_id = request.args.get('warehouse_id')
    q = request.args.get('q', '')
    query = db.session.query(Stock, Product, Warehouse).join(Product).join(Warehouse).filter(
        Product.is_active == True, Warehouse.is_active == True)
    if warehouse_id:
        query = query.filter(Stock.warehouse_id == warehouse_id)
    if q:
        query = query.filter(Product.name.contains(q))
    rows = []
    for stock, product, wh in query.all():
        st = 'ok'
        if stock.quantity <= 0:
            st = 'out'
        elif product.min_stock > 0 and stock.quantity <= product.min_stock:
            st = 'low'
        rows.append({
            'product_id': product.id,
            'warehouse_id': wh.id,
            'code': product.code,
            'name': product.name,
            'category': product.category.name if product.category else '—',
            'warehouse': wh.name,
            'branch': wh.branch.name if wh.branch else '—',
            'quantity': stock.quantity,
            'unit': product.unit,
            'status': st,
        })
    return jsonify({'rows': rows, 'count': len(rows)})


@app.route('/api/dashboard-stats')
@login_required
def api_dashboard_stats():
    from datetime import timedelta
    today = date.today()
    sales_data = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        total = db.session.query(db.func.sum(Sale.total)).filter(
            db.func.date(Sale.date) == d).scalar() or 0
        sales_data.append({'date': d.strftime('%m/%d'), 'total': round(total, 2)})
    return jsonify({'daily_sales': sales_data})

@app.route('/inventory/adjust', methods=['GET', 'POST'])
@login_required
@admin_required
def adjust_stock():
    if request.method == 'POST':
        products = Product.query.filter_by(is_active=True).all()
        warehouses = Warehouse.query.filter_by(is_active=True).all()
        try:
            product_id = int(request.form.get('product_id') or 0)
            warehouse_id = int(request.form.get('warehouse_id') or 0)
            new_qty = float(request.form.get('quantity') or 0)
        except (TypeError, ValueError):
            flash('توجد بيانات غير صحيحة (الصنف/المخزن/الكمية) — يرجى المراجعة والمحاولة مرة أخرى', 'error')
            return render_template('stock_adjust.html', products=products, warehouses=warehouses)
        if not product_id or not warehouse_id:
            flash('يرجى اختيار الصنف والمخزن', 'error')
            return render_template('stock_adjust.html', products=products, warehouses=warehouses)
        if new_qty < 0:
            flash('لا يمكن أن تكون الكمية سالبة', 'error')
            return render_template('stock_adjust.html', products=products, warehouses=warehouses)
        reason = request.form.get('reason', 'تسوية يدوية')
        stock = Stock.query.filter_by(product_id=product_id, warehouse_id=warehouse_id).first()
        if not stock:
            stock = Stock(product_id=product_id, warehouse_id=warehouse_id, quantity=0)
            db.session.add(stock)
        old_qty = stock.quantity
        stock.quantity = new_qty
        db.session.add(StockAdjustmentLog(
            product_id=product_id,
            warehouse_id=warehouse_id,
            old_quantity=old_qty,
            new_quantity=new_qty,
            reason=reason or 'تسوية يدوية',
            user_id=current_user.id,
        ))
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash('حدث خطأ غير متوقع أثناء حفظ التسوية — لم يتم حفظ أي بيانات', 'error')
            return render_template('stock_adjust.html', products=products, warehouses=warehouses)
        flash(f'تم تسوية المخزون من {old_qty} إلى {new_qty} — {reason}', 'success')
        return redirect(url_for('inventory'))
    products = Product.query.filter_by(is_active=True).all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    return render_template('stock_adjust.html', products=products, warehouses=warehouses)

@app.route('/profile/password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        old = request.form['old_password']
        new = request.form['new_password']
        confirm = request.form['confirm_password']
        if not current_user.check_password(old):
            flash('كلمة المرور الحالية غير صحيحة', 'error')
        elif new != confirm:
            flash('كلمة المرور الجديدة غير متطابقة', 'error')
        elif len(new) < 6:
            flash('يجب أن تكون 6 أحرف على الأقل', 'error')
        else:
            current_user.set_password(new)
            db.session.commit()
            flash('تم تغيير كلمة المرور بنجاح', 'success')
            return redirect(safe_home_url_for(current_user))
    return render_template('change_password.html')

@app.route('/categories')
@login_required
def categories():
    cats = Category.query.all()
    return render_template('categories.html', cats=cats)

@app.route('/categories/add', methods=['POST'])
@login_required
def add_category():
    cat = Category(name=request.form['name'], parent_id=request.form.get('parent_id') or None)
    db.session.add(cat)
    db.session.commit()
    flash('تم إضافة التصنيف', 'success')
    return redirect(url_for('categories'))

@app.route('/categories/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_category(id):
    cat = Category.query.get_or_404(id)
    if request.method == 'POST':
        cat.name = request.form['name']
        pid = request.form.get('parent_id') or None
        if pid and int(pid) == cat.id:
            flash('لا يمكن جعل التصنيف أباً لنفسه', 'error')
            return redirect(url_for('edit_category', id=id))
        cat.parent_id = int(pid) if pid else None
        db.session.commit()
        flash('تم تحديث التصنيف', 'success')
        return redirect(url_for('categories'))
    others = Category.query.filter(Category.id != cat.id).all()
    return render_template('category_form.html', category=cat, cats=others)

@app.route('/categories/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_category(id):
    cat = Category.query.get_or_404(id)
    if cat.children:
        flash('لا يمكن الحذف: يوجد تصنيفات فرعية مرتبطة', 'error')
        return redirect(url_for('categories'))
    if cat.products:
        flash('لا يمكن الحذف: التصنيف مرتبط بأصناف', 'error')
        return redirect(url_for('categories'))
    db.session.delete(cat)
    db.session.commit()
    flash('تم حذف التصنيف', 'success')
    return redirect(url_for('categories'))


# ===== ADVANCED SEARCH API =====
@app.route('/api/search')
@login_required
def api_search():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'results': []})
    results = []
    # Products
    for p in Product.query.filter(Product.name.contains(q), Product.is_active==True).limit(4).all():
        results.append({'type': 'product', 'icon': 'fa-barcode', 'title': p.name, 'sub': f'كود: {p.code}', 'url': f'/products/edit/{p.id}'})
    # Customers
    for c in Customer.query.filter(db.or_(Customer.name.contains(q), Customer.phone.contains(q)), Customer.is_active==True).limit(4).all():
        results.append({'type': 'customer', 'icon': 'fa-user', 'title': c.name, 'sub': f'رصيد: {c.balance:.2f}', 'url': f'/customers/{c.id}/statement'})
    # Suppliers
    for s in Supplier.query.filter(db.or_(Supplier.name.contains(q), Supplier.phone.contains(q)), Supplier.is_active==True).limit(3).all():
        results.append({'type': 'supplier', 'icon': 'fa-truck', 'title': s.name, 'sub': 'مورد', 'url': f'/suppliers/{s.id}/statement'})
    # Sales invoices
    for s in Sale.query.filter(Sale.invoice_number.contains(q)).limit(3).all():
        results.append({'type': 'sale', 'icon': 'fa-receipt', 'title': s.invoice_number, 'sub': f'مبيعات — {s.total:.2f}', 'url': f'/sales/{s.id}'})
    # Purchase invoices
    for p in Purchase.query.filter(Purchase.invoice_number.contains(q)).limit(3).all():
        results.append({'type': 'purchase', 'icon': 'fa-shopping-cart', 'title': p.invoice_number, 'sub': f'مشتريات — {p.total:.2f}', 'url': f'/purchases/{p.id}'})
    return jsonify({'results': results})


# ===== EMPLOYEE SALARY / DETAIL =====
@app.route('/employees/<int:id>')
@login_required
def employee_detail(id):
    emp = Employee.query.get_or_404(id)
    return render_template('employee_detail.html', emp=emp)

@app.route('/employees/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_employee(id):
    emp = Employee.query.get_or_404(id)
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if code:
            emp.code = code
        emp.name = request.form['name']
        emp.phone = request.form.get('phone')
        emp.email = request.form.get('email')
        emp.position = request.form.get('position')
        emp.department = request.form.get('department')
        emp.branch_id = request.form.get('branch_id') or None
        emp.salary = float(request.form.get('salary', 0))
        emp.hire_date = datetime.strptime(request.form['hire_date'], '%Y-%m-%d').date() if request.form.get('hire_date') else None
        db.session.commit()
        flash('تم تحديث بيانات الموظف', 'success')
        return redirect(url_for('employees'))
    branches = Branch.query.filter_by(is_active=True).all()
    return render_template('employee_form.html', emp=emp, branches=branches)


# ===== BACKUP / EXPORT =====
@app.route('/settings/backup')
@login_required
@admin_required
def backup():
    import json
    from datetime import datetime as dt
    data = {
        'exported_at': dt.now().isoformat(),
        'version': '2.0',
        'customers': [{'id': c.id, 'code': c.code, 'name': c.name, 'phone': c.phone,
                        'balance': c.balance, 'credit_limit': c.credit_limit}
                       for c in Customer.query.all()],
        'suppliers': [{'id': s.id, 'code': s.code, 'name': s.name, 'phone': s.phone, 'balance': s.balance}
                       for s in Supplier.query.all()],
        'products': [{'id': p.id, 'code': p.code, 'name': p.name, 'unit': p.unit,
                       'cost_price': p.cost_price, 'sell_price': p.sell_price, 'min_stock': p.min_stock}
                      for p in Product.query.filter_by(is_active=True).all()],
        'sales_count': Sale.query.count(),
        'purchases_count': Purchase.query.count(),
        'total_sales': db.session.query(db.func.sum(Sale.total)).scalar() or 0,
        'total_purchases': db.session.query(db.func.sum(Purchase.total)).scalar() or 0,
    }
    from flask import Response
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment;filename=proerp_backup_{dt.now().strftime("%Y%m%d_%H%M%S")}.json'}
    )


# ===== QUICK CUSTOMER PAYMENT FROM SALES =====
@app.route('/sales/<int:id>/payment', methods=['POST'])
@login_required
def sale_payment(id):
    sale = Sale.query.get_or_404(id)
    try:
        amount = float(request.form['amount'])
    except (TypeError, ValueError, KeyError):
        flash('المبلغ غير صالح', 'error')
        return redirect(url_for('sale_detail', id=id))
    # المتبقي الفعلي = المتبقي الأصلي وقت إصدار الفاتورة ناقص أي دفعات سابقة ارتبطت بها فعلاً
    # عبر كشف الحساب (لأن sale.remaining لا يتغيّر بعد الآن ويبقى يعرض القيمة الأصلية).
    already_paid = _customer_linked_payments_total(sale.customer_id, sale.invoice_number)
    actual_remaining = float(sale.remaining or 0) - already_paid
    if actual_remaining <= 0.0001:
        flash(f'الفاتورة {sale.invoice_number} مسدَّدة بالكامل بالفعل', 'error')
        return redirect(url_for('sale_detail', id=id))
    if amount <= 0:
        flash('أدخل مبلغاً أكبر من صفر', 'error')
        return redirect(url_for('sale_detail', id=id))
    if amount > actual_remaining:
        amount = actual_remaining
    # لا نُعدّل sale.paid / sale.remaining هنا: الفاتورة الأصلية تبقى كما صدرت (أجل)،
    # والدفعة تُسجَّل فقط كحركة في كشف حساب العميل مع تحديث رصيده الإجمالي.
    if sale.customer_id:
        customer = Customer.query.get(sale.customer_id)
        if customer:
            customer.balance -= amount
    payment = CustomerPayment(customer_id=sale.customer_id, amount=amount,
                               notes=f'دفعة على حساب العميل — مرتبطة بفاتورة {sale.invoice_number}',
                               user_id=current_user.id)
    db.session.add(payment)
    db.session.commit()
    flash(f'تم تسجيل دفعة {amount:.2f} على الفاتورة {sale.invoice_number}', 'success')
    return redirect(url_for('sale_detail', id=id))


# ===== QUICK SUPPLIER PAYMENT FROM PURCHASES =====
@app.route('/purchases/<int:id>/payment', methods=['POST'])
@login_required
def purchase_payment(id):
    purchase = Purchase.query.get_or_404(id)
    try:
        amount = float(request.form['amount'])
    except (TypeError, ValueError, KeyError):
        flash('المبلغ غير صالح', 'error')
        return redirect(url_for('purchase_detail', id=id))
    already_paid = _supplier_linked_payments_total(purchase.supplier_id, purchase.invoice_number)
    actual_remaining = float(purchase.remaining or 0) - already_paid
    if actual_remaining <= 0.0001:
        flash(f'الفاتورة {purchase.invoice_number} مسدَّدة بالكامل بالفعل', 'error')
        return redirect(url_for('purchase_detail', id=id))
    if amount <= 0:
        flash('أدخل مبلغاً أكبر من صفر', 'error')
        return redirect(url_for('purchase_detail', id=id))
    if amount > actual_remaining:
        amount = actual_remaining
    # لا نُعدّل purchase.paid / purchase.remaining هنا: الفاتورة الأصلية تبقى كما صدرت (أجل)،
    # والدفعة تُسجَّل فقط كحركة في كشف حساب المورد مع تحديث رصيده الإجمالي.
    if purchase.supplier_id:
        supplier = Supplier.query.get(purchase.supplier_id)
        if supplier:
            supplier.balance -= amount
    payment = SupplierPayment(supplier_id=purchase.supplier_id, amount=amount,
                               notes=f'دفعة على حساب المورد — مرتبطة بفاتورة {purchase.invoice_number}',
                               user_id=current_user.id)
    db.session.add(payment)
    db.session.commit()
    flash(f'تم تسجيل دفعة {amount:.2f} على الفاتورة {purchase.invoice_number}', 'success')
    return redirect(url_for('purchase_detail', id=id))


# ===== INVOICE PRINT API =====
@app.route('/sales/<int:id>/print')
@login_required
def sale_print(id):
    sale = Sale.query.get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'sale_print.html',
        sale=sale,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/purchases/<int:id>/print')
@login_required
def purchase_print(id):
    purchase = Purchase.query.get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'purchase_print.html',
        purchase=purchase,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


# ===== USER MANAGEMENT - TOGGLE ACTIVE =====
@app.route('/settings/users/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_user(id):
    user = User.query.get_or_404(id)
    if user.role == 'developer' and current_user.role != 'developer':
        flash('غير مسموح بتعديل هذا الحساب', 'error')
        return redirect(url_for('users'))
    if user.id == current_user.id:
        flash('لا يمكنك تعطيل حسابك الخاص', 'error')
    else:
        user.is_active = not user.is_active
        db.session.commit()
        flash(f'تم {"تفعيل" if user.is_active else "تعطيل"} المستخدم {user.username}', 'success')
    return redirect(url_for('users'))


@app.route('/settings/users/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user_account(id):
    if not user_can_delete_users_account(current_user):
        flash('ليس لديك صلاحية حذف المستخدمين', 'error')
        return redirect(url_for('users'))
    u = User.query.get_or_404(id)
    if u.id == current_user.id:
        flash('لا يمكنك حذف حسابك الحالي', 'error')
        return redirect(url_for('users'))
    if u.role == 'developer' and current_user.role != 'developer':
        flash('غير مسموح بحذف هذا الحساب', 'error')
        return redirect(url_for('users'))
    try:
        db.session.delete(u)
        db.session.commit()
        flash('تم حذف المستخدم', 'success')
    except Exception:
        db.session.rollback()
        u.is_active = False
        db.session.commit()
        flash('لا يمكن الحذف النهائي لوجود سجلات مرتبطة؛ تم تعطيل الحساب بدلاً من ذلك', 'warning')
    return redirect(url_for('users'))


# ===== EXPENSE CATEGORIES SUMMARY =====
@app.route('/reports/expenses')
@login_required
def report_expenses():
    from sqlalchemy import func
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    expenses = Expense.query.filter(db.func.date(Expense.date).between(date_from, date_to)).all()
    by_category = {}
    for e in expenses:
        by_category[e.category] = by_category.get(e.category, 0) + e.amount
    total = sum(e.amount for e in expenses)
    return render_template('report_expenses.html',
        expenses=expenses, by_category=by_category,
        total=total, date_from=date_from, date_to=date_to)


def _open_browser():
    import webbrowser
    time_module.sleep(1.2)  # مهلة بسيطة حتى يبدأ السيرفر بالاستماع
    webbrowser.open('http://127.0.0.1:5000')


if __name__ == '__main__':
    init_db()
    # لا نفتح المتصفح مرتين عند استخدام debug reloader
    if not os.environ.get('WERKZEUG_RUN_MAIN'):
        threading.Thread(target=_open_browser, daemon=True).start()
    app.run(debug=True, host='0.0.0.0', port=5000)
