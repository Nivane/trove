// SSE parser — direct port of the vanilla app.js createSSEParser():
// fetch streaming + TextDecoder + line buffer split on "\n\n",
// tolerant of malformed frames (the server never closes the stream
// mid-event, but a proxy might).

import type { SseEvent } from './types'

export function createSSEParser(onEvent: (ev: SseEvent) => void) {
  let buffer = ''
  return {
    push(chunk: string) {
      buffer += chunk
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const ev = parseFrame(frame)
        if (ev) onEvent(ev)
      }
    },
    flush() {
      const ev = parseFrame(buffer)
      buffer = ''
      if (ev) onEvent(ev)
    },
  }
}

function parseFrame(frame: string): SseEvent | null {
  if (!frame.trim()) return null
  let type = 'message'
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) type = line.slice(6).trim()
    else if (line.startsWith('data:')) data += line.slice(5).trim()
  }
  if (!data) return null
  try {
    return { type, data: JSON.parse(data) }
  } catch {
    return null // skip malformed frames (legacy tolerance)
  }
}

/** POST /v1/chat (or /v1/sessions/{id}/resume) and stream typed events. */
export async function streamSse(
  path: string,
  body: Record<string, unknown>,
  onEvent: (ev: SseEvent) => void,
  signal?: AbortSignal,
): Promise<Response> {
  const { useAuthStore } = await import('../stores/auth')
  const auth = useAuthStore()
  const resp = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok) return resp
  const parser = createSSEParser(onEvent)
  const reader = resp.body!.getReader()
  const decoder = new TextDecoder()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    parser.push(decoder.decode(value, { stream: true }))
  }
  parser.flush()
  return resp
}
