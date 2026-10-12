/**
 * ontologyLayout — 「本体」页的确定性布局与投影派生（纯函数，零 DOM）。
 *
 * 图不进 ECharts：几何全部由纯函数算出来，测试直接断言返回值 ——
 * 「分层方向、回边不参与分层且不死循环、孤立对象垫底、标签车道夹取、
 * 声明名 vs 列连接回退、悬空链接不静默丢、同输入逐字段同输出」全部在这里
 * 钉死。确定性是这张图的卖点（位置 = 关系流向，同一份模型每次同一张图），
 * 所以「输入顺序不改变位置」是必须用测试守住的承诺。
 */
import { describe, it, expect } from 'vitest'
import {
  FONT,
  GAP_X,
  GRAPH_SCALE_LIMIT,
  LABEL_MAX_W,
  NODE_H,
  NODE_W,
  graphScaleHintNeeded,
  isFanout,
  isObjectGraph,
  layoutOntology,
  linkFallback,
  linkHasName,
  objectLabel,
  ontologyEditHref,
  textWidth,
  truncateWidth,
} from '../src/utils/ontologyLayout'
import type { ObjectLinkInfo, ObjectNodeInfo } from '../src/api/types'

function obj(dataset: string, extra: Partial<ObjectNodeInfo> = {}): ObjectNodeInfo {
  return { dataset, declared: true, ...extra }
}

function link(name: string, from: string, to: string, extra: Partial<ObjectLinkInfo> = {}): ObjectLinkInfo {
  return { name, from, to, ...extra }
}

/** 数据集 → 坐标（断言只看坐标，不看 nodes 的返回顺序）。 */
function positionsOf(objects: ObjectNodeInfo[], links: ObjectLinkInfo[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const n of layoutOntology(objects, links).nodes) out[n.dataset] = `${n.x},${n.y}`
  return out
}

const CHAIN: ObjectNodeInfo[] = [obj('loan'), obj('account'), obj('district')]
const CHAIN_LINKS: ObjectLinkInfo[] = [
  link('loan_to_account', 'loan', 'account'),
  link('account_to_district', 'account', 'district'),
]

describe('分层与位置（位置 = 关系流向）', () => {
  it('链式关系逐层右移：每层 x 差一个列宽，层内首行对齐', () => {
    const L = layoutOntology(CHAIN, CHAIN_LINKS)
    const at = (d: string) => L.nodes.find((n) => n.dataset === d)!
    expect(at('loan').x).toBe(0)
    expect(at('account').x).toBe(NODE_W + GAP_X)
    expect(at('district').x).toBe((NODE_W + GAP_X) * 2)
    // 单对象层垂直居中：bodyH = NODE_H，偏移 0。
    expect(at('loan').y).toBe(0)
    expect(L.height).toBe(NODE_H)
    expect(L.width).toBe((NODE_W + GAP_X) * 2 + NODE_W)
  })

  it('输入顺序不影响位置（同输入逐字段同输出；换返回顺序图不变）', () => {
    const forward = positionsOf(CHAIN, CHAIN_LINKS)
    const reversed = positionsOf([...CHAIN].reverse(), [...CHAIN_LINKS].reverse())
    expect(reversed).toEqual(forward)
    // 逐字段同输出：两次调用完全一致。
    expect(layoutOntology(CHAIN, CHAIN_LINKS)).toEqual(layoutOntology(CHAIN, CHAIN_LINKS))
  })

  it('最长路径分层：旁路节点不吃层号（a→b→c 与 a→c 并存时 c 仍在第 2 层）', () => {
    const objects = [obj('a'), obj('b'), obj('c')]
    const links = [link('ab', 'a', 'b'), link('bc', 'b', 'c'), link('ac', 'a', 'c')]
    const L = layoutOntology(objects, links)
    const x = (d: string) => L.nodes.find((n) => n.dataset === d)!.x
    expect(x('a')).toBeLessThan(x('b'))
    expect(x('b')).toBeLessThan(x('c'))
  })
})

