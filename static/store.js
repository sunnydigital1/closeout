// Closeout sandbox test store: real PayPal sandbox checkouts plus the messy cases.
const $ = (id) => document.getElementById(id);
const num = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd = (v) => `$${num.format(Number(v))}`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const TICK = '<svg class="mark tick-mark" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10.6l4 4L16.4 5.6"/></svg>';

let store = null;

async function api(path, options = {}) {
  const resp = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json' } });
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`;
    try { detail = (await resp.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return resp.json();
}

let toastTimer;
function toast(text) {
  const el = $('toast');
  el.innerHTML = `${TICK}<span>${esc(text)}</span>`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4500);
}

function notice(text) {
  $('notice').hidden = !text;
  $('noticeText').textContent = text || '';
}

// ------------------------------------------------------------------ rendering
function render() {
  $('product').innerHTML = store.products
    .map((p) => `<option value="${p.id}">${esc(p.name)}, ${usd(p.price)}</option>`)
    .join('');

  const orders = store.orders;
  const options = orders.length
    ? orders.map((o) => `<option value="${esc(o.ref)}">${esc(o.ref)}, ${usd(o.amount)}${o.charges > 1 ? ' (charged twice)' : ''}${o.refunded_in_paypal_only ? ' (refunded)' : ''}</option>`).join('')
    : '<option value="">Make a normal sale first</option>';
  for (const id of ['repeatRef', 'refundRef']) {
    const keep = $(id).value;
    $(id).innerHTML = options;
    if (keep && orders.some((o) => o.ref === keep)) $(id).value = keep;
    $(id).disabled = !orders.length;
  }
  $('refundBtn').disabled = !orders.length;

  if (store.invoices.length) {
    $('invoiceArea').innerHTML = `<table class="books">
      <thead><tr><th>Invoice</th><th>Shop</th><th class="money">Amount</th><th>Status</th></tr></thead>
      <tbody>${store.invoices.map((i) => `<tr><td>${esc(i.number)}</td><td>${esc(i.shop)}</td><td class="money">${usd(i.amount)}</td><td>${esc(invoiceStatus(i))}</td></tr>`).join('')}</tbody>
    </table>`;
  }
  $('stepInvoices').classList.toggle('is-done', store.invoices.length > 0);
  $('stepSales').classList.toggle('is-done', orders.length >= 3);
  const mistakes = orders.some((o) => o.charges > 1) && store.offbook.length > 0 && orders.some((o) => o.refunded_in_paypal_only);
  $('stepMistakes').classList.toggle('is-done', mistakes);

  $('booksPath').textContent = store.books_path;
  const rows = [
    ...store.invoices.map((i) => ({ ref: i.number, who: i.shop, amount: i.amount, status: i.kind === 'paid' ? 'paid' : 'invoiced', flag: '' })),
    ...orders.map((o) => ({
      ref: o.ref, who: o.customer, amount: o.amount, status: 'paid',
      flag: [o.charges > 1 ? 'charged twice' : '', o.refunded_in_paypal_only ? 'refunded' : ''].filter(Boolean).join(', '),
    })),
  ];
  $('booksTable').innerHTML = rows.length
    ? `<thead><tr><th>Reference</th><th>Customer</th><th class="money">Amount</th><th>Status</th></tr></thead>
       <tbody>${rows.map((r) => `<tr><td>${esc(r.ref)}</td><td>${esc(r.who)}${r.flag ? `<br><span class="flag">PayPal: ${esc(r.flag)}</span>` : ''}</td><td class="money">${usd(r.amount)}</td><td>${esc(r.status)}</td></tr>`).join('')}</tbody>`
    : '<tbody><tr><td class="empty-cell">Your books are empty. Create the invoices and make a sale.</td></tr></tbody>';

  $('storeLog').innerHTML = store.log.length
    ? store.log.map((e) => `<li><time datetime="${esc(e.at)}">${new Date(e.at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}</time><span>${esc(e.text)}</span></li>`).join('')
    : '<li class="empty">Nothing yet.</li>';
}

function invoiceStatus(i) {
  if (i.kind === 'paid') return 'Paid';
  if (i.kind === 'partial') return `Paid ${usd(i.paid)} of ${usd(i.amount)}`;
  if (i.kind === 'overdue') return `Due ${new Date(`${i.due}T00:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`;
  return 'Not due yet';
}

async function refresh() {
  store = await api('/api/store');
  render();
}

// ------------------------------------------------------------ PayPal buttons
function loadPayPal() {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    const params = new URLSearchParams({ 'client-id': store.client_id, currency: 'USD', intent: 'capture', components: 'buttons' });
    script.src = `https://www.paypal.com/sdk/js?${params}`;
    script.onload = resolve;
    script.onerror = () => reject(new Error("PayPal's checkout script didn't load. Check your internet connection and PAYPAL_CLIENT_ID."));
    document.head.appendChild(script);
  });
}

function mountButtons(containerId, body) {
  const paypal = window.paypal;
  paypal
    .Buttons({
      fundingSource: paypal.FUNDING.PAYPAL,
      style: { layout: 'horizontal', tagline: false, height: 42, shape: 'rect', color: 'gold' },
      createOrder: async () => {
        notice('');
        const res = await api('/api/store/orders', { method: 'POST', body: JSON.stringify(body()) });
        return res.id;
      },
      onApprove: async (data) => {
        const res = await api(`/api/store/orders/${data.orderID}/capture`, { method: 'POST' });
        toast(res.message);
        if (res.warning) notice(res.warning);
        await refresh();
      },
      onCancel: () => toast('Checkout cancelled. Nothing was charged.'),
      onError: (err) => notice(err?.message || 'PayPal checkout failed. Try again.'),
    })
    .render(`#${containerId}`);
}

// --------------------------------------------------------------------- wiring
async function init() {
  try {
    await refresh();
  } catch (err) {
    document.querySelector('.hero h1').textContent = "The test store can't start yet.";
    document.querySelector('.hero-sub').textContent = err.message;
    document.querySelector('.store-layout').hidden = true;
    return;
  }
  $('invoiceBtn')?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    e.target.textContent = 'Creating invoices in PayPal…';
    try {
      await api('/api/store/invoices', { method: 'POST' });
      toast('Created four wholesale invoices in PayPal.');
    } catch (err) {
      notice(err.message);
      e.target.disabled = false;
      e.target.textContent = 'Create the invoices';
    }
    await refresh();
  });
  $('refundBtn').addEventListener('click', async (e) => {
    const ref = $('refundRef').value;
    if (!ref) return;
    e.target.disabled = true;
    try {
      const res = await api('/api/store/refund', { method: 'POST', body: JSON.stringify({ ref }) });
      toast(res.message);
    } catch (err) {
      notice(err.message);
    }
    e.target.disabled = false;
    await refresh();
  });
  try {
    await loadPayPal();
    mountButtons('normalButtons', () => ({ scenario: 'normal', product_id: $('product').value, quantity: Number($('qty').value) }));
    mountButtons('repeatButtons', () => ({ scenario: 'repeat', ref: $('repeatRef').value }));
    mountButtons('offbookButtons', () => ({ scenario: 'offbook' }));
  } catch (err) {
    notice(err.message);
  }
}

init();
