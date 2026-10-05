/**
 * Record the README hero GIF (npm run gif) against a running local stack:
 * fresh session → type the demo question → screenshot every ~800ms until the
 * run reports done → hand the frames to scripts/build_readme_gif.py (Pillow)
 * which downsamples, drops unchanged frames and assembles assets/demo.gif.
 *
 *   TROVE_GIF_BASE      frontend origin      (default http://localhost:5173)
 *   TROVE_GIF_USER      login                (default admin)
 *   TROVE_GIF_PASS      password             (no default — required)
 *   TROVE_GIF_QUESTION  question to ask      (default: the KB's own group-by
 *                                            template, which the shipped
 *                                            financial KB answers with a chart)
 *   TROVE_GIF_FRAMES    frame scratch dir    (default $TMPDIR/trove-gif-frames)
 *   TROVE_GIF_OUT       output GIF           (default <repo>/assets/demo.gif)
 *   TROVE_GIF_CLEANUP   "1" → delete the session the run created afterwards
 *                                            (for the docs capture instance,
 *                                            where re-records must be idempotent)
 *
 * Notes:
 * - The question must be answerable by the *live* KB: a run that ends in a
 *   clarification or an error card exits non-zero instead of assembling a GIF
 *   that would misrepresent the product (TROVE_GIF_FORCE=1 overrides).
 * - The analysis panel is default-closed and documents only the *live* run
 *   (a restored session shows none of its steps), so the recorder opens it
 *   right after sending — the README caption promises the steps unfolding.
 * - By default the session the run creates is left in place on purpose —
 *   deleting the user's session history is not this script's business.
 * - Requires Pillow on the python3 it invokes: `python3 -m pip install pillow`.
 */
import { execFileSync } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const { chromium } = createRequire(import.meta.url)('playwright-core')

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '../..')

const BASE = process.env.TROVE_GIF_BASE || 'http://localhost:5173'
const USER = process.env.TROVE_GIF_USER || 'admin'
const PASS = process.env.TROVE_GIF_PASS
const QUESTION =
  process.env.TROVE_GIF_QUESTION ||
  'How many loan records are there for each Loan status code (A, B, D)?'
const FRAMES = process.env.TROVE_GIF_FRAMES || path.join(os.tmpdir(), 'trove-gif-frames')
const OUT = path.resolve(process.env.TROVE_GIF_OUT || path.join(REPO, 'assets/demo.gif'))

const VIEWPORT = { width: 1440, height: 900 }
const SCALE = 2
const CADENCE = 600
const RUN_CAP_MS = 480_000
const MAX_FRAMES = 420
const TAIL_MS = 3200

if (!PASS) {
  console.error('TROVE_GIF_PASS is required (no password is baked into the repo).')
  process.exit(2)
}

const login = async (u, p) => {
  const r = await fetch(new URL('/v1/auth/login', BASE), {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username: u, password: p }),
  })
  if (!r.ok) throw new Error(`login ${u} failed: HTTP ${r.status}`)
  return (await r.json()).token
}

rmSync(FRAMES, { recursive: true, force: true })
mkdirSync(FRAMES, { recursive: true })

