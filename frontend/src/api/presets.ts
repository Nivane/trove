/**
 * 预设包(接入模板)的取数层 —— GET /v1/admin/presets、GET /{name}、
 * POST /{name}/apply。
 *
 * 一条贯穿全部形状的纪律:**套用产出只有草稿**。`apply` 的返回是逐条账本
 * (`items[]`),三种状态各有确切的含义,页面必须分开渲染 ——
 * `drafted`(落了 pending 草稿,还没生效)/ `skipped`(无需动作)/
 * `unresolved`(引用解析不到,什么都没落)。把 `unresolved` 渲染成成功、
 * 或把 `drafted` 渲染成「已生效」,都会让一次什么都没接上的套用看起来像
 * 接入完成 —— 这正是这套接口要防的那件事。
 *
 * `source` 双源:`builtin` 随代码分发(只读)、`org` 由管理端维护;同名时
 * 组织版遮蔽内置版(`shadowed: true` 的行是**没生效的那份**)。
 */

import { apiGet, apiPost } from './http'

export type PresetSource = 'builtin' | 'org'
export type PresetItemStatus = 'drafted' | 'skipped' | 'unresolved'

export interface PresetBrief {
  name: string
  version: number
  description: string
  author: string
  source: PresetSource
  path: string
  /** 各段条目数(skills / decisions / domains / semantics / presentation)。 */
  counts: Record<string, number>
  /** true = 该内置版被同名的组织版遮蔽(它不会生效)。 */
  shadowed: boolean
}

export interface PresetApplyItem {
  section: string
  item: string
  status: PresetItemStatus
  reason: string
}

export interface PresetApplyReport {
  preset: string
  datasource: string
  source: string
  counts: Record<string, number>
  items: PresetApplyItem[]
}

export async function fetchPresets(): Promise<PresetBrief[]> {
  const body = await apiGet<{ presets: PresetBrief[] }>('/admin/presets')
  return body.presets ?? []
}

export async function applyPreset(
  name: string,
  datasource: string,
): Promise<PresetApplyReport> {
  return apiPost<PresetApplyReport>(
    `/admin/presets/${encodeURIComponent(name)}/apply`,
    { datasource },
  )
}
