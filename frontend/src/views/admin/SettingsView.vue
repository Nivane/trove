<template>
  <div class="admin-view">
    <div class="view-header">
      <div>
        <h2>{{ t('settingsNav', ui.lang) }}</h2>
        <p class="view-desc">{{ t('settingsPageDesc', ui.lang) }}</p>
      </div>
      <div class="view-header-right">
        <el-button class="refresh-btn" :loading="loading" @click="load">
          <RefreshCw :size="15" class="btn-icon" />
          {{ t('refresh', ui.lang) }}
        </el-button>
        <el-button type="primary" :loading="saving" :disabled="!dirty" @click="save">
          <Check :size="15" class="btn-icon" />
          {{ t('saveLabel', ui.lang) }}
        </el-button>
      </div>
    </div>

    <div class="settings-stack">
      <div v-if="saveError" class="form-error settings-error"><span>{{ saveError }}</span></div>

      <!-- ── LLM 模型 ── -->
      <div class="admin-card settings-card">
        <div class="setting-group-title">{{ t('modelGroup', ui.lang) }}</div>
        <el-form label-position="top" class="settings-form">
          <el-form-item :label="t('defaultModel', ui.lang)">
            <el-input
              v-model="form.llm.default_model"
              class="mono-input"
              spellcheck="false"
              autocomplete="off"
            />
          </el-form-item>
          <el-form-item :label="t('fastModel', ui.lang)">
            <el-input
              v-model="form.llm.fast_model"
              class="mono-input"
              spellcheck="false"
              autocomplete="off"
            />
            <div class="form-hint">{{ t('fastModelHint', ui.lang) }}</div>
          </el-form-item>
        </el-form>

        <div class="providers-head">
          <span class="providers-title">{{ t('providers', ui.lang) }}</span>
          <button class="mini-btn" @click="addProvider">
            <Plus :size="13" class="btn-icon" />
            {{ t('addProvider', ui.lang) }}
          </button>
        </div>
        <div v-for="(p, i) in form.llm.providers" :key="i" class="provider-row">
          <el-input
            v-model="p.name"
            class="provider-name"
            :placeholder="t('providerName', ui.lang)"
            spellcheck="false"
          />
          <el-input
            v-model="p.litellm_params.api_base"
            class="provider-base"
            :placeholder="t('apiBase', ui.lang)"
            spellcheck="false"
          />
          <el-input
            v-model="p.litellm_params.api_key"
            class="provider-key"
            type="password"
            show-password
            :placeholder="p.has_api_key ? t('apiKeyKept', ui.lang) : t('apiKey', ui.lang)"
          />
          <button class="mini-btn is-danger" @click="removeProvider(i)">
            <Trash2 :size="13" />
          </button>
        </div>
      </div>

      <!-- ── 查询结果限制 ── -->
      <div class="admin-card settings-card">
        <div class="setting-group-title">{{ t('settingsResultsHeading', ui.lang) }}</div>
        <el-form label-position="top" class="settings-form">
          <el-form-item :label="t('settingsDisplayRows', ui.lang)">
            <el-input-number
              v-model.number="form.app.result_display_rows"
              :min="1"
              :max="500"
              :step="10"
              controls-position="right"
            />
            <div class="form-hint">{{ t('settingsDisplayRowsHint', ui.lang) }}</div>
          </el-form-item>
          <el-form-item :label="t('settingsMaxRows', ui.lang)">
            <el-input-number
              v-model.number="form.app.result_max_rows"
              :min="1"
              :max="50000"
              :step="100"
              controls-position="right"
            />
            <div class="form-hint">{{ t('settingsMaxRowsHint', ui.lang) }}</div>
          </el-form-item>
        </el-form>
      </div>

      <!-- ── 交互 ── -->
      <div class="admin-card settings-card">
        <div class="setting-group-title">{{ t('interactionGroup', ui.lang) }}</div>
        <el-form label-position="top" class="settings-form">
          <el-form-item :label="t('language', ui.lang)">
            <el-select v-model="form.app.language" class="settings-select">
              <el-option label="中文" value="zh" />
              <el-option label="English" value="en" />
            </el-select>
          </el-form-item>
        </el-form>
      </div>

      <!-- ── 流程开关 ── -->
      <div class="admin-card settings-card">
        <div class="setting-group-title">{{ t('featureGroup', ui.lang) }}</div>
        <div class="switch-grid">
          <label v-for="defn in switchDefs" :key="defn.key" class="switch-row">
            <span class="switch-label">
              {{ t(defn.label, ui.lang) }}
              <span class="switch-hint">{{ t(defn.hint, ui.lang) }}</span>
            </span>
            <el-switch v-model="form.app[defn.key]" />
          </label>
          <label class="switch-row">
            <span class="switch-label">
              {{ t('reflectSkip', ui.lang) }}
              <span class="switch-hint">{{ t('reflectSkipHint', ui.lang) }}</span>
            </span>
            <el-select v-model="form.app.reflect_skip" class="settings-select reflect-select">
              <el-option v-for="o in reflectSkips" :key="o" :label="o" :value="o" />
            </el-select>
          </label>
        </div>
      </div>

      <!-- ── 会话保留 ── -->
      <div class="admin-card settings-card">
        <div class="setting-group-title">{{ t('retentionGroup', ui.lang) }}</div>
        <div class="retention-grid">
          <el-form-item
            v-for="defn in retentionDefs"
            :key="defn.key"
            :label="t(defn.label, ui.lang)"
          >
            <el-input-number
              v-model.number="form.retention[defn.key]"
              :min="0"
              controls-position="right"
              class="retention-input"
            />
          </el-form-item>
        </div>
        <div class="form-hint settings-db-hint">
          <Info :size="13" />
          {{ t('settingsDbHint', ui.lang) }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RefreshCw, Check, Plus, Trash2, Info } from 'lucide-vue-next'
