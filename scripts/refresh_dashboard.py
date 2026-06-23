#!/usr/bin/env python3
"""Gmail Dashboard Refresh Script.
Fetches emails via Gmail API, categorizes with Anthropic Claude, generates HTML dashboard.
Runs in GitHub Actions or locally."""

import os
import json
import sys
import html as html_module
import base64
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import anthropic

# --- Config ---
USER_ID = "me"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
TEMPLATE_PATH = os.path.join(REPO_DIR, "assets", "dashboard_template.html")
OUTPUT_PATH = os.path.join(REPO_DIR, "index.html")
DELETE_PATH = os.path.join(REPO_DIR, "commands", "delete.json")
REFRESH_PATH = os.path.join(REPO_DIR, "commands", "refresh.json")

# Email queries
QUERIES = {
    "important": "is:unread in:inbox -category:promotions -category:social -category:forums newer_than:30d",
    "important_flagged": "is:unread is:important newer_than:30d",
    "followup": "is:read in:inbox -category:promotions -category:social -category:forums newer_than:14d",
    "school": "(from:parentsquare OR from:cusdk8 OR from:stocklmeir OR subject:stocklmeir OR subject:CUSD OR subject:PTA) newer_than:7d",
    "promos": "category:promotions newer_than:3d",
    "social": "category:social newer_than:3d",
    "delete_promos": "category:promotions older_than:60d",
    "delete_social": "category:social older_than:60d",
    "delete_peachjar": "from:peachjar newer_than:60d",
}

CATEGORIZATION_PROMPT = """You are categorizing emails for Ido's personal dashboard. Context about Ido:
- Parent of Ella (9, entering 4th grade) and Emmie (6.5, entering 1st grade) at Stocklmeir Elementary, Cupertino Union School District (CUSD). School uses ParentSquare.
- Interested in camping, RV gear, vacation deals. Tracks via Slickdeals and CamelCamelCamel.
- Recently left Intuit. Active in BAJC (Bay Area Jewish community org).

Categorize each email below. Return a JSON array where each item has:
- "id": the thread_id
- "cat": one of "important", "followup", "school_ella", "school_emmie", "school_general", "promo", "deletion", "skip"
- "sum": one-line summary (what it is + what action needed, if any)
- "urg": "urgent", "action", "info", or "low"
- "tag": "PAY", "REVIEW", "RESPOND", "SIGN", "SAVE", "FYI", or null

RULES:
- important: UNREAD emails needing attention (bills, insurance, appointments, legal, financial, health, personal messages)
- followup: READ emails where someone is waiting on Ido or he needs to act (pending responses, unsigned docs, unpaid bills)
- school_ella: About Ella specifically (her teacher, her class, her grade)
- school_emmie: About Emmie specifically
- school_general: School-wide, district, PTA — NOT Peachjar flyers
- promo: ONLY these types: camping/RV/outdoor gear deals, Slickdeals alerts, CamelCamelCamel alerts, specific tracked travel routes (like Google Flights price alerts), LinkedIn direct messages, LinkedIn connection requests
- deletion: Peachjar flyer emails, generic hotel/travel promos (Expedia "save X%"), LinkedIn games/puzzles/newsletters/profile views, anything clearly not useful
- skip: Newsletters, routine notifications, already-handled items — don't show on dashboard

Be selective for promos — only surface deals Ido would actually care about. When in doubt about school emails, use school_general.

EMAILS:
"""


def get_gmail_service():
    """Create Gmail API service using OAuth credentials."""
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    return build("gmail", "v1", credentials=creds)


def search_threads(service, query, max_results=20):
    """Search Gmail threads, return list of thread stubs."""
    try:
        results = (
            service.users()
            .threads()
            .list(userId=USER_ID, q=query, maxResults=max_results)
            .execute()
        )
        return results.get("threads", [])
    except Exception as e:
        print(f"  Warning: Query failed: {e}")
        return []