describe('破环：互指/自环不死循环，回边走底部弧线', () => {
  it('a↔b 互指：两条边各画各的，回边 forward=false（不参与分层）', () => {
    const objects = [obj('a'), obj('b')]
    const links = [link('ab', 'a', 'b'), link('ba', 'b', 'a')]
    const L = layoutOntology(objects, links)
    expect(L.edges).toHaveLength(2)
    const ab = L.edges.find((e) => e.name === 'ab')!
    const ba = L.edges.find((e) => e.name === 'ba')!
    // 名字序 DFS：a 先访问 → a→b 是树边，b→a 是回边。
    expect(ab.forward).toBe(true)
    expect(ba.forward).toBe(false)
    const xa = L.nodes.find((n) => n.dataset === 'a')!.x
    const xb = L.nodes.find((n) => n.dataset === 'b')!.x
    expect(xb).toBeGreaterThan(xa)
  })

  it('自环不挂死，且按回边渲染（forward=false）', () => {
    const L = layoutOntology([obj('a')], [link('self', 'a', 'a')])
    expect(L.edges).toHaveLength(1)
    expect(L.edges[0].forward).toBe(false)
    expect(L.nodes[0].x).toBe(0)
  })
})

describe('孤立对象：不进分层，垫底单列一行（不隐藏 —— 它是覆盖率的分母）', () => {
  it('零链接：全部落在首行', () => {
    const L = layoutOntology([obj('a'), obj('b')], [])
    expect(L.nodes.map((n) => n.y)).toEqual([0, 0])
    expect(L.nodes.map((n) => n.x)).toEqual([0, NODE_W + GAP_X])
    expect(L.height).toBe(NODE_H)
  })

  it('有链接时孤立对象在主体下方另起一行', () => {
    const L = layoutOntology([obj('a'), obj('b'), obj('lone')], [link('ab', 'a', 'b')])
    const lone = L.nodes.find((n) => n.dataset === 'lone')!
    expect(lone.x).toBe(0)
    expect(lone.y).toBeGreaterThan(NODE_H)
    expect(L.height).toBe(lone.y + NODE_H)
  })
})

describe('边标签：声明了名字才铺名字，未声明只标基数', () => {
  it('声明名 + 基数 → 「名字 · 基数」', () => {
    const L = layoutOntology(
      [obj('a'), obj('b')],
      [link('r', 'a', 'b', { display_name: '发放', cardinality: '1:N' })],
    )
    expect(L.edges[0].label).toBe('发放 · 1:N')
    expect(L.edges[0].fanout).toBe(false)
  })

  it('未声明名字（投影回填列连接串）→ 只标基数，不铺连接串', () => {
    const L = layoutOntology(
      [obj('a'), obj('b')],
      [
        link('r', 'a', 'b', {
          display_name: 'a_id → b_id',
          from_columns: ['a_id'],
          to_columns: ['b_id'],
          cardinality: 'M:N',
        }),
      ],
    )
    expect(L.edges[0].label).toBe('M:N')
    expect(L.edges[0].fanout).toBe(true)
  })

  it('无名字且无基数 → 无标签（labelWidth=0，渲染层不画白底遮罩）', () => {
    const L = layoutOntology([obj('a'), obj('b')], [link('r', 'a', 'b')])
    expect(L.edges[0].label).toBe('')
    expect(L.edges[0].labelWidth).toBe(0)
  })

  it('标签按像素宽截断（LABEL_MAX_W 内），截断后仍带省略号', () => {
    const long = '很长的关系名字'.repeat(12)
    const L = layoutOntology(
      [obj('a'), obj('b')],
      [link('r', 'a', 'b', { display_name: long, cardinality: '1:N' })],
    )
    expect(L.edges[0].label.endsWith('…')).toBe(true)
    expect(textWidth(L.edges[0].label, FONT.label)).toBeLessThanOrEqual(LABEL_MAX_W + 1)
  })

  it('标签夹进层间车道：不压上任何一张卡片', () => {
    const wide = layoutOntology(
      [obj('a'), obj('b')],
      [link('r', 'a', 'b', { display_name: '很长的关系名字'.repeat(6), cardinality: 'M:N' })],
    )
    const e = wide.edges[0]
    const a = wide.nodes.find((n) => n.dataset === 'a')!
    const b = wide.nodes.find((n) => n.dataset === 'b')!
    // 车道放不下时退化为车道居中：仍在两张卡片之间。
    expect(e.labelX).toBeGreaterThan(a.x + NODE_W)
    expect(e.labelX).toBeLessThan(b.x)
  })
})

