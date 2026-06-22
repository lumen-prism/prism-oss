// chat-page.js — Chat tab (列表 + 详情 + chat edit modal)
// Split out of app.html on 2026-05-22.
//
// Exposes globals (functions are hoisted, top-level let is shared across classic scripts):
//   State: chSub, activeChat, chatRefreshTimer, lastChatFingerprint
//   List:  renderChatRows, buildChatRow, attachChatRowGestures, openChatActionSheet
//   Detail: enterChatDetail, exitChatDetail, renderChatMessages
//   Helpers: chatDisplayLabel, avatarUrl, buildAssistantActions
//   Scroll: chScrollToBottom, updateChScrollBottomBtn
//   Render: buildToolGroup, chatFingerprint
//   Input: chInputAutoGrow, chInputKey, openChatAttach, openChatModelPicker
//   Misc:  renderChatAttachRowFromShared, switchToChatFromCode, switchToCodeFromChat
//   Modal: openChatEdit, openChatEditFromDetail, closeChatEditModal
//
// Depends on (defined in app.html inline script):
//   API, TOKEN, sessions, activeSession, switchView, escapeHTML, KIND_LABEL, KIND_AV,
//   chMdRender (chat-md.js), wireCodeBlocks (chat-code.js), etc.

// === Chats list + detail ===
let chSub = 'list';   // 'list' | 'detail'
let activeChat = null;
let chatRefreshTimer = null;
let lastChatFingerprint = null;
let _chRealtimeSource = null;
let _chRealtimeSession = null;
let _chRealtimeRefreshTimer = null;
let _chRenderRequestSeq = 0;
let _chRenderInFlightKey = null;
let _chRenderQueuedKey = null;
const _CH_REVEAL_TICK_MS = 85;
const _CH_REVEAL_MIN_CHARS = 8;
const _CH_REVEAL_MAX_CHARS = 44;
const _CH_SEARCH_HIGHLIGHT_MS = 5200;
let _chRevealTimer = null;
let _chRevealSession = null;
let _chRevealPrimed = false;
const _chSeenLiveMessages = new Set();
const _chRevealStates = new Map();
// Pagination state for the "load earlier" button. chMsgLimit grows by chunks each
// click; the polling fetch reuses the bumped value so we don't shrink history
// back. _chPendingLoadEarlier is a one-shot flag so the next render uses the
// prepend-aware scroll restore instead of the keep-position one.
const _CH_DEFAULT_MSG_LIMIT = 80;
let chMsgLimit = _CH_DEFAULT_MSG_LIMIT;
let _chPendingLoadEarlier = false;
const _CH_PENDING_STORAGE_KEY = 'chPendingMessages:v1';
const _CH_SEND_MODE_KEY = 'chSendMode:v1';
const _CH_ORPHANED_SENDING_MS = 60 * 1000;
const _CH_UNCONFIRMED_PENDING_MS = 5 * 60 * 1000;
let _chPendingMessages = (() => {
  try {
    const rows = JSON.parse(localStorage.getItem(_CH_PENDING_STORAGE_KEY) || '[]');
    return Array.isArray(rows) ? rows.filter(row => row && row.session && row.id) : [];
  } catch (e) {
    return [];
  }
})();
const _chLiveMessageCache = new Map();
// Archive panel state: collapsed by default, list cached after first expand so
// reopening doesn't re-fetch. When chViewingArchive is set, the detail view is
// reading an archived session's JSONL instead of a live tmux session.
let chArchiveExpanded = false;
let chArchiveList = null;       // null = not yet loaded; [] = loaded but empty
let chViewingArchive = null;    // archive_id string when in archive detail view
const CH_DRAFTS_KEY = 'chDrafts:v1';
let chViewingUnified = null;    // unified store session_id when viewing from message store
// Tracks whether the user is currently pinned to the bottom of #chMsgs. Updated
// from the scroll listener and after every render. Used by switchView() to
// restore the pin when returning to the chats tab — iOS Safari can reset
// scrollTop on display:none/flex toggles.
let _chWasAtBottom = true;

// === Search: global on Chats list and scoped inside one conversation ===
let _chSearchTimer = null;
let _chSearchActive = false;
let _chSearchType = 'all';
let _chSearchScope = 'all';
let _chSearchRequestSeq = 0;
const _CH_SEARCH_PAGE_SIZE = 100;
let _chDetailSearchTimer = null;
let _chDetailSearchType = 'all';
let _chDetailSearchSessionId = null;
let _chDetailSearchOverlaySessionId = null;
let _chPendingFocusMessageId = null;
let _chPendingFocusSourceUuid = null;
let _chLiveFocusSourceUuid = null;
let _chLiveFocusMessageId = null;
let _chSearchReturnState = null;
let _chPendingPinBottom = false;
let _chPendingOpenCompactionUuid = null;
let _chLatestCompactionOpen = false;
let _chLatestCompactionUuid = null;
let _chCompactionHistoryOpen = false;
let _chCompactionHistorySignature = null;
let _chCompactionRecordsLoaded = false;

function _chMessageKey(message, index) {
  const stable = message.source_uuid || message.id;
  if (stable) return String(stable);
  const blockTypes = (message.blocks || []).map(block => block.type || '').join(',');
  return [message.role || '', message.ts || '', blockTypes].join(':');
}
function _chAssistantText(message) {
  return (message.blocks || [])
    .filter(block => block.type === 'text')
    .map(block => block.text || '')
    .join('\n\n')
    .trim();
}
function _chNextRevealEnd(text, shown) {
  if (shown >= text.length) return text.length;
  const minEnd = Math.min(text.length, shown + _CH_REVEAL_MIN_CHARS);
  const maxEnd = Math.min(text.length, shown + _CH_REVEAL_MAX_CHARS);
  for (let index = minEnd; index < maxEnd; index += 1) {
    if (/[\n。！？!?；;，,]/.test(text[index])) return index + 1;
  }
  return maxEnd;
}
function _chResetRevealState(session = null) {
  if (_chRevealTimer) {
    clearTimeout(_chRevealTimer);
    _chRevealTimer = null;
  }
  _chRevealSession = session;
  _chRevealPrimed = false;
  _chSeenLiveMessages.clear();
  _chRevealStates.clear();
}
function _chRevealIsRunning() {
  return Array.from(_chRevealStates.values()).some(state => !state.done);
}
function _chRevealFingerprint() {
  return Array.from(_chRevealStates.entries())
    .map(([key, state]) => key + ':' + state.shown + ':' + state.text.length)
    .join('|');
}
function _chQueueNewReplyReveal(name, messages) {
  if (chViewingArchive || chViewingUnified || _chRevealSession !== name) return;
  if (!_chRevealPrimed) {
    messages.forEach((message, index) => _chSeenLiveMessages.add(_chMessageKey(message, index)));
    _chRevealPrimed = true;
    return;
  }
  if (_chPendingLoadEarlier || _chLiveFocusSourceUuid || _chLiveFocusMessageId) return;
  messages.forEach((message, index) => {
    const key = _chMessageKey(message, index);
    if (_chSeenLiveMessages.has(key)) return;
    _chSeenLiveMessages.add(key);
    const text = message.role === 'assistant' ? _chAssistantText(message) : '';
    if (!text) return;
    const shown = _chNextRevealEnd(text, 0);
    _chRevealStates.set(key, {
      text,
      shown,
      done: shown >= text.length,
    });
  });
  _chScheduleRevealTick(name);
}
function _chScheduleRevealTick(name) {
  if (_chRevealTimer || !_chRevealIsRunning()) return;
  _chRevealTimer = setTimeout(() => {
    _chRevealTimer = null;
    if (chSub !== 'detail' || activeChat !== name || _chRevealSession !== name || chViewingArchive || chViewingUnified) return;
    _chRevealStates.forEach(state => {
      if (state.done) return;
      state.shown = _chNextRevealEnd(state.text, state.shown);
      state.done = state.shown >= state.text.length;
    });
    const cached = _chLiveMessageCache.get(name);
    if (cached) renderChatMessages(name, cached);
    _chScheduleRevealTick(name);
  }, _CH_REVEAL_TICK_MS);
}

function _chSavePendingMessages() {
  try {
    if (_chPendingMessages.length) localStorage.setItem(_CH_PENDING_STORAGE_KEY, JSON.stringify(_chPendingMessages));
    else localStorage.removeItem(_CH_PENDING_STORAGE_KEY);
  } catch (e) { /* localStorage may be unavailable in private browsing */ }
}
function _chMarkOrphanedSending(now = Date.now()) {
  let changed = false;
  _chPendingMessages.forEach(message => {
    if (!Number.isFinite(Number(message.createdAt))) return;
    const age = now - Number(message.createdAt);
    const submitTimedOut = message.status === 'sending' && age > _CH_ORPHANED_SENDING_MS;
    const receiptTimedOut = (message.status === 'pending' || message.status === 'queued' || message.status === 'direct') && age > _CH_UNCONFIRMED_PENDING_MS;
    if (submitTimedOut || receiptTimedOut) {
      // After navigation or a service/login interruption the browser no longer
      // knows whether this request reached tmux. Do not present it as in flight.
      message.status = 'unconfirmed';
      changed = true;
    }
  });
  if (changed) _chSavePendingMessages();
}
function _chPendingForSession(session) {
  _chMarkOrphanedSending();
  return _chPendingMessages.filter(message => message.session === session);
}
function _chPendingAdd(session, text, attachments, options = {}) {
  const createdAt = Date.now();
  const pending = {
    id: 'pending-' + createdAt + '-' + Math.random().toString(36).slice(2, 8),
    session,
    text: text || '',
    attachments: attachments || [],
    createdAt,
    ts: new Date(createdAt).toISOString(),
    status: 'sending',
    sendMode: options.sendMode || _chCurrentSendMode(),
    placement: options.placement || ((options.sendMode || _chCurrentSendMode()) === 'direct' ? 'terminalTail' : 'queueTail'),
  };
  _chPendingMessages.push(pending);
  _chSavePendingMessages();
  return pending;
}
function _chPendingUpdate(id, status) {
  const pending = _chPendingMessages.find(message => message.id === id);
  if (pending) {
    pending.status = status;
    _chSavePendingMessages();
  }
  return pending;
}
function _chPendingRemove(id) {
  _chPendingMessages = _chPendingMessages.filter(message => message.id !== id);
  _chSavePendingMessages();
}
function _chInvalidateLiveChat(name, options = {}) {
  if (!name) return;
  _chLiveMessageCache.delete(name);
  lastChatFingerprint = null;
  if (options.pinBottom) _chPendingPinBottom = true;
}
function _chScheduleReceiptPolls(session = activeChat) {
  const targetSession = session;
  [300, 800, 1600, 3000].forEach(delay => setTimeout(() => {
    if (chSub !== 'detail' || !targetSession || activeChat !== targetSession) return;
    _chInvalidateLiveChat(targetSession, { pinBottom: true });
    renderChatMessages(targetSession);
  }, delay));
}
function _chCurrentSendMode() {
  try {
    return localStorage.getItem(_CH_SEND_MODE_KEY) === 'direct' ? 'direct' : 'queue';
  } catch {
    return 'queue';
  }
}
function chToggleSendMode() {
  const next = _chCurrentSendMode() === 'direct' ? 'queue' : 'direct';
  try { localStorage.setItem(_CH_SEND_MODE_KEY, next); } catch {}
  chUpdateSendModeButton();
}
function chUpdateSendModeButton() {
  const btn = document.getElementById('chSendModeBtn');
  const label = document.getElementById('chSendModeLabel');
  if (!btn || !label) return;
  const mode = _chCurrentSendMode();
  btn.classList.toggle('direct', mode === 'direct');
  btn.title = mode === 'direct' ? '直达终端：不等队列，直接输入当前终端' : '队列发送：等待当前对话结束后投递';
  btn.setAttribute('aria-label', btn.title);
  label.textContent = mode === 'direct' ? '直达' : '队列';
}
function _chHasLiveWork(messages) {
  const groupHasPending = (g) => (g.tools || []).some(tool => !tool.done);
  return (messages || []).slice(-8).some(message => (message.blocks || []).some(block => {
    if (block.type === 'tool_group') return groupHasPending(block);
    if (block.type === 'process_group') {
      return (block.children || []).some(child =>
        child && child.type === 'tool_group' && groupHasPending(child)
      );
    }
    return false;
  }));
}
function _chScheduleChatRefresh(delay = 1500) {
  if (chatRefreshTimer) clearTimeout(chatRefreshTimer);
  chatRefreshTimer = null;
  if (chSub !== 'detail' || !activeChat || chViewingArchive || chViewingUnified) return;
  chatRefreshTimer = setTimeout(() => {
    chatRefreshTimer = null;
    if (chSub === 'detail' && activeChat) renderChatMessages(activeChat);
  }, delay);
}
function _chStopRealtimeEvents() {
  if (_chRealtimeRefreshTimer) {
    clearTimeout(_chRealtimeRefreshTimer);
    _chRealtimeRefreshTimer = null;
  }
  if (_chRealtimeSource) {
    try { _chRealtimeSource.close(); } catch (e) {}
  }
  _chRealtimeSource = null;
  _chRealtimeSession = null;
}
function _chRealtimeRefresh(name) {
  if (!name || chSub !== 'detail' || activeChat !== name || chViewingArchive || chViewingUnified) return;
  if (_chRealtimeRefreshTimer) clearTimeout(_chRealtimeRefreshTimer);
  _chRealtimeRefreshTimer = setTimeout(() => {
    _chRealtimeRefreshTimer = null;
    if (chSub !== 'detail' || activeChat !== name || chViewingArchive || chViewingUnified) return;
    _chInvalidateLiveChat(name);
    renderChatMessages(name);
  }, 60);
}
function _chStartRealtimeEvents(name) {
  const s = sessions.find(x => x.name === name);
  if (!s || (s.kind !== 'cc' && s.kind !== 'codex' && s.kind !== 'opencode')) {
    _chStopRealtimeEvents();
    return;
  }
  if (_chRealtimeSource && _chRealtimeSession === name) return;
  _chStopRealtimeEvents();
  if (!window.EventSource || !TOKEN) return;
  const url = API + '/sessions/' + encodeURIComponent(name) + '/chat-events?token=' + encodeURIComponent(TOKEN);
  const source = new EventSource(url);
  _chRealtimeSource = source;
  _chRealtimeSession = name;
  source.addEventListener('chat_update', () => _chRealtimeRefresh(name));
  source.onmessage = () => _chRealtimeRefresh(name);
  source.onerror = () => {
    // Keep the normal polling loop as fallback; EventSource will retry itself.
    if (_chRealtimeSession !== name) {
      try { source.close(); } catch (e) {}
    }
  };
}

function _chDetachHiddenCodeTerminalFor(name) {
  if (typeof termSession === 'undefined' || !termSession || termSession === name) return;
  if (typeof sse !== 'undefined' && sse) {
    try { sse.close(); } catch (e) {}
    sse = null;
  }
  if (typeof _switchGen !== 'undefined') _switchGen += 1;
  termSession = null;
}

function _chEditPending(pending) {
  if (!pending || activeChat !== pending.session) return;
  const input = document.getElementById('chInput');
  if (!input) return;
  const hasDraft = input.value.trim() || pendingAtts.length;
  if (hasDraft && !confirm('输入框里已有内容，替换为这条消息继续编辑吗？')) return;
  activeSession = pending.session;
  if (typeof clearAttachments === 'function') clearAttachments();
  pendingAtts = (pending.attachments || []).map(attachment => ({
    id: ++attSeq,
    name: attachment.fname,
    isImage: Boolean(attachment.isImage),
    blobUrl: attachment.isImage
      ? API + '/sessions/' + encodeURIComponent(attachment.session) + '/uploads/' + encodeURIComponent(attachment.fname) + '?token=' + encodeURIComponent(TOKEN)
      : null,
    path: '/tmp/dashboard-uploads/' + attachment.session + '/' + attachment.fname,
    uploading: false,
    error: false,
  }));
  if (typeof renderAttachments === 'function') renderAttachments();
  input.value = pending.text || '';
  chInputAutoGrow(input);
  _chPendingRemove(pending.id);
  const bubble = document.querySelector('[data-pending-id="' + CSS.escape(pending.id) + '"]');
  if (bubble) bubble.remove();
  chFocusComposer();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}
