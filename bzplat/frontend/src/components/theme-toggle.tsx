import { useEffect, useState } from 'react'
import { Laptop, Moon, Sun } from 'lucide-react'
import { useTheme } from 'next-themes'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

const ORDER = ['light', 'dark', 'system'] as const
type Theme = (typeof ORDER)[number]

const LABELS: Record<Theme, string> = {
  light: '浅色模式',
  dark: '深色模式',
  system: '跟随系统',
}

/**
 * 明暗主题切换按钮：三态循环（浅色 → 深色 → 跟随系统）。
 * 太阳=浅色、月亮=深色、显示器=跟随系统。放顶栏。基于 next-themes，SSR 安全。
 */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme()
  // mounted 前不渲染具体图标，避免 hydrate 不匹配（next-themes 在客户端才解析）。
  const [mounted, setMounted] = useState(false)
  useEffect(() => setMounted(true), [])

  const current = (theme as Theme) ?? 'light'
  const next = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length]

  const Icon = current === 'light' ? Sun : current === 'dark' ? Moon : Laptop

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`当前：${LABELS[current]}（点击切换到${LABELS[next]}）`}
          onClick={() => setTheme(next)}
          className={cn(
            'relative inline-flex size-9 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            className
          )}
        >
          {/* mount 前用占位，避免服务端/客户端图标不一致；mount 后按当前主题渲染 */}
          {mounted ? (
            <Icon className="size-[1.15rem]" />
          ) : (
            <span className="size-[1.15rem]" />
          )}
        </button>
      </TooltipTrigger>
      <TooltipContent>{LABELS[current]}，点击切换到{LABELS[next]}</TooltipContent>
    </Tooltip>
  )
}
