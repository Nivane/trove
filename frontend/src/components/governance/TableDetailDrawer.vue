<!--
  TableDetailDrawer — Tab4 的表详情(§2.6,规则四:与用户端表详情同源同款)。

  四块内容来自四个既有只读端点(catalog 详情 / DDL / KB notes / lineage),
  各自独立降级:缺一块只隐藏那一块,不整块报错(§3.3 血缘行)。
  冷启动(表从未被查过)是**真实空态**:query_log.count 为 0 时文案是
  「还没有查询历史」,不是「无依赖」—— 没记录 ≠ 没依赖。

  自取数;URL 里 table 非空即打开(§3.2),由 GovernanceView 接线。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import DetailDrawer from '../base/DetailDrawer.vue'
import StatePanel from '../base/StatePanel.vue'
import {
  fetchCatalogDdl,
  fetchCatalogTable,
  fetchKbTableNotes,
  fetchTableLineage,
} from '../../api/governance'
import type { CatalogTableDetail, KbTableNotes } from '../../api/governance'
import type { GovernanceLineageEdge, GovernanceTableLineage } from '../../api/types'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    table: string
    ds: string
  }>(),
  {},
)

const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const ui = useUiStore()

const loading = ref(false)
const error = ref('')
const detail = ref<CatalogTableDetail | null>(null)
const ddl = ref('')
const ddlError = ref('')
const notes = ref<KbTableNotes | null>(null)
const lineage = ref<GovernanceTableLineage | null>(null)

async function load() {
  if (!props.table || !props.ds) return
  loading.value = true
  error.value = ''
  ddlError.value = ''
  detail.value = null
  notes.value = null
  lineage.value = null
  try {
    detail.value = await fetchCatalogTable(props.table, props.ds)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    loading.value = false
    return
  }
  loading.value = false

  // 三块带子彼此独立:各自的失败只让自己缺席。
  const [ddlRes, notesRes, linRes] = await Promise.allSettled([
    fetchCatalogDdl(props.table, props.ds),
    fetchKbTableNotes(props.table, props.ds),
    fetchTableLineage(props.table, { datasource: props.ds }),
  ])
  if (ddlRes.status === 'fulfilled') ddl.value = ddlRes.value
  else ddlError.value = ddlRes.reason instanceof Error ? ddlRes.reason.message : String(ddlRes.reason)
  if (notesRes.status === 'fulfilled') notes.value = notesRes.value
  if (linRes.status === 'fulfilled') lineage.value = linRes.value
}

watch(
  () => [props.modelValue, props.table, props.ds] as const,
  ([open]) => {
    if (open) void load()
  },
  { immediate: true },
)

function colComment(name: string): string {
  return notes.value?.columns?.[name] || '—'
}

/** 样例行:stats[col] 的 sample / top_values(profiling 写了才有)。 */
const sampleRows = computed(() => {
  const stats = notes.value?.stats ?? {}
  const rows: { col: string; text: string }[] = []
  for (const [col, s] of Object.entries(stats)) {
    const sample = Array.isArray(s?.sample) ? (s.sample as unknown[]).map(String) : []
    const top = Array.isArray(s?.top_values)
      ? (s.top_values as unknown[]).map((v) =>
          Array.isArray(v) ? `${String(v[0])} (${String(v[1])})` : String(v),
        )
      : []
    const text = [...sample, ...top].join(' · ')
    if (text) rows.push({ col, text })
  }
  return rows
})

function edgeName(e: GovernanceLineageEdge): string {
  const kind = String(e?.kind ?? '')
  const label =
    kind === 'create_view'
      ? t('govLinKindView', ui.lang)
      : kind === 'create_table_as'
        ? t('govLinKindCtas', ui.lang)
        : kind === 'query'
          ? t('govLinKindQuery', ui.lang)
          : kind
  const name = e?.name ? e.name : kind === 'query' ? '' : t('govLinEdgeNoName', ui.lang)
  return name ? `${name}(${label})` : label
}

const historyCount = computed(() => lineage.value?.query_log.count ?? 0)
</script>

