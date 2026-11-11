# Million Pound · 家庭宠物守护

面向一只拉布拉多和一只缅因猫，识别吃饭、拆家、乱拉乱尿。Python + FastAPI + FFmpeg，配置网页与监控任务由同一服务运行，事件存入 SQLite。

## 在 Python 虚拟环境中启动

需要 Python 3.11+、FFmpeg（含 ffprobe 和 libx264）。所有 Python 命令均使用项目的 `.venv`，不向系统 Python 安装依赖。

```bash
cd ~/million-pound
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m million_pound init
.venv/bin/python -m million_pound serve
```

访问 `http://127.0.0.1:8097`。`init` 只运行一次，会显示随机初始密码，磁盘只保存密码哈希。默认暂停监控；配置视频源、测试连接、设置通知方式后，点击“开启守护”。8099 已由摄像头桥接占用，应用使用 8097。

当前开发设备已初始化、配置好视频源、模型与 Bark，后台服务及 linger 均已启用。直接登录即可使用，无需再次运行 `init`。

修改登录密码：

```bash
.venv/bin/python -m million_pound password
```

按提示输入至少 12 位密码，重启应用后生效；旧登录状态和旧图片签名一起失效。Cookie 有效期 12 小时，HTTPS 访问时启用 Secure。

## 小米摄像头接入

采用 [ha-xiaomi-camera 独立桥接服务](https://github.com/willjayyyy/ha-xiaomi-camera/blob/main/README.zh-CN.md#不用-home-assistant-也能跑)。需要 Docker，桥接主机与摄像头处于同一局域网。

1. 首次部署时复制 `.env.example` 为 `.env`，填写两个不同的随机长密码，并执行 `chmod 600 .env`。当前开发设备已配置，无需重复覆盖。
2. 执行 `sudo docker compose -f compose.camera.yaml up -d`。
3. 在 PC 直接打开 `http://127.0.0.1:8098`，用 `.env` 中的 `XIAOMI_CAMERA_WEB_PASSWORD` 登录。
4. 在桥接页面连接小米账号；小米登录完成后，将浏览器完整回调地址按页面提示粘贴回去。
5. 将摄像头的视频流地址填入应用设置。桥接为局域网模式，RTSP 地址需加入用户名 `xiaomi` 与 `.env` 中的 `XIAOMI_CAMERA_RTSP_PASSWORD`，形式为 `rtsp://xiaomi:密码@127.0.0.1:8554/camera_设备ID`。
6. 保存设置，点击“测试并查看截图”。默认使用原始视频流，应用支持 H.264/H.265 输入。

小米授权持久化在 `camera-data/`。不要把 8098 或 8554 映射到公网域名；公网只代理 8097 的应用。桥接镜像版本采用上游 `latest`，升级后需重新测试取流。

## 监控行为

- 按北京时间执行每天的监控时段，支持跨午夜；不填时段或开始与结束相同表示全天。
- 采样间隔按两轮开始时间计算。录制、加速、初判、截图复核串行执行；一轮超时则完成后再开始下一轮，不叠加采样任务。
- 原速录像编码为 H.264 并将首帧时间对齐到零，避免 RTSP 等待关键帧导致短片开头空白或无截图；录像保留原始分辨率。
- 加速视频交给 `d3` 的 `/v1/systemone`。三类事件分别判断，概率达到 0.7 进入候选；接口字段缺失或无效会记录运行错误，不当作“没有事件”。
- 候选截图取自原速录像的 10%、50%、90% 位置；事件时间显示本段原视频采样开始时间，截图标注原视频内的秒数。
- 仅有候选且达到复核间隔时，才调用 `swift-1.5` 的 `/v1/chat/completions`。复核开始时间持久化，失败及重启也不会绕过最短间隔。
- 因间隔限制未复核的候选标为“未复核”，不排队、不发送通知；之后新采样仍有该行为时再复核。原片不长期保存。
- 只有复核明确确认的事件才按类型发送 Bark；默认吃饭只记录、拆家及乱拉乱尿通知，可在网页修改。
- 同类型、同宠物在五分钟内合并通知。通知失败会标明；发送中断时标为“结果待确认”，不会自动重试原通知。
- 保存配置或暂停守护会中止当前采样，未完成复核的候选会标记中断。
- HTTPX 与 FFmpeg 不使用系统代理，保证局域网模型和摄像头直连。模型服务地址可填写根地址或带 `/v1` 的地址。

定时采样可能漏掉短暂行为，加速及少量截图也可能遗漏证据。实际行为效果需用家中宠物画面调整采样参数，不保证零误报或漏报。

## Bark 与公网截图

在配置页填写 Bark 地址。可以直接粘贴 Bark 示例中的完整 URL，应用会提取设备密钥并保留 `group`、`ttl` 等查询选项，再替换通知标题与正文。支持常规 `https://服务器/设备密钥/标题/正文` 格式。

按 [Bark 官方参数](https://github.com/Finb/Bark/blob/master/docs/en-us/params.md#image)，图片需要手机可访问的 URL：

1. 将 `https://pets.example.com` 的 HTTPS 反向代理指向 `127.0.0.1:8097`，参考 `deploy/nginx-location.conf`。转发 Host 和 X-Forwarded-Proto。
2. 在网页“公网访问地址”填写该 HTTPS 地址；留空时只发文字，事件里会明确标记。
3. 通知使用 `/shared/文件名?expires=...&sig=...`，一小时内无需登录即可读取指定截图；篡改文件名或到期会拒绝访问。普通事件与截图接口始终需要登录。
4. 点击“发送测试通知”可测试文字通道；真实确认事件用于最终验证手机图片展示。

## 后台运行

仓库提供当前设备的用户级 systemd 服务；换目录时修改其中的绝对路径。

```bash
mkdir -p ~/.config/systemd/user
cp deploy/million-pound.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now million-pound
systemctl --user status million-pound
tail -n 50 data/server.log
```

若需要退出 SSH 后继续运行及开机启动，由管理员执行一次 `sudo loginctl enable-linger "$USER"`。

只运行一个应用实例、一个 worker，避免重复采样与推送。更改本地配置文件或 CLI 修改密码后，执行 `systemctl --user restart million-pound`；网页修改设置即时生效。

## 本地数据与检查

- `config.json`：模型密钥、Bark 地址、视频源及设置，权限 600；网页不会返回已保存的密钥或带凭据的视频源。留空保留已有值，接口 PUT 显式传空字符串可清除。
- `.env`：桥接网页与 RTSP 密码，权限 600。
- `data/auth.json`：密码哈希和签名密钥。
- `data/events.sqlite3`：候选、复核结果、通知状态与截图索引。
- `data/images/`：事件截图，默认七天，约每小时检查清理；到期保留事件文字。
- 录像临时目录在每轮结束或取消时清理，异常退出遗留的临时片段会在应用下次启动时删除。
- 原始 `需求.txt` 含密钥，和以上运行数据均已通过 `.gitignore` 排除。

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试不调用真实模型、不发送通知，覆盖北京时间时段、配置校验、复核节流、事件模式、通知去重、任务取消清理、登录、CSRF、密钥脱敏、配置恢复、图片签名与保留期。

2026-11-11：17 项检查通过；另已实测本机摄像头取流、原视频截图、加速视频、两个模型真实请求、Bark 文字发送，以及桌面和手机浏览器操作。真实确认事件的图文通知需在公网域名接通后继续验证。
