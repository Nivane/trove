<!--
  OnboardingWizard — 接入向导 (设计稿 §2:三步都读既有状态,不新增任何东西).

  Registered → KB initialized → users authorized. Each step's stat comes
  straight from the aggregate's `wizard` block, and a step that cannot be
  counted shows `—` rather than a reassuring 0 (same honesty rule as the
  KPI row). Steps that are already satisfied turn into a "done" state
  instead of disappearing, so the trail stays visible.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { Check } from 'lucide-vue-next'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import type { OverviewWizard } from '../../api/overview'

type MsgKey = keyof typeof import('../../i18n').messages['zh']

const props = defineProps<{ wizard: OverviewWizard | null }>()

const ui = useUiStore()

interface WizardStep {
  key: string
  idx: number
  titleKey: MsgKey
  descKey: MsgKey
  unitKey: MsgKey
  ctaKey: MsgKey
  to: string
  stat: string
  done: boolean
}

function num(v: number | null | undefined): string {
  return v == null ? '—' : String(v)
}

const steps = computed<WizardStep[]>(() => {
  const w = props.wizard
  const registered = w?.registered ?? null
  const kbInit = w?.kb_initialized ?? null
  const noGrant = w?.users_without_grant ?? null
  return [
    {
      key: 'register',
      idx: 1,
      titleKey: 'ovWizStep1',
      descKey: 'ovWizStep1Desc',
      unitKey: 'ovWizStep1Unit',
      ctaKey: 'ovWizGoRegister',
      to: '/admin/datasources',
      stat: num(registered),
      done: registered !== null && registered > 0,
    },
    {
      key: 'kb',
      idx: 2,
      titleKey: 'ovWizStep2',
      descKey: 'ovWizStep2Desc',
      unitKey: 'ovWizStep2Unit',
      ctaKey: 'ovWizGoInit',
      to: '/admin/datasources',
      stat:
        registered === null || kbInit === null ? '—' : `${kbInit} / ${registered}`,
      done: registered !== null && registered > 0 && kbInit === registered,
    },
    {
      key: 'grant',
      idx: 3,
      titleKey: 'ovWizStep3',
      descKey: 'ovWizStep3Desc',
      unitKey: 'ovWizStep3Unit',
      ctaKey: 'ovWizGoGrant',
      to: '/admin/users?status=nogrant',
      stat: num(noGrant),
      done: noGrant === 0,
    },
  ]
})
</script>

<template>
  <ol class="wiz-list">
    <li
      v-for="s in steps"
      :key="s.key"
      class="wiz-step"
      :class="{ 'is-done': s.done, 'is-unknown': s.stat === '—' }"
    >
      <span class="wiz-no" aria-hidden="true">
        <Check v-if="s.done" :size="12" />
        <template v-else>{{ s.idx }}</template>
      </span>
      <div class="wiz-main">
        <div class="wiz-head">
          <span class="wiz-title">{{ t(s.titleKey, ui.lang) }}</span>
          <span class="wiz-stat">{{ s.stat }}</span>
          <span class="wiz-unit">{{ t(s.unitKey, ui.lang) }}</span>
        </div>
        <p class="wiz-desc">{{ t(s.descKey, ui.lang) }}</p>
      </div>
      <RouterLink v-if="!s.done" class="wiz-cta" :to="s.to">
        {{ t(s.ctaKey, ui.lang) }}
      </RouterLink>
      <span v-else class="wiz-done">{{ t('ovWizDone', ui.lang) }}</span>
    </li>
  </ol>
</template>

<style scoped>
.wiz-list {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
  counter-reset: none;
}

.wiz-step {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  padding: var(--sp-3) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.wiz-step:last-child {
  border-bottom: none;
}
.wiz-step:first-child {
  padding-top: 0;
}

.wiz-no {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex: none;
  margin-top: 2px;
  border-radius: var(--r-full);
  border: 1px solid var(--border-default);
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  font-variant-numeric: tabular-nums;
}
.wiz-step.is-done .wiz-no {
  border-color: var(--ok);
  background: var(--ok-bg);
  color: var(--ok);
}

.wiz-main {
  flex: 1;
  min-width: 0;
}

.wiz-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.wiz-title {
  font-size: var(--fs-xs);
  font-weight: 500;
  color: var(--text-primary);
}

.wiz-stat {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.wiz-step.is-unknown .wiz-stat {
  color: var(--text-tertiary);
}

.wiz-unit {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.wiz-desc {
  margin: 2px 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  line-height: var(--lh-relaxed);
}

.wiz-cta {
  flex: none;
  align-self: center;
  height: 26px;
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
.wiz-cta:hover {
  border-color: var(--border-strong);
  background: var(--surface-hover);
}
.wiz-cta:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}

.wiz-done {
  flex: none;
  align-self: center;
  font-size: var(--fs-2xs);
  color: var(--ok);
}
</style>
