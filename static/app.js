// Closeout dashboard: vanilla JS + AG Grid Community.
const agGrid = window.agGrid;
if (agGrid?.ModuleRegistry && agGrid?.AllCommunityModule) {
  try { agGrid.ModuleRegistry.registerModules([agGrid.AllCommunityModule]); } catch { /* already registered */ }
}

const $ = (id) => document.getElementById(id);
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// Each browser gets its own demo-month state, so people sharing a hosted demo
// don't see each other's approvals.
const SESSION = (() => {
  const fresh = () => (crypto.randomUUID ? crypto.randomUUID() : `s-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  try {
    let id = localStorage.getItem('closeout-session');
    if (!id) { id = fresh(); localStorage.setItem('closeout-session', id); }
    return id;
  } catch {
    return fresh();
  }
})();

const state = {
  config: null,
  source: 'sample',
  month: null,
  data: null,
  exp: null,
  expKey: null,
  selectedId: null,
  filter: 'all',
  grid: null,
  drawTicks: false,
  dialog: null,
};

// ---------------------------------------------------------------- formatting
const num = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd = (v) => `${v < 0 ? '-' : ''}$${num.format(Math.abs(v || 0))}`;
const ledgerMoney = (v) => (v === null || v === undefined ? '' : v < 0 ? `(${num.format(Math.abs(v))})` : num.format(v));
const asDate = (iso) => new Date(`${iso}T00:00:00`);
const shortDate = (iso) => asDate(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
const longDate = (iso) => asDate(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
const countries = new Intl.DisplayNames(['en'], { type: 'region' });
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

const KIND = {
  DUPLICATE_PAYMENT: 'Duplicate payment',
  UNPAID_INVOICE: 'Overdue invoice',
  PARTIAL_PAYMENT: 'Partly paid invoice',
  REFUND_NOT_RECORDED: 'Refund missing from books',
  CHARGEBACK: 'Chargeback',
  FEE_VARIANCE: 'Fee above your schedule',
  FX_DIFFERENCE: 'Currency conversion',
  UNRECORDED_SALE: 'Sale missing from books',
  MISSING_PAYMENT: 'Payment missing in PayPal',
  MISSING_INVOICE: 'Invoice missing in PayPal',
  AMOUNT_MISMATCH: 'Amount mismatch',
};
const SEVERITY = { high: 'High priority', medium: 'Medium priority', low: 'Low priority' };
const CHANNEL = { web: 'Checkout', invoice: 'Invoice', 'paypal-only': 'PayPal only', transfer: 'To bank' };
const PROVIDER = { gemini: 'Gemini', claude: 'Claude' };
const FACT_LABELS = {
  order: 'Order', customer: 'Customer', customer_email: 'Customer email', order_amount: 'Order amount',
  first_payment_id: 'First payment', first_payment_time: 'First payment at', second_payment_id: 'Second payment',
  second_payment_time: 'Second payment at', minutes_apart: 'Minutes apart', duplicate_amount: 'Duplicate amount',
  fee_on_duplicate: 'PayPal fee on the duplicate', invoice_number: 'Invoice', invoice_amount: 'Invoice amount',
  amount_paid: 'Paid so far', amount_due: 'Still due', issued: 'Issued', due_date: 'Due', close_date: 'Close date',
  days_overdue: 'Days overdue', refund_amount: 'Refund', refund_date: 'Refunded on', refund_id: 'Refund ID',
  status_in_books: 'Status in your books', payment_id: 'Payment ID', chargeback_id: 'Chargeback ID',
  chargeback_date: 'Chargeback date', amount_reversed: 'Amount taken back', dispute_fee: 'Dispute fee',
  total_taken_back: 'Total taken back', buyer_paid: 'Buyer paid', paypal_fee: 'PayPal fee',
  net_in_foreign_currency: 'Net before conversion', converted_to: 'Converted to', expected_net: 'Expected net',
  shortfall: 'Shortfall', payment_currency: 'Payment currency', sale_amount: 'Sale', fee_charged: 'Fee charged',
  expected_fee: 'Expected fee', extra_fee: 'Extra fee', buyer_country: 'Buyer country',
  your_fee_schedule: 'Your fee schedule', matches_international_rate: 'Matches international rate',
  international_fee_rate: 'International surcharge', date: 'Date', payer: 'Payer', payer_email: 'Payer email',
  amount: 'Amount', note_on_payment: 'Note on the payment', order_reference_on_payment: 'Order reference on the payment',
  paypal_payments_found: 'PayPal payments found', amount_in_books: 'Amount in your books', difference: 'Difference',
};

function factLabel(key) {
  return FACT_LABELS[key] || key.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}
function factValue(key, value) {
  const v = String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return longDate(v);
  const stamp = v.match(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC$/);
  if (stamp) return `${longDate(stamp[1])}, ${stamp[2]} UTC`;
  if (key === 'buyer_country' && /^[A-Z]{2}$/.test(v)) {
    try { return countries.of(v); } catch { return v; }
  }
  if (v === 'yes') return 'Yes';
  return v;
}

// --------------------------------------------------------------------- marks
const SVG = {
  tick: (extra = '', style = '') =>
    `<svg class="mark tick-mark ${extra}" viewBox="0 0 20 20" aria-hidden="true" style="${style}"><path d="M4 10.6l4 4L16.4 5.6"/></svg>`,
  ring: (sev) =>
    `<svg class="mark ring-mark ring-${sev}" viewBox="0 0 20 20" aria-hidden="true"><path d="M15.6 6.6C14.3 4.3 11.6 3.2 9 3.8 5.7 4.6 3.8 7.9 4.6 11.1c.8 3.2 4 5.1 7.2 4.3 3-.8 4.9-3.7 4.3-6.7-.2-.9-.6-1.8-1.2-2.5"/></svg>`,
  open: () => '<svg class="mark open-mark" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="6.2" fill="none"/></svg>',
  wait: () =>
    '<svg class="mark wait-mark" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="6.2"/><path d="M10 6.8V10l2.3 1.5"/></svg>',
  transfer: () =>
    '<svg class="mark transfer-mark" viewBox="0 0 20 20" aria-hidden="true"><path d="M3.5 7.5h11M11.5 4.5l3 3-3 3M16.5 12.5h-11M8.5 9.5l-3 3 3 3"/></svg>',
};

// ----------------------------------------------------------------------- api
async function api(path, options = {}) {
  const resp = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-Closeout-Session': SESSION },
  });
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`;
    try { detail = (await resp.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return resp.json();
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });
const where = () => ({ source: state.source, month: state.month });

// ---------------------------------------------------------------- the ledger
function markRenderer(params) {
  const r = params.data;
  if (!r || params.node.rowPinned) return '';
  const delay = state.drawTicks && !reduceMotion ? `--d:${(params.node.rowIndex % 18) * 40}ms` : '';
  if (r.status === 'issue') return r.issue_status !== 'open' ? SVG.tick('', delay) : SVG.ring(r.severity);
  if (r.status === 'matched') return SVG.tick('', delay);
  if (r.status === 'refunded') return SVG.tick('is-faint', delay);
  if (r.status === 'open') return SVG.open();
  if (r.status === 'waiting') return SVG.wait();
  if (r.status === 'transfer') return SVG.transfer();
  return '';
}

function findingText(params) {
  const r = params.data;
  if (!r || params.node?.rowPinned) return '';
  if (r.status === 'issue') {
    const issue = state.data?.issues.find((i) => i.id === r.issue_id);
    const label = (issue && KIND[issue.kind]) || r.issue_title;
    if (r.issue_status === 'resolved') return `${label} (handled)`;
    if (r.issue_status === 'reviewed') return `${label} (reviewed)`;
    return label;
  }
  if (r.status === 'transfer') return 'Transfer to your bank';
  if (r.status === 'refunded') return r.note || 'Refunded';
  if (r.status === 'open') return r.note || 'Not yet due';
  if (r.status === 'waiting') return 'Waiting for PayPal';
  return r.note ? `Matched. ${r.note}` : 'Matched';
}

function passesFilter(r) {
  switch (state.filter) {
    case 'attention': return r.status === 'issue' && r.issue_status === 'open';
    case 'matched': return ['matched', 'refunded', 'open'].includes(r.status) || (r.status === 'issue' && r.issue_status !== 'open');
    case 'transfers': return r.status === 'transfer';
    default: return true;
  }
}

function createLedger() {
  const theme = agGrid.themeQuartz.withParams({
    fontFamily: 'Archivo, ui-sans-serif, system-ui, sans-serif',
    fontSize: 13.5,
    headerFontSize: 12.5,
    headerFontWeight: 620,
    foregroundColor: '#1c2836',
    backgroundColor: '#fbfcf8',
    headerBackgroundColor: '#eef3e9',
    headerTextColor: '#46524d',
    chromeBackgroundColor: '#eef3e9',
    borderColor: '#d5e0ce',
    rowBorder: { color: '#dde7d6' },
    columnBorder: { color: '#e7eee2' },
    accentColor: '#2e6b3f',
    rowHoverColor: 'rgba(46, 107, 63, 0.06)',
    spacing: 6,
  });
  const money = (p) => ledgerMoney(p.value);
  const columnDefs = [
    { headerName: '', colId: 'mark', width: 40, sortable: false, resizable: false, suppressMovable: true, cellRenderer: markRenderer },
    { headerName: 'Date', field: 'day', width: 74, sort: 'asc', valueFormatter: (p) => (p.value ? shortDate(p.value) : '') },
    { headerName: 'Reference', field: 'reference', width: 116, filter: 'agTextColumnFilter' },
    { headerName: 'Customer', field: 'customer', flex: 1, minWidth: 120, filter: 'agTextColumnFilter', tooltipField: 'customer' },
    { headerName: 'Channel', field: 'channel', hide: true, valueFormatter: (p) => CHANNEL[p.value] || p.value || '' },
    { headerName: 'Expected', field: 'expected', type: 'rightAligned', width: 94, valueFormatter: money, filter: 'agNumberColumnFilter' },
    { headerName: 'Received', field: 'received', type: 'rightAligned', width: 94, valueFormatter: money, filter: 'agNumberColumnFilter' },
    { headerName: 'Fee', field: 'fee', type: 'rightAligned', width: 82, valueFormatter: money },
    { headerName: 'Net', field: 'net', type: 'rightAligned', width: 92, valueFormatter: money },
    {
      headerName: 'Difference', field: 'difference', type: 'rightAligned', width: 100, valueFormatter: money,
      filter: 'agNumberColumnFilter',
      cellClassRules: { 'cell-diff-bad': (p) => !p.node.rowPinned && p.value !== null && p.value !== undefined && Math.abs(p.value) >= 0.01 },
    },
    {
      headerName: 'Finding', colId: 'finding', flex: 1.5, minWidth: 170, valueGetter: findingText, filter: 'agTextColumnFilter',
      cellClassRules: { 'cell-finding-issue': (p) => p.data?.status === 'issue' && p.data?.issue_status === 'open' },
      tooltipValueGetter: (p) => p.data?.issue_title || (p.data?.status === 'waiting' && p.data.note) || findingText(p),
    },
    { headerName: 'PayPal transaction IDs', colId: 'txns', hide: true, valueGetter: (p) => (p.data?.txn_ids || []).join(' ') },
  ];
  state.grid = agGrid.createGrid($('grid'), {
    theme,
    columnDefs,
    // On narrower laptop screens, drop Net, then Fee (both still in the CSV export)
    // so the Finding column stays readable without sideways scrolling.
    onGridSizeChanged: (e) => {
      e.api.setColumnsVisible(['net'], e.clientWidth >= 984);
      e.api.setColumnsVisible(['fee'], e.clientWidth >= 892);
    },
    defaultColDef: { sortable: true, resizable: true },
    rowData: [],
    getRowId: (p) => p.data.id,
    rowHeight: 38,
    headerHeight: 38,
    animateRows: true,
    includeHiddenColumnsInQuickFilter: true,
    tooltipShowDelay: 400,
    isExternalFilterPresent: () => state.filter !== 'all',
    doesExternalFilterPass: (node) => passesFilter(node.data),
    rowClassRules: {
      'row-issue': (p) => p.data?.status === 'issue' && p.data?.issue_status === 'open',
      'row-resolved': (p) => p.data?.status === 'issue' && p.data?.issue_status !== 'open',
      'row-transfer': (p) => p.data?.status === 'transfer',
      'row-waiting': (p) => p.data?.status === 'waiting',
      'row-focus': (p) => Boolean(state.selectedId) && p.data?.issue_id === state.selectedId,
    },
    onRowClicked: (e) => { if (e.data?.issue_id) selectIssue(e.data.issue_id, { fromGrid: true }); },
    onFilterChanged: updateTotals,
    overlayNoRowsTemplate: '<span>No rows match this view.</span>',
  });
}

function updateTotals() {
  if (!state.grid) return;
  let count = 0, received = 0, fee = 0, net = 0;
  state.grid.forEachNodeAfterFilter((node) => {
    const r = node.data;
    if (!r) return;
    count += 1;
    received += r.received || 0;
    fee += r.fee || 0;
    net += r.net || 0;
  });
  state.grid.setGridOption('pinnedBottomRowData', [
    { id: 'total', status: 'total', customer: `Total of ${count.toLocaleString()} rows shown`, received, fee, net },
  ]);
  renderFilterSwitch();
}

function refreshRow(rowId) {
  const row = state.data.rows.find((r) => r.id === rowId);
  if (!row || !state.grid) return;
  state.grid.applyTransaction({ update: [row] });
  const node = state.grid.getRowNode(rowId);
  if (node) state.grid.redrawRows({ rowNodes: [node] });
}

// ------------------------------------------------------------------ rendering
function renderSourceSwitch() {
  $('sourceSwitch').innerHTML = state.config.sources
    .map((s) => `<button type="button" role="radio" aria-checked="${s.id === state.source}" data-source="${s.id}"
      ${s.available ? '' : 'disabled title="Add your PayPal sandbox keys to .env to connect"'}>${esc(s.label)}</button>`)
    .join('');
}

function renderFilterSwitch() {
  if (!state.data) return;
  const rows = state.data.rows;
  const counts = {
    attention: rows.filter((r) => r.status === 'issue' && r.issue_status === 'open').length,
    matched: rows.filter((r) => ['matched', 'refunded', 'open'].includes(r.status) || (r.status === 'issue' && r.issue_status !== 'open')).length,
    transfers: rows.filter((r) => r.status === 'transfer').length,
    all: rows.length,
  };
  const labels = { all: 'All', attention: 'Needs attention', matched: 'Matched', transfers: 'Transfers' };
  $('filterSwitch').innerHTML = Object.keys(labels)
    .map((k) => `<button type="button" role="radio" aria-checked="${state.filter === k}" data-filter="${k}">${labels[k]}<span class="count">${counts[k]}</span></button>`)
    .join('');
}

function openIssues() {
  return state.data.issues.filter((i) => i.status === 'open');
}

// PayPal's transaction search can lag new payments by up to 3 hours. Sales it
// hasn't reported yet are shown as waiting rather than flagged as missing.
function waitingNote(d) {
  const n = d.summary.waiting_count || 0;
  if (!n) return '';
  let when = '';
  if (d.meta.data_through) {
    const t = new Date(d.meta.data_through);
    const time = t.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    const sameDay = t.toDateString() === new Date().toDateString();
    when = ` PayPal's records currently run through ${sameDay ? `${time} today` : `${t.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`}.`;
  }
  const one = n === 1;
  return ` ${n} recent ${one ? 'sale is' : 'sales are'} waiting for PayPal to report ${one ? 'it' : 'them'}, which can take up to 3 hours.${when}`;
}

let heroAnim = 0;

function renderHero(animate = false) {
  const run = ++heroAnim;
  const d = state.data;
  const s = d.summary;
  const open = openIssues();
  const month = d.meta.month_name;
  const atRisk = open.reduce((sum, i) => sum + i.amount_at_risk, 0);
  const title = $('heroTitle');
  const waiting = waitingNote(d);
  title.classList.remove('is-error');
  $('monthLabel').textContent = `${d.meta.business_name}, ${d.meta.month_label}`;
  if (!open.length) {
    title.textContent = waiting ? `${month} ties out so far.` : `${month} is ready to close.`;
    $('heroSub').textContent = (d.issues.length
      ? `All ${d.issues.length} items are handled, and the other ${s.matched_count.toLocaleString()} records tie out to PayPal.`
      : `All ${s.matched_count.toLocaleString()} records tie out to PayPal. Nothing needs your review.`) + waiting;
    return;
  }
  const rate = Math.round(s.match_rate);
  $('heroSub').textContent = `${open.length} ${open.length === 1 ? 'item needs' : 'items need'} your review before you close the month. ${usd(atRisk)} is at stake.${waiting}`;
  if (!animate || reduceMotion) {
    title.textContent = `${month} is ${rate}% reconciled.`;
    return;
  }
  const start = performance.now();
  const step = (now) => {
    if (run !== heroAnim) return;
    const t = Math.min(1, (now - start) / 750);
    const eased = 1 - Math.pow(1 - t, 3);
    title.textContent = `${month} is ${Math.round(rate * eased)}% reconciled.`;
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderTally() {
  const s = state.data.summary;
  const items = [
    ['PayPal transactions', s.txn_count.toLocaleString()],
    ['Orders in your books', s.order_count.toLocaleString()],
    ['Sales', usd(s.sales_gross)],
    ['PayPal fees', usd(-s.fees)],
    ['Refunds and chargebacks', usd(-(s.refunds + s.chargebacks))],
    ['Sent to your bank', usd(-s.transfers)],
  ];
  $('tally').innerHTML = items.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('');
}

function aiName() {
  const ai = state.exp?.ai || state.config.ai;
  return PROVIDER[ai?.provider] || 'AI';
}

function renderAi() {
  const summary = $('aiSummary');
  const source = $('aiSource');
  const issues = state.data.issues;
  if (!state.exp) {
    summary.className = 'note-text is-waiting';
    summary.innerHTML = state.config.ai.enabled
      ? `Reviewing ${issues.length} ${issues.length === 1 ? 'item' : 'items'}<span class="dots"></span>`
      : 'Preparing your summary<span class="dots"></span>';
    source.textContent = state.config.ai.enabled
      ? `${aiName()} is writing plain-English explanations. Closeout checks every figure it uses against your data.`
      : '';
    return;
  }
  summary.className = 'note-text';
  summary.textContent = state.exp.summary || '';
  source.title = '';
  if (state.exp.error) {
    const busy = /busy|unavailable|high demand|50[234]|429/i.test(state.exp.error);
    const usedAi = Object.values(state.exp.items || {}).some((i) => i.source === 'ai');
    const what = usedAi ? 'this summary comes' : 'these explanations come';
    source.innerHTML = `${esc(busy ? `${aiName()} is busy right now` : `${aiName()} couldn't respond`)}, so ${what} from Closeout's rules. <button class="btn-link" type="button" data-retry>Try again</button>`;
    source.title = state.exp.error;
  } else if (state.exp.summary_source === 'ai') {
    source.textContent = `Written by ${aiName()} from this month's numbers. Every figure is checked against your PayPal data before it's shown.`;
  } else if (!state.config.ai.enabled) {
    source.textContent = 'Generated from your numbers. Add a Gemini API key to get written explanations.';
  } else {
    source.textContent = 'Generated from your numbers.';
  }
}

function renderList() {
  const issues = state.data.issues;
  const handled = issues.length - openIssues().length;
  $('reviewProgress').textContent = issues.length ? `${handled} of ${issues.length} handled` : '';
  const list = $('issueList');
  if (!issues.length) {
    const waiting = state.data.summary.waiting_count || 0;
    list.innerHTML = waiting
      ? `<li class="all-clear">${SVG.wait()}<span>Nothing to review yet. ${waiting} recent ${waiting === 1 ? 'sale is' : 'sales are'} still waiting for PayPal to report ${waiting === 1 ? 'it' : 'them'}.</span></li>`
      : `<li class="all-clear">${SVG.tick()}<span>Nothing to review. Every record ties out to PayPal.</span></li>`;
    return;
  }
  const stillWaiting = state.data.summary.waiting_count || 0;
  const done = handled === issues.length
    ? `<li class="all-clear">${SVG.tick()}<span>Every item is handled. ${stillWaiting
      ? `${stillWaiting} recent ${stillWaiting === 1 ? 'sale is' : 'sales are'} still waiting for PayPal to report ${stillWaiting === 1 ? 'it' : 'them'}.`
      : `${esc(state.data.meta.month_name)} is ready to close.`}</span></li>`
    : '';
  list.innerHTML = done + issues
    .map((i) => {
      const isDone = i.status !== 'open';
      return `<li><button class="issue-item${isDone ? ' is-done' : ''}" type="button" data-id="${esc(i.id)}">
        ${isDone ? SVG.tick() : SVG.ring(i.severity)}
        <span class="issue-title">${esc(i.title)}</span>
        <span class="amount num">${usd(i.amount_at_risk)}</span>
        <span class="issue-kind">${esc(KIND[i.kind] || i.kind)}, ${isDone ? esc((i.done_summary || 'handled').toLowerCase()) : SEVERITY[i.severity].toLowerCase()}</span>
      </button></li>`;
    })
    .join('');
}

function currentIssue() {
  return state.data?.issues.find((i) => i.id === state.selectedId) || null;
}

function renderDetail() {
  const issue = currentIssue();
  const detail = $('issueDetail');
  if (!issue) {
    detail.hidden = true;
    $('reviewList').hidden = false;
    return;
  }
  $('reviewList').hidden = true;
  detail.hidden = false;
  const exp = state.exp?.items?.[issue.id];
  const recommended = exp?.recommended_action || issue.actions[0]?.type;
  const ordered = [...issue.actions].sort((a, b) => (a.type === recommended ? -1 : b.type === recommended ? 1 : 0));
  const [primary, ...rest] = ordered;
  let sourceNote = '';
  if (exp) sourceNote = exp.source === 'ai' ? `Explained by ${aiName()}. Figures checked against your data.` : 'Rule-based explanation.';
  const explanation = exp
    ? `<p class="explanation">${esc(exp.explanation)}</p>`
    : `<p class="explanation is-waiting">Reading the matched records<span class="dots"></span></p>`;
  const isDone = issue.status !== 'open';
  detail.innerHTML = `
    <div class="detail-top">
      <button class="btn-link" type="button" data-back>Back to the list</button>
      <span class="severity ${issue.severity}">${SVG.ring(issue.severity)}${SEVERITY[issue.severity]}</span>
    </div>
    <h3>${esc(issue.title)}</h3>
    <p class="at-stake"><strong class="num">${usd(issue.amount_at_risk)}</strong> at stake</p>
    ${isDone ? `<p class="done-banner">${SVG.tick()}<span>${esc(issue.done_summary || (issue.status === 'reviewed' ? 'Marked as reviewed' : 'Handled'))}</span></p>` : ''}
    ${explanation}
    <p class="source-note">${sourceNote}</p>
    ${exp?.likely_cause ? `<p class="cause"><span>Likely cause:</span> ${esc(exp.likely_cause)}</p>` : ''}
    <table class="facts">
      <caption>What Closeout matched</caption>
      <tbody>${Object.entries(issue.facts)
        .map(([k, v]) => `<tr><th scope="row">${esc(factLabel(k))}</th><td>${esc(factValue(k, v))}</td></tr>`)
        .join('')}</tbody>
    </table>
    ${isDone || !primary ? '' : `<div class="actions">
      <button class="btn btn-ink" type="button" data-action="${primary.type}">${esc(primary.label)}</button>
      ${rest.length ? `<div class="secondary">${rest.map((a) => `<button class="btn btn-quiet" type="button" data-action="${a.type}">${esc(a.label)}</button>`).join('')}</div>` : ''}
    </div>`}`;
}

function selectIssue(id, { fromGrid = false } = {}) {
  state.selectedId = id;
  renderDetail();
  if (!state.grid) return;
  const issue = currentIssue();
  if (!fromGrid && issue) {
    if (state.filter !== 'all' && state.filter !== 'attention') {
      state.filter = 'all';
      state.grid.onFilterChanged();
    }
    const node = state.grid.getRowNode(issue.row_id);
    if (node) state.grid.ensureNodeVisible(node, 'middle');
  }
  state.grid.redrawRows();
  $('issueDetail').querySelector('h3')?.focus?.();
}

function renderAll(animate = false) {
  renderHero(animate);
  renderTally();
  renderAi();
  renderList();
  renderDetail();
  state.grid.setGridOption('rowData', state.data.rows);
  updateTotals();
  if (animate) {
    $('grid').classList.add('draw-ticks');
    setTimeout(() => {
      state.drawTicks = false;
      $('grid').classList.remove('draw-ticks');
    }, 1600);
  }
}

// ------------------------------------------------------------------ the close
function setBusy(busy) {
  const btn = $('runBtn');
  btn.disabled = busy;
  btn.textContent = busy ? 'Running the close…' : 'Run the close';
}

let closeRun = 0;

async function runClose(refresh = false) {
  const run = ++closeRun;
  heroAnim++;
  setBusy(true);
  state.selectedId = null;
  state.exp = null;
  const title = $('heroTitle');
  title.classList.remove('is-error');
  title.textContent = state.source === 'paypal' ? 'Pulling your PayPal transactions…' : 'Checking the demo month…';
  $('heroSub').textContent = '';
  try {
    const qs = new URLSearchParams({ source: state.source, month: state.month });
    if (refresh) qs.set('refresh', 'true');
    const data = await api(`/api/reconcile?${qs}`);
    if (run !== closeRun) return;
    state.data = data;
  } catch (err) {
    if (run !== closeRun) return;
    title.classList.add('is-error');
    title.textContent = err.message;
    $('heroSub').textContent = state.source === 'paypal'
      ? 'Check your PayPal sandbox keys in .env and that Transaction search is turned on for your app.'
      : '';
    setBusy(false);
    return;
  }
  state.drawTicks = true;
  renderAll(true);
  setBusy(false);
  explain();
}

async function explain() {
  const key = `${state.source}|${state.month}`;
  state.expKey = key;
  renderAi();
  let exp;
  try {
    exp = await post('/api/explain', where());
  } catch (err) {
    exp = { items: {}, summary: '', summary_source: 'rules', error: err.message, ai: state.config.ai };
  }
  if (state.expKey !== key) return;
  state.exp = exp;
  renderAi();
  renderDetail();
}

// -------------------------------------------------------------------- actions
async function openAction(actionType) {
  const issue = currentIssue();
  if (!issue) return;
  let prev;
  try {
    prev = await post('/api/actions/preview', { ...where(), issue_id: issue.id, action: actionType });
  } catch (err) {
    toast(err.message, false);
    return;
  }
  state.dialog = { issue, actionType, prev };
  $('dialogTitle').textContent = prev.title;
  const mode = $('dialogMode');
  mode.textContent = prev.mode;
  mode.classList.toggle('is-live', Boolean(prev.live));
  $('dialogLines').innerHTML = (prev.lines || []).map((l) => `<li>${esc(l)}</li>`).join('');
  $('dialogJournal').innerHTML = prev.journal ? journalTable(prev.journal) : '';
  const wrap = $('dialogMessageWrap');
  const box = $('dialogMessage');
  if (prev.message_limit) {
    wrap.hidden = false;
    box.maxLength = prev.message_limit;
    box.value = (prev.message || '').slice(0, prev.message_limit);
    updateCounter();
  } else {
    wrap.hidden = true;
    box.value = '';
  }
  const apiLine = $('dialogApi');
  apiLine.hidden = !prev.api_call;
  apiLine.innerHTML = prev.api_call ? `PayPal API: <code>${esc(prev.api_call)}</code>` : '';
  $('dialogError').hidden = true;
  const confirm = $('dialogConfirm');
  confirm.disabled = false;
  confirm.textContent = prev.confirm_label;
  $('actionDialog').showModal();
  (prev.message_limit ? box : confirm).focus();
}

function journalTable(entry) {
  const debit = entry.lines.reduce((s, l) => s + Number(l.debit), 0);
  const credit = entry.lines.reduce((s, l) => s + Number(l.credit), 0);
  const cell = (v) => (Number(v) ? num.format(Number(v)) : '');
  return `<table class="journal">
    <caption>${esc(entry.memo)}, ${esc(longDate(entry.date))}</caption>
    <thead><tr><th>Account</th><th class="money">Debit</th><th class="money">Credit</th></tr></thead>
    <tbody>${entry.lines.map((l) => `<tr><td>${esc(l.account)}</td><td class="money">${cell(l.debit)}</td><td class="money">${cell(l.credit)}</td></tr>`).join('')}</tbody>
    <tfoot><tr><td>Total</td><td class="money">${num.format(debit)}</td><td class="money">${num.format(credit)}</td></tr></tfoot>
  </table>`;
}

function updateCounter() {
  const box = $('dialogMessage');
  $('dialogCounter').textContent = `${box.value.length} / ${box.maxLength}`;
}

async function confirmAction() {
  const d = state.dialog;
  if (!d) return;
  const confirm = $('dialogConfirm');
  confirm.disabled = true;
  confirm.textContent = 'Working…';
  try {
    const res = await post('/api/actions/execute', {
      ...where(),
      issue_id: d.issue.id,
      action: d.actionType,
      approved: true,
      message: $('dialogMessageWrap').hidden ? null : $('dialogMessage').value,
    });
    $('actionDialog').close();
    const issue = state.data.issues.find((i) => i.id === d.issue.id);
    issue.status = res.issue_status || issue.status;
    if (res.issue_status && res.issue_status !== 'open') issue.done_summary = res.summary;
    const row = state.data.rows.find((r) => r.id === issue.row_id);
    if (row) row.issue_status = issue.status;
    refreshRow(issue.row_id);
    renderHero();
    renderList();
    renderDetail();
    renderFilterSwitch();
    toast(res.summary);
    loadActivity();
    if (res.open_url) window.open(res.open_url, '_blank', 'noopener');
  } catch (err) {
    const box = $('dialogError');
    box.textContent = err.message;
    box.hidden = false;
    confirm.disabled = false;
    confirm.textContent = d.prev.confirm_label;
  }
}

// ----------------------------------------------------------------- activity
async function loadActivity() {
  let items = [];
  try { items = await api('/api/activity'); } catch { return; }
  const list = $('activityList');
  if (!items.length) return;
  list.innerHTML = items
    .map((e) => {
      const t = new Date(e.at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
      return `<li><time datetime="${esc(e.at)}">${t}</time><span>${esc(e.summary)}</span></li>`;
    })
    .join('');
  $('adjustmentsLink').hidden = !items.some((e) => e.journal);
}

// ------------------------------------------------------------------- toast
let toastTimer;
function toast(text, ok = true) {
  const el = $('toast');
  el.innerHTML = `${ok ? SVG.tick() : ''}<span>${esc(text)}</span>`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

// ---------------------------------------------------------------------- ask
const SUGGESTIONS = [
  'Why is my bank transfer lower than my sales?',
  'Which customers owe me money?',
  'What should I fix first?',
];

async function ask(question) {
  const box = $('answer');
  box.hidden = false;
  box.innerHTML = `<p class="note-text is-waiting">Looking through ${esc(state.data?.meta.month_name || 'the month')}<span class="dots"></span></p>`;
  try {
    const res = await post('/api/ask', { ...where(), question });
    const note =
      res.source === 'ai'
        ? `Answered by ${aiName()} from this month's data. Figures checked against your records.`
        : res.source === 'guardrail'
          ? 'Closeout blocked an answer that used figures not found in your data.'
          : '';
    box.innerHTML = `<p>${esc(res.answer)}</p>${note ? `<p class="note-source">${note}</p>` : ''}`;
  } catch (err) {
    box.innerHTML = `<p>${esc(err.message)}</p>`;
  }
}

// -------------------------------------------------------------------- wiring
function wire() {
  $('runBtn').addEventListener('click', () => runClose(true));
  $('sourceSwitch').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-source]');
    if (!btn || btn.disabled || btn.dataset.source === state.source) return;
    state.source = btn.dataset.source;
    state.month = state.config.sources.find((s) => s.id === state.source).month;
    renderSourceSwitch();
    runClose(false);
  });
  $('filterSwitch').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-filter]');
    if (!btn) return;
    state.filter = btn.dataset.filter;
    state.grid.onFilterChanged();
  });
  let searchTimer;
  $('search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => state.grid.setGridOption('quickFilterText', e.target.value), 120);
  });
  $('exportBtn').addEventListener('click', () => {
    state.grid.exportDataAsCsv({
      fileName: `closeout-ledger-${state.month}.csv`,
      columnKeys: ['day', 'reference', 'customer', 'channel', 'expected', 'received', 'fee', 'net', 'difference', 'finding', 'txns'],
      processCellCallback: (p) => (p.column.getColId() === 'channel' ? CHANNEL[p.value] || p.value : p.value),
      skipPinnedBottom: true,
    });
  });
  $('issueList').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-id]');
    if (btn) selectIssue(btn.dataset.id);
  });
  $('issueDetail').addEventListener('click', (e) => {
    if (e.target.closest('[data-back]')) {
      state.selectedId = null;
      renderDetail();
      state.grid.redrawRows();
      return;
    }
    const btn = e.target.closest('button[data-action]');
    if (btn) openAction(btn.dataset.action);
  });
  $('aiSource').addEventListener('click', (e) => {
    if (!e.target.closest('[data-retry]')) return;
    state.exp = null;
    explain();
  });
  $('dialogConfirm').addEventListener('click', confirmAction);
  $('dialogMessage').addEventListener('input', updateCounter);
  $('actionDialog').addEventListener('close', () => { state.dialog = null; });
  $('askForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = $('askInput').value.trim();
    if (q.length >= 2) ask(q);
  });
  $('suggestions').innerHTML = SUGGESTIONS.map((s) => `<button class="chip" type="button">${esc(s)}</button>`).join('');
  $('suggestions').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    $('askInput').value = chip.textContent;
    ask(chip.textContent);
  });
}

async function init() {
  createLedger();
  wire();
  $('adjustmentsLink').href = `/api/adjustments.csv?session=${encodeURIComponent(SESSION)}`;
  try {
    state.config = await api('/api/config');
  } catch (err) {
    $('heroTitle').textContent = `Closeout couldn't start: ${err.message}`;
    return;
  }
  $('storeLink').hidden = !state.config.store;
  const params = new URLSearchParams(location.search);
  const wanted = params.get('source');
  const paypal = state.config.sources.find((s) => s.id === 'paypal');
  state.source = wanted === 'paypal' && paypal?.available ? 'paypal' : 'sample';
  state.month = state.config.sources.find((s) => s.id === state.source).month;
  renderSourceSwitch();
  await runClose(false);
  loadActivity();
}

init();
