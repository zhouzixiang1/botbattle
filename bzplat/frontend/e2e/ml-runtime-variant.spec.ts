// Python 源码上传的「运行库」变体选择器（v1.6）。
// 选择器只在程序类型为 Python 源码时出现；ML 项文案指向云存储权重工作流。
// 完整 ml 变体上传→预检→对局链路由发布候选的真实 docker 探针覆盖。
import { expect, test } from '@playwright/test'

import { PASSWORD, loginThroughUi } from './helpers'

const USER = process.env.BZ_E2E_USER || 'tester1'

test('runtime variant selector only shows for python source and offers std/ml', async ({ page }) => {
  await loginThroughUi(page, USER, PASSWORD)
  // HashRouter：必须走 hash 路由；裸 /mybots 会落到首页。
  await page.goto('/#/my-bots')
  await expect(page.getByText('上传新 Bot')).toBeVisible()

  // 程序类型默认 ELF：不出现「运行库」选择器
  await expect(page.getByText('运行库', { exact: true })).toBeHidden()

  // 切到 Python 源码：出现运行库选择器，默认标准库
  // （Radix 在表单内还会渲染一份隐藏的原生 option，定位器必须锚定可见触发器。）
  const typeSelect = page.getByRole('combobox').filter({ hasText: 'ELF 程序' })
  await typeSelect.click()
  await page.getByRole('option', { name: 'Python 源码' }).click()
  await expect(page.getByText('运行库', { exact: true })).toBeVisible()
  await expect(page.getByRole('combobox').filter({ hasText: '标准库（默认）' })).toBeVisible()

  // 打开运行库下拉：两个选项，ML 项列明库清单
  await page.getByRole('combobox').filter({ hasText: '标准库' }).click()
  await expect(page.getByRole('option', { name: 'ML 库（numpy · scipy · onnxruntime · torch CPU）' })).toBeVisible()
  await page.keyboard.press('Escape')

  // 切回 ELF：此时程序类型框显示「Python 源码」，按当前值定位再选回 ELF
  await page.getByRole('combobox').filter({ hasText: 'Python 源码' }).click()
  await page.getByRole('option', { name: 'ELF 程序' }).click()
  await expect(page.getByText('运行库', { exact: true })).toBeHidden()
})
