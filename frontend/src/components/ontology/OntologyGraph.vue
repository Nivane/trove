<!--
  OntologyGraph — 本体页的确定性结构图（SVG 渲染壳）。

  几何全部来自 utils/ontologyLayout 的纯函数（分层、路径、标签车道夹取、
  按宽截断），身份来自 utils/ontologyIdentity（图标 + 身份色）—— 组件只做
  四件事：把布局画进 SVG、把指针/键盘事件翻译成 select/clear、把选中态
  翻成 CSS 类（is-hot / is-nb / is-dim）、viewBox 缩放。

  两个刻意的取向：
  · **位置 = 关系流向**（左 → 右分层）。同一份语义模型在任何一次渲染里
    都是同一张图 —— 读图的人能记住形状，截图能对得上。
  · **只放大缩小，不平移**。没有自由平移就没有「拖丢了」：缩放以内容
    中心为轴，复位一键回原点。
  组件不猜业务：空态/错误态由 OntologyView 决定是否渲染本组件。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ObjectLinkInfo, ObjectNodeInfo } from '../../api/types'
import {
  FONT,
  NODE_H,
  NODE_W,
  PAD,
  layoutOntology,
  objectLabel,
  truncateWidth,
  type OntologyEdge,
  type OntologyNode,
  type OntologySelection,
} from '../../utils/ontologyLayout'
import {
  IDENTITY_PALETTE,
  UNDECLARED_COLOR,
  objectIdentity,
  tileFillOpacity,
  type ObjectIdentity,
} from '../../utils/ontologyIdentity'
import GlyphPrimitives from './GlyphPrimitives.vue'
import IdentityTile from './IdentityTile.vue'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const props = defineProps<{
  objects: ObjectNodeInfo[]
  links: ObjectLinkInfo[]
  selected?: OntologySelection | null
}>()

const emit = defineEmits<{
  select: [OntologySelection]
  /** 点空白 / Esc：清掉选中（图与侧栏回到未选态）。 */
  clear: []
}>()

const ui = useUiStore()

const ZOOM_MIN = 1
const ZOOM_MAX = 3

const zoom = ref(1)
const hover = ref<OntologySelection | null>(null)

const layout = computed(() => layoutOntology(props.objects, props.links))

/** 强调目标：悬停优先于选中（移开鼠标即回到选中态）。 */
const active = computed<OntologySelection | null>(() => hover.value ?? props.selected ?? null)

/** 邻接索引：选中/hover 一个对象时，谁是「邻居」谁是「背景」由它决定。 */
const neighbors = computed(() => {
  const m = new Map<string, Set<string>>()
  const add = (a: string, b: string) => {
    const set = m.get(a) ?? new Set<string>()
    set.add(b)
    m.set(a, set)
  }
  for (const e of layout.value.edges) {
    add(e.from, e.to)
    add(e.to, e.from)
  }
  return m
})

/** 渲染用的字号（与 ontologyLayout.FONT 同源 —— 宽度估算与画出来的必须同号）。 */
const fontVars = computed(() => ({
  '--onto-fs-name': `${FONT.name}px`,
  '--onto-fs-key': `${FONT.key}px`,
  '--onto-fs-meta': `${FONT.meta}px`,
  '--onto-fs-label': `${FONT.label}px`,
}))

/** viewBox 缩放：以内容中心为轴，只放大缩小。 */
const viewBox = computed(() => {
  const w = layout.value.width + PAD * 2
  const h = layout.value.height + PAD * 2
  const vw = w / zoom.value
  const vh = h / zoom.value
  return `${w / 2 - vw / 2} ${h / 2 - vh / 2} ${vw} ${vh}`
})

function zoomBy(factor: number) {
  zoom.value = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom.value * factor))
}

function zoomReset() {
  zoom.value = 1
}

function identityOf(n: OntologyNode): ObjectIdentity {
  return objectIdentity(n)
}

/* 三行的截断上限 = 卡片宽 − 文字左起 52 − 右留白 10 = 138，再各留一点余量；
   .nsub 是等宽（真宽约 0.6em，比 textWidth 的 0.56em 保守值更宽），多留些。 */
const CAP_NAME = 124
const CAP_KEY = 124

function nodeName(n: OntologyNode): string {
  return truncateWidth(objectLabel(n), CAP_NAME, FONT.name)
}

