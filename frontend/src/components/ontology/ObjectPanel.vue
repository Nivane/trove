<!--
  ObjectPanel — 本体页右侧详情栏（前端方案 §3.5，2026-10-12 物化层改版）。

  三态：对象 / 链接 / 未选。内容全部来自投影 DTO 的字段，零推断：
  · 对象：身份头（身份砖 + 业务显示名 + 数据集名）+ 声明徽标 + 三项统计
    （度量 / 字段 / 链接）+ 自然键 / 别名 chips + 业务度量清单 + 出/入链接行；
  · 链接：显示名（回退列连接文案）+ 方向（两端身份砖）+ 连接列 + 基数 + 扇出警示；
  · 未选：操作提示（含身份派生规则的一句话说明）。

  出/入链接行是**真按钮**（点击 = 选中该链接，与图上点击同一条 emit）——
  图与侧栏是同一份选中的两个视图，不是两条各自维护的状态。
  页内无第二编辑面：唯一的动作是「编辑（回语义台）」深链（对象带
  q=<dataset>，链接进关系 tab）。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { ObjectLinkInfo, ObjectNodeInfo } from '../../api/types'
import {
  isFanout,
  linkFallback,
  linkHasName,
  objectLabel,
  ontologyEditHref,
  truncateWidth,
  type OntologySelection,
} from '../../utils/ontologyLayout'
import { objectIdentity } from '../../utils/ontologyIdentity'
import IdentityTile from './IdentityTile.vue'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const props = defineProps<{
  ds: string
  selection?: OntologySelection | null
  objects: ObjectNodeInfo[]
  links: ObjectLinkInfo[]
}>()

const emit = defineEmits<{
  select: [OntologySelection]
  /** Esc / 图外点击：侧栏回到未选态（与图同一条出口）。 */
  clear: []
}>()

const ui = useUiStore()

/** 业务度量清单的展示上限（超额明写 + n 条，绝不静默截断）。 */
const METRIC_PREVIEW = 6

const sel = computed(() => props.selection ?? null)

/** 选中的对象（找不到 = 未选态；ds 切换后残值不渲染半个面板）。 */
const obj = computed<ObjectNodeInfo | null>(() => {
  const s = sel.value
  if (s?.kind !== 'object') return null
  return props.objects.find((o) => o.dataset === s.ref) ?? null
})

/** 选中的链接（按关系名查，与图的 emit 同键）。 */
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

/** 出/入链接（只含两端都在图上的链接；悬空的由图上提示条点名）。 */
const outLinks = computed(() => {
  const o = obj.value
  if (!o) return []
  return props.links.filter((l) => l.from === o.dataset && known(l.to))
})

const inLinks = computed(() => {
  const o = obj.value
  if (!o) return []
  return props.links.filter((l) => l.to === o.dataset && known(l.from))
})

function known(dataset: string): boolean {
  return props.objects.some((x) => x.dataset === dataset)
}

function identityOf(name: string) {
  return objectIdentity(props.objects.find((x) => x.dataset === name))
}

/** 关系标题原文：声明了名字用名字，否则回落列连接串。 */
function linkRawTitle(l: ObjectLinkInfo): string {
  return linkHasName(l) ? (l.display_name ?? '').trim() : linkFallback(l)
}

/** 关系行标题：窄列里按宽截断（旁边还有基数芯片与目标名要占位）。 */
function linkTitle(l: ObjectLinkInfo): string {
  return truncateWidth(linkRawTitle(l), 150, 12.5)
}

/** 面板标题：整行都是它，按真实字号（--fs-md = 18px）给足宽度。 */
const linkHeadline = computed(() => {
  const l = link.value
  return l ? truncateWidth(linkRawTitle(l), 300, 18) : ''
})

const metrics = computed(() => obj.value?.metrics ?? [])
const metricsShown = computed(() => metrics.value.slice(0, METRIC_PREVIEW))
const metricsMore = computed(() => Math.max(0, metrics.value.length - METRIC_PREVIEW))
const linkCount = computed(() => outLinks.value.length + inLinks.value.length)

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

/** fan_out 判据与图同源（同一份 isFanout：两处各写一遍迟早漂移）。 */
const fanout = computed(() => (link.value ? isFanout(link.value) : false))
</script>

