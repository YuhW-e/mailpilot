import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from email_assistant.ai.classification import Importance
from email_assistant.email.provider import MessageMetadata
from email_assistant.storage.business import Rule, RulePredicate
from email_assistant.storage.firestore import AccountKey

_SCHOOL_SUBJECT = re.compile(
    r"课程|考试|测验|作业|教务|调课|补课|停课|学籍|注册通知|行政通知"
    r"|\b(?:courses?|exams?|examination|midterms?|assignments?|homework|lectures?|"
    r"timetable changes?|registrar|academic notices?|enrol[ml]ents?|prüfung(?:en)?|"
    r"vorlesung(?:en)?|stundenplanänderung|studiensekretariat|einschreibung|"
    r"cours|examen(?:s)?|devoir(?:s)?|scolarité|inscription)\b",
    re.IGNORECASE,
)
_EXPLICIT_AD = re.compile(r"^(?:\[(?:advertisement|广告)\]|(?:advertisement|广告)\s*:)")


@dataclass(frozen=True)
class RuleDecision:
    protected: bool
    visible: bool | None
    category: str | None
    importance: Importance | None


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().casefold()


def _matches(predicate: RulePredicate, values: Iterable[str]) -> bool:
    needle = _normalize(predicate.value)
    if not needle:
        return False
    for value in values:
        candidate = _normalize(value)
        match predicate.operator:
            case "equals":
                if candidate == needle:
                    return True
            case "contains":
                if needle in candidate:
                    return True
            case "ends_with":
                if candidate.endswith(needle):
                    return True
    return False


def rule_order(entry: tuple[str, Rule]) -> tuple[int, bool, bool, bool, str]:
    key, rule = entry
    return (-rule.priority, not rule.override, rule.scope_id is None, rule.account_id is None, key)


def evaluate_rules(
    account: AccountKey,
    message: MessageMetadata,
    scope_ids: tuple[str, ...],
    rules: Iterable[tuple[str, Rule]],
) -> RuleDecision:
    fields = {"sender": (message.sender,), "subject": (message.subject,), "scope": scope_ids}
    matching = sorted(
        (
            (key, rule)
            for key, rule in rules
            if rule.enabled
            and rule.owner_id == account.owner_id
            and rule.account_id in (None, account.account_id)
            and (rule.scope_id is None or rule.scope_id in scope_ids)
            and _matches(rule.predicate, fields[rule.predicate.field])
        ),
        key=rule_order,
    )
    subject = _normalize(message.subject)
    protected = bool(_SCHOOL_SUBJECT.search(subject)) or any(
        rule.action.protect for _, rule in matching
    )
    visible: bool | None = True if protected else None
    category = None
    importance = None
    for _, rule in matching:
        action = rule.action
        if visible is None:
            visible = action.visible
            if visible is None and importance is None and action.importance == "advertisement":
                visible = False
        if category is None and action.category is not None:
            category = action.category.strip() or None
        if importance is None:
            importance = action.importance
    if visible is None and importance is None and _EXPLICIT_AD.match(subject):
        visible = False
        importance = "advertisement"
    return RuleDecision(protected, visible, category, importance)
