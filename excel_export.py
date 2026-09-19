# -*- coding: utf-8 -*-
"""
ProERP — تصدير إكسيل منسّق
==========================
Blueprint مستقل فيه كل شاشات التصدير:
  • قائمة العملاء            /export/customers.xlsx
  • قائمة الموردين           /export/suppliers.xlsx
  • الأصناف                  /export/products.xlsx
  • جرد المخزن               /export/inventory.xlsx
  • كشف حساب عميل تفصيلي     /export/customers/<id>/statement-detailed.xlsx
  • كشف حساب مورد تفصيلي     /export/suppliers/<id>/statement-detailed.xlsx

كل الاستيرادات من app.py بتتم جوّه الدوال (lazy) عشان نتفادى الـ circular import.
"""
from io import BytesIO
from datetime import datetime, date

from flask import Blueprint, request, send_file, flash, redirect, url_for
from flask_login import login_required, current_user

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover
    openpyxl = None

excel_bp = Blueprint('excel_export', __name__)

# ── مراجع الموديلات ─────────────────────────────────────────────────
# بتتحقن من app.py (عبر init_models) بعد تعريف db وكل الموديلات مباشرة،
# ومش عن طريق "from app import ..." — لأن الاستيراد ده بيخلي بايثون
# ينفّذ app.py من جديد لو كان شغّال كـ __main__ (مثلاً python app.py على
# وندوز)، فيتكوّن تطبيق Flask ثاني وكائن SQLAlchemy(app) ثاني مختلف
# عن اللي شغّال فعليًا، وده اللي كان بيسبب:
#   RuntimeError: The current Flask app is not registered with this
#   'SQLAlchemy' instance...
db = Customer = Supplier = Product = Stock = Warehouse = None
Sale = SaleItem = SaleReturn = SaleReturnItem = None
Purchase = PurchaseItem = PurchaseReturn = PurchaseReturnItem = None
get_app_settings_dict = None
DEFAULT_SETTINGS = None


def init_models(**kwargs):
    """تُستدعى مرة واحدة من app.py فور تعريف db وكل الموديلات، لحقن
    المراجع داخل هذا الموديول مباشرة بدل الاستيراد المتأخر."""
    globals().update(kwargs)

# ───────────────────────── الهوية البصرية ─────────────────────────
C_BRAND = '1F4E79'   # أزرق داكن — شريط العنوان
C_HEAD = '2E75B6'    # أزرق — صف رؤوس الأعمدة
C_ZEBRA = 'EEF4FB'   # أزرق فاتح جدًا — الصفوف المتبادلة
C_TOTAL = 'FFF2CC'   # أصفر فاتح — صف الإجماليات
C_BORDER = 'B4C7DC'
C_MUTED = '5A6B7B'
C_OK = '1E7E34'
C_BAD = 'C0392B'

FONT_NAME = 'Calibri'
MONEY = '#,##0.00'
QTY = '#,##0.###'
PCT = '0.0%'

_thin = Side(style='thin', color=C_BORDER)
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
RIGHT = Alignment(horizontal='right', vertical='center')


def _need_openpyxl():
    """يرجّع False لو المكتبة مش متثبتة، مع رسالة للمستخدم."""
    if openpyxl is None:
        flash('تعذّر التصدير: مكتبة openpyxl غير مثبّتة على الخادم', 'error')
        return False
    return True


def _company_name():
    try:
        gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
        return gs.get('company_name') or DEFAULT_SETTINGS.get('company_name') or 'ProERP'
    except Exception:
        return 'ProERP'


def _who():
    try:
        return getattr(current_user, 'full_name', None) or getattr(current_user, 'username', '—')
    except Exception:
        return '—'


