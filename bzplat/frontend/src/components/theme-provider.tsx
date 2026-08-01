import {
  ThemeProvider as NextThemesProvider,
  type ThemeProviderProps,
} from 'next-themes'

/**
 * 暗色模式 Provider（基于 next-themes）。
 * - 在 <html> 上切换 .dark class（配合 index.css 的 @custom-variant dark）
 * - localStorage 持久化，跟随系统偏好（attribute="class" defaultTheme="system"）
 * - SSR 安全，无首屏闪烁（next-themes 内置 suppressHydrationWarning）
 */
export function ThemeProvider({ children, ...props }: ThemeProviderProps) {
  return <NextThemesProvider {...props}>{children}</NextThemesProvider>
}
