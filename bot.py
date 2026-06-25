import os
import re
import io
import json
import time
import win32clipboard
import pyautogui
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from pywinauto import Desktop, keyboard
from supabase import create_client, Client

# --- CONFIGURATION ---
# Secrets (kept out of the public repo). All required except where noted.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE")
# Storage bucket holding the web-uploaded list PDF. The list is read
# straight from Storage and parsed in memory — there is no `source/` directory.
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET")

# Portal-specific details — moved to secrets so the public repo doesn't reveal
# the target site, its form selectors, or the print-window title.
PORTAL_URL = os.environ.get("PORTAL_URL")                       # login page URL
REG_INPUT_SELECTOR = os.environ.get("REG_INPUT_SELECTOR")       # reg-number input
LOGIN_BUTTON_SELECTOR = os.environ.get("LOGIN_BUTTON_SELECTOR")  # login button
PRINT_BUTTON_SELECTOR = os.environ.get("PRINT_BUTTON_SELECTOR")  # print-slip button
WINDOW_TITLE_RE = os.environ.get("WINDOW_TITLE_RE")            # native window title regex

# Names of the secrets that must be present for a run to proceed.
REQUIRED_SECRETS = [
    "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_TABLE", "SUPABASE_BUCKET",
    "PORTAL_URL", "REG_INPUT_SELECTOR", "LOGIN_BUTTON_SELECTOR",
    "PRINT_BUTTON_SELECTOR", "WINDOW_TITLE_RE",
]

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = "llama-3.3-70b-versatile"  # swap if Groq retires this model

# Optional cap for test runs (0 = process everyone in the PDF).
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "0"))

# Stop gracefully after this many minutes so the GitHub job isn't killed
# mid-record at the 6h limit (0 = no limit). Remaining regs resume next run.
RUN_BUDGET_MINUTES = int(os.environ.get("RUN_BUDGET_MINUTES", "0"))

# Sentinel the workflow checks: present => stopped early, re-dispatch to continue.
CONTINUE_FILE = ".continue"

# Skip a candidate if the portal takes longer than this to load after login.
# Configurable via env (seconds); defaults to 60s.
LOGIN_TIMEOUT_MS = int(os.environ.get("LOGIN_TIMEOUT_SECONDS", "60")) * 1000

# Fields parsed from each candidate's slip.
FIELDS = [
    "fullname", "gender", "date_of_birth", "email", "mobile_phone",
    "reg_no", "state_of_origin", "address", "local_government",
]
# Fields added from the uploaded list (NOT from the slip): course + faculty
# come from the PDF, list_name is the PDF filename.
LIST_FIELDS = ["course", "faculty", "list_name"]
REQUIRED_FIELDS = ["fullname"]
VERIFY_CHUNK = 25   # how many parsed records to audit per Groq call
SAMPLE_SIZE = 3     # slips used to learn the template before streaming saves


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _norm(value):
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def _looks_like_reg(token):
    """A reg number: alphanumeric, long-ish, with both digits and letters."""
    t = (token or "").strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{8,}", t):
        return False
    return any(c.isdigit() for c in t) and any(c.isalpha() for c in t)


def _looks_like_slip(text):
    """Heuristic: does this text actually look like a slip?"""
    return bool(text) and len(text) > 200 and re.search(
        r"jamb|fullname|gender|date of birth", text, re.IGNORECASE
    )


def _slip_matches_reg(text, reg):
    """The copied slip must belong to THIS candidate (guards stale clipboard)."""
    digits = reg[:12]  # the numeric core of a reg number
    return digits and digits in re.sub(r"\s+", "", text)


