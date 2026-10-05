<template>
  <div class="verify-strip">
    <span
      v-for="(st, i) in VERIFY_STAGE_ORDER"
      :key="st"
      class="vwrap"
    >
      <span v-if="i" class="vlink" aria-hidden="true" />
      <span class="vseg" :class="{ lit: lit.has(st) }">
        <span class="d" aria-hidden="true" />
        {{ stageLabel(st, ui.lang) }}
      </span>
    </span>
    <!-- 「已验证」印章:一行,接在骨架右端。三态 —— OK 才盖章(绿),
         有结论但认不出/非 OK 照实说(琥珀),没有结论就不表态(灰,
         只报工序数)。认不出 ≠ 通过(与溯源条同一纪律)。 -->
    <button
      type="button"
      class="vok"
      :class="`tone-${tone}`"
      :title="t('verifyStripOpen', ui.lang)"
      @click="$emit('open')"
    >
      <Check v-if="tone === 'ok'" :size="12" :stroke-width="2.5" aria-hidden="true" />
      <TriangleAlert v-else-if="tone === 'warn'" :size="12" :stroke-width="2.5" aria-hidden="true" />
      <span class="vok-main">{{ seal }}</span>
      <span v-if="rework !== null" class="vok-rework" :class="{ hit: reworkHit }">
        · {{ t('verifyStripRework', ui.lang, rework) }}
      </span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Check, TriangleAlert } from 'lucide-vue-next'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { trunc } from '../../utils/format'
import {
  VERIFY_STAGE_ORDER,
  correctionRounds,
  stageLabel,
  verifyStages,
} from '../../utils/steps'
import type { StepCard } from '../../stores/chat'
import type { DoneSummary } from '../../api/types'

const props = defineProps<{
  steps: StepCard[]
  summary?: DoneSummary | null
  /** 历史步骤被截断(上限之外还有步骤没落盘)—— 计数带 + 后缀。 */
  truncated?: boolean
}>()
defineEmits<{ open: [] }>()

const ui = useUiStore()

const lit = computed(() => verifyStages(props.steps))

const verdict = computed(() => String(props.summary?.verdict ?? ''))
const tone = computed(() =>
  verdict.value === 'OK' ? 'ok' : verdict.value ? 'warn' : 'neutral',
)

/** 工序数(历史截断时带 +)。 */
const count = computed(
  () => `${props.steps.length}${props.truncated ? '+' : ''}`,
)

/** 修正轮数(反思步骤的 retry_count);没跑过反思 → null,不写这句。 */
const rework = computed(() => correctionRounds(props.steps))
const reworkHit = computed(() => (rework.value ?? 0) > 0)

const seal = computed(() => {
  const n = t('verifyStripSteps', ui.lang, count.value)
  if (tone.value === 'ok') return `${t('verifyStripVerified', ui.lang)} · ${n}`
  if (tone.value === 'warn') return `${trunc(verdict.value, 28)} · ${n}`
  return n
})
</script>
