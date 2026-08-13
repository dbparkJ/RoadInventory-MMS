export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`brand ${compact ? 'compact' : ''}`} aria-label="MMS 도로대장 자동화">
      <span className="brand-accent" aria-hidden="true" />
      <span className="brand-copy">
        <strong>MMS 도로대장 자동화</strong>
        {!compact && <small>ROAD INVENTORY WORKSPACE</small>}
      </span>
    </div>
  )
}

export function BrandLogo() {
  return (
    <div className="brand-signature" aria-label="GEO&">
      <img className="brand-logo" src="/logo.png" alt="GEO&" />
    </div>
  )
}
