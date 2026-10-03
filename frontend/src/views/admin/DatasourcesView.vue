<template>
  <div class="admin-view" :aria-busy="loading || undefined">
    <PageHeader
      :title="t('datasources', ui.lang)"
      :description="t('dsPageDesc', ui.lang)"
      :breadcrumbs="crumbs"
    />

    <div class="stat-grid">
      <div class="stat-card">
        <span class="stat-icon"><Database :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('datasources', ui.lang) }}</span>
          <span class="stat-value">{{ rows.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon"><PlugZap :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('dsConnected', ui.lang) }}</span>
          <span class="stat-value">{{ connectedCount }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon"><WifiOff :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('dsDisconnected', ui.lang) }}</span>
          <span class="stat-value">{{ disconnectedCount }}</span>
        </div>
      </div>
    </div>

    <div v-if="!loading && !rows.length" class="ds-empty">
      <span class="ds-empty-icon"><Database :size="22" /></span>
      <div class="ds-empty-title">{{ t('dsEmpty', ui.lang) }}</div>
      <div class="ds-empty-sub">{{ t('dsEmptySub', ui.lang) }}</div>
      <el-button type="primary" class="add" @click="openDialog">
        <Plus :size="15" class="btn-icon" />
        {{ t('dsCreateTitle', ui.lang) }}
      </el-button>
    </div>

    <div v-else class="admin-card">
      <div class="card-toolbar">
        <el-input
          v-model="values.q"
          class="toolbar-search"
          :prefix-icon="Search"
          :placeholder="t('dsSearch', ui.lang)"
          :aria-label="t('dsSearch', ui.lang)"
          clearable
        />
        <el-select v-model="values.health" class="filter-select">
          <el-option :label="t('dsFilterHealth', ui.lang)" value="" />
          <el-option :label="t('dsConnected', ui.lang)" value="connected" />
          <el-option :label="t('dsDisconnected', ui.lang)" value="disconnected" />
        </el-select>
        <el-select v-model="values.drift" class="filter-select">
          <el-option :label="t('dsFilterDrift', ui.lang)" value="" />
          <el-option :label="t('dsDriftOpen', ui.lang)" value="open" />
          <el-option :label="t('dsDriftNone', ui.lang)" value="none" />
        </el-select>
        <span class="spacer" />
        <span class="view-count">
          {{ filtered.length }} · {{ connectedCount }} {{ t('dsConnected', ui.lang) }}
        </span>
        <el-button type="primary" class="add" @click="openDialog">
          <Plus :size="15" class="btn-icon" />
          {{ t('dsCreateTitle', ui.lang) }}
        </el-button>
      </div>

      <el-table
        v-loading="loading"
        :data="filtered"
        class="admin-table"
        max-height="var(--table-max-h)"
      >
        <template #empty>
          <TableEmpty />
        </template>
        <el-table-column :label="t('datasources', ui.lang)" min-width="240">
          <template #default="{ row }">
            <div class="ds-name-cell">
              <span class="ds-icon"><Database :size="16" /></span>
              <div class="ds-name-meta">
                <div class="ds-name-row">
                  <span class="ds-name">{{ row.name }}</span>
                  <span v-if="row.default" class="pill pill-accent">default</span>
                </div>
                <span class="ds-type">{{ dsTypeLabel(row.type) }}</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('dsStatus', ui.lang)" width="130">
          <template #default="{ row }">
            <span
              class="pill"
              :class="row.status === 'connected' ? 'pill-ok' : 'pill-neutral'"
            >
              <span
                class="pill-dot"
                :class="row.status === 'connected' ? 'pill-dot-ok' : ''"
              />
              {{
                row.status === 'connected'
                  ? t('dsConnected', ui.lang)
                  : t('dsDisconnected', ui.lang)
              }}
            </span>
            <span
              v-if="driftOpen(row) > 0"
              class="pill pill-warn ds-drift-pill"
              :title="t('dsDriftOpen', ui.lang)"
            >
              {{ t('dsDriftOpen', ui.lang) }} {{ driftOpen(row) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column
          :label="t('actions', ui.lang)"
          width="120"
          fixed="right"
        >
          <template #default="{ row }">
            <div class="row-actions">
              <el-tooltip
                :disabled="!editLocked(row)"
                :content="editLockedReason(row)"
              >
                <button
                  class="mini-btn icon edit"
                  :title="t('edit', ui.lang)"
                  :disabled="editLocked(row) || busy(row.name, 'edit')"
                  @click="openEdit(row)"
                >
                  <Pencil :size="13" />
                </button>
              </el-tooltip>
              <button
                class="mini-btn icon test"
                :title="t('dsTest', ui.lang)"
                :disabled="busy(row.name, 'test')"
                @click="testConnectionRow(row)"
              >
                <PlugZap :size="13" />
              </button>
              <button
                class="mini-btn icon"
                :title="t('dsReindex', ui.lang)"
                :disabled="busy(row.name, 'reindex')"
                @click="reindex(row)"
              >
                <RefreshCw :size="13" />
              </button>
              <button
                class="mini-btn icon is-danger"
                :title="t('dsRemove', ui.lang)"
                :disabled="busy(row.name, 'test')"
                @click="remove(row)"
              >
                <Trash2 :size="13" />
              </button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dlgOpen"
      :title="t('dsCreateTitle', ui.lang)"
      width="480"
      class="admin-dialog ds-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="add">
        <el-form-item :label="t('dsTypeLabel', ui.lang)">
          <el-select v-model="form.type" class="ds-type-select">
            <el-option
              v-for="opt in typeOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <div class="form-hint">{{ t('dsTypeHint', ui.lang) }}</div>
        </el-form-item>

        <el-form-item v-if="form.type !== 'demo'" :label="t('dsUrl', ui.lang)">
          <el-input
            v-model="form.url"
            class="ds-url-input"
            :placeholder="exampleFor(form.type)"
            spellcheck="false"
            autocomplete="off"
            @input="detectType"
          />
          <div class="form-hint">
            {{ t('dsExample', ui.lang) }}
            <button class="link-btn" type="button" @click="insertExample()">
              {{ exampleFor(form.type) }}
            </button>
          </div>
        </el-form-item>

        <el-form-item :label="t('dsName', ui.lang)">
          <el-input
            v-model="form.name"
            class="ds-name-field"
            :placeholder="t('dsNamePlaceholder', ui.lang)"
            autocomplete="off"
          />
          <div class="form-hint">{{ t('dsNameHint', ui.lang) }}</div>
        </el-form-item>

        <div v-if="formError" class="form-error">
          <AlertCircle :size="15" />
          <span>{{ formError }}</span>
        </div>
      </el-form>

      <template #footer>
        <span class="dialog-hint">
          <Info :size="13" />
          {{ t('dsProbeHint', ui.lang) }}
        </span>
        <span class="dialog-footer-spacer" />
        <el-button @click="dlgOpen = false">
{{
          t('cancel', ui.lang)
        }}
</el-button>
        <el-button
          type="primary"
          :loading="submitting"
          :disabled="!canSubmit"
          @click="add"
        >
          {{ t('dsRegister', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="editOpen"
      :title="`${t('dsEditTitle', ui.lang)} · ${editTarget?.name || ''}`"
      width="480"
      class="admin-dialog ds-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="saveEdit">
        <el-form-item :label="t('dsName', ui.lang)">
          <el-input v-model="editForm.name" class="ds-name-field" disabled />
        </el-form-item>

        <el-form-item :label="t('dsTypeLabel', ui.lang)">
          <el-input v-model="editForm.type" disabled class="ds-type-locked" />
        </el-form-item>

        <el-form-item :label="t('dsUrl', ui.lang)">
          <el-input
            v-model="editForm.url"
            class="ds-url-input mono-input"
            :placeholder="t('dsUrlPlaceholder', ui.lang)"
            spellcheck="false"
            autocomplete="off"
          />
          <div class="form-hint">
            <Info :size="13" />
            {{ t('dsEditUrlHint', ui.lang) }}
          </div>
        </el-form-item>

        <div v-if="editError" class="form-error">
          <AlertCircle :size="15" />
          <span>{{ editError }}</span>
        </div>
      </el-form>

      <template #footer>
        <el-button :loading="testing" @click="testEdit">
          <PlugZap :size="14" class="btn-icon" />
          {{ t('dsTest', ui.lang) }}
        </el-button>
        <span class="dialog-footer-spacer" />
        <el-button @click="editOpen = false">
{{
          t('cancel', ui.lang)
        }}
</el-button>
        <el-button
          type="primary"
          :loading="savingEdit"
          :disabled="!editForm.url.trim()"
          @click="saveEdit"
        >
          {{ t('saveLabel', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  Plus,
  Database,
  AlertCircle,
  Info,
  PlugZap,
  Pencil,
  Search,
  WifiOff,
  Trash2,
  RefreshCw,
} from 'lucide-vue-next'
import { apiDelete, apiGet, apiPost, apiPut } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError, notifySuccess } from '../../utils/notify'
import { dsTypeLabel } from '../../utils/format'
import { useListQuery } from '../../composables/useListQuery'
import type { DatasourceInfo } from '../../api/types'
import TableEmpty from '../../components/admin/TableEmpty.vue'
import PageHeader from '../../components/base/PageHeader.vue'

const ui = useUiStore()
const rows = ref<DatasourceInfo[]>([])
const loading = ref(false)
const dlgOpen = ref(false)
const submitting = ref(false)
const formError = ref('')
const busyMap = reactive<Record<string, boolean>>({})

/* ── URL state (P6 §4.3) ──────────────────────────────────────────────────
   q / health / drift are the keys this page acknowledges: the list stays
   shareable and /admin/datasources?drift=open lands on the drifted sources
   instead of a full list. All three filter client-side over the fetched
   catalog (the endpoint takes no filter parameters). */
const { values } = useListQuery({ q: '', health: '', drift: '' })

// 每源的未豁免漂移条目数(治理中心同一手法:一次只读查询);失败/未体检 =
// 0 条,过滤器就说"无漂移",不替后台编造体检结论。
const driftCounts = ref<Record<string, number>>({})

function driftOpen(row: DatasourceInfo): number {
  return driftCounts.value[row.name] ?? 0
}

const filtered = computed(() =>
  rows.value.filter((row) => {
    const needle = values.q.trim().toLowerCase()
    if (needle && !`${row.name}\n${row.type}`.toLowerCase().includes(needle)) return false
    if (values.health === 'connected' && row.status !== 'connected') return false
    if (values.health === 'disconnected' && row.status === 'connected') return false
    if (values.drift === 'open') return driftOpen(row) > 0
    if (values.drift === 'none') return driftOpen(row) === 0
    return true
  }),
)

const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  { label: t('datasources', ui.lang) },
])

const connectedCount = computed(
  () => rows.value.filter((r) => r.status === 'connected').length,
)
const disconnectedCount = computed(() => rows.value.length - connectedCount.value)

function busy(row: DatasourceInfo, action: string): boolean {
  return !!busyMap[`${row.name}:${action}`]
}
function setBusy(row: DatasourceInfo, action: string, v: boolean) {
  if (v) busyMap[`${row.name}:${action}`] = true
  else delete busyMap[`${row.name}:${action}`]
}

// ── 编辑锁定：已初始化知识库（或内置 demo）后不允许改动连接 ──
function editLocked(row: DatasourceInfo): boolean {
  return row.type === 'demo' || !!row.kb_initialized
}
function editLockedReason(row: DatasourceInfo): string {
  if (row.type === 'demo') return t('dsDemoLocked', ui.lang)
  if (row.kb_initialized) return t('dsEditLocked', ui.lang)
  return ''
}

const form = reactive({
  type: 'demo',
  name: '',
  url: '',
})

interface TypeMeta {
  value: string
  label: string
  example: string
}

// Connection-string examples per scheme — demo needs no URL.
const TYPES: Record<string, TypeMeta> = {
  demo: { value: 'demo', label: 'Demo · 内置示例', example: '' },
  mysql: {
    value: 'mysql',
    label: 'MySQL',
    example: 'mysql://user:pass@localhost:3306/financial',
  },
  doris: {
    value: 'doris',
    label: 'Doris',
    example: 'doris://user:pass@localhost:9030/financial',
  },
  clickhouse: {
    value: 'clickhouse',
    label: 'ClickHouse',
    example: 'clickhouse://user@localhost:8123/default',
  },
  sqlite: {
    value: 'sqlite',
    label: 'SQLite',
    example: 'sqlite:///path/to/data.db',
  },
  duckdb: {
    value: 'duckdb',
    label: 'DuckDB',
    example: 'duckdb:///path/to/data.duckdb',
  },
}

const typeOptions = Object.values(TYPES) as TypeMeta[]

function exampleFor(type: string): string {
  return TYPES[type]?.example ?? ''
}

const canSubmit = computed(() => {
  if (form.type === 'demo') return true
  return !!form.url.trim()
})

async function load() {
  loading.value = true
  try {
    rows.value = (await apiGet('/v1/admin/datasources')).datasources ?? []
    await loadDrift()
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

/** 漂移数:每源一次只读查询,失败不阻塞列表(过滤按 0 条算)。 */
async function loadDrift() {
  const counts: Record<string, number> = {}
  await Promise.all(
    rows.value.map(async (row) => {
      try {
        const body = await apiGet(
          `/v1/admin/drift?ds=${encodeURIComponent(row.name)}`,
        )
        counts[row.name] = (body.items ?? []).length
      } catch {
        counts[row.name] = 0
      }
    }),
  )
  driftCounts.value = counts
}

function openDialog() {
  formError.value = ''
  Object.assign(form, { type: 'demo', name: '', url: '' })
  dlgOpen.value = true
}

function insertExample() {
  form.url = exampleFor(form.type)
}

/** When a ``scheme://`` URL is pasted in, switch the type selector to match. */
function detectType() {
  const scheme = form.url.trim().split('://')[0].toLowerCase()
  if (scheme && scheme !== 'demo' && TYPES[scheme]) {
    form.type = scheme
  }
}

async function add() {
  if (!canSubmit.value || submitting.value) return
  formError.value = ''
  // demo carries no connection string — the bare marker tells the backend.
  const url = form.type === 'demo' ? 'demo' : form.url.trim()
  submitting.value = true
  try {
    await apiPost('/v1/admin/datasources', { name: form.name.trim(), url })
    dlgOpen.value = false
    notifySuccess(t('dsAddedOk', ui.lang))
    await load()
  } catch (e) {
    formError.value =
      e && typeof e === 'object' && 'message' in e
        ? String((e as { message: unknown }).message)
        : t('dsAddFail', ui.lang)
  } finally {
    submitting.value = false
  }
}

// ── 测试连接 / 编辑 ──
const testing = ref(false)
const editOpen = ref(false)
const savingEdit = ref(false)
const editError = ref('')
const editTarget = ref<DatasourceInfo | null>(null)
const editForm = reactive({ name: '', type: '', url: '' })

async function testUrl(url: string, row?: DatasourceInfo) {
  try {
    const body = await apiPost<{ ok: boolean; error?: string | null }>(
      '/v1/admin/datasources/test-connection',
      row ? { name: row.name } : { url },
    )
    if (body.ok) {
      notifySuccess(t('dsTestOk', ui.lang))
    } else {
      toastError(new Error(body.error || t('dsTestFail', ui.lang)))
    }
    return body.ok
  } catch (e) {
    toastError(e)
    return false
  }
}

async function testConnectionRow(row: DatasourceInfo) {
  setBusy(row, 'test', true)
  await testUrl('', row)
  setBusy(row, 'test', false)
}

async function reindex(row: DatasourceInfo) {
  setBusy(row, 'reindex', true)
  try {
    await apiPost('/v1/admin/index', { datasource: row.name })
    notifySuccess(t('dsReindexOk', ui.lang))
  } catch (e) {
    toastError(e)
  } finally {
    setBusy(row, 'reindex', false)
  }
}

async function testEdit() {
  testing.value = true
  editError.value = ''
  await testUrl(editForm.url.trim())
  testing.value = false
}

async function openEdit(row: DatasourceInfo) {
  if (editLocked(row)) return
  editTarget.value = row
  editError.value = ''
  setBusy(row, 'edit', true)
  try {
    const body = await apiGet<{ datasource: DatasourceInfo & { url: string } }>(
      `/v1/admin/datasources/${encodeURIComponent(row.name)}`,
    )
    const ds = body.datasource
    Object.assign(editForm, {
      name: ds.name,
      type: ds.type,
      url: ds.url || '',
    })
    editOpen.value = true
  } catch (e) {
    toastError(e)
  } finally {
    setBusy(row, 'edit', false)
  }
}

async function saveEdit() {
  if (!editTarget.value || !editForm.url.trim() || savingEdit.value) return
  savingEdit.value = true
  editError.value = ''
  try {
    await apiPut(
      `/v1/admin/datasources/${encodeURIComponent(editTarget.value.name)}`,
      { url: editForm.url.trim() },
    )
    editOpen.value = false
    notifySuccess(t('dsUpdatedOk', ui.lang))
    await load()
  } catch (e) {
    editError.value =
      e && typeof e === 'object' && 'message' in e
        ? String((e as { message: unknown }).message)
        : t('dsEditFail', ui.lang)
  } finally {
    savingEdit.value = false
  }
}

async function remove(row: DatasourceInfo) {
  try {
    await ElMessageBox.confirm(t('dsRemoveConfirm', ui.lang), t('dsRemove', ui.lang), {
      type: 'warning',
      confirmButtonText: t('dsRemove', ui.lang),
      cancelButtonText: t('cancel', ui.lang),
    })
  } catch {
    return
  }
  setBusy(row, 'remove', true)
  try {
    await apiDelete(`/v1/admin/datasources/${row.name}`)
    notifySuccess(t('dsRemovedOk', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    setBusy(row, 'remove', false)
  }
}

onMounted(load)
</script>
