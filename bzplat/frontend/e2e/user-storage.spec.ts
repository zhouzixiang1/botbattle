import test, { expect } from '@playwright/test'

import { loginThroughUi, monitorBrowser } from './helpers'

const USER = process.env.BZ_E2E_USER || 'tester1'
const FILE_NAME = 'e2e_weights.bin'
const PAYLOAD = Buffer.from('e2e storage payload')

test('settings storage tab uploads, lists and deletes a file', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)
  await page.goto('/#/settings')

  await page.getByRole('tab', { name: '云存储' }).click()
  const quota = page.locator('[data-storage-quota]')
  await expect(quota).toBeVisible()
  await expect(quota).toContainText('/ 256.0 MB')

  // 前次运行的残留先清掉，保证本用例自包含。
  const row = page.locator(`[data-storage-file="${FILE_NAME}"]`)
  if (await row.count()) {
    await page.locator(`[data-storage-delete="${FILE_NAME}"]`).click()
    await page.getByRole('button', { name: '确认', exact: true }).click()
    await expect(row).toHaveCount(0)
  }

  await page.locator('[data-storage-upload]').click()
  await page
    .locator('input[aria-label="选择要上传的文件"]')
    .setInputFiles({ name: FILE_NAME, mimeType: 'application/octet-stream', buffer: PAYLOAD })
  await expect(row).toBeVisible()
  await expect(row).toContainText('19 B')
  await expect(page.getByText(`已上传 ${FILE_NAME}`)).toBeVisible()

  await page.locator(`[data-storage-delete="${FILE_NAME}"]`).click()
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await expect(row).toHaveCount(0)
  await expect(page.getByText(`已删除 ${FILE_NAME}`)).toBeVisible()

  await monitor.expectClean()
})

test('storage upload rejects an empty file without leaving rows', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)
  await page.goto('/#/settings')
  await page.getByRole('tab', { name: '云存储' }).click()
  await expect(page.locator('[data-storage-quota]')).toBeVisible()

  await page.locator('[data-storage-upload]').click()
  await page
    .locator('input[aria-label="选择要上传的文件"]')
    .setInputFiles({ name: 'e2e_empty.bin', mimeType: 'application/octet-stream', buffer: Buffer.alloc(0) })
  await expect(page.getByText('不能上传空文件')).toBeVisible()
  await expect(page.locator('[data-storage-file="e2e_empty.bin"]')).toHaveCount(0)
  await monitor.expectClean()
})
