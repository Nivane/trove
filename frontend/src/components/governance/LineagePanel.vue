<!--
  LineagePanel — Tab4 的表搜索(§2.6)。

  取数三档(全部是既有只读端点,零新语义):
    · ds + q  → GET /v1/catalog/search(服务端模糊匹配);
    · ds 单选 → GET /v1/catalog/tables(catalog 一览,字段数 + 估算行数);
    · 无 ds + q → 跨源扇出:挨个源各搜一次,单源失败只让它自己缺席,
      「搜到几行 + 哪几个源没答上来」同时说清(§4.4g 逐源降级)。

  冷启动(表从未被查过)仍是**真实空态**——空态文案不写「无依赖」。

  纯呈现 + 自取数;点行只向上 emit open,URL 由 GovernanceView 写。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import StatePanel from '../base/StatePanel.vue'
import { apiGet } from '../../api/http'
import { fetchCatalogTables, searchCatalogTables } from '../../api/governance'
import type { DatasourceInfo } from '../../api/types'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const props = withDefaults(
  defineProps<{
    q: string
    ds: string
  }>(),
  {},
)

const emit = defineEmits<{
  (e: 'open', payload: { name: string; ds: string }): void
}>()

const ui = useUiStore()

interface Row {
  name: string
  ds: string
  columns: number
  row_count?: number | null
}

const loading = ref(false)
const error = ref('')
const rows = ref<Row[]>([])
/** 跨源扇出时:哪几个源没答上来(附原因)。 */
const degraded = ref<{ ds: string; error: string }[]>([])
const searchedSources = ref(0)

const hasQuery = computed(() => props.q.trim().length > 0)
const promptOnly = computed(() => !props.ds && !hasQuery.value)

async function load() {
  degraded.value = []
  searchedSources.value = 0
  error.value = ''
  if (promptOnly.value) {
    rows.value = []
    return
  }
  loading.value = true
  try {
    if (props.ds) {
      const list = hasQuery.value
        ? await searchCatalogTables(props.q.trim(), props.ds)
        : await fetchCatalogTables(props.ds)
      rows.value = list.map((r) => ({ ...r, ds: props.ds }))
    } else {
      // 跨源:先列源,再逐个扇出;每个源独立 try。
      const info = (await apiGet<{ datasources: DatasourceInfo[] }>('/v1/admin/datasources'))
        .datasources ?? []
      const names = info.map((d) => d.name)
      searchedSources.value = names.length
      const settled = await Promise.allSettled(
        names.map((name) => searchCatalogTables(props.q.trim(), name)),
      )
      const out: Row[] = []
      settled.forEach((s, i) => {
        if (s.status === 'fulfilled') {
          for (const r of s.value) out.push({ ...r, ds: names[i] })
        } else {
          degraded.value.push({
            ds: names[i],
            error: s.reason instanceof Error ? s.reason.message : String(s.reason),
          })
        }
      })
      rows.value = out
    }
  } catch (e) {
    rows.value = []
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(() => [props.q, props.ds] as const, () => void load(), { immediate: true })

const columns = computed(() => [
  { key: 'name', label: t('govLinColTable', ui.lang) },
  { key: 'ds', label: t('govLinColDs', ui.lang) },
  { key: 'columns', label: t('govLinColColumns', ui.lang), align: 'right' as const },
  { key: 'row_count', label: t('govLinColRows', ui.lang), align: 'right' as const },
])
</script>

<template>
  <div class="lin-panel">
    <p v-if="promptOnly" class="lin-prompt">{{ t('govLinPrompt', ui.lang) }}</p>
    <template v-else>
      <p v-if="!ds && searchedSources > 0" class="lin-cross mono">
        {{ t('govLinCrossSource', ui.lang, searchedSources) }}
      </p>

      <StatePanel v-if="loading" mode="loading" :title="t('govLoading', ui.lang)" />
      <StatePanel
        v-else-if="error"
        mode="error"
        :title="t('govLinSearchError', ui.lang)"
        :detail="error"
        :retry-text="t('retry', ui.lang)"
        @retry="load"
      />
      <template v-else>
        <p v-if="degraded.length" class="lin-degraded">
          <span class="is-warn">{{ t('govPartial', ui.lang) }}</span>
          <span v-for="d in degraded" :key="d.ds" class="lin-degraded-item">
            <span class="mono">{{ d.ds }}</span>
            <span class="lin-degraded-reason">{{ d.error }}</span>
          </span>
        </p>

        <table v-if="rows.length" class="lin-table">
          <thead>
            <tr>
              <th v-for="c in columns" :key="c.key" :class="{ 'is-right': c.align === 'right' }">
                {{ c.label }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="r in rows"
              :key="`${r.ds}.${r.name}`"
              class="lin-row"
              @click="emit('open', { name: r.name, ds: r.ds })"
            >
              <td class="lin-name">{{ r.name }}</td>
              <td class="mono">{{ r.ds }}</td>
              <td class="is-right">{{ r.columns }}</td>
              <td class="is-right">{{ r.row_count ?? '—' }}</td>
            </tr>
          </tbody>
        </table>

        <StatePanel
          v-else-if="hasQuery"
          mode="empty"
          :title="t('govLinNoMatch', ui.lang)"
        />
        <StatePanel
          v-else
          mode="empty"
          :title="t('govLinEmpty', ui.lang)"
          :description="t('govLinEmptyDesc', ui.lang)"
        />
      </template>
    </template>
  </div>
</template>

<style scoped>
.lin-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.lin-prompt {
  margin: 0;
  padding: var(--sp-4);
  color: var(--text-tertiary);
  text-align: center;
  border: 1px dashed var(--border-subtle);
  border-radius: var(--r-md);
}
.lin-cross {
  margin: 0;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.lin-degraded {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.is-warn {
  color: var(--warning, #b8860b);
  font-weight: 600;
}
.lin-degraded-item {
  display: inline-flex;
  gap: 4px;
}
.lin-degraded-reason {
  color: var(--text-tertiary);
}
.lin-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-xs);
}
.lin-table th {
  text-align: left;
  font-weight: 500;
  color: var(--text-tertiary);
  padding: 2px 10px 2px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.lin-table td {
  padding: 4px 10px 4px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.is-right {
  text-align: right;
}
.lin-row {
  cursor: pointer;
}
.lin-row:hover {
  background: var(--surface-raised);
}
.lin-name {
  font-weight: 600;
}
.mono {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-2xs);
}
</style>