def _new_sheet(wb, title, first=False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title[:31]
    ws.sheet_view.rightToLeft = True          # اتجاه الورقة من اليمين لليسار
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    return ws


def _title_block(ws, title, ncols, meta_lines=None):
    """شريط العنوان أعلى الورقة — يرجّع رقم الصف اللي بعده."""
    last = get_column_letter(ncols)

    ws.merge_cells(f'A1:{last}1')
    c = ws['A1']
    c.value = _company_name()
    c.font = Font(name=FONT_NAME, size=16, bold=True, color='FFFFFF')
    c.fill = PatternFill('solid', fgColor=C_BRAND)
    c.alignment = CENTER
    ws.row_dimensions[1].height = 28

    ws.merge_cells(f'A2:{last}2')
    c = ws['A2']
    c.value = title
    c.font = Font(name=FONT_NAME, size=13, bold=True, color=C_BRAND)
    c.alignment = CENTER
    ws.row_dimensions[2].height = 22

    row = 3
    for line in (meta_lines or []):
        ws.merge_cells(f'A{row}:{last}{row}')
        c = ws[f'A{row}']
        c.value = line
        c.font = Font(name=FONT_NAME, size=10, color=C_MUTED)
        c.alignment = CENTER
        ws.row_dimensions[row].height = 16
        row += 1

    ws.merge_cells(f'A{row}:{last}{row}')
    ws[f'A{row}'].value = 'تم التصدير: %s — بواسطة: %s' % (
        datetime.now().strftime('%Y-%m-%d %H:%M'), _who())
    ws[f'A{row}'].font = Font(name=FONT_NAME, size=9, italic=True, color=C_MUTED)
    ws[f'A{row}'].alignment = CENTER
    return row + 2  # سطر فاضي فاصل


def _header_row(ws, headers, row):
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = Font(name=FONT_NAME, size=11, bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor=C_HEAD)
        c.alignment = CENTER
        c.border = BORDER
    ws.row_dimensions[row].height = 26
    return row + 1


def _write_rows(ws, rows, start_row, formats=None, aligns=None):
    """rows = list من list. formats/aligns = dict {index العمود (1-based): قيمة}."""
    formats = formats or {}
    aligns = aligns or {}
    r = start_row
    for n, data in enumerate(rows):
        zebra = (n % 2 == 1)
        for i, val in enumerate(data, start=1):
            c = ws.cell(row=r, column=i, value=val)
            c.font = Font(name=FONT_NAME, size=10)
            c.border = BORDER
            c.alignment = aligns.get(i, CENTER)
            if i in formats:
                c.number_format = formats[i]
            if zebra:
                c.fill = PatternFill('solid', fgColor=C_ZEBRA)
        ws.row_dimensions[r].height = 18
        r += 1
    return r


def _total_row(ws, values, row, ncols, formats=None):
    """values = dict {index العمود: القيمة}. الباقي بيفضل فاضي."""
    formats = formats or {}
    for i in range(1, ncols + 1):
        c = ws.cell(row=row, column=i, value=values.get(i))
        c.font = Font(name=FONT_NAME, size=11, bold=True, color=C_BRAND)
        c.fill = PatternFill('solid', fgColor=C_TOTAL)
        c.border = BORDER
        c.alignment = CENTER
        if i in formats:
            c.number_format = formats[i]
    ws.row_dimensions[row].height = 22
    ws._erp_total_row = row      # عشان الفلتر ما يشملش صف الإجماليات
    return row + 1


def _finish(ws, widths, header_row, last_data_row):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    filter_end = last_data_row
    if getattr(ws, '_erp_total_row', None) == filter_end:
        filter_end -= 1
    if filter_end > header_row:
        ws.auto_filter.ref = '%s%d:%s%d' % (
            'A', header_row, get_column_letter(len(widths)), filter_end)
    ws.print_title_rows = '%d:%d' % (header_row, header_row)


def _empty_note(ws, row, ncols, text='لا توجد بيانات في هذا النطاق'):
    last = get_column_letter(ncols)
    ws.merge_cells('A%d:%s%d' % (row, last, row))
    c = ws['A%d' % row]
    c.value = text
    c.font = Font(name=FONT_NAME, size=11, italic=True, color=C_MUTED)
    c.alignment = CENTER
    ws.row_dimensions[row].height = 24
    return row + 1


def _kpi_sheet(wb, title, pairs):
    """ورقة ملخّص بسيطة: بند / قيمة."""
    ws = _new_sheet(wb, title)
    row = _title_block(ws, title, 2)
    row = _header_row(ws, ['البيان', 'القيمة'], row)
    start = row
    for n, (k, v, fmt) in enumerate(pairs):
        zebra = (n % 2 == 1)
        c1 = ws.cell(row=row, column=1, value=k)
        c2 = ws.cell(row=row, column=2, value=v)
        c1.font = Font(name=FONT_NAME, size=11, bold=True)
        c2.font = Font(name=FONT_NAME, size=11)
        for c in (c1, c2):
            c.border = BORDER
            c.alignment = CENTER
            if zebra:
                c.fill = PatternFill('solid', fgColor=C_ZEBRA)
        if fmt:
            c2.number_format = fmt
        ws.row_dimensions[row].height = 20
        row += 1
    _finish(ws, [38, 24], start - 1, row - 1)
    return ws


def _send(wb, filename):
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename,
    )


