# mini-shop

一个很小的 Go 下单服务，只用来测试 CodeProof 的通用兜底：Go 没有专门的符号索引和框架画像，考点要由模型根据目录、README 和源码挑出来。

- `main.go`：HTTP 入口和路由
- `internal/order`：下单，失败时按退避重试，用数据库事务保证扣库存和写订单一起成功
- `internal/cache`：带过期时间的本地缓存，用互斥锁保护
