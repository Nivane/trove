<!--
  HealthBanner — the overview's first sentence (设计稿 §2:横幅是状态,不是装饰).

  Three tones map straight onto the endpoint's semantics:
    ok          storage round-trip ✓ and every datasource SELECT 1 ✓
    degraded    at least one datasource unreachable — **or the probe leg
                itself failed** (``datasources: null``), in which case
                reachability is unknown: not "no sources", not "all
                reachable", and never rendered as ok
  ``unavailable`` never reaches this component — the page renders the
  whole-page error face for it instead (storage down = no partial results).
  ``probeError`` carries the failed probe leg's error type name so the
  reason is shown, not hidden.

  The facts line mirrors /v1/health's discipline: errors are type names
  only, LLM reports facts (mock / target / providers) and no verdict, and
  the read-only probe is shown as verified / not-probed / unverifiable —
  never upgraded to "safe".
-->
<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { CircleCheck, TriangleAlert } from 'lucide-vue-next'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import type { OverviewHealth } from '../../api/overview'
import { useReadOnly } from '../../composables/useReadOnly'

const { readOnly } = useReadOnly()

const props = defineProps<{ health: OverviewHealth; probeError?: string }>()

const ui = useUiStore()

const entries = computed(() => Object.entries(props.health.datasources ?? {}))
const total = computed(() => entries.value.length)
const unreachable = computed(() =>
  entries.value.filter(([, v]) => !v.ok).map(([name, v]) => ({ name, error: v.error || '' })),
)
const reachable = computed(() => total.value - unreachable.value.length)
const verified = computed(
  () => entries.value.filter(([, v]) => v.readonly?.verified === true).length,
)

/** 探测腿本身失败(``datasources: null``)≠「没有源」,也 ≠「全部可达」。 */
const probeUnknown = computed(() => props.health.datasources === null)

/** tone 认服务端的判定(status),不靠条目数反推 —— 条目为 0 的 degraded
 *  只有一种来源:探测腿失败,那时说绿就是把「不知道」说成「没问题」。 */
const tone = computed(() =>
  props.health.status !== 'ok' || unreachable.value.length ? 'degraded' : 'ok',
)

const title = computed(() => {
  if (probeUnknown.value) return t('ovHealthProbeUnknown', ui.lang)
  if (unreachable.value.length) {
    return t('ovHealthDegraded', ui.lang, unreachable.value.length)
  }
  return t('ovHealthOk', ui.lang, total.value)
})

/** Facts line parts; each is a small key/value pair rendered as one run-on line. */
const facts = computed(() => {
  const h = props.health
  const out: { key: string; text: string; bad?: boolean }[] = []
  out.push({
    key: t('ovHealthStorage', ui.lang),
    text: h.storage.ok ? '✓' : `✕ ${h.storage.error || ''}`.trim(),
    bad: !h.storage.ok,
  })
  const llmBits = [h.llm.target || '—']
  if (h.llm.providers) llmBits.push(t('ovHealthLlmProviders', ui.lang, h.llm.providers))
  if (h.llm.mock) llmBits.push('mock')
  out.push({ key: t('ovHealthLlm', ui.lang), text: llmBits.join(' · '), bad: h.llm.mock })
  if (probeUnknown.value) {
    // 可达性未知:给原因(类型名),不给数字 —— 数字会假装探过
    out.push({
      key: t('ovHealthProbe', ui.lang),
      text: props.probeError ? `✕ ${props.probeError}` : '—',
      bad: Boolean(props.probeError),
    })
  } else {
    out.push({
      key: t('ovDsReachable', ui.lang),
      text: `${reachable.value}/${total.value}`,
      bad: unreachable.value.length > 0,
    })
    out.push({
      key: t('ovReadonlyProbe', ui.lang),
      text: t('ovReadonlyVerified', ui.lang, verified.value),
    })
  }
  return out
})
</script>

<template>
  <section class="health-banner" :class="`is-${tone}`" role="status">
    <span class="hb-dot" aria-hidden="true">
      <TriangleAlert v-if="tone === 'degraded'" :size="15" />
      <CircleCheck v-else :size="15" />
    </span>

    <div class="hb-main">
      <h2 class="hb-title">{{ title }}</h2>
      <p class="hb-facts">
        <template v-for="(f, i) in facts" :key="f.key">
          <span v-if="i" class="hb-sep" aria-hidden="true">·</span>
          <span class="hb-fact">
            <span class="hb-fact-key">{{ f.key }}</span>
            <span class="hb-fact-val" :class="{ 'is-bad': f.bad }">{{ f.text }}</span>
          </span>
        </template>
      </p>
      <ul v-if="unreachable.length" class="hb-bad-list">
        <li v-for="d in unreachable" :key="d.name">
          <span class="hb-bad-name">{{ d.name }}</span>
          <span v-if="d.error" class="hb-bad-err">{{ d.error }}</span>
        </li>
      </ul>
    </div>

    <RouterLink v-if="!readOnly" class="hb-cta" to="/admin/datasources">
      {{ t('ovGoDatasources', ui.lang) }}
    </RouterLink>
  </section>
</template>

<style scoped>
.health-banner {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--ok);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}
.health-banner.is-degraded {
  border-left-color: var(--warn);
}

.hb-dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  flex: none;
  border-radius: var(--r-full);
  background: var(--ok-bg);
  color: var(--ok);
}
.is-degraded .hb-dot {
  background: var(--warn-bg);
  color: var(--warn);
}

.hb-main {
  flex: 1;
  min-width: 0;
}

.hb-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}

.hb-facts {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  line-height: var(--lh-relaxed);
}
.hb-sep {
  margin: 0 var(--sp-1);
  color: var(--text-tertiary);
}
.hb-fact-key {
  margin-right: 4px;
  color: var(--text-tertiary);
}
.hb-fact-val {
  font-variant-numeric: tabular-nums;
}
.hb-fact-val.is-bad {
  color: var(--danger);
}

.hb-bad-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-3);
  margin: var(--sp-2) 0 0;
  padding: 0;
  list-style: none;
  font-size: var(--fs-2xs);
}
.hb-bad-name {
  font-weight: 600;
  color: var(--danger);
}
.hb-bad-err {
  margin-left: var(--sp-1);
  padding: 0 var(--sp-1);
  border-radius: var(--r-sm);
  background: var(--danger-bg);
  color: var(--danger);
  font-family: var(--font-mono);
}

.hb-cta {
  flex: none;
  align-self: center;
  height: 28px;
  display: inline-flex;
  align-items: center;
  padding: 0 var(--sp-3);
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-2xs);
  font-weight: 500;
  text-decoration: none;
}
.hb-cta:hover {
  border-color: var(--border-strong);
  background: var(--surface-hover);
}
.hb-cta:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
</style>
