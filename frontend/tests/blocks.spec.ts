import { describe, it, expect } from 'vitest'
import { tokenize } from '../src/utils/blocks'

describe('markdown block tokenizer', () => {
  it('keeps plain text as a single md block', () => {
    const blocks = tokenize('hello\nworld')
    expect(blocks).toEqual([{ type: 'md', text: 'hello\nworld' }])
  })

  it('extracts a pipe table with headers and rows', () => {
    const blocks = tokenize(
      [
        '| name | amount |',
        '|------|--------|',
        '| a    | 10     |',
        '| b    | 20     |',
      ].join('\n'),
    )
    expect(blocks[0]).toEqual({
      type: 'table',
      headers: ['name', 'amount'],
      rows: [
        ['a', '10'],
        ['b', '20'],
      ],
    })
  })

  it('extracts sql fenced blocks and leaves other fences as markdown', () => {
    const blocks = tokenize(
      '```sql\nSELECT * FROM t\n```\n\ntext\n\n```python\nx=1\n```',
    )
    expect(blocks[0]).toEqual({ type: 'sql', code: 'SELECT * FROM t' })
    expect(blocks[1]).toEqual({
      type: 'md',
      text: '\ntext\n\n```python\nx=1\n```',
    })
  })

  it('preserves order of mixed content', () => {
    const blocks = tokenize('intro\n\n| a |\n|---|\n| 1 |\n\noutro')
    expect(blocks.map((b) => b.type)).toEqual(['md', 'table', 'md'])
  })

  it('does not mistake a separator header within indented content as top-level table', () => {
    const blocks = tokenize('  | not a table |\n  |---|---|')
    expect(blocks.every((b) => b.type === 'md')).toBe(true)
  })

  it('extracts a collapsible details block with its inner blocks', () => {
    const blocks = tokenize(
      [
        '<details>',
        '<summary>View SQL &amp; details</summary>',
        '',
        '### Generated SQL',
        '',
        '```sql',
        'SELECT * FROM t',
        '```',
        '',
        '| c |',
        '|---|',
        '| 1 |',
        '</details>',
      ].join('\n'),
    )
    expect(blocks).toHaveLength(1)
    const d = blocks[0] as {
      type: 'details'
      summary: string
      blocks: { type: string; text?: string; code?: string; headers?: string[]; rows?: string[][] }[]
    }
    expect(d.type).toBe('details')
    expect(d.summary).toBe('View SQL &amp; details')
    expect(d.blocks.map((b) => b.type)).toEqual(['md', 'sql', 'table'])
    expect(d.blocks[0].text).toContain('### Generated SQL')
    expect(d.blocks[1]).toEqual({ type: 'sql', code: 'SELECT * FROM t' })
    expect(d.blocks[2]).toEqual({ type: 'table', headers: ['c'], rows: [['1']] })
  })

  it('extracts multiple details blocks in order', () => {
    const blocks = tokenize(
      [
        '<details>',
        '<summary>结果明细</summary>',
        '',
        '| c |',
        '|---|',
        '| 1 |',
        '</details>',
        '',
        '<details>',
        '<summary>View SQL &amp; details</summary>',
        '',
        '```sql',
        'SELECT 1',
        '```',
        '</details>',
      ].join('\n'),
    )
    expect(blocks.map((b) => b.type)).toEqual(['details', 'details'])
    expect((blocks[0] as { summary: string }).summary).toBe('结果明细')
    expect((blocks[1] as { summary: string }).summary).toBe('View SQL &amp; details')
  })
})
