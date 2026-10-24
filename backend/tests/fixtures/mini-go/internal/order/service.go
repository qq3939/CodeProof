package order

import (
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"example.com/minishop/internal/cache"
)

var ErrOutOfStock = errors.New("out of stock")

type Service struct {
	db    *sql.DB
	cache *cache.Cache
}

func NewService(c *cache.Cache) *Service {
	return &Service{cache: c}
}

// HandleCreate 是下单接口的 handler
func (s *Service) HandleCreate(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ProductID string `json:"product_id"`
		Qty       int    `json:"qty"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	if err := s.createWithRetry(req.ProductID, req.Qty); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	s.cache.Invalidate("product:" + req.ProductID)
	w.WriteHeader(http.StatusCreated)
}

func (s *Service) HandleProduct(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	if v, ok := s.cache.Get("product:" + id); ok {
		_ = json.NewEncoder(w).Encode(v)
		return
	}
	http.NotFound(w, r)
}

// createWithRetry 遇到死锁之类的临时错误时按指数退避重试；库存不足不重试
func (s *Service) createWithRetry(productID string, qty int) error {
	backoff := 50 * time.Millisecond
	var err error
	for attempt := 0; attempt < 3; attempt++ {
		err = s.createOrder(productID, qty)
		if err == nil || errors.Is(err, ErrOutOfStock) {
			return err
		}
		time.Sleep(backoff)
		backoff *= 2
	}
	return err
}

// createOrder 在一个事务里扣库存、写订单：任何一步失败都 Rollback，两步要么都成功要么都不做
func (s *Service) createOrder(productID string, qty int) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	res, err := tx.Exec("UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?", qty, productID, qty)
	if err != nil {
		_ = tx.Rollback()
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		_ = tx.Rollback()
		return ErrOutOfStock
	}
	if _, err := tx.Exec("INSERT INTO orders(product_id, qty) VALUES (?, ?)", productID, qty); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}
