import { useEffect, useState } from 'react'
import { Laptop, Moon, Sun, type LucideIcon } from 'lucide-react'
import { useTheme } from 'next-themes'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'

const ORDER = ['light', 'dark', 'system'] as const
type Theme = (typeof ORDER)[number]

const LABELS: Record<Theme, string> = {
  light: '浅色',
  dark: '深色',
  system: '跟随系统',
}

const ICONS: Record<Theme, LucideIcon> = {
  light: Sun,
  dark: Moon,
  system: Laptop,
}

/**
 * 明暗主题切换：下拉三选一（浅色 / 深色 / 跟随系统）。
 * 按钮图标反映当前主题；太阳=浅色、月亮=深色、显示器=跟随系统。
 * 基于 next-themes，SSR 安全。
 */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme()
  // mounted 前不渲染具体图标，避免 hydrate 不匹配（next-themes 在客户端才解析）。
  const [mounted, setMounted] = useState(false)
  useEffect(() => setMounted(true), [])

  const current = ((theme as Theme) ?? 'light')
  const TriggerIcon = mounted ? ICONS[current] : null

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="切换主题"
          className={cn(
            'relative inline-flex size-9 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [@media(pointer:coarse)]:size-11',
            className
          )}
        >
          {/* mount 前用占位，避免服务端/客户端图标不一致；mount 后按当前主题渲染 */}
          {TriggerIcon ? (
            <TriggerIcon className="size-[1.15rem]" />
          ) : (
            <span className="size-[1.15rem]" />
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-32">
        <DropdownMenuRadioGroup
          value={mounted ? current : undefined}
          onValueChange={(value) => setTheme(value as Theme)}
        >
          {ORDER.map((item) => {
            const Icon = ICONS[item]
            return (
              <DropdownMenuRadioItem
                key={item}
                value={item}
                className="min-h-11 [@media(pointer:fine)_and_(min-width:40rem)]:min-h-8"
              >
                <Icon aria-hidden="true" className="size-4" />
                {LABELS[item]}
              </DropdownMenuRadioItem>
            )
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
