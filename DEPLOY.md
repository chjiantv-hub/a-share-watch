# A股盯盘 云端常驻部署

本目录是可直接部署的版本（不含 ngrok/watchdog）。镜像内单进程 uvicorn，FastAPI 启动时自动拉起后台轮询线程。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ACCESS_TOKEN` | 无（未设则在 /data 生成随机令牌） | 访问令牌。沿用现有链接就填 `/workspace/a-share-watch/data/token.txt` 的值 |
| `PORT` | 8080 | 监听端口 |
| `DATA_DIR` | /data | rules.json、alert_log.json、watchlist.md、行情缓存。首次启动从 `seed/watchlist.md`（仓库里是空表）复制 |
| `WATCHLIST_SEED` | 空 | 可选，如 `002343,600838,301520`：自选为空时启动即填入（个人自选不进公开仓库/镜像） |

`/healthz` 无需令牌（给保活/健康检查用），其余页面和接口都要令牌（`?t=`、Cookie 或 `X-Token`）。

## 平台选择
- **首选 ClawCloud Run（日本/东京区）**：容器常驻不休眠；免信用卡；用注册满 180 天的 GitHub 账号登录每月送 $5 额度（本应用 0.25 vCPU/512 MB 约 $2–3/月，够用）；亚洲机房，大陆访问一般可用（新加坡区 2026 年初有过大陆大面积连不上的反馈，所以选日本）；支持本地存储挂载，提醒规则重启不丢。
  - 前提：GitHub 账号注册 ≥180 天。不满足只送一次 $5（约 1 个月），之后要换 Render。**chjian007-ctrl 注册于 2026-10-31，不满足**，ClawCloud 只能免费约 1 个月。
- **备选 Render 免费版（新加坡区）**：免信用卡；15 分钟无访问会休眠（唤醒约 1 分钟，休眠期间后台轮询停止）→ 用 cron-job.org 每 10 分钟访问 `/healthz` 保持常驻（750 小时/月免费额度 ≥ 744 小时，一个服务 24 小时在线够用）；文件系统临时，重新部署/重启后提醒规则、触发记录、App 内改过的自选会回到初始状态；`*.onrender.com` 在大陆属于“部分子域时通时断”。
- 不选：Hugging Face Spaces（Docker Space 现在需要付费 PRO 才能建；huggingface.co 与 hf.space 在大陆被墙）、Koyeb（要信用卡，免费实例 1 小时无流量缩到 0，只有法兰克福/华盛顿）、Fly.io（无免费档）、Google Cloud Run / Oracle Always Free（都要信用卡）、Zeabur 免费版（闲置休眠）、Northflank Sandbox（价格页写“免卡、常驻”，但文档写“所有用户必须添加付款方式”，且官网正挂着安全事件公告，暂不推荐）。

## 第 1 步：推代码 + 产出镜像（一次性，需要 GitHub 令牌）
1. 浏览器：登录 GitHub → Settings → Developer settings → Personal access tokens (classic) → 新建，勾选 `repo`、`workflow`、`write:packages`（另建一个只勾 `read:packages` 的给 ClawCloud 拉镜像用）。
2. 在本机：`GH_TOKEN=ghp_xxx ./deploy.sh`
   - 创建**私有**仓库 `<用户名>/a-share-watch` 并推送；
   - 构建并推送镜像 `ghcr.io/<用户名小写>/a-share-watch-cloud:latest`（本机有 docker 就直接推，否则由仓库里的 GitHub Action 自动构建，约 3 分钟）。
   - 令牌不会写进仓库或镜像。

## 第 2A 步：ClawCloud Run（首选）——需要浏览器
1. 打开 https://run.claw.cloud → 用 **GitHub 登录**（这一步即完成注册并绑定 GitHub，无需信用卡）→ 右上角区域选 **Japan**。
2. App Launchpad → Create App：
   - Image：Private；Image Name `ghcr.io/<用户名小写>/a-share-watch-cloud:latest`；Registry `ghcr.io`；Username = GitHub 用户名；Password = read:packages 令牌
   - Usage: Fixed，Replicas 1，CPU 0.25（或 0.2），Memory 512M
   - Network：Container Port `8080`，打开 Public Access（自动生成 https 域名）
   - Environment：`ACCESS_TOKEN=<现有令牌>`，`WATCHLIST_SEED=<自选代码,逗号分隔>`
   - Local Storage：挂载路径 `/data`，1 GiB
3. 状态变 Running 后，打开 `https://<分配的域名>/healthz` 应返回 `{"ok":true,...}`；手机打开 `https://<域名>/?t=<令牌>`。
4. 更新代码：改完再跑 `./deploy.sh`，然后在 ClawCloud 应用页点 Restart/Update（镜像标签 latest，拉取策略 Always）。
5. 额度：控制台 Cost Center 看每日花费，确认 < $5/月。

## 第 2B 步：Render（备选）——需要浏览器
1. https://render.com → 用 GitHub 登录（免卡）→ 授权 Render 访问仓库 `a-share-watch`。
2. New → Blueprint → 选该仓库（读取 `render.yaml`：Docker、free、singapore、健康检查 /healthz）→ 填 `ACCESS_TOKEN` → Apply。
3. 保活（必须，否则 15 分钟后休眠、盘中不再轮询）：https://cron-job.org 免费注册 → 新建任务，URL `https://<服务名>.onrender.com/healthz`，每 10 分钟（至少覆盖北京时间 9:00–15:10 交易日；全天也行，额度够）。
4. 之后 `./deploy.sh` 推送即自动部署。注意部署/重启会清空 /data（规则与 App 内自选改动），稳定的自选请改 `seed/watchlist.md` 再推送。

## 本地验证（已做）
- `./run_local.sh`（端口默认 8790，可 `PORT=8797 ./run_local.sh`）——不碰正在运行的 8765 服务。
- Docker：`docker build -t a-share-watch:local . && docker run -e PORT=8798 -e ACCESS_TOKEN=... --network=host a-share-watch:local`

## 注意
- 云端的自选股是 `/data/watchlist.md` 独立副本，与本机 `/workspace/a-share-pool/watchlist.md`（盘前/竞价例行任务读的那份）不再自动同步。
- 大陆可达性随运营商/时段变化，部署后请用手机流量和 WiFi 各打开一次确认。