def _stamp():
    return datetime.now().strftime('%Y%m%d_%H%M')


# ═══════════════════════════ العملاء ═══════════════════════════
@excel_bp.route('/export/customers.xlsx')
@login_required
def export_customers():
    if not _need_openpyxl():
        return redirect(url_for('customers'))

    q = (request.args.get('q') or '').strip()
    query = Customer.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(Customer.name.contains(q), Customer.phone.contains(q)))
    items = query.order_by(Customer.name.asc()).all()

    headers = ['م', 'الكود', 'اسم العميل', 'الهاتف', 'البريد الإلكتروني',
               'العنوان', 'حد الائتمان', 'الرصيد', 'الحالة', 'تاريخ الإضافة']
    widths = [6, 14, 34, 16, 26, 30, 15, 15, 16, 16]

    wb = openpyxl.Workbook()
    ws = _new_sheet(wb, 'العملاء', first=True)
    meta = ['عدد العملاء: %d' % len(items)]
    if q:
        meta.append('بحث: %s' % q)
    row = _title_block(ws, 'قائمة العملاء', len(headers), meta)
    hrow = row
    row = _header_row(ws, headers, row)

    data, total_bal, total_limit, over = [], 0.0, 0.0, 0
    for n, c in enumerate(items, start=1):
        bal = float(c.balance or 0)
        limit = float(c.credit_limit or 0)
        total_bal += bal
        total_limit += limit
        if limit and bal > limit:
            over += 1
        data.append([
            n, c.code or '—', c.name, c.phone or '—', c.email or '—',
            (c.address or '—'), limit, bal,
            ('تجاوز حد الائتمان' if (limit and bal > limit) else ('مدين' if bal > 0 else ('دائن' if bal < 0 else 'مسدّد'))),
            c.created_at.strftime('%Y-%m-%d') if getattr(c, 'created_at', None) else '—',
        ])

    fmts = {7: MONEY, 8: MONEY}
    aligns = {3: RIGHT, 5: RIGHT, 6: RIGHT}
    if data:
        last = _write_rows(ws, data, row, fmts, aligns)
        last = _total_row(ws, {1: 'الإجمالي', 7: total_limit, 8: total_bal},
                          last, len(headers), {7: MONEY, 8: MONEY}) - 1
    else:
        last = _empty_note(ws, row, len(headers), 'لا يوجد عملاء') - 1
    _finish(ws, widths, hrow, last)

    _kpi_sheet(wb, 'ملخص العملاء', [
        ('عدد العملاء', len(items), None),
        ('إجمالي المديونية (أرصدة موجبة)', sum(float(c.balance or 0) for c in items if (c.balance or 0) > 0), MONEY),
        ('إجمالي الأرصدة الدائنة', sum(float(c.balance or 0) for c in items if (c.balance or 0) < 0), MONEY),
        ('صافي الأرصدة', total_bal, MONEY),
        ('إجمالي حدود الائتمان', total_limit, MONEY),
        ('عملاء تجاوزوا حد الائتمان', over, None),
    ])

    return _send(wb, 'العملاء_%s.xlsx' % _stamp())


