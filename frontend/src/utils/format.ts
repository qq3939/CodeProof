import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

export const fmtTime = (t?: string | null) => (t ? dayjs(t).format('YYYY-MM-DD HH:mm') : '-')
export const fromNow = (t?: string | null) => (t ? dayjs(t).fromNow() : '-')
export const fmtBytes = (n?: number | null) =>
  n == null ? '-' : n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${(n / 1024).toFixed(0)} KB`
export const shortSha = (s?: string | null) => (s ? s.slice(0, 8) : '')
export const scoreType = (s: number) => (s >= 80 ? 'success' : s >= 60 ? 'warning' : 'danger')
