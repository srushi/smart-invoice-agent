"""
Smart Invoice Agent — MCP Server
=================================
Exposes 5 invoice-domain tools over stdio transport:

  1. parse_invoice             — Extracts structured fields from raw invoice text
  2. lookup_purchase_order     — Retrieves a PO record by PO number (mock registry)
  3. validate_invoice_against_po — Cross-checks invoice vs PO for discrepancies
  4. calculate_payment_terms   — Computes due date, early-pay discount, late fee
  5. get_vendor_risk_profile   — Returns vendor payment history & risk score
"""

import json
import re
import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("smart-invoice-mcp")

# ─────────────────────────────────────────────────────────────────────────────
# Mock data stores (replace with real DB/API calls in production)
# ─────────────────────────────────────────────────────────────────────────────

_PURCHASE_ORDERS: dict[str, dict] = {
    "PO-2024-001": {
        "po_number": "PO-2024-001",
        "vendor_name": "Acme Supplies Ltd",
        "approved_amount": 12500.00,
        "currency": "USD",
        "issue_date": "2024-01-10",
        "expiry_date": "2024-12-31",
        "line_items": [
            {"description": "Office Furniture", "quantity": 10, "unit_price": 850.00},
            {"description": "Delivery & Installation", "quantity": 1, "unit_price": 3500.00},
        ],
        "status": "OPEN",
    },
    "PO-2024-002": {
        "po_number": "PO-2024-002",
        "vendor_name": "TechCore Solutions",
        "approved_amount": 48000.00,
        "currency": "USD",
        "issue_date": "2024-02-01",
        "expiry_date": "2024-06-30",
        "line_items": [
            {"description": "Software Licenses (annual)", "quantity": 50, "unit_price": 960.00},
        ],
        "status": "OPEN",
    },
    "PO-2024-003": {
        "po_number": "PO-2024-003",
        "vendor_name": "Global Freight Co",
        "approved_amount": 7200.00,
        "currency": "USD",
        "issue_date": "2024-03-15",
        "expiry_date": "2024-09-15",
        "line_items": [
            {"description": "Freight & Logistics Q2", "quantity": 1, "unit_price": 7200.00},
        ],
        "status": "OPEN",
    },
}

