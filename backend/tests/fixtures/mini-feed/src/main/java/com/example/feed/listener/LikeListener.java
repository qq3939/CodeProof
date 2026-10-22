package com.example.feed.listener;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Component;

@Component
public class LikeListener {
    private static final Logger log = LoggerFactory.getLogger(LikeListener.class);

    @RabbitListener(queues = "like.queue")
    public void onLike(Long feedId) {
        notifyAuthor(feedId);
    }

    @Async("feedExecutor")
    public void notifyAuthor(Long feedId) {
        log.info("notify author of feed {}", feedId);
    }
}
