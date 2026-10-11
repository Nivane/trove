/**
 * ontologyChart — 「本体」页的两个纯函数（前端方案 §3.3/§3.4）。
 *
 * 与 utils/chart.ts 的 buildWaterfallOption 同款纪律：option 由纯函数构造，
 * 单测不挂 ECharts 实例——「声明/未声明样式、fan_out 警示、标签回退、
 * 规模开关」全部变成可断言的 option 数据；ontologyEditHref 把深链 URL
 * 变成可断言的字符串。
 *
 * 图规格的确定性约定（§3.4）：
 *   · 声明对象：实心、主色（token --accent）、标签 = display_name；
 *   · 未声明对象：虚线边、低透明度、标签 = 数据集名（弱化为「表」节点）；
 *   · 边：标签 = display_name ?? 列连接回退文案，缀基数，箭头 from → to；
 *   · fan_out 边：警示色（token 派生）+ 虚线；
 *   · 布局：force + initLayout 'circular' + layoutAnimation:false ——
 *     同数据同位置（视觉验收可复现）；
 *   · 规模：节点数 > GRAPH_SCALE_LIMIT 出提示条（不聚合——聚合的图与
 *     声明的图不是同一张图，诚实降级优于视觉简化）。
 *
 * 配色全部取自 token（调用方传 ontologyGraphTokens()，明暗自动跟随），
 * 不新增色板。
 */
import type {
  ObjectGraphInfo,
  ObjectLinkInfo,
  ObjectNodeInfo,
} from '../api/types'

/** 图上的选中目标：对象按数据集名，链接按关系名（与 DTO 同键）。 */
export interface OntologySelection {
  kind: 'object' | 'link'
  ref: string
}

/** 图配色 token（页面从 CSS 变量读，测试显式给值 —— 明/暗两档可断言）。 */
export interface OntologyGraphTokens {
  /** 主色：已声明节点。 */
  accent: string
  /** 正文色：标签。 */
  text: string
  /** 次要色：边与 tooltip 细节。 */
  secondary: string
  /** 边框色：未声明节点与 tooltip 描边。 */
  border: string
  /** 警示色：fan_out 边。 */
  warn: string
  /** 置顶面：tooltip 背景。 */
  surface: string
}

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim()
  return v || fallback
}

/** 读当前设计 token（与 utils/chart.ts 的 chartTheme 同款，暗色自动跟随）。 */
export function ontologyGraphTokens(): OntologyGraphTokens {
  return {
    accent: cssVar('--accent', '#6366f1'),
    text: cssVar('--text-primary', '#18181b'),
    secondary: cssVar('--text-secondary', '#71717a'),
    border: cssVar('--border-subtle', '#e4e4e7'),
    warn: cssVar('--warn', '#f59e0b'),
    surface: cssVar('--surface-raised', '#ffffff'),
  }
}

/** 布局/文案之外的可选文案（i18n 由页面注入 —— 这里不写死任何一种语言）。 */
export interface OntologyGraphTexts {
  /** fan_out 边的 tooltip 解释行（ontologyFanoutWarn）。 */
  fanoutWarn?: string
}

/** 节点数超过此值出提示条（不聚合，只提示）。 */
export const GRAPH_SCALE_LIMIT = 60

/** 规模提示开关（§3.4 规模边界）。 */
export function graphScaleHintNeeded(objectCount: number): boolean {
  return objectCount > GRAPH_SCALE_LIMIT
}

/**
 * 形状守卫（§5）：响应体必须带 objects/links 两个数组 ——
 * 缺键 = 旧后端/代理形状 → 页面走错误态，绝不渲染假空图。
 * 零数据集是合法空态（objects: [] 通过守卫）。
 */
export function isObjectGraph(body: unknown): body is ObjectGraphInfo {
  if (!body || typeof body !== 'object') return false
  const b = body as { objects?: unknown; links?: unknown }
  return Array.isArray(b.objects) && Array.isArray(b.links)
}

/** 对象标签：display_name 优先，缺席回退数据集名。 */
export function objectLabel(obj: Pick<ObjectNodeInfo, 'dataset' | 'display_name'>): string {
  const name = (obj.display_name ?? '').trim()
  return name || obj.dataset
}

/**
 * M:N 归一化判定 —— 与编译器 `_is_many_to_many`
 * (trove/services/semantic_layer/compiler.py) 同口径：upper + 去
 * `_`/`-`/空白后落在拼写闭集。逐字 `=== 'M:N'` 会漏 `N:M`/`M2M` 等拼写，
 * 警示会静默消失（错方向：宁可多报风险，不可漏报）。
 */
function isManyToMany(cardinality: string | undefined): boolean {
  const c = (cardinality ?? '')
    .trim()
    .toUpperCase()
    .replace(/_/g, ' ')
    .replace(/-/g, ' ')
    .replace(/\s+/g, '')
  return ['MN', 'M:N', 'N:M', 'M2M', 'MANYTOMANY', 'MANY:MANY'].includes(c)
}

