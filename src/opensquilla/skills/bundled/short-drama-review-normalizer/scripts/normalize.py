"""Deterministic consent normalization for the short-drama review gate."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from typing import Any

_EMPTY_SENTINELS = frozenset(
    {
        "",
        "(empty)",
        "empty",
        "(空)",
        "空",
        "null",
        "none",
        "undefined",
    }
)

_CANONICAL_SNAPSHOT_PHASE = "canonical_script_snapshot"
_MAX_CANONICAL_SCRIPT_BYTES = 200_000

_CJK_PUNCTUATION_RE = re.compile(r"[\s，,。.!！?？、；;：:\"'“”‘’（）()\[\]【】]+")
_ASCII_PUNCTUATION_RE = re.compile(r"[^a-z0-9]+")

_CANCEL_CJK = frozenset(
    {
        "取消",
        "取消吧",
        "请取消",
        "请取消吧",
        "算了",
        "算了吧",
        "停止",
        "停止吧",
        "停止生成",
        "停下",
        "终止",
        "不做了",
        "别做了",
        "不要继续",
        "不要生成",
    }
)
_CANCEL_EN = frozenset(
    {
        "cancel",
        "cancel it",
        "please cancel",
        "stop",
        "stop it",
        "abort",
        "never mind",
        "nevermind",
        "do not continue",
        "dont continue",
        "do not generate",
        "dont generate",
    }
)
_CANCEL_PREFIX_RE = re.compile(
    r"^(?:请)?(?:取消|算了|停止|停下|终止|不做了|别做了|不要继续|不要生成)"
    r"(?:这次|这个|任务|生成|制作)?(?:吧|了)?(?:[，,。.!！?？；;\s]|$)"
    r"|^(?:please\s+)?(?:cancel|stop|abort|never\s*mind|do\s+not\s+continue|"
    r"don't\s+continue|do\s+not\s+generate|don't\s+generate)\b",
    re.IGNORECASE,
)
_CANCEL_CLAUSE_SPLIT_RE = re.compile(
    r"[\n，,。.!！?？；;]+|(?:但(?:是)?|不过)|\b(?:but|however)\b",
    re.IGNORECASE,
)
_CANCEL_TEMPORAL_PREFIX_RE = re.compile(
    r"^(?:请\s*)?(?:先|暂时|暂且|目前|现在)\s*",
    re.IGNORECASE,
)
_GENERATION_ACTION_ZH = r"(?:继续)?(?:生成|渲染|制作|出图|出视频|提交生成|调用(?:接口|API|提供商)?)"
_GENERATION_DEFER_ZH_RE = re.compile(
    rf"(?:先|暂时|暂且|目前|现在|这次)?\s*"
    rf"(?:不要|别|不再|先不|先别|暂不|暂停|暂缓|停止)\s*{_GENERATION_ACTION_ZH}|"
    rf"(?:稍后|晚点|之后|以后|过会儿|等会儿?|待会儿?)\s*再?\s*{_GENERATION_ACTION_ZH}|"
    rf"{_GENERATION_ACTION_ZH}\s*(?:先|暂时)?\s*(?:不要|别|暂停|暂缓|稍后|晚点)",
    re.IGNORECASE,
)
_NEGATED_GENERATION_STOP_ZH_RE = re.compile(
    rf"(?:不要|别)\s*(?:停止|暂停|暂缓)\s*{_GENERATION_ACTION_ZH}",
    re.IGNORECASE,
)
_GENERATION_ACTION_EN = (
    r"(?:generat(?:e|ing|ion)|render(?:ing)?|creat(?:e|ing|ion)|"
    r"submi(?:t|tting)|proceed(?:ing)?|continu(?:e|ing)|"
    r"(?:provider|api)\s+calls?)"
)
_GENERATION_DEFER_EN_RE = re.compile(
    rf"\b(?:do\s+not|don't|dont|not)\s+(?:yet\s+)?{_GENERATION_ACTION_EN}\b|"
    rf"\b{_GENERATION_ACTION_EN}\s+(?:not\s+yet|later|afterwards|another\s+time)\b|"
    rf"\b(?:hold\s+off(?:\s+on)?|wait|pause|stop)\b"
    rf"[^\n.!?;]{{0,32}}\b(?:before\s+|on\s+|until\s+)?{_GENERATION_ACTION_EN}\b|"
    rf"\b(?:later|afterwards|another\s+time)\b"
    rf"[^\n.!?;]{{0,24}}\b{_GENERATION_ACTION_EN}\b",
    re.IGNORECASE,
)
_NEGATED_GENERATION_STOP_EN_RE = re.compile(
    rf"\b(?:do\s+not|don't|dont)\s+(?:stop|pause)\b"
    rf"[^\n.!?;]{{0,16}}\b{_GENERATION_ACTION_EN}\b",
    re.IGNORECASE,
)
_STANDALONE_DEFER_CLAUSE_RE = re.compile(
    r"^(?:please\s+)?(?:not\s+(?:right\s+)?now|not\s+yet)$|"
    r"^(?:please\s+)?(?:wait|hold\s+off|pause)\b[^\n.!?;]{0,80}$|"
    r"^(?:先)?(?:等等|等一下|别急|暂停一下|暂缓一下|稍后再说)$",
    re.IGNORECASE,
)
_WITHHELD_CONSENT_EN_RE = re.compile(
    r"\bnot\s+(?:right\s+)?now\b|\bnot\s+yet\b|"
    r"\bwait\s+until\s+(?:i|we)\s+(?:say|confirm|approve|tell|give)\b|"
    r"\b(?:i|we)(?:'ll|\s+will)\s+(?:tell|say|confirm|approve|"
    r"let\s+you\s+know)\b|"
    r"\b(?:when|after|once)\s+(?:i|we)\s+(?:say|confirm|approve|give)\b",
    re.IGNORECASE,
)
_WITHHELD_CONSENT_ZH_RE = re.compile(
    r"(?:等|待)我(?:先)?(?:确认|通知|说|同意|批准|许可)|"
    r"我(?:确认|通知|说|同意|批准|许可)(?:后|了再)|"
    r"(?:确认|批准|同意|许可)(?:后|了)(?:再)?(?:开始|继续|生成|渲染|出图|出片)|"
    r"(?:现在)?(?:先)?(?:别|不要|不)\s*(?:开始|继续|出图|出片|生成|渲染|提交|调用)|"
    r"(?:先)?(?:只)?(?:改|调整)(?:方案|脚本|分镜|风格)"
    r"[^\n，。！？；;]{0,32}(?:不|别|不要)\s*(?:出图|出片|生成|渲染|开始)",
    re.IGNORECASE,
)
_TRANSFER_REFUSAL_RE = re.compile(
    r"不要|别|不让|不能|不得|不可以|不准|禁止|不允许|不同意|拒绝|"
    r"\b(?:do not|don't|never|must not|mustn't|should not|shouldn't|"
    r"cannot|can't|may not|not allowed to|refuse(?:d)? to|"
    r"do not consent|don't consent)\b",
    re.IGNORECASE,
)
_TRANSFER_ACTION_RE = re.compile(
    r"发送|上传|外发|发给|传给|分享|提交|调用|使用|"
    r"(?:发|传)出去|"
    r"\b(?:send|upload|share|submit|transfer|call|use)\b",
    re.IGNORECASE,
)
_DIRECT_TRANSFER_ACTION_RE = re.compile(
    r"发送|上传|外发|发给|传给|分享|提交|发布|"
    r"(?:发|传)(?:出去|这个|这些|它|文件|脚本|参考图|图片|内容|数据)|"
    r"\b(?:send|upload|share|submit|transfer|publish)\b",
    re.IGNORECASE,
)
_TRANSFER_CLAUSE_BOUNDARY_RE = re.compile(r"[\n。.!！?？；;]")
_TRANSFER_CLAUSE_WINDOW_CHARS = 180

_EXTERNAL_RECIPIENT_RE = re.compile(
    r"外部(?!角色|人物|主角|配角|演员|道具|场景|画面|风格|视角|镜头)"
    r"(?:人员|机构|服务|系统|平台|提供商|供应商)?|"
    r"第三方(?!角色|人物|主角|配角|演员|视角|镜头)"
    r"(?:人员|机构|服务|系统|平台|提供商|供应商)?|"
    r"提供商|供应商|云端(?!背景|场景|画面|天空)|接口|\bAPI\b|"
    r"\b(?:external|outside)\s+(?:part(?:y|ies)|providers?|vendors?|services?|systems?)\b|"
    r"\bthird[- ]part(?:y|ies)\b"
    r"(?!\s+(?:character|protagonist|villain|actor|role))|"
    r"\b(?:providers?|vendors?|cloud\s+services?|external\s+apis?)\b",
    re.IGNORECASE,
)
_EXTERNAL_VISIBILITY_ACTION_RE = re.compile(
    r"看到|查看|可见|读取|访问|接收|收到|获得|获取|拿到|接触|知悉|"
    r"\b(?:see|sees|seen|view|views|viewed|access|accesses|accessed|read|reads|"
    r"receive|receives|received|get|gets|got|obtain|obtains|obtained|"
    r"inspect|inspects|inspected)\b",
    re.IGNORECASE,
)
_NO_EXTERNAL_ACCESS_RE = re.compile(
    r"\bno\s+(?:external\s+|outside\s+)?"
    r"(?:third[- ]part(?:y|ies)|part(?:y|ies)|providers?|vendors?|services?|systems?)"
    r"\b(?!\s+(?:character|protagonist|villain|actor|role))"
    r"[^\n.!?;]{0,48}\b(?:see|view|access|read|receive|get|obtain|inspect)\b|"
    r"\bno\s+(?:external|third[- ]party)\s+(?:access|visibility|receipt)\b|"
    r"\bwithout\s+(?:any\s+)?(?:external|third[- ]party)\s+"
    r"(?:access|visibility|receipt)\b",
    re.IGNORECASE,
)
_NO_CLOUD_OR_NETWORK_RE = re.compile(
    r"(?:不要|别|不能|不得|不准|禁止|不允许|不)\s*"
    r"(?:上云|联网|连接(?:到)?网络|使用(?:任何)?(?:云(?:端)?(?:服务|处理|生成|接口)|"
    r"远程服务))|"
    r"(?:仅|只)?(?:离线|断网)(?:处理|生成|运行)?|"
    r"(?:云端处理|云端生成|云服务|远程服务|网络连接)"
    r"[^\n，。！？；;]{0,16}(?:不要|不能|不得|不允许|禁止)|"
    r"\b(?:do\s+not|don't|dont|never|must\s+not|cannot|can't|without)\s+"
    r"(?:use\s+|connect\s+to\s+|send\s+to\s+)?(?:the\s+|any\s+)?"
    r"(?:cloud(?!\s+(?:background|scene|sky|lighting|style|texture))|internet|network|"
    r"remote\s+services?)\b|"
    r"\buse\s+no\s+(?:cloud(?!\s+(?:background|scene|sky|lighting|style|texture))"
    r"(?:\s+services?)?|internet|network|remote\s+services?)\b|"
    r"\b(?:no[- ]cloud(?!\s+(?:background|scene|sky|lighting|style|texture))|"
    r"no[- ]network|offline|air[- ]gapped)"
    r"(?:\s+(?:only|processing|generation))?\b",
    re.IGNORECASE,
)
_ZH_PRIVATE_OBJECT = (
    r"(?:这些?|上述|该|本)?"
    r"(?:内容|数据|资料|文件|素材|参考图|图片|图像|脚本|提示词|输入|输出|信息|它们?|东西)"
)
_ZH_LOCAL_BOUNDARY = r"(?:本机|本地|这台设备|当前设备|本设备|设备(?:上|内)?)"
_EN_PRIVATE_OBJECT = (
    r"(?:this|that|it|these|those|everything|"
    r"(?:the\s+|all\s+|any\s+)?"
    r"(?:content|data|files?|assets?|materials?|reference\s+images?|images?|scripts?|"
    r"prompts?|inputs?|outputs?|information))"
)
_EN_LOCAL_BOUNDARY = (
    r"(?:on[- ]device|local[- ]only|locally|"
    r"on\s+(?:this|my|your|the\s+current)\s+(?:device|machine|computer)|"
    r"within\s+(?:this|my|your|the\s+current)\s+(?:device|machine|computer))"
)
_LOCAL_CONFINEMENT_RE = re.compile(
    rf"{_ZH_PRIVATE_OBJECT}[^\n。！？；;]{{0,32}}"
    rf"(?:不能|不得|不可以|不准|禁止|不要)"
    rf"[^\n。！？；;]{{0,16}}(?:离开|传出|移出){_ZH_LOCAL_BOUNDARY}|"
    rf"(?:不能|不得|不可以|不准|禁止|不要)"
    rf"[^\n。！？；;]{{0,16}}(?:让)?{_ZH_PRIVATE_OBJECT}"
    rf"[^\n。！？；;]{{0,12}}(?:离开|传出|移出){_ZH_LOCAL_BOUNDARY}|"
    rf"{_ZH_PRIVATE_OBJECT}[^\n。！？；;]{{0,24}}"
    rf"(?:必须|只能|仅能|只可|务必)?[^\n。！？；;]{{0,8}}"
    rf"(?:留|保留|保存|处理|运行|使用)在{_ZH_LOCAL_BOUNDARY}|"
    rf"(?:仅限|只限|只能|必须|仅|只)[^\n。！？；;]{{0,8}}{_ZH_LOCAL_BOUNDARY}"
    rf"[^\n。！？；;]{{0,8}}(?:处理|保存|保留|使用|运行|访问)|"
    rf"\bkeep\s+{_EN_PRIVATE_OBJECT}\s+(?:strictly\s+)?{_EN_LOCAL_BOUNDARY}\b|"
    rf"\b{_EN_PRIVATE_OBJECT}[^\n.!?;]{{0,32}}"
    rf"(?:must|should|has\s+to|needs?\s+to)\s+(?:stay|remain|be\s+kept|be\s+stored|"
    rf"be\s+processed)\s+(?:strictly\s+)?{_EN_LOCAL_BOUNDARY}\b|"
    rf"\b{_EN_PRIVATE_OBJECT}[^\n.!?;]{{0,32}}"
    rf"(?:must\s+not|must\s+never|cannot|can't|may\s+not|should\s+not|never)\s+leave\s+"
    rf"(?:(?:this|my|your|the(?:\s+current)?)\s+)?"
    rf"(?:device|machine|computer|local\s+environment)\b|"
    rf"\b(?:keep|store|process|handle)\s+{_EN_PRIVATE_OBJECT}\s+"
    rf"(?:strictly\s+)?{_EN_LOCAL_BOUNDARY}\b|"
    r"\b(?:on[- ]device|local[- ]only)\s+(?:processing|storage|handling|access)\s+only\b",
    re.IGNORECASE,
)
_PRIVATE_CONTENT_RE = re.compile(
    r"\b(?:keep|make)\s+(?:(?:my|our|the|these|those)\s+)?"
    r"(?:content|data|files?|assets?|materials?|reference\s+images?|images?|scripts?|"
    r"prompts?|inputs?|outputs?|information)\s+(?:strictly\s+)?private\b|"
    rf"{_ZH_PRIVATE_OBJECT}[^\n，。！？；;]{{0,20}}(?:必须)?"
    r"(?:保密|保持私密|不能泄露|不得泄露|不要泄露)",
    re.IGNORECASE,
)

_APPROVE_CJK = frozenset(
    {
        "好",
        "好的",
        "好继续",
        "好的继续吧",
        "好吧",
        "可以",
        "继续",
        "继续吧",
        "继续生成",
        "开始生成",
        "生成吧",
        "没问题",
        "没问题继续",
        "可以继续",
        "确认",
        "确认生成",
        "同意",
        "通过",
        "看起来不错",
        "很好",
        "就这样",
        "就这么做",
    }
)
_APPROVE_EN = frozenset(
    {
        "ok",
        "okay",
        "okay proceed",
        "yes",
        "continue",
        "proceed",
        "go ahead",
        "approved",
        "approve",
        "confirmed",
        "confirm",
        "looks good",
        "looks great",
        "yes proceed",
        "fine",
        "generate",
        "start generation",
    }
)

_ZH_DOMAIN = (
    r"风格|画风|角色|人物|主角|分镜|镜头|场景|剧情|故事|对白|台词|"
    r"旁白|配音|结尾|开头|节奏|服装|发型|背景|画面|时长|标题|字幕|"
    r"色调|光线|音乐"
)
_ZH_REQUEST = (
    r"请|把|将|想要|希望|改|换|调整|增加|减少|删掉|删除|添加|加上|"
    r"去掉|保留|设为|做成|改成|换成|使用|用|不要|别"
)
_ZH_MODIFIER = r"更|快点|慢点|短点|长点|温暖|冷峻|明亮|暗一些|提前|延后"
_ZH_ADJUST_RE = re.compile(
    rf"(?:{_ZH_REQUEST})[^。！？\n]{{0,48}}(?:{_ZH_DOMAIN})"
    rf"|(?:{_ZH_DOMAIN})[^。！？\n]{{0,48}}(?:{_ZH_REQUEST}|{_ZH_MODIFIER})",
    re.IGNORECASE,
)
_EN_DOMAIN = (
    r"style|character|protagonist|shot|scene|story|plot|dialogue|voiceover|"
    r"ending|opening|pace|pacing|costume|hair|background|duration|title|"
    r"subtitle|colour|color|lighting|music"
)
_EN_REQUEST = (
    r"change|make|use|set|add|remove|delete|replace|switch|adjust|increase|"
    r"decrease|shorten|lengthen|speed up|slow down|rewrite|keep|drop"
)
_EN_MODIFIER = r"faster|slower|shorter|longer|warmer|darker|brighter|earlier|later"
_EN_ADJUST_RE = re.compile(
    rf"\b(?:{_EN_REQUEST})\b[^.!?\n]{{0,64}}\b(?:{_EN_DOMAIN})s?\b"
    rf"|\b(?:{_EN_DOMAIN})s?\b[^.!?\n]{{0,64}}\b(?:{_EN_REQUEST}|{_EN_MODIFIER})\b",
    re.IGNORECASE,
)
_SHOT_COUNT_ZH_RE = re.compile(
    r"(?:改成|调整为|换成|做成|要|用)?\s*(10|[1-9])\s*(?:个)?(?:分镜|镜头|镜)"
)
_SHOT_COUNT_EN_RE = re.compile(
    r"(?:change|set|make|use)?[^.!?\n]{0,20}\b(10|[1-9])\s+shots?\b",
    re.IGNORECASE,
)
_UNCLEAR_QUESTION_RE = re.compile(
    r"[?？]|怎么样|可以吗|行吗|好吗|是不是|是否|我不确定|不知道|随便|你决定|"
    r"\b(?:maybe|perhaps|not sure|i wonder|what|why|how|can we|could we|"
    r"should we|surprise me)\b",
    re.IGNORECASE,
)
_STYLE_RE = re.compile(
    r"水墨|工笔|卡通|动画|二次元|写实|摄影|插画|赛博朋克|像素|黏土|"
    r"国风|漫画|扁平|绘本|anime|watercolou?r|photoreal|cinematic|"
    r"illustration|comic|cyberpunk|pixel|clay|ink[- ]?wash|\b[23]d\b",
    re.IGNORECASE,
)
_IDENTITY_TOKEN_RE = re.compile(
    r"(?:\d{1,2}\s*岁|男(?:孩|人|生|主角)?|女(?:孩|人|生|主角)?|"
    r"律师|学生|店员|职员|机器人|短发|长发|西装|制服|穿着|"
    r"years?[- ]old|woman|man|girl|boy|lawyer|student|clerk|robot|"
    r"short hair|long hair|suit|uniform|wearing)",
    re.IGNORECASE,
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _clean_review(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("review", "")
    if not isinstance(value, str):
        return ""
    text = _CONTROL_RE.sub(" ", value)
    return re.sub(r"\s+", " ", text).strip()[:4000]


def _cjk_token(text: str) -> str:
    return _CJK_PUNCTUATION_RE.sub("", text).lower()


def _ascii_token(text: str) -> str:
    return _ASCII_PUNCTUATION_RE.sub(" ", text.lower()).strip()


def _is_cancel(text: str) -> bool:
    if _cjk_token(text) in {_cjk_token(value) for value in _CANCEL_CJK}:
        return True
    if _ascii_token(text) in _CANCEL_EN:
        return True

    # Review replies commonly combine an edit with a later consent boundary:
    # "change the style, but do not generate yet".  Authorization must be
    # evaluated per clause so an earlier meaningful adjustment cannot swallow
    # a subsequent pause/cancel instruction.  Only hard punctuation and
    # adversative conjunctions split clauses; ordinary negative edits such as
    # "do not use a fade out" remain non-cancellation adjustments.
    for raw_clause in _CANCEL_CLAUSE_SPLIT_RE.split(text):
        clause = raw_clause.strip()
        if not clause:
            continue
        if _CANCEL_PREFIX_RE.search(clause):
            return True
        temporal_stripped = _CANCEL_TEMPORAL_PREFIX_RE.sub("", clause, count=1)
        if temporal_stripped != clause and _CANCEL_PREFIX_RE.search(temporal_stripped):
            return True
    return False


def _is_generation_deferred(text: str) -> bool:
    """Detect an explicit boundary that withholds paid-media submission now."""

    # Normalize typographic apostrophes only for matching. NEW_NOTES continues
    # to preserve the user's original text when an adjustment is authorized.
    comparable = text.replace("’", "'").replace("‘", "'")
    comparable = _NEGATED_GENERATION_STOP_ZH_RE.sub("", comparable)
    comparable = _NEGATED_GENERATION_STOP_EN_RE.sub("", comparable)
    if _WITHHELD_CONSENT_ZH_RE.search(comparable):
        return True
    if _WITHHELD_CONSENT_EN_RE.search(comparable):
        return True
    if _GENERATION_DEFER_ZH_RE.search(comparable):
        return True
    if _GENERATION_DEFER_EN_RE.search(comparable):
        return True
    for raw_clause in _CANCEL_CLAUSE_SPLIT_RE.split(comparable):
        clause = raw_clause.strip()
        if clause and _STANDALONE_DEFER_CLAUSE_RE.fullmatch(clause):
            return True
    return False


def _is_approval(text: str) -> bool:
    return _cjk_token(text) in {_cjk_token(value) for value in _APPROVE_CJK} or (
        _ascii_token(text) in _APPROVE_EN
    )


def _refuses_external_transfer(text: str) -> bool:
    # A positive local-confinement directive ("keep this on-device") and a
    # recipient denial ("no third party may see this") are refusals even
    # though neither necessarily contains a conventional negation + transfer
    # verb pair. Evaluate these explicit privacy constraints first.
    if (
        _LOCAL_CONFINEMENT_RE.search(text)
        or _NO_EXTERNAL_ACCESS_RE.search(text)
        or _NO_CLOUD_OR_NETWORK_RE.search(text)
        or _PRIVATE_CONTENT_RE.search(text)
    ):
        return True

    for refusal in _TRANSFER_REFUSAL_RE.finditer(text):
        # Keep all three signals inside the same hard-delimited clause and a
        # bounded window, but inspect both sides of the refusal. Natural
        # wording often names the target first ("external providers must not
        # use ..."); looking only after the negation turns that refusal into a
        # meaningful adjustment and incorrectly authorizes provider calls.
        left = max(0, refusal.start() - _TRANSFER_CLAUSE_WINDOW_CHARS)
        right = min(len(text), refusal.end() + _TRANSFER_CLAUSE_WINDOW_CHARS)

        before = text[left : refusal.start()]
        boundaries = list(_TRANSFER_CLAUSE_BOUNDARY_RE.finditer(before))
        if boundaries:
            left += boundaries[-1].end()

        after = text[refusal.end() : right]
        boundary = _TRANSFER_CLAUSE_BOUNDARY_RE.search(after)
        if boundary is not None:
            right = refusal.end() + boundary.start()

        clause = text[left:right]
        # Sending/uploading/sharing is itself an external-transfer verb, so a
        # negated direct action such as "do not upload this" must fail closed
        # even when the user omits a destination. Broader verbs such as
        # "use"/"call" still require an explicit external target; this keeps
        # ordinary edits such as "do not use a fade out" from being blocked.
        if _DIRECT_TRANSFER_ACTION_RE.search(clause):
            return True
        if _TRANSFER_ACTION_RE.search(clause) and _EXTERNAL_RECIPIENT_RE.search(clause):
            return True
        if _EXTERNAL_VISIBILITY_ACTION_RE.search(clause) and _EXTERNAL_RECIPIENT_RE.search(
            clause
        ):
            return True
    return False


def _extract_shot_count(text: str) -> str:
    match = _SHOT_COUNT_ZH_RE.search(text) or _SHOT_COUNT_EN_RE.search(text)
    return match.group(1) if match else "unchanged"


def _is_meaningful_adjustment(text: str) -> bool:
    if _ZH_ADJUST_RE.search(text) or _EN_ADJUST_RE.search(text):
        return True
    if _SHOT_COUNT_ZH_RE.search(text) or _SHOT_COUNT_EN_RE.search(text):
        return True
    # A question or delegation is not authorization by itself. Requests such
    # as "能把镜头 2 改快点吗？" already matched an action pattern above.
    if _UNCLEAR_QUESTION_RE.search(text):
        return False
    # The review prompt explicitly allows a one-line style or identity value.
    if len(text) <= 160 and _STYLE_RE.search(text):
        return True
    if len(text) <= 200 and len(_IDENTITY_TOKEN_RE.findall(text)) >= 2:
        return True
    return False


def _block(
    *,
    decision: str,
    basis: str,
    notes: str = "unchanged",
    shot_count: str = "unchanged",
) -> str:
    has_overrides = (
        "yes" if decision in {"proceed", "revise"} and notes != "unchanged" else "no"
    )
    return "\n".join(
        (
            f"DECISION: {decision}",
            f"CONSENT_BASIS: {basis}",
            f"HAS_OVERRIDES: {has_overrides}",
            "NEW_RENDER_STYLE: unchanged",
            "NEW_IDENTITY_ANCHOR: unchanged",
            f"NEW_N_SHOTS: {shot_count if has_overrides == 'yes' else 'unchanged'}",
            f"NEW_NOTES: {notes if has_overrides == 'yes' else 'unchanged'}",
        )
    )


def _normalize_initial_review(review: str) -> str:
    """Classify the first draft review without treating edits as consent."""

    if review.lower() in _EMPTY_SENTINELS:
        return _block(decision="hold", basis="unclear_or_off_topic")
    if _refuses_external_transfer(review):
        return _block(decision="hold", basis="external_transfer_refused")
    if _is_cancel(review):
        return _block(decision="cancel", basis="explicit_cancel")
    if _is_generation_deferred(review):
        return _block(decision="hold", basis="generation_deferred")
    if _is_approval(review):
        return _block(decision="proceed", basis="explicit_approval")
    if _is_meaningful_adjustment(review):
        return _block(
            decision="revise",
            basis="meaningful_adjustment",
            notes=review[:1000],
            shot_count=_extract_shot_count(review),
        )
    return _block(decision="hold", basis="unclear_or_off_topic")


def _normalize_revision_confirmation(
    confirmation: str,
    *,
    revision: str,
    basis: str = "explicit_approval_after_revision",
) -> str:
    """Require a new, explicit approval after applying a requested edit."""

    if confirmation.lower() in _EMPTY_SENTINELS:
        return _block(decision="hold", basis="revision_confirmation_required")
    if _refuses_external_transfer(confirmation):
        return _block(decision="hold", basis="external_transfer_refused")
    if _is_cancel(confirmation):
        return _block(decision="cancel", basis="explicit_cancel")
    if _is_generation_deferred(confirmation):
        return _block(decision="hold", basis="generation_deferred")
    if not _is_approval(confirmation):
        return _block(decision="hold", basis="revision_confirmation_required")
    return _block(
        decision="proceed",
        basis=basis,
        notes=revision[:1000],
        shot_count=_extract_shot_count(revision),
    )


def _flag(value: Any) -> bool:
    """Interpret one parent-authored JSON/Jinja boolean without truthy strings."""

    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}


def freeze_canonical_script_snapshot(payload: Mapping[str, Any]) -> str:
    """Return the exact in-memory script bound to one final review decision.

    The published ``script.txt`` remains user-editable, so it cannot be the
    authority for later paid arguments. This helper receives scheduler output
    directly and emits that value without reading the workspace or involving a
    model. Proceed, hold, and cancel all retain a canonical delivery snapshot;
    only the downstream paid-step conditions interpret proceed as consent.
    """

    approval = payload.get("approval")
    script = payload.get("script")
    decision_lines = (
        [line for line in approval.splitlines() if line.startswith("DECISION: ")]
        if isinstance(approval, str)
        else []
    )
    if len(decision_lines) != 1 or decision_lines[0] not in {
        "DECISION: proceed",
        "DECISION: hold",
        "DECISION: cancel",
    }:
        raise ValueError("canonical script snapshot requires one valid final decision")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("canonical script snapshot is empty")
    if len(script.encode("utf-8")) > _MAX_CANONICAL_SCRIPT_BYTES:
        raise ValueError("canonical script snapshot exceeds the 200000-byte limit")
    return script


def normalize_review(payload: Mapping[str, Any]) -> str:
    """Return a fail-closed, bounded decision block for the review workflow.

    The default phase classifies the first draft review.  ``media_approval``
    is the final provider-call gate: if that first reply requested a revision,
    a separate explicit confirmation is required after the revised preview.
    """

    if payload.get("phase") == _CANONICAL_SNAPSHOT_PHASE:
        return freeze_canonical_script_snapshot(payload)

    review = _clean_review(payload.get("review", ""))
    initial = _normalize_initial_review(review)
    if payload.get("phase") != "media_approval":
        return initial

    initial_fields = dict(line.split(": ", 1) for line in initial.splitlines())
    initial_decision = initial_fields.get("DECISION")
    snapshot_changed = _flag(payload.get("approval_snapshot_changed"))
    if initial_decision != "revise" and not (
        initial_decision == "proceed" and snapshot_changed
    ):
        return initial
    confirmation = _clean_review(payload.get("confirmation", ""))
    snapshot_approval = initial_decision == "proceed" and snapshot_changed
    return _normalize_revision_confirmation(
        confirmation,
        revision="unchanged" if snapshot_approval else review,
        basis=(
            "explicit_approval_after_script_snapshot_change"
            if snapshot_approval
            else "explicit_approval_after_revision"
        ),
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    try:
        output = normalize_review(payload)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
