# 账号常驻浏览器

每个启用账号拥有一个有界面 Chromium、独立 Xvfb 桌面、Openbox 窗口管理器和只监听回环地址的 x11vnc 服务。账号数据目录位于 `data/browser_profiles/`，目录名由平台用户 ID 和账号 ID 的组合散列生成。

```text
frontend/components/AccountBrowser.tsx  # noVNC 远程桌面与接管按钮
app/api_account_browser.py             # 账号鉴权、一次性连接票据、VNC 二进制转发
app/services/account_browser.py        # 接管、Token 确认、凭证保存、后台巡视
utils/account_browser/
├── manager.py                         # 每账号单实例、持久化目录、Cookie 检查点
└── desktop.py                         # 私有 X 显示、窗口管理器、VNC 进程
app/cookie_manager.py                  # 在账号事件循环中暂停及恢复自动任务
utils/manual_captcha.py                # Cookie 格式转换与 Token 请求
```

## 操作与状态

- 启用账号后，后台会启动并维持其浏览器；巡视间隔为 10 秒，启动多个账号可能需要更长时间。
- “打开浏览器”会暂停该账号自动任务并取得操作权；再次打开重用同一浏览器。一个账号同时只有一个远程操作连接，新连接替换旧连接。
- “关闭窗口”只断开连接，浏览器继续运行，账号仍保持接管。列表显示“浏览器接管中”。
- “打开验证页”在现有浏览器中打开实时获得的验证链接。验证页面出错时，可直接使用浏览器刷新或再次点击此按钮。
- “确认登录并恢复”先断开键鼠输入，再检查浏览器中登录的闲鱼用户与原账号一致；仅在 Token 成功后保存 Cookie、清除风控状态并恢复自动任务。失败则保留浏览器及接管状态，可重新连接继续处理。
- “不保存并结束接管”使用数据库中原有凭证恢复任务，不向数据库保存当前浏览器凭证。浏览器数据目录仍保留。
- 禁用或删除账号后，后台巡视停止对应浏览器。禁用保留数据目录。
- 进程异常退出后重新启动新进程，并使用原数据目录。服务重启保留浏览器登录数据；内存中的远程连接和接管状态不会跨服务重启保留。

浏览器 profile 持久化本地存储等状态。另保存 Cookie 检查点，以保留 Chromium 干净退出时可能清除的 session Cookie；正常每 10 秒及关闭前写入。突然断电可能丢失最近一次检查点后的修改。数据库凭证发生变化（例如重新扫码）后，非接管状态下同步至浏览器。

自动回复、消息连接仍由现有账号任务执行；这次替换的是人工操作及其浏览器生命周期，没有将商品抓取等所有独立自动化浏览器任务统一迁入常驻实例。

## 部署

重新构建 `Dockerfile` 或 `Dockerfile-cn`，镜像已安装 `xvfb`、`x11vnc`、`openbox` 和中文字体。现有 Compose 的 `./data:/app/data` 挂载会保留浏览器数据。每账号常驻 Chromium 会增加内存占用，请按账号数量调整 Compose 的内存限制。

远程桌面仅向系统管理员开放，并校验账号归属。各桌面共享应用容器的操作系统，不构成不可信租户之间的操作系统安全隔离，普通平台用户不能取得连接票据。

继续只暴露应用的 8080 端口，不发布 VNC 端口。noVNC 由 npm 依赖打包，无需 CDN。FastAPI 代理二进制 WebSocket 到本机 VNC，不需要额外 websockify 进程。代理须支持 WebSocket Upgrade 和长连接；仓库 nginx 配置已更新。

使用单个应用进程。账号浏览器和连接票据归该进程管理，不能用多个 Uvicorn worker 共享同一批 profile。此功能需要 Linux 桌面组件，推荐在 Docker 中运行。

## 验证

单元测试覆盖跨用户访问、票据重放/过期、Cookie 身份核对、Token 失败不保存、接管期间禁止自动重启，以及跨事件循环恢复。

真实桌面测试使用本地网页，不访问真实闲鱼账号：

```sh
cd frontend
node --input-type=module -e "import {build} from 'vite'; await build({base:'/client/',build:{outDir:'/tmp/account-browser-ui',emptyOutDir:true,rollupOptions:{input:'tests/account-browser.html'}}})"
cd ..
RUN_DESKTOP_TESTS=1 ACCOUNT_BROWSER_UI_DIR=/tmp/account-browser-ui python -m unittest tests.test_account_browser_desktop
```

桌面测试需要 Chromium、Xvfb、Openbox、x11vnc 以及项目 Python 依赖。它从实际 React 组件通过 noVNC 操作地址栏、输入框和按钮，验证关闭重开、账号隔离、进程退出清理和重启后 Cookie/本地存储保留。真实闲鱼风控是否解除仍须在实际部署中验收。
