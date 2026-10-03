# ZCode 任务通知

> ZCode 任务跑完不用守着，任务一完成，手机自动收到消息提醒。
>
> 开源：https://github.com/cjy-kenny/zcode-notify （MIT License）

电脑端跑一个通知服务，ZCode 任务开始（UserPromptSubmit hook）和结束（Stop hook）
都会自动回调它；手机端通过**网页版**（浏览器打开即用）或 **APK 版**（前台服务常驻，
后台也能弹系统通知）实时接收提醒。在家走局域网秒达，人在外面走公网 MQTT 中转也能收；
带响铃、震动、历史记录。

```
┌─────────────── 电脑（Windows） ───────────────┐
│  ZCode ──Stop hook──> hook_notify.py          │
│                          │ POST /notify       │
│                     server.py :8787           │
│                      ├─ 管理面板  /            │
│                      └─ SSE 推送 /events       │
└──────────────────────┬────────────────────────┘
                       │ 同一 Wi-Fi
              ┌────────▼─────────┐
              │ 手机：通知页 / APK │
              └──────────────────┘
```

## 快速开始

### 1. 电脑端：启动服务

双击 **启动通知服务.bat**（或 `python server.py`）。窗口会显示：

- 管理面板：`http://<电脑IP>:8787/`
- 手机端地址：`http://<电脑IP>:8787/phone`

### 2. 手机端（二选一）

**网页版（先试这个）**：手机浏览器打开上面的手机端地址（电脑控制台上有二维码可扫），
允许通知、可"添加到主屏幕"当 app 用。适合快速验证。

**APK 版（推荐日常用）**：把 `dist/zcode-notify-v0.3.apk` 传到手机安装（微信会被
改名，建议压缩成 zip 再传，长按"用其他应用打开"）。首次打开：

1. 手机和电脑连同一个 Wi-Fi，打开 app——它会自动搜索电脑并连上（UDP 广播发现，
   多台电脑时弹列表选）；搜不到才需要手动粘贴控制台上的「手机端地址」
2. 允许通知权限
3. 之后它会以"ZCode 通知服务运行中"常驻后台，app 不开着也能收到；
   电脑 IP 变了自动重搜，手机重启后自动恢复服务，人在外面走流量也照收

### 3. 验证链路

电脑控制台点「发送测试通知」，或 `python selftest.py`——所有在线手机应立刻响铃弹卡。

## ZCode hook（已完成，换机器才需要重配）

用户级 `~/.zcode/cli/config.json` 已注册两个 hook（`hooks.enabled: true`，
`process` 类型直调 Python，5s 超时，服务没开则静默跳过）：
**UserPromptSubmit**（收到新任务，args 带 `start` 参数）和 **Stop**（任务完成，
含摘要提取）。通知服务没开时两者都静默跳过。**注意：ZCode 在会话启动时读取
hooks 配置，改完要新开会话才生效。**

```json
{ "hooks": { "enabled": true, "events": { "Stop": [
  { "hooks": [ { "type": "process",
    "command": "<python.exe 绝对路径>",
    "args": ["<本项目>/hook_notify.py"],
    "timeoutMs": 5000 } ] } ] } } }
```

通知内容会带上项目名和最后一条回复的摘要（从会话转录提取，读不到就只报"执行完毕"）。
hook 对每轮回复结束都会触发；只想在特定工作区用的话，把这段 `hooks` 挪到
`<工作区>/.zcode/config.json` 即可。

## 机制说明

- **推送（双通道）**：局域网走 SSE 长连接（`/events`，15s 心跳，断线 3s 重连）；
  跨网络走公网 MQTT 中转（broker.emqx.io，服务端零依赖手写 QoS1 发布，手机端
  Paho 订阅）。订阅码随机生成（state.json 的 mqtt_topic），双通道按通知 id 去重；
  公网不通时自动退回只走局域网，互不影响。
- **自动配对**：服务端在 UDP 8788 应答 `ZCODE-NOTIFY-DISCOVER` 广播（回主机名 +
  可用 IP）；app 打开即广播、探测 `/status` 后自动连接——「装上就连」限同一 Wi-Fi。
