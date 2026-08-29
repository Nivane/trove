// Chat turn state machine — ports the vanilla handleEvent()/finishTurn()
// logic, including the hard-won batched-done aggregation:
//   - intermediate per-task `done` events only APPEND answer chunks;
//   - only the terminal `done` carrying summary.batched finalizes the turn;
//   - HITL pauses render an actions card; resume continues the same stream.

import { defineStore } from 'pinia'
import { streamSse } from '../api/sse'
import { apiGet, apiPost } from '../api/http'
import { useUiStore } from './ui'
import { notifyError } from '../utils/notify'
import { telemetry, newRequestId } from '../utils/telemetry'
import type {
  DoneSummary,
  HitlPayload,
  SseEvent,
  StepPayload,
  TaskItem,
} from '../api/types'

export interface StepCard {
  node: string
  label?: string
  payload: StepPayload
}

/** A node currently in flight (from a `begin` event, not yet resolved by a step). */
export interface LiveStep {
  node: string
  label?: string
  /** Wall clock (Date.now) when the node started running. */
  startedAt: number
  startedSeq: number
}

export interface Turn {
  question: string
  thoughts: string[]
  steps: StepCard[]
  answer: string
  /** 批收尾综合回答(summary.final_response):与逐条子任务答案(answer)分离展示 */
  synthesis?: string
  summary: DoneSummary | null
  status: 'streaming' | 'done' | 'error' | 'hitl'
  error?: string
  hitlBatch?: boolean
  hitlActionsShown?: boolean
  rating?: 1 | -1 | null
  requestId?: string
  /** In-flight nodes (from begin events) — powers the live "analysis now" bar. */
  live?: LiveStep[]
  /** Wall clock when the turn began streaming (live total-elapsed meter). */
  startedAt?: number
}

