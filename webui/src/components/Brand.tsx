import { ScanLine } from 'lucide-react'

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className="brand" aria-label="MMS Studio">
      <span className="brand-mark">
        <ScanLine size={20} strokeWidth={2.25} />
      </span>
      {!compact && (
        <span className="brand-copy">
          <strong>MMS Studio</strong>
          <small>Spatial Operations</small>
        </span>
      )}
    </div>
  )
}
