/**
 * ontologyLayout — 「本体」页的确定性布局与投影派生（纯函数，零副作用）。
 *
 * 与 utils/chart.ts 的 buildWaterfallOption 同款纪律：图的几何全部由纯函数
 * 算出来，组件只负责把结果画进 SVG ——「分层正确、回边不参与分层、标签夹进
 * 层间车道、同输入逐字段同输出」全部变成可断言的返回值，不挂 DOM。
 *
 * 布局为什么不用力导向：力导向的位置**没有语义**（同数据两次进页可以长得
 * 不一样，读图的人记不住形状），且节点一动整张图跟着飘。这里改成
 * **确定性分层**：x = 关系流向（左 → 右），y = 层内重心序。同一份语义模型
 * 在任何机器、任何一次渲染里都是同一张图。
 *
 * 算法（四步，全确定性）：
 *   1) 按名字序 DFS 三色标记破环 —— 回边不参与分层（M:N 互指不会死循环）；
 *   2) Kahn 最长路径分层 —— 每层编号 = 它在所有前驱之后；
 *   3) 层内重心法排序（固定 2 轮往返，名字兜底）—— 减少连线交叉；
 *   4) 固定尺寸网格 —— 卡片尺寸恒定，坐标 = 层号 × 列宽 / 层内序 × 行高。
 * 孤立对象（无任何链接）不进分层，在底部单列一行 —— 它们属于图的一部分
 * （声明覆盖率的分母），藏起来就成了「本体不存在」的假象。
 *
 * 边标签只在**声明了名字**时显示（`linkHasName`：投影总是回填列连接串，
 * 逐字比对认出来）——未声明的关系只标基数，列连接串去侧栏看：把
 * 「account_id → account_id」铺在层间车道上，除了互相遮挡没有任何信息量。
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

/** 节点数超过此值出提示条（不聚合 —— 聚合的图与声明的图不是同一张图）。 */
export const GRAPH_SCALE_LIMIT = 60

/** 规模提示开关。 */
export function graphScaleHintNeeded(objectCount: number): boolean {
  return objectCount > GRAPH_SCALE_LIMIT
}

/**
 * 形状守卫：响应体必须带 objects/links 两个数组 ——
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
 * fan_out 风险判据。投影 DTO 的契约是布尔（design §4.5）：布尔在场就是
 * 后端已经判完的结论，直接用（不信布尔 = 凭空造第二套判据）。
 *
 * 布尔缺席（模型原值直传的字符串 / 键被代理抹掉）时按后端同一个函数重算
 * （object_graph._link_fan_out ⇄ compiler._classify_edges）：**非 M:N →
 * false；M:N 且豁免串不是 `dedup` → true**。注意 `or ""` 那一层：
 * 后端把「缺席」与「空串」一视同仁地当成「无豁免」，所以 M:N + fan_out
 * 缺席 = **有风险**，不是「没声明就没风险」——错方向是宁可多报，
 * 漏报会让警示静默消失（那是这张图存在的理由之一）。
 */
export function isFanout(
  link: Pick<ObjectLinkInfo, 'fan_out'> & { cardinality?: string },
): boolean {
  const v = link.fan_out as unknown
  if (typeof v === 'boolean') return v
  const raw = v == null ? '' : String(v)
  return isManyToMany(link.cardinality) && raw.trim().toLowerCase() !== 'dedup'
}

/** 边的列连接回退文案（机器不猜：用声明的连接列逐字）。 */
export function linkFallback(
  link: Pick<ObjectLinkInfo, 'from' | 'to' | 'from_columns' | 'to_columns'>,
): string {
  const from = (link.from_columns ?? []).join(', ')
  const to = (link.to_columns ?? []).join(', ')
  if (from || to) return `${from} → ${to}`
  return `${link.from} → ${link.to}`
}

/**
 * 关系是否声明了自己的名字。
 *
 * 投影 DTO 的 `display_name` **总是**有值（后端 object_graph._link_display_name
 * 在 relationship.display_name 缺省时回填列连接串），所以「有没有声明名字」
 * 不能靠缺键判断 —— 这里把回退串逐字重算一遍再比对：相等 = 它是回退，
 * 不是声明。不猜、不模糊匹配；一个字面就叫「a_id → a_id」的声明会被
 * 当成回退，而那种情况下的渲染串本来就与该回退逐字相同。
 */
