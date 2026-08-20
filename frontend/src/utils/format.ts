export function fmtVal(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return v.toLocaleString('en-US')
    return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
  }
  return String(v)
}

export function trunc(s: string, n = 24): string {
  return s.length > n ? s.slice(0, n) + '…' : s
}

export function fmtDuration(ms: number | undefined): string {
  if (ms === undefined) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}