import { apiGet, apiPut } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError, notifySuccess } from '../../utils/notify'

const ui = useUiStore()

const loading = ref(false)
const saving = ref(false)
const saveError = ref('')
const mask = ref('__trove_masked_key__')
const snapshotKey = ref('')

type ProviderRow = {
  name: string
  has_api_key: boolean
  litellm_params: { api_key: string; api_base: string; [k: string]: unknown }
}

interface FormShape {
  llm: { default_model: string; fast_model: string; providers: ProviderRow[] }
  app: Record<string, unknown>
  retention: Record<string, number>
}

const form = reactive<FormShape>({
  llm: { default_model: '', fast_model: '', providers: [] },
  app: {},
  retention: {},
})

const switchDefs = [
  { key: 'date_parser', label: 'dateParser', hint: 'dateParserHint' },
  { key: 'fast_path', label: 'fastPath', hint: 'fastPathHint' },
  { key: 'explain_semantics', label: 'explainSemantics', hint: 'explainSemanticsHint' },
  { key: 'hitl', label: 'hitlSetting', hint: 'hitlHint' },
  { key: 'insights', label: 'insights', hint: 'insightsHint' },
  { key: 'result_cache', label: 'resultCache', hint: 'resultCacheHint' },
  { key: 'decompose_llm_judge', label: 'decomposeLlm', hint: 'decomposeLlmHint' },
] as const

const reflectSkips = ['simple', 'standard', 'all', 'off'] as const

const retentionDefs = [
  { key: 'max_sessions_per_user', label: 'maxSessions' },
  { key: 'active_grace_min', label: 'activeGrace' },
  { key: 'max_checkpoints_per_thread', label: 'maxCheckpoints' },
  { key: 'sweep_interval_hours', label: 'sweepInterval' },
] as const

const dirty = computed(() => snapshotKey.value !== JSON.stringify(snapshotOf(form)))

function snapshotOf(f: FormShape): Record<string, unknown> {
  const app = Object.fromEntries(
    Object.entries(f.app).map(([k, v]) => [`app.${k}`, v]),
  )
  const retention = Object.fromEntries(
    Object.entries(f.retention).map(([k, v]) => [`retention.${k}`, v]),
  )
  return {
    ...app,
    ...retention,
    'llm.default_model': f.llm.default_model,
    'llm.fast_model': f.llm.fast_model,
    'llm.providers': f.llm.providers,
  }
}

