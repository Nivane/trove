<template>
  <div class="admin-view" :aria-busy="loading || undefined">
    <PageHeader
      :title="t('jobs', ui.lang)"
      :description="t('jobsPageDesc', ui.lang)"
      :breadcrumbs="crumbs"
    />

    <div class="admin-card">
      <div class="card-toolbar">
        <el-input
          v-model="values.q"
          class="toolbar-search"
          :prefix-icon="Search"
          :placeholder="t('jobsSearch', ui.lang)"
          :aria-label="t('jobsSearch', ui.lang)"
          clearable
        />
        <el-select v-model="values.status" class="filter-select">
          <el-option :label="t('jobsFilterStatus', ui.lang)" value="" />
          <el-option :label="t('jobStatusError', ui.lang)" value="error" />
          <el-option :label="t('jobStatusAlert', ui.lang)" value="alert" />
          <el-option :label="t('jobStatusOk', ui.lang)" value="ok" />
          <el-option :label="t('disable', ui.lang)" value="disabled" />
        </el-select>
        <el-button v-if="!readOnly" type="primary" class="add" @click="openCreate">
          <Plus :size="15" class="btn-icon" />
          {{ t('jobCreateTitle', ui.lang) }}
        </el-button>
        <span class="spacer" />
        <span class="view-count">{{ filtered.length }}</span>
        <el-button class="refresh-btn" :loading="loading" @click="load">
          <RefreshCw :size="15" class="btn-icon" />
          {{ t('refresh', ui.lang) }}
        </el-button>
      </div>

      <div v-if="loading && !rows.length" class="table-skeleton">
        <div v-for="n in 5" :key="n" class="skeleton-row">
          <el-skeleton :rows="1" animated />
        </div>
      </div>

      <el-table
        v-else
        v-loading="loading"
        :data="paged"
        class="admin-table"
        max-height="var(--table-max-h)"
      >
        <template #empty>
          <TableEmpty />
        </template>
        <el-table-column :label="t('jobName', ui.lang)" min-width="160">
          <template #default="{ row }">
            <div class="job-name">{{ row.name }}</div>
            <div class="job-question">{{ row.question }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobScheduleType', ui.lang)" width="110">
          <template #default="{ row }">
            <span class="pill" :class="row.schedule_type === 'cron' ? 'pill-neutral' : 'pill-ok'">
              {{ row.schedule_type === 'cron' ? t('jobScheduleCron', ui.lang) : t('jobScheduleInterval', ui.lang) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobSchedule', ui.lang)" width="140">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.schedule }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobDatasource', ui.lang)" width="120">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.datasource }}</span>
            <!-- 主题域只做最小行展示(名称 + tooltip):它是任务的静态范围配置,
                 不是每次运行的结果状态 —— 状态类列留给 recent_run。 -->
            <div
              v-if="row.topic"
              class="job-topic-tag"
              :title="`${t('topicLabel', ui.lang)}: ${row.topic}`"
            >
              <span class="pill pill-accent">{{ row.topic }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobAlertExpr', ui.lang)" min-width="160">
          <template #default="{ row }">
            <span v-if="row.decision_rule" class="cell-mono">
              <span class="pill pill-neutral">{{ t('jobDecisionRule', ui.lang) }}</span>
              {{ row.decision_rule }}
            </span>
            <span v-else-if="row.alert_expr" class="cell-mono">{{ row.alert_expr }}</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobLastRun', ui.lang)" width="150">
          <template #default="{ row }">
            <div v-if="row.recent_run" class="job-last-run">
              <span class="pill" :class="runClass(row.recent_run.status)">
                {{ runLabel(row.recent_run.status) }}
              </span>
              <div v-if="row.recent_run.row_count != null" class="job-row-count">
                {{ row.recent_run.row_count }} rows
              </div>
            </div>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobNextRun', ui.lang)" width="160">
          <template #default="{ row }">
            <span v-if="row.enabled && row.next_run_at" class="cell-mono">
              {{ fmtDateTime(row.next_run_at) }}
            </span>
            <span v-else-if="!row.enabled" class="pill pill-warn">{{ t('disable', ui.lang) }}</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobStatus', ui.lang)" width="80">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :loading="toggling === row.id"
              :disabled="readOnly"
              @change="(v: boolean) => toggle(row, v)"
            />
          </template>
        </el-table-column>
        <!-- 只读角色下动作只剩「运行历史」(订阅管理走 /v1/admin/subscriptions,
             不在 analyst 只读面内 —— R1:看得见必须点得开),列宽随之收窄。 -->
        <el-table-column :label="t('auditAction', ui.lang)" :width="readOnly ? 130 : 310" fixed="right">
          <template #default="{ row }">
            <el-button v-if="!readOnly" size="small" :loading="running === row.id" @click="runNow(row)">
              <Play :size="14" class="btn-icon" />
              {{ t('jobRunNow', ui.lang) }}
            </el-button>
            <el-button size="small" @click="showRuns(row)">
              <History :size="14" class="btn-icon" />
              {{ t('jobRuns', ui.lang) }}
            </el-button>
            <!-- 订阅抽屉读的是 /v1/admin/subscriptions(admin 专属,不在 analyst
                 冻结只读清单里)→ 只读角色不渲染该入口,不留 403 死点击。 -->
            <el-button v-if="!readOnly" size="small" @click="openSubs(row)">
              <Bell :size="14" class="btn-icon" />
              {{ t('subsManage', ui.lang) }}
            </el-button>
            <el-button v-if="!readOnly" size="small" @click="openEdit(row)">
              <Pencil :size="14" class="btn-icon" />
            </el-button>
            <el-button v-if="!readOnly" size="small" type="danger" @click="remove(row)">
              <Trash2 :size="14" class="btn-icon" />
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="filtered.length > pageSize" class="pagination-bar">
        <el-pagination
          v-model:page-size="pageSize"
          :current-page="page"
          :total="filtered.length"
          :page-sizes="[20, 50, 100]"
          layout="total, prev, pager, next"
          @current-change="onPageChange"
        />
      </div>
    </div>

    <el-dialog
      v-model="dialogOpen"
      :title="editing ? t('jobEditTitle', ui.lang) : t('jobCreateTitle', ui.lang)"
      width="560px"
      class="job-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-form-item :label="t('jobName', ui.lang)">
          <el-input v-model="form.name" :placeholder="t('jobName', ui.lang)" />
        </el-form-item>
        <el-form-item :label="t('jobQuestion', ui.lang)">
          <el-input
            v-model="form.question"
            type="textarea"
            :rows="2"
            :placeholder="t('jobQuestion', ui.lang)"
          />
        </el-form-item>
        <el-form-item :label="t('jobDatasource', ui.lang)">
          <el-select v-model="form.datasource" filterable class="job-ds-select">
            <el-option
              v-for="d in datasources"
              :key="d.name"
              :label="d.name"
              :value="d.name"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('topicLabel', ui.lang)">
          <el-select v-model="form.topic" class="job-topic-select">
            <el-option :label="t('topicAny', ui.lang)" value="" />
            <el-option
              v-for="tp in topics"
              :key="tp.name"
              :label="tp.status === 'ok' ? tp.name : `${tp.name} (${t('topicStale', ui.lang)})`"
              :value="tp.name"
              :disabled="tp.status !== 'ok'"
            />
          </el-select>
          <div class="job-field-hint">{{ t('jobTopicHint', ui.lang) }}</div>
        </el-form-item>
        <el-form-item :label="t('jobScheduleType', ui.lang)">
          <el-radio-group v-model="form.schedule_type">
            <el-radio value="interval">{{ t('jobScheduleInterval', ui.lang) }}</el-radio>
            <el-radio value="cron">{{ t('jobScheduleCron', ui.lang) }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="form.schedule_type === 'cron' ? t('jobCron', ui.lang) : t('jobInterval', ui.lang)">
          <el-input
            v-model="form.schedule"
            :placeholder="form.schedule_type === 'cron' ? '0 9 * * *' : '30'"
          />
        </el-form-item>
        <el-form-item :label="t('jobDecisionRule', ui.lang)">
          <el-select v-model="form.decision_rule" clearable class="job-rule-select">
            <el-option label="—" value="" />
            <el-option
              v-for="r in rulesForDatasource"
              :key="r.id"
              :label="r.name ? `${r.name} (${r.id})` : r.id"
              :value="r.id"
              :disabled="!r.enabled"
            />
          </el-select>
          <div class="job-field-hint">{{ t('jobDecisionRuleHint', ui.lang) }}</div>
          <div v-if="rulesError" class="job-field-hint job-field-warn">{{ rulesError }}</div>
        </el-form-item>
        <el-form-item
          v-if="!form.decision_rule"
          :label="t('jobAlertExpr', ui.lang)"
        >
          <el-input v-model="form.alert_expr" :placeholder="t('jobAlertHint', ui.lang)" />
        </el-form-item>
        <el-form-item :label="t('jobAlertChannel', ui.lang)">
          <el-input v-model="form.alert_channel" placeholder="console" />
          <div class="job-field-hint">{{ t('jobChannelHint', ui.lang) }}</div>
        </el-form-item>
        <el-form-item :label="t('jobAlertCooldown', ui.lang)">
          <el-input-number v-model="form.alert_cooldown_min" :min="0" :max="1440" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">
          {{ editing ? t('save', ui.lang) : t('jobCreateTitle', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="runsOpen"
      :title="t('jobRuns', ui.lang)"
      width="720px"
      class="job-dialog"
    >
      <el-table v-loading="runsLoading" :data="runs" class="admin-table" max-height="420">
        <template #empty>
          <div class="dim">{{ t('jobRunsEmpty', ui.lang) }}</div>
        </template>
        <el-table-column :label="t('auditTime', ui.lang)" width="170">
          <template #default="{ row }">
            <span class="cell-mono">{{ fmtDateTime(row.started_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobStatus', ui.lang)" width="90">
          <template #default="{ row }">
            <span class="pill" :class="runClass(row.status)">{{ runLabel(row.status) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('rowCount', ui.lang)" width="90">
          <template #default="{ row }">
            <span v-if="row.row_count != null">{{ row.row_count }}</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobAlertExpr', ui.lang)" min-width="140">
          <template #default="{ row }">
            <span v-if="row.alert_sent" class="pill pill-warn">alert sent</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('ckptRunId', ui.lang)" min-width="120">
          <template #default="{ row }">
            <span class="cell-mono run-id">{{ row.verdict || '—' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 订阅抽屉:订阅者 × 任务。投递是 runner 里 best-effort 发生的,
         这里的「投递记录」是事后取证 —— 订了却没收到时,先看这里。 -->
    <DetailDrawer
      v-model="subsOpen"
      :title="subsTitle"
      width="720px"
      :close-label="t('close', ui.lang)"
    >
      <div class="subs-add">
        <div class="subs-add-hint">{{ t('subsAddHint', ui.lang) }}</div>
        <div class="subs-add-row">
          <el-input
            v-model="subForm.subscriber"
            class="subs-in-name"
            :placeholder="t('subsSubscriberPh', ui.lang)"
            :aria-label="t('subsSubscriber', ui.lang)"
          />
          <el-select
            v-model="subForm.mode"
            class="subs-in-mode"
            :aria-label="t('subsMode', ui.lang)"
          >
            <el-option :label="t('subsModeAlways', ui.lang)" value="always" />
            <el-option :label="t('subsModeAlertOnly', ui.lang)" value="alert_only" />
          </el-select>
          <el-button type="primary" :loading="subSaving" @click="addSub">
            <Plus :size="14" class="btn-icon" />
            {{ t('subsAdd', ui.lang) }}
          </el-button>
        </div>
        <el-input
          v-model="subForm.channel"
          class="subs-in-channel"
          :placeholder="t('subsChannelPh', ui.lang)"
          :aria-label="t('subsChannel', ui.lang)"
        />
      </div>

      <el-table
        v-loading="subsLoading"
        :data="subsRows"
        class="admin-table"
        max-height="280"
      >
        <template #empty>
          <div class="dim">{{ t('subsEmpty', ui.lang) }}</div>
        </template>
        <el-table-column :label="t('subsSubscriber', ui.lang)" min-width="150">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.subscriber }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('subsMode', ui.lang)" width="110">
          <template #default="{ row }">
            <span class="pill" :class="modeClass(row.mode)">
              {{ t(modeLabelKey(row.mode), ui.lang) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('subsChannel', ui.lang)" min-width="170">
          <template #default="{ row }">
            <span v-if="row.channel" class="cell-mono">{{ row.channel }}</span>
            <span v-else class="dim">{{ t('subsChannelInherit', ui.lang) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('subsEnabled', ui.lang)" width="80">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :loading="subToggling === row.id"
              @change="(v: boolean) => toggleSub(row, v)"
            />
          </template>
        </el-table-column>
        <el-table-column :label="t('auditAction', ui.lang)" width="130" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openSubEdit(row)">
              <Pencil :size="14" class="btn-icon" />
            </el-button>
            <el-button size="small" type="danger" @click="removeSub(row)">
              <Trash2 :size="14" class="btn-icon" />
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <h4 class="subs-sec">{{ t('subsDeliveries', ui.lang) }}</h4>
      <el-table
        v-loading="subsDeliveriesLoading"
        :data="subsDeliveries"
        class="admin-table"
        max-height="280"
      >
        <template #empty>
          <div class="dim">{{ t('subsDeliveriesEmpty', ui.lang) }}</div>
        </template>
        <el-table-column :label="t('subsRun', ui.lang)" width="70">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.run_id }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('subsSubscriber', ui.lang)" min-width="110">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.subscriber }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('subsChannel', ui.lang)" min-width="130">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.channel }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobStatus', ui.lang)" width="90">
          <template #default="{ row }">
            <span class="pill" :class="deliveryStatusClass(row.status)">
              {{ row.status === 'failed'
                ? t('subsDeliveryFailed', ui.lang)
                : t('subsDeliverySent', ui.lang) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('subsExcerpt', ui.lang)" min-width="200">
          <template #default="{ row }">
            <div class="subs-excerpt" :title="row.excerpt">{{ row.excerpt }}</div>
            <div v-if="row.error" class="subs-excerpt-err">{{ row.error }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('subsDeliveredAt', ui.lang)" width="160">
          <template #default="{ row }">
            <span class="cell-mono">{{ fmtDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </DetailDrawer>

    <el-dialog
      v-model="subEditOpen"
      :title="t('subsEditTitle', ui.lang)"
      width="480px"
      class="job-dialog"
    >
      <el-form label-position="top">
        <el-form-item :label="t('subsChannel', ui.lang)">
          <el-input
            v-model="subEditForm.channel"
            :placeholder="t('subsChannelPh', ui.lang)"
          />
        </el-form-item>
        <el-form-item :label="t('subsMode', ui.lang)">
          <el-select v-model="subEditForm.mode" class="subs-in-mode">
            <el-option :label="t('subsModeAlways', ui.lang)" value="always" />
            <el-option :label="t('subsModeAlertOnly', ui.lang)" value="alert_only" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="subEditOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="subEditSaving" @click="saveSubEdit">
          {{ t('save', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Bell, History, Pencil, Play, Plus, RefreshCw, Search, Trash2 } from 'lucide-vue-next'
import { apiGet, apiPatch, apiPost, apiDelete } from '../../api/http'
import {
  createSubscription,
  deleteSubscription,
  deliveryStatusClass,
  fetchDeliveries,
  fetchSubscriptions,
  modeClass,
  modeLabelKey,
  patchSubscription,
  type SubscriptionDelivery,
  type SubscriptionRow,
} from '../../api/subscriptions'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { notifySuccess, toastError } from '../../utils/notify'
import { fmtDateTime } from '../../utils/format'
import { useListQuery } from '../../composables/useListQuery'
import { fetchTopics, type TopicInfo } from '../../api/topics'
import TableEmpty from '../../components/admin/TableEmpty.vue'
import PageHeader from '../../components/base/PageHeader.vue'
import DetailDrawer from '../../components/base/DetailDrawer.vue'
import type { DatasourceInfo } from '../../api/types'
import { useReadOnly } from '../../composables/useReadOnly'

const { readOnly } = useReadOnly()

interface JobRow {
  id: string
  name: string
  question: string
  datasource: string
  workflow: string
  schedule_type: string
  schedule: string
  enabled: boolean
  alert_expr: string
  alert_channel: string
  alert_cooldown_min: number
  decision_rule: string
  topic: string
  next_run_at: string
  created_at: string
  updated_at: string
  recent_run?: {
    status: string
    row_count?: number
    alert_sent?: boolean
    started_at?: string
    verdict?: string
  } | null
  [k: string]: unknown
}

interface RunRow {
  id: number
  started_at: string
  finished_at: string
  status: string
  row_count: number | null
  alert_sent: boolean
  verdict: string
  result?: Record<string, unknown>
  [k: string]: unknown
}

const ui = useUiStore()
const rows = ref<JobRow[]>([])
const loading = ref(false)

/* ── URL state (P6 §4.3) ──────────────────────────────────────────────────
   q / status / page are the keys this page acknowledges. The list endpoint
   has no filter parameters (GET /v1/admin/jobs?limit= only), so filtering
   and paging stay client-side over the fetched rows — but the *state* is
   still the URL's, so /admin/jobs?status=error (the shell's failed-jobs
   drill-down) lands on the filtered list, a refresh keeps it and the link
   is shareable.
   status values are the last run's outcome, or 'disabled' for the enable
   switch: '' | error | alert | ok | disabled. */
const { values } = useListQuery({ q: '', status: '', page: '1' })
const pageSize = ref(20)

const filtered = computed(() => {
  const needle = values.q.trim().toLowerCase()
  return rows.value.filter((row) => {
    if (needle) {
      const hay = `${row.name || ''}\n${row.question || ''}`.toLowerCase()
      if (!hay.includes(needle)) return false
    }
    if (values.status === 'disabled') return !row.enabled
    if (values.status) return row.recent_run?.status === values.status
    return true
  })
})

const page = computed(() => Math.max(1, Number.parseInt(values.page, 10) || 1))
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)))
const paged = computed(() =>
  filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value),
)

const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  { label: t('jobs', ui.lang) },
])

function onPageChange(next: number) {
  values.page = String(Math.max(1, next))
}

// 过滤条件变化回到第 1 页(旧的第 7 页在新结果集里没有意义)。
watch(
  [() => values.q, () => values.status],
  () => {
    if (values.page !== '1') values.page = '1'
  },
)

// 结果集缩水(过滤/刷新)时把页码钳回合法范围。
watch([filtered, pageSize], () => {
  if (page.value > pageCount.value) values.page = String(pageCount.value)
})

const saving = ref(false)
const toggling = ref('')
const running = ref('')
const dialogOpen = ref(false)
const editing = ref<JobRow | null>(null)
const runsOpen = ref(false)
const runs = ref<RunRow[]>([])
const runsLoading = ref(false)
const datasources = ref<DatasourceInfo[]>([])

const emptyForm = {
  name: '',
  question: '',
  datasource: '',
  schedule_type: 'interval' as 'interval' | 'cron',
  schedule: '',
  alert_expr: '',
  alert_channel: '',
  alert_cooldown_min: 30,
  decision_rule: '',
  topic: '',
}
const form = reactive({ ...emptyForm })

interface RuleOption {
  id: string
  name: string
  enabled: boolean
}

const rules = ref<RuleOption[]>([])
const rulesError = ref('')
const topics = ref<TopicInfo[]>([])

// Only the rules of the datasource this job points at can compile — the
// semantic model is per-datasource, so the list is filtered rather than
// offering every rule in the project.
const rulesForDatasource = computed(() => rules.value)

async function loadRules() {
  rulesError.value = ''
  rules.value = []
  const ds = form.datasource
  if (!ds) return
  try {
    const body = await apiGet(`/v1/admin/decisions?datasource=${encodeURIComponent(ds)}`)
    rules.value = (body.rules ?? []) as RuleOption[]
  } catch (e) {
    // A datasource with no decisions.yml, or a corrupt one, shows as a hint —
    // creating a job must stay possible (a plain question job needs no rule).
    rulesError.value = e instanceof Error ? e.message : String(e)
  }
}

// The topic list is per-datasource too (the domains live in that
// datasource's semantic model). A failed fetch means "no domains to offer"
// (404 = no semantic model) — it does NOT mean the current value is invalid,
// so the value is left alone and the write-time check stays the loud gate;
// only a *successful* list may prune a selection that is gone or expired.
async function loadTopics() {
  const ds = form.datasource
  topics.value = []
  if (!ds) return
  let list: TopicInfo[] = []
  let loaded = false
  try {
    const body = await fetchTopics(ds)
    if (form.datasource !== ds) return // 换源了,这份清单已过期
    list = body.topics ?? []
    loaded = true
  } catch {
    if (form.datasource !== ds) return
  }
  topics.value = list
  if (!loaded) return
  if (form.topic && !list.some((tp) => tp.name === form.topic && tp.status === 'ok')) {
    form.topic = ''
  }
}

function runClass(status: string): string {
  if (status === 'ok') return 'pill-ok'
  if (status === 'alert') return 'pill-warn'
  return 'pill-danger'
}

function runLabel(status: string): string {
  if (status === 'ok') return t('jobStatusOk', ui.lang)
  if (status === 'alert') return t('jobStatusAlert', ui.lang)
  return t('jobStatusError', ui.lang)
}

async function loadDatasources() {
  try {
    const body = await apiGet('/v1/catalog/datasources')
    datasources.value = body.datasources ?? []
    if (!form.datasource && datasources.value.length) {
      const def = datasources.value.find((d) => d.default)
      form.datasource = def?.name || datasources.value[0].name
    }
  } catch {
    datasources.value = []
  }
}

async function load() {
  loading.value = true
  try {
    const body = await apiGet('/v1/admin/jobs')
    rows.value = (body.jobs ?? []) as JobRow[]
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, emptyForm)
  const def = datasources.value.find((d) => d.default)
  form.datasource = def?.name || datasources.value[0]?.name || ''
  dialogOpen.value = true
  void loadRules()
  void loadTopics()
}

function openEdit(row: JobRow) {
  editing.value = row
  Object.assign(form, {
    name: row.name,
    question: row.question,
    datasource: row.datasource,
    schedule_type: row.schedule_type,
    schedule: row.schedule,
    alert_expr: row.alert_expr,
    alert_channel: row.alert_channel,
    alert_cooldown_min: row.alert_cooldown_min,
    decision_rule: row.decision_rule || '',
    topic: row.topic || '',
  })
  dialogOpen.value = true
  void loadRules()
  void loadTopics()
}

// The rule list is per-datasource, so switching datasource reloads it and
// drops a rule that no longer belongs to the selected one. The topic list
// reloads the same way; loadTopics prunes a stale selection once the new
// list actually arrives.
watch(() => form.datasource, () => {
  if (!dialogOpen.value) return
  if (form.decision_rule && !rules.value.some((r) => r.id === form.decision_rule)) {
    form.decision_rule = ''
  }
  void loadRules()
  void loadTopics()
})

async function save() {
  if (!form.question.trim()) return
  saving.value = true
  try {
    const payload = {
      name: form.name,
      question: form.question,
      datasource: form.datasource,
      schedule_type: form.schedule_type,
      schedule: form.schedule,
      alert_expr: form.decision_rule ? '' : form.alert_expr,
      alert_channel: form.alert_channel,
      alert_cooldown_min: form.alert_cooldown_min,
      decision_rule: form.decision_rule,
      topic: form.topic,
    }
    if (editing.value) {
      await apiPatch(`/v1/admin/jobs/${editing.value.id}`, payload)
      notifySuccess(t('jobUpdated', ui.lang))
    } else {
      await apiPost('/v1/admin/jobs', payload)
      notifySuccess(t('jobCreated', ui.lang))
    }
    dialogOpen.value = false
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}

async function toggle(row: JobRow, enabled: boolean) {
  toggling.value = row.id
  try {
    await apiPatch(`/v1/admin/jobs/${row.id}`, { enabled })
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    toggling.value = ''
  }
}

async function runNow(row: JobRow) {
  try {
    await ElMessageBox.confirm(t('jobConfirmRun', ui.lang), t('jobRunNow', ui.lang), {
      type: 'warning',
      confirmButtonText: t('jobRunNow', ui.lang),
      cancelButtonText: t('cancel', ui.lang),
    })
  } catch {
    return
  }
  running.value = row.id
  try {
    const body = await apiPost(`/v1/admin/jobs/${row.id}/run`)
    ElMessage.success(t('jobRunTriggered', ui.lang))
    if (body?.run?.status) {
      ElMessage.success(
        `${t('jobStatus', ui.lang)}: ${runLabel(body.run.status)} · ${body.run.row_count ?? 0} rows`,
      )
    }
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    running.value = ''
  }
}

async function showRuns(row: JobRow) {
  runsOpen.value = true
  runsLoading.value = true
  try {
    const body = await apiGet(`/v1/admin/jobs/${row.id}/runs`)
    runs.value = (body.runs ?? []) as RunRow[]
  } catch (e) {
    toastError(e)
  } finally {
    runsLoading.value = false
  }
}

async function remove(row: JobRow) {
  try {
    await ElMessageBox.confirm(t('jobConfirmDelete', ui.lang), t('delete', ui.lang), {
      type: 'warning',
      confirmButtonText: t('delete', ui.lang),
      cancelButtonText: t('cancel', ui.lang),
    })
  } catch {
    return
  }
  try {
    await apiDelete(`/v1/admin/jobs/${row.id}`)
    notifySuccess(t('jobDeleted', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

/* ── 订阅(报告投递)─────────────────────────────────────────────────────
   一条订阅 = 订阅者 × 任务;mode 决定每期投还是仅告警投,channel 留空 =
   沿用任务通道。投递由 runner 在每次运行后 best-effort 发生,这里的投递
   记录是事后取证:订了却没收到,先来这里看是「没投」还是「投失败」。 */
const subsOpen = ref(false)
const subsJob = ref<JobRow | null>(null)
const subsRows = ref<SubscriptionRow[]>([])
const subsLoading = ref(false)
const subsDeliveries = ref<SubscriptionDelivery[]>([])
const subsDeliveriesLoading = ref(false)
const subSaving = ref(false)
const subToggling = ref('')
const subForm = reactive({
  subscriber: '',
  channel: '',
  mode: 'always' as 'always' | 'alert_only',
})
const subEditOpen = ref(false)
const subEditSaving = ref(false)
const subEditTarget = ref<SubscriptionRow | null>(null)
const subEditForm = reactive({
  channel: '',
  mode: 'always' as 'always' | 'alert_only',
})

const subsTitle = computed(() => {
  const job = subsJob.value?.name || subsJob.value?.id || ''
  return job ? `${t('subsDrawerTitle', ui.lang)} · ${job}` : t('subsDrawerTitle', ui.lang)
})

function openSubs(row: JobRow) {
  subsJob.value = row
  subsOpen.value = true
  subForm.subscriber = ''
  subForm.channel = ''
  subForm.mode = 'always'
  void loadSubs()
  void loadSubDeliveries()
}

async function loadSubs() {
  const job = subsJob.value
  if (!job) return
  subsLoading.value = true
  try {
    const body = await fetchSubscriptions({ job_id: job.id })
    subsRows.value = body.subscriptions ?? []
  } catch (e) {
    toastError(e)
  } finally {
    subsLoading.value = false
  }
}

async function loadSubDeliveries() {
  const job = subsJob.value
  if (!job) return
  subsDeliveriesLoading.value = true
  try {
    const body = await fetchDeliveries({ job_id: job.id })
    subsDeliveries.value = body.deliveries ?? []
  } catch (e) {
    toastError(e)
  } finally {
    subsDeliveriesLoading.value = false
  }
}

async function addSub() {
  const job = subsJob.value
  const subscriber = subForm.subscriber.trim()
  // 空订阅者只会换来后端 400;前端先拦(与 save() 的空问题同款)。
  if (!job || !subscriber) return
  subSaving.value = true
  try {
    await createSubscription(job.id, {
      subscriber,
      channel: subForm.channel.trim(),
      mode: subForm.mode,
    })
    notifySuccess(t('subsCreated', ui.lang))
    subForm.subscriber = ''
    subForm.channel = ''
    await loadSubs()
  } catch (e) {
    // 未知用户 / 重复订阅 / 坏通道都是 400 —— 后端 detail 比前端猜的准。
    toastError(e)
  } finally {
    subSaving.value = false
  }
}

async function toggleSub(sub: SubscriptionRow, enabled: boolean) {
  subToggling.value = sub.id
  try {
    await patchSubscription(sub.id, { enabled })
    await loadSubs()
  } catch (e) {
    toastError(e)
  } finally {
    subToggling.value = ''
  }
}

function openSubEdit(sub: SubscriptionRow) {
  subEditTarget.value = sub
  subEditForm.channel = sub.channel
  subEditForm.mode = sub.mode === 'alert_only' ? 'alert_only' : 'always'
  subEditOpen.value = true
}

async function saveSubEdit() {
  const target = subEditTarget.value
  if (!target) return
  subEditSaving.value = true
  try {
    await patchSubscription(target.id, {
      channel: subEditForm.channel.trim(),
      mode: subEditForm.mode,
    })
    notifySuccess(t('subsUpdated', ui.lang))
    subEditOpen.value = false
    await loadSubs()
  } catch (e) {
    toastError(e)
  } finally {
    subEditSaving.value = false
  }
}

async function removeSub(sub: SubscriptionRow) {
  try {
    await ElMessageBox.confirm(
      t('subsConfirmDelete', ui.lang),
      t('delete', ui.lang),
      {
        type: 'warning',
        confirmButtonText: t('delete', ui.lang),
        cancelButtonText: t('cancel', ui.lang),
      },
    )
  } catch {
    return
  }
  try {
    await deleteSubscription(sub.id)
    notifySuccess(t('subsDeleted', ui.lang))
    await loadSubs()
  } catch (e) {
    toastError(e)
  }
}

onMounted(() => {
  // 坏的 ?page=abc 归一为 1,而不是让 URL 说谎
  if (values.page !== String(page.value)) values.page = String(page.value)
  load()
  loadDatasources()
})
</script>

<style scoped>
.filter-select {
  width: 150px;
}

/* ── 订阅抽屉 ── */
.subs-add {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding-bottom: var(--sp-3);
  margin-bottom: var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
}

.subs-add-hint {
  font-size: var(--fs-xs);
  line-height: var(--lh-normal);
  color: var(--text-secondary);
}

.subs-add-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.subs-in-name {
  flex: 1;
  min-width: 0;
}

.subs-in-mode {
  width: 130px;
  flex: none;
}

.subs-sec {
  margin: var(--sp-5) 0 var(--sp-2);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}

.subs-excerpt {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-line;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.subs-excerpt-err {
  margin-top: 2px;
  font-size: var(--fs-2xs);
  color: var(--danger-text);
}

/* 主题域列内小标签:长域名截断,tooltip 给全名。 */
.job-topic-tag {
  margin-top: 4px;
  overflow: hidden;
}
</style>
