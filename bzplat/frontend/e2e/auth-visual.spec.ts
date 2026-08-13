import { expect, test, type Locator, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const AUTH_PAGES = [
  { path: '/login', layout: 'auth-login', title: '登录', headerAction: '注册' },
  { path: '/register', layout: 'auth-register', title: '注册账号', headerAction: '登录' },
  { path: '/reset-password', layout: 'auth-reset-password', title: '重置密码', headerAction: '登录' },
  { path: '/verify-email', layout: 'auth-verify-email', title: '验证邮箱', headerAction: '登录' },
] as const

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 960 },
  { name: 'mobile', width: 390, height: 844 },
] as const

const CAPTCHA_IMAGE = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='

async function mockAnonymousAuth(page: Page) {
  // These pages must be inspectable without creating users, consuming a captcha,
  // or changing a session. Keep the routes exact so a new unexpected auth call is
  // still reported by monitorBrowser rather than silently absorbed here.
  await page.route('**/api/auth/me', async (route) => {
    await route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '未登录' }),
    })
  })
  await page.route('**/api/auth/captcha', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        captcha_id: 'auth-visual-captcha',
        image_base64: CAPTCHA_IMAGE,
        ttl: 300,
      }),
    })
  })
}

async function visibleBox(locator: Locator, label: string) {
  const box = await locator.boundingBox()
  expect(box, `${label} must have a visible bounding box`).not.toBeNull()
  if (!box) throw new Error(`${label} has no visible bounding box`)
  return box
}

async function expectNoRootOverflow(page: Page, label: string) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  )
  expect(overflow, `${label} overflows the root by ${overflow}px`).toBeLessThanOrEqual(1)
}

async function expectUnifiedAuthLayout(
  page: Page,
  item: (typeof AUTH_PAGES)[number],
  viewport: (typeof VIEWPORTS)[number],
) {
  const header = page.locator('[data-shell-header]')
  const main = page.locator('#main-content')
  const frame = main.locator(`[data-page-layout="${item.layout}"]`)
  const pageHeader = frame.locator('[data-slot="page-header"]')
  const heading = pageHeader.getByRole('heading', { level: 1, name: item.title, exact: true })
  const card = frame.locator('[data-slot="card"]').first()

  await expect(frame).toBeVisible()
  await expect(heading).toBeVisible()
  await expect(card).toBeVisible()

  // The brand belongs to the shell alone. Keeping it out of main prevents the
  // doubled logo/title treatment that made the auth pages visually noisy.
  await expect(page.getByText('Botbattle', { exact: true })).toHaveCount(1)
  await expect(main.getByText('Botbattle', { exact: true })).toHaveCount(0)
  await expect(main.locator('h1')).toHaveCount(1)

  const headingBox = await visibleBox(heading, `${item.path} h1`)
  const headingRegionBox = await visibleBox(pageHeader, `${item.path} heading region`)
  const cardBox = await visibleBox(card, `${item.path} first card`)
  // The global main excludes the classic scrollbar gutter.  Auth content must
  // center within that usable region, which is the axis users actually see.
  const mainBox = await visibleBox(main, `${item.path} main`)
  const viewportCenter = mainBox.x + mainBox.width / 2
  const headingCenter = headingBox.x + headingBox.width / 2
  const cardCenter = cardBox.x + cardBox.width / 2
  expect(Math.abs(headingCenter - viewportCenter), `${item.path} h1 center offset`).toBeLessThanOrEqual(2)
  expect(Math.abs(cardCenter - headingCenter), `${item.path} card/h1 center-axis offset`).toBeLessThanOrEqual(2)

  const headingToCard = cardBox.y - (headingRegionBox.y + headingRegionBox.height)
  expect(headingToCard, `${item.path} heading-to-card gap`).toBeGreaterThanOrEqual(20)
  expect(headingToCard, `${item.path} heading-to-card gap`).toBeLessThanOrEqual(28)

  const action = header.getByRole('link', { name: item.headerAction, exact: true })
  await expect(action).toBeVisible()
  if (item.path === '/login') {
    await expect(header.getByRole('link', { name: '登录', exact: true })).toHaveCount(0)
  }

  if (viewport.name === 'mobile') {
    // Project interaction policy: in-page form actions are at least 40px; fixed
    // shell actions use the stricter 44px target. Inputs retain their compact
    // visual density but are not used as the target-size source of truth here.
    const formButtons = frame.locator('button')
    expect(await formButtons.count(), `${item.path} has form actions`).toBeGreaterThan(0)
    for (let index = 0; index < await formButtons.count(); index += 1) {
      const box = await visibleBox(formButtons.nth(index), `${item.path} form button ${index}`)
      expect(box.height, `${item.path} form button ${index} height`).toBeGreaterThanOrEqual(40)
    }
    const shellActions = header.locator('button, [data-slot="button"]')
    for (let index = 0; index < await shellActions.count(); index += 1) {
      const box = await visibleBox(shellActions.nth(index), `${item.path} shell action ${index}`)
      expect(box.height, `${item.path} shell action ${index} height`).toBeGreaterThanOrEqual(44)
    }
  }

  await expectNoRootOverflow(page, `${item.path}/${viewport.name}`)
}

