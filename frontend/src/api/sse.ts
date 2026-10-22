import { fetchEventSource } from '@microsoft/fetch-event-source'

export interface StreamEvent {
  event: string
  data: any
}

/**
 * 读取 POST 请求的流式响应（作答、要提示）。
 * EventSource 只能发 GET、不能带请求体，所以用 fetch-event-source（SPEC 8.7）。
 */
export async function postStream(url: string, body: unknown, onEvent: (e: StreamEvent) => void): Promise<void> {
  const ctrl = new AbortController()
  await fetchEventSource('/api' + url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    credentials: 'include',
    signal: ctrl.signal,
    openWhenHidden: true, // 切到别的标签页时不要断开重连，否则会重复提交作答
    async onopen(resp) {
      if (!resp.ok) {
        let msg = `请求失败（${resp.status}）`
        try {
          msg = (await resp.json()).error?.message || msg
        } catch {
          /* 不是 JSON */
        }
        throw new Error(msg)
      }
    },
    onmessage(m) {
      if (!m.event) return
      onEvent({ event: m.event, data: m.data ? JSON.parse(m.data) : null })
    },
    onerror(err) {
      throw err // 不自动重试：作答不是幂等的
    },
  })
}

/** 分析进度：GET + 同源 Cookie，用原生 EventSource 即可。 */
export function jobEvents(snapshotId: string, onProgress: (d: any) => void): EventSource {
  const es = new EventSource(`/api/jobs/${snapshotId}/events`, { withCredentials: true })
  es.addEventListener('progress', (ev) => {
    const d = JSON.parse((ev as MessageEvent).data)
    onProgress(d)
    if (d.status === 'ready' || d.status === 'failed') es.close()
  })
  es.onerror = () => es.close()
  return es
}
