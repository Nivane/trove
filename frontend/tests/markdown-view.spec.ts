/**
 * MarkdownView × object_columns(O2 可选增量)。
 *
 * 表头增强是**纯展示**增量:命中的列显示「列名 · 实体」,裸列名进
 * tooltip;单元格值与过滤/排序/复制/下载/数值判定/脱敏匹配一律不动
 * (它们全部继续用原始 headers)。键缺席 → 渲染管线逐字节同现状
 * (表头就是裸列名,连 title 属性都不加)。判据是键在不在,不是版本号。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import MarkdownView from '../src/components/chat/MarkdownView.vue'
import { useUiStore } from '../src/stores/ui'

let wrapper: VueWrapper | null = null

const SOURCE = [
  '结论如下。',
  '',
  '| name | balance |',
  '| --- | --- |',
  '| 张伟 | 1000 |',
  '| 李娜 | 2000 |',
].join('\n')

function mountView(
  objectColumns?: Record<string, { entity?: string; field?: string }> | null,
) {
  const pinia = createPinia()
  setActivePinia(pinia)
  useUiStore().lang = 'en'
  wrapper = mount(MarkdownView, {
    // 缺席态刻意**不传** prop(不是传 undefined)—— 钉"没有这个键"的路径。
    props: objectColumns ? { source: SOURCE, objectColumns } : { source: SOURCE },
    global: { plugins: [pinia] },
  })
  return wrapper
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

describe('MarkdownView — object_columns 表头增强', () => {
  it('键在:命中的列表头「name · 客户」+ 裸列名进 tooltip,单元格值一字不改', () => {
    const view = mountView({ name: { entity: '客户', field: 'name' } })

    const ths = view.findAll('.data-table thead th')
    expect(ths[0]?.text()).toBe('name · 客户')
    expect(ths[0]?.attributes('title')).toBe('name')
    // 没映射的列原样:裸列名,无 tooltip
    expect(ths[1]?.text()).toBe('balance')
    expect(ths[1]?.attributes('title')).toBeUndefined()

    // 单元格值一字不改(展示名只动表头)
    const cells = view.findAll('.data-table tbody tr:first-child td')
    expect(cells[0]?.text()).toBe('张伟')
    expect(cells[1]?.text()).toBe('1000')
  })

  it('键缺席:表头就是裸列名,不加任何 title(与现状逐字节一致)', () => {
    const view = mountView()

    const ths = view.findAll('.data-table thead th')
    expect(ths[0]?.text()).toBe('name')
    expect(ths[1]?.text()).toBe('balance')
    expect(ths.map((t) => t.attributes('title'))).toEqual([undefined, undefined])

    const cells = view.findAll('.data-table tbody tr:first-child td')
    expect(cells[0]?.text()).toBe('张伟')
    expect(cells[1]?.text()).toBe('1000')
  })
})
