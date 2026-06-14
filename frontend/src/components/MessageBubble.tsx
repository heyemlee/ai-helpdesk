import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Badge } from './ui'
import { agentLabel, confidenceTone, ms } from '@/lib/format'
import type { ChatMessage } from '@/hooks/useChatStream'

/** 单条消息。助手消息带一行路由元信息徽章，详情在右侧的路由检查器。 */
export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'
  const { meta, routing, result } = message

  if (isUser) {
    return (
      <article className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-sky-500/15 px-3.5 py-2
                        text-sm leading-relaxed text-slate-100 ring-1 ring-inset ring-sky-500/20">
          {message.content}
        </div>
      </article>
    )
  }

  const supporting = routing?.supporting_agents ?? result?.supporting_agents ?? []

  return (
    <article className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {meta?.intent_strategy === 'fast_path' && (
          <Badge tone="good" title="规则锚点命中，本轮意图识别没有发起任何 LLM 调用">
            规则通道
          </Badge>
        )}
        {routing?.primary_agent && (
          <Badge tone="info">主 {agentLabel(routing.primary_agent)}</Badge>
        )}
        {supporting.map((a) => (
          <Badge key={a} tone="muted">辅 {agentLabel(a)}</Badge>
        ))}
        {result?.handoff && <Badge tone="bad">转人工</Badge>}
        {result?.needs_clarification && <Badge tone="warn">澄清</Badge>}
        {result?.policy_violations && result.policy_violations.length > 0 && (
          <Badge tone="warn">策略拦截 {result.policy_violations.length}</Badge>
        )}
        {result?.answer_confidence !== undefined && (
          <Badge tone={confidenceTone(result.answer_confidence)}>
            置信 {result.answer_confidence.toFixed(2)}
          </Badge>
        )}
        {result?.latency_ms !== undefined && <Badge tone="muted">{ms(result.latency_ms)}</Badge>}
      </div>

      <div className="max-w-[92%] rounded-2xl rounded-bl-md bg-slate-800/60 px-3.5 py-2
                      text-sm leading-relaxed text-slate-100
                      ring-1 ring-inset ring-slate-700/50">
        {message.error ? (
          <span className="text-rose-300">{message.error}</span>
        ) : (
          <>
            {/* Agent 回复常带编号步骤和列表，按 Markdown 渲染而不是纯文本。
                react-markdown 默认不解析原始 HTML，模型输出的标签不会被执行。
                流式下每次增量都重渲染半截 Markdown——未闭合的语法先当普通文本，
                补全后自然变成格式，不需要额外处理。 */}
            <div className="prose-chat">
              <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown>
            </div>
            {message.streaming && (
              <span className="ml-0.5 inline-block h-4 w-[7px] translate-y-[2px]
                               animate-pulse bg-slate-300 align-baseline" />
            )}
          </>
        )}
      </div>
    </article>
  )
}
