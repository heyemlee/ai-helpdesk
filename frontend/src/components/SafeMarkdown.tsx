import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** Evidence can quote untrusted source text. Never fetch embedded images. */
export function SafeMarkdown({ children }: { children: string }) {
  return <Markdown remarkPlugins={[remarkGfm]} components={{
    img: ({ alt }) => <span>{alt ? `[${alt}]` : '[image]'}</span>,
  }}>{children}</Markdown>
}