def _clean_field(field, value):
    """
    Pull just the real value out of a captured span. Template extraction grabs
    everything up to the next *tracked* label, so a field can trail into
    untracked text (e.g. date_of_birth followed by "State of origin ...").
    """
    value = re.sub(r"\s+", " ", value or "").strip(" :\t\r\n-")
    if field == "date_of_birth":
        m = re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}", value)
        return m.group(0) if m else ""
    if field == "mobile_phone":
        m = re.search(r"0\d{10}", value) or re.search(r"\d{11}", value)
        return m.group(0) if m else re.sub(r"\D", "", value)[:11]
    if field == "email":
        m = re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)
        return m.group(0) if m else ""
    return value


# --------------------------------------------------------------------------- #
# Groq (AI) helpers
# --------------------------------------------------------------------------- #
def _get_groq():
    if not GROQ_API_KEY:
        return None
    try:
        from groq import Groq
        return Groq(api_key=GROQ_API_KEY)
    except Exception:
        return None


def _groq_json(client, prompt, system):
    """One temperature-0 JSON-mode call. Returns a dict or None."""
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return None


_EXTRACT_SYS = (
    "You extract structured data from document text. Only use what is present. "
    "If a field is absent, return an empty string. Never invent or guess values."
)


# --------------------------------------------------------------------------- #
# Step 1: read reg numbers from the list PDF
#   Groq inspects the column labels first (they get relabelled between years),
#   then we pull the values, which have a distinctive alphanumeric format.
# --------------------------------------------------------------------------- #
def fetch_pdf_from_storage():
    """
    Download the most recently uploaded PDF from the Supabase Storage bucket and
    return (list_name, pdf_bytes). The list is parsed entirely in memory — the
    PDF is never written to disk (there is no `source/` directory in CI).
    """
    try:
        sb = _get_supabase()
        entries = sb.storage.from_(SUPABASE_BUCKET).list()
    except Exception:
        return None, None

    pdfs = [f for f in entries if (f.get("name") or "").lower().endswith(".pdf")]
    if not pdfs:
        return None, None

    def _ts(f):
        meta = f.get("metadata") or {}
        return (f.get("updated_at") or f.get("created_at")
                or meta.get("lastModified") or f.get("name") or "")

    latest = sorted(pdfs, key=_ts)[-1]
    name = latest["name"]
    try:
        data = sb.storage.from_(SUPABASE_BUCKET).download(name)
    except Exception:
        return None, None

    list_name = os.path.splitext(os.path.basename(name))[0]
    return list_name, data


def _choose_course_x(header_words):
    """Find the x where the course/department column starts (heuristic + Groq)."""
    for w in header_words:
        if re.search(r"course|depart|programme|program", w["text"], re.IGNORECASE):
            return w["x0"]
    # Groq fallback handles a relabelled header.
    client = _get_groq()
    labels = [w["text"] for w in header_words]
    if client and labels:
        prompt = (
            f"Column headers left-to-right: {json.dumps(labels)}.\n"
            "Which one is the course / department / programme column?\n"
            'Return JSON: {"index": <0-based index>}.'
        )
        res = _groq_json(client, prompt, "You map table columns by meaning.")
        if res and isinstance(res.get("index"), int) and 0 <= res["index"] < len(header_words):
            w = header_words[res["index"]]
            return w["x0"]
    return None