for (const viewport of VIEWPORTS) {
  test(`anonymous auth pages share a clean centered shell (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize(viewport)
    const monitor = monitorBrowser(page)
    await mockAnonymousAuth(page)

    for (const item of AUTH_PAGES) {
      await page.goto(`/#${item.path}`)
      await expectUnifiedAuthLayout(page, item, viewport)

      if (item.path === '/login') {
        const captchaImageButton = page.getByRole('button', { name: '刷新图形验证码', exact: true })
        const captchaAnswer = page.getByPlaceholder('图中字符或算式结果')
        const imageBox = await visibleBox(captchaImageButton, 'login captcha image button')
        const answerBox = await visibleBox(captchaAnswer, 'login captcha answer input')
        expect(Math.abs(imageBox.height - answerBox.height), 'login captcha controls have equal height').toBeLessThanOrEqual(1)
      }
    }

    await monitor.expectClean()
  })
}

test('login remains aligned and surfaced after switching to dark mode', async ({ page }) => {
  await page.setViewportSize(VIEWPORTS[0])
  await page.emulateMedia({ colorScheme: 'light' })
  const monitor = monitorBrowser(page)
  await mockAnonymousAuth(page)
  await page.goto('/#/login')

  const frame = page.locator('[data-page-layout="auth-login"]')
  const heading = frame.getByRole('heading', { level: 1, name: '登录', exact: true })
  const card = frame.locator('[data-slot="card"]')
  const lightHeading = await visibleBox(heading, 'light-mode login h1')
  const lightCard = await visibleBox(card, 'light-mode login card')

  await page.getByRole('button', { name: /^当前：浅色模式/ }).click()
  await expect(page.locator('html')).toHaveClass(/dark/)
  await expect(heading).toBeVisible()
  await expect(card).toBeVisible()

  const darkHeading = await visibleBox(heading, 'dark-mode login h1')
  const darkCard = await visibleBox(card, 'dark-mode login card')
  expect(Math.abs(darkHeading.x - lightHeading.x), 'dark mode h1 horizontal drift').toBeLessThanOrEqual(2)
  expect(Math.abs(darkHeading.y - lightHeading.y), 'dark mode h1 vertical drift').toBeLessThanOrEqual(2)
  expect(Math.abs(darkCard.x - lightCard.x), 'dark mode card horizontal drift').toBeLessThanOrEqual(2)
  expect(Math.abs(darkCard.y - lightCard.y), 'dark mode card vertical drift').toBeLessThanOrEqual(2)

  const surfaces = await page.evaluate(() => {
    const shell = document.querySelector<HTMLElement>('[data-app-shell]')
    const visibleCard = document.querySelector<HTMLElement>('[data-page-layout="auth-login"] [data-slot="card"]')
    if (!shell || !visibleCard) throw new Error('login surfaces are missing')
    return {
      shell: getComputedStyle(shell).backgroundColor,
      card: getComputedStyle(visibleCard).backgroundColor,
      cardBorder: getComputedStyle(visibleCard).borderColor,
    }
  })
  expect(surfaces.card, 'dark card surface is painted').not.toBe('rgba(0, 0, 0, 0)')
  expect(surfaces.cardBorder, 'dark card border is painted').not.toBe('rgba(0, 0, 0, 0)')
  expect(surfaces.card, 'dark card remains distinct from the page surface').not.toBe(surfaces.shell)
  await expectNoRootOverflow(page, 'login/dark-desktop')
  await monitor.expectClean()
})
