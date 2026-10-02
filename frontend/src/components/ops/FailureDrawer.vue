<!--
  FailureDrawer — 单条失败的全部证据(问题、预测 SQL、gold SQL、错误、run_id)。
  gold SQL 只在这里出现,而本页是 admin-only 端点(§4.1 规则 5)。

  起草教训 = **预填 + 人工写入**(裁决 ⑤):复制一段可直接粘进 lessons.yml
  的 YAML 草稿,不调用任何新端点、不落库 —— 落库仍走知识库页的「起草 →
  人工确认」闸门。按钮旁的说明就是这件事本身,别省。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { Copy } from 'lucide-vue-next'
import type { OpsFailureItem } from '../../api/ops'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { copyText } from '../../utils/format'
import { notifySuccess } from '../../utils/notify'
import DetailDrawer from '../base/DetailDrawer.vue'

const props = defineProps<{
  modelValue: boolean
  item: OpsFailureItem | null
}>()

const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()

const ui = useUiStore()

const draft = computed(() => {
  const it = props.item
  if (!it) return ''
  const note = [it.verdict, it.path || '', it.error || ''].filter(Boolean).join(' · ')
  return [
    'lessons:',
    `  - pattern: ${it.question || it.qid}`,
    `    note: ${note}`,
    `    sql_snippet: ${(it.pred_sql || '').replace(/\n/g, ' ')}`,
    '    confirmed: false',
    '',
  ].join('\n')
})

async function copyDraft() {
  const ok = await copyText(draft.value)
  if (ok) notifySuccess(t('opsFailureDraftCopied', ui.lang))
}
</script>

<template>
  <DetailDrawer
    :model-value="modelValue"
    :title="t('opsFailureTitle', ui.lang)"
    :close-label="t('opsFailureClose', ui.lang)"
    width="560px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template v-if="item" #default>
      <div class="fd-meta">
        <code class="fd-qid">{{ item.qid || '—' }}</code>
        <span class="fd-verdict" :class="`is-${item.verdict.toLowerCase()}`">{{ item.verdict }}</span>
        <span v-if="item.path" class="fd-path">{{ item.path }}</span>
        <span class="fd-retries">{{ t('opsColRetries', ui.lang) }}: {{ item.retries }}</span>
      </div>

      <section class="fd-block">
        <h4 class="fd-label">{{ t('opsFailureQuestion', ui.lang) }}</h4>
        <p class="fd-text">{{ item.question || '—' }}</p>
      </section>

      <section class="fd-block">
        <h4 class="fd-label">{{ t('opsFailurePred', ui.lang) }}</h4>
        <pre class="fd-sql">{{ item.pred_sql || '—' }}</pre>
      </section>

      <section class="fd-block">
        <h4 class="fd-label">{{ t('opsFailureGold', ui.lang) }}</h4>
        <pre v-if="item.gold_sql" class="fd-sql">{{ item.gold_sql }}</pre>
        <p v-else class="fd-muted">{{ t('opsFailureNoGold', ui.lang) }}</p>
      </section>

      <section v-if="item.error" class="fd-block">
        <h4 class="fd-label">{{ t('opsFailureError', ui.lang) }}</h4>
        <pre class="fd-sql is-error">{{ item.error }}</pre>
      </section>

      <section class="fd-block">
        <h4 class="fd-label">{{ t('opsFailureRunId', ui.lang) }}</h4>
        <code class="fd-run">{{ item.run_id || '—' }}</code>
      </section>
    </template>

    <template #footer>
      <div class="fd-footer">
        <button type="button" class="fd-btn" :disabled="!item" @click="copyDraft">
          <Copy :size="14" /> {{ t('opsFailureDraft', ui.lang) }}
        </button>
        <p class="fd-hint">{{ t('opsFailureDraftHint', ui.lang) }}</p>
      </div>
    </template>
  </DetailDrawer>
</template>

<style scoped>
.fd-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-bottom: var(--sp-3);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.fd-qid {
  font-weight: 600;
  color: var(--text-primary);
}
.fd-verdict {
  padding: 1px 6px;
  border-radius: var(--r-sm);
  border: 1px solid var(--border-subtle);
  font-weight: 600;
}
.fd-verdict.is-mismatch {
  color: var(--danger, #b91c1c);
}
.fd-block {
  margin-bottom: var(--sp-3);
}
.fd-label {
  margin: 0 0 4px;
  font-size: var(--fs-2xs);
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.fd-text {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-primary);
}
.fd-sql {
  margin: 0;
  padding: var(--sp-2);
  border-radius: var(--r-sm);
  background: var(--surface-sunken);
  border: 1px solid var(--border-subtle);
  font-size: var(--fs-2xs);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow: auto;
}
.fd-sql.is-error {
  color: var(--danger, #b91c1c);
}
.fd-muted {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}
.fd-run {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.fd-footer {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.fd-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  align-self: flex-start;
  padding: 5px 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.fd-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.fd-hint {
  margin: 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
</style>
