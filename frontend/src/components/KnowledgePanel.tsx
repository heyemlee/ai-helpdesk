import { useRef, useState } from 'react'
import { useAddKnowledge, useKnowledgeStats, useSearch, useUploadKnowledge } from '@/api/hooks'
import { Badge, EmptyHint, ErrorNote, Panel } from './ui'
import { num } from '@/lib/format'

export function KnowledgePanel({ base }: { base: string }) {
  const [query, setQuery] = useState('退款多久能到账')
  const [title, setTitle] = useState('退款补充政策')
  const [content, setContent] = useState('大促期间退款审核时间可能延长到 3-5 个工作日。')
  const fileRef = useRef<HTMLInputElement>(null)

  const stats = useKnowledgeStats(base)
  const search = useSearch(base)
  const add = useAddKnowledge(base)
  const upload = useUploadKnowledge(base)

  return (
    <Panel
      title="知识库"
      badge={<Badge tone="info">RAG</Badge>}
      actions={<Badge tone="muted">{stats.data?.total_chunks ?? '-'} 片段</Badge>}
    >
      <div className="space-y-3">
        <div className="flex gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && search.mutate({ query })}
            placeholder="检索知识库"
            className="flex-1 rounded-lg border border-slate-700 bg-slate-950/60 px-2.5 py-1.5
                       text-xs text-slate-100 outline-none placeholder:text-slate-600
                       focus:border-sky-500/50"
          />
          <button
            onClick={() => search.mutate({ query })}
            disabled={search.isPending || !query.trim()}
            className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs text-slate-200 transition
                       hover:bg-slate-700 disabled:opacity-40"
          >
            {search.isPending ? '…' : '检索'}
          </button>
        </div>

        <ErrorNote error={search.error} />

        {search.data && (
          <div className="space-y-1.5">
            {search.data.reranked && <Badge tone="info">已重排</Badge>}
            {search.data.results.length === 0 && <EmptyHint>没有命中结果</EmptyHint>}
            {search.data.results.map((item, i) => (
              <article key={i} className="rounded-lg bg-slate-950/50 px-2.5 py-2">
                <div className="flex items-baseline justify-between gap-2">
                  <strong className="truncate text-xs text-slate-200">{item.title || '未命名'}</strong>
                  <span className="shrink-0 font-mono text-[10px] text-slate-500">
                    {item.score !== undefined ? num(item.score, 3) : '-'}
                  </span>
                </div>
                <p className="mt-1 line-clamp-3 text-[11px] leading-relaxed text-slate-400">
                  {item.content}
                </p>
              </article>
            ))}
          </div>
        )}

        <details className="text-xs">
          <summary className="cursor-pointer text-slate-500 hover:text-slate-300">导入文档</summary>
          <div className="mt-2 space-y-2">
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="标题"
              className="w-full rounded-lg border border-slate-700 bg-slate-950/60 px-2.5 py-1.5
                         text-xs text-slate-100 outline-none focus:border-sky-500/50"
            />
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={3}
              placeholder="内容"
              className="w-full resize-none rounded-lg border border-slate-700 bg-slate-950/60 px-2.5 py-1.5
                         text-xs text-slate-100 outline-none focus:border-sky-500/50"
            />
            <div className="flex gap-2">
              <button
                onClick={() => add.mutate([{ title: title.trim(), content: content.trim() }])}
                disabled={add.isPending || !title.trim() || !content.trim()}
                className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs text-slate-200 transition
                           hover:bg-slate-700 disabled:opacity-40"
              >
                添加
              </button>
              <button
                onClick={() => fileRef.current?.click()}
                disabled={upload.isPending}
                className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs text-slate-200 transition
                           hover:bg-slate-700 disabled:opacity-40"
              >
                上传文件
              </button>
              <input
                ref={fileRef}
                type="file"
                accept=".txt,.md,.json"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  e.target.value = ''
                  if (file) upload.mutate(file)
                }}
              />
            </div>
            <ErrorNote error={add.error ?? upload.error} />
            {(add.data ?? upload.data) && (
              <p className="text-[11px] text-emerald-300">
                {(add.data ?? upload.data)?.message}
              </p>
            )}
          </div>
        </details>
      </div>
    </Panel>
  )
}
