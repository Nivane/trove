<!--
  StyleguideView — /admin/styleguide (P7-W4).

  A living specimen room: the six base components rendered by their real
  implementations (never parallel copies), driven through the §6.3 state
  matrix, plus the density registers and the status palette read from the
  actual tokens. Reached by URL only — the W0-frozen 15-item IA stays as is.

  Nothing here is copy-pasted from the product; every string comes from i18n
  and every size/color from tokens.css.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import PageHeader from '../../components/base/PageHeader.vue'
import DataTable from '../../components/base/DataTable.vue'
import type { DataTableColumn } from '../../components/base/DataTable.vue'
import StatePanel from '../../components/base/StatePanel.vue'
import KpiTile from '../../components/base/KpiTile.vue'
import ConfirmDialog from '../../components/base/ConfirmDialog.vue'
import DetailDrawer from '../../components/base/DetailDrawer.vue'
import VerifyStrip from '../../components/chat/VerifyStrip.vue'
import StepCard from '../../components/chat/StepCard.vue'
import MarkdownView from '../../components/chat/MarkdownView.vue'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { barWidthPx } from '../../utils/steps'

const ui = useUiStore()

/* ── state matrix (§6.3) ───────────────────────────────────────────────
   The six states are the switcher; `live` is the post-action view the
   "clear filters" button lands on (populated table, nothing in flight). */
type DemoState =
  | 'live'
  | 'emptyFirst'
  | 'emptyFiltered'
  | 'loading'
  | 'refresh'
  | 'error'
  | 'forbidden'

type I18nKey = keyof typeof import('../../i18n').messages['zh']

const STATES: { key: DemoState; labelKey: I18nKey }[] = [
  { key: 'emptyFirst', labelKey: 'sgStateEmptyFirst' },
  { key: 'emptyFiltered', labelKey: 'sgStateEmptyFiltered' },
  { key: 'loading', labelKey: 'sgStateLoading' },
  { key: 'refresh', labelKey: 'sgStateRefresh' },
  { key: 'error', labelKey: 'sgStateError' },
  { key: 'forbidden', labelKey: 'sgStateForbidden' },
]

const state = ref<DemoState>('live')
const lastError = ref('')

function pick(next: DemoState) {
  state.value = next
  if (next === 'error' && !lastError.value) lastError.value = 'GET /v1/admin/datasources → 504'
}

const tableLoading = computed(() => state.value === 'loading' || state.value === 'refresh')
const tableRows = computed(() =>
  state.value === 'loading' || state.value === 'emptyFirst' || state.value === 'emptyFiltered'
    ? []
    : demoRows,
)
const showTable = computed(() => state.value !== 'error' && state.value !== 'forbidden')

/* ── the demo list: id (mono) / table (mono) / rows (numeric) / updated ── */
const columns = computed<DataTableColumn[]>(() => [
  { key: 'id', label: t('sgColId', ui.lang), mono: true, width: 150 },
  { key: 'table', label: t('sgColTable', ui.lang), mono: true, sortable: true },
  {
    key: 'rows',
    label: t('sgColRows', ui.lang),
    numeric: true,
    sortable: true,
    defaultDir: 'desc',
    width: 110,
  },
  { key: 'updated', label: t('sgColUpdated', ui.lang), width: 160 },
])

const demoRows = [
  { id: 'ds_financial', table: 'loan', rows: 682, updated: '2026-10-01 09:12' },
  { id: 'ds_financial', table: 'account', rows: 4500, updated: '2026-10-01 09:12' },
  { id: 'ds_financial', table: 'client', rows: 5369, updated: '2026-10-01 09:12' },
  { id: 'ds_financial', table: 'district', rows: 77, updated: '2026-09-28 18:40' },
]

const miniColumns = computed<DataTableColumn[]>(() => [
  { key: 'name', label: t('sgColTable', ui.lang) },
  { key: 'rows', label: t('sgColRows', ui.lang), numeric: true },
])
const miniRows = computed(() => [
  { name: 'loan', rows: 682 },
  { name: 'account', rows: 4500 },
])

