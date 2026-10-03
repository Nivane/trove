import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import {
  buildSessionMarkdown,
  sessionMarkdownFilename,
  useChatStore,
} from '../src/stores/chat'
import type { SessionExportLabels, Turn } from '../src/stores/chat'
import type { AnalysisPayload } from '../src/api/types'
import Sidebar from '../src/components/layout/Sidebar.vue'

const LABELS: SessionExportLabels = {
  results: '结果',
  rows: '行',
  cols: '列',
  generatedAt: '导出时间',
  rounds: '轮次',
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

const NOW = new Date('2026-10-03T14:05:00')

describe('buildSessionMarkdown — 整段会话导出', () => {
  it('渲染 标题/元信息/问题 heading/答案/SQL 代码块/结果表', () => {
    const turns = [
      turn({
        question: '哪个地区的平均贷款金额最高?',
        answer: '平均贷款金额最高的是 Prague,为 1000 千元。',
        summary: {
          sql: 'SELECT district, AVG(amount) FROM loan GROUP BY district ORDER BY 2 DESC',
          columns: ['地区', '平均贷款金额'],
          rows: [['Prague', 1000]],
        } as Turn['summary'],
      }),
    ]
    const md = buildSessionMarkdown(turns, {
      title: '季度分析',
      sessionId: 'abcdef12-3456-7890',
      labels: LABELS,
      now: NOW,
    })
    const lines = md.split('\n')
    // 前 10 行(验收口径:文档头 + 第一轮的开始)
    expect(lines.slice(0, 10)).toEqual([
      '# 季度分析',
      '',
      '> 导出时间: 2026/10/03 14:05 · 轮次: 1 · session:abcdef12',
      '',
      '---',
      '',
      '## 1. 哪个地区的平均贷款金额最高?',
      '',
      '平均贷款金额最高的是 Prague,为 1000 千元。',
      '',
    ])
    // SQL 代码块 + 结果表
    expect(md).toContain('```sql\nSELECT district, AVG(amount) FROM loan GROUP BY district ORDER BY 2 DESC\n```')
    expect(md).toContain('### 结果 (1 行 × 2 列)')
    expect(md).toContain('| 地区 | 平均贷款金额 |')
    expect(md).toContain('| --- | --- |')
    expect(md).toContain('| Prague | 1000 |')
  })

  it('标题缺省回退 session 短号;多行问题压成一行 heading', () => {
    const md = buildSessionMarkdown(
      [turn({ question: '第一行\n第二行', answer: 'ok' })],
      { sessionId: 'deadbeef-0000', labels: LABELS, now: NOW },
    )
    expect(md.startsWith('# deadbeef\n')).toBe(true)
    expect(md).toContain('## 1. 第一行 第二行')
  })

  it('回答与界面同口径(answer 优先,synthesis 兜底);失败轮按 error 原文落一行', () => {
    const md = buildSessionMarkdown(
      [
        turn({ question: 'q1', answer: '子任务答案A', synthesis: '综合回答A' }),
        turn({ question: 'q2', synthesis: '综合回答B' }),
        turn({ question: 'q3', error: 'datasource unreachable' }),
      ],
      { title: 't', sessionId: 'x', labels: LABELS, now: NOW },
    )
    // 界面显示 turn.answer(逐条子任务答案)→ 导出同一条
    expect(md).toContain('子任务答案A')
    expect(md).not.toContain('综合回答A')
    expect(md).toContain('综合回答B') // answer 缺席 → synthesis 兜底
    expect(md).toContain('> datasource unreachable')
  })

  it('超长/超宽结果表只导出前 N 并在 caption 注明两个维度', () => {
    const rows = Array.from({ length: 60 }, (_, i) => [i, 'x'])
    const cols = Array.from({ length: 12 }, (_, i) => `c${i}`)
    const md = buildSessionMarkdown(
      [
        turn({
          question: 'q',
          answer: 'a',
          summary: { columns: cols, rows: rows.map((r) => [...r, ...cols.slice(2).map(() => 0)]) } as Turn['summary'],
        }),
      ],
      { title: 't', sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(md).toContain('### 结果 (50 行 × 8 列 / 60 行 × 12 列)')
    const tableLines = md.split('\n').filter((l) => l.startsWith('| '))
    // 表头 + 分隔行 + 50 行数据
    expect(tableLines).toHaveLength(52)
    expect(tableLines[0]).toBe(`| ${cols.slice(0, 8).join(' | ')} |`)
  })

  it('单元格 | 转义、换行压成 <br>、null 留空', () => {
    const md = buildSessionMarkdown(
      [
        turn({
          question: 'q',
          answer: 'a',
          summary: { columns: ['a', 'b', 'c'], rows: [['x|y', 'l1\nl2', null]] } as Turn['summary'],
        }),
      ],
      { title: 't', sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(md).toContain('| x\\|y | l1<br>l2 |  |')
  })

  it('无列无行的轮次不渲染空表(旧会话只有纯文本 answer)', () => {
    const md = buildSessionMarkdown(
      [turn({ question: 'q', answer: 'plain' })],
      { title: 't', sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(md).not.toContain('### 结果')
    expect(md).not.toContain('| --- |')
  })
})

describe('buildSessionMarkdown — 分析证据节(补丁 2)', () => {
  const ANA = {
    version: 1,
    partial: false,
    evidence: {
      queries: [
        { purpose: 'overall', sql: 'SELECT SUM(amount) FROM loan', period: '2024' },
        {
          purpose: 'driver_tree',
          sql: 'SELECT district, SUM(amount) FROM loan GROUP BY 1',
        },
      ],
    },
  } as unknown as AnalysisPayload

  it('证据查询按 purpose 显示名渲染,节在结果表之前', () => {
    const md = buildSessionMarkdown(
      [
        turn({
          question: 'q',
          answer: 'a',
          summary: {
            sql: 'SELECT 1',
            columns: ['n'],
            rows: [[1]],
            analysis: ANA,
          } as Turn['summary'],
        }),
      ],
      { title: 't', sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(md).toContain('### 分析证据')
    expect(md).toContain('**1. 总体 · 2024**')
    expect(md).toContain('```sql\nSELECT SUM(amount) FROM loan\n```')
    expect(md).toContain('**2. 驱动分解**')
    expect(md.indexOf('### 分析证据')).toBeLessThan(md.indexOf('### 结果'))
  })

  it('证据超过 6 条只导出前 6 条并写明 已截断 N/M', () => {
    const queries = Array.from({ length: 8 }, (_, i) => ({
      purpose: 'probe',
      sql: `SELECT probe_${i}`,
    }))
    const ana = { evidence: { queries } } as unknown as AnalysisPayload
    const md = buildSessionMarkdown(
      [
        turn({
          question: 'q',
          answer: 'a',
          summary: { analysis: ana } as Turn['summary'],
        }),
      ],
      { title: 't', sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(md).toContain('**1. 维度探测**')
    expect(md).toContain('SELECT probe_5')
    expect(md).not.toContain('SELECT probe_6')
    expect(md).toContain('> 已截断 (6/8)')
  })

  it('partial 轮次带降级标注;无 analysis 的轮次不出该节', () => {
    const md = buildSessionMarkdown(
      [
        turn({
          question: 'q1',
          answer: 'a',
          summary: { analysis: { ...ANA, partial: true } } as Turn['summary'],
        }),
        turn({ question: 'q2', answer: 'plain' }),
      ],
      { title: 't', sessionId: 's', labels: LABELS, now: NOW },
    )
    expect(md).toContain('> **部分结果**: 部分组件未取到值')
    // 整段只出现一次「### 分析证据」(q2 无 analysis 不出节)
    expect(md.split('### 分析证据').length - 1).toBe(1)
  })
})

describe('sessionMarkdownFilename', () => {
  it('标题 + YYYYMMDD,非法字符清洗', () => {
    // 非法字符 → '-',首尾的 '-'/'.'/空白被剥掉,不留下悬空的连接符
    expect(sessionMarkdownFilename('季度/分析:Q3?', 'abcdef12', NOW)).toBe(
      '季度-分析-Q3-20261003.md',
    )
  })

  it('标题为空回退 session 短号', () => {
    expect(sessionMarkdownFilename('   ', 'abcdef12-3456', NOW)).toBe(
      'abcdef12-20261003.md',
    )
  })
})

describe('Sidebar 会话菜单(置顶/导出)', () => {
  beforeEach(() => localStorage.clear())

  function mountSidebar(sessions: Record<string, unknown>[]) {
    setActivePinia(createPinia())
    const chat = useChatStore()
    vi.spyOn(chat, 'listSessions').mockResolvedValue(undefined)
    chat.sessions = sessions as never
    return { wrapper: mount(Sidebar, { global: { stubs: { 'el-icon': true } } }), chat }
  }

  it('置顶行显示图钉标记,菜单显示「取消置顶」', async () => {
    const { wrapper } = mountSidebar([
      { session_id: 's1', title: '置顶会话', pinned: true },
    ])
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.session-pin').exists()).toBe(true)
    await wrapper.find('.session-more').trigger('click')
    const menu = wrapper.find('.session-menu')
    expect(menu.text()).toContain('取消置顶')
    expect(menu.text()).toContain('导出 Markdown')
    expect(menu.text()).toContain('导出 HTML 报告')
  })

  it('点击「导出 HTML 报告」→ 取轮次并触发 .html 下载', async () => {
    const { wrapper, chat } = mountSidebar([
      { session_id: 's3', title: '报告会话' },
    ])
    const fetch = vi
      .spyOn(chat, 'fetchSessionTurns')
      .mockResolvedValue([turn({ question: 'q', answer: 'a' })])
    // jsdom 没有 createObjectURL:stub 掉;anchor.click 也拦下避免导航告警
    const createObjectURL = vi.fn(() => 'blob:x')
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {})
    await wrapper.vm.$nextTick()
    await wrapper.find('.session-more').trigger('click')
    const btn = wrapper
      .findAll('.session-menu-item')
      .find((b) => b.text().includes('导出 HTML 报告'))!
    await btn.trigger('click')
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith('s3')
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    const a = click.mock.instances[0] as unknown as HTMLAnchorElement
    expect(a.download.startsWith('报告会话-')).toBe(true)
    expect(a.download.endsWith('.html')).toBe(true)
    click.mockRestore()
    vi.unstubAllGlobals()
  })

  it('未置顶行不显示图钉,菜单显示「置顶」,点击调用 pinSession(true)', async () => {
    const { wrapper, chat } = mountSidebar([
      { session_id: 's2', title: '普通会话', pinned: false },
    ])
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.session-pin').exists()).toBe(false)
    const pin = vi.spyOn(chat, 'pinSession').mockResolvedValue(undefined)
    await wrapper.find('.session-more').trigger('click')
    const btn = wrapper
      .findAll('.session-menu-item')
      .find((b) => b.text().includes('置顶'))!
    await btn.trigger('click')
    expect(pin).toHaveBeenCalledWith('s2', true)
  })
})
