from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EmailDecision:
    category: str
    is_task: bool
    confidence: float
    reason: str


OTP_RE = re.compile(
    r"(?:验证码|校验码|动态码|一次性密码|verification\s*code|security\s*code|one[- ]time\s*(?:code|password)|\botp\b)"
    r".{0,40}?\b\d{4,8}\b",
    re.I | re.S,
)
TASK_RE = re.compile(
    r"(?:请(?:于|在|尽快|及时)?|需要|必须|务必|待办|action\s+required|please|deadline|due\s+(?:by|date))"
    r".{0,80}(?:提交|完成|回复|确认|更新|审核|审批|签署|填写|缴纳|支付|参加|准备|处理|解决|review|submit|complete|respond|confirm|update|approve|sign|pay)",
    re.I | re.S,
)
AD_RE = re.compile(
    r"(?:限时|优惠|折扣|促销|领券|满减|低至|立即购买|新品上市|会员专享|unsubscribe|退订|sale|discount|coupon|shop\s+now)",
    re.I,
)
NEWSLETTER_RE = re.compile(r"(?:newsletter|weekly digest|daily digest|周报订阅|每日简报|每周简报)", re.I)


def classify_by_rules(subject: str, sender: str, body: str, headers: dict[str, str], label_ids: list[str]) -> EmailDecision | None:
    """Return a high-confidence decision, or None when semantic classification is needed."""
    text = f"{subject}\n{sender}\n{body[:12000]}"
    labels = {label.upper() for label in label_ids}
    if labels & {"SPAM", "TRASH", "CATEGORY_PROMOTIONS"}:
        return EmailDecision("advertisement", False, 0.99, "Gmail 已将邮件归入垃圾箱、垃圾邮件或促销分类")
    if OTP_RE.search(text) and not TASK_RE.search(text):
        return EmailDecision("otp", False, 0.99, "邮件主要内容是一次性验证码，没有后续待办要求")
    has_unsubscribe = bool(headers.get("list-unsubscribe"))
    precedence = headers.get("precedence", "").lower()
    if (has_unsubscribe or precedence in {"bulk", "list"}) and (AD_RE.search(text) or NEWSLETTER_RE.search(text)):
        return EmailDecision("advertisement", False, 0.97, "邮件头和正文表明这是营销或订阅群发邮件")
    if TASK_RE.search(text):
        return EmailDecision("task", True, 0.94, "邮件明确要求收件人完成、提交、回复或确认事项")
    if AD_RE.search(subject) and not TASK_RE.search(text):
        return EmailDecision("advertisement", False, 0.94, "邮件标题具有明显广告或促销特征")
    return None