# ═══════════════════════════ الموردون ═══════════════════════════
@excel_bp.route('/export/suppliers.xlsx')
@login_required
def export_suppliers():
    if not _need_openpyxl():
        return redirect(url_for('suppliers'))

    q = (request.args.get('q') or '').strip()
    query = Supplier.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(Supplier.name.contains(q), Supplier.phone.contains(q)))
    items = query.order_by(Supplier.name.asc()).all()

    headers = ['م', 'الكود', 'اسم المورد', 'الهاتف', 'البريد الإلكتروني', 'العنوان', 'الرصيد', 'الحالة']
    widths = [6, 14, 34, 16, 26, 34, 16, 16]

    wb = openpyxl.Workbook()
    ws = _new_sheet(wb, 'الموردون', first=True)
    meta = ['عدد الموردين: %d' % len(items)]
    if q:
        meta.append('بحث: %s' % q)
    row = _title_block(ws, 'قائمة الموردين', len(headers), meta)
    hrow = row
    row = _header_row(ws, headers, row)

    data, total_bal = [], 0.0
    for n, s in enumerate(items, start=1):
        bal = float(s.balance or 0)
        total_bal += bal
        data.append([
            n, s.code or '—', s.name, s.phone or '—', s.email or '—', (s.address or '—'),
            bal, ('مستحق للمورد' if bal > 0 else ('له رصيد لدينا' if bal < 0 else 'مسدّد')),
        ])

    if data:
        last = _write_rows(ws, data, row, {7: MONEY}, {3: RIGHT, 5: RIGHT, 6: RIGHT})
        last = _total_row(ws, {1: 'الإجمالي', 7: total_bal}, last, len(headers), {7: MONEY}) - 1
    else:
        last = _empty_note(ws, row, len(headers), 'لا يوجد موردون') - 1
    _finish(ws, widths, hrow, last)

    _kpi_sheet(wb, 'ملخص الموردين', [
        ('عدد الموردين', len(items), None),
        ('إجمالي المستحق للموردين', sum(float(s.balance or 0) for s in items if (s.balance or 0) > 0), MONEY),
        ('إجمالي الأرصدة المدينة لدى الموردين', sum(float(s.balance or 0) for s in items if (s.balance or 0) < 0), MONEY),
        ('صافي الأرصدة', total_bal, MONEY),
    ])

    return _send(wb, 'الموردون_%s.xlsx' % _stamp())


