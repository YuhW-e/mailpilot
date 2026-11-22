import re
import unicodedata
from datetime import date, time
from typing import Annotated, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

type Importance = Literal["urgent", "high", "normal", "low", "advertisement"]

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Category = Annotated[ShortText, Field(max_length=128)]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
_HAN = re.compile(r"[\u3400-\u9fff]")
_MONTHS = {
    alias: number
    for number, name in enumerate(
        (
            "january february march april may june july august september october november december"
        ).split(),
        start=1,
    )
    for alias in (name, name[:3])
}
_MONTH_PATTERN = "(?i:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_DATE = re.compile(
    r"(?<![0-9/.-])(?:"
    r"(?P<year>[0-9]{4})[-/.年](?P<month>[0-9]{1,2})[-/.月](?P<day>[0-9]{1,2})日?"
    r"|(?P<dotted_day>[0-9]{1,2})\.(?P<dotted_month>[0-9]{1,2})\.(?P<dotted_year>[0-9]{4})"
    rf"|(?P<named_day>[0-9]{{1,2}}) +(?P<named_month>{_MONTH_PATTERN}) +(?P<named_year>[0-9]{{4}})"
    rf"|(?P<us_month>{_MONTH_PATTERN}) +(?P<us_day>[0-9]{{1,2}}),? +(?P<us_year>[0-9]{{4}})"
    r")"
    r"(?:(?:T| +(?:(?i:at|um|à) +)?)(?P<time>[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)"
    r"(?: *(?P<meridiem>(?i:am|pm))\b)?"
    r"(?: *(?P<zone>(?:UTC|GMT)?[+-](?:[01][0-9]|2[0-3]):?[0-5][0-9]"
    r"|(?:[A-Za-z_+-]+/)+[A-Za-z_+-]+"
    r"|(?:Z|UTC|GMT|CET|CEST|EET|EEST|PST|PDT|MST|MDT|CST|CDT|EST|EDT|IST|JST|"
    r"KST|HKT|SGT|BST|AEST|AEDT|NZST|NZDT)\b))?)?"
    r"(?![0-9:/T]|[.-][0-9])"
)
_DEADLINE_CONTEXT = re.compile(
    r"\b(?:deadline|due|submit\s+by|register\s+by|no\s+later\s+than|abgabefrist|"
    r"abgabe\s+bis|frist|délai|date\s+limite)\b|截止|截至|最晚|之前提交",
    re.IGNORECASE,
)


class ModelClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    category: Category
    importance: Importance
    summary_zh: Summary
    reason: ShortText
    action_items: tuple[ShortText, ...] = Field(max_length=5)
    deadline: ShortText | None = Field(
        default=None, description="Exact date/time expression copied from the source."
    )
    uncertain: bool

    @field_validator("summary_zh", "reason")
    @classmethod
    def chinese_text(cls, value: str) -> str:
        if not _HAN.search(value):
            raise ValueError("Expected Chinese text.")
        return value

    @field_validator("category", "summary_zh", "reason", "deadline")
    @classmethod
    def printable(cls, value: str | None) -> str | None:
        if value is not None and has_controls(value):
            raise ValueError("Unexpected control character.")
        return value

    @field_validator("action_items")
    @classmethod
    def printable_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            cls.printable(value)
        return values


def has_controls(value: str) -> bool:
    return any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\t\n\r" for char in value
    )


class Deadline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["date", "zoned", "unresolved"]
    value: ShortText
    evidence: ShortText
    source: Literal["subject", "body"]
    timezone: ShortText | None = None


def check_deadline(
    proposed: str | None, subject: str, body: str, *, subject_limit: int, body_limit: int
) -> Deadline | None:
    if proposed is None:
        return None
    for name, text, limit in (("subject", subject, subject_limit), ("body", body, body_limit)):
        source = cast(Literal["subject", "body"], name)
        for match in _DATE.finditer(text, 0, min(len(text), limit + 160)):
            if match.end() > limit or match[0] != proposed:
                continue
            left = max(text.rfind("\n", 0, match.start()) + 1, match.start() - 100)
            line_end = text.find("\n", match.end(), limit)
            right = min(line_end if line_end >= 0 else limit, match.end() + 80)
            if not _DEADLINE_CONTEXT.search(text[left:right]):
                continue
            try:
                day = _calendar_day(match)
                if not match["time"]:
                    return Deadline(
                        kind="date", value=day.isoformat(), evidence=proposed, source=source
                    )
                hour, minute, *seconds = map(int, match["time"].split(":"))
                if meridiem := match["meridiem"]:
                    if not 1 <= hour <= 12:
                        continue
                    hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
                clock = time(hour, minute, seconds[0] if seconds else 0)
            except ValueError:
                continue
            zone = match["zone"]
            if not zone:
                return Deadline(kind="unresolved", value=proposed, evidence=proposed, source=source)
            if "/" in zone:
                try:
                    ZoneInfo(zone)
                except ZoneInfoNotFoundError:
                    continue
            return Deadline(
                kind="zoned",
                value=f"{day.isoformat()}T{clock.isoformat()}",
                evidence=proposed,
                source=source,
                timezone=zone,
            )
    return None


def _calendar_day(match: re.Match[str]) -> date:
    for prefix in ("", "dotted_", "named_", "us_"):
        if year := match[f"{prefix}year"]:
            month = match[f"{prefix}month"]
            month_number = int(month) if month.isdecimal() else _MONTHS[month.casefold()]
            return date(int(year), month_number, int(match[f"{prefix}day"]))
    raise ValueError("Missing date.")


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    status: Literal["classified", "filtered", "degraded"]
    category: Category | None
    importance: Importance
    summary_zh: Summary | None = None
    reason: ShortText
    action_items: tuple[ShortText, ...] = Field(default=(), max_length=5)
    deadline: Deadline | None = None
    needs_review: bool = False
