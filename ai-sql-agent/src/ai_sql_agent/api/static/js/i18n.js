/* 界面文案。字典在 i18n/en.js 与 i18n/zh.js，一行一个键。

   翻译的只有界面自己的话。业务数据（库名、表名、单元格、SQL、后端返回的
   answer / reason / 报错原文）一律原样显示——那是数据，替它换一种语言等于改数据。 */

import en from './i18n/en.js';
import zh from './i18n/zh.js';
import { state } from './state.js';

const DICTIONARIES = { en, zh };

export function t(key, vars) {
  const dictionary = DICTIONARIES[state.lang] || en;
  let text = key in dictionary ? dictionary[key] : en[key];
  if (text == null) return key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.split('{' + name + '}').join(String(value));
    }
  }
  return text;
}

/* 带数量的文案。英文有单复数：数量为 1 且字典里有 `<key>.one` 时用它。 */
export function tn(key, n, vars = {}) {
  const dictionary = DICTIONARIES[state.lang] || en;
  const single = key + '.one';
  return t(n === 1 && single in dictionary ? single : key, { n, ...vars });
}

export const has = (key) => key in en;

/* 两种语言各取一个：{en, zh} 这种由服务端或语义层给出的成对文案。 */
export function pick(pair) {
  if (!pair || typeof pair !== 'object') return '';
  return pair[state.lang] || pair.en || pair.zh || '';
}

export function applyStaticText(root = document) {
  document.documentElement.lang = state.lang === 'zh' ? 'zh-CN' : 'en';
  root.querySelectorAll('[data-i18n]').forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  root.querySelectorAll('[data-i18n-label]').forEach((element) => {
    const text = t(element.dataset.i18nLabel);
    element.setAttribute('aria-label', text);
    element.title = text;
  });
  root.querySelectorAll('[data-i18n-placeholder]').forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
}
