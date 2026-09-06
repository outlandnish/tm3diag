/* Shared alert rendering for the CAN Live viewer (/) and the Drive HUD (/dash).
 *
 * Both pages consume the same /api/alerts payload:
 *   faults[] — alert-matrix bits currently SET on the bus
 *   log[]    — distinct <NODE>_alertLog payloads the ECUs broadcast, decoded
 * and both entry kinds are the same shape (alert_log.alert_view: title,
 * description, cause, clear, effect + the raw NODE_aNNN_name), so one renderer
 * serves both. Only the CSS lives per page — each defines .fault / .alog-row /
 * .a-sec against its own palette variables.
 */
(function () {
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
  // Alert text is firmware-derived, so it goes through innerHTML escaped.
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ESC[c]);
  const hex3 = n => '0x' + Number(n).toString(16).toUpperCase().padStart(3, '0');
  const bytes = arr => (arr || []).map(b => b.toString(16).toUpperCase().padStart(2, '0')).join(' ');

  function fmtTime(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false });
  }

  // One-line "why the ECU logged this" for a decoded alertLog entry: the
  // offending message for CAN-rationality alerts, the decoded reason enum for
  // everything else that has one, else the raw payload words.
  function logWhy(e) {
    if (e.rationality && e.offending_id != null) {
      const who = e.offending_name || hex3(e.offending_id);
      const bad = e.bad_value1 != null ? ` (bad ${e.bad_value1}/${e.bad_value2})` : '';
      return `${who} · ${e.error_name || 'error ' + e.error_type}${bad}`;
    }
    if (e.reason_label) return e.reason_label;
    if (e.reason_text) return e.reason_text;
    return e.words && e.words.length
      ? 'words ' + e.words.map(w => w.toString(16).toUpperCase().padStart(4, '0')).join(' ')
      : '';
  }

  // Tooltip text: Tesla's own description, falling back to the trigger condition.
  function tooltip(a) {
    const head = `${a.name}${a.ecu && !a.name.startsWith(a.ecu) ? ' (' + a.ecu + ')' : ''}`;
    const body = a.description || a.cause || '';
    return body ? `${head}\n\n${body}` : head;
  }

  function section(heading, text) {
    return text ? `<div class="a-sec"><h3>${esc(heading)}</h3><p>${esc(text)}</p></div>` : '';
  }

  // Full detail body for the modal. Works for a fault bit and for a log entry —
  // a log entry just carries the extra payload fields.
  function detailHtml(a) {
    const out = [];
    // The decoded reason goes first — it's the one field that says which of the
    // alert's many possible triggers actually fired on this frame.
    if (a.reason_signal) {
      const short = a.reason_signal.replace(/^[A-Z][A-Z0-9]*_[a-z]{1,2}\d+_/, '');
      out.push('<div class="a-sec"><h3>Reason</h3><p><b>'
        + esc(a.reason_label || a.reason_value) + '</b>'
        + `<span class="hint"> — ${esc(short)}`
        + (a.reason_label ? '' : ' (value not in the catalog’s table)') + '</span></p></div>');
    }
    out.push(
      section('What it means', a.description),
      section('Cause', a.cause),
      section('Clears when', a.clear),
      section('Effect', a.effect),
    );
    if (!a.description && !a.cause && !a.clear && !a.effect) {
      out.push('<div class="a-sec"><p class="hint">No catalog text for this alert in the '
        + 'loaded firmware build — the title above is derived from the alert name. Point '
        + 'TM3_ROOT at a firmware extraction to get Tesla’s own description.</p></div>');
    }

    if (a.data) {
      // Every logged signal is listed, decoded or not: the undecoded ones say
      // what the remaining payload words mean even though their bit positions
      // aren't published anywhere (see alert_log's module docstring).
      const undec = (a.log_values || []).filter(v => v.value == null).length;
      const kv = (a.log_values || []).map(v => `<dt>${esc(v.name)}</dt><dd>`
        + (v.value == null ? '<span class="hint">not decoded</span>' : esc(v.value))
        + '</dd>').join('');
      out.push('<div class="a-sec"><h3>Logged by the ECU</h3>'
        + `<div class="a-code"><b>${esc(hex3(a.can_id))}</b> · ${esc(bytes(a.data))}`
        + (a.state ? ` · <b>${esc(a.state)}</b>` : '') + '</div>'
        + (kv ? `<dl class="a-kv" style="margin-top:8px">${kv}</dl>` : '')
        + (undec ? '<p class="hint" style="margin-top:6px">'
            + `${undec} field${undec > 1 ? 's have' : ' has'} no published bit layout — `
            + 'the raw payload above carries them.</p>' : '')
        + '</div>');
      out.push('<div class="a-sec"><h3>Seen</h3><dl class="a-kv">'
        + `<dt>first</dt><dd>${esc(fmtTime(a.first))}</dd>`
        + `<dt>last</dt><dd>${esc(fmtTime(a.last))}</dd>`
        + `<dt>ticks</dt><dd>${esc(a.count)}</dd></dl></div>`);
    } else if (a.log_signals && a.log_signals.length) {
      // A fault bit with no log frame captured yet: still say which signals the
      // ECU would log, so you know what to watch for on the alertLog id.
      out.push('<div class="a-sec"><h3>Logs these signals</h3><div class="a-code">'
        + esc(a.log_signals.join(', ')) + '</div></div>');
    }

    const extra = [];
    if (a.catalog_name && a.catalog_name !== a.name) extra.push('catalog: ' + a.catalog_name);
    if (a.audience) extra.push('audience ' + a.audience);
    out.push('<div class="a-sec"><h3>Alert code</h3><div class="a-code">'
      + `<b>${esc(a.name)}</b>${extra.length ? ' · ' + esc(extra.join(' · ')) : ''}`
      + '</div></div>');
    return out.join('');
  }

  function subtitle(a) {
    return [a.ecu || a.node, a.code].filter(Boolean).join(' · ');
  }

  // ---- element builders -----------------------------------------------------

  function faultCard(a, onOpen) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'fault' + (a.hardware ? ' hw' : '');
    el.title = tooltip(a);
    el.innerHTML = `<span class="tag">${esc(a.ecu || '')}${a.code ? ' · ' + esc(a.code) : ''}`
      + `${a.hardware ? ' · hw' : ''}</span>`
      + `<div class="f-title">${esc(a.title)}</div>`
      + (a.description ? `<div class="f-desc">${esc(a.description)}</div>` : '');
    el.onclick = () => onOpen(a);
    return el;
  }

  function logRow(e, onOpen) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'alog-row';
    el.title = tooltip(e);
    el.innerHTML = `<span class="t">${esc(fmtTime(e.last))}</span>`
      + `<span class="ecu">${esc(e.ecu || '')}</span>`
      + `<span class="code">${esc(e.code || 'a' + e.alert_code)}</span>`
      + `<span class="ttl">${esc(e.title)}</span>`
      + `<span class="why">${esc(logWhy(e))}</span>`
      + `<span class="n">×${esc(e.count)}</span>`;
    el.onclick = () => onOpen(e);
    return el;
  }

  // ---- page wiring ----------------------------------------------------------

  /* Wire a modal (a .modal wrapper + title/body elements) and return open(a). */
  function mountModal(modalEl, titleEl, bodyEl, closeEl) {
    const close = () => modalEl.classList.remove('open');
    if (closeEl) closeEl.onclick = close;
    modalEl.onclick = ev => { if (ev.target === modalEl) close(); };
    document.addEventListener('keydown', ev => {
      if (ev.key === 'Escape' && modalEl.classList.contains('open')) close();
    });
    return function open(a) {
      titleEl.textContent = a.title || a.name;
      bodyEl.innerHTML = `<div class="a-sub">${esc(subtitle(a))}</div>` + detailHtml(a);
      modalEl.classList.add('open');
    };
  }

  /* Repaint a faults container. Returns the number rendered. */
  function renderFaults(box, faults, onOpen, emptyHtml) {
    box.innerHTML = '';
    if (!faults || !faults.length) {
      box.innerHTML = emptyHtml || '<div class="no-faults">No active faults</div>';
      return 0;
    }
    faults.forEach(f => box.appendChild(faultCard(f, onOpen)));
    return faults.length;
  }

  /* Repaint an alert-log container. Returns the number rendered. */
  function renderLog(box, entries, onOpen, emptyHtml) {
    box.innerHTML = '';
    if (!entries || !entries.length) {
      box.innerHTML = emptyHtml
        || '<div class="hint">Nothing logged yet — ECUs broadcast an empty alert log '
           + 'until something is wrong.</div>';
      return 0;
    }
    entries.forEach(e => box.appendChild(logRow(e, onOpen)));
    return entries.length;
  }

  window.TMAlerts = {
    esc, hex3, bytes, fmtTime, logWhy, tooltip, detailHtml, subtitle,
    faultCard, logRow, mountModal, renderFaults, renderLog,
  };
})();
