import { ExternalLink, PanelTopClose } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

interface DetachablePanelControls {
  detached: boolean
  action: ReactNode
  returnToMain: () => void
}

interface DetachablePanelProps {
  id: string
  title: string
  placeholderClassName?: string
  hostHidden?: boolean
  onDetachedChange?: (detached: boolean) => void
  children: (controls: DetachablePanelControls) => ReactNode
}

export function DetachablePanel({
  id,
  title,
  placeholderClassName = '',
  hostHidden = false,
  onDetachedChange,
  children,
}: DetachablePanelProps) {
  const popupRef = useRef<Window | null>(null)
  const mountRef = useRef<HTMLDivElement>(null)
  const [panelHost] = useState(() => {
    const host = document.createElement('div')
    host.className = 'popout-panel'
    return host
  })
  const [portalRoot, setPortalRoot] = useState<HTMLDivElement | null>(null)

  const moveHome = useCallback(() => {
    if (mountRef.current && panelHost.parentNode !== mountRef.current) {
      mountRef.current.appendChild(panelHost)
    }
  }, [panelHost])

  useLayoutEffect(() => {
    if (!portalRoot) moveHome()
  }, [moveHome, portalRoot])

  const attach = useCallback(() => {
    const popup = popupRef.current
    popupRef.current = null
    moveHome()
    setPortalRoot(null)
    onDetachedChange?.(false)
    if (popup && !popup.closed) popup.close()
  }, [moveHome, onDetachedChange])

  const detach = useCallback(() => {
    if (popupRef.current && !popupRef.current.closed) {
      popupRef.current.focus()
      return
    }

    const sourceDocument = mountRef.current?.ownerDocument ?? document
    const sourceWindow = sourceDocument.defaultView ?? window
    const popup = sourceWindow.open(
      '',
      `mms-${id}`,
      'popup=yes,width=1180,height=760,left=80,top=80,resizable=yes,scrollbars=no',
    )
    if (!popup) return

    popup.document.head.replaceChildren()
    const base = popup.document.createElement('base')
    base.href = sourceDocument.baseURI
    popup.document.head.appendChild(base)
    const popupTitle = popup.document.createElement('title')
    popupTitle.textContent = `MMS · ${title}`
    popup.document.head.appendChild(popupTitle)
    sourceDocument.head.querySelectorAll('link[rel="stylesheet"], style').forEach((node) => {
      popup.document.head.appendChild(node.cloneNode(true))
    })

    popup.document.body.replaceChildren()
    popup.document.body.className = 'popout-body'
    const root = popup.document.createElement('div')
    root.className = 'popout-root'
    popup.document.body.appendChild(root)
    root.appendChild(panelHost)

    const onClose = () => {
      if (popupRef.current !== popup) return
      popupRef.current = null
      moveHome()
      setPortalRoot(null)
      onDetachedChange?.(false)
    }
    popup.addEventListener('beforeunload', onClose, { once: true })
    popupRef.current = popup
    setPortalRoot(root)
    onDetachedChange?.(true)
    popup.focus()
  }, [id, moveHome, onDetachedChange, panelHost, title])

  useEffect(() => () => {
    const popup = popupRef.current
    popupRef.current = null
    if (popup && !popup.closed) popup.close()
  }, [])

  const attachedAction = (
    <button
      type="button"
      className="icon-button popout-toggle"
      onClick={detach}
      title={`${title}을(를) 새 창으로 분리`}
      aria-label={`${title} 새 창으로 분리`}
    >
      <ExternalLink size={15} />
    </button>
  )
  const detachedAction = (
    <button
      type="button"
      className="icon-button popout-toggle"
      onClick={attach}
      title="기본 화면으로 되돌리기"
      aria-label={`${title} 기본 화면으로 되돌리기`}
    >
      <PanelTopClose size={15} />
    </button>
  )

  return (
    <>
      <div
        ref={mountRef}
        className={`detachable-host ${placeholderClassName}`}
        hidden={hostHidden}
      >
        {portalRoot && (
          <section className="detached-placeholder" aria-label={`${title} 분리됨`}>
            <ExternalLink size={24} />
            <strong>{title}</strong>
            <span>듀얼 모니터 창에서 표시 중입니다.</span>
            <div>
              <button type="button" className="button secondary" onClick={() => popupRef.current?.focus()}>
                창 앞으로 가져오기
              </button>
              <button type="button" className="button ghost" onClick={attach}>
                기본 화면으로 복귀
              </button>
            </div>
          </section>
        )}
      </div>
      {createPortal(
        children({
          detached: Boolean(portalRoot),
          action: portalRoot ? detachedAction : attachedAction,
          returnToMain: attach,
        }),
        panelHost,
      )}
    </>
  )
}