const token = await login(USER, PASS)
const browser = await chromium.launch({ channel: 'chrome' })
const ctx = await browser.newContext({
  viewport: VIEWPORT,
  deviceScaleFactor: SCALE,
  locale: 'zh-CN',
  colorScheme: 'light',
})
await ctx.addInitScript(([k, v]) => localStorage.setItem(k, v), ['trove_auth_token', token])
const page = await ctx.newPage()
await page.goto(new URL('/', BASE).href, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('.composer-input', { timeout: 20000 })
await page.waitForTimeout(2600) // let the brand fade-in settle before frame 0

await page.click('.new-session-btn') // fresh session → empty state, clean composer
await page.waitForTimeout(1400)

const shots = []
const t0 = Date.now()
let sent = false
let doneAt = null

const doneCheck = () =>
  page.evaluate(() => {
    const vis = (el) => !!el && el.offsetParent !== null
    return {
      statusDone: !!document.querySelector('.analysis-status.status-done'),
      streaming:
        vis(document.querySelector('.stop-btn')) ||
        vis(document.querySelector('.streaming-badge')) ||
        vis(document.querySelector('.stream-caret')),
      hasAnswer:
        (document.querySelector('.assistant-turn .answer')?.innerText || '').trim().length > 30,
    }
  })

const captureLoop = (async () => {
  let i = 0
  while (i < MAX_FRAMES) {
    const t = Date.now() - t0
    if (doneAt !== null && t - doneAt > TAIL_MS) break
    if (t > RUN_CAP_MS) {
      console.log(`run cap hit at ${(t / 1000).toFixed(0)}s — assembling what we have`)
      break
    }
    const file = path.join(FRAMES, `f${String(i).padStart(4, '0')}.png`)
    try {
      await page.screenshot({ path: file, animations: 'disabled', caret: 'hide' })
      shots.push({ file, t })
      i++
    } catch (e) {
      if (doneAt === null) console.error('screenshot failed:', e.message)
    }
    if (sent && doneAt === null && t > 2000) {
      try {
        const st = await doneCheck()
        if (st.statusDone || (st.hasAnswer && !st.streaming)) doneAt = Date.now() - t0
      } catch {
        // 探测失败只当"还没完成",下一帧再试 —— 不中断录制
      }
    }
    await page.waitForTimeout(CADENCE)
  }
})()

await page.click('.composer-input')
await page.locator('.composer-input').pressSequentially(QUESTION, { delay: 28 })
await page.waitForTimeout(400)
await page.click('.send-btn')
sent = true

// Open the analysis panel so the run's step timeline is on screen. The
// toggle only renders once the session has a turn (send first), and the
// panel is default-closed — mirrors capture-guide-shots.mjs.
try {
  await page.locator('.analysis-toggle').waitFor({ timeout: 5000 })
  if ((await page.locator('.analysis-panel.open').count()) === 0) {
    await page.click('.analysis-toggle')
  }
} catch {
  console.error('analysis panel toggle never appeared — recording without it')
}

await captureLoop

const fin = await page.evaluate(() => {
  const ans = document.querySelector('.assistant-turn .answer')?.innerText || ''
  return {
    errorCard: !!document.querySelector('.error-card'),
    clarify: /Clarification|缺少回答|请管理员|无法回答|拒绝/.test(ans),
    answerHead: ans.trim().split('\n').slice(0, 3).join(' / ').slice(0, 160),
  }
})
writeFileSync(path.join(FRAMES, 'times.json'), JSON.stringify(shots))
console.log(
  `captured ${shots.length} frames over ${((Date.now() - t0) / 1000).toFixed(1)}s — answer: ${fin.answerHead}`,
)

if (process.env.TROVE_GIF_CLEANUP === '1') {
  // Delete the session this run just created so a re-record starts clean
  // (the docs capture instance keeps re-shoots idempotent).
  await page.evaluate(async () => {
    const sid = localStorage.getItem('trove_ui_session')
    const token = localStorage.getItem('trove_auth_token')
    if (!sid) return
    await fetch(`/v1/sessions/${sid}`, {
      method: 'DELETE',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
  })
}

await browser.close()

if ((fin.errorCard || fin.clarify) && process.env.TROVE_GIF_FORCE !== '1') {
  console.error(
    'the run did not produce a real answer (clarification or error card) — refusing to ' +
      'assemble a GIF that misrepresents the product. Pick an answerable question ' +
      'TROVE_GIF_QUESTION=... (the question language must match the KB language), or force ' +
      'with TROVE_GIF_FORCE=1.',
  )
  process.exit(1)
}

execFileSync('python3', [path.join(REPO, 'scripts/build_readme_gif.py'), FRAMES, OUT], {
  stdio: 'inherit',
})