function _chComposerMatchesPending(pending) {
  const input = document.getElementById('chInput');
  if (!input || input.value !== (pending.text || '')) return false;
  const currentFiles = pendingAtts
    .filter(attachment => attachment.path && !attachment.error)
    .map(attachment => attachment.path.split('/').pop() || attachment.name)
    .sort();
  const pendingFiles = (pending.attachments || []).map(attachment => attachment.fname).sort();
  return currentFiles.length === pendingFiles.length && currentFiles.every((name, index) => name === pendingFiles[index]);
}
async function _chSubmitPrivateMessage(pending) {
  const prefix = (pending.attachments || [])
    .map(attachment => '@/tmp/dashboard-uploads/' + attachment.session + '/' + attachment.fname)
    .join(' ');
  const sentAt = new Date().toISOString();
  const taggedText = '<chat-input source="prism-chat" sent_at="' + sentAt + '" />\n' + (pending.text || '');
  const payload = prefix ? prefix + ' ' + taggedText : taggedText;
  const endpoint = pending.sendMode === 'direct' ? '/input' : '/chat-send';
  try {
    const response = await fetch(API + '/sessions/' + encodeURIComponent(pending.session) + endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ data: payload + '\r' }),
    });
    if (response.status === 401) {
      localStorage.removeItem('prism_token');
      TOKEN = '';
      if (typeof showLogin === 'function') showLogin();
      return { ok: false };
    }
    if (!response.ok) return { ok: false };
    const data = await response.json().catch(() => ({}));
    return { ok: true, queued: Boolean(data.queued), direct: pending.sendMode === 'direct' };
  } catch (error) {
    console.warn('chat send failed', error);
    return { ok: false, unconfirmed: true };
  }
}
function _chUploadSessionFromPath(path, fallback) {
  const match = String(path || '').match(/\/tmp\/dashboard-uploads\/([^/]+)\//);
  return match ? match[1] : fallback;
}
async function _chRetryPending(pending) {
  if (!pending || _sendInFlight || activeChat !== pending.session) return;
  const clearMirroredDraft = _chComposerMatchesPending(pending);
  activeSession = pending.session;
  const replacement = _chPendingAdd(
    pending.session,
    pending.text,
    (pending.attachments || []).map(attachment => ({ ...attachment })),
    {
      sendMode: pending.sendMode || _chCurrentSendMode(),
      placement: pending.placement || ((pending.sendMode || _chCurrentSendMode()) === 'direct' ? 'terminalTail' : 'queueTail'),
    },
  );
  _chPendingRemove(pending.id);
  const oldBubble = document.querySelector('[data-pending-id="' + CSS.escape(pending.id) + '"]');
  if (oldBubble) oldBubble.remove();
  _chShowPendingImmediately(replacement);

  _sendInFlight = true;
  const send = document.getElementById('chSendBtn');
  if (send) send.disabled = true;
  const result = await _chSubmitPrivateMessage(replacement);
  _sendInFlight = false;
  if (send) send.disabled = false;
  _chPendingUpdate(replacement.id, result.ok ? (result.direct ? 'direct' : (result.queued ? 'queued' : 'pending')) : (result.unconfirmed ? 'unconfirmed' : 'failed'));
  _chShowPendingImmediately(replacement);
  if (result.ok) {
    if (clearMirroredDraft) {
      const input = document.getElementById('chInput');
      if (input) {
        input.value = '';
        chInputAutoGrow(input);
      }
      if (typeof clearAttachments === 'function') clearAttachments();
    }
    _chInvalidateLiveChat(replacement.session, { pinBottom: true });
    if (activeChat === replacement.session) renderChatMessages(replacement.session);
    _chScheduleReceiptPolls(replacement.session);
  }
}
function _chNormalizePendingText(text) {
  return (text || '')
    .replace(/<chat-input\b[^>]*\/>\s*/gi, '')
    .replace(/@?\/tmp\/dashboard-uploads\/[^\s]+/gi, '')
    .replace(/\s+/g, ' ')
    .trim();
}
function _chMessageText(message) {
  return (message.blocks || []).filter(block => block.type === 'text').map(block => block.text || '').join('\n');
}
function _chPendingMatchesMessage(pending, message) {
  if (message.role !== 'user') return false;
  const messageMs = _chParseTs(message.ts) * 1000;
  if (messageMs && messageMs < pending.createdAt - 5000) return false;
  if (messageMs && messageMs > pending.createdAt + 10 * 60 * 1000) return false;
  const wanted = _chNormalizePendingText(pending.text);
  const actual = _chNormalizePendingText(_chMessageText(message));
  if (wanted && wanted !== actual && !_chPendingTextCloseEnough(wanted, actual)) return false;
  const fileNames = (message.blocks || []).map(block => block.fname || '').filter(Boolean);
  const attachmentNames = (pending.attachments || []).map(attachment => attachment.fname);
  if (attachmentNames.length && !attachmentNames.every(name => fileNames.includes(name) || _chMessageText(message).includes(name))) {
    return false;
  }
  return Boolean(wanted || attachmentNames.length);
}

function _chPendingTextCloseEnough(wanted, actual) {
  if (!wanted || !actual) return false;
  if (wanted.includes(actual) || actual.includes(wanted)) return Math.min(wanted.length, actual.length) >= 6;
  if (Math.abs(wanted.length - actual.length) > 2) return false;
  const rows = Array.from({ length: wanted.length + 1 }, (_, i) => i);
  for (let i = 1; i <= actual.length; i += 1) {
    let prev = rows[0];
    rows[0] = i;
    for (let j = 1; j <= wanted.length; j += 1) {
      const old = rows[j];
      rows[j] = actual[i - 1] === wanted[j - 1]
        ? prev
        : Math.min(prev, rows[j], rows[j - 1]) + 1;
      prev = old;
    }
    if (Math.min(...rows) > 2) return false;
  }
  return rows[wanted.length] <= 2 && Math.max(wanted.length, actual.length) >= 6;
}
function _chReconcilePending(session, messages) {
  const pending = _chPendingForSession(session);
  if (!pending.length) return pending;
  const users = messages.filter(message => message.role === 'user');
  const used = new Set();
  let changed = false;
  pending.filter(message => message.status !== 'failed').sort((a, b) => a.createdAt - b.createdAt).forEach(message => {
    const matchIndex = users.findIndex((candidate, index) => !used.has(index) && _chPendingMatchesMessage(message, candidate));
    if (matchIndex >= 0) {
      used.add(matchIndex);
      _chPendingRemove(message.id);
      changed = true;
    }
  });
  if (changed) _chSavePendingMessages();
  return _chPendingForSession(session);
}
function _chBuildPendingBubble(pending) {
  const bubble = document.createElement('div');
  bubble.className = 'ch-bubble user pending ' + pending.status;
  bubble.dataset.pendingId = pending.id;
  (pending.attachments || []).forEach(attachment => {
    if (!attachment.isImage) return;
    const img = document.createElement('img');
    img.src = API + '/sessions/' + encodeURIComponent(attachment.session) + '/uploads/' + encodeURIComponent(attachment.fname) + '?token=' + encodeURIComponent(TOKEN);
    img.alt = attachment.fname;
    bubble.appendChild(img);
  });
  const textParts = [];
  if ((pending.text || '').trim()) textParts.push(pending.text);
  (pending.attachments || []).filter(attachment => !attachment.isImage).forEach(attachment => textParts.push('附件: ' + attachment.fname));
  if (textParts.length) {
    const text = document.createElement('div');
    text.className = 'ch-text';
    text.innerHTML = chMdRenderUser(textParts.join('\n'));
    bubble.appendChild(text);
  }
  const state = document.createElement('div');
  state.className = 'ch-pending-state';
  state.textContent = pending.status === 'sending'
    ? '发送中...'
    : pending.status === 'queued'
      ? '排队中，等待当前对话结束...'
    : pending.status === 'direct'
      ? '已直达终端，等待记录确认...'
    : pending.status === 'failed'
      ? '发送失败'
      : pending.status === 'unconfirmed'
        ? '已送达终端，等待记录确认...'
        : '已送达终端，等待记录确认...';
  bubble.appendChild(state);
  if (pending.status === 'failed' || pending.status === 'unconfirmed') {
    const actions = document.createElement('div');
    actions.className = 'ch-pending-actions';
    const remove = document.createElement('button');
    remove.className = 'ch-pending-action';
    remove.type = 'button';
    remove.textContent = '移除';
    remove.onclick = () => {
      _chPendingRemove(pending.id);
      bubble.remove();
    };
    const edit = document.createElement('button');
    edit.className = 'ch-pending-action';
    edit.type = 'button';
    edit.textContent = '编辑';
    edit.onclick = () => _chEditPending(pending);
    const retry = document.createElement('button');
    retry.className = 'ch-pending-action retry';
    retry.type = 'button';
    retry.textContent = '重新发送';
    retry.onclick = () => _chRetryPending(pending);
    actions.appendChild(remove);
    actions.appendChild(edit);
    actions.appendChild(retry);
    bubble.appendChild(actions);
  }
  const timestamp = document.createElement('div');
  timestamp.className = 'ch-bubble-time';
  timestamp.textContent = _messageTimestamp(pending.ts);
  bubble.appendChild(timestamp);
  return bubble;
}
function _chShowPendingImmediately(pending) {
  if (chSub !== 'detail' || activeChat !== pending.session || chViewingArchive || chViewingUnified) return;
  const wrap = document.getElementById('chMsgs');
  const empty = document.getElementById('chEmpty');
  if (!wrap) return;
  const existing = wrap.querySelector('[data-pending-id="' + CSS.escape(pending.id) + '"]');
  const bubble = _chBuildPendingBubble(pending);
  if (existing) existing.replaceWith(bubble);
  else {
    const disclaimer = wrap.querySelector('.ch-disclaimer');
    if (disclaimer) wrap.insertBefore(bubble, disclaimer);
    else wrap.appendChild(bubble);
  }
  _chInvalidateLiveChat(pending.session, { pinBottom: true });
  if (empty) empty.style.display = 'none';
  _chPinChatBottomSoon(wrap);
}
function _chShowChatStartingState(session) {
  const wrap = document.getElementById('chMsgs');
  const empty = document.getElementById('chEmpty');
  if (!wrap) return;
  wrap.innerHTML = '';
  const pending = _chPendingForSession(session);
  pending.forEach(message => wrap.appendChild(_chBuildPendingBubble(message)));
  const loading = document.createElement('div');
  loading.className = 'ch-loading-state';
  loading.textContent = pending.length ? '正在载入聊天记录...' : '加载中...';
  wrap.appendChild(loading);
  if (empty) empty.style.display = 'none';
  wrap.scrollTop = wrap.scrollHeight;
}

function chSearchFocus() {
  if (_chSearchActive) return;
  const searchScope = document.getElementById('clSearchScope');
  const searchChips = document.getElementById('clSearchChips');
  const cancel = document.getElementById('clSearchCancel');
  const rows = document.getElementById('chRows');
  const chips = document.getElementById('chChips');
  const results = document.getElementById('clSearchResults');
  if (searchScope) searchScope.style.display = '';
  if (searchChips) searchChips.style.display = '';
  if (cancel) cancel.style.display = '';
  if (rows) rows.style.display = 'none';
  if (chips) chips.style.display = 'none';
  if (results) { results.style.display = ''; results.innerHTML = _chGlobalSearchPlaceholder(); }
  _chSearchActive = true;
}

function chGlobalSearchDebounced(val) {
  clearTimeout(_chSearchTimer);
  const clear = document.getElementById('clSearchClear');
  if (clear) clear.style.display = val ? '' : 'none';
  if (!val.trim() && _chSearchType === 'all') {
    const results = document.getElementById('clSearchResults');
    if (results) results.innerHTML = _chGlobalSearchPlaceholder();
    return;
  }
  _chSearchTimer = setTimeout(() => chRunGlobalSearch(false), 280);
}

function chSetSearchType(type) {
  _chSearchType = type;
  document.querySelectorAll('#clSearchChips .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.type === type));
  const q = (document.getElementById('clSearchInput')?.value || '').trim();
  if (q || type !== 'all') chRunGlobalSearch(false);
  else document.getElementById('clSearchResults').innerHTML = _chGlobalSearchPlaceholder();
}

function chSetSearchScope(scope) {
  _chSearchScope = scope || 'all';
  document.querySelectorAll('#clSearchScope .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.scope === _chSearchScope));
  const q = (document.getElementById('clSearchInput')?.value || '').trim();
  if (q || _chSearchType !== 'all') chRunGlobalSearch(false);
  else document.getElementById('clSearchResults').innerHTML = _chGlobalSearchPlaceholder();
}

function chClearGlobalSearch() {
  const input = document.getElementById('clSearchInput');
  if (input) { input.value = ''; input.blur(); }
  document.getElementById('clSearchClear')?.style.setProperty('display', 'none');
  document.getElementById('clSearchCancel')?.style.setProperty('display', 'none');
  document.getElementById('clSearchScope')?.style.setProperty('display', 'none');
  document.getElementById('clSearchChips')?.style.setProperty('display', 'none');
  const results = document.getElementById('clSearchResults');
  if (results) { results.style.display = 'none'; results.innerHTML = ''; delete results.dataset.searchRequestSeq; }
  document.getElementById('chRows')?.style.removeProperty('display');
  document.getElementById('chChips')?.style.removeProperty('display');
  _chSearchActive = false;
  _chSearchType = 'all';
  _chSearchScope = 'all';
  document.querySelectorAll('#clSearchChips .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.type === 'all'));
  document.querySelectorAll('#clSearchScope .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.scope === 'all'));
}

function _chGlobalSearchPlaceholder() {
  const scope = _chSearchScope === 'current'
    ? '当前聊天'
    : '所有记录';
  return '<div class="cl-sr-loading">在' + escapeHTML(scope) + '中输入关键词搜索，或选择图片、文件、链接</div>';
}

async function _chResolveGlobalSearchTarget() {
  if (_chSearchScope !== 'current') {
    return { sessionId: null, overlaySessionId: null, label: '所有记录' };
  }
  if (!activeChat) return { error: '还没有选中当前聊天' };
  try {
    const r = await fetch(API + '/sessions/' + encodeURIComponent(activeChat) + '/search-id', {
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    if (!r.ok) return { error: '当前聊天还没有可搜索的索引' };
    const data = await r.json();
    return {
      sessionId: data.session_id,
      overlaySessionId: data.overlay_session_id || null,
      label: chatDisplayLabel((sessions || []).find(s => s.name === activeChat) || { name: activeChat }),
    };
  } catch {
    return { error: '当前聊天索引加载失败' };
  }
}

async function chRunGlobalSearch(append = false) {
  const container = document.getElementById('clSearchResults');
  if (!container) return;
  const q = (document.getElementById('clSearchInput')?.value || '').trim();
  if (!q && _chSearchType === 'all') {
    container.innerHTML = _chGlobalSearchPlaceholder();
    return;
  }
  if (!append) container.innerHTML = '<div class="cl-sr-loading">准备搜索范围...</div>';
  const target = await _chResolveGlobalSearchTarget();
  if (target.error) {
    container.innerHTML = '<div class="cl-sr-loading">' + escapeHTML(target.error) + '</div>';
    return;
  }
  return chRunSearch(q, _chSearchType, target.sessionId, container, target.overlaySessionId, {
    append,
    scopeLabel: target.label,
  });
}

async function chRunSearch(q, type, sessionId, container, overlaySessionId = null, options = {}) {
  if (!container) return;
  const requestSeq = String(++_chSearchRequestSeq);
  container.dataset.searchRequestSeq = requestSeq;
  const stillCurrent = () => container.dataset.searchRequestSeq === requestSeq;
  const append = !!options.append;
  const offset = append ? Number(container.dataset.nextOffset || 0) : 0;
  if (!append) container.innerHTML = '<div class="cl-sr-loading">搜索中...</div>';
  try {
    let url = API + '/messages/search?limit=' + _CH_SEARCH_PAGE_SIZE + '&offset=' + encodeURIComponent(offset);
    if (q) url += '&q=' + encodeURIComponent(q);
    if (type && type !== 'all') url += '&type=' + encodeURIComponent(type);
    if (sessionId) url += '&session_id=' + encodeURIComponent(sessionId);
    if (overlaySessionId) url += '&overlay_session_id=' + encodeURIComponent(overlaySessionId);
    const r = await fetch(url, { headers: { Authorization: 'Bearer ' + TOKEN } });
    if (!stillCurrent()) return;
    if (!r.ok) { container.innerHTML = '<div class="cl-sr-loading">搜索失败</div>'; return; }
    const data = await r.json();
    const incoming = data.results || [];
    if (!stillCurrent()) return;
    const items = append ? (container._searchItems || []).concat(incoming) : incoming;
    container._searchItems = items;
    container.dataset.nextOffset = String(data.next_offset || items.length);
    container.dataset.hasMore = data.has_more ? '1' : '0';
    if (!items.length) { container.innerHTML = '<div class="cl-sr-loading">没有找到相关内容</div>'; return; }
    _renderSearchResults(container, items, {
      q,
      type,
      hasMore: !!data.has_more,
      scopeLabel: options.scopeLabel,
      onMore: () => chRunSearch(q, type, sessionId, container, overlaySessionId, { ...options, append: true }),
    });
  } catch (e) {
    if (stillCurrent()) container.innerHTML = '<div class="cl-sr-loading">网络错误</div>';
  }
}

function _renderSearchResults(container, items, opts) {
  const type = opts.type;
  container.innerHTML = '';
  const summary = document.createElement('div');
  summary.className = 'cl-sr-summary';
  summary.textContent = (opts.scopeLabel ? opts.scopeLabel + ' · ' : '') + '已显示 ' + items.length + ' 条' + (opts.hasMore ? '，还有更多' : '');
  container.appendChild(summary);
  if (type === 'image') {
    _renderImageGrid(container, items);
    return _appendSearchLoadMore(container, opts);
  }
  if (type === 'file' || type === 'link') {
    _renderResourceList(container, items, type);
    return _appendSearchLoadMore(container, opts);
  }
  const grouped = new Map();
  items.forEach(m => {
    const nav = m.navigation || {};
    const sid = m.session_id;
    if (!grouped.has(sid)) grouped.set(sid, { name: _searchSessionName(m), source: m.source, messages: [] });
    grouped.get(sid).messages.push(m);
  });
  _renderGroupedList(container, grouped, opts.q);
  _appendSearchLoadMore(container, opts);
}

function _appendSearchLoadMore(container, opts) {
  if (!opts.hasMore || !opts.onMore) return;
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'cl-sr-loadmore';
  more.textContent = '加载更多结果';
  more.onclick = () => {
    more.disabled = true;
    more.textContent = '加载中...';
    opts.onMore();
  };
  container.appendChild(more);
}

function _searchSessionName(msg) {
  return (msg.navigation || {}).display_name || msg.session_name || msg.session_auto_name || (msg.session_id || '').slice(0, 12);
}
function _messageTimestamp(ts) {
  const parsed = _chParseTs(ts);
  if (!parsed) return '';
  const d = new Date(parsed * 1000);
  const pad = n => String(n).padStart(2, '0');
  return d.getFullYear() + '/' + pad(d.getMonth() + 1) + '/' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}
function _searchTime(ts) {
  return _messageTimestamp(ts);
}
function _displaySender(sender) {
  if (sender === 'user') return 'You';
  if (sender === 'assistant') return 'Assistant';
  return sender || '';
}
function _cleanSearchSnippet(content) {
  return (content || '')
    .replace(/@?\/tmp\/dashboard-uploads\/[^\s]+/gi, '')
    .replace(/\s+/g, ' ').trim();
}
function _resourceUrl(resource) {
  if (!resource) return null;
  if (resource.serve_url) return (resource.serve_url.startsWith('/api/') ? PREFIX + resource.serve_url : API + resource.serve_url) + (resource.serve_url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
  if (resource.upload_session && resource.filename) return API + '/sessions/' + encodeURIComponent(resource.upload_session) + '/uploads/' + encodeURIComponent(resource.filename) + '?token=' + encodeURIComponent(TOKEN);
  if (resource.path) return API + '/files/download?path=' + encodeURIComponent(resource.path) + '&token=' + encodeURIComponent(TOKEN);
  return null;
}
function _resources(items, kind) {
  const out = [];
  items.forEach(msg => (msg.resources || []).filter(r => r.kind === kind).forEach(resource => out.push({ msg, resource })));
  return out;
}

function _renderImageGrid(container, items) {
  const entries = _resources(items, 'image');
  if (!entries.length) { container.innerHTML = '<div class="cl-sr-loading">没有找到图片</div>'; return; }
  const grid = document.createElement('div');
  grid.className = 'cl-sr-grid';
  entries.forEach(({msg, resource}) => {
    const cell = document.createElement('button');
    cell.type = 'button';
    cell.className = 'cl-sr-grid-cell' + (resource.available ? '' : ' unavailable');
    const url = _resourceUrl(resource);
    if (resource.available && url) {
      const img = document.createElement('img');
      img.src = url; img.alt = resource.filename || ''; img.loading = 'lazy';
      img.onerror = () => { cell.classList.add('unavailable'); cell.replaceChildren(_unavailableAssetLabel()); };
      cell.appendChild(img);
    } else {
      cell.appendChild(_unavailableAssetLabel());
    }
    cell.onclick = () => _showImagePreview(resource, msg);
    grid.appendChild(cell);
  });
  container.appendChild(grid);
}
function _unavailableAssetLabel() {
  const label = document.createElement('span');
  label.className = 'cl-sr-missing';
  label.textContent = '原图已清理';
  return label;
}

async function _downloadPreviewImage(url, resource) {
  if (!url) return;
  const fallbackName = 'image-' + Date.now() + '.jpg';
  const filename = (resource && resource.filename ? resource.filename : fallbackName).replace(/[^a-zA-Z0-9._-]/g, '_');
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error('download failed');
    const blob = await response.blob();
    const blobUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = blobUrl;
    link.download = filename || fallbackName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
  } catch (e) {
    window.open(url, '_blank', 'noopener');
  }
}

function _showImagePreview(resource, msg) {
  let overlay = document.getElementById('clImagePreview');
  if (!overlay) { overlay = document.createElement('div'); overlay.id = 'clImagePreview'; overlay.className = 'cl-img-overlay'; document.body.appendChild(overlay); }
  const sessName = _searchSessionName(msg);
  const url = _resourceUrl(resource);
  overlay.innerHTML = '';
  const bg = document.createElement('div'); bg.className = 'cl-img-overlay-bg'; bg.onclick = () => { overlay.style.display = 'none'; };
  const body = document.createElement('div'); body.className = 'cl-img-overlay-content';
  if (resource.available && url) {
    const img = document.createElement('img'); img.src = url; img.alt = resource.filename || '';
    body.appendChild(img);
  } else {
    const missing = document.createElement('div'); missing.className = 'cl-img-preview-missing'; missing.textContent = '原图已被旧会话清理'; body.appendChild(missing);
  }
  const info = document.createElement('div'); info.className = 'cl-img-overlay-info';
  const text = document.createElement('span'); text.textContent = sessName + ' · ' + _searchTime(msg.ts); info.appendChild(text);
  if (resource.available && url) {
    const download = document.createElement('button');
    download.type = 'button';
    download.className = 'cl-img-download';
    download.title = '下载图片';
    download.setAttribute('aria-label', '下载图片');
    download.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><polyline points="7 10 12 15 17 10"/><path d="M4 20h16"/></svg>';
    download.onclick = () => _downloadPreviewImage(url, resource);
    info.appendChild(download);
  }
  const button = document.createElement('button');
  if (msg.session_id) {
    const state = (msg.navigation || {}).state;
    button.textContent = state === 'archived' || state === 'history' ? '查看只读聊天' : '进入聊天';
    button.onclick = () => { overlay.style.display = 'none'; chOpenSearchHit(msg, sessName); };
  } else {
    button.textContent = '关闭';
    button.onclick = () => { overlay.style.display = 'none'; };
  }
  info.appendChild(button); body.appendChild(info); overlay.appendChild(bg); overlay.appendChild(body); overlay.style.display = 'flex';
}

function _resourceStateLabel(msg) {
  const state = (msg.navigation || {}).state;
  if (state === 'archived') return '已归档 · 只读';
  if (state === 'history') return '历史记录 · 只读';
  return '';
}
function _searchSourceLabel(msg) {
  if (msg.source === 'codex') return 'Codex';
  if (msg.source === 'cc') return 'CC';
  return msg.source || '记录';
}
function _searchStateLabel(msg) {
  const nav = msg.navigation || {};
  if (nav.state === 'live') return '实时';
  if (nav.state === 'archived') return '归档';
  if (nav.state === 'history') return '历史';
  return '索引';
}
function _searchMetaLabel(msg) {
  return [_searchSourceLabel(msg), _searchStateLabel(msg)].filter(Boolean).join(' · ');
}
function _linkHost(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return 'LINK'; }
}
function _renderResourceList(container, items, kind) {
  const entries = _resources(items, kind);
  if (!entries.length) { container.innerHTML = '<div class="cl-sr-loading">没有找到可定位的内容</div>'; return; }
  const list = document.createElement('div'); list.className = 'cl-resource-list ' + kind;
  entries.forEach(({msg, resource}) => {
    const row = document.createElement('button');
    row.type = 'button'; row.className = 'cl-resource-row ' + kind;
    const title = kind === 'file' ? (resource.filename || '附件') : _linkHost(resource.url);
    row.innerHTML = '<span class="cl-resource-kind"></span><span class="cl-resource-body"><span class="cl-resource-head"><strong></strong><b class="cl-resource-sender"></b></span><small></small><label></label></span><span class="cl-resource-arrow">›</span>';
    const badge = row.querySelector('.cl-resource-kind');
    badge.textContent = kind === 'file' ? ((title.split('.').pop() || 'FILE').slice(0, 5).toUpperCase()) : title.slice(0, 2).toUpperCase();
    row.querySelector('strong').textContent = title;
    const sender = row.querySelector('.cl-resource-sender');
    sender.textContent = _displaySender(msg.sender);
    sender.style.display = kind === 'link' && sender.textContent ? '' : 'none';
    row.querySelector('small').textContent = kind === 'link' ? resource.url : (_displaySender(msg.sender) + ' · ' + _searchTime(msg.ts));
    const state = _resourceStateLabel(msg);
    const status = row.querySelector('label');
    status.textContent = kind === 'link' ? [_searchTime(msg.ts), state].filter(Boolean).join(' · ') : state;
    status.style.display = status.textContent ? '' : 'none';
    _attachResourceGestures(row, msg, resource, kind);
    list.appendChild(row);
  });
  container.appendChild(list);
}

function _attachResourceGestures(row, msg, resource, kind) {
  const LONG_MS = 520;
  let timer = null, startX = 0, startY = 0, held = false;
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
  const blockNativeSelection = (e) => { e.preventDefault(); };
  row.addEventListener('selectstart', blockNativeSelection);
  row.addEventListener('dragstart', blockNativeSelection);
  row.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX; startY = e.touches[0].clientY; held = false;
    cancel();
    timer = setTimeout(() => {
      held = true;
      const selection = window.getSelection && window.getSelection();
      if (selection) selection.removeAllRanges();
      if (typeof hapticTap === 'function') hapticTap();
      _openResourceActions(msg, resource, kind);
    }, LONG_MS);
  }, { passive: true });
  row.addEventListener('touchmove', (e) => {
    if (!e.touches.length) return;
    if (Math.abs(e.touches[0].clientX - startX) > 9 || Math.abs(e.touches[0].clientY - startY) > 9) cancel();
  }, { passive: true });
  row.addEventListener('touchend', cancel);
  row.addEventListener('touchcancel', cancel);
  row.addEventListener('contextmenu', (e) => { e.preventDefault(); _openResourceActions(msg, resource, kind); });
  row.onclick = (e) => {
    if (held) { held = false; e.preventDefault(); return; }
    if (kind === 'link') { window.open(resource.url, '_blank', 'noopener'); return; }
    const url = _resourceUrl(resource);
    if (resource.available && url) window.open(url, '_blank', 'noopener');
    else _openResourceActions(msg, resource, kind);
  };
}
function _closeResourceActions() {
  const overlay = document.getElementById('clResourceActions');
  if (overlay) overlay.style.display = 'none';
}
function _openResourceActions(msg, resource, kind) {
  let overlay = document.getElementById('clResourceActions');
  if (!overlay) { overlay = document.createElement('div'); overlay.id = 'clResourceActions'; overlay.className = 'cl-resource-actions'; document.body.appendChild(overlay); }
  overlay.innerHTML = '<div class="cl-resource-actions-bg"></div><div class="cl-resource-actions-panel"><div class="cl-resource-actions-title"></div><button data-act="share">分享</button><button data-act="show">Show in Chat</button>' + (kind === 'link' ? '<button data-act="copy">复制链接</button>' : '') + '<button class="cancel" data-act="cancel">取消</button></div>';
  overlay.querySelector('.cl-resource-actions-title').textContent = kind === 'link' ? resource.url : (resource.filename || '附件');
  overlay.querySelector('.cl-resource-actions-bg').onclick = _closeResourceActions;
  overlay.querySelector('[data-act="cancel"]').onclick = _closeResourceActions;
  overlay.querySelector('[data-act="show"]').onclick = () => { _closeResourceActions(); chOpenSearchHit(msg, _searchSessionName(msg)); };
  const shareValue = kind === 'link' ? resource.url : _resourceUrl(resource);
  overlay.querySelector('[data-act="share"]').onclick = async () => {
    try {
      if (navigator.share) await navigator.share({ title: kind === 'file' ? (resource.filename || '附件') : undefined, url: shareValue || undefined, text: shareValue || undefined });
      else if (shareValue) await chCopyText(shareValue);
    } catch {}
    _closeResourceActions();
  };
  const copy = overlay.querySelector('[data-act="copy"]');
  if (copy) copy.onclick = async () => { await chCopyText(resource.url); _closeResourceActions(); };
  overlay.style.display = 'flex';
}

function _renderGroupedList(container, grouped, q) {
  grouped.forEach((group, sid) => {
    const el = document.createElement('div'); el.className = 'cl-sr-group';
    const head = document.createElement('div'); head.className = 'cl-sr-group-head';
    head.innerHTML = '<div class="cl-sr-group-name"></div><div class="cl-sr-group-count"></div><div class="cl-sr-group-badge"></div>';
    head.querySelector('.cl-sr-group-name').textContent = group.name;
    head.querySelector('.cl-sr-group-count').textContent = group.messages.length + ' 条匹配';
    head.querySelector('.cl-sr-group-badge').textContent = _searchMetaLabel(group.messages[0]);
    head.onclick = () => chOpenSearchHit(group.messages[0], group.name);
    el.appendChild(head);
    const appendItem = m => {
      const item = document.createElement('button'); item.type = 'button'; item.className = 'cl-sr-item';
      const snippet = _cleanSearchSnippet(m.content) || '(附件消息)';
      const highlighted = q ? chHighlightMatch(snippet.slice(0, 150), q) : escapeHTML(snippet.slice(0, 150));
      item.innerHTML = '<span class="cl-sr-item-body"><span class="cl-sr-item-sender"></span><span class="cl-sr-item-text"></span></span><span class="cl-sr-item-time"></span>';
      item.querySelector('.cl-sr-item-sender').textContent = [_displaySender(m.sender), _searchMetaLabel(m)].filter(Boolean).join(' · ');
      item.querySelector('.cl-sr-item-text').innerHTML = highlighted;
      item.querySelector('.cl-sr-item-time').textContent = _searchTime(m.ts);
      item.onclick = () => chOpenSearchHit(m, group.name);
      el.appendChild(item);
    };
    group.messages.slice(0, 3).forEach(appendItem);
    if (group.messages.length > 3) {
      const more = document.createElement('button');
      more.type = 'button'; more.className = 'cl-sr-more';
      more.textContent = '查看全部 ' + group.messages.length + ' 条结果 →';
      more.onclick = () => {
        more.remove();
        group.messages.slice(3).forEach(appendItem);
      };
      el.appendChild(more);
    }
    container.appendChild(el);
  });
}

function _chParseTs(ts) {
  if (!ts) return 0;
  if (typeof ts === 'number') return ts;
  const millis = new Date(ts).getTime();
  return Number.isFinite(millis) ? Math.floor(millis / 1000) : 0;
}
function chHighlightMatch(text, query) {
  const escaped = escapeHTML(text);
  const qEsc = escapeHTML(query);
  if (!qEsc) return escaped;
  const re = new RegExp('(' + qEsc.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
  return escaped.replace(re, '<mark>$1</mark>');
}
function _captureSearchOrigin() {
  const detail = document.getElementById('chatsDetail');
  if (detail && detail.classList.contains('search-open')) {
    const results = document.getElementById('chDetailSearchResults');
    return {
      mode: 'detail', chat: activeChat, sessionId: _chDetailSearchSessionId,
      overlaySessionId: _chDetailSearchOverlaySessionId,
      query: document.getElementById('chDetailSearchInput')?.value || '',
      type: _chDetailSearchType, scrollTop: results ? results.scrollTop : 0,
    };
  }
  if (_chSearchActive) {
    const results = document.getElementById('clSearchResults');
    return {
      mode: 'global', query: document.getElementById('clSearchInput')?.value || '',
      type: _chSearchType, scope: _chSearchScope, scrollTop: results ? results.scrollTop : 0,
    };
  }
  return null;
}
function _restoreSearchOrigin(state) {
  if (!state) return;
  if (state.mode === 'detail') {
    if (activeChat !== state.chat || chViewingArchive || chViewingUnified) enterChatDetail(state.chat);
    const panel = document.getElementById('chDetailSearch');
    const detail = document.getElementById('chatsDetail');
    const results = document.getElementById('chDetailSearchResults');
    if (detail) detail.classList.add('search-open');
    if (panel) panel.style.display = 'flex';
    _chDetailSearchSessionId = state.sessionId;
    _chDetailSearchOverlaySessionId = state.overlaySessionId || null;
    _chDetailSearchType = state.type || 'all';
    const input = document.getElementById('chDetailSearchInput');
    if (input) input.value = state.query || '';
    document.querySelectorAll('#chDetailSearchChips .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.type === _chDetailSearchType));
    chRunDetailSearch((state.query || '').trim(), _chDetailSearchType).then(() => { if (results) results.scrollTop = state.scrollTop || 0; });
    return;
  }
  exitChatDetail();
  chSearchFocus();
  _chSearchType = state.type || 'all';
  _chSearchScope = state.scope || 'all';
  const input = document.getElementById('clSearchInput');
  if (input) input.value = state.query || '';
  document.querySelectorAll('#clSearchChips .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.type === _chSearchType));
  document.querySelectorAll('#clSearchScope .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.scope === _chSearchScope));
  const results = document.getElementById('clSearchResults');
  chRunGlobalSearch(false).then(() => { if (results) results.scrollTop = state.scrollTop || 0; });
}
function chBackFromDetail() {
  const state = _chSearchReturnState;
  _chSearchReturnState = null;
  if (state) { _restoreSearchOrigin(state); return; }
  exitChatDetail();
}
function chOpenSearchHit(msg, label) {
  if (!msg) return;
  _chSearchReturnState = _captureSearchOrigin();
  const nav = msg.navigation || {};
  _chPendingFocusMessageId = msg.id == null ? null : String(msg.id);
  _chPendingFocusSourceUuid = msg.source_uuid || null;
  if (nav.state === 'live' && nav.live_name) {
    chClearGlobalSearch();
    chCloseDetailSearch();
    _chLiveFocusSourceUuid = msg.source_uuid || null;
    _chLiveFocusMessageId = msg.id == null ? null : String(msg.id);
    if (!(chSub === 'detail' && !chViewingArchive && !chViewingUnified && activeChat === nav.live_name)) {
      enterChatDetail(nav.live_name, { preserveFocus: true });
    }
    lastChatFingerprint = null;
    renderChatMessages(nav.live_name);
    return;
  }
  if (nav.state === 'archived' && nav.archive_id) {
    chClearGlobalSearch();
    chCloseDetailSearch();
    enterArchivedDetail(nav);
    return;
  }
  if (chSub === 'detail' && !chViewingArchive && !chViewingUnified && _chDetailSearchSessionId === msg.session_id) {
    chCloseDetailSearch();
    _chLiveFocusSourceUuid = msg.source_uuid || null;
    _chLiveFocusMessageId = msg.id == null ? null : String(msg.id);
    lastChatFingerprint = null;
    renderChatMessages(activeChat);
    return;
  }
  chSearchNavigateSession(msg.session_id, label, msg.id, msg.source_uuid);
}
function chSearchNavigateSession(sessionId, label, messageId, sourceUuid) {
  chClearGlobalSearch();
  chCloseDetailSearch();
  _chPendingFocusMessageId = messageId == null ? null : String(messageId);
  _chPendingFocusSourceUuid = sourceUuid || null;
  enterUnifiedDetail(sessionId, label);
}

function chToggleDetailSearch() {
  const panel = document.getElementById('chDetailSearch');
  const detail = document.getElementById('chatsDetail');
  const results = document.getElementById('chDetailSearchResults');
  if (!panel) return;
  if (panel.style.display === 'none' || !panel.style.display) {
    if (detail) detail.classList.add('search-open');
    panel.style.display = 'flex';
    if (results) { results.style.display = 'block'; results.innerHTML = _chDetailSearchPlaceholder(); }
    document.getElementById('chDetailSearchInput')?.focus();
    chResolveDetailSearchSession();
  } else {
    chCloseDetailSearch();
  }
}
function chCloseDetailSearch() {
  const panel = document.getElementById('chDetailSearch');
  const detail = document.getElementById('chatsDetail');
  const results = document.getElementById('chDetailSearchResults');
  if (detail) detail.classList.remove('search-open');
  if (panel) panel.style.display = 'none';
  if (results) { results.style.display = 'none'; results.innerHTML = ''; delete results.dataset.searchRequestSeq; }
  const input = document.getElementById('chDetailSearchInput');
  if (input) input.value = '';
  _chDetailSearchType = 'all';
  document.querySelectorAll('#chDetailSearchChips .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.type === 'all'));
}
async function chResolveDetailSearchSession() {
  if (chViewingUnified) { _chDetailSearchSessionId = chViewingUnified; _chDetailSearchOverlaySessionId = null; return chViewingUnified; }
  if (!activeChat) return null;
  try {
    const r = await fetch(API + '/sessions/' + encodeURIComponent(activeChat) + '/search-id', { headers: { Authorization: 'Bearer ' + TOKEN } });
    if (!r.ok) return null;
    const data = await r.json();
    _chDetailSearchSessionId = data.session_id;
    _chDetailSearchOverlaySessionId = data.overlay_session_id || null;
    return _chDetailSearchSessionId;
  } catch { return null; }
}
function chDetailSearchDebounced(value) {
  clearTimeout(_chDetailSearchTimer);
  _chDetailSearchTimer = setTimeout(() => chRunDetailSearch(value.trim(), _chDetailSearchType), 260);
}
function chSetDetailSearchType(type) {
  _chDetailSearchType = type;
  document.querySelectorAll('#chDetailSearchChips .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.type === type));
  chRunDetailSearch((document.getElementById('chDetailSearchInput')?.value || '').trim(), type);
}
// Default panel content: hint text + a direct shortcut to the session
// boundary list (the "会话" chip overflows off-screen on narrow phones).
function _chDetailSearchPlaceholder() {
  return '<div class="cl-sr-loading">搜索此聊天中的记录、图片、文件或链接</div>'
    + '<button type="button" class="cl-sr-item" onclick="chShowSessionBoundaries()">'
    + '<span class="cl-sr-item-body"><span class="cl-sr-item-sender">📍 会话边界</span>'
    + '<span class="cl-sr-item-text">按 session 列出分界点，点击直接跳转</span></span></button>';
}

async function chRunDetailSearch(q, type) {
  if (type === 'sessions') return chShowSessionBoundaries();
  const results = document.getElementById('chDetailSearchResults');
  if (!results) return;
  if (!q && type === 'all') { results.style.display = 'block'; results.innerHTML = _chDetailSearchPlaceholder(); return; }
  results.style.display = 'block';
  const sessionId = _chDetailSearchSessionId || await chResolveDetailSearchSession();
  if (!sessionId) { results.innerHTML = '<div class="cl-sr-loading">当前聊天还没有可搜索的索引</div>'; return; }
  chRunSearch(q, type, sessionId, results, _chDetailSearchOverlaySessionId);
}

// "会话" chip — list the thread's session boundaries, newest first;
// clicking one jumps the chat view to that boundary message.
async function chShowSessionBoundaries() {
  _chDetailSearchType = 'sessions';
  document.querySelectorAll('#chDetailSearchChips .cl-sr-chip').forEach(c => c.classList.toggle('active', c.dataset.type === 'sessions'));
  const results = document.getElementById('chDetailSearchResults');
  if (!results) return;
  results.style.display = 'block';
  if (chViewingArchive || chViewingUnified || !activeChat) {
    results.innerHTML = '<div class="cl-sr-loading">会话边界仅支持实时聊天</div>';
    return;
  }
  results.innerHTML = '<div class="cl-sr-loading">加载会话边界…</div>';
  try {
    const r = await fetch(API + '/sessions/' + encodeURIComponent(activeChat) + '/session-boundaries', {
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    if (!r.ok) { results.innerHTML = '<div class="cl-sr-loading">加载失败 (' + r.status + ')</div>'; return; }
    const data = await r.json();
    const boundaries = (data.boundaries || []).slice().reverse();
    if (!boundaries.length) { results.innerHTML = '<div class="cl-sr-loading">没有找到会话边界</div>'; return; }
    results.innerHTML = '';
    boundaries.forEach((b, idx) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'cl-sr-item';
      const isCurrent = idx === 0;
      const label = '新会话 · ' + String(b.sid).slice(0, 8) + (isCurrent ? '（当前）' : '');
      item.innerHTML = '<span class="cl-sr-item-body"><span class="cl-sr-item-sender"></span><span class="cl-sr-item-text"></span></span><span class="cl-sr-item-time"></span>';
      item.querySelector('.cl-sr-item-sender').textContent = label;
      item.querySelector('.cl-sr-item-text').textContent = b.snippet || '(无文字内容)';
      item.querySelector('.cl-sr-item-time').textContent = _searchTime(b.ts);
      item.onclick = () => {
        // Mirror chOpenSearchHit: capture the panel state (before close resets
        // it) so the back arrow returns here, and set BOTH focus vars — Live
        // picks the backend window, Pending does the scroll + highlight.
        _chSearchReturnState = _captureSearchOrigin();
        chCloseDetailSearch();
        _chPendingFocusSourceUuid = b.source_uuid || null;
        _chPendingFocusMessageId = null;
        _chLiveFocusSourceUuid = b.source_uuid || null;
        _chLiveFocusMessageId = null;
        lastChatFingerprint = null;
        renderChatMessages(activeChat);
      };
      results.appendChild(item);
    });
  } catch (e) {
    results.innerHTML = '<div class="cl-sr-loading">加载失败:' + (e.message || e) + '</div>';
  }
}

function chatDisplayLabel(s) {
  return s.chat_name || s.display_name || s.name;
}
function avatarUrl(s) {
  return API + '/sessions/' + encodeURIComponent(s.name) + '/avatar?token=' + encodeURIComponent(TOKEN) + '&v=' + (s.avatar_mtime || s.log_mtime || 0);
}

function _chReadDrafts() {
  try {
    const data = JSON.parse(localStorage.getItem(CH_DRAFTS_KEY) || '{}');
    return data && typeof data === 'object' ? data : {};
  } catch {
    return {};
  }
}
function _chDraftText(name) {
  if (!name) return '';
  const item = _chReadDrafts()[name];
  return item && typeof item.text === 'string' ? item.text : '';
}
function _chSetDraft(name, text) {
  if (!name) return;
  const drafts = _chReadDrafts();
  if ((text || '').trim()) {
    drafts[name] = { text, updated_at: Date.now() };
  } else {
    delete drafts[name];
  }
  try { localStorage.setItem(CH_DRAFTS_KEY, JSON.stringify(drafts)); } catch {}
}
function _chDraftPreview(name) {
  const text = _chDraftText(name).replace(/\s+/g, ' ').trim();
  return text ? text.slice(0, 120) : '';
}
function chPersistActiveDraft() {
  if (!activeChat || chViewingArchive || chViewingUnified) return;
  const input = document.getElementById('chInput');
  if (input) _chSetDraft(activeChat, input.value || '');
}
function chInputChange(el) {
  chInputAutoGrow(el);
  if (!activeChat || chViewingArchive || chViewingUnified) return;
  _chSetDraft(activeChat, el.value || '');
  renderChatRows();
}

function renderChatRows() {
  const wrap = document.getElementById('chRows');
  if (!wrap) return;
  // Chats = Claude + Codex + OpenCode sessions only. Shell stays in Code.
  let list = sessions.filter(s => s.kind === 'cc' || s.kind === 'codex' || s.kind === 'opencode');
  list.sort((a, b) => {
    return (b.last_message_at || b.log_mtime || b.created) - (a.last_message_at || a.log_mtime || a.created);
  });
  wrap.innerHTML = '';
  if (list.length) {
    list.forEach(s => wrap.appendChild(buildChatRow(s)));
  } else {
    const empty = document.createElement('div');
    empty.className = 'cl-empty';
    empty.textContent = '还没有聊天 — 去 Code 里新建一个';
    wrap.appendChild(empty);
  }
  // Archive entry always at the bottom — gated behind a click so it doesn't
  // dilute the live conversation list. Expand state is sticky for the session
  // so closing-and-reopening Chats keeps the panel as the user left it.
  wrap.appendChild(buildArchiveCard());
}

// Search is surfaced in Chat; the unified store is backend-only.

function buildArchiveCard() {
  const card = document.createElement('div');
  card.className = 'ch-archive-card' + (chArchiveExpanded ? ' open' : '');
  card.innerHTML = `
    <div class="ch-archive-head">
      <div class="ch-archive-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="3" y="4" width="18" height="4" rx="1"/>
          <path d="M5 8v11a1 1 0 001 1h12a1 1 0 001-1V8"/>
          <line x1="10" y1="13" x2="14" y2="13"/>
        </svg>
      </div>
      <div class="ch-archive-title">
        <div class="ch-archive-name">Archive</div>
        <div class="ch-archive-sub">归档的对话</div>
      </div>
      <svg class="ch-archive-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="6 9 12 15 18 9"/>
      </svg>
    </div>
    <div class="ch-archive-body"></div>
  `;
  const head = card.querySelector('.ch-archive-head');
  const body = card.querySelector('.ch-archive-body');
  head.onclick = () => toggleArchiveCard(card, body);
  if (chArchiveExpanded) {
    renderArchiveBody(body);
  }
  return card;
}

async function toggleArchiveCard(card, body) {
  chArchiveExpanded = !chArchiveExpanded;
  card.classList.toggle('open', chArchiveExpanded);
  if (chArchiveExpanded) {
    renderArchiveBody(body);
  } else {
    body.innerHTML = '';
  }
}

async function renderArchiveBody(body) {
  if (chArchiveList === null) {
    body.innerHTML = '<div class="ch-archive-loading">加载中…</div>';
    try {
      const r = await fetch(API + '/archived-sessions', { headers: { Authorization: 'Bearer ' + TOKEN } });
      if (!r.ok) {
        body.innerHTML = '<div class="ch-archive-loading">加载失败</div>';
        return;
      }
      const d = await r.json();
      chArchiveList = d.sessions || [];
    } catch (e) {
      body.innerHTML = '<div class="ch-archive-loading">网络错误</div>';
      return;
    }
  }
  body.innerHTML = '';
  if (!chArchiveList.length) {
    const empty = document.createElement('div');
    empty.className = 'ch-archive-loading';
    empty.textContent = '还没有归档的对话';
    body.appendChild(empty);
    return;
  }
  chArchiveList.forEach(meta => body.appendChild(buildArchivedRow(meta)));
}

function buildArchivedRow(meta, opts) {
  opts = opts || {};
  // Outer row is swipeable, inner item slides left to expose rename/delete
  // buttons. Mirrors the chat-row + ch-item swipe pattern, but with archive-
  // specific actions and a separate gesture handler so chat-row state doesn't
  // get tangled with archive-row state.
  const row = document.createElement('div');
  row.className = 'ar-row';
  row.dataset.archiveId = meta.archive_id;

  const actions = document.createElement('div');
  actions.className = 'ar-actions';
  actions.innerHTML = `
    <button class="ic-btn" data-act="rename" title="改名" aria-label="改名">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 113 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
    </button>
    <button class="ic-btn danger" data-act="delete" title="删除" aria-label="删除">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
    </button>
  `;
  actions.querySelector('[data-act="rename"]').onclick = (e) => { e.stopPropagation(); closeAllSwipes(); openArchiveRename(meta); };
  actions.querySelector('[data-act="delete"]').onclick = (e) => { e.stopPropagation(); closeAllSwipes(); confirmDeleteArchive(meta); };

  if (meta.auto_archived || meta.interrupted) row.classList.add('auto-archived');

  const item = document.createElement('div');
  item.className = 'ar-item';
  const label = meta.chat_name || meta.display_name || meta.name;
  const when = meta.archived_at ? shortDateLabel(meta.archived_at) : '';
  // For search results show the matched snippet; otherwise the first-message preview.
  const previewText = (opts.isSearch && meta.snippet) ? meta.snippet : (meta.preview || '');
  const preview = previewText.trim() || '（无预览）';
  const kindLabel = (typeof KIND_LABEL !== 'undefined' && KIND_LABEL[meta.kind]) || meta.kind || '';
  item.innerHTML = `
    <div class="ar-body">
      <div class="ar-name"></div>
      <div class="ar-preview"></div>
    </div>
    <div class="ar-meta">
      <div class="ar-kind"></div>
      <div class="ar-time"></div>
    </div>
  `;
  item.querySelector('.ar-name').textContent = label;
  item.querySelector('.ar-preview').textContent = preview;
  item.querySelector('.ar-kind').textContent = kindLabel;
  item.querySelector('.ar-time').textContent = when;

  row.appendChild(actions);
  row.appendChild(item);

  // cc archives have a JSONL; non-cc archives only have the raw log so we
  // gate the tap-to-open behavior. Swipe + long-press still work either way
  // (you may want to rename or delete a non-cc archive).
  if (meta.jsonl_path) {
    row.classList.add('clickable');
  } else {
    row.classList.add('disabled');
    item.title = '这条归档没有对话日志（非 Claude session）';
  }
  attachArchiveRowGestures(row, item, meta);
  return row;
}

function attachArchiveRowGestures(row, item, meta) {
  const SWIPE_OPEN = 120, SWIPE_TH = 50, LONG_MS = 500;
  let startX = 0, startY = 0, dx = 0, dy = 0;
  let dragging = false, blocked = false, longTimer = null;
  let openAtStart = false, suppressClick = false;
  const cancelLong = () => { if (longTimer) { clearTimeout(longTimer); longTimer = null; } };

  item.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    startX = t.clientX; startY = t.clientY; dx = 0; dy = 0;
    dragging = false; blocked = false; suppressClick = false;
    openAtStart = row.dataset.swiped === '1';
    cancelLong();
    longTimer = setTimeout(() => {
      longTimer = null;
      if (dragging || blocked) return;
      suppressClick = true;
      closeAllSwipes();
      if (typeof hapticTap === 'function') hapticTap();
      openArchiveActionSheet(meta, item);
    }, LONG_MS);
  }, { passive: true });

  item.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    dx = t.clientX - startX; dy = t.clientY - startY;
    if (!dragging && !blocked) {
      if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 8) { blocked = true; cancelLong(); return; }
      if (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) { dragging = true; cancelLong(); row.classList.add('dragging'); }
    }
    if (dragging) {
      const base = openAtStart ? -SWIPE_OPEN : 0;
      let nx = base + dx;
      if (nx > 0) nx = nx * 0.3;
      if (nx < -SWIPE_OPEN) nx = -SWIPE_OPEN + (nx + SWIPE_OPEN) * 0.3;
      item.style.transform = `translateX(${nx}px)`;
      if (e.cancelable) e.preventDefault();
    }
  }, { passive: false });

  const finish = () => {
    cancelLong();
    row.classList.remove('dragging');
    if (!dragging) return;
    suppressClick = true;
    const base = openAtStart ? -SWIPE_OPEN : 0;
    const final = base + dx;
    if (final < -SWIPE_TH) {
      row.dataset.swiped = '1';
      item.style.transform = `translateX(-${SWIPE_OPEN}px)`;
      closeAllSwipes(row);
    } else {
      row.removeAttribute('data-swiped');
      item.style.transform = '';
    }
    dragging = false;
  };
  item.addEventListener('touchend', finish);
  item.addEventListener('touchcancel', finish);

  item.addEventListener('click', (e) => {
    if (suppressClick) { suppressClick = false; e.preventDefault(); e.stopPropagation(); return; }
    if (row.dataset.swiped === '1') {
      e.preventDefault(); e.stopPropagation();
      row.removeAttribute('data-swiped'); item.style.transform = '';
      return;
    }
    if (meta.jsonl_path) enterArchivedDetail(meta);
  });
}

function openArchiveActionSheet(meta, anchorEl) {
  const sheet = document.getElementById('sessActionSheet');
  const panel = document.getElementById('as-panel');
  // Hide Archive (already archived) + model/effort (live-session only).
  const archiveBtn = document.getElementById('as-archive');
  const modelBtn = document.getElementById('as-model');
  const effortBtn = document.getElementById('as-effort');
  const hidden = [archiveBtn, modelBtn, effortBtn].filter(Boolean);
  hidden.forEach(el => el.style.display = 'none');
  const restoreHidden = () => hidden.forEach(el => el.style.display = '');
  const bind = (id, fn) => { document.getElementById(id).onclick = () => { closeActionSheet(); restoreHidden(); fn(); }; };
  bind('as-rename', () => openArchiveRename(meta));
  bind('as-delete', () => confirmDeleteArchive(meta));
  sheet.classList.add('open');
  // Restore the Archive button when the sheet is closed without picking an action.
  const oldOnclick = sheet.onclick;
  sheet.onclick = (e) => {
    if (oldOnclick) oldOnclick(e);
    if (!sheet.classList.contains('open')) { restoreHidden(); sheet.onclick = oldOnclick; }
  };
  requestAnimationFrame(() => {
    const r = anchorEl.getBoundingClientRect();
    const pw = panel.offsetWidth || 240;
    const ph = panel.offsetHeight || 160;
    const gap = 8, vw = window.innerWidth, vh = window.innerHeight;
    let left = r.right - pw;
    if (left < 14) left = 14;
    if (left + pw > vw - 14) left = vw - pw - 14;
    let top = r.bottom + gap;
    let origin = 'top right';
    if (top + ph > vh - 14) { top = r.top - ph - gap; origin = 'bottom right'; if (top < 14) top = Math.max(14, vh - ph - 14); }
    panel.style.left = left + 'px';
    panel.style.top = top + 'px';
    panel.style.transformOrigin = origin;
  });
}

async function openArchiveRename(meta) {
  const current = meta.chat_name || meta.display_name || meta.name;
  const next = prompt('改名（清空则使用原 session 名）', current);
  if (next === null) return;  // cancelled
  try {
    const r = await fetch(API + '/archived-sessions/' + encodeURIComponent(meta.archive_id) + '/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ display_name: next }),
    });
    if (!r.ok) { alert('改名失败'); return; }
    const d = await r.json();
    // Patch the in-memory cache and re-render whichever view is open.
    if (chArchiveList) {
      const hit = chArchiveList.find(m => m.archive_id === meta.archive_id);
      if (hit) {
        hit.display_name = d.display_name;
        hit.chat_name = d.chat_name ?? d.display_name;
      }
    }
    rerenderArchiveViews();
  } catch (e) { alert('网络错误'); }
}

async function confirmDeleteArchive(meta) {
  const label = meta.chat_name || meta.display_name || meta.name;
  if (!confirm(`删除归档 "${label}"？\n会清掉归档文件，但 Claude 的对话原始 JSONL 保留。`)) return;
  try {
    const r = await fetch(API + '/archived-sessions/' + encodeURIComponent(meta.archive_id), {
      method: 'DELETE',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    if (!r.ok) { alert('删除失败'); return; }
    // Drop from cache and re-render so the row disappears without an extra fetch.
    if (chArchiveList) {
      chArchiveList = chArchiveList.filter(m => m.archive_id !== meta.archive_id);
    }
    // If the user was viewing this archive in detail, exit back to the list.
    if (chViewingArchive === meta.archive_id) exitChatDetail();
    rerenderArchiveViews();
  } catch (e) { alert('网络错误'); }
}

function rerenderArchiveViews() {
  // Chat-side Archive card body (only if currently expanded).
  if (chArchiveExpanded) {
    const body = document.querySelector('.ch-archive-card .ch-archive-body');
    if (body) renderArchiveBody(body);
  }
  // Code-side Archive chip view.
  if (typeof currentFilter !== 'undefined' && currentFilter === 'archive' && typeof renderCards === 'function') {
    renderCards();
  }
  // Update chip count.
  if (typeof renderChips === 'function') renderChips();
}

function enterArchivedDetail(meta) {
  _chStopRealtimeEvents();
  // Archive detail lives on the Chats tab — if the user clicked an archive
  // from the Code Archive chip, jump there first so the detail view shows.
  if (typeof currentView !== 'undefined' && currentView !== 'chats' && typeof switchView === 'function') {
    switchView('chats');
  }
  chSub = 'detail';
  document.getElementById('chatsView').setAttribute('data-sub', 'detail');
  document.getElementById('chatsView').setAttribute('data-agent-kind', meta.kind || '');
  if (typeof updateThemeChrome === 'function') updateThemeChrome();
  chViewingArchive = meta.archive_id;
  chViewingUnified = null;
  _chResetRevealState();
  _chDetailSearchSessionId = null;
  _chDetailSearchOverlaySessionId = null;
  _chLiveFocusSourceUuid = null;
  _chLiveFocusMessageId = null;
  chCloseDetailSearch();
  activeChat = meta.name;
  // Don't touch activeSession — there's no live tmux pane for this archive.
  lastChatFingerprint = null;
  chMsgLimit = _CH_DEFAULT_MSG_LIMIT;
  _chPendingLoadEarlier = false;
  document.getElementById('chDetailTitle').textContent = meta.chat_name || meta.display_name || meta.name;
  const sub = ['📦 已归档'];
  if (meta.kind) sub.push(KIND_LABEL[meta.kind] || meta.kind);
  if (meta.archived_at) sub.push(shortDateLabel(meta.archived_at));
  document.getElementById('chDetailSub').textContent = sub.join(' · ');
  const switchBtn = document.getElementById('chSwitchToCode');
  if (switchBtn) switchBtn.style.display = 'none';
  const inputBar = document.getElementById('chInputBar');
  if (inputBar) inputBar.style.display = 'none';
  renderChatMessages(meta.name);
  // No polling for archives — content is frozen.
  if (chatRefreshTimer) { clearTimeout(chatRefreshTimer); chatRefreshTimer = null; }
}

function buildChatRow(s) {
  const row = document.createElement('div');
  row.className = 'ch-row';
  row.dataset.name = s.name;

  const item = document.createElement('div');
  item.className = 'ch-item';
  const kind = s.kind || 'shell';
  const avCls = kind;
  const label = chatDisplayLabel(s);
  const ts = s.last_message_at || s.log_mtime || s.created;
  const timeLabel = shortDateLabel(ts);
  const draftPreview = _chDraftPreview(s.name);
  const preview = (s.last_line || '').trim() || '（暂无消息）';
  const avatarHTML = s.has_avatar
    ? `<div class="ch-avatar ${avCls}"><img src="${avatarUrl(s)}" alt=""></div>`
    : '';
  item.innerHTML = `
    ${avatarHTML}
    <div class="ch-body">
      <div class="ch-name"></div>
      <div class="ch-preview"></div>
    </div>
    <div class="ch-time"></div>`;
  item.querySelector('.ch-name').textContent = label;
  const previewEl = item.querySelector('.ch-preview');
  if (draftPreview) {
    previewEl.innerHTML = '<span class="ch-draft-prefix">[草稿]</span> ' + escapeHTML(draftPreview);
    previewEl.classList.add('draft');
  } else {
    previewEl.textContent = preview;
    previewEl.classList.remove('draft');
  }
  item.querySelector('.ch-time').textContent = timeLabel;

  const actions = document.createElement('div');
  actions.className = 'cl-actions';
  actions.innerHTML = `
    <button class="ic-btn" data-act="edit" title="编辑" aria-label="编辑">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 113 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
    </button>
    <button class="ic-btn" data-act="archive" title="归档" aria-label="归档">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 001 1h12a1 1 0 001-1V8"/><line x1="10" y1="13" x2="14" y2="13"/></svg>
    </button>
    <button class="ic-btn danger" data-act="delete" title="删除" aria-label="删除">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
    </button>`;
  actions.querySelector('[data-act="edit"]').onclick = (e) => { e.stopPropagation(); closeAllSwipes(); openChatEdit(s); };
  actions.querySelector('[data-act="archive"]').onclick = (e) => { e.stopPropagation(); closeAllSwipes(); confirmArchive(s.name, label); };
  actions.querySelector('[data-act="delete"]').onclick = (e) => { e.stopPropagation(); closeAllSwipes(); confirmDelete(s.name, label); };

  row.appendChild(actions);
  row.appendChild(item);

  attachChatRowGestures(row, item, s);
  return row;
}

// Reuse the Code gesture handler, but tap goes to chat detail and the long-press
// menu's Rename routes through openChatEdit.
function attachChatRowGestures(row, item, s) {
  const SWIPE_OPEN = 180, SWIPE_TH = 60, LONG_MS = 500;
  let startX = 0, startY = 0, dx = 0, dy = 0;
  let dragging = false, blocked = false, longTimer = null;
  let openAtStart = false, suppressClick = false;
  const cancelLong = () => { if (longTimer) { clearTimeout(longTimer); longTimer = null; } };

  item.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    startX = t.clientX; startY = t.clientY; dx = 0; dy = 0;
    dragging = false; blocked = false; suppressClick = false;
    openAtStart = row.dataset.swiped === '1';
    cancelLong();
    longTimer = setTimeout(() => {
      longTimer = null;
      if (dragging || blocked) return;
      suppressClick = true;
      closeAllSwipes();
      hapticTap();
      liftRow(row);
      openChatActionSheet(s, item);
    }, LONG_MS);
  }, { passive: true });

  item.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    dx = t.clientX - startX; dy = t.clientY - startY;
    if (!dragging && !blocked) {
      if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 8) { blocked = true; cancelLong(); return; }
      if (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) { dragging = true; cancelLong(); row.classList.add('dragging'); }
    }
    if (dragging) {
      const base = openAtStart ? -SWIPE_OPEN : 0;
      let nx = base + dx;
      if (nx > 0) nx = nx * 0.3;
      if (nx < -SWIPE_OPEN) nx = -SWIPE_OPEN + (nx + SWIPE_OPEN) * 0.3;
      item.style.transform = `translateX(${nx}px)`;
      if (e.cancelable) e.preventDefault();
    }
  }, { passive: false });

  const finish = () => {
    cancelLong();
    row.classList.remove('dragging');
    if (!dragging) return;
    suppressClick = true;
    const base = openAtStart ? -SWIPE_OPEN : 0;
    const final = base + dx;
    if (final < -SWIPE_TH) {
      row.dataset.swiped = '1';
      item.style.transform = `translateX(-${SWIPE_OPEN}px)`;
      closeAllSwipes(row);
    } else {
      row.removeAttribute('data-swiped');
      item.style.transform = '';
    }
    dragging = false;
  };
  item.addEventListener('touchend', finish);
  item.addEventListener('touchcancel', finish);

  item.addEventListener('click', (e) => {
    if (suppressClick) { suppressClick = false; e.preventDefault(); e.stopPropagation(); return; }
    if (row.dataset.swiped === '1') {
      e.preventDefault(); e.stopPropagation();
      row.removeAttribute('data-swiped'); item.style.transform = '';
      return;
    }
    enterChatDetail(s.name);
  });
}

// Chat-specific long-press menu (Rename → opens chat edit instead of title-only)
function openChatActionSheet(s, anchorEl) {
  const sheet = document.getElementById('sessActionSheet');
  const panel = document.getElementById('as-panel');
  const modelBtn = document.getElementById('as-model');
  const effortBtn = document.getElementById('as-effort');
  const showModelEffort = s.kind === 'cc' || s.kind === 'codex' || s.kind === 'opencode';
  if (modelBtn) modelBtn.style.display = showModelEffort ? '' : 'none';
  if (effortBtn) effortBtn.style.display = showModelEffort ? '' : 'none';
  const bind = (id, fn) => { document.getElementById(id).onclick = () => { closeActionSheet(); fn(); }; };
  // bindLayered: keep the action sheet open so the picker stacks on top of it,
  // like the Code page does between cd-menu and the model/effort picker.
  const bindLayered = (id, fn) => { document.getElementById(id).onclick = () => fn(); };
  bindLayered('as-model', () => {
    activeSession = s.name;
    const p = document.getElementById('cdModelPicker');
    if (p && p.parentElement !== document.body) document.body.appendChild(p);
    if (typeof menuModel === 'function') menuModel();
  });
  bindLayered('as-effort', () => {
    activeSession = s.name;
    const p = document.getElementById('cdEffortPicker');
    if (p && p.parentElement !== document.body) document.body.appendChild(p);
    if (typeof menuEffort === 'function') menuEffort();
  });
  bind('as-rename', () => openChatEdit(s));
  bind('as-archive', () => confirmArchive(s.name, chatDisplayLabel(s)));
  bind('as-delete',  () => confirmDelete(s.name, chatDisplayLabel(s)));
  sheet.classList.add('open');
  requestAnimationFrame(() => {
    const r = anchorEl.getBoundingClientRect();
    const pw = panel.offsetWidth || 240;
    const ph = panel.offsetHeight || 160;
    const gap = 8, vw = window.innerWidth, vh = window.innerHeight;
    let left = r.right - pw;
    if (left < 14) left = 14;
    if (left + pw > vw - 14) left = vw - pw - 14;
    let top = r.bottom + gap;
    let origin = 'top right';
    if (top + ph > vh - 14) { top = r.top - ph - gap; origin = 'bottom right'; if (top < 14) top = Math.max(14, vh - ph - 14); }
    panel.style.left = left + 'px';
    panel.style.top = top + 'px';
    panel.style.transformOrigin = origin;
  });
}

function enterChatDetail(name, options = {}) {
  if (activeChat && activeChat !== name && !chViewingArchive && !chViewingUnified) chPersistActiveDraft();
  const preserveFocus = Boolean(options.preserveFocus);
  if (!preserveFocus) {
    _chLiveFocusSourceUuid = null;
    _chLiveFocusMessageId = null;
    _chPendingFocusSourceUuid = null;
    _chPendingFocusMessageId = null;
    _chPendingOpenCompactionUuid = null;
    _chPendingPinBottom = true;
    _chWasAtBottom = true;
  }
  chSub = 'detail';
  chViewingUnified = null;
  _chDetailSearchSessionId = null;
  _chDetailSearchOverlaySessionId = null;
  chCloseDetailSearch();
  const inputBar = document.getElementById('chInputBar');
  if (inputBar) inputBar.style.display = '';
  document.getElementById('chatsView').setAttribute('data-sub', 'detail');
  _chDetachHiddenCodeTerminalFor(name);
  activeChat = name;
  activeSession = name;     // route Code's upload / send pipeline at this session
  _chResetRevealState(name);
  lastChatFingerprint = null;
  chMsgLimit = _CH_DEFAULT_MSG_LIMIT;
  _chPendingLoadEarlier = false;
  if (typeof clearAttachments === 'function') clearAttachments();
  const msgsEl = document.getElementById('chMsgs');
  if (msgsEl && !msgsEl._scrollHooked) {
    msgsEl.addEventListener('scroll', updateChScrollBottomBtn, { passive: true });
    msgsEl._scrollHooked = true;
  }
  const inp = document.getElementById('chInput');
  if (inp) {
    inp.value = _chDraftText(name);
    chInputAutoGrow(inp);
  }
  const s = sessions.find(x => x.name === name);
  document.getElementById('chatsView').setAttribute('data-agent-kind', (s && s.kind) || '');
  if (inp) {
    const kind = s && s.kind;
    inp.placeholder = kind === 'codex' ? 'Ask Codex'
      : kind === 'shell' ? 'Run a command'
      : kind === 'opencode' ? 'Ask OpenCode'
      : 'Reply to Claude';
  }
  if (typeof updateThemeChrome === 'function') updateThemeChrome();
  document.getElementById('chDetailTitle').textContent = s ? chatDisplayLabel(s) : name;
  document.getElementById('chDetailSub').textContent = s ? (KIND_LABEL[s.kind] || s.kind) : '';
  const switchBtn = document.getElementById('chSwitchToCode');
  if (switchBtn) switchBtn.style.display = '';
  // Sync pill label immediately from localStorage, then re-sync after banner detect lands.
  // activeSession is already === name above, so the detect runs against the right session.
  chUpdateSendModeButton();
  updateChatModelLabel();
  _chCompactionHistoryOpen = false;
  _chCompactionHistorySignature = null;
  _chCompactionRecordsLoaded = false;
  if (s && (s.kind === 'cc' || s.kind === 'codex' || s.kind === 'opencode') && typeof detectModelEffortFromBanner === 'function') {
    detectModelEffortFromBanner().then(updateChatModelLabel).catch(() => {});
  }
  const cached = _chLiveMessageCache.get(name);
  if (cached) renderChatMessages(name, cached);
  else _chShowChatStartingState(name);
  renderChatMessages(name);
  _chScheduleChatRefresh();
  _chStartRealtimeEvents(name);
  _chStartUsagePolling(name);
}
function exitChatDetail() {
  _chStopRealtimeEvents();
  _chStopUsagePolling();
  chPersistActiveDraft();
  chCloseDetailSearch();
  chSub = 'list';
  document.getElementById('chatsView').setAttribute('data-sub', 'list');
  document.getElementById('chatsView').removeAttribute('data-agent-kind');
  if (typeof updateThemeChrome === 'function') updateThemeChrome();
  activeChat = null;
  chViewingArchive = null;
  chViewingUnified = null;
  _chDetailSearchSessionId = null;
  _chDetailSearchOverlaySessionId = null;
  _chLiveFocusSourceUuid = null;
  _chLiveFocusMessageId = null;
  _chPendingOpenCompactionUuid = null;
  _chResetRevealState();
  lastChatFingerprint = null;
  if (chatRefreshTimer) { clearTimeout(chatRefreshTimer); chatRefreshTimer = null; }
}

function enterUnifiedDetail(sessionId, displayName) {
  _chStopRealtimeEvents();
  if (typeof currentView !== 'undefined' && currentView !== 'chats' && typeof switchView === 'function') {
    switchView('chats');
  }
  chSub = 'detail';
  document.getElementById('chatsView').setAttribute('data-sub', 'detail');
  document.getElementById('chatsView').removeAttribute('data-agent-kind');
  if (typeof updateThemeChrome === 'function') updateThemeChrome();
  chViewingUnified = sessionId;
  chViewingArchive = null;
  _chResetRevealState();
  _chDetailSearchSessionId = sessionId;
  _chDetailSearchOverlaySessionId = null;
  chCloseDetailSearch();
  activeChat = sessionId;
  lastChatFingerprint = null;
  chMsgLimit = _CH_DEFAULT_MSG_LIMIT;
  _chPendingLoadEarlier = false;
  document.getElementById('chDetailTitle').textContent = displayName || sessionId.slice(0, 12);
  document.getElementById('chDetailSub').textContent = '已删除或历史记录 · 只读';
  const switchBtn = document.getElementById('chSwitchToCode');
  if (switchBtn) switchBtn.style.display = 'none';
  const inputBar = document.getElementById('chInputBar');
  if (inputBar) inputBar.style.display = 'none';
  renderChatMessages(sessionId);
  if (chatRefreshTimer) { clearTimeout(chatRefreshTimer); chatRefreshTimer = null; }
}

function buildAssistantActions(text, idx) {
  const row = document.createElement('div');
  row.className = 'ch-msg-actions';
  // copy / share / replay / 👍 / 👎 / regen — visual placeholders for v3; copy is wired
  row.innerHTML = `
    <button class="ch-msg-action" data-act="copy" title="复制" aria-label="复制">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M15 9h5a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-5"/><rect x="2" y="2" width="13" height="13" rx="2"/></svg>
    </button>
    <button class="ch-msg-action" data-act="share" title="分享" aria-label="分享">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><polyline points="7 9 12 4 17 9"/><path d="M20 16v3a2 2 0 01-2 2H6a2 2 0 01-2-2v-3"/></svg>
    </button>
    <button class="ch-msg-action" data-act="tts" title="朗读" aria-label="朗读">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><polygon points="6 4 20 12 6 20 6 4"/></svg>
    </button>
    <button class="ch-msg-action" data-act="up" title="赞" aria-label="赞">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>
    </button>
    <button class="ch-msg-action" data-act="down" title="踩" aria-label="踩">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/></svg>
    </button>
    <button class="ch-msg-action" data-act="regen" title="重试" aria-label="重试">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>
    </button>
  `;
  row.querySelector('[data-act="copy"]').onclick = async (e) => {
    e.stopPropagation();
    const btn = e.currentTarget;
    const ok = await chCopyText(text);
    btn.classList.add(ok ? 'active' : 'failed');
    setTimeout(() => btn.classList.remove('active', 'failed'), 800);
  };
  row.querySelector('[data-act="share"]').onclick = async (e) => {
    e.stopPropagation();
    try {
      if (navigator.share) await navigator.share({ text });
      else await chCopyText(text);
    } catch {}
  };
  // tts / up / down / regen: still placeholders for v3
  return row;
}

function chFocusComposer() {
  if (!activeChat || chViewingArchive || chViewingUnified) return;
  const hadSearchFocus = Boolean(_chLiveFocusSourceUuid || _chLiveFocusMessageId);
  _chLiveFocusSourceUuid = null;
  _chLiveFocusMessageId = null;
  _chPendingFocusSourceUuid = null;
  _chPendingFocusMessageId = null;
  _chPendingPinBottom = true;
  const pin = () => {
    const wrap = document.getElementById('chMsgs');
    if (!wrap) return;
    wrap.scrollTop = wrap.scrollHeight;
    updateChScrollBottomBtn();
  };
  if (hadSearchFocus) {
    chMsgLimit = _CH_DEFAULT_MSG_LIMIT;
    lastChatFingerprint = null;
    renderChatMessages(activeChat).then(() => requestAnimationFrame(pin));
  } else {
    requestAnimationFrame(pin);
  }
  setTimeout(pin, 260);
}

function chScrollToBottom() {
  const wrap = document.getElementById('chMsgs');
  if (!wrap) return;
  if ((_chLiveFocusSourceUuid || _chLiveFocusMessageId) && !chViewingArchive && !chViewingUnified) {
    _chLiveFocusSourceUuid = null;
    _chLiveFocusMessageId = null;
    lastChatFingerprint = null;
    renderChatMessages(activeChat);
  }
  wrap.scrollTop = wrap.scrollHeight;
  updateChScrollBottomBtn();
}
function updateChScrollBottomBtn() {
  const wrap = document.getElementById('chMsgs');
  const btn = document.getElementById('chScrollBottom');
  if (!wrap || !btn) return;
  const atBottom = (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight) < 80;
  _chWasAtBottom = atBottom;
  btn.style.display = atBottom ? 'none' : 'flex';
}

function _chPinChatBottomSoon(wrap = document.getElementById('chMsgs')) {
  if (!wrap) return;
  const pin = () => {
    wrap.scrollTop = wrap.scrollHeight;
    updateChScrollBottomBtn();
  };
  pin();
  requestAnimationFrame(pin);
  requestAnimationFrame(() => requestAnimationFrame(pin));
  setTimeout(pin, 80);
  setTimeout(pin, 240);
}

// Called from switchView when returning to the chats tab; if the user was
// pinned to the bottom before leaving, re-pin after the browser finishes
// re-laying out the now-visible scroll container. We set scrollTop three
// times (sync after a forced layout read, then again on the next rAF, then
// once more on a short timer) because iOS Safari sometimes restores the
// hidden scrollTop *after* our sync write — the timer catches that case.
function restoreChatScrollIfNeeded() {
  if (chSub !== 'detail') return;
  const wrap = document.getElementById('chMsgs');
  if (!wrap) return;
  if (!_chWasAtBottom) return;
  // Force layout so scrollHeight is accurate now that the view is display:flex.
  void wrap.offsetHeight;
  wrap.scrollTop = wrap.scrollHeight;
  requestAnimationFrame(() => { wrap.scrollTop = wrap.scrollHeight; });
  setTimeout(() => { wrap.scrollTop = wrap.scrollHeight; }, 60);
}

function _toolIcon(name) {
  const n = (name || '').toLowerCase();
  if (n === 'read') return '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="#7793a5" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>';
  if (n === 'edit' || n === 'write' || n === 'apply_patch' || n === 'apply_diff' || n === 'write_file' || n === 'create_file') return '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="#a06a52" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
  if (n === 'bash' || n === 'exec_command' || n === 'write_stdin') return '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="#6a9b6e" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>';
  if (n === 'grep' || n === 'glob' || n === 'websearch' || n === 'search_query' || n === 'find') return '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="#e0b870" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
  if (n === 'webfetch' || n === 'open') return '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="#b8a4c9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/></svg>';
  if (n === 'view_image' || n === 'image_query') return '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="#7793a5" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.5"/><path d="M21 15l-5-5L5 19"/></svg>';
  if (n === 'task' || n === 'agent' || n === 'parallel' || n === 'update_plan') return '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="#a06a52" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>';
  if (n === 'thinking') return '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="#b8a4c9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>';
  return '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="#a06a52" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 105.4 5.4l-9.7 9.7a2 2 0 11-2.8-2.8L17.3 9 14.7 6.3z"/></svg>';
}

function _toolRowIcon(name) {
  const n = (name || '').toLowerCase();
  if (n === 'read') return '<svg class="status" viewBox="0 0 24 24" fill="none" stroke="#7793a5" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
  if (n === 'edit' || n === 'write' || n === 'apply_patch' || n === 'apply_diff' || n === 'write_file' || n === 'create_file') return '<svg class="status" viewBox="0 0 24 24" fill="none" stroke="#a06a52" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
  if (n === 'bash' || n === 'exec_command' || n === 'write_stdin') return '<svg class="status" viewBox="0 0 24 24" fill="none" stroke="#6a9b6e" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>';
  if (n === 'grep' || n === 'glob' || n === 'websearch' || n === 'search_query' || n === 'find') return '<svg class="status" viewBox="0 0 24 24" fill="none" stroke="#e0b870" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
  if (n === 'webfetch' || n === 'open') return '<svg class="status" viewBox="0 0 24 24" fill="none" stroke="#b8a4c9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/></svg>';
  if (n === 'view_image' || n === 'image_query') return '<svg class="status" viewBox="0 0 24 24" fill="none" stroke="#7793a5" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.5"/><path d="M21 15l-5-5L5 19"/></svg>';
  return null;
}

const _INFLIGHT_THINKING_PHRASES = [
  ['✳', 'Cooking…', 'cup'],       ['✦', 'Computing…', 'coding'],   ['✳', 'Spelunking…', 'spark'],
  ['✦', 'Shimmying…', 'music'],   ['✳', 'Finagling…', 'coding'],   ['✢', 'Puttering…', 'plant'],
  ['✦', 'Perusing…', 'coding'],   ['✳', 'Cerebrating…', 'spark'],  ['✦', 'Mulling…', 'cup'],
  ['·', 'Wibbling…', 'music'],    ['✢', 'Philosophising…', 'plant'], ['·', 'Noodling…', 'sleep'],
  ['✢', 'Elucidating…', 'coding'],['·', 'Schlepping…', 'cup'],     ['✳', 'Simmering…', 'cup'],
  ['✦', 'Incubating…', 'sleep'],  ['✳', 'Transmuting…', 'spark'],  ['✢', 'Cogitating…', 'coding'],
  ['✦', 'Recombobulating…', 'music'], ['✳', 'Brewing…', 'cup'],    ['·', 'Pondering…', 'sleep'],
  ['✢', 'Conjuring…', 'spark'],   ['✦', 'Dreaming…', 'sleep'],     ['✳', 'Thinking…', 'coding'],
];
let _inflightPhraseIdx = Math.floor(Math.random() * _INFLIGHT_THINKING_PHRASES.length);
let _inflightRotateTimer = null;

function _inflightText() {
  const p = _INFLIGHT_THINKING_PHRASES[_inflightPhraseIdx];
  return p[0] + ' ' + p[1];
}
function _inflightCrabMarkup() {
  const state = _INFLIGHT_THINKING_PHRASES[_inflightPhraseIdx][2] || 'spark';
  const body =
    '<path class="body" d="M6 5h20v6h4v5h-4v3H6v-3H2v-5h4z"/>' +
    '<path class="body" d="M7 18h3v6H7zm5 0h3v6h-3zm5 0h3v6h-3zm5 0h3v6h-3z"/>' +
    '<path class="eye" d="M10 9h2v4h-2zm10 0h2v4h-2z"/>';
  let prop = '';
  if (state === 'coding') {
    prop =
      '<path class="prop-dark" d="M22 12h23v11H20v-2h2z"/>' +
      '<path class="prop-shadow" d="M24 14h19v7H23z"/>' +
      '<path class="prop-light" d="M30 17h2v2h-2zm3-2h2v2h-2zm2 4h3v2h-3zm3-4h2v2h-2z"/>';
  } else if (state === 'cup') {
    prop =
      '<path class="prop-gold" d="M33 13h9v9h-9zM42 15h4v5h-4v-2h2v-1h-2z"/>' +
      '<path class="prop-green" d="M35 10h2v3h-2zm4-3h2v5h-2z"/>';
  } else if (state === 'plant') {
    prop =
      '<path class="prop-pot" d="M34 18h10v6H35zM33 16h12v2H33z"/>' +
      '<path class="prop-green" d="M38 10h2v7h-2zm2 2h4v3h-4zm-5-2h3v4h-3z"/>';
  } else if (state === 'sleep') {
    prop =
      '<path class="prop-blanket" d="M18 16h21v8H14v-4h4z"/>' +
      '<path class="prop-pillow" d="M13 16h8v5h-8z"/>' +
      '<path class="prop-blue" d="M39 7h3v2h-2v2h-3V9h2zm5-4h3v2h-2v2h-3V5h2z"/>';
  } else if (state === 'music') {
    prop =
      '<path class="prop-dark" d="M7 6h3V3h13v3h3v3h3v8h-4v-7h-3V6H11v4H8v7H4V9h3z"/>' +
      '<path class="prop-blue" d="M4 11h4v7H4zm21 0h4v7h-4zM37 6h3v7h-2V9h-3V7h2zm5-4h3v7h-2V5h-3V3h2z"/>';
  } else {
    prop = '<path class="prop-gold" d="M38 3h3v3h3v3h-3v3h-3V9h-3V6h3z"/>';
  }
  return '<svg class="ch-crab-thinker" viewBox="0 0 48 28" shape-rendering="crispEdges">' + body + prop + '</svg>';
}
function _inflightSketchMarkup() {
  return '' +
    '<svg class="ch-sketch-thinker" viewBox="0 0 72 38" fill="none" aria-hidden="true">' +
      '<path class="sketch-paper" d="M12.5 9.5c8.4-1.3 20.4-1 29.8-.5 5.5.3 10.8.9 16 1.8-.4 6.7-.7 12.7-1.1 18.1-9.1 1-18.6 1.2-28.5.7-5.8-.3-11.5-.8-17.2-1.6.1-6.3.5-12.4 1-18.5Z"/>' +
      '<path class="sketch-paper sketch-paper-back" d="M13.8 10.8c7.5-1 19.6-.8 28.3-.4 5.4.3 10 .8 14.7 1.5-.1 5.2-.5 10.7-.9 15.7-8.2.8-17.6.8-26.3.4-5.7-.3-10.7-.7-16.4-1.3.2-5.1.4-10.5.6-15.9Z"/>' +
      '<path class="sketch-line line-a" d="M18 16.5c7.3-.9 16.2-.9 25.8-.1"/>' +
      '<path class="sketch-line line-b" d="M18 21.3c5.7-.6 12.6-.6 20.7.1"/>' +
      '<path class="sketch-line line-c" d="M18 25.8c9.1-.9 18.8-.8 29 .2"/>' +
      '<path class="sketch-pencil" d="M48.5 27.5 61 15.1l3.5 3.2-12.3 12.5-5.1 1.5 1.4-4.8Z"/>' +
      '<path class="sketch-pencil-edge" d="m58.7 17.4 3.3 3.1M48.5 27.5l3.7 3.3"/>' +
      '<path class="sketch-spark spark-a" d="M8 15.5c1.8-.7 3.6-.8 5.4-.4M9.5 22.7c1.4.8 2.9 1.2 4.5 1.1"/>' +
      '<path class="sketch-spark spark-b" d="M55.5 7.5c.9-1.4 1.3-2.9 1.3-4.4M59.5 8.8c1.2-1 2.5-1.6 4-1.9"/>' +
    '</svg>';
}
function _inflightThinkerMarkup() {
  return _inflightCrabMarkup() + _inflightSketchMarkup();
}
function _startInflightRotation() {
  if (_inflightRotateTimer) return;
  _inflightRotateTimer = setInterval(() => {
    const targets = document.querySelectorAll('.ch-gradient-text.ch-inflight-text');
    if (!targets.length) { clearInterval(_inflightRotateTimer); _inflightRotateTimer = null; return; }
    _inflightPhraseIdx = (_inflightPhraseIdx + 1) % _INFLIGHT_THINKING_PHRASES.length;
    targets.forEach(t => {
      t.style.opacity = '0';
      setTimeout(() => {
        t.textContent = _inflightText();
        const icon = t.closest('.ch-tool-group-head').querySelector('.ch-pixel-thinker');
        if (icon) icon.innerHTML = _inflightThinkerMarkup();
        t.style.opacity = '1';
      }, 300);
    });
  }, 3000);
}

function buildToolGroup(group) {
  const wrap = document.createElement('div');
  wrap.className = 'ch-tool-group';
  const allTools = group.tools || [];
  // AskUserQuestion gets its own interactive card and is pulled out of the
  // collapsed tool list so the user actually sees it.
  const askTools = allTools.filter(t => t && t.name === 'AskUserQuestion' && t.ask);
  const tools = allTools.filter(t => !(t && t.name === 'AskUserQuestion' && t.ask));
  if (askTools.length && !tools.length) {
    askTools.forEach(t => wrap.appendChild(_buildAskCard(t)));
    return wrap;
  }
  const total = tools.length;
  const pending = tools.filter(t => !t.done);
  const allDone = pending.length === 0;
  const isInflight = total === 1 && tools[0].id === 'inflight' && !tools[0].done;
  if (isInflight) wrap.classList.add('inflight');
  const isThinkingInflight = isInflight && (tools[0].name === 'thinking');
  if (isThinkingInflight) wrap.classList.add('thinking-inflight');

  const lastTool = isInflight ? tools[0] : (pending.length ? pending[pending.length - 1] : tools[tools.length - 1]);
  let label;
  if (isThinkingInflight) label = '__thinking_html__';
  else if (isInflight) label = tools[0].summary || tools[0].name;
  else if (allDone) label = `使用 ${total} 个工具`;
  else label = `正在执行 ${lastTool.summary || lastTool.name}`;

  const head = document.createElement('div');
  head.className = 'ch-tool-group-head';

  if (isInflight) {
    if (isThinkingInflight) {
      head.innerHTML =
        '<span class="ch-pixel-thinker" aria-hidden="true">' +
          _inflightThinkerMarkup() +
        '</span>' +
        '<span class="label ch-inflight-text ch-gradient-text"></span>';
      head.querySelector('.label').textContent = _inflightText();
      _startInflightRotation();
    } else {
      head.innerHTML = '<span class="ch-inflight-dot"></span><span class="label ch-inflight-text"></span>';
      head.querySelector('.label').textContent = label;
    }
  } else {
    const icon = _toolIcon(allDone ? '' : lastTool.name);
    head.innerHTML =
      '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>' +
      icon +
      '<span class="label"></span>' +
      (allDone ? '' : '<span class="spinner"></span>');
    head.querySelector('.label').textContent = label;
  }
  if (!isInflight) head.onclick = () => wrap.classList.toggle('expanded');
  wrap.appendChild(head);

  if (!isInflight) {
    const list = document.createElement('div');
    list.className = 'ch-tool-list';
    tools.forEach((t, i) => {
      const row = document.createElement('div');
      row.className = 'ch-tool-row ' + (t.done ? 'done' : 'pending');
      row.style.animationDelay = (i * 0.04) + 's';
      if (t.done) {
        const customIcon = _toolRowIcon(t.name);
        row.innerHTML = (customIcon || '<svg class="status" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>') +
          '<span class="text"></span>';
      } else {
        row.innerHTML = '<span class="status"></span><span class="text"></span>';
      }
      row.querySelector('.text').textContent = t.summary || t.name;
      list.appendChild(row);
    });
    wrap.appendChild(list);
  }
  askTools.forEach(t => wrap.appendChild(_buildAskCard(t)));
  return wrap;
}

// ===== AskUserQuestion choice card =====
// Per-tool ephemeral state so we can advance question focus locally and remember
// which checkboxes the user toggled before they hit submit.
const _askLocalState = (typeof window !== 'undefined' && window._askLocalState) || {};
if (typeof window !== 'undefined') window._askLocalState = _askLocalState;

function _ensureAskState(toolId) {
  if (!_askLocalState[toolId]) {
    _askLocalState[toolId] = {
      focused: 0,      // index of the question currently accepting input
      toggled: {},     // qIdx -> Set of oIdx (multiSelect picks already sent)
      sent: {},        // qIdx -> chosen label (for visual confirmation pre-tool_result)
      typeOpen: -1,    // qIdx whose "Type something" inline input is open
      submitting: false,
    };
  }
  return _askLocalState[toolId];
}

function _askPickedLabels(answer, opt) {
  // answer can be string, comma-joined string, array
  if (Array.isArray(answer)) return answer.includes(opt.label);
  if (typeof answer === 'string') {
    return answer === opt.label || answer.split(/,\s*/).includes(opt.label);
  }
  return false;
}

function _buildAskCard(tool) {
  const ask = tool.ask || {};
  const questions = ask.questions || [];
  const serverAnswers = ask.answers || {};
  const serverAnswered = !!tool.done;
  const rejected = !!ask.rejected;
  const state = _ensureAskState(tool.id);

  const card = document.createElement('div');
  card.className = 'ch-ask-card';
  card.dataset.toolId = tool.id || '';
  if (serverAnswered) card.classList.add('answered');
  if (rejected) card.classList.add('rejected');

  const head = document.createElement('div');
  head.className = 'ch-ask-head';
  let headLabel;
  if (serverAnswered) headLabel = rejected ? '你跳过了这次询问' : '你已回答 Claude 的问题';
  else if (questions.length > 1) headLabel = 'Claude 问你 ' + questions.length + ' 个问题';
  else headLabel = 'Claude 问你';
  head.innerHTML = '<span class="ch-ask-icon">❓</span><span class="ch-ask-title"></span>';
  head.querySelector('.ch-ask-title').textContent = headLabel;
  card.appendChild(head);

  questions.forEach((q, qIdx) => {
    const qBox = document.createElement('div');
    qBox.className = 'ch-ask-question';
    const isFocused = !serverAnswered && qIdx === state.focused;
    const isMulti = !!q.multiSelect;
    const opts = q.options || [];
    if (!isFocused && !serverAnswered) qBox.classList.add('locked');
    if (isFocused) qBox.classList.add('focused');

    if (q.header) {
      const tag = document.createElement('div');
      tag.className = 'ch-ask-q-tag';
      tag.textContent = q.header;
      qBox.appendChild(tag);
    }
    const title = document.createElement('div');
    title.className = 'ch-ask-q-title';
    title.textContent = q.question || '';
    qBox.appendChild(title);
    if (isMulti && !serverAnswered) {
      const hint = document.createElement('div');
      hint.className = 'ch-ask-q-hint';
      hint.textContent = '多选，选完点"提交本题"';
      qBox.appendChild(hint);
    }

    const optsWrap = document.createElement('div');
    optsWrap.className = 'ch-ask-options';
    if (isMulti) optsWrap.classList.add('multi');

    const localAnswer = state.sent[qIdx];
    const toggledSet = state.toggled[qIdx] || new Set();

    opts.forEach((opt, oIdx) => {
      const btn = document.createElement('button');
      btn.className = 'ch-ask-opt';
      btn.type = 'button';
      const digit = oIdx + 1;
      btn.innerHTML =
        '<span class="ch-ask-opt-num">' + digit + '</span>' +
        '<span class="ch-ask-opt-body">' +
          '<span class="ch-ask-opt-label"></span>' +
          (opt.description ? '<span class="ch-ask-opt-desc"></span>' : '') +
        '</span>' +
        '<span class="ch-ask-opt-check"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span>';
      btn.querySelector('.ch-ask-opt-label').textContent = opt.label;
      if (opt.description) btn.querySelector('.ch-ask-opt-desc').textContent = opt.description;

      let picked = false;
      if (serverAnswered) {
        const ans = serverAnswers[q.question] || serverAnswers[q.header];
        picked = _askPickedLabels(ans, opt);
      } else if (isMulti) {
        picked = toggledSet.has(oIdx);
      } else {
        picked = localAnswer === opt.label;
      }
      if (picked) btn.classList.add('picked');
      const interactive = !serverAnswered && isFocused && !state.submitting;
      btn.disabled = !interactive;
      if (interactive) {
        btn.addEventListener('click', () => _onAskOptionClick(tool, qIdx, oIdx, opt.label));
      }
      optsWrap.appendChild(btn);
    });

    // "Type something" — only meaningful when focused & not yet answered.
    if (!serverAnswered && isFocused) {
      const typeBtn = document.createElement('button');
      typeBtn.className = 'ch-ask-opt ch-ask-opt-type';
      typeBtn.type = 'button';
      const typeDigit = opts.length + 1;
      typeBtn.innerHTML =
        '<span class="ch-ask-opt-num">' + typeDigit + '</span>' +
        '<span class="ch-ask-opt-body"><span class="ch-ask-opt-label">✍️ 自己写一个</span></span>';
      typeBtn.addEventListener('click', () => _onAskTypeOpen(tool, qIdx, opts.length, card));
      optsWrap.appendChild(typeBtn);
    }

    // Already-answered + the answer doesn't match any preset option → user
    // wrote their own text via "Type something". Surface it so the card
    // actually shows what they wrote.
    if (serverAnswered) {
      const ans = serverAnswers[q.question] || serverAnswers[q.header];
      if (ans) {
        const ansList = Array.isArray(ans) ? ans : String(ans).split(/,\s*/);
        const optLabels = new Set(opts.map(o => o.label));
        ansList.filter(a => a && !optLabels.has(a)).forEach(custom => {
          const row = document.createElement('div');
          row.className = 'ch-ask-opt ch-ask-opt-type picked';
          row.innerHTML =
            '<span class="ch-ask-opt-num">✍️</span>' +
            '<span class="ch-ask-opt-body">' +
              '<span class="ch-ask-opt-desc">自己写的</span>' +
              '<span class="ch-ask-opt-label"></span>' +
            '</span>' +
            '<span class="ch-ask-opt-check"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span>';
          row.querySelector('.ch-ask-opt-label').textContent = custom;
          optsWrap.appendChild(row);
        });
      }
    }
    qBox.appendChild(optsWrap);

    // Inline text input for "Type something"
    if (state.typeOpen === qIdx && isFocused && !serverAnswered) {
      const inputWrap = document.createElement('div');
      inputWrap.className = 'ch-ask-type-input';
      inputWrap.innerHTML =
        '<input type="text" placeholder="写下你的答案…" />' +
        '<button class="ch-ask-type-send" type="button">发送</button>';
      const input = inputWrap.querySelector('input');
      const sendBtn = inputWrap.querySelector('.ch-ask-type-send');
      const submit = () => {
        const v = (input.value || '').trim();
        if (!v) { input.focus(); return; }
        _onAskTypeSubmit(tool, qIdx, v);
      };
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
      sendBtn.addEventListener('click', submit);
      qBox.appendChild(inputWrap);
      setTimeout(() => input.focus(), 30);
    }

    if (isMulti && isFocused && !serverAnswered) {
      const submitRow = document.createElement('div');
      submitRow.className = 'ch-ask-submit-row';
      const submitBtn = document.createElement('button');
      submitBtn.className = 'ch-ask-submit-btn';
      submitBtn.type = 'button';
      submitBtn.textContent = toggledSet.size ? '提交本题 (' + toggledSet.size + ')' : '至少选一项';
      submitBtn.disabled = !toggledSet.size || state.submitting;
      submitBtn.addEventListener('click', () => _onAskMultiSubmit(tool, qIdx));
      submitRow.appendChild(submitBtn);
      qBox.appendChild(submitRow);
    }

    card.appendChild(qBox);
  });

  if (!serverAnswered) {
    const foot = document.createElement('div');
    foot.className = 'ch-ask-foot';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'ch-ask-cancel';
    cancelBtn.type = 'button';
    cancelBtn.textContent = '取消（去终端处理）';
    cancelBtn.addEventListener('click', () => _onAskCancel(tool));
    foot.appendChild(cancelBtn);
    card.appendChild(foot);
  } else if (rejected) {
    const note = document.createElement('div');
    note.className = 'ch-ask-foot-note';
    note.textContent = '没在 chat 里完成 — 已经回到对话';
    card.appendChild(note);
  }

  return card;
}

function _askRequestRender() {
  // Local state changed (focus advanced, checkbox toggled). Bust the fingerprint
  // gate so renderChatMessages actually repaints, and feed it the cached
  // messages so we don't make a network round-trip for a UI-only update.
  lastChatFingerprint = null;
  if (typeof activeChat === 'undefined' || !activeChat) return;
  if (typeof renderChatMessages !== 'function') return;
  const cached = _chLiveMessageCache.get(activeChat);
  renderChatMessages(activeChat, cached);
}

async function _sendAskKey(data) {
  if (typeof postTerminalInput !== 'function') return false;
  const sess = (typeof activeChat !== 'undefined' && activeChat) || (typeof activeSession !== 'undefined' && activeSession);
  if (!sess) return false;
  return postTerminalInput(data, sess);
}

async function _maybeAutoSubmitReview(tool, justFinishedIdx) {
  // After the last question of a multi-question set is answered, the TUI lands
  // on a Review screen ("1. Submit / 2. Cancel"). Send '1' to close it.
  const total = ((tool.ask || {}).questions || []).length;
  if (total <= 1) return;
  if (justFinishedIdx !== total - 1) return;
  await new Promise(r => setTimeout(r, 90));
  const state = _ensureAskState(tool.id);
  state.submitting = true;
  await _sendAskKey('1');
  state.submitting = false;
}

async function _onAskOptionClick(tool, qIdx, oIdx, label) {
  const state = _ensureAskState(tool.id);
  if (state.submitting) return;
  const ask = tool.ask || {};
  const q = (ask.questions || [])[qIdx];
  if (!q) return;
  const digit = String(oIdx + 1);
  if (q.multiSelect) {
    // Toggle: send digit (TUI toggles), update local set.
    state.submitting = true;
    const ok = await _sendAskKey(digit);
    state.submitting = false;
    if (!ok) return;
    if (!state.toggled[qIdx]) state.toggled[qIdx] = new Set();
    if (state.toggled[qIdx].has(oIdx)) state.toggled[qIdx].delete(oIdx);
    else state.toggled[qIdx].add(oIdx);
  } else {
    state.submitting = true;
    const ok = await _sendAskKey(digit);
    state.submitting = false;
    if (!ok) return;
    state.sent[qIdx] = label;
    // Single-select auto-advances in the TUI, mirror that locally so the next
    // question becomes the focused one immediately.
    state.focused = qIdx + 1;
    await _maybeAutoSubmitReview(tool, qIdx);
  }
  _askRequestRender();
}

function _onAskTypeOpen(tool, qIdx, numOptions, _cardEl) {
  const state = _ensureAskState(tool.id);
  state.typeOpen = qIdx;
  // We don't pre-send the hotkey — the TUI enters edit mode the moment we
  // press the digit, but we want to wait until the user actually has text to
  // submit so they can change their mind without poisoning the TUI buffer.
  _askRequestRender();
}

async function _onAskTypeSubmit(tool, qIdx, text) {
  const state = _ensureAskState(tool.id);
  if (state.submitting) return;
  const ask = tool.ask || {};
  const q = (ask.questions || [])[qIdx];
  if (!q) return;
  const numOptions = (q.options || []).length;
  const typeDigit = String(numOptions + 1);  // "Type something" lives right after options
  state.submitting = true;
  // Sequence: digit (enter edit mode) → text → Enter
  let ok = await _sendAskKey(typeDigit);
  if (ok) ok = await _sendAskKey(text);
  if (ok) ok = await _sendAskKey('\r');
  state.submitting = false;
  if (!ok) return;
  state.sent[qIdx] = text;
  state.typeOpen = -1;
  state.focused = qIdx + 1;
  await _maybeAutoSubmitReview(tool, qIdx);
  _askRequestRender();
}

async function _onAskMultiSubmit(tool, qIdx) {
  const state = _ensureAskState(tool.id);
  if (state.submitting) return;
  const set = state.toggled[qIdx];
  if (!set || !set.size) return;
  state.submitting = true;
  // Tab → move to next tab (next question or Submit). If this is the last
  // question, Tab lands on the Submit screen; we then send '1' to confirm.
  // If not last, Tab lands on the next question (TUI advances naturally).
  const ask = tool.ask || {};
  const isLast = qIdx === (ask.questions || []).length - 1;
  let ok = await _sendAskKey('\t');
  if (ok && isLast) ok = await _sendAskKey('1');
  state.submitting = false;
  if (!ok) return;
  state.focused = qIdx + 1;
  _askRequestRender();
}

async function _onAskCancel(tool) {
  const state = _ensureAskState(tool.id);
  if (state.submitting) return;
  state.submitting = true;
  await _sendAskKey('\x1b');  // Esc
  state.submitting = false;
  _askRequestRender();
}

// ===== Process group: merge consecutive tool_group + thinking into one card =====
// Why: a single turn often interleaves "use 3 tools → think → use 2 tools →
// think". Showing each as its own collapsed strip clutters the bubble. Roll
// them up into one outer card with a playful random phrase + icon counters,
// expandable to see the original timeline. Pure text blocks (the actual reply
// content) break the run so the final answer never gets hidden.

function _processSeedHash(s) {
  let h = 0;
  const str = String(s || '');
  for (let i = 0; i < str.length; i++) h = ((h << 5) - h + str.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function _mergeProcessBlocks(blocks) {
  const out = [];
  let buf = [];
  const flush = () => {
    if (!buf.length) return;
    let toolCount = 0;
    let thinkCount = 0;
    let seed = '';
    buf.forEach(b => {
      if (b.type === 'tool_group') {
        toolCount += (b.tools || []).length;
        if (!seed) {
          const firstTool = (b.tools || [])[0];
          if (firstTool && firstTool.id) seed = firstTool.id;
        }
      } else if (b.type === 'thinking') {
        thinkCount += 1;
        if (!seed) seed = (b.text || '').slice(0, 24);
      }
    });
    // Only roll up when the run actually mixes tools AND thinking. A run of
    // pure tools or pure thinking stays as-is (the user explicitly asked for
    // this — no synthetic outer wrapper when it adds nothing).
    if (toolCount && thinkCount) {
      out.push({
        type: 'process_group',
        children: buf.slice(),
        toolCount,
        thinkCount,
        seed: seed || String(out.length),
      });
    } else {
      out.push(...buf);
    }
    buf = [];
  };
  blocks.forEach(b => {
    if (b && (b.type === 'tool_group' || b.type === 'thinking')) {
      buf.push(b);
    } else {
      flush();
      out.push(b);
    }
  });
  flush();
  return out;
}

// Hand-rolled line icons, RemixIcon-ish 1.8 stroke for visual harmony with
// the rest of chat (which already uses lucide/feather-flavored strokes).
function _processIconTool() {
  return '<svg class="ch-pg-stat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M14.7 6.3a3 3 0 1 1-4 4l-6.3 6.3a1.5 1.5 0 0 0 2.1 2.1l6.3-6.3a3 3 0 0 0 4-4l-2.1 2.1-2-2 2-2z"/>' +
    '<path d="M14 14l5.5 5.5a1.6 1.6 0 0 1-2.3 2.3L11.7 16.3"/>' +
    '</svg>';
}
function _processIconThink() {
  return '<svg class="ch-pg-stat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M9 18h6"/>' +
    '<path d="M10 21h4"/>' +
    '<path d="M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.4 1.2 2.2h4.8c.1-.8.5-1.6 1.2-2.2A6 6 0 0 0 12 3z"/>' +
    '<path d="M12 9v3"/>' +
    '<path d="M10.5 11.5l3 0"/>' +
    '</svg>';
}

function buildProcessGroup(block) {
  const wrap = document.createElement('div');
  wrap.className = 'ch-process-group';
  const phrases = _INFLIGHT_THINKING_PHRASES;
  const p = phrases[_processSeedHash(block.seed) % phrases.length];
  const word = p[1];

  const head = document.createElement('div');
  head.className = 'ch-pg-head';
  head.innerHTML =
    '<svg class="ch-pg-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>' +
    '<span class="ch-pg-title">' +
      '<svg class="ch-pg-clover" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M16.17 7.83 2 22"/>' +
        '<path d="M4.02 12a2.827 2.827 0 1 1 3.81-4.17A2.827 2.827 0 1 1 12 4.02a2.827 2.827 0 1 1 4.17 3.81A2.827 2.827 0 1 1 19.98 12a2.827 2.827 0 1 1-3.81 4.17A2.827 2.827 0 1 1 12 19.98a2.827 2.827 0 1 1-4.17-3.81A2.827 2.827 0 1 1 4.02 12"/>' +
        '<path d="m7.83 7.83 8.34 8.34"/>' +
      '</svg>' +
      '<span class="ch-pg-word"></span>' +
    '</span>' +
    '<span class="ch-pg-stats">' +
      (block.toolCount ? '<span class="ch-pg-stat" title="工具调用">' + _processIconTool() + '<span class="ch-pg-stat-num"></span></span>' : '') +
      (block.thinkCount ? '<span class="ch-pg-stat" title="思考段落">' + _processIconThink() + '<span class="ch-pg-stat-num"></span></span>' : '') +
    '</span>';
  head.querySelector('.ch-pg-word').textContent = word;
  const numEls = head.querySelectorAll('.ch-pg-stat-num');
  let ni = 0;
  if (block.toolCount) numEls[ni++].textContent = String(block.toolCount);
  if (block.thinkCount) numEls[ni++].textContent = String(block.thinkCount);
  head.addEventListener('click', () => wrap.classList.toggle('expanded'));
  wrap.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ch-pg-body';
  (block.children || []).forEach(child => {
    if (child.type === 'tool_group') {
      body.appendChild(buildToolGroup(child));
      // Surface inline file cards inside the process group, same as we do
      // at the top level — otherwise edits/writes disappear from view.
      (child.tools || []).forEach(t => {
        if (t.file && t.file.path && t.done) {
          body.appendChild(_buildInlineFileCard(t.file));
        }
      });
    } else if (child.type === 'thinking') {
      body.appendChild(buildThinkingBlock(child));
    }
  });
  wrap.appendChild(body);
  return wrap;
}

function _buildInlineFileCard(file) {
  const card = document.createElement('div');
  card.className = 'ch-file-card inline';
  const ext = (file.name || '').split('.').pop() || '';
  const isImage = /^(jpg|jpeg|png|gif|webp|svg|bmp|ico)$/i.test(ext);
  card.innerHTML =
    '<div class="ch-file-icon">' + (isImage ? '🖼' : ext.slice(0, 4).toUpperCase()) + '</div>' +
    '<div class="ch-file-info"><div class="ch-file-name"></div><div class="ch-file-meta"></div></div>' +
    '<button class="ch-file-dl" title="下载" aria-label="下载"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><polyline points="7 10 12 15 17 10"/><path d="M4 20h16"/></svg></button>';
  card.querySelector('.ch-file-name').textContent = file.name || file.path;
  card.querySelector('.ch-file-meta').textContent = file.action === 'edit' ? '已修改' : '已创建';
  const dlUrl = API + '/files/download?path=' + encodeURIComponent(file.path) + '&token=' + encodeURIComponent(TOKEN);
  card.querySelector('.ch-file-dl').onclick = (e) => { e.stopPropagation(); _downloadFile(dlUrl, file.name); };
  card.onclick = () => _previewFile(file, dlUrl);
  return card;
}

const _PREVIEWABLE_EXT = /^(txt|md|json|js|ts|jsx|tsx|py|css|xml|yaml|yml|toml|sh|bash|log|csv|sql|rs|go|java|c|cpp|h|hpp|rb|php|swift|kt|conf|cfg|ini|env)$/i;
const _IMAGE_EXT = /^(jpg|jpeg|png|gif|webp|svg|bmp|ico)$/i;

function _downloadFile(url, filename) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || '';
  a.target = '_blank';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

async function _previewFile(file, dlUrl) {
  const ext = (file.name || '').split('.').pop() || '';
  if (_IMAGE_EXT.test(ext)) {
    _showImagePreview({ available: true, serve_url: '/api/files/download?path=' + encodeURIComponent(file.path) }, {});
    return;
  }
  if (/^html?$/i.test(ext)) {
    _previewHtml(file, dlUrl);
    return;
  }
  if (!_PREVIEWABLE_EXT.test(ext)) {
    _downloadFile(dlUrl, file.name);
    return;
  }
  let sheet = document.getElementById('chFilePreview');
  if (!sheet) {
    sheet = document.createElement('div');
    sheet.className = 'ch-thinking-sheet ch-file-preview-sheet';
    sheet.id = 'chFilePreview';
    sheet.innerHTML =
      '<div class="ch-ts-backdrop"></div>' +
      '<div class="ch-ts-panel">' +
        '<div class="ch-ts-handle"><div class="ch-ts-handle-bar"></div></div>' +
        '<div class="ch-ts-header">' +
          '<button class="ch-ts-close" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>' +
          '<div class="ch-ts-title"></div>' +
        '</div>' +
        '<div class="ch-ts-body" style="font-family:\'SF Mono\',Menlo,Consolas,monospace;font-size:0.88rem;line-height:1.5;tab-size:2"></div>' +
        '<div class="ch-file-preview-footer">' +
          '<button class="ch-file-preview-dl">下载文件</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(sheet);
    sheet.querySelector('.ch-ts-backdrop').onclick = () => { sheet.classList.remove('open'); };
    sheet.querySelector('.ch-ts-close').onclick = () => { sheet.classList.remove('open'); };
    sheet.querySelector('.ch-ts-handle').onclick = () => {
      sheet.querySelector('.ch-ts-panel').classList.toggle('expanded');
    };
  }
  const panel = sheet.querySelector('.ch-ts-panel');
  if (panel) panel.classList.remove('expanded');
  sheet.querySelector('.ch-ts-title').textContent = file.name || file.path;
  sheet.querySelector('.ch-ts-body').textContent = '加载中...';
  sheet.querySelector('.ch-file-preview-dl').onclick = () => _downloadFile(dlUrl, file.name);
  sheet.classList.add('open');
  try {
    const r = await fetch(dlUrl);
    if (!r.ok) throw new Error('加载失败');
    const text = await r.text();
    const body = sheet.querySelector('.ch-ts-body');
    body.textContent = text || '（空文件）';
  } catch (e) {
    sheet.querySelector('.ch-ts-body').textContent = '无法加载文件内容';
  }
}

function _previewHtml(file, dlUrl) {
  let sheet = document.getElementById('chHtmlPreview');
  if (!sheet) {
    sheet = document.createElement('div');
    sheet.className = 'ch-thinking-sheet ch-html-preview-sheet';
    sheet.id = 'chHtmlPreview';
    sheet.innerHTML =
      '<div class="ch-ts-backdrop"></div>' +
      '<div class="ch-ts-panel">' +
        '<div class="ch-ts-handle"><div class="ch-ts-handle-bar"></div></div>' +
        '<div class="ch-ts-header">' +
          '<button class="ch-ts-close" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>' +
          '<div class="ch-ts-title"></div>' +
        '</div>' +
        '<iframe class="ch-html-iframe" sandbox="allow-scripts allow-same-origin"></iframe>' +
        '<div class="ch-file-preview-footer">' +
          '<button class="ch-file-preview-dl">下载文件</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(sheet);
    sheet.querySelector('.ch-ts-backdrop').onclick = () => { sheet.classList.remove('open'); };
    sheet.querySelector('.ch-ts-close').onclick = () => { sheet.classList.remove('open'); };
    sheet.querySelector('.ch-ts-handle').onclick = () => {
      sheet.querySelector('.ch-ts-panel').classList.toggle('expanded');
    };
  }
  const panel = sheet.querySelector('.ch-ts-panel');
  if (panel) panel.classList.remove('expanded');
  sheet.querySelector('.ch-ts-title').textContent = file.name || file.path;
  const iframe = sheet.querySelector('.ch-html-iframe');
  iframe.srcdoc = '<p style="text-align:center;color:#999;padding:40px">加载中...</p>';
  sheet.querySelector('.ch-file-preview-dl').onclick = () => _downloadFile(dlUrl, file.name);
  sheet.classList.add('open');
  fetch(dlUrl).then(r => r.text()).then(html => {
    iframe.srcdoc = html;
  }).catch(() => {
    iframe.srcdoc = '<p style="text-align:center;color:#c04830;padding:40px">加载失败</p>';
  });
}

function buildThinkingBlock(block) {
  const text = block.text || '';
  const preview = text.replace(/\s+/g, ' ').trim();
  const strip = document.createElement('div');
  strip.className = 'ch-thinking-strip';
  strip.innerHTML =
    '<svg class="ch-ts-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' +
    '<span class="ch-ts-text"></span>' +
    '<svg class="ch-ts-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>';
  strip.querySelector('.ch-ts-text').textContent = preview.slice(0, 120) || 'Thought process';
  strip.onclick = () => openThinkingSheet(text);
  return strip;
}

function openThinkingSheet(text) {
  let sheet = document.getElementById('chThinkingSheet');
  if (!sheet) {
    sheet = document.createElement('div');
    sheet.className = 'ch-thinking-sheet';
    sheet.id = 'chThinkingSheet';
    sheet.innerHTML =
      '<div class="ch-ts-backdrop"></div>' +
      '<div class="ch-ts-panel">' +
        '<div class="ch-ts-handle"><div class="ch-ts-handle-bar"></div></div>' +
        '<div class="ch-ts-header">' +
          '<button class="ch-ts-close" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>' +
          '<div class="ch-ts-title">Thought process</div>' +
        '</div>' +
        '<div class="ch-ts-body"></div>' +
      '</div>';
    document.body.appendChild(sheet);
    sheet.querySelector('.ch-ts-backdrop').onclick = () => closeThinkingSheet();
    sheet.querySelector('.ch-ts-close').onclick = () => closeThinkingSheet();
    sheet.querySelector('.ch-ts-handle').onclick = () => {
      const panel = sheet.querySelector('.ch-ts-panel');
      if (panel) panel.classList.toggle('expanded');
    };
  }
  const panel = sheet.querySelector('.ch-ts-panel');
  if (panel) panel.classList.remove('expanded');
  sheet.querySelector('.ch-ts-body').textContent = text;
  sheet.classList.add('open');
}

function closeThinkingSheet() {
  const sheet = document.getElementById('chThinkingSheet');
  if (!sheet) return;
  const panel = sheet.querySelector('.ch-ts-panel');
  if (panel) panel.classList.remove('expanded');
  sheet.classList.remove('open');
}

function chatFingerprint(msgs) {
  // Cheap signature: count + per-message (role, ts, last-block snippet). Also
  // folds tool-group done states so a tool flipping pending→done re-renders.
  if (!msgs.length) return '__empty__';
  return msgs.map(m => {
    const blocks = m.blocks || [];
    const last = blocks.slice(-1)[0] || {};
    const rawSnip = last.text || last.label || last.src || '';
    const snip = rawSnip.slice(0, 24) + ':' + rawSnip.slice(-40);
    // Sum all tool group done-states across this message, so any tool finishing repaints.
    // Also walk into process_group.children so rolled-up runs still trigger re-renders
    // when an inner tool flips pending → done.
    const groupSig = (g) => (g.tools || []).map(t => [t.id || '', t.name || '', t.summary || '', t.done ? '1' : '0'].join(':')).join(',');
    const toolBits = blocks
      .flatMap(b => {
        if (b.type === 'tool_group') return [b];
        if (b.type === 'process_group') return (b.children || []).filter(c => c && c.type === 'tool_group');
        return [];
      })
      .map(groupSig)
      .join('-');
    return (m.role || '') + ':' + (m.ts || '') + ':' + blocks.length + ':' + snip + ':' + toolBits;
  }).join('|');
}

function _compactionSummaryText(summary) {
  const text = summary || '';
  const marker = text.indexOf('Summary:');
  return marker >= 0 ? text.slice(marker + 'Summary:'.length).trim() : text.trim();
}
function _formatCompactCount(value) {
  if (!Number.isFinite(value)) return '';
  return value >= 1000 ? Math.round(value / 1000) + 'k' : String(value);
}
function buildCompactionCard(block, ts) {
  const card = document.createElement('div');
  const running = block.status === 'running';
  card.className = 'ch-compact-card' + (running ? ' running' : '');
  const head = document.createElement(running ? 'div' : 'button');
  if (!running) head.type = 'button';
  head.className = 'ch-compact-head';
  const meta = block.metadata || {};
  const info = [];
  if (!running && Number.isFinite(meta.durationMs)) info.push(Math.round(meta.durationMs / 1000) + 's');
  if (!running && Number.isFinite(meta.preTokens) && Number.isFinite(meta.postTokens)) {
    info.push(_formatCompactCount(meta.preTokens) + ' -> ' + _formatCompactCount(meta.postTokens) + ' tokens');
  }
  const when = _messageTimestamp(ts);
  if (when) info.push(when);
  head.innerHTML = '<span class="ch-compact-icon"></span><span class="ch-compact-label"></span><span class="ch-compact-meta"></span>' + (running ? '' : '<span class="ch-compact-chevron">⌄</span>');
  head.querySelector('.ch-compact-icon').textContent = running ? '...' : '✓';
  head.querySelector('.ch-compact-label').textContent = running ? '正在压缩上下文' : '上下文已压缩';
  head.querySelector('.ch-compact-meta').textContent = info.join(' · ');
  card.appendChild(head);
  if (!running) {
    const detail = document.createElement('div');
    detail.className = 'ch-compact-detail';
    detail.innerHTML = chMdRender(_compactionSummaryText(block.summary));
    card.appendChild(detail);
    head.onclick = () => card.classList.toggle('open');
  }
  return card;
}
function renderCompactionHistory(compactions) {
  const banner = document.getElementById('chLatestCompaction');
  if (!banner) return;
  const records = (compactions || []).filter(message => (message.blocks || []).some(item => item.type === 'compaction' && item.status === 'done'));
  if (!records.length) {
    banner.style.display = 'none'; banner.innerHTML = '';
    _chCompactionHistorySignature = null; _chCompactionHistoryOpen = false;
    return;
  }
  const signature = records.map(message => String(message.source_uuid || '')).join('|');
  if (_chCompactionHistorySignature === signature && banner.querySelector('.ch-compact-history')) {
    banner.style.display = '';
    return;
  }
  _chCompactionHistorySignature = signature;
  _chCompactionHistoryOpen = false;
  banner.classList.remove('history-open');
  banner.innerHTML = '<button type="button" class="ch-compact-history-toggle"><span></span><small></small><b>查看记录</b></button><div class="ch-compact-history"></div>';
  banner.querySelector('.ch-compact-history-toggle span').textContent = '上下文压缩 · ' + records.length + ' 次';
  const recentBlock = records[0].blocks.find(item => item.type === 'compaction');
  const recentMeta = recentBlock.metadata || {};
  const recent = [];
  if (Number.isFinite(recentMeta.durationMs)) recent.push('最近 ' + Math.round(recentMeta.durationMs / 1000) + 's');
  if (Number.isFinite(recentMeta.preTokens) && Number.isFinite(recentMeta.postTokens)) recent.push(_formatCompactCount(recentMeta.preTokens) + ' -> ' + _formatCompactCount(recentMeta.postTokens));
  banner.querySelector('.ch-compact-history-toggle small').textContent = recent.join(' · ');
  const history = banner.querySelector('.ch-compact-history');
  records.forEach((message, index) => {
    const block = message.blocks.find(item => item.type === 'compaction');
    const meta = block.metadata || {};
    const entry = document.createElement('section');
    entry.className = 'ch-compact-entry';
    const pieces = [_messageTimestamp(message.ts)];
    if (Number.isFinite(meta.durationMs)) pieces.push(Math.round(meta.durationMs / 1000) + 's');
    if (Number.isFinite(meta.preTokens) && Number.isFinite(meta.postTokens)) pieces.push(_formatCompactCount(meta.preTokens) + ' -> ' + _formatCompactCount(meta.postTokens) + ' tokens');
    entry.innerHTML = '<button type="button" class="ch-compact-entry-head"><span></span><small></small><b>展开</b></button><div class="ch-latest-summary"></div>';
    entry.querySelector('span').textContent = index === 0 ? '最近一次压缩' : '第 ' + (records.length - index) + ' 次压缩';
    entry.querySelector('small').textContent = pieces.filter(Boolean).join(' · ');
    const summary = entry.querySelector('.ch-latest-summary');
    if (block.summary) summary.innerHTML = chMdRender(_compactionSummaryText(block.summary));
    entry.querySelector('.ch-compact-entry-head').onclick = () => {
      if (!block.summary) return;
      const open = entry.classList.toggle('open');
      entry.querySelector('b').textContent = open ? '收起' : '展开';
    };
    history.appendChild(entry);
  });
  banner.querySelector('.ch-compact-history-toggle').onclick = async () => {
    if (!_chCompactionRecordsLoaded) {
      banner.querySelector('.ch-compact-history-toggle b').textContent = '加载中...';
      try {
        const response = await fetch(API + '/sessions/' + encodeURIComponent(activeChat) + '/compactions', { headers: { Authorization: 'Bearer ' + TOKEN } });
        if (!response.ok) return;
        const payload = await response.json();
        _chCompactionRecordsLoaded = true;
        _chCompactionHistoryOpen = true;
        _chCompactionHistorySignature = null;
        renderCompactionHistory(payload.compactions || []);
        banner.classList.add('history-open');
        banner.querySelector('.ch-compact-history-toggle b').textContent = '收起记录';
      } catch (e) {
        banner.querySelector('.ch-compact-history-toggle b').textContent = '重试';
      }
      return;
    }
    _chCompactionHistoryOpen = !_chCompactionHistoryOpen;
    banner.classList.toggle('history-open', _chCompactionHistoryOpen);
    banner.querySelector('.ch-compact-history-toggle b').textContent = _chCompactionHistoryOpen ? '收起记录' : '查看记录';
  };
  banner.style.display = '';
}
function chOpenLatestCompaction(latest) {
  const uuid = latest && latest.source_uuid;
  const wrap = document.getElementById('chMsgs');
  const target = uuid && wrap ? wrap.querySelector('[data-source-uuid="' + CSS.escape(String(uuid)) + '"]') : null;
  if (target) {
    target.classList.add('open');
    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    return;
  }
  if (!uuid || !activeChat || chViewingArchive || chViewingUnified) return;
  _chLiveFocusSourceUuid = String(uuid);
  _chPendingOpenCompactionUuid = String(uuid);
  chMsgLimit = Math.max(chMsgLimit, _CH_DEFAULT_MSG_LIMIT * 3);
  lastChatFingerprint = null;
  renderChatMessages(activeChat);
}

// Terminal prompt card — surfaces blocking prompts (feedback survey,
// confirmations) from the tmux pane so the user can respond from chat.
function _chBuildTerminalPrompt(prompt, sessionName) {
  const card = document.createElement('div');
  card.className = 'ch-terminal-prompt';
  const label = document.createElement('div');
  label.className = 'ch-terminal-prompt-label';
  label.textContent = '⚠ ' + (prompt.label || '终端需要输入');
  card.appendChild(label);
  if (prompt.text) {
    const text = document.createElement('div');
    text.className = 'ch-terminal-prompt-text';
    text.textContent = prompt.text;
    card.appendChild(text);
  }
  const actions = document.createElement('div');
  actions.className = 'ch-terminal-prompt-actions';
  (prompt.actions || []).forEach(action => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ch-terminal-prompt-btn';
    btn.textContent = action.label;
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = '发送中…';
      try {
        await fetch(API + '/sessions/' + encodeURIComponent(sessionName) + '/terminal-respond', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
          body: JSON.stringify({ keys: action.keys }),
        });
        card.remove();
        lastChatFingerprint = null;
        setTimeout(() => renderChatMessages(sessionName), 1500);
      } catch (e) {
        btn.textContent = '失败';
        btn.disabled = false;
      }
    };
    actions.appendChild(btn);
  });
  card.appendChild(actions);
  return card;
}

// A merged chat view can span multiple physical sessions; mark where one
// jsonl ends and the next begins.
function _chBuildSessionDivider(m) {
  const div = document.createElement('div');
  div.className = 'ch-session-divider';
  const pill = document.createElement('span');
  pill.className = 'ch-session-divider-pill';
  const when = _messageTimestamp(m.ts);
  pill.textContent = '新会话 · ' + String(m.sid).slice(0, 8) + (when ? ' · ' + when : '');
  div.appendChild(pill);
  return div;
}

async function renderChatMessages(name, cachedData = null) {
  const liveFetchKey = (!cachedData && !chViewingArchive && !chViewingUnified) ? 'live:' + name : null;
  if (liveFetchKey && _chRenderInFlightKey === liveFetchKey) {
    _chRenderQueuedKey = liveFetchKey;
    return;
  }
  if (liveFetchKey) _chRenderInFlightKey = liveFetchKey;
  const requestSeq = ++_chRenderRequestSeq;
  const wrap = document.getElementById('chMsgs');
  const empty = document.getElementById('chEmpty');
  if (!wrap) {
    if (liveFetchKey && _chRenderInFlightKey === liveFetchKey) _chRenderInFlightKey = null;
    return;
  }
  try {
    let data = cachedData;
    if (!data) {
      const url = chViewingArchive
        ? API + '/archived-sessions/' + encodeURIComponent(chViewingArchive) + '/messages?limit=' + chMsgLimit + (_chPendingFocusSourceUuid ? '&focus_uuid=' + encodeURIComponent(_chPendingFocusSourceUuid) : '')
        : chViewingUnified
          ? API + '/messages/session/' + encodeURIComponent(chViewingUnified) + '/blocks?limit=' + chMsgLimit + (_chPendingFocusMessageId ? '&focus_id=' + encodeURIComponent(_chPendingFocusMessageId) : '')
          : API + '/sessions/' + encodeURIComponent(name) + '/chat-messages?limit=' + chMsgLimit + (_chLiveFocusSourceUuid ? '&focus_uuid=' + encodeURIComponent(_chLiveFocusSourceUuid) : '') + (_chLiveFocusMessageId ? '&focus_id=' + encodeURIComponent(_chLiveFocusMessageId) : '');
      const r = await fetch(url, {
        headers: { Authorization: 'Bearer ' + TOKEN },
      });
      if (r.status === 401) {
        localStorage.removeItem('prism_token');
        TOKEN = '';
        if (typeof showLogin === 'function') showLogin();
        return;
      }
      if (!r.ok) {
        console.warn('chat messages fetch failed', r.status);
        return;
      }
      data = await r.json();
      if (!chViewingArchive && !chViewingUnified) _chLiveMessageCache.set(name, data);
    }
    if (requestSeq !== _chRenderRequestSeq || chSub !== 'detail' || activeChat !== name) return;
    const msgs = data.messages || [];
    if (!chViewingArchive && !chViewingUnified) _chQueueNewReplyReveal(name, msgs);
    chMaybeSyncModelLabel(name);
    if (!chViewingArchive && !chViewingUnified) _chScheduleChatRefresh(_chHasLiveWork(msgs) ? 900 : 3000);
    const pendingMsgs = (!chViewingArchive && !chViewingUnified) ? _chReconcilePending(name, msgs) : [];
    if (!_chCompactionRecordsLoaded) renderCompactionHistory(data.compaction_overview || []);
    // Fingerprint also keys on chMsgLimit so bumping it via "load earlier"
    // bypasses the no-change short-circuit and forces a fresh render.
    const pendingFp = pendingMsgs.map(message => message.id + ':' + message.status).join('|');
    // Key on terminal_prompt too: it can appear/disappear while messages stay
    // unchanged (e.g. a model-switch menu pops up after a quiet turn).
    const tpFp = data.terminal_prompt ? (data.terminal_prompt.label + ':' + (data.terminal_prompt.actions || []).length) : '';
    const fp = chMsgLimit + ':' + chatFingerprint(msgs) + ':pending:' + pendingFp + ':reveal:' + _chRevealFingerprint() + ':tp:' + tpFp;
    if (fp === lastChatFingerprint) return;  // nothing changed — no repaint, no flicker
    lastChatFingerprint = fp;
    if (!msgs.length && !pendingMsgs.length && !(data.terminal_prompt && !chViewingArchive && !chViewingUnified)) {
      wrap.innerHTML = '';
      empty.style.display = '';
      return;
    }
    empty.style.display = 'none';
    const atBottom = (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight) < 80;
    const oldScrollHeight = wrap.scrollHeight;
    const oldScrollTop = wrap.scrollTop;
    const hasLiveSearchAnchor = Boolean((_chLiveFocusSourceUuid || _chLiveFocusMessageId) && !chViewingArchive && !chViewingUnified);
    const anchorSelector = _chLiveFocusSourceUuid
      ? '[data-source-uuid="' + CSS.escape(String(_chLiveFocusSourceUuid)) + '"]'
      : (_chLiveFocusMessageId ? '[data-message-id="' + CSS.escape(String(_chLiveFocusMessageId)) + '"]' : null);
    const oldAnchor = hasLiveSearchAnchor && anchorSelector ? wrap.querySelector(anchorSelector) : null;
    const oldAnchorOffset = oldAnchor ? oldAnchor.offsetTop - wrap.scrollTop : null;
    const wasLoadEarlier = _chPendingLoadEarlier;
    const shouldPinBottom = _chPendingPinBottom;
    _chPendingLoadEarlier = false;
    _chPendingPinBottom = false;
    // Build off-DOM into a fragment, then swap in one shot via replaceChildren.
    // Why: the previous `innerHTML = ''` + N appendChild pattern let the browser
    // paint a blank frame between clear and refill, which read as a flicker.
    const frag = document.createDocumentFragment();
    if (msgs.length >= chMsgLimit) {
      const more = document.createElement('button');
      more.className = 'ch-load-earlier';
      more.type = 'button';
      more.textContent = '加载更早的消息';
      more.onclick = () => {
        more.disabled = true;
        more.textContent = '加载中…';
        chMsgLimit += 160;
        _chPendingLoadEarlier = true;
        renderChatMessages(name);
      };
      frag.appendChild(more);
    }
    let lastSid = null;
    msgs.forEach((m, idx) => {
      if (m.sid) {
        if (lastSid && m.sid !== lastSid) {
          frag.appendChild(_chBuildSessionDivider(m));
        }
        lastSid = m.sid;
      }
      const blocks = _mergeProcessBlocks(m.blocks || []);
      const messageKey = _chMessageKey(m, idx);
      const revealState = m.role === 'assistant' ? _chRevealStates.get(messageKey) : null;
      let consumedTextChars = 0;
      const compaction = blocks.find(blk => blk.type === 'compaction');
      if (compaction) {
        const card = buildCompactionCard(compaction, m.ts);
        if (m.source_uuid) card.dataset.sourceUuid = String(m.source_uuid);
        frag.appendChild(card);
        return;
      }
      const bubble = document.createElement('div');
      bubble.className = 'ch-bubble ' + (m.role === 'user' ? 'user' : 'assistant');
      if (m.id != null) bubble.dataset.messageId = String(m.id);
      if (m.source_uuid) bubble.dataset.sourceUuid = String(m.source_uuid);
      blocks.forEach(blk => {
        if (blk.type === 'text') {
          let visibleText = blk.text || '';
          if (revealState && !revealState.done) {
            const blockStart = consumedTextChars + (consumedTextChars ? 2 : 0);
            const visibleChars = Math.max(0, Math.min(visibleText.length, revealState.shown - blockStart));
            visibleText = visibleText.slice(0, visibleChars);
            consumedTextChars = blockStart + (blk.text || '').length;
            if (!visibleText) return;
          }
          const t = document.createElement('div');
          t.className = 'ch-text';
          if (m.role === 'assistant') {
            t.innerHTML = chMdRender(visibleText);
            wireCodeBlocks(t);
          } else {
            t.innerHTML = chMdRenderUser(visibleText);
          }
          bubble.appendChild(t);
        } else if (blk.type === 'image') {
          if (blk.available === false) {
            const unavailable = document.createElement('div');
            unavailable.className = 'ch-missing-image';
            unavailable.textContent = '原图已清理';
            bubble.appendChild(unavailable);
          } else {
            const img = document.createElement('img');
            img.src = blk.src + (blk.src.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
            img.alt = blk.fname || '';
            img.loading = 'lazy';
            img.onclick = () => _showImagePreview({ available: true, serve_url: null, upload_session: (blk.src.match(/sessions\/([^/]+)\/uploads/) || [])[1], filename: blk.fname }, { session_id: chViewingUnified, session_name: document.getElementById('chDetailTitle').textContent, ts: m.ts, id: m.id });
            bubble.appendChild(img);
          }
        } else if (blk.type === 'tool_group') {
          bubble.appendChild(buildToolGroup(blk));
          (blk.tools || []).forEach(t => {
            if (t.file && t.file.path && t.done) {
              bubble.appendChild(_buildInlineFileCard(t.file));
            }
          });
        } else if (blk.type === 'thinking') {
          bubble.appendChild(buildThinkingBlock(blk));
        } else if (blk.type === 'process_group') {
          bubble.appendChild(buildProcessGroup(blk));
        }
      });
      const hasVisibleContent = blocks.some(b => b.type === 'text' || b.type === 'image');
      const messageTime = _messageTimestamp(m.ts);
      if (messageTime && hasVisibleContent) {
        const timestamp = document.createElement('div');
        timestamp.className = 'ch-bubble-time';
        timestamp.textContent = messageTime;
        bubble.appendChild(timestamp);
      }
      frag.appendChild(bubble);
      if (m.role === 'assistant') {
        const plainText = blocks.filter(b => b.type === 'text').map(b => b.text).join('\n\n').trim();
        if (plainText && (!revealState || revealState.done)) {
          const row = document.createElement('div');
          row.className = 'ch-msg-bottom';
          row.appendChild(buildAssistantActions(plainText, idx));
          frag.appendChild(row);
        }
      }
    });
    const tailPending = pendingMsgs
      .filter(pending => (pending.placement || 'queueTail') === 'terminalTail' || (pending.placement || 'queueTail') === 'queueTail')
      .sort((a, b) => (Number(a.createdAt) || 0) - (Number(b.createdAt) || 0));
    tailPending.forEach(pending => frag.appendChild(_chBuildPendingBubble(pending)));
    if (!chViewingArchive && !chViewingUnified && data.terminal_prompt) {
      frag.appendChild(_chBuildTerminalPrompt(data.terminal_prompt, name));
    }
    const disc = document.createElement('div');
    disc.className = 'ch-disclaimer';
    disc.innerHTML = `
      <svg class="ch-disclaimer-logo" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z"/></svg>
      <span>Claude is AI and can make mistakes.<br>Please double-check responses.</span>
    `;
    frag.appendChild(disc);
    wrap.replaceChildren(frag);
    let focusedSearchTarget = false;
    if (_chPendingFocusMessageId || _chPendingFocusSourceUuid) {
      let target = null;
      if (_chPendingFocusSourceUuid) target = wrap.querySelector('[data-source-uuid="' + CSS.escape(_chPendingFocusSourceUuid) + '"]');
      if (!target && _chPendingFocusMessageId) target = wrap.querySelector('[data-message-id="' + CSS.escape(_chPendingFocusMessageId) + '"]');
      if (target) {
        focusedSearchTarget = true;
        if (_chPendingOpenCompactionUuid && String(_chPendingOpenCompactionUuid) === String(target.dataset.sourceUuid || '')) {
          target.classList.add('open');
          _chPendingOpenCompactionUuid = null;
        }
        target.classList.add('search-target');
        target.scrollIntoView({ block: 'center', behavior: 'smooth' });
        setTimeout(() => target.classList.remove('search-target'), _CH_SEARCH_HIGHLIGHT_MS);
        _chPendingFocusMessageId = null;
        _chPendingFocusSourceUuid = null;
      }
    }
    // Scroll restore: three cases
    //   1. user was pinned to bottom → stay pinned (new messages auto-follow)
    //   2. user just clicked "load earlier" → keep their original top message
    //      at the same pixel position by shifting scrollTop by the new height
    //      that got inserted above
    //   3. polling re-render with content appended at the bottom → keep
    //      scrollTop unchanged so the message they're reading doesn't jump
    if (focusedSearchTarget) {
      // Search navigation owns the scroll position for this render.
    } else if (hasLiveSearchAnchor) {
      const currentAnchor = anchorSelector ? wrap.querySelector(anchorSelector) : null;
      wrap.scrollTop = currentAnchor && oldAnchorOffset !== null ? currentAnchor.offsetTop - oldAnchorOffset : oldScrollTop;
    } else if (shouldPinBottom || atBottom) {
      _chPinChatBottomSoon(wrap);
    } else if (wasLoadEarlier) {
      wrap.scrollTop = oldScrollTop + (wrap.scrollHeight - oldScrollHeight);
    } else {
      wrap.scrollTop = oldScrollTop;
    }
    // Images load async after replaceChildren and each one increases content
    // height as it decodes; without a re-pin, the viewport stays at a fixed
    // scrollTop while content grows underneath the bottom — looking exactly
    // like "the page keeps scrolling upward". Hook one-shot load listeners
    // that re-pin to bottom only if the user is still near it.
    wrap.querySelectorAll('img:not([data-pin-hooked])').forEach(img => {
      img.dataset.pinHooked = '1';
      if (img.complete) return;
      const repin = () => {
        const near = (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight) < 200;
        if (near) wrap.scrollTop = wrap.scrollHeight;
      };
      img.addEventListener('load', repin, { once: true });
      img.addEventListener('error', () => {}, { once: true });
    });
    updateChScrollBottomBtn();
  } catch (e) {
    if (!chViewingArchive && !chViewingUnified && chSub === 'detail' && activeChat === name) {
      _chScheduleChatRefresh(3000);
    }
  } finally {
    if (liveFetchKey && _chRenderInFlightKey === liveFetchKey) {
      _chRenderInFlightKey = null;
      if (_chRenderQueuedKey === liveFetchKey) {
        _chRenderQueuedKey = null;
        if (chSub === 'detail' && activeChat === name && !chViewingArchive && !chViewingUnified) {
          setTimeout(() => renderChatMessages(name), 0);
        }
      }
    }
  }
}

function chInputAutoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(120, el.scrollHeight) + 'px';
}
function chInputKey(e) {
  if (e.key !== 'Enter' || e.isComposing) return;
  // Mobile (touch, no hardware keyboard): Enter = newline, send only via button.
  // Desktop (≥721px): Enter = send, Alt/Shift+Enter = newline.
  const isDesktop = window.matchMedia('(min-width: 721px)').matches;
  if (isDesktop) {
    if (e.altKey || e.shiftKey) return;
    e.preventDefault();
    chSendMessage();
  } else if (e.metaKey || e.ctrlKey) {
    e.preventDefault();
    chSendMessage();
  }
}
// Chats reuses Code's pendingAtts / openImageEditor / sendInputBar pipeline so
// the upload, crop/draw editor, and send-with-Enter logic are identical.
function openChatAttach() {
  if (!activeChat) return;
  activeSession = activeChat;  // make Code's upload route at the right session
  if (typeof openAttach === 'function') openAttach();
}

// Desktop drag-and-drop upload for chat detail
(function initChatDragDrop() {
  let dragCounter = 0;
  const detail = () => document.getElementById('chatsDetail');

  document.addEventListener('dragenter', (e) => {
    const el = detail();
    if (!el || !activeChat) return;
    if (!el.contains(e.target) && e.target !== el) return;
    e.preventDefault();
    dragCounter++;
    if (dragCounter === 1) el.classList.add('drag-over');
  });

  document.addEventListener('dragleave', (e) => {
    const el = detail();
    if (!el) return;
    if (!el.contains(e.target) && e.target !== el) return;
    dragCounter--;
    if (dragCounter <= 0) { dragCounter = 0; el.classList.remove('drag-over'); }
  });

  document.addEventListener('dragover', (e) => {
    const el = detail();
    if (!el || !activeChat) return;
    if (el.contains(e.target) || e.target === el) e.preventDefault();
  });

  document.addEventListener('drop', (e) => {
    const el = detail();
    if (!el || !activeChat) return;
    if (!el.contains(e.target) && e.target !== el) return;
    e.preventDefault();
    dragCounter = 0;
    el.classList.remove('drag-over');
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    activeSession = activeChat;
    Array.from(files).forEach(f => uploadOneFile(f));
  });
})();

function openChatModelPicker() {
  // Bottom sheet showing both Model and Effort sections — toggles open/closed
  // when the input pill is tapped again. Items only swap the check; the sheet
  // stays open so users can compare or change effort right after model.
  if (!activeChat) return;
  const sheet = document.getElementById('chModelSheet');
  if (sheet.classList.contains('open')) { sheet.classList.remove('open'); return; }
  activeSession = activeChat;
  const kind = (typeof agentKindForSession === 'function') ? agentKindForSession(activeChat) : '';
  sheet.classList.toggle('codex-mode', kind === 'codex');
  if (typeof detectModelEffortFromBanner === 'function') {
    // Re-render once the banner detect lands, so the active check moves to the right row.
    detectModelEffortFromBanner().then(populateChModelSheet).catch(() => {});
  }
  populateChModelSheet();
  if (typeof refreshAgentCatalogForSession === 'function') {
    refreshAgentCatalogForSession(activeSession, populateChModelSheet).then(updateChatModelLabel).catch(() => {});
  }
  document.getElementById('chBsModelSection').classList.remove('collapsed');
  document.getElementById('chBsEffortSection').classList.add('collapsed');
  sheet.classList.add('open');
}
function closeChModelSheet() {
  const sheet = document.getElementById('chModelSheet');
  sheet.classList.remove('open');
  sheet.classList.remove('codex-mode');
}
function toggleChBsSection(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('collapsed');
}
let _chUsageTimer = null;
const _CH_USAGE_POLL_MS = 12000;

function _chFormatTokens(n) {
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    return (m >= 10 ? m.toFixed(1) : m.toFixed(2)).replace(/\.?0+$/, '') + 'M';
  }
  if (n >= 1000) return (Math.round(n / 100) / 10).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(n);
}

