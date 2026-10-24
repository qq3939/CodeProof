package main

import (
	"log"
	"net/http"
	"time"

	"example.com/minishop/internal/cache"
	"example.com/minishop/internal/order"
)

func main() {
	c := cache.New(30 * time.Second)
	svc := order.NewService(c)

	mux := http.NewServeMux()
	mux.HandleFunc("/orders", svc.HandleCreate)
	mux.HandleFunc("/products", svc.HandleProduct)

	// 读写超时都要设：不设的话慢客户端会一直占着连接
	server := &http.Server{
		Addr:         ":8080",
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
	}
	log.Fatal(server.ListenAndServe())
}
