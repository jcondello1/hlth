import os, json, datetime, logging, traceback, re
from google.oauth2 import service_account
from googleapiclient.discovery import build
import boto3

# ====== Environment ======
SHEET_ID   = os.environ["SHEET_ID"]                 # Google Sheet ID
SECRET_ID  = os.environ["SECRET_ID"]                # Secrets Manager secret with SA JSON
RANGE_NAME = os.environ.get("RANGE_NAME", "Daily Tracker")  # Tab name

# ====== Logging ======
log = logging.getLogger()
log.setLevel(logging.INFO)

# ====== Safe keys -> exact Sheet headers ======
SHEET_HEADER_MAP = {
    "date": "Date",
    "weight_lbs": "Weight (lbs)",
    "waist_in": "Waist (in)",
    "calories_controlled": "Calories Controlled (Y/N)",
    "calories_in": "Calories In (~2,450 cal/day)",
    "protein_target_hit": "Protein Target Hit (Y/N)",
    "protein_intake_g": "Protein Intake (~160g)",
    "protein_intake": "Protein Intake (~160g)",   # <-- alias
    "steps": "Steps",
    "jog_walk": "Jog/Walk (Y/N)",
    "jog_miles": "Jog Mls.",
    "after_dinner_walk": "After-Dinner Walk (Y/N)",
    "resist_training": "Resist Training (Y/N)",
    "notes": "Notes",
}

def _map_agent_args_to_sheet_headers(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if k in SHEET_HEADER_MAP:
            out[SHEET_HEADER_MAP[k]] = v
        else:
            out[k] = v
    return out

# ====== AWS Secrets -> Google Sheets client ======
def _get_sa_creds():
    sm = boto3.client("secretsmanager")
    sec = sm.get_secret_value(SecretId=SECRET_ID)["SecretString"]
    sa_info = json.loads(sec)
    return service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )

def _svc():
    creds = _get_sa_creds()
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

# ====== Helpers ======
def _col_letter(idx_zero_based: int) -> str:
    letters = ""
    n = idx_zero_based + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

def _normalize_bool(v):
    if v is None:
        return ""
    s = str(v).strip().lower()
    if s in {"y", "yes", "true", "1", "✓"}:
        return "Y"
    if s in {"n", "no", "false", "0"}:
        return "N"
    return s

from zoneinfo import ZoneInfo

LA_TZ = ZoneInfo("America/Los_Angeles")

def _today_iso():
    # Use your local day, not UTC
    return datetime.datetime.now(LA_TZ).date().isoformat()

def _parse_date_str(s: str):
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    # Try YYYY-MM-DD
    try:
        y, m, d = s.split("-")
        return datetime.date(int(y), int(m), int(d))
    except Exception:
        pass
    # Try M/D/Y or MM/DD/YYYY
    m = _DATE_RE.search(s)
    if m and "/" in m.group(1):
        mm, dd, yy = m.group(1).split("/")
        yy = int(yy)
        if yy < 100:
            yy = 2000 + yy if yy < 70 else 1900 + yy
        return datetime.date(int(yy), int(mm), int(dd))
    return None

# Extra helper to override bogus/default dates
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")

def _should_override_date(input_text: str | None, date_str: str | None, threshold_days: int = 3) -> bool:
    """
    If the user did NOT type a date in inputText and the provided date
    is missing/unparseable or differs from 'today' by >= threshold_days,
    override to today (America/Los_Angeles).
    """
    # If user explicitly typed a date, never override
    if input_text and _DATE_RE.search(input_text):
        return False

    d = _parse_date_str(date_str) if date_str else None
    if d is None:
        return True

    today = datetime.datetime.now(LA_TZ).date()
    return abs((d - today).days) >= threshold_days


