/**
 * YouOS Gmail Add-on (b280).
 *
 * Puts YouOS's review experience inside Gmail: open a thread and the sidebar
 * shows YouOS's draft for it, its calibrated confidence, and the "why", with
 * Dismiss / Regenerate / Open-draft actions. It talks to YOUR local YouOS
 * instance over its REST API — exposed to Google's servers via Tailscale Funnel
 * — authenticated with an X-YouOS-Token API token (see README).
 *
 * Privacy note: this script stores no mail content. It renders what YouOS
 * already computed locally; the only persisted state is your URL + token in
 * per-user script properties.
 */

var PROP_URL = 'YOUOS_URL'; // e.g. https://your-mac.tailXXXX.ts.net (Funnel)
var PROP_TOKEN = 'YOUOS_TOKEN'; // a YouOS API token (minted via the API/CLI)

function _props() { return PropertiesService.getUserProperties(); }
function _baseUrl() { return (_props().getProperty(PROP_URL) || '').replace(/\/+$/, ''); }
function _token() { return _props().getProperty(PROP_TOKEN) || ''; }

/** Call the YouOS REST API with the token header. Returns {code, body}. */
function _api(method, path, payload) {
  var opts = {
    method: method,
    muteHttpExceptions: true,
    headers: { 'X-YouOS-Token': _token() },
    contentType: 'application/json'
  };
  if (payload) { opts.payload = JSON.stringify(payload); }
  var resp = UrlFetchApp.fetch(_baseUrl() + path, opts);
  return { code: resp.getResponseCode(), body: resp.getContentText() };
}

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// --- Settings / homepage ---------------------------------------------------

// Homepage trigger: clicked from anywhere in Gmail (no thread open) → the YouOS
// dashboard (the in-Gmail queue). Falls back to Settings until configured.
function onHomepage(e) {
  if (!_baseUrl() || !_token()) { return _settingsCard(); }
  return _dashboardCard(_dashAccount());
}

// Universal-action entry point for "YouOS settings" (onHomepage now opens the
// dashboard, so the settings menu item needs its own function).
function openSettings() { return _settingsCard(); }

function _settingsCard() {
  var section = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph().setText(
      'Connect this add-on to your local YouOS instance. Expose YouOS with ' +
      'Tailscale Funnel and mint an API token — see the README.'))
    .addWidget(CardService.newTextInput()
      .setFieldName('url').setTitle('YouOS URL (Funnel HTTPS)')
      .setHint('https://your-mac.tailXXXX.ts.net').setValue(_baseUrl()))
    .addWidget(CardService.newTextInput()
      .setFieldName('token').setTitle('API token (X-YouOS-Token)')
      .setHint(_token() ? 'saved — leave blank to keep' : 'paste token'))
    .addWidget(CardService.newTextButton().setText('Save')
      .setOnClickAction(CardService.newAction().setFunctionName('saveSettings')));
  if (_baseUrl() && _token()) {
    section.addWidget(CardService.newTextButton().setText('Test connection')
      .setOnClickAction(CardService.newAction().setFunctionName('actTestConnection')));
    section.addWidget(CardService.newTextButton().setText('← Back to queue')
      .setOnClickAction(CardService.newAction().setFunctionName('actRefreshDash').setParameters({ account: '' })));
  }
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle('Settings'))
    .addSection(section).build();
}

// Ping the API and report what happened — turns "why isn't it working" into a
// one-tap answer (URL wrong / token bad / Funnel down vs all good).
function actTestConnection(e) {
  if (!_baseUrl()) { return _notify('Set the YouOS URL first'); }
  var msg;
  try {
    var res = _api('get', '/api/agent/accounts');
    if (res.code === 200) {
      var n = ((JSON.parse(res.body) || {}).accounts || []).length;
      msg = '✓ Connected — ' + n + ' account(s)';
    } else if (res.code === 401 || res.code === 403) {
      msg = '✗ Auth failed (' + res.code + ') — check the token';
    } else {
      msg = '✗ YouOS returned ' + res.code;
    }
  } catch (err) {
    msg = '✗ Can’t reach the URL — check the Funnel';
  }
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(msg)).build();
}

// An error card with a one-tap jump to Settings (instead of a dead end).
function _errorCard(title, body) {
  var section = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph().setText(_esc(body)))
    .addWidget(CardService.newTextButton().setText('Open Settings')
      .setOnClickAction(CardService.newAction().setFunctionName('actOpenSettings')));
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle(title))
    .addSection(section).build();
}

