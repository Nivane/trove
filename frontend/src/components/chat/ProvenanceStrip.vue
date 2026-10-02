<!--
  溯源条(P1 答案可信层):答案自带的身份,默认一行。
  折叠态 = 「数据源 · 时间 · 模型 · run_id」+ 状态 chip(KB 命中 / 校验 /
  脱敏三态);点开才是明细 —— 披露不许打扰阅读节奏(一行是承诺,不是装饰)。

  纪律三道,与后端 output.py 的 I7 同源:
  1. 拿不到就不显示:任一片段缺席则整段/整行不渲染,不编「未知」;
  2. 脱敏三态不合并:null / 空 / bypass 各说各话(bypass 是最该说清的一次);
  3. 明细最多 12 行,超出的进依据抽屉。
-->
<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import { Check, ChevronDown, ChevronUp, Copy } from 'lucide-vue-next'
import { t } from '../../i18n'
import type { Lang } from '../../i18n'
import type { DoneSummary } from '../../api/types'
import {
  buildChips,
  buildProvenance,
  sourceLabelKey,
  statusLabelKey,
  type ProvChip,
} from '../../utils/provenance'
import { copyText, trunc } from '../../utils/format'

const props = defineProps<{
  summary: DoneSummary | null
  /** 这轮的落盘时刻(live 轮 = done 时刻;历史轮 = 消息 timestamp)。 */
  at?: string
  lang: Lang
}>()

const emit = defineEmits<{
  (e: 'open-evidence'): void
  (e: 'open-replay'): void
}>()

const open = ref(false)
const copied = ref(false)
const uid = useId()
const detailId = `${uid}-detail`

const view = computed(() => buildProvenance(props.summary, props.at))
const chips = computed(() => buildChips(view.value))

const idParts = computed(() => {
  const x = view.value
  const parts: string[] = []
  if (x.datasource) parts.push(x.datasource)
  if (x.time) parts.push(x.time)
  if (x.model) parts.push(x.model)
  if (x.runShort) parts.push(`run ${x.runShort}`)
  return parts
})

const hasAny = computed(() => idParts.value.length > 0 || chips.value.length > 0)

function chipText(chip: ProvChip): string {
  switch (chip.id) {
    case 'kb':
      return `${t('provChipKb', props.lang)} ${chip.n ?? 0}`
    case 'verify':
      return `${t('provChipVerify', props.lang)} ${trunc(view.value.verdict, 24)}`
    case 'mask':
      return chip.titleKey === 'bypassBadgeTip'
        ? t('bypassBadge', props.lang)
        : `${t('maskedBadge', props.lang)} ${chip.n ?? 0}`
  }
}

function chipTitle(chip: ProvChip): string {
  return chip.titleKey ? t(chip.titleKey, props.lang) : ''
}

const sourceText = computed(() => {
  const raw = view.value.source
  if (!raw) return ''
  const key = sourceLabelKey(raw)
  return key ? `${t(key, props.lang)}（${raw}）` : raw
})

const modelText = computed(() =>
  view.value.model
    ? `${view.value.model} ${t('provModelSuffix', props.lang)}`
    : '',
)

const timeText = computed(() => {
  const x = view.value
  if (!x.time) return ''
  return x.elapsed ? `${x.time} · ${t('provElapsed', props.lang)} ${x.elapsed}` : x.time
})

const confidenceText = computed(() => {
  const x = view.value
  const parts: string[] = []
  if (x.confidence) parts.push(x.confidence)
  if (x.sqlConfidence) parts.push(`SQL ${x.sqlConfidence}`)
  if (x.confidenceWhy.length) parts.push(x.confidenceWhy.join(' · '))
  return parts.join(' · ')
})

const asOfText = computed(() => {
  const x = view.value
  if (!x.asOf) return ''
  if (x.asOfBasis && x.asOfBasis !== 'unknown')
    return `${x.asOf}（${t('provAsOfBasis', props.lang)}：${x.asOfBasis}）`
  return x.asOf
})

function hitStatus(status: string): string {
  const key = statusLabelKey(status)
  return key ? t(key, props.lang) : status
}

const kbText = computed(() =>
  view.value.kbHits
    .map((h) => (h.status ? `${h.label}（${hitStatus(h.status)}）` : h.label))
    .join(' · '),
)

const maskText = computed(() => {
  const x = view.value
  if (!x.mask) return ''
  if (x.mask.kind === 'bypass') return t('bypassBadgeTip', props.lang)
  const fields = x.maskedFields.length ? `（${x.maskedFields.join(' / ')}）` : ''
  return `${t('maskedBadge', props.lang)} ${x.mask.count}${fields} · ${t('provNoBypass', props.lang)}`
})

const identityText = computed(() => {
  const x = view.value
  if (!x.principalSubject) return ''
  const parts = [x.principalSubject]
  const role = roleText(x.principalRole)
  if (role) parts.push(`(${role})`)
  parts.push(
    x.onBehalfOf ? `on_behalf_of: ${x.onBehalfOf}` : t('provIdentitySelf', props.lang),
  )
  return parts.join(' · ')
})

function roleText(role: string): string {
  switch (role) {
    case 'admin':
      return t('adminRole', props.lang)
    case 'analyst':
      return t('analystRole', props.lang)
    case 'user':
      return t('userRole', props.lang)
    default:
      return role
  }
}

async function copyRunId() {
  const ok = await copyText(view.value.runId)
  if (!ok) return
  copied.value = true
  window.setTimeout(() => {
    copied.value = false
  }, 1600)
}
</script>

