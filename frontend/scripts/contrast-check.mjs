#!/usr/bin/env node
/**
 * contrast-check.mjs — reproducible WCAG 2.1 contrast audit for tokens.css.
 *
 * Zero dependencies. Reads ../src/assets/styles/tokens.css, resolves the
 * `:root` custom properties (following var() chains in declaration order,
 * last wins), then evaluates a hard-coded pair list — the §6.2 table plus the
 * status-family pairs and the pairs this wave's changes create. Exit code 1
 * if any pair misses its threshold.
 *
 * Thresholds: 4.5 = AA normal text, 3.0 = AA non-text (UI boundaries, icons,
 * large text). Values come from the WCAG relative-luminance formula:
 *   L = 0.2126 R + 0.7152 G + 0.0722 B   (sRGB → linear, 0.03928 knee)
 *   ratio = (Lmax + 0.05) / (Lmin + 0.05)
 *
 * Usage: node scripts/contrast-check.mjs [path/to/tokens.css]
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const TOKENS_PATH = process.argv[2]
  ? resolve(process.argv[2])
  : resolve(HERE, '../src/assets/styles/tokens.css')

/* ── token parsing ─────────────────────────────────────────────────────── */

/** Collect every `--name: value;` inside `:root { … }` blocks (in order). */
function readRootTokens(cssSource) {
  // Comments first: they legitimately mention `--token:` names and would
  // otherwise be parsed as declarations.
  const css = cssSource.replace(/\/\*[\s\S]*?\*\//g, '')
  const tokens = new Map()
  const block = /:root\s*\{([\s\S]*?)\}/g
  let m
  while ((m = block.exec(css))) {
    const decl = /(--[\w-]+)\s*:\s*([^;]+);/g
    let d
    while ((d = decl.exec(m[1]))) tokens.set(d[1], d[2].trim())
  }
  return tokens
}

/** Resolve a value through var() chains; returns hex/rgb or null. */
function resolveValue(tokens, name, seen = new Set()) {
  if (seen.has(name)) throw new Error(`var() cycle at ${name}`)
  seen.add(name)
  const raw = tokens.get(name)
  if (raw === undefined) throw new Error(`token not found: ${name}`)
  const varRef = /^var\(\s*(--[\w-]+)\s*(?:,[^)]*)?\)$/.exec(raw)
  if (varRef) return resolveValue(tokens, varRef[1], seen)
  return raw
}

/** Hex (#rgb / #rrggbb) → [r, g, b] 0-255. */
function toRgb(value, tokenName) {
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value.trim())
  if (!hex) {
    throw new Error(`${tokenName} does not resolve to a solid hex: "${value}"`)
  }
  const h = hex[1]
  const full = h.length === 3 ? [...h].map((c) => c + c).join('') : h
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16))
}

/* ── WCAG math ─────────────────────────────────────────────────────────── */

function channelLinear(c) {
  const s = c / 255
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
}

function luminance([r, g, b]) {
  return (
    0.2126 * channelLinear(r) +
    0.7152 * channelLinear(g) +
    0.0722 * channelLinear(b)
  )
}

function contrast(fg, bg) {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a)
  return (hi + 0.05) / (lo + 0.05)
}

/* ── the audit list (hard-coded on purpose: it is the §6.2 contract) ───── */

const TEXT = 4.5
const UI = 3.0