def get_thread_detail(service, thread_id):
    """Get thread metadata."""
    try:
        thread = (
            service.users()
            .threads()
            .get(
                userId=USER_ID,
                id=thread_id,
                format="metadata",
                metadataHeaders=["Subject", "From", "Date", "To"],
            )
            .execute()
        )
        return thread
    except Exception as e:
        print(f"  Warning: Could not fetch thread {thread_id}: {e}")
        return None


def parse_sender(from_header):
    """Extract clean sender name from From header."""
    if not from_header:
        return "Unknown"
    # "John Doe <john@example.com>" -> "John Doe"
    match = re.match(r'"?([^"<]+)"?\s*<', from_header)
    if match:
        return match.group(1).strip()
    return from_header.split("@")[0] if "@" in from_header else from_header


def parse_date(date_header):
    """Parse email date to readable format."""
    if not date_header:
        return ""
    try:
        dt = parsedate_to_datetime(date_header)
        now = datetime.now(timezone.utc)
        if dt.date() == now.date():
            return dt.strftime("Today %I:%M %p")
        elif (now - dt).days < 7:
            return dt.strftime("%a %b %d")
        else:
            return dt.strftime("%b %d")
    except Exception:
        return date_header[:10] if len(date_header) > 10 else date_header


def extract_email_info(thread):
    """Extract structured info from a thread."""
    messages = thread.get("messages", [])
    if not messages:
        return None

    msg = messages[-1]  # Latest message
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    labels = msg.get("labelIds", [])

    return {
        "thread_id": thread["id"],
        "subject": headers.get("Subject", "(no subject)"),
        "sender": parse_sender(headers.get("From", "")),
        "sender_email": headers.get("From", ""),
        "date": parse_date(headers.get("Date", "")),
        "date_raw": headers.get("Date", ""),
        "snippet": thread.get("snippet", ""),
        "is_unread": "UNREAD" in labels,
        "labels": labels,
    }


# --- Deletion Processing ---
def process_deletions(service):
    """Trash threads listed in commands/delete.json."""
    if not os.path.exists(DELETE_PATH):
        return 0

    print("Processing pending deletions...")
    with open(DELETE_PATH) as f:
        thread_ids = json.load(f)

    trashed = 0
    for tid in thread_ids:
        try:
            service.users().threads().modify(
                userId=USER_ID,
                id=tid,
                body={"addLabelIds": ["TRASH"]},
            ).execute()
            trashed += 1
        except Exception as e:
            print(f"  Failed to trash {tid}: {e}")

    print(f"  Trashed {trashed}/{len(thread_ids)} threads")
    os.remove(DELETE_PATH)
    return trashed


def clear_refresh_marker():
    """Remove the refresh command file."""
    if os.path.exists(REFRESH_PATH):
        os.remove(REFRESH_PATH)


# --- Email Fetching ---
def fetch_all_emails(service):
    """Fetch emails from all query categories."""
    all_emails = {}  # thread_id -> email_info + source

    for source, query in QUERIES.items():
        print(f"  Searching: {source}")
        max_r = 50 if source.startswith("delete_") else 20
        threads = search_threads(service, query, max_results=max_r)
        print(f"    Found {len(threads)} threads")

        for t in threads:
            tid = t["id"]
            if tid not in all_emails:
                detail = get_thread_detail(service, tid)
                if detail:
                    info = extract_email_info(detail)
                    if info:
                        info["sources"] = [source]
                        all_emails[tid] = info
            else:
                all_emails[tid]["sources"].append(source)

    print(f"  Total unique threads: {len(all_emails)}")
    return all_emails


