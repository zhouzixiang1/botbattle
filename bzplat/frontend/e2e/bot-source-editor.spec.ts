import test, { expect, type Locator, type Page } from '@playwright/test'
import { readFile } from 'node:fs/promises'

import { loginThroughUi, monitorBrowser, versionRow } from './helpers'

const USER = process.env.BZ_E2E_USER || 'tester1'

// samples/callbot.py 是通过上传预检的真实 Holdem 样例（response 0 = call/check）。
const CALLBOT_PATH = new URL('../../../samples/callbot.py', import.meta.url)

async function callbotSource(): Promise<string> {
  return readFile(CALLBOT_PATH, 'utf8')
}

async function chooseProgramType(page: Page, label: string) {
  const form = page.locator('form').filter({ has: page.locator('#upload-name') })
  await form.getByRole('combobox').nth(1).click()
  await page.getByRole('option', { name: label, exact: true }).click()
}

async function submitCreateUpload(page: Page) {
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === '/api/bots'
  ))
  const form = page.locator('form').filter({ has: page.locator('#upload-name') })
  await form.getByRole('button', { name: '上传', exact: true }).click()
  const response = await responsePromise
  expect(response.status(), await response.text()).toBe(200)
  return response
}

async function openVersionManager(page: Page, row: Locator, botName: string) {
  const trigger = row.getByRole('button', { name: `管理 ${botName}`, exact: true })
  await trigger.focus()
  await trigger.press('Enter')
  const versionItem = page.getByRole('menuitem', { name: '版本管理', exact: true })
  await expect(versionItem).toBeVisible()
  await page.keyboard.press('Enter')
  const manager = page.getByRole('dialog', { name: `版本管理 ${botName}`, exact: true })
  await expect(manager).toBeVisible()
  return manager
}

test('online editor creates a runnable python source bot', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)
  await page.goto('/#/my-bots')

  const uniqueName = `editor_py_${Date.now().toString(36)}`
  await page.locator('#upload-name').fill(uniqueName)
  await chooseProgramType(page, 'Python 源码')

  await page.locator('[data-testid="upload-mode-editor"]').click()
  const editor = page.locator('#upload-source')
  await expect(editor).toBeVisible()
  await expect(page.getByText('将保存为 main.py')).toBeVisible()
  await expect(page.locator('#upload-entry')).toHaveCount(0)
  await editor.fill(await callbotSource())

  const link = page.getByRole('link', { name: uniqueName, exact: true })
  await submitCreateUpload(page)
  await expect(link).toBeVisible()

  await monitor.expectClean()
})

test('single .py file uploads directly without zipping', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)
  await page.goto('/#/my-bots')

  const uniqueName = `single_py_${Date.now().toString(36)}`
  await page.locator('#upload-name').fill(uniqueName)
  await chooseProgramType(page, 'Python 源码')

  await page.locator('#upload-file').setInputFiles({
    name: 'my_solver.py',
    mimeType: 'text/x-python',
    buffer: Buffer.from(await callbotSource(), 'utf8'),
  })
  await expect(page.getByText('my_solver.py')).toBeVisible()

  const link = page.getByRole('link', { name: uniqueName, exact: true })
  await submitCreateUpload(page)
  await expect(link).toBeVisible()

  await monitor.expectClean()
})

test('online editor caps pasted code at 2 MB client-side', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)
  await page.goto('/#/my-bots')

  await page.locator('#upload-name').fill(`big_py_${Date.now().toString(36)}`)
  await chooseProgramType(page, 'Python 源码')
  await page.locator('[data-testid="upload-mode-editor"]').click()

  const editor = page.locator('#upload-source')
  await editor.fill('# padding\n' + 'x'.repeat(2 * 1024 * 1024))
  await expect(page.getByText('在线编辑代码超过 2 MB，请改用文件上传')).toBeVisible()

  let posted = false
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/bots') {
      posted = true
    }
  })
  const form = page.locator('form').filter({ has: page.locator('#upload-name') })
  await form.getByRole('button', { name: '上传', exact: true }).click()
  await expect(page.getByText('在线编辑代码超过 2 MB，请改用文件上传')).toBeVisible()
  expect(posted).toBe(false)

  await monitor.expectClean()
})

test('switching upload mode resets stale selections', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)
  await page.goto('/#/my-bots')
  await page.locator('#upload-name').fill(`reset_py_${Date.now().toString(36)}`)
  await chooseProgramType(page, 'Python 源码')

  // 编辑器输入后切回文件模式：编辑器与入口隐藏，提交前必须重新选文件。
  await page.locator('[data-testid="upload-mode-editor"]').click()
  await page.locator('#upload-source').fill(await callbotSource())
  await page.locator('[data-testid="upload-mode-file"]').click()
  await expect(page.locator('#upload-source')).toHaveCount(0)
  await expect(page.locator('#upload-file')).toBeVisible()
  await expect(page.getByText('未选择文件', { exact: true })).toBeVisible()

  // 编辑器模式下入口输入框不可见（入口固定为规范名）。
  await page.locator('[data-testid="upload-mode-editor"]').click()
  await expect(page.locator('#upload-entry')).toHaveCount(0)
  await expect(page.locator('#upload-source')).toBeVisible()

  await monitor.expectClean()
})

test('version manager uploads v2 through the online editor', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)
  await page.goto('/#/my-bots')

  const uniqueName = `ver_py_${Date.now().toString(36)}`
  await page.locator('#upload-name').fill(uniqueName)
  await chooseProgramType(page, 'Python 源码')
  await page.locator('[data-testid="upload-mode-editor"]').click()
  await page.locator('#upload-source').fill(await callbotSource())
  await submitCreateUpload(page)

  const link = page.getByRole('link', { name: uniqueName, exact: true })
  await expect(link).toBeVisible()
  const botId = Number(await link.getAttribute('href').then((href) => href?.split('/').pop()))
  expect(Number.isInteger(botId)).toBe(true)

  const row = page.locator('li').filter({ has: link })
  const manager = await openVersionManager(page, row, uniqueName)
  // 对话框内程序类型默认 ELF，先切到 Python 源码才会出现上传方式切换。
  await manager.getByRole('combobox').nth(1).click()
  await page.getByRole('option', { name: 'Python 源码', exact: true }).click()
  await manager.locator('[data-testid="upload-mode-editor"]').click()
  const versionEditor = manager.locator('#ver-source')
  await expect(versionEditor).toBeVisible()
  await versionEditor.fill(`${await callbotSource()}\n# v2: editor roundtrip\n`)
  await expect(manager.getByText('将保存为 main.py')).toBeVisible()

  const versionPromise = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === `/api/bots/${botId}/versions`
  ))
  await manager.getByRole('button', { name: '上传新版本', exact: true }).click()
  const response = await versionPromise
  expect(response.status(), await response.text()).toBe(200)
  await expect(versionRow(manager, 2)).toBeVisible()

  await monitor.expectClean()
})
