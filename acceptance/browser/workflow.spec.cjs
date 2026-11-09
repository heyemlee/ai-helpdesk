const { test, expect } = require(process.env.ACCEPTANCE_PLAYWRIGHT_MODULE)
const fs = require('node:fs')
const path = require('node:path')
const cfg = JSON.parse(fs.readFileSync(process.env.ACCEPTANCE_STACK + '/private-config.json', 'utf8'))
const provenance = JSON.parse(fs.readFileSync(process.env.ACCEPTANCE_STACK + '/provenance.json', 'utf8'))
const out = process.env.ACCEPTANCE_BROWSER_OUTPUT

test('real employee browser: waiting, approval, more material, upload outage and recovery', async ({ page, browser }) => {
  const started = Date.now()
  if (provenance.real_provider && process.env.ACCEPTANCE_REAL_BROWSER !== '1') throw new Error('Real browser calls need a separate release budget')
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
  await expect.poll(async () => (await getCase()).drafts.length, { timeout: 30000 }).toBe(1)
  const first = await getCase()
  expect(first.pending_fields).toEqual(['order_ref'])
  await work.getByRole('button', { name: '确认此版本草稿' }).click()
  await expect.poll(async () => (await getCase()).drafts[0].status).toBe('approved')
  await work.getByLabel('补充说明').fill('又收到一条现场反馈，但单号仍待客户补充。')
  await work.getByRole('button', { name: '保存并继续' }).click()
  await expect.poll(async () => (await getCase()).drafts.length, { timeout: 30000 }).toBe(2)
  const second = await getCase()
  await expect(work.getByText('版本 ' + second.version + ' · 待确认', { exact: true })).toBeVisible()
  expect(second.drafts.find(d => d.id === first.drafts[0].id).status).toBe('invalid')
  expect(second.case_id).toBe(first.case_id)

  // An actual client connection failure, then a successful real upload. No
  // injected employee identity is installed. External model mode is recorded.
  let failed = false
  await page.route('**/api/attachments', async route => {
    if (route.request().method() === 'POST' && !failed) { failed = true; return route.abort('connectionfailed') }
    return route.continue()
  })
  const image = path.resolve(process.env.ACCEPTANCE_CORPUS_DIR, 'images/development-images-001.png')
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
  await expect.poll(async () => (await getCase()).image_reviews.length, { timeout: 300000 }).toBe(1)
  const withImage = await getCase()
  if (provenance.provider_mode === 'disabled') {
    expect(withImage.image_reviews[0].result.query_outcome).toBe('extraction_failed')
    expect(Object.values(withImage.facts).some(f => f.source === 'verified_image')).toBe(false)
  } else {
    const corpus = JSON.parse(fs.readFileSync(path.join(process.env.ACCEPTANCE_CORPUS_DIR, 'development.json')))
    const truth = corpus.scenarios.find(s => s.id === 'development-images-001').truth.image_fields
    const fields = withImage.image_reviews[0].result.fields
    expect(fields).toHaveLength(5)
    for (const field of fields) {
      expect(field.outcome).toBe('matched')
      expect(field.value).toBe(truth[field.field].value)
    }
    await expect.poll(async () => (await getCase()).drafts.some(d => d.valid && d.body.includes('图片已核实')), { timeout: 240000 }).toBe(true)
  }
  await page.reload()
  await page.getByRole('button', { name: '进入跨消息工单' }).click()
  await work.getByLabel('选择问题').selectOption(selected)
  await expect(work.getByRole('img', { name: 'development-images-001.png' })).toBeVisible()
  const attachment = withImage.materials[0].attachment_id
  const anonymous = await browser.newContext()
  expect((await anonymous.request.get(cfg.origin + '/api/attachments/' + attachment + '/content')).status()).toBe(401)
  await anonymous.close()
  await page.screenshot({ path: out + '/browser-failure-path.png', fullPage: true })
  fs.writeFileSync(out + '/recording.json', JSON.stringify({ kind: provenance.provider_mode + '_browser_workflow', passed: true,
    employee_login: true, role: 'admin', upload: true, multiple_turns: true, approval: true,
    failure_recovery: true, real_provider: provenance.real_provider, model_quality_claim: provenance.real_provider, release_eligible: false, provenance,
    checks: ['HttpOnly session', 'pending order retained', 'internal approval', 'new material invalidates old draft', 'failed upload retried', provenance.provider_mode === 'disabled' ? 'model failure not promoted to image fact' : 'five image fields verified against authored records', 'anonymous image denied'],
    elapsed_seconds: (Date.now() - started) / 1000, first, second, withImage: await getCase() }, null, 2))
})