def read_records(pdf_source):
    """
    Read (reg, course, faculty) for every candidate. `pdf_source` is a path or a
    file-like object (we pass an in-memory BytesIO of the Storage download).

    The list is a borderless text PDF, so we bucket words by x-position: the
    course column starts at the x of its header word; the reg number is the
    alphanumeric token to its left; the faculty is the most recent
    'FACULTY OF ...' section header.
    """
    import pdfplumber

    records, seen = [], set()
    faculty = ""  # fallback when no "FACULTY OF ..." header has been seen
    course_x = None
    with pdfplumber.open(pdf_source) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            lines = {}
            for w in words:
                lines.setdefault(round(w["top"]), []).append(w)

            # First pass over this page: find reg rows and all course-column words.
            reg_rows = []        # (top, reg, faculty)
            course_words = []    # (top, x0, text)
            for top in sorted(lines):
                ws = sorted(lines[top], key=lambda x: x["x0"])
                text = " ".join(w["text"] for w in ws).strip()

                # Header row: locate the course column once.
                if course_x is None and \
                        re.search(r"\bCOURSE\b|DEPARTMENT|PROGRAMME", text, re.IGNORECASE) and \
                        re.search(r"FULLNAME|NAME|JAMB|S/?NO", text, re.IGNORECASE):
                    course_x = _choose_course_x(ws)
                    continue
                if re.match(r"FACULTY OF\b", text, re.IGNORECASE):
                    faculty = text
                    continue
                if course_x is None:
                    continue  # still in the preamble, before the header

                line_reg = None
                for w in ws:
                    if w["x0"] < course_x - 5:
                        if _looks_like_reg(w["text"]):
                            line_reg = w["text"].upper()
                    else:
                        course_words.append((top, w["x0"], w["text"]))
                if line_reg:
                    reg_rows.append((top, line_reg, faculty))

            # Second pass: assign course words to each reg's vertical band. A long
            # name wraps the course onto extra lines, so we bound each record
            # between the midpoints to its neighbouring reg rows (no bleed).
            rtops = [r[0] for r in reg_rows]
            for k, (rtop, reg, fac) in enumerate(reg_rows):
                if reg in seen:
                    continue
                seen.add(reg)
                lo = (rtops[k - 1] + rtop) / 2 if k > 0 else rtop - 1000
                hi = (rtop + rtops[k + 1]) / 2 if k + 1 < len(rtops) else rtop + 1000
                mine = sorted(
                    [(t, x, txt) for (t, x, txt) in course_words if lo <= t < hi],
                    key=lambda z: (z[0], z[1]),
                )
                records.append({
                    "reg": reg,
                    "course": " ".join(z[2] for z in mine).strip(),
                    "faculty": fac,
                })

    return records


# --------------------------------------------------------------------------- #
# PRIMARY slip parser: AI-learned template, applied deterministically
# --------------------------------------------------------------------------- #
def llm_learn_template(sample_texts):
    """
    ONE Groq call: from the first 2-3 slips, return a label map (reused to parse
    every slip for free) plus the parsed values for slip 1 (for the self-check).
    """
    client = _get_groq()
    if not client:
        return None
    samples = "\n\n----- NEXT SLIP -----\n\n".join(
        f"[SLIP {i + 1}]\n{t}" for i, t in enumerate(sample_texts)
    )
    prompt = (
        "Below are 1-3 slips sharing the same layout.\n"
        f"For each field: {FIELDS}\n"
        "1) Identify the exact literal label text that appears immediately "
        "before the value (e.g. \"Fullname\", \"Date of birth\"). Use \"\" if none.\n"
        "2) Extract the values for SLIP 1 only. Normalize date_of_birth to "
        "YYYY-MM-DD. Use \"\" if absent.\n\n"
        'Return JSON: {"labels": {<field>: <label>, ...}, '
        '"values": {<field>: <slip-1 value>, ...}}\n\n'
        f"SLIPS:\n{samples}"
    )
    res = _groq_json(client, prompt, _EXTRACT_SYS)
    if res and isinstance(res.get("labels"), dict):
        return res
    return None


def parse_with_template(raw_text, labels):
    """Deterministic, label-anchored extraction using a learned label map."""
    all_labels = [lbl for lbl in labels.values() if lbl]
    lower = raw_text.lower()
    data = {}
    for field in FIELDS:
        label = labels.get(field, "")
        if not label:
            data[field] = ""
            continue
        idx = lower.find(label.lower())
        if idx == -1:
            data[field] = ""
            continue
        rest = raw_text[idx + len(label):]
        rest_lower = rest.lower()
        cut = len(rest)
        for other in all_labels:
            if other == label:
                continue
            oidx = rest_lower.find(other.lower())
            if oidx != -1 and oidx < cut:
                cut = oidx
        data[field] = _clean_field(field, rest[:cut])
    return data


