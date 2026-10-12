/**
 * ontologyIdentity — 对象类型身份（物化层）的确定性派生（纯函数，零副作用）。
 *
 * 「本体」页上的每个对象要能一眼认出**它是什么**，而不是一根写着表名的
 * 灰柱子：声明过 entity 的对象获得自己的图标与身份色 —— **声明即物化**，
 * 没声明的保持灰色「表」图标（缺口可见，正是去声明的驱动力）。
 *
 * 派生是确定性的，因为它必须可复现：
 *   1) 小词表按对象名 / 显示名 / 别名匹配业务概念（中英关键词），命中即用；
 *   2) 未命中按 **数据集名的哈希**落进固定调色板 + 通用立方体图标 ——
 *      同一份语义模型在任何进程、任何一次渲染里都是同一个身份；
 *   3) 未声明 → 灰色「表」（不参与词表，免得半声明的对象看起来已完成）。
 *
 * 将来 entity 声明可以直接给 icon/color 时，这里改成**声明优先、派生兜底**
 * （DTO 加两个可选字段即可）—— 那是另一次改版，不在这里预埋半套。
 */
import type { ObjectNodeInfo } from '../api/types'

/** 图标名（闭集：词表与哈希兜底只能从这里取）。 */
export type IconName =
  | 'bank'
  | 'person'
  | 'pin'
  | 'money'
  | 'receipt'
  | 'swap'
  | 'card'
  | 'cube'
  | 'table'

/** 图元（30×30 视野，笔画属性由渲染方统一给：见 GLYPH_STROKE_WIDTH）。 */
export interface Glyph {
  /** 路径 d 串。 */
  paths?: string[]
  /** [cx, cy, r]。 */
  circles?: [number, number, number][]
  /** [x, y, width, height, rx]。 */
  rects?: [number, number, number, number, number][]
}

/** 图标笔画（图元本身不含样式：同一份几何在卡片、侧栏、图例里复用）。 */
export const GLYPH_STROKE_WIDTH = 1.7

export const GLYPHS: Record<IconName, Glyph> = {
  bank: {
    paths: [
      'M3.5 9.5 L12 3.5 L20.5 9.5',
      'M5.5 9.5 V19.5',
      'M10 9.5 V19.5',
      'M14 9.5 V19.5',
      'M18.5 9.5 V19.5',
      'M3 19.5 H21',
    ],
  },
  person: {
    circles: [[12, 8, 3.6]],
    paths: ['M4.5 20.5 c0 -4.1 3.4 -6.5 7.5 -6.5 s7.5 2.4 7.5 6.5'],
  },
  pin: {
    paths: ['M12 21.5 C12 21.5 19 15 19 10.5 A7 7 0 1 0 5 10.5 C5 15 12 21.5 12 21.5 Z'],
    circles: [[12, 10.5, 2.6]],
  },
  money: {
    rects: [[2.5, 6.5, 19, 11, 2]],
    circles: [[12, 12, 2.6]],
    paths: ['M6 9.5 v5', 'M18 9.5 v5'],
  },
  receipt: {
    paths: [
      'M6.5 3.5 H17.5 V20.5 L15.2 18.9 L12.9 20.5 L10.6 18.9 L8.3 20.5 L6.5 19.1 Z',
      'M9.5 8 h5',
      'M9.5 11.5 h5',
    ],
  },
  swap: {
    paths: ['M4 8.5 h12', 'M13 5.5 l3 3 -3 3', 'M20 15.5 h-12', 'M11 12.5 l-3 3 3 3'],
  },
  card: {
    rects: [[2.5, 5.5, 19, 13, 2]],
    paths: ['M2.5 10 h19'],
  },
  cube: {
    paths: ['M12 4 L20 8.5 V15.5 L12 20 L4 15.5 V8.5 Z', 'M12 4 V12', 'M12 12 L20 8.5', 'M12 12 L4 8.5'],
  },
  table: {
    rects: [[3.5, 4.5, 17, 15, 2]],
    paths: ['M3.5 10 h17', 'M10 10 v9.5'],
  },
}

/**
 * 业务概念词表（顺序 = 优先级，先命中先用）。
 * 关键词同时收中英文：KB 内容语言跟随提问语言，两边都要认得出。
 */
const ICON_LEXICON: { re: RegExp; icon: IconName; color: string }[] = [
  { re: /account|账户/, icon: 'bank', color: '#4f46e5' },
  { re: /client|customer|客户|顾客/, icon: 'person', color: '#0d9488' },
  { re: /district|region|city|地区|区域/, icon: 'pin', color: '#0284c7' },
  { re: /loan|贷款/, icon: 'money', color: '#7c3aed' },
  { re: /order|指令|委托/, icon: 'receipt', color: '#a16207' },
  { re: /trans|交易|流水/, icon: 'swap', color: '#16a34a' },
  { re: /card|卡片/, icon: 'card', color: '#db2777' },
]

/**
 * 身份调色板（哈希兜底用）—— **分类色**，刻意不是状态色：
 * 身份色说「这是哪一类对象」，不说「它好不好」；ok/warn/danger 三族留给
 * 状态语义，别在这里借用（一个绿色的对象不该读起来像「健康」）。
 * 8 色对白底均 ≥3.3:1（图形元素 3:1 档），13% 底色上同样清晰。
 */
export const IDENTITY_PALETTE = [
  '#4f46e5', // indigo（同 --accent）
  '#0d9488', // teal
  '#0284c7', // sky
  '#7c3aed', // violet
  '#db2777', // pink
  '#16a34a', // green
  '#a16207', // gold
  '#64748b', // slate
]

/** 未声明对象的中性灰（同 --border-strong）。 */
export const UNDECLARED_COLOR = '#a1a1aa'

export interface ObjectIdentity {
  /** 是否已声明（声明 = 有 entity：显示名/自然键/别名）。 */
  declared: boolean
  icon: IconName
  /** 身份色；未声明恒为中性灰。 */
  color: string
}

/** djb2 变体：稳定、无符号、跨进程一致（绝不用 Math.random 或旧 hash）。 */
export function hashName(s: string): number {
  let h = 5381
  for (const ch of String(s)) h = ((h * 33) ^ (ch.codePointAt(0) ?? 0)) >>> 0
  return h
}

/** 对象 → 类型身份（未声明 → 灰色「表」；声明 → 词表命中或哈希兜底）。 */
export function objectIdentity(
  obj:
    | Pick<ObjectNodeInfo, 'dataset' | 'display_name' | 'aliases' | 'declared'>
    | null
    | undefined,
): ObjectIdentity {
  if (!obj || obj.declared !== true) {
    return { declared: false, icon: 'table', color: UNDECLARED_COLOR }
  }
  const hay = [obj.dataset, obj.display_name ?? '', ...(obj.aliases ?? [])]
    .join(' ')
    .toLowerCase()
  for (const entry of ICON_LEXICON) {
    if (entry.re.test(hay)) return { declared: true, icon: entry.icon, color: entry.color }
  }
  return {
    declared: true,
    icon: 'cube',
    color: IDENTITY_PALETTE[hashName(obj.dataset) % IDENTITY_PALETTE.length],
  }
}

/** 身份砖的底色透明度：已声明是淡身份色，未声明是更实的中性灰。 */
export function tileFillOpacity(identity: ObjectIdentity): number {
  return identity.declared ? 0.13 : 0.35
}