# ====== Upsert (merge-only for provided fields) ======
def add_or_upsert(values_by_header, force_top_insert_today=False):
    svc = _svc()  # spreadsheets() resource

    headers_resp = svc.values().get(
        spreadsheetId=SHEET_ID,
        range=f"{RANGE_NAME}!1:1"
    ).execute()
    headers = headers_resp.get("values", [[]])[0]
    if not headers:
        raise RuntimeError("Header row empty. Put your headers in row 1.")

    incoming_date = values_by_header.get("Date") or _today_iso()
    values_by_header["Date"] = incoming_date

    date_col_idx = headers.index("Date")
    date_col_letter = _col_letter(date_col_idx)

    dates_resp = svc.values().get(
        spreadsheetId=SHEET_ID,
        range=f"{RANGE_NAME}!{date_col_letter}2:{date_col_letter}"
    ).execute()
    date_cells = dates_resp.get("values", [])
    target_row_idx = None
    for i, cell in enumerate(date_cells, start=2):
        if cell and len(cell) > 0 and str(cell[0]) == incoming_date:
            target_row_idx = i
            break

    last_col_letter = _col_letter(len(headers) - 1)

    def fmt(v, h):
        return _normalize_bool(v) if h.endswith("(Y/N)") else ("" if v is None else str(v))

    # If the date already exists, update it in place
    if target_row_idx:
        existing_resp = svc.values().get(
            spreadsheetId=SHEET_ID,
            range=f"{RANGE_NAME}!A{target_row_idx}:{last_col_letter}{target_row_idx}"
        ).execute()
        existing_row = existing_resp.get("values", [[]])
        existing_row = existing_row[0] if existing_row else []
        current_values = existing_row + [""] * (len(headers) - len(existing_row))

        for col_idx, h in enumerate(headers):
            if h in values_by_header:
                current_values[col_idx] = fmt(values_by_header[h], h)

        body = {"values": [current_values]}
        svc.values().update(
            spreadsheetId=SHEET_ID,
            range=f"{RANGE_NAME}!A{target_row_idx}",
            valueInputOption="USER_ENTERED",
            body=body
        ).execute()

        changed = {h: values_by_header[h] for h in headers if h in values_by_header}
        return {
            "action": "update",
            "row": target_row_idx,
            "date": incoming_date,
            "headers": headers,
            "changed": changed,
            "written": current_values
        }

    # No existing row for this date → create one
    # If caller asked for top insert AND the date is today, insert at row 2
    if force_top_insert_today and incoming_date == _today_iso():
        # Insert a new empty row at row index 1 (0-based), i.e., spreadsheet row 2
        sheet_id = _get_sheet_id(svc)
        svc.batchUpdate(
            spreadsheetId=SHEET_ID,
            body={
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": 1,
                                "endIndex": 2
                            },
                            "inheritFromBefore": False
                        }
                    }
                ]
            }
        ).execute()

        # Build the new row values
        new_values = [""] * len(headers)
        for col_idx, h in enumerate(headers):
            if h in values_by_header:
                new_values[col_idx] = fmt(values_by_header[h], h)

        svc.values().update(
            spreadsheetId=SHEET_ID,
            range=f"{RANGE_NAME}!A2",
            valueInputOption="USER_ENTERED",
            body={"values": [new_values]}
        ).execute()

        changed = {h: values_by_header[h] for h in headers if h in values_by_header}
        return {
            "action": "prepend",
            "row": 2,
            "date": incoming_date,
            "headers": headers,
            "changed": changed,
            "written": new_values
        }

    # Otherwise append to bottom (existing behavior)
    new_values = [""] * len(headers)
    for col_idx, h in enumerate(headers):
        if h in values_by_header:
            new_values[col_idx] = fmt(values_by_header[h], h)

    svc.values().append(
        spreadsheetId=SHEET_ID,
        range=RANGE_NAME,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [new_values]}
    ).execute()

    changed = {h: values_by_header[h] for h in headers if h in values_by_header}
    return {
        "action": "append",
        "row": "new",
        "date": incoming_date,
        "headers": headers,
        "changed": changed,
        "written": new_values
    }