const PAIRS = [
  // §6.2 body copy — primary / secondary / tertiary on every surface
  ['--text-primary', '--surface-raised', TEXT, '§6.2 body'],
  ['--text-primary', '--surface-canvas', TEXT, '§6.2 body on canvas'],
  ['--text-primary', '--surface-muted', TEXT, '§6.2 body on muted'],
  ['--text-secondary', '--surface-raised', TEXT, '§6.2 secondary'],
  ['--text-secondary', '--surface-canvas', TEXT, '§6.2 secondary on canvas'],
  ['--text-tertiary', '--surface-raised', TEXT, '§6.2 tertiary (was #a1a1aa)'],
  ['--text-tertiary', '--surface-canvas', TEXT, '§6.2 tertiary on canvas'],
  // gray-500 measures 4.40:1 on --surface-muted (below 4.5), so text on that
  // surface uses the role token --text-on-muted (gray-600) instead — the pair
  // is checked here at the same 4.5 bar.
  ['--text-on-muted', '--surface-muted', TEXT, 'text on muted surface'],
  // §6.2 badges / status text
  ['--ok-text', '--ok-bg', TEXT, '§6.2 success badge'],
  ['--warn-text', '--warn-bg', TEXT, '§6.2 warning badge (was amber-500)'],
  ['--danger-text', '--danger-bg', TEXT, '§6.2 danger badge (was red-500)'],
  ['--info-text', '--info-bg', TEXT, 'info badge (W23 contract)'],
  ['--danger-text', '--surface-raised', TEXT, 'error copy on card'],
  ['--warn-text', '--surface-raised', TEXT, 'warning copy on card'],
  ['--ok-text', '--surface-raised', TEXT, 'success copy on card'],
  ['--info-text', '--surface-raised', TEXT, 'info copy on card'],
  // §6.2 links / emphasis / buttons
  ['--accent', '--surface-raised', TEXT, '§6.2 link/emphasis (was #6366f1)'],
  ['--accent', '--surface-canvas', TEXT, 'link on canvas'],
  ['--accent', '--accent-soft', TEXT, 'link on accent-soft'],
  ['--accent-active', '--accent-soft', TEXT, '§6.2 active nav item'],
  ['--on-accent', '--accent', TEXT, '§6.2 primary button text'],
  ['--on-accent', '--accent-hover', TEXT, 'primary button hover text'],
  ['--on-accent', '--danger', TEXT, '§6.2 danger button text'],
  ['--on-accent', '--danger-hover', TEXT, 'danger button hover text'],
  // family borders vs their own bg (1.4.11 non-text)
  ['--ok-border', '--ok-bg', UI, 'success border'],
  ['--warn-border', '--warn-bg', UI, 'warning border'],
  ['--danger-border', '--danger-bg', UI, 'danger border'],
  ['--info-border', '--info-bg', UI, 'info border'],
  // solid status tones used as dots / icons / left rules on a white card
  ['--ok', '--surface-raised', UI, 'success mark'],
  ['--warn', '--surface-raised', UI, 'warning mark'],
  ['--danger', '--surface-raised', UI, 'danger mark'],
  ['--info', '--surface-raised', UI, 'info mark'],
  // accent as a graphic (borders, icons, chart marks) — 3:1 tier only
  ['--accent-graphic', '--surface-raised', UI, 'graphic accent'],
  // control boundary: unchecked checkbox / form control edge on white
  ['--text-tertiary', '--surface-raised', UI, 'control boundary'],
]

/* ── run ───────────────────────────────────────────────────────────────── */

const css = readFileSync(TOKENS_PATH, 'utf8')
const tokens = readRootTokens(css)
const resolved = new Map()
function color(name) {
  if (!resolved.has(name)) resolved.set(name, toRgb(resolveValue(tokens, name), name))
  return resolved.get(name)
}

const rows = []
let failed = 0
for (const [fg, bg, min, label] of PAIRS) {
  const ratio = contrast(color(fg), color(bg))
  const pass = ratio >= min
  if (!pass) failed += 1
  rows.push({ label, pair: `${fg} / ${bg}`, ratio, min, pass })
}

const nameW = Math.max(...rows.map((r) => r.label.length))
const pairW = Math.max(...rows.map((r) => r.pair.length))
console.log(`tokens: ${TOKENS_PATH}`)
console.log(`${'pair'.padEnd(nameW)}  ${'tokens'.padEnd(pairW)}  ratio    min  verdict`)
for (const r of rows) {
  console.log(
    `${r.label.padEnd(nameW)}  ${r.pair.padEnd(pairW)}  ${r.ratio
      .toFixed(2)
      .padStart(5)}  ${r.min.toFixed(1)}  ${r.pass ? 'PASS' : 'FAIL'}`,
  )
}
console.log(
  `\n${rows.length - failed}/${rows.length} pairs pass ` +
    `(${PAIRS.filter((p) => p[2] === TEXT).length} text @4.5:1, ` +
    `${PAIRS.filter((p) => p[2] === UI).length} non-text @3:1)`,
)
if (failed) {
  console.error(`${failed} pair(s) below threshold`)
  process.exit(1)
}
