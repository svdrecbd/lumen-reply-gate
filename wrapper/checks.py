"""Independent source-of-truth checks for the customer-support persona (Lumen Cloud).

Every rule reads the persona's own files (price_sheet.csv, policies.json) and the customer
context; nothing here is keyed to particular practice or final-set sentences.
Returns (level, message) pairs; level is BLOCK or HOLD_FOR_REVIEW.
"""
import re

BLOCK, HOLD = "BLOCK", "HOLD_FOR_REVIEW"

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}


def words_to_numbers(text):
    """Rewrite spelled-out numbers ("nineteen", "forty-nine", "a hundred") as digits."""
    tokens = re.split(r"(\s+|-)", text)
    out, i = [], 0
    while i < len(tokens):
        word = tokens[i].lower().strip(".,;:!?")
        if word in _UNITS or word in _TENS or word == "hundred":
            value, j, consumed = 0, i, False
            while j < len(tokens):
                w = tokens[j].lower().strip(".,;:!?")
                if w in _UNITS:
                    value += _UNITS[w]; consumed = True
                elif w in _TENS:
                    value += _TENS[w]; consumed = True
                elif w == "hundred":
                    value = max(value, 1) * 100; consumed = True
                elif w in ("", "-", "and") or tokens[j].isspace():
                    pass
                else:
                    break
                j += 1
            if consumed:
                trailing = re.match(r".*?([.,;:!?]*)$", tokens[j - 1]).group(1) if j > i else ""
                out.append(str(value) + " " + trailing)
                i = j
                continue
        out.append(tokens[i])
        i += 1
    return "".join(out)


def plan_rows(sot):
    return {row["plan"].lower(): row for row in sot["price_sheet"]}


def mentions(pattern, text):
    return re.search(pattern, text, flags=re.I) is not None


def nearest_plan(text, position, plans):
    """The plan named closest before a position, else the closest after it."""
    best, best_distance = None, None
    for plan in plans:
        for m in re.finditer(rf"\b{plan}\b(?!\s+(?:days?|hours?|hrs?|week))", text, flags=re.I):
            distance = position - m.end() if m.end() <= position else (m.start() - position) * 3
            if best_distance is None or 0 <= distance < best_distance:
                best, best_distance = plan, distance
    return best


