<!--
  OverviewView — the admin console's landing page (/admin, P3).

  One screen answers two questions: what state is the platform in, and what
  needs attention. Three rules from the design doc are load-bearing here:

    · the banner speaks first — /v1/health's semantics (ok / degraded /
      unavailable) rendered honestly, with CTA and type-name-only errors;
    · every number is an entry point — the six KPIs, the datasource rows,
      the todo kinds and the event rows all link with their filter state in
      the URL (win stays in the URL via useListQuery; #todos / #datasources
      anchor the sections, per the P6 URL protocol, and this page owns the
      scroll+focus);
    · every block degrades on its own — a block the aggregate could not
      fetch renders its own ModuleErrorCard, never a page-wide toast, and
      an inexact count renders as ≥ N instead of a precise-looking low
      number (0 and null are different information).

  The aggregate is one round trip (GET /v1/admin/overview); nothing on this
  page writes — management actions stay on their own pages.
-->
<template>
  <div class="admin-view overview-page" :aria-busy="loading || undefined">
    <PageHeader :title="t('ovTitle', ui.lang)" :breadcrumbs="crumbs">
      <template #description>
        <span>{{ t('ovDesc', ui.lang) }}</span>
        <span v-if="asOf" class="ov-asof"> · {{ asOf }}</span>
      </template>
      <template #actions>
        <div class="win-seg" role="group" :aria-label="t('ovWindow', ui.lang)">
          <button
            v-for="w in OVERVIEW_WINDOWS"
            :key="w"
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.win === w }"
            :aria-pressed="values.win === w"
            @click="setWindow(w)"
          >
            {{ w }}
          </button>
        </div>
        <el-button :loading="loading" @click="load">
          {{ t('refresh', ui.lang) }}
        </el-button>
      </template>
    </PageHeader>

    <!-- first paint: skeleton mirrors the real layout (banner / KPI / cols) -->
    <div
      v-if="loading && !payload && !pageError"
      class="ov-skeleton"
      role="status"
      :aria-label="t('ovSkeletonAria', ui.lang)"
    >
      <span class="sk-bar sk-banner" />
      <div class="sk-kpis">
        <span v-for="n in 6" :key="n" class="sk-bar sk-kpi" />
      </div>
      <div class="sk-cols">
        <span class="sk-bar sk-panel" />
        <span class="sk-bar sk-panel" />
      </div>
      <p v-if="slow" class="ov-slow">{{ t('ovLoadingSlow', ui.lang) }}</p>
    </div>

    <!-- whole-page error: fetch failed, or storage down (503 payload) -->
    <StatePanel
      v-else-if="pageError"
      mode="error"
      :title="t('ovPageErrorTitle', ui.lang)"
      :description="t('ovPageErrorDesc', ui.lang)"
      :detail="pageError.detail"
    >
      <template #action>
        <el-button size="small" type="primary" @click="load">
          {{ t('retry', ui.lang) }}
        </el-button>
        <a class="ov-raw-probe" href="/v1/health" target="_blank" rel="noopener">
          {{ t('ovRawProbe', ui.lang) }}
        </a>
      </template>
    </StatePanel>

    <!-- first empty (no datasources): onboarding replaces banner + KPIs -->
    <section v-else-if="firstEmpty" class="ov-first-empty">
      <h2 class="ofe-title">{{ t('ovEmptyFirstTitle', ui.lang) }}</h2>
      <p class="ofe-desc">{{ t('ovEmptyFirstDesc', ui.lang) }}</p>
      <RouterLink class="ofe-cta" to="/admin/datasources">
        {{ t('ovEmptyFirstCta', ui.lang) }}
      </RouterLink>
      <div class="ofe-wizard">
        <OnboardingWizard :wizard="payload?.wizard ?? null" />
      </div>
    </section>

    <template v-else-if="payload">
      <!-- banner: tri-state health, straight from /v1/health's judgement -->
      <HealthBanner
        v-if="payload.health && payload.health.status !== 'unavailable'"
        :health="payload.health"
        :probe-error="healthProbeError"
      />
      <ModuleErrorCard
        v-else
        :title="t('ovBlockHealth', ui.lang)"
        :detail="degLine(degraded.health)"
        :retry-text="t('retry', ui.lang)"
        @retry="load"
      />

      <!-- KPI row — every number is an entry point -->
      <div class="kpi-row">
        <KpiTile
          v-for="tile in kpiTiles"
          :key="tile.key"
          :label="tile.label"
          :value="tile.value"
          :sub="tile.sub"
          @click="onKpi(tile)"
        />
      </div>

      <!-- usage strip: failure buckets (approximate) / window-empty / degraded -->
      <div v-if="!payload.usage" class="ov-strip-slot">
        <ModuleErrorCard
          :title="t('ovBlockUsage', ui.lang)"
          :detail="degLine(degraded.usage)"
          :retry-text="t('retry', ui.lang)"
          @retry="load"
        />
      </div>
      <template v-else>
        <div v-if="!payload.usage.available" class="ov-strip" role="status">
          <span class="ov-strip-text">{{ t('ovUsageUnavailable', ui.lang) }}</span>
        </div>
        <div v-else-if="usageEmpty" class="ov-strip" role="status">
          <span class="ov-strip-text">{{ t('ovUsageEmpty', ui.lang) }}</span>
          <span class="ov-strip-hint">{{ t('ovUsageEmptyHint', ui.lang) }}</span>
          <button type="button" class="ov-strip-btn" @click="setWindow('7d')">
            {{ t('ovSee7d', ui.lang) }}
          </button>
          <RouterLink class="ov-strip-btn" :to="opsHref">
            {{ t('ovGoOps', ui.lang) }}
          </RouterLink>
        </div>
        <div v-else-if="payload.usage.failures_by_class.length" class="ov-strip">
          <span class="ov-strip-text">{{ t('ovFailuresTitle', ui.lang) }}</span>
          <span
            v-for="b in payload.usage.failures_by_class"
            :key="b.class"
            class="ov-fail-chip"
            :title="b.domain ? b.class + ' · ' + b.domain : b.class"
          >
            {{ b.class }}<b>{{ b.count }}</b>
          </span>
          <span class="ov-strip-hint">
            {{
              payload.usage.sample_capped
                ? t('ovFailuresCapped', ui.lang)
                : t('ovFailuresApprox', ui.lang)
            }}
          </span>
        </div>
      </template>

      <!-- datasource health: five columns + drawer; rows degrade individually -->
      <section id="datasources" class="ov-card" tabindex="-1">
        <header class="ov-card-head">
          <div>
            <h2 class="ov-card-title">{{ t('ovDsTitle', ui.lang) }}</h2>
            <p v-if="payload.datasources" class="ov-card-sub">
              {{ t('ovDsCount', ui.lang, payload.datasources.length) }}
              <template v-if="kbInitializedCount !== null">
                · {{ t('ovDsKbInit', ui.lang, kbInitializedCount) }}
              </template>
            </p>
          </div>
          <RouterLink class="ov-card-link" to="/admin/datasources">
            {{ t('datasources', ui.lang) }} →
          </RouterLink>
        </header>

        <ModuleErrorCard
          v-if="!payload.datasources"
          :title="t('ovBlockDs', ui.lang)"
          :detail="degLine(degraded.datasources)"
          :retry-text="t('retry', ui.lang)"
          @retry="load"
        />
        <template v-else>
          <p v-if="degraded.datasources.length" class="ov-warn-strip" role="status">
            {{ degLine(degraded.datasources) }}
          </p>
          <DataTable
            :columns="dsColumns"
            :rows="payload.datasources"
            row-key="name"
            row-clickable
            :loading="loading"
            :skeleton-rows="3"
            @row-click="openDs"
          >
            <template #cell-name="{ row }">
              <span class="ds-name" :class="{ 'is-bad': dsUnreachable(d(row)) }">
                <span class="ds-dot" aria-hidden="true" />
                {{ d(row).name }}
              </span>
            </template>
            <template #cell-status="{ row }">
              <span class="ov-pill" :class="dsUnreachable(d(row)) ? 'is-bad' : 'is-ok'">
                {{
                  d(row).status === 'connected'
                    ? t('ovStatusConnected', ui.lang)
                    : t('ovStatusDisconnected', ui.lang)
                }}
              </span>
            </template>
            <template #cell-kb="{ row }">
              <span v-if="d(row).kb_initialized === null" class="ov-dash">—</span>
              <span
                v-else
                class="ov-pill"
                :class="d(row).kb_initialized ? 'is-ok' : 'is-idle'"
              >
                {{
                  d(row).kb_initialized
                    ? t('ovKbInitYes', ui.lang)
                    : t('ovKbInitNo', ui.lang)
                }}
              </span>
            </template>
            <template #cell-assets="{ row }">
              <span v-if="!assetLine(d(row))" class="ov-dash">—</span>
              <span v-else class="ds-assets" :title="assetTitle(d(row))">
                {{ assetLine(d(row)) }}
              </span>
            </template>
            <template #cell-readonly="{ row }">
              <span class="ov-pill" :class="roClass(d(row))">{{ roLabel(d(row)) }}</span>
            </template>
            <template #cell-drift="{ row }">
              <span
                class="ds-drift"
                :class="{
                  'ov-dash': d(row).drift_open === null,
                  'is-inexact': !d(row).drift_count_exact,
                }"
              >
                {{ fmtCount(d(row).drift_open, d(row).drift_count_exact) }}
              </span>
            </template>
          </DataTable>
        </template>
      </section>

      <div class="ov-cols">
        <!-- todo queue: eight sources, one total -->
        <section id="todos" class="ov-card" tabindex="-1">
          <header class="ov-card-head">
            <div>
              <h2 class="ov-card-title">{{ t('ovTodosTitle', ui.lang) }}</h2>
              <!-- 块降级时不报「0 类来源」——未取到与零是两条不同的信息 -->
              <p v-if="payload.todos" class="ov-card-sub">
                {{ t('ovTodosSub', ui.lang, payload.todos.items.length) }}
                · {{ t('ovTodosTotal', ui.lang) }}
                <b class="ov-total">{{
                  fmtCount(payload.todos.total, payload.todos.count_exact)
                }}</b>
              </p>
            </div>
          </header>
          <ModuleErrorCard
            v-if="!payload.todos"
            :title="t('ovBlockTodos', ui.lang)"
            :detail="degLine(degraded.todos)"
            :retry-text="t('retry', ui.lang)"
            @retry="load"
          />
          <TodoQueue v-else :todos="payload.todos" />
        </section>

        <aside class="ov-aside">
          <!-- onboarding: three steps, all reading existing state -->
          <section class="ov-card">
            <header class="ov-card-head">
              <div>
                <h2 class="ov-card-title">{{ t('ovWizTitle', ui.lang) }}</h2>
                <p class="ov-card-sub">{{ t('ovWizSub', ui.lang) }}</p>
              </div>
            </header>
            <OnboardingWizard :wizard="payload.wizard" />
          </section>

          <!-- recent events: the audit stream, window-scoped -->
          <section class="ov-card">
            <header class="ov-card-head">
              <div>
                <h2 class="ov-card-title">{{ t('ovEventsTitle', ui.lang) }}</h2>
                <p v-if="payload.recent_events" class="ov-card-sub">
                  {{ t('ovEventsSub', ui.lang, payload.recent_events.length) }}
                </p>
              </div>
              <RouterLink class="ov-card-link" to="/admin/audit">
                {{ t('ovEventsAudit', ui.lang) }} →
              </RouterLink>
            </header>
            <ModuleErrorCard
              v-if="!payload.recent_events"
              :title="t('ovBlockEvents', ui.lang)"
              :detail="degLine(degraded.recent_events)"
              :retry-text="t('retry', ui.lang)"
              @retry="load"
            />
            <p v-else-if="!payload.recent_events.length" class="ov-muted">
              {{ t('ovEventsEmpty', ui.lang) }}
            </p>
            <ul v-else class="ov-events">
              <li v-for="(ev, i) in payload.recent_events" :key="ev.ts + '#' + i">
                <RouterLink class="oe-link" :to="ev.href">
                  <span class="oe-time">{{ fmtDateTime(ev.ts) }}</span>
                  <span class="oe-action">{{ ev.action }}</span>
                  <span class="oe-user">{{ ev.username }}</span>
                  <span class="oe-status" :class="ev.status >= 400 ? 'is-bad' : 'is-ok'">
                    {{ ev.status }}
                  </span>
                </RouterLink>
              </li>
            </ul>
          </section>

          <!-- cost & cache: an honest gap card, not a fake number -->
          <section class="ov-card">
            <header class="ov-card-head">
              <div>
                <h2 class="ov-card-title">{{ t('ovCostTitle', ui.lang) }}</h2>
              </div>
              <RouterLink class="ov-card-link" :to="opsHref">
                {{ t('ovGoOps', ui.lang) }} →
              </RouterLink>
            </header>
            <p class="ov-muted">{{ t('ovCostGap', ui.lang) }}</p>
          </section>
        </aside>
      </div>
    </template>

    <!-- datasource drawer: a summary of the datasource page, not a replica -->
    <DetailDrawer
      v-model="drawerOpen"
      width="420px"
      :aria-label="t('ovDsDrawerTitle', ui.lang)"
      :close-label="t('close', ui.lang)"
    >
      <template #header>
        <div v-if="drawerRow" class="dsh">
          <span class="dsh-name">{{ drawerRow.name }}</span>
          <span class="ov-pill" :class="dsUnreachable(drawerRow) ? 'is-bad' : 'is-ok'">
            {{
              drawerRow.status === 'connected'
                ? t('ovStatusConnected', ui.lang)
                : t('ovStatusDisconnected', ui.lang)
            }}
          </span>
        </div>
      </template>

      <div v-if="drawerRow" class="ds-drawer">
        <section class="dd-sec">
          <h4>{{ t('ovDrawerConn', ui.lang) }}</h4>
          <dl class="dd-facts">
            <dt>{{ t('ovColConn', ui.lang) }}</dt>
            <dd>{{ drawerRow.status }}</dd>
            <dt>{{ t('ovHealthProbe', ui.lang) }}</dt>
            <dd>
              <template v-if="drawerPing">
                {{ drawerPing.ok ? '✓' : '✕ ' + (drawerPing.error || '') }}
              </template>
              <template v-else>—</template>
            </dd>
            <dt>{{ t('ovColReadonly', ui.lang) }}</dt>
            <dd>
              {{ roLabel(drawerRow) }}
              <span class="dd-mono">{{ drawerRow.readonly.basis }}</span>
            </dd>
          </dl>
        </section>

        <section class="dd-sec">
          <h4>{{ t('ovDrawerKb', ui.lang) }}</h4>
          <p v-if="drawerRow.kb_initialized === null" class="ov-dash">—</p>
          <p v-else>
            {{
              drawerRow.kb_initialized
                ? t('ovKbInitYes', ui.lang)
                : t('ovKbInitNo', ui.lang)
            }}
            <template v-if="drawerRow.refused !== null">
              · {{ t('ovDrawerRefused', ui.lang, drawerRow.refused) }}
            </template>
          </p>
          <p
            v-if="drawerRow.kb_items && Object.keys(drawerRow.kb_items).length"
            class="dd-assets"
          >
            <span v-for="(n, kind) in drawerRow.kb_items" :key="kind" class="ov-fail-chip">
              {{ kind }}<b>{{ n }}</b>
            </span>
          </p>
        </section>

        <section class="dd-sec">
          <h4>{{ t('ovDrawerDrift', ui.lang) }}</h4>
          <p>
            {{ t('ovDriftOpen', ui.lang) }}
            <b :class="{ 'is-inexact': !drawerRow.drift_count_exact }">
              {{ fmtCount(drawerRow.drift_open, drawerRow.drift_count_exact) }}
            </b>
          </p>
        </section>
      </div>

      <template #footer>
        <RouterLink class="dd-go" to="/admin/datasources">
          {{ t('ovGoDsPage', ui.lang) }}
        </RouterLink>
      </template>
    </DetailDrawer>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  OVERVIEW_WINDOWS,
  fetchOverview,
  type OverviewDatasourceRow,
  type OverviewDegradedEntry,
  type OverviewDsPing,
  type OverviewPayload,
} from '../../api/overview'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { fmtDateTime } from '../../utils/format'
import { useListQuery } from '../../composables/useListQuery'
import PageHeader from '../../components/base/PageHeader.vue'
import KpiTile from '../../components/base/KpiTile.vue'
import DataTable, { type DataTableColumn } from '../../components/base/DataTable.vue'
import DetailDrawer from '../../components/base/DetailDrawer.vue'
import StatePanel from '../../components/base/StatePanel.vue'
import HealthBanner from '../../components/overview/HealthBanner.vue'
import ModuleErrorCard from '../../components/overview/ModuleErrorCard.vue'
import TodoQueue from '../../components/overview/TodoQueue.vue'
import OnboardingWizard from '../../components/overview/OnboardingWizard.vue'

