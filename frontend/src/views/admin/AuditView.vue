<template>
  <div class="admin-view">
    <div class="view-header">
      <div>
        <h2>{{ t('audit', ui.lang) }}</h2>
        <p class="view-desc">{{ t('auditPageDesc', ui.lang) }}</p>
      </div>
      <div class="view-header-right">
        <div class="audit-filters">
          <el-input
            v-model="action"
            :placeholder="t('auditAction', ui.lang)"
            clearable
            class="audit-filter-input"
            @keyup.enter="load"
            @clear="load"
          >
            <template #prefix>
              <Search :size="14" />
            </template>
          </el-input>
          <el-button class="refresh-btn" :loading="loading" @click="load">
            <RefreshCw :size="15" class="btn-icon" />
            {{ t('refresh', ui.lang) }}
          </el-button>
        </div>
      </div>
    </div>

    <div class="admin-card">
      <el-table
        v-loading="loading"
        :data="entries"
        class="admin-table"
        empty-text="—"
      >
        <el-table-column :label="t('auditTime', ui.lang)" width="180">
          <template #default="{ row }">
            <span class="cell-mono">{{ fmtDateTime(row.ts) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('auditUser', ui.lang)" width="150">
          <template #default="{ row }">
            <span class="cell-user">{{ row.username || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('auditAction', ui.lang)" min-width="170">
          <template #default="{ row }">
            <span class="pill pill-neutral">{{ row.action }}</span>
          </template>
        </el-table-column>
        <el-table-column label="method" width="84">
          <template #default="{ row }">
            <span class="method-badge">{{
              row.method || '—'
            }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('auditPath', ui.lang)" min-width="220">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.path }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('auditStatus', ui.lang)" width="90">
          <template #default="{ row }">
            <span
              class="pill"
              :class="okStatus(row.status) ? 'pill-ok' : 'pill-danger'"
            >
              {{ row.status ?? '—' }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RefreshCw, Search } from 'lucide-vue-next'
import { apiGet } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError } from '../../utils/notify'
import { fmtDateTime } from '../../utils/format'

interface AuditEntry {
  ts?: string
  username?: string
  action?: string
  method?: string
  path?: string
  status?: number
  [k: string]: unknown
}

const ui = useUiStore()
const entries = ref<AuditEntry[]>([])
const loading = ref(false)
const action = ref('')

function okStatus(s: unknown): boolean {
  return typeof s === 'number' && s >= 200 && s < 400
}

async function load() {
  loading.value = true
  try {
    const q = action.value ? `?action=${encodeURIComponent(action.value)}` : ''
    const body = await apiGet(`/v1/admin/audit${q}`)
    entries.value = body.audit ?? []
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>