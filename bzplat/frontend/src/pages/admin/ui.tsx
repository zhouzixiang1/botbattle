/**
 * 管理端共享 UI 组件。
 * 现统一从共享组件库 @/components/ui/* re-export，消除 admin 与前台的组件分裂。
 * 保留原导出名以兼容现有 admin 页面 import。
 */
export { Card } from '@/components/ui/card'
export { MetricCard } from '@/components/ui/metric-card'
export { EmptyState, Loading, ErrorMsg, StatusBadge, RefreshBtn } from '@/components/ui/status'
export { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
export { Switch } from '@/components/ui/switch'
export { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

/**
 * admin 表单 input 的统一 className（含隐藏原生 number spinner）。
 * 跨浏览器 number input 的上下箭头外观不一，统一去掉；其余样式与历史保持一致。
 */
export const inp =
  'mt-1 block w-full rounded-lg border border-input bg-background px-3 py-2 appearance-none [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:m-0 [&::-webkit-outer-spin-button]:m-0'