const SESSION_KEY = 'trove_ui_session'
const SESSION_PAGE_SIZE = 20

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessionId: localStorage.getItem(SESSION_KEY) || '',
    sessions: [] as {
      session_id: string
      created_at?: string
      updated_at?: string
      message_count?: number
      title?: string
    }[],
    sessionsLoading: false,
    sessionsOffset: 0,
    sessionsHasMore: true,
    turns: [] as Turn[],
    tasks: [] as TaskItem[],
    batchRunning: false,
    streaming: false,
    pendingHitl: null as null | {
      sessionId: string
      workflow: string
      batch: boolean
    },
    controller: null as AbortController | null,
  }),
  getters: {
    currentTurn(state): Turn | null {
      return state.turns.length ? state.turns[state.turns.length - 1] : null
    },
  },
  actions: {
    setSessionId(id: string) {
      this.sessionId = id
      localStorage.setItem(SESSION_KEY, id)
    },
    clearSession() {
      this.setSessionId('')
      this.turns = []
      this.tasks = []
    },
    async listSessions() {
      // reset pagination and reload the first page (after create/delete/send)
      this.sessions = []
      this.sessionsOffset = 0
      this.sessionsHasMore = true
      return this.loadMoreSessions()
    },
    async loadMoreSessions() {
      if (this.sessionsLoading || !this.sessionsHasMore) return
      this.sessionsLoading = true
      try {
        const body = await apiGet<{
          sessions: {
            session_id: string
            created_at?: string
            updated_at?: string
            message_count?: number
            title?: string
          }[]
          has_more?: boolean
        }>(`/v1/sessions?limit=${SESSION_PAGE_SIZE}&offset=${this.sessionsOffset}`)
        const page = body.sessions ?? []
        this.sessions.push(...page)
        this.sessionsOffset += page.length
        this.sessionsHasMore = !!body.has_more && page.length > 0
        return page.length
      } finally {
        this.sessionsLoading = false
      }
    },
    async createSession() {
      const body = await apiPost('/v1/sessions', {})
      this.setSessionId(body.session_id)
      await this.loadTasks(body.session_id)
      return body.session_id
    },
    async loadSession(sid: string) {
      this.setSessionId(sid)
      try {
        const body = await apiGet(`/v1/sessions/${sid}`)
        this.turns = restoreTurns(body.messages ?? [])
      } catch {
        this.turns = []
      }
      await this.loadTasks(sid)
    },
    async loadTasks(sid: string) {
      try {
        const body = await apiGet(`/v1/sessions/${sid}/tasks`)
        this.tasks = body.tasks ?? []
      } catch {
        // silently ignore (cross-round restore is best-effort)
      }
    },
    async deleteSession(sid: string) {
      try {
        await fetch(`/v1/sessions/${sid}`, {
          method: 'DELETE',
          headers: this._authHeaders(),
        })
        if (sid === this.sessionId) this.setSessionId('')
        await this.listSessions()
      } catch (e) {
        notifyError(String((e as Error)?.message ?? 'delete failed'))
      }
    },
    async renameSession(sid: string, title: string) {
      try {
        await apiPost(`/v1/sessions/${sid}/title`, { title })
        const row = this.sessions.find((s) => s.session_id === sid)
        if (row) row.title = title
      } catch (e) {
        notifyError(String((e as Error)?.message ?? 'rename failed'))
      }
    },
    _authHeaders(): Record<string, string> {
      const token = localStorage.getItem('trove_auth_token')
      return token ? { Authorization: `Bearer ${token}` } : {}
    },

    async send(question: string) {
      this.streaming = true
      this.batchRunning = false
      this.pendingHitl = null
      this.controller = new AbortController()
      const requestId = newRequestId()
      this.turns.push({
        question,
        thoughts: [],
        steps: [],
        answer: '',
        summary: null,
        status: 'streaming',
        requestId,
        live: [],
        startedAt: Date.now(),
      })

      let retried = false
      for (;;) {
        const ui = useUiStore()
        const body: Record<string, unknown> = {
          question,
          workflow: 'reflection',
        }
        if (ui.datasource) body.datasource = ui.datasource
        if (this.sessionId) body.session_id = this.sessionId

        const resp = await streamSse(
          '/v1/chat',
          body,
          (ev) => this.onEvent(ev),
          this.controller.signal,
        )

        if (resp.status === 404 && this.sessionId && !retried) {
          // stale session on the server → retry once with a fresh one
          retried = true
          this.turns.pop()
          this.setSessionId('')
          await this.createSession()
          // re-instate the turn so the retried stream's events target it
          this.turns.push({
            question,
            thoughts: [],
            steps: [],
            answer: '',
            summary: null,
            status: 'streaming',
            requestId: newRequestId(),
            live: [],
            startedAt: Date.now(),
          })
          continue
        }
        if (!resp.ok) {
          telemetry.error('chat.send', `HTTP ${resp.status}`, {
            requestId,
            error: resp.statusText,
          })
          this._failTurn(`HTTP ${resp.status}`)
          return
        }
        break
      }

      const t = this.currentTurn
      if (t && t.status === 'streaming') {
        // stream closed without a terminal event — guard against a hung turn
        if (!t.answer && !t.error) {
          telemetry.error('chat.send', 'stream interrupted', { requestId })
          t.error = 'stream interrupted'
          t.status = 'error'
        } else {
          t.status = 'done'
        }
        t.live = []
      }
      this.streaming = false
      this.batchRunning = false
      this.controller = null
      await this.listSessions()
    },

    onEvent(ev: SseEvent) {
      const t = this.currentTurn
      if (!t) return
      switch (ev.type) {
        case 'session': {
          const sid = (ev.data as { session_id?: string }).session_id
          if (sid) this.setSessionId(sid)
          break
        }
        case 'begin': {
          const node = String(ev.data.node ?? '')
          if (!node) break
          if (!t.startedAt) t.startedAt = Date.now()
          t.live = t.live ?? []
          // A repeated begin of the SAME node (backend re-fires for node
          // chains) should not double-count — bump the live marker instead.
          const tail = t.live[t.live.length - 1]
          if (tail && tail.node === node) {
            tail.startedAt = Date.now()
          } else {
            t.live.push({
              node,
              label: ev.data.label as string | undefined,
              startedAt: Date.now(),
              startedSeq: t.live.length + 1,
            })
          }
          break
        }
        case 'thought': {
          const text = String(ev.data.content ?? ev.data.text ?? '')
          if (text.trim()) t.thoughts.push(text)
          break
        }
        case 'step': {
          const p = ev.data as StepPayload
          t.steps.push({
            node: p.node ?? p.label ?? 'step',
            label: p.label,
            payload: p,
          })
          // A step marks the completion of the current node chain — resolve
          // every pending begin (nested sub-nodes included).
          t.live = []
          break
        }
        case 'task': {
          const task = ev.data as Partial<TaskItem> & { task_id: string }
          const idx = this.tasks.findIndex((x) => x.task_id === task.task_id)
          if (idx >= 0)
            this.tasks[idx] = { ...this.tasks[idx], ...task } as TaskItem
          else this.tasks.push(task as TaskItem)
          this.batchRunning = this.tasks.some(
            (x) => x.status === 'pending' || x.status === 'in_progress',
          )
          break
        }
        case 'hitl': {
          const p = ev.data as HitlPayload
          const total = p.payload?.task_context?.total
          t.status = 'hitl'
          t.hitlBatch = !!total && total > 1
          t.hitlActionsShown = false
          t.live = []
          this.pendingHitl = {
            sessionId: this.sessionId,
            workflow: 'reflection',
            batch: t.hitlBatch,
          }
          break
        }
        case 'done': {
          const summary = ev.data.summary as DoneSummary | undefined
          const content = String(ev.data.content ?? '')
          t.live = []
          if (summary?.batched) {
            // terminal batched done → finalize the whole turn; the synthesis
            // answer is kept separate (rendered above the per-task answers)
            t.summary = summary
            t.synthesis = summary.final_response
            t.status = 'done'
          } else {
            const answerAdd = summary?.final_response || content
            if (answerAdd && !t.answer.includes(answerAdd)) {
              t.answer += (t.answer ? '\n\n' : '') + answerAdd
            }
            if (summary) t.summary = summary
            if (summary?.sql && !t.steps.some((s) => s.node === 'gen_sql')) {
              t.steps.push({
                node: 'gen_sql',
                payload: { node: 'gen_sql', sql: summary.sql },
              })
            }
            // Batch in progress → intermediate per-task done; wait for the
            // terminal batched done. Otherwise this is the final answer.
            if (!this.batchRunning) t.status = 'done'
          }
          break
        }
        case 'error': {
          const msg = String(
            ev.data.error ??
              ev.data.message ??
              ev.data.content ??
              (ev.data as { summary?: { error?: string } }).summary?.error ??
              '',
          )
          this._failTurn(msg || 'unknown error')
          break
        }
        default:
          // legacy flat events (plan/verdict/correction/sql/result) are
          // rendered from `step` events — tolerated and ignored here
          break
      }
    },

    _failTurn(message: string) {
      const t = this.currentTurn
      if (t) {
        t.error = message
        t.status = 'error'
        t.live = []
      }
    },

    stop() {
      this.controller?.abort()
      const t = this.currentTurn
      if (t && t.status === 'streaming') {
        t.status = t.answer ? 'done' : 'error'
        t.error = t.answer ? undefined : 'aborted'
        t.live = []
      }
      this.streaming = false
      this.batchRunning = false
    },

    async resume(decision: 'yes' | 'approve_all' | 'no') {
      const hitl = this.pendingHitl
      if (!hitl) return
      this.pendingHitl = null
      const t = this.currentTurn
      if (t) {
        t.status = 'streaming'
        t.hitlActionsShown = true
      }
      this.streaming = true
      this.controller = new AbortController()
      await streamSse(
        `/v1/sessions/${hitl.sessionId}/resume`,
        { decision, workflow: hitl.workflow },
        (ev) => {
          const tt = this.currentTurn
          if (!tt) return
          if (ev.type === 'begin') {
            const node = String(ev.data.node ?? '')
            if (!node) return
            if (!tt.startedAt) tt.startedAt = Date.now()
            tt.live = tt.live ?? []
            const tail = tt.live[tt.live.length - 1]
            if (tail && tail.node === node) {
              tail.startedAt = Date.now()
            } else {
              tt.live.push({
                node,
                label: ev.data.label as string | undefined,
                startedAt: Date.now(),
                startedSeq: tt.live.length + 1,
              })
            }
          } else if (ev.type === 'step') {
            const p = ev.data as StepPayload
            tt.steps.push({
              node: p.node ?? p.label ?? 'step',
              label: p.label,
              payload: p,
            })
            tt.live = []
          } else if (ev.type === 'task') {
            const task = ev.data as Partial<TaskItem> & { task_id: string }
            const idx = this.tasks.findIndex((x) => x.task_id === task.task_id)
            if (idx >= 0)
              this.tasks[idx] = { ...this.tasks[idx], ...task } as TaskItem
            this.batchRunning = this.tasks.some(
              (x) => x.status === 'pending' || x.status === 'in_progress',
            )
          } else if (ev.type === 'done') {
            const summary = ev.data.summary as DoneSummary | undefined
            const content = String(ev.data.content ?? '')
            tt.live = []
            if (summary?.batched) {
              // terminal batched done → synthesis kept separate from the
              // per-task answer chunks appended below
              tt.summary = summary
              tt.synthesis = summary.final_response
              tt.status = 'done'
            } else {
              const answerAdd = summary?.final_response || content
              if (answerAdd && !tt.answer.includes(answerAdd)) {
                tt.answer += (tt.answer ? '\n\n' : '') + answerAdd
              }
              if (summary) tt.summary = summary
            }
          } else if (ev.type === 'error') {
            this._failTurn(
              String(
                ev.data.error ??
                  ev.data.message ??
                  ev.data.content ??
                  'resume failed',
              ),
            )
          }
        },
        this.controller.signal,
      )
      if (this.currentTurn?.status === 'streaming')
        this.currentTurn.status = 'done'
      if (this.currentTurn) this.currentTurn.live = []
      this.streaming = false
      this.batchRunning = false
      this.controller = null
    },

    /** Re-send the most recent failed turn's question. */
    async retry() {
      const t = this.currentTurn
      if (!t || t.status !== 'error' || !t.question) return
      await this.send(t.question)
    },

    /** Regenerate the last answer: drop its turn and stream a fresh run. */
    async regenerate() {
      const t = this.currentTurn
      if (!t || !t.question || this.streaming) return
      this.turns = this.turns.slice(0, -1)
      this.tasks = []
      await this.send(t.question)
    },

    /** Edit a past user message and branch from there (ChatGPT-style). */
    async editAndResend(index: number, question: string) {
      const q = question.trim()
      if (!q || this.streaming) return
      if (index < 0 || index >= this.turns.length) return
      this.turns = this.turns.slice(0, index)
      await this.send(q)
    },

    async rateTurn(index: number, vote: 1 | -1) {
      const t = this.turns[index]
      if (!t || !t.question) return
      const summary = t.summary
      const body: Record<string, unknown> = {
        question: t.question,
        vote,
      }
      if (t.answer) body.note = t.answer.slice(0, 800)
      if (summary?.sql) body.sql_snippet = summary.sql
      if (summary?.run_id) body.run_id = summary.run_id
      try {
        await apiPost('/v1/kb/ratings', body)
        t.rating = vote
      } catch (e) {
        console.error('rate failed', e)
      }
    },

    async clearConversation() {
      if (this.sessionId) {
        await fetch(`/v1/sessions/${this.sessionId}/clear`, {
          method: 'POST',
          headers: this._authHeaders(),
        })
      }
      this.turns = []
      this.tasks = []
    },

    async compactConversation() {
      if (!this.sessionId) return
      await fetch(`/v1/sessions/${this.sessionId}/compact`, {
        method: 'POST',
        headers: this._authHeaders(),
      })
    },
  },
})

