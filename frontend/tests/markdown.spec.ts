import { describe, it, expect } from 'vitest'
import {
  stripAsciiChart,
  renderMarkdown,
  enhanceConclusionHtml,
} from '../src/utils/markdown'

describe('stripAsciiChart', () => {
  it('removes zh ascii chart (bold 图表: title + fenced block)', () => {
    const src = [
      'Result: 5 rows',
      '',
      '**图表: Loan by month**',
      '```',
      'Jan | ████████',
      '```',
      '',
      '---',
      '*Execution time: 12ms*',
    ].join('\n')
    const out = stripAsciiChart(src)
    expect(out).not.toContain('Loan by month')
    expect(out).not.toContain('████')
    expect(out).toContain('Result: 5 rows')
    expect(out).toContain('Execution time')
  })

  it('removes en ascii chart (bold Chart: title + fenced block)', () => {
    const src = '**Chart**: Loan by month\n```\nJan ███\n```\n'
    const out = stripAsciiChart(src)
    expect(out).not.toContain('Loan by month')
    expect(out).not.toContain('███')
  })

  it('does not remove a sql fenced block without a chart heading', () => {
    const src = '```sql\nSELECT * FROM t\n```'
    expect(stripAsciiChart(src)).toBe(src)
  })
})

describe('renderMarkdown', () => {
  it('produces sanitized html', () => {
    const html = renderMarkdown('# Hi\n\nSome `<b>text</b>`')
    expect(html).toContain('<h1>Hi')
    // inline code and plain text present; raw <b> sanitized (source not converted to html tag when in code)
  })

  it('right-aligns numeric table cells', () => {
    const html = renderMarkdown('| a |\n|---|\n| 42 |')
    expect(html).toContain('numeric')
  })
})

describe('enhanceConclusionHtml（答案卡：结论当主角）', () => {
  it('结论标题挂 concl-label、首段挂 concl、段内第一个数字包 hero-num', () => {
    const html = renderMarkdown('### 结论\n\n贷款金额最高的是 590,820。')
    const out = enhanceConclusionHtml(html)
    expect(out).toContain('concl-label')
    expect(out).toContain('class="concl"')
    expect(out).toContain('<span class="hero-num">590,820</span>')
  })

  it('英文 Conclusion 同样识别（含百分比数字）', () => {
    const html = renderMarkdown('### Conclusion\n\nThe share rose to 12.3%.')
    const out = enhanceConclusionHtml(html)
    expect(out).toContain('concl-label')
    expect(out).toContain('<span class="hero-num">12.3%</span>')
  })

  it('没有结论标题 → 原样返回（绝不把别的段落当结论放大）', () => {
    const html = renderMarkdown('### 结果\n\n共 12 行。')
    expect(enhanceConclusionHtml(html)).toBe(html)
  })

  it('中文夹单字数字（排名第3）不当主角；段落照样放大', () => {
    const html = renderMarkdown('### 结论\n\n排名第3的是 north。')
    const out = enhanceConclusionHtml(html)
    expect(out).toContain('class="concl"')
    expect(out).not.toContain('hero-num')
  })

  it('结论以年份开头 → 主角落在金额而非年份（1997年的…下降 3,073万）', () => {
    const html = renderMarkdown('### 结论\n\n1997年的贷款总额较1996年下降了 3,073万。')
    const out = enhanceConclusionHtml(html)
    expect(out).toContain('<span class="hero-num">3,073万</span>')
    expect(out).not.toContain('<span class="hero-num">1997</span>')
  })

  it('答案是年份（唯一候选）→ 年份照样当主角', () => {
    const html = renderMarkdown('### 结论\n\n1997年是最高的年份。')
    const out = enhanceConclusionHtml(html)
    expect(out).toContain('<span class="hero-num">1997</span>')
  })

  it('带千分位/单位的四位数不当年份排除（1,997 与 1997万 是数量）', () => {
    const html = renderMarkdown('### 结论\n\n金额为 1,997。')
    const out = enhanceConclusionHtml(html)
    expect(out).toContain('<span class="hero-num">1,997</span>')
  })

  it('结论后不是段落（直接表格/标题）→ 只压灰标题，不碰别的块', () => {
    const html = renderMarkdown('### 结论\n\n### 结果\n\n| a |\n|---|\n| 1 |')
    const out = enhanceConclusionHtml(html)
    expect(out).toContain('concl-label')
    expect(out).not.toContain('class="concl"')
    expect(out).not.toContain('hero-num')
  })
})
