/**
 * قوائم اختيار الأصناف في فواتير المبيعات/المشتريات/التحويلات — ظهور واضح وبحث فوري
 */
(function () {
    var openPanel = null;
    var scrollHandler = null;

    function esc(s) {
        if (s == null) return '';
        var d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    function hideOpen() {
        if (openPanel) {
            openPanel.style.display = 'none';
            if (openPanel._erpHost && openPanel.parentElement === document.body) {
                openPanel._erpHost.appendChild(openPanel);
            }
            openPanel = null;
        }
        if (scrollHandler) {
            window.removeEventListener('scroll', scrollHandler, true);
            scrollHandler = null;
        }
    }

    function positionPanel(input, panel) {
        if (!panel._erpHost) panel._erpHost = panel.parentElement;
        if (panel.parentElement !== document.body) {
            document.body.appendChild(panel);
        }
        var r = input.getBoundingClientRect();
        var w = Math.max(r.width, 280);
        var rtl = document.documentElement.getAttribute('dir') === 'rtl';
        panel.style.setProperty('position', 'fixed', 'important');
        panel.style.top = (r.bottom + 4) + 'px';
        panel.style.width = w + 'px';
        panel.style.maxWidth = 'min(96vw, 520px)';
        if (rtl) {
            panel.style.left = 'auto';
            panel.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
        } else {
            panel.style.right = 'auto';
            panel.style.left = Math.max(8, r.left) + 'px';
        }
        panel.style.maxHeight = 'min(320px, 45vh)';
        panel.style.overflowY = 'auto';
        panel.style.display = 'block';
        panel.style.zIndex = '20000';
        panel.style.background = 'var(--card)';
        panel.style.color = 'var(--text)';
        panel.style.border = '2px solid var(--accent)';
        panel.style.borderRadius = '10px';
        panel.style.boxShadow = '0 16px 48px rgba(0,0,0,.35)';
    }

    function num(v) {
        var n = parseFloat(v);
        return isNaN(n) ? 0 : n;
    }

    function round3(n) {
        return Math.round((n + Number.EPSILON) * 1000) / 1000;
    }

    function getRowSelector(row) {
        var tbody = row && row.parentElement;
        return (tbody && tbody.dataset && tbody.dataset.erpRowSelector) || 'tr.item-row';
    }

    function findDuplicateRow(row, productId) {
        if (!row || !row.parentElement) return null;
        var rows = row.parentElement.querySelectorAll(getRowSelector(row));
        for (var i = 0; i < rows.length; i++) {
            if (rows[i] === row) continue;
            var pid = rows[i].querySelector('.pid') || rows[i].querySelector('.product-id');
            if (pid && String(pid.value || '') === String(productId)) return rows[i];
        }
        return null;
    }

    function incrementRowQty(row) {
        var qty = row.querySelector('.qty');
        if (!qty) return;
        var step = num(qty.step) || 1;
        qty.value = String(round3(num(qty.value) + step));
        if (typeof window.calcRow === 'function') window.calcRow(qty);
    }

    function clearRowProduct(row) {
        if (!row) return;
        var search = row.querySelector('.product-search');
        if (search) search.value = '';
        var pid = row.querySelector('.pid') || row.querySelector('.product-id');
        if (pid) pid.value = '';
        var price = row.querySelector('.price');
        if (price) price.value = '0';
        var avail = row.querySelector('.avail') || row.querySelector('.avail-qty');
        if (avail) avail.value = '';
        var barcode = row.querySelector('.barcode');
        if (barcode) barcode.value = '';
        var code = row.querySelector('.product-code');
        if (code) code.value = '';
        var rowTotal = row.querySelector('.row-total');
        if (rowTotal) rowTotal.value = '';
        var qty = row.querySelector('.qty');
        if (qty && (!qty.value || num(qty.value) <= 0)) qty.value = '1';
        if (qty && typeof window.calcRow === 'function') window.calcRow(qty);
    }

    function applyProductToRow(row, p, opts) {
        if (!row || !p) return;
        var duplicate = opts && opts.mergeDuplicates !== false ? findDuplicateRow(row, p.id) : null;
        if (duplicate) {
            incrementRowQty(duplicate);
            clearRowProduct(row);
            if (typeof window.showToast === 'function') {
                window.showToast('الصنف موجود بالفعل وتم دمج الكمية', 'info');
            }
            return;
        }

        var search = row.querySelector('.product-search');
        if (search) search.value = p.name || '';
        var pid = row.querySelector('.pid') || row.querySelector('.product-id');
        if (pid) pid.value = p.id || '';
        var price = row.querySelector('.price');
        var useCost = !!(opts && opts.useCost);
        if (price) price.value = String(useCost ? (p.cost || 0) : (p.price || 0));
        var avail = row.querySelector('.avail') || row.querySelector('.avail-qty');
        if (avail) avail.value = String(p.qty != null ? p.qty : (p.stock || 0));
        var barcode = row.querySelector('.barcode');
        if (barcode) barcode.value = p.barcode || '';
        var code = row.querySelector('.product-code');
        if (code) code.value = p.code || '';
        var qty = row.querySelector('.qty');
        // Quantity rule:
        // - normal units: integer quantity (step 1, min 1)
        // - kilo units: allow half kilo steps (step 0.5, min 0.5)
        if (qty) {
            var unit = String(p.unit || '').trim();
            var isKilo = /كيلو|kg|kilo/i.test(unit);
            row.dataset.qtyMode = isKilo ? 'half' : 'int';
            if (isKilo) {
                qty.step = '0.5';
                qty.min = '0.5';
                if (!qty.value || num(qty.value) <= 0) qty.value = '1';
            } else {
                qty.step = '1';
                qty.min = '1';
                if (!qty.value || num(qty.value) <= 0) qty.value = '1';
                qty.value = String(Math.max(1, Math.round(num(qty.value))));
            }
        }
        if (qty && row.querySelector('.row-total') && typeof window.calcRow === 'function') {
            window.calcRow(qty);
        }
    }

    function fetchUrl(q, warehouseId, limit) {
        var params = new URLSearchParams();
        params.set('q', q || '');
        params.set('limit', String(limit || 20));
        if (warehouseId) params.set('warehouse_id', warehouseId);
        return '/products/search?' + params.toString();
    }

    function renderItems(products, panel, input, row, opts) {
        var useCost = opts.useCost;
        if (!products.length) {
            panel.innerHTML = '<div class="erp-pl-empty" style="padding:14px;text-align:center;color:var(--text-muted);font-size:13px;">لا توجد أصناف</div>';
            return;
        }
        panel.innerHTML = products.map(function (p) {
            var price = useCost ? p.cost : p.price;
            var extra = useCost
                ? ('<span style="color:var(--info);">تكلفة: ' + esc(price) + '</span>')
                : ('<span style="color:var(--accent);">بيع: ' + esc(price) + '</span> <span style="color:var(--success);margin-right:8px;">متاح: ' + esc(p.qty) + ' ' + esc(p.unit || '') + '</span>');
            var dn = String(p.name).replace(/"/g, '&quot;');
            return '<div class="erp-pl-item" tabindex="0" data-id="' + p.id + '" data-name="' + dn + '" data-code="' + esc(p.code || '') + '" data-barcode="' + esc(p.barcode || '') + '" data-price="' + price + '" data-qty="' + (p.qty != null ? p.qty : (p.stock || 0)) + '" ' +
                'style="padding:12px 14px;cursor:pointer;font-size:13px;border-bottom:1px solid var(--border);text-align:right;line-height:1.45;color:var(--text);">' +
                '<strong style="color:var(--text);">' + esc(p.name) + '</strong> <span style="color:var(--text-muted);font-size:12px;">' + esc(p.code) + '</span>' +
                '<div style="font-size:11px;margin-top:6px;">' + extra + '</div></div>';
        }).join('');

        var items = panel.querySelectorAll('.erp-pl-item');
        items.forEach(function (item) {
            function pickCurrent() {
                applyProductToRow(row, {
                    id: item.getAttribute('data-id'),
                    name: item.getAttribute('data-name'),
                    code: item.getAttribute('data-code'),
                    barcode: item.getAttribute('data-barcode'),
                    price: item.getAttribute('data-price'),
                    qty: item.getAttribute('data-qty')
                }, opts || {});
                panel.dataset.activeIndex = '-1';
                hideOpen();
            }
            item.addEventListener('mousedown', function (e) { e.preventDefault(); pickCurrent(); });
            item.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); pickCurrent(); } });
        });
    }

    function highlightActive(panel, index) {
        var items = panel.querySelectorAll('.erp-pl-item');
        if (!items.length) return;
        var clamped = Math.max(0, Math.min(index, items.length - 1));
        panel.dataset.activeIndex = String(clamped);
        items.forEach(function (it, i) {
            it.style.background = i === clamped ? 'rgba(59,130,246,.14)' : 'transparent';
        });
        try { items[clamped].scrollIntoView({ block: 'nearest' }); } catch (_) {}
    }

    function getActiveItem(panel) {
        var items = panel.querySelectorAll('.erp-pl-item');
        if (!items.length) return null;
        var idx = parseInt(panel.dataset.activeIndex || '-1', 10);
        if (isNaN(idx) || idx < 0 || idx >= items.length) return null;
        return items[idx];
    }

    async function loadAndShow(input, panel, row, opts) {
        var wh = opts.getWarehouseId ? opts.getWarehouseId() : '';
        if (opts.requireWarehouse && !wh) {
            panel.innerHTML = '<div style="padding:14px;color:var(--warning);font-size:13px;text-align:center;">اختر المخزن أولاً</div>';
            positionPanel(input, panel);
            openPanel = panel;
            return;
        }
        var q = (input.value || '').trim();
        panel.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-muted);font-size:13px;">جاري التحميل...</div>';
        positionPanel(input, panel);
        openPanel = panel;
        try {
            var res = await fetch(fetchUrl(q, wh, opts.limit || 20));
            var data = await res.json();
            var products = Array.isArray(data) ? data : (data.items || []);
            renderItems(products, panel, input, row, opts);
            panel.dataset.activeIndex = '-1';
            positionPanel(input, panel);
        } catch (e) {
            panel.innerHTML = '<div style="padding:14px;color:var(--danger);">خطأ في التحميل</div>';
        }
    }

    window.ErpProductLinePicker = {
        bind: function (input, options) {
            options = options || {};
            if (!input || input.dataset.erpPickerBound === '1') return;
            var row = input.closest('tr');
            if (!row) return;
            var panel = row.querySelector('.product-dropdown');
            if (!panel) {
                panel = document.createElement('div');
                panel.className = 'product-dropdown';
                input.parentNode.appendChild(panel);
            }
            input.classList.add('erp-product-search');
            panel.classList.add('erp-pl-panel');
            input.dataset.erpPickerBound = '1';

            input.addEventListener('focus', function () {
                hideOpen();
                loadAndShow(input, panel, row, options);
                scrollHandler = function () { if (openPanel === panel) positionPanel(input, panel); };
                window.addEventListener('scroll', scrollHandler, true);
            });

            var t = null;
            input.addEventListener('input', function () {
                clearTimeout(t);
                t = setTimeout(function () {
                    loadAndShow(input, panel, row, options);
                    if (!scrollHandler) {
                        scrollHandler = function () { if (openPanel === panel) positionPanel(input, panel); };
                        window.addEventListener('scroll', scrollHandler, true);
                    }
                }, 200);
            });

            input.addEventListener('keydown', function (e) {
                if (openPanel !== panel && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
                    e.preventDefault();
                    loadAndShow(input, panel, row, options);
                    return;
                }
                if (openPanel !== panel) return;

                if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                    e.preventDefault();
                    var dir = e.key === 'ArrowDown' ? 1 : -1;
                    var idx = parseInt(panel.dataset.activeIndex || '-1', 10);
                    if (isNaN(idx)) idx = -1;
                    highlightActive(panel, idx + dir);
                    return;
                }

                if (e.key === 'Enter') {
                    var item = getActiveItem(panel);
                    if (item) {
                        e.preventDefault();
                        item.dispatchEvent(new Event('mousedown'));
                    }
                }
            });

            document.addEventListener('click', function (e) {
                if (!input.parentNode.contains(e.target) && panel !== e.target && !panel.contains(e.target)) {
                    panel.style.display = 'none';
                    if (openPanel === panel) openPanel = null;
                }
            });
        },
        bindAll: function (selector, options) {
            document.querySelectorAll(selector).forEach(function (inp) {
                window.ErpProductLinePicker.bind(inp, options);
            });
        },
        applyToRow: applyProductToRow,
        clearRowProduct: clearRowProduct
    };

    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.clear-row-product');
        if (!btn) return;
        var row = btn.closest('tr');
        if (!row) return;
        clearRowProduct(row);
    });
})();