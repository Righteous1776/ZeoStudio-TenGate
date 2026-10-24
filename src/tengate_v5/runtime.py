from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .config import TenGateConfig, resolve_secrets
from .github import GitHubBinding, GitHubWebhookAdapter
from .github_bridge import GitHubTaskBridge
from .github_inbox import GitHubEventInbox
from .identity import HmacSealAuthority
from .kernel import GateKernel


@dataclass(slots=True)
class RuntimeStack:
    config: TenGateConfig
    authority: HmacSealAuthority
    adapter: GitHubWebhookAdapter
    inbox: GitHubEventInbox
    kernel: GateKernel
    bridge: GitHubTaskBridge

    @classmethod
    def open(cls, config: TenGateConfig, environ: Mapping[str, str]) -> RuntimeStack:
        secrets = resolve_secrets(config, environ)
        authority = HmacSealAuthority(secrets.seal_key, issuer=config.identity.issuer)
        bindings = {
            (binding.repository, binding.login): GitHubBinding(
                roles=binding.roles,
                lane=binding.lane,
                event_rules=binding.event_rules,
                seal_ttl_seconds=binding.seal_ttl_seconds,
            )
            for binding in config.github.bindings
        }
        adapter = GitHubWebhookAdapter(
            secrets.webhook_secret,
            authority,
            bindings,
            max_body_bytes=config.github.max_body_bytes,
        )
        kernel = GateKernel(config.database, authority)
        try:
            inbox = GitHubEventInbox(
                config.database,
                authority,
                max_attempts=config.github.max_attempts,
            )
        except Exception:
            kernel.close()
            raise
        bridge = GitHubTaskBridge(
            adapter,
            inbox,
            kernel,
            task_prefix=config.bridge.task_prefix,
            routes=config.bridge.routes,
        )
        return cls(config, authority, adapter, inbox, kernel, bridge)

    def close(self) -> None:
        self.inbox.close()
        self.kernel.close()

    def __enter__(self) -> RuntimeStack:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

