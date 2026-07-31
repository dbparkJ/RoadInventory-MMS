export function formatBytes(value?: number): string {
  if (value === undefined || !Number.isFinite(value)) return '—'
  if (value === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const exponent = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  const result = value / 1024 ** exponent
  return `${result >= 10 || exponent === 0 ? result.toFixed(0) : result.toFixed(1)} ${units[exponent]}`
}

export function formatDistance(value?: number): string {
  if (value === undefined) return '—'
  return value >= 1000 ? `${(value / 1000).toFixed(1)} km` : `${Math.round(value)} m`
}

export function formatCount(value?: number): string {
  if (value === undefined) return '—'
  return new Intl.NumberFormat('ko-KR', { notation: value >= 10_000 ? 'compact' : 'standard' }).format(
    value,
  )
}

export function formatDate(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('ko-KR', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export function formatFrameTimestamp(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date)
}

export function formatDuration(seconds?: number): string {
  if (seconds === undefined || seconds < 0) return '계산 중'
  if (seconds < 60) return `${Math.ceil(seconds)}초`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.ceil(seconds % 60)
  return `${minutes}분 ${rest}초`
}

export function joinPath(...parts: string[]): string {
  return parts
    .flatMap((part) => part.split('/'))
    .filter(Boolean)
    .join('/')
}
