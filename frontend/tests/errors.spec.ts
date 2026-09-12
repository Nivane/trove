import { describe, it, expect } from 'vitest'
import { errorCard } from '../src/utils/errors'

// 后端 present_error() 的产物(services/errors/present.py):用户可见的
// 标题/解释/建议 + 机器细节。前端只做呈现,不重新分类、不猜文案。
const INFO = {
  kind: 'gave_up',
  title: '这次没能给出可靠结果',
  explanation:
    '你的问题需要的计算方式,系统自动修正了 3 轮,仍然没能算稳,为避免给出错误的数字,已停止继续尝试。',
  suggestion: '换个说法再问一次,或把问题拆小一点(比如先限定时间范围或地区)。',
  retryable: true,
  detail: {
    raw: '回退目标 schema_linking 连续失败且无档可升，优雅降级',
    node: 'schema_linking',
    error_class: 'PLAN_DRIFT',
    domain: 'plan',
  },
}

describe('error card model', () => {
  it('takes the user-facing copy straight from the backend error_info', () => {
    const card = errorCard({ error: 'raw', error_info: INFO }, 'zh')!
    expect(card.title).toBe(INFO.title)
    expect(card.explanation).toBe(INFO.explanation)
    expect(card.suggestion).toBe(INFO.suggestion)
    expect(card.retryable).toBe(true)
  })

  it('keeps internal wording out of the headline and inside the admin detail', () => {
    const card = errorCard({ error: 'raw', error_info: INFO }, 'zh')!
    for (const leak of ['schema_linking', '优雅降级', '回退目标', 'PLAN_DRIFT']) {
      expect(card.title).not.toContain(leak)
      expect(card.explanation).not.toContain(leak)
      expect(card.suggestion).not.toContain(leak)
    }
    expect(card.detail.raw).toContain('优雅降级')
    expect(card.detail.node).toBe('schema_linking')
    expect(card.detail.errorClass).toBe('PLAN_DRIFT')
  })

  it('gives the admin detail a human node label alongside the raw node name', () => {
    const card = errorCard({ error_info: INFO }, 'zh')!
    expect(card.detail.nodeLabel).toContain('表关联')
    expect(card.detail.nodeLabel).toContain('schema_linking')
  })

  it('marks a permission failure non-retryable so the UI hides retry', () => {
    const card = errorCard(
      {
        error_info: {
          ...INFO,
          kind: 'permission',
          retryable: false,
          detail: { raw: 'Access denied', node: 'execute_sql' },
        },
      },
      'zh',
    )!
    expect(card.retryable).toBe(false)
  })

  it('falls back to scaffolding when a persisted turn has no error_info', () => {
    const card = errorCard({ error: 'no such table: loans' }, 'zh')!
    expect(card.title).toBe('回答没有完成')
    expect(card.explanation).toContain('no such table')
    expect(card.detail.raw).toBe('no such table: loans')
    // 老会话可能存下整段错误 markdown —— 去掉标题前缀与折叠块,别重复渲染
    const legacy = errorCard(
      {
        error:
          '**错误**: 回退目标 schema_linking 连续失败\n\n<details><summary>技术细节</summary>raw</details>\n',
      },
      'zh',
    )!
    expect(legacy.explanation).toBe('回退目标 schema_linking 连续失败')
  })

  it('returns null when the turn carries no error at all', () => {
    expect(errorCard(null, 'zh')).toBeNull()
    expect(errorCard({}, 'zh')).toBeNull()
    expect(errorCard({ error: '' }, 'zh')).toBeNull()
  })

  it('presents the fallback in english when the UI is english', () => {
    const card = errorCard({ error: 'boom' }, 'en')!
    expect(card.title).toBe('The answer did not complete')
    expect(card.suggestion.length).toBeGreaterThan(0)
  })
})
