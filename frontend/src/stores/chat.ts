// Chat turn state machine — ports the vanilla handleEvent()/finishTurn()
// logic, including the hard-won batched-done aggregation:
//   - intermediate per-task `done` events only APPEND answer chunks;
//   - only the terminal `done` carrying summary.batched finalizes the turn;
//   - HITL pauses render an actions card; resume continues the same stream.

import { defineStore } from 'pinia'
import { streamSse } from '../api/sse'
import { apiGet, apiPost } from '../api/http'
import { notifyError } from '../utils/notify'
import { telemetry, newRequestId } from '../utils/telemetry'
import type { DoneSummary, HitlPayload, SseEvent, StepPayload, TaskItem } from '../api/types'

export interface StepCard {
  node: string
  label?: string
  payload: StepPayload
}

export interface Turn {
  question: string
  thoughts: string[]
  steps: StepCard[]
  answer: string
  summary: DoneSummary | null
  status: 'streaming' | 'done' | 'error' | 'hitl'
  error?: string
  hitlBatch?: boolean
  hitlActionsShown?: boolean
  rating?: 1 | -1 | null
  requestId?: string
}

const SESSION_KEY = 'trove_ui_session'

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessionId: localStorage.getItem(SESSION_KEY) || '',
    sessions: [] as { session_id: string; created_at?: string; updated_at?: string; message_count?: number; title?: string }[],
    sessionsLoading: false,
    turns: [] as Turn[],
    tasks: [] as TaskItem[],
    batchRunning: false,
    streaming: false,
    pendingHitl: null as null | { sessionId: string; workflow: string; batch: boolean },
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
      this.sessionsLoading = true
      try {
        const body = await apiGet('/v1/sessions')
        this.sessions = body.sessions ?? []
        return this.sessions
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
      this.turns = []
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
      })

      let retried = false
      for (;;) {
        const body: Record<string, unknown> = {
          question,
          workflow: 'reflection',
        }
        if (this.sessionId) body.session_id = this.sessionId

        const resp = await streamSse('/v1/chat', body, (ev) => this.onEvent(ev), this.controller.signal)

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
          })
          continue
        }
        if (!resp.ok) {
          telemetry.error('chat.send', `HTTP ${resp.status}`, { requestId, error: resp.statusText })
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
        case 'thought': {
          const text = String(ev.data.content ?? ev.data.text ?? '')
          if (text.trim()) t.thoughts.push(text)
          break
        }
        case 'step': {
          const p = ev.data as StepPayload
          t.steps.push({ node: p.node ?? p.label ?? 'step', label: p.label, payload: p })
          break
        }
        case 'task': {
          const task = ev.data as Partial<TaskItem> & { task_id: string }
          const idx = this.tasks.findIndex((x) => x.task_id === task.task_id)
          if (idx >= 0) this.tasks[idx] = { ...this.tasks[idx], ...task } as TaskItem
          else this.tasks.push(task as TaskItem)
          this.batchRunning = this.tasks.some((x) => x.status === 'pending' || x.status === 'in_progress')
          break
        }
        case 'hitl': {
          const p = ev.data as HitlPayload
          const total = p.payload?.task_context?.total
          t.status = 'hitl'
          t.hitlBatch = !!total && total > 1
          t.hitlActionsShown = false
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
          if (summary?.batched) {
            // terminal batched done → finalize the whole turn
            t.summary = summary
            t.status = 'done'
          } else {
            const answerAdd = summary?.final_response || content
            if (answerAdd && !t.answer.includes(answerAdd)) {
              t.answer += (t.answer ? '\n\n' : '') + answerAdd
            }
            if (summary) t.summary = summary
            if (summary?.sql && !t.steps.some((s) => s.node === 'gen_sql')) {
              t.steps.push({ node: 'gen_sql', payload: { node: 'gen_sql', sql: summary.sql } })
            }
            // Batch in progress → intermediate per-task done; wait for the
            // terminal batched done. Otherwise this is the final answer.
            if (!this.batchRunning) t.status = 'done'
          }
          break
        }
        case 'error': {
          const msg = String(ev.data.error ?? ev.data.message ?? ev.data.content ?? (ev.data as { summary?: { error?: string } }).summary?.error ?? '')
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
      }
    },

    stop() {
      this.controller?.abort()
      const t = this.currentTurn
      if (t && t.status === 'streaming') {
        t.status = t.answer ? 'done' : 'error'
        t.error = t.answer ? undefined : 'aborted'
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
          if (ev.type === 'step') {
            const p = ev.data as StepPayload
            tt.steps.push({ node: p.node ?? p.label ?? 'step', label: p.label, payload: p })
          } else if (ev.type === 'task') {
            const task = ev.data as Partial<TaskItem> & { task_id: string }
            const idx = this.tasks.findIndex((x) => x.task_id === task.task_id)
            if (idx >= 0) this.tasks[idx] = { ...this.tasks[idx], ...task } as TaskItem
            this.batchRunning = this.tasks.some(
              (x) => x.status === 'pending' || x.status === 'in_progress',
            )
          } else if (ev.type === 'done') {
            const summary = ev.data.summary as DoneSummary | undefined
            const content = String(ev.data.content ?? '')
            const answerAdd = summary?.final_response || content
            if (answerAdd && !tt.answer.includes(answerAdd)) {
              tt.answer += (tt.answer ? '\n\n' : '') + answerAdd
            }
            if (summary) tt.summary = summary
            if (summary?.batched || !summary?.final_response) {
              tt.status = 'done'
            }
          } else if (ev.type === 'error') {
            this._failTurn(String(ev.data.error ?? ev.data.message ?? ev.data.content ?? 'resume failed'))
          }
        },
        this.controller.signal,
      )
      if (this.currentTurn?.status === 'streaming') this.currentTurn.status = 'done'
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
