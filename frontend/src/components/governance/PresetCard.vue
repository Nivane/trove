<!--
  PresetCard — 预设包(接入模板)的治理入口,挂在治理中心「覆盖与体检」页。

  三件事,一件不多:
    · 列出内置 + 组织两份来源(同名遮蔽的行标出来 —— 那是**没生效**的那份);
    · 选数据源 → 套用;
    · 把套用报告逐条摊开(drafted / skipped / unresolved 三分,reason 原样)。

  诚实点(与后端同一条):套用只落 pending 草稿,**什么都没生效**。所以
  「落草稿 N 条」的旁边必须站着「逐条确认后才生效」,且 `unresolved` 与
  `drafted` 用不同的样式 —— 一次引用全没解析上的套用不该看起来像成功。
  只读角色(analyst)把「套用」按钮藏掉(后端仍是 403,隐藏是体验层)。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { useReadOnly } from '../../composables/useReadOnly'
import { toastError } from '../../utils/notify'
import {
  applyPreset,
  fetchPresets,
  type PresetApplyReport,
  type PresetBrief,
} from '../../api/presets'

const props = withDefaults(
  defineProps<{
    /** 可选数据源(来自当前覆盖率清单;空数组 = 还没有源可选)。 */
    datasources?: string[]
    /** 默认选中的数据源(页面当前的 ds 筛选值)。 */
    datasource?: string
  }>(),
  { datasources: () => [], datasource: '' },
)

const ui = useUiStore()
const { readOnly } = useReadOnly()

const presets = ref<PresetBrief[]>([])
const loading = ref(false)
const error = ref('')
const picked = ref('')
const target = ref(props.datasource)
const busy = ref(false)
const report = ref<PresetApplyReport | null>(null)

/** 变更源清单时跟随页面筛选;选过的值不覆盖。 */
const sources = computed(() => props.datasources.filter(Boolean))

const counts = computed(() => report.value?.counts ?? {})
const items = computed(() => report.value?.items ?? [])

function label(p: PresetBrief): string {
  const parts = [p.name]
  if (p.source === 'org') parts.push(t('presetSourceOrg', ui.lang))
  if (p.shadowed) parts.push(t('presetShadowed', ui.lang))
  return parts.join(' · ')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    presets.value = await fetchPresets()
    if (!picked.value && presets.value.length) {
      picked.value = presets.value[0].name
    }
    if (!target.value && sources.value.length) target.value = sources.value[0]
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function onApply() {
  if (!picked.value || !target.value || busy.value) return
  busy.value = true
  report.value = null
  try {
    report.value = await applyPreset(picked.value, target.value)
  } catch (e) {
    toastError(e, t('presetApplyFailed', ui.lang))
  } finally {
    busy.value = false
  }
}

onMounted(load)

/** 页面顶栏的 ds 筛选是显式动作 —— 卡片的默认目标跟着它走。 */
watch(
  () => props.datasource,
  (v) => {
    if (v) target.value = v
  },
)
</script>

<template>
  <div class="preset-card">
    <div class="pc-head">
      <span class="pc-title">{{ t('presetTitle', ui.lang) }}</span>
      <span class="pc-desc">{{ t('presetDesc', ui.lang) }}</span>
    </div>

    <p v-if="error" class="pc-error">{{ t('presetLoadError', ui.lang) }}: {{ error }}</p>
    <p v-else-if="loading" class="pc-muted">{{ t('govLoading', ui.lang) }}</p>
    <p v-else-if="!presets.length" class="pc-muted">{{ t('presetEmpty', ui.lang) }}</p>

    <div v-else class="pc-row">
      <el-select
        v-model="picked"
        class="pc-select"
        :aria-label="t('presetTitle', ui.lang)"
      >
        <el-option
          v-for="p in presets"
          :key="`${p.source}:${p.name}`"
          :value="p.name"
          :label="label(p)"
        />
      </el-select>

      <el-select
        v-model="target"
        class="pc-select"
        :placeholder="t('presetPickDatasource', ui.lang)"
        :aria-label="t('presetPickDatasource', ui.lang)"
      >
        <el-option v-for="ds in sources" :key="ds" :value="ds" :label="ds" />
      </el-select>

      <el-button
        v-if="!readOnly"
        type="primary"
        :loading="busy"
        :disabled="!picked || !target"
        @click="onApply"
      >
        {{ t('presetApply', ui.lang) }}
      </el-button>
    </div>

    <p v-if="!loading && presets.length" class="pc-muted pc-hint">
      {{ t('presetHint', ui.lang) }}
    </p>

    <div v-if="report" class="pc-report">
      <div class="pc-report-head">
        <span class="pc-report-title">
          {{ t('presetReportTitle', ui.lang) }} —
          {{ report.preset }} → {{ report.datasource }}
        </span>
        <span class="pc-counts">
          {{ t('presetDraftedN', ui.lang, counts.drafted ?? 0) }} ·
          {{ t('presetSkippedN', ui.lang, counts.skipped ?? 0) }} ·
          {{ t('presetUnresolvedN', ui.lang, counts.unresolved ?? 0) }}
        </span>
      </div>

      <ul class="pc-items">
        <li
          v-for="(it, i) in items"
          :key="`${it.section}:${it.item}:${i}`"
          class="pc-item"
          :class="`is-${it.status}`"
        >
          <span class="pc-status">{{ t(`presetStatus_${it.status}`, ui.lang) }}</span>
          <span class="pc-sec">{{ it.section }}</span>
          <span class="pc-name">{{ it.item }}</span>
          <span class="pc-reason">{{ it.reason }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.preset-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  padding: var(--sp-3);
  margin-top: var(--sp-3);
}
.pc-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.pc-title {
  font-weight: 600;
}
.pc-desc {
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.pc-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: var(--sp-2);
}
.pc-select {
  min-width: 180px;
}
.pc-muted {
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
  margin: 4px 0 0;
}
.pc-error {
  color: var(--danger-text);
  font-size: var(--fs-2xs);
  margin: 4px 0 0;
}
.pc-report {
  margin-top: var(--sp-3);
  border-top: 1px dashed var(--border-subtle);
  padding-top: var(--sp-2);
}
.pc-report-head {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: baseline;
}
.pc-report-title {
  font-weight: 600;
}
.pc-counts {
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}
.pc-items {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  font-size: var(--fs-2xs);
}
.pc-item {
  display: flex;
  gap: 6px;
  align-items: baseline;
  padding: 2px 0;
  flex-wrap: wrap;
}
.pc-status {
  min-width: 3.5em;
  font-weight: 600;
}
.is-drafted .pc-status {
  color: var(--accent);
}
.is-skipped .pc-status {
  color: var(--text-tertiary);
}
.is-unresolved .pc-status {
  color: var(--danger-text);
}
.pc-sec,
.pc-name {
  font-family: var(--font-mono, monospace);
  color: var(--text-secondary);
}
.pc-reason {
  color: var(--text-tertiary);
}
</style>
