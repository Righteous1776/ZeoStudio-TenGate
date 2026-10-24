# ZeoStudio TenGate V5

ZeoStudio 自主开发的任务治理与多智能体协作内核，提供：

- 可校验的任务状态流转与乐观并发控制；
- 持久 Inbox、幂等投递、回执与重启恢复；
- 外部身份验证边界、Owner/Governor/Lane 权限门禁；
- 检查点、不可变证据引用与哈希审计链；
- 路径 Lease、能力发现和明确降级；
- GitHub Webhook 原始载荷验签、仓库身份绑定与事件白名单；
- 持久 GitHub 事件收件箱、处理租约、失败重试与崩溃恢复；
- GitHub 资源事件到 TenGate Task 的策略复核与幂等桥接；
- 本机默认监听的 HTTP Webhook 服务、健康/就绪检查与请求大小门禁；
- 自动事件处理 Worker、可观测运行计数与有界安全停机；
- 在线一致性备份、HMAC 签名清单、只读数据库检查与隔离恢复演练；
- 可重复执行的可靠性测试与 Relay Benchmark。

当前版本使用 Python 标准库运行，不要求额外运行时依赖。

## 安全报告

安全问题请勿创建公开 Issue；报告方式和支持范围见 `SECURITY.md`。

配置文件只保存环境变量名称，不保存 Webhook Secret、Actor Seal 密钥或备份清单密钥。示例见
`examples/tengate.config.json`。

## 快速开始

```bash
PYTHONPATH=src python examples/quickstart.py
```

## 命令行

```bash
PYTHONPATH=src python -m tengate_v5.cli --config examples/tengate.config.json check-config
```

`check-config` 和 `check-database` 不读取密钥；`backup` 与 `rehearse-restore` 只读取独立的备份清单
密钥；运行时命令按需读取 Actor Seal 和 Webhook 密钥：

```bash
PYTHONPATH=src python -m tengate_v5.cli --config examples/tengate.config.json init
PYTHONPATH=src python -m tengate_v5.cli --config examples/tengate.config.json status
PYTHONPATH=src python -m tengate_v5.cli --config examples/tengate.config.json serve
PYTHONPATH=src python -m tengate_v5.cli --config examples/tengate.config.json check-database
PYTHONPATH=src python -m tengate_v5.cli --config examples/tengate.config.json backup --output tengate-v5.backup.db
PYTHONPATH=src python -m tengate_v5.cli --config examples/tengate.config.json rehearse-restore \
  --backup tengate-v5.backup.db --manifest tengate-v5.backup.db.manifest.json
```

HTTP 服务默认同时启动后台事件 Worker，提供 `GET /healthz`、`GET /readyz` 和
`POST /github/webhook`；使用 `serve --no-worker` 可运行仅接收节点。生产部署应在受信任的 TLS
反向代理后运行，并限制来源网络；服务本身仍会对 GitHub 原始请求体执行 HMAC-SHA256 验签。

## 验证

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python tools/relay_bench.py
PYTHONPATH=src python tools/github_inbox_bench.py
PYTHONPATH=src python tools/service_bench.py
PYTHONPATH=src python tools/multiprocess_soak.py
PYTHONPATH=src python tools/failure_injection.py
PYTHONPATH=src python tools/release_validate.py --full
```