const ui = useUiStore()
const router = useRouter()
const route = useRoute()

/* ── window in the URL (P6 协议:/admin 承认的键 = win + #todos/#datasources) ── */
const { values } = useListQuery({ win: '24h' })
const isWindow = (w: string) => (OVERVIEW_WINDOWS as readonly string[]).includes(w)
/** 非法 win(手改 URL)在第一次取数前自愈回默认,URL 随之清干净。 */
const apiWindow = computed(() => (isWindow(values.win) ? values.win : '24h'))

function setWindow(w: string) {
  values.win = w
}

/* ── fetch lifecycle: one round trip, block-level honesty ──────────────── */
const payload = ref<OverviewPayload | null>(null)
const loading = ref(false)
const slow = ref(false)
const errorText = ref('')
let seq = 0
let slowTimer: ReturnType<typeof setTimeout> | null = null
let hardTimer: ReturnType<typeof setTimeout> | null = null

function clearTimers() {
  if (slowTimer) clearTimeout(slowTimer)
  if (hardTimer) clearTimeout(hardTimer)
  slowTimer = null
  hardTimer = null
}

async function load() {
  const my = ++seq
  loading.value = true
  errorText.value = ''
  slow.value = false
  clearTimers()
  slowTimer = setTimeout(() => {
    if (my === seq) slow.value = true
  }, 3000)
  // 骨架挂够 10s 就不是「慢」而是「失败」——按错误页处理并给 timeout 类型名,
  // 迟到的响应不再落地(seq 失效)。
  hardTimer = setTimeout(() => {
    if (my !== seq) return
    seq++
    loading.value = false
    errorText.value = 'Timeout'
  }, 10000)
  try {
    const body = await fetchOverview(apiWindow.value)
    if (my !== seq) return
    payload.value = body
  } catch (e) {
    if (my !== seq) return
    errorText.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (my === seq) {
      loading.value = false
      clearTimers()
    }
  }
}

