"""Top-level API router."""

from fastapi import APIRouter

from app.api.routes import location

from app.api.routes import (
    admin,
    auth,
    certifications,
    community,
    discovery,
    finance,
    health,
    identity,
    matchmaker,
    media,
    meeting,
    membership,
    organization,
    points,
    presence,
    profile,
    regions,
    social,
    users,
)


api_router = APIRouter()
api_router.include_router(location.router, tags=["位置服务"])
api_router.include_router(location.users_router, tags=["位置服务"])
api_router.include_router(health.router, tags=["系统"])
api_router.include_router(auth.router, tags=["账号与认证"])
api_router.include_router(users.router, tags=["账号与认证"])
api_router.include_router(certifications.router, tags=["认证审核"])
api_router.include_router(membership.router, tags=["会员"])
api_router.include_router(points.router, tags=["积分"])
api_router.include_router(regions.router, tags=["地区"])
api_router.include_router(presence.router, tags=["消息"])
api_router.include_router(identity.router, tags=["账号与认证"])
api_router.include_router(profile.router, tags=["首页与资料"])
api_router.include_router(discovery.router, tags=["首页与资料"])
api_router.include_router(discovery.users_router, tags=["首页与资料"])
api_router.include_router(matchmaker.router, tags=["红娘"])
api_router.include_router(matchmaker.product_router, tags=["红娘"])
api_router.include_router(matchmaker.requests_router, tags=["红娘"])
api_router.include_router(meeting.router, tags=["红娘"])
api_router.include_router(social.router, tags=["消息"])
api_router.include_router(community.router, tags=["社区"])
api_router.include_router(media.router, tags=["社区"])
api_router.include_router(admin.router, tags=["管理后台"])
api_router.include_router(matchmaker.admin_router, tags=["管理后台"])
api_router.include_router(meeting.admin_router, tags=["管理后台"])
api_router.include_router(finance.admin_router, tags=["管理后台"])
api_router.include_router(organization.router, tags=["组织与归属"])
api_router.include_router(organization.promotion_router, tags=["组织与归属"])
api_router.include_router(organization.partner_router, tags=["组织与归属"])
api_router.include_router(finance.router, tags=["财务与结算"])


OPENAPI_TAGS = [
    {"name": "账号与认证", "description": "登录、账号身份、实名认证和账号安全。"},
    {"name": "首页与资料", "description": "推荐、搜索、公开资料和用户资料管理。"},
    {"name": "红娘", "description": "红娘申请、服务牵线、约见申请和约会记录。"},
    {"name": "社区", "description": "帖子、评论、互动、话题和纸飞机。"},
    {"name": "消息", "description": "申请认识、匹配、聊天、通知和关系安全。"},
    {"name": "管理后台", "description": "内容、消息、红娘、财务和运营治理。"},
    {"name": "组织与归属", "description": "门店、组织成员、资源分派、推广和合伙团队。"},
    {"name": "财务与结算", "description": "订单、分成、账本、余额和提现。"},
    {"name": "认证审核", "description": "认证资料和认证审核相关能力。"},
    {"name": "会员", "description": "会员相关能力。"},
    {"name": "积分", "description": "积分账户和积分流水相关能力。"},
    {"name": "地区", "description": "省市区等地区数据查询。"},
    {"name": "系统", "description": "健康检查和系统发现信息。"},
]