export function linkHasName(link: ObjectLinkInfo): boolean {
  const name = (link.display_name ?? '').trim()
  return !!name && name !== linkFallback(link)
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

/* ── 排版近似（SVG 没有 CSS 省略号，宽度要自己算） ─────────────────── */

/**
 * 文字宽度近似值：CJK 按 1.02em、其余按 0.56em。
 * 近似即可 —— 用途只有两处（截断到卡片宽度、把边标签夹进层间车道），
 * 两者都留了余量。
 */
export function textWidth(s: string, size: number): number {
  let w = 0
  for (const ch of String(s)) w += (ch.codePointAt(0) ?? 0) > 0x2e7f ? size * 1.02 : size * 0.56
  return w
}

/** 按像素宽度截断，放不下时缀省略号（逐字符长，绝不切半个代理对）。 */
export function truncateWidth(s: string, maxPx: number, size: number): string {
  const str = String(s)
  if (textWidth(str, size) <= maxPx) return str
  let out = ''
  for (const ch of str) {
    if (textWidth(out + ch + '…', size) > maxPx) break
    out += ch
  }
  return out + '…'
}

/* ── 布局几何常量（组件 CSS 里的字号与之同源） ─────────────────────── */

/**
 * 卡片尺寸 / 间距 / 画布内边距（px，SVG 用户坐标）。
 *
 * NODE_W 是 200 而不是更窄：卡片三行字的字号受仓库字号下限约束
 * （scripts/check-font-size.mjs，12px 起），中文最密的「度量 / 字段」那行
 * 在 12px 下需要 ~138px 才不被截断，200 − 文字左起 52 − 右留白 10 = 138。
 * 卡片再窄就只能靠截断凑，而把一行计数截成省略号是假的克制。
 */
export const NODE_W = 200
export const NODE_H = 76
export const GAP_X = 84
export const GAP_Y = 26
export const PAD = 26

/** 卡片文字各行的字号（宽度估算与 CSS 必须一致，≥12px 是仓库下限）。 */
export const FONT = {
  name: 15,
  key: 12,
  meta: 12,
  label: 12,
} as const

/** 边标签的最大宽度（px）——超出即截断，绝不压到邻层卡片上。 */
export const LABEL_MAX_W = 150

/** 布局后的对象（投影字段 + 网格坐标）。 */
export interface OntologyNode extends ObjectNodeInfo {
  x: number
  y: number
}

/** 布局后的链接（投影字段 + 路径与标签几何）。 */
export interface OntologyEdge extends ObjectLinkInfo {
  /** 在原 links 数组里的下标（稳定身份，选中/悬停只认它）。 */
  key: number
  /** 三次贝塞尔路径（箭头终点已让出箭头长度）。 */
  path: string
  /** 标签文案：声明了名字 → 「名字 · 基数」；未声明 → 只标基数。 */
  label: string
  labelX: number
  labelY: number
  /** 标签白底遮罩宽度（= 文字宽 + 内边距）。 */
  labelWidth: number
  /** 左 → 右的正向边（false = 回边/同层边，走底部弧线）。 */
  forward: boolean
  /** 扇出风险（与侧栏同一个判据）。 */
  fanout: boolean
}

/** 布局结果：节点、边、画布尺寸，以及被丢弃的悬空链接名。 */
export interface OntologyLayout {
  nodes: OntologyNode[]
  edges: OntologyEdge[]
  /** 画布宽高（不含 PAD —— 由渲染方按需外包）。 */
  width: number
  height: number
  /**
   * 指向未声明数据集的链接名（正常模型里恒为空）。**不静默丢**：
   * KPI 的「链接」计数来自 DTO，图上少画一条必须说得出来。
   */
  dangling: string[]
}

/**
 * 语义模型投影 → 确定性分层布局（纯函数，同输入逐字段同输出）。
 *
 * 输出顺序：`nodes` = 输入顺序（声明顺序），`edges` = 输入顺序 ——
 * 位置由结构决定，不由输入顺序决定（换数据库返回顺序，图不变）。
 */
export function layoutOntology(
  objects: ObjectNodeInfo[],
  links: ObjectLinkInfo[],
): OntologyLayout {
  const ids = objects.map((o) => o.dataset).sort()
  const idset = new Set(ids)

  const dangling: string[] = []
  const edges: { link: ObjectLinkInfo; key: number }[] = []
  links.forEach((link, i) => {
    if (idset.has(link.from) && idset.has(link.to)) edges.push({ link, key: i })
    else dangling.push(link.name)
  })

  // 1) DFS 三色标记破环（按名字序 = 确定性；回边不参与分层）。
  const adj = new Map<string, { to: string; key: number }[]>(ids.map((id) => [id, []]))
  for (const e of edges) adj.get(e.link.from)?.push({ to: e.link.to, key: e.key })
  const color = new Map<string, number>(ids.map((id) => [id, 0]))
  const back = new Set<number>()
  const visit = (id: string) => {
    color.set(id, 1)
    for (const e of adj.get(id) ?? []) {
      const c = color.get(e.to)
      if (c === 0) visit(e.to)
      else if (c === 1) back.add(e.key) // 回边（含自环）
    }
    color.set(id, 2)
  }
  for (const id of ids) if (color.get(id) === 0) visit(id)

  // 2) Kahn 最长路径分层。
  const dag = edges.filter((e) => !back.has(e.key))
  const indeg = new Map<string, number>(ids.map((id) => [id, 0]))
  const out = new Map<string, string[]>(ids.map((id) => [id, []]))
  for (const e of dag) {
    indeg.set(e.link.to, (indeg.get(e.link.to) ?? 0) + 1)
    out.get(e.link.from)?.push(e.link.to)
  }
  const layer = new Map<string, number>(ids.map((id) => [id, 0]))
  const queue = ids.filter((id) => indeg.get(id) === 0)
  while (queue.length) {
    const u = queue.shift() as string
    for (const v of out.get(u) ?? []) {
      layer.set(v, Math.max(layer.get(v) ?? 0, (layer.get(u) ?? 0) + 1))
      indeg.set(v, (indeg.get(v) ?? 0) - 1)
      if (indeg.get(v) === 0) queue.push(v)
    }
  }

  const touched = new Set<string>()
  for (const e of edges) {
    touched.add(e.link.from)
    touched.add(e.link.to)
  }
  const linked = ids.filter((id) => touched.has(id))
  const isolated = ids.filter((id) => !touched.has(id))
  const maxLayer = linked.length ? Math.max(...linked.map((id) => layer.get(id) ?? 0)) : -1
  const layers: string[][] = []
  for (let i = 0; i <= maxLayer; i++) layers.push(linked.filter((id) => layer.get(id) === i))

  // 3) 层内重心法排序（2 轮往返；重心相同按名字，绝不依赖输入顺序）。
  const pos = new Map<string, number>()
  const indexLayers = () => layers.forEach((ls) => ls.forEach((id, i) => pos.set(id, i)))
  const preds = new Map<string, string[]>(ids.map((id) => [id, []]))
  const succs = new Map<string, string[]>(ids.map((id) => [id, []]))
  for (const e of edges) {
    preds.get(e.link.to)?.push(e.link.from)
    succs.get(e.link.from)?.push(e.link.to)
  }
  const barycenter = (ls: string[], map: Map<string, string[]>): string[] =>
    ls
      .map((id, i) => {
        const ns = (map.get(id) ?? [])
          .map((n) => pos.get(n))
          .filter((v): v is number => v !== undefined)
        return { id, k: ns.length ? ns.reduce((a, b) => a + b, 0) / ns.length : i }
      })
      .sort((a, b) => a.k - b.k || (a.id < b.id ? -1 : 1))
      .map((x) => x.id)
  indexLayers()
  for (let p = 0; p < 2; p++) {
    for (let i = 1; i < layers.length; i++) {
      indexLayers()
      layers[i] = barycenter(layers[i], preds)
    }
    for (let i = layers.length - 2; i >= 0; i--) {
      indexLayers()
      layers[i] = barycenter(layers[i], succs)
    }
  }
  indexLayers()

  // 4) 固定网格坐标：层内垂直居中，孤立对象底部单列一行。
  const rowH = NODE_H + GAP_Y
  const rows = Math.max(1, ...layers.map((l) => l.length))
  const bodyH = rows * rowH - GAP_Y
  const positions = new Map<string, { x: number; y: number }>()
  layers.forEach((ls, li) => {
    const off = (bodyH - (ls.length * rowH - GAP_Y)) / 2
    ls.forEach((id, ri) => positions.set(id, { x: li * (NODE_W + GAP_X), y: off + ri * rowH }))
  })
  const isolatedY = linked.length ? bodyH + GAP_Y * 1.6 : 0
  isolated.forEach((id, ri) => positions.set(id, { x: ri * (NODE_W + GAP_X), y: isolatedY }))

  const layerW = layers.length ? layers.length * (NODE_W + GAP_X) - GAP_X : 0
  const isolatedW = isolated.length ? isolated.length * (NODE_W + GAP_X) - GAP_X : 0
  const width = Math.max(NODE_W, layerW, isolatedW)
  const height = isolated.length ? isolatedY + NODE_H : bodyH

  // 边：正向边左出右进；回边/同层边走底部弧线（不穿过卡片阵列）。
  const layoutEdges: OntologyEdge[] = edges.map((e) => {
    const a = positions.get(e.link.from) as { x: number; y: number }
    const b = positions.get(e.link.to) as { x: number; y: number }
    const forward = b.x > a.x
    let path: string
    let labelX: number
    let labelY: number
    if (forward) {
      const x1 = a.x + NODE_W
      const y1 = a.y + NODE_H / 2
      const x2 = b.x - 7 // 让出箭头长度
      const y2 = b.y + NODE_H / 2
      const dx = Math.max(38, (x2 - x1) * 0.5)
      path = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`
      labelX = (x1 + x2) / 2
      labelY = (y1 + y2) / 2
    } else {
      const x1 = a.x + NODE_W / 2
      const y1 = a.y + NODE_H
      const x2 = b.x + NODE_W / 2
      const y2 = b.y + NODE_H
      const bow = 46 + Math.abs(x2 - x1) * 0.12
      path = `M ${x1} ${y1} C ${x1} ${y1 + bow}, ${x2} ${y2 + bow}, ${x2} ${y2}`
      labelX = (x1 + x2) / 2
      labelY = (y1 + y2) / 2 + bow * 0.75
    }
    const cardinality = (e.link.cardinality ?? '').trim()
    const raw = linkHasName(e.link)
      ? `${(e.link.display_name ?? '').trim()}${cardinality ? ` · ${cardinality}` : ''}`
      : cardinality
    const label = truncateWidth(raw, LABEL_MAX_W, FONT.label)
    const labelWidth = label ? textWidth(label, FONT.label) + 12 : 0
    return {
      ...e.link,
      key: e.key,
      path,
      label,
      labelX,
      labelY,
      labelWidth,
      forward,
      fanout: isFanout(e.link),
    }
  })

  // 标签横向夹进「层间车道」：不与任何一张卡片重叠（宽过车道时退化为车道居中）。
  for (const e of layoutEdges) {
    const a = positions.get(e.from) as { x: number; y: number }
    const b = positions.get(e.to) as { x: number; y: number }
    const half = e.labelWidth / 2
    if (e.forward) {
      const lo = a.x + NODE_W + 6 + half
      const hi = b.x - 6 - half
      e.labelX = lo <= hi ? Math.min(Math.max(e.labelX, lo), hi) : (a.x + NODE_W + b.x) / 2
    } else {
      const left = Math.min(a.x, b.x)
      const right = Math.max(a.x, b.x)
      const lo = left + 10 + half
      const hi = right + NODE_W - 10 - half
      e.labelX = lo <= hi ? Math.min(Math.max(e.labelX, lo), hi) : (left + right + NODE_W) / 2
    }
  }

  return {
    nodes: objects.map((o) => ({ ...o, ...(positions.get(o.dataset) ?? { x: 0, y: 0 }) })),
    edges: layoutEdges,
    width,
    height,
    dangling,
  }
}
