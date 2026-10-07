/**
 * 通过真实浏览器验证前台问答：登录态恢复、输入、SSE 消费、公开结果渲染与刷新恢复。
 * 业务期待复用 golden_questions.json；运行环境由 run_frontend_e2e.sh 显式隔离。
 */
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const { chromium } = require('playwright')

function option(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

const baseUrl = option('--base-url', 'http://127.0.0.1:5175').replace(/\/$/, '')
const datasetPath = option('--dataset', path.resolve('eval/golden_questions.json'))
const outputPath = option('--output', path.resolve('eval/reports/frontend_e2e_report.json'))
const selectedCaseIds = new Set((option('--case-id', '') ?? '').split(',').filter(Boolean))
const excludedEvaluationMode = option('--exclude-evaluation-mode', '')

function parseSseEvents(body) {
  return body
    .split('\n\n')
    .map((frame) => frame.split('\n').find((line) => line.startsWith('data:'))?.slice(5).trim())
    .filter(Boolean)
    .map((data) => JSON.parse(data))
}

function assertCase(condition, message) {
  if (!condition) throw new Error(message)
}

function checkResult(testCase, events) {
  const completed = events.find((event) => event.type === 'run_completed')
  assertCase(completed, '缺少 run_completed 事件')
  const result = completed.payload?.result
  assertCase(result && typeof result === 'object', 'run_completed 缺少完整 result')
  assertCase(result.query_plan?.intent === testCase.expected_intent, `意图不匹配：${result.query_plan?.intent}`)
  assertCase(result.query_plan?.next_action === testCase.expected_action, `下一步不匹配：${result.query_plan?.next_action}`)
  assertCase(Boolean(result.evidence?.length) === testCase.must_have_evidence, '证据是否存在不符合测评集')

  const evidenceIds = new Set((result.evidence ?? []).map((item) => item.id))
  for (const expectedId of testCase.expected_evidence_ids ?? []) {
    assertCase(evidenceIds.has(expectedId), `缺少预期证据：${expectedId}`)
  }
  for (const forbiddenId of testCase.must_not_retrieve_ids ?? []) {
    assertCase(!evidenceIds.has(forbiddenId), `出现禁止证据：${forbiddenId}`)
  }
  if (testCase.expected_bundle_count !== undefined) {
    assertCase(result.bundles.length === testCase.expected_bundle_count, '套餐数量不匹配')
  }
  if (testCase.expected_bundle_id !== undefined) {
    assertCase(result.bundles[0]?.bundle_id === testCase.expected_bundle_id, '套餐 ID 不匹配')
  }
  if (testCase.expected_fallback_reason !== undefined) {
    assertCase(
      result.model_usage?.skipped_or_fallback_reason === testCase.expected_fallback_reason,
      `降级原因不匹配：${result.model_usage?.skipped_or_fallback_reason}`,
    )
  }
  if (testCase.expected_requires_confirmation !== undefined) {
    assertCase(result.requires_confirmation === testCase.expected_requires_confirmation, '确认状态不匹配')
  }
  if (testCase.expected_action_count !== undefined) {
    assertCase(result.actions.length === testCase.expected_action_count, '交易动作数量不匹配')
  }
  return result
}

async function createAuthenticatedContext(browser, testCase, caseIndex) {
  const context = await browser.newContext({ reducedMotion: 'reduce' })
  const suffix = `${caseIndex}_${testCase.id}`.replaceAll(/[^A-Za-z0-9_]/g, '_')
  const username = `e2e_${suffix}`.slice(0, 64)
  const password = 'CorrectHorseBattery1'
  const registration = await context.request.post(`${baseUrl}/api/customer/auth/register`, {
    data: {
      username,
      email: `${username}@example.test`,
      display_name: `测评 ${testCase.id}`,
      password,
    },
  })
  assertCase(registration.status() === 202, `注册失败：${registration.status()}`)
  const login = await context.request.post(`${baseUrl}/api/customer/auth/login`, {
    data: { username, password },
  })
  assertCase(login.ok(), `登录失败：${login.status()}`)
  return context
}

async function executeCase(browser, testCase, caseIndex, failureDirectory) {
  let context
  let page
  const browserErrors = []
  let cartDraftRequests = 0

  try {
    context = await createAuthenticatedContext(browser, testCase, caseIndex)
    page = await context.newPage()
    page.on('pageerror', (error) => browserErrors.push(error.message))
    page.on('request', (request) => {
      if (request.url().endsWith('/api/cart-drafts')) cartDraftRequests += 1
    })
    await page.goto(baseUrl, { waitUntil: 'networkidle' })
    const composer = page.getByPlaceholder('描述新的肤感、预算或偏好…')
    await composer.waitFor({ state: 'visible' })
    const sseResponse = page.waitForResponse((response) => (
      response.url().endsWith('/api/agent/runs') && response.request().method() === 'POST'
    ))
    await composer.fill(testCase.prompt)
    await page.getByRole('button', { name: '发送消息' }).click()
    const events = parseSseEvents(await (await sseResponse).text())
    assertCase(events.some((event) => event.type === 'run_started'), '缺少 run_started 事件')
    assertCase(events.some((event) => event.type === 'retrieval_plan'), '缺少 retrieval_plan 事件')
    assertCase(!events.some((event) => event.type === 'run_failed'), 'SSE 返回 run_failed')
    const result = checkResult(testCase, events)

    const latestAnswer = page.locator('.chat-turn--assistant .highlighted-answer').last()
    await latestAnswer.waitFor({ state: 'visible' })
    assertCase((await latestAnswer.innerText()).includes(result.answer), '最终回答未渲染到前台')
    assertCase(browserErrors.length === 0, `浏览器异常：${browserErrors.join(' | ')}`)
    assertCase(cartDraftRequests === 0, '测评不应创建购物车草稿')

    await page.reload({ waitUntil: 'networkidle' })
    await page
      .locator('.chat-turn--user p')
      .filter({ hasText: testCase.prompt })
      .waitFor({ state: 'visible' })
    const restoredAnswer = page.locator('.chat-turn--assistant .highlighted-answer').last()
    await restoredAnswer.waitFor({ state: 'visible' })
    assertCase((await restoredAnswer.innerText()).includes(result.answer), '刷新后未恢复持久化回答')

    return {
      case_id: testCase.id,
      dimension: testCase.dimension,
      passed: true,
      event_types: events.map((event) => event.type),
      intent: result.query_plan.intent,
      evidence_ids: result.evidence.map((item) => item.id),
      fallback_reason: result.model_usage?.skipped_or_fallback_reason ?? null,
      cart_draft_requests: cartDraftRequests,
    }
  } catch (error) {
    const screenshotPath = path.join(failureDirectory, `${testCase.id}.png`)
    await page?.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined)
    return {
      case_id: testCase.id,
      dimension: testCase.dimension,
      passed: false,
      error: error instanceof Error ? error.message : String(error),
      screenshot: screenshotPath,
    }
  } finally {
    await context?.close()
  }
}

