// "Where the month's sales went": the month's PayPal sales split into where the
// money ended up. A tall segmented bar drawn with AG Charts Community, plus a
// names-only legend, a one-line takeaway and a table view.
//
// The four colours are categorical slots from the dataviz reference palette, in an
// order that passes its checks for adjacent pairs on this surface (lightness band,
// chroma, colour-blind separation, normal-vision separation). Two of them sit
// below 3:1 against the page, so every value is also in the table view and the
// tooltip, never in colour alone.

const PARTS = {
  bank: { name: 'Sent to your bank', color: '#2a78d6' },
  still: { name: 'Still in PayPal', color: '#1baf7a' },
  fees: { name: 'PayPal fees', color: '#6250d6' },
  refunds: { name: 'Refunds and chargebacks', color: '#e87ba4' },
};
const SURFACE = '#fbfcf8';
const LABEL_FONT = '600 12.5px Archivo, ui-sans-serif, system-ui, sans-serif';

const num = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd = (cents) => `$${num.format(cents / 100)}`;
const pct = (share) => (share > 0 && share < 0.005 ? '<1%' : `${Math.round(share * 100)}%`);
const $ = (id) => document.getElementById(id);

let chart = null;
let measureCtx = null;

/** Money in cents: sales and where they went. Summary signs: costs are negative. */
export function splitParts(summary) {
  const c = (v) => Math.round((Number(v) || 0) * 100);
  const sales = c(summary.sales_gross);
  const fees = Math.max(0, -c(summary.fees));
  const refunds = Math.max(0, -(c(summary.refunds) + c(summary.chargebacks)));
  const bank = Math.max(0, -c(summary.transfers));
  return { sales, fees, refunds, bank, still: sales - fees - refunds - bank };
}

function textWidth(text) {
  if (!measureCtx) measureCtx = document.createElement('canvas').getContext('2d');
  measureCtx.font = LABEL_FONT;
  return measureCtx.measureText(text).width;
}

function takeaway(p, month) {
  const costs = p.fees + p.refunds;
  if (p.still < 0) {
    return `You sent ${usd(p.bank)} to your bank, more than ${month}'s sales left after fees and refunds. `
      + `The extra ${usd(-p.still)} came from money already in PayPal.`;
  }
  if (!p.bank) {
    return `Nothing has gone to your bank yet. After ${usd(costs)} in fees, refunds and chargebacks, `
      + `${usd(p.still)} of ${month}'s sales is in your PayPal balance.`;
  }
  return `Your bank got ${usd(p.bank)} of ${usd(p.sales)} in sales. Of the other ${usd(p.sales - p.bank)}, `
    + `${usd(p.still)} is still in PayPal and fees, refunds and chargebacks took ${usd(costs)}.`;
}

function renderLegend(items) {
  const legend = $('splitLegend');
  legend.replaceChildren(...items.map(({ key }) => {
    const li = document.createElement('li');
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = PARTS[key].color;
    li.append(swatch, document.createTextNode(PARTS[key].name));
    return li;
  }));
}

function renderTable(items, total) {
  const body = $('splitTableBody');
  body.replaceChildren(...items.map(({ key, cents }) => {
    const tr = document.createElement('tr');
    for (const text of [PARTS[key].name, usd(cents), pct(cents / total)]) {
      const td = document.createElement('td');
      td.textContent = text;
      tr.append(td);
    }
    return tr;
  }));
}

// A sliver too thin to see (a 1% part on a narrow card) is drawn at a minimum
// width, taken from the largest block. Labels, tooltips and the table always use
// the true amounts.
const MIN_BLOCK_PX = 7;

function plotted(items, total, width) {
  const minCents = Math.ceil((MIN_BLOCK_PX / Math.max(width, 1)) * total);
  const out = items.map((i) => ({ ...i, plot: Math.max(i.cents, minCents) }));
  const extra = out.reduce((sum, i) => sum + (i.plot - i.cents), 0);
  const largest = out.reduce((a, b) => (b.plot > a.plot ? b : a), out[0]);
  largest.plot -= extra;
  return out;
}

