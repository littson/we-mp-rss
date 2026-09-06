# 上游基准迁移结果

## 基准

- 上游仓库：`rachelos/we-mp-rss`
- 上游分支：`main`
- 上游提交：`cadb903555b05b5cc3fe855aa45815f9efd8f28c`
- 上游版本：`1.5.2`
- 本地旧提交：`060c4591c5efa9eed8b7ce2a71011ee050ccb43e`
- 迁移方式：在干净上游工作树逐项重写补丁，没有执行无共同历史的直接合并。

## 已移植

### API

- `GET /api/v1/wx/mps`
  - 默认上限恢复为 1000，最大 10000。
  - 保留 `kw` 模糊检索。
  - 输出 `frequency`、最近更新时间、RSS 地址和更新时间。
- `GET /api/v1/wx/mps/search/{kw}`
  - 每项输出可直接确认订阅的 `subscribe_payload`。
- `POST /api/v1/wx/mps/confirm`
  - Base64 fakeid 解码。
  - 按 faker_id 和内部 ID 幂等创建或更新。
  - 新订阅进入首次采集队列。
  - 同步消息任务订阅列表并热重载调度。
- `POST /api/v1/wx/mps`
  - 复用确认订阅逻辑，保持旧调用方兼容。
- `DELETE /api/v1/wx/mps/{mp_id}`
  - 删除订阅及关联文章。
  - 同步消息任务、重载调度并清理 RSS 缓存。
- `GET /api/v1/wx/auth/qr/last-login`
  - 返回最近成功登录的时间戳和 ISO 时间。

### 内部逻辑

- Feed 增加 `frequency`、`consecutive_failures`、
  `last_success_sync_time`，启动时幂等补列。
- 旧 `high`、`low`、空频率统一迁移为 `daily`。
- 自适应频率仍采用 28 天窗口、14 天最小观察期、3 篇最小样本；
  分为 `twice_daily`、`daily`、`three_day`。
- 每日 06:00/23:00、每日 23:00、每 3 天 23:00 执行对应采集，
  每 14 天从 03:00 起重新统计。
- 自适应采集不依赖消息任务，不发送消息任务 webhook。
- 消息任务新增、修改、删除后立即重载调度和级联任务。
- RSS 全文使用标准 content namespace，避免生成无效的
  `content:encoded` XML。
- 静态正文解析支持 `window.picture_page_info_list` 图片型文章，
  排除水印和分享封面，只保留正文图片。
- 浏览器正文抓取在没有 `#js_content`/`#js_article` 时回退到静态图片型解析。
- 登录令牌增加 `last_login_time`。

### 配置

- 新增 `tools/migrate_legacy_config.py`：
  - 保留数据库、通知、secret、端口等已有本地值。
  - `gather_content` / `model` 映射到新的 `gather` 层级。
  - 强制启用 `gather.content=true`。
  - 强制启用 `rss.full_context=true`。
  - 有旧 token 时迁移到 `data/wx.lic`，已有新令牌时不覆盖。
  - 旧配置未使用 Redis 时保持文件回退，不自动连接或启动本地 Redis。
- 没有回补历史文章；只影响迁移后新增或重新抓取的文章。

## 采用上游实现，不重复移植

- 多账号登录和切换。
- RSS、Atom、JSON Feed 及全文补抓队列。
- 请求超时、失败重试、陈旧锁恢复。
- 队列空闲阻塞等待。
- Redis/file cache、级联节点、AK/SK 认证。
- 上游 notice 告警；旧硬编码告警地址和签名没有带入。

## 上游基线附带修复

- 移除 `web_ui/src/main.ts` 重复的 `createApp` 导入。
- 将 `@vitejs/plugin-vue` 升级到兼容 Vite 8 的版本，并补充 Vite 8
  可选构建依赖 `esbuild`。
- 新增 `pytest.ini`，只收集维护中的 `tests/`，避免示例脚本改变
  `sys.path` 后让 `core/queue` 遮蔽 Python 标准库 `queue`。

## 验证

- Python 3.12 应用源码编译通过。
- 后端完整维护测试：25 passed。
- FastAPI 应用导入成功，142 个唯一路由；
  `/api/v1/wx/mps/confirm` 与
  `/api/v1/wx/auth/qr/last-login` 存在。
- `web_ui`: `npm ci` 通过。
- `web_ui`: `npm run build` 通过。
- 配置迁移 dry-run：数据库配置保留、全文采集与全文 RSS 均为 true。

## 已知上游问题

- 上游 `tools/fix_db.py` 第 23 行原本存在未闭合字符串，执行全仓库
  `compileall` 会失败。本次未修改该独立工具；应用目录和本次新增迁移工具
  的语法检查均通过。
- 前端构建仍提示大 chunk、直接 `eval` 和无效动态拆包警告，不影响构建产物。

## Git 元数据收尾

工作区源码已经以上游提交为基准，但执行环境对 `.git` 只有读权限，因此当前
`dev` 引用仍指向旧提交 `060c459`。在仓库根目录执行以下命令后，Git 状态会从
466 项跨基线差异收敛为本文列出的 22 个本地补丁文件：

```bash
git branch backup/pre-upstream-sync-20260730 060c4591c5efa9eed8b7ce2a71011ee050ccb43e
git remote add upstream https://github.com/rachelos/we-mp-rss.git
git fetch upstream main
git reset --mixed upstream/main
```

如果已经存在名为 `upstream` 的远程，跳过 `remote add`；如果该远程地址不同，
先核对后再执行。`reset --mixed` 会移动 `dev` 基准和索引，不会覆盖当前工作区文件。
