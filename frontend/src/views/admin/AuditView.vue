<template>
  <div class="admin-view">
    <header class="view-header">
      <div>
        <h2>{{ t('audit', ui.lang) }}</h2>
        <p class="view-desc">{{ t('auditPageDesc', ui.lang) }}</p>
      </div>
    </header>

    <div class="stat-grid">
      <div class="stat-card">
        <span class="stat-icon accent"><ScrollText :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('audit', ui.lang) }}</span>
          <span class="stat-value">{{ total }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon ok"><ShieldCheck :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('statusActive', ui.lang) }}</span>
          <span class="stat-value">{{ statusSummary.ok }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon warn"><AlertTriangle :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('errors', ui.lang) }}</span>
          <span class="stat-value">{{ statusSummary.err }}</span>
        </div>
      </div>
    </div>

    <div class="admin-card">
      <div class="card-toolbar">
        <div class="audit-filters">
          <el-input
            v-model="action"
            :placeholder="t('auditAction', ui.lang)"
            clearable
            class="audit-filter-input"
            :prefix-icon="Search"
            @keyup.enter="onActionChange"
            @clear="onActionChange"
          />
        </div>
        <span class="spacer" />
        <span class="view-count">{{ total }}</span>
        <el-button class="refresh-btn" :loading="loading" @click="load">
          <RefreshCw :size="15" class="btn-icon" />
          {{ t('refresh', ui.lang) }}
        </el-button>
      </div>
      <div v-if="loading && !entries.length" class="table-skeleton">
        <div v-for="n in 8" :key="n" class="skeleton-row">
          <el-skeleton :rows="1" animated />
        </div>
      </div>
      <el-table
        v-else
        v-loading="loading"
        :data="entries"
        class="admin-table"
        max-height="calc(100vh - 300px)"
      >
        <template #empty>
          <TableEmpty />
        </template>
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
        <el-table-column :label="t('auditAction', ui.lang)" min-width="180">
          <template #default="{ row }">
            <span class="pill pill-neutral">{{ row.action }}</span>
          </template>
        </el-table-column>
        <el-table-column label="method" width="90">
          <template #default="{ row }">
            <span class="method-badge" :class="methodClass(row.method)">{{
              row.method || '—'
            }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('auditPath', ui.lang)" min-width="240">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.path }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('auditStatus', ui.lang)" width="100">
          <template #default="{ row }">
            <span
              class="pill"
              :class="okStatus(row.status) ? 'pill-ok' : 'pill-danger'"
            >
              <span
                class="pill-dot"
                :class="okStatus(row.status) ? 'pill-dot-ok' : ''"
              />
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
import { computed, onMounted, ref, watch } from 'vue'
import {
  RefreshCw,
  Search,
  ScrollText,
  ShieldCheck,
  AlertTriangle,
} from 'lucide-vue-next'
import { apiGet } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError } from '../../utils/notify'
import { fmtDateTime } from '../../utils/format'
import TableEmpty from '../../components/admin/TableEmpty.vue'

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

// A peek at the current page's outcome split, not a full aggregate — the
// table already shows each row; the stat cards summarize the visible page.
const statusSummary = computed(() => {
  let ok = 0
  let err = 0
  for (const e of entries.value) {
    if (okStatus(e.status)) ok += 1
    else if (!okStatus(e.status) && e.status != null) err += 1
  }
  return { ok, err }
})

function methodClass(method?: string): string {
  const m = (method || '').toUpperCase()
  if (m === 'GET') return 'is-get'
  if (m === 'POST' || m === 'PUT' || m === 'PATCH') return 'is-post'
  if (m === 'DELETE') return 'is-delete'
  return 'is-warn'
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

// 结果集缩水时(如刷新后总条数减少)把当前页钳回合法范围并重拉数据,
// 否则翻页控件会停在超出范围的页码上,出现"翻页没反应/白页"的假故障。
watch(total, () => {
  const maxPage = Math.max(1, Math.ceil(total.value / pageSize.value))
  if (page.value > maxPage) {
    page.value = maxPage
    load()
  }
})

onMounted(load)
</script>
