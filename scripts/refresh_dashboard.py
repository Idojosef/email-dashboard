#!/usr/bin/env python3
"""Gmail Dashboard Refresh Script.
Fetches emails via Gmail API, categorizes with rules, generates HTML dashboard.
Runs in GitHub Actions or locally. No AI API needed."""

import os
import json
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# Pacific time (UTC-7 PDT / UTC-8 PST)
PT = timezone(timedelta(hours=-7))

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --- Config ---
USER_ID = "me"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
TEMPLATE_PATH = os.path.join(REPO_DIR, "assets", "dashboard_template.html")
OUTPUT_PATH = os.path.join(REPO_DIR, "index.html")
DELETE_PATH = os.path.join(REPO_DIR, "commands", "delete.json")
REFRESH_PATH = os.path.join(REPO_DIR, "commands", "refresh.json")

# --- Categorization Rules ---

# Senders/subjects that indicate school content
SCHOOL_PATTERNS = {
    "senders": ["parentsquare", "cusdk8", "cusd", "stocklmeir", "peachjar"],
    "subjects": ["stocklmeir", "cusd", "pta", "school", "parent square"],
}

# Ella-specific keywords (name, grade, teacher)
ELLA_KEYWORDS = ["ella", "4th grade", "fourth grade", "grade 4", "3rd grade", "third grade", "grade 3", "srivatsangam"]

# Emmie-specific keywords
EMMIE_KEYWORDS = ["emmie", "emmy", "1st grade", "first grade", "grade 1", "kindergarten", "kinder"]

# Interesting promo senders/subjects
PROMO_INTERESTING = {
    "senders": ["slickdeals", "camelcamelcamel", "rei.com", "rei co-op", "campingworld",
                "thousandtrails", "thousand trails", "jackery", "google flights",
                "noreply-travel@google"],
    "subjects": ["camping", "rv ", "outdoor", "campground", "hiking", "trail",
                 "power station", "solar panel", "tent", "kayak", "yosemite",
                 "national park", "slickdeals", "price drop", "price alert",
                 "tracked route", "tracked price"],
}

# LinkedIn - only DMs and connections
LINKEDIN_GOOD = ["sent you a message", "connection request", "wants to connect",
                 "accepted your invitation", "direct message", "new message"]
LINKEDIN_BAD = ["puzzle", "game", "who viewed", "skill assessment", "newsletter",
                "trending", "job alert", "is hiring", "endorsed", "anniversary",
                "birthday", "reacted to", "commented on", "liked your"]

# Always skip these senders for promos (generic/unwanted)
PROMO_SKIP_SENDERS = ["expedia", "hotels.com", "booking.com", "trivago",
                       "groupon", "wish.com", "temu", "shein"]

# Peachjar - always route to deletion
PEACHJAR_SUBJECTS = ["new school and community flyers", "new flyers", "new school flyer",
                     "community flyers for your child"]


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
    """Search Gmail threads."""
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

    msg = messages[-1]
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    labels = msg.get("labelIds", [])

    return {
        "thread_id": thread["id"],
        "subject": headers.get("Subject", "(no subject)"),
        "sender": parse_sender(headers.get("From", "")),
        "sender_email": headers.get("From", "").lower(),
        "date": parse_date(headers.get("Date", "")),
        "snippet": thread.get("snippet", ""),
        "is_unread": "UNREAD" in labels,
        "labels": labels,
    }


def matches_any(text, patterns):
    """Check if text contains any of the patterns (case-insensitive)."""
    text_lower = text.lower()
    return any(p.lower() in text_lower for p in patterns)


def guess_action_tag(subject, snippet, sender_email):
    """Guess an action tag based on keywords."""
    text = (subject + " " + snippet).lower()
    sender = sender_email.lower()

    if any(w in text for w in ["pay invoice", "payment due", "pay your", "balance due", "amount due"]):
        return "PAY"
    if any(w in text for w in ["sign ", "signature", "e-sign", "docusign"]):
        return "SIGN"
    if any(w in text for w in ["save these", "credentials", "password", "pin ", "login"]):
        return "SAVE"
    if any(w in text for w in ["reply", "respond", "rsvp", "waiting for", "your input", "your feedback"]):
        return "RESPOND"
    if any(w in text for w in ["review", "statement", "renewal", "policy", "document", "report card"]):
        return "REVIEW"
    return "FYI"


