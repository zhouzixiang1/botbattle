# 前端设计系统基础（UI Foundation）

本文档描述 botbattle 前端（`bzplat/frontend/`）的设计 token 体系、暗色模式、组件库规范与取材库。是所有前端视觉工作的基础。

> 技术栈：React 19 + Vite 8 + **Tailwind CSS v4**（CSS-first）+ shadcn/ui + Radix UI + lucide-react + recharts。

## 路径别名

全项目统一用 `@/` 别名指向 `src/`：

```ts
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
```

- 配置在 `tsconfig.app.json`（`baseUrl` + `paths`）与 `vite.config.ts`（`resolve.alias`）。
- **新代码一律用 `@/`，不要再加新的相对路径导入**。旧相对路径在页面改造 PR 中渐进迁移。

## 设计 Token 体系（src/index.css）

采用 **shadcn v4 的 OKLCH 双主题 token**，映射到 emerald 品牌色系。

### 语义变量（在 :root 浅色 / .dark 暗色中定义）
| 变量 | 用途 | 浅色 | 暗色 |
|------|------|------|------|
| `--background` / `--foreground` | 页面底 / 正文 | 近白 / 深灰蓝 | 深蓝灰 / 近白 |
| `--card` / `--card-foreground` | 卡片底 | 纯白 | 深一档蓝灰 |
| `--primary` / `--primary-foreground` | 品牌主色（emerald） | emerald-600 | emerald-500（提亮） |
| `--secondary` / `--muted` / `--accent` | 次要/静音/强调 | 低饱和绿灰 | 深绿灰 |
| `--destructive` | 危险/错误 | 红 | 浅红 |
| `--border` / `--input` / `--ring` | 边框/输入/聚焦环 | slate-200 | 白色 10%/15% 透明 |
| `--chart-1..5` | 图表色阶 | emerald 系 + amber | 对应提亮 |

### 使用方式（Tailwind utility）
```tsx
<div className="bg-background text-foreground">          // 页面底+正文
<div className="bg-card border border-border rounded-lg"> // 卡片
<button className="bg-primary text-primary-foreground">   // 主按钮
<span className="text-muted-foreground">                  // 次要文字
```

通过 `@theme inline` 把 `--background` 桥接到 `--color-background`，使 `bg-background` 等 utility 可用。

### 品牌色别名（brand-* 仍可用）
为平滑迁移，保留了 `--color-brand-50..900`（OKLCH emerald 色阶）。**新代码优先用 `primary` 语义 token，`brand-*` 仅在需要具体色阶时用**。

### 特殊保留
- `--color-felt-400..700`：扑克桌深绿毡面，**仅牌桌用，不随主题切换**（`.felt-table` 类）。
- `--font-display`（Source Serif 4 衬线）：仅 `.page-title` / logo；`--font-mono`（JetBrains Mono）：代码/比分。

## 暗色模式

基于 **next-themes**（`src/components/theme-provider.tsx`）：
- `attribute="class"`：在 `<html>` 上切换 `.dark` class。
- `defaultTheme="light"` + `enableSystem`：默认浅色，但跟随系统偏好（用户可手动覆盖）。
- `disableTransitionOnChange`：切换瞬间无过渡闪烁。
- localStorage 持久化（next-themes 内置）。
- SSR 安全、无首屏闪烁。

切换组件：`src/components/theme-toggle.tsx`（太阳/月亮 lucide 图标，放顶栏）。

在 `App.tsx` 最外层包裹：
```tsx
<ThemeProvider attribute="class" defaultTheme="light" enableSystem disableTransitionOnChange>
  ...
</ThemeProvider>
```

### 写暗色变体的规则
- **优先用语义 token**（`bg-background` 在浅/暗自动切换），这样**无需写 `dark:` 变体**。
- 只在确实需要浅暗不同表现时才用 `dark:` 前缀（如棋盘外框）。
- 暗色下的边框统一用 `oklch(1 0 0 / 10%)`（白色低透明），已在 token 定义好。