function nodeKeys(n: OntologyNode): string {
  return truncateWidth((n.natural_keys ?? []).join(', '), CAP_KEY, FONT.key)
}

/** 计数行是**有界**内容（两个小整数 + 两个标签），不截断 —— 截它只会把
    「2 度量 · 8 字段」骗成省略号；宽度由 NODE_W 保证（见 ontologyLayout）。 */
function nodeMeta(n: OntologyNode): string {
  const metrics = n.metrics?.length ?? 0
  const fields = n.field_count ?? 0
  return `${metrics} ${t('ontologyMetricsCount', ui.lang)} · ${fields} ${t('ontologyFieldsCount', ui.lang)}`
}

/** 节点强调态（与边的 is-dim/is-hot 同源：一处判定，两处渲染）。 */
function nodeState(n: OntologyNode) {
  const act = active.value
  if (!act) return { hot: false, near: false, dim: false }
  if (act.kind === 'object') {
    const hot = n.dataset === act.ref
    const near = !hot && (neighbors.value.get(act.ref)?.has(n.dataset) ?? false)
    return { hot, near, dim: !hot && !near }
  }
  const e = layout.value.edges.find((x) => x.name === act.ref)
  const near = !!e && (e.from === n.dataset || e.to === n.dataset)
  return { hot: false, near, dim: !near }
}

function nodeClass(n: OntologyNode) {
  const s = nodeState(n)
  return {
    'is-undeclared': !n.declared,
    'is-hot': s.hot,
    'is-nb': s.near,
    'is-dim': s.dim,
  }
}

function edgeState(e: OntologyEdge) {
  const act = active.value
  if (!act) return { hot: false, dim: false }
  if (act.kind === 'link') return { hot: e.name === act.ref, dim: e.name !== act.ref }
  const hot = e.from === act.ref || e.to === act.ref
  return { hot, dim: !hot }
}

function edgeClass(e: OntologyEdge) {
  const s = edgeState(e)
  return { 'is-fanout': e.fanout, 'is-hot': s.hot, 'is-dim': s.dim }
}

function pick(kind: 'object' | 'link', ref: string) {
  emit('select', { kind, ref })
}

/** 图例用身份砖：一个是「已声明的通用对象」（兜底立方体），一个是「表」。 */
const LEGEND_DECLARED: ObjectIdentity = {
  declared: true,
  icon: 'cube',
  color: IDENTITY_PALETTE[0],
}
const LEGEND_UNDECLARED: ObjectIdentity = { declared: false, icon: 'table', color: UNDECLARED_COLOR }
</script>

