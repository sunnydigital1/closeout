// A self-playing, captioned tour of Closeout. It drives the real UI with a
// visible pointer, so the same run works as a walkthrough for judges
// (?tour=demo) and as the demo video (?tour=video, which adds a segment on a
// live PayPal sandbox account). scripts/record_demo.py records it to MP4.
//
// Options: ?tour=demo | video, &speed=2 (faster, for testing), &live=reminder
// (in the live segment, send an invoice reminder instead of refunding a duplicate).

const REPO = 'github.com/sunnydigital1/closeout';
const $ = (id) => document.getElementById(id);
const usd = (v) => `$${new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(v || 0))}`;

const TOUR_CSS = `
.tour-layer { position: fixed; inset: auto; margin: 0; border: 0; padding: 0; background: transparent; overflow: visible; color: inherit; }
.tour-caption {
  left: 50%; bottom: 40px; transform: translate(-50%, 12px);
  width: max-content; max-width: min(1180px, calc(100vw - 48px));
  padding: 16px 26px; border-radius: 14px;
  background: rgba(28, 40, 54, 0.96); color: #fff;
  font: 500 clamp(17px, 1.42vw, 26px)/1.38 var(--font); letter-spacing: -0.003em;
  text-align: center; text-wrap: balance;
  box-shadow: 0 14px 40px rgba(28, 40, 54, 0.28);
  opacity: 0; transition: opacity 0.35s ease, transform 0.35s ease; pointer-events: none;
}
.tour-caption.is-on { opacity: 1; transform: translate(-50%, 0); }
.tour-caption b { font-weight: 700; color: #f7dd4a; }
.tour-cursor { left: 0; top: 0; width: 30px; height: 30px; pointer-events: none; transition: transform 0.75s cubic-bezier(0.45, 0, 0.2, 1), opacity 0.3s ease; opacity: 0; }
.tour-cursor.is-on { opacity: 1; }
.tour-cursor svg { width: 30px; height: 30px; filter: drop-shadow(0 2px 3px rgba(0, 0, 0, 0.3)); }
.tour-cursor .ring { position: absolute; left: -14px; top: -14px; width: 28px; height: 28px; border-radius: 50%; border: 3px solid rgba(28, 40, 54, 0.55); opacity: 0; }
.tour-cursor.is-clicking .ring { animation: tour-ring 0.5s ease-out; }
@keyframes tour-ring { from { opacity: 1; transform: scale(0.4); } to { opacity: 0; transform: scale(1.7); } }
.tour-card {
  inset: 0; width: 100vw; height: 100vh; max-width: none; max-height: none; pointer-events: none;
  display: grid; place-content: center; text-align: center; gap: 18px;
  background: var(--paper); opacity: 0; transition: opacity 0.6s ease;
}
.tour-card.is-on { opacity: 1; }
.tour-card.is-instant { transition: none; }
.tour-card .mark { width: 74px; height: 74px; margin: 0 auto; }
.tour-card .mark path { fill: none; stroke: var(--tick); stroke-width: 3.2; stroke-linecap: round; stroke-linejoin: round; }
.tour-card h1 { margin: 0; font-size: clamp(56px, 6.4vw, 112px); line-height: 1; font-weight: 780; font-stretch: 125%; letter-spacing: -0.02em; }
.tour-card p { margin: 0 auto; max-width: 30ch; font-size: clamp(22px, 2vw, 36px); line-height: 1.3; color: var(--ink-soft); text-wrap: balance; }
.tour-card .rule { width: 160px; height: 0; margin: 6px auto; border-top: 3px double var(--rule-strong); }
.tour-card .small { font-size: clamp(16px, 1.25vw, 22px); color: var(--graphite); max-width: none; }
.tour-card .small strong { color: var(--ink); font-weight: 650; }
`;

class Stop extends Error {}

