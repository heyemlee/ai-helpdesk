/* 结果卡片上的交互：复制、表格排序翻页、反馈、折叠开合。

   问答页和历史页（回看本次会话缓存的结果）画的是同一张卡片，交互也该是同一套。
   调用方只需要说清两件事：点击落在哪个 turn 上、改完之后怎么重画它。 */

import { api } from './api.js';
import { copyText, delegate, toast } from './dom.js';
import { t } from './i18n.js';
import { summaryText, tableText } from './result.js';
import { save, state } from './state.js';

export const newTurnUi = () => ({
  open: {},
  tables: {},
  views: {},          // 每一步结果正在用哪种看法（数字 / 图表 / 表格）；没选过就用默认的
  feedback: { open: false, comment: '', sending: false, error: '' },
});

async function copied(text, successKey = 'toast.copied') {
  toast(t(await copyText(text) ? successKey : 'toast.copyFailed'));
}

function tableUi(turn, scope) {
  if (!turn.ui.tables[scope]) turn.ui.tables[scope] = { page: 1, sort: null };
  return turn.ui.tables[scope];
}

export function bindTurnActions(root, { turnOf, redraw }) {
  const withTurn = (handler) => (target, event) => {
    const turn = turnOf(target);
    if (turn) handler(turn, target, event);
  };

  root.addEventListener('input', (event) => {
    if (event.target.dataset.input !== 'feedback-comment') return;
    const turn = turnOf(event.target);
    if (turn) turn.ui.feedback.comment = event.target.value;
  });

  // <details> 的 toggle 不冒泡，用捕获阶段接；开合状态记到 turn 上，重画时照此还原
  root.addEventListener('toggle', (event) => {
    const detail = event.target;
    if (!detail.dataset || !detail.dataset.detail) return;
    const turn = turnOf(detail);
    if (!turn) return;
    const was = Boolean(turn.ui.open[detail.dataset.detail]);
    turn.ui.open[detail.dataset.detail] = detail.open;
    // 原始响应展开时才序列化那一大段 JSON
    if (detail.dataset.detail === 'raw' && detail.open !== was) redraw(turn);
  }, true);

  delegate(root, 'click', {
    'copy-question': withTurn((turn) => copied(turn.question)),
    'copy-summary': withTurn((turn) => { if (turn.result) copied(summaryText(turn)); }),
    'copy-table': withTurn((turn, target) => {
      if (turn.result) copied(tableText(turn, Number(target.dataset.step)), 'toast.tableCopied');
    }),
    'copy-sql': withTurn((turn, target) => {
      const step = turn.result && turn.result.steps.find((item) => item.index === Number(target.dataset.step));
      if (step) copied(target.dataset.which === 'original' ? step.sql : step.finalSql);
    }),
    'copy-trace': withTurn((turn) => { if (turn.result) copied(turn.result.traceId); }),

    'sort-table': withTurn((turn, target) => {
      const ui = tableUi(turn, target.dataset.scope);
      const key = target.dataset.key;
      // 点一下升序，再点降序，第三下回到服务端返回的原始顺序
      if (!ui.sort || ui.sort.key !== key) ui.sort = { key, direction: 'asc' };
      else if (ui.sort.direction === 'asc') ui.sort = { key, direction: 'desc' };
      else ui.sort = null;
      ui.page = 1;
      redraw(turn);
    }),
    'page-table': withTurn((turn, target) => {
      tableUi(turn, target.dataset.scope).page = Number(target.dataset.page) || 1;
      redraw(turn);
    }),
    'step-view': withTurn((turn, target) => {
      turn.ui.views[target.dataset.step] = target.dataset.which;
      redraw(turn);
    }),

    feedback: withTurn(async (turn, target) => {
      if (!turn.result) return;
      const ui = turn.ui.feedback;
      ui.sending = true;
      ui.error = '';
      redraw(turn);
      try {
        // 只发 trace_id 与判定：问题原文和 SQL 由服务端按审计记录回填，不由页面说了算
        await api.feedback(turn.result.traceId, target.dataset.verdict, ui.comment.trim());
        state.feedback[turn.result.traceId] = target.dataset.verdict;
        save();
      } catch (error) {
        ui.error = error.message || `HTTP ${error.status || 0}`;
      } finally {
        ui.sending = false;
        redraw(turn);
      }
    }),
    'feedback-reset': withTurn((turn) => {
      if (!turn.result) return;
      delete state.feedback[turn.result.traceId];
      save();
      redraw(turn);
    }),
    'feedback-comment': withTurn((turn) => {
      turn.ui.feedback.open = !turn.ui.feedback.open;
      redraw(turn);
      const area = root.querySelector(`[data-turn="${turn.id}"] [data-input="feedback-comment"]`);
      if (area) area.focus();
    }),
  });
}
