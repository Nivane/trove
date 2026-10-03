/**
 * useNavBadges — 侧栏角标与顶栏健康点的唯一取数处。
 *
 * 一次 `GET /v1/admin/overview?window=24h` 喂两类展示（沿用 AdminLayout
 * 现有机制，无新端点）：
 *   · badges — badgeKey → { total, exact }；0 / 缺 / 降级 / 请求失败
 *     一律不显示（「把没取到画成 0」是这一屏最不该犯的错）。
 *   · health — overview.health.status（ok / degraded / unavailable），
 *     payload 里没有 health 就不显示；顶栏不发明数据。
 *
 * 每个角标都从既有字段确定性推导（todos.items / datasources / wizard），
 * 计数不精确时渲染「≥ N」。各页自身 total 就位后（W3）只需换这里的实现。
 */
import { ref, type Ref } from 'vue'
import {
  fetchOverview,
  type OverviewPayload,
  type OverviewTodoKind,
} from '../api/overview'
import type { BadgeKey } from '../components/layout/navModel'

export type ConsoleHealth = 'ok' | 'degraded' | 'unavailable'

export interface BadgeCount {
  total: number
  /** overview 的 count_exact —— false 时渲染「≥ N」。 */
  exact: boolean
}

/** 治理收件箱（跨源 pending 聚合）= /v1/admin/todos 的 pending 类目。 */
const PENDING_KINDS: OverviewTodoKind[] = [
  'kb_lesson',
  'kb_example',
  'semantic_draft',
  'skill_draft',
  'memory_preference',
]

function countOf(
  payload: OverviewPayload,
  kind: OverviewTodoKind,
): BadgeCount | undefined {
  const item = payload.todos?.items?.find((i) => i.kind === kind)
  // 来源未装配 / 降级（available=false）或没数出来（count=null）→ 不显示。
  if (!item || !item.available || item.count == null) return undefined
  return { total: item.count, exact: item.count_exact }
}

function sumOf(
  payload: OverviewPayload,
  kinds: OverviewTodoKind[],
): BadgeCount | undefined {
  let total = 0
  let exact = true
  let any = false
  for (const kind of kinds) {
    const count = countOf(payload, kind)
    if (!count) continue
    total += count.total
    exact = exact && count.exact
    any = true
  }
  return any ? { total, exact } : undefined
}

/** 纯函数：payload → 角标表（可单测，不发请求）。 */
export function badgesFromPayload(
  payload: OverviewPayload,
): Partial<Record<BadgeKey, BadgeCount>> {
  const badges: Partial<Record<BadgeKey, BadgeCount>> = {}
  if (payload.todos) {
    badges.todos = {
      total: payload.todos.total,
      exact: payload.todos.count_exact,
    }
  }
  const govPending = sumOf(payload, PENDING_KINDS)
  if (govPending) badges.govPending = govPending
  const failedJobs = countOf(payload, 'job_failed')
  if (failedJobs) badges.failedJobs = failedJobs
  const kbPending = sumOf(payload, ['kb_lesson', 'kb_example'])
  if (kbPending) badges.kbPending = kbPending
  const semPending = countOf(payload, 'semantic_draft')
  if (semPending) badges.semPending = semPending
  const skillDrafts = countOf(payload, 'skill_draft')
  if (skillDrafts) badges.skillDrafts = skillDrafts
  const nogrant = payload.wizard?.users_without_grant
  if (nogrant != null) badges.nogrant = { total: nogrant, exact: true }
  // 数据源角标是页面自己数的：未初始化 KB 或存在未闭环漂移。
  if (payload.datasources) {
    const issues = payload.datasources.filter(
      (ds) => ds.kb_initialized === false || (ds.drift_open ?? 0) > 0,
    ).length
    badges.dsIssues = { total: issues, exact: true }
  }
  return badges
}

/** 角标文案：0 不显示；不精确时 ≥ N。 */
export function formatBadge(count: BadgeCount | undefined): string | null {
  if (!count || count.total <= 0) return null
  return count.exact ? String(count.total) : `≥ ${count.total}`
}

export interface UseNavBadges {
  badges: Ref<Partial<Record<BadgeKey, BadgeCount>>>
  health: Ref<ConsoleHealth | null>
  load: () => Promise<void>
}

export function useNavBadges(): UseNavBadges {
  const badges = ref<Partial<Record<BadgeKey, BadgeCount>>>({})
  const health = ref<ConsoleHealth | null>(null)

  async function load() {
    try {
      const payload = await fetchOverview('24h')
      badges.value = badgesFromPayload(payload)
      health.value = payload.health?.status ?? null
    } catch {
      // 取不到就什么都不显示 —— 不把「没取到」画成 0，也不画成健康。
      badges.value = {}
      health.value = null
    }
  }

  return { badges, health, load }
}