export async function startTour({ state, mode = 'demo' }) {
  const params = new URLSearchParams(location.search);
  const speed = Math.max(0.25, Number(params.get('speed')) || 1);
  let stopped = false;

  const style = document.createElement('style');
  style.textContent = TOUR_CSS;
  document.head.append(style);

  const layer = (cls, html = '') => {
    const el = document.createElement('div');
    el.className = `tour-layer ${cls}`;
    el.setAttribute('popover', 'manual');
    el.innerHTML = html;
    document.body.append(el);
    el.showPopover();
    return el;
  };
  const cardEl = layer('tour-card');
  const captionEl = layer('tour-caption');
  captionEl.setAttribute('role', 'status');
  const cursorEl = layer('tour-cursor', `<span class="ring"></span><svg viewBox="0 0 30 30" aria-hidden="true">
    <path d="M6 3.5l16.5 15.2-7.4.8 4.4 8.6-3.5 1.7-4.3-8.7-5.7 5.1z" fill="#fff" stroke="#1c2836" stroke-width="1.6" stroke-linejoin="round"/></svg>`);
  let cursorAt = { x: innerWidth * 0.62, y: innerHeight * 0.55 };
  cursorEl.style.transform = `translate(${cursorAt.x}px, ${cursorAt.y}px)`;

  // Popovers stack in the order they were shown; a modal dialog opened later sits
  // above them, so re-show the tour layers to keep captions and pointer on top.
  const raise = () => {
    for (const el of [cardEl, captionEl, cursorEl]) {
      if (!el.isConnected) continue;
      el.hidePopover();
      el.showPopover();
    }
  };

  const onKey = (e) => { if (e.key === 'Escape') stopped = true; };
  addEventListener('keydown', onKey);

  // The recorder (scripts/record_demo.py) exposes a real mouse so hover states and
  // the chart tooltip behave exactly as they do for a person. In a normal browser
  // the tour falls back to synthetic events.
  const driver = typeof window.__tourMove === 'function' && typeof window.__tourClick === 'function'
    ? { move: (x, y) => window.__tourMove(x, y), click: (x, y) => window.__tourClick(x, y) }
    : null;

  const sleep = (ms) => new Promise((resolve, reject) => {
    setTimeout(() => (stopped ? reject(new Stop()) : resolve()), ms / speed);
  });
  const waitFor = async (test, { timeout = 45000, label = 'condition' } = {}) => {
    const start = performance.now();
    for (;;) {
      if (stopped) throw new Stop();
      const value = test();
      if (value) return value;
      if (performance.now() - start > timeout) throw new Error(`Tour timed out waiting for ${label}`);
      await new Promise((r) => setTimeout(r, 100));
    }
  };
  const readingTime = (text) => 1400 + text.replace(/<[^>]+>/g, '').split(/\s+/).length * 330;

  const caption = async (html, hold) => {
    captionEl.classList.remove('is-on');
    await sleep(220);
    captionEl.innerHTML = html;
    captionEl.classList.add('is-on');
    await sleep(hold ?? readingTime(html));
  };
  const hideCaption = () => captionEl.classList.remove('is-on');

  const card = async (html, hold, { instant = false } = {}) => {
    cardEl.innerHTML = html;
    cardEl.classList.toggle('is-instant', instant);
    cardEl.classList.add('is-on');
    if (instant) window.__tourT0 = Date.now(); // the recorder starts the video here
    await sleep(hold);
    cardEl.classList.remove('is-instant');
  };
  const hideCard = async () => {
    cardEl.classList.remove('is-on');
    await sleep(650);
  };

  const scrollTo = async (el, offset = 90) => {
    const top = Math.max(0, el.getBoundingClientRect().top + scrollY - offset);
    scrollTo_(top);
    await sleep(1000);
  };
  const scrollTo_ = (top) => window.scrollTo({ top, behavior: 'smooth' });

  const moveTo = async (el, { fx = 0.5, fy = 0.5 } = {}) => {
    const r = el.getBoundingClientRect();
    cursorAt = { x: r.left + r.width * fx, y: r.top + r.height * fy };
    cursorEl.classList.add('is-on');
    cursorEl.style.transform = `translate(${cursorAt.x - 6}px, ${cursorAt.y - 4}px)`;
    await sleep(800);
    if (driver) await driver.move(cursorAt.x, cursorAt.y);
  };
  const click = async (el, opts) => {
    await moveTo(el, opts);
    cursorEl.classList.remove('is-clicking');
    void cursorEl.offsetWidth;
    cursorEl.classList.add('is-clicking');
    await sleep(160);
    if (driver) await driver.click(cursorAt.x, cursorAt.y);
    else el.click();
    await sleep(380);
  };
  // Synthetic pointer events on whatever is under the pointer (the chart canvas),
  // so AG Charts shows its tooltip as it would for a real mouse.
  const pointerAt = (x, y, types) => {
    const target = document.elementFromPoint(x, y);
    if (!target) return;
    for (const type of types) {
      const Ev = type.startsWith('pointer') ? PointerEvent : MouseEvent;
      target.dispatchEvent(new Ev(type, { bubbles: true, composed: true, clientX: x, clientY: y, pointerType: 'mouse', pointerId: 1 }));
    }
  };
  const hover = async (el, fx, fy) => {
    await moveTo(el, { fx, fy });
    if (driver) return;
    const r = el.getBoundingClientRect();
    pointerAt(r.left + r.width * fx, r.top + r.height * fy, ['pointerover', 'pointerenter', 'mouseover', 'pointermove', 'mousemove']);
  };
  const unhover = async (el) => {
    if (driver) { await moveTo($('heroTitle'), { fx: 0.9, fy: 0.5 }); return; }
    const r = el.getBoundingClientRect();
    pointerAt(r.left + r.width / 2, r.top + r.height / 2, ['pointerout', 'pointerleave', 'mouseout', 'mouseleave']);
  };

  const closeDone = () => state.data && !$('runBtn').disabled && !/…|\.\.\./.test($('heroTitle').textContent);
  const explained = () => state.exp && state.exp.summary !== undefined;

  try {
    window.__tourStarted = true;
    scrollTo_(0);
    await waitFor(closeDone, { label: 'the first close' });

    // ---------------------------------------------------------------- title
    await card(`<svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><path d="M6 17.5l6.5 6.5L26 8"/></svg>
      <h1>Closeout</h1><div class="rule"></div>
      <p>Month-end close for small businesses that sell through PayPal</p>`, 4200, { instant: true });
    await hideCard();

    // ------------------------------------------------------------- the close
    await caption('Closing the month means matching every PayPal payment to your own order records. For a small shop that is hours of spreadsheet work.');
    const run = $('runBtn');
    await moveTo(run);
    await caption('Closeout does it in one click. It pulls the month from PayPal’s <b>Transaction Search</b> and <b>Invoicing</b> APIs and checks every payment against the books.', 5200);
    await click(run);
    hideCaption();
    await waitFor(closeDone, { label: 'the close' });
    await sleep(1300);
    const s = state.data.summary;
    const open = state.data.issues.filter((i) => i.status === 'open');
    const atStake = open.reduce((sum, i) => sum + i.amount_at_risk, 0);
    await caption(`${s.txn_count} PayPal transactions against ${s.order_count} orders: ${Math.round(s.match_rate)}% tie out exactly. ${open.length} items need a look, with <b>${usd(atStake)}</b> at stake.`);

    await waitFor(explained, { label: 'the AI summary', timeout: 60000 });
    await moveTo($('aiSummary'), { fx: 0.85, fy: 0.5 });
    await caption(state.exp.summary_source === 'ai'
      ? 'Gemini writes the summary. Every figure it uses is checked against the data before it’s shown, so it can’t invent a number.'
      : 'No AI key is set here, so Closeout’s rules wrote this summary. With a Gemini key, Gemini writes it, and every figure is checked against the data first.');

    // ---------------------------------------------------------------- chart
    const chartEl = $('splitChart');
    if (!$('split').hidden && !$('split').classList.contains('is-empty')) {
      await hover(chartEl, 0.3, 0.5);
      await caption('An <b>AG Charts</b> figure answers the question every seller asks: why is the bank deposit lower than my sales?');
      await hover(chartEl, 0.86, 0.5);
      await sleep(1600);
      await unhover(chartEl);
    }

    // --------------------------------------------------------------- ledger
    await scrollTo($('ledgerTitle').closest('.ledger'), 24);
    await moveTo($('grid'), { fx: 0.12, fy: 0.3 });
    await caption('The ledger is built on <b>AG Grid</b>: a green tick where PayPal and the books agree, highlighter where they don’t.');
    const attention = document.querySelector('#filterSwitch button[data-filter="attention"]');
    await click(attention);
    await caption(`Filter to what needs attention: ${open.length} of ${state.data.rows.length} rows. Red ink only where the money doesn’t tie out.`);

    // ----------------------------------------------------------- one issue
    const dup = open.find((i) => i.actions.some((a) => a.type === 'refund_duplicate'));
    if (dup) {
      const item = document.querySelector(`#issueList button[data-id="${CSS.escape(dup.id)}"]`);
      await click(item, { fx: 0.4 });
      await waitFor(() => !$('issueDetail').hidden, { label: 'the issue detail' });
      await sleep(600);
      await moveTo($('issueDetail').querySelector('.explanation'), { fx: 0.8, fy: 0.3 });
      await caption('Each item comes with a plain-English explanation, a likely cause, and the exact PayPal records behind it.');

      const refundBtn = $('issueDetail').querySelector('button[data-action="refund_duplicate"]');
      await click(refundBtn);
      await waitFor(() => $('actionDialog').open && !$('dialogConfirm').disabled, { label: 'the approval dialog' });
      raise();
      await sleep(500);
      await moveTo($('dialogApi'), { fx: 0.6 });
      await caption('Nothing moves without your approval. First you see what will happen, the exact PayPal API call, and the note to the customer.', 6200);
      await click($('dialogConfirm'));
      await waitFor(() => !$('actionDialog').open, { label: 'the refund' });
      await sleep(400);
      await caption(state.source === 'sample'
        ? 'Approved. On a live account this calls PayPal’s refund API with a request ID, so the same refund can never go out twice.'
        : 'Approved. The refund went through PayPal’s API with a request ID, so it can never go out twice.');
    }

    // ------------------------------------------------------------------ ask
    await scrollTo($('askTitle').closest('.lower'), 60);
    const chip = [...document.querySelectorAll('#suggestions .chip')].find((b) => /bank transfer/i.test(b.textContent))
      || document.querySelector('#suggestions .chip');
    await click(chip);
    await caption('Ask questions in plain English. Answers use only this month’s numbers, and are checked the same way.', 3600);
    await waitFor(() => !$('answer').hidden && !$('answer').querySelector('.is-waiting'), { label: 'the answer', timeout: 60000 });
    await moveTo($('answer'), { fx: 0.7, fy: 0.4 });
    await sleep(4200);

    // ------------------------------------------------- live PayPal sandbox
    const paypal = state.config.sources.find((x) => x.id === 'paypal');
    if (mode === 'video' && paypal?.available) {
      scrollTo_(0);
      await sleep(900);
      const btn = document.querySelector('#sourceSwitch button[data-source="paypal"]');
      await moveTo(btn);
      await caption('Now the same flow on a live <b>PayPal sandbox</b> account: real API calls, real test money.', 4200);
      await click(btn);
      hideCaption();
      await waitFor(closeDone, { label: 'the live close', timeout: 90000 });
      await sleep(1500);
      const live = state.data.summary;
      const invoices = state.data.rows.filter((r) => r.channel === 'invoice').length;
      await caption(`PayPal just returned ${live.txn_count} transactions and ${invoices} invoices for this account.`
        + (live.waiting_count ? ' Sales PayPal hasn’t reported yet show as waiting, never as missing.' : ''));

      const liveOpen = state.data.issues.filter((i) => i.status === 'open');
      // &live=reminder skips the refund (say, if it was already refunded by hand).
      const prefer = params.get('live') === 'reminder' ? ['send_reminder'] : ['refund_duplicate', 'send_reminder'];
      let pick = null;
      for (const type of prefer) {
        const issue = liveOpen.find((i) => i.actions.some((a) => a.type === type));
        if (issue) { pick = { issue, type }; break; }
      }
      if (pick) {
        await waitFor(explained, { label: 'the live explanations', timeout: 60000 });
        await scrollTo($('ledgerTitle').closest('.ledger'), 24);
        const item = document.querySelector(`#issueList button[data-id="${CSS.escape(pick.issue.id)}"]`);
        await click(item, { fx: 0.4 });
        await waitFor(() => !$('issueDetail').hidden, { label: 'the live issue' });
        await sleep(900);
        const actionBtn = $('issueDetail').querySelector(`button[data-action="${pick.type}"]`);
        await click(actionBtn);
        await waitFor(() => $('actionDialog').open && !$('dialogConfirm').disabled, { label: 'the live dialog' });
        raise();
        await sleep(500);
        await moveTo($('dialogMode'), { fx: 0.5 });
        await caption(pick.type === 'refund_duplicate'
          ? 'This one is real: approving it refunds the duplicate through PayPal’s Payments API.'
          : 'This one is real: approving it sends the customer a reminder through PayPal’s Invoicing API.', 4600);
        await click($('dialogConfirm'));
        await waitFor(() => !$('actionDialog').open || !$('dialogError').hidden, { label: 'the live action', timeout: 60000 });
        if ($('actionDialog').open) {
          $('actionDialog').close();
        } else {
          await sleep(500);
          await caption(pick.type === 'refund_duplicate'
            ? 'Done. PayPal processed the refund, and it’s recorded in the activity log.'
            : 'Done. PayPal accepted the reminder, and it’s recorded in the activity log.', 4200);
        }
      }
    }

    // ------------------------------------------------------------------ end
    hideCaption();
    cursorEl.classList.remove('is-on');
    await card(`<svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><path d="M6 17.5l6.5 6.5L26 8"/></svg>
      <h1>Closeout</h1><div class="rule"></div>
      <p>The month, reconciled. Every fix approved by you.</p>
      <p class="small">Open source under the MIT license &middot; <strong>${REPO}</strong></p>
      <p class="small">PayPal Transaction Search, Invoicing, Orders and Payments APIs &middot; Gemini &middot; AG Grid &middot; AG Charts</p>`, 6500);
  } catch (err) {
    if (!(err instanceof Stop)) console.error(err);
  } finally {
    window.__tourDone = true;
    removeEventListener('keydown', onKey);
    if (mode !== 'video') {
      captionEl.classList.remove('is-on');
      cardEl.classList.remove('is-on');
      cursorEl.classList.remove('is-on');
      setTimeout(() => { captionEl.remove(); cardEl.remove(); cursorEl.remove(); style.remove(); }, 700);
    }
  }
}
