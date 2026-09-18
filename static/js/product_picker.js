/**
 * ErpProductPicker — مكتبة اختيار الأصناف الموحّدة
 * تعمل على: فاتورة مبيعات، فاتورة مشتريات، مرتجع مبيعات، مرتجع مشتريات
 *
 * الاستخدام:
 *   ErpProductPicker.init('.product-search', options)
 *   ErpProductPicker.bindRow(inputEl, options)
 *
 * Options:
 *   getWarehouseId()  → string|null
 *   useCost           → bool  (true = fill cost_price, false = fill sell_price)
 *   requireWarehouse  → bool
 *   onSelect(p, row)  → callback بعد الاختيار
 */
(function (global) {
    'use strict';

    /* ══════════════════════════════════════
       Internal State
    ══════════════════════════════════════ */
    var _cache       = {};          // { cacheKey: [ ...products ] }
    var _allCache    = {};          // { warehouseId: [ ...allProducts ] }  ← cache كل الأصناف
    var _active      = null;        // currently focused input
    var _fetchTimers = {};          // debounce timers per input

    /* ══════════════════════════════════════
       CSS — injected once
    ══════════════════════════════════════ */
    function _injectStyles() {
        if (document.getElementById('erp-pp-styles')) return;
        var s = document.createElement('style');
        s.id  = 'erp-pp-styles';
        s.textContent = [
            /* Wrapper */
            '.erp-pp-wrap{position:relative;}',

            /* Arrow icon inside input */
            '.erp-pp-wrap .erp-pp-arrow{',
            '  position:absolute;left:10px;top:50%;transform:translateY(-50%);',
            '  color:var(--text-muted,#64748b);font-size:10px;pointer-events:none;',
            '  transition:transform .2s;}',
            '.erp-pp-wrap.erp-pp-open .erp-pp-arrow{transform:translateY(-50%) rotate(180deg);}',

            /* Input tweak */
            '.erp-pp-input{padding-left:30px!important;cursor:pointer;transition:border-color .18s;}',
            '.erp-pp-wrap.erp-pp-open .erp-pp-input{border-color:var(--accent,#f59e0b)!important;box-shadow:0 0 0 3px rgba(245,158,11,.1);}',

            /* Dropdown panel */
            '@keyframes erp-pp-in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}',
            '.erp-pp-dropdown{',
            '  display:none;position:fixed;',
            '  min-width:320px;max-height:280px;overflow-y:auto;',
            '  background:var(--secondary,#1e2a3a);',
            '  border:1px solid var(--border,rgba(255,255,255,.1));',
            '  border-radius:12px;',
            '  z-index:99999;',
            '  box-shadow:0 12px 40px rgba(0,0,0,.55);',
            '  scrollbar-width:thin;',
            '  animation:erp-pp-in .18s ease;',
            '}',
            '.erp-pp-dropdown::-webkit-scrollbar{width:4px;}',
            '.erp-pp-dropdown::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:4px;}',

            /* Search box inside dropdown */
            '.erp-pp-search-wrap{',
            '  position:sticky;top:0;z-index:1;',
            '  padding:8px 10px;',
            '  background:var(--secondary,#1e2a3a);',
            '  border-bottom:1px solid var(--border,rgba(255,255,255,.08));',
            '}',
            '.erp-pp-search-box{',
            '  width:100%;padding:7px 32px 7px 10px;',
            '  background:rgba(255,255,255,.06);',
            '  border:1px solid var(--border,rgba(255,255,255,.12));',
            '  border-radius:8px;color:var(--text,#e2e8f0);',
            '  font-size:13px;font-family:inherit;outline:none;direction:rtl;',
            '  transition:border-color .18s,box-shadow .18s;',
            '}',
            '.erp-pp-search-box:focus{',
            '  border-color:var(--accent,#f59e0b);',
            '  box-shadow:0 0 0 3px rgba(245,158,11,.12);',
            '}',
            '.erp-pp-search-wrap .erp-pp-si{',
            '  position:absolute;right:20px;top:50%;transform:translateY(-50%);',
            '  color:var(--text-muted,#94a3b8);font-size:13px;pointer-events:none;',
            '}',

            /* List items */
            '.erp-pp-item{',
            '  display:flex;align-items:center;gap:10px;',
            '  padding:10px 14px;cursor:pointer;',
            '  border-bottom:1px solid rgba(255,255,255,.05);',
            '  transition:background .1s;',
            '  outline:none;',
            '}',
            '.erp-pp-item:last-child{border-bottom:none;}',
            '.erp-pp-item:hover,.erp-pp-item.erp-pp-focused{',
            '  background:rgba(245,158,11,.10);',
            '}',
            '.erp-pp-item.erp-pp-focused{background:rgba(245,158,11,.16);}',

            /* Item parts */
            '.erp-pp-name{',
            '  font-size:13px;font-weight:600;color:var(--text,#e2e8f0);',
            '  flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;',
            '}',
            '.erp-pp-code{font-size:11px;color:var(--text-muted,#94a3b8);flex-shrink:0;}',
            '.erp-pp-stock{',
            '  font-size:11px;padding:2px 8px;border-radius:12px;',
            '  font-weight:700;flex-shrink:0;',
            '}',
            '.erp-pp-stock.in{background:rgba(16,185,129,.15);color:#34d399;}',
            '.erp-pp-stock.out{background:rgba(239,68,68,.12);color:#f87171;}',

            /* States */
            '.erp-pp-empty,.erp-pp-loading,.erp-pp-error{',
            '  padding:18px;text-align:center;',
            '  color:var(--text-muted,#94a3b8);font-size:13px;',
            '}',
            '.erp-pp-loading{color:var(--accent,#f59e0b);}',
            '.erp-pp-error{color:var(--danger,#ef4444);}',

            /* highlight mark */
            '.erp-pp-item mark{',
            '  background:rgba(245,158,11,.35);color:inherit;border-radius:2px;padding:0 1px;',
            '}',
        ].join('\n');
        document.head.appendChild(s);
    }

    /* ══════════════════════════════════════
       DOM helpers
    ══════════════════════════════════════ */
    function _wrap(inp) { return inp.closest('.erp-pp-wrap'); }
    function _dd(inp)   { var w=_wrap(inp); return w && w.querySelector('.erp-pp-dropdown'); }

    function _pos(inp) {
        var dd = _dd(inp); if (!dd) return;
        var r  = inp.getBoundingClientRect();
        var vH = window.innerHeight;
        var spaceBelow = vH - r.bottom;
        var spaceAbove = r.top;
        var ddH = Math.min(280, dd.scrollHeight || 280);

        dd.style.left  = r.left + 'px';
        dd.style.width = Math.max(320, r.width) + 'px';
        dd.style.right = 'auto';

        if (spaceBelow >= ddH || spaceBelow >= spaceAbove) {
            dd.style.top    = (r.bottom + window.scrollY + 3) + 'px';
            dd.style.bottom = 'auto';
        } else {
            dd.style.bottom = (vH - r.top + 3) + 'px';
            dd.style.top    = 'auto';
        }
    }

    /* ══════════════════════════════════════
       Open / Close
    ══════════════════════════════════════ */
    function _open(inp) {
        _active = inp;
        var w  = _wrap(inp); if (!w) return;
        var dd = w.querySelector('.erp-pp-dropdown');
        if (!dd) return;

        // close all others
        document.querySelectorAll('.erp-pp-dropdown').forEach(function(d) {
            if (d !== dd) { d.style.display='none'; var pw=d.closest('.erp-pp-wrap'); if(pw) pw.classList.remove('erp-pp-open'); }
        });

        dd.style.display = 'block';
        w.classList.add('erp-pp-open');
        _pos(inp);

        // focus the internal search box and clear it to show all products
        var sb = dd.querySelector('.erp-pp-search-box');
        if (sb) {
            sb.value = '';   // ← دائماً نبدأ بنص فارغ لعرض كل الأصناف
            setTimeout(function(){ sb.focus(); }, 30);
        }

        // جلب كل الأصناف (q فارغ)
        _fetch(inp, '', dd);
    }

    function _close(inp) {
        var w  = _wrap(inp); if (!w) return;
        var dd = w.querySelector('.erp-pp-dropdown');
        if (dd) dd.style.display = 'none';
        w.classList.remove('erp-pp-open');
    }

    function _closeAll() {
        document.querySelectorAll('.erp-pp-dropdown').forEach(function(d) { d.style.display='none'; });
        document.querySelectorAll('.erp-pp-wrap').forEach(function(w) { w.classList.remove('erp-pp-open'); });
        _active = null;
    }

    /* ══════════════════════════════════════
       Fetch + Render
    ══════════════════════════════════════ */
    function _fetch(inp, q, dd) {
        var opts  = inp._ppOpts || {};
        var whId  = (opts.getWarehouseId && opts.getWarehouseId()) || '';
        var isEmpty = (q === '' || q === null || q === undefined);

        /* ── عند q فارغ: استخدم all-cache أو جلب كل الأصناف ── */
        if (isEmpty) {
            if (_allCache[whId]) { _render(dd, inp, _allCache[whId], ''); return; }
            dd.innerHTML = '<div class="erp-pp-loading"><i class="fas fa-spinner fa-spin"></i> جاري تحميل الأصناف...</div>';
            clearTimeout(_fetchTimers[inp._ppId]);
            _fetchTimers[inp._ppId] = setTimeout(function() {
                var url = '/api/product/search?q=' + (whId ? '&warehouse_id=' + whId : '');
                fetch(url)
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        _allCache[whId] = data;   // احفظ في all-cache
                        _render(dd, inp, data, '');
                    })
                    .catch(function() {
                        dd.innerHTML = '<div class="erp-pp-error"><i class="fas fa-exclamation-circle"></i> خطأ في تحميل الأصناف</div>';
                    });
            }, 0);
            return;
        }

        /* ── عند وجود نص: بحث عادي مع cache ── */
        var key = q + '|' + whId;
        if (_cache[key]) { _render(dd, inp, _cache[key], q); return; }

        dd.innerHTML = '<div class="erp-pp-loading"><i class="fas fa-spinner fa-spin"></i> جاري البحث...</div>';

        var url = '/api/product/search?q=' + encodeURIComponent(q) + (whId ? '&warehouse_id=' + whId : '');

        clearTimeout(_fetchTimers[inp._ppId]);
        _fetchTimers[inp._ppId] = setTimeout(function() {
            fetch(url)
                .then(function(r) { return r.json(); })
                .then(function(data) { _cache[key] = data; _render(dd, inp, data, q); })
                .catch(function() {
                    dd.innerHTML = '<div class="erp-pp-error"><i class="fas fa-exclamation-circle"></i> خطأ في تحميل الأصناف</div>';
                });
        }, 180);
    }

    function _esc(s) { return String(s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

    function _highlight(text, q) {
        if (!q) return _esc(text);
        try {
            var re = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + ')', 'gi');
            return _esc(text).replace(re, '<mark>$1</mark>');
        } catch(e) { return _esc(text); }
    }

    function _render(dd, inp, data, q) {
        if (!data || !data.length) {
            dd.innerHTML = '<div class="erp-pp-search-wrap" style="position:relative;">' +
                '<i class="fas fa-search erp-pp-si"></i>' +
                '<input class="erp-pp-search-box" placeholder="ابحث عن صنف..." value="' + _esc(q) + '" autocomplete="off">' +
                '</div>' +
                '<div class="erp-pp-empty"><i class="fas fa-box-open" style="font-size:22px;display:block;margin-bottom:6px;opacity:.4;"></i>لا توجد أصناف</div>';
            _bindSearchBox(dd, inp);
            return;
        }

        var rows = data.map(function(p, i) {
            var inStock = (p.qty > 0);
            var qt  = inStock ? (p.qty + ' ' + _esc(p.unit||'')) : 'غير متاح';
            var qcl = inStock ? 'in' : 'out';
            /* أول عنصر يكون محدداً تلقائياً */
            var focusedClass = (i === 0) ? ' erp-pp-focused' : '';
            return '<div class="erp-pp-item' + focusedClass + '" tabindex="-1" role="option"' +
                ' data-idx="' + i + '"' +
                ' data-id="'    + _esc(p.id)   + '"' +
                ' data-name="'  + _esc(p.name) + '"' +
                ' data-price="' + (p.price||0) + '"' +
                ' data-cost="'  + (p.cost||0)  + '"' +
                ' data-qty="'   + (p.qty||0)   + '"' +
                ' data-unit="'  + _esc(p.unit||'') + '"' +
                ' data-code="'  + _esc(p.code||'') + '"' +
                '>' +
                '<span class="erp-pp-name">'  + _highlight(p.name, q) + '</span>' +
                '<span class="erp-pp-code">'  + _esc(p.code||'') + '</span>' +
                '<span class="erp-pp-stock ' + qcl + '">' + qt + '</span>' +
                '</div>';
        }).join('');

        dd.innerHTML =
            '<div class="erp-pp-search-wrap" style="position:relative;">' +
            '<i class="fas fa-search erp-pp-si"></i>' +
            '<input class="erp-pp-search-box" placeholder="ابحث عن صنف..." value="' + _esc(q) + '" autocomplete="off">' +
            '</div>' +
            rows;

        _bindSearchBox(dd, inp);
        _bindItems(dd, inp);
        _pos(inp);
    }

    /* ══════════════════════════════════════
       Bind search box inside dropdown
    ══════════════════════════════════════ */
    function _bindSearchBox(dd, inp) {
        var sb = dd.querySelector('.erp-pp-search-box');
        if (!sb) return;

        sb.addEventListener('input', function() {
            var q    = this.value.trim();
            var whId = (inp._ppOpts&&inp._ppOpts.getWarehouseId&&inp._ppOpts.getWarehouseId())||'';

            if (q === '') {
                /* مسح النص → عرض كل الأصناف مباشرة من cache إن وُجد */
                if (_allCache[whId]) { _render(dd, inp, _allCache[whId], ''); return; }
            } else {
                /* بحث عادي: احذف cache البحث لإجبار fetch جديد */
                delete _cache[q + '|' + whId];
            }
            _fetch(inp, q, dd);
        });

        sb.addEventListener('keydown', function(e) {
            _keyNav(e, dd, inp);
        });
    }

    /* ══════════════════════════════════════
       Keyboard navigation
    ══════════════════════════════════════ */
    function _keyNav(e, dd, inp) {
        var items = Array.from(dd.querySelectorAll('.erp-pp-item'));
        if (!items.length) return;

        var focused = dd.querySelector('.erp-pp-item.erp-pp-focused');
        var idx     = focused ? items.indexOf(focused) : -1;

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (focused) focused.classList.remove('erp-pp-focused');
            idx = (idx + 1) % items.length;
            items[idx].classList.add('erp-pp-focused');
            items[idx].scrollIntoView({ block:'nearest' });

        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (focused) focused.classList.remove('erp-pp-focused');
            idx = (idx - 1 + items.length) % items.length;
            items[idx].classList.add('erp-pp-focused');
            items[idx].scrollIntoView({ block:'nearest' });

        } else if (e.key === 'Enter') {
            e.preventDefault();
            var sel = focused || items[0];
            if (sel) _select(sel, inp);

        } else if (e.key === 'Escape') {
            e.preventDefault();
            _close(inp);
            inp.focus();

        } else if (e.key === 'Tab') {
            _close(inp);
        }
    }

    /* ══════════════════════════════════════
       Bind click on items
    ══════════════════════════════════════ */
    function _bindItems(dd, inp) {
        dd.querySelectorAll('.erp-pp-item').forEach(function(el) {
            el.addEventListener('mousedown', function(e) { e.preventDefault(); }); // prevent blur
            el.addEventListener('click', function() { _select(this, inp); });
            el.addEventListener('keydown', function(e) { _keyNav(e, dd, inp); });
        });
    }

    /* ══════════════════════════════════════
       Select a product
    ══════════════════════════════════════ */
    function _select(el, inp) {
        var w    = _wrap(inp); if (!w) return;
        var pid  = w.querySelector('.pid');
        var row  = w.closest('tr');
        var opts = inp._ppOpts || {};

        // fill main input
        inp.value = el.dataset.name;
        if (pid) pid.value = el.dataset.id;

        // fill available stock (sale form)
        var avail = row && row.querySelector('.avail');
        if (avail) avail.value = (el.dataset.qty||'0') + ' ' + (el.dataset.unit||'');

        // fill price
        var priceInp = row && row.querySelector('.price');
        if (priceInp) {
            var usePrice = opts.useCost
                ? parseFloat(el.dataset.cost)
                : parseFloat(el.dataset.price);
            if ((parseFloat(priceInp.value)||0) === 0 && usePrice > 0)
                priceInp.value = usePrice.toFixed(2);
        }

        _close(inp);
        inp.focus();

        // fire user callback
        if (opts.onSelect) {
            opts.onSelect({
                id:    el.dataset.id,
                name:  el.dataset.name,
                price: parseFloat(el.dataset.price)||0,
                cost:  parseFloat(el.dataset.cost)||0,
                qty:   parseFloat(el.dataset.qty)||0,
                unit:  el.dataset.unit||'',
                code:  el.dataset.code||'',
            }, row);
        }

        // trigger calcRow if exists
        if (row) {
            var qtyInp = row.querySelector('.qty');
            if (qtyInp && typeof window.calcRow === 'function') window.calcRow(qtyInp);
        }
    }

    /* ══════════════════════════════════════
       Bind single input
    ══════════════════════════════════════ */
    var _ppCounter = 0;

    function bindRow(inp, opts) {
        if (inp._ppBound) return;
        inp._ppBound = true;
        inp._ppOpts  = opts || {};
        inp._ppId    = 'pp_' + (++_ppCounter);

        _injectStyles();

        /* ── upgrade DOM ── */
        var container = inp.parentElement;
        if (!container.classList.contains('erp-pp-wrap')) {
            container.classList.add('erp-pp-wrap');
        }
        inp.classList.add('erp-pp-input');
        inp.setAttribute('autocomplete', 'off');
        inp.setAttribute('readonly', 'readonly'); // use internal search box

        // arrow icon
        if (!container.querySelector('.erp-pp-arrow')) {
            var arrow = document.createElement('i');
            arrow.className = 'fas fa-chevron-down erp-pp-arrow';
            container.appendChild(arrow);
        }

        // create dropdown if not exists
        var dd = container.querySelector('.erp-pp-dropdown');
        if (!dd) {
            dd = document.createElement('div');
            dd.className = 'erp-pp-dropdown';
            dd.setAttribute('role', 'listbox');
            container.appendChild(dd);
        }

        /* ── Events on main input ── */
        inp.addEventListener('click', function(e) {
            e.stopPropagation();
            if (dd.style.display === 'block') { _close(inp); }
            else { _open(inp); }
        });

        inp.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' || e.key === 'ArrowDown') { e.preventDefault(); _open(inp); }
            if (e.key === 'Escape') { _close(inp); }
        });

        inp.addEventListener('focus', function() {
            // small delay to allow click to fire first
            setTimeout(function() {
                if (dd.style.display !== 'block') _open(inp);
            }, 80);
        });
    }

    /* ══════════════════════════════════════
       Init all matching inputs
    ══════════════════════════════════════ */
    function init(selector, opts) {
        document.querySelectorAll(selector).forEach(function(inp) { bindRow(inp, opts); });
    }

    /* ══════════════════════════════════════
       Global close on outside click / scroll
    ══════════════════════════════════════ */
    document.addEventListener('click', function(e) {
        if (!e.target.closest('.erp-pp-wrap') && !e.target.closest('.erp-pp-dropdown')) _closeAll();
    });

    window.addEventListener('scroll', function() {
        if (_active) _pos(_active);
    }, { passive: true });

    window.addEventListener('resize', function() {
        if (_active) _pos(_active);
    });

    /* ══════════════════════════════════════
       Public API
    ══════════════════════════════════════ */
    var publicApi = {
        init:       init,
        bindRow:    bindRow,
        clearCache: function() { _cache = {}; _allCache = {}; },

        /* ── Aliases للتوافق مع الكود القديم ── */
        bind:       bindRow,                          // ErpProductLinePicker.bind(el, opts)
        bindAll:    init,                             // ErpProductLinePicker.bindAll(sel, opts)
        cacheClear: function() { _cache = {}; _allCache = {}; }, // ErpProductLinePicker.cacheClear()
    };

    global.ErpProductPicker    = publicApi;
    global.ErpProductLinePicker = publicApi;   // ← alias كامل للكود القديم

}(window));
