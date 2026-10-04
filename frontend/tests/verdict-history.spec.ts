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

  it('a dim-less rule reports the trigger once, not twice with a dangling separator', async () => {
    // 无维度规则(整表判定)的唯一分组 dim 是空串:它的 fired 与规则级
    // 触发位是同一件事 —— chip 里只能说一次,且不留悬空的「 · 」。
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-high', count: 1,
      verdicts: [
        brief({
          diff: {
            prev_id: 1,
            rule_digest_changed: false,
            prev_rule_digest: 'sha256:b',
            rule_digest: 'sha256:b',
            status_change: ['ok', 'alert'],
            trigger: 'fired',
            groups: [{ dim: '', change: 'fired', triggered: [false, true], delta_pct: [-0.06, 3.7] }],
          },
        }),
      ],
    })
    const view = await mountDrawer()
    const chip = view.find('.vh-item .vh-chip').text()
    expect(chip).toBe('Now firing')
    // 幅度突变没有规则级对应位 —— 空 dim 的 jump 仍要单独写出
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-high', count: 1,
      verdicts: [
        brief({
          diff: {
            prev_id: 1,
            rule_digest_changed: false,
            prev_rule_digest: 'sha256:b',
            rule_digest: 'sha256:b',
            status_change: null,
            trigger: null,
            groups: [{ dim: '', change: 'jump', triggered: [true, true], delta_pct: [-0.1, -0.5] }],
          },
        }),
      ],
    })
    await view.unmount()
    wrapper = null
    const view2 = await mountDrawer()
    expect(view2.find('.vh-item .vh-chip').text()).toBe('magnitude jump')
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

  it('renders the noise band: section, position scores, degraded reasons', async () => {
    // 声明了 significance 的判定:证据里多出 significance 节,行卡多出
    // gated/confidence 两键。note(位置分数非概率)必须出现 —— 它防的
    // 正是这一节被读成概率。
    const banded: VerdictDetail = {
      verdict: {
        ...brief(),
        evidence: {
          ...DETAIL.verdict.evidence,
          rows: [
            { ...(DETAIL.verdict.evidence as any).rows[0],
              gated: true, confidence: 0.93 },
            { ...(DETAIL.verdict.evidence as any).rows[1],
              gated: false, confidence: 0.0 },
          ],
          significance: {
            required: true,
            min_confidence: 0,
            seasonal: { grain: 'month', lookback: 12, mode: 'trailing', k: 3.5 },
            note: '位置分数非概率:(|z|−k)/k 截断到 [0,1]',
            by_dim: {
              华东: { k: 3.5, z: 6.74, outside: true, confidence: 0.93,
                     gated: true, reason: '' },
              华北: { k: 3.5, z: 1.21, outside: false, confidence: 0.0,
                     gated: false, reason: 'within_band' },
            },
            degraded: [{ stage: 'significance', reason: 'unmatched_buckets:1' }],
          },
          budget: { limit: 12, used: 5, by_stage: { judge: 2, significance: 1 }, yielded: [] },
        },
      },
      diff: null,
    }
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-drop', count: 1, verdicts: [brief()],
    })
    vi.mocked(fetchVerdict).mockResolvedValue(banded)
    const view = await mountDrawer()
    await view.find('.vh-row').trigger('click')
    await flushPromises()

    const text = view.text()
    expect(text).toContain('Noise band · significance gate')
    expect(text).toContain('位置分数非概率')
    expect(text).toContain('6.74')            // z
    expect(text).toContain('0.93')            // 位置分
    expect(text).toContain('Outside band')    // 华东确认超带
    expect(text).toContain('within_band')     // 华北:判了,在带内
    expect(text).toContain('unmatched_buckets:1') // 降级必须可见
    expect(text).toContain('Query ledger')
    expect(text).toContain('5/12')            // 本次判定花了几条
  })

  it('without a declared band the detail keeps its old columns', async () => {
    vi.mocked(fetchVerdicts).mockResolvedValue({
      datasource: 'demo', rule_id: 'loan-drop', count: 1, verdicts: [brief()],
    })
    vi.mocked(fetchVerdict).mockResolvedValue(DETAIL)
    const view = await mountDrawer()
    await view.find('.vh-row').trigger('click')
    await flushPromises()

    // 未声明 → 不出现任何新节/新列(拿不到不渲染)
    expect(view.text()).not.toContain('Noise band')
    expect(view.text()).not.toContain('Position')
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
