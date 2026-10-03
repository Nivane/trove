import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { messages } from '../src/i18n'

/**
 * i18n parity (P7-W4 §6.4): zh and en must carry exactly the same key set,
 * with no duplicates on either side. Until this spec existed the alignment
 * was only ever checked by an ad-hoc script — a one-sided addition would
 * ship silently.
 *
 * The runtime object cannot reveal duplicates (the later literal wins), so
 * the source is scanned as well, and the scan is cross-checked against the
 * runtime keys to prove it read the same thing the app does.
 */

// vitest runs with the frontend package as cwd (import.meta.url is an http
// URL under the jsdom environment, so it cannot be used here).
const SOURCE = resolve(process.cwd(), 'src/i18n.ts')

/** Top-level keys of one language block, in file order, duplicates kept. */
function scanBlockKeys(source: string, block: 'zh' | 'en'): string[] {
  const start = source.indexOf(`\n  ${block}: {`)
  if (start < 0) throw new Error(`block not found: ${block}`)
  const rest = source.slice(start + 1)
  const end = rest.search(/\n {2}\},?\n/)
  const body = end < 0 ? rest : rest.slice(0, end)
  // Top-level entries sit at exactly 4 spaces; nested objects indent deeper.
  return [...body.matchAll(/^ {4}([A-Za-z_$][\w$]*)\s*:/gm)].map((m) => m[1])
}

const zhScanned = scanBlockKeys(readFileSync(SOURCE, 'utf8'), 'zh')
const enScanned = scanBlockKeys(readFileSync(SOURCE, 'utf8'), 'en')
const zhRuntime = Object.keys(messages.zh)
const enRuntime = Object.keys(messages.en)

describe('i18n parity', () => {
  it('scans the same keys the app loads (scan fidelity)', () => {
    expect(zhScanned).toEqual(zhRuntime)
    expect(enScanned).toEqual(enRuntime)
  })

  it('has no duplicate keys in either block', () => {
    const dupes = (keys: string[]) => keys.filter((k, i) => keys.indexOf(k) !== i)
    expect(dupes(zhScanned)).toEqual([])
    expect(dupes(enScanned)).toEqual([])
  })

  it('zh and en key sets are identical (both directions)', () => {
    expect(zhRuntime.filter((k) => !(k in messages.en))).toEqual([])
    expect(enRuntime.filter((k) => !(k in messages.zh))).toEqual([])
    expect(zhRuntime.length).toBe(enRuntime.length)
  })

  it('zh and en declare the same order', () => {
    expect(zhScanned).toEqual(enScanned)
  })

  it('every value is a non-empty string in both languages', () => {
    for (const lang of ['zh', 'en'] as const) {
      for (const [key, value] of Object.entries(messages[lang])) {
        expect(typeof value, `${lang}.${key}`).toBe('string')
        expect((value as string).length, `${lang}.${key}`).toBeGreaterThan(0)
      }
    }
  })
})
