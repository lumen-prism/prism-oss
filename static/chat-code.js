// chat-code.js — Code-block interactions for chat messages
// Split out of app.html on 2026-05-22.
// Exposes globals: wireCodeBlocks, openCodeOverlay, closeCodeOverlay, copyCodeFromOverlay
// Depends on: window.hljs (loaded from vendor/hljs/highlight.min.js)
// Used by: app.html (chat tab) — wires up copy/expand buttons after chMdRender

// Robust copy: clipboard API first, hidden-textarea execCommand fallback.
// Returns true only if the text actually landed on the clipboard.
async function chCopyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {}
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
    ta.setAttribute('readonly', '');
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

function wireCodeBlocks(root) {
  root.querySelectorAll('.ch-code').forEach(block => {
    const code = block.getAttribute('data-code') || '';
    const lang = block.getAttribute('data-lang') || 'Code';
    const decoded = (() => {
      const d = document.createElement('textarea');
      d.innerHTML = code;
      return d.value;
    })();
    const copyBtn = block.querySelector('.ch-code-copy');
    const expandBtn = block.querySelector('.ch-code-expand');
    if (copyBtn) copyBtn.onclick = async (e) => {
      e.stopPropagation();
      const ok = await chCopyText(decoded);
      copyBtn.classList.add('copied');
      copyBtn.innerHTML = ok
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>';
      setTimeout(() => {
        copyBtn.classList.remove('copied');
        copyBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
      }, 1400);
    };
    if (expandBtn) expandBtn.onclick = (e) => {
      e.stopPropagation();
      openCodeOverlay(lang, decoded);
    };
    // syntax highlight
    const codeEl = block.querySelector('pre code');
    if (codeEl && window.hljs && !codeEl.dataset.hl) {
      codeEl.dataset.hl = '1';
      const norm = (lang || '').toLowerCase().trim();
      const alias = { js: 'javascript', ts: 'typescript', py: 'python',
                      sh: 'bash', shell: 'bash', zsh: 'bash',
                      html: 'xml', yml: 'yaml' };
      const target = alias[norm] || norm;
      try {
        if (target && window.hljs.getLanguage(target)) {
          const r = window.hljs.highlight(codeEl.textContent, { language: target, ignoreIllegals: true });
          codeEl.innerHTML = r.value;
          codeEl.classList.add('hljs');
        } else {
          const r = window.hljs.highlightAuto(codeEl.textContent);
          codeEl.innerHTML = r.value;
          codeEl.classList.add('hljs');
        }
      } catch {}
    }
  });
}

function openCodeOverlay(lang, code) {
  const overlay = document.getElementById('chCodeOverlay');
  if (!overlay) return;
  overlay.querySelector('.lang').textContent = lang || 'Code';
  const pre = overlay.querySelector('pre');
  pre.textContent = code;
  // wrap in <code> + highlight
  let codeEl = pre.querySelector('code');
  if (!codeEl) {
    codeEl = document.createElement('code');
    codeEl.textContent = code;
    pre.textContent = '';
    pre.appendChild(codeEl);
  }
  if (window.hljs) {
    const norm = (lang || '').toLowerCase().trim();
    const alias = { js: 'javascript', ts: 'typescript', py: 'python',
                    sh: 'bash', shell: 'bash', zsh: 'bash',
                    html: 'xml', yml: 'yaml' };
    const target = alias[norm] || norm;
    try {
      if (target && window.hljs.getLanguage(target)) {
        const r = window.hljs.highlight(code, { language: target, ignoreIllegals: true });
        codeEl.innerHTML = r.value;
      } else {
        const r = window.hljs.highlightAuto(code);
        codeEl.innerHTML = r.value;
      }
      codeEl.classList.add('hljs');
    } catch {}
  }
  overlay.classList.add('open');
}
function closeCodeOverlay() {
  document.getElementById('chCodeOverlay')?.classList.remove('open');
}
async function copyCodeFromOverlay() {
  const overlay = document.getElementById('chCodeOverlay');
  const code = overlay?.querySelector('pre code')?.textContent
            ?? overlay?.querySelector('pre')?.textContent ?? '';
  await chCopyText(code);
  const btn = document.getElementById('chCodeOverlayCopy');
  if (btn) {
    btn.classList.add('copied');
    setTimeout(() => btn.classList.remove('copied'), 1400);
  }
}

