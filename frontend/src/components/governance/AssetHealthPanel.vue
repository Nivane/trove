<!--
  AssetHealthPanel — Tab2 的 KB 资产体检抽屉(§2.4)。

  它是「KB 看起来正常」与「磁盘文件真的被采纳」之间唯一的区分点:
  `GET /v1/kb/assets` 的 refused 记账非空 = 该文件**没有被镜像采用**。
  拒绝原因原文展示,不改写、不截断。

  自取数(单源),打开时才请求;失败落 StatePanel error + 重试。
-->
<script setup lang="ts">
import { ref, watch } from 'vue'
import DetailDrawer from '../base/DetailDrawer.vue'
import StatePanel from '../base/StatePanel.vue'
import { fetchKbAssets } from '../../api/governance'
import type { KbAssetsPayload } from '../../api/governance'
import type { KbAsset } from '../../api/types'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    ds: string
  }>(),
  {},
)

const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const ui = useUiStore()
const loading = ref(false)
const error = ref('')
const payload = ref<KbAssetsPayload | null>(null)

async function load() {
  if (!props.ds) return
  loading.value = true
  error.value = ''
  try {
    payload.value = await fetchKbAssets(props.ds)
  } catch (e) {
    payload.value = null
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.ds] as const,
  ([open]) => {
    if (open) void load()
  },
  { immediate: true },
)

/** 采纳与否从 refused 记账读(键是 `<ds>/<file>` 相对路径)。 */
function refusal(file: string): string {
  const map = payload.value?.refused ?? {}
  const key = `${props.ds}/${file}`
  if (key in map) return map[key]
  const hit = Object.keys(map).find((k) => k.endsWith(`/${file}`))
  return hit ? map[hit] : ''
}

function adopted(asset: KbAsset): boolean {
  return !refusal(asset.file)
}

const yes = () => t('govAssetYes', ui.lang)
const no = () => t('govAssetNo', ui.lang)
</script>

<template>
  <DetailDrawer
    :model-value="modelValue"
    :title="`${t('govAssetTitle', ui.lang)} · ${ds}`"
    width="620px"
    :close-label="t('close', ui.lang)"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <StatePanel
      v-if="loading"
      mode="loading"
      :title="t('govLoading', ui.lang)"
    />
    <StatePanel
      v-else-if="error"
      mode="error"
      :title="t('govAssetError', ui.lang)"
      :detail="error"
      :retry-text="t('retry', ui.lang)"
      @retry="load"
    />
    <StatePanel
      v-else-if="!payload || payload.assets.length === 0"
      mode="empty"
      :title="t('govAssetEmpty', ui.lang)"
    />
    <div v-else class="asset-panel">
      <p class="ap-desc">{{ t('govAssetDesc', ui.lang) }}</p>
      <table class="ap-table">
        <thead>
          <tr>
            <th>{{ t('govAssetFile', ui.lang) }}</th>
            <th>{{ t('govAssetGenerator', ui.lang) }}</th>
            <th>{{ t('govAssetEdited', ui.lang) }}</th>
            <th>{{ t('govAssetAdopted', ui.lang) }}</th>
            <th>{{ t('govAssetReason', ui.lang) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in payload.assets" :key="a.file" :class="{ 'is-refused': !adopted(a) }">
            <td class="mono">{{ a.file }}</td>
            <td>{{ a.generator || '—' }}</td>
            <td>
              <span v-if="a.edited === true">{{ yes() }}</span>
              <span v-else-if="a.edited === false">{{ no() }}</span>
              <span v-else class="ap-unknown" :title="t('govAssetUnknown', ui.lang)">—</span>
            </td>
            <td>
              <span class="ap-adopt" :class="{ 'is-bad': !adopted(a) }">
                {{ adopted(a) ? yes() : no() }}
              </span>
            </td>
            <td class="ap-reason">
              <span v-if="refusal(a.file)">{{ refusal(a.file) }}</span>
              <span v-else>—</span>
              <span v-if="a.error" class="ap-extra">格式门: {{ a.error }}</span>
              <span v-else-if="a.refused" class="ap-extra">格式门: {{ a.refused }}</span>
              <span v-if="a.needs_migration" class="ap-extra">需迁移</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </DetailDrawer>
</template>

<style scoped>
.asset-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.ap-desc {
  margin: 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.ap-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-xs);
}
.ap-table th {
  text-align: left;
  color: var(--text-tertiary);
  font-weight: 500;
  padding: 2px 10px 2px 0;
  border-bottom: 1px solid var(--border-subtle);
  white-space: nowrap;
}
.ap-table td {
  padding: 4px 10px 4px 0;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: top;
}
.ap-table tr.is-refused td {
  background: var(--warn-soft, var(--surface-sunken));
}
.mono {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-2xs);
  white-space: nowrap;
}
.ap-unknown {
  color: var(--text-tertiary);
}
.ap-adopt.is-bad {
  color: var(--danger, #d64545);
  font-weight: 600;
}
.ap-reason {
  word-break: break-word;
  color: var(--text-secondary);
}
.ap-extra {
  display: block;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
</style>
