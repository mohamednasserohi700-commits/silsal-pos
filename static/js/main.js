// ProERP - Global JavaScript Helpers

// Auto-dismiss flash alerts after 4 seconds
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.alert').forEach(alert => {
        setTimeout(() => {
            alert.style.transition = 'opacity 0.5s, transform 0.5s';
            alert.style.opacity = '0';
            alert.style.transform = 'translateY(-10px)';
            setTimeout(() => alert.remove(), 500);
        }, 4000);
    });

    // Highlight active nav link based on URL
    const path = window.location.pathname;
    document.querySelectorAll('.nav-link').forEach(link => {
        if (link.getAttribute('href') === path) {
            link.classList.add('active');
        }
    });

    // Auto-format number inputs on blur
    document.querySelectorAll('input[type="number"].currency').forEach(input => {
        input.addEventListener('blur', () => {
            const val = parseFloat(input.value);
            if (!isNaN(val)) input.value = val.toFixed(2);
        });
    });

    // Confirm delete buttons
    document.querySelectorAll('[data-confirm]').forEach(el => {
        el.addEventListener('click', e => {
            if (!confirm(el.dataset.confirm)) e.preventDefault();
        });
    });

    // Table row click-through
    document.querySelectorAll('tr[data-href]').forEach(row => {
        row.classList.add('clickable');
        row.addEventListener('click', () => window.location = row.dataset.href);
    });
});

// Format numbers with commas
function formatNumber(n) {
    return parseFloat(n || 0).toLocaleString('ar-EG', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Show toast notification
function showToast(msg, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `alert alert-${type}`;
    toast.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);z-index:9999;min-width:300px;text-align:center;animation:slideDown 0.3s ease;';
    toast.innerHTML = `<i class="fas fa-${type === 'success' ? 'check-circle' : type === 'warning' ? 'exclamation-triangle' : 'exclamation-circle'}"></i> ${msg}`;
    document.body.appendChild(toast);
    setTimeout(() => { toast.style.opacity='0'; setTimeout(() => toast.remove(), 500); }, 3000);
}

// Loading button state
function setLoading(btn, loading = true) {
    if (loading) {
        btn.dataset.originalText = btn.innerHTML;
        btn.innerHTML = '<span class="spinner"></span> جاري التحميل...';
        btn.disabled = true;
    } else {
        btn.innerHTML = btn.dataset.originalText;
        btn.disabled = false;
    }
}
