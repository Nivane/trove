<template>
  <div class="admin-view">
    <header class="view-header">
      <div>
        <h2>{{ t('decisions', ui.lang) }}</h2>
        <p class="view-desc">{{ t('decisionsPageDesc', ui.lang) }}</p>
      </div>
      <div class="view-actions">
        <el-select
          v-model="ds"
          class="ds-select"
          :placeholder="t('kbSelectDs', ui.lang)"
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
      </div>
    </header>

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
      <el-table v-loading="loading" :data="rules" class="admin-table">
        <template #empty>
          <TableEmpty>{{ t('decisionsEmpty', ui.lang) }}</TableEmpty>
        </template>
        <el-table-column :label="t('decisionsRuleId', ui.lang)" width="160">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.id }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobName', ui.lang)" min-width="150">
          <template #default="{ row }">{{ row.name || '—' }}</template>
        </el-table-column>
        <el-table-column :label="t('decisionsSeverity', ui.lang)" width="110">
          <template #default="{ row }">
            <span class="pill" :class="severityClass(row.severity)">
              {{ row.severity }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsWindow', ui.lang)" width="120">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.window || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsConditions', ui.lang)" min-width="220">
          <template #default="{ row }">
            <div v-for="(c, i) in row.conditions" :key="i" class="cell-mono cond">
              <span class="dim">{{ row.condition_mode }}</span> {{ c }}
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobEnabled', ui.lang)" width="90">
          <template #default="{ row }">
            <span class="pill" :class="row.enabled ? 'pill-ok' : 'pill-neutral'">
              {{ row.enabled ? t('enable', ui.lang) : t('disable', ui.lang) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('decisionsReferencedBy', ui.lang)" min-width="180">
          <template #default="{ row }">
            <div v-for="(j, i) in row.referenced_by" :key="i" class="dim">{{ j }}</div>
            <span v-if="!row.referenced_by.length" class="dim">—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

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
import { notifySuccess, toastError } from '../../utils/notify'
import TableEmpty from '../../components/admin/TableEmpty.vue'
import type { DatasourceInfo } from '../../api/types'

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
  [k: string]: unknown
}

const ui = useUiStore()
const datasources = ref<DatasourceInfo[]>([])
const ds = ref('')
const rules = ref<RuleRow[]>([])
const issues = ref<string[]>([])
const digest = ref('')
const loading = ref(false)
const saving = ref(false)
const editorOpen = ref(false)
const yamlText = ref('')
const saveError = ref('')

const connected = computed(() =>
  datasources.value.filter((d) => d.status === 'connected'),
)

function severityClass(sev: string): string {
  if (sev === 'critical') return 'pill-danger'
  if (sev === 'warning') return 'pill-warn'
  return 'pill-neutral'
}

async function loadDatasources() {
  try {
    const body = await apiGet('/v1/catalog/datasources')
    datasources.value = body.datasources ?? []
    if (!ds.value && connected.value.length) {
      const dflt = connected.value.find((d) => d.default)
      ds.value = dflt ? dflt.name : connected.value[0].name
    }
  } catch {
    datasources.value = []
  }
}

async function load() {
  if (!ds.value) return
  loading.value = true
  try {
    const body = await apiGet(
      `/v1/admin/decisions?datasource=${encodeURIComponent(ds.value)}`,
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
      `/v1/admin/decisions/raw?datasource=${encodeURIComponent(ds.value)}`,
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
      datasource: ds.value,
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
.view-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.decisions-issues ul {
  margin: 0;
  padding: var(--sp-3) var(--sp-5) var(--sp-3) calc(var(--sp-5) + 18px);
}
.decisions-issues li {
  font-size: 12px;
  color: var(--el-color-warning);
}
.cond {
  font-size: 12px;
}
.decisions-yaml :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
}
.decisions-error {
  margin-top: 8px;
  padding: 8px 10px;
  font-size: 12px;
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  border-radius: 4px;
  white-space: pre-wrap;
}
</style>
