/**
 * The page. Submit some documents, then render whatever the socket pushes back.
 *
 * The rendering is deliberately dumb: every `case` event carries the *whole* record, so
 * there is nothing to accumulate and no message types to reconcile — `render(record)`
 * gets the latest one and redraws from scratch. That is the property the API was built
 * around, and it is why this file has no state machine in it.
 */

import { connect } from './socketio.js';

const $ = (id) => document.getElementById(id);
const show = (el, on = true) => el.classList.toggle('hidden', !on);
const esc = (value) =>
  String(value ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

let socket = null;

// --- the document boxes -------------------------------------------------------

function addDocument(name = '', text = '') {
  const box = document.createElement('div');
  box.className = 'doc';
  box.innerHTML = `
    <input class="name" placeholder="Document name" value="${esc(name)}">
    <textarea class="text" placeholder="INVOICE&#10;Order No: PO-1042&#10;Total Amount: 51,000.00"></textarea>`;
  box.querySelector('.text').value = text;
  $('docs').append(box);
}

const collect = () =>
  [...document.querySelectorAll('.doc')]
    .map((box) => ({
      name: box.querySelector('.name').value.trim() || null,
      text: box.querySelector('.text').value.trim(),
    }))
    .filter((doc) => doc.text);

async function loadSample() {
  const documents = await (await fetch('/v1/sample')).json();
  $('docs').replaceChildren();
  documents.forEach((doc) => addDocument(doc.name, doc.text));
}

// --- submitting ---------------------------------------------------------------

async function submit() {
  const documents = collect();
  show($('error'), false);

  if (documents.length < 2) {
    $('error').textContent = 'Fill in at least two documents — there is nothing to compare otherwise.';
    return show($('error'));
  }

  $('submit').disabled = true;
  try {
    const response = await fetch('/v1/cases', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ documents }),
    });
    const record = await response.json();
    if (!response.ok) throw new Error(JSON.stringify(record.detail ?? record));
    watch(record);
  } catch (error) {
    $('error').textContent = String(error.message ?? error);
    show($('error'));
  } finally {
    $('submit').disabled = false;
  }
}

/** Subscribe to one case and let `render` do the rest. */
function watch(record) {
  socket?.close();
  show($('idle'), false);
  show($('live'));
  show($('report-panel'), false);
  render(record);

  // The trail sits below the submit button, so without this the whole run happens
  // off-screen and there is nothing to watch.
  $('live').closest('.panel').scrollIntoView({ behavior: 'smooth', block: 'start' });

  socket = connect(location.origin);
  socket.on('case', render);
  socket.on('done', () => socket.close());
  socket.emit('subscribe', { case_id: record.case_id });
}

// --- rendering ----------------------------------------------------------------

function render(record) {
  $('case-id').textContent = record.case_id;
  $('status').textContent = record.status;
  $('status').className = `pill ${record.status}`;
  $('raw').textContent = JSON.stringify(record, null, 2);

  $('trail').innerHTML = record.events
    .map(
      (event) => `<li>
        <span class="stage">${esc(event.stage)}</span>
        <span>${esc(event.message)}</span>
        <span class="when">${new Date(event.at).toLocaleTimeString()}</span>
      </li>`,
    )
    .join('');

  if (record.error) {
    $('error').textContent = record.error;
    show($('error'));
  }
  if (record.report) renderReport(record.report);
}

const VERDICTS = {
  clean: 'Clean — every shared field agreed',
  needs_review: 'Needs review — some fields disagree',
  blocked: 'Blocked — important fields disagree',
};

function renderReport(report) {
  show($('report-panel'));
  $('verdict').textContent = VERDICTS[report.verdict] ?? report.verdict;
  $('verdict').className = `verdict ${report.verdict}`;
  $('summary').textContent = report.summary;

  // doc_id is what a finding cites; a person wants the name it was submitted under.
  const named = Object.fromEntries(report.documents.map((doc) => [doc.doc_id, doc.name]));

  $('mismatches').innerHTML = report.mismatches.length
    ? report.mismatches
        .map(
          (mismatch) => `<div class="finding ${mismatch.severity}">
            <header>
              <span class="field">${esc(mismatch.field)}</span>
              <span class="sev">${esc(mismatch.severity)}</span>
            </header>
            <p>${esc(mismatch.explanation)}</p>
            <table>${mismatch.values
              .map(
                (value) => `<tr>
                  <td class="who">${esc(named[value.doc_id] ?? value.doc_id)}</td>
                  <td class="val">${esc(value.value)}</td>
                </tr>`,
              )
              .join('')}</table>
          </div>`,
        )
        .join('')
    : '<p class="hint">No disagreements found.</p>';

  $('matched').textContent = report.matched_fields.join(' · ') || 'None — no field appeared twice.';

  $('documents').innerHTML = report.documents
    .map(
      (doc) => `<div class="card">
        <h4>${esc(doc.name)} <span class="hint">· ${esc(doc.title)}</span></h4>
        <p class="note">${esc(doc.note)}</p>
        <table>${Object.entries(doc.fields)
          .map(([field, value]) => `<tr><td class="who">${esc(field)}</td><td class="val">${esc(value)}</td></tr>`)
          .join('')}</table>
      </div>`,
    )
    .join('');
}

// --- wiring -------------------------------------------------------------------

$('add').onclick = () => addDocument();
$('clear').onclick = () => {
  $('docs').replaceChildren();
  addDocument();
  addDocument();
};
$('sample').onclick = loadSample;
$('submit').onclick = submit;

loadSample();
