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
            @keyup.enter="onActionChange"
            @clear="onActionChange"
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
      <div class="pagination-bar">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="onSizeChange"
        />
      </div>
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
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)

function okStatus(s: unknown): boolean {
  return typeof s === 'number' && s >= 200 && s < 400
}

async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (action.value) params.set('action', action.value)
    params.set('limit', String(pageSize.value))
    params.set('offset', String((page.value - 1) * pageSize.value))
    const body = await apiGet(`/v1/admin/audit?${params}`)
    entries.value = body.audit ?? []
    total.value = body.total ?? 0
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

// 过滤条件变化回到第 1 页;页大小变化同理(当前页可能超出新分页范围)
function onActionChange() {
  page.value = 1
  load()
}

function onSizeChange() {
  page.value = 1
  load()
}

onMounted(load)
</script>

<style scoped>
.pagination-bar {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>