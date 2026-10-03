<template>
  <div class="admin-view" :aria-busy="loading || undefined">
    <PageHeader
      :title="t('audit', ui.lang)"
      :description="t('auditPageDesc', ui.lang)"
      :breadcrumbs="crumbs"
    />

    <div class="stat-grid">
      <div class="stat-card">
        <span class="stat-icon accent"><ScrollText :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('audit', ui.lang) }}</span>
          <span class="stat-value">{{ total }}</span>
          <!-- §7: 卡面必须写清口径 —— 这张是服务端按当前筛选的全量 -->
          <span class="stat-sub">{{ t('auditScopeFiltered', ui.lang) }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon ok"><ShieldCheck :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('statusActive', ui.lang) }}</span>
          <span class="stat-value">{{ statusSummary.ok }}</span>
          <!-- 下一张卡是当前页切分,不是全量 —— 口径写在卡面 -->
          <span class="stat-sub">{{ t('auditScopePage', ui.lang) }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon warn"><AlertTriangle :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('errors', ui.lang) }}</span>
          <span class="stat-value">{{ statusSummary.err }}</span>
          <span class="stat-sub">{{ t('auditScopePage', ui.lang) }}</span>
        </div>
      </div>
    </div>

    <div class="admin-card">
      <div class="card-toolbar">
        <div class="audit-filters">
          <el-input
            v-model="values.user_id"
            :placeholder="t('auditUser', ui.lang)"
            clearable
            class="audit-filter-input audit-filter-user"
            :aria-label="t('auditUser', ui.lang)"
          />
          <el-input
            v-model="values.action"
            :placeholder="t('auditAction', ui.lang)"
            clearable
            class="audit-filter-input"
            :prefix-icon="Search"
            :aria-label="t('auditAction', ui.lang)"
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
        max-height="var(--table-max-h)"
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
          v-model:page-size="pageSize"
          :current-page="page"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @current-change="onPageChange"
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
import { useListQuery } from '../../composables/useListQuery'
import TableEmpty from '../../components/admin/TableEmpty.vue'
import PageHeader from '../../components/base/PageHeader.vue'

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
const pageSize = ref(20)
const total = ref(0)

/* ── URL state (P6 §4.3) ──────────────────────────────────────────────────
   user_id / action / page are the keys this page acknowledges, so a
   filtered trail is shareable, survives a refresh and the back button
   undoes a filter. The shell (overview) and the users page deep-link
   straight in — /admin/audit?action=query.execute, ?user_id=3 — and until
   these keys were read here those links landed on an unfiltered list. */
const { values } = useListQuery({ user_id: '', action: '', page: '1' })

const page = computed(() => Math.max(1, Number.parseInt(values.page, 10) || 1))

// 末项面包屑带筛选上下文(与 URL 同源):动作 / 用户 ID。
const filterContext = computed(() =>
  [values.action, values.user_id ? `#${values.user_id}` : ''].filter(Boolean).join(' · '),
)
const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  {
    label: filterContext.value
      ? `${t('audit', ui.lang)} · ${filterContext.value}`
      : t('audit', ui.lang),
  },
])

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
    if (values.action) params.set('action', values.action)
    if (values.user_id) params.set('user_id', values.user_id)
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

// 翻页把页码写回 URL(后退键可回到上一页),由下面的 watch 统一触发请求。
function onPageChange(next: number) {
  values.page = String(Math.max(1, next))
}

// 页大小变化会让当前页号失效:先回到第 1 页(此 watch 建在取数 watch 之前,
// 复位先落地)。
watch(pageSize, () => {
  if (values.page !== '1') values.page = '1'
})

// 过滤条件变化重取数据并回到第 1 页。
watch(
  [() => values.user_id, () => values.action],
  () => {
    if (values.page !== '1') values.page = '1'
    else void load()
  },
)

watch([() => values.page, pageSize], () => void load())

// 结果集缩水时(如刷新后总条数减少)把当前页钳回合法范围并重拉数据,
// 否则翻页控件会停在超出范围的页码上,出现"翻页没反应/白页"的假故障。
watch(total, () => {
  const maxPage = Math.max(1, Math.ceil(total.value / pageSize.value))
  if (page.value > maxPage) values.page = String(maxPage)
})

onMounted(() => {
  // 坏的 ?page=abc 归一为 1,而不是让 URL 说谎
  if (values.page !== String(page.value)) values.page = String(page.value)
  void load()
})
</script>
