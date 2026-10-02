import { computed, reactive, watch, type ComputedRef } from 'vue'
import {
  useRoute,
  useRouter,
  type LocationQueryRaw,
  type RouteLocationNormalizedLoaded,
  type Router,
} from 'vue-router'

/**
 * useListQuery — list filter state that lives in the URL (P6 console rule:
 * state in the URL, not in the session), so a filtered list is shareable,
 * survives a refresh, and the back button undoes a filter change.
 *
 * `defaults` declares both the keys that sync and their "empty" values:
 * a key equal to its default (or to '') is never written to the query —
 * a clean list keeps a clean URL. Values are strings, like query params.
 *
 * Unrelated query params are left untouched; external navigation
 * (back/forward, a pasted link) flows back into `values`.
 *
 * ```ts
 * const { values, isActive, reset } = useListQuery({ q: '', role: '', status: '' })
 * ```
 */
export interface UseListQueryOptions {
  /** Injection seam for tests; defaults to the active router/route. */
  router?: Router
  route?: RouteLocationNormalizedLoaded
  /** 'replace' (default) keeps history clean; 'push' makes back undo filters. */
  mode?: 'replace' | 'push'
}

export interface UseListQuery<T extends Record<string, string>> {
  /** Filter state, writable (bind directly: v-model="values.q"). */
  values: T
  /** True when any filter differs from its default. */
  isActive: ComputedRef<boolean>
  /** Back to defaults — the params are dropped from the URL. */
  reset: () => void
}

function asString(raw: unknown, fallback: string): string {
  if (Array.isArray(raw)) return raw.length ? String(raw[raw.length - 1]) : fallback
  if (raw === null || raw === undefined) return fallback
  return String(raw)
}

/** True when the URL already carries our keys exactly as `values` wants them. */
function matches(
  query: RouteLocationNormalizedLoaded['query'],
  keys: string[],
  wanted: Record<string, string | undefined>,
): boolean {
  return keys.every((key) => asString(query[key], '') === (wanted[key] ?? ''))
}

export function useListQuery<T extends Record<string, string>>(
  defaults: T,
  options: UseListQueryOptions = {},
): UseListQuery<T> {
  const router = options.router ?? useRouter()
  const route = options.route ?? useRoute()
  const mode = options.mode ?? 'replace'
  const keys = Object.keys(defaults)

  // Writes go through the string-indexed view — T is only the typed face.
  const state = reactive({ ...defaults }) as Record<string, string>
  const values = state as unknown as T

  const wantedQuery = (): Record<string, string | undefined> => {
    const wanted: Record<string, string | undefined> = {}
    for (const key of keys) {
      const value = state[key]
      wanted[key] = value === '' || value === defaults[key] ? undefined : value
    }
    return wanted
  }

  // URL → state: first paint, back/forward, pasted links.
  watch(
    () => route.query,
    () => {
      for (const key of keys) {
        const next = asString(route.query[key], defaults[key])
        if (state[key] !== next) state[key] = next
      }
    },
    { immediate: true },
  )

  // State → URL. A no-op change never navigates (typing that lands on the
  // same query must not spam router.replace).
  watch(state, () => {
    const wanted = wantedQuery()
    if (matches(route.query, keys, wanted)) return
    const next: LocationQueryRaw = { ...route.query }
    for (const key of keys) {
      const value = wanted[key]
      if (value === undefined) delete next[key]
      else next[key] = value
    }
    // Best effort: an aborted/redundant navigation must never break the page.
    void Promise.resolve(router[mode]({ query: next, hash: route.hash })).catch(() => {})
  })

  const isActive = computed(() =>
    keys.some((key) => state[key] !== '' && state[key] !== defaults[key]),
  )

  function reset() {
    for (const key of keys) state[key] = defaults[key]
  }

  return { values, isActive, reset }
}