<template>
  <section class="onto-graph-card" :aria-label="t('ontology', ui.lang)">
    <div class="onto-graph-head">
      <div class="onto-legend">
        <span class="onto-lg">
          <IdentityTile :identity="LEGEND_DECLARED" :size="16" />
          {{ t('ontologyLegendDeclared', ui.lang) }}
        </span>
        <span class="onto-lg">
          <IdentityTile :identity="LEGEND_UNDECLARED" :size="16" />
          {{ t('ontologyLegendUndeclared', ui.lang) }}
        </span>
        <span class="onto-lg">
          <svg width="26" height="12" viewBox="0 0 26 12" aria-hidden="true">
            <path class="lg-line" d="M1 6 H17" />
            <path class="lg-head" d="M16 2.2 L23 6 L16 9.8 z" />
          </svg>
          {{ t('ontologyLegendArrow', ui.lang) }}
        </span>
        <span class="onto-lg">
          <svg width="26" height="12" viewBox="0 0 26 12" aria-hidden="true">
            <path class="lg-dash" d="M1 6 H23" />
          </svg>
          {{ t('ontologyLegendFanout', ui.lang) }}
        </span>
      </div>
      <div class="onto-zoom">
        <button
          type="button"
          :aria-label="t('ontologyZoomOut', ui.lang)"
          :disabled="zoom <= ZOOM_MIN + 0.001"
          @click="zoomBy(1 / 1.25)"
        >
          −
        </button>
        <button
          type="button"
          :aria-label="t('ontologyZoomIn', ui.lang)"
          :disabled="zoom >= ZOOM_MAX - 0.001"
          @click="zoomBy(1.25)"
        >
          +
        </button>
        <button type="button" :disabled="zoom <= ZOOM_MIN + 0.001" @click="zoomReset">
          {{ t('ontologyZoomReset', ui.lang) }}
        </button>
      </div>
    </div>

    <svg
      class="onto-svg"
      :viewBox="viewBox"
      preserveAspectRatio="xMidYMid meet"
      :style="fontVars"
      role="img"
      :aria-label="t('ontology', ui.lang)"
      @click="emit('clear')"
      @keydown.esc="emit('clear')"
    >
      <defs>
        <marker
          id="onto-arrow"
          viewBox="0 0 10 10"
          refX="8.5"
          refY="5"
          markerWidth="6.5"
          markerHeight="6.5"
          orient="auto"
        >
          <path class="mk-fill" d="M0 0 L10 5 L0 10 z" />
        </marker>
        <marker
          id="onto-arrow-warn"
          viewBox="0 0 10 10"
          refX="8.5"
          refY="5"
          markerWidth="6.5"
          markerHeight="6.5"
          orient="auto"
        >
          <path class="mk-warn" d="M0 0 L10 5 L0 10 z" />
        </marker>
        <marker
          id="onto-arrow-hot"
          viewBox="0 0 10 10"
          refX="8.5"
          refY="5"
          markerWidth="6.5"
          markerHeight="6.5"
          orient="auto"
        >
          <path class="mk-hot" d="M0 0 L10 5 L0 10 z" />
        </marker>
      </defs>

      <g :transform="`translate(${PAD},${PAD})`">
        <!-- 边在前、节点在后：卡片永远压住线，标签不会盖在卡面上 -->
        <g
          v-for="e in layout.edges"
          :key="`e${e.key}`"
          class="edge-g"
          :class="edgeClass(e)"
          :data-link="e.name"
          @click.stop="pick('link', e.name)"
          @mouseenter="hover = { kind: 'link', ref: e.name }"
          @mouseleave="hover = null"
        >
          <path class="ehit" :d="e.path" />
          <path class="eline" :d="e.path" />
          <g v-if="e.label" class="elab" :transform="`translate(${e.labelX},${e.labelY})`">
            <rect
              :x="-e.labelWidth / 2"
              y="-9"
              :width="e.labelWidth"
              height="18"
              rx="5"
            />
            <text text-anchor="middle" y="4">{{ e.label }}</text>
          </g>
        </g>

        <g
          v-for="n in layout.nodes"
          :key="n.dataset"
          class="node-g"
          :class="nodeClass(n)"
          :transform="`translate(${n.x},${n.y})`"
          :data-dataset="n.dataset"
          role="button"
          tabindex="0"
          :aria-label="objectLabel(n)"
          @click.stop="pick('object', n.dataset)"
          @keydown.enter.prevent.stop="pick('object', n.dataset)"
          @keydown.space.prevent.stop="pick('object', n.dataset)"
          @mouseenter="hover = { kind: 'object', ref: n.dataset }"
          @mouseleave="hover = null"
        >
          <rect class="ncard" :width="NODE_W" :height="NODE_H" rx="12" />
          <g class="ntile" :transform="'translate(12,12)'">
            <rect
              width="30"
              height="30"
              rx="9"
              :fill="identityOf(n).color"
              :fill-opacity="tileFillOpacity(identityOf(n))"
            />
            <GlyphPrimitives :icon="identityOf(n).icon" :color="identityOf(n).color" />
          </g>
          <text class="nname" x="52" y="28">{{ nodeName(n) }}</text>
          <text class="nsub" x="52" y="47">{{ t('ontologyCardKey', ui.lang) }} {{ nodeKeys(n) }}</text>
          <text class="nmeta" x="52" y="63">{{ nodeMeta(n) }}</text>
        </g>
      </g>
    </svg>

    <!-- 悬空链接（指向未声明数据集）：不静默丢 —— KPI 的链接数来自 DTO，
         图上少画一条必须说得出来。正常模型里恒不出现。 -->
    <p v-if="layout.dangling.length" class="onto-dangling" role="status">
      {{ t('ontologyDanglingLinks', ui.lang, layout.dangling.length) }}
    </p>

    <p class="onto-graph-cap">{{ t('ontologyGraphCaption', ui.lang) }}</p>
  </section>
</template>

<style scoped>
.onto-graph-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
  padding: var(--sp-3) var(--sp-3) var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}

.onto-graph-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.onto-legend {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
}