function saveSettings(e) {
  var inputs = (e && e.commonEventObject && e.commonEventObject.formInputs) || {};
  function val(k) {
    return inputs[k] && inputs[k].stringInputs && inputs[k].stringInputs.value[0];
  }
  var url = val('url');
  var token = val('token');
  if (url) { _props().setProperty(PROP_URL, url.trim()); }
  if (token && token.trim()) { _props().setProperty(PROP_TOKEN, token.trim()); }
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Saved'))
    .setNavigation(CardService.newNavigation().updateCard(_settingsCard()))
    .build();
}

// --- Dashboard (the in-Gmail queue; homepage trigger) ----------------------

var PROP_DASH_ACCT = 'YOUOS_DASH_ACCOUNT';
var DASH_CAP = 6;  // items shown per section before "+N more"

function _dashAccount() { return _props().getProperty(PROP_DASH_ACCT) || ''; }

function _apiJson(path) {
  try { var r = _api('get', path); if (r.code === 200) { return JSON.parse(r.body); } } catch (err) {}
  return null;
}

function _accounts() {
  var d = _apiJson('/api/agent/accounts');
  return (d && d.accounts) || [];
}

function _btn(text, fn, params) {
  return CardService.newTextButton().setText(text)
    .setOnClickAction(CardService.newAction().setFunctionName(fn).setParameters(params));
}

function _dashRow(section, title, sub, buttons) {
  section.addWidget(CardService.newDecoratedText()
    .setText(_esc(title)).setBottomLabel(_esc(sub)).setWrapText(true));
  if (buttons) { section.addWidget(buttons); }
}

// Urgent threads (urgency_score ≥ 0.5) sort first and get a 🔴 marker.
function _byUrgency(a, b) {
  return (Number(b.urgency_score) || 0) - (Number(a.urgency_score) || 0)
    || (Number(b.needs_reply_score) || 0) - (Number(a.needs_reply_score) || 0);
}
function _rowTitle(r) {
  var urgent = (Number(r.urgency_score) || 0) >= 0.5 ? '🔴 ' : '';
  return urgent + (r.subject || '(no subject)');
}

// Prev/Next pager for a section: re-renders the dashboard with new offsets.
function _pager(section, account, total, dOff, sOff, which, off) {
  if (total <= DASH_CAP && off === 0) { return; }
  var bs = CardService.newButtonSet();
  var prev = Math.max(0, off - DASH_CAP), next = off + DASH_CAP;
  if (off > 0) { bs.addButton(_btn('◂ Prev', 'actRefreshDash', _pageParams(account, dOff, sOff, which, prev))); }
  if (next < total) { bs.addButton(_btn('Next ▸', 'actRefreshDash', _pageParams(account, dOff, sOff, which, next))); }
  section.addWidget(bs);
}
function _pageParams(account, dOff, sOff, which, off) {
  return {
    account: account || '',
    dOff: String(which === 'd' ? off : dOff),
    sOff: String(which === 's' ? off : sOff)
  };
}

