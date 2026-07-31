import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** 合并 Tailwind class，后者覆盖前者冲突项（cn("p-2", "p-4") → "p-4"） */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
