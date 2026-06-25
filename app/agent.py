"""
Smart Invoice Agent — Multi-Agent Workflow (ADK 2.2.0)
=======================================================
Graph:
  START → security_checkpoint ──PROCEED──► extractor_node
                               └─SECURITY_EVENT─► security_block_node
          extractor_node ──────────────────────► validator_node
          validator_node ──────────────────────► approval_node
          approval_node ──NEEDS_REVIEW──────────► human_review_node
                        └─AUTO_APPROVE──────────► final_output_node
          human_review_node ───────────────────► final_output_node
          security_block_node ─────────────────► final_output_node
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.adk.workflow import START, Edge, Workflow, node
from mcp import StdioServerParameters

from app.config import config

# ─────────────────────────────────────────────────────────────────────────────
# MCP connection params (stdio transport, local mcp_server.py)
# ─────────────────────────────────────────────────────────────────────────────

_MCP_SERVER_PATH = os.path.join(os.path.dirname(__file__), "mcp_server.py")

_MCP_CONNECTION = StdioConnectionParams(
    server_params=StdioServerParameters(
        command=sys.executable,
        args=[_MCP_SERVER_PATH],
        env=None,
    )
)

# ─────────────────────────────────────────────────────────────────────────────
# Security helpers
# ─────────────────────────────────────────────────────────────────────────────

_PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN_REDACTED]"),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[CARD_REDACTED]"),
    (re.compile(r"\b[A-Z]{2}\d{6,9}\b"), "[PASSPORT_REDACTED]"),
    (re.compile(r"\b\d{9,18}\b"), "[BANK_ACCT_REDACTED]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}"), "[EMAIL_REDACTED]"),
]

_INJECTION_KEYWORDS = [
    "ignore previous instructions",
    "disregard all prior",
    "you are now",
    "act as",
    "jailbreak",
    "system prompt",
    "forget everything",
    "new instructions",
    "override",
]


def _scrub_pii(text: str) -> tuple[str, list[str]]:
    redacted: list[str] = []
    for pattern, replacement in _PII_PATTERNS:
        if pattern.search(text):
            redacted.append(replacement)
            text = pattern.sub(replacement, text)
    return text, redacted


def _detect_injection(text: str) -> list[str]:
    lower = text.lower()
    return [kw for kw in _INJECTION_KEYWORDS if kw in lower]


def _audit_log(event_type: str, severity: str, details: dict) -> None:
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "event": event_type,
        "severity": severity,
        "details": details,
    }
    print(f"[AUDIT] {json.dumps(entry)}")


async def _run_node_with_retry(ctx: Context, node, *args, **kwargs):
    import asyncio
    import re
    max_retries = 5
    delay = 5.0
    for attempt in range(max_retries):
        try:
            return await ctx.run_node(node, *args, **kwargs)
        except Exception as e:
            err_msg = str(e).lower()
            if any(kwd in err_msg for kwd in ["quota", "exhausted", "429", "rate limit", "resource_exhausted"]):
                if attempt == max_retries - 1:
                    _audit_log("RATE_LIMIT_FAILED", "ERROR", {"error": str(e)})
                    raise
                match = re.search(r"retry in ([\d\.]+)s", str(e), re.IGNORECASE)
                if not match:
                    match = re.search(r"retry after ([\d\.]+)s", str(e), re.IGNORECASE)
                if not match:
                    match = re.search(r"retry delay':\s*'([\d\.]+)s'", str(e), re.IGNORECASE)
                
                wait_time = float(match.group(1)) + 1.0 if match else delay
                _audit_log("RATE_LIMIT_HIT", "WARNING", {
                    "attempt": attempt + 1,
                    "wait_seconds": wait_time,
                    "error": str(e)[:250]
                })
                print(f"[RETRY] Rate limit hit. Waiting {wait_time:.2f}s before retrying (attempt {attempt + 1}/{max_retries})...")
                await asyncio.sleep(wait_time)
                delay *= 2
            else:
                raise


# ─────────────────────────────────────────────────────────────────────────────
# Workflow Function Nodes  (receive ctx: Context; return route string)
# ─────────────────────────────────────────────────────────────────────────────

@node
async def security_checkpoint(ctx: Context) -> Event:
    """Scrubs PII, detects prompt injection, emits audit log. Returns route."""
    # Retrieve raw input from state or context user content
    raw_input = ctx.state.get("invoice_raw", "")
    if not raw_input and ctx.user_content and ctx.user_content.parts:
        raw_input = "".join(part.text for part in ctx.user_content.parts if part.text)
        ctx.state["invoice_raw"] = raw_input

    cleaned, redacted_fields = _scrub_pii(raw_input)
    if redacted_fields:
        ctx.state["invoice_raw"] = cleaned
        _audit_log("PII_REDACTED", "WARNING", {
            "fields_redacted": redacted_fields,
            "invoice_snippet": cleaned[:120],
        })

    injections = _detect_injection(raw_input)
    if injections:
        _audit_log("INJECTION_DETECTED", "CRITICAL", {
            "keywords_found": injections,
            "input_snippet": raw_input[:120],
        })
        ctx.state["security_block_reason"] = (
            f"Injection keywords detected: {injections}"
        )
        return Event(route="SECURITY_EVENT")

    if not re.search(r"\$[\d,]+|\d+\.\d{2}", raw_input):
        _audit_log("INVALID_INVOICE_FORMAT", "WARNING", {
            "reason": "No monetary amount found in input",
        })

    _audit_log("SECURITY_CHECK_PASSED", "INFO", {
        "pii_redacted": bool(redacted_fields),
    })
    return Event(route="PROCEED")


@node(rerun_on_resume=True)
async def extractor_node(ctx: Context) -> str:
    """Run ExtractorAgent (via ctx.run_node) with MCP tools."""
    invoice_raw = ctx.state.get("invoice_raw", "")

    mcp_tools = McpToolset(connection_params=_MCP_CONNECTION)
    try:
        agent = LlmAgent(
            name="ExtractorAgent",
            model=config.model,
            instruction=(
                "You are an invoice data extraction specialist.\n"
                "Given raw invoice text, use the 'parse_invoice' MCP tool to extract "
                "structured fields: vendor_name, invoice_number, invoice_date, due_date, "
                "po_number, line_items (list), subtotal, tax, total_amount, currency.\n"
                "Return ONLY a valid JSON object with these fields. "
                "If a field is missing, use null. Never fabricate data."
            ),
            tools=await mcp_tools.get_tools(),
        )
        extracted_text = await _run_node_with_retry(
            ctx,
            agent,
            f"Extract all invoice data from this text:\n\n{invoice_raw}",
        )
    finally:
        await mcp_tools.close()

    extracted_text = extracted_text or ""
    try:
        clean = re.sub(r"```(?:json)?|```", "", str(extracted_text)).strip()
        ctx.state["invoice_extracted"] = json.loads(clean)
    except (json.JSONDecodeError, Exception):
        ctx.state["invoice_extracted"] = {"raw_extraction": str(extracted_text)}

    _audit_log("EXTRACTION_COMPLETE", "INFO", {
        "fields": list(ctx.state["invoice_extracted"].keys()),
    })
    return "EXTRACTED"


@node(rerun_on_resume=True)
async def validator_node(ctx: Context) -> str:
    """Run ValidatorAgent (via ctx.run_node) with MCP tools."""
    extracted = ctx.state.get("invoice_extracted", {})

    mcp_tools = McpToolset(connection_params=_MCP_CONNECTION)
    try:
        agent = LlmAgent(
            name="ValidatorAgent",
            model=config.model,
            instruction=(
                "You are an invoice validation specialist.\n"
                "Given extracted invoice data (JSON), use 'lookup_purchase_order' to "
                "fetch the matching PO, then 'validate_invoice_against_po' to check:\n"
                "- PO number match\n- Amount within tolerance (±5%)\n"
                "- Vendor name match\n- Due date is not past-due.\n"
                'Return JSON: {"valid": bool, "issues": [...], '
                '"risk_level": "LOW"|"MEDIUM"|"HIGH"}'
            ),
            tools=await mcp_tools.get_tools(),
        )
        validation_text = await _run_node_with_retry(
            ctx,
            agent,
            f"Validate this extracted invoice data:\n\n"
            f"{json.dumps(extracted, indent=2)}",
        )
    finally:
        await mcp_tools.close()

    validation_text = validation_text or ""
    try:
        clean = re.sub(r"```(?:json)?|```", "", str(validation_text)).strip()
        ctx.state["validation_result"] = json.loads(clean)
    except (json.JSONDecodeError, Exception):
        ctx.state["validation_result"] = {
            "raw_validation": str(validation_text),
            "valid": False,
            "risk_level": "HIGH",
        }

    risk = ctx.state["validation_result"].get("risk_level", "UNKNOWN")
    _audit_log("VALIDATION_COMPLETE", "INFO", {
        "risk_level": risk,
        "valid": ctx.state["validation_result"].get("valid"),
    })
    return "VALIDATED"


@node(rerun_on_resume=True)
async def approval_node(ctx: Context) -> Event:
    """Run ApprovalAgent (via ctx.run_node) — returns AUTO_APPROVE or NEEDS_REVIEW."""
    validation = ctx.state.get("validation_result", {})
    extracted = ctx.state.get("invoice_extracted", {})

    agent = LlmAgent(
        name="ApprovalAgent",
        model=config.model,
        instruction=(
            "You are an invoice approval decision agent.\n"
            "Given the validation result JSON, apply these rules:\n"
            '- risk_level == "LOW" and valid == true → AUTO_APPROVE\n'
            '- risk_level == "MEDIUM" → NEEDS_REVIEW\n'
            '- risk_level == "HIGH" or valid == false → NEEDS_REVIEW\n'
            "- total_amount > 50000 → always NEEDS_REVIEW\n"
            'Return ONLY JSON: {"decision": "AUTO_APPROVE"|"NEEDS_REVIEW", "reason": "..."}'
        ),
    )
    payload = {
        "validation": validation,
        "total_amount": extracted.get("total_amount"),
        "currency": extracted.get("currency", "USD"),
    }
    decision_text = await _run_node_with_retry(
        ctx,
        agent,
        f"Make an approval decision for:\n\n{json.dumps(payload, indent=2)}",
    )

    decision_text = decision_text or ""
    try:
        clean = re.sub(r"```(?:json)?|```", "", str(decision_text)).strip()
        ctx.state["approval_decision"] = json.loads(clean)
    except (json.JSONDecodeError, Exception):
        ctx.state["approval_decision"] = {
            "decision": "NEEDS_REVIEW",
            "reason": str(decision_text),
        }

    decision = ctx.state["approval_decision"].get("decision", "NEEDS_REVIEW")
    _audit_log("APPROVAL_DECISION", "INFO", {
        "decision": decision,
        "reason": ctx.state["approval_decision"].get("reason"),
    })
    return Event(route=decision)


@node(rerun_on_resume=True)
async def human_review_node(ctx: Context):
    """HITL: pause and ask the human reviewer to APPROVE or REJECT."""
    from google.adk.events.request_input import RequestInput

    extracted = ctx.state.get("invoice_extracted", {})
    validation = ctx.state.get("validation_result", {})
    approval = ctx.state.get("approval_decision", {})

    summary = (
        "INVOICE REVIEW REQUIRED\n"
        f"Vendor: {extracted.get('vendor_name', 'Unknown')}\n"
        f"Amount: {extracted.get('total_amount')} {extracted.get('currency', 'USD')}\n"
        f"PO#: {extracted.get('po_number', 'N/A')}\n"
        f"Issues: {', '.join(validation.get('issues', [])) or 'None'}\n"
        f"Risk: {validation.get('risk_level', 'UNKNOWN')}\n"
        f"Agent Reason: {approval.get('reason', '')}\n\n"
        "Type APPROVE or REJECT:"
    )

    # First pass: yield RequestInput to pause the workflow and prompt the user.
    # On resume, rerun_on_resume=True causes this node to run again with
    # ctx.resume_inputs populated — we then read the human's answer.
    if "human_review" not in ctx.resume_inputs:
        yield RequestInput(interrupt_id="human_review", message=summary)
        return

    human_response = ctx.resume_inputs.get("human_review", "")
    ctx.state["human_decision"] = str(human_response).strip().upper()
    _audit_log("HUMAN_REVIEW_COMPLETE", "INFO", {
        "human_decision": ctx.state["human_decision"],
        "vendor": extracted.get("vendor_name"),
    })
    from google.adk.events.event import Event as _Event
    yield _Event(output="REVIEWED")


@node
async def security_block_node(ctx: Context) -> str:
    """Terminal node: record BLOCKED result."""
    reason = ctx.state.get("security_block_reason", "Security policy violation")
    ctx.state["final_result"] = {
        "status": "BLOCKED",
        "reason": reason,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    }
    _audit_log("INVOICE_BLOCKED", "CRITICAL", {"reason": reason})
    return "DONE"


@node
async def final_output_node(ctx: Context) -> str:
    """Assembles and prints the structured final result."""
    extracted = ctx.state.get("invoice_extracted", {})
    validation = ctx.state.get("validation_result", {})
    approval = ctx.state.get("approval_decision", {})
    human_decision = ctx.state.get("human_decision")

    if human_decision:
        status = "APPROVED" if human_decision == "APPROVE" else "REJECTED"
        final_decision = f"HUMAN_{status}"
    else:
        final_decision = approval.get("decision", "AUTO_APPROVE")

    ctx.state["final_result"] = {
        "status": final_decision,
        "vendor": extracted.get("vendor_name"),
        "invoice_number": extracted.get("invoice_number"),
        "amount": extracted.get("total_amount"),
        "currency": extracted.get("currency", "USD"),
        "risk_level": validation.get("risk_level"),
        "issues": validation.get("issues", []),
        "reason": approval.get("reason", ""),
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    }

    _audit_log("INVOICE_PROCESSED", "INFO", ctx.state["final_result"])
    print(f"\n{'='*60}")
    print(f"INVOICE RESULT: {json.dumps(ctx.state['final_result'], indent=2)}")
    print(f"{'='*60}\n")
    return "DONE"


# ─────────────────────────────────────────────────────────────────────────────
# Workflow Graph  (ADK 2.2.0 Edge-based definition)
# ─────────────────────────────────────────────────────────────────────────────

root_agent = Workflow(
    name="InvoiceProcessingWorkflow",
    description=(
        "Processes invoices through security checks, extraction, validation, "
        "approval, and optional human review."
    ),
    edges=[
        # START → security_checkpoint (unconditional)
        (START, security_checkpoint),

        # security_checkpoint → conditional fan-out
        (security_checkpoint, {
            "PROCEED": extractor_node,
            "SECURITY_EVENT": security_block_node,
        }),

        # linear pipeline
        (extractor_node, validator_node),
        (validator_node, approval_node),

        # approval → conditional fan-out
        (approval_node, {
            "AUTO_APPROVE": final_output_node,
            "NEEDS_REVIEW": human_review_node,
        }),

        # converge on final_output_node
        (human_review_node, final_output_node),
        (security_block_node, final_output_node),
    ],
)

# ADK web / playground also looks for `app` as an alias
app = root_agent
