// Chat turn state machine — ports the vanilla handleEvent()/finishTurn()
// logic, including the hard-won batched-done aggregation:
//   - intermediate per-task `done` events only APPEND answer chunks;
//   - only the terminal `done` carrying summary.batched finalizes the turn;
//   - HITL pauses render an actions card; resume continues the same stream.

import { defineStore } from 'pinia'
import { streamSse } from '../api/sse'
import { apiGet } from '../api/http'
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
}

const SESSION_KEY = 'trove_ui_session'

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessionId: localStorage.getItem(SESSION_KEY) || '',
    sessions: [] as { session_id: string; created_at?: string; updated_at?: string; message_count?: number }[],
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
      const body = await apiGet('/v1/sessions')
      this.sessions = body.sessions ?? []
      return this.sessions
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
      await fetch(`/v1/sessions/${sid}`, {
        method: 'DELETE',
        headers: this._authHeaders(),
      })
      if (sid === this.sessionId) this.setSessionId('')
      await this.listSessions()
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
      this.turns.push({
        question,
        thoughts: [],
        steps: [],
        answer: '',
        summary: null,
        status: 'streaming',
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
          continue
        }
        if (!resp.ok) {
          this._failTurn(`HTTP ${resp.status}`)
          return
        }
        break
      }

      const t = this.currentTurn
      if (t && t.status === 'streaming') {
        // stream closed without a terminal event — guard against a hung turn
        if (!t.answer && !t.error) {
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
        case 'thought':
          t.thoughts.push(String(ev.data.content ?? ev.data.text ?? ''))
          break
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
          if (summary?.batched) {
            // terminal batched done → finalize the whole turn
            t.summary = summary
            t.status = 'done'
          } else if (summary?.final_response) {
            // per-task done inside a batch: only append the answer chunk
            t.answer += '\n\n' + summary.final_response
            if (summary.sql) t.steps.push({ node: 'gen_sql', payload: { sql: summary.sql } })
          } else {
            t.status = 'done'
          }
          break
        }
        case 'error': {
          const msg = String(ev.data.error ?? ev.data.message ?? '')
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
            if (summary?.final_response) tt.answer += '\n\n' + summary.final_response
            if (summary?.batched || !summary?.final_response) {
              tt.summary = summary ?? tt.summary
              tt.status = 'done'
            }
          } else if (ev.type === 'error') {
            this._failTurn(String(ev.data.error ?? 'resume failed'))
          }
        },
        this.controller.signal,
      )
      if (this.currentTurn?.status === 'streaming') this.currentTurn.status = 'done'
      this.streaming = false
      this.batchRunning = false
      this.controller = null
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
