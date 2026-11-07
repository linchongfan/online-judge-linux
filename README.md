# 新保研在线判题系统（Linux 版）

基于 Vue 3 + FastAPI + MySQL + Redis 的在线判题系统，运行于 Linux。前端认证、题库、做题、提交、个人中心、竞赛及五类管理页面均已接入真实 API；后端提供测试数据、异步判题、竞赛与管理接口；判题使用 isolate + cgroup v2 的独立沙箱，支持 **C++ / C / Java / Python3**。

> 本仓库是仅面向 Linux 的发行版，已移除 Windows 沙箱后端、PowerShell 脚本与 Windows 专用文档。需要 Windows 开发环境请使用完整版仓库。

## 文档导航

- **[Docker 容器化部署](deploy/DOCKER_DEPLOYMENT.md)**：当前线上方案，前后端/消费者/维护/判题全部容器化，复用 1Panel 的 MySQL 与 Redis。
- **[Linux 单机自动化部署方案](deploy/LINUX_AUTOMATED_DEPLOYMENT.md)**：systemd + 宿主机 Nginx 的可选方案，含证书、备份、回切与排障。
- **[生产部署与运维](deploy/README.md)**：上线顺序、安全边界、备份和失陷处置。
- **[系统设计与安全防御计划](计划.md)**：功能范围、数据设计、威胁模型与验收标准。

## 运行环境

- 业务后端：Python **3.12**
- 前端：Bun **1.4.2**、Vue 3、TypeScript、Vite
- 生产数据与队列：MySQL **8.4.13**、Redis **8.10.3**、Celery
- 用户代码工具链：
  - C：GCC 13，`-std=c99`
  - C++：G++ 13，`-std=c++11 -O2`
  - Java：OpenJDK 17，`--release 8` 保持 Java 8 语言/API 目标，入口 `public class Main`
  - Python：独立 Python 3.12（`-I -B`），与业务后端进程隔离
- 界面语言名称统一为 **C++ / C / Java / Python3**。

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m alembic upgrade head
.venv/bin/python scripts/seed.py
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

未配置 `.env` 时后端使用 `data/dev.db`（SQLite），验证码写入 `data/mail_outbox/`。正式部署应复制 `.env.example` 为 `.env`，更换全部密钥并配置 MySQL、Redis、SMTP。

- API 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`/api/v1/health/live`、`/api/v1/health/ready`
- 开发种子账号：`admin / Admin12345`、`demo / Demo12345`（生产禁用）

生产管理员用交互式 CLI 创建：

```bash
.venv/bin/python scripts/create_admin.py <username> <email>
```

前端：

```bash
bun install
bun run dev      # http://127.0.0.1:5173，/api 代理到 8000
bun run build
```

## 判题架构

```text
浏览器 → Nginx → FastAPI ──► MySQL
                    │      └► Redis（db0 结果队列 / db1 Celery）
                    │
                    └─ Celery 任务队列 ─► judge-worker（isolate 沙箱）
                                              │
                    签名结果（HMAC-SHA256）◄──┘
                    │
              result-consumer → 幂等落库 → 前端轮询
```

判题 Worker 不连接数据库，只消费队列并回传签名结果：

- 投递成功仅表示进入队列；Worker 真正开始执行时发布 `started` 事件，后端据此进入 `JUDGING` 并开启执行租约。
- 队列超时（默认 600s）与租约过期由维护进程巡检，按 `judge_task_max_attempts` 重试，超过后标记 `SE`。
- 结果消费者使用 `BRPOPLPUSH`，落库成功才确认；签名非法或状态冲突进入死信队列 `judge:results:dead`。
- 隐藏测试点不回传用户程序 stderr；仅公开样例返回脱敏诊断。

### isolate 沙箱

`JUDGE_SANDBOX_BACKEND=isolate` 时使用 `judge/linux_isolate.py`：

- 每次运行创建独立 box（mount namespace + network namespace + cgroup v2）；
- 限制 CPU/墙钟、硬内存、进程数、文件大小、任务目录块与 inode；
- 用户程序以独立低权 UID 运行，根文件系统只读，仅任务目录可写；
- 默认无网络设备，用户程序无法访问本机 Redis/MySQL/FastAPI 或宿主机配置；
- 硬限制触发后由后端 `execution_info` 回报 TLE/MLE，不再误报 RE。

`local` 后端只用于无害本地测试，生产策略会拒绝它。

### 启动判题链路（开发）

```bash
redis-server --bind 127.0.0.1 --port 6379          # 或使用 1Panel 实例
.venv/bin/python scripts/run_judge_worker.py       # Celery solo worker + 心跳
.venv/bin/python scripts/run_consumer.py           # 结果消费者
.venv/bin/python scripts/run_judge_maintenance.py  # Outbox 补偿 + 租约巡检
```

端到端验收（四语言 AC/WA/CE 等）：

```bash
.venv/bin/python scripts/e2e_judge_acceptance.py \
  --base-url http://127.0.0.1:8000/api/v1 \
  --account admin@123321 --password admin@123321 --problem-id 1000
```

判题机自身的隔离验收（需在授权 Linux 判题机执行）：

```bash
JUDGE_VM=1 .venv/bin/python -m pytest -m linux_judge_vm
```

## 生产部署

