# 省市区查询接口

区域数据来自项目 `data/p-c-a.json`，接口无需登录，供资料填写和筛选器使用。返回结构统一为：

```json
{
  "items": [{"code": "11", "name": "北京市"}],
  "total": 1
}
```

`items` 按数据文件原有顺序返回，`total` 为当前层级数量；本字典接口不分页。

## 查询省份

`GET /api/v1/regions/provinces`

无请求参数。编码为两位数字，例如 `11`。

## 查询城市

`GET /api/v1/regions/cities?province_code=11`

`province_code` 必填，字符串，必须是两位数字。省份编码不存在返回 `404`：`{"detail":"省份编码不存在"}`。

## 查询区县

`GET /api/v1/regions/districts?city_code=1101`

`city_code` 必填，字符串，必须是四位数字。城市编码不存在返回 `404`：`{"detail":"城市编码不存在"}`。

参数格式错误返回 `422`。区域文件读取失败属于服务配置错误，不应在生产环境静默返回空数据。