function _dashboardCard(account, dOff, sOff) {
  dOff = Number(dOff) || 0;
  sOff = Number(sOff) || 0;
  var accounts = _accounts();
  if (!account && accounts.length) { account = accounts[0]; }
  var acctQ = account ? ('&account=' + encodeURIComponent(account)) : '';

  var pend = _apiJson('/api/agent/pending?limit=200' + acctQ) || {};
  var rows = pend.rows || [];
  var drafts = rows.filter(function (r) { return r.tier === 'draft'; }).sort(_byUrgency);
  var surface = rows.filter(function (r) { return r.tier === 'surface'; }).sort(_byUrgency);
  var events = ((_apiJson('/api/agent/events/pending') || {}).events || [])
    .filter(function (ev) { return !account || ev.account === account; });
  var fu = _apiJson('/api/agent/followups' + (account ? ('?account=' + encodeURIComponent(account)) : '')) || {};
  var owed = fu.owed || [], awaiting = fu.awaiting || [];

  var builder = CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle(account || 'default account'));

  // Account switcher (when >1 mailbox) + Refresh + last-updated.
  var top = CardService.newCardSection();
  if (accounts.length > 1) {
    var picker = CardService.newSelectionInput()
      .setType(CardService.SelectionInputType.DROPDOWN).setFieldName('dash_account').setTitle('Account')
      .setOnChangeAction(CardService.newAction().setFunctionName('actSetDashAccount'));
    for (var i = 0; i < accounts.length; i++) { picker.addItem(accounts[i], accounts[i], accounts[i] === account); }
    top.addWidget(picker);
  }
  top.addWidget(_btn('↻ Refresh', 'actRefreshDash', { account: account || '' }));
  var now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'HH:mm');
  top.addWidget(CardService.newDecoratedText().setText('<font color="#9aa0a6">Updated ' + now + '</font>').setWrapText(true));
  builder.addSection(top);

  // Inbox-zero state.
  if (!drafts.length && !events.length && !surface.length && !owed.length && !awaiting.length) {
    builder.addSection(CardService.newCardSection()
      .addWidget(CardService.newTextParagraph().setText('🎉 <b>Inbox zero.</b> Nothing in the YouOS queue right now.')));
    builder.addSection(CardService.newCardSection().addWidget(_btn('⚙ Settings', 'actOpenSettings', {})));
    return builder.build();
  }

  // Drafts to review → Push / Dismiss (urgent first).
  var ds = CardService.newCardSection().setHeader('📝 Drafts to review (' + drafts.length + ')');
  if (!drafts.length) { ds.addWidget(CardService.newTextParagraph().setText('<i>None.</i>')); }
  drafts.slice(dOff, dOff + DASH_CAP).forEach(function (r) {
    var bs = CardService.newButtonSet();
    bs.addButton(_btn('Push', 'actPush', { rowId: String(r.id), source: 'dashboard', account: account || '' }));
    bs.addButton(_btn('Dismiss', 'actDismissAsk', { rowId: String(r.id), source: 'dashboard', account: account || '' }));
    _dashRow(ds, _rowTitle(r), 'from ' + (r.sender || '?'), bs);
  });
  _pager(ds, account, drafts.length, dOff, sOff, 'd', dOff);
  builder.addSection(ds);

  // Meeting confirmations → Approve / Dismiss.
  var msec = CardService.newCardSection().setHeader('📅 Meeting confirmations (' + events.length + ')');
  if (!events.length) { msec.addWidget(CardService.newTextParagraph().setText('<i>None.</i>')); }
  events.slice(0, DASH_CAP).forEach(function (ev) {
    var bs = CardService.newButtonSet();
    bs.addButton(_btn('Approve', 'actApproveEvent', { eventId: String(ev.id), source: 'dashboard', account: account || '' }));
    bs.addButton(_btn('Dismiss', 'actDismissEvent', { eventId: String(ev.id), source: 'dashboard', account: account || '' }));
    _dashRow(msec, ev.title || 'Meeting', _fmtEventTime(ev.start_iso, ev.end_iso), bs);
  });
  builder.addSection(msec);

  // Needs review (surfaced, not drafted) → Draft it / Dismiss.
  var ns = CardService.newCardSection().setHeader('🔎 Needs review (' + surface.length + ')');
  if (!surface.length) { ns.addWidget(CardService.newTextParagraph().setText('<i>None.</i>')); }
  surface.slice(sOff, sOff + DASH_CAP).forEach(function (r) {
    var bs = CardService.newButtonSet();
    bs.addButton(_btn('Draft it', 'actRegenerate', { rowId: String(r.id), source: 'dashboard', account: account || '' }));
    bs.addButton(_btn('Dismiss', 'actDismissAsk', { rowId: String(r.id), source: 'dashboard', account: account || '' }));
    _dashRow(ns, _rowTitle(r), 'from ' + (r.sender || '?'), bs);
  });
  _pager(ns, account, surface.length, dOff, sOff, 's', sOff);
  builder.addSection(ns);

  // Follow-ups (read-only — open the thread to act).
  var fs = CardService.newCardSection()
    .setHeader('⏰ Follow-ups (' + (fu.owed_count != null ? fu.owed_count : owed.length) +
               ' owed · ' + (fu.awaiting_count != null ? fu.awaiting_count : awaiting.length) + ' awaiting)');
  owed.slice(0, 3).forEach(function (r) { _dashRow(fs, r.subject || '(no subject)', 'owed ' + r.age_days + 'd · ' + (r.sender || ''), null); });
  awaiting.slice(0, 3).forEach(function (r) { _dashRow(fs, r.subject || '(no subject)', 'awaiting reply ' + r.age_days + 'd · ' + (r.sender || ''), null); });
  if (!owed.length && !awaiting.length) { fs.addWidget(CardService.newTextParagraph().setText('<i>None.</i>')); }
  builder.addSection(fs);

  builder.addSection(CardService.newCardSection().addWidget(_btn('⚙ Settings', 'actOpenSettings', {})));
  return builder.build();
}

function actSetDashAccount(e) {
  var v = _formVal(e, 'dash_account');
  if (v) { _props().setProperty(PROP_DASH_ACCT, v); }
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(_dashboardCard(v || _dashAccount()))).build();
}

function actRefreshDash(e) {
  var p = (e.commonEventObject || {}).parameters || {};
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(
      _dashboardCard(p.account || _dashAccount(), p.dOff, p.sOff))).build();
}

