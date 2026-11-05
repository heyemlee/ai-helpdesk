/**
 * 等结果时的六段进度（移植自旧控制台 process.js 的 stageProgress）。
 *
 * 只看服务端真实发来的阶段事件。最后一条事件所在的那一步是"正在做"；排在它前面的是"过去了"——
 * 服务端按顺序一个阶段一个阶段地走，后面的阶段开始了，前面的就已经结束。前面某一步没收到事件不代表
 * 它被跳过：进度事件在队列满的时候会被丢掉（终态不会），而规划这一步服务端每次都会报（简单问题只是
 * 带一个 short_path 标记）。没有阶段事件（POST /query 这条路径）就画不出这条进度，调用方不该画它。
 */

export const STEP_ORDER = ['understand', 'retrieve', 'plan', 'generate', 'execute', 'check'] as const
export type LearnStep = (typeof STEP_ORDER)[number]

/** 契约 §4.5 的阶段 → 学习模式的六步 */
export const STAGE_TO_STEP: Record<string, LearnStep> = {
  normalization: 'understand', routing: 'understand', answering: 'understand',
  retrieval: 'retrieve', planning: 'plan', generation: 'generate',
  execution: 'execute', repair: 'execute', validation: 'check',
}

export type StepState = 'done' | 'now' | 'todo'

export function stageProgress(stages: ReadonlyArray<{ stage: string }> | null | undefined):
  Array<{ id: LearnStep; state: StepState }> {
  const steps = (stages ?? []).map((stage) => STAGE_TO_STEP[stage.stage]).filter((step): step is LearnStep => !!step)
  const current = steps.length ? STEP_ORDER.indexOf(steps[steps.length - 1]!) : -1
  return STEP_ORDER.map((id, index) => ({
    id,
    state: index === current ? 'now' : index < current ? 'done' : 'todo',
  }))
}
