package com.example.feed.job;

import com.example.feed.service.impl.FeedServiceImpl;
import java.util.Set;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ZSetOperations;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
public class CountFlushJob {

    private final StringRedisTemplate redisTemplate;
    private final FeedServiceImpl feedService;
    private final Object flushLock = new Object();

    public CountFlushJob(StringRedisTemplate redisTemplate, FeedServiceImpl feedService) {
        this.redisTemplate = redisTemplate;
        this.feedService = feedService;
    }

    @Scheduled(fixedRate = 60000)
    public void flush() {
        synchronized (flushLock) {
            Set<ZSetOperations.TypedTuple<String>> tuples =
                redisTemplate.opsForZSet().rangeWithScores(FeedServiceImpl.LIKE_KEY, 0, -1);
            if (tuples == null) {
                return;
            }
            for (ZSetOperations.TypedTuple<String> t : tuples) {
                feedService.saveLikeCount(Long.valueOf(t.getValue()), t.getScore().longValue());
            }
        }
    }
}