<template>
  <aside class="onto-panel" :aria-label="t('ontology', ui.lang)" @keydown.esc="emit('clear')">
    <p class="onto-panel-kind">
      {{
        mode === 'object'
          ? t('ontologyPanelKindObject', ui.lang)
          : mode === 'link'
            ? t('ontologyPanelKindLink', ui.lang)
            : t('ontologyPanelHead', ui.lang)
      }}
    </p>

    <!-- 未选：操作提示 + 身份派生规则 -->
    <template v-if="mode === 'none'">
      <p class="onto-panel-hint">{{ t('ontologyPanelHint', ui.lang) }}</p>
      <p class="onto-panel-hint is-soft">{{ t('ontologyPanelHint2', ui.lang) }}</p>
    </template>

    <!-- 对象 -->
    <template v-else-if="mode === 'object' && obj">
      <div class="onto-panel-head">
        <IdentityTile :identity="objectIdentity(obj)" :size="40" />
        <div class="onto-idhead-text">
          <h3 class="onto-panel-title">{{ objectLabel(obj) }}</h3>
          <div class="onto-mono">{{ obj.dataset }}</div>
        </div>
      </div>
      <div class="onto-badges">
        <span class="onto-badge" :class="obj.declared ? 'is-declared' : 'is-undeclared'">
          {{ obj.declared ? t('ontologyDeclared', ui.lang) : t('ontologyUndeclared', ui.lang) }}
        </span>
        <span class="onto-badge is-muted">
          {{
            obj.declared
              ? t('ontologyIdentityDerived', ui.lang)
              : t('ontologyIdentityUndeclared', ui.lang)
          }}
        </span>
      </div>

      <div class="onto-stats">
        <div>
          <b>{{ metrics.length }}</b>
          <span>{{ t('ontologyMetricsCount', ui.lang) }}</span>
        </div>
        <div>
          <b>{{ obj.field_count ?? 0 }}</b>
          <span>{{ t('ontologyFieldsCount', ui.lang) }}</span>
        </div>
        <div>
          <b>{{ linkCount }}</b>
          <span>{{ t('ontologyKpiLinks', ui.lang) }}</span>
        </div>
      </div>

      <section class="onto-sec">
        <h4>{{ t('ontologyNaturalKeys', ui.lang) }}</h4>
        <div class="onto-chips">
          <span v-for="k in obj.natural_keys ?? []" :key="k" class="onto-chip">{{ k }}</span>
          <span v-if="!(obj.natural_keys ?? []).length" class="onto-dash">—</span>
        </div>
      </section>

      <section class="onto-sec">
        <h4>{{ t('ontologyAliases', ui.lang) }}</h4>
        <div class="onto-chips">
          <span v-for="a in obj.aliases ?? []" :key="a" class="onto-chip">{{ a }}</span>
          <span v-if="!(obj.aliases ?? []).length" class="onto-dash">—</span>
        </div>
      </section>

      <section class="onto-sec">
        <h4>{{ t('ontologyBusinessMetrics', ui.lang, metrics.length) }}</h4>
        <div v-if="metricsShown.length" class="onto-mlist">
          <div v-for="m in metricsShown" :key="m">{{ m }}</div>
          <div v-if="metricsMore" class="is-more">
            {{ t('ontologyMoreMetrics', ui.lang, metricsMore) }}
          </div>
        </div>
        <span v-else class="onto-dash">—</span>
      </section>

      <section class="onto-sec">
        <h4>{{ t('ontologyOutLinks', ui.lang, outLinks.length) }}</h4>
        <button
          v-for="l in outLinks"
          :key="`o${l.name}`"
          type="button"
          class="onto-lrow"
          @click="emit('select', { kind: 'link', ref: l.name })"
        >
          <IdentityTile :identity="identityOf(l.to)" :size="16" />
          <span class="onto-lname" :class="{ 'is-unnamed': !linkHasName(l) }">{{ linkTitle(l) }}</span>
          <span class="onto-chip">{{ l.cardinality || '—' }}</span>
          <span class="onto-ltgt">{{ l.to }}</span>
        </button>
        <span v-if="!outLinks.length" class="onto-dash">—</span>
      </section>

      <section class="onto-sec">
        <h4>{{ t('ontologyInLinks', ui.lang, inLinks.length) }}</h4>
        <button
          v-for="l in inLinks"
          :key="`i${l.name}`"
          type="button"
          class="onto-lrow"
          @click="emit('select', { kind: 'link', ref: l.name })"
        >
          <IdentityTile :identity="identityOf(l.from)" :size="16" />
          <span class="onto-lname" :class="{ 'is-unnamed': !linkHasName(l) }">{{ linkTitle(l) }}</span>
          <span class="onto-chip">{{ l.cardinality || '—' }}</span>
          <span class="onto-ltgt">{{ l.from }}</span>
        </button>
        <span v-if="!inLinks.length" class="onto-dash">—</span>
      </section>
    </template>

    <!-- 链接 -->
    <template v-else-if="mode === 'link' && link">
      <div class="onto-panel-head">
        <div class="onto-idhead-text">
          <h3 class="onto-panel-title">{{ linkHeadline }}</h3>
          <div class="onto-mono">{{ link.name }}</div>
        </div>
      </div>

      <section class="onto-sec">
        <h4>{{ t('ontologyDirection', ui.lang) }}</h4>
        <div class="onto-dir">
          <IdentityTile :identity="identityOf(link.from)" :size="18" />
          <span class="onto-mono2">{{ link.from }}</span>
          <span class="onto-arw">→</span>
          <IdentityTile :identity="identityOf(link.to)" :size="18" />
          <span class="onto-mono2">{{ link.to }}</span>
        </div>
      </section>

      <section class="onto-sec">
        <h4>{{ t('ontologyLinkColumns', ui.lang) }}</h4>
        <div class="onto-mono2 onto-join">{{ joinColumns }}</div>
      </section>

      <section class="onto-sec">
        <h4>{{ t('ontologyLinkCardinality', ui.lang) }}</h4>
        <span class="onto-chip">{{ link.cardinality || '—' }}</span>
      </section>

      <section class="onto-sec">
        <h4>{{ t('ontologyFanoutSection', ui.lang) }}</h4>
        <p v-if="fanout" class="onto-fanout" role="note">
          {{ t('ontologyFanoutWarn', ui.lang) }}
        </p>
        <span v-else class="onto-badge is-muted">{{ t('ontologyNoFanout', ui.lang) }}</span>
      </section>
    </template>

    <RouterLink v-if="mode !== 'none'" class="onto-panel-link" :to="editHref">
      {{ t('ontologyEditDeepLink', ui.lang) }}
    </RouterLink>
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

