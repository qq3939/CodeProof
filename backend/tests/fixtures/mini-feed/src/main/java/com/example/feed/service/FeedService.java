package com.example.feed.service;

import com.example.feed.entity.Feed;
import java.util.List;

public interface FeedService {
    List<Feed> listLatest(int limit);

    Feed getFeed(Long id);

    long like(Long id);
}
