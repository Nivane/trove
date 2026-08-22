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

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      return true
    } catch {
      return false
    }
  }
}