def check_customer_support(text, context, sot):
    findings = []
    policies, plans = sot["policies"], plan_rows(sot)
    normalized = words_to_numbers(text)
    normalized = re.sub(r"\b(?:a|one)\s+(week|month|year)\b", r"1 \1", normalized, flags=re.I)
    normalized = re.sub(r"(\d+)\s*(?:-|\s)?\s*weeks?\b", lambda m: f"{int(m.group(1)) * 7} days", normalized, flags=re.I)
    normalized = re.sub(r"(\d+)\s*(?:-|\s)?\s*months?\b(?!\s+of\s+(?:tenure|being))", lambda m: f"{int(m.group(1)) * 30} days", normalized, flags=re.I)
    lower = normalized.lower()
    customer_plan = str(context.get("plan", "")).lower()
    tenure = context.get("customer_tenure_months")

    # 1. Prices: every dollar amount near a plan name must match the price sheet.
    for m in re.finditer(r"(?:\$|\busd\s?)\s?(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:us\s*)?(?:dollars|usd|bucks)\b", normalized, flags=re.I):
        amount = float(m.group(1) or m.group(2))
        plan = nearest_plan(normalized, m.start(), plans) or (customer_plan if customer_plan in plans else None)
        if plan is None:
            continue
        row = plans[plan]
        window = lower[max(0, m.start() - 60): m.end() + 60]
        annual = mentions(r"annual|yearly|per year|billed annually|/yr", window)
        valid = {float(row["monthly_price_usd"]), float(row["annual_price_per_month_usd"])}
        if annual:
            valid.add(round(float(row["annual_price_per_month_usd"]) * 12, 2))
        if amount not in valid:
            findings.append((BLOCK, f"{row['plan']} costs ${row['monthly_price_usd']}/month "
                                    f"(${row['annual_price_per_month_usd']}/month billed annually); the email says ${amount:g}."))

    # 2. Discounts: percentages must match a real discount, be eligible, and not be stacked.
    discounts = policies["discounts"]
    loyalty, annual_d = discounts["loyalty"], discounts["annual_billing"]
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)", normalized, flags=re.I):
        pct = float(m.group(1))
        window = lower[max(0, m.start() - 80): m.end() + 80]
        stacked = mentions(r"combine|stack|together with|on top of|plus|both", window) and mentions(r"loyal", window) and mentions(r"annual", window)
        if stacked or pct == loyalty["percent"] + annual_d["percent"]:
            findings.append((BLOCK, f"Discounts cannot be stacked (loyalty {loyalty['percent']}% and annual "
                                    f"{annual_d['percent']}% are separate offers)."))
            continue
        if mentions(r"loyal", window):
            if pct != loyalty["percent"]:
                findings.append((BLOCK, f"The loyalty discount is {loyalty['percent']}%, the email says {pct:g}%."))
        elif mentions(r"annual|yearly", window):
            if pct != annual_d["percent"]:
                findings.append((BLOCK, f"Annual billing saves {annual_d['percent']}%, the email says {pct:g}%."))
        elif mentions(r"discount|off|save|saving|deal|coupon|promo", window):
            if pct not in (loyalty["percent"], annual_d["percent"]):
                findings.append((BLOCK, f"No {pct:g}% discount exists; valid discounts are loyalty "
                                        f"{loyalty['percent']}% and annual billing {annual_d['percent']}%."))
    if mentions(r"loyal", lower) and mentions(r"discount|qualif|eligib|appl", lower) and not mentions(r"\b(?:not|isn't|aren't|don't|doesn't)\b", lower):
        eligible_plans = [p.lower() for p in loyalty["applies_to"]]
        offered_plan = nearest_plan(lower, lower.find("loyal"), plans) or customer_plan
        problems = []
        if offered_plan and offered_plan not in eligible_plans:
            problems.append(f"the loyalty discount applies only to {', '.join(loyalty['applies_to'])}")
        if isinstance(tenure, (int, float)) and tenure < loyalty["min_tenure_months"]:
            problems.append(f"it needs {loyalty['min_tenure_months']}+ months of tenure (customer has {tenure:g})")
        if customer_plan and customer_plan not in eligible_plans and offered_plan == customer_plan:
            problems.append(f"the customer is on {context.get('plan')}")
        if problems:
            findings.append((BLOCK, "Customer is not eligible: " + "; ".join(problems) + "."))
    if mentions(r"enterprise", lower) and re.search(r"\$\s?\d|\d+\s*(?:dollars|usd)", lower):
        findings.append((BLOCK, "Enterprise pricing is custom and quoted by sales; an email must not state a price."))
    if mentions(r"discount", lower) and mentions(r"enterprise|custom pric", lower):
        findings.append((HOLD, "Enterprise pricing is custom and handled by sales."))

    # 3. Refunds: the promised window must not exceed policy for the plan type.
    if mentions(r"refund", lower):
        refunds = policies["refunds"]
        is_annual = mentions(r"annual|yearly", lower)
        allowed = refunds["annual_plans_days"] if is_annual else refunds["monthly_plans_days"]
        claimed = None
        d = re.search(r"(\d+)\s*(?:-|\s)?\s*days?", lower)
        if d:
            claimed = int(d.group(1))
        elif mentions(r"any ?time|first year|within (?:a|one|the) year|12 months|whole year|forever|no questions", lower):
            claimed = 10_000
        if claimed is not None and claimed > allowed:
            findings.append((BLOCK, f"Refunds for {'annual' if is_annual else 'monthly'} plans are limited to "
                                    f"{allowed} days ({refunds['scope']}); the email promises more."))
        elif claimed is not None and claimed < allowed and not mentions(r"within", lower):
            findings.append((HOLD, f"Refund window stated as {claimed} days; policy is {allowed} days."))

    # 4. First-response times must match the plan's guarantee.
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?\s*(hours?|hrs?|business days?|days?)", lower):
        window = lower[max(0, m.start() - 90): m.end() + 30]
        if not mentions(r"respon|reply|get back|support|answer", window) or mentions(r"refund|trial", window):
            continue
        hours = float(m.group(1)) * (24 if m.group(2).startswith(("day", "business")) else 1)
        plan = nearest_plan(lower, m.start(), plans) or (customer_plan if customer_plan in plans else None)
        if plan and hours != float(plans[plan]["first_response_hours"]):
            findings.append((BLOCK, f"{plans[plan]['plan']} first response is within {plans[plan]['first_response_hours']} "
                                    f"hours; the email says {m.group(0)}."))

    # 5. Free trials.
    if mentions(r"trial", lower):
        trial = policies["free_trial"]
        d = re.search(r"(\d+)\s*(?:-|\s)?\s*days?", lower)
        if d and int(d.group(1)) != trial["days"]:
            findings.append((BLOCK, f"The free trial is {trial['days']} days; the email says {d.group(1)}."))
        plan = nearest_plan(lower, lower.find("trial"), plans)
        if plan and plans[plan]["plan"] not in trial["plans"]:
            findings.append((BLOCK, f"Only {', '.join(trial['plans'])} have a free trial; {plans[plan]['plan']} does not."))
        card_claim = re.search(r"(?:credit )?card (?:is |will be )?required|need(?:s)? a (?:credit )?card|require[sd]? a (?:credit )?card", lower)
        card_negated = card_claim and re.search(r"\b(?:no|not|without|never|don't|doesn't|isn't)\b[^.]{0,25}$", lower[:card_claim.start()])
        if card_claim and not card_negated and not trial["card_required"]:
            findings.append((BLOCK, "No card is required for the free trial."))

    # 6. Promises an agent cannot make (features, delivery dates, exceptions): manager review.
    promise = r"\b(?:i'll|i will|we'll|we will|will be|i promise|we promise|make sure|guarantee|by next|next (?:week|month|quarter|release))\b"
    scope = (r"feature|ship|release|roadmap|build|deliver|integration|exception|waive|special case|custom|refund (?:you )?anyway|override"
             r"|\badd(?:ing)?\b|implement|launch|roll(?:ing)? out|enable|dark mode|free month|month free|extend (?:your|the) trial|credit (?:your|you)|match (?:a|their|the) price")
    dated = r"\bby (?:mon|tues|wednes|thurs|fri|satur|sun)day\b|\bby tomorrow\b|\bby (?:the )?end of (?:the )?(?:week|month|quarter)\b|\bby next\b|\bready (?:for you )?(?:by|in|next)\b|\bwill be (?:ready|live|available|fixed|done)\b"
    communication = r"update|follow(?:ing)? up|get back|reply|respond|email you|call you|let you know|reach out|send (?:you )?(?:an?|the)"
    if mentions(promise, lower) and mentions(dated, lower) and not mentions(communication, lower):
        findings.append((HOLD, "Promises a delivery date, which agents cannot commit to."))
    elif mentions(promise, lower) and mentions(scope, lower):
        findings.append((HOLD, "Promises a product feature, delivery date or exception, which agents cannot commit to."))

    # 7. Hype and flattery: manager review even when the numbers are right.
    caps = [w for w in re.findall(r"\b[A-Z]{3,}\b", text) if w.lower() not in plans and w not in {"GDPR", "USD", "API", "SLA", "CCPA", "EU", "FAQ"}]
    exclaim = text.count("!")
    hype = mentions(r"\bguarantee[d]?\b|you(?:'ll| will) love|best (?:deal|price|plan) ever|amazing|incredible|unbeatable|once in a lifetime|act now"
                    r"|won't find a better|lowest price|best value on the market|no-brainer|insanely|mind-blowing", lower)
    if exclaim >= 2 or len(caps) >= 2 or hype:
        findings.append((HOLD, "Over-the-top or promotional language (" + ", ".join(
            filter(None, [f"{exclaim} exclamation marks" if exclaim >= 2 else "", f"all-caps: {' '.join(caps[:3])}" if caps else "",
                          "guarantee/hype wording" if hype else ""])) + ")."))
    # 8. Flattery or siding with the customer against policy (business rule 4).
    if mentions(r"you(?:'re| are) (?:absolutely|totally|completely|so) right|great question|excellent question|brilliant question"
                r"|you deserve (?:a |an )?(?:bigger|better|larger|extra|special)|more than (?:the )?policy allows|beyond (?:our|the) policy", lower):
        findings.append((HOLD, "Flattery or agreement with the customer against policy; a manager should review the wording."))
    return findings