function actOpenSettings(e) {
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(_settingsCard())).build();
}

// --- Contextual: a Gmail message is open -----------------------------------

function onGmailMessage(e) {
  if (!_baseUrl() || !_token()) { return _settingsCard(); }
  var threadId = e && e.gmail && e.gmail.threadId;
  if (!threadId) { return _infoCard('No thread', 'Open a conversation to see its YouOS draft.'); }

  // A confirmed-meeting card (b282) takes priority when one is queued for this
  // thread — it's a single-tap "create the event". Shown above the draft card.
  var cards = [];
  var evt = _eventForThread(threadId);
  if (evt) { cards.push(_eventCard(evt)); }

  var res;
  try {
    res = _api('get', '/api/agent/pending/by_thread/' + encodeURIComponent(threadId));
  } catch (err) {
    if (cards.length) { return cards; }
    return _errorCard('Can’t reach YouOS', 'Check the Funnel URL in Settings.\n\n' + _esc(err));
  }
  if (res.code === 200) {
    cards.push(_draftCard(JSON.parse(res.body)));
  } else if (res.code === 401 || res.code === 403) {
    if (!cards.length) { return _errorCard('Auth failed', 'Check your API token in Settings.'); }
  } else if (res.code !== 404 && !cards.length) {
    return _errorCard('YouOS error ' + res.code, _esc(res.body).slice(0, 300));
  }

  if (cards.length) { return cards; }
  return _noDraftCard(threadId);
}

// Contextual card when YouOS has nothing queued for the open thread: offer to
// draft a reply on demand (with an optional steer).
function _noDraftCard(threadId) {
  var section = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph().setText('YouOS has no draft for this thread yet.'));
  if (threadId) {
    section.addWidget(CardService.newTextInput().setFieldName('instruction')
      .setTitle('Optional instruction').setHint('e.g. propose Thursday; decline politely').setMultiline(true));
    section.addWidget(CardService.newTextButton().setText('Draft a reply')
      .setTextButtonStyle(CardService.TextButtonStyle.FILLED)
      .setOnClickAction(CardService.newAction().setFunctionName('actDraftForThread')
        .setParameters({ threadId: String(threadId) })));
  }
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle('Nothing queued'))
    .addSection(section).build();
}

// Contextual "Draft a reply": generate on demand, then show the full draft card.
function actDraftForThread(e) {
  var threadId = e.commonEventObject.parameters.threadId;
  var instruction = _formVal(e, 'instruction').trim();
  var payload = { thread_id: String(threadId) };
  if (instruction) { payload.instruction = instruction; }
  var res = _api('post', '/api/agent/draft_for_thread', payload);
  if (res.code !== 200) { return _notify('Draft failed (' + res.code + ')'); }
  var row = JSON.parse(res.body).row;
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Drafted'))
    .setNavigation(CardService.newNavigation().updateCard(_draftCard(row))).build();
}

/** Latest queued calendar event for a thread, or null. Never throws. */
function _eventForThread(threadId) {
  try {
    var res = _api('get', '/api/agent/events/by_thread/' + encodeURIComponent(threadId));
    if (res.code === 200) { return JSON.parse(res.body).event; }
  } catch (err) { /* ignore — no event card */ }
  return null;
}

// Show the slot's WALL-CLOCK time as proposed (the meeting's own tz, carried in
// the ISO offset) rather than converting to the script's tz — so it matches the
// time YouOS offered.
function _fmtEventTime(startIso, endIso) {
  try {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(startIso));
    var me = /T(\d{2}):(\d{2})/.exec(String(endIso));
    if (!m) { return startIso + ' – ' + endIso; }
    var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    var day = Utilities.formatDate(d, Session.getScriptTimeZone(), 'EEE MMM d');
    function to12(H, M) { var h = Number(H); var ap = h >= 12 ? 'PM' : 'AM'; return (((h + 11) % 12) + 1) + ':' + M + ' ' + ap; }
    var end = me ? '–' + to12(me[1], me[2]) : '';
    return day + ', ' + to12(m[4], m[5]) + end;
  } catch (err) { return startIso + ' – ' + endIso; }
}

