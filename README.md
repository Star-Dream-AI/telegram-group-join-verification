# telegram-group-join-verification
a telegram bot to help you verify group member based of Cloudflare Turnstile

这是一个完整的 Telegram Bot + Telegram Mini App 自动化验证系统。当新成员加入 Telegram 群组时，系统会自动对其进行发言限制（禁言），并向其发送私聊消息提示点击打开 Mini App 页面完成 **Cloudflare Turnstile 旋转门人机验证**。验证成功后，系统将自动解除群组发言限制。 <br> 如果你不想自己部署的话，欢迎使用我的bot：<a href="https://t.me/stardreamverify_bot">@stardreamverify_bot</a>

<a href="https://tgbotmau.quoi.dev/?bot=stardreamverify_bot" target="_blank">
		<img alt="@stardreamverify_bot" title="@stardreamverify_bot MAU" src="https://tgbotmau.quoi.dev/api/bot/stardreamverify_bot/mau/badge?style=flat">
	</a>

<b>中文详细教程参见：</b> <a href="https://www.xmlans.com/web/telegram-group-join-verification-bot.html" target="_blank">
		星梦博客 入群验证Bot
	</a> <br>

<b>English install guide：</b> <a href="https://en.xmlans.com/web/telegram-group-join-verification-turnstile-bot/" target="_blank">
		Star Dream Blog
	</a>

---

## 功能特性

- **自动禁言新成员**：监听群组 `ChatMemberUpdated` 加入事件，新成员入群即刻禁止发言。
- **Telegram Mini App 无缝集成**：使用 Telegram 内置 Webview 打开验证页面，提供极佳的极简交互体验。
- **Cloudflare Turnstile 安全校验**：防范自动化机器人与垃圾信息。
- **安全验证**：
  HMAC-SHA256 校验 Telegram `initData` 签名，防止篡改与伪造请求。

---

## 目录结构

```
verify/
├── config.py           # 环境变量与配置项读取
├── main.py             # FastAPI Web 服务 + aiogram 3 Telegram Bot 核心后端
├── static/
│   └── index.html      # Telegram Mini App 前端 UI (Turnstile SDK + Telegram SDK)
├── .env.example        # 环境变量配置模板
├── data/pending.json   # 运行时生成：待验证记录（已加入 .gitignore）
├── requirements.txt    # Python 依赖包
└── README.md           # 项目使用与部署说明文档
```

---

## 快速开始与配置说明

### 1. 准备工作

#### (1) 创建 Telegram Bot
1. 在 Telegram 中联系 [@BotFather](https://t.me/BotFather)。
2. 发送 `/newbot`，按照提示创建机器人的名称与 Username，获取 **Bot Token**。
3. 将机器人拉入目标 Telegram 群组，并**赋予管理员 (Administrator) 权限**，确保勾选了 **"Restrict Users" (封禁/限制用户)** 权限。

#### (2) 获取 Cloudflare Turnstile 密钥
1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)。
2. 进入 **Turnstile** 菜单 -> **Add Site** (添加站点)。
3. 输入站点名称，Domain 可填写您的域名（本地测试可添加 `localhost`）。
4. 获得 **Site Key**（前端用）和 **Secret Key**（后端验证用）。

---

### 2. 安装与配置

#### (1) 安装依赖
在目录下运行：
```bash
pip install -r requirements.txt
```

#### (2) 配置环境变量
拷贝 `.env.example` 为 `.env`：
```bash
cp .env.example .env
```
编辑 `.env` 文件，填入实际参数：
```ini
BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
CF_SITE_KEY=0x4AAAAAAABxxxxxxxxxxxx
CF_SECRET_KEY=0x4AAAAAAABxxxxxxxxxxxxxxxxxxxx
WEBAPP_URL=https://your-tunnel-domain.com
HOST=0.0.0.0
PORT=4567
# 可选：待验证记录的持久化文件（相对路径以项目目录为基准），重启后不会丢失
DATA_FILE=data/pending.json
# 可选：Telegram initData 最长有效期（秒），防止截获的 initData 被长期重放
INIT_DATA_MAX_AGE=86400
```

> ⚠️ **注意**：Telegram Mini App **必须** 使用 **HTTPS** 协议。
> 本地开发测试时，您可以使用 [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) 或 `ngrok` 进行端口穿透：
> ```bash
> # 使用 Cloudflare Tunnel 快速穿透本地 4567 端口
> cloudflared tunnel --url http://localhost:4567
> ```
> 将穿透得到的 `https://xxxx.trycloudflare.com` 填入 `.env` 中的 `WEBAPP_URL`。

---

### 3. 运行服务

启动应用（同时包含 Web API 服务与 Telegram Bot 监听）：
```bash
python main.py
```

或者使用 uvicorn 启动：
```bash
uvicorn main:app --host 0.0.0.0 --port 4567
```

> ⚠️ 只能运行 **单个进程**：不要加 `--workers N`，否则多个进程会同时轮询 Telegram（`Conflict` 报错）且各自维护待验证记录。

---

## 从旧版升级

旧版把待验证记录存在内存里，新版改为持久化到 `DATA_FILE`。升级后请管理员在群组的「受限用户」列表中，**手动解封被旧版 Bot 禁言、尚未完成验证的用户**。否则这些用户退群再进时，会被当作"管理员已设置的限制"处理，验证通过后仍保持禁言。

---

## 验证测试流程

1. 使用测试账号加入已拉入 Bot 的 Telegram 群组。
2. 观察 Bot 是否自动对测试账号应用了限制发言（禁言）。
3. 查看 Bot 发送给用户的私聊消息（或群组提示），点击 **"🔐 点击完成 Cloudflare 旋转门验证"** 按钮。
4. Telegram 内嵌 Webview 将打开 Mini App 页面。
5. 勾选 Cloudflare 旋转门人机验证复选框。
6. 验证通过后，页面显示成功动画，Bot 自动解除该用户在 Telegram 群组内的发言限制。

---

## 安全性说明

后端 `/api/verify` 校验流程：
1. **initData 签名**：通过 HMAC-SHA256(`WebAppData`, bot_token) 算法验证前端传来的 `initData`，确保 `user_id` 来自真实 Telegram 用户且未被篡改；同时校验 `auth_date`，超过 `INIT_DATA_MAX_AGE` 的 initData 一律拒绝。
2. **Turnstile 二次校验**：将 Turnstile Token 发送至 `https://challenges.cloudflare.com/turnstile/v0/siteverify` 进行二次校验，防范伪造验证请求。
3. **不绕过管理员禁言**：用户入群前若已被管理员限制（退群再进时 Telegram 会保留限制），验证通过后恢复的是原有限制，而不是完全解封。

<img src="https://files.astrbot.app/watashiwa-koseino-desukara.gif" width="100"/>