# --- AI Categorization ---
def auto_categorize(emails):
    """Pre-categorize obvious emails without AI to save tokens."""
    needs_ai = []
    auto_results = []

    for email in emails.values():
        sources = email.get("sources", [])
        sender_lower = email.get("sender_email", "").lower()
        subject_lower = email.get("subject", "").lower()

        # Auto-deletion: old promos, old social, peachjar
        if "delete_promos" in sources or "delete_social" in sources:
            auto_results.append({
                "id": email["thread_id"],
                "cat": "deletion",
                "sum": email["snippet"][:80],
                "urg": "low",
                "tag": None,
            })
        elif "delete_peachjar" in sources or "peachjar" in sender_lower:
            auto_results.append({
                "id": email["thread_id"],
                "cat": "deletion",
                "sum": f"Peachjar flyer: {email['subject'][:60]}",
                "urg": "low",
                "tag": None,
            })
        else:
            needs_ai.append(email)

    return needs_ai, auto_results


def categorize_with_claude(emails_needing_ai):
    """Send emails to Claude for categorization."""
    if not emails_needing_ai:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Build email list for the prompt
    email_lines = []
    for e in emails_needing_ai:
        status = "UNREAD" if e["is_unread"] else "READ"
        sources = ", ".join(e["sources"])
        email_lines.append(
            f'- id: "{e["thread_id"]}" | status: {status} | sources: {sources} | '
            f'from: {e["sender"]} <{e["sender_email"]}> | '
            f'subject: {e["subject"]} | snippet: {e["snippet"][:120]}'
        )

    prompt = CATEGORIZATION_PROMPT + "\n".join(email_lines)

    print(f"  Sending {len(emails_needing_ai)} emails to Claude for categorization...")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    # Parse JSON from response
    text = response.content[0].text
    # Extract JSON array from response (handle markdown code blocks)
    json_match = re.search(r"\[[\s\S]*\]", text)
    if json_match:
        try:
            results = json.loads(json_match.group())
            print(f"  Claude categorized {len(results)} emails")
            return results
        except json.JSONDecodeError as e:
            print(f"  Warning: Failed to parse Claude response: {e}")
            return []
    else:
        print("  Warning: No JSON array found in Claude response")
        return []


# --- HTML Generation ---
def render_email_item(email_info, cat_info, show_done_check=False, show_trash_btn=False, show_delete_check=False):
    """Render a single email as HTML."""
    tid = html_module.escape(email_info["thread_id"])
    sender = html_module.escape(email_info["sender"])
    subject = html_module.escape(email_info["subject"][:80])
    summary = html_module.escape(cat_info.get("sum", email_info["snippet"][:100]))
    date = html_module.escape(email_info["date"])
    urgency = cat_info.get("urg", "info")
    tag = cat_info.get("tag")

    parts = []
    parts.append('<div class="email-item">')

    if show_done_check:
        parts.append(f'    <input type="checkbox" class="done-check" data-thread-id="{tid}" onchange="handleDoneCheck(this)">')

    if show_delete_check:
        parts.append(f'    <input type="checkbox" class="delete-check" data-thread-id="{tid}">')

    if not show_delete_check:
        parts.append(f'    <div class="urgency-dot {urgency}"></div>')

    parts.append('    <div class="email-content">')
    parts.append('        <div class="email-top">')
    parts.append(f'            <span class="email-sender">{sender}</span>')
    parts.append(f'            <span class="email-date">')
    if show_trash_btn:
        parts.append(f'                <button class="trash-btn" onclick="trashSingleEmail(this, \'{tid}\')" title="Move to trash">&#128465;</button>')
    parts.append(f'                {date}')
    parts.append(f'            </span>')
    parts.append('        </div>')
    parts.append(f'        <div class="email-subject">{subject}</div>')
    parts.append(f'        <div class="email-summary">{summary}</div>')
    if tag:
        tag_class = {"PAY": "deadline", "REVIEW": "review", "RESPOND": "respond", "SIGN": "deadline", "SAVE": "review", "FYI": "fyi"}.get(tag, "fyi")
        parts.append(f'        <span class="email-action-tag {tag_class}">{tag}</span>')
    parts.append('    </div>')
    parts.append('</div>')

    return "\n".join(parts)


