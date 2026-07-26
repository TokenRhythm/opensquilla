"""Localized, server-generated notices shared by chat channels.

The locale is a Gateway-wide operator preference stored in
``control_ui.default_locale``. It deliberately does not depend on an inbound
provider locale, request headers, or authorization state: wording can vary,
while admission and approval decisions must not.
"""

from __future__ import annotations

from typing import Any, Literal

ChannelSystemMessageKey = Literal[
    "pairing_required",
    "pairing_approved",
    "approval_prompt",
    "approval_prompt_always",
    "approval_card_title",
    "approval_card_question",
    "approval_card_details",
    "approval_card_approve",
    "approval_card_always",
    "approval_card_deny",
    "approval_card_note",
    "approval_card_note_always",
    "approval_label_command",
    "approval_label_network",
    "approval_label_network_host",
    "approval_label_path",
    "approval_label_code",
    "approval_packages",
    "approval_unknown_command",
    "approval_probe_throttled",
    "approval_no_pending",
    "approval_owner_only",
    "approval_always_requires_admin",
    "approval_invalid_choice",
    "approval_already_resolved",
    "approval_resolution_failed",
    "approval_denied",
    "approval_approved_once",
    "approval_approved_always",
    "command_usage_sandbox",
    "command_unsupported",
    "command_completed",
    "command_denied",
    "command_failed",
    "command_sandbox_denied",
    "command_sandbox_failed",
    "command_sandbox_updated",
    "command_sandbox_standard",
    "command_sandbox_trusted",
    "command_sandbox_full",
    "command_sandbox_unknown_mode",
    "command_compact_denied",
    "command_compact_failed",
    "command_compact_completed",
    "command_compact_skipped",
    "command_meta_denied",
    "command_meta_failed",
    "command_meta_empty",
    "command_meta_heading",
    "command_missing_scope",
    "command_new_denied",
    "command_new_unavailable",
]

_DEFAULT_LOCALE = "en"