# ═══════════════════════════ الأصناف ═══════════════════════════
@excel_bp.route('/export/products.xlsx')
@login_required
def export_products():
    if not _need_openpyxl():
        return redirect(url_for('products'))

    q = (request.args.get('q') or '').strip()
    query = Product.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(Product.name.contains(q), Product.code.contains(q)))
    items = query.order_by(Product.name.asc()).all()

    # أرصدة كل صنف في كل المخازن — استعلام واحد بدل استعلام لكل صنف
    balances = dict(
        db.session.query(Stock.product_id, db.func.coalesce(db.func.sum(Stock.quantity), 0))
        .group_by(Stock.product_id).all()
    )

    headers = ['م', 'الكود', 'اسم الصنف', 'الباركود', 'التصنيف', 'الوحدة',
               'سعر التكلفة', 'سعر البيع', 'هامش الربح', 'الحد الأدنى',
               'الرصيد الحالي', 'قيمة التكلفة', 'قيمة البيع']
    widths = [6, 14, 36, 18, 20, 10, 14, 14, 12, 12, 14, 16, 16]

    wb = openpyxl.Workbook()
    ws = _new_sheet(wb, 'الأصناف', first=True)
    meta = ['عدد الأصناف: %d' % len(items)]
    if q:
        meta.append('بحث: %s' % q)
    row = _title_block(ws, 'قائمة الأصناف', len(headers), meta)
    hrow = row
    row = _header_row(ws, headers, row)

    data = []
    t_qty = t_cost = t_sell = 0.0
    by_cat = {}
    for n, p in enumerate(items, start=1):
        qty = float(balances.get(p.id, 0) or 0)
        cost = float(p.cost_price or 0)
        sell = float(p.sell_price or 0)
        margin = ((sell - cost) / cost) if cost else 0
        cat = p.category.name if getattr(p, 'category', None) else '—'
        val_cost = qty * cost
        val_sell = qty * sell
        t_qty += qty
        t_cost += val_cost
        t_sell += val_sell
        agg = by_cat.setdefault(cat, {'n': 0, 'qty': 0.0, 'cost': 0.0, 'sell': 0.0})
        agg['n'] += 1
        agg['qty'] += qty
        agg['cost'] += val_cost
        agg['sell'] += val_sell
        data.append([
            n, p.code or '—', p.name, p.barcode or '—', cat, p.unit or '—',
            cost, sell, margin, float(p.min_stock or 0), qty, val_cost, val_sell,
        ])

    fmts = {7: MONEY, 8: MONEY, 9: PCT, 10: QTY, 11: QTY, 12: MONEY, 13: MONEY}
    if data:
        last = _write_rows(ws, data, row, fmts, {3: RIGHT})
        last = _total_row(ws, {1: 'الإجمالي', 11: t_qty, 12: t_cost, 13: t_sell},
                          last, len(headers), {11: QTY, 12: MONEY, 13: MONEY}) - 1
    else:
        last = _empty_note(ws, row, len(headers), 'لا توجد أصناف') - 1
    _finish(ws, widths, hrow, last)

    # ورقة: ملخص حسب التصنيف
    ws2 = _new_sheet(wb, 'ملخص التصنيفات')
    h2 = ['التصنيف', 'عدد الأصناف', 'إجمالي الكمية', 'قيمة التكلفة', 'قيمة البيع', 'الربح المتوقع']
    row2 = _title_block(ws2, 'ملخص الأصناف حسب التصنيف', len(h2))
    hrow2 = row2
    row2 = _header_row(ws2, h2, row2)
    rows2 = [[k, v['n'], v['qty'], v['cost'], v['sell'], v['sell'] - v['cost']]
             for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1]['sell'])]
    f2 = {3: QTY, 4: MONEY, 5: MONEY, 6: MONEY}
    if rows2:
        last2 = _write_rows(ws2, rows2, row2, f2, {1: RIGHT})
        last2 = _total_row(ws2, {1: 'الإجمالي', 2: len(items), 3: t_qty,
                                 4: t_cost, 5: t_sell, 6: t_sell - t_cost},
                           last2, len(h2), f2) - 1
    else:
        last2 = _empty_note(ws2, row2, len(h2)) - 1
    _finish(ws2, [26, 14, 16, 18, 18, 18], hrow2, last2)

    return _send(wb, 'الأصناف_%s.xlsx' % _stamp())


