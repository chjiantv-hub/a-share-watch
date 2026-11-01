# A股盯盘 云端常驻部署

本目录是可直接部署的版本（不含 ngrok/watchdog）。镜像内单进程 uvicorn，FastAPI 启动时自动拉起后台轮询线程。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ACCESS_TOKEN` | 无（未设则在 /data 生成随机令牌） | 访问令牌。沿用现有链接就填 `/workspace/a-share-watch/data/token.txt` 的值 |
| `PORT` | 8080 | 监听端口 |
| `DATA_DIR` | /data | rules.json、alert_log.json、watchlist.md、行情缓存。首次启动从 `seed/watchlist.md`（仓库里是空表）复制 |
| `WATCHLIST_SEED` | 空 | 可选，如 `002343,600838,301520`：自选为空时启动即填入（个人自选不进公开仓库/镜像） |
| `XAI_API_KEY` | 空 | **可选**。xAI API Key（[console.x.ai](https://console.x.ai/)）。未设置时服务照常启动，`/api/grok/*` 返回「Grok 未配置」，前端按钮禁用。密钥仅存环境变量，不下发前端。 |
| `XAI_MODEL` | `grok-4.20-0309-non-reasoning` | 可选。xAI Chat Completions 模型 ID；默认取文档中的非推理快速档（适合结构化中文解读，相对便宜）。可改为 `grok-4.3` 等。 |

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


## 五核智选 FiveCore（/fivecore/）
- 内嵌在本服务中，地址 `/fivecore/?t=<ACCESS_TOKEN>`，与盯盘共用同一访问令牌（中间件 + 常数时间比较）。
- 行情直接取自本进程缓存（腾讯 / 东方财富 / 同花顺备用 / 乐咕），标记 `public_web_unlicensed`；缺失字段为 null，页面显示「未取到」。
- 外部授权行情写入：`POST /fivecore/api/ingest/market`，请求头 `X-Ingest-Token`；需设置环境变量 `FIVECORE_INGEST_TOKEN`（未设置则写入接口关闭）。`data_quality` 以 `licensed` 开头时优先使用。
- 测试：`pip install pytest httpx && python -m pytest -q tests`

## 公告/催化自动抓取（选股入池）
- 模块 `app/catalyst.py`。观察池候选（通过硬性条件后按排序前 20 只）+ 自选，在后台抓取近 14 个自然日（约 10 个交易日）的公告与资讯；竞价重点/开盘确认卡片从缓存读取，缺失时后台补抓。
- 来源顺序（任一成功即止）：公告 = 东方财富公告 `np-anotice-stock.eastmoney.com` → 巨潮资讯 `cninfo.com.cn` → 新浪财经公告列表；资讯 = 东方财富搜索 `search-api-web.eastmoney.com`（仅保留标题提及本股的条目）。每次请求 6 秒超时、重试 1 次；并发 4。
- 分类（标题关键词）：
  - 负面排除：监管问询/立案（问询函、关注函、监管函、警示函、立案、处罚；异常波动问询回复与审核问询除外）、ST/退市风险、业绩预亏、股东减持 → 观察池直接「剔除」，理由写明公告标题。
  - 风险项：限售股解禁（股权激励解锁除外）、交易风险提示、澄清/否认相关业务、重大事项终止、诉讼/冻结 → 「未核验（存在风险项需人工核验）」。
  - 正面催化：政策、业绩增长、订单/重大合同、产业事件（含控制权变更）、增持/回购 → 「已核验（自动：类别）」；仅资讯命中时注明「仅资讯来源」。中介机构文件（法律意见、核查意见、持续督导等）不计为正面。
  - 资讯中的负面命中只降为风险项；公告源全部失败时，资讯正面也不放行。
- 失败处理：不编造。抓取失败为「未核验（公告源均失败：…）」，进行中为「未核验（抓取中/抓取超时，后台继续）」。用户点「催化已核验」优先于自动结果。
- 缓存：`$DATA_DIR/catalyst/<池日>/<代码>.json`；成功 3 小时、失败 10 分钟后重抓。`job_observe` 最多等待 8 秒，其余后台完成后自动重建观察池；`/api/pool` 只读缓存，不阻塞。
- 接口：`GET /api/catalyst/{code}?day=YYYYMMDD` 返回全部条目；`/api/diag` 含各来源成功/失败计数。环境变量 `CATALYST_FETCH=0` 可关闭。
- 可达性：本机（非新加坡）实测四个来源均可用。Render 新加坡节点曾访问东财 push2 行情接口失败，公告接口为不同主机，是否可用以 `/api/diag` 的 `catalyst.sources` 为准；失败时会自动回落到巨潮/新浪。

## Grok 行情解读（xAI）
- 模块 `app/grok_explain.py`。后端调用 `POST https://api.x.ai/v1/chat/completions`（OpenAI 兼容）；**密钥只读环境变量 `XAI_API_KEY`，永不写入仓库/镜像/前端**。
- 未配置密钥：进程正常启动；`GET /api/grok/status` → `{configured:false, model}`；`POST /api/grok/explain` → `{ok:false, error:"Grok 未配置…"}`；页面顶栏显示「Grok未配置」，按钮禁用。
- 已配置：在「情」页 / 「选」页点「Grok解读盘面」，或在自选/观察池/竞价/确认卡片点「Grok解读」。服务端用当前缓存（情绪、观察池、竞价、开盘确认、交易池、催化、现价）组装 JSON 上下文，要求模型只引用上下文数字，输出「结论 / 依据 / 风险与缺失数据 / 下一步观察点」。超时约 45 秒；同一股票或盘面同时只允许 1 个进行中的请求（防免费实例打爆）。
- 默认模型：`grok-4.20-0309-non-reasoning`（xAI 文档列出的非推理档；可用 `XAI_MODEL` 覆盖）。按 token 计费，频繁点会消耗额度，请留意 console.x.ai 用量。
- **启用步骤（有密钥后）**：Render Dashboard → Environment → 添加 `XAI_API_KEY=...`（可选 `XAI_MODEL`）→ Save → **Manual Deploy**。ClawCloud 同理改环境变量后 Restart。无需改代码。
- 接口：`GET /api/grok/status`；`POST /api/grok/explain` body `{"scope":"stock"|"market","code":"六位可选"}`。鉴权与其它 API 相同（`?t=` / Cookie / `X-Token`）。
- 测试：`python -m pytest -q tests/test_grok_explain.py`（全部 mock，不访问外网）。