## 组件库（src/components/ui/）

基于 shadcn/ui（copy-paste，我们持有代码）+ Radix UI 原语。**这是全项目唯一的 UI 组件抽象层**。

- 风格：`new-york`（components.json 配置）。
- 统一 `radix-ui` 包（非独立 `@radix-ui/react-*`）。
- 图标统一用 **lucide-react**（按需导入）。

组件清单见 [UI_COMPONENTS.md](./UI_COMPONENTS.md)。

## 取材库（refs/ui-refs/，不入库）

已 clone 的参考模板（`.gitignore` 忽略，仅本地取材）：
| 目录 | 项目 | 取什么 |
|------|------|--------|
| `refs/ui-refs/shadcn-admin` | satnaing/shadcn-admin（★12.8k, MIT） | 页面骨架、暗色实现、数据表格 |
| `refs/ui-refs/ui` | shadcn-ui/ui 官方（★120k, MIT） | 组件 DNA、token 标准、Chart |
| `refs/ui-refs/react-tournament-brackets` | g-loot（★302, MIT） | 赛事对阵图 |
| `refs/ui-refs/recharts` | recharts（★27.4k, MIT） | 图表 |
| `refs/ui-refs/magicui` | magicui（★21.8k, MIT） | 首页 hero 动效、number-ticker |

## 关键约束
- **无 emoji**：全项目图标统一 lucide-react。
- **无紫色/米色**：刻意规避 AI 默认审美（emerald 品牌色系）。
- 浅色为主、暗色对等：浅色是默认，暗色必须达到同等可用性。
- **不破坏功能**：路由、API、业务逻辑不动，只改视觉/结构/组件。

## 代码分割（PR-F7）

页面组件用 `React.lazy` + `Suspense` 按需加载（`src/components/shell/app-shell.tsx`）：
- 主包 `index.js`：~365KB（115KB gzip）—— 含框架 + Shell + 首页。
- 每个页面独立 chunk（2-20KB），访问时才加载。
- 重依赖隔离：recharts 只在 BotDetail chunk（346KB），不进主包。
- 懒加载 fallback：`PageFallback`（Loader2 旋转图标）。

## 响应式（PR-F7）

- **断点**：`sm`(640) / `md`(768) / `lg`(1024) / `xl`(1280) / `max-w-screen-2xl`(1536)。
- **顶栏**：`md` 以上横向图标导航；`md` 以下汉堡菜单 → Sheet 侧滑抽屉。
- **表格**：窄屏隐藏次要列（`hidden sm:table-cell` / `hidden md:table-cell` / `hidden lg:table-cell`），或用 `overflow-x-auto` 横向滚动。
- **卡片网格**：`grid-cols-2 sm:grid-cols-3 lg:grid-cols-4` 自适应列数。
- **表单**：单栏，`max-w-md` 居中。

## 暗色全覆盖（PR-F7）

- 全项目 className 用语义 token（自动明暗切换），**禁裸 hex / 禁 `slate-*`/`brand-*`/`error-*` 等硬编码**。
- 例外（刻意固定配色，不跟随主题）：
  - 扑克桌 `.felt-table` 深绿毡面（始终深色）。
  - 五子棋木色棋盘、点格棋配色。
  - `LogsTab` 日志查看器（始终深色终端风格，level 颜色针对深底校准）。
  - 段位徽章（`tier-badge.tsx`，浅暗双色已内置 `dark:` 变体）。

## 可访问性（PR-F7）

- 所有交互元素有 `aria-label`（图标按钮）/ `title`。
- `focus-visible:ring` 聚焦环（Button/Input 等组件内置）。
- 图标按钮带 `sr-only` 文本（如 ThemeToggle）。
- 对比度遵循 WCAG AA（OKLCH token 已校准）。
