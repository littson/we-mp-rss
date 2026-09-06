# 微信读书公众号数据流程

## 目标与边界

本流程从已登录的微信读书网页会话获取书架中的公众号文章，不进入阅读器页面，不执行阅读器 JavaScript，也不处理其反调试逻辑。

默认通过项目内二维码登录获取 Cookie，并保存到独立的微信读书会话存储。仍可使用本地 `config.yaml` 或环境变量作为兼容方案。Cookie 不写入日志或数据库。

## 用户操作

1. 打开 `https://weread.qq.com/web/shelf`。
2. 未登录时扫描页面二维码。
3. 确认目标公众号已出现在书架中；未出现时先将公众号加入书架。
4. 在项目顶部点击书本图标，或在用户菜单选择“微信读书授权”。
5. 扫码成功后，在“添加订阅”页粘贴公众号文章链接并提交。

外部程序可以调用 `POST /api/v1/wx/mps/by_article/subscribe`，一次完成公众号识别、微信读书书架去重、本地订阅创建和 RSS 地址返回。调用方法见 [外部 API 文档](./外部API文档.md)。

阅读器地址形如：

```text
https://weread.qq.com/web/mp/reader/<infoId>
```

采集不需要访问该地址。书架中的公众号内部 ID 可直接作为接口的 `bookId`，例如 `MP_WXS_3074348403`。

## 已观察的登录请求

网页扫码登录依次使用以下请求：

```text
POST /web/login/getuid
POST /web/login/getinfo
POST /web/login/weblogin
POST /web/login/session/init
```

项目通过独立浏览器上下文打开登录页、截取页面生成的二维码并等待扫码。扫码成功后只保存 `weread.qq.com` 域下的 Cookie，公众号平台的登录状态不受影响。

项目接口：

```http
GET  /api/v1/wx/weread/auth/qr/code
GET  /api/v1/wx/weread/auth/qr/status
POST /api/v1/wx/weread/auth/unbind
```

## 数据接口

### 公众号信息

```http
GET /web/book/info?bookId=<bookId>
```

已观察字段包括：`bookId`、`title`、`author`、`cover`、`intro`、`type`、`format`、`version` 和更新时间。

公众号封面和名称还会通过以下接口交叉校验：

```http
GET /web/mp/cover?bookId=<bookId>
```

### 文章列表

```http
GET /web/mp/articles?bookId=<bookId>&offset=<offset>
```

只采集最新一页，固定使用 `offset=0`。已观察每页返回 20 个文章组。

响应结构：

```json
{
  "reviews": [
    {
      "createTime": 1710000000,
      "subCount": 1,
      "subReviews": [
        {
          "reviewId": "<reviewId>",
          "review": {
            "type": 16,
            "mpInfo": {}
          }
        }
      ]
    }
  ],
  "clearAll": 1,
  "synckey": 1710000000
}
```

### 单篇元数据

列表没有内嵌完整 `review.mpInfo` 时使用：

```http
GET /web/mp/review/single?reviewId=<reviewId>
```

响应的 `review.mpInfo` 已观察到以下字段：

| 字段 | 用途 |
| --- | --- |
| `originalId` | 微信文章原始标识 |
| `doc_url` | 微信公众号原文链接 |
| `pic_url` | 封面图 |
| `title` | 标题 |
| `content` | 摘要 |
| `mp_name` | 公众号名称 |
| `time` | 发布时间，Unix 秒 |
| `readNum` | 阅读数 |
| `likeNum` | 点赞数 |
| `payType` | 付费类型 |

### 单篇正文

```http
GET /web/mp/content?reviewId=<reviewId>
```

响应状态为 200，`Content-Type` 为 `text/plain`，响应体实际是完整 HTML。实现从 `#js_content` 提取正文；只有 `gather.content=true` 时请求该接口。

### 加入书架

```http
POST /web/shelf/add
Content-Type: application/json

{"bookIds":["MP_WXS_3074348403"]}
```

已使用书架中现有公众号进行幂等验证，响应为：

```json
{"succ": 1}
```

项目提供两个封装接口：

```http
POST /api/v1/wx/weread/shelf/add
POST /api/v1/wx/weread/shelf/add-all
POST /api/v1/wx/weread/subscriptions
```

`/subscriptions` 会在同一业务流程中加入微信读书书架、创建项目 Feed，并触发首次抓取。加入书架失败时不会提交 Feed。

`/shelf/add-all` 会读取项目现有公众号和微信读书真实书架，只提交差集；重复点击不会再次添加已经存在的公众号。请求按 50 个一批提交，并排除精选文章和非 `MP_WXS_` 订阅。

## 项目字段映射

| 项目字段 | 微信读书字段 |
| --- | --- |
| `Article.id` | `reviewId`；重复抓取时据此去重 |
| `Article.mp_id` | `bookId` / 现有 `Feed.id` |
| `Article.title` | `review.mpInfo.title` |
| `Article.url` | 规范化后的 `review.mpInfo.doc_url`；删除场景、会话和追踪参数 |
| `Article.pic_url` | `review.mpInfo.pic_url` |
| `Article.description` | `review.mpInfo.content` |
| `Article.publish_time` | 优先 `review.mpInfo.time`（原文发布时间），缺失时回退 review/文章组 `createTime` |
| `Article.content` | `/web/mp/content` 提取结果 |
| `Article.publish_info` | 来源、阅读数、点赞数等 JSON |

## 配置

```yaml
gather:
  model: weread
  content: false

weread:
  cookie: ${WEREAD_COOKIE:-}
  timeout: ${WEREAD_TIMEOUT:-30}
  login_timeout: ${WEREAD_LOGIN_TIMEOUT:-300}
  browser_type: ${WEREAD_BROWSER_TYPE:-firefox}
```

二维码登录不可用时，可以通过环境变量提供兼容 Cookie：

```bash
export WEREAD_COOKIE='<从已登录会话取得的 Cookie 请求头值>'
```

一次性取得 Cookie 的操作：

1. 停留在已登录的微信读书书架页，按 F12。
2. 打开 **Network**，刷新书架页。
3. 选择名称为 `shelf` 的文档请求。
4. 在 **Headers → Request Headers** 中复制 `cookie` 的完整值。
5. 关闭开发者工具。书架页不会加载阅读器反调试脚本。

Cookie 缺失、返回 401/403、或 JSON 接口返回登录 HTML 时，采集器会给出登录失效错误。

## 反调试结论

反调试逻辑位于阅读器相关脚本中。书架页的同源数据接口可直接调用，因此无需修改 Chrome 启动参数、CDP 会话或网页脚本。
