# astrbot_plugin_douyin_live

抖音直播开播检测插件（AstrBot v4）

- 订阅主播直播间，每 5 分钟轮询一次开播状态
- 支持直接粘贴手机 App 分享出来的整段文本（自动解析 v.douyin.com 短链）
- 开播/下播自动推送「封面图 + 直播间链接 + 配文」到指定群
- 群聊里说触发词（如「看看腿」）即时检查并回复正在直播的主播

## 检测原理

用 Playwright 无头浏览器渲染 `live.douyin.com/{room_id}/` 页面，通过页面文案判定：

- 页面含「在线观众 / 本场点赞」或标题含「xxx的抖音直播间」→ 开播
- 页面含「未开播 / 暂未开播 / 直播回放」→ 未开播

抖音 web 接口（webcast enter）带签名校验，直接 HTTP 调用会被拒，因此走页面渲染最稳。
默认使用系统 Edge（`msedge`），无需额外下载浏览器；也可留空用 Playwright Chromium
（需先 `playwright install chromium`）。

## 依赖

- Python 依赖见 `requirements.txt`（playwright、aiohttp）
- 系统浏览器：Edge（默认）或 Playwright Chromium

## 用法

| 命令 | 说明 |
| --- | --- |
| `订阅 <直播间号或链接>` | 订阅，例：`订阅 82685125012` 或 `订阅 https://live.douyin.com/82685125012/` |
| `取消订阅 <直播间号>` | 取消订阅 |
| `订阅列表` | 查看全部订阅及当前开播状态 |
| `直播转发 <群号>` | 开播推送转发到指定群 |
| `取消转发 <群号>` | 移除转发群 |
| `转发列表` | 查看转发群 |
| 触发词（默认：看看腿 等） | 即时检查，有在播的发封面+链接+配文，全没播回「都还没开播呢」 |

> 注意：抖音 web 接口需要登录态签名，暂不支持用「抖音号」自动解析成直播间号，
> 订阅请直接给直播间号（纯数字）或直播间链接。

## 配置项

- `enabled`：启用开关
- `trigger_words`：触发词，逗号分隔
- `reply_text`：开播配文（默认「看吧色鬼！」）
- `not_live_text`：全部未开播时的回复，留空不回复
- `poll_interval`：轮询间隔秒数（默认 300）
- `fetch_cover`：是否发送封面图
- `ttwid`：可选，浏览器登录抖音后从 Cookie 复制的 ttwid，填了更不容易被风控
- `browser_channel`：`msedge`（默认）或留空用 Playwright Chromium