# ═══════════════════════════ جرد المخزن ═══════════════════════════
@excel_bp.route('/export/inventory.xlsx')
@login_required
def export_inventory():
    if not _need_openpyxl():
        return redirect(url_for('inventory'))

    warehouse_id = request.args.get('warehouse_id')
    q = (request.args.get('q') or '').strip()

    query = db.session.query(Stock, Product, Warehouse).join(
        Product, Stock.product_id == Product.id).join(
        Warehouse, Stock.warehouse_id == Warehouse.id)
    if warehouse_id:
        query = query.filter(Stock.warehouse_id == warehouse_id)
    if q:
        query = query.filter(Product.name.contains(q))
    rows_db = query.order_by(Warehouse.name.asc(), Product.name.asc()).all()

    wh_name = 'جميع المخازن'
    if warehouse_id:
        w = Warehouse.query.get(warehouse_id)
        wh_name = w.name if w else wh_name

    headers = ['م', 'المخزن', 'كود الصنف', 'اسم الصنف', 'الباركود', 'التصنيف', 'الوحدة',
               'الكمية', 'الحد الأدنى', 'الحالة', 'سعر التكلفة', 'قيمة التكلفة',
               'سعر البيع', 'قيمة البيع']
    widths = [6, 20, 14, 34, 16, 18, 10, 12, 12, 16, 14, 16, 14, 16]

    wb = openpyxl.Workbook()
    ws = _new_sheet(wb, 'جرد المخزن', first=True)
    meta = ['المخزن: %s' % wh_name, 'عدد السطور: %d' % len(rows_db)]
    if q:
        meta.append('بحث: %s' % q)
    row = _title_block(ws, 'تقرير جرد المخزن', len(headers), meta)
    hrow = row
    row = _header_row(ws, headers, row)

    data = []
    t_qty = t_cost = t_sell = 0.0
    low_rows = []
    by_wh = {}
    for n, (st, p, wh) in enumerate(rows_db, start=1):
        qty = float(st.quantity or 0)
        cost = float(p.cost_price or 0)
        sell = float(p.sell_price or 0)
        minq = float(p.min_stock or 0)
        cat = p.category.name if getattr(p, 'category', None) else '—'
        if qty <= 0:
            status = 'نافد'
        elif minq and qty <= minq:
            status = 'تحت الحد الأدنى'
        else:
            status = 'متوفر'
        vc, vs = qty * cost, qty * sell
        t_qty += qty
        t_cost += vc
        t_sell += vs
        agg = by_wh.setdefault(wh.name, {'n': 0, 'qty': 0.0, 'cost': 0.0, 'sell': 0.0})
        agg['n'] += 1
        agg['qty'] += qty
        agg['cost'] += vc
        agg['sell'] += vs
        line = [n, wh.name, p.code or '—', p.name, p.barcode or '—', cat, p.unit or '—',
                qty, minq, status, cost, vc, sell, vs]
        data.append(line)
        if status != 'متوفر':
            low_rows.append([len(low_rows) + 1, wh.name, p.code or '—', p.name,
                             p.unit or '—', qty, minq, max(0.0, minq - qty), status])

    fmts = {8: QTY, 9: QTY, 11: MONEY, 12: MONEY, 13: MONEY, 14: MONEY}
    if data:
        last = _write_rows(ws, data, row, fmts, {4: RIGHT})
        # تلوين حالة الأصناف الناقصة
        for i in range(len(data)):
            cell = ws.cell(row=row + i, column=10)
            if cell.value == 'متوفر':
                cell.font = Font(name=FONT_NAME, size=10, bold=True, color=C_OK)
            else:
                cell.font = Font(name=FONT_NAME, size=10, bold=True, color=C_BAD)
        last = _total_row(ws, {1: 'الإجمالي', 8: t_qty, 12: t_cost, 14: t_sell},
                          last, len(headers), {8: QTY, 12: MONEY, 14: MONEY}) - 1
    else:
        last = _empty_note(ws, row, len(headers), 'لا توجد أرصدة مطابقة') - 1
    _finish(ws, widths, hrow, last)

    # ورقة: ملخص حسب المخزن
    ws2 = _new_sheet(wb, 'ملخص المخازن')
    h2 = ['المخزن', 'عدد الأصناف', 'إجمالي الكمية', 'قيمة التكلفة', 'قيمة البيع']
    row2 = _title_block(ws2, 'ملخص الجرد حسب المخزن', len(h2))
    hrow2 = row2
    row2 = _header_row(ws2, h2, row2)
    rows2 = [[k, v['n'], v['qty'], v['cost'], v['sell']]
             for k, v in sorted(by_wh.items(), key=lambda kv: -kv[1]['cost'])]
    f2 = {3: QTY, 4: MONEY, 5: MONEY}
    if rows2:
        last2 = _write_rows(ws2, rows2, row2, f2, {1: RIGHT})
        last2 = _total_row(ws2, {1: 'الإجمالي', 2: len(rows_db), 3: t_qty, 4: t_cost, 5: t_sell},
                           last2, len(h2), f2) - 1
    else:
        last2 = _empty_note(ws2, row2, len(h2)) - 1
    _finish(ws2, [26, 14, 16, 18, 18], hrow2, last2)

    # ورقة: أصناف تحت الحد الأدنى
    ws3 = _new_sheet(wb, 'تحت الحد الأدنى')
    h3 = ['م', 'المخزن', 'كود الصنف', 'اسم الصنف', 'الوحدة', 'الرصيد', 'الحد الأدنى', 'النقص', 'الحالة']
    row3 = _title_block(ws3, 'أصناف نافدة أو تحت الحد الأدنى', len(h3),
                        ['عدد الأصناف: %d' % len(low_rows)])
    hrow3 = row3
    row3 = _header_row(ws3, h3, row3)
    f3 = {6: QTY, 7: QTY, 8: QTY}
    if low_rows:
        last3 = _write_rows(ws3, low_rows, row3, f3, {4: RIGHT}) - 1
    else:
        last3 = _empty_note(ws3, row3, len(h3), 'لا توجد أصناف تحت الحد الأدنى — المخزون سليم') - 1
    _finish(ws3, [6, 20, 14, 34, 10, 12, 12, 12, 18], hrow3, last3)

    return _send(wb, 'جرد_المخزن_%s.xlsx' % _stamp())


