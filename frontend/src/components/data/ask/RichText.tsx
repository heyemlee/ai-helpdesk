import { Fragment } from 'react'
import { parseRichText, type Inline } from '@/lib/sql/richtext'

/** 对话回答：只认加粗、行内代码、列表和标题；所有文字按文本渲染，模型写不进任何标记 */
function InlineText({ parts }: { parts: Inline[] }) {
  return (
    <>
      {parts.map((part, index) => (
        part.kind === 'strong' ? <strong key={index}>{part.text}</strong>
          : part.kind === 'code' ? <code key={index}>{part.text}</code>
            : <Fragment key={index}>{part.text}</Fragment>
      ))}
    </>
  )
}

export function RichText({ text }: { text: string }) {
  return (
    <div className="sq-rich-text">
      {parseRichText(text).map((block, index) => {
        if (block.kind === 'heading') return <p key={index}><strong><InlineText parts={block.content} /></strong></p>
        if (block.kind === 'list') {
          const items = block.items.map((item, at) => <li key={at}><InlineText parts={item} /></li>)
          return block.ordered ? <ol key={index}>{items}</ol> : <ul key={index}>{items}</ul>
        }
        return (
          <p key={index}>
            {block.lines.map((line, at) => <Fragment key={at}>{at > 0 && <br />}<InlineText parts={line} /></Fragment>)}
          </p>
        )
      })}
    </div>
  )
}