function _eventCard(ev) {
  var section = CardService.newCardSection();
  section.addWidget(CardService.newDecoratedText()
    .setTopLabel('Confirmed meeting').setText(_esc(ev.title || 'Meeting'))
    .setBottomLabel(_fmtEventTime(ev.start_iso, ev.end_iso)).setWrapText(true));
  var who = (ev.attendees || []).join(', ') || 'no attendees (self-only)';
  section.addWidget(CardService.newDecoratedText()
    .setText('<font color="#5f6368">Invite: ' + _esc(who) + '</font>').setWrapText(true));

  if (ev.status === 'created') {
    var msg = 'Event created.';
    if (ev.meet_link) { msg += ' Meet: ' + _esc(ev.meet_link); }
    section.addWidget(CardService.newDecoratedText()
      .setText('<font color="#188038">✓ ' + msg + '</font>').setWrapText(true));
    if (ev.meet_link) {
      section.addWidget(CardService.newTextButton().setText('Join Meet')
        .setOpenLink(CardService.newOpenLink().setUrl(ev.meet_link)));
    }
  } else if (ev.status === 'pending') {
    var reasons = (ev.reasons || []).slice(0, 2).join(' · ');
    if (reasons) {
      section.addWidget(CardService.newDecoratedText()
        .setText('<font color="#5f6368">' + _esc(reasons) + '</font>').setWrapText(true));
    }
    var buttons = CardService.newButtonSet();
    buttons.addButton(CardService.newTextButton().setText('Approve & create')
      .setOnClickAction(CardService.newAction().setFunctionName('actApproveEvent')
        .setParameters({ eventId: String(ev.id) })));
    buttons.addButton(CardService.newTextButton().setText('Dismiss')
      .setOnClickAction(CardService.newAction().setFunctionName('actDismissEvent')
        .setParameters({ eventId: String(ev.id) })));
    section.addWidget(buttons);
  }

  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle('Meeting confirmation'))
    .addSection(section).build();
}

// Categorical dismissal reasons — must mirror app/agent/store.py DISMISSAL_REASONS.
var DISMISS_REASONS = [
  ['noise', 'Noise — shouldn’t have drafted'],
  ['wrong_sender', 'Wrong sender'],
  ['wrong_content', 'Wrong content / missed the point'],
  ['already_handled', 'Already handled outside YouOS'],
  ['other', 'Other']
];

/** Read a single form-input string value from an action event (or ''). */
function _formVal(e, key) {
  var inputs = (e && e.commonEventObject && e.commonEventObject.formInputs) || {};
  return (inputs[key] && inputs[key].stringInputs && inputs[key].stringInputs.value[0]) || '';
}