# ====== Bedrock event extraction ======
def _coerce_kv_list_to_dict(x):
    # Accept [{"name":"date","value":"2025-09-27"}, ...] or [{"key":...,"value":...}]
    if isinstance(x, list):
        out = {}
        for item in x:
            if isinstance(item, dict):
                k = item.get("name") or item.get("key")
                v = item.get("value")
                if k is not None:
                    out[str(k)] = v
        return out
    return None

def _parse_possible_json(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return None
    return x if isinstance(x, dict) else None

def _deep_find_parameters(obj):
    if isinstance(obj, dict):
        if "parameters" in obj:
            return obj["parameters"]
        for v in obj.values():
            found = _deep_find_parameters(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_parameters(v)
            if found is not None:
                return found
    return None

def _extract_payload(event):
    params = None
    if isinstance(event, dict):
        if "parameters" in event:
            params = event["parameters"]
        else:
            params = _deep_find_parameters(event)

    if params is not None:
        parsed = _parse_possible_json(params)
        if isinstance(parsed, dict):
            return parsed
        kv = _coerce_kv_list_to_dict(params)
        if isinstance(kv, dict):
            return kv

    # API Gateway-ish: {"body": "...json..."} or dict
    if isinstance(event, dict) and "body" in event:
        raw = event["body"]
        parsed = _parse_possible_json(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
        if isinstance(parsed, dict):
            return parsed

    # Fallback: accept simple dicts of primitives
    if isinstance(event, dict) and any(isinstance(v, (str, int, float, bool)) for v in event.values()):
        return event

    return {}

# ====== Free-text fallback parser (if only inputText arrives) ======
_num = r"([-+]?\d+(?:\.\d+)?)"
def _parse_freeform_text(s: str) -> dict:
    if not isinstance(s, str) or not s.strip():
        return {}
    text = s.lower()
    out = {}

    m = re.search(r"\bweight\s*"+_num+r"\s*(?:lb|lbs)?\b", text)
    if m: out["weight_lbs"] = float(m.group(1))

    m = re.search(r"\bwaist\s*"+_num+r"\s*(?:in|inch|inches)?\b", text)
    if m: out["waist_in"] = float(m.group(1))

    m = re.search(r"\bsteps?\s*([0-9][0-9,]*)\b", text)  # "steps 10842" or "10,842 steps"
    if m: out["steps"] = int(m.group(1).replace(",", ""))

    m = re.search(r"\bjog(?:ged)?\s*"+_num+r"\s*(?:mi|mile|miles)\b", text)  # "jogged 3.1 miles"
    if m:
        out["jog_miles"] = float(m.group(1))
        out["jog_walk"] = "Y"
    elif "jog" in text or "run" in text:
        out["jog_walk"] = "Y"

    if "after-dinner walk" in text or "after dinner walk" in text:
        out["after_dinner_walk"] = "Y"

    if re.search(r"\b(resistance|resist)\s+training\b", text) or "lifting" in text or "weights" in text:
        out["resist_training"] = "Y"

    m = re.search(r"\bcalories?\s*([0-9][0-9,]*)\b", text)   # "calories 2520"
    if m: out["calories_in"] = int(m.group(1).replace(",", ""))

    if "not controlled" in text or "uncontrolled" in text:
        out["calories_controlled"] = "N"
    elif "controlled" in text:
        out["calories_controlled"] = "Y"

    m = re.search(r"\bprotein\s*"+_num+r"\s*(?:g|gram|grams)?\b", text)  # "protein 160 g"
    if m: out["protein_intake_g"] = float(m.group(1))
    if "protein target hit" in text or "hit protein" in text:
        out["protein_target_hit"] = "Y"
    if "missed protein" in text:
        out["protein_target_hit"] = "N"

    m = re.search(r"notes?\s*:\s*(.*)$", s, re.IGNORECASE)
    if m:
        out["notes"] = m.group(1).strip()

    return out


def _safe_preview(x):
    try:
        return json.dumps(x)[:4000] if isinstance(x, (dict, list)) else str(type(x))
    except Exception:
        return str(type(x))

def _get_sheet_id(svc):
    # svc is your spreadsheets() resource from _svc()
    meta = svc.get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(sheetId,title))"
    ).execute()
    for s in meta.get("sheets", []):
        p = s.get("properties", {})
        if p.get("title") == RANGE_NAME:
            return p.get("sheetId")
    raise RuntimeError(f"Could not find sheetId for tab '{RANGE_NAME}'")

def _ok_function(event, body_dict, code=200):
    """
    Return a properly formatted Bedrock Action Group function response.
    """
    resp = {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", "action_group_healthlog_updater"),
            "function": event.get("function", "upsert_log"),
            "functionResponse": {
                "responseBody": {
                    "TEXT": {
                        "body": json.dumps(body_dict)
                    }
                }
            }
        }
    }
    
    log.info("FINAL RETURN JSON (Bedrock compatible): %s", json.dumps(resp, indent=2))
    return resp