def template_self_check(parsed, llm_values):
    """Confirm the template reproduces the AI's slip-1 answer. Allow one miss."""
    if not llm_values:
        return False
    keys = [k for k in ("fullname", "date_of_birth", "email") if llm_values.get(k)]
    if not keys:
        return False
    matches = sum(1 for k in keys if _norm(parsed.get(k)) == _norm(llm_values.get(k)))
    return matches >= max(1, len(keys) - 1)


# --------------------------------------------------------------------------- #
# FALLBACK slip parser: regex first, then Groq verifies the cheap result
# --------------------------------------------------------------------------- #
def parse_slip_text(raw_text):
    """Regex parser. Tightly coupled to the current slip wording."""
    def extract(pattern, text, default=""):
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else default

    return {
        "fullname":         extract(r"Fullname\s+([A-Z][A-Z\s]+?)\s+Gender", raw_text),
        "gender":           extract(r"Gender\s+(MALE|FEMALE)", raw_text),
        "date_of_birth":    extract(r"Date of birth\s+(\d{4}-\d{2}-\d{2})", raw_text),
        "email":            extract(r"Email address\s+(\S+)", raw_text),
        "mobile_phone":     extract(r"Mobile phone\s+(\d+)", raw_text),
        "reg_no":           extract(r"JAMB Reg No\s+(\S+)", raw_text),
        "state_of_origin":  extract(r"State of origin\s+(.+?)\s+Address", raw_text),
        "address":          extract(r"Address\s+(.+?)\s+(?:Local government|L\.?G\.?A\.?|Email|Mobile)", raw_text),
        "local_government": extract(r"(?:Local government|L\.?G\.?A\.?)\s+(.+?)\s+(?:Email|Mobile|State)", raw_text),
    }


def llm_parse_single(raw_text):
    """Full-text AI re-parse, used to rescue a single bad record."""
    client = _get_groq()
    if not client:
        return None
    prompt = (
        f"Extract these fields from the document below: {FIELDS}\n"
        "Normalize date_of_birth to YYYY-MM-DD. Use \"\" for anything absent. "
        "Return a flat JSON object keyed by those field names.\n\n"
        f"SLIP:\n{raw_text}"
    )
    res = _groq_json(client, prompt, _EXTRACT_SYS)
    if not res:
        return None
    return {f: str(res.get(f, "") or "") for f in FIELDS}


def verify_results(parsed_list):
    """
    Cheap quality gate: send only the small parsed dicts (not the full slip
    text) to Groq in chunks and let it flag garbage. Returns list[bool].
    """
    verdicts = [True] * len(parsed_list)
    client = _get_groq()
    if not client:
        return verdicts

    for start in range(0, len(parsed_list), VERIFY_CHUNK):
        chunk = parsed_list[start:start + VERIFY_CHUNK]
        items = [
            {"i": start + j, **{k: d.get(k, "") for k in FIELDS}}
            for j, d in enumerate(chunk)
        ]
        prompt = (
            "Each item below is a record parsed from a document. Flag the ones "
            "that look like garbage: a fullname that is not a real name, a gender "
            "that is not MALE/FEMALE, an email that is not an email, an empty "
            "fullname, or mismatched fields.\n"
            'Return JSON {"bad": [i, ...]} listing ONLY the bad "i" values.\n\n'
            + json.dumps(items, ensure_ascii=False)
        )
        res = _groq_json(client, prompt, "You audit parsed records for quality.")
        if res and isinstance(res.get("bad"), list):
            for i in res["bad"]:
                if isinstance(i, int) and 0 <= i < len(parsed_list):
                    verdicts[i] = False
    return verdicts


def verify_one(data):
    """Per-record version of verify_results, for streaming mode. True = valid."""
    return verify_results([data])[0]


def validate_parsed(data):
    return all(data.get(f) for f in REQUIRED_FIELDS)


