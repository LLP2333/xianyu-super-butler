# 单账号原生浏览器登录

账号管理先使用一个账号、一份浏览器资料和固定 HTTPS 端口 `3001`。桌面由官方 `lscr.io/linuxserver/chrome:latest` 直接提供，管家不转发画面或鼠标输入，不主动请求闲鱼消息 Token。

## 目录与职责

```text
app/
├─ api_account_browser.py              # 管理员入口、一次性桌面链接、访问会话认证
├─ services/account_browser.py         # 首次添加、暂停/恢复、账号归属、明确清空操作
└─ db_manager.py                       # 原子创建单个账号，禁止覆盖已有账号
utils/account_browser/
├─ containers.py                       # 官方容器、固定端口、专属资料卷、限定范围清空
├─ manager.py                          # 浏览器生命周期、内网 CDP 连接
├─ listener.py                         # 被动读取网页 Token 响应并核对 Cookie 身份
└─ tokens.py                           # 线程安全的内存消息 Token 与授权状态
frontend/components/
├─ AccountList.tsx                     # 单账号入口和账号运行状态
└─ AccountBrowser.tsx                  # 打开桌面、登录进度、明确确认清空
XianyuAutoAsync.py                     # 使用网页 Token 注册消息服务，成功响应后才标记连接
deploy/account-browser/
├─ cdp.conf                           # 只读挂载的内网调试接口，保留独立认证
└─ desktop-auth.py                    # 官方启动钩子，只替换桌面认证，保留原生所有位置和输入路径
```

原扫码添加弹窗、扫码添加接口、手工 POST Cookie 添加入口已删除。已有账号可继续编辑设置；多账号业务模块没有扩展，此入口一次只允许一个账号。

## 首次添加：同一个浏览器生命周期

```text
点击“浏览器添加账号”
  → 启动 primary 原生浏览器（此时数据库中还没有闲鱼账号）
  → 建立只读监听 → 一次性链接打开 https://localhost:3001
  → 用户在 Chrome 中登录、完成网站验证、打开 www.goofish.com/im
  → 网页自行获取消息 Token
  → 核对请求 Cookie 的 unb 与当前 Cookie 的 unb 一致
  → 原子创建账号记录，保存 Cookie，授权该网页消息 Token
  → 同一浏览器、同一 profile、同一监听继续运行，无重启或资料迁移
  → 账号任务使用网页 Token 与对应 deviceId/appKey 连接消息服务
  → 收到 /reg 成功响应后显示“监听中”
```

已有账号通过“打开浏览器”进入时，先暂停账号任务；完成验证并捕获新 Token 后，点击“采用网页 Token 并恢复”。账号身份必须与已有记录一致。登录失败、未产生 Token 或重复账号均不会被当作添加成功。

关闭管理弹窗不会停止浏览器或监听。首次添加可在后台完成；用户可重新打开入口查看进度。浏览器独立保留 profile；管家正常停止时关闭其监听和浏览器，让 Chrome 写入数据。

## 三种凭证

| 凭证 | 用途 | 存储/传递 |
| --- | --- | --- |
| 桌面一次性票据 | 从管家打开原生桌面，免输用户名/密码 | 随机票据，60 秒内一次有效；兑换后跳转根路径，不继续留在地址栏 |
| 闲鱼 Cookie | 账号登录状态以及其他账号业务请求 | 捕获并核对后保存在账号数据库 |
| 闲鱼消息 Token | 管家的聊天连接注册 | 仅内存保存，不下发前端、不写日志；到期或被拒绝后等待网页新 Token |

桌面票据不包含闲鱼 Cookie、消息 Token、管家登录 Token或内部调试密码。兑换后使用 Secure、HttpOnly、SameSite=Lax 的桌面会话 Cookie，访问期限一小时；管理员退出登录或容器重建后，新请求不能继续访问。已经建立的原生 WebSocket 不会因票据到期自动断开，关闭/重启浏览器会终止连接。

桌面 nginx 在本容器内检查会话，静态资源和 WebSocket 仍直接由原版 Selkies 提供。仅认证请求访问管家；画面与输入不经过管家。CDP 仍有独立 Basic Auth，仅在 Docker 内网可达，不公开端口或密码。

## 清空浏览器数据

界面提供“清空浏览器数据”，必须在确认框中明确确认后才执行：

1. 暂停账号任务，撤销内存消息 Token；已有账号保留身份和业务设置，清除已保存的旧登录 Cookie。
2. 关闭该浏览器和监听。
3. 校验部署及用户归属，只删除该 `primary` 容器的 `/config` 命名数据卷。
4. 创建干净浏览器，固定端口仍为 `3001`。用户重新登录。

清空范围包括 Chrome 登录资料、Cookie、缓存、本地存储、历史等 `/config` 内数据；不会删除账号设置、商品、订单或其他浏览器的历史数据卷。没有点击确认不会清空。

## 部署

```sh
docker pull lscr.io/linuxserver/chrome:latest
export ACCOUNT_BROWSER_CDP_CONFIG="$PWD/deploy/account-browser/cdp.conf"
docker compose build
docker compose up -d
```

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| ACCOUNT_BROWSER_IMAGE | lscr.io/linuxserver/chrome:latest | 官方镜像，无自定义镜像构建 |
| ACCOUNT_BROWSER_NETWORK | xianyu-browsers | 浏览器与管家使用的 Docker 网络 |
| ACCOUNT_BROWSER_CDP_CONFIG | Compose 目录下 deploy/account-browser/cdp.conf | Docker 宿主机上的绝对路径 |
| ACCOUNT_BROWSER_API_URL | http://xianyu-app:8080 | 浏览器容器可访问的管家内网地址，用于桌面认证 |
| ACCOUNT_BROWSER_BIND | 127.0.0.1 | 固定宿主端口 3001 的绑定地址 |
| ACCOUNT_BROWSER_HOST | localhost | 用户看到的桌面主机名 |

NAS 使用 `docker-compose.nas.yml`，设置正确宿主机配置路径、绑定地址和主机名。`3001` 必须未被其他服务占用；不会自动改成随机端口。镜像自签名证书提示仍由用户核对地址后处理，一次性链接不绕过浏览器证书检查。

镜像仅增加启动参数、持久资料卷、只读 CDP 配置，以及一个[官方支持的 `/custom-cont-init.d/` 初始化脚本](https://docs.linuxserver.io/general/container-customization/)。初始化脚本以 root 所有权安装到容器，修改桌面认证而不改写原生静态资源或输入协议。脚本失败时不会撤掉原有认证；健康检查失败后管家停止该容器。

管家仍需 Docker 管理权限；浏览器本身不挂载 Docker socket。命名卷以部署 ID、用户和固定 primary 槽位命名；已有旧版账号浏览器数据卷不自动迁移或删除。

## 验证边界

单元测试包括首次捕获后自动建号、重复账号不覆盖、身份切换拒绝、Token 到期/拒绝、桌面票据一次性/过期/登录退出/容器重建失效、清空前明确确认及归属校验。

原生集成测试使用独立官方 Chrome：通过 HTTP 管家测试页点击 HTTPS 桌面链接，验证免密码打开、原生 Selkies 输入组件正常加载；并验证实际网页只发送一次 Token 请求，监听器捕获其响应而不补发请求。

真实验收仍需用户完成闲鱼登录与网站验证，捕获真实 Token 后观察消息注册及收发。捕获 Token 或测试通过，都不等于平台已经接受真实账号的独立消息连接。
