import { defineStore } from 'pinia'
import { ref } from 'vue'
import { http } from '../api/http'
import type { Me } from '../api/types'

export const useUserStore = defineStore('user', () => {
  const me = ref<Me | null>(null)
  const loaded = ref(false)

  async function fetchMe(): Promise<Me | null> {
    try {
      const { data } = await http.get<Me>('/me', { silent: true } as any)
      me.value = data
    } catch {
      me.value = null
    }
    loaded.value = true
    return me.value
  }

  async function logout() {
    await http.post('/auth/logout')
    me.value = null
    location.href = '/login'
  }

  return { me, loaded, fetchMe, logout }
})
