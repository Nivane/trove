import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import {
  buildSessionMarkdown,
  sessionMarkdownFilename,
  useChatStore,
} from '../src/stores/chat'
import type { SessionExportLabels, Turn } from '../src/stores/chat'
import Sidebar from '../src/components/layout/Sidebar.vue'

const LABELS: SessionExportLabels = {
  results: '结果',
  rows: '行',
  cols: '列',
  generatedAt: '导出时间',
  rounds: '轮次',
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