function _draftCard(row) {
  // A draft exists if YouOS drafted one OR you generated one here (amended).
  var draftText = row.amended_draft || row.draft || '';
  var hasDraft = !!draftText;
  var conf = (row.calibrated_score != null) ? row.calibrated_score : row.needs_reply_score;
  var subtitle = (hasDraft ? 'Drafted' : 'Surfaced — not drafted') +
    (conf != null ? ' · ' + Math.round(conf * 100) + '% likely to deserve a reply' : '') +
    ' · ' + (row.status || 'pending');

  var actionable = (row.status === 'pending' || row.status === 'amended');
  var rid = String(row.id);

  var builder = CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle(subtitle));

  // Section 1 — the draft (if any) + WHY (for surfaced threads, why it wasn't
  // drafted — the actionable feedback) + Gmail-draft status. When actionable the
  // draft is an EDITABLE field so you can tweak the wording in place (Save edits
  // / Push both read it).
  var draftSection = CardService.newCardSection();
  if (hasDraft && actionable) {
    draftSection.addWidget(CardService.newTextInput().setFieldName('edited')
      .setTitle('Draft (editable)').setMultiline(true).setValue(draftText));
  } else if (hasDraft) {
    draftSection.addWidget(CardService.newTextParagraph().setText(_esc(draftText)));
  } else {
    draftSection.addWidget(CardService.newTextParagraph()
      .setText('<i>YouOS surfaced this for review but didn’t draft a reply.</i>'));
  }
  var reasons = (row.reasons || []).slice(0, 4).join(' · ');
  if (reasons) {
    draftSection.addWidget(CardService.newDecoratedText()
      .setTopLabel(hasDraft ? 'Why' : 'Why it wasn’t drafted')
      .setText('<font color="#5f6368">' + _esc(reasons) + '</font>').setWrapText(true));
  }
  if (row.gmail_draft_id) {
    draftSection.addWidget(CardService.newDecoratedText()
      .setText('<font color="#1a73e8">In your Gmail Drafts, ready to send.</font>').setWrapText(true));
  }
  builder.addSection(draftSection);

  if (actionable) {
    // Section 2 — primary actions: save edits, push (once a draft exists), mark sent.
    var actSection = CardService.newCardSection();
    if (hasDraft) {
      actSection.addWidget(CardService.newTextButton().setText('Push to Gmail Drafts')
        .setTextButtonStyle(CardService.TextButtonStyle.FILLED)
        .setOnClickAction(CardService.newAction().setFunctionName('actPush').setParameters({ rowId: rid })));
      actSection.addWidget(CardService.newTextButton().setText('Save edits')
        .setOnClickAction(CardService.newAction().setFunctionName('actSaveEdit').setParameters({ rowId: rid })));
    }
    actSection.addWidget(CardService.newTextButton().setText('Mark sent manually')
      .setOnClickAction(CardService.newAction().setFunctionName('actMarkSent').setParameters({ rowId: rid })));
    builder.addSection(actSection);

    // Section 3 — generate / refine. For a surfaced (no-draft) thread this is
    // "Draft it" (also the strongest feedback: you DID want a reply here); once
    // a draft exists it becomes "Refine with a prompt".
    var genSection = CardService.newCardSection().setHeader(hasDraft ? 'Refine with a prompt' : 'Draft a reply');
    genSection.addWidget(CardService.newTextInput().setFieldName('instruction')
      .setTitle(hasDraft ? 'Instruction' : 'Optional instruction')
      .setHint('e.g. shorter; decline politely; propose Thursday').setMultiline(true));
    genSection.addWidget(CardService.newTextButton()
      .setText(hasDraft ? 'Regenerate' : 'Draft it')
      .setTextButtonStyle(hasDraft ? CardService.TextButtonStyle.TEXT : CardService.TextButtonStyle.FILLED)
      .setOnClickAction(CardService.newAction().setFunctionName('actRegenerate').setParameters({ rowId: rid })));
    builder.addSection(genSection);

    // Section 4 — dismiss with categorical feedback + optional note. For a
    // surfaced thread this is how you confirm "correctly skipped" (noise) or
    // correct a miss (wrong_*), feeding the needs-reply tuning loop.
    var dismissSection = CardService.newCardSection().setHeader('Dismiss with feedback');
    var reasonInput = CardService.newSelectionInput()
      .setType(CardService.SelectionInputType.DROPDOWN).setFieldName('reason').setTitle('Reason');
    for (var i = 0; i < DISMISS_REASONS.length; i++) {
      reasonInput.addItem(DISMISS_REASONS[i][1], DISMISS_REASONS[i][0], i === 0);
    }
    dismissSection.addWidget(reasonInput);
    dismissSection.addWidget(CardService.newTextInput().setFieldName('note')
      .setTitle('Note (optional)').setHint('free-text, e.g. why this was wrong'));
    dismissSection.addWidget(CardService.newTextButton().setText('Dismiss')
      .setOnClickAction(CardService.newAction().setFunctionName('actDismiss').setParameters({ rowId: rid })));
    builder.addSection(dismissSection);
  }

  return builder.build();
}

// --- Actions ---------------------------------------------------------------

function actRegenerate(e) {
  var rowId = e.commonEventObject.parameters.rowId;
  var instruction = _formVal(e, 'instruction').trim();
  var payload = instruction ? { instruction: instruction } : {};
  var res = _api('post', '/api/agent/pending/' + rowId + '/regenerate', payload);
  if (res.code !== 200) { return _notify('Regenerate failed (' + res.code + ')'); }
  var data = JSON.parse(res.body);
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(instruction ? 'Re-drafted from your prompt' : 'Regenerated'))
    .setNavigation(_navAfter(e, _draftCard(data.row)))
    .build();
}

function actSaveEdit(e) {
  var rowId = e.commonEventObject.parameters.rowId;
  var edited = _formVal(e, 'edited').trim();
  if (!edited) { return _notify('Nothing to save'); }
  var res = _api('post', '/api/agent/pending/' + rowId + '/amend', { amended_draft: edited });
  if (res.code !== 200) { return _notify('Save failed (' + res.code + ')'); }
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Saved'))
    .setNavigation(_navAfter(e, _draftCard(JSON.parse(res.body).row))).build();
}

function actPush(e) {
  var rowId = e.commonEventObject.parameters.rowId;
  // Honor in-card edits: if the editable draft field was changed, save it before
  // pushing so what you see is what lands in Gmail Drafts.
  var edited = _formVal(e, 'edited').trim();
  if (edited) { _api('post', '/api/agent/pending/' + rowId + '/amend', { amended_draft: edited }); }
  var res = _api('post', '/api/agent/pending/' + rowId + '/push_to_gmail', {});
  if (res.code !== 200) { return _notify('Push failed (' + res.code + ')'); }
  var data = JSON.parse(res.body);
  var msg = data.pushed_already ? 'Already in Gmail Drafts' : 'Pushed to Gmail Drafts';
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(msg))
    .setNavigation(_navAfter(e, _draftCard(data.row))).build();
}