.onto-lg {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

/* 图上的线走 --gray-400（#a1a1aa）：tokens.css 的 legacy 别名块把
   --border-strong 重绑到了 --border-default（gray-300），在「线即内容」的
   图里太淡；gray-400 正是 tokens 注释里「装饰性分隔 / 图形元素」那一档。 */
.lg-line {
  stroke: var(--gray-400);
  stroke-width: 1.4;
  fill: none;
}

.lg-head {
  fill: var(--gray-400);
}

.lg-dash {
  stroke: var(--warn);
  stroke-width: 1.6;
  stroke-dasharray: 5 3.5;
  fill: none;
}

.onto-zoom {
  display: flex;
  gap: var(--sp-1);
}

.onto-zoom button {
  min-width: 28px;
  height: 28px;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-xs);
  cursor: pointer;
}

.onto-zoom button:hover:not(:disabled) {
  background: var(--surface-muted);
}

.onto-zoom button:disabled {
  opacity: 0.4;
  cursor: default;
}

.onto-zoom button:focus-visible {
  outline: 2px solid var(--accent-graphic);
  outline-offset: 1px;
}

.onto-svg {
  display: block;
  width: 100%;
  height: 430px;
}

@media (max-width: 1080px) {
  .onto-svg {
    height: 380px;
  }
}

@media (max-width: 560px) {
  .onto-svg {
    height: 320px;
  }
}

.onto-graph-cap {
  margin: 0;
  font-size: var(--fs-2xs);
  line-height: var(--lh-normal);
  color: var(--text-secondary);
}

.onto-dangling {
  margin: 0;
  padding: var(--sp-1) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--warn-bg);
  color: var(--warn-text);
  font-size: var(--fs-2xs);
}

/* ── 节点卡片 ── */
.node-g {
  cursor: pointer;
}

.node-g:focus {
  outline: none;
}

.ncard {
  fill: var(--surface-raised);
  stroke: var(--border-default);
  stroke-width: 1.5;
}

.node-g.is-undeclared .ncard {
  fill: var(--surface-canvas);
  stroke-dasharray: 5 4;
}

.nname {
  font-size: var(--onto-fs-name);
  font-weight: 600;
  fill: var(--text-primary);
}

.node-g.is-undeclared .nname {
  font-size: 13.5px;
  font-weight: 500;
  fill: var(--text-secondary);
}

.nsub,
.nmeta {
  fill: var(--text-secondary);
}

.nsub {
  font-family: var(--font-mono);
  font-size: var(--onto-fs-key);
}

.nmeta {
  font-size: var(--onto-fs-meta);
}

/* ── 边 ── */
.eline {
  fill: none;
  stroke: var(--gray-400);
  stroke-width: 1.4;
}

.ehit {
  fill: none;
  stroke: transparent;
  stroke-width: 14;
  pointer-events: stroke;
  cursor: pointer;
}

.edge-g .eline {
  marker-end: url(#onto-arrow);
}

.mk-fill {
  fill: var(--gray-400);
}

.mk-warn {
  fill: var(--warn);
}

.mk-hot {
  fill: var(--accent);
}

.edge-g.is-fanout .eline {
  stroke: var(--warn);
  stroke-width: 1.6;
  stroke-dasharray: 6 4;
  marker-end: url(#onto-arrow-warn);
}

.elab text {
  font-size: var(--onto-fs-label);
  font-weight: 500;
  fill: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.elab rect {
  fill: var(--surface-raised);
}

.edge-g.is-fanout .elab text {
  fill: var(--warn-text);
}

/* ── 强调态（hover / 选中） ── */
.node-g.is-dim {
  opacity: 0.22;
}

.edge-g.is-dim {
  opacity: 0.12;
}

.node-g.is-hot .ncard {
  stroke: var(--accent);
  stroke-width: 2.5;
  stroke-dasharray: none;
  fill: var(--accent-soft);
}

.node-g.is-nb .ncard {
  stroke: var(--accent-graphic);
  stroke-width: 2;
}

.node-g:focus-visible .ncard {
  stroke: var(--accent);
  stroke-width: 2.5;
  stroke-dasharray: none;
}

.edge-g.is-hot .eline {
  stroke: var(--accent);
  stroke-width: 1.8;
  stroke-dasharray: none;
  marker-end: url(#onto-arrow-hot);
}

.edge-g.is-hot .elab text {
  fill: var(--accent);
}

@media (prefers-reduced-motion: no-preference) {
  .node-g,
  .edge-g {
    transition: opacity var(--dur-fast) var(--ease);
  }
}
</style>
