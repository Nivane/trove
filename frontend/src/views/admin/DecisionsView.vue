<template>
  <div class="admin-view">
    <PageHeader
      :title="t('decisions', ui.lang)"
      :description="t('decisionsPageDesc', ui.lang)"
      :breadcrumbs="crumbs"
    >
      <template #actions>
        <el-select
          v-model="values.ds"
          class="ds-select"
          :placeholder="t('kbSelectDs', ui.lang)"
          :aria-label="t('kbSelectDs', ui.lang)"
          @change="load"
        >
          <el-option
            v-for="d in connected"
            :key="d.name"
            :value="d.name"
            :label="d.name"
          />
        </el-select>
        <el-button :loading="loading" @click="load">
          <RefreshCw :size="14" />
        </el-button>
        <el-button type="primary" @click="openEditor">
          <Pencil :size="14" />
          {{ t('decisionsEdit', ui.lang) }}
        </el-button>
      </template>
    </PageHeader>

    <div v-if="issues.length" class="admin-card decisions-issues">
      <div class="card-header">
        <div class="card-title">{{ t('decisionsIssues', ui.lang) }}</div>
        <div class="card-actions">
          <span class="pill pill-warn">{{ issues.length }}</span>
        </div>
      </div>
      <ul>
        <li v-for="(i, idx) in issues" :key="idx" class="cell-mono">{{ i }}</li>
      </ul>
    </div>

    <div class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('decisions', ui.lang) }}</div>
        <div class="card-actions">
          <span v-if="digest" class="cell-mono dim">sha256:{{ digest.slice(0, 12) }}</span>
        </div>
      </div>
      <el-table
        v-loading="loading"
        :data="rules"
        class="admin-table"
        max-height="var(--table-max-h)"
      >
        <template #empty>
          <TableEmpty>{{ t('decisionsEmpty', ui.lang) }}</TableEmpty>
        </template>
        <!-- 列宽合计刻意压在内容列宽(~1175px)以内:9 列都按内容能放下的
             最小宽取,超出就只剩横滚 —— 而横滚会让最后一列(判定历史)
             整列消失。收窄只减内边距,不做 show-overflow-tooltip 之类的
             隐藏:文字放不下时换行,不截断。 -->
        <el-table-column :label="t('decisionsRuleId', ui.lang)" width="140">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.id }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobName', ui.lang)" min-width="130">
          <template #default="{ row }">{{ row.name || '—' }}</template>
        </el-table-column>
        <el-table-column :label="t('decisionsSeverity', ui.lang)" width="100">
          <template #default="{ row }">
            <span class="pill" :class="severityClass(row.severity)">
              {{ row.severity }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsWindow', ui.lang)" width="110">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.window || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsConditions', ui.lang)" min-width="190">
          <template #default="{ row }">
            <div v-for="(c, i) in row.conditions" :key="i" class="cell-mono cond">
              <span class="dim">{{ row.condition_mode }}</span> {{ c }}
            </div>
            <!-- 噪声带是触发条件的一部分(条件命中还要超带才算数),所以它
                 渲染在条件列内而不是新开一列:表格宽度是收紧过的,新列
                 会把判定历史挤出可视区。未声明 → 不渲染,与历史一致。 -->
            <div v-if="row.seasonal" class="cell-mono cond band-line">
              <span class="dim">{{ t('decisionsSeasonal', ui.lang) }}</span>
              {{ row.seasonal.grain || 'auto' }} × {{ row.seasonal.lookback }} · k={{ row.seasonal.k }}
              <span
                v-if="row.significance"
                class="pill"
                :class="row.significance.require === 'outside_band' ? 'pill-warn' : 'pill-neutral'"
              >
                {{ row.significance.require === 'outside_band'
                  ? t('decisionsBandRequired', ui.lang)
                  : t('decisionsBandRecord', ui.lang) }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobEnabled', ui.lang)" width="80">
          <template #default="{ row }">
            <span class="pill" :class="row.enabled ? 'pill-ok' : 'pill-neutral'">
              {{ row.enabled ? t('enable', ui.lang) : t('disable', ui.lang) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsLatest', ui.lang)" min-width="170">
          <template #default="{ row }">
            <div v-if="row.latest_verdict" class="decisions-latest">
              <span
                class="pill"
                :class="verdictStatusClass(row.latest_verdict.status)"
              >
                {{ row.latest_verdict.status }}
              </span>
              <span class="dim cell-mono">
                {{ fmtDateTime(row.latest_verdict.evaluated_at) }}
              </span>
            </div>
            <span v-else class="dim">{{ t('decisionsNeverJudged', ui.lang) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsReferencedBy', ui.lang)" min-width="150">
          <template #default="{ row }">
            <div v-for="(j, i) in row.referenced_by" :key="i" class="dim">{{ j }}</div>
            <span v-if="!row.referenced_by.length" class="dim">—</span>
          </template>
        </el-table-column>
        <!-- 判定历史是这张表的唯一动作列:固定在最右,让它在任何视口宽度下
             都常驻(收窄列宽只是让它更早进入可视区,不能保证不出视口)。 -->
        <el-table-column
          :label="t('decisionsHistory', ui.lang)"
          width="100"
          fixed="right"
        >
          <template #default="{ row }">
            <el-button link size="small" @click="openHistory(row)">
              {{ t('decisionsHistoryOpen', ui.lang) }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 判定质量(B7):判定史与行动效果的回评。拿不到(本进程未接判定
         存储 / 文件损坏)就整节不渲染 —— 这是读视图,读不到不装;桶为空
         = 还没有判定史,照答并说明。 -->
    <div v-if="quality" class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('decisionsQuality', ui.lang) }}</div>
        <div class="card-actions">
          <span class="pill pill-ok">{{ quality.summary.ok }}</span>
          <span class="pill pill-warn">{{ quality.summary.alert }}</span>
          <span class="pill pill-danger">{{ quality.summary.error }}</span>
        </div>
      </div>
      <p class="view-desc decisions-quality-desc">
        {{ t('decisionsQualityDesc', ui.lang) }}
      </p>
      <div v-if="quality.degraded.length" class="decisions-quality-degraded">
        {{ t('decisionsQualityEffectsDegraded', ui.lang) }}
      </div>
      <div v-if="!quality.buckets.length" class="decisions-quality-empty dim">
        {{ t('decisionsQualityEmpty', ui.lang) }}
      </div>
      <el-table v-else :data="quality.buckets" class="admin-table" size="small">
        <el-table-column :label="t('decisionsQualityRule', ui.lang)" min-width="150">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.rule_id || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsQualityRev', ui.lang)" width="180">
          <template #default="{ row }">
            <div class="decisions-quality-rev">
              <span class="cell-mono dim">{{ revShort(row.rule_rev) }}</span>
              <span class="pill" :class="revClass(row)">{{ revLabel(row) }}</span>
            </div>
          </template>
        </el-table-column>
        <!-- 判定(正常/触发/失败)与效果(有效/无变化/判不了/失败)分开计:
             一条全是 error 的规则与一条判了但从没动到数的规则,是两种问题。 -->
        <el-table-column :label="t('decisionsQualityVerdicts', ui.lang)" width="170">
          <template #default="{ row }">
            <span class="cell-mono">
              {{ row.ok }} /
              <span class="decisions-quality-alert">{{ row.alert }}</span> /
              <span class="decisions-quality-danger">{{ row.error }}</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsQualityTriggeredRate', ui.lang)" width="100">
          <template #default="{ row }">
            <span class="cell-mono">{{ rate(row.triggered_rate) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsQualityEffects', ui.lang)" width="180">
          <template #default="{ row }">
            <span class="cell-mono">
              {{ row.effects.effective }} / {{ row.effects.no_effect }} /
              {{ row.effects.unverifiable }} / {{ row.effects.errors }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsQualityEffectiveRate', ui.lang)" min-width="170">
          <template #default="{ row }">
            <span v-if="row.effective_rate != null" class="cell-mono">
              {{ rate(row.effective_rate) }}
            </span>
            <!-- 分母不够就不给比率 —— 近似值比没有更糟;说清为什么。 -->
            <span v-else class="dim decisions-quality-insufficient">
              {{ t('decisionsQualityRateGated', ui.lang) }}
              · {{ row.insufficient.map(insuffLabel).join(' / ') }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <VerdictHistoryDrawer
      v-model="historyOpen"
      :datasource="values.ds"
      :rule-id="historyRule?.id ?? ''"
      :rule-name="historyRule?.name ?? ''"
    />

    <el-dialog
      v-model="editorOpen"
      :title="t('decisionsEdit', ui.lang)"
      width="760px"
    >
      <p class="view-desc">{{ t('decisionsEditorHint', ui.lang) }}</p>
      <el-input
        v-model="yamlText"
        type="textarea"
        :rows="22"
        class="decisions-yaml"
        spellcheck="false"
        :placeholder="'version: 1\nrules: []'"
      />
      <div v-if="saveError" class="decisions-error cell-mono">{{ saveError }}</div>
      <template #footer>
        <el-button @click="editorOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">
          {{ t('save', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Pencil, RefreshCw } from 'lucide-vue-next'
import { apiGet, apiPut } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { fmtDateTime } from '../../utils/format'
import { notifySuccess, toastError } from '../../utils/notify'
import { useListQuery } from '../../composables/useListQuery'
import TableEmpty from '../../components/admin/TableEmpty.vue'
import VerdictHistoryDrawer from '../../components/admin/VerdictHistoryDrawer.vue'
import {
  fetchDecisionQuality,
  verdictStatusClass,
  type DecisionQuality,
  type QualityBucket,
  type VerdictBrief,
} from '../../api/decisions'
import PageHeader from '../../components/base/PageHeader.vue'
import type { DatasourceInfo } from '../../api/types'

interface SeasonalBlock {
  grain: string
  lookback: number
  mode: string
  k: number
}

interface SignificanceBlock {
  require: string
  min_confidence?: number
}

interface RuleRow {
  id: string
  name: string
  enabled: boolean
  severity: string
  owner_role: string
  window: string
  scope: string
  emit: string
  conditions: string[]
  condition_mode: string
  referenced_by: string[]
  /** Schema v3 —— 未声明的规则后端不带这两个键(拿不到不渲染)。 */
  seasonal?: SeasonalBlock
  significance?: SignificanceBlock
  /** null = 还没被任何任务判过(或本进程没接 store),不是"判定正常"。 */
  latest_verdict: VerdictBrief | null
  [k: string]: unknown
}

const ui = useUiStore()
const datasources = ref<DatasourceInfo[]>([])

/* ── URL state (P6 §4.3) ──────────────────────────────────────────────────
   ds is the one key this page acknowledges — the backend endpoint takes
   exactly `datasource=`, so the choice is a real filter and a shared link
   /admin/decisions?ds=demo lands on the same rule list (same shape as the
   KB page, which owns its ds selector the same way). */
const { values } = useListQuery({ ds: '' })

const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  { label: t('decisions', ui.lang) },
])

const rules = ref<RuleRow[]>([])
const issues = ref<string[]>([])
const digest = ref('')
/** 判定质量回评;null = 读不到(未接 store / 文件损坏)→ 整节不渲染。 */
const quality = ref<DecisionQuality | null>(null)
const loading = ref(false)
const saving = ref(false)
const editorOpen = ref(false)
const yamlText = ref('')
const saveError = ref('')
const historyOpen = ref(false)
const historyRule = ref<RuleRow | null>(null)

const connected = computed(() =>
  datasources.value.filter((d) => d.status === 'connected'),
)

function severityClass(sev: string): string {
  if (sev === 'critical') return 'pill-danger'
  if (sev === 'warning') return 'pill-warn'
  return 'pill-neutral'
}

/* ── 判定质量的展示口径 ────────────────────────────────── */

function rate(v: number | null): string {
  return v == null ? '—' : `${(v * 100).toFixed(0)}%`
}

function revShort(rev: string): string {
  return !rev || rev === 'rev_unknown' ? '—' : rev.slice(0, 8)
}

/** 桶 ↔ 当前文件的版本关系,三态 + 「规则已删」共四档,文案各不相同。 */
function revLabel(row: QualityBucket): string {
  if (!row.rule_declared) return t('decisionsQualityRuleGone', ui.lang)
  if (row.rule_rev_current === true) return t('decisionsQualityRevCurrent', ui.lang)
  if (row.rule_rev_current === false) return t('decisionsQualityRevOld', ui.lang)
  return t('decisionsQualityRevUnknown', ui.lang)
}

function revClass(row: QualityBucket): string {
  if (!row.rule_declared) return 'pill-neutral'
  if (row.rule_rev_current === true) return 'pill-ok'
  if (row.rule_rev_current === false) return 'pill-warn'
  return 'pill-neutral'
}

const INSUFF_KEY: Record<string, Parameters<typeof t>[0]> = {
  few_verdicts: 'decisionsQualityFewVerdicts',
  few_effects: 'decisionsQualityFewEffects',
  no_effects: 'decisionsQualityNoEffects',
}

function insuffLabel(code: string): string {
  const key = INSUFF_KEY[code]
  return key ? t(key, ui.lang) : code
}

async function loadDatasources() {
  try {
    const body = await apiGet('/v1/catalog/datasources')
    datasources.value = body.datasources ?? []
    if (!values.ds && connected.value.length) {
      const dflt = connected.value.find((d) => d.default)
      values.ds = dflt ? dflt.name : connected.value[0].name
    }
  } catch {
    datasources.value = []
  }
}

async function load() {
  if (!values.ds) return
  loading.value = true
  try {
    const body = await apiGet(
      `/v1/admin/decisions?datasource=${encodeURIComponent(values.ds)}`,
    )
    rules.value = (body.rules ?? []) as RuleRow[]
    issues.value = (body.issues ?? []) as string[]
    digest.value = (body.digest ?? '') as string
  } catch (e) {
    // 422 (a file that does not parse) and "no file yet" both land here —
    // showing an empty table plus the reason beats an empty table alone.
    rules.value = []
    issues.value = []
    digest.value = ''
    toastError(e)
  } finally {
    loading.value = false
  }
  await loadQuality()
}

/** 质量的第二条腿:它读不到不影响规则表 —— 静默收起整节(422 的 toast
 *  已由规则表那条给出,重复报两次只是噪声)。返回体没有 ``buckets``
 *  就不是一份质量报告(代理/旧后端),同样按读不到处理。 */
async function loadQuality() {
  try {
    const body = await fetchDecisionQuality(values.ds)
    quality.value = body && Array.isArray(body.buckets) ? body : null
  } catch {
    quality.value = null
  }
}

function openHistory(row: RuleRow) {
  historyRule.value = row
  historyOpen.value = true
}

async function openEditor() {
  saveError.value = ''
  editorOpen.value = true
  yamlText.value = ''
  try {
    // The file verbatim, not the display rows: the editor is a text editor,
    // so the API owns parsing. (The read is verbatim so authored comments are
    // visible; saving re-serializes the document, so they do not survive.)
    const body = await apiGet(
      `/v1/admin/decisions/raw?datasource=${encodeURIComponent(values.ds)}`,
    )
    yamlText.value = (body.text ?? '') as string
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : String(e)
  }
}

async function save() {
  saving.value = true
  saveError.value = ''
  try {
    await apiPut('/v1/admin/decisions', {
      datasource: values.ds,
      text: yamlText.value,
    })
    notifySuccess(t('decisionsSaved', ui.lang))
    editorOpen.value = false
    await load()
  } catch (e) {
    // The lint gate's message is the useful part — keep it visible in the
    // dialog so the editor can be fixed without reopening.
    saveError.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await loadDatasources()
  await load()
})
</script>

<style scoped>
.decisions-issues ul {
  margin: 0;
  padding: var(--sp-3) var(--sp-5) var(--sp-3) calc(var(--sp-5) + 18px);
}
.decisions-issues li {
  font-size: var(--fs-2xs);
  color: var(--el-color-warning);
}
.cond {
  font-size: var(--fs-2xs);
}
.band-line {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 2px;
}
.decisions-latest {
  display: flex;
  gap: 6px;
  align-items: center;
}
.decisions-quality-desc {
  padding: 0 var(--sp-5) var(--sp-2);
}
.decisions-quality-degraded {
  margin: 0 var(--sp-5) var(--sp-3);
  padding: 6px 10px;
  font-size: var(--fs-2xs);
  color: var(--el-color-warning);
  background: var(--el-color-warning-light-9);
  border-radius: 4px;
}
.decisions-quality-empty {
  padding: 0 var(--sp-5) var(--sp-4);
  font-size: var(--fs-2xs);
}
.decisions-quality-rev {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.decisions-quality-alert {
  color: var(--el-color-warning);
}
.decisions-quality-danger {
  color: var(--el-color-danger);
}
.decisions-quality-insufficient {
  font-size: var(--fs-2xs);
}
.decisions-yaml :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: var(--fs-2xs);
  line-height: 1.5;
}
.decisions-error {
  margin-top: 8px;
  padding: 8px 10px;
  font-size: var(--fs-2xs);
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  border-radius: 4px;
  white-space: pre-wrap;
}
</style>