_MESSAGES: dict[str, dict[ChannelSystemMessageKey, str]] = {
    "en": {
        "pairing_required": (
            "Access approval is required. Pairing request: {pairing_code}. "
            "Ask an OpenSquilla operator to approve it before sending another message."
        ),
        "pairing_approved": "Access approved. Send a message to start chatting.",
        "approval_prompt": (
            "Approval needed to run a privileged command.\n"
            "{label}: {command}\n"
            "Code: {code}\n"
            "Reply /approve {code} to allow or /deny {code} to refuse."
        ),
        "approval_prompt_always": (
            "Approval needed to run a privileged command.\n"
            "{label}: {command}\n"
            "Code: {code}\n"
            "Reply /approve {code} to allow, /approve {code} always to stop asking "
            "for this kind, or /deny {code} to refuse."
        ),
        "approval_card_title": "Approval needed",
        "approval_card_question": "Run a privileged command?",
        "approval_card_details": (
            "{question}\n**{label}:** `{command}`\n**{code_label}:** `{code}`"
        ),
        "approval_card_approve": "Approve",
        "approval_card_always": "Always allow",
        "approval_card_deny": "Deny",
        "approval_card_note": "Or reply /approve {code} or /deny {code}.",
        "approval_card_note_always": (
            "Or reply /approve {code}, /approve {code} always, or /deny {code}."
        ),
        "approval_label_command": "Command",
        "approval_label_network": "Network",
        "approval_label_network_host": "Network host",
        "approval_label_path": "Path",
        "approval_label_code": "Code",
        "approval_packages": "packages: {bundle_id}",
        "approval_unknown_command": "(unknown command)",
        "approval_probe_throttled": (
            "Too many failed approval attempts — wait a minute and try again."
        ),
        "approval_no_pending": "No pending approval {code}.",
        "approval_owner_only": (
            "Only the session owner can resolve this. Ask them to reply /approve {code}."
        ),
        "approval_always_requires_admin": (
            "'Always' needs a channel admin. Reply /approve {code} to allow just this once."
        ),
        "approval_invalid_choice": (
            "Could not apply approval {code} — it is still pending. Resolve it from the console."
        ),
        "approval_already_resolved": "Approval {code} was already resolved.",
        "approval_resolution_failed": (
            "Could not apply approval {code} — it is still pending, please try again."
        ),
        "approval_denied": "Denied {code}.",
        "approval_approved_once": "Approved {code} — running …",
        "approval_approved_always": (
            "Approved {code} — this kind won't ask again this session."
        ),
        "command_usage_sandbox": "Usage: /sandbox standard | trusted | full",
        "command_unsupported": "Unsupported command: {command}. Try /help.",
        "command_completed": "/{name} completed",
        "command_denied": "/{name} denied{reason}",
        "command_failed": "/{name} failed{reason}",
        "command_sandbox_denied": "Sandbox mode denied: {reason}",
        "command_sandbox_failed": "Sandbox mode failed: {reason}",
        "command_sandbox_updated": "Sandbox mode set to {mode}.",
        "command_sandbox_standard": "Standard-Sandbox",
        "command_sandbox_trusted": "Managed Execution",
        "command_sandbox_full": "Full Host Access",
        "command_sandbox_unknown_mode": "updated",
        "command_compact_denied": "Compact denied: {reason}",
        "command_compact_failed": "Compact failed: {reason}",
        "command_compact_completed": "Context compacted.",
        "command_compact_skipped": "Already within context budget; no compact was applied.",
        "command_meta_denied": "/meta denied: {reason}",
        "command_meta_failed": "/meta failed: {reason}",
        "command_meta_empty": "No meta-skills available.",
        "command_meta_heading": "Available meta-skills:",
        "command_missing_scope": ": missing {missing}",
        "command_new_denied": "/new denied: Insufficient scope for method: {method}{detail}",
        "command_new_unavailable": "/new failed: command unavailable",
    },
    "zh-Hans": {
        "pairing_required": (
            "需要访问审批。配对申请：{pairing_code}。"
            "请联系 OpenSquilla 操作员批准后再发送消息。"
        ),
        "pairing_approved": "访问已获批准。请发送一条消息以开始对话。",
        "approval_prompt": (
            "需要批准才能运行特权命令。\n"
            "{label}：{command}\n"
            "代码：{code}\n"
            "回复 /approve {code} 以允许，或回复 /deny {code} 以拒绝。"
        ),
        "approval_prompt_always": (
            "需要批准才能运行特权命令。\n"
            "{label}：{command}\n"
            "代码：{code}\n"
            "回复 /approve {code} 以允许，回复 /approve {code} always 以不再询问此类操作，"
            "或回复 /deny {code} 以拒绝。"
        ),
        "approval_card_title": "需要批准",
        "approval_card_question": "要运行特权命令吗？",
        "approval_card_details": (
            "{question}\n**{label}：** `{command}`\n**{code_label}：** `{code}`"
        ),
        "approval_card_approve": "批准",
        "approval_card_always": "始终允许",
        "approval_card_deny": "拒绝",
        "approval_card_note": "或回复 /approve {code} 或 /deny {code}。",
        "approval_card_note_always": (
            "或回复 /approve {code}、/approve {code} always 或 /deny {code}。"
        ),
        "approval_label_command": "命令",
        "approval_label_network": "网络",
        "approval_label_network_host": "网络主机",
        "approval_label_path": "路径",
        "approval_label_code": "代码",
        "approval_packages": "软件包：{bundle_id}",
        "approval_unknown_command": "（未知命令）",
        "approval_probe_throttled": "失败的批准尝试过多，请等待一分钟后重试。",
        "approval_no_pending": "没有待处理的批准 {code}。",
        "approval_owner_only": (
            "只有会话所有者可以处理此批准。请让其回复 /approve {code}。"
        ),
        "approval_always_requires_admin": (
            "“始终允许”需要频道管理员权限。回复 /approve {code} 仅允许这一次。"
        ),
        "approval_invalid_choice": (
            "无法应用批准 {code}，它仍在等待处理。请在控制台中处理。"
        ),
        "approval_already_resolved": "批准 {code} 已被处理。",
        "approval_resolution_failed": (
            "无法应用批准 {code}，它仍在等待处理，请重试。"
        ),
        "approval_denied": "已拒绝 {code}。",
        "approval_approved_once": "已批准 {code}，正在运行……",
        "approval_approved_always": "已批准 {code}，本次会话中将不再询问此类操作。",
        "command_usage_sandbox": "用法：/sandbox standard | trusted | full",
        "command_unsupported": "不支持的命令：{command}。请尝试 /help。",
        "command_completed": "/{name} 已完成",
        "command_denied": "/{name} 已被拒绝{reason}",
        "command_failed": "/{name} 失败{reason}",
        "command_sandbox_denied": "沙箱模式被拒绝：{reason}",
        "command_sandbox_failed": "沙箱模式设置失败：{reason}",
        "command_sandbox_updated": "沙箱模式已设为 {mode}。",
        "command_sandbox_standard": "标准沙箱",
        "command_sandbox_trusted": "受管执行",
        "command_sandbox_full": "完全主机访问",
        "command_sandbox_unknown_mode": "已更新",
        "command_compact_denied": "压缩被拒绝：{reason}",
        "command_compact_failed": "压缩失败：{reason}",
        "command_compact_completed": "上下文已压缩。",
        "command_compact_skipped": "上下文仍在预算范围内，未执行压缩。",
        "command_meta_denied": "/meta 被拒绝：{reason}",
        "command_meta_failed": "/meta 失败：{reason}",
        "command_meta_empty": "没有可用的元技能。",
        "command_meta_heading": "可用的元技能：",
        "command_missing_scope": "：缺少 {missing}",
        "command_new_denied": "/new 被拒绝：方法权限不足：{method}{detail}",
        "command_new_unavailable": "/new 失败：命令不可用",
    },
    "ja": {
        "pairing_required": (
            "アクセスの承認が必要です。ペアリング リクエスト: {pairing_code}。"
            "別のメッセージを送る前に、OpenSquilla のオペレーターに承認を依頼してください。"
        ),
        "pairing_approved": (
            "アクセスが承認されました。メッセージを送信して会話を開始してください。"
        ),
        "approval_prompt": (
            "特権コマンドを実行するには承認が必要です。\n"
            "{label}: {command}\n"
            "コード: {code}\n"
            "/approve {code} で許可するか、/deny {code} で拒否してください。"
        ),
        "approval_prompt_always": (
            "特権コマンドを実行するには承認が必要です。\n"
            "{label}: {command}\n"
            "コード: {code}\n"
            "/approve {code} で許可、/approve {code} always でこの種類の確認を今後表示"
            "しない、または /deny {code} で拒否してください。"
        ),
        "approval_card_title": "承認が必要です",
        "approval_card_question": "特権コマンドを実行しますか？",
        "approval_card_details": (
            "{question}\n**{label}：** `{command}`\n**{code_label}：** `{code}`"
        ),
        "approval_card_approve": "承認",
        "approval_card_always": "常に許可",
        "approval_card_deny": "拒否",
        "approval_card_note": "または /approve {code} か /deny {code} と返信してください。",
        "approval_card_note_always": (
            "または /approve {code}、/approve {code} always、/deny {code} と返信してください。"
        ),
        "approval_label_command": "コマンド",
        "approval_label_network": "ネットワーク",
        "approval_label_network_host": "ネットワーク ホスト",
        "approval_label_path": "パス",
        "approval_label_code": "コード",
        "approval_packages": "パッケージ: {bundle_id}",
        "approval_unknown_command": "(不明なコマンド)",
        "approval_probe_throttled": (
            "承認コードの試行回数が多すぎます。1 分待ってからもう一度試してください。"
        ),
        "approval_no_pending": "保留中の承認 {code} はありません。",
        "approval_owner_only": (
            "この承認を解決できるのはセッション所有者だけです。"
            "/approve {code} と返信するよう依頼してください。"
        ),
        "approval_always_requires_admin": (
            "「常に許可」にはチャンネル管理者が必要です。"
            "/approve {code} で今回だけ許可してください。"
        ),
        "approval_invalid_choice": (
            "承認 {code} を適用できません。まだ保留中です。コンソールから解決してください。"
        ),
        "approval_already_resolved": "承認 {code} はすでに解決されています。",
        "approval_resolution_failed": (
            "承認 {code} を適用できません。まだ保留中です。もう一度試してください。"
        ),
        "approval_denied": "{code} を拒否しました。",
        "approval_approved_once": "{code} を承認しました。実行中です ...",
        "approval_approved_always": (
            "{code} を承認しました。このセッションではこの種類を今後確認しません。"
        ),
        "command_usage_sandbox": "使い方: /sandbox standard | trusted | full",
        "command_unsupported": "未対応のコマンドです: {command}。/help を試してください。",
        "command_completed": "/{name} が完了しました",
        "command_denied": "/{name} は拒否されました{reason}",
        "command_failed": "/{name} は失敗しました{reason}",
        "command_sandbox_denied": "サンドボックス モードは拒否されました: {reason}",
        "command_sandbox_failed": "サンドボックス モードの設定に失敗しました: {reason}",
        "command_sandbox_updated": "サンドボックス モードを {mode} に設定しました。",
        "command_sandbox_standard": "標準サンドボックス",
        "command_sandbox_trusted": "管理された実行",
        "command_sandbox_full": "完全なホストアクセス",
        "command_sandbox_unknown_mode": "更新済み",
        "command_compact_denied": "コンテキスト圧縮は拒否されました: {reason}",
        "command_compact_failed": "コンテキスト圧縮に失敗しました: {reason}",
        "command_compact_completed": "コンテキストを圧縮しました。",
        "command_compact_skipped": "コンテキストは予算内のため、圧縮しませんでした。",
        "command_meta_denied": "/meta は拒否されました: {reason}",
        "command_meta_failed": "/meta は失敗しました: {reason}",
        "command_meta_empty": "利用可能なメタスキルはありません。",
        "command_meta_heading": "利用可能なメタスキル:",
        "command_missing_scope": ": 不足している権限 {missing}",
        "command_new_denied": (
            "/new は拒否されました: メソッドの権限が不足しています: {method}{detail}"
        ),
        "command_new_unavailable": "/new は失敗しました: コマンドを利用できません",
    },
    "fr": {
        "pairing_required": (
            "Une approbation d'accès est requise. Demande d'appairage : {pairing_code}. "
            "Demandez à un opérateur OpenSquilla de l'approuver avant d'envoyer un autre message."
        ),
        "pairing_approved": "Accès approuvé. Envoyez un message pour commencer à discuter.",
        "approval_prompt": (
            "Une approbation est requise pour exécuter une commande privilégiée.\n"
            "{label} : {command}\n"
            "Code : {code}\n"
            "Répondez /approve {code} pour autoriser ou /deny {code} pour refuser."
        ),
        "approval_prompt_always": (
            "Une approbation est requise pour exécuter une commande privilégiée.\n"
            "{label} : {command}\n"
            "Code : {code}\n"
            "Répondez /approve {code} pour autoriser, /approve {code} always pour ne plus "
            "demander pour ce type, ou /deny {code} pour refuser."
        ),
        "approval_card_title": "Approbation requise",
        "approval_card_question": "Exécuter une commande privilégiée ?",
        "approval_card_details": (
            "{question}\n**{label} :** `{command}`\n**{code_label} :** `{code}`"
        ),
        "approval_card_approve": "Approuver",
        "approval_card_always": "Toujours autoriser",
        "approval_card_deny": "Refuser",
        "approval_card_note": "Ou répondez /approve {code} ou /deny {code}.",
        "approval_card_note_always": (
            "Ou répondez /approve {code}, /approve {code} always ou /deny {code}."
        ),
        "approval_label_command": "Commande",
        "approval_label_network": "Réseau",
        "approval_label_network_host": "Hôte réseau",
        "approval_label_path": "Chemin",
        "approval_label_code": "Code",
        "approval_packages": "paquets : {bundle_id}",
        "approval_unknown_command": "(commande inconnue)",
        "approval_probe_throttled": (
            "Trop de tentatives d'approbation ont échoué. Attendez une minute et réessayez."
        ),
        "approval_no_pending": "Aucune approbation en attente pour {code}.",
        "approval_owner_only": (
            "Seul le propriétaire de la session peut résoudre ceci. Demandez-lui de répondre "
            "/approve {code}."
        ),
        "approval_always_requires_admin": (
            "« Toujours autoriser » nécessite un administrateur du canal. Répondez /approve "
            "{code} pour autoriser cette fois seulement."
        ),
        "approval_invalid_choice": (
            "Impossible d'appliquer l'approbation {code} ; elle est toujours en attente. "
            "Résolvez-la depuis la console."
        ),
        "approval_already_resolved": "L'approbation {code} a déjà été résolue.",
        "approval_resolution_failed": (
            "Impossible d'appliquer l'approbation {code} ; elle est toujours en attente. "
            "Réessayez."
        ),
        "approval_denied": "Approbation {code} refusée.",
        "approval_approved_once": "Approbation {code} accordée - exécution en cours ...",
        "approval_approved_always": (
            "Approbation {code} accordée - ce type ne demandera plus cette session."
        ),
        "command_usage_sandbox": "Utilisation : /sandbox standard | trusted | full",
        "command_unsupported": "Commande non prise en charge : {command}. Essayez /help.",
        "command_completed": "/{name} terminé",
        "command_denied": "/{name} refusé{reason}",
        "command_failed": "/{name} a échoué{reason}",
        "command_sandbox_denied": "Mode bac à sable refusé : {reason}",
        "command_sandbox_failed": "Échec du mode bac à sable : {reason}",
        "command_sandbox_updated": "Mode bac à sable défini sur {mode}.",
        "command_sandbox_standard": "Bac à sable standard",
        "command_sandbox_trusted": "Exécution gérée",
        "command_sandbox_full": "Accès complet à l'hôte",
        "command_sandbox_unknown_mode": "mis à jour",
        "command_compact_denied": "Compactage refusé : {reason}",
        "command_compact_failed": "Échec du compactage : {reason}",
        "command_compact_completed": "Contexte compacté.",
        "command_compact_skipped": (
            "Le contexte est déjà dans le budget ; aucun compactage appliqué."
        ),
        "command_meta_denied": "/meta refusé : {reason}",
        "command_meta_failed": "/meta a échoué : {reason}",
        "command_meta_empty": "Aucune méta-compétence disponible.",
        "command_meta_heading": "Méta-compétences disponibles :",
        "command_missing_scope": " : autorisation manquante {missing}",
        "command_new_denied": (
            "/new refusé : autorisation insuffisante pour la méthode : {method}{detail}"
        ),
        "command_new_unavailable": "/new a échoué : commande indisponible",
    },
    "de": {
        "pairing_required": (
            "Eine Zugriffsfreigabe ist erforderlich. Kopplungsanfrage: {pairing_code}. "
            "Bitten Sie einen OpenSquilla-Operator, sie zu genehmigen, bevor Sie eine "
            "weitere Nachricht senden."
        ),
        "pairing_approved": (
            "Zugriff genehmigt. Senden Sie eine Nachricht, um den Chat zu beginnen."
        ),
        "approval_prompt": (
            "Zum Ausführen eines privilegierten Befehls ist eine Freigabe erforderlich.\n"
            "{label}: {command}\n"
            "Code: {code}\n"
            "Antworten Sie mit /approve {code} zum Erlauben oder /deny {code} zum Ablehnen."
        ),
        "approval_prompt_always": (
            "Zum Ausführen eines privilegierten Befehls ist eine Freigabe erforderlich.\n"
            "{label}: {command}\n"
            "Code: {code}\n"
            "Antworten Sie mit /approve {code} zum Erlauben, /approve {code} always, um "
            "für diese Art nicht mehr gefragt zu werden, oder /deny {code} zum Ablehnen."
        ),
        "approval_card_title": "Freigabe erforderlich",
        "approval_card_question": "Privilegierten Befehl ausführen?",
        "approval_card_details": (
            "{question}\n**{label}:** `{command}`\n**{code_label}:** `{code}`"
        ),
        "approval_card_approve": "Genehmigen",
        "approval_card_always": "Immer erlauben",
        "approval_card_deny": "Ablehnen",
        "approval_card_note": "Oder antworten Sie mit /approve {code} oder /deny {code}.",
        "approval_card_note_always": (
            "Oder antworten Sie mit /approve {code}, /approve {code} always oder /deny {code}."
        ),
        "approval_label_command": "Befehl",
        "approval_label_network": "Netzwerk",
        "approval_label_network_host": "Netzwerkhost",
        "approval_label_path": "Pfad",
        "approval_label_code": "Code",
        "approval_packages": "Pakete: {bundle_id}",
        "approval_unknown_command": "(unbekannter Befehl)",
        "approval_probe_throttled": (
            "Zu viele fehlgeschlagene Freigabeversuche. Warten Sie eine Minute und versuchen "
            "Sie es erneut."
        ),
        "approval_no_pending": "Keine ausstehende Freigabe für {code}.",
        "approval_owner_only": (
            "Nur der Sitzungsinhaber kann dies auflösen. Bitten Sie ihn, mit /approve {code} "
            "zu antworten."
        ),
        "approval_always_requires_admin": (
            "„Immer erlauben“ benötigt einen Kanaladministrator. Antworten Sie mit /approve "
            "{code}, um dies nur einmal zu erlauben."
        ),
        "approval_invalid_choice": (
            "Freigabe {code} konnte nicht angewendet werden; sie ist weiterhin ausstehend. "
            "Lösen Sie sie in der Konsole auf."
        ),
        "approval_already_resolved": "Freigabe {code} wurde bereits aufgelöst.",
        "approval_resolution_failed": (
            "Freigabe {code} konnte nicht angewendet werden; sie ist weiterhin ausstehend. "
            "Versuchen Sie es erneut."
        ),
        "approval_denied": "Freigabe {code} abgelehnt.",
        "approval_approved_once": "Freigabe {code} genehmigt - wird ausgeführt ...",
        "approval_approved_always": (
            "Freigabe {code} genehmigt - diese Art wird in dieser Sitzung nicht erneut fragen."
        ),
        "command_usage_sandbox": "Verwendung: /sandbox standard | trusted | full",
        "command_unsupported": "Nicht unterstützter Befehl: {command}. Versuchen Sie /help.",
        "command_completed": "/{name} abgeschlossen",
        "command_denied": "/{name} abgelehnt{reason}",
        "command_failed": "/{name} fehlgeschlagen{reason}",
        "command_sandbox_denied": "Sandbox-Modus abgelehnt: {reason}",
        "command_sandbox_failed": "Sandbox-Modus fehlgeschlagen: {reason}",
        "command_sandbox_updated": "Sandbox-Modus auf {mode} gesetzt.",
        "command_sandbox_standard": "Standard-Sandbox",
        "command_sandbox_trusted": "Verwaltete Ausführung",
        "command_sandbox_full": "Vollständiger Hostzugriff",
        "command_sandbox_unknown_mode": "aktualisiert",
        "command_compact_denied": "Komprimierung abgelehnt: {reason}",
        "command_compact_failed": "Komprimierung fehlgeschlagen: {reason}",
        "command_compact_completed": "Kontext komprimiert.",
        "command_compact_skipped": (
            "Der Kontext liegt bereits im Budget; keine Komprimierung wurde angewendet."
        ),
        "command_meta_denied": "/meta abgelehnt: {reason}",
        "command_meta_failed": "/meta fehlgeschlagen: {reason}",
        "command_meta_empty": "Keine Meta-Skills verfügbar.",
        "command_meta_heading": "Verfügbare Meta-Skills:",
        "command_missing_scope": ": fehlende Berechtigung {missing}",
        "command_new_denied": "/new abgelehnt: Unzureichender Umfang für Methode: {method}{detail}",
        "command_new_unavailable": "/new fehlgeschlagen: Befehl nicht verfügbar",
    },
    "es": {
        "pairing_required": (
            "Se requiere aprobación de acceso. Solicitud de emparejamiento: {pairing_code}. "
            "Pide a un operador de OpenSquilla que la apruebe antes de enviar otro mensaje."
        ),
        "pairing_approved": "Acceso aprobado. Envía un mensaje para empezar a chatear.",
        "approval_prompt": (
            "Se requiere aprobación para ejecutar un comando con privilegios.\n"
            "{label}: {command}\n"
            "Código: {code}\n"
            "Responde /approve {code} para permitir o /deny {code} para rechazar."
        ),
        "approval_prompt_always": (
            "Se requiere aprobación para ejecutar un comando con privilegios.\n"
            "{label}: {command}\n"
            "Código: {code}\n"
            "Responde /approve {code} para permitir, /approve {code} always para no volver "
            "a preguntar por este tipo, o /deny {code} para rechazar."
        ),
        "approval_card_title": "Se requiere aprobación",
        "approval_card_question": "¿Ejecutar un comando con privilegios?",
        "approval_card_details": (
            "{question}\n**{label}:** `{command}`\n**{code_label}:** `{code}`"
        ),
        "approval_card_approve": "Aprobar",
        "approval_card_always": "Permitir siempre",
        "approval_card_deny": "Rechazar",
        "approval_card_note": "O responde /approve {code} o /deny {code}.",
        "approval_card_note_always": (
            "O responde /approve {code}, /approve {code} always o /deny {code}."
        ),
        "approval_label_command": "Comando",
        "approval_label_network": "Red",
        "approval_label_network_host": "Host de red",
        "approval_label_path": "Ruta",
        "approval_label_code": "Código",
        "approval_packages": "paquetes: {bundle_id}",
        "approval_unknown_command": "(comando desconocido)",
        "approval_probe_throttled": (
            "Demasiados intentos de aprobación fallidos. Espera un minuto e inténtalo de nuevo."
        ),
        "approval_no_pending": "No hay ninguna aprobación pendiente para {code}.",
        "approval_owner_only": (
            "Solo el propietario de la sesión puede resolver esto. Pídele que responda "
            "/approve {code}."
        ),
        "approval_always_requires_admin": (
            "«Permitir siempre» requiere un administrador del canal. Responde /approve {code} "
            "para permitir solo esta vez."
        ),
        "approval_invalid_choice": (
            "No se pudo aplicar la aprobación {code}; sigue pendiente. Resuélvela desde la "
            "consola."
        ),
        "approval_already_resolved": "La aprobación {code} ya se resolvió.",
        "approval_resolution_failed": (
            "No se pudo aplicar la aprobación {code}; sigue pendiente. Inténtalo de nuevo."
        ),
        "approval_denied": "Aprobación {code} rechazada.",
        "approval_approved_once": "Aprobación {code} concedida - ejecutando ...",
        "approval_approved_always": (
            "Aprobación {code} concedida - este tipo no volverá a preguntar en esta sesión."
        ),
        "command_usage_sandbox": "Uso: /sandbox standard | trusted | full",
        "command_unsupported": "Comando no compatible: {command}. Prueba /help.",
        "command_completed": "/{name} completado",
        "command_denied": "/{name} denegado{reason}",
        "command_failed": "/{name} falló{reason}",
        "command_sandbox_denied": "Modo aislado denegado: {reason}",
        "command_sandbox_failed": "El modo aislado falló: {reason}",
        "command_sandbox_updated": "Modo aislado establecido en {mode}.",
        "command_sandbox_standard": "Entorno aislado estándar",
        "command_sandbox_trusted": "Ejecución administrada",
        "command_sandbox_full": "Acceso completo al host",
        "command_sandbox_unknown_mode": "actualizado",
        "command_compact_denied": "Compactación denegada: {reason}",
        "command_compact_failed": "La compactación falló: {reason}",
        "command_compact_completed": "Contexto compactado.",
        "command_compact_skipped": (
            "El contexto ya está dentro del presupuesto; no se aplicó compactación."
        ),
        "command_meta_denied": "/meta denegado: {reason}",
        "command_meta_failed": "/meta falló: {reason}",
        "command_meta_empty": "No hay meta-habilidades disponibles.",
        "command_meta_heading": "Meta-habilidades disponibles:",
        "command_missing_scope": ": falta el permiso {missing}",
        "command_new_denied": (
            "/new denegado: alcance insuficiente para el método: {method}{detail}"
        ),
        "command_new_unavailable": "/new falló: comando no disponible",
    },
}


def channel_message_locale(config: Any = None) -> str:
    """Return the configured Gateway locale, with a stable English fallback."""

    control_ui = getattr(config, "control_ui", None)
    locale = getattr(control_ui, "default_locale", _DEFAULT_LOCALE)
    return locale if isinstance(locale, str) and locale in _MESSAGES else _DEFAULT_LOCALE


def render_channel_message(
    key: ChannelSystemMessageKey,
    *,
    config: Any = None,
    **values: str,
) -> str:
    """Render a fixed channel message from the persisted Gateway locale."""

    return _MESSAGES[channel_message_locale(config)][key].format(**values)
