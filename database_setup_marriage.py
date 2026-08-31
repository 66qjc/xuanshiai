# database_setup_marriage.py - 婚恋交友小程序数据库表结构初始化
# 注意：此文件主要用于数据库表结构定义和初始化
# 日常数据库操作请使用 core.database.get_conn()
# 已移除 SQLAlchemy ORM，完全使用 pymysql
import os
import sys
import logging
import pymysql
import re
from urllib.parse import unquote, urlsplit


FAIL_CLOSED_COMMUNITY_COLUMNS = frozenset(
    {
        ("user_profile", "community_city_name"),
        ("user_profile", "community_city_code"),
        ("user_profile", "community_city_updated_at"),
        ("community_post", "visibility"),
        ("community_post", "declaration"),
        ("community_post", "deleted_at"),
        ("community_post", "moderation_status"),
        ("community_post", "moderation_report_id"),
        ("community_comment", "root_id"),
        ("community_comment", "deleted_at"),
        ("community_comment", "moderation_status"),
        ("community_comment", "moderation_report_id"),
        ("user_report", "target_type"),
        ("user_report", "target_id"),
        ("user_report", "action"),
        ("user_report", "reviewed_by"),
        ("user_report", "reviewed_at"),
        ("user_privacy", "notify_follow"),
        ("user_privacy", "notify_message"),
        ("user_notification", "target_type"),
        ("user_notification", "target_id"),
        ("paper_plane", "moderation_status"),
        ("paper_plane", "moderation_report_id"),
        ("paper_plane", "voice_url"),
        ("paper_plane", "voice_duration_sec"),
    }
)


def _validate_database_name(database: str) -> str:
    """只允许配置中的安全数据库标识符，避免拼接 SQL 时产生注入风险。"""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database):
        raise ValueError("数据库名只能包含字母、数字和下划线，长度不能超过64")
    return database

def get_db_config():
    """
    从环境变量获取数据库配置。
    优先解析 DATABASE_URL，如果不存在则回退到独立的 DB_* 变量。
    """
    # 1. 优先尝试从 DATABASE_URL 解析
    database_url = os.getenv('DATABASE_URL', '')
    if not database_url:
        # 让独立执行的建表脚本复用项目 Settings 的 .env 加载逻辑。
        try:
            from app.core.config import settings

            database_url = settings.database_url
        except Exception:
            database_url = ''
    if database_url:
        # 兼容 mysql:// 和 mysql+aiomysql://，并正确解码 URL 中的特殊字符。
        parsed = urlsplit(database_url.replace("mysql+aiomysql://", "mysql://", 1))
        if parsed.scheme == "mysql" and parsed.hostname and parsed.username and parsed.password is not None and parsed.port:
            user = unquote(parsed.username)
            password = unquote(parsed.password)
            host = parsed.hostname
            port = parsed.port
            database = _validate_database_name(parsed.path.lstrip("/"))
            return {
                'host': host,
                'port': port,
                'user': user,
                'password': password,
                'database': database,
            }
        else:
            logger.warning(f"⚠️ 无法解析 DATABASE_URL: {database_url}，回退到独立变量")

    # 2. 回退方案：从独立的 DB_* 变量读取
    return {
        'host': os.getenv('DB_HOST', 'localhost'),
        'port': int(os.getenv('DB_PORT', 3306)),
        'user': os.getenv('DB_USER', 'root'),
        'password': os.getenv('DB_PASSWORD', ''),
            'database': _validate_database_name(os.getenv('DB_NAME', 'xuanshiai')),
        }


# =============================================
# 日志配置
# =============================================

def get_logger(name):
    """获取日志记录器"""
    logger = logging.getLogger(name)
    root_logger = logging.getLogger()
    # The FastAPI application configures the root logger before importing this
    # module. Reusing it prevents each initialization message from being logged twice.
    if not logger.handlers and not root_logger.handlers:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


# 使用统一的日志配置
logger = get_logger(__name__)


