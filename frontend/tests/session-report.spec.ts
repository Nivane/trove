import { describe, it, expect } from 'vitest'
import {
  buildSessionHtml,
  sessionReportFilename,
} from '../src/utils/session-report'
import type { SessionReportLabels } from '../src/utils/session-report'
import type { AnalysisPayload, ChartSpec } from '../src/api/types'
import type { Turn } from '../src/stores/chat'

const LABELS: SessionReportLabels = {
  generatedAt: '导出时间',
  rounds: '轮次',
  results: '结果',
  rows: '行',
  cols: '列',
  chart: '图表',
  analysis: {
    title: '分析证据',
    evidence: '证据',
    partial: '部分结果',
    partialHint: '部分组件未取到值',
    truncated: '已截断',
    purposes: {
      overall: '总体',
      probe: '维度探测',
      drilldown: '下钻',
      driver_tree: '驱动分解',
    },
  },
}

const NOW = new Date('2026-10-03T14:05:00')

function turn(partial: Partial<Turn>): Turn {
  return {
    question: '',
    thoughts: [],
    steps: [],
    answer: '',
    summary: null,
    status: 'done',
    ...partial,
  }
}

const BAR = {
  type: 'bar',
  title: '各城市贷款金额',
  categories: ['Prague', 'Brno'],
  series: [{ name: '金额', data: [100, 50] }],
} as unknown as ChartSpec

const WATERFALL = {
  type: 'waterfall',
  title: '贷款金额归因',
  categories: ['基期', 'Prague', 'Brno', '当前'],
  series: [{ name: '金额', data: [1000, 200, -150, 1050] }],
} as unknown as ChartSpec

/** output 节点会嵌进答案的 ASCII 图表块(与 spark.py 同形)。 */
function asciiBlock(title: string, body: string): string {
  return `**图表**: ${title}\n\`\`\`\n${body}\n\`\`\``
}

const ANALYSIS = {
  version: 1,
  partial: false,
  charts: [WATERFALL],
  evidence: {
    queries: [
      {
        purpose: 'driver_tree',
        sql: 'SELECT district, SUM(amount) FROM loan GROUP BY 1',
        period: '2024',
      },
    ],
  },
} as unknown as AnalysisPayload