/* ── answer card (验证条 + 计时条):样例是一条走满六段的成功链 ── */
const sgSteps = [
  { node: 'route_intent', payload: { node: 'route_intent', elapsed_ms: 2400 } },
  { node: 'schema_linking', payload: { node: 'schema_linking', elapsed_ms: 3100 } },
  { node: 'query_sketch', payload: { node: 'query_sketch', elapsed_ms: 900 } },
  { node: 'gen_sql', payload: { node: 'gen_sql', elapsed_ms: 3400, sql: 'SELECT ...' } },
  { node: 'execute_sql', payload: { node: 'execute_sql', elapsed_ms: 1800, row_count: 12 } },
  { node: 'validate', payload: { node: 'validate', elapsed_ms: 300, rules_passed: true } },
  { node: 'reflect', payload: { node: 'reflect', elapsed_ms: 700, verdict: 'OK', retry_count: 0 } },
  { node: 'output', payload: { node: 'output', elapsed_ms: 500 } },
]
const sgSummary = { verdict: 'OK' }
const sgMaxMs = 3400
const sgBars = sgSteps.map((s) => barWidthPx(s.payload.elapsed_ms, sgMaxMs))
const sgConclusion = [
  '### 结论',
  '',
  '贷款金额最高的是 590,820。',
  '',
  '### 洞察',
  '',
  '- 贷款集中在少数几个地区。',
].join('\n')

/* ── density: read both registers from the live cascade ──────────────── */
const pageEl = ref<HTMLElement | null>(null)
const rootDensity = ref({ row: '', control: '' })
const scopeDensity = ref({ row: '', control: '' })

onMounted(() => {
  const root = getComputedStyle(document.documentElement)
  rootDensity.value = {
    row: root.getPropertyValue('--density-row-h').trim(),
    control: root.getPropertyValue('--density-control-h').trim(),
  }
  if (pageEl.value) {
    const cs = getComputedStyle(pageEl.value)
    scopeDensity.value = {
      row: cs.getPropertyValue('--density-row-h').trim(),
      control: cs.getPropertyValue('--density-control-h').trim(),
    }
  }
})

/* The comfortable specimen keeps the page's console scope but re-declares
   the root register on its own subtree — tokens all the way down. The
   literals only stand in when the cascade cannot be read (jsdom / SSR);
   in the browser the live root values win. */
const chatDensityStyle = computed(() => ({
  '--density-row-h': rootDensity.value.row || '40px',
  '--density-control-h': rootDensity.value.control || '34px',
}))

/* ── KPI / dialog / drawer specimens ─────────────────────────────────── */
const kpiActive = ref(true)
const confirmOpen = ref(false)
const confirmLoading = ref(false)
const drawerOpen = ref(false)

const palette = computed(() => [
  { key: 'ok', label: t('sgPaletteOk', ui.lang) },
  { key: 'warn', label: t('sgPaletteWarn', ui.lang) },
  { key: 'danger', label: t('sgPaletteDanger', ui.lang) },
  { key: 'info', label: t('sgPaletteInfo', ui.lang) },
])
</script>