function _chPillCollapsedKey(name) { return 'chUsageCollapsed_' + (name || ''); }
function _chPillIsCollapsed(name) {
  try { return localStorage.getItem(_chPillCollapsedKey(name)) === '1'; } catch (e) { return false; }
}
function _chPillSetCollapsed(name, v) {
  try {
    if (v) localStorage.setItem(_chPillCollapsedKey(name), '1');
    else localStorage.removeItem(_chPillCollapsedKey(name));
  } catch (e) {}
}

function _chRenderUsage(data) {
  const pill = document.getElementById('chUsagePill');
  if (!pill) return;
  if (!data || !data.available) {
    pill.style.display = 'none';
    return;
  }
  pill.style.display = '';
  const pct = Math.min(100, Math.max(0, Number(data.pct) || 0));
  let state = 'ok';
  if (pct >= 85) state = 'alarm';
  else if (pct >= 60) state = 'warn';
  pill.dataset.state = state;
  pill.style.setProperty('--usage-pct', pct.toFixed(1) + '%');
  const txt = pill.querySelector('.ch-usage-text');
  const pctEl = pill.querySelector('.ch-usage-pct');
  if (txt) txt.textContent = _chFormatTokens(data.tokens) + ' / ' + _chFormatTokens(data.window);
  if (pctEl) pctEl.textContent = (pct < 10 ? pct.toFixed(1) : pct.toFixed(0)) + '%';
  pill.title = `${data.model || ''} · ${(data.tokens || 0).toLocaleString()} / ${(data.window || 0).toLocaleString()} tokens`;
  if (_chPillIsCollapsed(activeChat)) pill.classList.add('collapsed');
  else pill.classList.remove('collapsed');
}

