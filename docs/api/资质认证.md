# 资质认证接口

本期支持学历、房产、婚姻三项认证材料提交。接口只记录前端填写的材料并进入审核中，不代表真实性校验通过。

## 查询认证状态

`GET /api/v1/users/me/certifications`，需要登录。返回 `education`、`house`、`marriage` 三个对象，每个对象包含 `kind`、`status`（0未提交、1审核中、2通过、3失败）、`material_submitted`、`submitted_at`、`reviewed_at`、`fail_reason`、`next_action`。不返回身份证原件等敏感信息。

## 提交认证材料

- `PUT /api/v1/users/me/certifications/education`
- `PUT /api/v1/users/me/certifications/house`
- `PUT /api/v1/users/me/certifications/marriage`

三者均需登录，且只修改当前账号。学历请求体只有 `{"education":"本科"}`，只允许小学、初中、高中、中专、大专、本科、硕士、博士。房产使用 `multipart/form-data` 上传一个 `file` 图片，服务端复用统一图片大小和像素校验。婚姻请求体为 `{"is_unmarried":true}`，表示用户确认自己未婚；该确认不是第三方认证结论。接口会将提交状态设为 1。原状态为 2 时本期仍允许重新提交，但会重新进入审核中，后续应由后台审核接口控制是否允许覆盖。

错误：`401` 未登录，`422` 参数格式错误，`503` 数据库不可用。
