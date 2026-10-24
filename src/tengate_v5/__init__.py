"""ZeoStudio TenGate V5 governance and coordination kernel."""

from .kernel import GateKernel
from .config import BackupConfig, TenGateConfig, load_config
from .github import GitHubBinding, GitHubWebhookAdapter, VerifiedGitHubEvent
from .github_bridge import BridgeResult, GitHubTaskBridge
from .github_inbox import DeliveryState, DeliveryView, GitHubEventInbox
from .identity import HmacSealAuthority
from .models import ActorSeal, InboxParcel, TaskState
from .runtime import RuntimeStack
from .server import WebhookApplication, create_server
from .service import BridgeWorker, WorkerSnapshot, WorkerState
from .storage import (
    BackupManifest, DatabaseReport, RestoreRehearsal, SignedBackupResult,
    create_backup, create_signed_backup, inspect_database, rehearse_restore, verify_backup_manifest,
)

__all__ = [
    "ActorSeal", "BridgeResult", "DeliveryState", "DeliveryView", "GateKernel",
    "GitHubBinding", "GitHubEventInbox", "GitHubTaskBridge", "GitHubWebhookAdapter",
    "BackupConfig", "BackupManifest", "BridgeWorker", "DatabaseReport", "HmacSealAuthority",
    "InboxParcel", "RestoreRehearsal", "RuntimeStack", "SignedBackupResult", "TaskState", "TenGateConfig",
    "VerifiedGitHubEvent", "WebhookApplication", "WorkerSnapshot", "WorkerState", "create_server",
    "create_backup", "create_signed_backup", "inspect_database", "load_config", "rehearse_restore",
    "verify_backup_manifest",
]
__version__ = "5.0.0"

