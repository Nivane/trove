<template>
  <div class="data-table-wrap">
    <div class="data-table-toolbar">
      <span class="data-table-meta"
        >{{ rows.length }} {{ t('rows', ui.lang) }} × {{ headers.length }}
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
            >
              {{ h }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in rows" :key="i">
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
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { CopyDocument, Download } from '@element-plus/icons-vue'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { notifySuccess, notifyError } from '../../utils/notify'

const props = defineProps<{ headers: string[]; rows: unknown[][] }>()
const ui = useUiStore()

function isNumericCol(colIdx: number): boolean {
  const sample = props.rows.slice(0, 20).some((r) => {
    const v = r[colIdx]
    return v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v))
  })
  return sample
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
  const text = [props.headers, ...props.rows]
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
  const csv = [props.headers, ...props.rows]
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