# --------------------------------------------------------------------------- #
# Supabase
# --------------------------------------------------------------------------- #
_SUPABASE = None


def _get_supabase():
    """One reused client (we save one record at a time, streaming)."""
    global _SUPABASE
    if _SUPABASE is None:
        _SUPABASE = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _SUPABASE


def fetch_done_regs(list_name):
    """
    Resume support: return the set of reg numbers already saved for this list,
    so a re-run skips them (each GitHub job is capped at 6h and starts fresh).
    Paginated because Supabase returns at most 1000 rows per request.
    """
    done = set()
    try:
        sb = _get_supabase()
        step, start = 1000, 0
        while True:
            res = (sb.table(SUPABASE_TABLE)
                     .select("reg_no")
                     .eq("list_name", list_name)
                     .range(start, start + step - 1)
                     .execute())
            rows = res.data or []
            for r in rows:
                if r.get("reg_no"):
                    done.add(r["reg_no"].upper())
            if len(rows) < step:
                break
            start += step
    except Exception:
        pass
    return done


def record_exists(reg, list_name):
    """
    A candidate is a duplicate only if the SAME reg appears in the SAME list.
    The same person may legitimately appear in two different lists under a
    different faculty/course, so we key on (reg_no, list_name).
    """
    try:
        sb = _get_supabase()
        res = (sb.table(SUPABASE_TABLE)
                 .select("reg_no")
                 .eq("reg_no", reg)
                 .eq("list_name", list_name)
                 .limit(1)
                 .execute())
        return bool(res.data)
    except Exception:
        return False  # don't block the insert on a check error


def save_to_supabase_table(parsed_data):
    reg = parsed_data.get("reg_no", "")
    list_name = parsed_data.get("list_name", "")
    if record_exists(reg, list_name):
        return False
    try:
        _get_supabase().table(SUPABASE_TABLE).insert(parsed_data).execute()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Scraping (pywinauto + clipboard)
# --------------------------------------------------------------------------- #
def clear_clipboard():
    """Empty the clipboard so a failed copy can't leave stale slip text behind."""
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
    except Exception:
        pass
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def get_clipboard_text():
    win32clipboard.OpenClipboard()
    try:
        data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
    except Exception:
        data = ""
    finally:
        win32clipboard.CloseClipboard()
    return data