<template>
  <div ref="pageEl" class="admin-view sg-page">
    <PageHeader
      :title="t('styleguideTitle', ui.lang)"
      :description="t('styleguideDesc', ui.lang)"
      :breadcrumbs="[
        { label: t('sgCrumbRoot', ui.lang), to: '/admin' },
        { label: t('sgCrumbCurrent', ui.lang) },
      ]"
    />

    <!-- state switcher: drives every state-bearing specimen below -->
    <div class="sg-switch" role="group" :aria-label="t('sgStateLabel', ui.lang)">
      <button
        v-for="s in STATES"
        :key="s.key"
        type="button"
        class="sg-switch-btn"
        :class="{ 'is-on': state === s.key }"
        :aria-pressed="state === s.key"
        @click="pick(s.key)"
      >
        {{ t(s.labelKey, ui.lang) }}
      </button>
    </div>

    <!-- ── DataTable × state matrix ── -->
    <section class="sg-card">
      <h2 class="sg-card-title">DataTable</h2>
      <p class="sg-card-note">{{ t('sgDataTableNote', ui.lang) }}</p>

      <DataTable
        v-if="showTable"
        :columns="columns"
        :rows="tableRows"
        row-key="id"
        selectable
        :loading="tableLoading"
        :skeleton-rows="4"
        :select-all-label="t('sgSelectAll', ui.lang)"
      >
        <template #empty>
          <StatePanel
            mode="empty"
            :title="
              state === 'emptyFiltered'
                ? t('sgEmptyFilteredTitle', ui.lang)
                : t('sgEmptyFirstTitle', ui.lang)
            "
            :description="
              state === 'emptyFiltered'
                ? t('sgEmptyFilteredDesc', ui.lang)
                : t('sgEmptyFirstDesc', ui.lang)
            "
          >
            <template #action>
              <!-- the two guidance actions the matrix prescribes: go register,
                   or clear the filters and land back on a populated list -->
              <RouterLink
                v-if="state !== 'emptyFiltered'"
                class="sg-btn"
                to="/admin/datasources"
              >
                {{ t('sgEmptyFirstAction', ui.lang) }}
              </RouterLink>
              <button v-else type="button" class="sg-btn" @click="state = 'live'">
                {{ t('sgClearFilters', ui.lang) }}
              </button>
            </template>
          </StatePanel>
        </template>
      </DataTable>

      <StatePanel
        v-else-if="state === 'error'"
        mode="error"
        :title="t('sgErrorTitle', ui.lang)"
        :description="t('sgErrorDesc', ui.lang)"
        :detail="lastError"
        :retry-text="t('sgErrorRetry', ui.lang)"
        @retry="state = 'refresh'"
      />
      <StatePanel
        v-else
        mode="empty"
        :title="t('sgForbiddenTitle', ui.lang)"
        :description="t('sgForbiddenDesc', ui.lang)"
      />
    </section>

    <!-- ── StatePanel, all three modes side by side ── -->
    <section class="sg-card">
      <h2 class="sg-card-title">StatePanel</h2>
      <p class="sg-card-note">{{ t('sgStatePanelNote', ui.lang) }}</p>
      <div class="sg-grid">
        <div class="sg-specimen">
          <StatePanel mode="loading" :title="t('sgLoadingTitle', ui.lang)" />
        </div>
        <div class="sg-specimen">
          <StatePanel
            mode="empty"
            :title="t('sgEmptyFirstTitle', ui.lang)"
            :description="t('sgEmptyFirstDesc', ui.lang)"
          />
        </div>
        <div class="sg-specimen">
          <StatePanel
            mode="error"
            :title="t('sgErrorTitle', ui.lang)"
            :description="t('sgErrorDesc', ui.lang)"
            :retry-text="t('sgErrorRetry', ui.lang)"
            @retry="pick('refresh')"
          />
        </div>
      </div>
    </section>

    <!-- ── KpiTile: KPI as filter ── -->
    <section class="sg-card">
      <h2 class="sg-card-title">KpiTile</h2>
      <p class="sg-card-note">{{ t('sgKpiNote', ui.lang) }}</p>
      <div class="sg-kpis">
        <KpiTile
          :label="t('sgKpiUsers', ui.lang)"
          :value="128"
          :sub="t('sgKpiNoDatasource', ui.lang)"
          :active="kpiActive"
          @click="kpiActive = !kpiActive"
        />
        <KpiTile :label="t('sgKpiQueries', ui.lang)" value="1,024" />
      </div>
    </section>

    <!-- ── density registers ── -->
    <section class="sg-card">
      <h2 class="sg-card-title">{{ t('sgDensityTitle', ui.lang) }}</h2>
      <p class="sg-card-note">{{ t('sgDensityNote', ui.lang) }}</p>
      <div class="sg-grid">
        <div class="sg-density is-chat" :style="chatDensityStyle">
          <div class="sg-density-head">
            {{ t('sgDensityChat', ui.lang) }}
            <span class="sg-density-vals">
              {{ t('sgDensityRow', ui.lang) }} {{ rootDensity.row || '40px' }} ·
              {{ t('sgDensityControl', ui.lang) }} {{ rootDensity.control || '34px' }}
            </span>
          </div>
          <DataTable :columns="miniColumns" :rows="miniRows" row-key="name" />
          <button type="button" class="sg-btn">{{ t('sgDemoRow', ui.lang) }}</button>
        </div>
        <div class="sg-density is-console">
          <div class="sg-density-head">
            {{ t('sgDensityConsole', ui.lang) }}
            <span class="sg-density-vals">
              {{ t('sgDensityRow', ui.lang) }} {{ scopeDensity.row || '32px' }} ·
              {{ t('sgDensityControl', ui.lang) }} {{ scopeDensity.control || '30px' }}
            </span>
          </div>
          <DataTable :columns="miniColumns" :rows="miniRows" row-key="name" />
          <button type="button" class="sg-btn">{{ t('sgDemoRow', ui.lang) }}</button>
        </div>
      </div>
    </section>

    <!-- ── status / severity palette ── -->
    <section class="sg-card">
      <h2 class="sg-card-title">{{ t('sgPaletteTitle', ui.lang) }}</h2>
      <p class="sg-card-note">{{ t('sgPaletteNote', ui.lang) }}</p>
      <div class="sg-grid sg-grid-4">
        <div v-for="fam in palette" :key="fam.key" class="sg-fam" :class="`is-${fam.key}`">
          <span class="sg-fam-chip">{{ fam.label }}</span>
          <span class="sg-fam-mark" aria-hidden="true" />
          <span class="sg-fam-detail">text / bg / border</span>
        </div>
      </div>
    </section>

    <!-- ── ConfirmDialog ── -->
    <section class="sg-card">
      <h2 class="sg-card-title">ConfirmDialog</h2>
      <p class="sg-card-note">{{ t('sgConfirmNote', ui.lang) }}</p>
      <div class="sg-actions">
        <button type="button" class="sg-btn" @click="confirmOpen = true">
          {{ t('sgOpenConfirm', ui.lang) }}
        </button>
        <label class="sg-check">
          <input v-model="confirmLoading" type="checkbox">
          {{ t('sgConfirmLoading', ui.lang) }}
        </label>
      </div>
      <ConfirmDialog
        v-model="confirmOpen"
        :title="t('sgConfirmHeading', ui.lang)"
        :confirm-text="t('sgConfirmOk', ui.lang)"
        :cancel-text="t('sgCancel', ui.lang)"
        :loading="confirmLoading"
        danger
      >
        <template #impact>{{ t('sgConfirmImpact', ui.lang) }}</template>
      </ConfirmDialog>
    </section>

    <!-- ── DetailDrawer ── -->
    <section class="sg-card">
      <h2 class="sg-card-title">DetailDrawer</h2>
      <p class="sg-card-note">{{ t('sgDrawerNote', ui.lang) }}</p>
      <div class="sg-actions">
        <button type="button" class="sg-btn" @click="drawerOpen = true">
          {{ t('sgOpenDrawer', ui.lang) }}
        </button>
      </div>
      <DetailDrawer
        v-model="drawerOpen"
        :title="t('sgDrawerHeading', ui.lang)"
        :close-label="t('sgDrawerClose', ui.lang)"
      >
        <p class="sg-drawer-body">{{ t('sgDrawerBody', ui.lang) }}</p>
      </DetailDrawer>
    </section>

    <!-- ── Answer card: verify strip + conclusion + step timing bars ── -->
    <section class="sg-card">
      <h2 class="sg-card-title">{{ t('sgVerifyTitle', ui.lang) }}</h2>
      <p class="sg-card-note">{{ t('sgVerifyDesc', ui.lang) }}</p>
      <VerifyStrip :steps="sgSteps" :summary="sgSummary" />
      <div class="sg-answer">
        <MarkdownView hero :source="sgConclusion" />
      </div>
      <div class="sg-stepbars">
        <div class="step-group-head">
          <span>{{ t('sgStepBars', ui.lang) }}</span>
          <span class="cnt">3</span>
        </div>
        <StepCard
          v-for="(s, k) in sgSteps.slice(3, 6)"
          :key="k"
          :card="s"
          :bar="sgBars[k + 3]"
        />
      </div>
    </section>

    <!-- ── PageHeader (specimen copy; never fights document.title) ── -->
    <section class="sg-card">
      <h2 class="sg-card-title">PageHeader</h2>
      <p class="sg-card-note">{{ t('sgPageHeaderNote', ui.lang) }}</p>
      <div class="sg-specimen">
        <PageHeader
          :title="t('sgDrawerHeading', ui.lang)"
          :description="t('styleguideDesc', ui.lang)"
          :sync-document-title="false"
          :breadcrumbs="[
            { label: t('sgCrumbRoot', ui.lang), to: '/admin' },
            { label: t('sgCrumbCurrent', ui.lang) },
          ]"
        />
      </div>
    </section>
  </div>