- **省心三件**：电脑端 设置开机自启.bat（写入启动文件夹静默启动，取消开机自启.bat
  可撤销，是否启用由你双击决定）；手机端开机自启 Receiver（重启自动恢复服务）；
  订阅码无需手填——app 自己从 `/status` 取。
- **鉴权**：写入（POST /notify）本机回环免口令，局域网需 `?token=`（服务首次启动
  自动生成，存 `state.json`）；读取不设限——v0.1 假定家庭 Wi-Fi 可信。
- **防重复**：app 在前台看通知页时，网页自己响铃，前台服务不再弹系统通知。
- **回程链接**：state.json 的 `remote_url`（管理面板可设，走 POST /config）非空时，每条通知自动带上 `link` 字段，手机通知页卡片出现「打开远程页面回复」按钮，点一下直达 ZCode 官方远程输入框——通知知进度，一键回话。远程会话换了在面板更新链接即可。
- **历史**：服务端保留最近 200 条（`state.json`），手机端拉取 `/history` 渲染。
- **APK**：无 Gradle 手工流水线（`android/build_apk.py`，优先复用随身编程项目的
  `_build` 工具链，缺失时自动回退本机 Android SDK），WebView 壳 + 前台服务（dataSync），
  debug 签名，minSdk 24。

## 文件结构

```
zcode-notify/
├── server.py            服务端（单文件，零依赖）
├── hook_notify.py       ZCode Stop hook 回调脚本
├── selftest.py          一键自测
├── gen_icon.py          图标生成（纯标准库写 PNG）
├── 启动通知服务.bat       电脑端启动入口
├── 设置开机自启.bat       可选：登录 Windows 静默启动服务（取消开机自启.bat 撤销）
├── 打包APK.bat           APK 构建入口
├── make_package.py       打手机传输压缩包（微信传 zip 不改名，内附使用说明）
├── web/                 手机端页面 + 控制台 + PWA 清单
├── android/             WebView 壳 + 前台服务 + MQTT 订阅 + 构建流水线
└── dist/                zcode-notify-v0.3.apk + ZCode任务通知-安装包-v0.3.zip（微信传输用）
```

仓库内已附带两个第三方文件，免去国内下载困难：`android/libs/org.eclipse.paho.client.mqttv3-1.2.5.jar`
（Eclipse Paho MQTT 客户端，EPL/EDL 双许可）、`web/qr.min.js`（qrcodejs，MIT）。

## 已实测 / 未实测

- ✅ 已实测（电脑端）：服务启动、UDP 自动配对应答、hook 脚本（开始+完成+摘要提取）、
  SSE 实时推送、公网 MQTT 往返、真实订阅码端到端演练（电脑→broker→订阅方）、
  页面渲染、历史持久化、二维码/复制地址、APK 构建与签名校验
- ✅ 已实测（真机 OPPO PFZM10，Android 15，2026-10-03）：APK 安装启动、手动粘贴连接、
  SSE 实时推送、前台服务常驻（dataSync）、回程按钮渲染。注：首启 UDP 自动配对在该机型
  未成（手机省电休眠+广播不可靠），手动粘贴即用；锁屏系统通知需在 app 内点「开启系统通知」
  授权（Android 15 不允许 adb 代授）后验证
- ⏳ 待真机验证：后台/锁屏收通知、跨网络实际收发、手机重启自启、通知点击拉起 app
- 已知边界：跨网络依赖公共 broker（broker.emqx.io，可靠性不作保证；server.py 顶部
  两个常量可改成自建 mosquitto）；手机离线时的消息不补投（clean session）；电脑关机
  或服务没开就收不到；部分国产 ROM 会杀后台/拦自启，可把 app 加白名单

## v0.4 候选

自建/加密中转（MQTTS 或自托管 mosquitto，公共实例只作默认兜底）、离线消息补投
（persistent session）、任务失败提醒（PostToolUseFailure）、图标与通知渠道细节、
iOS 侧（Bark 对接）。~~通知里加"查看回复全文"链接~~（full 字段已带全文）、
~~回程链接~~（已实现：remote_url + 手机端按钮）。
