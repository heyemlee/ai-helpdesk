/* 对话回答的排版。

   对话路径的回答是模型写的一段文字，里面常带 Markdown 记号（**加粗**、`代码`、列表）。
   原样显示满屏星号，直接当 HTML 插入又等于让模型往页面里写标记。
   这里只认最常见的几种记号，其余一律当普通文字；所有文字都经 html`` 转义，
   不解析链接、图片和原始 HTML——这段文字是解释，不需要能点的东西。 */

import { html } from './dom.js';

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g;

function inline(text) {
  return text.split(INLINE).filter((part) => part !== '').map((part) => {
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) {
      return html`<strong>${part.slice(2, -2)}</strong>`;
    }
    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
      return html`<code>${part.slice(1, -1)}</code>`;
    }
    return part;
  });
}

export function renderRichText(text) {
  const blocks = [];
  let paragraph = [];
  let list = null;              // { ordered, items }

  const endParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(html`<p>${paragraph.map((line, index) => html`${index ? html`<br>` : ''}${inline(line)}`)}</p>`);
    paragraph = [];
  };
  const endList = () => {
    if (!list) return;
    const items = list.items.map((item) => html`<li>${inline(item)}</li>`);
    blocks.push(list.ordered ? html`<ol class="numbered">${items}</ol>` : html`<ul class="bullets">${items}</ul>`);
    list = null;
  };

  String(text || '').split(/\r?\n/).forEach((raw) => {
    const line = raw.trim();
    if (!line) { endParagraph(); endList(); return; }

    const bullet = /^[-*•]\s+(.*)$/.exec(line);
    const numbered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      endParagraph();
      const ordered = Boolean(numbered);
      if (list && list.ordered !== ordered) endList();
      if (!list) list = { ordered, items: [] };
      list.items.push((bullet || numbered)[1]);
      return;
    }

    endList();
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) {
      endParagraph();
      blocks.push(html`<p><strong>${inline(heading[1])}</strong></p>`);
      return;
    }
    paragraph.push(line);
  });
  endParagraph();
  endList();

  return html`<div class="rich-text">${blocks}</div>`;
}