/**
 * fan_out 风险判据。投影 DTO 的契约是布尔（design §4.5）；
 * 再兜一层模型原值，逐条照编译器权威语义（compiler.py `_classify_edges`）：
 * M:N 边**唯一已实现的豁免是 `fan_out == 'dedup'`**；`bridge:<dataset>`
 * 豁免未实现 → 保守仍判有风险。故非 M:N → false；M:N 且非 dedup
 * （含空串与 bridge:*）→ true。两种表示都收敛到同一个布尔 ——
 * 表示法变化不会让警示静默消失。
 */
export function isFanout(
  link: Pick<ObjectLinkInfo, 'fan_out'> & { cardinality?: string },
): boolean {
  const v = link.fan_out as unknown
  if (typeof v === 'string') {
    return isManyToMany(link.cardinality) && v.trim().toLowerCase() !== 'dedup'
  }
  return v === true
}

/** 边标签的列连接回退文案（机器不猜：用声明的连接列逐字）。 */
function columnFallback(link: ObjectLinkInfo): string {
  const from = (link.from_columns ?? []).join(', ')
  const to = (link.to_columns ?? []).join(', ')
  if (from || to) return `${from} → ${to}`
  return `${link.from} → ${link.to}`
}

/** 边标签：display_name ?? 列连接回退；缀基数（1:N）。 */
export function linkLabel(link: ObjectLinkInfo): string {
  const base = (link.display_name ?? '').trim() || columnFallback(link)
  const cardinality = (link.cardinality ?? '').trim()
  return cardinality ? `${base} · ${cardinality}` : base
}

/**
 * 深链 URL（FR-GRAPH-4：页内无第二编辑面，编辑一律回语义台）。
 * 对象 → 数据集 tab + q=<dataset>；链接 → 关系 tab。
 */
export function ontologyEditHref(
  kind: 'object' | 'link',
  ref: string,
  ds: string,
): string {
  const base = '/admin/semantic'
  const params = new URLSearchParams()
  if (ds) params.set('ds', ds)
  if (kind === 'object') {
    params.set('tab', 'datasets')
    params.set('q', ref)
  } else {
    params.set('tab', 'relationships')
  }
  const query = params.toString()
  return query ? `${base}?${query}` : base
}

/**
 * ECharts graph option（纯函数）。节点/边都带 `onto` 摘要 ——
 * tooltip 与点击事件只认这一份摘要（组件不重新推导数据含义）。
 */
export function buildGraphOption(
  objects: ObjectNodeInfo[],
  links: ObjectLinkInfo[],
  tokens: OntologyGraphTokens,
  texts: OntologyGraphTexts = {},
): Record<string, unknown> {
  const nodes = objects.map((o) => {
    const declared = o.declared === true
    return {
      id: o.dataset,
      name: o.dataset,
      symbolSize: declared ? 34 : 22,
      label: {
        show: true,
        position: 'right',
        formatter: objectLabel(o),
        color: declared ? tokens.text : tokens.secondary,
        fontSize: 12,
      },
      itemStyle: declared
        ? {
            color: tokens.accent,
            borderColor: tokens.accent,
            borderWidth: 2,
            borderType: 'solid',
            opacity: 1,
          }
        : {
            color: tokens.surface,
            borderColor: tokens.secondary,
            borderWidth: 1.5,
            borderType: 'dashed',
            opacity: 0.55,
          },
      onto: {
        kind: 'object',
        ref: o.dataset,
        label: objectLabel(o),
        dataset: o.dataset,
      },
    }
  })

  const edges = links.map((l) => {
    const fanout = isFanout(l)
    return {
      source: l.from,
      target: l.to,
      name: l.name,
      label: {
        show: true,
        formatter: linkLabel(l),
        color: fanout ? tokens.warn : tokens.secondary,
        fontSize: 12,
      },
      lineStyle: fanout
        ? { color: tokens.warn, width: 1.6, type: 'dashed' }
        : { color: tokens.border, width: 1.4, type: 'solid' },
      onto: {
        kind: 'link',
        ref: l.name,
        label: linkLabel(l),
        fanout,
      },
    }
  })

  return {
    // 位置稳定：进场不播放布局动画（同数据同截图，视觉验收可复现）。
    animation: false,
    tooltip: {
      trigger: 'item',
      backgroundColor: tokens.surface,
      borderColor: tokens.border,
      borderWidth: 1,
      textStyle: { color: tokens.text, fontSize: 12 },
      formatter: (params: unknown) => {
        const p = (Array.isArray(params) ? params[0] : params) as
          | { data?: { onto?: Record<string, unknown> } }
          | undefined
        const onto = p?.data?.onto
        if (!onto) return ''
        const label = String(onto.label ?? '')
        if (onto.kind === 'object') return `${label}<br/>${String(onto.dataset ?? '')}`
        const lines = [label]
        if (onto.fanout === true && texts.fanoutWarn) lines.push(texts.fanoutWarn)
        return lines.join('<br/>')
      },
    },
    series: [
      {
        type: 'graph',
        layout: 'force',
        // force 的固定初值 + 关闭布局动画：每次进页位置稳定。
        force: {
          initLayout: 'circular',
          repulsion: 220,
          edgeLength: 120,
          gravity: 0.08,
        },
        layoutAnimation: false,
        roam: true,
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 8,
        emphasis: { focus: 'adjacency' },
        data: nodes,
        links: edges,
      },
    ],
  }
}