const allCases = JSON.parse(await readFile(datasetPath, 'utf8'))
const dataset = selectedCaseIds.size
  ? allCases.filter((testCase) => selectedCaseIds.has(testCase.id))
  : allCases.filter((testCase) => testCase.evaluation_mode !== excludedEvaluationMode)
if (dataset.length === 0) throw new Error('未找到要运行的测评案例')
const failureDirectory = path.join(path.dirname(outputPath), 'frontend_e2e_failures')
await mkdir(failureDirectory, { recursive: true })
await mkdir(path.dirname(outputPath), { recursive: true })

const browser = await chromium.launch({
  headless: true,
  channel: process.env.PLAYWRIGHT_BROWSER_CHANNEL ?? 'chrome',
})
const cases = []
try {
  for (const [index, testCase] of dataset.entries()) {
    cases.push(await executeCase(browser, testCase, index, failureDirectory))
  }
} finally {
  await browser.close()
}

const report = {
  generated_at: new Date().toISOString(),
  execution_mode: 'frontend_e2e_deterministic_local_no_external_model_no_transaction_write',
  base_url: baseUrl,
  dataset: path.basename(datasetPath),
  summary: {
    total_cases: cases.length,
    passed_cases: cases.filter((item) => item.passed).length,
    failed_cases: cases.filter((item) => !item.passed).length,
  },
  cases,
}
await writeFile(outputPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
console.log(JSON.stringify(report.summary))
if (report.summary.failed_cases > 0) process.exitCode = 1
