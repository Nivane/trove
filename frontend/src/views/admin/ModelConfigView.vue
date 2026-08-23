<template>
  <div class="admin-view">
    <div class="view-header">
      <div>
        <h2>{{ t('modelConfig', ui.lang) }}</h2>
        <p class="view-desc">{{ t('modelConfigPageDesc', ui.lang) }}</p>
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

      <div class="admin-card settings-card">
        <div class="card-header">
          <div class="card-title">{{ t('modelConfig', ui.lang) }}</div>
        </div>
        <div class="settings-card__body">
          <div class="model-note">
            <Info :size="13" />
            {{ t('modelFallbackNote', ui.lang) }}
          </div>
          <el-form label-position="top" class="settings-form">
            <el-form-item :label="t('defaultModel', ui.lang)">
              <el-input
                v-model="form.default_model"
                class="mono-input"
                spellcheck="false"
                autocomplete="off"
              />
            </el-form-item>
            <el-form-item :label="t('fastModel', ui.lang)">
              <el-input
                v-model="form.fast_model"
                class="mono-input"
                spellcheck="false"
                autocomplete="off"
              />
              <div class="form-hint">{{ t('fastModelHint', ui.lang) }}</div>
            </el-form-item>
          </el-form>

          <div class="providers-block">
            <div class="providers-head">
              <span class="providers-title">{{ t('providers', ui.lang) }}</span>
              <button class="mini-btn" @click="addProvider">
                <Plus :size="13" class="btn-icon" />
                {{ t('addProvider', ui.lang) }}
              </button>
            </div>
            <div v-for="(p, i) in form.providers" :key="i" class="provider-row">
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
  default_model: string
  fast_model: string
  providers: ProviderRow[]
}

const form = reactive<FormShape>({
  default_model: '',
  fast_model: '',
  providers: [],
})

const dirty = computed(() => snapshotKey.value !== JSON.stringify(snapshotOf(form)))

function snapshotOf(f: FormShape): Record<string, unknown> {
  return {
    'llm.default_model': f.default_model,
    'llm.fast_model': f.fast_model,
    'llm.providers': f.providers,
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
    form.default_model = String(v['llm.default_model'] ?? '')
    form.fast_model = String(v['llm.fast_model'] ?? '')
    form.providers = ((v['llm.providers'] as ProviderRow[] | undefined) ?? []).map(
      (p) => {
        const { api_key, ...rest } = p.litellm_params ?? {}
        return {
          name: p.name ?? '',
          has_api_key: !!p.has_api_key,
          litellm_params: { api_key: '', ...rest },
        }
      },
    )
    snapshotKey.value = JSON.stringify(snapshotOf(form))
    saveError.value = ''
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

function addProvider() {
  form.providers.push({
    name: '',
    has_api_key: false,
    litellm_params: { api_key: '', api_base: '' },
  })
}

function removeProvider(i: number) {
  form.providers.splice(i, 1)
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
  const masked = form.providers.map((p) => {
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
    notifySuccess(t('settingsSavedOk', ui.lang))
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
.model-note {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  background: var(--surface-canvas);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  padding: var(--sp-3) var(--sp-4);
  line-height: 1.5;
}
.providers-block {
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-4);
  margin-top: var(--sp-1);
}
.providers-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-3);
}
.providers-title {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.provider-row {
  display: grid;
  grid-template-columns: 180px 1fr 220px 28px;
  gap: var(--sp-2);
  align-items: center;
  margin-bottom: var(--sp-2);
  font-family: var(--font-mono);
}
</style>
