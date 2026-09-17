import { HardDrive } from 'lucide-react'

import { useAuth } from '@/components/useAuth'
import { PageFrame, PageHeader } from '@/components/layout'
import { EmptyState } from '@/components/ui/status'
import { UserStoragePanel } from '@/components/UserStoragePanel'

export default function Storage() {
  const { user } = useAuth()
  if (!user) {
    return (
      <PageFrame layout="account-storage-guest">
        <PageHeader title="云存储" description="登录后管理你的模型权重等数据文件。" />
        <EmptyState
          text="请先登录，再管理云存储文件。"
          icon={<HardDrive className="size-5 opacity-50" />}
          className="py-10"
        />
      </PageFrame>
    )
  }
  return (
    <PageFrame layout="account-storage">
      <PageHeader
        title="云存储"
        description="存放模型权重等数据文件；对局开始时自动只读挂载到 /mnt/data 与 /app/data，程序内用 data/文件名 读取。"
      />
      <UserStoragePanel />
    </PageFrame>
  )
}
