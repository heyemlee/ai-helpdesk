const { test, expect } = require(process.env.ACCEPTANCE_PLAYWRIGHT_MODULE)
const fs = require('node:fs')
const path = require('node:path')
const cfg = JSON.parse(fs.readFileSync(process.env.ACCEPTANCE_STACK + '/private-config.json', 'utf8'))
const provenance = JSON.parse(fs.readFileSync(process.env.ACCEPTANCE_STACK + '/provenance.json', 'utf8'))
const out = process.env.ACCEPTANCE_BROWSER_OUTPUT

test('real employee browser: waiting, approval, more material, upload outage and recovery', async ({ page, browser }) => {
  if (provenance.real_provider) throw new Error('This browser failure-path test requires the provider-disabled stack')
  fs.mkdirSync(out, { recursive: true })
  await page.addInitScript(() => localStorage.setItem('helpdesk.lang', 'zh'))
  await page.goto('/')
  await page.getByLabel('员工账号').fill('acceptance-admin')
  await page.getByLabel('密码', { exact: true }).fill(cfg.password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('button', { name: '退出登录' })).toBeVisible()
  const session = await (await page.request.get('/api/auth/session')).json()
  expect(session.actor.roles).toContain('admin')
  const cookies = await page.context().cookies()
  expect(cookies.some(c => c.httpOnly && c.sameSite === 'Strict')).toBeTruthy()
  await page.goto('/#/tickets')
  await page.getByRole('button', { name: '进入跨消息工单' }).click()
  const work = page.getByRole('region', { name: '跨消息工单' })
  await work.getByLabel('问题类型').selectOption('order')
  await work.getByLabel('补充说明').fill('需要跟进订单，客户暂时还没有提供编号。')
  await work.getByRole('button', { name: '保存并继续' }).click()
  await expect(work.getByText('等待补料', { exact: true })).toBeVisible({ timeout: 30000 })
  const selected = await work.getByLabel('选择问题').inputValue()
  const getCase = async () => (await page.request.get('/api/tickets/' + selected + '/case')).json()
  await expect.poll(async () => (await getCase()).drafts.length).toBe(1)
  const first = await getCase()
  expect(first.pending_fields).toEqual(['order_ref'])
  await work.getByRole('button', { name: '确认此版本草稿' }).click()
  await expect.poll(async () => (await getCase()).drafts[0].status).toBe('approved')
  await work.getByLabel('补充说明').fill('又收到一条现场反馈，但单号仍待客户补充。')
  await work.getByRole('button', { name: '保存并继续' }).click()
  await expect.poll(async () => (await getCase()).drafts.length).toBe(2)
  const second = await getCase()
  await expect(work.getByText('版本 ' + second.version + ' · 待确认', { exact: true })).toBeVisible()
  expect(second.drafts.find(d => d.id === first.drafts[0].id).status).toBe('invalid')
  expect(second.case_id).toBe(first.case_id)

  // An actual client connection failure, then a successful real upload. No
  // synthetic image/model response or injected employee identity is installed.
  let failed = false
  await page.route('**/api/attachments', async route => {
    if (route.request().method() === 'POST' && !failed) { failed = true; return route.abort('connectionfailed') }
    return route.continue()
  })
  const image = path.resolve(__dirname, '../data/v1/images/development-images-001.png')
  await work.locator('input[type=file]').setInputFiles(image)
  await expect(work.getByText('上传失败，文件已保留，可重试。')).toBeVisible()
  await work.getByRole('button', { name: '重试上传' }).click()
  await expect(work.getByRole('img', { name: 'development-images-001.png' })).toBeVisible()
  await page.unroute('**/api/attachments')
  let submitted = page.waitForResponse(r => r.url().endsWith('/tickets/cases/inputs') && r.request().method() === 'POST')
  await work.getByRole('button', { name: '保存并继续' }).click()
  let submitResponse = await submitted
  if (submitResponse.status() === 409) {
    await expect(work.getByRole('alert')).toContainText('输入已保留')
    submitted = page.waitForResponse(r => r.url().endsWith('/tickets/cases/inputs') && r.request().method() === 'POST')
    await work.getByRole('button', { name: '保存并继续' }).click()
    submitResponse = await submitted
  }
  expect(submitResponse.status()).toBe(202)
  await expect.poll(async () => (await getCase()).image_reviews.length, { timeout: 90000 }).toBe(1)
  const withImage = await getCase()
  expect(withImage.image_reviews[0].result.query_outcome).toBe('extraction_failed')
  expect(Object.values(withImage.facts).some(f => f.source === 'verified_image')).toBe(false)
  await page.reload()
  await page.getByRole('button', { name: '进入跨消息工单' }).click()
  await work.getByLabel('选择问题').selectOption(selected)
  await expect(work.getByRole('img', { name: 'development-images-001.png' })).toBeVisible()
  const attachment = withImage.materials[0].attachment_id
  const anonymous = await browser.newContext()
  expect((await anonymous.request.get(cfg.origin + '/api/attachments/' + attachment + '/content')).status()).toBe(401)
  await anonymous.close()
  await page.screenshot({ path: out + '/browser-failure-path.png', fullPage: true })
  fs.writeFileSync(out + '/recording.json', JSON.stringify({ kind: 'provider_disabled_browser_workflow', passed: true,
    employee_login: true, role: 'admin', upload: true, multiple_turns: true, approval: true,
    failure_recovery: true, real_provider: false, release_eligible: false, provenance,
    checks: ['HttpOnly session', 'pending order retained', 'internal approval', 'new material invalidates old draft', 'failed upload retried', 'model failure not promoted to image fact', 'anonymous image denied'],
    first, second, withImage }, null, 2))
})
