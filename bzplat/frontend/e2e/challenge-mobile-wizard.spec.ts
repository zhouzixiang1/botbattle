// 挑战页窄屏三步向导（对局 → 座位 → 确认）。
// 桌面（≥1280px）保持既有单屏表单，不受本用例影响；此处只验证窄屏行为：
// 分步显隐、往返导航、分区保持挂载（不重拉 /api/games）与提交门禁。
import test, { expect } from '@playwright/test'

import { loginThroughUi, monitorBrowser } from './helpers'

const USER = process.env.BZ_E2E_USER || 'tester1'

test('challenge form becomes a three-step wizard below 1280px', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await loginThroughUi(page, USER)
  await page.goto('/#/challenge')

  const form = page.getByTestId('challenge-form')
  await expect(form).toBeVisible()

  // 向导导航在第 1 步；桌面不渲染该导航。
  const nav = form.getByTestId('challenge-wizard-nav')
  await expect(nav).toBeVisible()
  await expect(form.getByTestId('challenge-prev')).toBeDisabled()

  // 第 1 步：游戏与时限可见，座位与提交不可见。
  const game = form.getByRole('combobox', { name: '游戏' })
  await expect(game).toBeVisible()
  await expect(form.getByTestId('challenge-my-seat')).toBeHidden()
  await expect(form.getByRole('button', { name: '开始对局', exact: true })).toBeHidden()

  // 分区保持挂载（CSS 隐藏）：游戏选择器存在但不重复拉取。
  let gamesReads = 0
  page.on('response', (response) => {
    if (new URL(response.url()).pathname === '/api/games') gamesReads += 1
  })
  await form.getByTestId('challenge-next').click()
  await expect(form.getByTestId('challenge-my-seat')).toBeVisible()
  await expect(game).toBeHidden()
  await expect(form.getByRole('button', { name: '我亲自上场', exact: true })).toBeVisible()

  // 第 3 步：提交门禁——未选 Bot 时按钮禁用并给出提示；返回第 2 步不丢状态。
  await form.getByTestId('challenge-next').click()
  const submit = form.getByRole('button', { name: '开始对局', exact: true })
  await expect(submit).toBeVisible()
  await expect(submit).toBeDisabled()
  await expect(form.getByText('请为双方选择 Bot')).toBeVisible()

  // 进入第 3 步时「下一步」卸载，焦点移到「上一步」。
  await expect(form.getByTestId('challenge-prev')).toBeFocused()
  await form.getByTestId('challenge-prev').click()
  await expect(form.getByTestId('challenge-my-seat')).toBeVisible()
  await form.getByTestId('challenge-prev').click()
  await expect(game).toBeVisible()
  expect(gamesReads).toBe(0)

  // 390px 无横向溢出；导航按钮触控目标 ≥44px。
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  for (const testId of ['challenge-prev', 'challenge-next']) {
    const box = await form.getByTestId(testId).boundingBox()
    expect(box?.height).toBeGreaterThanOrEqual(44)
  }

  await monitor.expectClean()
})

test('challenge form stays single-screen at desktop width', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.setViewportSize({ width: 1280, height: 800 })
  await loginThroughUi(page, USER)
  await page.goto('/#/challenge')

  const form = page.getByTestId('challenge-form')
  await expect(form).toBeVisible()
  await expect(form.getByTestId('challenge-wizard-nav')).toHaveCount(0)
  await expect(form.getByRole('combobox', { name: '游戏' })).toBeVisible()
  await expect(form.getByTestId('challenge-my-seat')).toBeVisible()
  await expect(form.getByRole('button', { name: '开始对局', exact: true })).toBeVisible()

  await monitor.expectClean()
})