# ====== Handler ======
def lambda_handler(event, context):
    log.info("HELLO_FROM_HANDLER start")
    log.info("RAW EVENT: %s", _safe_preview(event))

    try:
        # 1) Extract structured params
        body = _extract_payload(event)
        
        input_text = event.get("inputText") if isinstance(event, dict) else None

        # If we only got free text (or want to augment), parse it:
        if isinstance(input_text, str) and input_text.strip():
            try:
                parsed_free = _parse_freeform_text(input_text)
                if isinstance(parsed_free, dict) and parsed_free:
                    # Only fill keys that aren't already present
                    for k, v in parsed_free.items():
                        body.setdefault(k, v)
            except Exception:
                pass

        # If user said "today", force today's date and request top-prepend behavior
        force_top_insert_today = False
        if isinstance(input_text, str) and "today" in input_text.lower():
            body["date"] = _today_iso()
            force_top_insert_today = True


        # 2) 2-param workaround: unpack JSON string in "payload"
        if isinstance(body.get("payload"), str):
            try:
                extra = json.loads(body["payload"])
                if isinstance(extra, dict):
                    body.update(extra)
            except Exception:
                pass
        body.pop("payload", None)

        # 3) Use today (LA) if agent passed an old/default date and the user didn't type a date
        input_text = event.get("inputText") if isinstance(event, dict) else None
        if _should_override_date(input_text, body.get("date") or body.get("Date")):
            body["date"] = _today_iso()

        log.info("Extracted keys: %s", sorted(list(body.keys())))

        # 4) Map to Sheet headers
        mapped = _map_agent_args_to_sheet_headers(body)

        # 5) Guard: require at least one meaningful field besides Date
        meaningful_headers = {
            "Weight (lbs)","Waist (in)","Calories Controlled (Y/N)","Calories In (~2,450 cal/day)",
            "Protein Target Hit (Y/N)","Protein Intake (~160g)","Steps","Jog/Walk (Y/N)",
            "Jog Mls.","After-Dinner Walk (Y/N)","Resist Training (Y/N)","Notes"
        }
        if not any(h in mapped for h in meaningful_headers):
            return _ok_function(
                event,
                {"error": "no_parameters", "message": "No structured fields detected; no update performed."},
                400
            )

        # 6) Write to Google Sheets
        result = add_or_upsert(mapped, force_top_insert_today=force_top_insert_today)
        log.info("UPSERT RESULT: %s", json.dumps(result)[:800])

        # 7) Return Bedrock "function" envelope (NOT API-style)
        #    _ok_function will echo event['function'] (e.g., 'upsert_log')
        return _ok_function(event, result, 200)

    except Exception as e:
        log.error("Unhandled exception in lambda_handler\n" + traceback.format_exc())
        return _ok_function(
            event,
            {"error": "bad_request", "message": str(e)}
        )
