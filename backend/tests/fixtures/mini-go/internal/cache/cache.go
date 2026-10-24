package cache

import (
	"sync"
	"time"
)

type entry struct {
	value     any
	expiresAt time.Time
}

// Cache 是带过期时间的本地缓存；多个请求会同时读写，所以用 sync.Mutex 保护 map
type Cache struct {
	mu    sync.Mutex
	ttl   time.Duration
	items map[string]entry
}

func New(ttl time.Duration) *Cache {
	return &Cache{ttl: ttl, items: make(map[string]entry)}
}

func (c *Cache) Get(key string) (any, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.items[key]
	if !ok || time.Now().After(e.expiresAt) {
		delete(c.items, key)
		return nil, false
	}
	return e.value, true
}

func (c *Cache) Set(key string, value any) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.items[key] = entry{value: value, expiresAt: time.Now().Add(c.ttl)}
}

// Invalidate 在商品库存变化后调用，否则缓存里会一直是旧库存
func (c *Cache) Invalidate(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.items, key)
}