.onto-panel-kind {
  margin: 0;
  padding-bottom: var(--sp-2);
  border-bottom: 1px solid var(--border-subtle);
  font-size: var(--fs-2xs);
  font-weight: 600;
  letter-spacing: 0.05em;
  color: var(--text-secondary);
}

.onto-panel-hint {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
}

.onto-panel-hint.is-soft {
  color: var(--text-secondary);
  opacity: 0.85;
}

.onto-panel-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}

.onto-idhead-text {
  min-width: 0;
}

.onto-panel-title {
  margin: 0;
  font-size: var(--fs-md);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.onto-mono {
  margin-top: 2px;
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.onto-mono2 {
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.onto-badges {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
}

.onto-badge {
  padding: 2px var(--sp-2);
  border-radius: var(--r-full);
  font-size: var(--fs-2xs);
  font-weight: 600;
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  white-space: nowrap;
}

.onto-badge.is-declared {
  color: var(--ok-text);
  background: var(--ok-bg);
  border-color: var(--ok-border);
}

.onto-badge.is-undeclared,
.onto-badge.is-muted {
  background: var(--surface-muted);
  border-color: transparent;
  font-weight: 500;
}

.onto-stats {
  display: flex;
  gap: var(--sp-2);
}

.onto-stats > div {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  min-width: 0;
}

.onto-stats b {
  font-size: var(--fs-md);
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}

.onto-stats span {
  font-size: var(--fs-2xs);
  color: var(--text-on-muted);
}

.onto-sec {
  margin-top: var(--sp-2);
}

.onto-sec h4 {
  margin: 0 0 var(--sp-1);
  font-size: var(--fs-2xs);
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--text-secondary);
}

.onto-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
}

.onto-chip {
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  padding: 2px var(--sp-2);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  color: var(--text-on-muted);
  white-space: nowrap;
}

.onto-dash {
  color: var(--text-secondary);
  font-size: var(--fs-xs);
}

.onto-mlist {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.onto-mlist .is-more {
  color: var(--text-secondary);
}

.onto-dir {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  flex-wrap: wrap;
}

.onto-arw {
  color: var(--text-secondary);
}

/* 关系行：真按钮（键盘可达），点击 = 选中该链接 */
.onto-lrow {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-1) var(--sp-2);
  border: none;
  border-radius: var(--r-sm);
  background: transparent;
  font-size: var(--fs-2xs);
  text-align: left;
  cursor: pointer;
}

.onto-lrow:hover {
  background: var(--surface-muted);
}

.onto-lrow:focus-visible {
  outline: 2px solid var(--accent-graphic);
  outline-offset: -1px;
}

.onto-lname {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--accent);
}

/* 未声明名字的关系：回退列连接串，等宽弱化（不是「名字」，别用主色） */
.onto-lname.is-unnamed {
  font-family: var(--font-mono);
  color: var(--text-secondary);
}

.onto-ltgt {
  margin-left: auto;
  flex: none;
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}

.onto-fanout {
  margin: 0;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--warn-bg);
  color: var(--warn-text);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
}

.onto-panel-link {
  margin-top: var(--sp-3);
  align-self: stretch;
  padding: var(--sp-2);
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  color: var(--text-primary);
  font-size: var(--fs-xs);
  text-align: center;
  text-decoration: none;
}

.onto-panel-link:hover {
  background: var(--surface-muted);
}
</style>