</template>

<style scoped>
.sg-page {
  gap: var(--sp-4);
}

/* state switcher */
.sg-switch {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
}
.sg-switch-btn {
  height: var(--density-control-h, 30px);
  padding: 0 var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-full);
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}
.sg-switch-btn:hover {
  border-color: var(--border-default);
  color: var(--text-primary);
}
.sg-switch-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
.sg-switch-btn.is-on {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent-active);
  font-weight: 600;
}

/* cards */
.sg-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-4) var(--sp-5);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}
.sg-card-title {
  margin: 0;
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--text-primary);
}
.sg-card-note {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.sg-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: var(--sp-3);
}
.sg-grid-4 {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}

.sg-specimen {
  border: 1px dashed var(--border-default); /* decorative wrapper only */
  border-radius: var(--r-md);
  overflow: hidden;
}

/* density registers */
.sg-density {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-canvas);
}
.sg-density-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-primary);
}
.sg-density-vals {
  font-family: var(--font-ident, var(--font-mono));
  font-size: var(--fs-2xs);
  font-weight: 400;
  color: var(--text-secondary);
}
.sg-density .data-table {
  width: 100%;
}

/* palette */
.sg-fam {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
}
.sg-fam-chip {
  padding: 2px var(--sp-2);
  border-radius: var(--r-full);
  font-size: var(--fs-2xs);
}
.sg-fam-mark {
  width: 10px;
  height: 10px;
  border-radius: var(--r-full);
}
.sg-fam-detail {
  font-family: var(--font-ident, var(--font-mono));
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}

