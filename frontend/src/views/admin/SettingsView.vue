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

      <!-- ── 查询结果限制 ── -->
      <div class="admin-card settings-card">
        <div class="card-header">
          <div class="card-title">{{ t('settingsResultsHeading', ui.lang) }}</div>
        </div>
        <div class="settings-card__body">
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
      </div>

      <!-- ── 语义层 ── -->
      <div class="admin-card settings-card">
        <div class="card-header">
          <div class="card-title">{{ t('semanticLayerGroup', ui.lang) }}</div>
        </div>
        <div class="settings-card__body">
          <el-form label-position="top" class="settings-form">
            <el-form-item :label="t('semanticLayerPath', ui.lang)">
              <el-input
                v-model="form.app.semantic_layer_path"
                class="mono-input"
                spellcheck="false"
                autocomplete="off"
                :placeholder="t('semanticLayerPlaceholder', ui.lang)"
              />
              <div class="form-hint">{{ t('semanticLayerHint', ui.lang) }}</div>
            </el-form-item>
          </el-form>
        </div>
      </div>

      <!-- ── 交互 ── -->
      <div class="admin-card settings-card">
        <div class="card-header">
          <div class="card-title">{{ t('interactionGroup', ui.lang) }}</div>
        </div>
        <div class="settings-card__body">
          <el-form label-position="top" class="settings-form">
            <el-form-item :label="t('language', ui.lang)">
              <el-select v-model="form.app.language" class="settings-select">
                <el-option label="中文" value="zh" />
                <el-option label="English" value="en" />
              </el-select>
            </el-form-item>
          </el-form>
        </div>
      </div>

      <!-- ── 流程开关 ── -->
      <div class="admin-card settings-card">
        <div class="card-header">
          <div class="card-title">{{ t('featureGroup', ui.lang) }}</div>
        </div>
        <div class="settings-card__body">
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
      </div>

      <!-- ── 会话保留 ── -->
      <div class="admin-card settings-card">
        <div class="card-header">
          <div class="card-title">{{ t('retentionGroup', ui.lang) }}</div>
        </div>
        <div class="settings-card__body">
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
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RefreshCw, Check, Info } from 'lucide-vue-next'
import { apiGet, apiPut } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError, notifySuccess } from '../../utils/notify'

const ui = useUiStore()

const loading = ref(false)
const saving = ref(false)
const saveError = ref('')
const snapshotKey = ref('')

interface FormShape {
  app: Record<string, unknown>
  retention: Record<string, number>
}

const form = reactive<FormShape>({
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

function buildPayload(): Record<string, unknown> {
  const values: Record<string, unknown> = {}
  const current = snapshotOf(form)
  const prev = JSON.parse(snapshotKey.value || '{}') as Record<string, unknown>
  for (const key of Object.keys(current)) {
    if (JSON.stringify(current[key]) !== JSON.stringify(prev[key])) {
      values[key] = current[key]
    }
  }
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
.settings-form {
  max-width: 560px;
}
.mono-input :deep(.el-input__inner) {
  font-family: var(--font-mono);
}
.settings-select {
  width: 200px;
}
.switch-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 0 var(--sp-5);
}
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-3) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.switch-row:last-child {
  border-bottom: none;
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
  margin-top: var(--sp-1);
}
</style>