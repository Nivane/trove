/**
 * VerdictHistoryDrawer — 判定历史抽屉。
 *
 * 契约由服务端端点定死(mock 的就是它的返回形状)。这里钉三条渲染纪律:
 *
 *   · 判定不可编辑 —— 抽屉里没有任何写操作;
 *   · ``diff: null`` 是**窗口边界**(该条前面没有可比的一条),必须与
 *     "与上次相同"渲染成不同的样子 —— 把没得比读成平静是审计视图最危险
 *     的错觉;
 *   · 证据里分析的 ``degraded`` 必须显示:桥没做成的那一步写在这里。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import VerdictHistoryDrawer from '../src/components/admin/VerdictHistoryDrawer.vue'
import { useUiStore } from '../src/stores/ui'
import { fetchVerdict, fetchVerdicts } from '../src/api/decisions'
import type { VerdictBrief, VerdictDetail } from '../src/api/decisions'

vi.mock('../src/api/decisions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/decisions')>()
  return {
    ...actual, // verdictStatusClass 是纯函数,用真的
    fetchVerdicts: vi.fn(),
    fetchVerdict: vi.fn(),
  }
})

let wrapper: VueWrapper | null = null

function brief(over: Partial<VerdictBrief> = {}): VerdictBrief {
  return {
    id: 2,
    datasource: 'demo',
    rule_id: 'loan-drop',
    rule_digest: 'sha256:b',
    run_id: 7,
    job_id: 'job-1',
    status: 'alert',
    triggered: true,
    severity: 'warning',
    priority: 2,
    message: '[warning] 华东: 当期 600, 基期 1,200, 变化 -50.0%',
    error: '',
    row_count: 2,
    evidence_truncated: false,
    anchor_date: '2026-09-27',
    evaluated_at: '2026-09-27T10:00:00',
    created_at: '2026-09-27T10:00:00',
    diff: {
      prev_id: 1,
      rule_digest_changed: false,
      prev_rule_digest: 'sha256:b',
      rule_digest: 'sha256:b',
      status_change: ['ok', 'alert'],
      trigger: 'fired',
      groups: [],
    },
    ...over,
  }
}

const DETAIL: VerdictDetail = {
  verdict: {
    ...brief(),
    evidence: {
      rule_id: 'loan-drop',
      rows: [
        { dim: '华东', current: 600, baseline: 1200, delta: -600,
          delta_pct: -0.5, contribution: -0.92, triggered: true, matched: ['delta_pct < -0.1'] },
        { dim: '华北', current: 450, baseline: 400, delta: 50,
          delta_pct: 0.125, contribution: 0.08, triggered: false, matched: [] },
      ],
      evidence: {
        sql_current: 'SELECT loan.region, SUM(loan.amount) FROM loan GROUP BY loan.region',
        sql_baseline: 'SELECT loan.region, SUM(loan.amount) FROM loan WHERE loan.date <= \'2026-08-31\' GROUP BY loan.region',
        columns: ['region', 'loan_balance'],
        rows: [['华东', 600], ['华北', 450]],
        row_count: 2,
        truncated: false,
      },
      analysis: {
        top_components: [{ dim: 'region', value: '华东', delta: -600, contribution: -0.92, source: 'dimension' }],
        residual: { value: 0.0, exact: true, reason: 'identity' },
        degraded: [{ stage: 'driver_tree', reason: 'not_aggregate:loan_net' }],
      },
    },
  },
  diff: {
    prev_id: 1,
    rule_digest_changed: true,
    prev_rule_digest: 'sha256:a',
    rule_digest: 'sha256:b',
    status_change: ['ok', 'alert'],
    trigger: 'fired',
    groups: [{ dim: '华东', change: 'jump', triggered: [true, true], delta_pct: [-0.1, -0.5] }],
  },
}

async function mountDrawer() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useUiStore().lang = 'en'
  wrapper = mount(VerdictHistoryDrawer, {
    props: { modelValue: true, datasource: 'demo', ruleId: 'loan-drop', ruleName: '贷款余额环比下滑' },
    global: { plugins: [pinia], stubs: { teleport: true } },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
  setActivePinia(createPinia())
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('VerdictHistoryDrawer — 判定历史', () => {
  it('lists verdicts with status pill and message', async () => {
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-drop', count: 1, verdicts: [brief()],
    })
    const view = await mountDrawer()
    const text = view.text()
    expect(text).toContain('Verdict history')
    expect(text).toContain('alert')
    expect(text).toContain('华东: 当期 600')
  })

  it('renders a null diff as a window boundary, never as "no change"', async () => {
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-drop', count: 1,
      verdicts: [brief({ diff: null })],
    })
    const view = await mountDrawer()
    const text = view.text()
    expect(text).toContain('Window boundary')
    expect(text).not.toContain('No change since previous')
  })

  it('summarizes an adjacent diff: trigger + rule edit + group change', async () => {
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-drop', count: 2,
      verdicts: [
        brief({
          diff: {
            prev_id: 1,
            rule_digest_changed: true,
            prev_rule_digest: 'sha256:a',
            rule_digest: 'sha256:b',
            status_change: ['ok', 'alert'],
            trigger: 'fired',
            groups: [{ dim: '华东', change: 'jump', triggered: [true, true], delta_pct: [-0.1, -0.5] }],
          },
        }),
        brief({ id: 1, status: 'ok', triggered: false, message: '', diff: null }),
      ],
    })
    const view = await mountDrawer()
    const first = view.findAll('.vh-item')[0].text()
    expect(first).toContain('Now firing')
    expect(first).toContain('Rule edited')
    expect(first).toContain('magnitude jump')
  })

  it('a rule that never ran shows the empty state, not an empty table', async () => {
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-drop', count: 0, verdicts: [],
    })
    const view = await mountDrawer()
    expect(view.text()).toContain('No verdicts recorded yet')
  })

  it('a failed read shows the reason and a retry, not an empty history', async () => {
    vi.mocked(fetchVerdicts).mockRejectedValue(new Error('store down'))
    const view = await mountDrawer()
    const text = view.text()
    expect(text).toContain('Could not load the verdict history')
    expect(text).toContain('store down')

    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-drop', count: 1, verdicts: [brief()],
    })
    await view.find('.sp-actions button').trigger('click')
    await flushPromises()
    expect(view.text()).toContain('alert')
  })

  it('expanding a verdict fetches and shows its evidence, drivers and degraded notes', async () => {
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-drop', count: 1, verdicts: [brief()],
    })
    vi.mocked(fetchVerdict).mockResolvedValue(DETAIL)
    const view = await mountDrawer()

    expect(view.text()).not.toContain('SUM(loan.amount)') // 未展开不拉证据
    await view.find('.vh-row').trigger('click')
    await flushPromises()

    const text = view.text()
    expect(vi.mocked(fetchVerdict)).toHaveBeenCalledWith(2)
    expect(text).toContain('SELECT loan.region, SUM(loan.amount)')
    expect(text).toContain('华东')            // 判定行
    expect(text).toContain('Triggered')
    expect(text).toContain('Driver analysis')
    expect(text).toContain('region=华东')
    expect(text).toContain('identity')        // 残差口径
    expect(text).toContain('not_aggregate:loan_net') // 降级必须可见
  })

  it('collapses the detail on a second click without refetching', async () => {
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-drop', count: 1, verdicts: [brief()],
    })
    vi.mocked(fetchVerdict).mockResolvedValue(DETAIL)
    const view = await mountDrawer()
    await view.find('.vh-row').trigger('click')
    await flushPromises()
    await view.find('.vh-row').trigger('click')
    await flushPromises()
    await view.find('.vh-row').trigger('click')
    await flushPromises()
    expect(vi.mocked(fetchVerdict)).toHaveBeenCalledTimes(1)
    expect(view.text()).toContain('SELECT loan.region')
  })
})
