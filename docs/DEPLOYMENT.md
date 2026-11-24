# MailPilot 云端部署手册

术语和运行契约见 [架构](ARCHITECTURE.md)。部署包含定时摘要、Telegram webhook 和后台交互。邮件推送、续期及巡检可按本页配置启用。

## 准备测试环境

使用已启用计费的专用 GCP 测试项目、独立 Gmail 测试账户、Telegram 测试 Bot 和私聊。操作员需能启用 API、管理 IAM、配置 Cloud Run、Artifact Registry、Firestore、Secret Manager、Gmail API、Cloud Tasks、Scheduler 和 WIF。准备 Bash、Python 3、Docker Buildx、jq、uv 以及支持 `--invoker-iam-check` 的当前 gcloud。

默认在 `europe-west6` 配置 Cloud Run、Artifact Registry、Firestore 和 Cloud Tasks。Scheduler 资源默认在 `europe-west1`，按各计划明确配置的时区触发。可分别设定 `MAILPILOT_REGION`、`MAILPILOT_FIRESTORE_LOCATION` 和 `MAILPILOT_SCHEDULER_REGION`。创建数据库前核对其位置，Firestore 位置在创建时确定。 [Cloud Run 区域](https://docs.cloud.google.com/run/docs/locations) · [Cloud Tasks 区域](https://docs.cloud.google.com/tasks/docs/locations) · [Firestore 区域](https://docs.cloud.google.com/firestore/native/docs/locations)

```bash
export MAILPILOT_PROJECT_ID=TEST_PROJECT
export MAILPILOT_REGION=europe-west6
bash infra/deploy.sh --dry-run
gcloud auth login
bash infra/bootstrap.sh
```

干运行打印基础资源、索引及 TTL、IAM、WIF、服务和计划的 gcloud 命令。Bootstrap 启用 Run、Scheduler、Tasks、Firestore、Secret Manager、Gmail、Artifact Registry、IAM、IAM Credentials 和 STS API，并创建专用服务账号和空的应用 secrets。重复运行时复用已有资源并重新应用脚本管理的配置。已有外部授权应通过项目 IAM 单独核对。

Bootstrap 异步提交 Firestore 复合索引、单字段索引豁免和 TTL 配置，提交后继续配置其他资源。上线前检查后台操作结果，确认所需复合索引均为 `READY`，再验证依赖这些索引的查询。 [Firestore 索引操作](https://firebase.google.com/docs/firestore/query-data/indexing#use_the_google_cloud_cli)

```bash
gcloud firestore operations list --project="$MAILPILOT_PROJECT_ID" --database='(default)'
gcloud firestore indexes composite list --project="$MAILPILOT_PROJECT_ID" --database='(default)'
```

### 运行身份

| 身份 | 访问范围 |
| --- | --- |
| Ingress | 默认 Firestore 数据库、Telegram webhook secret、交互队列入队和 Tasks 调用身份的 `actAs`。 |
| Worker | 默认数据库、指定的 Bot、模型、Gmail OAuth 客户端和账户 secrets、交互队列入队和 Tasks 调用身份的 `actAs`、指定摘要 Job 的启动和参数覆盖。 |
| Job | 默认数据库、指定的 Bot、模型、Gmail OAuth 客户端和账户 secrets。 |
| Scheduler、Tasks | 分别使用各自的服务账号调用 Worker。两个 OIDC audience 都是 Worker 的默认 `run.app` origin。应用按路由校验邮件地址与签名。 |
| GitHub 部署 | 邮件助理 Artifact Registry 仓库写入、三个已有的 Cloud Run 资源部署权限及对应运行身份的 `actAs`。 |

Firestore IAM 条件限制数据库，业务集合和账户归属由应用校验。Cloud Run 部署者可以替换运行时镜像，因此需保护部署工作流、默认分支和环境。 [Firestore IAM](https://docs.cloud.google.com/firestore/native/docs/manage-databases) · [Cloud Run 服务间认证](https://docs.cloud.google.com/run/docs/authenticating/service-to-service)

### Secret Manager 和 Gmail OAuth

为 `mailpilot-telegram-webhook`、`mailpilot-telegram-bot`、`mailpilot-model-key` 通过 Secret Manager 控制台录入 webhook 随机密钥、Bot Token 和模型 API Key。Webhook 密钥使用 Telegram 允许的字母、数字、下划线和连字符。记录各版本号用于部署。在测试 Bot 中启动私聊，记录 Bot 数字 ID、私聊 ID 和用户 ID。Telegram 私聊使用相同的私聊和用户 ID，两张名单的交集接收定时摘要。 [Telegram setWebhook](https://core.telegram.org/bots/api#setwebhook)

在同一个测试项目的 Google Auth Platform 创建桌面应用客户端，使用 `openid` 和 `gmail.readonly`。若使用 External / Testing，将专用 Gmail 账号加入测试用户。该模式下 Gmail 刷新令牌通常七天过期，适用于首次验收。授权 CLI 通过本机随机端口和系统浏览器接收回调。在浏览器选中专用测试账号并授权。 [Google 桌面应用 OAuth](https://developers.google.com/identity/protocols/oauth2/native-app) · [刷新令牌过期条件](https://developers.google.com/identity/protocols/oauth2#expiration)

将下载的桌面客户端 JSON 保存在本地，把文件路径设为 `MAILPILOT_GMAIL_CLIENT_FILE`。重新运行 bootstrap 创建 `mailpilot-gmail-oauth`，将 JSON 作为 secret 新版本纳入管理，并记录数字版本号。应用部署只接收 Secret Manager 引用。

```bash
bash infra/bootstrap.sh
gcloud secrets versions add mailpilot-gmail-oauth --project "$MAILPILOT_PROJECT_ID" \
  --data-file="$MAILPILOT_GMAIL_CLIENT_FILE"
gcloud auth application-default login
uv run --locked python scripts/authorize.py authorize --provider gmail \
  --project "$MAILPILOT_PROJECT_ID" --owner "$MAILPILOT_TEST_OWNER" \
  --gmail-client-file "$MAILPILOT_GMAIL_CLIENT_FILE"
```

`MAILPILOT_TEST_OWNER` 填获准用户的 Telegram 数字 ID。CLI 输出 Gmail 账户 ID，随后用实际 ID 授予 Worker 和 Job 该账户的凭据版本权限。多个 secret ID 用空格分隔。

```bash
MAILPILOT_ACCOUNT_SECRETS="mailpilot-gmail-ACCOUNT_ID" bash infra/bootstrap.sh
```

旧 Outlook 部署可显式设置 `MAILPILOT_EMAIL_PROVIDER=outlook`，使用既有 Entra 桌面应用、`Mail.Read` 和旧的授权 CLI 参数。 [Microsoft Entra 回调配置](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url)

## 首次部署

用操作员身份推送初始镜像。首个 Job 和两个服务由操作员创建，后续发布可使用 GitHub 环境。

```bash
registry="$MAILPILOT_REGION-docker.pkg.dev"
image="$registry/$MAILPILOT_PROJECT_ID/mailpilot/mailpilot"
gcloud auth configure-docker "$registry"
metadata=$(mktemp)
docker buildx build --platform linux/amd64 --tag "$image:$(git rev-parse HEAD)" \
  --push --metadata-file "$metadata" .
export MAILPILOT_IMAGE="$image@$(jq -er '."containerimage.digest"' "$metadata")"
rm -f "$metadata"
```

部署需要这些非密钥配置。Secret Manager 版本号为正整数。模型名称是已经批准使用的 LiteLLM 供应商模型。

| 设置 | 值 |
| --- | --- |
| `MAILPILOT_ALLOWED_USER_IDS`、`MAILPILOT_ALLOWED_CHAT_IDS` | 正整数 JSON 数组，如 `[1001]`。 |
| `MAILPILOT_TELEGRAM_BOT_ID` | Bot 数字 ID。 |
| `MAILPILOT_EMAIL_PROVIDER` | 部署默认 `gmail`。Outlook 部署可显式设为 `outlook`。 |
| `MAILPILOT_GMAIL_OAUTH_VERSION` | Gmail 桌面 OAuth 客户端的 secret 数字版本。 |
| `MAILPILOT_OUTLOOK_TENANT_ID`、`MAILPILOT_OUTLOOK_CLIENT_ID` | 显式使用 Outlook 时的 Entra 租户和应用 UUID。 |
| `MAILPILOT_MODEL_PROVIDER`、`MAILPILOT_MODEL_NAME` | 供应商为 `openai`、`gemini` 或 `anthropic`。 |
| `MAILPILOT_WEBHOOK_VERSION`、`MAILPILOT_BOT_VERSION`、`MAILPILOT_MODEL_VERSION` | 对应应用 secret 的数字版本。 |
| `MAILPILOT_WORK_BUDGET_SECONDS`、`MAILPILOT_HARD_TIMEOUT_SECONDS` | 默认 240、600 秒。脚本要求最少预留 30 秒交接，硬超时上限 900 秒。 |

导出设置后执行 `bash infra/deploy.sh`。部署公共 Ingress、私有 Worker 与一次性 Job，并创建或更新三条 Scheduler 计划。Scheduler 会按配置启用，可在调试期间暂停。Ingress 与 Worker 支持缩零。凭据通过 ADC 和 Secret Manager 读取。

### 注册 Telegram webhook

使用 `gcloud run services describe mailpilot-ingress --region "$MAILPILOT_REGION" --project "$MAILPILOT_PROJECT_ID" --format="value(status.url)"` 获取 Ingress 默认地址。设置 `MAILPILOT_WEBHOOK_URL` 为该地址加 `/telegram/webhook`，运行以下代码。操作员账号需有访问 Bot 和 webhook secret 指定版本的权限。

```bash
uv run --locked python - <<'PY'
import os
import subprocess

import httpx

project = os.environ["MAILPILOT_PROJECT_ID"]

def secret(name, version):
    return subprocess.check_output(
        ["gcloud", f"--project={project}", "secrets", "versions", "access",
         os.environ[version], f"--secret=mailpilot-{name}"],
        text=True, stderr=subprocess.DEVNULL,
    ).strip()

try:
    token = secret("telegram-bot", "MAILPILOT_BOT_VERSION")
    payload = {
        "url": os.environ["MAILPILOT_WEBHOOK_URL"],
        "secret_token": secret("telegram-webhook", "MAILPILOT_WEBHOOK_VERSION"),
        "allowed_updates": ["message", "callback_query"],
    }
    with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
        origin = f"https://api.telegram.org/bot{token}"
        current = client.get(f"{origin}/getWebhookInfo").raise_for_status().json()["result"]["url"]
        if current not in ("", payload["url"]):
            raise SystemExit("Bot 已在其他地址注册 webhook。")
        response = client.post(f"{origin}/setWebhook", json=payload).raise_for_status()
        if response.json().get("ok") is not True:
            raise RuntimeError
        result = client.get(f"{origin}/getWebhookInfo").raise_for_status().json()["result"]
        if result["url"] != payload["url"]:
            raise RuntimeError
except Exception:
    raise SystemExit("Telegram webhook 注册失败。") from None
print("Telegram webhook 已注册。")
PY
```

## CI 和 GitHub 部署

1. 在 GitHub 创建 `mailpilot-deploy` 环境，将部署分支限定为 `main`。按需要设置审核人。
2. 将 `MAILPILOT_GITHUB_REPOSITORY` 设为 `OWNER/REPO`，另导出数字 `MAILPILOT_GITHUB_REPOSITORY_ID` 和 `MAILPILOT_GITHUB_OWNER_ID`。运行 `bash infra/wif.sh`。WIF 验证仓库与所有者数字 ID、环境、分支和部署 workflow。
3. 在 GitHub 环境变量中填写 `GCP_PROJECT`、`GCP_REGION`、`GCP_WIF_PROVIDER` 和 `GCP_DEPLOY_SERVICE_ACCOUNT`。后两项使用脚本输出的资源名。
4. 从 `main` 手动运行 **Deploy MailPilot**。CI 运行无真实凭据的入口和调度测试、Lint、类型检查与部署干运行。本地完整测试目录按仓库规则保存在工作区。部署借助 GitHub OIDC 获取短期身份，上传带提交 SHA 标签的镜像，再按镜像摘要更新三个 Cloud Run 入口。

保留发布的完整镜像摘要和 Cloud Run revision。GitHub 只需配置上述变量；应用密钥留在 Secret Manager。 [Google WIF](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines) · [GitHub OIDC 声明](https://docs.github.com/en/actions/reference/security/oidc)

## 验证与恢复

- 检查两项服务的 `min=0` 和 revision 最小实例数。Ingress 的 `/healthz` 可公开访问，匿名 Worker 请求应被 Cloud Run 拒绝。使用授权测试身份进一步验证 Scheduler 和 Tasks token 只能访问各自路由，错误 audience 被拒绝。
- 检查 Scheduler 中两条 `Europe/Zurich` 计划与一条每 15 分钟执行的 UTC 恢复计划。自然计划运行后检查 `digest_job_intents`、`digest_batches`、Job 日志和测试 Telegram 聊天。重试依据 Scheduler 原计划时间；临时手工触发应使用符合路由时段的计划时间。
- 在独立测试私聊发送 `/digest` 和 `/detail 1` 核对交互。用已登记批次 ID 重复执行 Job，验证只有一次已确认投递。Job 日志记录批次短 ID、状态、耗时、账户数、同步页数、邮件数、模型请求、同步与模型重试次数和待补收账户数。参考负载使用 3 个账户，合计 30 封合成邮件。既有 Outlook 样本每账户 8 个文件夹，Gmail 使用每账户一个邮箱级游标。
- `retry` 的批次由 UTC 恢复计划重新启动。长时间没有执行登记或超过执行租约的启动意图也会重提。对于 `unknown`，先核对聊天和投递记录；确认后由操作员记录结果，用户明确要求时再按补发流程处理。
- 在已授权测试项目重复执行 bootstrap、WIF 和完整部署，核对资源名、IAM、OIDC、时区、索引和同一镜像摘要均收敛。记录结果并核对部署日志和产物仅包含运行所需的非密钥信息。

## 轮换与回滚

- 模型密钥或 Bot Token 轮换时，在 Secret Manager 添加新版本并更新对应版本号，以同一镜像摘要重新执行完整部署。Bot Token 更新后重新注册 webhook。确认实际交互及活动执行后停用旧版本。
- Webhook 密钥轮换时，为新版本重新部署 Ingress 并立即重新注册 webhook。切换间隙中的更新会收到验证失败并由 Telegram 重试。保持旧版本可用直至新配置验证通过。
- Gmail 刷新凭据由操作员使用 CLI 的 `rotate`，参数同授权并增加 `--account ACCOUNT_ID`。撤销 MailPilot 访问使用 `revoke`。需要重授权时运行原授权命令并指定同一个 `--account`。换 OAuth 客户端时，先以新客户端重授权账户，再更新 `MAILPILOT_GMAIL_OAUTH_VERSION` 并部署。
- 镜像回滚时，先暂停两条摘要计划和恢复计划，并按需暂停交互队列。使用上一已知正常版本的完整镜像摘要执行 `MAILPILOT_IMAGE=... bash infra/deploy.sh --release-only`，检查三个入口与数据契约兼容，再恢复计划及队列。若回滚涉及环境变量或 secret 引用，使用上一版配置执行完整部署。保留 Firestore 中的游标、任务、投递和待核对记录。

## 紧急邮件通知与翻译

更新 MailPilot 镜像后，Telegram 可用 `/translate 1 3`、单封「翻译」和「本页全部翻译」。工作者只在请求内回读邮件并生成译文。启用前确认所选模型允许处理测试邮件正文，及 Telegram 端的内容保留符合项目要求。

紧急提醒默认关闭。启用步骤如下。

1. 在 OAuth 应用所在 GCP 项目设置 `MAILPILOT_URGENT_ENABLED=true` 并运行 `infra/bootstrap.sh`。脚本创建 `mailpilot-sync`、授予 Ingress 和 Worker 入队权限，并应用 Firestore 索引与 TTL。等待 `emails` 集合中含 `urgent_check_at` 的复合索引就绪。
2. 只使用补扫时保持 `MAILPILOT_EMAIL_PUSH=false`。若启用推送，将 `MAILPILOT_INGRESS_URL` 设为公开 Ingress 的稳定 HTTPS origin，并选择已配置的邮箱渠道。
   - **Gmail** 先在同项目运行 `infra/pubsub.sh`。执行者需要在专用的 `mailpilot-pubsub` 服务账号上具备 `iam.serviceAccounts.actAs`。脚本创建或更新 topic `mailpilot-gmail` 和认证推送订阅 `mailpilot-gmail-push`，赋予 Gmail 系统身份 topic 发布权限，并授权 Pub/Sub 服务代理签发该专用身份的 OIDC token。推送端点和 token audience 均为 `$MAILPILOT_INGRESS_URL/email/gmail`。
   - **Outlook** 在 bootstrap 创建的 `mailpilot-graph-client-state` 中添加由密码学安全随机源生成的至少 32 字节的文本密钥。将 `MAILPILOT_GRAPH_STATE_VERSION` 设为对应的版本号。仅 Worker 需要读取该密钥。消息和生命周期回调都使用 `$MAILPILOT_INGRESS_URL/email/graph`。
3. 设置 `MAILPILOT_EMAIL_PUSH=true` 后运行完整的 `infra/deploy.sh` 以应用新环境变量。每 15 分钟的恢复计划登记各活动账户的独立同步工作，按需创建或续期订阅。Gmail 每日续订 watch，Outlook 在离到期不足 48 小时时续期。
4. 使用专用测试账户发送触发紧急规则的合成邮件，检查 Worker 的 `email_wake` 和 `email_urgent_result` 事件、Telegram 投递及摘要中的「已提醒」。按渠道分别验证。在测试环境暂时停用推送，确认下一轮 15 分钟补扫依旧投递。真实端到端测量与发布验收见 [TASK-06-01](tasks/06-release/task.md)。

`subscriptions` 的 `status`、`expires_at` 和 `last_error` 可用于核对续期和补扫降级情况。Graph 密钥轮换时先部署新版本引用，等待 Worker 用新密钥重建订阅，再停用旧版本。若只需关闭推送，使用 `MAILPILOT_EMAIL_PUSH=false` 重跑完整部署，巡检仍会运行。若要关闭整个紧急功能，使用 `MAILPILOT_URGENT_ENABLED=false` 重跑完整部署，并在专用测试环境核对队列和订阅状态。Gmail watch 与 Graph 订阅会在各自的到期时间停止，Cloud Pub/Sub 订阅可由操作员清理。

[Gmail 推送配置](https://developers.google.com/workspace/gmail/api/guides/push) · [Pub/Sub 推送认证](https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions) · [Graph 生命周期通知](https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events)

## 发布验收与监控

1. 核对 [发布清单](RELEASE_CHECKLIST.md) 和候选提交。无凭据 CI 会启动 Firestore Emulator，使用合成账户、邮件及模拟服务验证核心恢复和去重。GitHub 部署只更新三个 Cloud Run 入口的镜像。Scheduler、Pub/Sub、功能开关和凭据版本的变化应按本手册另行应用完整配置。
2. 保存上一正常镜像的完整 Artifact Registry 引用及配置版本。发布后，在测试项目核对版本、流量 revision、Job 和 Scheduler。以下工作区端到端测试只读取资源配置与状态。生产观测以及真实邮箱和 Telegram 行为通过发布清单留证。

```bash
export MAILPILOT_E2E=1
export MAILPILOT_E2E_PROJECT=TEST_PROJECT
export MAILPILOT_E2E_IMAGE='REGION-docker.pkg.dev/TEST_PROJECT/mailpilot/mailpilot@sha256:RELEASE_DIGEST'
uv run --locked --extra dev python -m pytest tests/e2e/test_staging.py -q
```

如部署在其他区域，另设 `MAILPILOT_E2E_REGION` 和 `MAILPILOT_E2E_SCHEDULER_REGION`。使用可读取测试项目 Run 和 Scheduler 元数据的 gcloud 账号。

| 症状或指标 | 核对与处置 |
| --- | --- |
| 调度未送达或 Job 超时 | 将 Scheduler 计划时刻、Worker 请求、Job 执行名和摘要批次短 ID 关联。检查 Job 退出状态、`seconds`、`pending_accounts` 与恢复计划，保留原批次重试。 |
| 续期失败、延迟或漏通知 | 关注 `email_subscription_failure`、续期剩余时间、游标年龄和同步队列积压。将 `email_wake` 和 `email_urgent_result` 按任务短 ID 关联，核对通知与补扫渠道。恢复订阅后验证从原游标继续。 |
| 仍无法确认 Telegram 投递 | 按批次与分段核对测试聊天是否收到，保留该条的 `UNKNOWN` 及核对结果。需要补发时先由用户确认，再按关联的单次补发处理。 |
| Ingress 活性探针异常 | 获取当前 Ingress 默认地址并核对 `/healthz`，同时检查正确签名的 Telegram 请求及 Worker 任务是否正常。把探针错误、revision 和时间记入验收记录。 |
| 需回滚 | 记录三个当前入口及上一正常摘要，按本页暂停计划和所需队列，回滚镜像与相应配置，验证活性、Worker IAM、Webhook 和订阅后恢复计划及队列。 |

Cloud Logging 查询聚焦应用事件、状态码、时间和短 ID。关注 Job 失败、连续续期失败、队列积压和待核对投递。建立告警时使用批准的运维收件渠道，仅附元数据；截图和工件也保留脱敏结果。
