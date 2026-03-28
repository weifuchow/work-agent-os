import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatDate(iso: string | null): string {
  if (!iso) return "-"
  return new Date(iso).toLocaleString("zh-CN")
}