# ══════════════════ كشف حساب تفصيلي — عميل / مورد ══════════════════
def _statement_workbook(party_label, party, rows, products_summary,
                        grand_qty, grand_total, date_from, date_to, balance):
    headers = ['م', 'التاريخ', 'رقم الفاتورة', 'نوع الحركة', 'الصنف',
               'الوحدة', 'الكمية', 'السعر', 'الإجمالي']
    widths = [6, 14, 18, 16, 36, 10, 12, 14, 16]

    wb = openpyxl.Workbook()
    ws = _new_sheet(wb, 'الحركة التفصيلية', first=True)
    meta = [
        '%s: %s%s' % (party_label, party.name,
                      ('  —  كود: %s' % party.code) if getattr(party, 'code', None) else ''),
        'الفترة من %s إلى %s' % (date_from, date_to),
    ]
    if getattr(party, 'phone', None):
        meta.append('هاتف: %s' % party.phone)
    row = _title_block(ws, 'كشف حساب تفصيلي', len(headers), meta)
    hrow = row
    row = _header_row(ws, headers, row)

    data = []
    running = 0.0
    for n, r in enumerate(rows, start=1):
        running += float(r['total'] or 0)
        d = r['date']
        data.append([
            n,
            d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d or '—'),
            r.get('invoice_number') or '—',
            r.get('type_label') or '—',
            r.get('product') or '—',
            r.get('unit') or '—',
            float(r.get('qty') or 0),
            float(r.get('price') or 0),
            float(r.get('total') or 0),
        ])

    fmts = {7: QTY, 8: MONEY, 9: MONEY}
    if data:
        last = _write_rows(ws, data, row, fmts, {5: RIGHT})
        for i, r in enumerate(rows):
            cell = ws.cell(row=row + i, column=4)
            if r.get('type') == 'return':
                cell.font = Font(name=FONT_NAME, size=10, bold=True, color=C_BAD)
            else:
                cell.font = Font(name=FONT_NAME, size=10, bold=True, color=C_OK)
        last = _total_row(ws, {1: 'الإجمالي', 7: grand_qty, 9: grand_total},
                          last, len(headers), {7: QTY, 9: MONEY}) - 1
    else:
        last = _empty_note(ws, row, len(headers)) - 1
    _finish(ws, widths, hrow, last)

    # ورقة: ملخص الأصناف
    ws2 = _new_sheet(wb, 'ملخص الأصناف')
    h2 = ['م', 'الصنف', 'الوحدة', 'صافي الكمية', 'صافي القيمة', 'نسبة من الإجمالي']
    row2 = _title_block(ws2, 'ملخص الأصناف خلال الفترة', len(h2),
                        ['%s: %s' % (party_label, party.name),
                         'الفترة من %s إلى %s' % (date_from, date_to)])
    hrow2 = row2
    row2 = _header_row(ws2, h2, row2)
    rows2 = []
    for n, (name, agg) in enumerate(products_summary.items(), start=1):
        share = (agg['total'] / grand_total) if grand_total else 0
        rows2.append([n, name, agg.get('unit') or '—', agg['qty'], agg['total'], share])
    f2 = {4: QTY, 5: MONEY, 6: PCT}
    if rows2:
        last2 = _write_rows(ws2, rows2, row2, f2, {2: RIGHT})
        last2 = _total_row(ws2, {1: 'الإجمالي', 4: grand_qty, 5: grand_total, 6: 1 if grand_total else 0},
                           last2, len(h2), f2) - 1
    else:
        last2 = _empty_note(ws2, row2, len(h2)) - 1
    _finish(ws2, [6, 40, 10, 16, 18, 18], hrow2, last2)

    # ورقة: الملخص المالي
    _kpi_sheet(wb, 'الملخص المالي', [
        (party_label, party.name, None),
        ('الفترة', 'من %s إلى %s' % (date_from, date_to), None),
        ('عدد سطور الحركة', len(rows), None),
        ('عدد الأصناف المختلفة', len(products_summary), None),
        ('صافي الكمية', grand_qty, QTY),
        ('صافي قيمة الحركة خلال الفترة', grand_total, MONEY),
        ('الرصيد الحالي (إجمالي)', float(balance or 0), MONEY),
    ])
    return wb


