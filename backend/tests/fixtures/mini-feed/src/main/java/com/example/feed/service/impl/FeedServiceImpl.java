package com.example.feed.service.impl;

import com.example.feed.entity.Feed;
import com.example.feed.mapper.FeedMapper;
import com.example.feed.service.FeedService;
import java.util.List;
import java.util.concurrent.TimeUnit;
import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.cache.annotation.CacheEvict;
import org.springframework.cache.annotation.Cacheable;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

@Service
public class FeedServiceImpl implements FeedService {

    public static final String LIKE_KEY = "feed:like:count";

    private final FeedMapper feedMapper;
    private final StringRedisTemplate redisTemplate;
    private final RedissonClient redissonClient;
    private final RabbitTemplate rabbitTemplate;

    public FeedServiceImpl(FeedMapper feedMapper, StringRedisTemplate redisTemplate,
                           RedissonClient redissonClient, RabbitTemplate rabbitTemplate) {
        this.feedMapper = feedMapper;
        this.redisTemplate = redisTemplate;
        this.redissonClient = redissonClient;
        this.rabbitTemplate = rabbitTemplate;
    }

    @Override
    public List<Feed> listLatest(int limit) {
        return feedMapper.selectLatest(limit);
    }

    @Override
    @Cacheable(value = "feed", key = "#id")
    public Feed getFeed(Long id) {
        return feedMapper.selectById(id);
    }

    @Override
    @CacheEvict(value = "feed", key = "#id")
    public long like(Long id) {
        RLock lock = redissonClient.getLock("feed:like:lock:" + id);
        try {
            if (!lock.tryLock(1, 5, TimeUnit.SECONDS)) {
                throw new IllegalStateException("too busy");
            }
            Double score = redisTemplate.opsForZSet().incrementScore(LIKE_KEY, String.valueOf(id), 1);
            rabbitTemplate.convertAndSend("like.exchange", "like", id);
            return score == null ? 0 : score.longValue();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(e);
        } finally {
            if (lock.isHeldByCurrentThread()) {
                lock.unlock();
            }
        }
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW, rollbackFor = Exception.class)
    public void saveLikeCount(Long id, long count) {
        feedMapper.updateLikeCount(id, count);
    }
}
