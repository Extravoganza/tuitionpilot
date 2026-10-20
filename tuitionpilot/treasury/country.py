"""Country Intelligence Layer (CIL), spec v2.1 §5–9.

Sits between the invoice/student context and the optimizer:

    Invoice + Student Context → Corridor Resolver → Country Intelligence →
    Eligibility & Compliance Gates → Deadline Intelligence + Optimizer → Policy …

Principles enforced here (deterministic code, never the LLM):

* Profiles are versioned. Once a version has been used by a run it is frozen;
  an update creates the next version and old runs keep their version.
* Every rule and method has a scope, a source, ``checked_at`` and
  ``valid_until``. A stale BLOCKING rule turns the profile into
  RULES_OUTDATED; it never authorises a payment.
* UNKNOWN is never ELIGIBLE. Only ELIGIBLE methods may be ranked.
* A source that covers another university or corridor does not count
  (SOURCE_SCOPE_MISMATCH). One university's page is not "the rules of China".
* The two China corridors are separate profiles. Inbound methods of a Chinese
  university are never mirrored onto an outbound payment.
* Unknown values stay UNKNOWN: no guessed lead time, no guessed calendar.

All China-pack content below is a DEMO FIXTURE (synthetic universities, synthetic
rules). The public university pages listed in RESEARCH_SOURCES are research
starting points only: they are NOT_VERIFIED and are not used for decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Optional

from .calendar import BusinessCalendar

# ---------------------------------------------------------------- vocabulary
ELIGIBILITY = ("READY", "MISSING_DOCUMENTS", "MANUAL_REVIEW", "RULES_OUTDATED", "NOT_SUPPORTED")
METHOD_STATUS = ("ELIGIBLE", "INELIGIBLE", "UNKNOWN", "STALE", "MANUAL_REVIEW")
DATA_QUALITY = ("VERIFIED_SOURCE", "USER_PROVIDED", "SIMULATED", "STALE", "UNKNOWN")

FROM_ABROAD_TO_CHINA = "FROM_ABROAD_TO_CHINA"
FROM_CHINA_TO_ABROAD = "FROM_CHINA_TO_ABROAD"


class ProfileFrozen(RuntimeError):
    """A profile version that a run has used cannot be edited; publish a new version."""


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    publisher: str
    title: str
    url: Optional[str]
    checked_at: Optional[date]
    scope: dict                      # {"university_id": ..., "corridor_id": ..., "payer_types": [...]}
    quality: str                     # one of DATA_QUALITY
    status: str                      # DEMO_FIXTURE | NOT_VERIFIED | VERIFIED
    published_at: Optional[date] = None   # never invented: None when the source shows no date

    def to_dict(self) -> dict:
        return {"source_id": self.source_id, "publisher": self.publisher, "title": self.title, "url": self.url,
                "published_at": self.published_at.isoformat() if self.published_at else None,
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
                "scope": self.scope, "quality": self.quality, "status": self.status}


@dataclass(frozen=True)
class CountryRule:
    rule_id: str
    kind: str                        # DOCUMENT | REFERENCE | METHOD | CALENDAR
    text: str
    severity: str                    # BLOCKING | WARNING
    source_id: str
    checked_at: Optional[date]
    valid_until: Optional[date]
    requires: tuple = ()             # document keys for DOCUMENT rules
    payer_types: tuple = ()          # empty = every payer type
    version: int = 1

    def freshness(self, as_of: date) -> str:
        if self.checked_at is None or self.valid_until is None:
            return "UNKNOWN"
        return "FRESH" if as_of <= self.valid_until else "STALE"

    def to_dict(self, as_of: date) -> dict:
        return {"rule_id": self.rule_id, "kind": self.kind, "text": self.text, "severity": self.severity,
                "source_id": self.source_id, "requires": list(self.requires),
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
                "valid_until": self.valid_until.isoformat() if self.valid_until else None,
                "freshness": self.freshness(as_of), "version": self.version}


@dataclass(frozen=True)
class PaymentMethod:
    method_id: str
    name: str
    corridor_id: str
    university_id: str
    availability: str                # CONFIRMED | UNAVAILABLE | UNKNOWN (per source, within scope)
    source_id: Optional[str]
    checked_at: Optional[date]
    valid_until: Optional[date]
    payer_types: tuple = ("STUDENT", "PARENT_SPONSOR")
    currencies: tuple = ()
    requires_instrument: Optional[str] = None     # e.g. "mainland_bank_account"
    reference_supported: Optional[bool] = None    # None = unknown
    processing_bd: Optional[tuple] = None         # (min, max) business days; None = unknown
    route_id: Optional[str] = None                # optimizer route this method maps to (fixtures)
    caveats: tuple = ()

    def to_dict(self) -> dict:
        return {"method_id": self.method_id, "name": self.name, "corridor_id": self.corridor_id,
                "university_id": self.university_id, "availability": self.availability,
                "source_id": self.source_id, "payer_types": list(self.payer_types),
                "currencies": list(self.currencies), "requires_instrument": self.requires_instrument,
                "reference_supported": self.reference_supported,
                "processing_bd": list(self.processing_bd) if self.processing_bd else None,
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
                "valid_until": self.valid_until.isoformat() if self.valid_until else None,
                "route_id": self.route_id, "caveats": list(self.caveats)}


@dataclass(frozen=True)
class CalendarProfile:
    tz: Optional[str]
    holidays: frozenset = frozenset()
    supported: bool = True           # False → dates are not computed, caveat shown
    source_id: Optional[str] = None
    note: str = ""


@dataclass(frozen=True)
class CorridorProfile:
    profile_id: str
    version: int
    corridor_id: str
    sender_country: str              # "*" = any country except receiver
    receiver_country: str
    university_id: str
    university_name: str
    invoice_currency: str
    payer_types: tuple
    rules: tuple
    methods: tuple
    sources: tuple
    calendar: CalendarProfile
    processing_buffer_bd: int
    status: str = "DEMO_FIXTURE"

    def source(self, source_id: Optional[str]) -> Optional[SourceRecord]:
        return next((s for s in self.sources if s.source_id == source_id), None)

    def header(self) -> dict:
        return {"profile_id": self.profile_id, "profile_version": self.version, "corridor_id": self.corridor_id,
                "sender_country": self.sender_country, "receiver_country": self.receiver_country,
                "university_id": self.university_id, "university_name": self.university_name,
                "invoice_currency": self.invoice_currency, "status": self.status}


class ProfileRegistry:
    """Immutable, versioned profiles. A version used by a run is frozen."""

    def __init__(self):
        self._versions: dict[str, list[CorridorProfile]] = {}
        self._used: set = set()

    def publish(self, profile: CorridorProfile) -> CorridorProfile:
        hist = self._versions.setdefault(profile.profile_id, [])
        expected = len(hist) + 1
        if profile.version != expected:
            raise ValueError(f"next version of {profile.profile_id} must be {expected}")
        hist.append(profile)
        return profile

    def amend(self, profile_id: str, **changes) -> CorridorProfile:
        """Create the next version; never edits an existing one."""
        cur = self.latest(profile_id)
        return self.publish(replace(cur, version=cur.version + 1, **changes))

    def replace_in_place(self, profile_id: str, version: int, **changes) -> CorridorProfile:
        """Only allowed before first use (fixture authoring). After use: ProfileFrozen."""
        if (profile_id, version) in self._used:
            raise ProfileFrozen(f"{profile_id} v{version} was used by a run; publish v{version + 1} instead")
        hist = self._versions[profile_id]
        hist[version - 1] = replace(hist[version - 1], **changes)
        return hist[version - 1]

    def get(self, profile_id: str, version: Optional[int] = None) -> CorridorProfile:
        hist = self._versions[profile_id]
        return hist[-1] if version is None else hist[version - 1]

    def latest(self, profile_id: str) -> CorridorProfile:
        return self._versions[profile_id][-1]

    def mark_used(self, profile: CorridorProfile) -> None:
        self._used.add((profile.profile_id, profile.version))

    def find(self, corridor_id: str, university_id: str) -> Optional[CorridorProfile]:
        for hist in self._versions.values():
            p = hist[-1]
            if p.corridor_id == corridor_id and p.university_id == university_id:
                return p
        return None

    def list(self) -> list[dict]:
        return [{**h[-1].header(), "versions": len(h)} for h in self._versions.values()]


def profile_to_dict(p: CorridorProfile, as_of: date = date(2026, 8, 17)) -> dict:
    """Full machine-readable profile (used by the site's payment checker, a port of check_eligibility)."""
    return {**p.header(), "payer_types": list(p.payer_types), "processing_buffer_bd": p.processing_buffer_bd,
            "rules": [{**r.to_dict(as_of), "payer_types": list(r.payer_types)} for r in p.rules],
            "methods": [m.to_dict() for m in p.methods], "sources": [s.to_dict() for s in p.sources],
            "calendar": {"tz": p.calendar.tz, "supported": p.calendar.supported, "note": p.calendar.note,
                         "holidays": sorted(d.isoformat() for d in p.calendar.holidays)}}


# ---------------------------------------------------------- corridor resolver
def resolve_corridor(sender_country: Optional[str], university_country: Optional[str],
                     payer_type: Optional[str]) -> dict:
    """Direction is explicit. Anything the China pack does not cover is NOT_SUPPORTED."""
    s = (sender_country or "").upper().strip()
    u = (university_country or "").upper().strip()
    if not s or not u or not payer_type:
        missing = [n for n, v in (("sender_country", s), ("university_country", u), ("payer_type", payer_type)) if not v]
        return {"status": "NEEDS_INPUT", "corridor_id": None, "reason_codes": ["CORRIDOR_AMBIGUOUS"],
                "question": f"Please provide: {', '.join(missing)}"}
    if s == "CN" and u == "CN":
        return {"status": "NOT_SUPPORTED", "corridor_id": None, "reason_codes": ["CORRIDOR_UNSUPPORTED"],
                "detail": "domestic payment inside mainland China is outside the cross-border pack"}
    if u == "CN":
        return {"status": "RESOLVED", "corridor_id": FROM_ABROAD_TO_CHINA, "reason_codes": [],
                "detail": f"payer in {s} pays a university in mainland China"}
    if s == "CN":
        return {"status": "RESOLVED", "corridor_id": FROM_CHINA_TO_ABROAD, "reason_codes": [],
                "detail": f"payer in mainland China pays a university in {u}"}
    return {"status": "NOT_SUPPORTED", "corridor_id": None, "reason_codes": ["CORRIDOR_UNSUPPORTED"],
            "detail": f"{s} → {u} is not covered by the China-first pack"}


# ----------------------------------------------------------- eligibility gate
@dataclass
class EligibilityRequest:
    corridor_id: str
    university_id: str
    sender_country: str
    payer_type: str
    invoice_currency: str
    reference: Optional[str]
    documents: frozenset             # document keys the user has provided
    instruments: frozenset = frozenset()   # e.g. {"foreign_card", "kzt_account"}
    as_of: date = date(2026, 8, 17)


def _scope_ok(src: Optional[SourceRecord], university_id: str, corridor_id: str, payer_type: str) -> tuple[bool, str]:
    if src is None:
        return False, "no source"
    sc = src.scope
    if sc.get("university_id") not in (None, university_id):
        return False, f"source covers {sc.get('university_id')}, not {university_id}"
    if sc.get("corridor_id") not in (None, corridor_id):
        return False, f"source covers {sc.get('corridor_id')}, not {corridor_id}"
    if sc.get("payer_types") and payer_type not in sc["payer_types"]:
        return False, f"source does not cover payer type {payer_type}"
    return True, ""


def check_method(m: PaymentMethod, p: CorridorProfile, req: EligibilityRequest, docs_ok: bool) -> dict:
    """Deterministic predicates in a fixed order. Returns status + reason codes + trace."""
    trace, reasons = [], []

    def out(status):
        return {"method_id": m.method_id, "name": m.name, "status": status, "reason_codes": reasons,
                "trace": trace, "route_id": m.route_id, "processing_bd": list(m.processing_bd) if m.processing_bd else None,
                "source_id": m.source_id, "caveats": list(m.caveats),
                "data_quality": (p.source(m.source_id).quality if p.source(m.source_id) else "UNKNOWN")}

    if m.corridor_id != req.corridor_id:
        reasons.append("SOURCE_SCOPE_MISMATCH")
        trace.append(f"method belongs to {m.corridor_id}; this payment is {req.corridor_id}")
        return out("INELIGIBLE")
    src = p.source(m.source_id)
    ok, why = _scope_ok(src, req.university_id, req.corridor_id, req.payer_type)
    if not ok:
        reasons.append("SOURCE_SCOPE_MISMATCH" if src else "METHOD_UNKNOWN")
        trace.append(why)
        return out("UNKNOWN")
    trace.append(f"source {src.source_id} covers {req.university_id} / {req.corridor_id}")
    if m.availability == "UNKNOWN":
        reasons.append("METHOD_UNKNOWN")
        trace.append("source does not say whether this method is available")
        return out("UNKNOWN")
    if m.availability == "UNAVAILABLE":
        reasons.append("METHOD_INELIGIBLE")
        trace.append("source says the method is not offered")
        return out("INELIGIBLE")
    if m.checked_at is None or m.valid_until is None:
        reasons.append("DATA_STALE")
        trace.append("method status has no checked_at / valid_until")
        return out("UNKNOWN")
    if req.as_of > m.valid_until:
        reasons.append("RULES_OUTDATED")
        trace.append(f"method status valid until {m.valid_until.isoformat()}, today {req.as_of.isoformat()}")
        return out("STALE")
    if req.payer_type not in m.payer_types:
        reasons.append("METHOD_INELIGIBLE")
        trace.append(f"payer type {req.payer_type} not covered")
        return out("INELIGIBLE")
    if m.currencies and req.invoice_currency not in m.currencies:
        reasons.append("CURRENCY_MISMATCH")
        trace.append(f"method settles {', '.join(m.currencies)}, invoice is {req.invoice_currency}")
        return out("INELIGIBLE")
    if m.requires_instrument and m.requires_instrument not in req.instruments:
        reasons.append("METHOD_INELIGIBLE")
        trace.append(f"needs {m.requires_instrument.replace('_', ' ')}, which the payer has not declared")
        return out("INELIGIBLE")
    if m.reference_supported is None:
        reasons.append("MANUAL_REVIEW_REQUIRED")
        trace.append("unknown whether the method carries the payment reference")
        return out("MANUAL_REVIEW")
    if m.reference_supported is False:
        reasons.append("REFERENCE_MISMATCH")
        trace.append("method cannot carry the payment reference the university requires")
        return out("INELIGIBLE")
    if not req.reference:
        reasons.append("MISSING_DOCUMENTS")
        trace.append("payment reference missing")
        return out("INELIGIBLE")
    if not docs_ok:
        reasons.append("MISSING_DOCUMENTS")
        trace.append("required documents incomplete")
        return out("INELIGIBLE")
    if m.processing_bd is None:
        reasons.append("DATA_INCOMPLETE")
        trace.append("processing time unknown: deadline cannot be checked")
        return out("MANUAL_REVIEW")
    trace.append("all predicates passed within scope and freshness")
    return out("ELIGIBLE")


def check_eligibility(p: CorridorProfile, req: EligibilityRequest, registry: Optional[ProfileRegistry] = None) -> dict:
    """The CIL output contract (spec §5): status, methods, documents, deadlines, sources, trace."""
    if registry is not None:
        registry.mark_used(p)
    if p.corridor_id != req.corridor_id or p.university_id != req.university_id:
        return {"corridor_id": req.corridor_id, "eligibility_status": "NOT_SUPPORTED",
                "reason_codes": ["CORRIDOR_UNSUPPORTED"], "eligible_methods": [], "methods": [],
                "profile_version": None, "decision_trace": ["no profile for this corridor and university"]}
    trace, warnings = [], []
    stale_blocking, missing_docs = [], []
    rules = []
    for r in p.rules:
        if r.payer_types and req.payer_type not in r.payer_types:
            continue
        rd = r.to_dict(req.as_of)
        fresh = r.freshness(req.as_of)
        src = p.source(r.source_id)
        ok, why = _scope_ok(src, req.university_id, req.corridor_id, req.payer_type)
        if not ok:
            rd["scope"] = "SOURCE_SCOPE_MISMATCH"
            warnings.append(f"{r.rule_id}: {why}")
            if r.severity == "BLOCKING":
                stale_blocking.append(r.rule_id)
        elif fresh != "FRESH" and r.severity == "BLOCKING":
            stale_blocking.append(r.rule_id)
            trace.append(f"{r.rule_id} is {fresh}: automatic approval is blocked")
        if r.kind == "DOCUMENT":
            miss = [d for d in r.requires if d not in req.documents]
            rd["missing"] = miss
            if miss and r.severity == "BLOCKING":
                missing_docs.extend(miss)
        rules.append(rd)
    docs_ok = not missing_docs
    methods = [check_method(m, p, req, docs_ok) for m in p.methods]
    eligible = [m for m in methods if m["status"] == "ELIGIBLE"]
    if stale_blocking:
        status, codes = "RULES_OUTDATED", ["RULES_OUTDATED"]
        eligible = []                                  # stale critical rule: nothing is auto-allowed
    elif missing_docs:
        status, codes = "MISSING_DOCUMENTS", ["MISSING_DOCUMENTS"]
    elif eligible:
        status, codes = "READY", []
    elif any(m["status"] in ("MANUAL_REVIEW", "UNKNOWN") for m in methods):
        status, codes = "MANUAL_REVIEW", ["MANUAL_REVIEW_REQUIRED"]
    else:
        status, codes = "NOT_SUPPORTED", ["METHOD_INELIGIBLE"]
    for m in methods:
        trace.append(f"{m['method_id']} {m['status']}: {'; '.join(m['trace'][-1:])}")
    required = sorted({d for r in p.rules if r.kind == "DOCUMENT" and (not r.payer_types or req.payer_type in r.payer_types)
                       for d in r.requires})
    qualities = {s.quality for s in p.sources if s.status != "NOT_VERIFIED"}
    return {"corridor_id": p.corridor_id, "eligibility_status": status, "reason_codes": codes,
            "eligible_methods": [m["method_id"] for m in eligible], "methods": methods,
            "required_documents": [{"document": d, "provided": d in req.documents} for d in required],
            "rules": rules, "warnings": warnings, "profile_version": p.version, "profile_id": p.profile_id,
            "profile_status": p.status, "sources": [s.to_dict() for s in p.sources],
            "data_quality": "SIMULATED" if qualities <= {"SIMULATED"} else sorted(qualities),
            "decision_trace": trace, "as_of": req.as_of.isoformat()}


# --------------------------------------------------------- deadline projection
def project_deadline(p: CorridorProfile, method: PaymentMethod, due: date, as_of: date) -> dict:
    """Latest safe arrival, latest initiation date and expected arrival range. Unknown stays unknown."""
    cal = p.calendar
    out = {"method_id": method.method_id, "due_date": due.isoformat(), "timezone": cal.tz,
           "buffer_bd": p.processing_buffer_bd, "calendar_supported": cal.supported, "caveats": []}
    if not cal.supported or cal.tz is None:
        out.update(status="NEEDS_INPUT", latest_safe_arrival=None, latest_initiation=None, arrival_range=None)
        out["caveats"].append("receiving calendar is not supported: no exact date is shown")
        return out
    bc = BusinessCalendar(tz=cal.tz, holidays=cal.holidays)
    safe = bc.subtract_business_days(bc.roll_forward(due), p.processing_buffer_bd)
    out["latest_safe_arrival"] = safe.isoformat()
    if method.processing_bd is None:
        out.update(status="NEEDS_INPUT", latest_initiation=None, arrival_range=None)
        out["caveats"].append("processing time unknown: initiation date cannot be computed")
        return out
    lo, hi = method.processing_bd
    latest_init = bc.subtract_business_days(safe, hi)
    start = bc.roll_forward(as_of)
    a0, a1 = bc.add_business_days(start, lo), bc.add_business_days(start, hi)
    out.update(latest_initiation=latest_init.isoformat(), arrival_range=[a0.isoformat(), a1.isoformat()],
               status="ON_TIME" if a1 <= safe else "INFEASIBLE_DEADLINE")
    if out["status"] == "INFEASIBLE_DEADLINE":
        out["reason_codes"] = ["INFEASIBLE_DEADLINE"]
    days_left = bc.business_days_between(as_of, latest_init)
    if 0 <= days_left <= 1 and out["status"] == "ON_TIME":
        out["status"] = "AT_RISK"
        out["caveats"].append("initiation window closes within 1 business day: manual review")
    out["caveats"].append("arrival is a range from fixture lead times, not a guarantee")
    return out


# ----------------------------------------------------------------- the pack
FIXTURE_CHECKED = date(2026, 8, 10)
FIXTURE_VALID = date(2026, 12, 31)

# Research starting points from the spec (§6). NOT_VERIFIED: never used to authorise anything.
RESEARCH_SOURCES = (
    SourceRecord("RS-TSINGHUA", "Tsinghua University", "Payment instructions for international students",
                 "https://xfzsf.tsinghua.edu.cn/xsglxt/lxs/wszf_sm.jsp", None,
                 {"university_id": "tsinghua", "corridor_id": FROM_ABROAD_TO_CHINA}, "UNKNOWN", "NOT_VERIFIED"),
    SourceRecord("RS-PKU", "Peking University", "Tuition payment (international students)",
                 "https://isdplus.pku.edu.cn/ORIENTATION/English/UPON_YOUR_ARRIVAL/Tuition_Payment.htm", None,
                 {"university_id": "pku", "corridor_id": FROM_ABROAD_TO_CHINA}, "UNKNOWN", "NOT_VERIFIED"),
    SourceRecord("RS-XJTU", "Xi'an Jiaotong University", "Online payment methods",
                 "https://sie.xjtu.edu.cn/en/info/1141/3022.htm", None,
                 {"university_id": "xjtu", "corridor_id": FROM_ABROAD_TO_CHINA}, "UNKNOWN", "NOT_VERIFIED"),
    SourceRecord("RS-SAFE", "State Administration of Foreign Exchange", "Official site (starting point)",
                 "https://www.safe.gov.cn/en/", None, {"corridor_id": FROM_CHINA_TO_ABROAD}, "UNKNOWN", "NOT_VERIFIED"),
)

CN_HOLIDAYS_DEMO = frozenset(date(2026, 10, d) for d in range(1, 8))   # DEMO FIXTURE, not an official calendar


def _src(sid, title, university_id, corridor_id, payer_types=None):
    return SourceRecord(sid, "Synthetic demo fixture", title, None, FIXTURE_CHECKED,
                        {"university_id": university_id, "corridor_id": corridor_id,
                         **({"payer_types": list(payer_types)} if payer_types else {})},
                        "SIMULATED", "DEMO_FIXTURE")


def inbound_profile() -> CorridorProfile:
    """Kazakhstan → Shenzhen Demo University (CNY). Corridor A."""
    U, C = "demo-university-a", FROM_ABROAD_TO_CHINA
    sources = (_src("DS-A-01", "Demo University A bursar page: payment methods", U, C),
               _src("DS-A-02", "Demo University A: payment reference rules", U, C),
               RESEARCH_SOURCES[0])
    rules = (
        CountryRule("A-REF-01", "REFERENCE", "Student number must appear in the payment reference", "BLOCKING",
                    "DS-A-02", FIXTURE_CHECKED, FIXTURE_VALID),
        CountryRule("A-DOC-01", "DOCUMENT", "Tuition invoice and admission number", "BLOCKING", "DS-A-01",
                    FIXTURE_CHECKED, FIXTURE_VALID, requires=("tuition_invoice", "admission_number")),
    )
    methods = (
        PaymentMethod("A-WIRE", "International wire to the university CNY account", C, U, "CONFIRMED", "DS-A-01",
                      FIXTURE_CHECKED, FIXTURE_VALID, currencies=("CNY",), reference_supported=True,
                      processing_bd=(1, 3), route_id="R3",
                      caveats=("intermediary banks may deduct fees; receiving charges unknown",)),
        PaymentMethod("A-CARD", "University online portal, foreign card", C, U, "CONFIRMED", "DS-A-01",
                      date(2026, 3, 1), date(2026, 6, 30), currencies=("CNY",), reference_supported=True,
                      processing_bd=(0, 2), caveats=("card limits and status update times vary",)),
        PaymentMethod("A-WALLET", "Domestic mobile wallet", C, U, "CONFIRMED", "DS-A-01",
                      FIXTURE_CHECKED, FIXTURE_VALID, currencies=("CNY",), reference_supported=True,
                      requires_instrument="mainland_bank_account", processing_bd=(0, 1)),
        PaymentMethod("A-EDU", "Education payment platform", C, U, "UNKNOWN", "DS-A-01",
                      None, None, currencies=("CNY",)),
        PaymentMethod("A-TSINGHUA", "Method copied from another university's page", C, U, "CONFIRMED",
                      "RS-TSINGHUA", None, None, currencies=("CNY",)),
    )
    cal = CalendarProfile("Asia/Shanghai", CN_HOLIDAYS_DEMO, True, "DS-A-01",
                          "demo holiday list; official calendar not yet loaded")
    return CorridorProfile("cn-in-demo-a", 1, C, "*", "CN", U, "Shenzhen Demo University", "CNY",
                           ("STUDENT", "PARENT_SPONSOR"), rules, methods, sources, cal, 5)


def outbound_profile() -> CorridorProfile:
    """Mainland China → Almaty Demo University (KZT). Corridor B: separate rules, methods, documents."""
    U, C = "demo-university-b", FROM_CHINA_TO_ABROAD
    sources = (_src("DS-B-01", "Demo bank: outbound tuition remittance checklist", U, C),
               _src("DS-B-02", "Demo University B: tuition invoice and reference", U, C),
               RESEARCH_SOURCES[3])
    rules = (
        CountryRule("B-DOC-01", "DOCUMENT", "Admission letter and tuition invoice for the outbound transfer",
                    "BLOCKING", "DS-B-01", FIXTURE_CHECKED, FIXTURE_VALID,
                    requires=("admission_letter", "tuition_invoice")),
        CountryRule("B-DOC-02", "DOCUMENT", "Proof of relationship when a parent or sponsor pays", "BLOCKING",
                    "DS-B-01", FIXTURE_CHECKED, FIXTURE_VALID, requires=("relationship_proof",),
                    payer_types=("PARENT_SPONSOR",)),
        CountryRule("B-REF-01", "REFERENCE", "Invoice number in the payment details", "BLOCKING", "DS-B-02",
                    FIXTURE_CHECKED, FIXTURE_VALID),
    )
    methods = (
        PaymentMethod("B-WIRE", "Outbound bank wire for tuition", C, U, "CONFIRMED", "DS-B-01",
                      FIXTURE_CHECKED, FIXTURE_VALID, currencies=("KZT", "USD"), reference_supported=True,
                      processing_bd=(2, 5), caveats=("bank reviews documents before sending",)),
        PaymentMethod("B-APP", "Cross-border consumer app", C, U, "UNKNOWN", "DS-B-01", None, None,
                      currencies=("KZT",)),
        PaymentMethod("B-CARD", "Pay the university portal by card", C, U, "CONFIRMED", "DS-B-02",
                      FIXTURE_CHECKED, FIXTURE_VALID, currencies=("KZT",), reference_supported=None,
                      processing_bd=(0, 2)),
    )
    cal = CalendarProfile("Asia/Almaty", frozenset(), True, "DS-B-02", "weekends only; holidays not loaded")
    return CorridorProfile("cn-out-demo-b", 1, C, "CN", "KZ", U, "Almaty Demo University", "KZT",
                           ("STUDENT", "PARENT_SPONSOR"), rules, methods, sources, cal, 3)


def china_pack() -> ProfileRegistry:
    reg = ProfileRegistry()
    reg.publish(inbound_profile())
    reg.publish(outbound_profile())
    return reg


# The same conditional invoice in two contexts (spec §6 "mandatory demo moment").
DEMO_CONTEXTS = {
    "A": {"label": "Student in Kazakhstan pays a Chinese university",
          "sender_country": "KZ", "university_country": "CN", "payer_type": "STUDENT",
          "university_id": "demo-university-a", "invoice_currency": "CNY", "amount": "45000.00",
          "due": date(2026, 9, 3), "reference": "SDU-2026-AUT-104233",
          "documents": frozenset({"tuition_invoice", "admission_number"}),
          "instruments": frozenset({"kzt_account", "foreign_card"})},
    "B": {"label": "Parent in China pays a university in Kazakhstan",
          "sender_country": "CN", "university_country": "KZ", "payer_type": "PARENT_SPONSOR",
          "university_id": "demo-university-b", "invoice_currency": "KZT", "amount": "3040540.55",
          "due": date(2026, 9, 3), "reference": "ADU-2026-INV-5531",
          "documents": frozenset({"tuition_invoice", "admission_letter"}),
          "instruments": frozenset({"cny_account"})},
}


def run_context(key: str, registry: Optional[ProfileRegistry] = None, as_of: date = date(2026, 8, 17),
                extra_documents: frozenset = frozenset()) -> dict:
    reg = registry or china_pack()
    c = DEMO_CONTEXTS[key]
    res = resolve_corridor(c["sender_country"], c["university_country"], c["payer_type"])
    out = {"context": key, "label": c["label"], "corridor": res, "simulation": "SIMULATION — NO REAL MONEY MOVES",
           "invoice": {"amount": c["amount"], "currency": c["invoice_currency"], "due": c["due"].isoformat(),
                       "reference": c["reference"], "university_id": c["university_id"]}}
    if res["status"] != "RESOLVED":
        out["eligibility"] = {"eligibility_status": res["status"], "reason_codes": res["reason_codes"]}
        return out
    p = reg.find(res["corridor_id"], c["university_id"])
    if p is None:
        out["eligibility"] = {"eligibility_status": "NOT_SUPPORTED", "reason_codes": ["CORRIDOR_UNSUPPORTED"]}
        return out
    req = EligibilityRequest(res["corridor_id"], c["university_id"], c["sender_country"], c["payer_type"],
                             c["invoice_currency"], c["reference"], c["documents"] | extra_documents,
                             c["instruments"], as_of)
    elig = check_eligibility(p, req, reg)
    out["profile"] = p.header()
    out["eligibility"] = elig
    out["calendar"] = {"tz": p.calendar.tz, "supported": p.calendar.supported, "note": p.calendar.note,
                       "holidays_loaded": len(p.calendar.holidays)}
    out["deadlines"] = [project_deadline(p, m, c["due"], as_of) for m in p.methods
                        if any(x["method_id"] == m.method_id and x["status"] == "ELIGIBLE" for x in elig["methods"])]
    return out
