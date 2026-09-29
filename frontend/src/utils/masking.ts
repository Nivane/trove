// 字段级脱敏的前端标记(设计 §7.2 / P6)。
//
// 后端把 `masking_applied = {fields: {字段名: 模式}, bypass}` 放进 done.summary
// (§6.2)。这里只做一件事:**把报告对到用户看到的那张表上** —— 数据已经被改写了
// (手机号变 138****8888、身份证变哈希),而改写本身是无声的,不说一句就会被读成
// 真实值。
//
// 为什么是**按名字对**而不是按列位置:展示用的表格来自模型写的 markdown(可能换
// 别名、调列序、只挑几列),与结果集的列位置不是一回事。名字对不上就不标 —— 宁可
// 少标一列,也不给一列没被改写的打上「已脱敏」(那会让用户以为看到了假数据)。

export interface MaskingReport {
  fields?: Record<string, string> | null
  bypass?: boolean
}

/** 字段名归一化:大小写、引号、下划线、空白都不算差别(`ID_Card` = `id card`)。 */
export function normalizeField(name: string): string {
  let text = String(name ?? '')
    .trim()
    .toLowerCase()
  const dot = text.lastIndexOf('.')
  if (dot >= 0) text = text.slice(dot + 1) // c.phone → phone
  return text.replace(/[^\p{L}\p{N}]+/gu, '')
}

//: 同名字段取最严模式 —— 与后端 `masking.STRICTNESS` 同序(同一份语义,
//: 但两处各写一份:前端不 import 后端代码)。
const STRICTNESS: Record<string, number> = { partial: 1, hash: 2, null: 3 }

/** 列下标 → 模式。只含**名字对得上**的列;对不上的不猜。 */
export function maskedColumnModes(
  headers: string[],
  report: MaskingReport | null | undefined,
): Record<number, string> {
  const byKey = new Map<string, string>()
  for (const [name, mode] of Object.entries(report?.fields || {})) {
    const key = normalizeField(name)
    if (!key) continue
    const previous = byKey.get(key)
    if (previous === undefined || (STRICTNESS[mode] ?? 0) > (STRICTNESS[previous] ?? 0)) {
      byKey.set(key, mode)
    }
  }
  const out: Record<number, string> = {}
  headers.forEach((header, index) => {
    const key = normalizeField(header)
    if (key && byKey.has(key)) out[index] = byKey.get(key)!
  })
  return out
}

/** 答案上方那一枚提示;不需要提示时返回 null。
 *
 * 三态要分清(与 §6.2 的三态一致):`null` = 这一步没跑(该部署没配脱敏),
 * `{}` = 跑了但没改动,两者都不提示;`bypass` = **以原文返回** —— 它 `fields`
 * 是空的,却恰恰是最该说一句的那次。 */
export function maskingBadge(
  report: MaskingReport | null | undefined,
): { kind: 'masked' | 'bypass'; count: number } | null {
  if (!report) return null
  const count = Object.keys(report.fields || {}).length
  if (report.bypass) return { kind: 'bypass', count }
  return count > 0 ? { kind: 'masked', count } : null
}
