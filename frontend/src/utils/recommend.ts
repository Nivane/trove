/** 推荐问题 = KB 示例优先 + 最近会话首问兜底;去空去重,最多 max 条。 */
export function mergeRecommendations(
  examples: string[],
  sessions: { title?: string }[],
  max = 4,
): string[] {
  const recent = sessions
    .map((s) => s.title)
    .filter((t): t is string => !!t && !!t.trim())
  const seen = new Set<string>()
  const out: string[] = []
  for (const q of [...examples, ...recent]) {
    const key = q.trim().toLowerCase()
    if (!key || seen.has(key)) continue
    seen.add(key)
    out.push(q.trim())
    if (out.length >= max) break
  }
  return out
}