onMounted(() => {
  if (!isWindow(values.win)) {
    values.win = '24h' // 自愈;下面的 watch 会接力取数
  } else {
    void load()
  }
  if (route.hash) void nextTick().then(() => scrollToAnchor(route.hash.slice(1)))
})

watch(
  () => values.win,
  (w) => {
    if (!isWindow(w)) {
      values.win = '24h'
      return
    }
    void load()
  },
)

/* ── page-level states ─────────────────────────────────────────────────── */
const pageError = computed<{ detail: string } | null>(() => {
  if (errorText.value) return { detail: errorText.value }
  const h = payload.value?.health
  if (h && h.status === 'unavailable') {
    return { detail: ('storage: ' + (h.storage.error || '')).trim() }
  }
  return null
})

const firstEmpty = computed(
  () =>
    !!payload.value &&
    Array.isArray(payload.value.datasources) &&
    payload.value.datasources.length === 0,
)

const crumbs = computed(() => [
  { label: t('admin', ui.lang) },
  { label: t('ovTitle', ui.lang) },
])

const asOf = computed(() => {
  const p = payload.value
  if (!p) return ''
  return (
    t('ovAsOf', ui.lang) +
    ' ' +
    fmtClock(p.generated_at) +
    ' · ' +
    t('ovElapsed', ui.lang) +
    ' ' +
    p.elapsed_ms +
    'ms'
  )
})

function fmtClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds())
}

/* ── degraded bookkeeping: which block / source could not be fetched ───── */
const degraded = computed(() => {
  const by = (block: string): OverviewDegradedEntry[] =>
    (payload.value?.degraded ?? []).filter((e) => e.block === block)
  return {
    health: by('health'),
    usage: by('usage'),
    todos: by('todos'),
    datasources: by('datasources'),
    recent_events: by('recent_events'),
  }
})

/** 探测腿失败时唯一的 health 降级来源是 datasources;错误只报类型名。 */
const healthProbeError = computed(
  () => degraded.value.health.find((e) => e.source === 'datasources')?.error ?? '',
)

/** 「本块失败于 14:02:11 · 类型名」——错误只报类型名(沿用 /v1/health 纪律)。 */
function degLine(entries: OverviewDegradedEntry[]): string {
  if (!entries.length) return t('ovDegradedUnknown', ui.lang)
  const head = entries
    .slice(0, 3)
    .map(
      (e) =>
        e.source +
        ': ' +
        e.error +
        ' · ' +
        t('ovFailedAt', ui.lang) +
        ' ' +
        fmtClock(e.at),
    )
    .join('; ')
  return entries.length > 3 ? head + '; +' + (entries.length - 3) : head
}

/* ── KPI row ───────────────────────────────────────────────────────────── */
interface KpiTileSpec {
  key: string
  label: string
  value: string
  sub: string
  anchor?: string
  to?: string
}