function actMarkSent(e) {
  var rowId = e.commonEventObject.parameters.rowId;
  var res = _api('post', '/api/agent/pending/' + rowId + '/mark_sent', {});
  if (res.code !== 200) { return _notify('Mark-sent failed (' + res.code + ')'); }
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Marked sent'))
    .setNavigation(_navAfter(e, _infoCard('Marked sent', 'Closed this row — you sent it yourself.'))).build();
}

// Dashboard Dismiss → a small reason card (the list has no room for a per-row
// dropdown), so the feedback signal is preserved without N dropdowns.
function actDismissAsk(e) {
  var p = e.commonEventObject.parameters;
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(
      _dismissReasonCard(p.rowId, p.source, p.account))).build();
}

function _dismissReasonCard(rowId, source, account) {
  var section = CardService.newCardSection().setHeader('Dismiss with feedback');
  var reasonInput = CardService.newSelectionInput()
    .setType(CardService.SelectionInputType.DROPDOWN).setFieldName('reason').setTitle('Reason');
  for (var i = 0; i < DISMISS_REASONS.length; i++) {
    reasonInput.addItem(DISMISS_REASONS[i][1], DISMISS_REASONS[i][0], i === 0);
  }
  section.addWidget(reasonInput);
  section.addWidget(CardService.newTextInput().setFieldName('note')
    .setTitle('Note (optional)').setHint('free-text, e.g. why this was wrong'));
  var bs = CardService.newButtonSet();
  bs.addButton(_btn('Confirm dismiss', 'actDismiss', { rowId: String(rowId), source: source || '', account: account || '' }));
  bs.addButton(_btn('Cancel', 'actRefreshDash', { account: account || '' }));
  section.addWidget(bs);
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle('Dismiss')).addSection(section).build();
}

function actDismiss(e) {
  var p = e.commonEventObject.parameters;
  var rowId = p.rowId;
  var reason = _formVal(e, 'reason') || 'noise';
  var note = _formVal(e, 'note').trim();
  var payload = { reason: reason };
  if (note) { payload.note = note; }
  var res = _api('post', '/api/agent/pending/' + rowId + '/dismiss', payload);
  if (res.code !== 200) { return _notify('Dismiss failed (' + res.code + ')'); }
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Dismissed (' + reason + ')'))
    .setNavigation(CardService.newNavigation().updateCard(_dismissedCard(rowId, p.source, p.account))).build();
}

// Post-dismiss card with an Undo (and Back to queue when it came from there).
function _dismissedCard(rowId, source, account) {
  var section = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph().setText('Dismissed. YouOS won’t resurface this thread.'));
  var bs = CardService.newButtonSet();
  bs.addButton(_btn('Undo', 'actRestore', { rowId: String(rowId), source: source || '', account: account || '' }));
  if (source === 'dashboard') { bs.addButton(_btn('← Back to queue', 'actRefreshDash', { account: account || '' })); }
  section.addWidget(bs);
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle('Dismissed')).addSection(section).build();
}

function actRestore(e) {
  var p = e.commonEventObject.parameters;
  var res = _api('post', '/api/agent/pending/' + p.rowId + '/restore', {});
  if (res.code !== 200) { return _notify('Undo failed (' + res.code + ')'); }
  var back = (p.source === 'dashboard')
    ? _dashboardCard(p.account || _dashAccount())
    : _draftCard(JSON.parse(res.body).row);
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Restored'))
    .setNavigation(CardService.newNavigation().updateCard(back)).build();
}

function actApproveEvent(e) {
  var eventId = e.commonEventObject.parameters.eventId;
  var res = _api('post', '/api/agent/events/' + eventId + '/approve', {});
  if (res.code === 403) {
    // A shut gate (send frontier / create_events flag). Surface the reason so
    // the user knows it's off by default and how to enable it.
    var reason = '';
    try { reason = JSON.parse(res.body).detail || ''; } catch (err) { reason = ''; }
    return CardService.newActionResponseBuilder()
      .setNotification(CardService.newNotification().setText('Blocked: ' + (reason || 'event creation is disabled')))
      .build();
  }
  if (res.code !== 200) { return _notify('Create failed (' + res.code + ')'); }
  var data = JSON.parse(res.body);
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(
      data.meet_link ? 'Event created — Meet link ready' : 'Calendar event created'))
    .setNavigation(_navAfter(e, _eventCard(data.event)))
    .build();
}

function actDismissEvent(e) {
  var eventId = e.commonEventObject.parameters.eventId;
  var res = _api('post', '/api/agent/events/' + eventId + '/dismiss',
    { note: 'dismissed from the Gmail add-on' });
  if (res.code !== 200) { return _notify('Dismiss failed (' + res.code + ')'); }
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Event dismissed'))
    .setNavigation(_navAfter(e, _infoCard('Dismissed', 'YouOS won’t create this event.')))
    .build();
}