interface StoredMessage {
  role: string
  content: string
  metadata?: Record<string, unknown>
}

/** Rebuild chat turns from GET /v1/sessions/{id} messages.
 *
 * Persisted metadata carries the structured summary (sql / chart /
 * rows_preview ...) written by the backend's _record_exchange; older
 * sessions only have plain text — those fall back to text-only turns.
 */
export function restoreTurns(messages: StoredMessage[]): Turn[] {
  const turns: Turn[] = []
  for (const m of messages) {
    if (m.role === 'user') {
      turns.push({
        question: m.content,
        thoughts: [],
        steps: [],
        answer: '',
        summary: null,
        status: 'done',
      })
    } else if (m.role === 'assistant' && turns.length) {
      const t = turns[turns.length - 1]
      const meta = m.metadata ?? {}
      const summary = (meta.summary ?? null) as DoneSummary | null
      if (summary) {
        t.summary = {
          ...summary,
          final_response: summary.final_response || m.content,
        }
        t.answer = summary.final_response || m.content
        // 分析面板只服务"当前直播轮次":历史会话不重建步骤/日志,
        // 只保留 answer/summary(消息体渲染 SQL 与图表用),保证点开
        // 历史会话时右侧没有可展开的分析过程。
      } else {
        t.answer = m.content
      }
    }
  }
  return turns.filter((t) => t.question || t.answer)
}
