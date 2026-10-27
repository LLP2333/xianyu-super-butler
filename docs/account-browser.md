# 单账号原生浏览器登录

账号管理先使用一个账号、一份浏览器资料和固定 HTTP 端口 `3000`。桌面由官方 `lscr.io/linuxserver/chrome:latest` 直接提供，管家不转发画面或鼠标输入，不主动请求闲鱼消息 Token。

## 目录与职责

```text
app/
├─ api_account_browser.py              # 管理员入口、一次性桌面链接、访问会话认证
├─ services/account_browser.py         # 首次添加、暂停/恢复、账号归属、明确清空操作
└─ db_manager.py                       # 原子创建单个账号，禁止覆盖已有账号
utils/account_browser/
├─ containers.py                       # 官方容器、固定端口、专属资料卷、限定范围清空
├─ manager.py                          # 浏览器生命周期、内网 CDP 连接
├─ address.py                          # 系统配置中的桌面域名校验
├─ credentials.py                      # 请求前拦截失效凭据，响应拒绝后禁止重试
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

原扫码添加弹窗、扫码添加接口、手工 POST Cookie 添加入口以及密码自动登录接口已删除。账号设置不再收集闲鱼用户名、密码或“登录时显示浏览器”选项；登录和验证统一在原生 Chrome 中完成。已有账号可继续编辑设置；多账号业务模块没有扩展，此入口一次只允许一个账号。

## 首次添加：同一个浏览器生命周期

```text
点击“浏览器添加账号”
  → 启动 primary 原生浏览器（此时数据库中还没有闲鱼账号）
  → 建立只读监听 → 一次性链接打开 http://localhost:3000
  → 用户在 Chrome 中登录、完成网站验证、打开 www.goofish.com/im
  → 网页自行获取消息 Token
  → 核对请求 Cookie 的 unb 与当前 Cookie 的 unb 一致
  → 原子创建账号记录，保存 Cookie，授权该网页消息 Token
  → 同一浏览器、同一 profile、同一监听继续运行，无重启或资料迁移
  → 账号任务使用网页 Token 与对应 deviceId/appKey 连接消息服务
  → 收到 /reg 成功响应后显示“监听中”
```

已有账号凭据仍有效时，打开浏览器只查看监听，不暂停正常任务。凭据过期、网页退出登录、监听断开或平台拒绝凭据时，立即阻止后续 HTTP 请求和消息发送，并由管理循环暂停账号任务。用户在同一个原生浏览器登录、验证或刷新消息页后，监听器自动核对身份、保存 Cookie、采用 Token 并恢复任务，无需点击确认按钮。

账号浏览器窗口的“获取新凭据”按钮会主动暂停账号任务，启动或复用已有 Chrome，打开闲鱼首页并点击页面实际的“消息”链接。网站自行处理跳转参数，监听器沿用原流程保存 Cookie、采用消息 Token、关闭闲鱼标签页并恢复账号任务。打开首页和点击入口各最多等待 20 秒；失败时保留页面并提示手动处理，不直接跳转消息 URL，不强制点击被登录或验证遮挡的入口。按钮执行期间拒绝重复请求；此操作仅手动触发，没有定时刷新。

保存或启动失败时保持暂停并显示原因；关闭弹窗不改变该状态。业务接口不再通过重试换取凭据，也不再回写响应 Cookie。账号身份必须与已有记录一致。正常业务错误不等于凭据失效。已发出的请求无法撤回，但其迟到响应不能撤销后续采用的新凭据。相同 Token 的重复捕获或监听重连不会延长其本地有效期。

```text
等待网页登录（请求被拦截）
  → 网页产生新凭据 → 核对身份 → 暂停旧任务 → 保存 Cookie
  → 采用消息 Token → 恢复账号任务 → 等待消息注册结果
  → 凭据失效／平台拒绝 → 再次进入等待网页登录
