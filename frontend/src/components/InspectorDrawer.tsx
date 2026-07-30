import { useEffect, useRef, type ReactNode } from 'react'
import { useT } from '@/i18n'
import { Icon } from './Icon'

/** Native modal focus containment on compact screens; an adjacent panel on desktop. */
export function InspectorDrawer({ open, onClose, children }: {
  open: boolean; onClose: () => void; children: ReactNode
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const { t } = useT()
  useEffect(() => {
    const dialog = ref.current!
    const media = window.matchMedia('(max-width: 1180px)')
    const sync = () => {
      if (dialog.open) dialog.close()
      if (open) {
        if (media.matches) dialog.showModal()
        else dialog.show()
      }
    }
    sync()
    media.addEventListener('change', sync)
    return () => media.removeEventListener('change', sync)
  }, [open])
  return (
    <dialog ref={ref} className="inspector" aria-label={t('in.title')}
      onCancel={(event) => { event.preventDefault(); onClose() }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return
        const rect = event.currentTarget.getBoundingClientRect()
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose()
      }}>
      <div className="inspector-head">
        <Icon name="panel" /><h2>{t('in.title')}</h2><span className="spacer" />
        <button className="icon-btn" aria-label={t('in.close')} onClick={onClose}><Icon name="close" /></button>
      </div>
      {children}
    </dialog>
  )
}
