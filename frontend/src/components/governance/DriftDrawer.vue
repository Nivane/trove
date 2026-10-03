<!--
  DriftDrawer — 一条漂移的详情(§2.5)。

  影响面区块按 §6-D 渲染:检测器今天不采集 impact(`services/drift/service.py`
  不传回调),所以「四个组全空」是**事实**,渲染成一句「暂无影响面快照」,
  不画一张空表 —— 空表和「没有影响」在视觉上必须能分开。

  自取数(单条),打开时才请求。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import DetailDrawer from '../base/DetailDrawer.vue'
import StatePanel from '../base/StatePanel.vue'
import { fetchDriftDetail } from '../../api/governance'
import type { GovernanceDriftDetail } from '../../api/governance'
import type { GovernanceDriftItem } from '../../api/types'
import { t } from '../../i18n'
import { useReadOnly } from '../../composables/useReadOnly'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'

const { readOnly } = useReadOnly()

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    ds: string
    driftId: number | null
  }>(),
  {},
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'resolve', item: GovernanceDriftItem): void
  (e: 'waive', item: GovernanceDriftItem): void
}>()

const ui = useUiStore()
const loading = ref(false)
const error = ref('')
const detail = ref<GovernanceDriftDetail | null>(null)

async function load() {
  if (props.driftId === null) return
  loading.value = true
  error.value = ''
  try {
    detail.value = await fetchDriftDetail(props.ds, props.driftId)
  } catch (e) {
    detail.value = null
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.driftId] as const,
  ([open]) => {
    if (open) void load()
  },
  { immediate: true },
)

function fmtDetail(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

/** 影响面四组,只画非空的(空组是事实,画出来反而像「没有」。) */
const impactGroups = computed(() => {
  const impact = detail.value?.impact
  if (!impact) return []
  return (
    [
      ['metrics', impact.metrics ?? []],
      ['examples', impact.examples ?? []],
      ['rules', impact.rules ?? []],
      ['lessons', impact.lessons ?? []],
    ] as [string, string[]][]
  ).filter(([, items]) => items.length > 0)
})

const impactEmpty = computed(
  () => detail.value !== null && impactGroups.value.length === 0,
)
</script>

<template>
  <DetailDrawer
    :model-value="modelValue"
    :title="t('govDriftDetailTitle', ui.lang)"
    width="540px"
    :close-label="t('close', ui.lang)"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <StatePanel v-if="loading" mode="loading" :title="t('govLoading', ui.lang)" />
    <StatePanel
      v-else-if="error || !detail"
      mode="error"
      :title="t('govDriftError', ui.lang)"
      :detail="error"
      :retry-text="t('retry', ui.lang)"
      @retry="load"
    />
    <div v-else class="drift-drawer">
      <dl class="dd-fields">
        <dt>{{ t('govDriftColLevel', ui.lang) }}</dt>
        <dd class="mono">{{ detail.drift.level }}</dd>
        <dt>{{ t('govDriftDetailFieldKind', ui.lang) }}</dt>
        <dd class="mono">{{ detail.drift.kind }}</dd>
        <dt>{{ t('govDriftDetailFieldSubject', ui.lang) }}</dt>
        <dd class="mono">{{ detail.drift.subject }}</dd>
        <dt>{{ t('govDriftColSeverity', ui.lang) }}</dt>
        <dd>{{ detail.drift.severity }}</dd>
        <dt>{{ t('govDriftColStatus', ui.lang) }}</dt>
        <dd>{{ detail.drift.status }}</dd>
        <dt>{{ t('govInboxSource', ui.lang) }}</dt>
        <dd class="mono">{{ detail.drift.source }}</dd>
        <dt>first_seen</dt>
        <dd>{{ fmtDateTime(detail.drift.first_seen_at ?? undefined) || '—' }}</dd>
        <dt>seen_count</dt>
        <dd>{{ detail.drift.seen_count ?? '—' }}</dd>
        <template v-if="detail.drift.resolved_at">
          <dt>{{ t('govDriftResolver', ui.lang) }}</dt>
          <dd>{{ detail.drift.resolved_by || '—' }}</dd>
          <dt>{{ t('govDriftReasonShort', ui.lang) }}</dt>
          <dd>{{ detail.drift.resolve_reason || '—' }}</dd>
        </template>
      </dl>

      <section class="dd-block">
        <h4 class="dd-title">{{ t('govDriftImpact', ui.lang) }}</h4>
        <p v-if="impactEmpty" class="dd-empty">
          {{ t('govDriftImpactEmpty', ui.lang) }}
        </p>
        <div v-for="[group, items] in impactGroups" :key="group" class="dd-impact-group">
          <span class="dd-impact-label">{{ group }}</span>
          <span v-for="it in items" :key="it" class="dd-impact-item">{{ it }}</span>
        </div>
      </section>

      <section class="dd-block">
        <h4 class="dd-title">detail</h4>
        <pre class="dd-pre">{{ fmtDetail(detail.drift.detail) }}</pre>
      </section>

      <p class="dd-audit">{{ t('govDriftAuditNote', ui.lang) }}</p>

      <div v-if="!readOnly && detail.drift.status === 'open'" class="dd-actions">
        <button
          type="button"
          class="act-btn is-primary"
          @click="emit('resolve', detail.drift)"
        >
          {{ t('govDriftResolve', ui.lang) }}
        </button>
        <button type="button" class="act-btn" @click="emit('waive', detail.drift)">
          {{ t('govDriftWaive', ui.lang) }}
        </button>
      </div>
    </div>
  </DetailDrawer>
</template>

<style scoped>
.drift-drawer {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  font-size: var(--fs-xs);
}
.dd-fields {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 3px 12px;
  margin: 0;
}
.dd-fields dt {
  color: var(--text-tertiary);
}
.dd-fields dd {
  margin: 0;
  word-break: break-word;
}
.mono {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-2xs);
}
.dd-title {
  margin: 0 0 2px;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
  font-weight: 600;
}
.dd-empty {
  margin: 0;
  color: var(--text-tertiary);
}
.dd-impact-group {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
  margin-top: 2px;
}
.dd-impact-label {
  color: var(--text-tertiary);
}
.dd-impact-item {
  padding: 0 5px;
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
}
.dd-pre {
  margin: 0;
  padding: var(--sp-2);
  background: var(--surface-sunken);
  border-radius: var(--r-sm);
  overflow: auto;
  font-size: var(--fs-2xs);
  max-height: 240px;
}
.dd-audit {
  margin: 0;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.dd-actions {
  display: flex;
  gap: 8px;
}
.act-btn {
  padding: 2px 10px;
  border-radius: var(--r-sm);
  border: 1px solid var(--border-default);
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  cursor: pointer;
}
.act-btn.is-primary {
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}
</style>