def pywinauto_copy_handler():
    """Focus the window, click center, Ctrl+A, Ctrl+C."""
    desktop = Desktop(backend="uia")
    try:
        chrome_win = desktop.window(title_re=WINDOW_TITLE_RE)
        if not chrome_win.exists():
            return
        chrome_win.set_focus()
        time.sleep(8)
        w, h = pyautogui.size()
        pyautogui.click(w // 2, h // 2)
        time.sleep(1)
        keyboard.send_keys("^a")
        time.sleep(2)
        keyboard.send_keys("^c")
        time.sleep(2)
    except Exception:
        pass


def scrape_slip(context, reg):
    """
    Login for one reg and return its slip text, or "" to skip.
    Skips when: portal takes >1 min to load, the print button is missing, or no
    valid slip text is captured after Ctrl+A/Ctrl+C (with one retry).
    """
    page = context.new_page()
    try:
        # Original flow: open login page, wait for it, enter reg, click Login.
        page.goto(PORTAL_URL, timeout=LOGIN_TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=LOGIN_TIMEOUT_MS)
        page.fill(REG_INPUT_SELECTOR, reg)
        page.click(LOGIN_BUTTON_SELECTOR)

        # Skip if the portal is too slow to load after entering the reg number.
        try:
            page.wait_for_load_state("networkidle", timeout=LOGIN_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            return ""
        time.sleep(5)  # let the dashboard / next page settle (original behavior)

        btn_selector = PRINT_BUTTON_SELECTOR
        if not page.is_visible(btn_selector):
            return ""

        page.click(btn_selector)
        time.sleep(8)  # let the slip / print view render

        # Capture via clipboard, validating that we got THIS candidate's slip.
        for _ in range(2):
            clear_clipboard()  # avoid carrying over the previous candidate
            pywinauto_copy_handler()
            raw = get_clipboard_text() or ""
            if _looks_like_slip(raw) and _slip_matches_reg(raw, reg):
                return raw
        return ""
    except PlaywrightTimeoutError:
        return ""
    except Exception:
        return ""
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _parse_slip(raw, reg, template, use_template):
    """Parse one slip: template (primary) or regex+verify (fallback)."""
    if use_template:
        d = parse_with_template(raw, template["labels"])
        d["reg_no"] = reg
        if not validate_parsed(d):
            rescue = llm_parse_single(raw)
            if rescue:
                rescue["reg_no"] = reg
                d = rescue
        return d

    # Fallback: regex first, then Groq verifies the cheap result.
    d = parse_slip_text(raw)
    d["reg_no"] = reg
    if not verify_one(d):
        rescue = llm_parse_single(raw)
        if rescue:
            rescue["reg_no"] = reg
            d = rescue
    return d


def _process_and_save(rec, raw, template, use_template, list_name):
    """Parse a slip, attach the list fields, and save immediately."""
    reg = rec["reg"]
    data = _parse_slip(raw, reg, template, use_template)
    data["course"] = rec.get("course", "")
    data["faculty"] = rec.get("faculty", "")
    data["list_name"] = list_name
    save_to_supabase_table(data)


def run_bot():
    # Fail fast if any portal/Supabase secret is missing.
    missing = [name for name in REQUIRED_SECRETS if not os.environ.get(name)]
    if missing:
        return

    # Clear any stale sentinel from a previous run.
    if os.path.exists(CONTINUE_FILE):
        os.remove(CONTINUE_FILE)

    list_name, pdf_bytes = fetch_pdf_from_storage()
    if not pdf_bytes:
        return
    candidates = read_records(io.BytesIO(pdf_bytes))
    if not candidates:
        return

    # Resume: drop candidates already saved for this list (skips re-scraping).
    done = fetch_done_regs(list_name)
    if done:
        candidates = [c for c in candidates if c["reg"] not in done]

    if MAX_CANDIDATES > 0:
        candidates = candidates[:MAX_CANDIDATES]

    if not candidates:
        return

    # Stream: scrape -> parse -> save each candidate before the next one.
    # The first few successful slips are buffered to learn the template, then
    # flushed; everything after is fully one-at-a-time.
    template, use_template, decided, buffer = None, False, False, []

    def learn(samples):
        tpl = llm_learn_template([raw for _, raw in samples])
        use = False
        if tpl:
            check = parse_with_template(samples[0][1], tpl["labels"])
            use = template_self_check(check, tpl.get("values", {}))
        return tpl, use

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        context = browser.new_context()

        start_ts = time.time()
        stopped_early = False
        for rec in candidates:
            if RUN_BUDGET_MINUTES and (time.time() - start_ts) > RUN_BUDGET_MINUTES * 60:
                stopped_early = True
                break
            raw = scrape_slip(context, rec["reg"])
            if not raw:
                continue
            if not decided:
                buffer.append((rec, raw))
                if len(buffer) >= SAMPLE_SIZE:
                    template, use_template = learn(buffer)
                    decided = True
                    for brec, braw in buffer:
                        _process_and_save(brec, braw, template, use_template, list_name)
                    buffer = []
            else:
                _process_and_save(rec, raw, template, use_template, list_name)

        # Fewer total slips than SAMPLE_SIZE: learn from what we have, then flush.
        if not decided and buffer:
            template, use_template = learn(buffer)
            for brec, braw in buffer:
                _process_and_save(brec, braw, template, use_template, list_name)

        browser.close()

    if stopped_early:
        with open(CONTINUE_FILE, "w") as f:
            f.write("more work remaining")


if __name__ == "__main__":
    run_bot()