def guess_urgency(subject, snippet, sender_email):
    """Guess urgency based on keywords."""
    text = (subject + " " + snippet).lower()

    if any(w in text for w in ["urgent", "action required", "failed", "expiring", "overdue",
                                "recall", "immediately", "deadline today", "cancell"]):
        return "urgent"
    if any(w in text for w in ["action needed", "pay ", "sign ", "review", "appointment",
                                "reminder", "due ", "renew", "refill", "prescription"]):
        return "action"
    return "info"


# --- Categorization ---
def categorize_email(email, source):
    """Categorize a single email using rules. Returns (category, summary, urgency, tag)."""
    subject = email["subject"]
    snippet = email["snippet"]
    sender = email["sender"]
    sender_email = email["sender_email"]
    subject_lower = subject.lower()
    sender_lower = sender_email.lower()
    text = (subject + " " + snippet).lower()

    # --- Deletion candidates ---

    # Old promos and social (from deletion queries)
    if source in ("delete_promos", "delete_social"):
        return "deletion", snippet[:80], "low", None

    # Peachjar - always deletion
    if "peachjar" in sender_lower or matches_any(subject_lower, PEACHJAR_SUBJECTS):
        return "deletion", f"Peachjar flyer: {subject[:60]}", "low", None

    # LinkedIn junk
    if "linkedin" in sender_lower and matches_any(text, LINKEDIN_BAD):
        return "deletion", snippet[:80], "low", None

    # Generic travel promos
    if matches_any(sender_lower, PROMO_SKIP_SENDERS):
        return "deletion", snippet[:80], "low", None

    # --- School ---
    if source == "school" or matches_any(sender_lower, SCHOOL_PATTERNS["senders"]):
        # Don't put peachjar in school (already caught above)
        if "peachjar" in sender_lower:
            return "deletion", f"Peachjar flyer: {subject[:60]}", "low", None

        # Check for child-specific content
        if matches_any(text, ELLA_KEYWORDS):
            return "school_ella", snippet[:100], "info", None
        elif matches_any(text, EMMIE_KEYWORDS):
            return "school_emmie", snippet[:100], "info", None
        else:
            return "school_general", snippet[:100], "info", None

    # --- Interesting promos ---
    if source in ("promos", "social"):
        # LinkedIn DMs and connections only
        if "linkedin" in sender_lower:
            if matches_any(text, LINKEDIN_GOOD):
                return "promo", snippet[:100], "info", None
            else:
                return "skip", "", "low", None

        # Check if it matches our interest patterns
        if matches_any(sender_lower, PROMO_INTERESTING["senders"]) or \
           matches_any(text, PROMO_INTERESTING["subjects"]):
            return "promo", snippet[:100], "info", None

        # Not interesting enough
        return "skip", "", "low", None

    # --- Important (unread) vs Follow-up (read) ---
    if email["is_unread"]:
        urgency = guess_urgency(subject, snippet, sender_email)
        tag = guess_action_tag(subject, snippet, sender_email)
        return "important", snippet[:100], urgency, tag
    else:
        # Read email - only include if it looks like it needs follow-up
        if any(w in text for w in ["action", "reply", "respond", "waiting", "pending",
                                    "due", "overdue", "pay", "sign", "review",
                                    "appointment", "confirm", "rsvp", "your input",
                                    "refill", "claim", "forwarded", "fwd:"]):
            tag = guess_action_tag(subject, snippet, sender_email)
            return "followup", snippet[:100], "action", tag
        else:
            return "skip", "", "low", None


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


def fetch_all_emails(service):
    """Fetch emails from all query categories."""
    all_emails = {}  # thread_id -> (email_info, source)

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
                        all_emails[tid] = (info, source)
            # Keep first source (more specific queries run first)

    print(f"  Total unique threads: {len(all_emails)}")
    return all_emails