_VENDOR_PROFILES: dict[str, dict] = {
    "acme supplies ltd": {
        "vendor_name": "Acme Supplies Ltd",
        "payment_history_score": 92,
        "average_days_to_pay": 28,
        "disputes_last_12m": 0,
        "risk_level": "LOW",
        "preferred_vendor": True,
    },
    "techcore solutions": {
        "vendor_name": "TechCore Solutions",
        "payment_history_score": 78,
        "average_days_to_pay": 45,
        "disputes_last_12m": 2,
        "risk_level": "MEDIUM",
        "preferred_vendor": False,
    },
    "global freight co": {
        "vendor_name": "Global Freight Co",
        "payment_history_score": 65,
        "average_days_to_pay": 60,
        "disputes_last_12m": 4,
        "risk_level": "HIGH",
        "preferred_vendor": False,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — parse_invoice
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def parse_invoice(invoice_text: str) -> dict[str, Any]:
    """
    Extract structured fields from raw invoice text using pattern matching.

    Args:
        invoice_text: The raw text of the invoice document.

    Returns:
        A dict with vendor_name, invoice_number, invoice_date, due_date,
        po_number, line_items, subtotal, tax, total_amount, currency.
    """
    result: dict[str, Any] = {
        "vendor_name": None,
        "invoice_number": None,
        "invoice_date": None,
        "due_date": None,
        "po_number": None,
        "line_items": [],
        "subtotal": None,
        "tax": None,
        "total_amount": None,
        "currency": "USD",
    }

    # Invoice number
    m = re.search(r"(?i)invoice\s*#?\s*:?\s*([A-Z0-9\-]+)", invoice_text)
    if m:
        result["invoice_number"] = m.group(1).strip()

    # PO number
    m = re.search(r"(?i)(?:PO|purchase\s*order)\s*#?\s*:?\s*([A-Z0-9\-]+)", invoice_text)
    if m:
        result["po_number"] = m.group(1).strip()

    # Dates
    date_pattern = re.compile(r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
    dates = date_pattern.findall(invoice_text)
    if dates:
        result["invoice_date"] = dates[0]
    if len(dates) > 1:
        result["due_date"] = dates[1]

    # Currency
    if "$" in invoice_text:
        result["currency"] = "USD"
    elif "€" in invoice_text or "EUR" in invoice_text:
        result["currency"] = "EUR"
    elif "£" in invoice_text or "GBP" in invoice_text:
        result["currency"] = "GBP"

    # Amounts — grab all dollar amounts, use the largest as total
    amounts = [float(a.replace(",", "")) for a in re.findall(r"\$?([\d,]+\.\d{2})", invoice_text)]
    if amounts:
        result["total_amount"] = max(amounts)
        if len(amounts) >= 2:
            result["subtotal"] = sorted(amounts)[-2] if len(amounts) > 1 else amounts[0]

    # Vendor name heuristic — first non-empty line often contains vendor
    lines = [l.strip() for l in invoice_text.strip().splitlines() if l.strip()]
    if lines:
        result["vendor_name"] = lines[0]

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — lookup_purchase_order
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def lookup_purchase_order(po_number: str) -> dict[str, Any]:
    """
    Retrieve a Purchase Order record by PO number.

    Args:
        po_number: The PO number to look up (e.g. 'PO-2024-001').

    Returns:
        The full PO record dict, or an error dict if not found.
    """
    po = _PURCHASE_ORDERS.get(po_number.strip().upper())
    if po:
        return {"found": True, "po": po}
    return {
        "found": False,
        "po_number": po_number,
        "error": f"No PO found with number '{po_number}'",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — validate_invoice_against_po
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def validate_invoice_against_po(
    invoice_data: str,
    po_data: str,
    amount_tolerance_pct: float = 5.0,
) -> dict[str, Any]:
    """
    Cross-check an extracted invoice against its corresponding PO.

    Args:
        invoice_data: JSON string of extracted invoice fields.
        po_data:      JSON string of PO record (from lookup_purchase_order).
        amount_tolerance_pct: Allowable percentage variance in amount (default 5%).

    Returns:
        {valid: bool, issues: [...], risk_level: "LOW"|"MEDIUM"|"HIGH"}
    """
    try:
        inv = json.loads(invoice_data) if isinstance(invoice_data, str) else invoice_data
        po = json.loads(po_data) if isinstance(po_data, str) else po_data
        if "po" in po:
            po = po["po"]
    except (json.JSONDecodeError, KeyError) as e:
        return {"valid": False, "issues": [f"Parse error: {e}"], "risk_level": "HIGH"}

    issues = []

    # Vendor name check
    inv_vendor = (inv.get("vendor_name") or "").lower().strip()
    po_vendor = (po.get("vendor_name") or "").lower().strip()
    if inv_vendor and po_vendor and inv_vendor not in po_vendor and po_vendor not in inv_vendor:
        issues.append(f"Vendor mismatch: invoice='{inv_vendor}' vs PO='{po_vendor}'")

    # Amount check
    inv_amount = inv.get("total_amount")
    po_amount = po.get("approved_amount")
    if inv_amount is not None and po_amount is not None:
        variance_pct = abs(inv_amount - po_amount) / po_amount * 100
        if variance_pct > amount_tolerance_pct:
            issues.append(
                f"Amount variance {variance_pct:.1f}% exceeds tolerance {amount_tolerance_pct}%: "
                f"invoice={inv_amount}, PO={po_amount}"
            )

    # Due date check
    due_date_str = inv.get("due_date")
    if due_date_str:
        try:
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
                try:
                    due_date = datetime.datetime.strptime(due_date_str, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                due_date = None
            if due_date and due_date < datetime.date.today():
                issues.append(f"Invoice is past-due (due: {due_date_str})")
        except Exception:
            pass

    # PO status check
    if po.get("status") != "OPEN":
        issues.append(f"PO status is '{po.get('status')}' — not OPEN")

    # Risk level
    if len(issues) == 0:
        risk_level = "LOW"
    elif len(issues) == 1:
        risk_level = "MEDIUM"
    else:
        risk_level = "HIGH"

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "risk_level": risk_level,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool 4 — calculate_payment_terms
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def calculate_payment_terms(
    invoice_date: str,
    total_amount: float,
    net_days: int = 30,
    early_pay_discount_pct: float = 2.0,
    early_pay_days: int = 10,
    late_fee_pct: float = 1.5,
) -> dict[str, Any]:
    """
    Compute payment schedule: due date, early-pay discount, and late fee.

    Args:
        invoice_date:           Invoice issue date (YYYY-MM-DD or MM/DD/YYYY).
        total_amount:           Invoice total in base currency.
        net_days:               Standard net payment days (default 30).
        early_pay_discount_pct: Discount % if paid within early_pay_days (default 2%).
        early_pay_days:         Days within which early discount applies (default 10).
        late_fee_pct:           Monthly late fee % after due date (default 1.5%).

    Returns:
        Dict with due_date, early_pay_deadline, early_pay_amount, late_fee_per_month.
    """
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y"):
        try:
            inv_date = datetime.datetime.strptime(invoice_date, fmt).date()
            break
        except ValueError:
            continue
    else:
        return {"error": f"Cannot parse invoice_date: '{invoice_date}'"}

    due_date = inv_date + datetime.timedelta(days=net_days)
    early_deadline = inv_date + datetime.timedelta(days=early_pay_days)
    early_amount = round(total_amount * (1 - early_pay_discount_pct / 100), 2)
    late_fee = round(total_amount * late_fee_pct / 100, 2)

    return {
        "invoice_date": str(inv_date),
        "due_date": str(due_date),
        "net_days": net_days,
        "early_pay_deadline": str(early_deadline),
        "early_pay_discount_pct": early_pay_discount_pct,
        "early_pay_amount": early_amount,
        "late_fee_per_month": late_fee,
        "is_overdue": datetime.date.today() > due_date,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool 5 — get_vendor_risk_profile
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_vendor_risk_profile(vendor_name: str) -> dict[str, Any]:
    """
    Retrieve payment history and risk profile for a vendor.

    Args:
        vendor_name: Name of the vendor to look up.

    Returns:
        Dict with payment_history_score, risk_level, disputes, preferred_vendor flag.
    """
    key = vendor_name.lower().strip()
    profile = _VENDOR_PROFILES.get(key)
    if profile:
        return {"found": True, "profile": profile}

    # Fuzzy fallback: partial match
    for k, v in _VENDOR_PROFILES.items():
        if k in key or key in k:
            return {"found": True, "profile": v, "note": "Partial name match"}

    return {
        "found": False,
        "vendor_name": vendor_name,
        "profile": {
            "payment_history_score": 50,
            "risk_level": "MEDIUM",
            "disputes_last_12m": 0,
            "preferred_vendor": False,
        },
        "note": "Vendor not in registry — defaulting to MEDIUM risk",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
