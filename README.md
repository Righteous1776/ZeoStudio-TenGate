# ZeoStudio TenGate

### Stop prompting agents to cooperate. Give them a system.

**AI Agent 的时代，不该靠聊天记录维持秩序。**

把混乱变成制度。把遗忘变成记忆。把“我做完了”变成可以验证的证据。

ZeoStudio 不是又一份随手拼贴的 Prompt。它将治理权限、任务持久化、Agent 间通信、提案审核、长期记忆、跨会话交接和可执行验收，组织为一套可移植的多 Agent 工作体系。

## ZeoStudio TenGate V4.0

今天，**ZeoStudio TenGate V4.0** 正式完成构建。

三套架构，一套思想。

- **A · Unified / 统一协作** — 将协作、治理和持续任务汇入统一入口。
- **B · Exchange & Continuity / 连续执行** — 聚焦 Agent 通信、任务状态和断点续接。
- **C · Governed Proposal / 治理集成** — 聚焦权限边界、隔离提案、审查门禁和可追溯交付。

从任务持久化到 Agent 治理，从编号记忆库到跨会话接管——我们尝试回答一个更严肃的问题：

> 如果 AI 真正开始协作，它们应该遵守什么制度？

## Built for real agent work

- Owner / Governor / Lane 分工与权限控制
- 双通道消息与提案治理体系
- Task Contract、冲突处理、审核和回滚
- M00–M09 编号记忆库与经过审核的会话整理
- 多平台部署与能力降级规范
- 任务交接、验证脚本、证据管理和验收流程

## Official distribution

V4.0 通过 **signed offline challenge archive** 发行。请从 GitHub Releases 下载唯一官方附件 `ZeoStudio_PUBLIC_V4.0.zip`，解压至干净目录，并先验证签名与文件摘要。

```bash
python -m pip install -r requirements.txt
python VERIFY_RELEASE.py --directory . \
  --pinned-fingerprint 3d6d821fea7656004a147873d6e79f4c6494eea18758200c924ef742297ce3c3
```

发布包 SHA-256：

```text
a4831e700c30a19631e0d82f12be8169e903683558e6184a9589ea8a2e4b9c99
```

签名、公钥指纹与认证加密用于保护发行工件的真实性与完整性；它们不代表独立密码学审计，也不保证在所有硬件上具有相同计算耗时。

## License

ZeoStudio 原创材料以 [Apache License 2.0](LICENSE) 发布。第三方材料如带有单独许可或署名要求，以对应文件中的声明为准。

## Author

**ZeoStudio · 久野零**

> 协作不是口号，治理才是底座。
