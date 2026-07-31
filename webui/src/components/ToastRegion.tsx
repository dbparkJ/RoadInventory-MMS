import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'
import { useEffect } from 'react'

export interface Toast {
  id: string
  tone: 'info' | 'success' | 'error'
  title: string
  message?: string
}

export function ToastRegion({
  toasts,
  dismiss,
}: {
  toasts: Toast[]
  dismiss: (id: string) => void
}) {
  return (
    <div className="toast-region" aria-live="polite">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} dismiss={dismiss} />
      ))}
    </div>
  )
}

function ToastItem({ toast, dismiss }: { toast: Toast; dismiss: (id: string) => void }) {
  useEffect(() => {
    const timer = window.setTimeout(() => dismiss(toast.id), toast.tone === 'error' ? 7000 : 4500)
    return () => window.clearTimeout(timer)
  }, [dismiss, toast])

  const Icon = toast.tone === 'success' ? CheckCircle2 : toast.tone === 'error' ? AlertCircle : Info
  return (
    <div className={`toast toast-${toast.tone}`}>
      <Icon size={18} />
      <div>
        <strong>{toast.title}</strong>
        {toast.message && <p>{toast.message}</p>}
      </div>
      <button type="button" className="icon-button bare" onClick={() => dismiss(toast.id)}>
        <X size={15} />
        <span className="sr-only">알림 닫기</span>
      </button>
    </div>
  )
}
