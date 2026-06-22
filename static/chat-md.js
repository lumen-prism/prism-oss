// chat-md.js — Markdown renderer for chat messages
// Split out of app.html on 2026-05-22.
// Exposes globals: chMdEscape, chMdRenderUser, chMdInl, chMdCodeBlock, chMdTable, chMdRender
// Used by: app.html (chat tab), chat-archive.html, memory.html (future)

// === Markdown renderer for assistant messages (adapted from files.html) ===
function chMdEscape(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
const CH_BARE_LINK_RE = /(^|[\s(\[【「])((?:https?:\/\/|www\.)[^\s<)\]】」]+|(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|app|ai|cn|co|me|tech|xyz)(?:\/[^\s<)\]】」]*)?)/gi;
function chMdLinkify(text) {
  const parts = text.split(/(<a [^>]*>[\s\S]*?<\/a>|<img [^>]*>)/g);
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 !== 0) continue;
    parts[i] = parts[i].replace(CH_BARE_LINK_RE, (match, pre, url) => {
      const trailing = (url.match(/[.,;:!?，。]+$/) || [''])[0];
      const cleanUrl = trailing ? url.slice(0, -trailing.length) : url;
      const href = /^(?:https?:\/\/)/i.test(cleanUrl) ? cleanUrl : 'https://' + cleanUrl;
      return pre + '<a href="' + href + '" target="_blank" rel="noopener">' + cleanUrl + '</a>' + trailing;
    });
  }
  return parts.join('');
}
// Minimal user-side render — only: links, bare URLs, italic, strikethrough, escapes.
// No bold (** would also match common emphasis), no inline code (false matches on `).
function chMdRenderUser(src) {
  if (!src) return '';
  let t = chMdEscape(src);
  // explicit [text](url) link
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // <url> autolink
  t = t.replace(/&lt;((?:https?|ftp):\/\/[^\s&]+)&gt;/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  t = chMdLinkify(t);
  // bold: **x** and __x__
  t = t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/__(.+?)__/g, '<strong>$1</strong>');
  // italic: *x* or _x_
  t = t.replace(/(^|[^*])\*([^*\n]+?)\*([^*]|$)/g, '$1<em>$2</em>$3');
  t = t.replace(/(^|[\s(])_([^_\n]+?)_([\s.,!?)]|$)/g, '$1<em>$2</em>$3');
  // strikethrough
  t = t.replace(/~~(.+?)~~/g, '<del>$1</del>');
  // preserve newlines
  return t.replace(/\n/g, '<br>');
}
function chMdInl(t) {
  // -- inline code: pull out first so its contents aren't touched by other rules --
  const CODE_MAP = {};
  let codeCounter = 0;
  // double-backtick first — allows internal single backticks: `` ` ``
  t = t.replace(/``\s?((?:(?!``)[\s\S])+?)\s?``/g, (_, inner) => {
    const key = '\u0002CODE' + (codeCounter++) + '\u0002';
    CODE_MAP[key] = '<code>' + inner + '</code>';
    return key;
  });
  // single-backtick: standard inline code (pulled out so internal *_~ don't get eaten)
  t = t.replace(/`([^`\n]+)`/g, (_, inner) => {
    const key = '\u0002CODE' + (codeCounter++) + '\u0002';
    CODE_MAP[key] = '<code>' + inner + '</code>';
    return key;
  });
  // -- escape sequences: replace \X with placeholder, restore at end --
  // covers: \\ \* \_ \` \[ \] \( \) \# \> \| \~ \!
  const ESC_MAP = {};
  let escCounter = 0;
  t = t.replace(/\\([\\*_`\[\]()#>|~!])/g, (_, ch) => {
    const key = '\u0001ESC' + (escCounter++) + '\u0001';
    ESC_MAP[key] = ch;
    return key;
  });
  // images / links first (they contain () which would get eaten by other regex)
  t = t.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img src="$2" alt="$1">');
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // autolink: <https://...> form
  t = t.replace(/&lt;((?:https?|ftp):\/\/[^\s&]+)&gt;/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  t = chMdLinkify(t);
  t = t.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  t = t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/__(.+?)__/g, '<strong>$1</strong>');
  t = t.replace(/(^|[^*])\*([^*\n]+?)\*([^*]|$)/g, '$1<em>$2</em>$3');
  t = t.replace(/(^|[\s(])_([^_\n]+?)_([\s.,!?)]|$)/g, '$1<em>$2</em>$3');
  t = t.replace(/~~(.+?)~~/g, '<del>$1</del>');
  // restore inline code (must be before escape restore, since the inner can contain anything)
  t = t.replace(/\u0002CODE\d+\u0002/g, k => CODE_MAP[k] !== undefined ? CODE_MAP[k] : k);
  // restore escaped chars (after all replacements done)
  t = t.replace(/\u0001ESC\d+\u0001/g, k => ESC_MAP[k] !== undefined ? ESC_MAP[k] : k);
  return t;
}
function chMdCodeBlock(lang, code) {
  const esc = chMdEscape(code);
  const language = chMdEscape((lang || 'Code').trim() || 'Code');
  // data-code is the raw text used for copy/expand. We escape its attribute value
  // via the same chMdEscape (sufficient for double-quoted attrs since &<> are escaped).
  const rawAttr = chMdEscape(code).replace(/"/g, '&quot;');
  return `<div class="ch-code" data-code="${rawAttr}" data-lang="${language}">
    <div class="ch-code-head">
      <span class="ch-code-lang">${language}</span>
      <button class="ch-code-btn ch-code-copy" title="复制" aria-label="复制">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
      </button>
      <button class="ch-code-btn ch-code-expand" title="展开" aria-label="展开">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
      </button>
    </div>
    <pre><code>${esc}</code></pre>
  </div>`;
}

function chMdTable(tableLines) {
  if (tableLines.length < 2) return '';
  const parseRow = l => l.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
  const headers = parseRow(tableLines[0]);
  let out = '<table><thead><tr>';
  headers.forEach(c => { out += '<th>' + chMdInl(chMdEscape(c)) + '</th>'; });
  out += '</tr></thead><tbody>';
  // Skip the separator row (index 1) — already inferred from headers
  for (let i = 2; i < tableLines.length; i++) {
    const cells = parseRow(tableLines[i]);
    out += '<tr>';
    cells.forEach(c => { out += '<td>' + chMdInl(chMdEscape(c)) + '</td>'; });
    out += '</tr>';
  }
  return out + '</tbody></table>';
}

function chMdRender(src) {
  if (!src) return '';
  const lines = src.split('\n');
  let out = '', inCode = false, codeBuf = [], codeLang = '';
  // listStack: each entry is 'ul' or 'ol'; current nesting depth = listStack.length - 1
  let listStack = [];
  const closeAllLists = () => {
    while (listStack.length) {
      out += listStack.pop() === 'ol' ? '</ol>' : '</ul>';
    }
  };
  // 2 spaces (or 1 tab) per indent level
  const indentDepth = (raw) => {
    const m = raw.match(/^([ \t]*)/);
    let n = 0;
    for (const c of m[1]) n += (c === '\t') ? 2 : 1;
    return Math.floor(n / 2);
  };
  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];
    const fence = line.trimStart().match(/^```(.*)$/);
    if (fence) {
      if (!inCode) { closeAllLists(); inCode = true; codeBuf = []; codeLang = fence[1].trim(); }
      else { out += chMdCodeBlock(codeLang, codeBuf.join('\n')); inCode = false; codeLang = ''; }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }
    if (line.trim() === '') { closeAllLists(); continue; }
    const hm = line.match(/^(#{1,6})\s+(.*)$/);
    if (hm) { closeAllLists(); out += '<h' + hm[1].length + '>' + chMdInl(chMdEscape(hm[2])) + '</h' + hm[1].length + '>'; continue; }
    if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(line.trim())) { closeAllLists(); out += '<hr>'; continue; }
    if (line.trimStart().startsWith('> ')) {
      closeAllLists();
      let bq = line.replace(/^\s*>\s?/, '');
      while (i+1 < lines.length && lines[i+1].trimStart().startsWith('> ')) { i++; bq += '\n' + lines[i].replace(/^\s*>\s?/, ''); }
      out += '<blockquote>' + bq.split('\n').map(l => '<p>' + chMdInl(chMdEscape(l)) + '</p>').join('') + '</blockquote>';
      continue;
    }
    // Table: starts with | and the NEXT line is a separator like |---|---|
    if (line.trim().startsWith('|') && i+1 < lines.length
        && /^\s*\|?[\s-:|]+\|[\s-:|]*$/.test(lines[i+1])
        && lines[i+1].includes('-')) {
      closeAllLists();
      const tableLines = [line, lines[i+1]];
      i += 1;
      while (i+1 < lines.length && lines[i+1].trim().startsWith('|')) { i++; tableLines.push(lines[i]); }
      out += chMdTable(tableLines);
      continue;
    }
    // List item — unordered or ordered, with nesting + task-checkbox support
    let listMatch = line.match(/^(\s*)[-*+]\s+(.*)$/);
    let listType = listMatch ? 'ul' : null;
    if (!listMatch) {
      listMatch = line.match(/^(\s*)\d+\.\s+(.*)$/);
      if (listMatch) listType = 'ol';
    }
    if (listMatch) {
      const depth = indentDepth(line);
      // Going up — close lists deeper than target
      while (listStack.length > depth + 1) {
        out += listStack.pop() === 'ol' ? '</ol>' : '</ul>';
      }
      // At target depth but type changed (ul <-> ol) — swap list
      if (listStack.length === depth + 1 && listStack[depth] !== listType) {
        out += listStack.pop() === 'ol' ? '</ol>' : '</ul>';
        listStack.push(listType);
        out += listType === 'ol' ? '<ol>' : '<ul>';
      }
      // Going down — open lists until at target depth
      while (listStack.length < depth + 1) {
        listStack.push(listType);
        out += listType === 'ol' ? '<ol>' : '<ul>';
      }
      // Task-list checkbox: [ ] or [x] right after the bullet
      const content = listMatch[2];
      const taskM = content.match(/^\[([ xX])\]\s+(.*)$/);
      let liInner, liClass = '';
      if (taskM) {
        const done = taskM[1].toLowerCase() === 'x';
        liClass = ' class="ch-task-li"';
        liInner = '<span class="ch-task' + (done ? ' done' : '') + '"></span>' + chMdInl(chMdEscape(taskM[2]));
      } else {
        liInner = chMdInl(chMdEscape(content));
      }
      out += '<li' + liClass + '>' + liInner + '</li>';
      continue;
    }
    closeAllLists();
    let para = line;
    while (i+1 < lines.length && lines[i+1].trim() !== ''
           && !lines[i+1].match(/^#{1,6}\s/) && !lines[i+1].trimStart().startsWith('```')
           && !lines[i+1].trimStart().startsWith('> ')
           && !lines[i+1].match(/^(\s*)[-*+]\s+/) && !lines[i+1].match(/^(\s*)\d+\.\s+/)
           && !/^(\*{3,}|-{3,}|_{3,})\s*$/.test(lines[i+1].trim())
           && !lines[i+1].trim().startsWith('|')) {
      i++; para += '\n' + lines[i];
    }
    const html = chMdInl(chMdEscape(para)).replace(/\n/g, '<br>');
    out += '<p>' + html + '</p>';
  }
  closeAllLists();
  if (inCode) out += chMdCodeBlock(codeLang, codeBuf.join('\n'));
  return out;
}