/** 计数纪律:null → 「—」;不精确 → 「≥ N」,绝不给出偏低的精确数。 */
function fmtCount(count: number | null, exact: boolean): string {
  if (count === null) return '—'
  return exact ? String(count) : `≥ ${count}`
}

function sumOrNull(values_: (number | null)[] | undefined | null): number | null {
  if (!values_ || !values_.length) return null
  if (values_.some((v) => v === null)) return null
  return (values_ as number[]).reduce((a, b) => a + b, 0)
}

const refusedTotal = computed(() =>
  sumOrNull(payload.value?.datasources?.map((r) => r.refused)),
)
const driftTotal = computed(() =>
  sumOrNull(payload.value?.datasources?.map((r) => r.drift_open)),
)
const driftExact = computed(() => {
  const rows = payload.value?.datasources
  return !!rows && rows.length > 0 && rows.every((r) => r.drift_count_exact)
})

const usageEmpty = computed(
  () => !!payload.value?.usage && payload.value.usage.questions === 0,
)

const winSub = computed(() => t('ovKpiWindowSub', ui.lang, apiWindow.value))

const kpiTiles = computed<KpiTileSpec[]>(() => {
  const p = payload.value
  const usage = p?.usage ?? null
  const todos = p?.todos ?? null
  const job = todos?.items.find((i) => i.kind === 'job_failed') ?? null
  const jobsOk = job !== null && job.count !== null
  const dsOk = Array.isArray(p?.datasources) && p.datasources.length > 0
  return [
    {
      key: 'todos',
      label: t('ovKpiTodos', ui.lang),
      value: todos ? fmtCount(todos.total, todos.count_exact) : '—',
      sub: todos ? t('ovKpiTodosSub', ui.lang) : t('ovKpiNoData', ui.lang),
      anchor: 'todos',
    },
    {
      key: 'questions',
      label: t('ovKpiQuestions', ui.lang),
      value: usage ? fmtCount(usage.questions, usage.count_exact) : '—',
      sub: usage ? winSub.value : t('ovKpiNoData', ui.lang),
      to: opsHref.value,
    },
    {
      key: 'success',
      label: t('ovKpiSuccess', ui.lang),
      value:
        usage && usage.success_rate !== null
          ? (usage.success_rate * 100).toFixed(1) + '%'
          : '—',
      sub: !usage
        ? t('ovKpiNoData', ui.lang)
        : usage.questions === 0
          ? t('ovKpiSuccessSubEmpty', ui.lang)
          : usage.sample_capped
            ? t('ovKpiSampleCapped', ui.lang)
            : winSub.value,
      to: opsHref.value,
    },
    {
      key: 'failed_jobs',
      label: t('ovKpiFailedJobs', ui.lang),
      value: jobsOk ? fmtCount(job.count, job.count_exact) : '—',
      sub: jobsOk ? winSub.value : t('ovKpiNoData', ui.lang),
      to: '/admin/jobs?status=error',
    },
    {
      key: 'refused',
      label: t('ovKpiRefused', ui.lang),
      value: dsOk ? fmtCount(refusedTotal.value, true) : '—',
      sub: dsOk ? t('ovKpiRefusedSub', ui.lang) : t('ovKpiNoData', ui.lang),
      to: '/admin/kb?tab=assets&prob=1',
    },
    {
      key: 'drift',
      label: t('ovKpiDrift', ui.lang),
      value: dsOk ? fmtCount(driftTotal.value, driftExact.value) : '—',
      sub: dsOk ? t('ovKpiDriftSub', ui.lang) : t('ovKpiNoData', ui.lang),
      to: '/admin/datasources',
    },
  ]
})