describe('buildSessionHtml — 自包含 HTML 报告', () => {
  it('文档骨架:doctype/charset/标题转义/元信息', () => {
    const html = buildSessionHtml(
      [turn({ question: '哪个地区最高?', answer: 'Prague。' })],
      {
        title: '季度 <分析>',
        sessionId: 'abcdef12-3456',
        labels: LABELS,
        lang: 'zh',
        now: NOW,
      },
    )
    expect(html.startsWith('<!doctype html>')).toBe(true)
    expect(html).toContain('<meta charset="utf-8">')
    expect(html).toContain('<html lang="zh-CN">')
    expect(html).toContain('<title>季度 &lt;分析&gt;</title>')
    expect(html).toContain('导出时间: 2026/10/03 14:05')
    expect(html).toContain('轮次: 1')
    expect(html).toContain('<span class="idx">1</span>哪个地区最高?')
  })

  it('english lang 走 en;整份文档零外部资源、零脚本', () => {
    const html = buildSessionHtml([turn({ question: 'q', answer: 'a' })], {
      sessionId: 's',
      labels: LABELS,
      lang: 'en',
      now: NOW,
    })
    expect(html).toContain('<html lang="en">')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('<link')
    expect(html).not.toContain('@import')
    expect(html).not.toMatch(/src="https?:/)
  })

  it('答案走 renderMarkdown;原始 HTML 不落地(html:false + 净化)', () => {
    const html = buildSessionHtml(
      [
        turn({
          question: 'q',
          answer: '<img src=x onerror=alert(1)>\n\n**粗体**',
        }),
      ],
      { sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(html).not.toContain('<img')
    expect(html).toContain('<strong>粗体</strong>')
  })

  it('主图 ASCII 块被内联 SVG 顶替(figcaption 用图题)', () => {
    const answer = `总额如下。\n\n${asciiBlock('各城市贷款金额', 'Prague ███ 100\nBrno █ 50')}\n`
    const html = buildSessionHtml(
      [
        turn({
          question: 'q',
          answer,
          summary: { chart: BAR } as Turn['summary'],
        }),
      ],
      { sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(html).toContain('<svg')
    expect(html).toContain('role="img"')
    expect(html).toContain('各城市贷款金额')
    expect(html).not.toContain('Prague ███ 100')
    expect(html).not.toContain('**图表**')
  })

  it('不支持的图形(pie)不画 SVG,ASCII 块原样保留', () => {
    const answer = `看图。\n\n${asciiBlock('饼图', 'x')}\n`
    const html = buildSessionHtml(
      [
        turn({
          question: 'q',
          answer,
          summary: { chart: { type: 'pie', title: '饼图' } } as unknown as Turn['summary'],
        }),
      ],
      { sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(html).not.toContain('<svg')
    expect(html).toContain('<strong>图表</strong>') // markdown 渲染后的块头仍在
    expect(html).toContain('饼图')
  })

  it('归因瀑布 → SVG(涨绿跌红、Δ 值带符号)+ 证据 details', () => {
    // 与 output 节点同形:主图块在前、归因瀑布块在后
    const answer =
      `总额与归因如下。\n\n${asciiBlock('各城市贷款金额', 'Prague ███ 100\nBrno █ 50')}\n\n` +
      `${asciiBlock('贷款金额归因', '基期 1000\nPrague +200\nBrno -150\n当前 1050')}\n`
    const html = buildSessionHtml(
      [
        turn({
          question: 'q',
          answer,
          summary: { chart: BAR, analysis: ANALYSIS } as Turn['summary'],
        }),
      ],
      { sessionId: 's', labels: LABELS, now: NOW },
    )
    // 主图 + 瀑布 = 2 张 SVG(瀑布被替换进答案位置,分析块不重复)
    expect(html.match(/<svg/g)).toHaveLength(2)
    expect(html).not.toContain('基期 1000') // ASCII 块已被吃掉
    expect(html).toContain('#15803d') // 涨绿
    expect(html).toContain('#dc2626') // 跌红
    expect(html).toContain('+200')
    expect(html).toContain('-150')
    expect(html).toContain('分析证据')
    expect(html).toContain('证据 (1)')
    expect(html).toContain('1. 驱动分解 · 2024')
    expect(html).toContain('SELECT district, SUM(amount) FROM loan GROUP BY 1')
  })

  it('partial 轮次显示降级标注', () => {
    const html = buildSessionHtml(
      [
        turn({
          question: 'q',
          answer: 'a',
          summary: {
            analysis: { ...ANALYSIS, partial: true },
          } as Turn['summary'],
        }),
      ],
      { sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(html).toContain('<p class="notice">部分结果: 部分组件未取到值</p>')
  })

  it('结果表:数字右对齐 + 超限写明 导出/完整 维度', () => {
    const cols = Array.from({ length: 12 }, (_, i) => `c${i}`)
    const rows = Array.from({ length: 60 }, (_, i) =>
      cols.map((_, j) => (j === 0 ? i : `v${i}`)),
    )
    const html = buildSessionHtml(
      [
        turn({
          question: 'q',
          answer: 'a',
          summary: { columns: cols, rows } as Turn['summary'],
        }),
      ],
      { sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(html).toContain('结果 (50 行 × 8 列 / 60 行 × 12 列)')
    expect(html).toContain('class="numeric"')
    expect((html.match(/<tr>/g) ?? []).length).toBe(51) // 表头 + 50 行
  })

  it('老会话/失败轮:无 summary 不出分析/SQL/结果块,error 原文入档', () => {
    const html = buildSessionHtml(
      [
        turn({ question: 'q1', answer: 'plain' }),
        turn({ question: 'q2', error: 'datasource unreachable' }),
      ],
      { sessionId: 's', labels: LABELS, now: NOW },
    )
    // 注意断言要锚元素(CSS 里也有 .report-ana/.report-sql/.report-results)
    expect(html).not.toContain('<section class="report-ana">')
    expect(html).not.toContain('<details class="report-sql">')
    expect(html).not.toContain('<figure class="report-results">')
    expect(html).toContain('<p class="turn-error">datasource unreachable</p>')
  })
})

describe('sessionReportFilename', () => {
  it('标题 + YYYYMMDD + .html,非法字符同 md 口径清洗', () => {
    expect(sessionReportFilename('季度/分析:Q3?', 'abcdef12', NOW)).toBe(
      '季度-分析-Q3-20261003.html',
    )
  })

  it('标题为空回退 session 短号', () => {
    expect(sessionReportFilename('   ', 'abcdef12-3456', NOW)).toBe(
      'abcdef12-20261003.html',
    )
  })
})
