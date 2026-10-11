/**
 * 行动提案对象引用(entity_ref)的展示口径 —— **单一处**,行动台三处
 * (列表行 / 详情抽屉 / 批量审批预览)共用(三处各写一遍迟早漂移)。
 *
 * 契约(文档三 §5.6):``evidence_refs["entity_ref"] =
 * {"type": <实体显示名>, "keys": {<键字段>: <值>}}``,创建时冻结。
 * 四条纪律:
 *
 *   · 形状守卫:**解析不了就不算数** —— 坏形状不装作对象块,原样留在
 *     扁平证据文本里可见(宁缺不猜,更不吞);
 *   · 前端不重算、不补算:没有这个键就没有对象块(键缺席 → 现状);
 *   · 键字段名原样出现(不编业务标签):第一个键值跟在实体名后,其余
 *     键以（字段：值）括注 —— 机器能确定的只有原始键名;
 *   · 深浅拷贝一个规范化对象:展示层不改动冻结的那份证据。
 */

import type { Lang } from '../i18n'
import type { EntityRefInfo } from '../api/types'

/** 形状守卫:type 是非空字符串、keys 是非空键值对象 → 规范化引用;
 *  其余(键缺席/空 keys/值全空/类型不对)一律 null,回退现状扁平文本。 */
export function parseEntityRef(value: unknown): EntityRefInfo | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  const type = typeof raw.type === 'string' ? raw.type.trim() : ''
  const keysRaw = raw.keys
  if (!type || !keysRaw || typeof keysRaw !== 'object' || Array.isArray(keysRaw)) {
    return null
  }
  const keys: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(keysRaw as Record<string, unknown>)) {
    if (v === null || v === undefined || v === '') continue
    keys[k] = v
  }
  if (!Object.keys(keys).length) return null
  return { type, keys }
}

/** 证据字典 → 已解析的对象引用(键缺席/坏形状 → null)。 */
export function entityRefOf(
  evidenceRefs: Record<string, unknown> | null | undefined,
): EntityRefInfo | null {
  return parseEntityRef(evidenceRefs?.entity_ref)
}

/** 对象块正文(标签「针对」由渲染处加):「贷款 L456（customer：张伟）」。
 *  第一个键值跟在实体名后,其余键按原始字段名括注;zh 全角、en 半角。 */
export function formatEntityRef(ref: EntityRefInfo, lang: Lang): string {
  const entries = Object.entries(ref.keys ?? {})
  const [first, ...rest] = entries
  const head = first
    ? `${ref.type ?? ''} ${String(first[1])}`.trim()
    : (ref.type ?? '')
  if (!rest.length) return head
  const [open, close] = lang === 'zh' ? ['（', '）'] : [' (', ')']
  const sep = lang === 'zh' ? '：' : ': '
  const inner = rest.map(([k, v]) => `${k}${sep}${String(v)}`).join(' · ')
  return `${head}${open}${inner}${close}`
}
