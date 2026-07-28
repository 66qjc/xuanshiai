"""Schemas for community posts, comments, topics, activities and paper planes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

PositiveInt = Annotated[int, Field(ge=1)]


CITY_CODE_PATTERN = r"^(?:[0-9]{4}|[0-9]{6})$"


class CommunityPostCreate(BaseModel):
    content: str = Field(default="", max_length=2000)
    images: list[str] = Field(default_factory=list, max_length=9)
    video: str | None = Field(default=None, max_length=500)
    image_media_ids: list[PositiveInt] = Field(default_factory=list, max_length=9)
    video_media_id: int | None = Field(default=None, ge=1)
    location: str | None = Field(default=None, max_length=128)
    topic_id: int | None = Field(default=None, ge=1)
    visibility: Literal[0, 1, 2] = 0
    declaration: Literal["", "内容包含虚构演绎", "内容包含广告推广", "内容可能引起不适"] = ""

    @model_validator(mode="after")
    def validate_media_and_content(self) -> "CommunityPostCreate":
        text = (self.content or "").strip()
        self.content = text
        image_ids = list(self.image_media_ids or [])
        has_image_ids = len(image_ids) > 0
        has_video_id = self.video_media_id is not None
        has_images = len(self.images or []) > 0
        has_video = bool(self.video)
        if has_image_ids and has_video_id:
            raise ValueError("图片和视频不能同时存在")
        if has_images and has_video:
            raise ValueError("图片和视频不能同时存在")
        if (has_image_ids or has_images) and (has_video_id or has_video):
            raise ValueError("图片和视频不能同时存在")
        if len(image_ids) > 9:
            raise ValueError("图片最多 9 张")
        if not text and not has_image_ids and not has_video_id and not has_images and not has_video:
            raise ValueError("正文与媒体不能同时为空")
        return self


class CommunityPostResponse(BaseModel):
    id: int
    user_id: int
    nickname: str | None
    avatar: str | None
    content: str
    images: list[str]
    video: str | None
    location: str | None
    visibility: Literal[0, 1, 2] = 0
    declaration: Literal["", "内容包含虚构演绎", "内容包含广告推广", "内容可能引起不适"] = ""
    topic_id: int | None = None
    topic_name: str | None = None
    like_count: int
    comment_count: int
    collect_count: int = 0
    is_liked: bool
    is_collected: bool = False
    is_followed: bool = False
    gender: int | None = None
    age: int | None = None
    mbti: str | None = None
    school: str | None = None
    hometown: str | None = None
    residence: str | None = None
    realname_status: int = 0
    created_at: datetime


class CommunityPostPage(BaseModel):
    items: list[CommunityPostResponse]
    page: int
    page_size: int
    total: int


class CommunityCommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    parent_id: int | None = Field(default=None, ge=1)


class CommunityCommentResponse(BaseModel):
    id: int
    post_id: int
    user_id: int
    nickname: str | None
    avatar: str | None
    parent_id: int | None
    root_id: int | None = None
    target_comment_id: int | None = None
    target_user_id: int | None = None
    reply_to_user: str | None = None
    content: str
    like_count: int
    is_liked: bool = False
    reply_count: int = 0
    is_deleted: bool = False
    can_delete: bool = False
    replies: list["CommunityCommentResponse"] = Field(default_factory=list)
    created_at: datetime


class CommentCursorPage(BaseModel):
    items: list[CommunityCommentResponse]
    next_cursor: str | None = None
    has_more: bool = False


CommunityCommentResponse.model_rebuild()


class CommunityTopicResponse(BaseModel):
    id: int
    name: str
    icon: str | None
    sort: int
    post_count: int
    participant_count: int
    heat: int
    joined: bool
    created_at: datetime | None = None


class CommunityTopicPage(BaseModel):
    items: list[CommunityTopicResponse]
    page: int
    page_size: int
    total: int


class CommunityTopicDetailResponse(BaseModel):
    topic: CommunityTopicResponse
    posts: CommunityPostPage
    sort: Literal["hot", "latest"]


class CommunityTopicJoinResponse(BaseModel):
    success: bool = True
    joined: bool = True
    topic_id: int
    participant_count: int = 0


class ActivitySignupCreate(BaseModel):
    real_name: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=20)
    remark: str | None = Field(default=None, max_length=255)


class ActivityResponse(BaseModel):
    id: int
    title: str
    cover: str | None
    type: str | None
    city: str | None
    address: str | None
    start_time: datetime
    end_time: datetime
    signup_deadline: datetime | None
    max_people: int
    current_people: int
    price: float
    status: int
    status_text: str
    description: str | None
    my_status: int | None
    my_status_text: str
    created_at: datetime


class ActivityPage(BaseModel):
    items: list[ActivityResponse]
    page: int
    page_size: int
    total: int


class ActivitySignupResponse(BaseModel):
    success: bool = True
    activity_id: int
    my_status: int
    my_status_text: str
    message: str


class CommunityBannerResponse(BaseModel):
    id: int
    title: str | None
    image_url: str
    link_type: str | None
    link_value: str | None
    sort: int
    position: str


class CommunityQuotaItem(BaseModel):
    total: int
    used: int
    remain: int
    points_available: bool = True
    points_cost: int | None = None


class CommunityQuotasResponse(BaseModel):
    apply_daily: CommunityQuotaItem
    paper_plane_daily: CommunityQuotaItem


class CommunityCityResponse(BaseModel):
    """同城当前城市：name 展示，code 为市一级 6 位码（如 330100），区筛二期。"""

    name: str
    code: str | None = None


class CommunityCityUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    code: str | None = Field(
        default=None,
        max_length=6,
        pattern=CITY_CODE_PATTERN,
        description="市一级 city_code，只接受 4 或 6 位 ASCII 数字",
    )


class CommunityReportReason(BaseModel):
    id: str
    label: str


class CommunityReportCreate(BaseModel):
    target_type: Literal["post", "comment", "paper_plane"]
    target_id: int = Field(ge=1)
    reason_id: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    images: list[str] = Field(default_factory=list, max_length=6)


class CommunityReportResponse(BaseModel):
    id: int
    target_type: Literal["post", "comment", "paper_plane", "user"]
    target_id: int | None
    target_user_id: int
    type: str
    status: Literal[0, 1, 2]
    created_at: datetime


class CommunityCollectResponse(BaseModel):
    id: int
    is_collected: bool
    collect_count: int


_ALLOWED_STORAGE_PREFIX = "/storage/uploads/"


def _validate_internal_url(url: str | None, field_name: str) -> str | None:
    """确保媒体 URL 只能是服务器自有 /storage/uploads/ 路径，拒绝外链和绝对路径。"""
    if url is None:
        return None
    v = url.strip()
    if not v.startswith(_ALLOWED_STORAGE_PREFIX):
        raise ValueError(f"{field_name} 必须是已上传的内部存储路径")
    return v


class PaperPlaneCreate(BaseModel):
    content: str = Field(default="", max_length=1000)
    images: list[str] = Field(default_factory=list, max_length=6)
    image_media_ids: list[PositiveInt] = Field(default_factory=list, max_length=6)
    city: str | None = Field(default=None, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=5)
    is_anonymous: bool = True
    voice_url: str | None = Field(default=None, max_length=500)
    voice_duration_sec: int | None = Field(default=None, ge=1, le=60)

    @field_validator("voice_url", mode="before")
    @classmethod
    def validate_voice_url(cls, v: str | None) -> str | None:
        return _validate_internal_url(v, "voice_url")

    @model_validator(mode="after")
    def require_text_or_voice_or_images(self) -> "PaperPlaneCreate":
        text = (self.content or "").strip()
        has_voice = bool(self.voice_url)
        has_image_ids = len(self.image_media_ids or []) > 0
        has_images = len(self.images or []) > 0
        if not text and not has_voice and not has_image_ids and not has_images:
            raise ValueError("纸飞机至少需要文字、语音或图片")
        if self.voice_url and self.voice_duration_sec is None:
            raise ValueError("语音纸飞机需提供 voice_duration_sec")
        if self.voice_duration_sec is not None and not self.voice_url:
            raise ValueError("voice_duration_sec 需配合 voice_url")
        if len(self.image_media_ids or []) > 6:
            raise ValueError("纸飞机图片最多 6 张")
        self.content = text
        return self


class PaperPlaneResponse(BaseModel):
    id: int
    content: str
    images: list[str]
    city: str | None
    tags: list[str]
    is_anonymous: bool
    reply_count: int
    voice_url: str | None = None
    voice_duration_sec: int | None = None
    created_at: datetime


class PaperPlaneReplyCreate(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    is_anonymous: bool = True


class PaperPlaneReplyResponse(BaseModel):
    id: int
    plane_id: int
    user_id: int
    content: str
    is_anonymous: bool
    conversation_id: int | None = None
    created_at: datetime


class PaperPlaneConversationResponse(BaseModel):
    id: int
    plane_id: int
    owner_id: int
    replier_id: int
    status: int
    last_message: str | None = None
    last_message_at: datetime | None = None
    unread_count: int = 0
    plane_content: str | None = None
    peer_label: str = "匿名用户"
    created_at: datetime


class PaperPlaneMessageCreate(BaseModel):
    content: str = Field(default="", max_length=1000)
    type: Literal[1, 3] = 1
    media_url: str | None = Field(default=None, max_length=500)
    voice_duration_sec: int | None = Field(default=None, ge=1, le=60)

    @field_validator("media_url", mode="before")
    @classmethod
    def validate_media_url(cls, v: str | None) -> str | None:
        return _validate_internal_url(v, "media_url")

    @model_validator(mode="after")
    def validate_message_payload(self) -> "PaperPlaneMessageCreate":
        text = (self.content or "").strip()
        if self.type == 1 and not text:
            raise ValueError("文本消息不能为空")
        if self.type == 3 and not self.media_url:
            raise ValueError("语音消息需提供 media_url")
        if self.type == 3 and self.voice_duration_sec is None:
            raise ValueError("语音消息需提供 voice_duration_sec")
        self.content = text
        return self


class PaperPlaneMessageResponse(BaseModel):
    id: int
    conversation_id: int
    from_user_id: int
    mine: bool
    content: str
    type: int
    media_url: str | None = None
    voice_duration_sec: int | None = None
    created_at: datetime


class MediaUploadResponse(BaseModel):
    url: str
    content_type: str
    size: int
    purpose: str


class CommunityMediaResponse(BaseModel):
    id: int
    purpose: Literal["post", "paper_plane"]
    media_type: Literal["image", "video"]
    url: str
    thumbnail_url: str | None = None
    file_size: int | None = None
    duration_seconds: int | None = None
    status: Literal["ready", "bound", "deleted"]
