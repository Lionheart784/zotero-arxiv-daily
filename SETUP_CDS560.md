# 部署说明（CDS560 版）

这个 fork 在原版 zotero-arxiv-daily 的基础上加了两项功能：

1. **按期刊 / 会议级别筛选 arXiv 推荐**：使用 [CCF 推荐目录](https://ccf.atom.im/)（2026 版）和 [SCImago SJR](https://www.scimagojr.com/journalrank.php) 分区，低于 **CCF-C** 且低于 **SJR Q3** 的论文会被剔除。还没发表、两份名单都查不到的预印本会保留，并标注为"未评级"。
2. **「已读/CDS560」分类的每日新增被引提醒**：数据来自 Semantic Scholar，取不到时改用 OpenAlex。施引文献同样按上面的门槛过滤。Google Scholar 没有开放 API，所以不用它。

两项功能的结果合并在同一封每日邮件里。默认每天香港时间 06:00 运行，对应 UTC 22:00，可在 `.github/workflows/main.yml` 里修改。

## 一次性设置

### 1. 启用 Actions

打开仓库的 **Actions** 标签页，点 "I understand my workflows, go ahead and enable them"。

fork 出来的仓库默认关闭定时任务，还需要进入左侧的 **Send emails daily**，点 **Enable workflow**。

### 2. 填写 Secrets

进入 **Settings → Secrets and variables → Actions → New repository secret**，逐个添加：

| 名称 | 内容 |
| --- | --- |
| `ZOTERO_ID` | Zotero 用户 ID，是一串数字，在 https://www.zotero.org/settings/security 页面能看到 |
| `ZOTERO_KEY` | 在同一页面新建 API key，勾选 *Allow library access* 即可，只读就够 |
| `SENDER` | 用来发信的邮箱，默认按 QQ 邮箱配置 |
| `SENDER_PASSWORD` | 发信邮箱的 **SMTP 授权码**，不是登录密码 |
| `RECEIVER` | 收信邮箱 |
| `OPENAI_API_KEY` | 用于生成 TL;DR 的 LLM API key |
| `OPENAI_API_BASE` | LLM 接口地址，例如 `https://api.openai.com/v1` 或 `https://api.siliconflow.cn/v1` |

以下几项可选：

| 名称 | 类型 | 用途 |
| --- | --- | --- |
| `SMTP_SERVER` / `SMTP_PORT` | Variable | 发信邮箱不是 QQ 邮箱时填写，例如 163 邮箱填 `smtp.163.com` / `465` |
| `LLM_MODEL` | Variable | 模型名，默认 `gpt-4o-mini` |
| `S2_API_KEY` | Secret | Semantic Scholar 的免费 API key。没有也能用，但更容易被限流 |
| `OPENALEX_API_KEY` / `OPENALEX_MAILTO` | Secret | OpenAlex 备用数据源使用 |

### 3. 测试

在 Actions 页面手动运行 **Test**（调试模式，只取少量论文）或 **Send emails daily**，跑完后检查日志和邮箱。

## 需要知道的事

- **被引提醒第一天只建立基线**。第一次运行时，会把每篇文献已有的引用全部记录下来，不发提醒；从第二天起只提醒新增的引用。记录保存在 `state/citation_state.json`，由 workflow 自动提交回仓库。
- **评级数据会自动更新**。每次运行都会尝试下载最新的 CCF 页面和 SJR 表格。下载失败时（SJR 有 Cloudflare 防护，经常拦截服务器请求），就使用仓库里 `data/` 下的快照。
- **Zotero 需要先同步**。这个工具读取的是 zotero.org 上的云端数据，本地有改动的条目要先在 Zotero 客户端里点同步。
- **配置文件**是 `config/user_config.yaml`，arXiv 分类、门槛、追踪的分类路径都在里面修改。如果设置了 `CUSTOM_CONFIG` 变量，会优先使用该变量里的配置。
