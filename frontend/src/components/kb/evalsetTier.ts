/**
 * 验收集 tier 徽标 —— 一档一个 i18n 键 + 一个 class。
 *
 * 只认四个**已声明**的分层:`verified / semantic / synthetic / benchmark`
 * (与后端 ``services.evalset.models.TIER_ORDER`` 同源)。未知 tier **原文
 * 显示**并给 ``tier-unknown`` 灰样式 —— 猜一个颜色会把「后端新增了分层」
 * 渲染成「和某个已知分层一样」,读端就再也发现不了口径变了。
 */

export type EvalsetTier = 'verified' | 'semantic' | 'synthetic' | 'benchmark' | 'unknown'

const KNOWN: Record<string, { key: string; className: string }> = {
  verified: { key: 'kbEvalsetTierVerified', className: 'tier-verified' },
  semantic: { key: 'kbEvalsetTierSemantic', className: 'tier-semantic' },
  synthetic: { key: 'kbEvalsetTierSynthetic', className: 'tier-synthetic' },
  benchmark: { key: 'kbEvalsetTierBenchmark', className: 'tier-benchmark' },
}

export function tierBadge(
  tier: string,
  t: (k: string) => string,
): { label: string; className: string } {
  const known = KNOWN[tier]
  if (known) return { label: t(known.key), className: known.className }
  return { label: tier, className: 'tier-unknown' }
}