/** Quality/cost drill-down is /admin/ops?tab=usage (P4, P6 跨文档决定)。P4 未合入
 *  时不留死链,退回审计页的 query.execute 过滤 —— 问答量的原始证据就在那里。 */
const opsHref = computed(() =>
  router.hasRoute('admin-ops')
    ? '/admin/ops?tab=usage&win=' + apiWindow.value
    : '/admin/audit?action=query.execute',
)

function onKpi(tile: KpiTileSpec) {
  if (tile.anchor) {
    goAnchor(tile.anchor)
    return
  }
  if (tile.to) void router.push(tile.to)
}

/* ── anchors (#todos / #datasources — P6 的 hash 协议,页面负责滚动+焦点) ── */
function scrollToAnchor(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  el.focus?.()
  el.scrollIntoView?.({ block: 'start' })
}

function goAnchor(id: string) {
  const hash = '#' + id
  if (route.hash === hash) {
    scrollToAnchor(id)
    return
  }
  void router.push({ hash })
}

watch(
  () => route.hash,
  (hash) => {
    if (hash) void nextTick().then(() => scrollToAnchor(hash.slice(1)))
  },
)

/* ── datasource table ──────────────────────────────────────────────────── */
const dsColumns = computed<DataTableColumn[]>(() => [
  { key: 'name', label: t('datasources', ui.lang) },
  { key: 'status', label: t('ovColConn', ui.lang), width: 110 },
  { key: 'kb', label: t('ovColKbInit', ui.lang), width: 130 },
  { key: 'assets', label: t('ovColAssets', ui.lang) },
  { key: 'readonly', label: t('ovColReadonly', ui.lang), width: 150 },
  { key: 'drift', label: t('ovColDrift', ui.lang), width: 90, align: 'right' },
])