```

相关状态只返回捕获时间、采用结果和错误原因，不向前端返回 Cookie 或消息 Token。

关闭管理弹窗不会停止浏览器或监听。首次添加可在后台完成；用户可重新打开入口查看进度。浏览器独立保留 profile；管家正常停止时关闭其监听和浏览器，让 Chrome 写入数据。

## 三种凭证

| 凭证 | 用途 | 存储/传递 |
| --- | --- | --- |
| 桌面一次性票据 | 从管家打开原生桌面，免输用户名/密码 | 随机票据，60 秒内一次有效；兑换后跳转根路径，不继续留在地址栏 |
| 闲鱼 Cookie | 账号登录状态以及其他账号业务请求 | 捕获并核对后保存在账号数据库 |
| 闲鱼消息 Token | 管家的聊天连接注册 | 仅内存保存，不下发前端、不写日志；到期或被拒绝后等待网页新 Token |

桌面票据不包含闲鱼 Cookie、消息 Token、管家登录 Token或内部调试密码。兑换后使用 HttpOnly、SameSite=Lax 的桌面会话 Cookie；访问地址为 HTTPS 时同时设置 Secure，访问期限一小时；管理员退出登录或容器重建后，新请求不能继续访问。已经建立的原生 WebSocket 不会因票据到期自动断开，关闭/重启浏览器会终止连接。

桌面 nginx 在本容器内检查会话，静态资源和 WebSocket 仍直接由原版 Selkies 提供。仅认证请求访问管家；画面与输入不经过管家。CDP 仍有独立 Basic Auth，仅在 Docker 内网可达，不公开端口或密码。

## 清空浏览器数据

界面提供“清空浏览器数据”，必须在确认框中明确确认后才执行：

1. 暂停账号任务，撤销内存消息 Token；已有账号保留身份和业务设置，清除已保存的旧登录 Cookie。
2. 关闭该浏览器和监听。
3. 校验部署及用户归属，只删除该 `primary` 容器的 `/config` 命名数据卷。
4. 创建干净浏览器，固定端口仍为 `3000`。用户重新登录。

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
| ACCOUNT_BROWSER_BIND | 127.0.0.1 | 固定宿主端口 3000 的绑定地址 |
| ACCOUNT_BROWSER_HOST | localhost | 用户看到的桌面主机名 |

NAS 使用 `docker-compose.nas.yml`，设置正确宿主机配置路径、绑定地址和主机名。`3000` 必须未被其他服务占用；不会自动改成随机端口。更新后重启管家，再启动浏览器，旧端口容器会重建并复用原有 `/config` 资料卷，不需要清空登录数据。

镜像仅增加启动参数、持久资料卷、只读 CDP 配置，以及一个[官方支持的 `/custom-cont-init.d/` 初始化脚本](https://docs.linuxserver.io/general/container-customization/)。初始化脚本以 root 所有权安装到容器，修改桌面认证而不改写原生静态资源或输入协议。脚本失败时不会撤掉原有认证；健康检查失败后管家停止该容器。

管家仍需 Docker 管理权限；浏览器本身不挂载 Docker socket。命名卷以部署 ID、用户和固定 primary 槽位命名；已有旧版账号浏览器数据卷不自动迁移或删除。

## 验证边界

单元测试包括首次捕获后自动建号、重复账号不覆盖、身份切换拒绝、Token 到期/拒绝、桌面票据一次性/过期/登录退出/容器重建失效、清空前明确确认及归属校验。

原生集成测试使用独立官方 Chrome：通过 HTTP 管家测试页点击 HTTPS 桌面链接，验证免密码打开、原生 Selkies 输入组件正常加载；并验证实际网页只发送一次 Token 请求，监听器捕获其响应而不补发请求。

真实验收仍需用户完成闲鱼登录与网站验证，捕获真实 Token 后观察消息注册及收发。捕获 Token 或测试通过，都不等于平台已经接受真实账号的独立消息连接。

## 本地与外网地址

系统配置 → 账号与同步 → 原生浏览器访问地址，可选择本地地址或自定义 HTTP/HTTPS 根地址（例如 `https://browser.example.com`）。空配置使用部署环境的本地地址；自定义地址同时用于弹窗显示和一次性桌面授权链接，不改变内部 CDP 连接。域名需要配置 HTTPS，反向代理通过 HTTP 和 WebSocket 转发到同一个原生浏览器的 3000 端口，不支持子路径部署。保存后重新打开桌面即可生效。

### Cloudflare Tunnel

```text
用户浏览器 → https://browser.example.com
           → Cloudflare Tunnel → http://127.0.0.1:3000
                              → Chrome 容器:3000
Chrome 容器 → Docker 内网 → 管家:8080（授权校验）
管家 → Docker 内网 → Chrome 容器:9223（CDP）
```

Tunnel 运行在 Docker 宿主机时，公开主机名设置为 `browser.example.com`，服务类型选 **HTTP**，URL 填 `127.0.0.1:3000`。管家系统设置中的桌面地址填 `https://browser.example.com`，然后从管家点击打开桌面，取得一次性授权。直接打开域名且没有桌面会话时返回未授权属于正常行为。

Tunnel 如果运行在独立 Docker 容器中，`127.0.0.1` 指向 Tunnel 自身。此时将 `ACCOUNT_BROWSER_BIND` 设置为宿主机可达的网卡地址（或 `0.0.0.0`），重启管家并重新启动浏览器，Tunnel 服务地址使用 `http://宿主机可达地址:3000`。

源站无需 HTTPS，也无需关闭证书校验。外部访问继续使用 HTTPS，保证桌面音视频所需的浏览器安全上下文；直接通过远程 IP 的 HTTP 访问可能无法正常使用这些功能。

## 监听状态

弹窗独立显示浏览器监听状态和凭据捕获结果，每两秒刷新。只有后台监听任务存活、CDP 上下文已连接且 ready 时才显示“监听中”；断开、连接中和状态查询失败分别展示。重连操作只重建监听连接，复用原浏览器及登录资料；监听正常时禁用重连按钮。