线上采用 **Docker 容器化**：前端 Nginx 容器开放 `9898`，FastAPI、结果消费者、维护进程、备份与判题 Worker 各自独立容器；MySQL 8.4.13、Redis 8.10.3 复用服务器已有实例，均只监听本机。

```bash
cd /opt/xinbaoyan-src && git pull --ff-only
cd /opt/xinbaoyan-docker
docker build -f backend.Dockerfile  -t xinbaoyan-backend:local  /opt/xinbaoyan-src
docker build -f frontend.Dockerfile -t xinbaoyan-frontend:local /opt/xinbaoyan-src
docker build -f judge.Dockerfile    -t xinbaoyan-judge:local    /opt/xinbaoyan-src
./patch-backend-image.sh && ./patch-judge-image.sh
docker compose up -d --force-recreate api result-consumer maintenance backup frontend
docker compose --profile judge up -d --force-recreate judge-worker
```

完整步骤、cgroup 前置、补丁说明、备份与回滚见 **[Docker 容器化部署](deploy/DOCKER_DEPLOYMENT.md)**。不引入容器编排时，可用 `deploy/linux/` 与 `deploy/judge-linux/` 的 systemd 方案，见 **[Linux 单机自动化部署方案](deploy/LINUX_AUTOMATED_DEPLOYMENT.md)**。

首次部署会自动创建两个初始账号（幂等）：

| 角色 | 账号 | 密码 |
| --- | --- | --- |
| 管理员 | `admin@123321` | `admin@123321` |
| 测试用户 | `test@123456` | `test@123456` |

首次登录后请立即修改密码。容器方案用 `docker compose run --rm migrate` 建号；systemd 方案可用 `CREATE_DEFAULT_USERS=0` 禁用自动创建。

## C 语言培养方案题库

仓库内置 10 个知识模块、共 100 道 C 语言练习题（题号 2000-2099），按模块由易到难排布：

| 题号 | 知识模块 |
| --- | --- |
| 2000-2009 | 程序设计入门 |
| 2010-2019 | 数据与表达式 |
| 2020-2029 | 输入与输出 |
| 2030-2039 | 选择结构 |
| 2040-2049 | 循环结构 |
| 2050-2059 | 数组与字符串 |
| 2060-2069 | 函数 |
| 2070-2079 | 指针 |
| 2080-2089 | 构造类型 |
| 2090-2099 | 文件与综合应用 |

每题包含题面、输入输出说明、数据范围、1 个公开样例、3 个隐藏测试点和 C 语言参考解。标准答案由参考解编译运行生成并通过线上判题验证（C 语言 AC）。

导入到数据库（幂等，已存在的题号会跳过）：

```bash
.venv/bin/python scripts/seed_c_curriculum.py
```

数据文件为 `scripts/curriculum_problems.json`，修改题目后重新导入只会新增缺失的题号；如要覆盖已有题目，先删除对应 `problem` 记录或改用新的题号区间。

## 配置与持久化

| 配置 | 用途 |
| --- | --- |
| `SECRET_KEY`、`JUDGE_RESULT_SECRET` | Web 会话与判题结果签名，生产必须独立随机 |
| `DATABASE_URL` | 开发默认 SQLite；生产使用 MySQL 8.4.13 |
| `REDIS_URL` | 限流、判题结果队列（0 号库） |
| `CELERY_BROKER_URL`、`CELERY_RESULT_BACKEND` | Celery 队列与结果后端（1、2 号库） |
| `REFRESH_COOKIE_SECURE`、`CORS_ORIGINS` | HTTPS Cookie 与受控来源 |
| `SMTP_*` | 找回密码邮件；生产未配置时找回流程报错 |
| `STORAGE_ROOT` | 测试数据版本等持久化文件，必须纳入备份 |
| `JUDGE_WORK_ROOT`、`JUDGE_META_ROOT`、`JUDGE_LINUX_ROOTFS` | 判题任务目录、元数据与只读 rootfs |

生产配置位于 `/etc/xinbaoyan/`（业务）与 `/etc/xinbaoyan-judge/`（判题）；持久化数据位于 `/var/lib/xinbaoyan/data`、`/var/lib/xinbaoyan-judge/`、`/var/backups/xinbaoyan`。

## 测试与检查

```bash
.venv/bin/python -m pytest        # 后端：API、权限、判题可靠性、isolate 参数与元数据
.venv/bin/python -m ruff check app judge tests
.venv/bin/python -m mypy app judge
bun run test                      # 前端单元测试
bun run build
```

覆盖范围：认证与角色权限、Refresh Token 轮换、题目发布、测试数据 ZIP 安全校验、隐藏数据边界、提交幂等、竞赛时间/报名约束、ACM 罚时、结果回调去重、默认账号幂等创建、判题 TLE/MLE 元数据映射、isolate 参数与路径映射。

## 安全要点

- 生产必须使用 `isolate` 后端；普通 subprocess、普通 Docker 容器都不构成安全边界。
- 判题 Worker 不持有 MySQL、SMTP 或 Web `SECRET_KEY`；用户程序无法继承 Worker 的 Redis 凭据。
- 隐藏测试点、标准答案、内部路径与凭据不得出现在接口、日志或错误信息里。
- 重复提交、Worker 重试和重复回调不会重复计数；成绩可由 `submission` 明细重建。
- 疑似沙箱突破时按整机可能受影响处置：停止接单、网络隔离、保留证据、从可信镜像重建，而不是只重启进程。