function chartOptions(items, total) {
  const host = $('splitChart');
  const datum = { k: 'month' };
  for (const { key, cents, plot } of plotted(items, total, host.clientWidth || 360)) {
    datum[key] = plot / 100;
    datum[`${key}Cents`] = cents;
  }
  const totalDollars = total / 100;
  return {
    container: host,
    data: [datum],
    height: 30,
    background: { fill: 'transparent' },
    padding: { top: 0, right: 0, bottom: 0, left: 0 },
    seriesArea: { padding: { top: 0, right: 0, bottom: 0, left: 0 } },
    legend: { enabled: false },
    theme: { params: { fontFamily: 'Archivo, ui-sans-serif, system-ui, sans-serif', foregroundColor: '#1c2836' } },
    series: items.map(({ key }) => ({
      type: 'bar',
      direction: 'horizontal',
      stacked: true,
      xKey: 'k',
      yKey: key,
      yName: PARTS[key].name,
      fill: PARTS[key].color,
      // A 2px gap in the surface colour between blocks (not a border).
      stroke: SURFACE,
      strokeWidth: 2,
      label: {
        enabled: true,
        placement: 'inside-start',
        color: '#ffffff',
        fontFamily: 'Archivo, ui-sans-serif, system-ui, sans-serif',
        fontWeight: 600,
        fontSize: 12.5,
        // Print the share only where it fits with room to spare; the rest lives in
        // the tooltip and the table view.
        formatter: ({ value, datum: row, yKey }) => {
          const label = pct((row?.[`${yKey}Cents`] ?? value * 100) / total);
          const blockWidth = (host.clientWidth || 0) * (value / totalDollars);
          return blockWidth >= textWidth(label) + 18 ? label : '';
        },
      },
      tooltip: {
        renderer: ({ datum: row, yKey }) => {
          const cents = row[`${yKey}Cents`];
          return `<div class="split-tip"><span class="key" style="background:${PARTS[yKey].color}"></span>`
            + `<strong>${usd(cents)}</strong><span>${PARTS[yKey].name}, ${pct(cents / total)}</span></div>`;
        },
      },
    })),
    axes: {
      x: { type: 'category', position: 'left', paddingInner: 0, paddingOuter: 0, label: { enabled: false }, line: { enabled: false }, tick: { enabled: false } },
      y: {
        type: 'number', position: 'bottom', min: 0, max: totalDollars, nice: false,
        label: { enabled: false }, gridLine: { enabled: false }, line: { enabled: false }, tick: { enabled: false },
      },
    },
  };
}

/** Draw or refresh the figure for the current reconciliation. */
export function renderSplit(data, { reduceMotion = false } = {}) {
  const figure = $('split');
  const charts = window.agCharts;
  if (!figure || !charts?.AgCharts) {
    if (figure) figure.hidden = true;
    return;
  }
  const month = data.meta.month_name;
  const p = splitParts(data.summary);
  figure.hidden = false;
  $('splitTitle').textContent = `Where ${month}'s sales went`;

  const empty = p.sales <= 0;
  figure.classList.toggle('is-empty', empty);
  if (empty) {
    $('splitNote').textContent = `PayPal hasn't reported any sales for ${month} yet.`;
    $('splitLegend').replaceChildren();
    $('splitTableBody').replaceChildren();
    if (chart) { chart.destroy(); chart = null; }
    return;
  }

  const order = p.still >= 0 ? ['bank', 'still', 'fees', 'refunds'] : ['bank', 'fees', 'refunds'];
  const items = order.map((key) => ({ key, cents: p[key] })).filter((i) => i.cents > 0);
  const total = items.reduce((sum, i) => sum + i.cents, 0);

  $('splitNote').textContent = takeaway(p, month);
  $('splitChart').setAttribute(
    'aria-label',
    items.map(({ key, cents }) => `${PARTS[key].name} ${usd(cents)} (${pct(cents / total)})`).join('; '),
  );
  renderLegend(items);
  renderTable(items, total);

  const options = chartOptions(items, total);
  if (chart) chart.update(options);
  else chart = charts.AgCharts.create(options);
  // Chart animation is an AG Charts Enterprise feature, so the bar is drawn in
  // left to right with a CSS reveal instead (skipped for reduced motion).
  const host = $('splitChart');
  host.classList.remove('is-drawing');
  if (!reduceMotion) {
    void host.offsetWidth;
    host.classList.add('is-drawing');
  }
}

export function initSplit() {
  const toggle = $('splitTableToggle');
  toggle?.addEventListener('click', () => {
    const table = $('splitTable');
    table.hidden = !table.hidden;
    toggle.setAttribute('aria-expanded', String(!table.hidden));
    toggle.textContent = table.hidden ? 'Show the figures' : 'Hide the figures';
  });
}
