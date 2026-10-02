// 答案溯源层(P1):把一份终态摘要 + 落盘时间映射成「能披露的行」。
//
// 纪律(与后端 output.py 的 I7 同一条):**拿不到就不显示** —— 每个字段在
// 没有数据时返回空串/null,调用方整行不渲染,而不是渲染成「未知」或猜一个
// 值。这里是纯函数层:组件只负责 i18n 标签与排版,单测可直接驱动。

import type {
  ConfidenceEvidenceItem,
  DoneSummary,
  ExecutionEvidence,
  KbHitItem,
  PrincipalWire,
} from '../api/types'
import type { MaskingReport } from './masking'
import { maskingBadge } from './masking'
import { fmtDuration, trunc } from './format'

export interface KbHitView {
  kind: string
  /** 人读描述:term → "术语 → 映射";example/template → 问句(截断)。 */
  label: string
  /** 模板治理状态原样(certified / draft / …);'' = 没有这个键。 */
  status: string
}

export interface ProvenanceView {
  datasource: string
  /** 生成时间(秒级);'' = 这次拿不到时间。 */
  time: string
  /** 耗时(如 "1.4s");'' = 未测。 */
  elapsed: string
  model: string
  runId: string
  /** run_id 短形态(前 8 位,折叠条用)。 */
  runShort: string
  /** answer_source 原值;'' = 没有可披露的答案。 */
  source: string
  /** "82%";'' = 不可披露(0 或缺失)。 */
  confidence: string
  sqlConfidence: string
  confidenceWhy: string[]
  /** 数据截止;'' = 没查过。 */
  asOf: string
  /** 截止依据(as_of_basis 原值:'' / 'unknown' / 描述)。 */
  asOfBasis: string
  kbHits: KbHitView[]
  verdict: string
  /** 与 masking.ts 同源的三态(null / 空 / bypass)。 */
  mask: { kind: 'masked' | 'bypass'; count: number } | null
  maskedFields: string[]
  principalSubject: string
  principalRole: string
  onBehalfOf: string
}

/** 折叠条状态 chip(由 view 派生,顺序即优先级)。 */
export interface ProvChip {
  id: 'kb' | 'verify' | 'mask'
  n?: number
  tone?: 'ok' | 'warn'
  titleKey?: 'bypassBadgeTip' | 'maskedColTip'
}

/** 来源档位 → i18n key('' = 认不出的取值,按原样透出,不猜一个档位)。 */
export type SourceKey =
  | 'provSrcCertified'
  | 'provSrcReused'
  | 'provSrcCompiled'
  | 'provSrcGenerated'

export function sourceLabelKey(source: string): SourceKey | '' {
  switch (source) {
    case 'certified':
      return 'provSrcCertified'
    case 'reused':
      return 'provSrcReused'
    case 'compiled':
      return 'provSrcCompiled'
    case 'generated':
      return 'provSrcGenerated'
    default:
      return ''
  }
}

/** 模板治理状态 → i18n key('' = 原样透出:认不出 ≠ 已认证)。 */
export function statusLabelKey(status: string): 'provStatusCertified' | 'provStatusDraft' | '' {
  switch (status) {
    case 'certified':
      return 'provStatusCertified'
    case 'draft':
      return 'provStatusDraft'
    default:
      return ''
  }
}

/** 秒级时间戳("YYYY-MM-DD HH:mm:ss");坏值/空值 → ''(整行不渲染)。 */
export function fmtStamp(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  )
}

function pct(v: number | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v) || v <= 0) return ''
  return `${Math.round(v * 100)}%`
}

function hitView(h: KbHitItem): KbHitView {
  const kind = String(h.kind ?? '')
  if (kind === 'term') {
    return {
      kind,
      label: [h.term, h.mapping].filter(Boolean).join(' → ') || '',
      status: '',
    }
  }
  return {
    kind: kind || 'example',
    label: trunc(String(h.question ?? ''), 48),
    status: String(h.status ?? ''),
  }
}

/**
 * 一份摘要 → 可披露视图。
 *
 * `at` = 这轮的落盘时间(live 轮为 done 时刻,历史轮为消息 timestamp);
 * 缺席时 `time` 为空串 —— 折叠条会省掉时间这个片段,而不是编一个。
 */
export function buildProvenance(
  summary: DoneSummary | null | undefined,
  at?: string,
): ProvenanceView {
  const s = summary ?? {}
  const ev: ExecutionEvidence = s.execution_evidence ?? {}
  const report = (s.masking_applied ?? null) as MaskingReport | null
  const badge = maskingBadge(report)
  const p: PrincipalWire = s.principal ?? {}
  const evidence: ConfidenceEvidenceItem[] = s.confidence_evidence ?? []
  const runId = String(s.run_id ?? '')
  return {
    datasource: String(s.datasource ?? ''),
    time: fmtStamp(at),
    elapsed: fmtDuration(s.total_elapsed_ms),
    model: String(s.model ?? ''),
    runId,
    runShort: runId ? runId.slice(0, 8) : '',
    source: String(s.answer_source ?? ''),
    confidence: pct(s.confidence),
    sqlConfidence: pct(s.sql_confidence),
    confidenceWhy: evidence.map((e) => String(e.why ?? '')).filter(Boolean),
    // as_of_basis 三态:'' = 没查过 → 整行不渲染;值 = 有依据;'unknown' =
    // 查过但未知 → 只报数据截止本身,不报依据。
    asOf: typeof ev.data_as_of === 'string' ? ev.data_as_of : '',
    asOfBasis: String(ev.as_of_basis ?? ''),
    kbHits: (s.kb_hits ?? []).map(hitView).filter((h) => h.label),
    verdict: String(s.verdict ?? ''),
    mask: badge,
    maskedFields: Object.keys(report?.fields ?? {}),
    principalSubject: String(p.subject ?? ''),
    principalRole: String(p.role ?? ''),
    onBehalfOf: String(p.on_behalf_of ?? ''),
  }
}

/** 折叠条的三枚状态 chip(KB 命中 / 校验 / 脱敏),没有的就不出现。 */
export function buildChips(view: ProvenanceView): ProvChip[] {
  const chips: ProvChip[] = []
  if (view.kbHits.length) {
    chips.push({ id: 'kb', n: view.kbHits.length, tone: 'ok' })
  }
  if (view.verdict) {
    // OK = 通过(绿);其余(EMPTY / RETRY: … / NO_SQL / 认不出的原文)一律
    // 按"有话说"渲染 —— 认不出≠通过。
    chips.push({ id: 'verify', tone: view.verdict === 'OK' ? 'ok' : 'warn' })
  }
  if (view.mask) {
    chips.push({
      id: 'mask',
      n: view.mask.kind === 'bypass' ? undefined : view.mask.count,
      tone: 'warn',
      titleKey: view.mask.kind === 'bypass' ? 'bypassBadgeTip' : 'maskedColTip',
    })
  }
  return chips
}