// After an action, go back to where it was invoked from: the dashboard (when a
// dashboard row triggered it) or the per-thread card otherwise.
function _navAfter(e, perThreadCard) {
  var p = (e.commonEventObject && e.commonEventObject.parameters) || {};
  var target = (p.source === 'dashboard') ? _dashboardCard(p.account || _dashAccount()) : perThreadCard;
  return CardService.newNavigation().updateCard(target);
}

function _notify(text) {
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(text)).build();
}

// --- Compose: insert YouOS's draft into the reply you're writing -----------

/**
 * Runs when you open the YouOS action while composing a reply. Looks up YouOS's
 * draft for the thread and offers to insert it into the compose body. Requires
 * draftAccess: METADATA (for e.gmail.threadId) + the compose scope.
 */
function onGmailCompose(e) {
  if (!_baseUrl() || !_token()) { return _settingsCard(); }
  var threadId = e && e.gmail && e.gmail.threadId; // populated on a reply
  var row = null;
  if (threadId) {
    try {
      var res = _api('get', '/api/agent/pending/by_thread/' + encodeURIComponent(threadId));
      if (res.code === 200) { row = JSON.parse(res.body); }
    } catch (err) { /* fall through to the empty card */ }
  }

  var section = CardService.newCardSection();
  var draftText = row ? (row.amended_draft || row.draft) : '';
  if (draftText) {
    section.addWidget(CardService.newTextParagraph().setText(_esc(draftText)));
    section.addWidget(CardService.newTextButton().setText('Insert into reply')
      .setTextButtonStyle(CardService.TextButtonStyle.FILLED)
      .setOnClickAction(CardService.newAction().setFunctionName('insertYouosDraft')
        .setParameters({ threadId: String(threadId) })));
  } else if (threadId) {
    // No draft yet → generate one on demand for THIS thread, then insert.
    section.addWidget(CardService.newTextParagraph().setText('No YouOS draft yet — generate one for this thread:'));
    section.addWidget(CardService.newTextInput().setFieldName('instruction')
      .setTitle('Optional instruction').setHint('e.g. shorter; decline politely').setMultiline(true));
    section.addWidget(CardService.newTextButton().setText('Draft a reply')
      .setTextButtonStyle(CardService.TextButtonStyle.FILLED)
      .setOnClickAction(CardService.newAction().setFunctionName('actComposeDraft')
        .setParameters({ threadId: String(threadId) })));
  } else {
    section.addWidget(CardService.newTextParagraph().setText('Open a reply to draft with YouOS.'));
  }
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle('Insert draft'))
    .addSection(section).build();
}

// Compose "Draft a reply": generate on demand for the open thread, then re-render
// the compose card so the new draft + Insert button appear.
function actComposeDraft(e) {
  var threadId = e.commonEventObject.parameters.threadId;
  var instruction = _formVal(e, 'instruction').trim();
  var payload = { thread_id: String(threadId) };
  if (instruction) { payload.instruction = instruction; }
  var res = _api('post', '/api/agent/draft_for_thread', payload);
  if (res.code !== 200) { return _notify('Draft failed (' + res.code + ')'); }
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Drafted'))
    .setNavigation(CardService.newNavigation().updateCard(
      onGmailCompose({ gmail: { threadId: threadId } }))).build();
}

/**
 * Inserts the draft into the current compose body. Re-fetches by threadId
 * (rather than passing the body as an action parameter, which is size-bounded),
 * converts newlines to <br>, and inserts at the cursor. Returns a no-op update
 * if the draft can't be loaded, so the compose action never errors out.
 */
function insertYouosDraft(e) {
  var threadId = e.commonEventObject.parameters.threadId;
  var html = '';
  try {
    var res = _api('get', '/api/agent/pending/by_thread/' + encodeURIComponent(threadId));
    if (res.code === 200) {
      var draft = JSON.parse(res.body).draft || '';
      html = _esc(draft).replace(/\n/g, '<br>');
    }
  } catch (err) { /* leave html empty → no-op insert */ }

  var update = CardService.newUpdateDraftBodyAction()
    .addUpdateContent(html, CardService.ContentType.MUTABLE_HTML)
    .setUpdateType(CardService.UpdateDraftBodyType.IN_PLACE_INSERT);
  return CardService.newUpdateDraftActionResponseBuilder()
    .setUpdateDraftBodyAction(update).build();
}

function _infoCard(title, body) {
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle(title))
    .addSection(CardService.newCardSection()
      .addWidget(CardService.newTextParagraph().setText(_esc(body))))
    .build();
}
