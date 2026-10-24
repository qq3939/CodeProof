package order

import "testing"

// 测试文件不应该进考点：通用兜底会把 _test.go 排除在关键词搜索之外
func TestRetryStopsOnOutOfStock(t *testing.T) {
	t.Skip("fixture only")
}
