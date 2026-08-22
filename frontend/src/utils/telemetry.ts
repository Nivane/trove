// Lightweight client telemetry + error reporting hook.
// Default sinks to console; in production you'd register an upstream (Sentry,
// Datadog) via setReporter(). Every user-facing operation can carry a
// requestId so incidents trace back to a server run_id.

type Reporter = (event: TelemetryEvent) => void

export interface TelemetryEvent {
  level: 'info' | 'warn' | 'error'
  source: string
  message: string
  requestId?: string
  meta?: Record<string, unknown>
  error?: unknown
  ts: string
}

let reporter: Reporter | null = null

/** Register an upstream sink (e.g. Sentry.captureException wrapper). */
export function setReporter(r: Reporter | null) {
  reporter = r
}

/** Generate a short per-operation tracing id (opaque, client-side). */
export function newRequestId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `rid-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function emit(
  level: TelemetryEvent['level'],
  source: string,
  message: string,
  meta?: TelemetryEvent['meta'] & { requestId?: string; error?: unknown },
) {
  const event: TelemetryEvent = {
    level,
    source,
    message,
    requestId: meta?.requestId,
    meta,
    error: meta?.error,
    ts: new Date().toISOString(),
  }
  reporter?.(event)
  if (level === 'error')
    console.error(`[trove:${source}]`, message, meta?.error ?? '')
  else if (level === 'warn') console.warn(`[trove:${source}]`, message)
}

export const telemetry = {
  info: (source: string, message: string, meta?: Record<string, unknown>) =>
    emit('info', source, message, meta),
  warn: (source: string, message: string, meta?: Record<string, unknown>) =>
    emit('warn', source, message, meta),
  error: (
    source: string,
    message: string,
    meta?: Record<string, unknown> & { error?: unknown; requestId?: string },
  ) => emit('error', source, message, meta),
}
