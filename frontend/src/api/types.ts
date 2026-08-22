// Shared wire types for the SSE event stream and API payloads.

export interface ChartSpec {
  type: string
  title?: string
  dimension?: string
  categories?: string[]
  series?: { name?: string; data?: (number | string | null)[] }[]
  measures?: string[]
}

export interface DoneSummary {
  session_id?: string
  question?: string
  sql?: string
  row_count?: number
  verdict?: string
  reason?: string
  error?: string
  final_response?: string
  columns?: string[]
  chart?: ChartSpec | null
  chart_option?: Record<string, unknown> | null
  insights?: unknown[]
  hitl_status?: string
  batched?: boolean
}

export interface StepPayload {
  node?: string
  label?: string
  content?: string
  sql?: string
  row_count?: number
  execution_time_ms?: number
  [k: string]: unknown
}

export interface TaskItem {
  task_id: string
  title: string
  status: 'pending' | 'in_progress' | 'done' | 'failed'
  position: number
}

export interface HitlPayload {
  payload?: {
    task_context?: { total?: number }
    [k: string]: unknown
  }
  [k: string]: unknown
}

export interface SseEvent {
  type: string
  data: Record<string, unknown>
}

export interface DatasourceInfo {
  name: string
  type: string
  default?: boolean
  status?: string
  kb_initialized?: boolean
  kb_items?: Record<string, number>
}
