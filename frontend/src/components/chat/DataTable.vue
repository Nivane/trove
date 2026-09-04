<template>
  <div class="data-table-wrap">
    <div class="data-table-toolbar">
      <input
        v-model="filter"
        class="data-table-filter"
        :placeholder="t('tableFilter', ui.lang)"
        @input="page = 1"
      />
      <span class="data-table-meta"
        >{{ filteredRows.length }} {{ t('rows', ui.lang) }} × {{ headers.length }}
        {{ t('cols', ui.lang) }}</span
      >
      <span class="data-table-actions">
        <button class="icon-btn" :title="t('copyTable', ui.lang)" @click="copy">
          <el-icon :size="14"><CopyDocument /></el-icon>
        </button>
        <button
          class="icon-btn"
          :title="t('downloadCsv', ui.lang)"
          @click="downloadCsv"
        >
          <el-icon :size="14"><Download /></el-icon>
        </button>
      </span>
    </div>
    <div class="data-table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th
              v-for="(h, j) in headers"
              :key="h"
              :class="{ numeric: isNumericCol(j) }"
              @click="toggleSort(j)"
            >
              <span class="th-inner">
                {{ h }}
                <span v-if="sortCol === j" class="sort-arrow">{{
                  sortDir === 1 ? '▲' : '▼'
                }}</span>
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in pageRows" :key="i">
            <td
              v-for="(cell, j) in row"
              :key="j"
              :class="{ numeric: isNumericCol(j) }"
              :title="String(cell ?? '')"
            >
              {{ fmtCell(cell) }}
            </td>
          </tr>
        </tbody>
        <tfoot v-if="aggRow.length">
          <tr>
            <td
              v-for="(v, j) in aggRow"
              :key="j"
              :class="{ numeric: isNumericCol(j) }"
            >
              {{ fmtCell(v) }}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
    <div v-if="pageCount > 1" class="data-table-pager">
      <button
        class="icon-btn"
        :disabled="page <= 1"
        @click="page -= 1"
      >
        <el-icon :size="14"><ArrowLeft /></el-icon>
      </button>
      <span class="data-table-page">{{ page }} / {{ pageCount }}</span>
      <button
        class="icon-btn"
        :disabled="page >= pageCount"
        @click="page += 1"
      >
        <el-icon :size="14"><ArrowRight /></el-icon>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  CopyDocument,
  Download,
  ArrowLeft,
  ArrowRight,
} from '@element-plus/icons-vue'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { notifySuccess, notifyError } from '../../utils/notify'

const props = defineProps<{
  headers: string[]
  rows: unknown[][]
  /** 完整查询结果(供下载):未提供时回退到展示的 rows。 */
  downloadRows?: unknown[][] | null
}>()
const ui = useUiStore()

const PAGE_SIZE = 50
const filter = ref('')
const sortCol = ref(-1)
const sortDir = ref(1) // 1 asc, -1 desc
const page = ref(1)

/** 下载用数据:优先完整查询结果(由后端 summary.rows 提供)。 */
const exportRows = computed(
  () => props.downloadRows && props.downloadRows.length
    ? props.downloadRows
    : props.rows,
)

function cellText(v: unknown): string {
  if (v === null || v === undefined) return ''
  return String(v).toLowerCase()
}

const filteredRows = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return props.rows
  return props.rows.filter((row) =>
    row.some((cell) => cellText(cell).includes(q)),
  )
})

const sortedRows = computed(() => {
  if (sortCol.value < 0) return filteredRows.value
  const col = sortCol.value
  const dir = sortDir.value
  return [...filteredRows.value].sort((a, b) => {
    const av = a[col]
    const bv = b[col]
    const an = Number(av)
    const bn = Number(bv)
    if (!Number.isNaN(an) && !Number.isNaN(bn)) return (an - bn) * dir
    return String(av ?? '').localeCompare(String(bv ?? '')) * dir
  })
})

const pageCount = computed(() =>
  Math.max(1, Math.ceil(sortedRows.value.length / PAGE_SIZE)),
)

const pageRows = computed(() => {
  const start = (page.value - 1) * PAGE_SIZE
  return sortedRows.value.slice(start, start + PAGE_SIZE)
})

/** 数值列:对过滤后的可见行抽样判断(与展示一致)。 */
function isNumericCol(colIdx: number): boolean {
  const sample = filteredRows.value.slice(0, 20).some((r) => {
    const v = r[colIdx]
    return v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v))
  })
  return sample
}

function num(v: unknown): number {
  if (v === null || v === undefined || v === '') return 0
  const n = Number(v)
  return Number.isNaN(n) ? 0 : n
}

/** 数值列聚合行:合计 + 平均值(只有一列时表头自适应)。 */
const aggRow = computed(() => {
  return props.headers.map((h, j) => {
    if (!isNumericCol(j)) return ''
    const vals = filteredRows.value.map((r) => num(r[j]))
    if (!vals.length) return ''
    const sum = vals.reduce((a, b) => a + b, 0)
    const avg = sum / vals.length
    return `${sum.toLocaleString('en-US')} · avg ${avg.toLocaleString('en-US', { maximumFractionDigits: 2 })}`
  })
})

function toggleSort(j: number) {
  if (sortCol.value === j) {
    sortDir.value = -sortDir.value
  } else {
    sortCol.value = j
    sortDir.value = 1
  }
  page.value = 1
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return v.toLocaleString('en-US')
    return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
  }
  return String(v)
}

async function copy() {
  const text = [props.headers, ...sortedRows.value]
    .map((r) => r.map(fmtCell).join('\t'))
    .join('\n')
  try {
    await navigator.clipboard.writeText(text)
    notifySuccess(t('copied', ui.lang))
  } catch {
    notifyError(t('copyFailed', ui.lang))
  }
}

function downloadCsv() {
  const esc = (v: unknown) => {
    const s = fmtCell(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  // 按完整查询结果下载(不被答案表格的展示行数截断)
  const rows = exportRows.value
  const csv = [props.headers, ...rows]
    .map((r) => r.map(esc).join(','))
    .join('\n')
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `trove-result-${Date.now()}.csv`
  a.click()
  URL.revokeObjectURL(url)
}
</script>