def generate_dashboard(all_emails, categorization_results):
    """Generate the full HTML dashboard."""
    # Build lookup: thread_id -> categorization
    cat_lookup = {r["id"]: r for r in categorization_results}

    # Sort emails into sections
    sections = {
        "important": [],
        "followup": [],
        "school_ella": [],
        "school_emmie": [],
        "school_general": [],
        "promo": [],
        "deletion": [],
    }

    for tid, email in all_emails.items():
        cat = cat_lookup.get(tid, {})
        category = cat.get("cat", "skip")
        if category in sections:
            sections[category].append((email, cat))

    # Read template
    with open(TEMPLATE_PATH) as f:
        template = f.read()

    # Generate HTML for each section
    def render_section(items, done_check=False, trash_btn=False, delete_check=False):
        if not items:
            return '<div class="empty-state">Nothing here right now.</div>'
        return "\n".join(
            render_email_item(email, cat, show_done_check=done_check, show_trash_btn=trash_btn, show_delete_check=delete_check)
            for email, cat in items
        )

    now = datetime.now().strftime("%b %d, %Y %I:%M %p")

    replacements = {
        "{{LAST_UPDATED}}": now,
        "{{IMPORTANT_COUNT}}": str(len(sections["important"])),
        "{{IMPORTANT_ITEMS}}": render_section(sections["important"], done_check=True, trash_btn=True),
        "{{FOLLOWUP_COUNT}}": str(len(sections["followup"])),
        "{{FOLLOWUP_ITEMS}}": render_section(sections["followup"], done_check=True, trash_btn=True),
        "{{SCHOOL_COUNT}}": str(len(sections["school_ella"]) + len(sections["school_emmie"]) + len(sections["school_general"])),
        "{{SCHOOL_ELLA_ITEMS}}": render_section(sections["school_ella"]),
        "{{SCHOOL_EMMIE_ITEMS}}": render_section(sections["school_emmie"]),
        "{{SCHOOL_GENERAL_ITEMS}}": render_section(sections["school_general"]),
        "{{PROMOS_COUNT}}": str(len(sections["promo"])),
        "{{PROMO_ITEMS}}": render_section(sections["promo"]),
        "{{DELETE_COUNT}}": str(len(sections["deletion"])),
        "{{DELETE_ITEMS}}": render_section(sections["deletion"], delete_check=True),
    }

    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)

    return result


# --- Main ---
def main():
    print("=== Gmail Dashboard Refresh ===")
    print(f"Time: {datetime.now().isoformat()}")

    # Step 1: Set up Gmail service
    print("\n1. Connecting to Gmail API...")
    service = get_gmail_service()

    # Step 2: Process pending deletions
    print("\n2. Checking for pending deletions...")
    trashed = process_deletions(service)
    clear_refresh_marker()

    # Step 3: Fetch emails
    print("\n3. Fetching emails...")
    all_emails = fetch_all_emails(service)

    if not all_emails:
        print("No emails found. Generating empty dashboard.")
        categorization_results = []
    else:
        # Step 4: Auto-categorize obvious emails
        print("\n4. Auto-categorizing...")
        needs_ai, auto_results = auto_categorize(all_emails)
        print(f"  Auto-categorized: {len(auto_results)}, needs AI: {len(needs_ai)}")

        # Step 5: AI categorization
        print("\n5. AI categorization...")
        ai_results = categorize_with_claude(needs_ai)
        categorization_results = auto_results + ai_results

    # Step 6: Generate HTML
    print("\n6. Generating dashboard HTML...")
    dashboard_html = generate_dashboard(all_emails, categorization_results)

    with open(OUTPUT_PATH, "w") as f:
        f.write(dashboard_html)
    print(f"  Saved to {OUTPUT_PATH}")

    # Summary
    cat_counts = {}
    for r in categorization_results:
        cat = r.get("cat", "skip")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    print("\n=== Summary ===")
    if trashed:
        print(f"Trashed: {trashed} emails")
    for cat, count in sorted(cat_counts.items()):
        print(f"  {cat}: {count}")
    print("Dashboard generated successfully!")


if __name__ == "__main__":
    main()
