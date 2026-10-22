package com.example.feed.mapper;

import com.example.feed.entity.Feed;
import java.util.List;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

@Mapper
public interface FeedMapper {
    @Select("select * from feed order by id desc limit #{limit}")
    List<Feed> selectLatest(@Param("limit") int limit);

    @Select("select * from feed where id = #{id}")
    Feed selectById(@Param("id") Long id);

    @Update("update feed set like_count = #{count} where id = #{id}")
    int updateLikeCount(@Param("id") Long id, @Param("count") long count);
}
