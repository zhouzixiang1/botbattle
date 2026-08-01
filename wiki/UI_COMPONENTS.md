# 前端 UI 组件库（src/components/ui/）

全项目唯一的 UI 组件抽象层，基于 [shadcn/ui](https://ui.shadcn.com)（new-york 风格）+ Radix UI 原语 + lucide-react 图标。

> 基础设施见 [UI_FOUNDATION.md](./UI_FOUNDATION.md)。

## 使用原则

1. **新代码一律用 `@/components/ui/*`**，不要再写内联的 `rounded-lg border border-input...` 重复样式。
2. **图标统一 lucide-react**（按需导入），禁止 emoji。
3. **颜色一律用语义 token**（`bg-background` / `text-primary` / `border-border`），不裸 hex。
4. 暗色通过 token 自动适配；仅在需要浅暗不同表现时用 `dark:` 前缀。

## 组件清单

### 表单类
| 组件 | 来源 | 用途 |
|------|------|------|
| `Button` | shadcn | 主按钮。`variant="default\|secondary\|outline\|ghost\|destructive\|link"`，`size="default\|sm\|lg\|icon"` |
| `Input` | shadcn | 文本输入（替换原 31 处重复样式） |
| `Textarea` | shadcn | 多行输入 |
| `Label` | shadcn | 表单标签 |
| `Select` | shadcn | 下拉选择（Radix Select） |
| `Switch` | shadcn | 开关（通知偏好等） |

### 展示类
| 组件 | 来源 | 用途 |
|------|------|------|
| `Card` / `CardHeader` / `CardTitle` / `CardDescription` / `CardContent` / `CardFooter` | shadcn | 卡片容器（替换 `.card` 类 + 手写卡片头） |
| `Badge` | shadcn | 徽章。`variant="default\|secondary\|destructive\|outline"` |
| `Avatar` | shadcn | 头像（用户主页、评论） |
| `Separator` | shadcn | 分隔线 |
| `Table` / `TableHeader` / `TableBody` / `TableRow` / `TableHead` / `TableCell` | shadcn | 表格（统一排行榜/Bot列表/对局列表） |
| `Tabs` / `TabsList` / `TabsTrigger` / `TabsContent` | shadcn | 选项卡（替换 5+ 处手写 tab 样式） |
| `Skeleton` | shadcn | 骨架屏加载 |
| `Tooltip` | shadcn | 悬浮提示（需在 App 包 `TooltipProvider`，已就位） |

### 反馈/交互类
| 组件 | 来源 | 用途 |
|------|------|------|
| `Dialog` / `DialogContent` / `DialogHeader` / `DialogTitle` / `DialogFooter` | shadcn | 模态框（替换裸 div Modal） |
| `DropdownMenu` | shadcn | 下拉菜单（主题切换、用户菜单） |
| `Popover` | shadcn | 气泡（NotificationBell） |
| `Command` | shadcn (cmdk) | 命令面板/全局搜索 |
| `Toaster` (sonner) | shadcn | 全局 Toast 提示（已挂载在 App，用 `import { toast } from 'sonner'`） |

### 项目封装类（`status.tsx` / `metric-card.tsx`）
| 组件 | 用途 |
|------|------|
| `EmptyState` | 空状态（Inbox 图标 + 文案，居中） |
| `Loading` | 加载中（Loader2 旋转图标） |
| `ErrorMsg` | 错误提示（AlertCircle + destructive 色） |
| `RefreshBtn` | 刷新按钮（RefreshCw 图标） |
| `StatusBadge` | 状态徽章（自动按对局/赛事/邮件状态映射颜色 + 中文标签） |
| `MetricCard` | 指标卡（label + 数值 + hint + danger + icon） |

## 使用示例

```tsx
import { Button } from '@/components/ui/button'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { EmptyState, Loading, StatusBadge } from '@/components/ui/status'
import { toast } from 'sonner'

<Card>
  <CardHeader><CardTitle>Bot 信息</CardTitle></CardHeader>
  <CardContent>
    <Input placeholder="Bot 名称" />
    <Button variant="default" size="sm">保存</Button>
    <StatusBadge status="running" />
    <EmptyState text="暂无对局" />
    {loading ? <Loading /> : <div>...</div>}
    <Button onClick={() => toast.success('已保存')}>提交</Button>
  </CardContent>
</Card>
```

## 迁移说明

原 `src/pages/admin/ui.tsx` 的 `Card/MetricCard/EmptyState/Loading/ErrorMsg/StatusBadge/RefreshBtn` 将在页面改造 PR 中逐步替换为本组件库（`Card`→shadcn `Card`，其余→`status.tsx`/`metric-card.tsx`），消除 admin 与前台的组件分裂。
