<!--
  ObjectPanel — 本体页右侧详情栏（前端方案 §3.5）。

  三态：对象 / 链接 / 未选。内容全部来自投影 DTO 的字段，零推断：
  · 对象：显示名（标签回退数据集名）+ 声明徽标 + 自然键 / 别名 / 度量数 / 字段数；
  · 链接：显示名（回退列连接文案）+ 方向 + 连接列 + 基数 + fan_out 警示；
  · 未选：操作提示。

  页内无第二编辑面：唯一的动作是「编辑（回语义台）」深链，
  URL 由 ontologyEditHref 纯函数生成（对象带 q=<dataset>，链接进关系 tab）。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { ObjectLinkInfo, ObjectNodeInfo } from '../../api/types'
import {
  isFanout,
  objectLabel,
  ontologyEditHref,
  type OntologySelection,
} from '../../utils/ontologyChart'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const props = defineProps<{
  ds: string
  selection?: OntologySelection | null
  objects: ObjectNodeInfo[]
  links: ObjectLinkInfo[]
}>()

const ui = useUiStore()

const sel = computed(() => props.selection ?? null)

/** 选中的对象（找不到 = 未选态；ds 切换后残值不渲染半个面板）。 */
const obj = computed<ObjectNodeInfo | null>(() => {
  const s = sel.value
  if (s?.kind !== 'object') return null
  return props.objects.find((o) => o.dataset === s.ref) ?? null
})

/** 选中的链接。 */
const link = computed<ObjectLinkInfo | null>(() => {
  const s = sel.value
  if (s?.kind !== 'link') return null
  return props.links.find((l) => l.name === s.ref) ?? null
})

const mode = computed<'object' | 'link' | 'none'>(() => {
  if (obj.value) return 'object'
  if (link.value) return 'link'
  return 'none'
})

/** 关联数据集在方向行里显示为它的标签（缺失回退数据集名——名字总是有的）。 */
function datasetLabel(name: string): string {
  const o = props.objects.find((x) => x.dataset === name)
  return o ? objectLabel(o) : name
}

const direction = computed(() => {
  const l = link.value
  if (!l) return ''
  return `${datasetLabel(l.from)} → ${datasetLabel(l.to)}`
})

const joinColumns = computed(() => {
  const l = link.value
  if (!l) return ''
  const from = (l.from_columns ?? []).join(', ')
  const to = (l.to_columns ?? []).join(', ')
  return `${from} → ${to}`
})

const editHref = computed(() => {
  const s = sel.value
  if (!s) return ''
  return ontologyEditHref(s.kind, s.ref, props.ds)
})

const fanout = computed(() => (link.value ? isFanout(link.value) : false))

function listText(items?: string[]): string {
  const v = (items ?? []).filter((x) => x)
  return v.length ? v.join(' · ') : '—'
}
</script>

<template>
  <aside class="onto-panel" :aria-label="t('ontology', ui.lang)">
    <!-- 未选：操作提示 -->
    <p v-if="mode === 'none'" class="onto-panel-hint">{{ t('ontologyPanelHint', ui.lang) }}</p>

    <!-- 对象 -->
    <template v-else-if="mode === 'object' && obj">
      <div class="onto-panel-head">
        <h3 class="onto-panel-title">{{ objectLabel(obj) }}</h3>
        <span class="onto-badge" :class="obj.declared ? 'is-declared' : 'is-undeclared'">
          {{ obj.declared ? t('ontologyDeclared', ui.lang) : t('ontologyUndeclared', ui.lang) }}
        </span>
      </div>
      <dl class="onto-rows">
        <div class="onto-row">
          <dt>{{ t('semDatasets', ui.lang) }}</dt>
          <dd class="is-mono">{{ obj.dataset }}</dd>
        </div>
        <div class="onto-row">
          <dt>{{ t('ontologyNaturalKeys', ui.lang) }}</dt>
          <dd class="is-mono">{{ listText(obj.natural_keys) }}</dd>
        </div>
        <div class="onto-row">
          <dt>{{ t('ontologyAliases', ui.lang) }}</dt>
          <dd>{{ listText(obj.aliases) }}</dd>
        </div>
        <div class="onto-row">
          <dt>{{ t('ontologyMetricsCount', ui.lang) }}</dt>
          <dd>{{ obj.metrics?.length ?? 0 }}</dd>
        </div>
        <div class="onto-row">
          <dt>{{ t('ontologyFieldsCount', ui.lang) }}</dt>
          <dd>{{ obj.field_count ?? 0 }}</dd>
        </div>
      </dl>
      <RouterLink class="onto-panel-link" :to="editHref">
        {{ t('ontologyEditDeepLink', ui.lang) }}
      </RouterLink>
    </template>

    <!-- 链接 -->
    <template v-else-if="mode === 'link' && link">
      <div class="onto-panel-head">
        <h3 class="onto-panel-title">{{ link.display_name || link.name }}</h3>
      </div>
      <dl class="onto-rows">
        <div class="onto-row">
          <dt>{{ t('ontologyDirection', ui.lang) }}</dt>
          <dd>{{ direction }}</dd>
        </div>
        <div class="onto-row">
          <dt>{{ t('ontologyLinkColumns', ui.lang) }}</dt>
          <dd class="is-mono">{{ joinColumns }}</dd>
        </div>
        <div class="onto-row">
          <dt>{{ t('ontologyLinkCardinality', ui.lang) }}</dt>
          <dd>{{ link.cardinality || '—' }}</dd>
        </div>
      </dl>
      <p v-if="fanout" class="onto-fanout" role="note">{{ t('ontologyFanoutWarn', ui.lang) }}</p>
      <RouterLink class="onto-panel-link" :to="editHref">
        {{ t('ontologyEditDeepLink', ui.lang) }}
      </RouterLink>
    </template>
  </aside>
</template>

<style scoped>
.onto-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  min-width: 0;
}

.onto-panel-hint {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}

.onto-panel-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.onto-panel-title {
  margin: 0;
  font-size: var(--fs-md);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.onto-badge {
  padding: 1px var(--sp-1);
  border-radius: var(--r-sm);
  font-size: var(--fs-xs);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}

.onto-badge.is-declared {
  color: var(--accent);
  border-color: var(--accent);
}

.onto-badge.is-undeclared {
  color: var(--text-secondary);
}

.onto-rows {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin: 0;
}

.onto-row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
}

.onto-row dt {
  flex: 0 0 76px;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}

.onto-row dd {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}

.onto-row dd.is-mono {
  font-family: var(--font-mono);
}

.onto-fanout {
  margin: 0;
  padding: var(--sp-1) var(--sp-2);
  border-radius: var(--r-sm);
  background: var(--warn-bg);
  color: var(--warn-text);
  font-size: var(--fs-sm);
}

.onto-panel-link {
  align-self: flex-start;
  color: var(--accent);
  font-size: var(--fs-sm);
}
</style>
