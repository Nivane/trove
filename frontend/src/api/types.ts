// Shared wire types for the SSE event stream and API payloads.

export interface ChartSpec {
  type: string
  title?: string
  dimension?: string
  categories?: string[]
  series?: { name?: string; data?: (number | string | null)[] }[]
  measures?: string[]
}

export interface ErrorInfo {
  /** 失败归类:gave_up / too_complex / permission / datasource / model /
   *  query / mismatch / unclear / unknown —— 决定卡片文案与是否可重试。 */
  kind?: string
  title?: string
  explanation?: string
  suggestion?: string
  retryable?: boolean
  detail?: {
    /** 原始错误文本(内部措辞),仅管理员折叠区展示。 */
    raw?: string
    node?: string
    error_class?: string
    domain?: string
    [k: string]: unknown
  }
}

export interface DoneSummary {
  session_id?: string
  run_id?: string
  question?: string
  /** 意图层改写痕迹:省略式追问/纯反馈替换上一问时的原始问题。 */
  rewritten_question?: string
  /** 回答所用数据源名。 */
  datasource?: string
  sql?: string
  row_count?: number
  verdict?: string
  reason?: string
  error?: string
  /** 错误呈现层产物:用户可见的标题/解释/建议 + 机器细节。前端渲染错误卡片,
   *  不解析错误 markdown、不重猜类别。 */
  error_info?: ErrorInfo
  final_response?: string
  columns?: string[]
  /** 完整查询结果(受后端 result_max_rows 约束;下载用,不放回答案表格)。 */
  rows?: unknown[][]
  chart?: ChartSpec | null
  chart_option?: Record<string, unknown> | null
  insights?: unknown[]
  hitl_status?: string
  batched?: boolean
  total_elapsed_ms?: number
  token_usage?: { prompt?: number; completion?: number; total?: number }
  cached?: boolean
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
