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

    <!-- ── 拒绝频率报表(A5):哪些问题问得多却答不了 ──
         数据不可读时显式「不可用」(绝不铺一排 0);覆盖面自述就近在
         卡底 —— 「查不成的绿是假绿」的同一纪律,反向也成立。 -->
    <div class="admin-card refusal-card">
      <div class="card-toolbar">
        <span class="card-title">{{ t('refusalReport', ui.lang) }}</span>
        <span class="spacer" />
        <el-select
          v-model="refusalDays"
          class="refusal-days"
          :aria-label="t('refusalWindow', ui.lang)"
        >
          <el-option
            v-for="d in [7, 30, 90]"
            :key="d"
            :value="d"
            :label="t('refusalDaysOption', ui.lang, d)"
          />
        </el-select>
      </div>
      <div v-if="refusalLoading" class="refusal-loading">
        <el-skeleton :rows="3" animated />
      </div>
      <div v-else-if="refusal && refusal.available === false" class="refusal-degraded">
        <AlertTriangle :size="15" />
        <span>{{ t('refusalUnavailable', ui.lang) }}</span>
        <span v-if="refusal.note" class="cell-mono refusal-note-inline">{{ refusal.note }}</span>
      </div>
      <template v-else-if="refusal">
        <div class="stat-grid refusal-stats">
          <div class="stat-card">
            <span class="stat-icon warn"><AlertTriangle :size="18" /></span>
            <div class="stat-meta">
              <span class="stat-label">{{ t('refusalTotal', ui.lang) }}</span>
              <span class="stat-value">{{ refusal.total ?? '—' }}</span>
              <span class="stat-sub">{{ t('refusalScopeWindow', ui.lang) }}</span>
            </div>
          </div>
          <div class="stat-card">
            <span class="stat-icon accent"><GitBranch :size="18" /></span>
            <div class="stat-meta">
              <span class="stat-label">{{ t('refusalRelationMissing', ui.lang) }}</span>
              <span class="stat-value">{{ refusal.relationship_missing?.count ?? '—' }}</span>
              <span class="stat-sub">{{ t('refusalRelationHint', ui.lang) }}</span>
            </div>
          </div>
        </div>
        <div class="refusal-cols">
          <div class="refusal-col">
            <h4>{{ t('refusalTopQuestions', ui.lang) }}</h4>
            <ul v-if="refusal.top_questions?.length" class="refusal-list">
              <li v-for="q in refusal.top_questions" :key="q.question">
                <span class="refusal-q">{{ q.question }}</span>
                <span class="pill pill-neutral">{{ q.count }}</span>
                <span class="refusal-reasons">{{ (q.reasons || []).join(' · ') }}</span>
              </li>
            </ul>
            <p v-else class="cell-muted">{{ t('refusalNone', ui.lang) }}</p>
          </div>
          <div class="refusal-col">
            <h4>{{ t('refusalRelationQuestions', ui.lang) }}</h4>
            <ul
              v-if="refusal.relationship_missing?.top_questions?.length"
              class="refusal-list"
            >
              <li
                v-for="q in refusal.relationship_missing.top_questions"
                :key="q.question"
              >
                <span class="refusal-q">{{ q.question }}</span>
                <span class="pill pill-neutral">{{ q.count }}</span>
              </li>
            </ul>
            <p v-else class="cell-muted">{{ t('refusalNone', ui.lang) }}</p>
          </div>
        </div>
        <div v-if="refusal.by_reason?.length" class="refusal-chips">
          <span class="refusal-chips-label">{{ t('refusalByReason', ui.lang) }}</span>
          <span v-for="r in refusal.by_reason" :key="r.reason" class="pill pill-neutral">
            {{ r.reason }} · {{ r.count }}
          </span>
        </div>
        <div v-if="refusal.by_miss?.length" class="refusal-chips">
          <span class="refusal-chips-label">{{ t('refusalByMiss', ui.lang) }}</span>
          <span v-for="r in refusal.by_miss" :key="r.reason" class="pill pill-neutral">
            {{ r.reason }} · {{ r.count }}
          </span>
        </div>
        <p v-if="refusal.capped" class="refusal-foot">{{ t('refusalCapped', ui.lang) }}</p>
        <p
          v-if="ui.lang === 'en' ? refusal.coverage_note_en : refusal.coverage_note"
          class="refusal-foot"
        >
          {{ ui.lang === 'en' ? refusal.coverage_note_en : refusal.coverage_note }}
        </p>
      </template>
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
        <el-button class="refresh-btn" :loading="loading" @click="load(); loadRefusal()">
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
  GitBranch,
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

interface RefusalQuestion {
  question: string
  count: number
  reasons?: string[]
  datasources?: string[]
}

interface RefusalReport {
  available: boolean
  total: number | null
  capped?: boolean
  note?: string
  coverage_note: string
  coverage_note_en: string
  by_reason?: { reason: string; count: number }[]
  by_miss?: { reason: string; count: number }[]
  top_questions?: RefusalQuestion[]
  relationship_missing?: {
    reasons?: string[]
    count: number | null
    top_questions?: RefusalQuestion[]
  }
}

const ui = useUiStore()
const entries = ref<AuditEntry[]>([])
const loading = ref(false)
const pageSize = ref(20)
const total = ref(0)

/* ── 拒绝频率报表(A5):独立于表格筛选的投影,有自己的窗口 ── */
const refusal = ref<RefusalReport | null>(null)
const refusalLoading = ref(false)
const refusalDays = ref(30)

async function loadRefusal() {
  refusalLoading.value = true
  try {
    refusal.value = await apiGet<RefusalReport>(
      `/v1/admin/audit/refusal-report?days=${refusalDays.value}`,
    )
  } catch (e) {
    // 读不成**不铺零**:与后端降级同一形状(available:false + 原因),
    // 卡面显式「不可用」而不是一排 0 或上一次窗口的旧读数。
    refusal.value = {
      available: false,
      total: null,
      coverage_note: '',
      coverage_note_en: '',
      note: e instanceof Error ? e.message : String(e),
    }
  } finally {
    refusalLoading.value = false
  }
}

watch(refusalDays, () => void loadRefusal())

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
  void loadRefusal()
})
</script>

<style scoped>
/* ── 拒绝频率报表卡(A5) ──────────────────────────────────────────────
   只服务本卡;全局类(stat-grid / pill / card-toolbar)与卡内局部类混用,
   局部的一律 refusal- 前缀。 */
.refusal-days {
  width: 130px;
}
.refusal-loading,
.refusal-degraded,
.refusal-stats,
.refusal-cols,
.refusal-chips,
.refusal-foot {
  padding: 0 var(--sp-5);
}
.refusal-degraded {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding-top: var(--sp-4);
  padding-bottom: var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--warn-text, var(--text-secondary));
}
.refusal-note-inline {
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}
.refusal-stats {
  padding-top: var(--sp-4);
}
.refusal-cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--sp-5);
  padding-top: var(--sp-4);
}
.refusal-col h4 {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-secondary);
}
.refusal-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.refusal-list li {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) 0;
  border-bottom: 1px dashed var(--border-subtle);
}
.refusal-list li:last-child {
  border-bottom: none;
}
.refusal-q {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
}
.refusal-reasons {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.refusal-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  padding-top: var(--sp-3);
}
.refusal-chips-label {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.refusal-foot {
  padding-top: var(--sp-2);
  padding-bottom: var(--sp-4);
  margin: 0;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  line-height: 1.5;
}
@media (max-width: 900px) {
  .refusal-cols {
    grid-template-columns: 1fr;
  }
}
</style>