function chPillToggleCollapse(name) {
  name = name || activeChat;
  if (!name) return;
  const pill = document.getElementById('chUsagePill');
  if (!pill) return;
  const collapsed = !_chPillIsCollapsed(name);
  _chPillSetCollapsed(name, collapsed);
  if (collapsed) pill.classList.add('collapsed');
  else pill.classList.remove('collapsed');
}

function chPillTextClick(name) {
  name = name || activeChat;
  if (!name) return;
  // 点文字 = 折叠/展开切换
  chPillToggleCollapse(name);
}

async function _chFetchUsage(name) {
  try {
    const r = await fetch(API + '/sessions/' + encodeURIComponent(name) + '/usage', {
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { return null; }
}

function _chStartUsagePolling(name) {
  _chStopUsagePolling();
  const tick = async () => {
    if (activeChat !== name) return;
    const data = await _chFetchUsage(name);
    if (activeChat === name) _chRenderUsage(data);
  };
  tick();
  _chUsageTimer = setInterval(tick, _CH_USAGE_POLL_MS);
}

function _chStopUsagePolling() {
  if (_chUsageTimer) { clearInterval(_chUsageTimer); _chUsageTimer = null; }
  const pill = document.getElementById('chUsagePill');
  if (pill) pill.style.display = 'none';
}

function updateChatModelLabel() {
  const el = document.getElementById('chModelLabel');
  if (!el || !activeChat) return;
  const kind = (typeof agentKindForSession === 'function') ? agentKindForSession(activeChat) : '';
  let label = 'Default';
  if (typeof modelLabelForSession === 'function') {
    label = modelLabelForSession(activeChat);
  } else {
    const slug = localStorage.getItem('cdModel:' + activeChat) || 'default';
    const mdl = (typeof CC_MODELS !== 'undefined' ? CC_MODELS : []).find(m => m.slug === slug);
    label = mdl ? mdl.name : 'Default';
  }
  if (kind === 'codex') label = label.replace(/^GPT-/i, '');
  el.textContent = label;

  const ef = document.getElementById('chEffortLabel');
  if (ef) {
    if (kind === 'codex') {
      const efforts = (typeof agentEffortsForSession === 'function')
        ? agentEffortsForSession(activeChat) : [];
      let slug = localStorage.getItem('cdEffort:' + activeChat) || '';
      if (!slug) {
        const modelSlug = localStorage.getItem('cdModel:' + activeChat) || 'default';
        const defaultModel = (typeof CODEX_MODELS !== 'undefined')
          ? CODEX_MODELS.find(m => m.slug !== 'default')?.slug : '';
        const resolved = modelSlug === 'default' ? defaultModel : modelSlug;
        slug = (typeof CODEX_MODEL_DEFAULT_EFFORT !== 'undefined' && resolved)
          ? (CODEX_MODEL_DEFAULT_EFFORT[resolved] || '') : '';
      }
      const item = efforts.find(e => e.slug === slug);
      ef.textContent = item ? item.name : '';
    } else {
      ef.textContent = '';
    }
  }
}
function populateChModelSheet() {
  const modelList = document.getElementById('chBsModelList');
  const effortList = document.getElementById('chBsEffortList');
  if (!modelList || !effortList) return;
  const models = typeof agentModelsForSession === 'function'
    ? agentModelsForSession(activeSession)
    : (typeof CC_MODELS !== 'undefined' ? CC_MODELS : []);
  const curModel = typeof storedModelSlugForSession === 'function'
    ? storedModelSlugForSession(activeSession)
    : (localStorage.getItem('cdModel:' + activeSession) || 'default');
  const efforts = typeof agentEffortsForSession === 'function'
    ? agentEffortsForSession(activeSession, curModel)
    : (typeof CC_EFFORTS !== 'undefined' ? CC_EFFORTS : []);
  // Use the account/session-specific default until this session has a detected or selected model.
  const curEffort = localStorage.getItem('cdEffort:' + activeSession) || '';
  updateChatModelLabel();
  const checkSvg = '<div class="ch-bs-check"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></div>';
  const swapActive = (list, row) => {
    list.querySelectorAll('.ch-bs-item.active').forEach(el => el.classList.remove('active'));
    row.classList.add('active');
  };
  modelList.innerHTML = '';
  for (const mdl of models) {
    const row = document.createElement('div');
    row.className = 'ch-bs-item' + (mdl.slug === curModel ? ' active' : '');
    row.innerHTML = checkSvg +
      '<div class="ch-bs-body"><div class="ch-bs-name">' + escapeHTML(mdl.name) + '</div>' +
      (mdl.desc ? '<div class="ch-bs-desc">' + escapeHTML(mdl.desc) + '</div>' : '') + '</div>';
    row.onclick = () => {
      swapActive(modelList, row);
      // Pre-update label optimistically so the pill reflects the choice immediately;
      // pickModel will set localStorage and we'll re-read on next open anyway.
      localStorage.setItem('cdModel:' + activeSession, mdl.slug);
      if (typeof syncEffortSelectionForModel === 'function') syncEffortSelectionForModel(activeSession, mdl.slug);
      updateChatModelLabel();
      populateChModelSheet();
      if (typeof pickModel === 'function') pickModel(mdl.slug);
    };
    modelList.appendChild(row);
  }
  effortList.innerHTML = '';
  for (const ef of efforts) {
    const row = document.createElement('div');
    row.className = 'ch-bs-item' + (ef.slug === curEffort ? ' active' : '');
    row.innerHTML = checkSvg +
      '<div class="ch-bs-body"><div class="ch-bs-name">' + escapeHTML(ef.name) + '</div>' +
      (ef.desc ? '<div class="ch-bs-desc">' + escapeHTML(ef.desc) + '</div>' : '') + '</div>';
    row.onclick = () => {
      swapActive(effortList, row);
      localStorage.setItem('cdEffort:' + activeSession, ef.slug);
      updateChatModelLabel();
      if (typeof pickEffort === 'function') pickEffort(ef.slug);
    };
    effortList.appendChild(row);
  }
}

// Renders Code's pendingAtts array into the Chat attach row (Code already
// renders into #cdAttachments; we mirror into #chAttachRow with the chat-style
// thumbnails). Called from a small hook patched onto renderAttachments below.
function renderChatAttachRowFromShared() {
  const row = document.getElementById('chAttachRow');
  if (!row) return;
  row.innerHTML = '';
  for (const a of pendingAtts) {
    const item = document.createElement('div');
    item.className = 'ch-attach-item' + (a.uploading ? ' uploading' : '') + (a.error ? ' error' : '');
    item.onclick = () => {
      if (a.uploading || a.error) return;
      if (a.isImage) openImageEditor(a);
      else openAttPreview(a);
    };
    if (a.isImage && a.blobUrl) {
      item.innerHTML = `<img src="${a.blobUrl}" alt=""><button class="ch-attach-x">×</button>`;
    } else {
      const safeName = escapeHTML(a.name);
      item.innerHTML = `
        <div class="file-fallback">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          <span class="fname">${safeName}</span>
        </div>
        <button class="ch-attach-x">×</button>`;
    }
    item.querySelector('.ch-attach-x').onclick = (e) => { e.stopPropagation(); removeAtt(a.id); };
    row.appendChild(item);
  }
}

async function chSendMessage() {
  if (!activeChat) return;
  const targetChat = activeChat;
  chFocusComposer();
  activeSession = targetChat;
  const chInput = document.getElementById('chInput');
  const stillUploading = pendingAtts.some(attachment => attachment.uploading);
  if (stillUploading) { alert('等图片上传完再发'); return; }
  const failedAttachments = pendingAtts.some(attachment => attachment.error || !attachment.path);
  if (failedAttachments) { alert('有图片上传失败，先删除或重新上传再发'); return; }
  const readyAttachments = pendingAtts.filter(attachment => attachment.path && !attachment.error).map(attachment => ({
    session: attachment.uploadSession || _chUploadSessionFromPath(attachment.path, targetChat),
    fname: attachment.path.split('/').pop() || attachment.name,
    isImage: Boolean(attachment.isImage),
  }));
  if (!chInput.value.trim() && !readyAttachments.length) return;
  const sendMode = _chCurrentSendMode();
  const pending = _chPendingAdd(targetChat, chInput.value, readyAttachments, { sendMode });
  _chShowPendingImmediately(pending);
  _sendInFlight = true;
  const send = document.getElementById('chSendBtn');
  if (send) send.disabled = true;
  const result = await _chSubmitPrivateMessage(pending);
  _sendInFlight = false;
  if (send) send.disabled = false;
  if (!result.ok) {
    _chPendingUpdate(pending.id, result.unconfirmed ? 'unconfirmed' : 'failed');
    _chShowPendingImmediately(pending);
    if (!result.unconfirmed) chInput.focus();
    return;
  }
  _chPendingUpdate(pending.id, result.direct ? 'direct' : (result.queued ? 'queued' : 'pending'));
  _chShowPendingImmediately(pending);
  _chSetDraft(targetChat, '');
  if (activeChat === targetChat) {
    chInput.value = '';
    chInputAutoGrow(chInput);
  }
  renderChatRows();
  if (typeof clearAttachments === 'function') clearAttachments();
  // Cascade a few quick polls. Bind them to the sent session so a group/private
  // view switch cannot leave this pending bubble without a receipt.
  _chInvalidateLiveChat(targetChat, { pinBottom: true });
  if (activeChat === targetChat) renderChatMessages(targetChat);
  _chScheduleReceiptPolls(targetChat);
}

window.addEventListener('pagehide', chPersistActiveDraft);
window.addEventListener('beforeunload', chPersistActiveDraft);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') chPersistActiveDraft();
});

// Switch buttons between Code <-> Chats detail views
let _chModelDetectAt = 0;
function chMaybeSyncModelLabel(name) {
  if (!name || chViewingArchive || chViewingUnified) return;
  const s = sessions.find(x => x.name === name);
  if (!s || (s.kind !== 'cc' && s.kind !== 'codex' && s.kind !== 'opencode')) return;
  if (typeof detectModelEffortFromBanner !== 'function') return;
  const now = Date.now();
  if (now - _chModelDetectAt < 5000) return;
  _chModelDetectAt = now;
  detectModelEffortFromBanner().then(updateChatModelLabel).catch(() => {});
}

function switchToChatFromCode() {
  if (!activeSession) return;
  // Only meaningful for cc/codex sessions
  const s = sessions.find(x => x.name === activeSession);
  if (!s || (s.kind !== 'cc' && s.kind !== 'codex' && s.kind !== 'opencode')) return;
  const name = activeSession;
  // Keep the hidden terminal's SSE connection alive while viewing Chat. That
  // lets debug output continue accumulating in xterm scrollback, so returning
  // to Code is instant and scrollable instead of resetting to one screen.
  if (typeof stopLoginPoll === 'function') stopLoginPoll();
  if (typeof stopRecoveryPoll === 'function') stopRecoveryPoll();
  if (typeof hideLoginBanner === 'function') hideLoginBanner(false);
  if (typeof hideRecoveryBanner === 'function') hideRecoveryBanner();
  switchView('chats');
  enterChatDetail(name);
}
function switchToCodeFromChat() {
  if (!activeChat) return;
  const name = activeChat;
  exitChatDetail();
  switchView('code', { deferSessionRefresh: true });
  const wrap = document.getElementById('codeTermWrap');
  const loading = !!(wrap && wrap.classList.contains('terminal-loading'));
  if (termSession === name && activeSession === name && codeSub === 'detail' && term && !loading) {
    renderDetailTabs();
    if (typeof safeFit === 'function') safeFit();
    setTimeout(() => {
      if (activeSession === name && codeSub === 'detail' && typeof syncRemoteSize === 'function') {
        syncRemoteSize().catch(() => {});
      }
    }, 0);
  } else {
    enterDetail(name);
  }
  setTimeout(() => {
    if (currentView === 'code' && activeSession === name && typeof loadSessions === 'function') {
      loadSessions();
    }
  }, 600);
}

// === Chat edit modal (chat_name + avatar) ===
function openChatEdit(s) {
  const m = document.getElementById('chatEditModal');
  document.getElementById('ce-name-target').value = s.name;
  document.getElementById('ce-chat-name').value = s.chat_name || '';
  document.getElementById('ce-name-hint').textContent = s.display_name ? `不填则用 Code 名 "${s.display_name}"` : `不填则用 "${s.name}"`;
  const avPreview = document.getElementById('ce-avatar-preview');
  if (s.has_avatar) avPreview.src = avatarUrl(s);
  else avPreview.removeAttribute('src');
  avPreview.classList.toggle('empty', !s.has_avatar);
  document.getElementById('ce-avatar-clear').style.display = s.has_avatar ? '' : 'none';
  document.getElementById('ce-err').textContent = '';
  m.classList.add('open');
}
function openChatEditFromDetail(evt) {
  const anchor = (evt && evt.currentTarget) || document.querySelector('.cd-actions-pill .cd-pill-btn:last-child');
  // Archive detail: route to the archive-specific action sheet (Rename + Delete only)
  if (chViewingArchive) {
    const meta = (chArchiveList || []).find(m => m.archive_id === chViewingArchive);
    if (!meta || !anchor) return;
    openArchiveActionSheet(meta, anchor);
    return;
  }
  if (!activeChat) return;
  const s = sessions.find(x => x.name === activeChat);
  if (!s) return;
  if (typeof openChatActionSheet === 'function' && anchor) {
    openChatActionSheet(s, anchor);
  } else {
    openChatEdit(s);
  }
}
function closeChatEditModal() {
  document.getElementById('chatEditModal').classList.remove('open');
}
async function submitChatEdit() {
  const name = document.getElementById('ce-name-target').value;
  const chatName = document.getElementById('ce-chat-name').value.trim();
  const errEl = document.getElementById('ce-err');
  errEl.textContent = '';
  try {
    const r = await fetch(API + '/sessions/' + encodeURIComponent(name) + '/chat-name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ chat_name: chatName }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      errEl.textContent = (d.detail || '保存失败');
      return;
    }
  } catch (e) { errEl.textContent = '网络错误'; return; }
  closeChatEditModal();
  await loadSessions();
}