.sg-fam.is-ok .sg-fam-chip {
  background: var(--ok-bg);
  color: var(--ok-text);
  border: 1px solid var(--ok-border);
}
.sg-fam.is-ok .sg-fam-mark {
  background: var(--ok);
}
.sg-fam.is-warn .sg-fam-chip {
  background: var(--warn-bg);
  color: var(--warn-text);
  border: 1px solid var(--warn-border);
}
.sg-fam.is-warn .sg-fam-mark {
  background: var(--warn);
}
.sg-fam.is-danger .sg-fam-chip {
  background: var(--danger-bg);
  color: var(--danger-text);
  border: 1px solid var(--danger-border);
}
.sg-fam.is-danger .sg-fam-mark {
  background: var(--danger);
}
.sg-fam.is-info .sg-fam-chip {
  background: var(--info-bg);
  color: var(--info-text);
  border: 1px solid var(--info-border);
}
.sg-fam.is-info .sg-fam-mark {
  background: var(--info);
}

/* demo controls */
.sg-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-3);
}
.sg-btn {
  display: inline-flex;
  align-items: center;
  height: var(--density-control-h, 30px);
  padding: 0 var(--sp-4);
  border: 1px solid var(--accent);
  border-radius: var(--r-sm);
  background: var(--accent);
  color: var(--on-accent);
  font-size: var(--fs-xs);
  font-weight: 500;
}
.sg-btn:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
.sg-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.sg-check {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.sg-drawer-body {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  line-height: var(--lh-relaxed);
}

/* answer-card specimen: 卡片里让答案/步骤各占一块,互不贴边 */
.sg-answer {
  padding: var(--sp-1) 0;
}
.sg-stepbars {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  max-width: 320px; /* 面板真实宽度,计时条比例才是所见即所得 */
}
</style>
