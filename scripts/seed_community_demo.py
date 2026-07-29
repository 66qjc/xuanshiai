"""为小程序调试生成可重复的本地社区演示数据。

本脚本只操作下方定义的演示数据行，不会删除用户数据，
且拒绝在 development/testing 之外的环境运行。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


DEMO_TOPIC_NAMES = (
    "真诚关系观察",
    "周末同城见面",
    "婚恋沟通练习",
    "一个人也要好好生活",
)

DEMO_ACTIVITY_TITLES = (
    "南京周末读书会",
    "上海城市漫步",
    "杭州真诚关系分享会",
)

_TOPICS = (
    ("真诚关系观察", "/static/portraits/profile-woman-community.jpg", 40),
    ("周末同城见面", "/static/portraits/profile-man-sea.jpg", 30),
    ("婚恋沟通练习", "/static/portraits/profile-woman-alt.jpg", 20),
    ("一个人也要好好生活", "/static/portraits/profile-man-light.jpg", 10),
)

# 以下为当前开发数据库中已有的本地测试账号。
# 缺失的账号会被跳过，因此即使本地重置后重新填充也能正常工作。
DEMO_PROFILE_PHONES = (
    "13800001001",
    "13800001002",
    "13998020600",
    "13905000870",
    "17870810285",
    "17870810286",
    "17870810291",
)

_DEMO_PROFILES = (
    {
        "phone": "13800001001",
        "nickname": "林知夏",
        "avatar": "/static/portraits/profile-woman-main.jpg",
        "gender": 2,
        "birthday": "1996-05-18",
        "mbti": "INFJ",
        "constellation": "金牛座",
        "hometown": "南京",
        "residence": "南京",
        "occupation": "内容策划",
        "industry": "文化传媒",
        "education_level": "本科",
        "school": "南京大学",
        "self_intro": "喜欢阅读、散步和把复杂的事情慢慢说清楚。",
        "love_view": "先认真认识，再一起决定关系的节奏。",
        "hobbies": "阅读、散步、城市旅行",
        "interest_tags": ["阅读", "散步", "旅行"],
        "personality_tags": ["耐心", "真诚", "有边界"],
    },
    {
        "phone": "13800001002",
        "nickname": "周予安",
        "avatar": "/static/portraits/profile-man-sea.jpg",
        "gender": 1,
        "birthday": "1995-09-12",
        "mbti": "ISFP",
        "constellation": "处女座",
        "hometown": "扬州",
        "residence": "上海",
        "occupation": "建筑设计",
        "industry": "设计服务",
        "education_level": "硕士",
        "school": "东南大学",
        "self_intro": "工作之外喜欢拍照，也愿意认真听别人讲完一件事。",
        "love_view": "舒服的关系应该让双方都能做自己。",
        "hobbies": "摄影、音乐、城市漫步",
        "interest_tags": ["摄影", "音乐", "散步"],
        "personality_tags": ["温和", "专注", "可靠"],
    },
    {
        "phone": "13998020600",
        "nickname": "顾言澄",
        "avatar": "/static/portraits/profile-woman-community.jpg",
        "gender": 2,
        "birthday": "1997-02-26",
        "mbti": "ENFP",
        "constellation": "双鱼座",
        "hometown": "苏州",
        "residence": "南京",
        "occupation": "心理咨询助理",
        "industry": "教育服务",
        "education_level": "本科",
        "school": "南京师范大学",
        "self_intro": "相信表达和倾听都需要练习，周末常去看展。",
        "love_view": "坦诚说出需要，也尊重对方的边界。",
        "hobbies": "看展、烘焙、读书",
        "interest_tags": ["看展", "烘焙", "读书"],
        "personality_tags": ["开朗", "共情", "细腻"],
    },
    {
        "phone": "13905000870",
        "nickname": "沈知意",
        "avatar": "/static/portraits/profile-woman-alt.jpg",
        "gender": 2,
        "birthday": "1998-11-03",
        "mbti": "INTP",
        "constellation": "天蝎座",
        "hometown": "无锡",
        "residence": "上海",
        "occupation": "产品经理",
        "industry": "互联网",
        "education_level": "本科",
        "school": "上海大学",
        "self_intro": "喜欢把生活过得有条理，也保留一点随性。",
        "love_view": "好的关系需要明确，也需要留有空间。",
        "hobbies": "手帐、电影、羽毛球",
        "interest_tags": ["电影", "运动", "手帐"],
        "personality_tags": ["理性", "独立", "认真"],
    },
    {
        "phone": "17870810285",
        "nickname": "许闻洲",
        "avatar": "/static/portraits/profile-man-light.jpg",
        "gender": 1,
        "birthday": "1994-07-21",
        "mbti": "ISTJ",
        "constellation": "巨蟹座",
        "hometown": "合肥",
        "residence": "杭州",
        "occupation": "高校教师",
        "industry": "教育",
        "education_level": "博士",
        "school": "浙江大学",
        "self_intro": "生活节奏稳定，喜欢骑行和做一顿慢饭。",
        "love_view": "长期关系靠日常的尊重和兑现承诺。",
        "hobbies": "骑行、做饭、历史书",
        "interest_tags": ["骑行", "做饭", "历史"],
        "personality_tags": ["稳重", "守信", "克制"],
    },
    {
        "phone": "17870810286",
        "nickname": "唐婉",
        "avatar": "/static/portraits/profile-woman-main.jpg",
        "gender": 2,
        "birthday": "1996-08-09",
        "mbti": "ESFJ",
        "constellation": "狮子座",
        "hometown": "杭州",
        "residence": "杭州",
        "occupation": "品牌运营",
        "industry": "消费品",
        "education_level": "本科",
        "school": "浙江工商大学",
        "self_intro": "愿意分享生活里的小事，也在学习照顾自己。",
        "love_view": "互相支持，比漂亮的承诺更重要。",
        "hobbies": "瑜伽、咖啡、旅行",
        "interest_tags": ["瑜伽", "咖啡", "旅行"],
        "personality_tags": ["热心", "坦率", "有趣"],
    },
    {
        "phone": "17870810291",
        "nickname": "程野",
        "avatar": "/static/portraits/profile-man-sea.jpg",
        "gender": 1,
        "birthday": "1997-12-15",
        "mbti": "ENFJ",
        "constellation": "射手座",
        "hometown": "南通",
        "residence": "南京",
        "occupation": "公益项目专员",
        "industry": "社会服务",
        "education_level": "本科",
        "school": "河海大学",
        "self_intro": "周末常参加公益活动，想认识同样愿意把日子过好的人。",
        "love_view": "彼此诚实、一起成长，关系才有安全感。",
        "hobbies": "徒步、公益、吉他",
        "interest_tags": ["徒步", "公益", "音乐"],
        "personality_tags": ["主动", "热诚", "有行动力"],
    },
)

_ACTIVITIES = (
    {
        "title": "南京周末读书会",
        "cover": "/static/portraits/profile-woman-community.jpg",
        "type": "同城交流",
        "city": "南京",
        "address": "鼓楼区先锋书店附近",
        "days": 3,
        "start_hour": 14,
        "end_hour": 16,
        "max_people": 12,
        "description": "围绕亲密关系和自我成长，带一本最近正在读的书来聊聊。",
    },
    {
        "title": "上海城市漫步",
        "cover": "/static/portraits/profile-man-sea.jpg",
        "type": "线下见面",
        "city": "上海",
        "address": "徐汇区武康路集合",
        "days": 5,
        "start_hour": 10,
        "end_hour": 12,
        "max_people": 10,
        "description": "慢慢走一段城市街区，认识愿意认真生活的人。",
    },
    {
        "title": "杭州真诚关系分享会",
        "cover": "/static/portraits/profile-woman-alt.jpg",
        "type": "主题分享",
        "city": "杭州",
        "address": "西湖区湖畔空间",
        "days": 7,
        "start_hour": 19,
        "end_hour": 21,
        "max_people": 20,
        "description": "分享一次真实的关系经历，也听听别人如何表达边界和期待。",
    },
)

_DEMO_POSTS = (
    {
        "author_phone": "13800001001",
        "topic_name": "真诚关系观察",
        "content": "最近在练习把自己的想法说清楚，发现坦诚不等于急着给答案。",
        "images": ["/static/portraits/profile-woman-main.jpg"],
        "location": "南京",
        "declaration": "",
        "likes": ("13800001002", "13998020600"),
    },
    {
        "author_phone": "13800001002",
        "topic_name": "周末同城见面",
        "content": "周末去武康路走了一圈，慢慢认识一座城市，也慢慢认识自己。",
        "images": ["/static/portraits/profile-man-sea.jpg"],
        "location": "上海",
        "declaration": "",
        "likes": ("13905000870", "17870810286"),
    },
    {
        "author_phone": "13998020600",
        "topic_name": "婚恋沟通练习",
        "content": "今天的练习是：说需求之前，先确认自己真正想要的是什么。",
        "images": [],
        "location": "南京",
        "declaration": "",
        "likes": ("13800001001", "17870810291"),
    },
    {
        "author_phone": "13905000870",
        "topic_name": "一个人也要好好生活",
        "content": "下班后给自己做了一顿饭。照顾好自己，是建立关系之前很重要的一课。",
        "images": ["/static/portraits/profile-woman-alt.jpg"],
        "location": "上海",
        "declaration": "",
        "likes": ("13800001002", "17870810285"),
    },
    {
        "author_phone": "17870810285",
        "topic_name": "真诚关系观察",
        "content": "比起热闹的开场，我更珍惜可以把一件小事聊完整的下午。",
        "images": [],
        "location": "杭州",
        "declaration": "",
        "likes": ("17870810286", "17870810291"),
    },
    {
        "author_phone": "17870810286",
        "topic_name": "周末同城见面",
        "content": "想发起一次轻松的周末咖啡散步，不赶时间，也不预设结果。",
        "images": ["/static/portraits/profile-woman-main.jpg"],
        "location": "杭州",
        "declaration": "",
        "likes": ("13905000870", "17870810291"),
    },
    {
        "author_phone": "17870810291",
        "topic_name": "婚恋沟通练习",
        "content": "关系里有分歧很正常，重要的是我们愿不愿意继续把话说完。",
        "images": [],
        "location": "南京",
        "declaration": "",
        "likes": ("13998020600", "13800001001"),
    },
    {
        "author_phone": "13800001001",
        "topic_name": "一个人也要好好生活",
        "content": "把周末留给阅读和朋友，生活不是等待谁出现之后才开始。",
        "images": ["/static/portraits/profile-woman-community.jpg"],
        "location": "南京",
        "declaration": "",
        "likes": ("13998020600", "17870810285"),
    },
    {
        "author_phone": "13800001002",
        "topic_name": "真诚关系观察",
        "content": "认识一个人，可以从分享最近喜欢的一首歌开始。",
        "images": [],
        "location": "上海",
        "declaration": "",
        "likes": ("13998020600", "13905000870"),
    },
    {
        "author_phone": "13998020600",
        "topic_name": "一个人也要好好生活",
        "content": "今天走了很远的路，回家时突然觉得，稳定的自己很有力量。",
        "images": [],
        "location": "南京",
        "declaration": "",
        "likes": ("17870810286", "17870810291"),
    },
)

DEMO_POST_CONTENTS = tuple(item["content"] for item in _DEMO_POSTS)
DEMO_POST_DECLARATIONS = tuple(item["declaration"] for item in _DEMO_POSTS)

_DEMO_COMMENTS = (
    (0, "13998020600", "把话说清楚真的需要练习，这句话很有共鸣。"),
    (1, "17870810286", "下次有类似路线可以一起走走。"),
    (2, "13800001002", "先确认自己的需要，再表达给对方，受教了。"),
    (3, "17870810285", "好好吃饭和好好生活都值得被记录。"),
    (4, "17870810291", "我也更喜欢慢一点的聊天。"),
    (5, "13905000870", "不预设结果的见面，反而更轻松。"),
    (6, "13800001001", "愿意把话说完很重要。"),
    (7, "13800001002", "这个周末的安排听起来很舒服。"),
    (0, "17870810286", "同感，坦诚比完美更重要。"),
    (1, "13800001001", "下次可以一起走走，我也在上海。"),
    (2, "13905000870", "确认自己的需求，是关系里很重要的一步。"),
    (3, "13800001001", "一个人也要好好吃饭，说得太好了。"),
    (4, "17870810286", "慢慢聊，不着急，这样的关系更真实。"),
    (5, "13800001002", "不预设结果的相处，反而更让人期待。"),
    (6, "13998020600", "愿意沟通就是关系的开始。"),
    (7, "17870810285", "周末安排听起来很舒服。"),
    (8, "17870810291", "稳定的自己，是关系里最好的基础。"),
    (9, "13800001001", "这首歌我也喜欢，愿意多聊聊。"),
)

_DEMO_SIGNUPS = (
    ("南京周末读书会", "13800001001", "想和大家聊聊最近读到的书。"),
    ("南京周末读书会", "13998020600", "第一次参加，希望认识新朋友。"),
    ("上海城市漫步", "13800001002", "愿意从一段城市散步开始。"),
    ("上海城市漫步", "13905000870", "周末时间合适。"),
    ("杭州真诚关系分享会", "17870810285", "想听听大家如何处理关系边界。"),
    ("杭州真诚关系分享会", "17870810286", "期待线下交流。"),
)

_AVAILABLE_PLANES = (
    {
        "owner_phone": "13998020600",
        "content": "最近在学着把忙碌和休息分开，希望认识也愿意认真生活的人。",
        "city": "南京",
        "tags": ["生活", "真诚"],
    },
    {
        "owner_phone": "13905000870",
        "content": "今天看完一部老电影，想找人交换各自最喜欢的一句台词。",
        "city": "上海",
        "tags": ["电影", "交流"],
    },
    {
        "owner_phone": "17870810285",
        "content": "周末准备骑一段西湖边的路，愿意从一杯咖啡开始慢慢认识。",
        "city": "杭州",
        "tags": ["骑行", "同城"],
    },
    {
        "owner_phone": "17870810286",
        "content": "这周给自己留了一点空白时间，也想听听别人最近的好消息。",
        "city": "杭州",
        "tags": ["日常", "倾听"],
    },
)

DEMO_AVAILABLE_PLANE_CONTENTS = tuple(item["content"] for item in _AVAILABLE_PLANES)

_PLANES = (
    {
        "content": "最近在练习把自己的想法说清楚，也想认识愿意认真聊天的人。",
        "city": "南京",
        "tags": ["真诚", "沟通"],
        "owner_phone": "13800001002",
        "replier_phone": "13800001001",
        "first_message": "看到你的分享了，我也很想聊聊如何把话说清楚。",
    },
    {
        "content": "周末想找一位同城朋友散步，先从交换一首最近喜欢的歌开始。",
        "city": "上海",
        "tags": ["同城", "周末"],
        "owner_phone": "13800001001",
        "replier_phone": "13800001002",
        "first_message": "我最近也在听很多歌，愿意从歌单开始认识。",
    },
)

_BANNERS = (
    {
        "title": "认真关系观察",
        "image_url": "/static/portraits/profile-woman-community.jpg",
        "link_type": "topic",
        "sort": 30,
    },
    {
        "title": "南京周末读书会",
        "image_url": "/static/portraits/profile-man-sea.jpg",
        "link_type": "activity",
        "sort": 20,
    },
    {
        "title": "把想说的话扔出去",
        "image_url": "/static/portraits/profile-woman-alt.jpg",
        "link_type": "plane",
        "sort": 10,
    },
)


def _user_id_by_phone(cursor: Any, phone: str) -> int:
    """根据手机号查询用户 ID，并确认已实名认证。"""
    cursor.execute(
        """SELECT u.id, COALESCE(ua.realname_status, 0) AS realname_status
        FROM users u LEFT JOIN user_auth ua ON ua.user_id = u.id
        WHERE u.phone = %s LIMIT 1""",
        (phone,),
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeError(f"演示数据需要先存在用户 {phone}")
    user_id = int(row["id"] if isinstance(row, dict) else row[0])
    realname_status = int(row["realname_status"] if isinstance(row, dict) else row[1])
    if realname_status != 2:
        raise RuntimeError(f"演示用户 {phone} 还没有实名认证，不能测试社区写操作")
    return user_id


def _existing_user_id(cursor: Any, phone: str) -> int | None:
    """查询用户是否存在（不要求实名认证）。"""
    cursor.execute("SELECT id FROM users WHERE phone=%s LIMIT 1", (phone,))
    row = cursor.fetchone()
    if not row:
        return None
    return int(row["id"] if isinstance(row, dict) else row[0])


def _upsert_demo_profile(cursor: Any, item: dict[str, Any], user_id: int, now: datetime) -> None:
    """写入或更新演示用户的个人资料和实名认证信息。"""
    education_code = {
        "高中": 1,
        "大专": 2,
        "本科": 3,
        "硕士": 4,
        "博士": 5,
    }.get(item["education_level"], 3)
    cursor.execute(
        """UPDATE users SET nickname=%s, avatar=%s, gender=%s, birthday=%s,
        status=1, is_real_name=1, is_married=1, data_complete_rate=90,
        updated_at=%s WHERE id=%s""",
        (
            item["nickname"],
            item["avatar"],
            item["gender"],
            item["birthday"],
            now,
            user_id,
        ),
    )
    cursor.execute(
        """INSERT INTO user_profile
        (user_id, hometown, residence, mbti, constellation, self_intro,
         love_view, hobbies, photos, occupation, industry, education_level,
         interest_tags, personality_tags, online_status, last_active_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)
        ON DUPLICATE KEY UPDATE hometown=VALUES(hometown), residence=VALUES(residence),
        mbti=VALUES(mbti), constellation=VALUES(constellation), self_intro=VALUES(self_intro),
        love_view=VALUES(love_view), hobbies=VALUES(hobbies), photos=VALUES(photos),
        occupation=VALUES(occupation), industry=VALUES(industry),
        education_level=VALUES(education_level), interest_tags=VALUES(interest_tags),
        personality_tags=VALUES(personality_tags), online_status=1, last_active_at=VALUES(last_active_at)""",
        (
            user_id,
            item["hometown"],
            item["residence"],
            item["mbti"],
            item["constellation"],
            item["self_intro"],
            item["love_view"],
            item["hobbies"],
            json.dumps([item["avatar"]], ensure_ascii=False),
            item["occupation"],
            item["industry"],
            education_code,
            json.dumps(item["interest_tags"], ensure_ascii=False),
            json.dumps(item["personality_tags"], ensure_ascii=False),
            now,
        ),
    )
    cursor.execute(
        """INSERT INTO user_auth
        (user_id, school, education, job, realname_status, auth_status, auth_step,
         education_verified, job_verified, verified_at)
        VALUES (%s,%s,%s,%s,2,2,4,1,1,%s)
        ON DUPLICATE KEY UPDATE school=VALUES(school), education=VALUES(education),
        job=VALUES(job), realname_status=2, auth_status=2, auth_step=4,
        education_verified=1, job_verified=1, verified_at=VALUES(verified_at)""",
        (user_id, item["school"], item["education_level"], item["occupation"], now),
    )


def _upsert_post(
    cursor: Any,
    item: dict[str, Any],
    user_ids: dict[str, int],
    topic_ids: dict[str, int],
    now: datetime,
) -> int | None:
    """写入或更新一篇演示帖子，并同步其点赞数和评论数。"""
    user_id = user_ids.get(item["author_phone"])
    topic_id = topic_ids.get(item["topic_name"])
    if user_id is None or topic_id is None:
        return None
    images = json.dumps(item["images"], ensure_ascii=False)
    cursor.execute(
        "SELECT id FROM community_post WHERE user_id=%s AND content=%s LIMIT 1",
        (user_id, item["content"]),
    )
    existing = cursor.fetchone()
    if existing:
        post_id = int(existing["id"] if isinstance(existing, dict) else existing[0])
        cursor.execute(
            """UPDATE community_post SET topic_id=%s, images=%s, location=%s,
            declaration=%s, visibility=0, status=1, updated_at=%s WHERE id=%s""",
            (topic_id, images, item["location"], item["declaration"], now, post_id),
        )
    else:
        cursor.execute(
            """INSERT INTO community_post
            (user_id, topic_id, content, images, location, visibility, declaration, status,
             view_count, like_count, comment_count, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,0,%s,1,0,0,0,%s,%s)""",
            (
                user_id,
                topic_id,
                item["content"],
                images,
                item["location"],
                item["declaration"],
                now,
                now,
            ),
        )
        post_id = int(cursor.lastrowid)

    for phone in item["likes"]:
        liker_id = user_ids.get(phone)
        if liker_id is not None and liker_id != user_id:
            cursor.execute(
                "INSERT IGNORE INTO community_like (user_id, target_id, type) VALUES (%s,%s,1)",
                (liker_id, post_id),
            )
    cursor.execute(
        """UPDATE community_post SET like_count=(SELECT COUNT(*) FROM community_like
        WHERE target_id=%s AND type=1), comment_count=(SELECT COUNT(*) FROM community_comment
        WHERE post_id=%s AND status=1) WHERE id=%s""",
        (post_id, post_id, post_id),
    )
    return post_id


def _upsert_comment(cursor: Any, post_id: int, user_id: int, content: str, now: datetime) -> int:
    """写入或更新一条演示评论。"""
    cursor.execute(
        "SELECT id FROM community_comment WHERE post_id=%s AND user_id=%s AND content=%s LIMIT 1",
        (post_id, user_id, content),
    )
    existing = cursor.fetchone()
    if existing:
        return int(existing["id"] if isinstance(existing, dict) else existing[0])
    cursor.execute(
        """INSERT INTO community_comment
        (post_id, user_id, content, status, created_at)
        VALUES (%s,%s,%s,1,%s)""",
        (post_id, user_id, content, now),
    )
    return int(cursor.lastrowid)


def _upsert_activity_signup(
    cursor: Any,
    activity_id: int,
    user_id: int,
    phone: str,
    nickname: str,
    remark: str,
) -> None:
    """写入或更新活动报名记录（重复报名则取消之前的取消记录）。"""
    cursor.execute(
        """INSERT INTO activity_signup
        (activity_id, user_id, real_name, phone, remark, status)
        VALUES (%s,%s,%s,%s,%s,1)
        ON DUPLICATE KEY UPDATE real_name=VALUES(real_name), phone=VALUES(phone),
        remark=VALUES(remark), status=1, cancel_reason=NULL""",
        (activity_id, user_id, nickname, phone, remark),
    )


def _row_id(cursor: Any) -> int:
    """从游标结果中取第一行第一列作为 ID。"""
    row = cursor.fetchone()
    if not row:
        raise RuntimeError("演示数据写入后没有找到对应记录")
    return int(row["id"] if isinstance(row, dict) else row[0])


def _upsert_activity(cursor: Any, item: dict[str, Any], created_by: int, now: datetime) -> int:
    """写入或更新一场演示线下活动。"""
    start_time = now + timedelta(days=item["days"])
    start_time = start_time.replace(hour=item["start_hour"], minute=0, second=0, microsecond=0)
    end_time = start_time.replace(hour=item["end_hour"])
    deadline = start_time - timedelta(hours=3)
    cursor.execute("SELECT id FROM offline_activity WHERE title = %s LIMIT 1", (item["title"],))
    existing = cursor.fetchone()
    params = (
        item["cover"], item["type"], item["city"], item["address"], start_time,
        end_time, deadline, item["max_people"], item["description"], created_by,
    )
    if existing:
        activity_id = int(existing["id"] if isinstance(existing, dict) else existing[0])
        cursor.execute(
            """UPDATE offline_activity SET cover=%s, type=%s, city=%s, address=%s,
            start_time=%s, end_time=%s, signup_deadline=%s, max_people=%s,
            current_people=0, price=0, status=1, description=%s, created_by=%s
            WHERE id=%s""",
            params + (activity_id,),
        )
        return activity_id
    cursor.execute(
        """INSERT INTO offline_activity
        (title, cover, type, city, address, start_time, end_time, signup_deadline,
         max_people, current_people, price, status, description, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,1,%s,%s)""",
        (item["title"],) + params,
    )
    return int(cursor.lastrowid)


def _upsert_available_plane(
    cursor: Any, item: dict[str, Any], users: dict[str, int], now: datetime
) -> int | None:
    """写入一条可认领的纸飞机（漂流瓶）演示数据。"""
    owner_id = users.get(item["owner_phone"])
    if owner_id is None:
        return None
    expire_at = now + timedelta(days=3)
    tags = json.dumps(item["tags"], ensure_ascii=False)
    cursor.execute(
        "SELECT id FROM paper_plane WHERE user_id=%s AND content=%s AND city=%s LIMIT 1",
        (owner_id, item["content"], item["city"]),
    )
    existing = cursor.fetchone()
    if existing:
        plane_id = int(existing["id"] if isinstance(existing, dict) else existing[0])
        cursor.execute(
            """UPDATE paper_plane SET images=%s, tags=%s, is_anonymous=1,
            reply_count=0, status=1, moderation_status=1, expire_at=%s WHERE id=%s""",
            (json.dumps([], ensure_ascii=False), tags, expire_at, plane_id),
        )
        return plane_id
    cursor.execute(
        """INSERT INTO paper_plane
        (user_id, content, images, city, tags, is_anonymous, reply_count, status,
         moderation_status, expire_at)
        VALUES (%s,%s,%s,%s,%s,1,0,1,1,%s)""",
        (owner_id, item["content"], json.dumps([], ensure_ascii=False), item["city"], tags, expire_at),
    )
    return int(cursor.lastrowid)


def _upsert_plane_and_conversation(
    cursor: Any, item: dict[str, Any], users: dict[str, int], now: datetime
) -> tuple[int, int]:
    """写入或更新一艘关联了回复对话的纸飞机，同时初始化首条消息。"""
    owner_id = users[item["owner_phone"]]
    replier_id = users[item["replier_phone"]]
    expire_at = now + timedelta(days=3)
    tags = json.dumps(item["tags"], ensure_ascii=False)
    cursor.execute(
        "SELECT id FROM paper_plane WHERE user_id=%s AND content=%s AND city=%s LIMIT 1",
        (owner_id, item["content"], item["city"]),
    )
    existing = cursor.fetchone()
    if existing:
        plane_id = int(existing["id"] if isinstance(existing, dict) else existing[0])
        cursor.execute(
            """UPDATE paper_plane SET images=%s, tags=%s, is_anonymous=1,
            status=1, moderation_status=1, expire_at=%s WHERE id=%s""",
            (json.dumps([], ensure_ascii=False), tags, expire_at, plane_id),
        )
    else:
        cursor.execute(
            """INSERT INTO paper_plane
            (user_id, content, images, city, tags, is_anonymous, reply_count, status,
             moderation_status, expire_at)
            VALUES (%s,%s,%s,%s,%s,1,0,1,1,%s)""",
            (owner_id, item["content"], json.dumps([], ensure_ascii=False), item["city"], tags, expire_at),
        )
        plane_id = int(cursor.lastrowid)

    cursor.execute(
        "SELECT id FROM paper_plane_conversation WHERE plane_id=%s AND replier_id=%s LIMIT 1",
        (plane_id, replier_id),
    )
    existing_conversation = cursor.fetchone()
    if existing_conversation:
        conversation_id = int(
            existing_conversation["id"] if isinstance(existing_conversation, dict) else existing_conversation[0]
        )
        cursor.execute(
            """UPDATE paper_plane_conversation SET status=1, last_message=%s,
            last_message_at=%s, owner_unread=1, replier_unread=0 WHERE id=%s""",
            (item["first_message"], now, conversation_id),
        )
    else:
        cursor.execute(
            """INSERT INTO paper_plane_conversation
            (plane_id, owner_id, replier_id, status, last_message, last_message_at,
             owner_unread, replier_unread)
            VALUES (%s,%s,%s,1,%s,%s,1,0)""",
            (plane_id, owner_id, replier_id, item["first_message"], now),
        )
        conversation_id = int(cursor.lastrowid)

    cursor.execute(
        """SELECT id FROM paper_plane_message
        WHERE conversation_id=%s AND from_user_id=%s AND content=%s LIMIT 1""",
        (conversation_id, replier_id, item["first_message"]),
    )
    if not cursor.fetchone():
        cursor.execute(
            """INSERT INTO paper_plane_message
            (conversation_id, from_user_id, content, type) VALUES (%s,%s,%s,1)""",
            (conversation_id, replier_id, item["first_message"]),
        )
    return plane_id, conversation_id


def _upsert_banner(cursor: Any, item: dict[str, Any], target_id: int) -> int:
    """写入或更新社区页面的轮播 Banner 配置。"""
    cursor.execute(
        "SELECT id FROM config_banner WHERE title=%s AND position='community' LIMIT 1",
        (item["title"],),
    )
    existing = cursor.fetchone()
    params = (
        item["image_url"],
        item["link_type"],
        str(target_id) if target_id > 0 else None,
        item["sort"],
    )
    if existing:
        banner_id = int(existing["id"] if isinstance(existing, dict) else existing[0])
        cursor.execute(
            """UPDATE config_banner SET image_url=%s, link_type=%s, link_value=%s,
            sort=%s, position='community', is_active=1, start_at=NULL, end_at=NULL
            WHERE id=%s""",
            params + (banner_id,),
        )
        return banner_id
    cursor.execute(
        """INSERT INTO config_banner
        (title, image_url, link_type, link_value, sort, position, is_active, start_at, end_at)
        VALUES (%s,%s,%s,%s,%s,'community',1,NULL,NULL)""",
        (item["title"],) + params,
    )
    return int(cursor.lastrowid)


def seed_community_demo(
    connection: Any = None,
    *,
    environment: str | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """填充社区演示数据的主入口。

    依次写入：演示用户资料 → 话题与参与者 → 帖子与点赞 →
    评论 → 线下活动与报名 → 纸飞机与对话 → 轮播 Banner。
    所有修改在一条事务中提交，失败时回滚。

    参数:
        connection: 数据库连接；为空时自动创建。
        environment: 环境标识；仅在 development/testing 下执行。
        now: 基准时间；为空时取当前 UTC。

    返回:
        各类型数据的写入数量统计。
    """
    env = (environment or os.getenv("ENV") or os.getenv("ENVIRONMENT") or "development").lower()
    if env not in {"development", "testing"}:
        raise RuntimeError("社区演示数据只允许在 development/testing 环境执行")

    owned_connection = connection is None
    if owned_connection:
        import pymysql
        from database_setup_marriage import get_db_config

        connection = pymysql.connect(
            **get_db_config(),
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )

    current_time = (now or datetime.now(UTC)).replace(tzinfo=None)
    cursor = connection.cursor()
    try:
        profile_by_phone = {item["phone"]: item for item in _DEMO_PROFILES}
        user_ids: dict[str, int] = {}
        profile_count = 0
        for item in _DEMO_PROFILES:
            user_id = _existing_user_id(cursor, item["phone"])
            if user_id is None:
                continue
            user_ids[item["phone"]] = user_id
            _upsert_demo_profile(cursor, item, user_id, current_time)
            profile_count += 1

        required_plane_phones = {
            phone for item in _PLANES for phone in (item["owner_phone"], item["replier_phone"])
        }
        for phone in required_plane_phones:
            if phone not in user_ids:
                user_ids[phone] = _user_id_by_phone(cursor, phone)

        topic_ids: list[int] = []
        topic_ids_by_name: dict[str, int] = {}
        for name, icon, sort in _TOPICS:
            cursor.execute(
                """INSERT INTO community_topic (name, icon, sort, is_active)
                VALUES (%s,%s,%s,1)
                ON DUPLICATE KEY UPDATE icon=VALUES(icon), sort=VALUES(sort), is_active=1""",
                (name, icon, sort),
            )
            cursor.execute("SELECT id FROM community_topic WHERE name=%s LIMIT 1", (name,))
            topic_id = _row_id(cursor)
            topic_ids.append(topic_id)
            topic_ids_by_name[name] = topic_id
            for user_id in user_ids.values():
                cursor.execute(
                    "INSERT IGNORE INTO community_topic_participant (topic_id, user_id) VALUES (%s,%s)",
                    (topic_id, user_id),
                )

        activity_ids_by_title = {
            item["title"]: _upsert_activity(cursor, item, user_ids["13800001001"], current_time)
            for item in _ACTIVITIES
        }
        activity_ids = list(activity_ids_by_title.values())

        post_ids_by_index: dict[int, int] = {}
        for index, item in enumerate(_DEMO_POSTS):
            post_id = _upsert_post(cursor, item, user_ids, topic_ids_by_name, current_time)
            if post_id is not None:
                post_ids_by_index[index] = post_id

        comment_count = 0
        for index, phone, content in _DEMO_COMMENTS:
            post_id = post_ids_by_index.get(index)
            user_id = user_ids.get(phone)
            if post_id is None or user_id is None:
                continue
            _upsert_comment(cursor, post_id, user_id, content, current_time)
            comment_count += 1

        signup_count = 0
        for activity_title, phone, remark in _DEMO_SIGNUPS:
            activity_id = activity_ids_by_title.get(activity_title)
            user_id = user_ids.get(phone)
            profile = profile_by_phone.get(phone)
            if activity_id is None or user_id is None or profile is None:
                continue
            _upsert_activity_signup(
                cursor,
                activity_id,
                user_id,
                phone,
                profile["nickname"],
                remark,
            )
            signup_count += 1
        for activity_id in activity_ids:
            cursor.execute(
                """UPDATE offline_activity SET current_people=(SELECT COUNT(*) FROM activity_signup
                WHERE activity_id=%s AND status IN (0,1)) WHERE id=%s""",
                (activity_id, activity_id),
            )

        available_plane_ids = [
            plane_id
            for item in _AVAILABLE_PLANES
            if (plane_id := _upsert_available_plane(cursor, item, user_ids, current_time)) is not None
        ]
        plane_conversations = [
            _upsert_plane_and_conversation(cursor, item, user_ids, current_time)
            for item in _PLANES
        ]
        banner_ids = [
            _upsert_banner(cursor, _BANNERS[0], topic_ids[0]),
            _upsert_banner(cursor, _BANNERS[1], activity_ids[0]),
            _upsert_banner(cursor, _BANNERS[2], 0),
        ]

        # 用户隐私设置（privacy API 必需）
        for uid in user_ids.values():
            cursor.execute(
                """INSERT IGNORE INTO user_privacy (user_id) VALUES (%s)""",
                (uid,),
            )

        # 用户初始积分（200 分，可兑换 4 次纸飞机）
        INITIAL_POINTS = 200
        for uid in user_ids.values():
            cursor.execute(
                """SELECT id FROM user_points WHERE user_id = %s AND type = 0 LIMIT 1""",
                (uid,),
            )
            if not cursor.fetchone():
                cursor.execute(
                    """INSERT INTO user_points (user_id, type, amount, balance, description)
                       VALUES (%s, 0, %s, %s, '演示数据初始积分')""",
                    (uid, INITIAL_POINTS, INITIAL_POINTS),
                )

        # 用户城市设置（按 residence 分配）
        city_map = {
            "13800001001": "南京",
            "13800001002": "上海",
            "13998020600": "南京",
            "13905000870": "上海",
            "17870810285": "杭州",
            "17870810286": "杭州",
            "17870810291": "南京",
        }
        city_code_map = {
            "南京": "320100",
            "上海": "310100",
            "杭州": "330100",
        }
        for phone, uid in user_ids.items():
            city = city_map.get(phone)
            if city:
                code = city_code_map.get(city, "")
                cursor.execute(
                    """UPDATE user_profile SET community_city_name = %s,
                       community_city_code = %s WHERE user_id = %s""",
                    (city, code, uid),
                )

        # 演示通知数据
        _DEMO_NOTIFICATIONS = (
            ("like", "有人点赞了你的动态", "最近在练习把自己的想法说清楚…", "post", post_ids_by_index.get(0), "13800001002"),
            ("comment", "有人评论了你的动态", "把话说清楚真的需要练习，这句话很有共鸣。", "post", post_ids_by_index.get(0), "13998020600"),
            ("follow", "新关注", "林雨桐关注了你", "user", user_ids.get("13800001002"), "13800001002"),
            ("system", "实名认证通过", "你的实名认证已通过审核，可以参与社区互动了。", "", 0, ""),
            ("activity", "活动报名成功", "你已成功报名「南京周末读书会」", "activity", activity_ids_by_title.get("南京周末读书会"), ""),
            ("like", "有人点赞了你的动态", "周末去武康路走了一圈…", "post", post_ids_by_index.get(1), "13905000870"),
            ("comment", "有人评论了你的动态", "下次有类似路线可以一起走走。", "post", post_ids_by_index.get(1), "17870810286"),
            ("system", "欢迎加入宣誓爱", "欢迎来到宣誓爱，开始你的真诚关系之旅。", "", 0, ""),
        )
        for ntype, title, content, target_type, target_id, actor_phone in _DEMO_NOTIFICATIONS:
            actor_id = user_ids.get(actor_phone) if actor_phone else None
            recipient_id = next(
                (uid for pid, uid in user_ids.items() if pid == "13800001001"),
                list(user_ids.values())[0],
            )
            if target_type == "post" and target_id is None:
                continue
            cursor.execute(
                """INSERT INTO notification (user_id, notification_type, title, content,
                   target_type, target_id, actor_user_id, is_read, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE id = id""",
                (
                    recipient_id, ntype, title, content,
                    target_type, target_id or 0, actor_id,
                    0, current_time - timedelta(hours=1),
                ),
            )

        # 积分兑换产品（纸飞机次数）
        _POINT_PRODUCTS = (
            ("paper_plane_throw_chance", "纸飞机投放次数", "right", 50, None, 0),
            ("paper_plane_catch_chance", "纸飞机捡拾次数", "right", 50, None, 1),
        )
        for code, name, product_type, points_cost, value, sort in _POINT_PRODUCTS:
            cursor.execute(
                """INSERT INTO config_point_product (code, name, product_type, points_cost, value, stock, sort, is_active)
                   VALUES (%s, %s, %s, %s, %s, NULL, %s, 1)
                   ON DUPLICATE KEY UPDATE name = VALUES(name), points_cost = VALUES(points_cost), is_active = 1""",
                (code, name, product_type, points_cost, value, sort),
            )

        connection.commit()
        return {
            "topics": len(topic_ids),
            "activities": len(activity_ids),
            "profiles": profile_count,
            "posts": len(post_ids_by_index),
            "comments": comment_count,
            "signups": signup_count,
            "paper_planes": len(available_plane_ids) + len(plane_conversations),
            "available_paper_planes": len(available_plane_ids),
            "conversations": len(plane_conversations),
            "banners": len(banner_ids),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        if owned_connection:
            connection.close()


if __name__ == "__main__":
    result = seed_community_demo()
    print("社区演示数据已写入: " + json.dumps(result, ensure_ascii=False))
