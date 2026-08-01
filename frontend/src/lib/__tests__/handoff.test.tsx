/**
 * 转人工原因的对译。后端记的是固定句式的中文；英文界面逐条译、数字照抄，
 * 认不出的句式原样留着——这里不替后端解释它没说过的话。
 */
import { describe, expect, it } from 'vitest'
import { render } from '@/test/render'
import { useT } from '@/i18n'
import { handoffReason } from '../handoff'

function shown(reason: string | null | undefined, lang: 'en' | 'zh') {
  let text = ''
  function Probe() {
    const { t, lang: current } = useT()
    text = handoffReason(reason, t, current)
    return null
  }
  render(<Probe />, { lang })
  return text
}

describe('转人工原因', () => {
  it.each([
    ['Agent 自评置信度 0.65 低于 0.7',
     'The assistant’s own confidence in its answer was 0.65, below the required 0.7.'],
    ['Agent 自评置信度 0.40 低于 0.7，其中答案策略拦截 1 处无依据承诺',
     'The assistant’s own confidence in its answer was 0.40, below the required 0.7; 1 unsupported promise was removed from the reply.'],
    ['Agent 自评置信度 0.15 低于 0.7，其中答案策略拦截 3 处无依据承诺',
     'The assistant’s own confidence in its answer was 0.15, below the required 0.7; 3 unsupported promises were removed from the reply.'],
    ['意图置信度 0.31 低于 0.35', 'Intent confidence was 0.31, below the required 0.35.'],
    ['路由领域分 0.20 低于 0.3', 'Routing score was 0.20, below the required 0.3.'],
    ['意图为 human_handoff，用户明确要求人工', 'The customer explicitly asked for a person (intent: human_handoff).'],
    ['紧急度为 CRITICAL，直接转人工', 'Urgency was rated critical, so it went straight to a person.'],
    ['专家 Agent 与通用 Agent 均执行失败', 'Neither the specialist nor the general assistant could produce a reply.'],
    ['没有可用的 Agent 实例', 'No assistant was available to answer.'],
    ['Agent 流式执行失败', 'The assistant failed while writing the reply.'],
  ])('%s', (recorded, english) => {
    expect(shown(recorded, 'en')).toBe(english)
    expect(shown(recorded, 'zh')).toBe(recorded)        // 中文界面一个字不动
  })

  it('多条原因逐条译；认不出的那条原样留着', () => {
    expect(shown('意图置信度 0.31 低于 0.35；路由领域分 0.20 低于 0.3；某条以后新增的原因', 'en')).toBe(
      'Intent confidence was 0.31, below the required 0.35. Routing score was 0.20, below the required 0.3. 某条以后新增的原因')
  })

  it('没有原因就是空，不补一句', () => {
    expect(shown('', 'en')).toBe('')
    expect(shown(undefined, 'en')).toBe('')
    expect(shown(null, 'zh')).toBe('')
  })
})
