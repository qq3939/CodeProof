import axios, { AxiosError } from 'axios'
import { ElMessage } from 'element-plus'

// 所有接口都走同源的 /api，认证靠 HttpOnly Cookie（前端拿不到 token，XSS 也偷不走）
export const http = axios.create({ baseURL: '/api', withCredentials: true, timeout: 120000 })

http.interceptors.response.use(
  (r) => r,
  (err: AxiosError<{ error?: { message?: string; code?: string } }>) => {
    const status = err.response?.status
    const msg = err.response?.data?.error?.message || err.message
    if (status === 401) {
      if (!location.pathname.startsWith('/login')) {
        location.href = '/login?next=' + encodeURIComponent(location.pathname)
      }
    } else if (!(err.config as { silent?: boolean } | undefined)?.silent) {
      ElMessage.error(msg)
    }
    return Promise.reject(err)
  },
)

export function apiError(e: unknown): string {
  const ax = e as AxiosError<{ error?: { message?: string } }>
  return ax.response?.data?.error?.message || (e as Error).message || '请求失败'
}