@excel_bp.route('/export/customers/<int:id>/statement-detailed.xlsx')
@login_required
def export_customer_statement_detailed(id):
    if not _need_openpyxl():
        return redirect(url_for('customer_statement_detailed', id=id))
    from collections import defaultdict

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

    summary = defaultdict(lambda: {'qty': 0.0, 'total': 0.0, 'unit': ''})
    for r in rows:
        agg = summary[r['product']]
        agg['unit'] = r['unit']
        agg['qty'] += r['qty'] if r['type'] == 'sale' else -r['qty']
        agg['total'] += r['total']
    summary = dict(sorted(summary.items(), key=lambda kv: -kv[1]['total']))

    grand_total = sum(r['total'] for r in rows)
    grand_qty = sum(r['qty'] if r['type'] == 'sale' else -r['qty'] for r in rows)

    wb = _statement_workbook('العميل', customer, rows, summary, grand_qty,
                             grand_total, date_from, date_to, customer.balance)
    return _send(wb, 'كشف_حساب_%s_%s.xlsx' % (customer.name[:25], _stamp()))


@excel_bp.route('/export/suppliers/<int:id>/statement-detailed.xlsx')
@login_required
def export_supplier_statement_detailed(id):
    if not _need_openpyxl():
        return redirect(url_for('supplier_statement_detailed', id=id))
    from collections import defaultdict

    supplier = Supplier.query.get_or_404(id)
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())

    purchase_rows = db.session.query(PurchaseItem, Purchase).join(
        Purchase, PurchaseItem.purchase_id == Purchase.id).filter(
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

    summary = defaultdict(lambda: {'qty': 0.0, 'total': 0.0, 'unit': ''})
    for r in rows:
        agg = summary[r['product']]
        agg['unit'] = r['unit']
        agg['qty'] += r['qty'] if r['type'] == 'purchase' else -r['qty']
        agg['total'] += r['total']
    summary = dict(sorted(summary.items(), key=lambda kv: -kv[1]['total']))

    grand_total = sum(r['total'] for r in rows)
    grand_qty = sum(r['qty'] if r['type'] == 'purchase' else -r['qty'] for r in rows)

    wb = _statement_workbook('المورد', supplier, rows, summary, grand_qty,
                             grand_total, date_from, date_to, supplier.balance)
    return _send(wb, 'كشف_حساب_مورد_%s_%s.xlsx' % (supplier.name[:25], _stamp()))
