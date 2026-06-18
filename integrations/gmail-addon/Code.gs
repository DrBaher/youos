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

function onHomepage() { return _settingsCard(); }

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
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle('Settings'))
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

// --- Contextual: a Gmail message is open -----------------------------------

function onGmailMessage(e) {
  if (!_baseUrl() || !_token()) { return _settingsCard(); }
  var threadId = e && e.gmail && e.gmail.threadId;
  if (!threadId) { return _infoCard('No thread', 'Open a conversation to see its YouOS draft.'); }

  var res;
  try {
    res = _api('get', '/api/agent/pending/by_thread/' + encodeURIComponent(threadId));
  } catch (err) {
    return _infoCard('Can’t reach YouOS', 'Check the Funnel URL in Settings.\n\n' + _esc(err));
  }
  if (res.code === 404) { return _infoCard('Nothing queued', 'YouOS has no draft for this thread.'); }
  if (res.code === 401 || res.code === 403) { return _infoCard('Auth failed', 'Check your API token in Settings.'); }
  if (res.code !== 200) { return _infoCard('YouOS error ' + res.code, _esc(res.body).slice(0, 300)); }

  return _draftCard(JSON.parse(res.body));
}

function _draftCard(row) {
  var conf = (row.calibrated_score != null) ? row.calibrated_score : row.needs_reply_score;
  var subtitle = (row.tier === 'draft' ? 'Drafted' : 'Surfaced') +
    (conf != null ? ' · ' + Math.round(conf * 100) + '% likely to deserve a reply' : '') +
    ' · ' + (row.status || 'pending');

  var section = CardService.newCardSection();
  if (row.draft) {
    section.addWidget(CardService.newTextParagraph().setText(_esc(row.draft)));
  } else {
    section.addWidget(CardService.newTextParagraph().setText('<i>Surfaced for review — no draft generated.</i>'));
  }
  var reasons = (row.reasons || []).slice(0, 4).join(' · ');
  if (reasons) {
    section.addWidget(CardService.newDecoratedText()
      .setText('<font color="#5f6368">' + _esc(reasons) + '</font>').setWrapText(true));
  }
  if (row.gmail_draft_id) {
    section.addWidget(CardService.newDecoratedText()
      .setText('<font color="#1a73e8">This draft is in your Gmail Drafts, ready to send.</font>')
      .setWrapText(true));
  }

  var buttons = CardService.newButtonSet();
  if (row.status === 'pending' || row.status === 'amended') {
    buttons.addButton(CardService.newTextButton().setText('Regenerate')
      .setOnClickAction(CardService.newAction().setFunctionName('actRegenerate')
        .setParameters({ rowId: String(row.id) })));
    buttons.addButton(CardService.newTextButton().setText('Dismiss')
      .setOnClickAction(CardService.newAction().setFunctionName('actDismiss')
        .setParameters({ rowId: String(row.id) })));
  }
  section.addWidget(buttons);

  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS draft').setSubtitle(subtitle))
    .addSection(section).build();
}

// --- Actions ---------------------------------------------------------------

function actRegenerate(e) {
  var rowId = e.commonEventObject.parameters.rowId;
  var res = _api('post', '/api/agent/pending/' + rowId + '/regenerate', {});
  if (res.code !== 200) { return _notify('Regenerate failed (' + res.code + ')'); }
  var data = JSON.parse(res.body);
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Regenerated'))
    .setNavigation(CardService.newNavigation().updateCard(_draftCard(data.row)))
    .build();
}

function actDismiss(e) {
  var rowId = e.commonEventObject.parameters.rowId;
  var res = _api('post', '/api/agent/pending/' + rowId + '/dismiss',
    { reason: 'noise', note: 'dismissed from the Gmail add-on' });
  if (res.code !== 200) { return _notify('Dismiss failed (' + res.code + ')'); }
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Dismissed'))
    .setNavigation(CardService.newNavigation().updateCard(
      _infoCard('Dismissed', 'YouOS won’t resurface this thread.')))
    .build();
}

function _notify(text) {
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(text)).build();
}

function _infoCard(title, body) {
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('YouOS').setSubtitle(title))
    .addSection(CardService.newCardSection()
      .addWidget(CardService.newTextParagraph().setText(_esc(body))))
    .build();
}
