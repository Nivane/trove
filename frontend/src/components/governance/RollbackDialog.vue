<!--
  RollbackDialog — 回滚确认(§2.5,§6-4)。

  三条口径:
    · 受影响文件清单**运行时取自后端**(`GET /v1/kb/assets` 的 assets[].file,
      与回滚端点恢复的 `ds_dir.glob("*.yml")` 同一个枚举)—— demo 6 个 /
      financial 5 个因部署而异,写死任何一个都是错的(§C.2 U4);
    · 清单取不到 = 列不出爆炸半径 → **回滚阻断**,不是「大概没事」;
    · 必须输入数据源名才能执行。
-->
<script lang="ts">
export interface RollbackTarget {
  ds: string
  sha: string
}
</script>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import ConfirmDialog from '../base/ConfirmDialog.vue'
import { fetchKbAssets } from '../../api/governance'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    ds: string
    sha: string
    /** 回滚请求进行中(父组件持有)。 */
    loading?: boolean
    /** 上一次失败的原因(空 = 无)。 */
    error?: string
  }>(),
  { loading: false, error: '' },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'confirm'): void
}>()

const ui = useUiStore()

const files = ref<string[]>([])
const filesLoading = ref(false)
const filesError = ref('')
const typed = ref('')

async function loadFiles() {
  if (!props.ds) return
  filesLoading.value = true
  filesError.value = ''
  try {
    const payload = await fetchKbAssets(props.ds)
    files.value = (payload.assets ?? []).map((a) => a.file)
  } catch (e) {
    files.value = []
    filesError.value = e instanceof Error ? e.message : String(e)
  } finally {
    filesLoading.value = false
  }
}

watch(
  () => [props.modelValue, props.ds] as const,
  ([open]) => {
    if (open) {
      typed.value = ''
      void loadFiles()
    }
  },
  { immediate: true },
)

const filesOk = computed(() => !filesLoading.value && !filesError.value && files.value.length > 0)
const nameOk = computed(() => typed.value.trim() === props.ds)
/** 阻断:清单不在场或名字没打对 —— 两种都不许点。 */
const blocked = computed(() => !filesOk.value || !nameOk.value)

function onConfirm() {
  if (blocked.value) return
  emit('confirm')
}
</script>

<template>
  <ConfirmDialog
    :model-value="modelValue"
    class="rollback-dialog"
    :data-blocked="blocked ? 'true' : 'false'"
    :title="`${t('govRollTitle', ui.lang)} · ${sha.slice(0, 7)}`"
    :confirm-text="t('govRollConfirm', ui.lang)"
    :cancel-text="t('cancel', ui.lang)"
    danger
    :loading="loading || filesLoading"
    @update:model-value="emit('update:modelValue', $event)"
    @confirm="onConfirm"
  >
    <div class="rd-body">
      <p class="rd-label">{{ t('govRollImpact', ui.lang) }}</p>

      <p v-if="filesLoading" class="rd-loading">{{ t('govRollFilesLoading', ui.lang) }}</p>
      <p v-else-if="filesError" class="rd-blocked">
        {{ t('govRollFilesError', ui.lang) }}
        <code class="rd-detail">{{ filesError }}</code>
      </p>
      <template v-else>
        <p class="rd-count">{{ t('govRollFilesLine', ui.lang, files.length) }}</p>
        <ul class="rd-files">
          <li v-for="f in files" :key="f" class="mono">{{ f }}</li>
        </ul>
        <p class="rd-note">{{ t('govRollFilesRuntime', ui.lang) }}</p>
      </template>

      <p class="rd-irreversible">{{ t('govRollIrreversible', ui.lang) }}</p>
      <p v-if="error" class="rd-blocked">{{ t('govActionFailed', ui.lang) }}: {{ error }}</p>
    </div>

    <template #impact>
      <input
        v-model="typed"
        class="rd-input"
        type="text"
        :placeholder="t('govRollTypeName', ui.lang, ds)"
        :aria-label="t('govRollTypeName', ui.lang, ds)"
      >
      <p v-if="typed && !nameOk" class="rd-blocked">{{ t('govRollNameMismatch', ui.lang) }}</p>
    </template>
  </ConfirmDialog>
</template>

<style scoped>
.rd-body {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.rd-label {
  margin: 0 0 4px;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.rd-count {
  margin: 0 0 4px;
  font-weight: 600;
  color: var(--text-primary);
}
.rd-files {
  margin: 0 0 4px;
  padding-left: 18px;
  max-height: 180px;
  overflow: auto;
}
.rd-note,
.rd-irreversible {
  margin: 2px 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.rd-loading {
  margin: 0;
  color: var(--text-tertiary);
}
.rd-blocked {
  margin: 4px 0 0;
  color: var(--danger, #d64545);
  font-weight: 600;
}
.rd-detail {
  display: block;
  font-size: var(--fs-2xs);
  font-weight: 400;
}
.rd-input {
  width: 100%;
  margin-top: 6px;
  padding: 4px 8px;
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-xs);
}
.mono {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-2xs);
}
</style>

<!-- ConfirmDialog 把内容 Teleport 到 body,scoped 样式够不着它的确认按钮;
     阻断态用一条全局规则把按钮变成「看得到、点不动」。 -->
<style>
.rollback-dialog[data-blocked='true'] .confirm-btn.is-solid {
  opacity: 0.5;
  pointer-events: none;
}
</style>