<template>
  <DetailDrawer
    :model-value="modelValue"
    :title="table ? `${ds}.${table}` : t('govLinDrawerTitle', ui.lang)"
    width="620px"
    :close-label="t('close', ui.lang)"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <StatePanel
      v-if="!ds"
      mode="empty"
      :title="t('govLinDrawerTitle', ui.lang)"
      :description="t('govCovDs', ui.lang)"
    />
    <StatePanel v-else-if="loading" mode="loading" :title="t('govLoading', ui.lang)" />
    <StatePanel
      v-else-if="error || !detail"
      mode="error"
      :title="t('govLinError', ui.lang)"
      :detail="error"
      :retry-text="t('retry', ui.lang)"
      @retry="load"
    />
    <div v-else class="tbl-drawer">
      <p class="td-meta">
        {{ t('govLinFieldCount', ui.lang, detail.columns.length) }}
        <span v-if="detail.schema" class="mono">· {{ detail.schema }}</span>
        <span v-if="detail.row_count != null" class="mono">
          · {{ t('govLinColRows', ui.lang) }} ≈ {{ detail.row_count }}
        </span>
      </p>

      <section class="td-block">
        <h4 class="td-title">{{ t('govLinFields', ui.lang) }}</h4>
        <table class="td-table">
          <thead>
            <tr>
              <th>{{ t('govLinFieldCol', ui.lang) }}</th>
              <th>{{ t('govLinTypeCol', ui.lang) }}</th>
              <th>{{ t('govLinCommentCol', ui.lang) }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in detail.columns" :key="c.name">
              <td class="mono">
                {{ c.name }}
                <span v-if="c.primary_key" class="td-chip">PK</span>
                <span v-else-if="c.foreign_key" class="td-chip">FK</span>
              </td>
              <td class="mono">{{ c.type }}</td>
              <td>{{ colComment(c.name) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="td-block">
        <h4 class="td-title">{{ t('govLinSamples', ui.lang) }}</h4>
        <p v-if="sampleRows.length === 0" class="td-muted">{{ t('govLinSamplesEmpty', ui.lang) }}</p>
        <div v-for="s in sampleRows" :key="s.col" class="td-sample">
          <span class="mono td-sample-col">{{ s.col }}</span>
          <span class="td-sample-vals">{{ s.text }}</span>
        </div>
      </section>

      <section class="td-block">
        <h4 class="td-title">{{ t('govLinDdl', ui.lang) }}</h4>
        <pre v-if="ddl" class="td-pre">{{ ddl }}</pre>
        <p v-else-if="ddlError" class="td-muted">{{ ddlError }}</p>
      </section>

      <section class="td-block">
        <h4 class="td-title">{{ t('govLinDefs', ui.lang) }}</h4>
        <p v-if="!lineage || lineage.definitions.length === 0" class="td-muted">
          {{ t('govLinDefsEmpty', ui.lang) }}
        </p>
        <div v-for="(d, i) in lineage?.definitions ?? []" :key="i" class="td-def">
          <span class="td-chip">{{ d.kind === 'view' ? t('govLinKindView', ui.lang) : t('govLinKindCtas', ui.lang) }}</span>
          <pre class="td-pre">{{ d.sql }}</pre>
        </div>
      </section>

      <section class="td-block">
        <h4 class="td-title">{{ t('govLinHistory', ui.lang) }}</h4>
        <p v-if="historyCount === 0" class="td-muted">{{ t('govLinHistoryEmpty', ui.lang) }}</p>
        <template v-else>
          <p class="td-history">
            {{ t('govLinHistoryHits', ui.lang, historyCount) }}
            <span v-if="lineage?.query_log.last_at" class="mono">
              · {{ fmtDateTime(lineage.query_log.last_at) }}
            </span>
          </p>
          <div class="td-layers">
            <div class="td-layer">
              <span class="td-layer-label">{{ t('govLinUpstream', ui.lang) }}</span>
              <template v-if="(lineage?.upstream ?? []).length">
                <span v-for="(e, i) in lineage?.upstream ?? []" :key="i" class="td-edge">
                  {{ edgeName(e) }}
                </span>
              </template>
              <span v-else class="td-muted">{{ t('govLinNoEdge', ui.lang) }}</span>
            </div>
            <div class="td-layer">
              <span class="td-layer-label">{{ t('govLinDownstream', ui.lang) }}</span>
              <template v-if="(lineage?.downstream ?? []).length">
                <span v-for="(e, i) in lineage?.downstream ?? []" :key="i" class="td-edge">
                  {{ edgeName(e) }}
                </span>
              </template>
              <span v-else class="td-muted">{{ t('govLinNoEdge', ui.lang) }}</span>
            </div>
          </div>
        </template>
      </section>

      <section class="td-block">
        <h4 class="td-title">{{ t('govLinNotes', ui.lang) }}</h4>
        <p v-if="!notes || (!notes.description && Object.keys(notes.metrics ?? {}).length === 0)" class="td-muted">
          {{ t('govLinNotesEmpty', ui.lang) }}
        </p>
        <template v-else>
          <p v-if="notes?.description" class="td-desc">{{ notes.description }}</p>
          <div v-for="(def, name) in notes?.metrics ?? {}" :key="name" class="td-metric">
            <span class="mono">{{ name }}</span>
            <span class="td-muted">{{ def }}</span>
          </div>
        </template>
      </section>
    </div>
  </DetailDrawer>
</template>

<style scoped>
.tbl-drawer {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  font-size: var(--fs-xs);
}
.td-meta {
  margin: 0;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.td-title {
  margin: 0 0 3px;
  font-size: var(--fs-2xs);
  font-weight: 600;
  color: var(--text-tertiary);
}
.td-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-xs);
}
.td-table th {
  text-align: left;
  color: var(--text-tertiary);
  font-weight: 500;
  padding: 1px 10px 1px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.td-table td {
  padding: 2px 10px 2px 0;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: top;
}
.td-chip {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: var(--r-sm);
  border: 1px solid var(--border-subtle);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.td-muted {
  margin: 0;
  color: var(--text-tertiary);
}
.mono {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-2xs);
}
.td-sample {
  display: flex;
  gap: 8px;
  margin-top: 2px;
}
.td-sample-col {
  min-width: 90px;
  color: var(--text-tertiary);
}
.td-sample-vals {
  word-break: break-word;
  color: var(--text-secondary);
}
.td-pre {
  margin: 2px 0 0;
  padding: var(--sp-2);
  background: var(--surface-sunken);
  border-radius: var(--r-sm);
  font-size: var(--fs-2xs);
  overflow: auto;
  max-height: 180px;
}
.td-def {
  margin-top: 4px;
}
.td-history {
  margin: 0 0 4px;
  color: var(--text-secondary);
}
.td-layers {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.td-layer {
  display: flex;
  gap: 6px;
  align-items: baseline;
  flex-wrap: wrap;
}
.td-layer-label {
  color: var(--text-tertiary);
  min-width: 40px;
}
.td-edge {
  padding: 0 5px;
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
}
.td-desc {
  margin: 0 0 4px;
}
.td-metric {
  display: flex;
  gap: 8px;
  margin-top: 2px;
}
</style>
