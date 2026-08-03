/**
 * 管理端共享 UI 组件。
 * 现统一从共享组件库 @/components/ui/* re-export，消除 admin 与前台的组件分裂。
 * 保留原导出名以兼容现有 admin 页面 import。
 */
export { Card } from '@/components/ui/card'
export { MetricCard } from '@/components/ui/metric-card'
export { EmptyState, Loading, ErrorMsg, StatusBadge, RefreshBtn } from '@/components/ui/status'
export { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
