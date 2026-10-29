// UI language. It is read once when the page loads, and changing it reloads
// the page so the label tables built at module load pick up the new language.
const KEY='omm.web.language.v1';
export const languages=[['ko','한국어'],['en','English']];

export function detectLanguage(stored, browser) {
  if (stored==='ko' || stored==='en') return stored;
  return String(browser || 'ko').toLowerCase().startsWith('ko') ? 'ko' : 'en';
}

function initial() {
  // Outside a browser (node tests) keep Korean, the original UI language.
  if (typeof document==='undefined') return 'ko';
  let stored=null;
  try { stored=localStorage.getItem(KEY); } catch {}
  return detectLanguage(stored, navigator.languages?.[0] || navigator.language);
}

export const lang=initial();

// L('한국어 문장', 'English sentence') picks the text for the current language.
export const L=(ko, en)=>lang==='en' ? en : ko;

export function setLanguage(value) {
  if (value===lang) return;
  try { localStorage.setItem(KEY, value); } catch {}
  location.reload();
}