/** DataTable cells receive `unknown`; the row shape is ours. */
function d(row: unknown): OverviewDatasourceRow {
  return row as OverviewDatasourceRow
}

function healthPing(row: OverviewDatasourceRow): OverviewDsPing | null {
  return payload.value?.health?.datasources?.[row.name] ?? null
}

function dsUnreachable(row: OverviewDatasourceRow): boolean {
  if (row.status !== 'connected') return true
  const ping = healthPing(row)
  return ping !== null && !ping.ok
}

const READONLY_LABEL: Record<string, keyof typeof import('../../i18n').messages['zh']> = {
  grants: 'ovRoYes',
  unverifiable: 'ovRoUnverifiable',
  probe_failed: 'ovRoProbeFailed',
  not_probed: 'ovRoNotProbed',
}

function roLabel(row: OverviewDatasourceRow): string {
  if (row.readonly?.verified === false) return t('ovRoNo', ui.lang)
  return t(READONLY_LABEL[row.readonly?.basis] ?? 'ovRoNotProbed', ui.lang)
}

function roClass(row: OverviewDatasourceRow): string {
  if (row.readonly?.verified === true) return 'is-ok'
  if (row.readonly?.verified === false) return 'is-bad'
  return 'is-idle'
}

function assetLine(row: OverviewDatasourceRow): string {
  return Object.entries(row.kb_items ?? {})
    .map(([kind, n]) => n + ' ' + kind)
    .join(' · ')
}

function assetTitle(row: OverviewDatasourceRow): string {
  return Object.entries(row.kb_items ?? {})
    .map(([kind, n]) => kind + ': ' + n)
    .join('\n')
}

const kbInitializedCount = computed(() => {
  const rows = payload.value?.datasources
  if (!rows || !rows.length) return null
  if (rows.some((r) => r.kb_initialized === null)) return null
  return rows.filter((r) => r.kb_initialized).length
})

/* ── drawer ────────────────────────────────────────────────────────────── */
const drawerOpen = ref(false)
const drawerRow = ref<OverviewDatasourceRow | null>(null)
const drawerPing = computed(() =>
  drawerRow.value ? healthPing(drawerRow.value) : null,
)

function openDs(row: unknown) {
  drawerRow.value = row as OverviewDatasourceRow
  drawerOpen.value = true
}
</script>

<style scoped>
.ov-asof {
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

/* window segmented control */
.win-seg {
  display: inline-flex;
  padding: 2px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
}
.seg-btn {
  height: 24px;
  padding: 0 var(--sp-3);
  border-radius: var(--r-sm);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  font-weight: 500;
}
.seg-btn:hover {
  color: var(--text-primary);
}
.seg-btn.is-active {
  background: var(--surface-raised);
  color: var(--accent-active);
  box-shadow: var(--shadow-xs);
}
.seg-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}

/* KPI row */
.kpi-row {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--sp-2);
}
@media (max-width: 1100px) {
  .kpi-row {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 720px) {
  .kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

/* skeleton */
.ov-skeleton {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.sk-bar {
  display: block;
  border-radius: var(--r-md);
  background: linear-gradient(
    90deg,
    var(--surface-muted) 25%,
    var(--border-subtle) 50%,
    var(--surface-muted) 75%
  );
  background-size: 200% 100%;
  animation: ov-shimmer 1.2s linear infinite;
}
.sk-banner {
  height: 62px;
}
.sk-kpis {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--sp-2);
}
.sk-kpi {
  height: 64px;
}
.sk-cols {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  gap: var(--sp-4);
}
.sk-panel {
  height: 260px;
}
@keyframes ov-shimmer {
  from {
    background-position: 200% 0;
  }
  to {
    background-position: -200% 0;
  }
}
.ov-slow {
  margin: 0;
  text-align: center;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}

/* strips (usage / degraded notes) */
.ov-strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  font-size: var(--fs-2xs);
}
.ov-strip-text {
  font-weight: 500;
  color: var(--text-primary);
}
.ov-strip-hint {
  color: var(--text-tertiary);
}
.ov-strip-btn {
  height: 22px;
  display: inline-flex;
  align-items: center;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-2xs);
  text-decoration: none;
}
.ov-strip-btn:hover {
  border-color: var(--border-strong);
  background: var(--surface-hover);
}
.ov-strip-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
.ov-fail-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 var(--sp-2);
  border-radius: var(--r-full);
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}
.ov-fail-chip b {
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.ov-warn-strip {
  margin: 0 0 var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--warn);
  border-radius: var(--r-sm);
  background: var(--warn-bg);
  color: var(--warn-text);
  font-size: var(--fs-2xs);
}

