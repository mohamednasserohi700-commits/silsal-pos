/**
 * Barcode input support for invoice forms (sale/purchase).
 * - Scan/enter barcode then press Enter
 * - Adds product line, or increments quantity if already exists
 */
(function () {
  function num(v) {
    var n = parseFloat(v);
    return isNaN(n) ? 0 : n;
  }

  function round3(n) {
    return Math.round((n + Number.EPSILON) * 1000) / 1000;
  }

  function findRowByProductId(tbody, pid) {
    var rows = tbody.querySelectorAll(tbody.dataset.erpRowSelector || 'tr.item-row');
    for (var i = 0; i < rows.length; i++) {
      var rowPid = rows[i].querySelector('.pid');
      if (rowPid && String(rowPid.value || '') === String(pid)) return rows[i];
    }
    return null;
  }

  function findFirstEmptyRow(tbody) {
    var rows = tbody.querySelectorAll(tbody.dataset.erpRowSelector || 'tr.item-row');
    for (var i = 0; i < rows.length; i++) {
      var rowPid = rows[i].querySelector('.pid');
      if (rowPid && !String(rowPid.value || '').trim()) return rows[i];
    }
    return null;
  }

  function setRowProduct(row, p, opts) {
    if (window.ErpProductLinePicker && typeof window.ErpProductLinePicker.applyToRow === 'function') {
      window.ErpProductLinePicker.applyToRow(row, p, opts || {});
      return;
    }
    var search = row.querySelector('.product-search');
    if (search) search.value = p.name || '';
    var pid = row.querySelector('.pid');
    if (pid) pid.value = p.id;
    var price = row.querySelector('.price');
    if (price) price.value = String(opts.useCost ? (p.cost || 0) : (p.price || 0));
    var avail = row.querySelector('.avail') || row.querySelector('.avail-qty');
    if (avail) avail.value = String(p.qty || 0);
    var barcode = row.querySelector('.barcode');
    if (barcode) barcode.value = p.barcode || '';
    var dd = row.querySelector('.product-dropdown');
    if (dd) dd.style.display = 'none';
  }

  async function fetchByBarcode(barcode, warehouseId) {
    var params = new URLSearchParams();
    params.set('barcode', barcode);
    if (warehouseId) params.set('warehouse_id', warehouseId);
    var res = await fetch('/api/product/by_barcode?' + params.toString());
    if (!res.ok) {
      var err = null;
      try {
        err = await res.json();
      } catch (_) {}
      return { ok: false, status: res.status, error: err && err.error ? err.error : 'error' };
    }
    var data = await res.json();
    return { ok: true, data: data };
  }

  async function handleBarcode(barcodeInput, opts) {
    var barcode = String(barcodeInput.value || '').trim();
    if (!barcode) return;

    var tbody = document.getElementById(opts.itemsBodyId || 'itemsBody');
    if (!tbody) return;
    tbody.dataset.erpRowSelector = opts.rowSelector || tbody.dataset.erpRowSelector || 'tr.item-row';

    var statusEl = null;
    if (opts.statusSelector) statusEl = document.querySelector(opts.statusSelector);
    function setStatus(msg, type) {
      if (!statusEl) return;
      if (!msg) {
        statusEl.textContent = '';
        statusEl.style.display = 'none';
        statusEl.classList.remove('is-error', 'is-info');
        return;
      }
      statusEl.textContent = msg;
      statusEl.style.display = 'block';
      statusEl.classList.remove('is-error', 'is-info');
      statusEl.classList.add(type === 'error' ? 'is-error' : 'is-info');
    }

    var wh = opts.getWarehouseId ? opts.getWarehouseId() : '';
    if (opts.requireWarehouse && !wh) {
      if (typeof window.showToast === 'function') window.showToast('اختر المخزن أولاً', 'danger');
      setStatus('اختر المخزن أولاً', 'error');
      barcodeInput.select();
      return;
    }

    barcodeInput.disabled = true;
    try {
      var r = await fetchByBarcode(barcode, wh);
      if (!r.ok) {
        if (r.status === 404) {
          if (typeof window.showToast === 'function') window.showToast('الباركود غير موجود', 'danger');
          setStatus('الباركود غير موجود', 'error');
        } else {
          if (typeof window.showToast === 'function') window.showToast('تعذر جلب الصنف بالباركود', 'danger');
          setStatus('تعذر جلب الصنف بالباركود', 'error');
        }
        barcodeInput.select();
        return;
      }

      var p = r.data;
      setStatus('', 'info');
      var existing = findRowByProductId(tbody, p.id);
      if (existing) {
        var q = existing.querySelector('.qty');
        if (q) {
          var step = num(q.step) || 1;
          q.value = String(round3(num(q.value) + step));
          if (typeof window.calcRow === 'function') window.calcRow(q);
        }
      } else {
        var row = findFirstEmptyRow(tbody);
        if (!row) {
          if (typeof opts.addRow === 'function') {
            opts.addRow();
          } else if (typeof window.addItem === 'function') {
            window.addItem();
          }
          row = tbody.querySelector((opts.rowSelector || tbody.dataset.erpRowSelector || 'tr.item-row') + ':last-child') || findFirstEmptyRow(tbody);
        }
        if (!row) return;

        setRowProduct(row, p, opts);
        var qty = row.querySelector('.qty');
        if (qty) qty.value = qty.value && num(qty.value) > 0 ? qty.value : '1';
        if (typeof window.calcRow === 'function' && qty) window.calcRow(qty);
      }

      barcodeInput.value = '';
      barcodeInput.focus();
    } finally {
      barcodeInput.disabled = false;
    }
  }

  async function handleRowBarcode(rowBarcodeInput, opts) {
    var barcode = String(rowBarcodeInput.value || '').trim();
    if (!barcode) return;
    var row = rowBarcodeInput.closest('tr');
    if (!row) return;
    var tbody = document.getElementById(opts.itemsBodyId || 'itemsBody');
    if (!tbody) return;
    tbody.dataset.erpRowSelector = opts.rowSelector || tbody.dataset.erpRowSelector || 'tr.item-row';

    var wh = opts.getWarehouseId ? opts.getWarehouseId() : '';
    if (opts.requireWarehouse && !wh) {
      if (typeof window.showToast === 'function') window.showToast('اختر المخزن أولاً', 'danger');
      rowBarcodeInput.select();
      return;
    }

    rowBarcodeInput.disabled = true;
    try {
      var r = await fetchByBarcode(barcode, wh);
      if (!r.ok) {
        if (typeof window.showToast === 'function') {
          window.showToast(r.status === 404 ? 'الباركود غير موجود' : 'تعذر جلب الصنف بالباركود', 'danger');
        }
        rowBarcodeInput.select();
        return;
      }
      setRowProduct(row, r.data, opts);
      var qty = row.querySelector('.qty');
      if (qty) qty.value = qty.value && num(qty.value) > 0 ? qty.value : '1';
      if (typeof window.calcRow === 'function' && qty) window.calcRow(qty);
    } finally {
      rowBarcodeInput.disabled = false;
    }
  }

  function isTypingField(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    var tag = (el.tagName || '').toLowerCase();
    if (tag === 'textarea' || tag === 'select') return true;
    if (tag !== 'input') return false;
    var t = (el.getAttribute('type') || 'text').toLowerCase();
    return ['text', 'search', 'email', 'password', 'tel', 'url', 'number', 'date', 'datetime-local', 'month', 'time', 'week'].indexOf(t) !== -1;
  }

  function attachScanListener(barcodeInput, opts) {
    var buf = '';
    var timer = null;
    var lastAt = 0;
    var minLen = opts.minScanLength || 6;
    var idleMs = opts.idleMs || 80;

    function flush() {
      if (timer) clearTimeout(timer);
      timer = null;
      var code = String(buf || '').trim();
      buf = '';
      if (code.length >= minLen) {
        barcodeInput.value = code;
        handleBarcode(barcodeInput, opts);
      }
    }

    document.addEventListener('keydown', function (e) {
      if (e.ctrlKey || e.altKey || e.metaKey) return;

      var target = e.target;
      if (target !== barcodeInput && isTypingField(target)) return;

      // Common scanner suffixes
      if (e.key === 'Enter' || e.key === 'Tab') {
        if (buf) {
          e.preventDefault();
          flush();
        }
        return;
      }

      if (e.key && e.key.length === 1) {
        var now = Date.now();
        // If there's a long pause, start a new buffer
        if (now - lastAt > 250) buf = '';
        lastAt = now;
        buf += e.key;

        if (timer) clearTimeout(timer);
        timer = setTimeout(function () {
          flush();
        }, idleMs);
      }
    }, true);
  }

  window.ErpInvoiceBarcode = {
    bind: function (inputSelector, options) {
      var inp = typeof inputSelector === 'string' ? document.querySelector(inputSelector) : inputSelector;
      if (!inp) return;
      var opts = options || {};

      // Make it work without clicking the field
      if (opts.autoFocus !== false) {
        try { inp.setAttribute('autocomplete', 'off'); } catch (_) {}
        setTimeout(function () { try { inp.focus(); } catch (_) {} }, 50);
      }

      // Still support manual Enter inside the field
      inp.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          handleBarcode(inp, opts);
        }
      });

      // Global scanner listener (auto read / no Enter)
      attachScanListener(inp, opts);
    },
    bindRowBarcodes: function (selector, options) {
      var opts = options || {};
      document.querySelectorAll(selector).forEach(function (inp) {
        if (!inp || inp.dataset.erpRowBarcodeBound === '1') return;
        inp.dataset.erpRowBarcodeBound = '1';
        inp.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') {
            e.preventDefault();
            handleRowBarcode(inp, opts);
          }
        });
        inp.addEventListener('blur', function () {
          if (String(inp.value || '').trim()) handleRowBarcode(inp, opts);
        });
      });
    }
  };
})();