# --- HTML Generation ---
def escape(text):
    """HTML-escape text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_email_item(email, summary, urgency, tag, show_done_check=False, show_trash_btn=False, show_delete_check=False):
    """Render a single email as HTML."""
    tid = escape(email["thread_id"])
    sender = escape(email["sender"])
    subject = escape(email["subject"][:80])
    summary_text = escape(summary)
    date = escape(email["date"])

    parts = ['<div class="email-item">']

    if show_done_check:
        parts.append(f'    <input type="checkbox" class="done-check" data-thread-id="{tid}" onchange="handleDoneCheck(this)">')

    if show_delete_check:
        parts.append(f'    <input type="checkbox" class="delete-check" data-thread-id="{tid}">')

    if not show_delete_check:
        parts.append(f'    <div class="urgency-dot {urgency}"></div>')

    parts.append('    <div class="email-content">')
    parts.append('        <div class="email-top">')
    parts.append(f'            <span class="email-sender">{sender}</span>')
    parts.append('            <span class="email-date">')
    if show_trash_btn:
        parts.append(f"                <button class=\"trash-btn\" onclick=\"trashSingleEmail(this, '{tid}')\" title=\"Move to trash\">&#128465;</button>")
    parts.append(f'                {date}')
    parts.append('            </span>')
    parts.append('        </div>')
    parts.append(f'        <div class="email-subject">{subject}</div>')
    parts.append(f'        <div class="email-summary">{summary_text}</div>')
    if tag:
        tag_class = {"PAY": "deadline", "REVIEW": "review", "RESPOND": "respond",
                     "SIGN": "deadline", "SAVE": "review", "FYI": "fyi"}.get(tag, "fyi")
        parts.append(f'        <span class="email-action-tag {tag_class}">{tag}</span>')
    parts.append('    </div>')
    parts.append('</div>')

    return "\n".join(parts)


def generate_dashboard(categorized_emails):
    """Generate the full HTML dashboard."""
    sections = {
        "important": [],
        "followup": [],
        "school_ella": [],
        "school_emmie": [],
        "school_general": [],
        "promo": [],
        "deletion": [],
    }

    for email, cat, summary, urgency, tag in categorized_emails:
        if cat in sections:
            sections[cat].append((email, summary, urgency, tag))

    # Read template
    with open(TEMPLATE_PATH) as f:
        template = f.read()

    def render_section(items, done_check=False, trash_btn=False, delete_check=False):
        if not items:
            return '<div class="empty-state">Nothing here right now.</div>'
        return "\n".join(
            render_email_item(email, summary, urgency, tag,
                            show_done_check=done_check, show_trash_btn=trash_btn,
                            show_delete_check=delete_check)
            for email, summary, urgency, tag in items
        )

    now = datetime.now(PT).strftime("%b %d, %Y %I:%M %p PT")
    school_count = len(sections["school_ella"]) + len(sections["school_emmie"]) + len(sections["school_general"])

    replacements = {
        "{{LAST_UPDATED}}": now,
        "{{IMPORTANT_COUNT}}": str(len(sections["important"])),
        "{{IMPORTANT_ITEMS}}": render_section(sections["important"], done_check=True, trash_btn=True),
        "{{FOLLOWUP_COUNT}}": str(len(sections["followup"])),
        "{{FOLLOWUP_ITEMS}}": render_section(sections["followup"], done_check=True, trash_btn=True),
        "{{SCHOOL_COUNT}}": str(school_count),
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
    print("=== Gmail Dashboard Refresh (Rule-Based) ===")
    print(f"Time: {datetime.now().isoformat()}")

    # Step 1: Gmail service
    print("\n1. Connecting to Gmail API...")
    service = get_gmail_service()

    # Step 2: Process deletions
    print("\n2. Checking for pending deletions...")
    trashed = process_deletions(service)
    clear_refresh_marker()

    # Step 3: Fetch emails
    print("\n3. Fetching emails...")
    all_emails = fetch_all_emails(service)

    # Step 4: Categorize
    print("\n4. Categorizing emails...")
    categorized = []
    cat_counts = {}

    for tid, (email, source) in all_emails.items():
        cat, summary, urgency, tag = categorize_email(email, source)
        if cat != "skip":
            categorized.append((email, cat, summary, urgency, tag))
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    # Step 5: Generate HTML
    print("\n5. Generating dashboard HTML...")
    html = generate_dashboard(categorized)

    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"  Saved to {OUTPUT_PATH}")

    # Summary
    print("\n=== Summary ===")
    if trashed:
        print(f"Trashed: {trashed} emails")
    for cat, count in sorted(cat_counts.items()):
        print(f"  {cat}: {count}")
    print("Dashboard generated successfully!")


if __name__ == "__main__":
    main()
