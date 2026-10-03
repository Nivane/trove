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
  ErrorInfo,
  HitlPayload,
  SessionInfo,
  SseEvent,
  StepPayload,
  TaskItem,
} from '../api/types'
import { fmtDateTime } from '../utils/format'

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
  /** 结构化错误(summary.error_info):错误卡片渲染这份,而非解析 error 文本。 */
  errorInfo?: ErrorInfo
  hitlBatch?: boolean
  hitlActionsShown?: boolean
  rating?: 1 | -1 | null
  requestId?: string
  /** In-flight nodes (from begin events) — powers the live "analysis now" bar. */
  live?: LiveStep[]
  /** Wall clock when the turn began streaming (live total-elapsed meter). */
  startedAt?: number
  /** 答案落盘的 ISO 时间(live 轮 = 终态时刻;历史轮 = 消息 timestamp)。
   *  缺席 = 拿不到 —— 溯源条省掉时间片段,不拿别的时刻冒名顶替。 */
  at?: string
}

const SESSION_KEY = 'trove_ui_session'
const SESSION_PAGE_SIZE = 20

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessionId: localStorage.getItem(SESSION_KEY) || '',
    sessions: [] as SessionInfo[],
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
          sessions: SessionInfo[]
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
        // 会话级数据源记忆:服务端消息元数据为准(跨设备一致),
        // 本地按会话存储兜底。
        const ui = useUiStore()
        const ds = lastTurnDatasource(this.turns)
        if (ds && ui.datasourceList.some((d) => d.name === ds)) {
          ui.setDatasource(ds)
          ui.rememberSessionDatasource(sid)
        } else {
          ui.restoreSessionDatasource(sid)
        }
        // 主题域与数据源同口径:服务端消息元数据优先,本地按会话存储兜底。
        // 放在数据源之后 —— setDatasource 会重置主题域,这里把它放回来。
        const tp = lastTurnTopic(this.turns)
        if (tp) {
          ui.setTopic(tp)
          ui.rememberSessionTopic(sid)
        } else {
          ui.restoreSessionTopic(sid)
        }
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
    /** 置顶/取消置顶。排序在服务端(置顶在前 + updated_at desc):
     *  写成功后重取列表,而不是本地把行挪到顶部 —— 本地挪动只对
     *  「已加载的那一页」成立,翻到第二页就会露馅。 */
    async pinSession(sid: string, pinned: boolean) {
      try {
        await apiPost(`/v1/sessions/${sid}/pin`, { pinned })
        const row = this.sessions.find((s) => s.session_id === sid)
        if (row) row.pinned = pinned
        await this.listSessions()
      } catch (e) {
        notifyError(String((e as Error)?.message ?? 'pin failed'))
      }
    },
    /** 取某会话的整段轮次(导出用):当前会话用内存态,其余只读拉取。
     *  拿不到就如实抛错 —— 导出一份缺轮的文档比报错更糟。 */
    async fetchSessionTurns(sid: string): Promise<Turn[]> {
      if (sid && sid === this.sessionId && this.turns.length) return this.turns
      const body = await apiGet(`/v1/sessions/${sid}`)
      return restoreTurns((body.messages ?? []) as StoredMessage[])
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
        // 主题域经 activeTopic 校验后才带上:过期/不属于当前源的值静默退化为「不限定」
        if (ui.activeTopic) body.topic = ui.activeTopic
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
          t.at = new Date().toISOString()
        }
        t.live = []
      }
      this.streaming = false
      this.batchRunning = false
      this.controller = null
      // 记住本轮实际使用的数据源与主题域,便于切回该会话时恢复
      const ui = useUiStore()
      ui.rememberSessionDatasource(this.sessionId)
      ui.rememberSessionTopic(this.sessionId)
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
            t.at = new Date().toISOString()
          } else {
            const answerAdd = summary?.final_response || content
            if (answerAdd && !t.answer.includes(answerAdd)) {
              t.answer += (t.answer ? '\n\n' : '') + answerAdd
            }
            if (summary) t.summary = summary
            if (summary?.error_info) t.errorInfo = summary.error_info
            if (summary?.sql && !t.steps.some((s) => s.node === 'gen_sql')) {
              t.steps.push({
                node: 'gen_sql',
                payload: { node: 'gen_sql', sql: summary.sql },
              })
            }
            // Batch in progress → intermediate per-task done; wait for the
            // terminal batched done. Otherwise this is the final answer.
            if (!this.batchRunning) {
              t.status = 'done'
              t.at = new Date().toISOString()
            }
          }
          break
        }
        case 'error': {
          // 原始串照旧留给诊断(telemetry / 反馈上报),呈现一律走 error_info。
          const summary = (ev.data as { summary?: DoneSummary }).summary
          const msg = String(
            ev.data.error ??
              ev.data.message ??
              summary?.error ??
              ev.data.content ??
              '',
          )
          this._failTurn(msg || 'unknown error', summary?.error_info)
          break
        }
        default:
          // legacy flat events (plan/verdict/correction/sql/result) are
          // rendered from `step` events — tolerated and ignored here
          break
      }
    },

    _failTurn(message: string, errorInfo?: ErrorInfo) {
      const t = this.currentTurn
      if (t) {
        t.error = message
        if (errorInfo) t.errorInfo = errorInfo
        t.status = 'error'
        t.live = []
      }
    },

    stop() {
      this.controller?.abort()
      const t = this.currentTurn
      if (t && t.status === 'streaming') {
        t.status = t.answer ? 'done' : 'error'
        if (t.answer) t.at = new Date().toISOString()
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
              tt.at = new Date().toISOString()
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
      if (this.currentTurn?.status === 'streaming') {
        this.currentTurn.status = 'done'
        this.currentTurn.at = new Date().toISOString()
      }
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

    /** 提交评分。返回值 = 是否成功 —— 依据抽屉的回执只报真实去向,
     *  失败就什么都不说,不显示一张"已提交"的假回执。 */
    async rateTurn(index: number, vote: 1 | -1, reason?: string): Promise<boolean> {
      const t = this.turns[index]
      if (!t || !t.question) return false
      const summary = t.summary
      const body: Record<string, unknown> = {
        question: t.question,
        vote,
      }
      // 负评带原因标签:优先 reason,否则回退答案摘要
      if (reason) {
        body.note = reason
      } else if (t.answer) {
        body.note = t.answer.slice(0, 800)
      }
      if (summary?.sql) body.sql_snippet = summary.sql
      if (summary?.run_id) body.run_id = summary.run_id
      try {
        await apiPost('/v1/kb/ratings', body)
        t.rating = vote
        return true
      } catch (e) {
        console.error('rate failed', e)
        return false
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
  /** 消息落盘 ISO 时间(GET /v1/sessions/{id} 一直带着它)。 */
  timestamp?: string
}

/** 最近一个有数据源记录的 turn(会话元数据为准,从尾往前找)。 */
function lastTurnDatasource(turns: Turn[]): string {
  for (let i = turns.length - 1; i >= 0; i--) {
    const ds = turns[i].summary?.datasource
    if (ds) return ds
  }
  return ''
}

/** 最近一个有主题域记录的 turn(与数据源同口径:服务端元数据优先)。 */
function lastTurnTopic(turns: Turn[]): string {
  for (let i = turns.length - 1; i >= 0; i--) {
    const tp = turns[i].summary?.topic
    if (tp) return tp
  }
  return ''
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
      // 历史轮时间戳还原:消息自带的 timestamp 就是这一轮答案的落盘时刻
      // (此前只读了 metadata,把整条时间线丢了)。缺席就不填 —— 溯源条
      // 省掉时间片段,而不是拿"现在"冒名顶替。
      if (m.timestamp) t.at = m.timestamp
      const meta = m.metadata ?? {}
      const summary = (meta.summary ?? null) as DoneSummary | null
      if (summary) {
        t.summary = {
          ...summary,
          final_response: summary.final_response || m.content,
        }
        t.answer = summary.final_response || m.content
        if (summary.error_info) t.errorInfo = summary.error_info
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

// ── 会话导出 Markdown(① 整段会话 → 一个 .md)────────────────────
//
// 纯函数(不碰 DOM):Sidebar 负责取轮次 + blob 下载,这里只把
// 已落盘的 turn 渲染成 Markdown。忠实渲染已定口径:每轮 = 用户问题
// heading + 答案 markdown + 有 SQL 时 ```sql 代码块 + 有分析时证据查询
// 节(补丁 2)+ 可渲染时结果表;图表不随文导出(界面里本来就有,HTML
// 报告另走 session-report.ts)。truncate 是**写明**的截断 ——
// 表格超过上限时 caption 同时给出「导出维度 / 完整维度」,不做静默丢行。

/** 结果表导出上限(超宽/超长只导出前 N,注明完整维度)。 */
export const EXPORT_MAX_ROWS = 50
export const EXPORT_MAX_COLS = 8

/** 分析证据节查询上限(超出写明「已截断 N/M」)。 */
export const EXPORT_MAX_QUERIES = 6

/** 分析证据节文案(补丁 2):答案 markdown 已含归因表/驱动树,本节只补
 *  证据查询 SQL 与降级标注 —— 不做同文重复。 */
export interface SessionAnalysisLabels {
  /** 节标题(「分析证据」)。 */
  title: string
  /** 证据抽屉摘要(「证据」)。 */
  evidence: string
  partial: string
  partialHint: string
  truncated: string
  /** purpose(overall/probe/drilldown/driver_tree)→ 显示名;缺 key 原样透出。 */
  purposes: Record<string, string>
}

/** 导出文档里的小标题/表头文案(由调用方按 ui.lang 从 i18n 取)。 */
export interface SessionExportLabels {
  results: string
  rows: string
  cols: string
  generatedAt: string
  rounds: string
  analysis: SessionAnalysisLabels
}

/** 单元格 → markdown 表格单元:null 空串,| 转义,换行压成 <br>。 */
function mdCell(v: unknown): string {
  if (v === null || v === undefined) return ''
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v)
  return s.replace(/\|/g, '\\|').replace(/\r?\n/g, '<br>')
}

/** 多行问题压成一行(heading 里不能有换行)。 */
function oneLine(s: string): string {
  return s.replace(/\s*\r?\n\s*/g, ' ').trim()
}

/** 一轮的结果表(无列/无行时返回空数组 —— 不渲染空表)。 */
function resultTable(turn: Turn, labels: SessionExportLabels): string[] {
  const summary = turn.summary
  const columns = (summary?.columns ?? []).map((c) => String(c))
  const all = (summary?.rows?.length
    ? summary.rows
    : summary?.rows_preview ?? []) as unknown[][]
  if (!columns.length || !all.length) return []

  const shownCols = columns.slice(0, EXPORT_MAX_COLS)
  const shownRows = all.slice(0, EXPORT_MAX_ROWS)
  const truncated =
    all.length > shownRows.length || columns.length > shownCols.length
  const dims = `${all.length} ${labels.rows} × ${columns.length} ${labels.cols}`
  const shownDims =
    `${shownRows.length} ${labels.rows} × ${shownCols.length} ${labels.cols}`
  const lines = [
    '',
    `### ${labels.results} (${truncated ? `${shownDims} / ${dims}` : dims})`,
    '',
    `| ${shownCols.map((c) => mdCell(c)).join(' | ')} |`,
    `| ${shownCols.map(() => '---').join(' | ')} |`,
  ]
  for (const row of shownRows) {
    lines.push(`| ${shownCols.map((_, j) => mdCell(row[j])).join(' | ')} |`)
  }
  return lines
}

/** 一轮的分析证据节(补丁 2):降级标注 + 证据查询 SQL(上限
 *  EXPORT_MAX_QUERIES,超出写明)。无 analysis / 无证据 → 空数组,
 *  老会话逐项跳过(同①纪律)。 */
function analysisSection(turn: Turn, labels: SessionExportLabels): string[] {
  const a = turn.summary?.analysis
  const al = labels.analysis
  if (!a) return []
  const queries = a.evidence?.queries ?? []
  if (!queries.length && !a.partial) return []
  const lines: string[] = ['', `### ${al.title}`]
  if (a.partial) {
    lines.push('', `> **${al.partial}**: ${al.partialHint}`)
  }
  const shown = queries.slice(0, EXPORT_MAX_QUERIES)
  shown.forEach((ev, i) => {
    const purpose =
      al.purposes[String(ev.purpose ?? '')] ?? String(ev.purpose ?? '')
    const meta = [purpose, ev.period, ev.filter]
      .filter(Boolean)
      .map(String)
      .join(' · ')
    lines.push('', `**${i + 1}. ${meta}**`)
    const sql = (ev.sql ?? '').trim()
    if (sql) lines.push('', '```sql', sql, '```')
  })
  if (queries.length > shown.length) {
    lines.push('', `> ${al.truncated} (${shown.length}/${queries.length})`)
  }
  return lines
}

/** 整段会话 → 一个 Markdown 文档。 */
export function buildSessionMarkdown(
  turns: Turn[],
  opts: {
    title?: string
    sessionId: string
    labels: SessionExportLabels
    now?: Date
  },
): string {
  const { labels } = opts
  const now = opts.now ?? new Date()
  const sid = opts.sessionId || ''
  const head = (opts.title ?? '').trim() || sid.slice(0, 8) || 'session'
  const out = [
    `# ${head}`,
    '',
    `> ${labels.generatedAt}: ${fmtDateTime(now.toISOString())} · ` +
      `${labels.rounds}: ${turns.length} · session:${sid.slice(0, 8)}`,
  ]
  turns.forEach((turn, i) => {
    out.push('', '---', '')
    out.push(`## ${i + 1}. ${oneLine(turn.question || '')}`)
    // 与界面同口径(ChatView 的 `turn.answer || turn.synthesis`):
    // 批收尾轮界面上显示的是逐条子任务答案,synthesis 只在 answer 缺席时兜底
    const answer = (turn.answer || turn.synthesis || '').trim()
    if (answer) out.push('', answer)
    else if (turn.error) out.push('', `> ${oneLine(turn.error)}`)
    const sql = (turn.summary?.sql || '').trim()
    if (sql) out.push('', '```sql', sql, '```')
    out.push(...analysisSection(turn, labels))
    out.push(...resultTable(turn, labels))
  })
  out.push('')
  return out.join('\n')
}

/** 下载文件名基底:`<标题或首问截断>-<YYYYMMDD>`,非法字符清洗
 *  (md 导出与 HTML 报告共用)。 */
export function sessionFileBase(
  title: string | undefined,
  sessionId: string,
  now: Date = new Date(),
): string {
  const raw = (title ?? '').trim() || sessionId.slice(0, 8) || 'session'
  const safe = raw
    // eslint-disable-next-line no-control-regex
    .replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '-')
    .replace(/\s+/g, ' ')
    .replace(/^[.\-\s]+|[.\-\s]+$/g, '')
    .slice(0, 40)
    .trim()
  const d =
    `${now.getFullYear()}` +
    `${String(now.getMonth() + 1).padStart(2, '0')}` +
    `${String(now.getDate()).padStart(2, '0')}`
  return `${safe || sessionId.slice(0, 8) || 'session'}-${d}`
}

/** 下载文件名:`<标题或首问截断>-<YYYYMMDD>.md`,非法字符清洗。 */
export function sessionMarkdownFilename(
  title: string | undefined,
  sessionId: string,
  now: Date = new Date(),
): string {
  return `${sessionFileBase(title, sessionId, now)}.md`
}