/* cards & layout */
.ov-card {
  padding: var(--sp-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}
.ov-card:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.ov-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
}
.ov-card-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.ov-card-sub {
  margin: 2px 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.ov-total {
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.ov-card-link {
  flex: none;
  font-size: var(--fs-2xs);
  color: var(--accent-hover);
  text-decoration: none;
  border-radius: var(--r-sm);
}
.ov-card-link:hover {
  text-decoration: underline;
}
.ov-card-link:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.ov-cols {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  gap: var(--sp-4);
  align-items: start;
}
.ov-aside {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.ov-muted {
  margin: 0;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
  line-height: var(--lh-relaxed);
}

/* table cells */
.ds-name {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-weight: 500;
}
.ds-dot {
  width: 6px;
  height: 6px;
  flex: none;
  border-radius: var(--r-full);
  background: var(--ok);
}
.ds-name.is-bad .ds-dot {
  background: var(--danger);
}
.ds-name.is-bad {
  color: var(--danger);
}
.ov-pill {
  display: inline-flex;
  align-items: center;
  padding: 0 var(--sp-2);
  border-radius: var(--r-full);
  font-size: var(--fs-2xs);
  white-space: nowrap;
}
.ov-pill.is-ok {
  background: var(--ok-bg);
  color: var(--ok);
}
.ov-pill.is-bad {
  background: var(--danger-bg);
  color: var(--danger);
}
.ov-pill.is-idle {
  background: var(--surface-muted);
  color: var(--text-tertiary);
}
.ov-dash {
  color: var(--text-tertiary);
}
.ds-assets {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}
.ds-drift {
  font-variant-numeric: tabular-nums;
}
.ds-drift.is-inexact {
  color: var(--warn);
}

/* events */
.ov-events {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}
.oe-link {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr) auto auto;
  align-items: baseline;
  gap: var(--sp-2);
  padding: var(--sp-1) 0;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  text-decoration: none;
}
.ov-events li:last-child .oe-link {
  border-bottom: none;
}
.oe-link:hover {
  color: var(--text-primary);
}
.oe-link:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: -2px;
}
.oe-time {
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.oe-action {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
}
.oe-status.is-ok {
  color: var(--text-tertiary);
}
.oe-status.is-bad {
  color: var(--danger);
  font-weight: 600;
}

/* first-empty */
.ov-first-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-10) var(--sp-6);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  text-align: center;
}
.ofe-title {
  margin: 0;
  font-size: var(--fs-lg);
  font-weight: 600;
  color: var(--text-primary);
}
.ofe-desc {
  margin: 0;
  max-width: 52ch;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
}
.ofe-cta {
  display: inline-flex;
  align-items: center;
  height: 32px;
  margin-top: var(--sp-2);
  padding: 0 var(--sp-5);
  border-radius: var(--r-md);
  background: var(--accent);
  color: var(--on-accent);
  font-size: var(--fs-xs);
  font-weight: 500;
  text-decoration: none;
}
.ofe-cta:hover {
  background: var(--accent-hover);
}
.ofe-cta:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.ofe-wizard {
  width: 100%;
  max-width: 560px;
  margin-top: var(--sp-4);
  text-align: left;
}

.ov-raw-probe {
  font-size: var(--fs-2xs);
  color: var(--accent-hover);
}

/* drawer */
.dsh {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}
.dsh-name {
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--text-primary);
}
.dd-sec {
  margin-bottom: var(--sp-4);
}
.dd-sec h4 {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-primary);
}
.dd-facts {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: var(--sp-1) var(--sp-3);
  margin: 0;
  font-size: var(--fs-2xs);
}
.dd-facts dt {
  color: var(--text-tertiary);
}
.dd-facts dd {
  margin: 0;
  color: var(--text-primary);
}
.dd-mono {
  margin-left: var(--sp-1);
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}
.dd-assets {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
  margin: var(--sp-2) 0 0;
}
.dd-go {
  display: inline-flex;
  align-items: center;
  height: 28px;
  padding: 0 var(--sp-4);
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-2xs);
  font-weight: 500;
  text-decoration: none;
}
.dd-go:hover {
  border-color: var(--border-strong);
  background: var(--surface-hover);
}

@media (max-width: 1100px) {
  .ov-cols,
  .sk-cols {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (prefers-reduced-motion: reduce) {
  .sk-bar {
    animation: none;
  }
}
</style>