async function load() {
  loading.value = true
  try {
    const body = await apiGet<{
      values: Record<string, unknown>
      mask: string
    }>('/v1/admin/settings')

    const v = body.values ?? {}
    mask.value = body.mask
    form.llm.default_model = String(v['llm.default_model'] ?? '')
    form.llm.fast_model = String(v['llm.fast_model'] ?? '')
    form.llm.providers = ((v['llm.providers'] as ProviderRow[] | undefined) ?? []).map(
      (p) => {
        const { api_key, ...rest } = p.litellm_params ?? {}
        return {
          name: p.name ?? '',
          has_api_key: !!p.has_api_key,
          litellm_params: { api_key: '', ...rest },
        }
      },
    )
    for (const key of Object.keys(form.app)) delete form.app[key]
    for (const key of Object.keys(form.retention)) delete form.retention[key]
    for (const [k, val] of Object.entries(v)) {
      if (k.startsWith('app.')) form.app[k.slice(4)] = val
      else if (k.startsWith('retention.')) form.retention[k.slice(11)] = Number(val)
    }
    if (form.app.reflect_skip == null) form.app.reflect_skip = 'simple'
    snapshotKey.value = JSON.stringify(snapshotOf(form))
    saveError.value = ''
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

function addProvider() {
  form.llm.providers.push({
    name: '',
    has_api_key: false,
    litellm_params: { api_key: '', api_base: '' },
  })
}

function removeProvider(i: number) {
  form.llm.providers.splice(i, 1)
}

function buildPayload(): Record<string, unknown> {
  const values: Record<string, unknown> = {}
  const current = snapshotOf(form)
  const prev = JSON.parse(snapshotKey.value || '{}') as Record<string, unknown>
  for (const key of Object.keys(current)) {
    if (key === 'llm.providers') continue
    if (JSON.stringify(current[key]) !== JSON.stringify(prev[key])) {
      values[key] = current[key]
    }
  }
  // providers: sent every time so renames/additions/removals persist; a kept
  // api_key round-trips as the mask sentinel so the secret never leaks
  const masked = form.llm.providers.map((p) => {
    if (p.litellm_params.api_key) {
      return { name: p.name, litellm_params: { ...p.litellm_params } }
    }
    const { api_key, ...rest } = p.litellm_params
    const params: Record<string, unknown> = { ...rest }
    if (p.has_api_key) params.api_key = mask.value
    return { name: p.name, litellm_params: params }
  })
  values['llm.providers'] = masked
  return values
}

async function save() {
  const values = buildPayload()
  if (!Object.keys(values).length) return
  saving.value = true
  saveError.value = ''
  try {
    await apiPut('/v1/admin/settings', { values })
    notifySaveOk()
    await load()
  } catch (e) {
    saveError.value =
      e && typeof e === 'object' && 'message' in e
        ? String((e as { message: unknown }).message)
        : t('settingsSaveFail', ui.lang)
  } finally {
    saving.value = false
  }
}

function notifySaveOk() {
  notifySuccess(t('settingsSavedOk', ui.lang))
}

onMounted(load)
</script>

<style scoped>
.settings-stack {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.settings-error {
  margin-bottom: 0;
}
.settings-card {
  padding: var(--sp-4) var(--sp-5);
}
.setting-group-title {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: var(--sp-3);
}
.settings-form {
  max-width: 520px;
}
.mono-input :deep(.el-input__inner) {
  font-family: var(--font-mono);
}
.settings-select {
  width: 180px;
}
.providers-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-2);
}
.providers-title {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
}
.provider-row {
  display: grid;
  grid-template-columns: 180px 1fr 220px 28px;
  gap: var(--sp-2);
  align-items: center;
  margin-bottom: var(--sp-2);
  font-family: var(--font-mono);
}
.switch-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: var(--sp-2) var(--sp-5);
}
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-2) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.switch-label {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--fs-sm);
}
.switch-hint {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.reflect-select {
  width: 140px;
}
.retention-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-6);
  max-width: 720px;
}
.retention-input {
  width: 180px;
}
.settings-db-hint {
  margin-top: var(--sp-3);
}
</style>