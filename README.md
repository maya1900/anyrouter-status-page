# anyrouter status page

一个可迁移的最小状态页项目：

- 静态页面：`docs/`
- 探测脚本：`scripts/check_anyrouter.py`
- GitHub Actions 模板：`.github/workflows/status-check.yml`

## 功能

- 每次探测只保留一个状态来源
  - 优先直接运行最新版 `Claude CLI`
  - 如果机器上没有 `claude` 命令，再回退到 HTTP 探测
- `max_tokens=1`
- 记录：
  - 当前 HTTP status code
  - 是否成功吐出文本
  - 最近错误消息
  - 最近探测耗时
- 页面会额外标记数据是否过期
  - 超过 20 分钟未刷新，直接提示“当前页面不可信”
- 只保留最近 7 天，按小时聚合

## 本地运行

1. 复制配置文件：

   ```bash
   cp .env.example .env
   ```

2. 填入：

   - `ANYROUTER_API_BASE`
   - `ANYROUTER_API_KEY`
   - `ANYROUTER_MODEL`（推荐：`claude-opus-4-7[1m]`）

3. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

4. 执行探测：

   ```bash
   python scripts/check_anyrouter.py
   ```

5. 打开 `docs/index.html` 预览页面，或用任意静态服务器托管 `docs/`。

## 部署到新仓库

推荐把本目录内容提升到新仓库根目录，最终结构类似：

- `.github/workflows/status-check.yml`
- `docs/`
- `scripts/`
- `requirements.txt`

然后：

1. GitHub Pages 指向 `docs/`
2. 仓库 Secrets 配置：
   - `ANYROUTER_API_BASE`
   - `ANYROUTER_API_KEY`
   - `ANYROUTER_MODEL`
3. 启用 Actions
4. 使用 Cloudflare Worker 定时触发该 workflow 的 `repository_dispatch`
   - 当前线上配置为每 10 分钟触发一次

## 推荐触发方式

如果你真的想要接近“每 10 分钟一次”的效果，推荐只保留 Cloudflare Worker 作为自动触发器。

推荐做法：

- 外部定时器
  - 主用 `repository_dispatch`
  - Cloudflare Worker、Cron Job、或者你自己的服务器都行
- GitHub Actions
  - 只保留 `workflow_dispatch` 和 `repository_dispatch`
  - 不再启用 GitHub 自带 `schedule`

### 1. GitHub 侧

当前 workflow 已经支持：

- `workflow_dispatch`
  - 手动点按钮跑
- `repository_dispatch`
  - 外部程序通过 GitHub API 触发

### 2. 最小 curl 触发

仓库里已经带了一个脚本：

- `scripts/dispatch_repository_event.sh`

用法：

```bash
export GITHUB_TOKEN="ghp_xxx"
export GITHUB_OWNER="maya1900"
export GITHUB_REPO="anyrouter-status-page"

bash scripts/dispatch_repository_event.sh
```

这个脚本会向 GitHub 发一个：

- `event_type=status-check`

你需要的 token 权限至少要能触发仓库 workflow。

### 3. Cloudflare Worker 部署

仓库里带了一个最小 Worker 示例：

- `examples/cloudflare-worker.js`

部署步骤：

1. 在 Cloudflare Dashboard 打开 `Workers & Pages`
2. 创建一个新的 Worker
3. 把 `examples/cloudflare-worker.js` 的内容粘进去
4. 在 Worker 的 `Settings -> Variables` 里添加这些环境变量：

- `GITHUB_TOKEN`
- `GITHUB_OWNER`
- `GITHUB_REPO`
- `GITHUB_EVENT_TYPE`

建议值：

- `GITHUB_OWNER=maya1900`
- `GITHUB_REPO=anyrouter-status-page`
- `GITHUB_EVENT_TYPE=status-check`

5. 在 Worker 的 `Triggers` 里添加一个 Cron Trigger

推荐 Cron：

- `*/10 * * * *`

6. 保存并部署

Worker 每次跑的时候会调用 GitHub：

- `POST /repos/{owner}/{repo}/dispatches`

### 4. 如何测试 Worker

测试方式：

- 直接访问 Worker 的 URL
  - 会立即触发一次 `repository_dispatch`
- 去 GitHub Actions 页面确认是否出现新的 `repository_dispatch` 运行记录

### 5. 为什么不用外部程序去调 `workflow_dispatch`

`repository_dispatch` 更适合这种“外部系统定时踹一下仓库”的场景。

原因很简单：

- 语义更直接
- 请求体更短
- 不需要额外关心 `ref`
- 你已经在 workflow 里显式支持了 `status-check`

### 6. 不要和 GitHub schedule 同时开

不建议让 Cloudflare Worker 和 GitHub `schedule` 同时跑。

原因：

- 会产生重复 workflow run
- 会更频繁地产生 `chore(status)` 提交
- 会让排队和触发来源更难排查

当前仓库已经去掉了 GitHub `schedule`，默认只保留：

- `workflow_dispatch`
- `repository_dispatch`

## 说明

- GitHub 只会识别仓库根目录下的 `.github/workflows/`。当前目录里的 workflow 是迁移模板，默认按“迁移后位于仓库根目录”来写。
- GitHub Actions 自带定时任务并不总是准点，所以这个项目把“过期数据”单独标红，避免旧数据冒充实时状态。
- README 里提到的外部定时触发，推荐走 `repository_dispatch`，不要再拿 `workflow_dispatch` 当主要自动化入口。
- 探测失败时脚本仍会写入状态文件；只有缺少配置或写文件失败时才会退出非零。

## Opus 4.7[1m] 兼容说明

旧脚本最大的问题不是“请求太旧”，而是“页面绿了不代表 CLI 真能用”。

现在仓库的策略已经收敛成一件事：

- 只监控 `Claude CLI` 真正关心的可用性
- 能直接跑 `Claude CLI` 的环境，就不再手搓 payload 猜请求体
- 不再保留额外对照探针，避免把页面复杂化
- 如果页面长时间没更新，直接把“监控本身失真”暴露出来

这样页面表达就很直接：

- 绿：CLI 现在能用
- 红：CLI 现在不能用

## GitHub Actions 里的探测方式

workflow 现在会先安装最新版 `Claude CLI`，然后由 `scripts/check_anyrouter.py` 优先调用真实 CLI 做探测。

这样做的目的很简单：

- 让状态页尽量跟“最新版 CLI 的真实可用性”一致
- 避免因为 CLI 版本升级导致 payload 变化，页面又开始误报

## 🚩 友情链接

感谢 **LinuxDo** 社区的支持！

[![LinuxDo](https://img.shields.io/badge/社区-LinuxDo-blue?style=for-the-badge)](https://linux.do/)

## 致敬

- [lsdefine/GenericAgent](https://github.com/lsdefine/GenericAgent)
  - 本项目中模拟 Claude Code CLI 发送请求的方式参考了这个项目。
