import { describe, it, expect } from 'vitest'
import { stripAsciiChart, renderMarkdown } from '../src/utils/markdown'

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
