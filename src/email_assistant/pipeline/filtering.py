from email_assistant.pipeline.rules import RuleDecision


def filtering_reason(decision: RuleDecision) -> str | None:
    if decision.visible is False:
        return "规则识别为广告" if decision.importance == "advertisement" else "用户规则已过滤"
    return None