describe('linkHasName / linkFallback：回退串逐字识别，不猜', () => {
  it('与回退串逐字相同 = 未声明；差一个字 = 已声明', () => {
    const l = link('r', 'a', 'b', {
      display_name: 'a_id → b_id',
      from_columns: ['a_id'],
      to_columns: ['b_id'],
    })
    expect(linkFallback(l)).toBe('a_id → b_id')
    expect(linkHasName(l)).toBe(false)
    expect(linkHasName({ ...l, display_name: '发放' })).toBe(true)
    // 空 display_name 也是「没有名字」，不是「名字是空串」。
    expect(linkHasName({ ...l, display_name: '' })).toBe(false)
    expect(linkHasName({ ...l, display_name: undefined })).toBe(false)
  })

  it('无列信息时回退到数据集名（仍然逐字比对）', () => {
    const l = link('r', 'a', 'b', { display_name: 'a → b' })
    expect(linkFallback(l)).toBe('a → b')
    expect(linkHasName(l)).toBe(false)
  })
})

describe('悬空链接：不静默丢（KPI 计数来自 DTO，图上少画必须说得出来）', () => {
  it('指向未声明数据集的链接不进绘制，但按名字记录在案', () => {
    const L = layoutOntology([obj('a')], [link('ghost', 'a', 'nope'), link('selfish', 'a', 'a')])
    expect(L.dangling).toEqual(['ghost'])
    expect(L.edges.map((e) => e.name)).toEqual(['selfish'])
  })

  it('正常模型里恒为空', () => {
    expect(layoutOntology(CHAIN, CHAIN_LINKS).dangling).toEqual([])
  })
})

describe('fan_out 归一化（与编译器 _is_many_to_many 同口径）', () => {
  it('布尔直传；模型原值按 M:N 闭合拼写判，dedup 是唯一豁免', () => {
    expect(isFanout({ fan_out: true })).toBe(true)
    expect(isFanout({ fan_out: false })).toBe(false)
    expect(isFanout({ fan_out: '', cardinality: 'M:N' })).toBe(true)
    expect(isFanout({ fan_out: '', cardinality: 'N:M' })).toBe(true)
    expect(isFanout({ fan_out: '', cardinality: 'M2M' })).toBe(true)
    expect(isFanout({ fan_out: '', cardinality: '1:N' })).toBe(false)
    expect(isFanout({ fan_out: 'dedup', cardinality: 'M:N' })).toBe(false)
    // bridge:<dataset> 豁免未实现 → 保守仍判有风险（宁可多报）。
    expect(isFanout({ fan_out: 'bridge:account', cardinality: 'M:N' })).toBe(true)
  })
})

