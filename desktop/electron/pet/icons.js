'use strict';

// SVG icon set — replaces emoji glyphs so the UI renders identically on every
// machine and matches the octopus art style (no system-font emoji jitter).
//
// Each icon is a raw SVG string sized 1em via `width=1em height=1em` so it
// inherits font-size and color (fill=currentColor) — drop them inline anywhere
// a glyph used to sit, no extra CSS class needed for default layout.
//
// USAGE:
//   const html = icon('check')                 → inline SVG string
//   const out  = withIcons('✅ 已允许')          → emoji → SVG, text otherwise
//   setTextWithIcons(el, '💬 hello')           → safe text→innerHTML pipeline
//
// `withIcons` is XSS-safe IF the caller treats the OUTPUT as innerHTML AND the
// input text was previously escaped (it textContent-encodes the surrounding
// text via `escapeHtml`, only the icon name is trusted).

(function (root) {
  // 24x24 viewBox, outline+fill strokes mostly. Pure SVG, no fonts.
  // Picked to match the octopus art language: clean rounded lines, no gradients.
  const ICONS = {
    // ✅ 完成/允许
    check: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5 L10 17.5 L19 7"/></svg>',
    // 💬 对话/说
    chat: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6.5C4 5.4 4.9 4.5 6 4.5h12c1.1 0 2 .9 2 2v8c0 1.1-.9 2-2 2h-7l-4 3.5v-3.5H6c-1.1 0-2-.9-2-2v-8z"/></svg>',
    // ✋ 等待/请示
    hand: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 11V4.8a1.3 1.3 0 0 1 2.6 0V11"/><path d="M10.6 10.4V3.5a1.3 1.3 0 0 1 2.6 0V11"/><path d="M13.2 10.4V4.2a1.3 1.3 0 0 1 2.6 0V12"/><path d="M15.8 10V6.4a1.3 1.3 0 0 1 2.6 0v8.6c0 3.6-2.7 6-6 6-2.7 0-4.6-1.4-5.6-3.6L4.6 13.4a1.4 1.4 0 0 1 2.2-1.7L8 13.2"/></svg>',
    // 💤 睡眠
    zzz: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 7h6l-6 8h6"/><path d="M13 12h5l-5 6h5"/></svg>',
    // 📊 详情/图表
    chart: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20V4"/><path d="M4 20h16"/><path d="M8 18v-6"/><path d="M12 18V8"/><path d="M16 18v-4"/></svg>',
    // 🎭 形象/换皮肤（剧院面具）
    mask: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 6.5C3.5 4.6 5 3.5 7 3.5h10c2 0 3.5 1.1 3.5 3v5.2c0 4.6-3.8 8.3-8.5 8.3S3.5 16.3 3.5 11.7V6.5z"/><circle cx="9" cy="11" r="1.3" fill="currentColor"/><circle cx="15" cy="11" r="1.3" fill="currentColor"/><path d="M9.5 15c1 1 4 1 5 0"/></svg>',
    // 🧟 后台/僵尸进程（盒子里两只小眼）
    zombie: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="6" width="16" height="13" rx="2.5"/><circle cx="9" cy="12" r="1.4" fill="currentColor"/><circle cx="15" cy="12" r="1.4" fill="currentColor"/><path d="M9 16h6"/><path d="M2 9v4M22 9v4"/></svg>',
    // 📄 日志/文件
    doc: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h10c1.1 0 2-.9 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/></svg>',
    // 🔎 巡视/搜索
    search: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.8-4.8"/></svg>',
    // 掠夺/抓取（向内收拢的三条会话轨迹）
    grab: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h8M4 12h6M4 18h8"/><path d="m14 8 4 4-4 4"/><path d="M18 6v12"/></svg>',
    // 🔔 铃铛
    bell: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6.5 16.5V11a5.5 5.5 0 1 1 11 0v5.5l1.5 2h-14z"/><path d="M10 20a2 2 0 0 0 4 0"/></svg>',
    // 🔇 静音（铃铛 + 斜线）
    'bell-off': '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6.5 16.5V11a5.5 5.5 0 0 1 8.5-4.6"/><path d="M17.5 11v5.5l1.5 2h-13"/><path d="M10 20a2 2 0 0 0 4 0"/><path d="M3 3l18 18" stroke-width="2.3"/></svg>',
    // ⏻ 电源/退出
    power: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v9"/><path d="M6.5 7.5a8 8 0 1 0 11 0"/></svg>',
  };

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function icon(name) {
    return ICONS[name] || '';
  }

  // emoji code-point → icon name. Add to this map as we wire more icons in.
  const EMOJI_TO_ICON = {
    '✅': 'check',
    '💬': 'chat',
    '✋': 'hand',
    '💤': 'zzz',
    '📊': 'chart',
    '🎭': 'mask',
    '🧟': 'zombie',
    '📄': 'doc',
    '🔔': 'bell',
    '🔇': 'bell-off',
    '⏻': 'power',
  };

  const EMOJI_SRC = '(' + Object.keys(EMOJI_TO_ICON).map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')';
  const EMOJI_RE = new RegExp(EMOJI_SRC, 'g');       // for replace-all in withIcons
  const EMOJI_TEST_RE = new RegExp(EMOJI_SRC);       // no /g — .test() must be stateless

  // Take a plain string, escape it (HTML-safe), then swap known emoji for SVG.
  // Output is intended for innerHTML; surrounding user text is already escaped.
  function withIcons(text) {
    const escaped = escapeHtml(text);
    return escaped.replace(EMOJI_RE, (e) => '<span class="oi">' + (ICONS[EMOJI_TO_ICON[e]] || e) + '</span>');
  }

  function setTextWithIcons(el, text) {
    if (!el) return;
    el.innerHTML = withIcons(text == null ? '' : String(text));
  }

  // Detect: does this string contain any of our mapped emoji? If not, callers
  // can keep using textContent for speed — useful in hot paths.
  function hasMappedEmoji(text) {
    // Use the non-global regex: a /g regex's .test() advances lastIndex and
    // wouldn't reset, so repeated calls on the same string flip true/false/true…
    return EMOJI_TEST_RE.test(String(text == null ? '' : text));
  }

  root.OctoIcons = { icon, withIcons, setTextWithIcons, hasMappedEmoji, EMOJI_TO_ICON };
})(typeof window !== 'undefined' ? window : globalThis);
