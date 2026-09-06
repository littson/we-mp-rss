# WeRSS 外部订阅 API

本文档面向需要通过程序把微信公众号加入 WeRSS，并获取对应 RSS 地址的调用方。

## 前置条件

- WeRSS 服务已经完成初始化。
- 管理员已经在 WeRSS 中完成微信读书扫码授权。
- 调用方持有 WeRSS 的 Access Key 和 Secret Key。
- 服务端可以访问微信公众号文章页和微信读书网页接口。

API 基础地址示例：

```text
https://rss.example.com/api/v1/wx
```

## 认证

接口支持 Access Key/Secret Key 或后台登录取得的 Bearer Token。外部程序建议使用 Access Key/Secret Key：

```http
Authorization: AK-SK <AccessKey>:<SecretKey>
```

请勿把 Access Key、Secret Key、Bearer Token 或微信读书 Cookie 写入公开日志和代码仓库。

## 根据公众号文章链接新增订阅

使用公众号任意一篇可正常访问的文章链接识别公众号、加入微信读书书架、创建本地订阅，并返回 RSS 地址。

### 请求

```http
POST /api/v1/wx/mps/by_article/subscribe
Authorization: AK-SK <AccessKey>:<SecretKey>
Content-Type: application/json

{
  "url": "https://mp.weixin.qq.com/s/xxxxxxxxxxxxxxxxxxxxxx"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `url` | string | 是 | `mp.weixin.qq.com/s/` 开头的公众号文章链接，最长 2000 字符 |

### 首次新增成功

HTTP 状态码：`200`

```json
{
  "code": 0,
  "message": "订阅添加成功",
  "data": {
    "id": "MP_WXS_3581557283",
    "mp_name": "示例公众号",
    "mp_cover": "/files/avatar/example.jpg",
    "mp_intro": "公众号简介",
    "status": 1,
    "frequency": "daily",
    "recent_update_time": 0,
    "recent_update_time_text": null,
    "rss_url": "https://rss.example.com/rss/MP_WXS_3581557283",
    "created_at": "2026-08-03T12:00:00",
    "updated_at": "2026-08-03T12:00:00",
    "created": true,
    "shelf_added": true,
    "initial_fetch_queued": true,
    "message_task_updated": 0
  }
}
```

### 订阅已存在

重复提交同一篇文章或同一公众号的其他文章不会重复创建订阅，也不会重复加入微信读书书架。

HTTP 状态码：`200`

```json
{
  "code": 0,
  "message": "订阅已存在",
  "data": {
    "id": "MP_WXS_3581557283",
    "mp_name": "示例公众号",
    "rss_url": "https://rss.example.com/rss/MP_WXS_3581557283",
    "created": false,
    "shelf_added": false,
    "initial_fetch_queued": false
  }
}
```

实际响应仍会包含公众号状态、封面、简介、更新时间等字段；上例省略了未变化字段。

### 字段语义

| 字段 | 说明 |
| --- | --- |
| `id` | WeRSS 公众号 ID，也是 RSS 地址中的订阅 ID |
| `rss_url` | 可直接添加到 RSS 阅读器的绝对地址 |
| `created` | 本次调用是否新建了本地订阅 |
| `shelf_added` | 本次调用是否把公众号加入微信读书书架 |
| `initial_fetch_queued` | 是否已把首次文章采集加入后台队列 |

接口返回时首次文章采集可能仍在进行。此时访问 `rss_url` 可能暂时没有文章，稍后再次读取即可。

### 错误响应

参数或外部数据错误返回 HTTP `400`：

```json
{
  "detail": {
    "code": 40001,
    "message": "请输入有效的微信公众号文章链接",
    "data": null
  }
}
```

常见情况：

- 链接不是 `mp.weixin.qq.com/s/` 文章链接。
- 文章已删除、不可访问或触发了微信环境验证。
- 无法从文章页面识别发布公众号。
- 该公众号无法通过微信读书校验或加入书架。
- 微信读书授权已经失效，需要管理员重新扫码。

认证失败返回 HTTP `401`。

请求体缺少 `url` 或字段类型不正确时，FastAPI 参数校验返回 HTTP `422`。

## 获取 RSS

新增接口返回的 `rss_url` 可以直接使用：

```http
GET /rss/MP_WXS_3581557283
```

RSS 地址本身当前不要求 API 认证。返回内容类型为 `application/xml`。

## 调用示例

### curl

```bash
curl --request POST \
  'https://rss.example.com/api/v1/wx/mps/by_article/subscribe' \
  --header 'Authorization: AK-SK WKxxxxxxxx:SKxxxxxxxx' \
  --header 'Content-Type: application/json' \
  --data '{"url":"https://mp.weixin.qq.com/s/xxxxxxxxxxxxxxxxxxxxxx"}'
```

### Python

```python
import requests

response = requests.post(
    "https://rss.example.com/api/v1/wx/mps/by_article/subscribe",
    headers={
        "Authorization": "AK-SK WKxxxxxxxx:SKxxxxxxxx",
        "Content-Type": "application/json",
    },
    json={
        "url": "https://mp.weixin.qq.com/s/xxxxxxxxxxxxxxxxxxxxxx",
    },
    timeout=60,
)
response.raise_for_status()
rss_url = response.json()["data"]["rss_url"]
print(rss_url)
```

## 兼容接口

以下原有接口继续保留：

| 方法和路径 | 用途 |
| --- | --- |
| `GET /api/v1/wx/mps?kw=<关键词>` | 查询项目中已有的公众号订阅 |
| `GET /api/v1/wx/mps/search/<关键词>` | 旧客户端兼容搜索，范围同样仅限项目内订阅 |
| `POST /api/v1/wx/mps/by_article?url=<文章链接>` | 解析文章和公众号资料，不创建订阅 |
| `POST /api/v1/wx/mps` | 使用已有公众号资料创建订阅 |
| `POST /api/v1/wx/mps/confirm` | 旧版确认订阅接口 |

新接入方应优先使用 `POST /api/v1/wx/mps/by_article/subscribe`，不需要先调用搜索或解析接口。