class DatabaseManager:
    def __init__(self):
        self._ensure_database_exists()

    def _ensure_database_exists(self):
        """确保数据库存在，如果不存在则创建"""
        try:
            temp_config = get_db_config().copy()
            database = _validate_database_name(temp_config.pop('database'))
            import pymysql
            conn = pymysql.connect(**temp_config)
            cursor = conn.cursor()
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                f"DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            conn.commit()
            conn.close()
            logger.debug(f"数据库 `{database}` 已就绪")
        except Exception as e:
            logger.error(f"❌ 数据库初始化失败: {e}")
            raise

    def _ensure_table_columns(self, cursor, table_name: str, required_columns: dict):
        """
        确保表的必需字段存在，如果不存在则添加

        Args:
            cursor: 数据库游标
            table_name: 表名
            required_columns: 必需字段字典，格式为 {字段名: 字段定义}
        """
        try:
            cursor.execute(f"SHOW COLUMNS FROM {table_name}")
            existing_columns = {row['Field'] for row in cursor.fetchall()}
        except Exception as e:
            logger.debug(f"表 {table_name} 可能不存在，将在创建表时处理: {e}")
            return

        for column_name, column_def in required_columns.items():
            if column_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_def}")
                    logger.info(f"✅ 已添加字段 {table_name}.{column_name}")
                except Exception as e:
                    logger.warning(f"⚠️ 添加字段 {table_name}.{column_name} 失败: {e}")
                    if (table_name.strip("`"), column_name) in FAIL_CLOSED_COMMUNITY_COLUMNS:
                        raise

    def _ensure_required_columns(self, cursor):
        """补齐已存在旧表缺少的用户与认证模块字段。"""
        required_columns = {
            'users': {
                'password_algo': "`password_algo` varchar(32) DEFAULT NULL COMMENT '密码哈希算法'",
                'password_updated_at': "`password_updated_at` datetime DEFAULT NULL",
                'password_failed_count': "`password_failed_count` int unsigned NOT NULL DEFAULT '0'",
                'password_locked_until': "`password_locked_until` datetime DEFAULT NULL",
                'phone_verified_at': "`phone_verified_at` datetime DEFAULT NULL",
                'wechat_bound_at': "`wechat_bound_at` datetime DEFAULT NULL",
                'last_login_ip': "`last_login_ip` varchar(64) DEFAULT NULL",
                'last_login_device_id': "`last_login_device_id` varchar(128) DEFAULT NULL",
                'risk_status': "`risk_status` tinyint NOT NULL DEFAULT '0' COMMENT '0正常 1关注 2限制'",
                'frozen_at': "`frozen_at` datetime DEFAULT NULL",
                'frozen_reason': "`frozen_reason` varchar(255) DEFAULT NULL",
                'deletion_requested_at': "`deletion_requested_at` datetime DEFAULT NULL",
                'deletion_scheduled_at': "`deletion_scheduled_at` datetime DEFAULT NULL",
                'deletion_cancelled_at': "`deletion_cancelled_at` datetime DEFAULT NULL",
                'deleted_at': "`deleted_at` datetime DEFAULT NULL",
            },
            'user_auth': {
                'realname_status': "`realname_status` tinyint NOT NULL DEFAULT '0' COMMENT '0未认证 1认证中 2通过 3失败 4人工复核 5撤销'",
                'realname_provider': "`realname_provider` varchar(64) DEFAULT NULL",
                'provider_request_id': "`provider_request_id` varchar(128) DEFAULT NULL",
                'provider_result_code': "`provider_result_code` varchar(64) DEFAULT NULL",
                'submitted_at': "`submitted_at` datetime DEFAULT NULL",
                'verified_at': "`verified_at` datetime DEFAULT NULL",
                'failed_at': "`failed_at` datetime DEFAULT NULL",
                'retry_count': "`retry_count` int unsigned NOT NULL DEFAULT '0'",
                'next_retry_at': "`next_retry_at` datetime DEFAULT NULL",
                'manual_review_by': "`manual_review_by` bigint unsigned DEFAULT NULL",
                'manual_review_at': "`manual_review_at` datetime DEFAULT NULL",
                'revoked_at': "`revoked_at` datetime DEFAULT NULL",
                'revoked_reason': "`revoked_reason` varchar(255) DEFAULT NULL",
                'id_card_hash': "`id_card_hash` char(64) DEFAULT NULL COMMENT '身份证号哈希，用于去重'",
                'id_card_masked': "`id_card_masked` varchar(32) DEFAULT NULL",
                'encryption_version': "`encryption_version` varchar(32) DEFAULT NULL",
                'marriage_cert': "`marriage_cert` varchar(255) DEFAULT NULL COMMENT '婚姻认证材料描述或地址'",
                'marriage_verified': "`marriage_verified` tinyint NOT NULL DEFAULT '0' COMMENT '0未提交 1审核中 2通过 3失败'",
                'marriage_fail_reason': "`marriage_fail_reason` varchar(255) DEFAULT NULL",
                'marriage_submitted_at': "`marriage_submitted_at` datetime DEFAULT NULL",
                'marriage_reviewed_at': "`marriage_reviewed_at` datetime DEFAULT NULL",
            },
            'user_profile': {
                'weight': "`weight` int DEFAULT NULL COMMENT '体重kg'",
                'occupation': "`occupation` varchar(128) DEFAULT NULL COMMENT '职业'",
                'industry': "`industry` varchar(128) DEFAULT NULL COMMENT '行业'",
                'education_level': "`education_level` tinyint DEFAULT NULL COMMENT '学历等级'",
                'hometown_province_code': "`hometown_province_code` varchar(32) DEFAULT NULL",
                'hometown_city_code': "`hometown_city_code` varchar(32) DEFAULT NULL",
                'hometown_district_code': "`hometown_district_code` varchar(32) DEFAULT NULL",
                'residence_province_code': "`residence_province_code` varchar(32) DEFAULT NULL",
                'residence_city_code': "`residence_city_code` varchar(32) DEFAULT NULL",
                'residence_district_code': "`residence_district_code` varchar(32) DEFAULT NULL",
                # 社区同城浏览偏好（独立于资料现居，避免污染 discovery same_city）
                'community_city_name': "`community_city_name` varchar(64) DEFAULT NULL COMMENT '同城浏览城市名'",
                'community_city_code': "`community_city_code` varchar(32) DEFAULT NULL COMMENT '同城浏览市一级码'",
                'community_city_updated_at': "`community_city_updated_at` datetime DEFAULT NULL COMMENT '同城偏好上次变更时间'",
                'location_source': "`location_source` varchar(32) DEFAULT NULL",
                'location_updated_at': "`location_updated_at` datetime DEFAULT NULL",
                'location_precision': "`location_precision` decimal(10,2) DEFAULT NULL",
                'location_consent': "`location_consent` tinyint NOT NULL DEFAULT '0'",
                'location_visible': "`location_visible` tinyint NOT NULL DEFAULT '0'",
                'interest_tags': "`interest_tags` json DEFAULT NULL",
                'personality_tags': "`personality_tags` json DEFAULT NULL",
                'completion_algorithm_version': "`completion_algorithm_version` varchar(32) DEFAULT NULL",
                'completion_calculated_at': "`completion_calculated_at` datetime DEFAULT NULL",
            },
            'user_profile_completion': {
                'weight_completed': "`weight_completed` tinyint NOT NULL DEFAULT '0'",
                'hometown_completed': "`hometown_completed` tinyint NOT NULL DEFAULT '0'",
                'mbti_completed': "`mbti_completed` tinyint NOT NULL DEFAULT '0'",
                'single_pledge_completed': "`single_pledge_completed` tinyint NOT NULL DEFAULT '0'",
            },
            'user_privacy': {
                'anonymous_browse_enabled': "`anonymous_browse_enabled` tinyint NOT NULL DEFAULT '0' COMMENT 'VIP无痕浏览'",
                'notify_message': "`notify_message` tinyint NOT NULL DEFAULT '1' COMMENT '新消息通知'",
                'notify_follow': "`notify_follow` tinyint NOT NULL DEFAULT '1' COMMENT '关注通知'",
                'privacy_version': "`privacy_version` varchar(32) DEFAULT NULL",
                'privacy_updated_at': "`privacy_updated_at` datetime DEFAULT NULL",
                'show_profile': "`show_profile` tinyint NOT NULL DEFAULT '1' COMMENT '是否展示个人资料'",
                'show_likes': "`show_likes` tinyint NOT NULL DEFAULT '1' COMMENT '是否展示喜欢列表'",
                'show_posts': "`show_posts` tinyint NOT NULL DEFAULT '1' COMMENT '是否展示个人动态'",
            },
            'community_post': {
                'visibility': "`visibility` tinyint NOT NULL DEFAULT '0' COMMENT '0公开 1仅好友 2仅自己'",
                'declaration': "`declaration` varchar(32) NOT NULL DEFAULT '' COMMENT '内容声明'",
                'deleted_at': "`deleted_at` datetime DEFAULT NULL COMMENT '用户软删除时间'",
                'moderation_status': "`moderation_status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 0审核中 2管理员下架'",
                'moderation_report_id': "`moderation_report_id` bigint unsigned DEFAULT NULL COMMENT '当前下架来源举报ID'",
                'moderation_reason': "`moderation_reason` varchar(255) DEFAULT NULL",
                'moderated_by': "`moderated_by` bigint unsigned DEFAULT NULL",
                'moderated_at': "`moderated_at` datetime DEFAULT NULL",
            },
            'community_comment': {
                'root_id': "`root_id` bigint unsigned DEFAULT NULL COMMENT '一级根评论ID；一级评论为空'",
                'deleted_at': "`deleted_at` datetime DEFAULT NULL COMMENT '用户软删除时间'",
                'moderation_status': "`moderation_status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 0审核中 2管理员下架'",
                'moderation_report_id': "`moderation_report_id` bigint unsigned DEFAULT NULL COMMENT '当前下架来源举报ID'",
                'moderation_reason': "`moderation_reason` varchar(255) DEFAULT NULL",
                'moderated_by': "`moderated_by` bigint unsigned DEFAULT NULL",
                'moderated_at': "`moderated_at` datetime DEFAULT NULL",
            },
            'chat_session': {
                'user1_pinned_at': "`user1_pinned_at` datetime DEFAULT NULL",
                'user2_pinned_at': "`user2_pinned_at` datetime DEFAULT NULL",
            },
            'matchmaker_admin_account': {
                'data_scope': "`data_scope` varchar(16) NOT NULL DEFAULT 'SELF' COMMENT 'SELF/STORE/ORGANIZATION/ALL'",
                'organization_id': "`organization_id` bigint unsigned DEFAULT NULL",
            },
            'user_notification': {
                'target_type': "`target_type` varchar(32) DEFAULT NULL COMMENT '前端导航目标类型'",
                'target_id': "`target_id` bigint unsigned DEFAULT NULL COMMENT '前端导航目标ID'",
            },
            'user_report': {
                'target_type': "`target_type` varchar(32) NOT NULL DEFAULT 'user' COMMENT 'user|post|comment|paper_plane'",
                'target_id': "`target_id` bigint unsigned DEFAULT NULL COMMENT '内容对象ID；user 举报可为空或等于 target_user_id'",
                'action': "`action` varchar(32) NOT NULL DEFAULT 'none' COMMENT 'none|hide_content|restore_content|dismiss'",
                'reviewed_by': "`reviewed_by` bigint unsigned DEFAULT NULL COMMENT '原举报审核人'",
                'reviewed_at': "`reviewed_at` datetime DEFAULT NULL COMMENT '举报审核时间'",
            },
            'paper_plane': {
                'moderation_status': "`moderation_status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 2下架（与 lifecycle status 分离）'",
                'moderation_report_id': "`moderation_report_id` bigint unsigned DEFAULT NULL COMMENT '当前下架来源举报ID'",
                'voice_url': "`voice_url` varchar(500) DEFAULT NULL COMMENT '语音地址'",
                'voice_duration_sec': "`voice_duration_sec` int DEFAULT NULL COMMENT '语音时长秒'",
            },
            'community_media': {
                'moderation_status': "`moderation_status` varchar(24) NOT NULL DEFAULT 'pending' COMMENT 'pending/approved/rejected/hidden'",
                'moderation_reason': "`moderation_reason` varchar(255) DEFAULT NULL",
                'moderated_by': "`moderated_by` bigint unsigned DEFAULT NULL",
                'moderated_at': "`moderated_at` datetime DEFAULT NULL",
            },
            'paper_plane_reply': {
                'moderation_status': "`moderation_status` varchar(24) NOT NULL DEFAULT 'approved' COMMENT 'pending/approved/rejected/hidden'",
                'moderation_reason': "`moderation_reason` varchar(255) DEFAULT NULL",
                'moderated_by': "`moderated_by` bigint unsigned DEFAULT NULL",
                'moderated_at': "`moderated_at` datetime DEFAULT NULL",
            },
            'paper_plane_message': {
                'moderation_status': "`moderation_status` varchar(24) NOT NULL DEFAULT 'approved' COMMENT 'pending/approved/rejected/hidden'",
                'moderation_reason': "`moderation_reason` varchar(255) DEFAULT NULL",
                'moderated_by': "`moderated_by` bigint unsigned DEFAULT NULL",
                'moderated_at': "`moderated_at` datetime DEFAULT NULL",
            },
            'community_moderation_task': {
                'provider': "`provider` varchar(32) NOT NULL DEFAULT 'local' COMMENT 'local/aliyun_market/manual'",
            },
            'config_sensitive_word': {
                'action': "`action` varchar(24) NOT NULL DEFAULT 'replace' COMMENT 'reject/replace/manual_review'",
            },
            'user_login_log': {
                'login_status': "`login_status` tinyint NOT NULL DEFAULT '1' COMMENT '1成功 2失败'",
                'failure_reason': "`failure_reason` varchar(255) DEFAULT NULL",
                'session_id': "`session_id` bigint unsigned DEFAULT NULL",
                'device_id': "`device_id` varchar(128) DEFAULT NULL",
                'platform': "`platform` varchar(32) DEFAULT NULL",
                'os_version': "`os_version` varchar(32) DEFAULT NULL",
                'app_version': "`app_version` varchar(32) DEFAULT NULL",
                'user_agent': "`user_agent` varchar(512) DEFAULT NULL",
                'region': "`region` varchar(128) DEFAULT NULL",
                'risk_level': "`risk_level` tinyint NOT NULL DEFAULT '0'",
                'is_suspicious': "`is_suspicious` tinyint NOT NULL DEFAULT '0'",
            },
            'user_matchmaker_apply': {
                'application_type': "`application_type` varchar(32) NOT NULL DEFAULT 'service_matchmaker' COMMENT '申请类型 promoter推广红娘 partner合伙人 service_matchmaker服务红娘'",
                'application_details': "`application_details` json DEFAULT NULL COMMENT '红娘审核扩展资料'",
                'reviewed_by': "`reviewed_by` bigint unsigned DEFAULT NULL",
                'reviewed_at': "`reviewed_at` datetime DEFAULT NULL",
                'suspended_at': "`suspended_at` datetime DEFAULT NULL",
                'suspension_reason': "`suspension_reason` varchar(255) DEFAULT NULL",
            },
            'payment_order': {
                'idempotency_key': "`idempotency_key` varchar(128) DEFAULT NULL COMMENT '客户端幂等键'",
                'service_product_id': "`service_product_id` bigint unsigned DEFAULT NULL COMMENT '红娘服务商品ID'",
                'matchmaker_id': "`matchmaker_id` bigint unsigned DEFAULT NULL COMMENT '红娘服务对象ID'",
                'service_request_id': "`service_request_id` bigint unsigned DEFAULT NULL COMMENT '关联红娘服务申请ID'",
                'service_requirement': "`service_requirement` varchar(2000) DEFAULT NULL COMMENT '红娘服务需求'",
            },
            'meeting_request': {
                'service_id': "`service_id` bigint unsigned DEFAULT NULL COMMENT '关联红娘服务单'",
            },
            'matchmaker_service': {
                'order_id': "`order_id` bigint unsigned DEFAULT NULL COMMENT '关联支付订单ID'",
                'product_id': "`product_id` bigint unsigned DEFAULT NULL COMMENT '红娘服务商品ID'",
            },
            'user_boost': {
                'start_at': "`start_at` datetime DEFAULT CURRENT_TIMESTAMP",
                'end_at': "`end_at` datetime DEFAULT NULL",
                'status': "`status` tinyint NOT NULL DEFAULT '1' COMMENT '1生效中 2已过期 3已撤销'",
            },
        }

        for table_name, columns in required_columns.items():
            self._ensure_table_columns(cursor, f'`{table_name}`', columns)
        self._ensure_payment_order_product_type(cursor)
        self._ensure_community_moderation_status_types(cursor)
        self._ensure_matchmaker_application_index(cursor)
        self._ensure_payment_order_idempotency_index(cursor)
        self._ensure_community_post_feed_indexes(cursor)
        self._ensure_idempotency_contract(cursor)

    def _ensure_payment_order_product_type(self, cursor):
        """Keep package codes such as ``monthly`` lossless in payment orders."""
        try:
            cursor.execute("SHOW COLUMNS FROM `payment_order` LIKE 'product_type'")
            row = cursor.fetchone()
            if row and "char" not in str(row["Type"]).lower() and "text" not in str(row["Type"]).lower():
                cursor.execute("ALTER TABLE `payment_order` MODIFY COLUMN `product_type` varchar(64) DEFAULT NULL COMMENT '商品编码'")
        except Exception as exc:
            logger.warning(f"⚠️ payment_order.product_type 类型迁移失败: {exc}")

    def _ensure_community_moderation_status_types(self, cursor):
        """Migrate pre-merge string states on posts/comments to numeric states."""
        for table_name in ("community_post", "community_comment"):
            try:
                cursor.execute(
                    f"SHOW COLUMNS FROM `{table_name}` LIKE 'moderation_status'"
                )
                row = cursor.fetchone()
                col_type = str(row["Type"]).lower() if row else ""
                if "char" in col_type or "text" in col_type or "enum" in col_type:
                    cursor.execute(
                        f"""UPDATE `{table_name}`
                        SET moderation_status = CASE moderation_status
                            WHEN 'pending' THEN 0
                            WHEN 'approved' THEN 1
                            WHEN 'replaced' THEN 1
                            WHEN 'rejected' THEN 2
                            WHEN 'hidden' THEN 2
                            WHEN 'deleted' THEN 2
                            ELSE moderation_status
                        END"""
                    )
                cursor.execute(
                    f"""ALTER TABLE `{table_name}`
                    MODIFY COLUMN `moderation_status` tinyint NOT NULL DEFAULT 1
                    COMMENT '1 normal, 0 pending, 2 hidden'"""
                )
            except Exception as exc:
                logger.warning("Failed to migrate %s moderation_status: %s", table_name, exc)
                if (table_name, "moderation_status") in FAIL_CLOSED_COMMUNITY_COLUMNS:
                    raise

    def _ensure_community_post_feed_indexes(self, cursor):
        """为社区动态流补齐复合索引，避免 status + 排序走 filesort。"""
        indexes = {
            'idx_status_top_created': ('community_post', '(`status`,`is_top`,`created_at`)'),
            'idx_status_like_created': ('community_post', '(`status`,`like_count`,`created_at`)'),
            'idx_post_visibility_state': (
                'community_post',
                '(`status`,`moderation_status`,`deleted_at`,`created_at`)',
            ),
            'idx_root_created': ('community_comment', '(`root_id`,`id`)'),
        }
        for index_name, (table_name, columns) in indexes.items():
            try:
                cursor.execute(f"""
                    SELECT INDEX_NAME
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = '{table_name}'
                      AND INDEX_NAME = '{index_name}'
                """)
                if cursor.fetchone():
                    continue
                cursor.execute(
                    f"ALTER TABLE `{table_name}` ADD KEY `{index_name}` {columns}"
                )
                logger.debug(f"✅ 社区动态索引 {index_name} 已添加")
            except pymysql.MySQLError as exc:
                logger.warning(f"⚠️ 社区动态索引 {index_name} 添加失败: {exc}")

    def _ensure_idempotency_contract(self, cursor):
        """Upgrade the durable idempotency table without relying on server-local time."""
        try:
            cursor.execute("SHOW FULL COLUMNS FROM `api_idempotency_record`")
            columns = {row['Field']: row for row in cursor.fetchall()}

            key_column = columns.get('idempotency_key')
            if key_column and str(key_column.get('Collation') or '').lower() != 'utf8mb4_bin':
                cursor.execute("""
                    ALTER TABLE `api_idempotency_record`
                    MODIFY COLUMN `idempotency_key` varchar(128)
                    CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL
                    COMMENT '客户端幂等键'
                """)

            timestamp_columns = ('created_at', 'updated_at')
            needs_timestamp_upgrade = any(
                column_name in columns
                and (
                    str(columns[column_name].get('Type') or '').lower() != 'datetime(6)'
                    or columns[column_name].get('Default') is not None
                    or bool(columns[column_name].get('Extra'))
                )
                for column_name in timestamp_columns
            )
            if needs_timestamp_upgrade:
                cursor.execute("""
                    UPDATE `api_idempotency_record`
                    SET `created_at` = UTC_TIMESTAMP(6), `updated_at` = UTC_TIMESTAMP(6)
                    WHERE `state` = 'reserved'
                """)
                for column_name in timestamp_columns:
                    cursor.execute(f"""
                        ALTER TABLE `api_idempotency_record`
                        MODIFY COLUMN `{column_name}` datetime(6) NOT NULL
                    """)
        except pymysql.MySQLError as exc:
            logger.warning(f"api_idempotency_record 契约迁移失败: {exc}")
            raise

    def _ensure_payment_order_idempotency_index(self, cursor):
        """Prevent duplicate paid-service orders when clients retry concurrently."""
        try:
            cursor.execute("""
                SELECT INDEX_NAME FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'payment_order'
                  AND INDEX_NAME = 'uk_payment_order_type_idempotency'
            """)
            if cursor.fetchone():
                return
            cursor.execute("""
                SELECT user_id, idempotency_key, COUNT(*) AS duplicate_count
                FROM payment_order
                WHERE idempotency_key IS NOT NULL
                GROUP BY user_id, idempotency_key
                HAVING COUNT(*) > 1
                LIMIT 1
            """)
            if cursor.fetchone():
                logger.warning("payment_order 存在重复幂等键，跳过唯一索引迁移，请先清理历史数据")
                return
            cursor.execute("""
                ALTER TABLE `payment_order`
                ADD UNIQUE KEY `uk_payment_order_type_idempotency` (`user_id`, `type`, `idempotency_key`)
            """)
        except pymysql.MySQLError as exc:
            logger.warning(f"payment_order 幂等键唯一索引迁移失败: {exc}")

    def _ensure_matchmaker_application_index(self, cursor):
        """将旧版红娘申请的单用户唯一索引升级为单用户单申请类型唯一索引。"""
        try:
            cursor.execute("""
                SELECT INDEX_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'user_matchmaker_apply'
                  AND INDEX_NAME = 'uk_user_id'
            """)
            if cursor.fetchone():
                cursor.execute("ALTER TABLE `user_matchmaker_apply` DROP INDEX `uk_user_id`")
            cursor.execute("""
                SELECT INDEX_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'user_matchmaker_apply'
                  AND INDEX_NAME = 'uk_user_id_type'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE `user_matchmaker_apply` ADD UNIQUE KEY `uk_user_id_type` (`user_id`,`application_type`)")
        except pymysql.MySQLError as exc:
            logger.warning(f"⚠️ 红娘申请索引升级失败，请检查历史重复数据: {exc}")

    def _add_foreign_key(
        self,
        cursor,
        table_name: str,
        column: str,
        ref_table: str = 'users',
        ref_column: str = 'id',
        on_delete: str = 'CASCADE',
    ):
        """添加外键约束（幂等）"""
        try:
            fk_name = f"fk_{table_name}_{column}"
            cursor.execute(f"""
                SELECT CONSTRAINT_NAME, DELETE_RULE
                FROM information_schema.REFERENTIAL_CONSTRAINTS
                WHERE CONSTRAINT_SCHEMA = DATABASE()
                AND TABLE_NAME = '{table_name}'
                AND CONSTRAINT_NAME = '{fk_name}'
            """)
            existing = cursor.fetchone()
            if existing and str(existing.get('DELETE_RULE') or '').upper() == on_delete:
                return
            if existing:
                cursor.execute(
                    f"ALTER TABLE `{table_name}` DROP FOREIGN KEY `{fk_name}`"
                )

            cursor.execute(f"""
                ALTER TABLE `{table_name}`
                ADD CONSTRAINT `{fk_name}`
                FOREIGN KEY (`{column}`) REFERENCES `{ref_table}`(`{ref_column}`) ON DELETE {on_delete}
            """)
            logger.debug(f"✅ 外键 {fk_name} 已添加")
        except pymysql.MySQLError as e:
            # 1061: 重复键名, 1062: 重复键, 1215: 无法添加外键约束（通常因为数据类型不匹配或引用表不存在）
            if e.args[0] in (1061, 1062, 1215):
                logger.debug(f"ℹ️ 外键 {fk_name} 跳过（{e.args[1]}）")
            else:
                logger.warning(f"⚠️ 外键 {fk_name} 添加失败: {e}")

    def _add_all_foreign_keys(self, cursor):
        """添加所有外键约束"""
        # 所有引用 users.id 的 user_id 字段
        user_id_tables = [
            ('user_auth', 'user_id'),
            ('user_registration_intent', 'user_id'),
            ('user_role', 'user_id'),
            ('user_profile', 'user_id'),
            ('user_privacy', 'user_id'),
            ('user_login_log', 'user_id'),
            ('user_session', 'user_id'),
            ('user_agreement_acceptance', 'user_id'),
            ('user_partner_preference', 'user_id'),
            ('user_media', 'user_id'),
            ('user_profile_completion', 'user_id'),
            ('user_block', 'user_id'),
            ('user_block', 'target_user_id'),
            ('user_report', 'user_id'),
            ('user_report', 'target_user_id'),
            ('report_appeal', 'appellant_user_id'),
            ('user_favorite', 'user_id'),
            ('user_favorite', 'target_user_id'),
            ('user_browse_history', 'user_id'),
            ('user_browse_history', 'target_user_id'),
            ('user_notification', 'user_id'),
            ('user_boost', 'user_id'),
            ('user_boost', 'target_user_id'),
            ('user_match', 'user_id'),
            ('user_match', 'target_user_id'),
            ('user_points', 'user_id'),
            ('user_membership', 'user_id'),
            ('user_task', 'user_id'),
            ('user_checkin', 'user_id'),
            ('user_matchmaker_apply', 'user_id'),
            ('user_match_recommend', 'user_id'),
            ('user_match_recommend', 'recommend_user_id'),
            ('user_swipe_record', 'user_id'),
            ('user_swipe_record', 'target_user_id'),
            ('match_apply', 'from_user_id'),
            ('match_apply', 'to_user_id'),
            ('chat_session', 'user1_id'),
            ('chat_session', 'user2_id'),
            ('chat_message', 'from_user_id'),
            ('chat_message', 'to_user_id'),
            ('community_post', 'user_id'),
            ('community_comment', 'user_id'),
            ('community_like', 'user_id'),
            ('community_media', 'user_id'),
            ('paper_plane', 'user_id'),
            ('paper_plane_reply', 'user_id'),
            ('matchmaker_service', 'user_id'),
            ('matchmaker_service', 'matchmaker_id'),
            ('matchmaker_rating', 'user_id'),
            ('matchmaker_rating', 'matchmaker_id'),
            ('offline_activity', 'created_by'),
            ('activity_signup', 'user_id'),
            ('payment_order', 'user_id'),
            ('user_read_notification', 'user_id'),
            ('feedback', 'user_id'),
            ('user_device', 'user_id'),
            ('invite_record', 'inviter_id'),
            ('invite_record', 'invitee_id'),
            ('user_feature_vector', 'user_id'),
            ('user_behavior_event', 'user_id'),
            ('user_behavior_event', 'target_user_id'),
            ('user_mbti_result', 'user_id'),
            ('user_love_style_result', 'user_id'),
            ('user_match_score_history', 'user_id'),
            ('user_match_score_history', 'target_user_id'),
            ('user_exposure', 'user_id'),
        ]

        for table, column in user_id_tables:
            self._add_foreign_key(cursor, table, column)

        self._add_foreign_key(
            cursor, 'user_report', 'reviewed_by', on_delete='SET NULL'
        )
        self._add_foreign_key(
            cursor, 'report_appeal', 'reviewed_by', on_delete='SET NULL'
        )

        # 登录日志中的会话关联允许为空，保留历史日志兼容性。
        self._add_foreign_key(cursor, 'user_login_log', 'session_id', 'user_session')
        self._add_foreign_key(cursor, 'report_appeal', 'report_id', 'user_report')
        for table_name in ('community_post', 'community_comment', 'paper_plane'):
            self._add_foreign_key(
                cursor,
                table_name,
                'moderation_report_id',
                'user_report',
                on_delete='SET NULL',
            )

        # 社区相关外键
        try:
            # community_comment.post_id → community_post.id
            cursor.execute("""
                SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'community_comment'
                AND CONSTRAINT_TYPE = 'FOREIGN KEY' AND CONSTRAINT_NAME = 'fk_community_comment_post_id'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE `community_comment`
                    ADD CONSTRAINT `fk_community_comment_post_id`
                    FOREIGN KEY (`post_id`) REFERENCES `community_post`(`id`) ON DELETE CASCADE
                """)
                logger.debug("✅ 外键 fk_community_comment_post_id 已添加")

            # community_like.target_id → community_post.id 或 community_comment.id（软外键，不强制）
            # 社区点赞的 target_id 可能是动态ID或评论ID，不添加物理外键
        except Exception as e:
            logger.debug(f"ℹ️ 社区外键处理: {e}")

        # 纸飞机回复外键
        try:
            cursor.execute("""
                SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'paper_plane_reply'
                AND CONSTRAINT_TYPE = 'FOREIGN KEY' AND CONSTRAINT_NAME = 'fk_paper_plane_reply_plane_id'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE `paper_plane_reply`
                    ADD CONSTRAINT `fk_paper_plane_reply_plane_id`
                    FOREIGN KEY (`plane_id`) REFERENCES `paper_plane`(`id`) ON DELETE CASCADE
                """)
                logger.debug("✅ 外键 fk_paper_plane_reply_plane_id 已添加")
        except Exception as e:
            logger.debug(f"ℹ️ 纸飞机外键处理: {e}")

        # 红娘评价外键
        try:
            cursor.execute("""
                SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'matchmaker_rating'
                AND CONSTRAINT_TYPE = 'FOREIGN KEY' AND CONSTRAINT_NAME = 'fk_matchmaker_rating_service_id'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE `matchmaker_rating`
                    ADD CONSTRAINT `fk_matchmaker_rating_service_id`
                    FOREIGN KEY (`service_id`) REFERENCES `matchmaker_service`(`id`) ON DELETE CASCADE
                """)
                logger.debug("✅ 外键 fk_matchmaker_rating_service_id 已添加")
        except Exception as e:
            logger.debug(f"ℹ️ 红娘评价外键处理: {e}")

        # 活动报名外键
        try:
            cursor.execute("""
                SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'activity_signup'
                AND CONSTRAINT_TYPE = 'FOREIGN KEY' AND CONSTRAINT_NAME = 'fk_activity_signup_activity_id'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE `activity_signup`
                    ADD CONSTRAINT `fk_activity_signup_activity_id`
                    FOREIGN KEY (`activity_id`) REFERENCES `offline_activity`(`id`) ON DELETE CASCADE
                """)
                logger.debug("✅ 外键 fk_activity_signup_activity_id 已添加")
        except Exception as e:
            logger.debug(f"ℹ️ 活动报名外键处理: {e}")

        # chat_message.session_id → chat_session.id
        try:
            cursor.execute("""
                SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_message'
                AND CONSTRAINT_TYPE = 'FOREIGN KEY' AND CONSTRAINT_NAME = 'fk_chat_message_session_id'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE `chat_message`
                    ADD CONSTRAINT `fk_chat_message_session_id`
                    FOREIGN KEY (`session_id`) REFERENCES `chat_session`(`id`) ON DELETE CASCADE
                """)
                logger.debug("✅ 外键 fk_chat_message_session_id 已添加")
        except Exception as e:
            logger.debug(f"ℹ️ 聊天消息外键处理: {e}")

    def init_all_tables(self, cursor):
        """初始化数据库表结构"""
        logger.info("初始化数据库表结构")
        from app.db.business_schema import BUSINESS_TABLES

        tables = {
            # ============================================
            # 1. 用户主表
            # ============================================
            'users': """
                CREATE TABLE IF NOT EXISTS `users` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `openid` varchar(64) DEFAULT NULL COMMENT '微信openid',
                    `unionid` varchar(64) DEFAULT NULL COMMENT '微信unionid',
                    `phone` varchar(20) DEFAULT NULL COMMENT '手机号',
                    `password` varchar(255) DEFAULT NULL COMMENT '密码（预留）',
                    `nickname` varchar(64) DEFAULT NULL COMMENT '昵称',
                    `avatar` varchar(255) DEFAULT NULL COMMENT '头像URL',
                    `gender` tinyint DEFAULT NULL COMMENT '性别 1男 2女',
                    `birthday` date DEFAULT NULL COMMENT '出生日期',
                    `status` tinyint DEFAULT '1' COMMENT '状态 1正常 2冻结 3注销',
                    `is_real_name` tinyint DEFAULT '0' COMMENT '是否实名认证 0否 1是',
                    `is_married` tinyint DEFAULT '0' COMMENT '婚姻状态 0未知 1未婚 2离异 3丧偶',
                    `is_single_pledge` tinyint DEFAULT '0' COMMENT '是否签署单身承诺 0否 1是',
                    `data_complete_rate` tinyint DEFAULT '0' COMMENT '资料完整度 0-100',
                    `register_ip` varchar(64) DEFAULT NULL,
                    `last_login_at` datetime DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_phone` (`phone`),
                    UNIQUE KEY `uk_openid` (`openid`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户主表'
            """,

            # ============================================
            # 1.1 用户注册意图
            # ============================================
            'user_registration_intent': """
                CREATE TABLE IF NOT EXISTS `user_registration_intent` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `intent_type` varchar(32) NOT NULL COMMENT 'self_match自己找 parent_match父母帮找 companion找搭子',
                    `source` varchar(32) DEFAULT NULL COMMENT '选择来源 register/profile',
                    `version` varchar(16) NOT NULL DEFAULT 'v1',
                    `status` tinyint NOT NULL DEFAULT '1' COMMENT '1当前有效 2已撤销',
                    `selected_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    `revoked_at` datetime DEFAULT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_intent_user` (`user_id`),
                    KEY `idx_intent_type_status` (`intent_type`,`status`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户注册意图'
            """,

            # ============================================
            # 1.2 用户平台角色
            # ============================================
            'user_role': """
                CREATE TABLE IF NOT EXISTS `user_role` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `role_code` varchar(32) NOT NULL COMMENT 'user普通用户 promoter推广红娘 partner合伙人 service_matchmaker服务红娘 admin管理员',
                    `status` tinyint NOT NULL DEFAULT '1' COMMENT '1有效 2暂停 3撤销',
                    `granted_by` bigint unsigned DEFAULT NULL,
                    `granted_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    `revoked_at` datetime DEFAULT NULL,
                    `revoke_reason` varchar(255) DEFAULT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_role` (`user_id`,`role_code`),
                    KEY `idx_role_status` (`role_code`,`status`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户平台角色'
            """,
            'admin_permission': """
                CREATE TABLE IF NOT EXISTS `admin_permission` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `permission_code` varchar(64) NOT NULL,
                    `granted_by` bigint unsigned DEFAULT NULL,
                    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_admin_permission` (`user_id`,`permission_code`),
                    KEY `idx_admin_permission_user` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='管理员权限'
            """,

            # ============================================
            # 1.3 用户登录会话
            # ============================================
            'user_session': """
                CREATE TABLE IF NOT EXISTS `user_session` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `access_token_hash` char(64) DEFAULT NULL COMMENT 'Access Token哈希',
                    `refresh_token_hash` char(64) NOT NULL COMMENT 'Refresh Token哈希',
                    `device_id` varchar(128) DEFAULT NULL,
                    `platform` varchar(32) DEFAULT NULL,
                    `app_version` varchar(32) DEFAULT NULL,
                    `ip` varchar(64) DEFAULT NULL,
                    `user_agent` varchar(512) DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `last_used_at` datetime DEFAULT NULL,
                    `access_expire_at` datetime NOT NULL,
                    `refresh_expire_at` datetime NOT NULL,
                    `revoked_at` datetime DEFAULT NULL,
                    `revoke_reason` varchar(64) DEFAULT NULL,
                    `status` tinyint NOT NULL DEFAULT '1' COMMENT '1有效 2撤销 3过期',
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_refresh_token_hash` (`refresh_token_hash`),
                    KEY `idx_session_user_status` (`user_id`,`status`),
                    KEY `idx_session_device` (`device_id`),
                    KEY `idx_session_refresh_expire` (`refresh_expire_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户登录会话'
            """,

            # ============================================
            # 1.2 协议和安全承诺签署记录
            # ============================================
            'user_agreement_acceptance': """
                CREATE TABLE IF NOT EXISTS `user_agreement_acceptance` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `agreement_type` varchar(64) NOT NULL COMMENT '协议类型',
                    `agreement_version` varchar(32) NOT NULL COMMENT '协议版本',
                    `content_hash` char(64) DEFAULT NULL COMMENT '协议内容哈希',
                    `accepted_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    `accepted_ip` varchar(64) DEFAULT NULL,
                    `device_id` varchar(128) DEFAULT NULL,
                    `scene` varchar(32) DEFAULT NULL COMMENT '注册/资料/聊天/社区',
                    `status` tinyint NOT NULL DEFAULT '1' COMMENT '1有效 2撤销',
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_agreement_version` (`user_id`,`agreement_type`,`agreement_version`),
                    KEY `idx_agreement_type_version` (`agreement_type`,`agreement_version`),
                    KEY `idx_agreement_user_time` (`user_id`,`accepted_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户协议签署记录'
            """,

            # ============================================
            # 1.3 用户择偶要求
            # ============================================
            'user_partner_preference': """
                CREATE TABLE IF NOT EXISTS `user_partner_preference` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `age_min` tinyint unsigned DEFAULT NULL,
                    `age_max` tinyint unsigned DEFAULT NULL,
                    `height_min` smallint unsigned DEFAULT NULL,
                    `height_max` smallint unsigned DEFAULT NULL,
                    `education_min` tinyint unsigned DEFAULT NULL,
                    `income_min` decimal(10,2) DEFAULT NULL,
                    `marriage_status` tinyint DEFAULT NULL COMMENT '婚姻状况要求',
                    `preferred_province_code` varchar(32) DEFAULT NULL,
                    `preferred_city_codes` json DEFAULT NULL COMMENT '期望城市编码列表',
                    `accept_long_distance` tinyint NOT NULL DEFAULT '0',
                    `accept_cross_province` tinyint NOT NULL DEFAULT '0',
                    `housing_requirement` tinyint DEFAULT NULL COMMENT '0不限 1有房 2无房',
                    `smoking_requirement` tinyint DEFAULT NULL COMMENT '0不限 1不抽烟 2可接受',
                    `drinking_requirement` tinyint DEFAULT NULL COMMENT '0不限 1不饮酒 2可接受',
                    `extra_requirement` varchar(1000) DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_preference_user` (`user_id`),
                    KEY `idx_preference_age` (`age_min`,`age_max`),
                    KEY `idx_preference_height` (`height_min`,`height_max`),
                    KEY `idx_preference_province` (`preferred_province_code`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户择偶要求'
            """,

            # ============================================
            # 1.4 用户媒体明细
            # ============================================
            'user_media': """
                CREATE TABLE IF NOT EXISTS `user_media` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `media_type` varchar(16) NOT NULL COMMENT 'avatar/photo/video',
                    `file_url` varchar(512) NOT NULL,
                    `storage_key` varchar(512) DEFAULT NULL,
                    `thumbnail_url` varchar(512) DEFAULT NULL,
                    `mime_type` varchar(128) DEFAULT NULL,
                    `file_size` bigint unsigned DEFAULT NULL,
                    `duration_seconds` smallint unsigned DEFAULT NULL,
                    `sort_order` smallint unsigned NOT NULL DEFAULT '0',
                    `is_primary` tinyint NOT NULL DEFAULT '0',
                    `review_status` tinyint NOT NULL DEFAULT '0' COMMENT '0审核中 1通过 2拒绝 3隐藏',
                    `review_reason` varchar(255) DEFAULT NULL,
                    `reviewed_at` datetime DEFAULT NULL,
                    `deleted_at` datetime DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_media_storage_key` (`storage_key`),
                    KEY `idx_media_user_type` (`user_id`,`media_type`,`deleted_at`),
                    KEY `idx_media_user_order` (`user_id`,`sort_order`),
                    KEY `idx_media_review_status` (`review_status`,`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户头像相册和视频'
            """,

            # ============================================
            # 1.5 用户资料完整度明细
            # ============================================
            'user_profile_completion': """
                CREATE TABLE IF NOT EXISTS `user_profile_completion` (
                    `user_id` bigint unsigned NOT NULL,
                    `gender_completed` tinyint NOT NULL DEFAULT '0',
                    `birthday_completed` tinyint NOT NULL DEFAULT '0',
                    `location_completed` tinyint NOT NULL DEFAULT '0',
                    `marriage_completed` tinyint NOT NULL DEFAULT '0',
                    `occupation_completed` tinyint NOT NULL DEFAULT '0',
                    `education_completed` tinyint NOT NULL DEFAULT '0',
                    `income_completed` tinyint NOT NULL DEFAULT '0',
                    `height_completed` tinyint NOT NULL DEFAULT '0',
                    `weight_completed` tinyint NOT NULL DEFAULT '0',
                    `hometown_completed` tinyint NOT NULL DEFAULT '0',
                    `avatar_completed` tinyint NOT NULL DEFAULT '0',
                    `intro_completed` tinyint NOT NULL DEFAULT '0',
                    `album_completed` tinyint NOT NULL DEFAULT '0',
                    `interest_completed` tinyint NOT NULL DEFAULT '0',
                    `preference_completed` tinyint NOT NULL DEFAULT '0',
                    `realname_completed` tinyint NOT NULL DEFAULT '0',
                    `mbti_completed` tinyint NOT NULL DEFAULT '0',
                    `single_pledge_completed` tinyint NOT NULL DEFAULT '0',
                    `score` decimal(5,2) NOT NULL DEFAULT '0.00',
                    `algorithm_version` varchar(32) DEFAULT NULL,
                    `calculated_at` datetime DEFAULT NULL,
                    PRIMARY KEY (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户资料完整度明细'
            """,

            # ============================================
            # 2. 认证信息表
            # ============================================
            'user_auth': """
                CREATE TABLE IF NOT EXISTS `user_auth` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `real_name` varchar(64) DEFAULT NULL COMMENT '真实姓名',
                    `id_card` varchar(255) DEFAULT NULL COMMENT '身份证号（加密）',
                    `id_card_front` varchar(255) DEFAULT NULL COMMENT '身份证正面',
                    `id_card_back` varchar(255) DEFAULT NULL COMMENT '身份证反面',
                    `face_photo` varchar(255) DEFAULT NULL COMMENT '人脸照片',
                    `face_verified` tinyint DEFAULT '0' COMMENT '人脸认证 0未 1通过 2失败',
                    `education` varchar(64) DEFAULT NULL COMMENT '学历',
                    `school` varchar(128) DEFAULT NULL COMMENT '学校',
                    `education_cert` varchar(255) DEFAULT NULL COMMENT '学历证书图片',
                    `education_verified` tinyint DEFAULT '0' COMMENT '学历认证状态 0未 1通过 2失败',
                    `job` varchar(128) DEFAULT NULL COMMENT '职业',
                    `company` varchar(128) DEFAULT NULL COMMENT '公司',
                    `job_cert` varchar(255) DEFAULT NULL COMMENT '工作证明图片',
                    `job_verified` tinyint DEFAULT '0' COMMENT '工作认证状态 0未 1通过 2失败',
                    `house_cert` varchar(255) DEFAULT NULL COMMENT '房产证明',
                    `house_verified` tinyint DEFAULT '0' COMMENT '房产认证状态',
                    `auth_status` tinyint DEFAULT '0' COMMENT '整体认证状态 0未提交 1审核中 2通过 3失败',
                    `auth_step` tinyint DEFAULT '0' COMMENT '认证步骤 0未提交 1资料已提交 2人脸待认证 3审核中 4已通过 5已失败',
                    `fail_reason` varchar(255) DEFAULT NULL COMMENT '失败原因',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_id` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户认证信息表'
            """,

            # ============================================
            # 3. 用户资料扩展
            # ============================================
            'user_profile': """
                CREATE TABLE IF NOT EXISTS `user_profile` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `height` int DEFAULT NULL COMMENT '身高cm',
                    `income` decimal(10,2) DEFAULT NULL COMMENT '月收入',
                    `hometown` varchar(64) DEFAULT NULL COMMENT '家乡',
                    `residence` varchar(64) DEFAULT NULL COMMENT '现居地',
                    `latitude` decimal(10,8) DEFAULT NULL COMMENT '纬度（GCJ-02）',
                    `longitude` decimal(11,8) DEFAULT NULL COMMENT '经度（GCJ-02）',
                    `mbti` varchar(8) DEFAULT NULL COMMENT 'MBTI类型',
                    `constellation` varchar(16) DEFAULT NULL COMMENT '星座',
                    `tags` json DEFAULT NULL COMMENT '个性标签 ["颜控","宠物"]',
                    `self_intro` text COMMENT '自我介绍',
                    `love_view` text COMMENT '爱情观',
                    `ideal_partner` text COMMENT '理想另一半',
                    `single_reason` varchar(255) DEFAULT NULL COMMENT '单身原因',
                    `family_background` text COMMENT '家庭背景',
                    `hobbies` text COMMENT '兴趣爱好',
                    `photos` json DEFAULT NULL COMMENT '相册图片URL列表',
                    `video` varchar(255) DEFAULT NULL COMMENT '视频自我介绍',
                    `online_status` tinyint DEFAULT '0' COMMENT '在线状态 0离线 1在线 2隐身',
                    `last_active_at` datetime DEFAULT NULL COMMENT '最近活跃时间',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_id` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户扩展资料表'
            """,

            # ============================================
            # 4. 隐私设置
            # ============================================
            'user_privacy': """
                CREATE TABLE IF NOT EXISTS `user_privacy` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `hide_phone` tinyint DEFAULT '0' COMMENT '隐藏手机号',
                    `hide_school` tinyint DEFAULT '0' COMMENT '隐藏学校',
                    `hide_company` tinyint DEFAULT '0' COMMENT '隐藏公司',
                    `hide_distance` tinyint DEFAULT '0' COMMENT '隐藏距离',
                    `hide_online_status` tinyint DEFAULT '0' COMMENT '隐藏在线状态',
                    `show_profile` tinyint NOT NULL DEFAULT '1' COMMENT '是否展示个人资料',
                    `show_likes` tinyint NOT NULL DEFAULT '1' COMMENT '是否展示喜欢列表',
                    `show_posts` tinyint NOT NULL DEFAULT '1' COMMENT '是否展示个人动态',
                    `block_colleagues` json DEFAULT NULL COMMENT '屏蔽同事/熟人 手机号列表',
                    `only_auth_can_contact` tinyint DEFAULT '0' COMMENT '仅认证用户可联系我',
                    `only_vip_can_see_detail` tinyint DEFAULT '0' COMMENT '仅会员可见详细资料',
                    `who_can_see_me` tinyint DEFAULT '1' COMMENT '1所有人 2仅认证 3仅VIP 4完全私密',
                    `match_status` tinyint DEFAULT '1' COMMENT '交友状态 1公开展示 2委托红娘 3完全私密 4暂停服务 5已脱单',
                    `notify_like` tinyint DEFAULT '1' COMMENT '点赞通知 0关闭 1开启',
                    `notify_comment` tinyint DEFAULT '1' COMMENT '评论通知',
                    `notify_follow` tinyint DEFAULT '1' COMMENT '关注通知',
                    `notify_message` tinyint NOT NULL DEFAULT '1' COMMENT '新消息通知',
                    `notify_match` tinyint DEFAULT '1' COMMENT '匹配成功通知',
                    `notify_apply` tinyint DEFAULT '1' COMMENT '牵线申请通知',
                    `notify_system` tinyint DEFAULT '1' COMMENT '系统消息通知',
                    `notify_activity` tinyint DEFAULT '1' COMMENT '活动通知',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_id` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户隐私设置表'
            """,

            # ============================================
            # 5. 登录日志
            # ============================================
            'user_login_log': """
                CREATE TABLE IF NOT EXISTS `user_login_log` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `login_type` tinyint DEFAULT '1' COMMENT '1微信 2手机号',
                    `ip` varchar(64) DEFAULT NULL,
                    `device` varchar(255) DEFAULT NULL COMMENT '设备信息',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user_id` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户登录日志'
            """,

            # ============================================
            # 6. 拉黑记录
            # ============================================
            'user_block': """
                CREATE TABLE IF NOT EXISTS `user_block` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL COMMENT '拉黑者',
                    `target_user_id` bigint unsigned NOT NULL COMMENT '被拉黑者',
                    `reason` varchar(255) DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_pair` (`user_id`,`target_user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='拉黑记录'
            """,

            'user_restriction': """
                CREATE TABLE IF NOT EXISTS `user_restriction` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `restriction_type` varchar(32) NOT NULL COMMENT 'TOTAL_BAN/POST_RESTRICTED/COMMENT_RESTRICTED/MESSAGE_RESTRICTED/APPLICATION_RESTRICTED',
                    `reason_code` varchar(64) NOT NULL,
                    `reason` varchar(255) NOT NULL,
                    `starts_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    `ends_at` datetime DEFAULT NULL,
                    `status` tinyint NOT NULL DEFAULT '1' COMMENT '1生效 2已解除',
                    `ended_at` datetime DEFAULT NULL,
                    `created_by` bigint unsigned NOT NULL,
                    `note` varchar(1000) DEFAULT NULL,
                    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user_restriction_active` (`user_id`, `status`, `restriction_type`, `ends_at`),
                    KEY `idx_user_restriction_created` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户功能限制与封禁记录'
            """,

            # ============================================
            # 7. 举报记录
            # ============================================
            'user_report': """
                CREATE TABLE IF NOT EXISTS `user_report` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL COMMENT '举报人',
                    `target_user_id` bigint unsigned NOT NULL COMMENT '被举报人/内容作者',
                    `target_type` varchar(32) NOT NULL DEFAULT 'user' COMMENT 'user|post|comment|paper_plane',
                    `target_id` bigint unsigned DEFAULT NULL COMMENT '内容对象ID；user 举报可为空或等于 target_user_id',
                    `type` varchar(64) DEFAULT NULL COMMENT '举报类型 骚扰/虚假/诈骗等',
                    `desc` text COMMENT '描述',
                    `images` json DEFAULT NULL COMMENT '截图证据',
                    `status` tinyint DEFAULT '0' COMMENT '0待处理 1已处理 2驳回',
                    `result` varchar(255) DEFAULT NULL COMMENT '处理结果',
                    `action` varchar(32) NOT NULL DEFAULT 'none' COMMENT 'none|hide_content|restore_content|dismiss',
                    `reviewed_by` bigint unsigned DEFAULT NULL COMMENT '原举报审核人',
                    `reviewed_at` datetime DEFAULT NULL COMMENT '举报审核时间',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_target` (`target_user_id`),
                    KEY `idx_target_object` (`target_type`, `target_id`),
                    KEY `idx_status_created` (`status`, `created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='举报记录'
            """,

            'report_appeal': """
                CREATE TABLE IF NOT EXISTS `report_appeal` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `report_id` bigint unsigned NOT NULL,
                    `appellant_user_id` bigint unsigned NOT NULL,
                    `reason` varchar(1000) NOT NULL,
                    `status` tinyint NOT NULL DEFAULT '0' COMMENT '0待复审 1申诉通过 2申诉驳回',
                    `result` varchar(255) DEFAULT NULL,
                    `reviewed_by` bigint unsigned DEFAULT NULL,
                    `reviewed_at` datetime DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_report_id` (`report_id`),
                    KEY `idx_appeal_status_created` (`status`,`created_at`),
                    KEY `idx_appeal_user_created` (`appellant_user_id`,`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='举报申诉与独立复审记录'
            """,

            # ============================================
            # 8. 收藏/关注/喜欢记录
            # ============================================
            'user_favorite': """
                CREATE TABLE IF NOT EXISTS `user_favorite` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `target_user_id` bigint unsigned NOT NULL,
                    `type` tinyint DEFAULT '1' COMMENT '1喜欢 2收藏 3关注',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_pair_type` (`user_id`,`target_user_id`,`type`),
                    KEY `idx_target` (`target_user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户收藏/关注/喜欢记录'
            """,

            # ============================================
            # 9. 浏览足迹
            # ============================================
            'user_browse_history': """
                CREATE TABLE IF NOT EXISTS `user_browse_history` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `target_user_id` bigint unsigned NOT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_target` (`target_user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户浏览足迹'
            """,

            # ============================================
            # 9.1 用户通知
            # ============================================
            'user_notification': """
                CREATE TABLE IF NOT EXISTS `user_notification` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `notification_type` varchar(64) NOT NULL,
                    `title` varchar(128) NOT NULL,
                    `content` varchar(255) DEFAULT NULL,
                    `payload` json DEFAULT NULL,
                    `related_user_id` bigint unsigned DEFAULT NULL,
                    `related_id` bigint unsigned DEFAULT NULL,
                    `target_type` varchar(32) DEFAULT NULL COMMENT '前端导航目标类型',
                    `target_id` bigint unsigned DEFAULT NULL COMMENT '前端导航目标ID',
                    `is_read` tinyint NOT NULL DEFAULT '0',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `read_at` datetime DEFAULT NULL,
                    PRIMARY KEY (`id`),
                    KEY `idx_user_created` (`user_id`,`created_at`),
                    KEY `idx_user_unread` (`user_id`,`is_read`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户通知'
            """,

            # ============================================
            # 9.2 用户首页筛选条件
            # ============================================
            'user_discovery_filter': """
                CREATE TABLE IF NOT EXISTS `user_discovery_filter` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `filter_json` json NOT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_id` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户首页筛选条件'
            """,

            # ============================================
            # 10. 爆灯记录
            # ============================================
            'user_boost': """
                CREATE TABLE IF NOT EXISTS `user_boost` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `target_user_id` bigint unsigned NOT NULL,
                    `amount` decimal(10,2) DEFAULT '5.00' COMMENT '支付金额',
                    `order_no` varchar(64) DEFAULT NULL COMMENT '订单号',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_target` (`target_user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='爆灯记录'
            """,

            # ============================================
            # 11. 匹配记录
            # ============================================
            'user_match': """
                CREATE TABLE IF NOT EXISTS `user_match` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `target_user_id` bigint unsigned NOT NULL,
                    `status` tinyint DEFAULT '1' COMMENT '1已匹配 2已聊天 3已取消',
                    `matched_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_pair` (`user_id`,`target_user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='匹配记录'
            """,

            # ============================================
            # 12. 积分流水
            # ============================================
            'user_points': """
                CREATE TABLE IF NOT EXISTS `user_points` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `type` tinyint DEFAULT '1' COMMENT '1签到 2任务 3邀请好友 4兑换消费',
                    `amount` int DEFAULT '0' COMMENT '变动数量（正负）',
                    `balance` int DEFAULT '0' COMMENT '变动后余额',
                    `desc` varchar(255) DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='积分流水'
            """,

            # ============================================
            # 13. 会员购买记录
            # ============================================
            'config_point_product': """
                CREATE TABLE IF NOT EXISTS `config_point_product` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `code` varchar(64) NOT NULL,
                    `name` varchar(128) NOT NULL,
                    `product_type` varchar(16) NOT NULL,
                    `points_cost` int unsigned NOT NULL,
                    `value` varchar(255) DEFAULT NULL,
                    `stock` int DEFAULT NULL,
                    `sort` int NOT NULL DEFAULT '0',
                    `is_active` tinyint NOT NULL DEFAULT '1',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_point_product_code` (`code`),
                    KEY `idx_point_product_active` (`is_active`,`sort`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,

            'user_quota_usage': """
                CREATE TABLE IF NOT EXISTS `user_quota_usage` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `quota_code` varchar(32) NOT NULL,
                    `quota_date` date NOT NULL,
                    `source` varchar(16) NOT NULL DEFAULT 'free',
                    `amount` int unsigned NOT NULL DEFAULT '1',
                    `reason` varchar(255) DEFAULT NULL,
                    `target_user_id` bigint unsigned DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_quota_user_date` (`user_id`,`quota_code`,`quota_date`),
                    KEY `idx_quota_target` (`target_user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='User quota usage ledger'
            """,

            'user_quota_grant': """
                CREATE TABLE IF NOT EXISTS `user_quota_grant` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `quota_code` varchar(32) NOT NULL,
                    `remaining` int unsigned NOT NULL DEFAULT '0',
                    `source` varchar(16) NOT NULL DEFAULT 'points',
                    `order_no` varchar(64) NOT NULL,
                    `expires_at` datetime DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_quota_grant_order` (`order_no`),
                    KEY `idx_quota_grant_user` (`user_id`,`quota_code`,`remaining`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='额外权益次数账户'
            """,

            'point_redeem_order': """
                CREATE TABLE IF NOT EXISTS `point_redeem_order` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `order_no` varchar(64) NOT NULL,
                    `user_id` bigint unsigned NOT NULL,
                    `product_id` bigint unsigned NOT NULL,
                    `product_code` varchar(64) NOT NULL,
                    `points_cost` int unsigned NOT NULL,
                    `status` tinyint NOT NULL DEFAULT '0',
                    `idempotency_key` varchar(128) NOT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_point_redeem_order_no` (`order_no`),
                    UNIQUE KEY `uk_point_redeem_idempotency` (`user_id`,`idempotency_key`),
                    KEY `idx_point_redeem_user` (`user_id`,`created_at`),
                    KEY `idx_point_redeem_status` (`status`,`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,

            'user_membership': """
                CREATE TABLE IF NOT EXISTS `user_membership` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `package_type` varchar(64) DEFAULT NULL COMMENT '套餐类型 月/季/年',
                    `amount` decimal(10,2) DEFAULT NULL,
                    `order_no` varchar(64) DEFAULT NULL,
                    `start_at` datetime DEFAULT NULL,
                    `end_at` datetime DEFAULT NULL,
                    `status` tinyint DEFAULT '1' COMMENT '1生效中 2已过期 3已退款',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会员购买记录'
            """,

            # ============================================
            # 14. 用户任务完成记录
            # ============================================
            'user_task': """
                CREATE TABLE IF NOT EXISTS `user_task` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `task_code` varchar(64) NOT NULL COMMENT '任务编码 如: certify, upload_photo, invite_friend',
                    `status` tinyint DEFAULT '1' COMMENT '1已完成 2已领奖',
                    `reward` varchar(255) DEFAULT NULL COMMENT '奖励内容',
                    `completed_at` datetime DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_task` (`user_id`,`task_code`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户任务记录'
            """,

            # ============================================
            # 15. 签到记录
            # ============================================
            'user_checkin': """
                CREATE TABLE IF NOT EXISTS `user_checkin` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `checkin_date` date NOT NULL,
                    `continuous_days` int DEFAULT '1' COMMENT '连续签到天数',
                    `points` int DEFAULT '0' COMMENT '本次签到获得积分',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_date` (`user_id`,`checkin_date`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='签到记录'
            """,

            # ============================================
            # 16. 红娘申请表
            # ============================================
            'user_matchmaker_apply': """
                CREATE TABLE IF NOT EXISTS `user_matchmaker_apply` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `application_type` varchar(32) NOT NULL DEFAULT 'service_matchmaker' COMMENT '申请类型 promoter推广红娘 partner合伙人 service_matchmaker服务红娘',
                    `real_name` varchar(64) DEFAULT NULL,
                    `phone` varchar(20) DEFAULT NULL,
                    `intro` text COMMENT '自我介绍/优势',
                    `cert_images` json DEFAULT NULL COMMENT '资质证书图片',
                    `application_details` json DEFAULT NULL COMMENT '红娘审核扩展资料',
                    `status` tinyint DEFAULT '0' COMMENT '0待审核 1通过 2驳回',
                    `fail_reason` varchar(255) DEFAULT NULL,
                    `reviewed_by` bigint unsigned DEFAULT NULL,
                    `reviewed_at` datetime DEFAULT NULL,
                    `suspended_at` datetime DEFAULT NULL,
                    `suspension_reason` varchar(255) DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_id_type` (`user_id`,`application_type`),
                    KEY `idx_application_type_status` (`application_type`,`status`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='红娘申请表'
            """,

            # ============================================
            # 17. 推荐记录
            # ============================================
            'user_match_recommend': """
                CREATE TABLE IF NOT EXISTS `user_match_recommend` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL COMMENT '被推荐给谁',
                    `recommend_user_id` bigint unsigned NOT NULL COMMENT '推荐了谁',
                    `recommend_date` date NOT NULL COMMENT '推荐日期',
                    `match_score` decimal(5,2) DEFAULT '0.00' COMMENT 'AI匹配度分数',
                    `match_reason` varchar(255) DEFAULT NULL COMMENT '匹配理由（同乡/同校/兴趣重合等）',
                    `recommend_source` varchar(16) DEFAULT 'system' COMMENT '推荐来源 system/ai/manual',
                    `is_viewed` tinyint DEFAULT '0' COMMENT '是否已查看 0否 1是',
                    `is_liked` tinyint DEFAULT '0' COMMENT '是否已点喜欢 0否 1是',
                    `is_passed` tinyint DEFAULT '0' COMMENT '是否已划过 0否 1是',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user_date` (`user_id`,`recommend_date`),
                    KEY `idx_recommend_user` (`recommend_user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每日推荐记录'
            """,

            # ============================================
            # 18. 滑动行为记录
            # ============================================
            'user_swipe_record': """
                CREATE TABLE IF NOT EXISTS `user_swipe_record` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `target_user_id` bigint unsigned NOT NULL,
                    `action` tinyint NOT NULL COMMENT '1喜欢(❤️) 2无感(划过) 3申请认识(牵线)',
                    `scene` varchar(32) DEFAULT 'recommend' COMMENT '场景: recommend推荐页 / plaza广场 / community社区',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_target` (`target_user_id`),
                    UNIQUE KEY `uk_user_target_action` (`user_id`,`target_user_id`,`action`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='滑动行为记录'
            """,

            # ============================================
            # 19. 牵线申请记录
            # ============================================
            'match_apply': """
                CREATE TABLE IF NOT EXISTS `match_apply` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `from_user_id` bigint unsigned NOT NULL COMMENT '发起方',
                    `to_user_id` bigint unsigned NOT NULL COMMENT '接收方',
                    `message` varchar(255) DEFAULT NULL COMMENT '申请留言',
                    `status` tinyint DEFAULT '0' COMMENT '0待处理 1同意 2拒绝 3已过期',
                    `responded_at` datetime DEFAULT NULL COMMENT '处理时间',
                    `expire_at` datetime DEFAULT NULL COMMENT '过期时间（默认72小时）',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_from` (`from_user_id`),
                    KEY `idx_to` (`to_user_id`),
                    KEY `idx_status` (`status`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='牵线申请记录'
            """,

            # ============================================
            # 20. 聊天会话
            # ============================================
            'chat_session': """
                CREATE TABLE IF NOT EXISTS `chat_session` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user1_id` bigint unsigned NOT NULL,
                    `user2_id` bigint unsigned NOT NULL,
                    `last_message` text COMMENT '最后一条消息',
                    `last_message_time` datetime DEFAULT NULL COMMENT '最后消息时间',
                    `unread_count_user1` int DEFAULT '0' COMMENT 'user1未读数',
                    `unread_count_user2` int DEFAULT '0' COMMENT 'user2未读数',
                    `is_user1_hidden` tinyint DEFAULT '0' COMMENT 'user1是否删除会话',
                    `is_user2_hidden` tinyint DEFAULT '0' COMMENT 'user2是否删除会话',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_pair` (`user1_id`,`user2_id`),
                    KEY `idx_user1` (`user1_id`),
                    KEY `idx_user2` (`user2_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='聊天会话'
            """,

            # ============================================
            # 21. 聊天消息
            # ============================================
            'chat_message': """
                CREATE TABLE IF NOT EXISTS `chat_message` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `session_id` bigint unsigned NOT NULL,
                    `from_user_id` bigint unsigned NOT NULL,
                    `to_user_id` bigint unsigned NOT NULL,
                    `type` tinyint DEFAULT '1' COMMENT '1文本 2图片 3语音 4视频 5小程序卡片 6系统消息',
                    `content` text COMMENT '消息内容',
                    `media_url` varchar(500) DEFAULT NULL COMMENT '媒体文件URL',
                    `is_read` tinyint DEFAULT '0' COMMENT '是否已读 0否 1是',
                    `read_at` datetime DEFAULT NULL,
                    `revoked_at` datetime DEFAULT NULL COMMENT '撤回时间（NULL表示未撤回）',
                    `quote_message_id` bigint unsigned DEFAULT NULL COMMENT '引用的消息ID（回复引用）',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_session` (`session_id`),
                    KEY `idx_from_to` (`from_user_id`,`to_user_id`),
                    KEY `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='聊天消息'
            """,

            # ============================================
            # AI 助手会话与消息
            # ============================================
            'ai_assistant_session': """
                CREATE TABLE IF NOT EXISTS `ai_assistant_session` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `title` varchar(80) NOT NULL DEFAULT 'AI助手会话',
                    `status` tinyint NOT NULL DEFAULT '1',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_ai_session_user` (`user_id`,`status`,`updated_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI助手会话'
            """,
            'ai_assistant_message': """
                CREATE TABLE IF NOT EXISTS `ai_assistant_message` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `session_id` bigint unsigned NOT NULL,
                    `role` varchar(16) NOT NULL,
                    `content` text NOT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_ai_message_session` (`session_id`,`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI助手消息'
            """,

            # ============================================
            'ai_advisor_session': """
                CREATE TABLE IF NOT EXISTS `ai_advisor_session` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `advisor_type` varchar(32) NOT NULL DEFAULT 'relationship',
                    `chat_session_id` bigint unsigned DEFAULT NULL,
                    `title` varchar(80) NOT NULL DEFAULT 'Relationship advisor',
                    `status` tinyint NOT NULL DEFAULT '1',
                    `deleted_at` datetime DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_ai_advisor_session_user` (`user_id`,`status`,`updated_at`),
                    KEY `idx_ai_advisor_session_chat` (`chat_session_id`,`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI relationship advisor session'
            """,
            'ai_advisor_message': """
                CREATE TABLE IF NOT EXISTS `ai_advisor_message` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `session_id` bigint unsigned NOT NULL,
                    `user_id` bigint unsigned NOT NULL,
                    `role` varchar(16) NOT NULL,
                    `scenario` varchar(32) DEFAULT NULL,
                    `input_text` text DEFAULT NULL,
                    `output_json` json DEFAULT NULL,
                    `risk_level` varchar(16) NOT NULL DEFAULT 'none',
                    `status` varchar(16) NOT NULL DEFAULT 'success',
                    `model_name` varchar(128) DEFAULT NULL,
                    `prompt_version` varchar(64) DEFAULT NULL,
                    `knowledge_version` varchar(64) DEFAULT NULL,
                    `request_id` varchar(64) DEFAULT NULL,
                    `latency_ms` int DEFAULT NULL,
                    `quota_consumed` tinyint NOT NULL DEFAULT '0',
                    `quota_refunded` tinyint NOT NULL DEFAULT '0',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_ai_advisor_message_session` (`session_id`,`created_at`),
                    KEY `idx_ai_advisor_message_user` (`user_id`,`created_at`),
                    KEY `idx_ai_advisor_message_request` (`request_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI relationship advisor message'
            """,
            'ai_advisor_knowledge': """
                CREATE TABLE IF NOT EXISTS `ai_advisor_knowledge` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `advisor_type` varchar(32) NOT NULL DEFAULT 'relationship',
                    `category` varchar(32) NOT NULL,
                    `scenario` varchar(32) NOT NULL,
                    `relationship_stage` varchar(32) NOT NULL DEFAULT 'new',
                    `tone` varchar(32) NOT NULL DEFAULT 'natural',
                    `content` varchar(1000) NOT NULL,
                    `reason` varchar(1000) DEFAULT NULL,
                    `risk_level` varchar(16) NOT NULL DEFAULT 'low',
                    `source` varchar(255) DEFAULT NULL,
                    `version` varchar(64) NOT NULL DEFAULT 'seed-v1',
                    `enabled` tinyint NOT NULL DEFAULT '1',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_ai_advisor_knowledge_lookup` (`advisor_type`,`scenario`,`relationship_stage`,`tone`,`enabled`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI relationship advisor knowledge'
            """,
            'ai_advisor_feedback': """
                CREATE TABLE IF NOT EXISTS `ai_advisor_feedback` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `message_id` bigint unsigned NOT NULL,
                    `user_id` bigint unsigned NOT NULL,
                    `feedback_type` varchar(32) NOT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_ai_advisor_feedback_user_message` (`message_id`,`user_id`),
                    KEY `idx_ai_advisor_feedback_user` (`user_id`,`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI relationship advisor feedback'
            """,

            # 22. 社区动态
            # ============================================
            'community_post': """
                CREATE TABLE IF NOT EXISTS `community_post` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `topic_id` bigint unsigned DEFAULT NULL COMMENT '话题ID',
                    `content` text COMMENT '文字内容',
                    `images` json DEFAULT NULL COMMENT '图片URL列表',
                    `video` varchar(255) DEFAULT NULL COMMENT '视频URL',
                    `location` varchar(128) DEFAULT NULL COMMENT '位置信息',
                    `visibility` tinyint NOT NULL DEFAULT '0' COMMENT '0公开 1仅好友 2仅自己',
                    `declaration` varchar(32) NOT NULL DEFAULT '' COMMENT '内容声明',
                    `moderation_reason` varchar(255) DEFAULT NULL,
                    `moderated_by` bigint unsigned DEFAULT NULL,
                    `moderated_at` datetime DEFAULT NULL,
                    `view_count` int DEFAULT '0' COMMENT '浏览次数',
                    `like_count` int DEFAULT '0' COMMENT '点赞数',
                    `comment_count` int DEFAULT '0' COMMENT '评论数',
                    `status` tinyint DEFAULT '1' COMMENT '1正常 2审核中 3违规下架',
                    `deleted_at` datetime DEFAULT NULL COMMENT '用户软删除时间',
                    `moderation_status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 0审核中 2管理员下架',
                    `moderation_report_id` bigint unsigned DEFAULT NULL COMMENT '当前下架来源举报ID',
                    `is_top` tinyint DEFAULT '0' COMMENT '是否置顶 0否 1是',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_topic` (`topic_id`),
                    KEY `idx_created` (`created_at`),
                    KEY `idx_status_top_created` (`status`,`is_top`,`created_at`),
                    KEY `idx_status_like_created` (`status`,`like_count`,`created_at`)
                    ,KEY `idx_post_visibility_state` (`status`,`moderation_status`,`deleted_at`,`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='社区动态'
            """,

            # ============================================
            # 23. 动态评论
            # ============================================
            'community_comment': """
                CREATE TABLE IF NOT EXISTS `community_comment` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `post_id` bigint unsigned NOT NULL,
                    `user_id` bigint unsigned NOT NULL,
                    `parent_id` bigint unsigned DEFAULT NULL COMMENT '父评论ID（支持二级回复）',
                    `root_id` bigint unsigned DEFAULT NULL COMMENT '一级根评论ID；一级评论为空',
                    `content` varchar(500) NOT NULL,
                    `moderation_reason` varchar(255) DEFAULT NULL,
                    `moderated_by` bigint unsigned DEFAULT NULL,
                    `moderated_at` datetime DEFAULT NULL,
                    `like_count` int DEFAULT '0',
                    `status` tinyint DEFAULT '1' COMMENT '兼容生命周期状态；新删除使用 deleted_at',
                    `deleted_at` datetime DEFAULT NULL COMMENT '用户软删除时间',
                    `moderation_status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 0审核中 2管理员下架',
                    `moderation_report_id` bigint unsigned DEFAULT NULL COMMENT '当前下架来源举报ID',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_post` (`post_id`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_parent` (`parent_id`)
                    ,KEY `idx_root_created` (`root_id`,`id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='动态评论'
            """,

            # ============================================
            # 24. 动态点赞
            # ============================================
            'community_like': """
                CREATE TABLE IF NOT EXISTS `community_like` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `target_id` bigint unsigned NOT NULL,
                    `type` tinyint DEFAULT '1' COMMENT '1动态 2评论 3收藏',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_target` (`user_id`,`target_id`,`type`),
                    KEY `idx_target` (`target_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='动态点赞'
            """,

            # ============================================
            # 25. 话题分类
            # ============================================
            'community_topic': """
                CREATE TABLE IF NOT EXISTS `community_topic` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `name` varchar(64) NOT NULL COMMENT '话题名 如 诚意帖/同乡/树洞',
                    `icon` varchar(255) DEFAULT NULL,
                    `sort` int DEFAULT '0',
                    `is_active` tinyint DEFAULT '1',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_name` (`name`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='话题分类'
            """,

            # ============================================
            # 25b. 话题参与
            # ============================================
            'community_topic_participant': """
                CREATE TABLE IF NOT EXISTS `community_topic_participant` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `topic_id` bigint unsigned NOT NULL,
                    `user_id` bigint unsigned NOT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_topic_user` (`topic_id`, `user_id`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_topic_created` (`topic_id`, `created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='话题参与'
            """,

            # ============================================
            # 25c. 社区动态与纸飞机媒体
            # ============================================
            'community_media': """
                CREATE TABLE IF NOT EXISTS `community_media` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `purpose` varchar(32) NOT NULL COMMENT 'post|paper_plane',
                    `media_type` varchar(16) NOT NULL COMMENT 'image|video',
                    `file_url` varchar(512) NOT NULL,
                    `storage_key` varchar(512) NOT NULL,
                    `thumbnail_url` varchar(512) DEFAULT NULL,
                    `mime_type` varchar(128) DEFAULT NULL,
                    `file_size` bigint unsigned DEFAULT NULL,
                    `duration_seconds` smallint unsigned DEFAULT NULL,
                    `moderation_status` varchar(24) NOT NULL DEFAULT 'pending' COMMENT 'pending/approved/rejected/hidden',
                    `moderation_reason` varchar(255) DEFAULT NULL,
                    `moderated_by` bigint unsigned DEFAULT NULL,
                    `moderated_at` datetime DEFAULT NULL,
                    `status` varchar(16) NOT NULL DEFAULT 'ready' COMMENT 'ready|bound|deleted',
                    `deleted_at` datetime DEFAULT NULL,
                    `expire_at` datetime DEFAULT NULL COMMENT '未绑定媒体过期时间',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_community_media_storage_key` (`storage_key`),
                    KEY `idx_community_media_user_purpose` (`user_id`,`purpose`,`status`,`deleted_at`),
                    KEY `idx_community_media_expire` (`status`,`expire_at`,`deleted_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='社区动态与纸飞机媒体'
            """,

            # ============================================
            # 25d. 社区媒体绑定关系
            # ============================================
            'community_media_attachment': """
                CREATE TABLE IF NOT EXISTS `community_media_attachment` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `media_id` bigint unsigned NOT NULL,
                    `target_type` varchar(32) NOT NULL COMMENT 'post|paper_plane',
                    `target_id` bigint unsigned NOT NULL,
                    `sort_order` smallint unsigned NOT NULL DEFAULT '0',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_community_media_attachment_media` (`media_id`),
                    KEY `idx_community_media_attachment_target` (`target_type`,`target_id`,`sort_order`),
                    CONSTRAINT `fk_community_media_attachment_media`
                        FOREIGN KEY (`media_id`) REFERENCES `community_media`(`id`) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='社区媒体绑定关系'
            """,

            # ============================================
            # 26. 纸飞机（漂流瓶）
            # ============================================
            'paper_plane': """
                CREATE TABLE IF NOT EXISTS `paper_plane` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL COMMENT '发送者',
                    `content` text NOT NULL COMMENT '内容（文字/图片）',
                    `images` json DEFAULT NULL COMMENT '图片列表',
                    `city` varchar(64) DEFAULT NULL COMMENT '同城城市',
                    `tags` json DEFAULT NULL COMMENT '标签 如 三观/情感/吐槽',
                    `is_anonymous` tinyint DEFAULT '1' COMMENT '是否匿名 0否 1是',
                    `voice_url` varchar(500) DEFAULT NULL COMMENT '语音地址',
                    `voice_duration_sec` int DEFAULT NULL COMMENT '语音时长秒',
                    `reply_count` int DEFAULT '0',
                    `like_count` int DEFAULT '0',
                    `status` tinyint DEFAULT '1' COMMENT '1待回应 2已回应 3已过期',
                    `moderation_status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 2下架（与 lifecycle status 分离）',
                    `moderation_report_id` bigint unsigned DEFAULT NULL COMMENT '当前下架来源举报ID',
                    `expire_at` datetime DEFAULT NULL COMMENT '过期时间（默认24小时）',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_city` (`city`),
                    KEY `idx_status` (`status`),
                    KEY `idx_moderation_status` (`moderation_status`),
                    KEY `idx_created` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='纸飞机（漂流瓶）'
            """,

            # ============================================
            # 27. 纸飞机回复
            # ============================================
            'paper_plane_reply': """
                CREATE TABLE IF NOT EXISTS `paper_plane_reply` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `plane_id` bigint unsigned NOT NULL,
                    `user_id` bigint unsigned NOT NULL COMMENT '回复者',
                    `content` text NOT NULL,
                    `moderation_status` varchar(24) NOT NULL DEFAULT 'approved' COMMENT 'pending/approved/rejected/hidden',
                    `moderation_reason` varchar(255) DEFAULT NULL,
                    `moderated_by` bigint unsigned DEFAULT NULL,
                    `moderated_at` datetime DEFAULT NULL,
                    `is_anonymous` tinyint DEFAULT '1',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_plane` (`plane_id`),
                    KEY `idx_user` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='纸飞机回复'
            """,

            # ============================================
            # 27b. 纸飞机匿名会话
            # ============================================
            'paper_plane_conversation': """
                CREATE TABLE IF NOT EXISTS `paper_plane_conversation` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `plane_id` bigint unsigned NOT NULL,
                    `owner_id` bigint unsigned NOT NULL COMMENT '纸飞机主人',
                    `replier_id` bigint unsigned NOT NULL COMMENT '回复者',
                    `status` tinyint DEFAULT '1' COMMENT '1进行中 2已结束',
                    `last_message` varchar(200) DEFAULT NULL,
                    `last_message_at` datetime DEFAULT NULL,
                    `owner_unread` int DEFAULT '0',
                    `replier_unread` int DEFAULT '0',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_plane_replier` (`plane_id`,`replier_id`),
                    KEY `idx_owner` (`owner_id`),
                    KEY `idx_replier` (`replier_id`),
                    KEY `idx_last_message` (`last_message_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='纸飞机匿名会话'
            """,

            # ============================================
            # 27c. 纸飞机匿名会话消息
            # ============================================
            'paper_plane_message': """
                CREATE TABLE IF NOT EXISTS `paper_plane_message` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `conversation_id` bigint unsigned NOT NULL,
                    `from_user_id` bigint unsigned NOT NULL,
                    `content` text,
                    `moderation_status` varchar(24) NOT NULL DEFAULT 'approved' COMMENT 'pending/approved/rejected/hidden',
                    `moderation_reason` varchar(255) DEFAULT NULL,
                    `moderated_by` bigint unsigned DEFAULT NULL,
                    `moderated_at` datetime DEFAULT NULL,
                    `type` tinyint DEFAULT '1' COMMENT '1文本 3语音',
                    `media_url` varchar(500) DEFAULT NULL,
                    `voice_duration_sec` int DEFAULT NULL,
                    `reply_id` bigint unsigned DEFAULT NULL COMMENT '关联首次 paper_plane_reply',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_conversation` (`conversation_id`),
                    KEY `idx_from_user` (`from_user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='纸飞机匿名会话消息'
            """,

            # ============================================
            # 28. API 幂等记录
            # ============================================
            'api_idempotency_record': """
                CREATE TABLE IF NOT EXISTS `api_idempotency_record` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `operation` varchar(64) NOT NULL COMMENT '受保护的创建操作',
                    `idempotency_key` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL COMMENT '客户端幂等键',
                    `payload_hash` char(64) NOT NULL COMMENT '规范化请求载荷SHA-256',
                    `state` varchar(16) NOT NULL DEFAULT 'reserved' COMMENT 'reserved/completed',
                    `owner_token` char(36) NOT NULL COMMENT '当前预留所有者',
                    `response_json` json DEFAULT NULL COMMENT '完成响应快照',
                    `created_at` datetime(6) NOT NULL,
                    `updated_at` datetime(6) NOT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_api_idempotency_scope` (`user_id`,`operation`,`idempotency_key`),
                    KEY `idx_api_idempotency_state_updated` (`state`,`updated_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='API幂等预留与响应快照'
            """,

            # ============================================
            # 29. 红娘服务订单
            # ============================================
            'matchmaker_service': """
                CREATE TABLE IF NOT EXISTS `matchmaker_service` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL COMMENT '用户',
                    `matchmaker_id` bigint unsigned DEFAULT NULL COMMENT '红娘ID（users表）',
                    `service_type` tinyint DEFAULT '1' COMMENT '1付费牵线 3私人定制',
                    `status` tinyint DEFAULT '0' COMMENT '0已通过待开始 1服务中 2服务完成 3已取消/退款',
                    `order_id` bigint unsigned DEFAULT NULL COMMENT '关联支付订单ID',
                    `product_id` bigint unsigned DEFAULT NULL COMMENT '红娘服务商品ID',
                    `requirement` text COMMENT '用户需求描述',
                    `feedback` text COMMENT '红娘反馈',
                    `start_at` datetime DEFAULT NULL,
                    `end_at` datetime DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_matchmaker` (`matchmaker_id`),
                    KEY `idx_matchmaker_service_order` (`order_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='红娘服务订单'
            """,

            # ============================================
            # 29. 红娘评价
            # ============================================
            'matchmaker_rating': """
                CREATE TABLE IF NOT EXISTS `matchmaker_rating` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `service_id` bigint unsigned NOT NULL,
                    `user_id` bigint unsigned NOT NULL,
                    `matchmaker_id` bigint unsigned NOT NULL,
                    `score` tinyint DEFAULT '5' COMMENT '评分 1-5',
                    `content` varchar(500) DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_matchmaker` (`matchmaker_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='红娘评价'
            """,

            # ============================================
            # 30. 线下活动
            # ============================================
            'offline_activity': """
                CREATE TABLE IF NOT EXISTS `offline_activity` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `title` varchar(128) NOT NULL,
                    `cover` varchar(255) DEFAULT NULL COMMENT '封面图',
                    `type` varchar(64) DEFAULT NULL COMMENT '活动类型 1v1情感咨询/线下脱单局/竞争力评分等',
                    `city` varchar(64) DEFAULT NULL COMMENT '城市',
                    `address` varchar(255) DEFAULT NULL COMMENT '详细地址（报名后可见）',
                    `start_time` datetime NOT NULL,
                    `end_time` datetime NOT NULL,
                    `signup_deadline` datetime DEFAULT NULL COMMENT '报名截止时间',
                    `max_people` int DEFAULT '0' COMMENT '人数上限',
                    `current_people` int DEFAULT '0' COMMENT '已报名人数',
                    `price` decimal(10,2) DEFAULT '0.00' COMMENT '报名费',
                    `status` tinyint DEFAULT '1' COMMENT '1招募中 2已满 3进行中 4已结束 5已取消',
                    `description` text COMMENT '活动详情',
                    `created_by` bigint unsigned DEFAULT NULL COMMENT '创建人（运营）',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_city` (`city`),
                    KEY `idx_time` (`start_time`),
                    KEY `idx_status` (`status`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='线下活动'
            """,

            # ============================================
            # 31. 活动报名
            # ============================================
            'activity_signup': """
                CREATE TABLE IF NOT EXISTS `activity_signup` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `activity_id` bigint unsigned NOT NULL,
                    `user_id` bigint unsigned NOT NULL,
                    `real_name` varchar(64) DEFAULT NULL COMMENT '真实姓名（报名时填）',
                    `phone` varchar(20) DEFAULT NULL COMMENT '联系电话',
                    `remark` varchar(255) DEFAULT NULL COMMENT '备注',
                    `status` tinyint DEFAULT '0' COMMENT '0待审核 1报名成功 2已取消 3已拒绝',
                    `cancel_reason` varchar(255) DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_activity_user` (`activity_id`,`user_id`),
                    KEY `idx_user` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='活动报名'
            """,

            # ============================================
            # 32. 支付订单
            # ============================================
            'payment_order': """
                CREATE TABLE IF NOT EXISTS `payment_order` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `order_no` varchar(64) NOT NULL COMMENT '订单号',
                    `type` tinyint DEFAULT '1' COMMENT '1会员 2爆灯 3牵线套餐 4置顶曝光 5活动报名 6虚拟商品',
                    `product_id` bigint DEFAULT NULL COMMENT '商品ID',
                    `product_type` varchar(64) DEFAULT NULL COMMENT '商品编码，如 monthly、superlike、boost_7d',
                    `product_name` varchar(128) DEFAULT NULL,
                    `amount` decimal(10,2) NOT NULL COMMENT '金额',
                    `pay_type` tinyint DEFAULT '1' COMMENT '1微信支付 2余额 3积分',
                    `status` tinyint DEFAULT '0' COMMENT '0待支付 1支付成功 2支付失败 3已退款',
                    `transaction_id` varchar(64) DEFAULT NULL COMMENT '微信支付交易号',
                    `pay_time` datetime DEFAULT NULL,
                    `refund_time` datetime DEFAULT NULL,
                    `expire_at` datetime DEFAULT NULL COMMENT '支付过期时间',
                    `idempotency_key` varchar(128) DEFAULT NULL COMMENT '客户端幂等键',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_order_no` (`order_no`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_status` (`status`),
                    UNIQUE KEY `uk_payment_order_type_idempotency` (`user_id`,`type`,`idempotency_key`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='支付订单'
            """,

            # ============================================
            # 33. 系统通知
            # ============================================
            'system_notification': """
                CREATE TABLE IF NOT EXISTS `system_notification` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `type` varchar(32) NOT NULL COMMENT 'like/comment/follow/match/apply/system/activity',
                    `title` varchar(128) DEFAULT NULL,
                    `content` varchar(500) NOT NULL,
                    `target_url` varchar(255) DEFAULT NULL COMMENT '跳转链接',
                    `target_id` bigint DEFAULT NULL COMMENT '关联ID（动态ID/用户ID等）',
                    `is_global` tinyint DEFAULT '0' COMMENT '是否全局通知 0否 1是',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_type` (`type`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='系统通知'
            """,

            # ============================================
            # 34. 用户通知已读记录
            # ============================================
            'user_read_notification': """
                CREATE TABLE IF NOT EXISTS `user_read_notification` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `notification_id` bigint unsigned NOT NULL,
                    `is_read` tinyint DEFAULT '1',
                    `read_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_noti` (`user_id`,`notification_id`),
                    KEY `idx_user` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户通知已读记录'
            """,

            # ============================================
            # 35. 意见反馈
            # ============================================
            'feedback': """
                CREATE TABLE IF NOT EXISTS `feedback` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `type` tinyint DEFAULT '1' COMMENT '1Bug反馈 2功能建议 3体验问题 4其他',
                    `content` text NOT NULL,
                    `images` json DEFAULT NULL,
                    `contact` varchar(64) DEFAULT NULL COMMENT '联系方式',
                    `status` tinyint DEFAULT '0' COMMENT '0待处理 1已处理 2已采纳 3不处理',
                    `reply` text COMMENT '处理回复',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='意见反馈'
            """,

            # ============================================
            # 36. 用户设备信息
            # ============================================
            'user_device': """
                CREATE TABLE IF NOT EXISTS `user_device` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `device_id` varchar(128) DEFAULT NULL COMMENT '设备唯一标识',
                    `platform` varchar(32) DEFAULT NULL COMMENT 'ios/android',
                    `os_version` varchar(32) DEFAULT NULL,
                    `app_version` varchar(32) DEFAULT NULL,
                    `push_token` varchar(255) DEFAULT NULL COMMENT '推送token',
                    `last_active_at` datetime DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_device` (`device_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户设备信息'
            """,

            # ============================================
            # 37. 邀请好友记录
            # ============================================
            'invite_record': """
                CREATE TABLE IF NOT EXISTS `invite_record` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `inviter_id` bigint unsigned NOT NULL COMMENT '邀请人',
                    `invitee_id` bigint unsigned NOT NULL COMMENT '被邀请人',
                    `invite_code` varchar(32) DEFAULT NULL COMMENT '邀请码',
                    `status` tinyint DEFAULT '0' COMMENT '0已邀请 1已注册 2已实名 3已奖励',
                    `reward_points` int DEFAULT '0' COMMENT '奖励积分',
                    `reward_membership_days` int DEFAULT '0' COMMENT '奖励会员天数',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_invitee` (`invitee_id`),
                    KEY `idx_inviter` (`inviter_id`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='邀请好友记录'
            """,

            # ============================================
            # 38. 会员套餐配置
            # ============================================
            'config_membership_package': """
                CREATE TABLE IF NOT EXISTS `config_membership_package` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `name` varchar(64) NOT NULL COMMENT '套餐名称 如 月度会员/季度会员/年度会员',
                    `code` varchar(32) NOT NULL COMMENT '套餐编码 monthly/quarterly/yearly',
                    `duration_days` int NOT NULL COMMENT '有效天数',
                    `price` decimal(10,2) NOT NULL COMMENT '价格（元）',
                    `original_price` decimal(10,2) DEFAULT NULL COMMENT '原价（划线价）',
                    `daily_price` decimal(10,2) DEFAULT NULL COMMENT '日均价（展示用）',
                    `sort` int DEFAULT '0' COMMENT '排序 越小越靠前',
                    `is_active` tinyint DEFAULT '1' COMMENT '是否上架 0否 1是',
                    `is_recommend` tinyint DEFAULT '0' COMMENT '是否推荐套餐 0否 1是',
                    `badge` varchar(32) DEFAULT NULL COMMENT '角标 如 热销/超值',
                    `rights` json NOT NULL COMMENT '权益列表 ["不限申请次数","无痕浏览","优先曝光","查看最近来访","高级筛选","专属标识"]',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_code` (`code`),
                    KEY `idx_active_sort` (`is_active`,`sort`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会员套餐配置'
            """,

            # ============================================
            # 39. 爆灯/置顶曝光套餐配置
            # ============================================
            'config_boost_package': """
                CREATE TABLE IF NOT EXISTS `config_boost_package` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `type` tinyint NOT NULL COMMENT '1爆灯 2置顶曝光',
                    `name` varchar(64) NOT NULL COMMENT '套餐名称',
                    `duration_days` int DEFAULT '0' COMMENT '有效天数（置顶曝光用）',
                    `price` decimal(10,2) NOT NULL COMMENT '价格',
                    `sort` int DEFAULT '0',
                    `is_active` tinyint DEFAULT '1',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_type_active` (`type`,`is_active`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='爆灯/置顶曝光套餐配置'
            """,

            # ============================================
            # 40. 首页Banner配置
            # ============================================
            'config_banner': """
                CREATE TABLE IF NOT EXISTS `config_banner` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `title` varchar(128) DEFAULT NULL COMMENT '标题',
                    `image_url` varchar(255) NOT NULL COMMENT '图片URL',
                    `link_type` varchar(32) DEFAULT NULL COMMENT '跳转类型 activity/url/miniprogram/none',
                    `link_value` varchar(500) DEFAULT NULL COMMENT '跳转值（活动ID/URL/小程序路径）',
                    `sort` int DEFAULT '0' COMMENT '排序',
                    `position` varchar(32) DEFAULT 'home' COMMENT '位置 home首页 / community社区',
                    `is_active` tinyint DEFAULT '1',
                    `start_at` datetime DEFAULT NULL COMMENT '生效开始时间',
                    `end_at` datetime DEFAULT NULL COMMENT '生效结束时间',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_position_active_time` (`position`,`is_active`,`start_at`,`end_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Banner配置'
            """,

            # ============================================
            # 41. 活动模板配置
            # ============================================
            'config_activity_template': """
                CREATE TABLE IF NOT EXISTS `config_activity_template` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `type` varchar(64) NOT NULL COMMENT '活动类型 1v1情感咨询/线下脱单局/竞争力评分/黑心媒婆大赛',
                    `name` varchar(64) NOT NULL,
                    `icon` varchar(255) DEFAULT NULL,
                    `default_cover` varchar(255) DEFAULT NULL COMMENT '默认封面图',
                    `default_price` decimal(10,2) DEFAULT '0.00',
                    `description_template` text COMMENT '活动描述模板（支持占位符）',
                    `is_active` tinyint DEFAULT '1',
                    `sort` int DEFAULT '0',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_type` (`type`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='活动模板配置'
            """,

            # ============================================
            # 42. 任务奖励规则配置
            # ============================================
            'config_reward_rule': """
                CREATE TABLE IF NOT EXISTS `config_reward_rule` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `task_code` varchar(64) NOT NULL COMMENT '任务编码',
                    `task_name` varchar(64) NOT NULL COMMENT '任务名称',
                    `task_type` tinyint DEFAULT '1' COMMENT '1新手任务 2日常任务 3成长任务',
                    `reward_type` tinyint DEFAULT '1' COMMENT '1积分 2纸飞机次数 3会员天数 4曝光卡 5牵线次数',
                    `reward_value` int DEFAULT '0' COMMENT '奖励值（数量）',
                    `daily_limit` int DEFAULT '0' COMMENT '每日可完成次数（0不限）',
                    `is_active` tinyint DEFAULT '1',
                    `sort` int DEFAULT '0',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_task_code` (`task_code`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='任务奖励规则配置'
            """,

            # ============================================
            # 43. 敏感词库
            # ============================================
            'config_sensitive_word': """
                CREATE TABLE IF NOT EXISTS `config_sensitive_word` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `word` varchar(64) NOT NULL COMMENT '敏感词',
                    `category` varchar(32) DEFAULT NULL COMMENT '分类 政治/色情/暴力/诈骗等',
                    `level` tinyint DEFAULT '1' COMMENT '严重等级 1-3',
                    `action` varchar(24) NOT NULL DEFAULT 'replace' COMMENT 'reject/replace/manual_review',
                    `is_active` tinyint DEFAULT '1',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_word` (`word`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='敏感词库'
            """,
            'community_moderation_task': """
                CREATE TABLE IF NOT EXISTS `community_moderation_task` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `target_type` varchar(32) NOT NULL,
                    `target_id` bigint unsigned NOT NULL,
                    `user_id` bigint unsigned NOT NULL,
                    `status` varchar(24) NOT NULL DEFAULT 'pending' COMMENT 'pending/approved/rejected/replaced/deleted/hidden',
                    `risk_level` tinyint NOT NULL DEFAULT '1',
                    `provider` varchar(32) NOT NULL DEFAULT 'local' COMMENT 'local/aliyun_market/manual',
                    `matched_words` json DEFAULT NULL,
                    `raw_content` text,
                    `display_content` text,
                    `reason` varchar(255) DEFAULT NULL,
                    `reviewed_by` bigint unsigned DEFAULT NULL,
                    `reviewed_at` datetime DEFAULT NULL,
                    `expires_at` datetime NOT NULL,
                    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_moderation_target` (`target_type`,`target_id`),
                    KEY `idx_moderation_queue` (`status`,`risk_level`,`created_at`),
                    KEY `idx_moderation_expire` (`expires_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='社区内容审核任务'
            """,

            # ============================================
            # 44. 用户特征向量（用于AI推荐）
            # ============================================
            'user_feature_vector': """
                CREATE TABLE IF NOT EXISTS `user_feature_vector` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `age` int DEFAULT NULL,
                    `gender` tinyint DEFAULT NULL,
                    `height` int DEFAULT NULL,
                    `income_level` tinyint DEFAULT NULL COMMENT '收入等级 1-5',
                    `education_level` tinyint DEFAULT NULL COMMENT '学历等级 1博士 2硕士 3本科 4大专 5高中',
                    `city_code` varchar(32) DEFAULT NULL COMMENT '城市编码',
                    `hometown_city` varchar(32) DEFAULT NULL COMMENT '家乡城市',
                    `mbti_ei` tinyint DEFAULT NULL COMMENT 'E/I 0-100',
                    `mbti_sn` tinyint DEFAULT NULL COMMENT 'S/N 0-100',
                    `mbti_tf` tinyint DEFAULT NULL COMMENT 'T/F 0-100',
                    `mbti_jp` tinyint DEFAULT NULL COMMENT 'J/P 0-100',
                    `interest_vector` json DEFAULT NULL COMMENT '兴趣标签权重 {"音乐":0.8,"旅行":0.6,"读书":0.9}',
                    `active_score` decimal(5,2) DEFAULT '0.00' COMMENT '活跃度评分',
                    `response_rate` decimal(5,2) DEFAULT '0.00' COMMENT '回复率',
                    `avg_response_time` int DEFAULT NULL COMMENT '平均回复时间（秒）',
                    `version` int DEFAULT '1' COMMENT '版本号（用于增量更新）',
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_id` (`user_id`),
                    KEY `idx_mbti` (`mbti_ei`,`mbti_sn`,`mbti_tf`,`mbti_jp`),
                    KEY `idx_city_education` (`city_code`,`education_level`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户特征向量（AI推荐）'
            """,

            # ============================================
            # 45. 用户行为事件（实时推荐用）
            # ============================================
            'user_behavior_event': """
                CREATE TABLE IF NOT EXISTS `user_behavior_event` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `target_user_id` bigint unsigned DEFAULT NULL COMMENT '关联目标用户',
                    `target_post_id` bigint unsigned DEFAULT NULL COMMENT '关联动态ID',
                    `event_type` varchar(32) NOT NULL COMMENT '事件类型: view_profile / like / pass / chat / reply / post / share / checkin',
                    `event_value` varchar(255) DEFAULT NULL COMMENT '事件附加值（如停留时长、点赞等）',
                    `session_id` varchar(64) DEFAULT NULL COMMENT '会话ID（用于归因）',
                    `ip` varchar(64) DEFAULT NULL,
                    `device` varchar(128) DEFAULT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user_time` (`user_id`,`created_at`),
                    KEY `idx_target_user` (`target_user_id`),
                    KEY `idx_type_time` (`event_type`,`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户行为事件（实时推荐）'
            """,

            # ============================================
            # 46. MBTI测试结果
            # ============================================
            'user_mbti_result': """
                CREATE TABLE IF NOT EXISTS `user_mbti_result` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `mbti_type` varchar(8) NOT NULL COMMENT 'ENFJ/INTJ等',
                    `ei_score` int DEFAULT '0' COMMENT 'E/I 分数 0-100',
                    `sn_score` int DEFAULT '0' COMMENT 'S/N 分数',
                    `tf_score` int DEFAULT '0' COMMENT 'T/F 分数',
                    `jp_score` int DEFAULT '0' COMMENT 'J/P 分数',
                    `dimensions` json DEFAULT NULL COMMENT '各维度详细描述',
                    `description` text COMMENT '性格描述',
                    `test_version` varchar(16) DEFAULT NULL COMMENT '测试版本',
                    `test_date` date NOT NULL COMMENT '测试日期',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_date` (`user_id`,`test_date`),
                    KEY `idx_mbti_type` (`mbti_type`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='MBTI测试结果'
            """,

            # ============================================
            # 47. 恋爱风格测试结果
            # ============================================
            'user_love_style_result': """
                CREATE TABLE IF NOT EXISTS `user_love_style_result` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `attachment_style` varchar(32) DEFAULT NULL COMMENT '依恋类型: 安全型/焦虑型/回避型/恐惧型',
                    `love_language` json DEFAULT NULL COMMENT '爱的语言: ["肯定话语","服务行动","接受礼物","精心时刻","身体接触"]',
                    `relationship_expectation` text COMMENT '关系期望',
                    `test_date` date NOT NULL,
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_user_date` (`user_id`,`test_date`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='恋爱风格测试结果'
            """,

            # ============================================
            # 48. 用户间匹配分历史
            # ============================================
            'user_match_score_history': """
                CREATE TABLE IF NOT EXISTS `user_match_score_history` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL,
                    `target_user_id` bigint unsigned NOT NULL,
                    `match_score` decimal(5,2) NOT NULL COMMENT '匹配分 0-100',
                    `dimension_scores` json DEFAULT NULL COMMENT '各维度分数 {"age_similarity":85,"interest_overlap":72,"mbti_compatibility":90}',
                    `match_reason` varchar(255) DEFAULT NULL COMMENT '匹配理由',
                    `is_clicked` tinyint DEFAULT '0' COMMENT '用户是否点击查看',
                    `is_liked` tinyint DEFAULT '0',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user_target` (`user_id`,`target_user_id`),
                    KEY `idx_score` (`match_score`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户间匹配分历史'
            """,

            # ============================================
            # 49. 每日用户统计
            # ============================================
            'stat_daily_user': """
                CREATE TABLE IF NOT EXISTS `stat_daily_user` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `stat_date` date NOT NULL COMMENT '统计日期',
                    `total_users` int DEFAULT '0' COMMENT '累计注册用户数',
                    `new_users` int DEFAULT '0' COMMENT '新增注册用户',
                    `auth_users` int DEFAULT '0' COMMENT '累计认证用户数',
                    `new_auth_users` int DEFAULT '0' COMMENT '新增认证用户',
                    `total_vip_users` int DEFAULT '0' COMMENT '累计VIP用户数',
                    `active_vip_users` int DEFAULT '0' COMMENT '当前活跃VIP用户数',
                    `new_vip_users` int DEFAULT '0' COMMENT '新增VIP用户',
                    `male_users` int DEFAULT '0',
                    `female_users` int DEFAULT '0',
                    `avg_age` decimal(4,1) DEFAULT '0.0' COMMENT '平均年龄',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_date` (`stat_date`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每日用户统计'
            """,

            # ============================================
            # 50. 每日活跃/互动统计
            # ============================================
            'stat_daily_activity': """
                CREATE TABLE IF NOT EXISTS `stat_daily_activity` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `stat_date` date NOT NULL,
                    `dau` int DEFAULT '0' COMMENT '日活跃用户数',
                    `wau` int DEFAULT '0' COMMENT '周活跃用户数',
                    `mau` int DEFAULT '0' COMMENT '月活跃用户数',
                    `avg_session_duration` int DEFAULT '0' COMMENT '平均会话时长（秒）',
                    `avg_daily_visits` decimal(5,2) DEFAULT '0.00' COMMENT '人均每日访问次数',
                    `total_swipes` int DEFAULT '0' COMMENT '总滑动次数',
                    `total_likes` int DEFAULT '0' COMMENT '总点赞数',
                    `total_matches` int DEFAULT '0' COMMENT '匹配成功数',
                    `total_chat_messages` int DEFAULT '0' COMMENT '总消息数',
                    `total_posts` int DEFAULT '0' COMMENT '总动态发布数',
                    `total_comments` int DEFAULT '0' COMMENT '总评论数',
                    `total_paper_planes` int DEFAULT '0' COMMENT '总纸飞机数',
                    `like_to_match_rate` decimal(5,2) DEFAULT '0.00' COMMENT '点赞转匹配率 %',
                    `match_to_chat_rate` decimal(5,2) DEFAULT '0.00' COMMENT '匹配转聊天率 %',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_date` (`stat_date`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每日活跃/互动统计'
            """,

            # ============================================
            # 51. 每日匹配/牵线统计
            # ============================================
            'stat_daily_match': """
                CREATE TABLE IF NOT EXISTS `stat_daily_match` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `stat_date` date NOT NULL,
                    `total_recommendations` int DEFAULT '0' COMMENT '总推荐次数',
                    `avg_recommend_score` decimal(5,2) DEFAULT '0.00' COMMENT '平均推荐分',
                    `like_count` int DEFAULT '0' COMMENT '点赞数',
                    `pass_count` int DEFAULT '0' COMMENT '无感数',
                    `apply_count` int DEFAULT '0' COMMENT '申请认识数',
                    `match_success_count` int DEFAULT '0' COMMENT '互相匹配成功数',
                    `match_apply_count` int DEFAULT '0' COMMENT '牵线申请数',
                    `match_apply_agree_count` int DEFAULT '0' COMMENT '牵线同意数',
                    `match_apply_reject_count` int DEFAULT '0' COMMENT '牵线拒绝数',
                    `like_match_rate` decimal(5,2) DEFAULT '0.00' COMMENT '点赞→匹配率',
                    `apply_agree_rate` decimal(5,2) DEFAULT '0.00' COMMENT '申请→同意率',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_date` (`stat_date`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每日匹配/牵线统计'
            """,

            # ============================================
            # 52. 每日营收统计
            # ============================================
            'stat_daily_revenue': """
                CREATE TABLE IF NOT EXISTS `stat_daily_revenue` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `stat_date` date NOT NULL,
                    `total_revenue` decimal(12,2) DEFAULT '0.00' COMMENT '总营收',
                    `membership_revenue` decimal(12,2) DEFAULT '0.00' COMMENT '会员营收',
                    `boost_revenue` decimal(12,2) DEFAULT '0.00' COMMENT '爆灯营收',
                    `exposure_revenue` decimal(12,2) DEFAULT '0.00' COMMENT '置顶曝光营收',
                    `activity_revenue` decimal(12,2) DEFAULT '0.00' COMMENT '活动报名营收',
                    `consulting_revenue` decimal(12,2) DEFAULT '0.00' COMMENT '情感咨询营收',
                    `matchmaker_revenue` decimal(12,2) DEFAULT '0.00' COMMENT '红娘服务营收',
                    `other_revenue` decimal(12,2) DEFAULT '0.00' COMMENT '其他营收',
                    `total_orders` int DEFAULT '0' COMMENT '总订单数',
                    `membership_orders` int DEFAULT '0',
                    `boost_orders` int DEFAULT '0',
                    `exposure_orders` int DEFAULT '0',
                    `activity_orders` int DEFAULT '0',
                    `arpu` decimal(10,2) DEFAULT '0.00' COMMENT '每用户平均收入',
                    `ltv` decimal(10,2) DEFAULT '0.00' COMMENT '用户生命周期价值（累计）',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_date` (`stat_date`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每日营收统计'
            """,

            # ============================================
            # 53. 用户行为漏斗（转化分析）
            # ============================================
            'stat_user_behavior_funnel': """
                CREATE TABLE IF NOT EXISTS `stat_user_behavior_funnel` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `stat_date` date NOT NULL,
                    `funnel_name` varchar(64) NOT NULL COMMENT '漏斗名称: 注册-认证-匹配-聊天',
                    `step_1_count` int DEFAULT '0' COMMENT '阶段1: 注册',
                    `step_2_count` int DEFAULT '0' COMMENT '阶段2: 完善资料（80%+）',
                    `step_3_count` int DEFAULT '0' COMMENT '阶段3: 实名认证',
                    `step_4_count` int DEFAULT '0' COMMENT '阶段4: 首次点赞',
                    `step_5_count` int DEFAULT '0' COMMENT '阶段5: 首次匹配成功',
                    `step_6_count` int DEFAULT '0' COMMENT '阶段6: 首次聊天',
                    `step_7_count` int DEFAULT '0' COMMENT '阶段7: 首次线下见面',
                    `step_1_to_2_rate` decimal(5,2) DEFAULT '0.00',
                    `step_2_to_3_rate` decimal(5,2) DEFAULT '0.00',
                    `step_3_to_4_rate` decimal(5,2) DEFAULT '0.00',
                    `step_4_to_5_rate` decimal(5,2) DEFAULT '0.00',
                    `step_5_to_6_rate` decimal(5,2) DEFAULT '0.00',
                    `step_6_to_7_rate` decimal(5,2) DEFAULT '0.00',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_date_name` (`stat_date`,`funnel_name`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户行为漏斗'
            """,

            # ============================================
            # 54. 用户留存统计
            # ============================================
            'stat_retention': """
                CREATE TABLE IF NOT EXISTS `stat_retention` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `cohort_date` date NOT NULL COMMENT '注册批次日期',
                    `day_1` decimal(5,2) DEFAULT '0.00' COMMENT '次日留存率 %',
                    `day_3` decimal(5,2) DEFAULT '0.00' COMMENT '3日留存',
                    `day_7` decimal(5,2) DEFAULT '0.00' COMMENT '7日留存',
                    `day_14` decimal(5,2) DEFAULT '0.00' COMMENT '14日留存',
                    `day_30` decimal(5,2) DEFAULT '0.00' COMMENT '30日留存',
                    `day_60` decimal(5,2) DEFAULT '0.00' COMMENT '60日留存',
                    `day_90` decimal(5,2) DEFAULT '0.00' COMMENT '90日留存',
                    `cohort_size` int DEFAULT '0' COMMENT '该批次总人数',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_cohort_date` (`cohort_date`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户留存统计'
            """,

            # ============================================
            # 55. 用户置顶曝光购买记录
            # ============================================
            'user_exposure': """
                CREATE TABLE IF NOT EXISTS `user_exposure` (
                    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
                    `user_id` bigint unsigned NOT NULL COMMENT '用户ID',
                    `package_id` bigint unsigned NOT NULL COMMENT '关联 config_boost_package.id (type=2置顶)',
                    `order_no` varchar(64) DEFAULT NULL COMMENT '关联支付订单号',
                    `start_at` datetime NOT NULL COMMENT '生效开始时间',
                    `end_at` datetime NOT NULL COMMENT '生效结束时间',
                    `status` tinyint DEFAULT '1' COMMENT '1生效中 2已过期 3已退款',
                    `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
                    `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_user` (`user_id`),
                    KEY `idx_package` (`package_id`),
                    KEY `idx_status_time` (`status`,`start_at`,`end_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户置顶曝光购买记录'
            """,
        }

        # 本次一期商业化领域表与基础用户表保持同一初始化入口。
        tables.update(BUSINESS_TABLES)

        # AI 派生投影（revision/outbox/消费收据）与业务表同一入口。
        from app.db.derivation_schema import (
            DERIVATION_TABLES,
            ensure_derivation_task10_columns,
        )

        tables.update(DERIVATION_TABLES)

        # AI-CORE/M04/M03/M06 表（ai_consent_grant、ai_task、ai_generation_audit
        # 及 13 张画像/搜索/投影/兼容度表）只做幂等建表，不做生产自动迁移。
        from app.db.ai_schema import (
            AI_CONSENT_OPERATION_TABLE,
            AI_TABLES,
            ensure_ai_compatibility_engine_columns,
            ensure_ai_legacy_columns,
            ensure_ai_profile_entry_columns,
            ensure_ai_profile_session_columns,
            ensure_ai_projection_columns,
            ensure_ai_search_snapshot_columns,
            ensure_ai_task_columns,
        )

        tables.update(AI_TABLES)
        tables["ai_consent_operation"] = AI_CONSENT_OPERATION_TABLE

        # 创建所有表
        for table_name, sql in tables.items():
            cursor.execute(sql)
            logger.debug(f"表 `{table_name}` 已创建/确认")

        # Keep one explicit bootstrap super-admin for upgraded installations.
        cursor.execute("""
            INSERT IGNORE INTO matchmaker_admin_permission (account_id, permission)
            SELECT first_account.id, '*'
            FROM matchmaker_admin_account first_account
            WHERE first_account.id = (SELECT MIN(id) FROM matchmaker_admin_account)
        """)

        # Publish the three initial packages and quota products once. Existing
        # rows remain operator-controlled and are never overwritten.
        cursor.execute("""
            INSERT IGNORE INTO config_membership_package
                (name, code, duration_days, price, original_price, daily_price, sort, is_active, badge, rights)
            VALUES
                ('新人专享', 'monthly', 90, 299.00, NULL, 3.32, 1, 1, '含3次申请', '{"apply_bonus": 3, "browse_daily_limit": 20}'),
                ('心动专享', 'quarterly', 150, 520.00, NULL, 3.47, 2, 1, '含5次申请', '{"apply_bonus": 5, "browse_daily_limit": 20}'),
                ('臻爱专享', 'yearly', 365, 999.00, NULL, 2.74, 3, 1, '含20次申请', '{"apply_bonus": 20, "browse_daily_limit": 20}')
        """)
        cursor.execute("""
            INSERT IGNORE INTO config_point_product (code, name, product_type, points_cost, value, sort, is_active)
            VALUES
                ('extra_apply', '额外申请次数', 'right', 20, '1', 1, 1),
                ('browse_unlock', '额外资料查看次数', 'right', 10, '1', 2, 1),
                ('paper_plane_unlock', '纸飞机', 'right', 30, '1', 3, 1),
                ('profile_detail_unlock', '完整资料卡解锁', 'right', 50, '1', 4, 1)
        """)
        cursor.execute("""
            INSERT IGNORE INTO config_reward_rule
                (task_code, task_name, task_type, reward_type, reward_value, daily_limit, is_active, sort)
            VALUES
                ('daily_login', '每日登录', 2, 1, 5, 1, 1, 1),
                ('profile_complete', '完成资料', 1, 1, 50, 1, 1, 2),
                ('realname_verified', '完成实名认证', 1, 1, 100, 1, 1, 3)
        """)

        # 兼容已存在的旧库：CREATE TABLE IF NOT EXISTS 不会补齐新增字段。
        self._ensure_required_columns(cursor)
        self._ensure_admin_home_columns(cursor)

        # 旧库的 ai_feature_projection 不会由 CREATE TABLE IF NOT EXISTS 补齐
        # Task 9 新增列（版本向量/可见性/失效原因等），与上面同模式幂等补列
        # （SHOW COLUMNS→ALTER TABLE ADD COLUMN；表不存在时由 helper 静默跳过）。
        ensure_ai_projection_columns(cursor)
        # WP-S1 / F9：旧库 ai_task 幂等补 progress_percent（展示用阶段进度），
        # 与上面同模式（SHOW COLUMNS→ALTER TABLE ADD COLUMN）。
        ensure_ai_task_columns(cursor)
        # WP-P1 / F4：旧库画像字段表幂等补条目 4 列（field_kind 默认
        # 'structured'，存量行与 structured 链路零影响）。
        ensure_ai_profile_entry_columns(cursor)
        # WP-P4 / F5：旧库画像会话幂等补 session_kind（默认 'build'）。
        ensure_ai_profile_session_columns(cursor)
        # WP-S2 / F10：旧库搜索快照幂等补 partial_visible（默认 'none'）。
        ensure_ai_search_snapshot_columns(cursor)
        # WP-C1 / F11：旧库兼容度快照幂等补 engine/brand_label（engine 默认
        # 'rule-v1'，存量快照语义零变化）。
        ensure_ai_compatibility_engine_columns(cursor)
        # Task 2 additive AI fields are safe to backfill during bootstrap;
        # constraint/index changes remain in the reviewed migration runner.
        ensure_ai_legacy_columns(cursor)
        ensure_derivation_task10_columns(cursor)

        self._backfill_comment_roots(cursor)

        # 历史发帖作者回填到话题参与表（幂等，可重复执行）
        self._backfill_topic_participants(cursor)

        # 添加外键约束
        self._add_all_foreign_keys(cursor)

        logger.info(f"✅ 数据库表结构初始化完成（{len(tables)}张表）")

    def _ensure_admin_home_columns(self, cursor) -> None:
        """Backfill tenant boundaries for databases created before the admin-home module."""
        for table_name in (
            "admin_sms_statistics",
            "admin_academy_category",
            "admin_recharge_item",
            "admin_announcement_version",
            "admin_announcement",
        ):
            cursor.execute(f"SHOW COLUMNS FROM `{table_name}` LIKE 'tenant_id'")
            if not cursor.fetchone():
                cursor.execute(
                    f"ALTER TABLE `{table_name}` ADD COLUMN `tenant_id` bigint unsigned NOT NULL DEFAULT 1 AFTER `id`"
                )

    def _backfill_comment_roots(self, cursor) -> None:
        """Resolve legacy nested replies to their top-level root, one depth at a time."""
        while True:
            cursor.execute(
                """
                UPDATE community_comment AS comment
                INNER JOIN community_comment AS parent ON parent.id = comment.parent_id
                SET comment.root_id = CASE
                    WHEN parent.parent_id IS NULL THEN parent.id
                    ELSE parent.root_id
                END
                WHERE comment.parent_id IS NOT NULL
                  AND comment.root_id IS NULL
                  AND (parent.parent_id IS NULL OR parent.root_id IS NOT NULL)
                """
            )
            if cursor.rowcount is None or cursor.rowcount <= 0:
                break

    def _backfill_topic_participants(self, cursor) -> None:
        """将 status=1 话题帖作者幂等写入 community_topic_participant。"""
        cursor.execute(
            """
            INSERT IGNORE INTO community_topic_participant (topic_id, user_id, created_at)
            SELECT p.topic_id, p.user_id, MIN(p.created_at)
            FROM community_post p
            INNER JOIN community_topic t ON t.id = p.topic_id AND t.is_active = 1
            WHERE p.topic_id IS NOT NULL AND p.status = 1
              AND p.deleted_at IS NULL AND p.moderation_status = 1
            GROUP BY p.topic_id, p.user_id
            """
        )
        affected = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0
        logger.info("话题参与回填完成（新增/忽略行数 rowcount=%s）", affected)

    def create_test_data(self, cursor, conn) -> int:
        """创建测试数据（可选）"""
        # 环境检查：防止在生产环境误执行
        env = os.getenv('ENV', os.getenv('ENVIRONMENT', 'development')).lower()
        if env in ('production', 'prod'):
            logger.warning("⚠️ 当前为生产环境，跳过测试数据创建")
            return 0

        logger.info("创建测试数据...")

        phone = '13800138000'

        # 插入或更新测试用户
        cursor.execute("""
            INSERT INTO `users` (phone, nickname, gender, status)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE nickname = VALUES(nickname)
        """, (phone, '测试用户', 1, 1))

        # 显式查询用户 ID（ON DUPLICATE KEY UPDATE 时 lastrowid 不可靠）
        cursor.execute("SELECT id FROM `users` WHERE phone = %s", (phone,))
        row = cursor.fetchone()
        if not row:
            raise RuntimeError(f"创建或查询测试用户失败，手机号: {phone}")
        user_id = row['id']

        # 创建测试用户资料
        cursor.execute("""
            INSERT INTO `user_profile` (user_id, height, income, hometown, residence, mbti, constellation, tags)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE height = VALUES(height)
        """, (user_id, 175, 15000.00, '杭州', '上海', 'ENFJ', '天秤座', '["颜控", "宠物"]'))

        # 创建隐私设置
        cursor.execute("""
            INSERT INTO `user_privacy` (user_id)
            VALUES (%s)
            ON DUPLICATE KEY UPDATE user_id = user_id
        """, (user_id,))

        conn.commit()
        logger.info(f"✅ 测试数据创建完成 | 用户ID: {user_id}")
        return user_id


# ==================== 对外接口函数 ====================

def create_database():
    """根据 `.env` 中的配置，创建 MySQL 数据库（如果不存在）"""
    import pymysql
    cfg = get_db_config()
    host = cfg['host']
    port = cfg['port']
    user = cfg['user']
    password = cfg['password']
    dbname = _validate_database_name(cfg['database'])

    conn = pymysql.connect(host=host, port=port, user=user, password=password, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            )
    finally:
        conn.close()

    logger.info("✅ 数据库创建完成（若不存在）")


def initialize_database():
    """初始化数据库表结构（如果尚未创建）"""
    logger.info("正在检查数据库表结构...")
    create_database()

    cfg = get_db_config()
    conn = pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cursor:
            db_manager = DatabaseManager()
            db_manager.init_all_tables(cursor)
        conn.commit()
    finally:
        conn.close()

    logger.info("✅ 数据库表结构初始化完成。")


def create_test_data():
    """创建测试数据（可选）"""
    logger.info("正在创建测试数据...")
    cfg = get_db_config()
    conn = pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cursor:
            db_manager = DatabaseManager()
            db_manager.create_test_data(cursor, conn)
    finally:
        conn.close()
    logger.info("✅ 测试数据创建完成。")


def init_db():
    """一键初始化数据库（兼容接口）"""
    initialize_database()
    logger.info("[database_setup_marriage] 初始化完成！")


# ==================== 主入口 ====================
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        initialize_database()
        create_test_data()
    else:
        initialize_database()
