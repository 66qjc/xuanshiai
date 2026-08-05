"""Generate concrete frontend-facing examples from the FastAPI OpenAPI contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.main import app


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "api" / "前端接口联调示例.md"
SPEC: dict[str, Any] = app.openapi()
SCHEMAS: dict[str, Any] = SPEC.get("components", {}).get("schemas", {})

FIELD_MEANINGS = {
    "id": "当前记录的唯一 ID",
    "user_id": "用户 ID",
    "target_user_id": "目标用户 ID",
    "matchmaker_id": "红娘用户 ID",
    "organization_id": "组织/门店 ID",
    "store_id": "门店 ID",
    "service_id": "红娘服务申请 ID",
    "order_id": "订单 ID",
    "order_no": "业务订单号，用于后续查询或支付",
    "product_id": "商品 ID",
    "product_code": "商品业务编码",
    "package_code": "套餐业务编码",
    "task_code": "任务业务编码",
    "code": "业务编码或地区编码",
    "name": "展示名称",
    "nickname": "用户昵称",
    "avatar": "头像 URL",
    "phone": "手机号；返回时通常脱敏",
    "email": "电子邮箱",
    "content": "文本内容",
    "description": "详细描述",
    "reason": "操作或审核原因",
    "note": "补充说明",
    "feedback": "处理反馈或结果说明",
    "status": "当前业务状态，具体枚举见本接口约束",
    "active": "是否启用",
    "enabled": "是否开启该功能",
    "is_vip": "当前是否为有效会员",
    "is_online": "是否在线",
    "online": "是否在线",
    "amount": "金额，服务端按货币最小单位/定点值处理",
    "price": "商品价格",
    "points": "本次积分变动值",
    "balance": "操作后的余额",
    "total": "符合条件的记录总数",
    "page": "页码，从 1 开始",
    "page_size": "每页记录数",
    "has_more": "是否还有下一页",
    "created_at": "创建时间，ISO 8601 格式",
    "updated_at": "最后更新时间，ISO 8601 格式",
    "start_at": "开始时间",
    "end_at": "结束时间",
    "expires_at": "到期时间",
    "expire_at": "订单或资源到期时间",
    "latitude": "纬度，范围 -90 到 90",
    "longitude": "经度，范围 -180 到 180",
    "radius_km": "查询半径，单位公里",
    "distance_km": "与目标的距离，单位公里",
    "signature": "第三方回调签名，服务端用于验签",
    "transaction_id": "支付渠道交易号",
    "access_token": "访问受保护接口的短期 Token",
    "refresh_token": "用于刷新访问 Token 的长期 Token",
}


def resolve(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not schema:
        return {}
    if "$ref" in schema:
        return SCHEMAS.get(schema["$ref"].rsplit("/", 1)[-1], {})
    if "anyOf" in schema:
        for item in schema["anyOf"]:
            if item.get("type") != "null":
                return resolve(item)
    if "oneOf" in schema:
        return resolve(schema["oneOf"][0])
    return schema


def sample(schema: dict[str, Any] | None, name: str = "field", depth: int = 0) -> Any:
    if depth > 6:
        return "sample"
    schema = resolve(schema)
    if "example" in schema:
        return schema["example"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {
            key: sample(value, key, depth + 1)
            for key, value in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [sample(schema.get("items", {}), name, depth + 1)]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.2
    if kind == "boolean":
        return True
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "2026-08-05T10:00:00+08:00"
        if fmt == "date":
            return "2026-08-05"
        if "pattern" in schema and "\\d" in schema["pattern"]:
            return "110101199001011234" if "id_card" in name else ("1101" if schema.get("minLength", 0) == 4 else "11")
        if "phone" in name:
            value = "13800138000"
        elif "email" in name:
            value = "frontend@example.com"
        elif "password" in name:
            value = "Password123"
        elif "token" in name:
            value = "<access-token>"
        elif "url" in name or "avatar" in name or "image" in name:
            value = "/storage/uploads/example.jpg"
        elif "amount" in name or "price" in name:
            value = "99.00"
        elif schema.get("pattern", "").startswith("^[a-z]"):
            value = "example_code"
        else:
            value = "example"
        minimum_length = schema.get("minLength", 0)
        if minimum_length and len(value) < minimum_length and "pattern" not in schema:
            value += "x" * (minimum_length - len(value))
        return value
    return "example"


def type_name(schema: dict[str, Any] | None) -> str:
    schema = resolve(schema)
    if "enum" in schema:
        return "/".join(map(str, schema["enum"]))
    if schema.get("type") == "array":
        return f"array<{type_name(schema.get('items', {}))}>"
    if schema.get("type") == "object" or "properties" in schema:
        return "object"
    return schema.get("type", "object")


def table_rows(schema: dict[str, Any], prefix: str = "", depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    schema = resolve(schema)
    rows: list[str] = []
    for name, child in schema.get("properties", {}).items():
        full_name = f"{prefix}.{name}" if prefix else name
        required = name in schema.get("required", [])
        child_resolved = resolve(child)
        constraints = []
        for key in ("minLength", "maxLength", "minimum", "maximum", "pattern"):
            if key in child_resolved:
                constraints.append(f"{key}={child_resolved[key]}")
        if "enum" in child_resolved:
            constraints.append("枚举=" + ", ".join(map(str, child_resolved["enum"])))
        rule = "; ".join(constraints) or "按 Schema 类型校验"
        meaning = child_resolved.get("description") or FIELD_MEANINGS.get(name) or f"接口中的 `{full_name}` 字段"
        rows.append(
            f"| `{full_name}` | {type_name(child)} | {'是' if required else '否'} | "
            f"{rule} | {meaning} | `{json.dumps(sample(child, name), ensure_ascii=False)}` |"
        )
        if child_resolved.get("type") == "object" or "properties" in child_resolved:
            rows.extend(table_rows(child_resolved, full_name, depth + 1))
        elif child_resolved.get("type") == "array":
            item = resolve(child_resolved.get("items", {}))
            if item.get("type") == "object" or "properties" in item:
                rows.extend(table_rows(item, f"{full_name}[]", depth + 1))
    return rows


def request_schema(operation: dict[str, Any]) -> tuple[dict[str, Any], str]:
    body = operation.get("requestBody", {}).get("content", {})
    if not body:
        return {}, ""
    media_type = next(iter(body))
    return resolve(body[media_type].get("schema")), media_type


def response_schema(operation: dict[str, Any]) -> tuple[dict[str, Any], str]:
    responses = operation.get("responses", {})
    success = next((code for code in responses if str(code).startswith("2")), "200")
    content = responses.get(success, {}).get("content", {})
    if not content:
        return {}, str(success)
    media_type = next(iter(content))
    return resolve(content[media_type].get("schema")), str(success)


def make_document() -> str:
    lines = [
        "# 前端接口联调示例",
        "",
        "> 本文档由 `scripts/generate_frontend_api_examples.py` 根据当前 FastAPI OpenAPI 定义生成。每个 HTTP 操作独立成节，示例值用于开发联调，不是生产密钥。字段含义优先参考对应模块中文文档；本文件重点保证请求和响应 JSON 结构完整。",
        "",
        "## 通用调用约定",
        "",
        "- 服务地址：`http://127.0.0.1:8000`；所有 `/api/v1` 路径均以此为基础拼接。",
        "- 登录接口之后，将返回的 `access_token` 放入 `Authorization: Bearer <access-token>`。",
        "- JSON 请求使用 `Content-Type: application/json`；上传接口按文档标注的 `multipart/form-data` 提交。",
        "- `204 No Content` 响应没有 JSON，前端不要调用 `response.json()`。",
        "- 示例中的 `<access-token>`、`<signed-payload>` 和文件路径必须替换为实际值。",
        "",
    ]
    operation_no = 0
    for path, path_item in SPEC.get("paths", {}).items():
        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if not operation:
                continue
            operation_no += 1
            title = operation.get("summary") or operation.get("operationId") or "未命名接口"
            lines += [f"## {operation_no}. `{method.upper()} {path}` {title}", ""]
            security = "需要登录" if operation.get("security") else "无需登录"
            lines += [f"**基本信息**：{security}；成功状态码以 OpenAPI 为准。", ""]

            parameters = operation.get("parameters", [])
            body_schema, media_type = request_schema(operation)
            request_content_type = media_type or "application/json"
            lines += ["### 请求地址和 Headers", "", f"请求地址：`http://127.0.0.1:8000{path}`", "", "```json"]
            headers = {"Content-Type": request_content_type}
            if operation.get("security"):
                headers["Authorization"] = "Bearer <access-token>"
            for parameter in parameters:
                if parameter.get("in") == "header":
                    headers[parameter["name"]] = str(sample(parameter.get("schema", {}), parameter["name"]))
            lines += [json.dumps(headers, ensure_ascii=False, indent=2), "```", ""]

            if parameters:
                lines += ["### 请求参数", "", "| 参数 | 位置 | 类型 | 必填 | 约束 | 含义 | 示例 |", "| --- | --- | --- | --- | --- | --- | --- |"]
                for parameter in parameters:
                    schema = parameter.get("schema", {})
                    constraints = []
                    for key in ("minLength", "maxLength", "minimum", "maximum", "pattern"):
                        if key in schema:
                            constraints.append(f"{key}={schema[key]}")
                    if "enum" in schema:
                        constraints.append("枚举=" + ", ".join(map(str, schema["enum"])))
                    rows = [
                        f"| `{parameter['name']}` | {parameter['in']} | {type_name(schema)} | "
                        f"{'是' if parameter.get('required') else '否'} | "
                        f"{'；'.join(constraints) or '无额外约束'} | {parameter.get('description') or '接口参数'} | "
                        f"`{json.dumps(sample(schema, parameter['name'], 0), ensure_ascii=False)}` |"
                    ]
                    lines.extend(rows)
                lines.append("")
            else:
                lines += ["### 请求参数", "", "无 Path、Query、Header 参数。", ""]

            if parameters:
                parameter_example = {
                    parameter["name"]: sample(parameter.get("schema", {}), parameter["name"])
                    for parameter in parameters
                }
                lines += ["### Path/Query/Header 参数 JSON 示例", "", "```json", json.dumps(parameter_example, ensure_ascii=False, indent=2), "```", ""]

            if body_schema:
                lines += ["### 请求体 JSON 示例" if media_type == "application/json" else f"### 请求体示例（{media_type}）", ""]
                if media_type == "application/json":
                    lines += ["```json", json.dumps(sample(body_schema, "body"), ensure_ascii=False, indent=2), "```", ""]
                else:
                    lines += ["字段 `file` 使用真实文件上传，其余字段按表单字段提交。", ""]
                lines += ["### 请求体字段", "", "| 字段 | 类型 | 必填 | 约束 | 含义 | 示例 |", "| --- | --- | --- | --- | --- | --- |"]
                lines.extend(table_rows(body_schema))
                lines.append("")
            else:
                lines += ["### 请求体", "", "无请求体。", ""]

            response, status = response_schema(operation)
            lines += ["### 成功返回示例", "", f"成功状态码：`{status}`。", ""]
            if response:
                lines += ["```json", json.dumps(sample(response, "response"), ensure_ascii=False, indent=2), "```", "", "### 返回字段", "", "| 字段 | 类型 | 必返 | 约束 | 含义 | 示例 |", "| --- | --- | --- | --- | --- | --- |"]
                for row in table_rows(response):
                    lines.append(row.replace(" | 按 Schema 类型校验 | ", " | ").replace(" | 是 | ", " | 是 | "))
                lines.append("")
                if response.get("type") == "array":
                    lines += ["无数据时返回：`[]`。", ""]
            else:
                lines += ["无返回体。", ""]
            lines += ["### 错误返回示例", "", "```json", '{"detail":"参数校验失败"}', "```", "", "---", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    OUTPUT.write_text(make_document(), encoding="utf-8")
    print(f"generated {OUTPUT}")