<template>
  <div v-if="hasAny" class="prov" :class="{ open }">
    <button
      type="button"
      class="prov-bar"
      :aria-expanded="open"
      :aria-controls="detailId"
      @click="open = !open"
    >
      <span class="prov-id mono">{{ idParts.join(' · ') }}</span>
      <span class="prov-sum">
        <span
          v-for="chip in chips"
          :key="chip.id"
          class="prov-chip"
          :class="chip.tone"
          :title="chipTitle(chip)"
        >
          {{ chipText(chip) }}
        </span>
      </span>
      <span class="prov-caret">
        {{ t('provDetails', lang) }}
        <ChevronUp v-if="open" :size="12" />
        <ChevronDown v-else :size="12" />
      </span>
    </button>

    <div v-if="open" :id="detailId" class="prov-detail">
      <dl class="prov-grid">
        <div v-if="view.datasource" class="prov-row">
          <dt>{{ t('provRowDatasource', lang) }}</dt>
          <dd>{{ view.datasource }}</dd>
        </div>
        <div v-if="timeText" class="prov-row">
          <dt>{{ t('provRowGeneratedAt', lang) }}</dt>
          <dd>{{ timeText }}</dd>
        </div>
        <div v-if="modelText" class="prov-row">
          <dt>{{ t('provRowModel', lang) }}</dt>
          <dd>{{ modelText }}</dd>
        </div>
        <div v-if="view.runId" class="prov-row">
          <dt>run_id</dt>
          <dd class="run-cell">
            <span class="mono run-id">{{ view.runId }}</span>
            <button type="button" class="prov-mini" :title="t('copy', lang)" @click="copyRunId()">
              <Check v-if="copied" :size="12" />
              <Copy v-else :size="12" />
              {{ copied ? t('copied', lang) : t('copy', lang) }}
            </button>
            <button
              type="button"
              class="prov-mini"
              :title="t('provReplay', lang)"
              @click="emit('open-replay')"
            >
              {{ t('provReplay', lang) }} →
            </button>
          </dd>
        </div>
        <div v-if="sourceText" class="prov-row">
          <dt>{{ t('provRowSource', lang) }}</dt>
          <dd>{{ sourceText }}</dd>
        </div>
        <div v-if="confidenceText" class="prov-row">
          <dt>{{ t('provRowConfidence', lang) }}</dt>
          <dd>{{ confidenceText }}</dd>
        </div>
        <div v-if="asOfText" class="prov-row">
          <dt>{{ t('provRowAsOf', lang) }}</dt>
          <dd>{{ asOfText }}</dd>
        </div>
        <div v-if="kbText" class="prov-row">
          <dt>{{ t('provRowKb', lang) }}</dt>
          <dd>{{ kbText }}</dd>
        </div>
        <div v-if="view.verdict" class="prov-row">
          <dt>{{ t('provRowVerify', lang) }}</dt>
          <dd>{{ view.verdict }}</dd>
        </div>
        <div v-if="maskText" class="prov-row">
          <dt>{{ t('provRowMask', lang) }}</dt>
          <dd>{{ maskText }}</dd>
        </div>
        <div v-if="identityText" class="prov-row">
          <dt>{{ t('provRowIdentity', lang) }}</dt>
          <dd>{{ identityText }}</dd>
        </div>
      </dl>
      <div class="prov-foot">
        <button type="button" class="prov-mini evidence-cta" @click="emit('open-evidence')">
          {{ t('provOpenEvidence', lang) }}
        </button>
        <span>{{ t('provFoot', lang) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.prov {
  margin-top: var(--sp-3);
}

.prov-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  border: 1px dashed var(--border-default);
  border-radius: var(--r-md);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  line-height: var(--lh-normal);
  text-align: left;
  cursor: pointer;
}
.prov-bar:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.prov-bar:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
.prov.open .prov-bar {
  border-bottom-style: solid;
  border-radius: var(--r-md) var(--r-md) 0 0;
}

.prov-id {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.prov-sum {
  display: flex;
  gap: var(--sp-1);
  margin-left: auto;
  flex: none;
}

.prov-chip {
  padding: 0 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-full);
  background: var(--surface-raised);
  white-space: nowrap;
}
.prov-chip.ok {
  border-color: var(--green-100);
  background: var(--ok-bg);
  color: var(--ok-text);
}
.prov-chip.warn {
  border-color: var(--amber-100);
  background: var(--warn-bg);
  color: var(--warn-text);
}

.prov-caret {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex: none;
  color: var(--text-tertiary);
}

.prov-detail {
  padding: var(--sp-3);
  border: 1px solid var(--border-default);
  border-top: none;
  border-radius: 0 0 var(--r-md) var(--r-md);
  background: var(--surface-raised);
}

.prov-grid {
  display: grid;
  grid-template-columns: 84px 1fr;
  row-gap: var(--sp-2);
  column-gap: var(--sp-3);
  margin: 0;
}
.prov-row {
  display: contents;
}
dt {
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
dd {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-xs);
  word-break: break-word;
}

.run-cell {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.run-id {
  font-size: var(--fs-2xs);
}

.prov-mini {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 7px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  cursor: pointer;
}
.prov-mini:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.prov-mini:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}

.prov-foot {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
  padding-top: var(--sp-2);
  border-top: 1px dashed var(--border-subtle);
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.evidence-cta {
  flex: none;
  color: var(--indigo-600);
  border-color: var(--indigo-100);
  background: var(--surface-accent);
}
</style>
