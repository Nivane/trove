/**
 * 语义变更评审的纯函数 —— 状态→色调、verdict→i18n 键、影响面摘要行。
 * 组件只做渲染：分类与文案键在这里，测试也钉在这里。
 */
import type { SemanticImpact } from '../api/types'

export function changeStatusTone(status: string): 'ok' | 'warn' | 'danger' | 'muted' {
  if (status === 'merged') return 'ok'
  if (status === 'open') return 'warn'
  if (status === 'stale') return 'danger'
  return 'muted'
}

const VERDICTS: Record<string, string> = {
  improves: 'semVerdictImproves',
  neutral: 'semVerdictNeutral',
  regresses: 'semVerdictRegresses',
  not_applicable: 'semVerdictNotApplicable',
}

export function verdictKey(verdict: string): string {
  return VERDICTS[verdict] ?? 'semVerdictUnknown'
}

/** 影响面的列表字段（`basis` 是映射不是列表，不能进这张表）。 */
type ImpactListKey = 'metrics' | 'rules' | 'topics' | 'examples' | 'lessons'

const KIND_KEYS: Array<[ImpactListKey, string]> = [
  ['metrics', 'semImpactMetric'],
  ['rules', 'semImpactRule'],
  ['topics', 'semImpactTopic'],
  ['examples', 'semImpactExample'],
  ['lessons', 'semImpactLesson'],
]

export function impactLines(
  impact: SemanticImpact,
  t: (key: string) => string,
): string[] {
  const lines: string[] = []
  for (const [field, key] of KIND_KEYS) {
    for (const name of impact[field] ?? []) {
      const basis = impact.basis?.[name]
      lines.push(`${t(key)}: ${name}${basis ? ` (${basis})` : ''}`)
    }
  }
  return lines
}