describe('形状守卫 / 规模开关 / 标签 / 深链', () => {
  it('缺 objects|links 键一律拒绝；空数组是合法空图', () => {
    expect(isObjectGraph({ objects: [], links: [] })).toBe(true)
    expect(isObjectGraph({ objects: [obj('a')], links: [] })).toBe(true)
    expect(isObjectGraph({ objects: [] })).toBe(false)
    expect(isObjectGraph({ links: [] })).toBe(false)
    expect(isObjectGraph({ objects: {}, links: [] })).toBe(false)
    expect(isObjectGraph(null)).toBe(false)
    expect(isObjectGraph('nope')).toBe(false)
  })

  it('节点数 > 60 才提示（不聚合）', () => {
    expect(GRAPH_SCALE_LIMIT).toBe(60)
    expect(graphScaleHintNeeded(60)).toBe(false)
    expect(graphScaleHintNeeded(61)).toBe(true)
  })

  it('objectLabel：显示名（去空白）优先，缺席回退数据集名', () => {
    expect(objectLabel({ dataset: 'loan', display_name: '  贷款 ' })).toBe('贷款')
    expect(objectLabel({ dataset: 'loan', display_name: '' })).toBe('loan')
    expect(objectLabel({ dataset: 'loan', display_name: null })).toBe('loan')
  })

  it('深链：对象带 tab=datasets + q；链接进 tab=relationships；ds 缺席不编造参数', () => {
    expect(ontologyEditHref('object', 'loan', 'demo')).toBe(
      '/admin/semantic?ds=demo&tab=datasets&q=loan',
    )
    expect(ontologyEditHref('link', 'loan_to_account', 'demo')).toBe(
      '/admin/semantic?ds=demo&tab=relationships',
    )
    expect(ontologyEditHref('object', 'loan', '')).toBe('/admin/semantic?tab=datasets&q=loan')
  })
})

describe('排版近似（宽度算得准，截断才不切半个字）', () => {
  it('CJK 按 1.02em、其余按 0.56em 估宽', () => {
    expect(textWidth('贷款', 12)).toBeCloseTo(24.48, 5)
    expect(textWidth('abcd', 10)).toBeCloseTo(22.4, 5)
    expect(textWidth('', 12)).toBe(0)
  })

  it('放得下就原样返回；放不下按字符截断并缀省略号（不切代理对）', () => {
    expect(truncateWidth('贷款', 100, 12)).toBe('贷款')
    expect(truncateWidth('abcdefghij', 20, 12)).toBe('a…')
    // 代理对（emoji）整字截断：不产生半个字符。
    const cut = truncateWidth('😀😀😀😀', 14, 12)
    expect([...cut].every((ch) => ch === '😀' || ch === '…')).toBe(true)
  })
})

describe('卡片排版预算（字号走 CSS 变量，字号下限门看不见这里）', () => {
  /** 文字左起 52（身份砖 12+30 + 10 沟槽），右留白 10。 */
  const TEXT_W = NODE_W - 52 - 10

  it('三行字号都不低于仓库下限 12px', () => {
    // scripts/check-font-size.mjs 只扫字面量 px；这里的字号经 --onto-fs-* 变量
    // 落到 CSS，绕过了那道门 —— 所以下限必须在这一侧也钉住。
    for (const [line, size] of Object.entries(FONT)) {
      expect(size, line).toBeGreaterThanOrEqual(12)
    }
  })

  it('计数行（有界内容，不截断）在最坏行文下也放得进卡片', () => {
    // 计数行 = 两个小整数 + 两个标签，是中英文都最短的形态；它必须整行放得下，
    // 否则就只剩截断一条路（把「8 字段」骗成省略号）。中英各取一条最长形态。
    expect(textWidth('2 Metrics · 8 Fields', FONT.meta)).toBeLessThanOrEqual(TEXT_W)
    expect(textWidth('2 度量 · 8 字段', FONT.meta)).toBeLessThanOrEqual(TEXT_W)
  })

  it('名字行 / 键行的截断上限不超过可用宽度（估宽偏保守也不越界）', () => {
    // 组件里的 CAP_* 是常量，这里用「截断后宽度」反推：上限即截断阈值。
    const capped = truncateWidth('x'.repeat(200), 124, FONT.name)
    expect(textWidth(capped, FONT.name)).toBeLessThanOrEqual(TEXT_W)
    const cappedKey = truncateWidth('x'.repeat(200), 124, FONT.key)
    expect(textWidth(cappedKey, FONT.key)).toBeLessThanOrEqual(TEXT_W)
  })
})
