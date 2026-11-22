import json
import math
import os
import random
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from hadal_grammar import check_grammar_locally
from hadal_research import (
    RESEARCH_RESPONSE_INSTRUCTIONS,
    TINYFISH_RESEARCH_URL,
    build_tinyfish_query,
)

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional dependency for the GUI app
    def load_dotenv(*_args, **_kwargs):
        return False

APP_NAME = "HADAL"
DEFAULT_THEME = "dark"
ORGANIZATION_METHODS = {"type", "name", "size", "modified"}
ORGANIZATION_SORTS = {
    "name",
    "modified_newest",
    "modified_oldest",
    "size_largest",
    "size_smallest",
}
DEFAULT_SIZE_THRESHOLDS_MB = (1, 100)
MAX_DUPLICATE_SCAN_FILES = 10000
SETTINGS_PATH = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "HADAL" / "settings.json"
SIDEBAR_MIN_WIDTH = 100
SIDEBAR_MAX_WIDTH = 400
SIDEBAR_DEFAULT_WIDTH = 200
ANIMATION_INTERVAL_MS = 8


def organization_folder_name(source, method, stat_result=None):
    if method == "type":
        return source.suffix[1:].upper() if source.suffix else "_NO_EXTENSION"
    if method == "name":
        first_character = source.name.lstrip(".")[:1].upper()
        return first_character if first_character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" else "_OTHER"
    if method == "size":
        file_size = (stat_result or source.stat()).st_size
        small_mb, large_mb = DEFAULT_SIZE_THRESHOLDS_MB
        if file_size < small_mb * 1_000_000:
            return f"Small - under {small_mb:g} MB"
        if file_size < large_mb * 1_000_000:
            return f"Medium - {small_mb:g} to {large_mb:g} MB"
        return f"Large - {large_mb:g} MB or more"
    if method == "modified":
        modified_time = (stat_result or source.stat()).st_mtime
        return datetime.fromtimestamp(modified_time).strftime("%Y-%m")
    raise ValueError("Choose file type, name, size, or date modified organization.")


def build_organization_preview(root, method, sort_by):
    if not isinstance(method, str) or method not in ORGANIZATION_METHODS:
        raise ValueError("Choose file type, name, size, or date modified organization.")
    if not isinstance(sort_by, str) or sort_by not in ORGANIZATION_SORTS:
        raise ValueError("Choose a supported sort order.")
    moves = []
    skipped = []

    for source in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        try:
            if source.is_symlink() or not source.is_file():
                continue
            stat_result = source.stat()
            if not is_inside(source.resolve(strict=True), root):
                continue

            folder_name = organization_folder_name(source, method, stat_result)
            destination = root / folder_name / source.name
            if destination.parent.is_symlink() or (
                destination.parent.exists() and not destination.parent.is_dir()
            ):
                skipped.append({
                    "source": source.name,
                    "reason": "Destination folder name is already used by a file.",
                })
                continue
            if destination.exists() or destination.is_symlink():
                skipped.append({
                    "source": source.name,
                    "reason": "Destination already exists.",
                })
                continue

            if sort_by == "name":
                order_value = source.name.casefold()
            elif sort_by == "modified_newest":
                order_value = -stat_result.st_mtime
            elif sort_by == "modified_oldest":
                order_value = stat_result.st_mtime
            elif sort_by == "size_largest":
                order_value = -stat_result.st_size
            else:
                order_value = stat_result.st_size
            moves.append({
                "source_name": source.name,
                "destination_folder": folder_name,
                "destination_name": source.name,
                "_order_value": order_value,
            })
        except (OSError, RuntimeError):
            skipped.append({
                "source": source.name,
                "reason": "Could not safely inspect this file.",
            })

    moves.sort(key=lambda item: (
        item["destination_folder"].casefold(),
        item["_order_value"],
        item["source_name"].casefold(),
    ))
    for item in moves:
        item.pop("_order_value")
    return moves, skipped


def run_tinyfish_research(
    query, api_key, language="en", session_id=None, opener=None, on_delta=None
):
    query = build_tinyfish_query(query)
    if not api_key:
        raise ValueError("TINYFISH_API_KEY is not configured.")

    payload = {"query": query, "mode": "instant", "stream": True, "output_language": language}
    if session_id:
        payload["session_id"] = session_id
    request = urllib.request.Request(
        TINYFISH_RESEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    open_request = opener or urllib.request.urlopen
    try:
        response_context = open_request(request, timeout=2700)
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"TinyFish returned HTTP {exc.code}: {details or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to TinyFish: {exc.reason}") from exc

    result = None
    citations = []
    new_session_id = session_id
    streamed_text = ""
    with response_context as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError as exc:
                raise RuntimeError("TinyFish returned an invalid event stream.") from exc
            if not isinstance(event, dict):
                raise RuntimeError("TinyFish returned an invalid event.")
            event_type = event.get("event")
            if event_type == "session":
                new_session_id = event.get("session_id", new_session_id)
            elif event_type == "final_result":
                result = event.get("result")
                citations = event.get("citations") or []
            elif event_type == "synthesis_delta":
                delta = event.get("delta", "")
                if not isinstance(delta, str):
                    raise RuntimeError("TinyFish returned an invalid response update.")
                streamed_text += delta
                if on_delta and delta:
                    on_delta(streamed_text)
            elif event_type == "synthesis_discarded":
                streamed_text = ""
                if on_delta:
                    on_delta("")
            elif event_type == "error":
                details = event.get("message") or event.get("error")
                if isinstance(details, dict):
                    details = details.get("message") or json.dumps(details, ensure_ascii=False)
                raise RuntimeError(str(details or "TinyFish research failed."))
            elif event_type == "done":
                break

    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("TinyFish finished without returning a research report.")
    if not isinstance(citations, list):
        raise RuntimeError("TinyFish returned citations in an unexpected format.")
    return {"result": result.strip(), "citations": citations, "session_id": new_session_id}


def format_research_report(report):
    return (report or "").strip()
INTERFACE_PRESETS = {
    "emerald": {
        "accent": "#10a37f",
        "dark": {
            "main": "#151a18", "sidebar": "#0d1310", "card": "#1b2420",
            "border": "#303b35", "user_bubble": "#20382d", "selection": "#294536",
        },
    },
    "blue": {
        "accent": "#3b82f6",
        "dark": {
            "main": "#101923", "sidebar": "#0b121c", "card": "#162230",
            "border": "#29394a", "user_bubble": "#1c3346", "selection": "#29465d",
        },
    },
    "violet": {
        "accent": "#8b5cf6",
        "dark": {
            "main": "#171321", "sidebar": "#110d1b", "card": "#211a30",
            "border": "#39304c", "user_bubble": "#302443", "selection": "#403354",
        },
    },
    "rose": {
        "accent": "#e65383",
        "dark": {
            "main": "#1f1417", "sidebar": "#170d10", "card": "#2a1a20",
            "border": "#453038", "user_bubble": "#3c242e", "selection": "#513341",
        },
    },
    "amber": {
        "accent": "#d99a24",
        "dark": {
            "main": "#1c1910", "sidebar": "#141108", "card": "#282316",
            "border": "#423b29", "user_bubble": "#3b311d", "selection": "#51452b",
        },
    },
    "black": {
        "accent": "#8b8b8b",
        "dark": {
            "main": "#000000", "sidebar": "#050505", "card": "#111111",
            "border": "#303030", "user_bubble": "#252525", "selection": "#383838",
        },
    },
}
UI_TEXT = {
    "new_chat": {"en": "+   New chat", "hi": "+   नई चैट"},
    "chat_tab": {"en": "Chat", "hi": "चैट"},
    "organizer_tab": {"en": "File organizer", "hi": "फ़ाइल व्यवस्थापक"},
    "grammar_tab": {"en": "Grammar & Spelling", "hi": "व्याकरण और वर्तनी"},
    "grammar_title": {"en": "Grammar checker", "hi": "व्याकरण जाँच"},
    "grammar_subtitle": {
        "en": "English spelling and common grammar checks run locally. Text is never sent to TinyFish.",
        "hi": "अंग्रेज़ी वर्तनी और सामान्य व्याकरण जाँच आपके डिवाइस पर होती है। पाठ TinyFish को नहीं भेजा जाता।",
    },
    "grammar_input": {"en": "TEXT TO CHECK", "hi": "जाँचने के लिए पाठ"},
    "grammar_placeholder": {
        "en": "Paste or type your text here…",
        "hi": "अपना पाठ यहाँ लिखें या चिपकाएँ…",
    },
    "grammar_check": {
        "en": "Check Grammar & Spelling",
        "hi": "व्याकरण और वर्तनी जाँचें",
    },
    "grammar_result": {"en": "CORRECTED TEXT", "hi": "सुधारा हुआ पाठ"},
    "grammar_empty": {"en": "Enter some text to check.", "hi": "जाँचने के लिए पाठ लिखें।"},
    "grammar_no_issues": {
        "en": "No common issues were detected by the basic local checker.",
        "hi": "स्थानीय जाँच में कोई सामान्य त्रुटि नहीं मिली।",
    },
    "grammar_issues": {"en": "Suggestions: ", "hi": "सुझाव: "},
    "organizer_title": {"en": "File organizer", "hi": "फ़ाइल व्यवस्थापक"},
    "organizer_subtitle": {
        "en": "Choose a folder and how to group its files. Review the preview before confirming moves.",
        "hi": "फ़ोल्डर और फ़ाइलों को व्यवस्थित करने का तरीका चुनें। बदलाव की पुष्टि से पहले पूर्वावलोकन देखें।",
    },
    "organize_method": {"en": "Organize by", "hi": "व्यवस्थित करने का तरीका"},
    "organize_type": {"en": "File type", "hi": "फ़ाइल प्रकार"},
    "organize_name": {"en": "Name (A–Z)", "hi": "नाम (A–Z)"},
    "organize_size": {"en": "File size", "hi": "फ़ाइल का आकार"},
    "organize_modified": {"en": "Date modified (Year/Month)", "hi": "बदलने की तारीख (वर्ष/महीना)"},
    "organize_sort": {"en": "Preview sort", "hi": "पूर्वावलोकन क्रम"},
    "organize_sort_name": {"en": "Name A–Z", "hi": "नाम A–Z"},
    "organize_sort_modified_newest": {"en": "Newest first", "hi": "नवीनतम पहले"},
    "organize_sort_modified_oldest": {"en": "Oldest first", "hi": "सबसे पुराना पहले"},
    "organize_sort_size_largest": {"en": "Largest first", "hi": "सबसे बड़ा पहले"},
    "organize_sort_size_smallest": {"en": "Smallest first", "hi": "सबसे छोटा पहले"},
    "organize_review_duplicates": {"en": "Review duplicates", "hi": "डुप्लिकेट देखें"},
    "organize_duplicate_title": {"en": "Possible duplicate files", "hi": "संभावित डुप्लिकेट फ़ाइलें"},
    "organize_duplicate_note": {
        "en": "Scans recursively. Matches use filename and size only; file contents are not read, and nothing is deleted.",
        "hi": "फ़ोल्डरों में भी खोजता है। मिलान केवल नाम और आकार पर आधारित हैं; फ़ाइल सामग्री नहीं पढ़ी जाती और कुछ भी हटाया नहीं जाता।",
    },
    "organize_preview": {"en": "Preview", "hi": "पूर्वावलोकन"},
    "organize_apply": {"en": "Apply preview", "hi": "पूर्वावलोकन लागू करें"},
    "organize_select_folder": {"en": "Select a folder first.", "hi": "पहले एक फ़ोल्डर चुनें।"},
    "organize_preview_title": {"en": "Organization preview", "hi": "व्यवस्था का पूर्वावलोकन"},
    "folders": {"en": "SELECTED FOLDERS", "hi": "चुने हुए फ़ोल्डर"},
    "folder_count": {"en": "{count} folders connected", "hi": "{count} फ़ोल्डर जुड़े"},
    "folder_count_one": {"en": "{count} folder connected", "hi": "{count} फ़ोल्डर जुड़ा"},
    "add_folder": {"en": "+  Add folder", "hi": "+  फ़ोल्डर जोड़ें"},
    "remove_folder": {"en": "−  Remove selected", "hi": "−  चुना हुआ हटाएँ"},
    "privacy": {
        "en": "Research questions go to TinyFish.\nSelected folders stay on your device.\nFile contents are never read.\nDrag divider to resize sidebar.",
        "hi": "शोध प्रश्न TinyFish को भेजे जाते हैं।\nचुने हुए फ़ोल्डर आपके डिवाइस पर रहते हैं।\nफ़ाइल सामग्री कभी नहीं पढ़ी जाती।\nसाइडबार का आकार बदलने के लिए रेखा खींचें।",
    },
    "new_conversation": {"en": "New conversation", "hi": "नई बातचीत"},
    "settings": {"en": "⚙  Settings", "hi": "⚙  सेटिंग्स"},
    "settings_title": {"en": "Settings", "hi": "सेटिंग्स"},
    "settings_subtitle": {"en": "Personalize your HADAL experience", "hi": "HADAL को अपनी पसंद के अनुसार बदलें"},
    "dark_theme": {"en": "Dark theme", "hi": "डार्क थीम"},
    "accent_color": {"en": "Color preset", "hi": "रंग प्रीसेट"},
    "preset_emerald": {"en": "Emerald", "hi": "पन्ना"},
    "preset_blue": {"en": "Ocean blue", "hi": "समुद्री नीला"},
    "preset_violet": {"en": "Violet", "hi": "बैंगनी"},
    "preset_rose": {"en": "Rose", "hi": "गुलाबी"},
    "preset_amber": {"en": "Amber", "hi": "एम्बर"},
    "preset_black": {"en": "Black", "hi": "काला"},
    "resize_sidebar": {"en": "Drag to resize sidebar", "hi": "साइडबार का आकार बदलने के लिए खींचें"},
    "language": {"en": "Language", "hi": "भाषा"},
    "close": {"en": "Done", "hi": "हो गया"},
    "settings_save_error": {"en": "Could not save settings.", "hi": "सेटिंग्स सहेजी नहीं जा सकीं।"},
    "composer_hint": {
        "en": "Message HADAL…  (Ctrl+Enter to send)",
        "hi": "HADAL को संदेश लिखें…  (भेजने के लिए Ctrl+Enter)",
    },
    "footer": {
        "en": "Research by TinyFish · Local file actions stay on your device",
        "hi": "TinyFish द्वारा शोध · फ़ाइल कार्य आपके डिवाइस पर",
    },
    "research_status": {
        "en": "Assistant: Diving into the depths…",
        "hi": "Assistant: गहराइयों में खोज रहा है…",
    },
    "tinyfish_key_error": {
        "en": "HADAL needs a TinyFish API key.\n"
        "Create a .env file beside the app containing:\n"
        "TINYFISH_API_KEY=your_tinyfish_key_here\n"
        "Install .env support with: py -m pip install python-dotenv\n"
        "Get a key from https://agent.tinyfish.ai/api-keys, then restart HADAL.\n"
        "Keep the key private and never commit the .env file. TinyFish Research availability or account limits may apply.",
        "hi": "HADAL को TinyFish API कुंजी चाहिए।\n"
        "ऐप के पास .env फ़ाइल बनाएँ और इसमें जोड़ें:\n"
        "TINYFISH_API_KEY=your_tinyfish_key_here\n"
        ".env समर्थन के लिए चलाएँ: py -m pip install python-dotenv\n"
        "https://agent.tinyfish.ai/api-keys से कुंजी लें, फिर HADAL फिर शुरू करें।\n"
        "कुंजी निजी रखें और .env फ़ाइल कमिट न करें। TinyFish Research की उपलब्धता या खाता सीमाएँ लागू हो सकती हैं।",
    },
    "ready": {
        "en": (
            "HADAL is ready.\n"
            "To organize files:\n"
            "1. Click Add folder and choose a folder. Use its number from the sidebar.\n"
            "2. In File organizer, select File type, Name (A–Z), or Size, then preview.\n"
            "3. Review the preview, then choose Apply preview and confirm the move.\n"
            "File actions stay on your device; file contents are never read."
        ),
        "hi": (
            "HADAL तैयार है।\n"
            "फ़ाइलें व्यवस्थित करने के लिए:\n"
            "1. फ़ोल्डर जोड़ें पर क्लिक करके फ़ोल्डर चुनें। साइडबार में उसका नंबर देखें।\n"
            "2. फ़ाइल व्यवस्थापक में प्रकार, नाम (A–Z), या आकार चुनकर पूर्वावलोकन करें।\n"
            "3. पूर्वावलोकन जाँचें, फिर पूर्वावलोकन लागू करें चुनें और बदलाव की पुष्टि करें।\n"
            "फ़ाइल के काम आपके डिवाइस पर होते हैं; फ़ाइल सामग्री कभी नहीं पढ़ी जाती।"
        ),
    },
    "quick_list": {"en": "List files", "hi": "फ़ाइलें सूचीबद्ध करें"},
    "quick_search": {"en": "Search filenames", "hi": "फ़ाइल नाम खोजें"},
    "quick_organize": {"en": "Preview organization", "hi": "व्यवस्था का पूर्वावलोकन"},
    "quick_apply": {"en": "Apply preview", "hi": "पूर्वावलोकन लागू करें"},
}

# For a PyInstaller one-file build, configuration lives beside the .exe,
# not inside PyInstaller's temporary extraction directory.
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class FileAssistant:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        if not self.root.exists():
            raise ValueError("The selected location does not exist.")
        if not self.root.is_dir():
            raise ValueError("The selected location is not a folder.")
        self.organization_plan = None

    def _resolve_folder(self, subfolder):
        if subfolder is None:
            subfolder = ""
        if not isinstance(subfolder, str):
            raise ValueError("Folder path must be text.")
        rel = Path(subfolder)
        if rel.is_absolute() or any(part == ".." for part in rel.parts):
            raise ValueError("That path is outside the selected folder.")
        folder = (self.root / rel).resolve(strict=False)
        try:
            folder.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("That path is outside the selected folder.") from exc
        if not folder.exists() or not folder.is_dir():
            raise ValueError("That path is not a folder.")
        return folder

    def list_files(self, subfolder=""):
        folder = self._resolve_folder(subfolder)
        entries = []
        for item in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            try:
                if item.is_symlink():
                    continue
                resolved = item.resolve(strict=True)
                if not is_inside(resolved, self.root):
                    continue
                if item.is_dir():
                    entries.append(item.name + "/")
                else:
                    entries.append(item.name)
            except (OSError, RuntimeError):
                continue
        return {"entries": entries}

    def search_files(self, query):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Search text cannot be empty.")
        needle = query.casefold()
        matches = []
        for path in self.root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve(strict=True)
                if not is_inside(resolved, self.root):
                    continue
                if needle in path.name.casefold():
                    rel = str(path.relative_to(self.root)).replace("/", os.sep)
                    matches.append(rel)
            except (OSError, RuntimeError):
                continue
        matches.sort(key=lambda value: value.casefold())
        return {"matches": matches}

    def preview_organization(self, method="type", sort_by="name"):
        if not isinstance(method, str):
            raise ValueError("Choose file type, name, size, or date modified organization.")
        plan, skipped = build_organization_preview(self.root, method, sort_by)
        self.organization_plan = {
            "root": self.root,
            "method": method,
            "moves": plan,
        }
        return {
            "method": method,
            "sort_by": sort_by,
            "note": "Files are ordered within destination groups in this preview and its confirmation; Windows controls how folders are displayed.",
            "count": len(plan),
            "moves": [
                {"source": item["source_name"], "destination": f'{item["destination_folder"]}/{item["destination_name"]}'}
                for item in plan
            ],
            "skipped": skipped,
        }

    def apply_organization(self):
        if not self.organization_plan:
            return {"status": "No current preview. Preview an organization plan first."}
        root = self.organization_plan["root"]
        moves = self.organization_plan["moves"]
        if not moves:
            self.organization_plan = None
            return {"status": "Nothing needs to be moved."}

        response = input(
            "Move " + str(len(moves)) + " file(s)? [yes/no]: "
        ).strip().lower()
        if response not in {"y", "yes"}:
            self.organization_plan = None
            return {"status": "Cancelled by user. No files were moved."}

        moved = []
        skipped = []
        for item in moves:
            source_name = item["source_name"]
            folder_name = item["destination_folder"]
            source = root / source_name
            try:
                if source.is_symlink() or not source.is_file() or not is_inside(source.resolve(strict=True), root):
                    skipped.append({"source": source_name, "reason": "Source is missing or unsafe."})
                    continue
                destination_dir = root / folder_name
                if destination_dir.exists() and (destination_dir.is_symlink() or not destination_dir.is_dir()):
                    skipped.append({"source": source_name, "reason": "Destination folder is not a safe directory."})
                    continue
                destination_dir.mkdir(exist_ok=True)
                destination = destination_dir / source_name
                if destination.exists() or destination.is_symlink():
                    skipped.append({"source": source_name, "reason": "Destination already exists."})
                    continue
                source.rename(destination)
                moved.append({"source": source_name, "destination": f"{folder_name}/{source_name}"})
            except (OSError, RuntimeError, ValueError) as exc:
                skipped.append({"source": source_name, "reason": str(exc)})
        self.organization_plan = None
        return {"status": "Organization finished.", "moved": moved, "skipped": skipped}


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_root(path_text: str) -> Path:
    """Resolve and validate a user-selected root directory."""
    if not isinstance(path_text, (str, os.PathLike)):
        raise ValueError("The selected location must be a folder path.")
    root = Path(path_text).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The selected location is not a folder.")
    return root


def safe_child(root: Path, relative: str, must_exist: bool = True) -> Path:
    """Resolve a relative path and ensure it stays within its selected root."""
    if not isinstance(relative, str):
        raise ValueError("Path must be text.")
    rel = Path(relative)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise ValueError("Absolute paths and '..' are not allowed.")
    candidate = root / rel
    candidate = candidate.resolve(strict=must_exist)
    if not is_inside(candidate, root):
        raise ValueError("That path is outside the selected folder.")
    return candidate


class LocalFileAIApp:
    def __init__(self, root):
        self.root = root
        self.chat_font = tkfont.Font(root=root, family="Segoe UI", size=11)
        self.quick_action_font = tkfont.Font(root=root, family="Segoe UI", size=9)
        self.root.title(APP_NAME)
        self.root.geometry("1000x720")
        self.root.minsize(760, 560)

        self.folders = []
        self.organization_plan = None
        self.organization_method = "type"
        self.organization_sort_by = "name"
        self.organization_options = {"sort_by": "name"}
        self.busy = False
        self.tinyfish_api_key = os.getenv("TINYFISH_API_KEY", "").strip()
        self.research_session_id = None
        self.chat_items = []
        self.connection_status = "checking"
        self.language = "en"
        self.accent_preset = "emerald"
        self.sidebar_width = SIDEBAR_DEFAULT_WIDTH
        self.settings_error = None
        self._animation_after_ids = set()
        self._animation_last_run = {}
        self._animation_delta = 1.0
        self._background_bubbles = []
        self._chat_splash_particles = []
        self._background_phase = 0.0
        self._background_mode = None
        self._background_size = None
        self._chat_current_particles = []
        self._grammar_bubble_particles = []
        self._grammar_text_length = 0
        self._surface_wave_items = []
        self._surface_size = None
        self._surface_phase = 0.0
        self._sidebar_river_items = []
        self._sidebar_river_size = None
        self._sidebar_river_phase = 0.0
        self._sidebar_river_objects = {}
        self._organizer_bubbles = []
        self._grammar_bubbles = []
        self._organizer_bubble_canvases = []
        self._grammar_bubble_canvases = []
        self._organizer_bubble_phase = 0.0
        self._composer_river_items = []
        self._composer_river_decorations = []
        self._composer_river_objects = {}
        self._composer_river_size = None
        self._composer_river_phase = 0.0
        self._prompt_bubble_particles = []
        self._prompt_text_length = 0
        self._composer_bubble_emission_active = False
        self._composer_bubble_emit_ticks = 0
        self._has_user_messages = False
        self._load_settings()

        self._build_ui()
        self.root.bind("<Destroy>", self._stop_animations, add="+")
        self._animate_chat_background()
        self._animate_ocean_surface()
        self._animate_composer_river()
        self._animate_sidebar_river()
        self._animate_organizer_bubbles()
        if self.settings_error:
            self._append(f"Could not load saved settings: {self.settings_error}", role="system")
        self._load_api_key()

    def _load_settings(self):
        try:
            if not SETTINGS_PATH.exists():
                return
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            language = settings.get("language", "en")
            accent_preset = settings.get("accent_preset")
            legacy_accent = settings.get("accent_color")
            sidebar_width = settings.get("sidebar_width", SIDEBAR_DEFAULT_WIDTH)
            if language in {"en", "hi"}:
                self.language = language
            if accent_preset in INTERFACE_PRESETS:
                self.accent_preset = accent_preset
            elif legacy_accent in {
                preset["accent"] for preset in INTERFACE_PRESETS.values()
            }:
                self.accent_preset = next(
                    key for key, preset in INTERFACE_PRESETS.items()
                    if preset["accent"] == legacy_accent
                )
            if isinstance(sidebar_width, int) and not isinstance(sidebar_width, bool):
                self.sidebar_width = max(SIDEBAR_MIN_WIDTH, min(SIDEBAR_MAX_WIDTH, sidebar_width))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.settings_error = str(exc)

    def _save_settings(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(
                json.dumps({
                    "language": self.language,
                    "accent_preset": self.accent_preset,
                    "sidebar_width": self.sidebar_width,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror(
                self._t("settings_title"),
                f"{self._t('settings_save_error')}\n{exc}",
                parent=self.root,
            )

    def _t(self, key):
        return UI_TEXT.get(key, {}).get(self.language, UI_TEXT.get(key, {}).get("en", key))

    @property
    def accent_color(self):
        return INTERFACE_PRESETS[self.accent_preset]["accent"]

    def _start_sidebar_resize(self, _event=None):
        self.sidebar_separator.configure(bg=self._theme_colors()["accent"])

    def _resize_sidebar(self, event):
        self.root.update_idletasks()
        requested = event.x_root - self.shell.winfo_rootx()
        available_max = max(
            SIDEBAR_MIN_WIDTH,
            min(SIDEBAR_MAX_WIDTH, self.shell.winfo_width() - 430),
        )
        self.sidebar_width = max(SIDEBAR_MIN_WIDTH, min(available_max, requested))
        self.sidebar.configure(width=self.sidebar_width)

    def _finish_sidebar_resize(self, _event=None):
        self.sidebar.configure(width=self.sidebar_width)
        self._save_settings()

    def _sidebar_divider_hover(self, _event=None):
        self.sidebar_separator.configure(bg=self._theme_colors()["accent"])

    def _sidebar_divider_unhover(self, _event=None):
        self.sidebar_separator.configure(bg=self._theme_colors()["sidebar_divider"])

    def _build_ui(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.style = style
        style.configure(".", font=("Segoe UI", 10))
        self.root.configure(bg="#f7f7f8")

        self.shell = tk.Frame(self.root, bg="#f7f7f8")
        self.shell.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(self.shell, width=self.sidebar_width, bg="#f0f1f3", padx=14, pady=16)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.sidebar_river = tk.Canvas(
            self.sidebar, width=72, bg="#f0f1f3",
            highlightthickness=0, bd=0, takefocus=0,
        )
        self.sidebar_river.place(
            relx=0.5, y=-16, relheight=1, height=32, width=72, anchor="n"
        )
        self.sidebar_river.bind("<Configure>", self._draw_sidebar_river)

        self.brand_label = tk.Label(
            self.sidebar, text=APP_NAME, bg="#f0f1f3", fg="#17201f",
            font=("Segoe UI", 15, "bold"), anchor="w"
        )
        self.brand_label.pack(fill="x", pady=(0, 24))
        self.brand_label.configure(text="◈  HADAL")
        self.chat_tab_button = self._create_rounded_button(
            self.sidebar, "Chat", lambda: self._show_page("chat"),
            variant="sidebar", anchor="w", height=42, padding=14, bold=True,
        )
        self.chat_tab_button.pack(fill="x", pady=(0, 8))
        self.organizer_tab_button = self._create_rounded_button(
            self.sidebar, "File organizer",
            lambda: self._show_page("organizer"),
            variant="sidebar", anchor="w", height=42, padding=14,
        )
        self.organizer_tab_button.pack(fill="x", pady=(0, 8))
        self.grammar_tab_button = self._create_rounded_button(
            self.sidebar, "Grammar", lambda: self._show_page("grammar"),
            variant="sidebar", anchor="w", height=42, padding=14,
        )
        self.grammar_tab_button.pack(fill="x")
        self.new_chat_button = self._create_rounded_button(
            self.sidebar, "+   New chat", self.new_chat,
            variant="sidebar", anchor="w", height=42, padding=14, bold=True,
        )

        self.sidebar_separator = tk.Frame(
            self.shell, width=6, bg="#555555", cursor="sb_h_double_arrow"
        )
        self.sidebar_separator.pack(side="left", fill="y")
        self.sidebar_separator.bind("<Button-1>", self._start_sidebar_resize)
        self.sidebar_separator.bind("<B1-Motion>", self._resize_sidebar)
        self.sidebar_separator.bind("<ButtonRelease-1>", self._finish_sidebar_resize)
        self.sidebar_separator.bind("<Enter>", self._sidebar_divider_hover)
        self.sidebar_separator.bind("<Leave>", self._sidebar_divider_unhover)

        self.main_panel = tk.Frame(self.shell, bg="#f7f7f8")
        self.main_panel.pack(side="left", fill="both", expand=True)

        self.chat_page = tk.Frame(self.main_panel, bg="#f7f7f8")
        self.organizer_page = tk.Frame(self.main_panel, bg="#f7f7f8")
        self.grammar_page = tk.Frame(self.main_panel, bg="#f7f7f8")
        self._active_page = "chat"

        self.header = tk.Frame(self.chat_page, bg="#f7f7f8", padx=24, pady=16)
        self.header.pack(fill="x")
        self.title_label = tk.Label(
            self.header, text="New conversation", bg="#f7f7f8", fg="#17201f",
            font=("Segoe UI", 14, "bold"), anchor="w"
        )
        self.title_label.pack(side="left")
        self.new_chat_button = self._create_rounded_button(
            self.header, "+   New chat", self.new_chat,
            variant="main", height=36, padding=13, bold=True,
        )
        self.new_chat_button.pack(side="right", padx=(8, 0))
        self.settings_button = self._create_rounded_button(
            self.header, "⚙  Settings", self.open_settings,
            variant="main", height=36, padding=13,
        )
        self.settings_button.pack(side="right", padx=(8, 0))
        chat_area = tk.Frame(self.chat_page, bg="#f7f7f8")
        chat_area.pack(fill="both", expand=True, padx=24)
        self.chat = tk.Canvas(chat_area, bg="#f7f7f8", highlightthickness=0, bd=0)
        self.chat_scrollbar = ttk.Scrollbar(chat_area, orient="vertical", command=self.chat.yview)
        self.chat.configure(yscrollcommand=self.chat_scrollbar.set)
        self.chat_scrollbar.pack(side="right", fill="y")
        self.ocean_surface = tk.Canvas(
            chat_area, height=28, bg="#f7f7f8", highlightthickness=0, bd=0
        )
        self.ocean_surface.pack(side="bottom", fill="x")
        self.ocean_surface.bind("<Configure>", self._draw_ocean_surface)
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat_inner = tk.Frame(self.chat, bg="#f7f7f8")
        self.chat_window = self.chat.create_window((0, 0), window=self.chat_inner, anchor="nw")
        self.chat_inner.bind("<Configure>", self._update_chat_scrollregion)
        self.chat.bind("<Configure>", self._resize_chat_inner)
        self.chat.bind("<Configure>", self._draw_chat_background, add="+")
        self.chat_render_width = 0
        self.chat.bind_all("<MouseWheel>", self._on_chat_mousewheel)

        self.prompt_area = tk.Frame(self.chat_page, bg="#f7f7f8", padx=24, pady=14)
        self.prompt_area.pack(fill="x")
        self.prompt_shell = tk.Canvas(
            self.prompt_area, height=96, bg="#f7f7f8",
            highlightthickness=0, bd=0
        )
        self.prompt_shell.pack(fill="x")
        self.prompt_shell.bind("<Configure>", self._redraw_prompt_shell)
        self.prompt = tk.Text(
            self.prompt_shell, height=2, wrap="word", bg="#ffffff", fg="#17201f",
            insertbackground="#17201f", relief="flat", bd=0, padx=5, pady=5,
            font=("Segoe UI", 11)
        )
        self.prompt_window = self.prompt_shell.create_window(
            (28, 18), window=self.prompt, anchor="nw"
        )
        self.prompt.bind("<Control-Return>", self._send_on_shortcut)
        self.prompt.bind("<Return>", self._send_on_return)
        self.prompt.bind("<FocusIn>", self._clear_prompt_placeholder)
        self.prompt.bind("<FocusOut>", self._restore_prompt_placeholder)
        self.prompt.bind("<<Modified>>", self._on_prompt_modified, add="+")
        self.prompt.edit_modified(False)
        self.prompt_placeholder_active = False
        self.send_button = tk.Canvas(
            self.prompt_shell, width=42, height=42, bg="#ffffff",
            highlightthickness=0, bd=0, cursor="hand2"
        )
        self.send_window = self.prompt_shell.create_window(
            (0, 0), window=self.send_button, anchor="nw"
        )
        self.send_button.create_oval(2, 2, 40, 40, fill="#10a37f", outline="", tags="send-circle")
        self.send_button.create_text(21, 20, text="↑", fill="#ffffff", font=("Segoe UI", 16, "bold"), tags="send-arrow")
        self.send_button.bind("<Button-1>", lambda _event: self.send_message())
        self.send_button.bind("<Enter>", self._send_button_hover)
        self.send_button.bind("<Leave>", self._send_button_unhover)
        self.quick_actions = tk.Frame(self.prompt_area, bg="#f7f7f8")
        self.quick_actions.pack(fill="x", pady=(0, 6), before=self.prompt_shell)
        self.quick_action_buttons = []
        for text_key, prompt in (
            ("quick_list", "List files in folder 1"),
            ("quick_search", "Search filenames for "),
            ("quick_organize", "Preview organization for folder 1"),
            ("quick_apply", "Apply the previewed organization"),
        ):
            button = tk.Canvas(
                self.quick_actions, height=34, bg="#f7f7f8",
                highlightthickness=0, bd=0, cursor="hand2"
            )
            button.text_key = text_key
            button.command = lambda value=prompt: self._fill_prompt(value)
            button.bind("<Button-1>", lambda _event, target=button: target.command())
            button.bind("<Enter>", lambda _event, target=button: self._style_quick_action(target, hovering=True))
            button.bind("<Leave>", lambda _event, target=button: self._style_quick_action(target))
            button.pack(side="left", padx=(0, 7))
            self.quick_action_buttons.append((button, text_key))
            self._style_quick_action(button)

        self.organizer_header = tk.Frame(
            self.organizer_page, bg="#000000", padx=64, pady=24
        )
        self.organizer_header.pack(fill="x")
        self.organizer_title = tk.Label(
            self.organizer_header, text=self._t("organizer_title"),
            bg="#000000", fg="#ffffff",
            font=("Segoe UI", 20, "bold"), anchor="w",
        )
        self.organizer_title.pack(fill="x")
        self.organizer_subtitle = tk.Label(
            self.organizer_header, text=self._t("organizer_subtitle"),
            bg="#000000", fg="#a1a1a1",
            font=("Segoe UI", 10), anchor="w", justify="left",
        )
        self.organizer_subtitle.pack(fill="x", pady=(6, 0))
        self.organizer_content = tk.Frame(
            self.organizer_page, bg="#000000", padx=64, pady=4
        )
        self.organizer_content.pack(fill="both", expand=True, pady=(4, 24))
        self.folders_heading = tk.Label(
            self.organizer_content, text=self._t("folders"),
            bg="#000000", fg="#a1a1a1",
            font=("Segoe UI", 9, "bold"), anchor="w",
        )
        self.folders_heading.pack(fill="x", pady=(0, 8))
        self.folder_count_label = tk.Label(
            self.organizer_content, text="0 folders connected",
            bg="#000000", fg="#a1a1a1",
            font=("Segoe UI", 9), anchor="w",
        )
        self.folder_count_label.pack(fill="x", pady=(0, 10))
        self.folder_list = tk.Listbox(
            self.organizer_content, height=9, exportselection=False, bd=0,
            relief="flat", highlightthickness=1, activestyle="none",
            font=("Segoe UI", 10), selectborderwidth=0,
            bg="#000000", fg="#ffffff",
            selectbackground="#303030", selectforeground="#ffffff",
            highlightbackground="#303030", highlightcolor="#303030",
        )
        self.folder_list.pack(fill="both", expand=True, pady=(0, 12))
        self.folder_list.bind(
            "<<ListboxSelect>>", self._organization_folder_changed
        )
        self.organize_controls = tk.Frame(self.organizer_content, bg="#000000")
        self.organize_controls.pack(fill="x", pady=(0, 10))
        self.organize_method_label = tk.Label(
            self.organize_controls, text=self._t("organize_method"),
            bg="#000000", fg="#a1a1a1",
            font=("Segoe UI", 9), anchor="w",
        )
        self.organize_method_label.pack(side="left", padx=(0, 10))
        self.organize_method_var = tk.StringVar(value=self._t("organize_type"))
        self.organize_method_box = ttk.Combobox(
            self.organize_controls,
            textvariable=self.organize_method_var,
            values=self._organization_method_labels(),
            state="readonly",
            width=28,
        )
        self.organize_method_box.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.organize_method_box.bind(
            "<<ComboboxSelected>>", self._organization_method_changed
        )
        self.organize_preview_button = self._create_rounded_button(
            self.organize_controls, self._t("organize_preview"),
            self._preview_selected_organization,
            variant="accent", height=36, padding=12, bold=True,
        )
        self.organize_preview_button.pack(side="left", padx=(0, 7))
        self.organize_apply_button = self._create_rounded_button(
            self.organize_controls, self._t("organize_apply"),
            self._apply_organization_preview,
            variant="organizer", height=36, padding=12,
        )
        self.organize_apply_button.pack(side="left")
        self.organize_options = tk.Frame(self.organizer_content, bg="#000000")
        self.organize_options.pack(fill="x", pady=(0, 10))
        self.organize_options_row = tk.Frame(self.organize_options, bg="#000000")
        self.organize_options_row.pack(fill="x", pady=(0, 7))
        self.organize_sort_label = tk.Label(
            self.organize_options_row, text=self._t("organize_sort"),
            bg="#000000", fg="#a1a1a1", font=("Segoe UI", 9),
        )
        self.organize_sort_label.pack(side="left", padx=(0, 8))
        self.organize_sort_var = tk.StringVar(value=self._t("organize_sort_name"))
        self.organize_sort_box = ttk.Combobox(
            self.organize_options_row,
            textvariable=self.organize_sort_var,
            values=self._organization_sort_labels(),
            state="readonly",
            width=18,
        )
        self.organize_sort_box.pack(side="left", padx=(0, 14))
        self.organize_sort_box.bind(
            "<<ComboboxSelected>>", self._organization_options_changed
        )
        self.organizer_actions = tk.Frame(self.organizer_content, bg="#000000")
        self.organizer_actions.pack(fill="x")
        self.add_folder_button = self._create_rounded_button(
            self.organizer_actions, "+  Add folder", self.add_folder,
            variant="organizer", anchor="w", height=40, padding=14,
        )
        self.add_folder_button.pack(side="left", padx=(0, 8))
        self.remove_folder_button = self._create_rounded_button(
            self.organizer_actions, "−  Remove selected", self.remove_folder,
            variant="organizer", anchor="w", height=40, padding=14,
        )
        self.remove_folder_button.pack(side="left")
        self.review_duplicates_button = self._create_rounded_button(
            self.organizer_actions, self._t("organize_review_duplicates"),
            self._review_selected_duplicates,
            variant="organizer", height=40, padding=14,
        )
        self.review_duplicates_button.pack(side="left", padx=(8, 0))

        self.grammar_header = tk.Frame(
            self.grammar_page, bg="#f7f7f8", padx=64, pady=24
        )
        self.grammar_header.pack(fill="x")
        self.grammar_title = tk.Label(
            self.grammar_header, text=self._t("grammar_title"),
            bg="#f7f7f8", fg="#17201f", font=("Segoe UI", 20, "bold"),
            anchor="w",
        )
        self.grammar_title.pack(fill="x")
        self.grammar_subtitle = tk.Label(
            self.grammar_header, text=self._t("grammar_subtitle"),
            bg="#f7f7f8", fg="#6b7280", font=("Segoe UI", 10),
            anchor="w", justify="left",
        )
        self.grammar_subtitle.pack(fill="x", pady=(6, 0))
        self.grammar_content = tk.Frame(
            self.grammar_page, bg="#f7f7f8", padx=64, pady=4
        )
        self.grammar_content.pack(fill="both", expand=True, pady=(4, 24))
        self.grammar_input_label = tk.Label(
            self.grammar_content, text=self._t("grammar_input"),
            bg="#f7f7f8", fg="#6b7280", font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        self.grammar_input_label.pack(fill="x", pady=(0, 8))
        self.grammar_input = tk.Text(
            self.grammar_content, height=6, wrap="word", undo=True,
            bg="#ffffff", fg="#17201f", insertbackground="#17201f",
            relief="flat", bd=0, padx=12, pady=10,
            font=("Segoe UI", 11),
        )
        self.grammar_input.pack(fill="x")
        self.grammar_input.insert("1.0", self._t("grammar_placeholder"))
        self.grammar_input_placeholder_active = True
        self.grammar_input.bind("<FocusIn>", self._clear_grammar_placeholder)
        self.grammar_input.bind("<FocusOut>", self._restore_grammar_placeholder)
        self.grammar_input.bind(
            "<<Modified>>", self._on_grammar_input_modified, add="+"
        )
        self.grammar_input.edit_modified(False)
        self.grammar_check_button = self._create_rounded_button(
            self.grammar_content, self._t("grammar_check"),
            self._run_grammar_check, variant="main", anchor="center",
            height=40, padding=18, bold=True,
        )
        self.grammar_check_button.pack(anchor="w", pady=12)
        self.grammar_result_label = tk.Label(
            self.grammar_content, text=self._t("grammar_result"),
            bg="#f7f7f8", fg="#6b7280", font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        self.grammar_result_label.pack(fill="x", pady=(0, 8))
        self.grammar_output = tk.Text(
            self.grammar_content, height=10, wrap="word", state="disabled",
            bg="#f7f7f8", fg="#17201f", relief="flat", bd=0,
            padx=12, pady=10, font=("Segoe UI", 11),
        )
        self.grammar_output.pack(fill="both", expand=True)
        self.grammar_feedback = tk.Label(
            self.grammar_content, text="",
            bg="#f7f7f8", fg="#6b7280", font=("Segoe UI", 9),
            anchor="w", justify="left", wraplength=700,
        )
        self.grammar_feedback.pack(fill="x", pady=(8, 0))
        self._organizer_bubble_canvases = self._create_bubble_rails(
            self.organizer_page, "#000000", self._draw_organizer_bubbles
        )
        self._grammar_bubble_canvases = self._create_bubble_rails(
            self.grammar_page, "#f7f7f8", self._draw_grammar_bubbles
        )

        self._apply_theme()
        self._refresh_language()
        self._restore_prompt_placeholder()
        self._append(self._t("ready"), role="system")
        self._show_page("chat")

    def _create_rounded_button(
        self, parent, text, command, variant="main", anchor="center",
        height=36, padding=14, bold=False,
    ):
        font = tkfont.Font(
            root=self.root, family="Segoe UI", size=9, weight="bold" if bold else "normal"
        )
        width = font.measure(text) + padding * 2
        button = tk.Canvas(
            parent, width=width, height=height, highlightthickness=0,
            bd=0, cursor="hand2",
        )
        button.button_text = text
        button.button_command = command
        button.button_variant = variant
        button.button_anchor = anchor
        button.button_font = font
        button.button_padding = padding
        button.button_bold = bold
        button.button_hovering = False
        button.bind("<Button-1>", lambda _event: button.button_command())
        button.bind("<Enter>", lambda _event: self._set_button_hover(button, True))
        button.bind("<Leave>", lambda _event: self._set_button_hover(button, False))
        button.bind("<Configure>", lambda _event: self._style_rounded_button(button))
        self._style_rounded_button(button)
        return button

    def _style_rounded_button(self, button):
        colors = self._theme_colors()
        if button.button_variant == "accent":
            base = colors["accent"]
            fill = base
            text_color = "#ffffff"
        elif button.button_variant == "sidebar-active":
            base = colors["sidebar"]
            fill = colors["selection"]
            text_color = colors["text"]
        elif button.button_variant == "organizer":
            base = "#000000"
            fill = "#181818" if button.button_hovering else base
            text_color = "#ffffff"
        else:
            base = colors["sidebar"] if button.button_variant == "sidebar" else colors["main"]
            fill = colors["hover"] if button.button_hovering else base
            text_color = colors["text"] if button.button_bold or button.button_hovering else colors["muted"]
        width = max(button.winfo_width(), button.winfo_reqwidth())
        height = max(button.winfo_height(), button.winfo_reqheight())
        button.configure(bg=base)
        button.delete("all")
        self._draw_rounded_rectangle(
            button, 1, 1, width - 1, height - 1,
            radius=height // 2, fill=fill,
        )
        x = button.button_padding if button.button_anchor == "w" else width / 2
        button.create_text(
            x, height / 2,
            text=button.button_text, anchor="w" if button.button_anchor == "w" else "center",
            fill=text_color, font=button.button_font,
        )

    def _set_rounded_button_text(self, button, text):
        button.button_text = text
        button.configure(width=button.button_font.measure(text) + button.button_padding * 2)
        self._style_rounded_button(button)

    def _style_quick_action(self, button, hovering=False):
        colors = self._theme_colors()
        text = self._t(button.text_key)
        font = self.quick_action_font
        width = font.measure(text) + 28
        button.configure(
            width=width, bg=colors["main"],
            cursor="hand2",
        )
        button.delete("all")
        self._draw_rounded_rectangle(
            button, 1, 1, width - 1, 33, radius=16,
            fill=colors["hover"] if hovering else colors["card"],
            outline=colors["border"] if not hovering else colors["hover"],
            width=1,
        )
        button.create_text(
            width / 2, 17, text=text,
            fill=colors["text"] if hovering else colors["muted"],
            font=font,
        )

    def _set_button_hover(self, button, hovering):
        button.button_hovering = hovering
        self._style_rounded_button(button)

    def _redraw_prompt_shell(self, event=None):
        width = event.width if event else self.prompt_shell.winfo_width()
        height = event.height if event else self.prompt_shell.winfo_height()
        if width < 30 or height < 30:
            return
        self._clear_prompt_bubbles()
        colors = self._theme_colors()
        self.prompt_shell.delete("composer-background")
        self.prompt_shell.delete("composer-river")
        self._draw_rounded_rectangle(
            self.prompt_shell, 3, 4, width - 2, height - 1,
            radius=42, fill=colors["border"], tags=("composer-background",)
        )
        self._draw_rounded_rectangle(
            self.prompt_shell, 2, 1, width - 3, height - 6,
            radius=40, fill=colors["card"], outline=colors["border"],
            width=1, tags=("composer-background",)
        )
        self._composer_river_size = (width, height)
        self._composer_river_items = []
        self._composer_river_decorations = []
        self._composer_river_objects = {}
        self._draw_composer_river(width, height)
        self.prompt_shell.tag_lower("composer-background")
        self.prompt_shell.coords(self.prompt_window, 27, 14)
        self.prompt_shell.itemconfigure(
            self.prompt_window,
            width=max(100, width - 108), height=height - 40
        )
        self.prompt_shell.coords(
            self.send_window, max(30, width - 66), max(12, (height - 42) / 2 - 2)
        )

    def _draw_composer_river(self, width, height):
        if width < 30 or height < 30:
            return
        colors = self._theme_colors()
        left = 8
        right = max(left + 1, width - 8)
        self._composer_river_bounds = (left, right)
        channel_top = height - 23
        channel_bottom = height - 6
        river_fill = self._blend_color(colors["card"], colors["accent"], 0.32)
        river_edge = self._blend_color(colors["card"], colors["accent"], 0.72)
        fill = self.prompt_shell.create_polygon(
            left, channel_top, right, channel_top, right, channel_bottom,
            left, channel_bottom, fill=river_fill, outline="",
            smooth=True, splinesteps=12,
            tags=("composer-river",),
        )
        surface = self.prompt_shell.create_line(
            left, channel_top, right, channel_top,
            smooth=True, splinesteps=12,
            fill="", tags=("composer-river",)
        )
        lower_current = self.prompt_shell.create_line(
            left, channel_top + 8, right, channel_top + 8,
            smooth=True, splinesteps=12,
            fill="", tags=("composer-river",)
        )
        highlights = [
            self.prompt_shell.create_line(
                left, channel_top, right, channel_top,
                fill=river_edge, width=2, capstyle="round",
                tags=("composer-river",),
            )
            for _ in range(4)
        ]
        decorations = [
            self.prompt_shell.create_oval(
                left, channel_top, left + 5, channel_top + 5,
                fill=river_edge, outline=river_edge, tags=("composer-river",),
            )
            for _ in range(3)
        ]
        for index, decoration in enumerate(decorations):
            self.prompt_shell.itemconfigure(
                decoration, outline=river_edge if index == 1 else "",
                fill=river_edge if index == 1 else river_fill,
                width=1,
            )
        self._composer_river_decorations = decorations
        self._composer_river_items = [
            fill, surface, lower_current, *highlights
        ]
        hull = self.prompt_shell.create_polygon(
            left, channel_top + 9, left + 7, channel_top + 14,
            left + 22, channel_top + 14, left + 27, channel_top + 9,
            fill=river_edge, outline=river_edge, tags=("composer-river",),
        )
        mast = self.prompt_shell.create_line(
            left + 13, channel_top + 9, left + 13, channel_top + 1,
            fill=river_edge, width=1, tags=("composer-river",),
        )
        sail = self.prompt_shell.create_polygon(
            left + 14, channel_top + 2, left + 14, channel_top + 8,
            left + 20, channel_top + 8, fill=colors["accent"], outline="",
            tags=("composer-river",),
        )
        logs = [
            self.prompt_shell.create_line(
                left, channel_top + 10, left + 15, channel_top + 10,
                fill=river_edge, width=4, capstyle="round",
                tags=("composer-river",),
            ),
            self.prompt_shell.create_line(
                left, channel_top + 13, left + 13, channel_top + 13,
                fill=river_edge, width=3, capstyle="round",
                tags=("composer-river",),
            ),
        ]
        fish_body = self.prompt_shell.create_oval(
            left, channel_top + 7, left + 12, channel_top + 13,
            fill=colors["accent"], outline="", tags=("composer-river",),
        )
        fish_tail = self.prompt_shell.create_polygon(
            left + 1, channel_top + 10, left - 4, channel_top + 6,
            left - 4, channel_top + 14, fill=river_edge, outline="",
            tags=("composer-river",),
        )
        fish_eye = self.prompt_shell.create_oval(
            left + 9, channel_top + 8, left + 10.5, channel_top + 9.5,
            fill=colors["card"], outline="", tags=("composer-river",),
        )
        lily_pads = []
        for _ in range(2):
            pad = self.prompt_shell.create_oval(
                left, channel_top + 12, left + 13, channel_top + 17,
                fill=river_edge, outline="", tags=("composer-river",),
            )
            notch = self.prompt_shell.create_line(
                left + 6.5, channel_top + 14, left + 11, channel_top + 12,
                fill=river_fill, width=1, tags=("composer-river",),
            )
            lily_pads.append((pad, notch))
        frog_body = self.prompt_shell.create_oval(
            left, channel_top + 6, left + 8, channel_top + 12,
            fill=colors["accent"], outline="", tags=("composer-river",),
        )
        frog_eyes = [
            self.prompt_shell.create_oval(
                left + offset, channel_top + 4, left + offset + 2,
                channel_top + 7, fill=river_edge, outline="",
                tags=("composer-river",),
            )
            for offset in (1, 5)
        ]
        frog_legs = [
            self.prompt_shell.create_line(
                left + x, channel_top + 10, left + x + dx,
                channel_top + 13, fill=colors["accent"], width=1.5,
                capstyle="round", tags=("composer-river",),
            )
            for x, dx in ((1, -2), (6, 2))
        ]
        self._composer_river_objects = {
            "boat": (hull, mast, sail),
            "logs": logs,
            "fish": (fish_body, fish_tail, fish_eye),
            "lily_pads": lily_pads,
            "frog": (frog_body, *frog_eyes, *frog_legs),
        }
        self._update_composer_river()

    def _update_composer_river(self):
        if not self._composer_river_items or not self.prompt_shell.winfo_exists():
            return
        width, height = self._composer_river_size
        left, right = self._composer_river_bounds
        channel_top = height - 23
        channel_bottom = height - 6
        span = right - left
        phase = self._composer_river_phase
        surface_swell = math.sin(phase * 0.55) * 2.1
        lower_swell = math.sin(phase * 0.48 + 1.2) * 1.2
        fill, surface, lower_current, *highlights = self._composer_river_items
        colors = self._theme_colors()
        river_fill = self._blend_color(colors["card"], colors["accent"], 0.32)
        river_edge = self._blend_color(colors["card"], colors["accent"], 0.72)
        surface_points = []
        lower_points = []
        for x in range(int(left), int(right) + 1, 3):
            surface_points.extend((
                x,
                channel_top + 3 + surface_swell
                + math.sin((x - left) * 0.05 - phase) * 3.5,
            ))
            lower_points.extend((
                x,
                channel_top + 8 + lower_swell
                + math.sin((x - left) * 0.045 - phase - 1.4) * 1.8,
            ))
        if not surface_points or surface_points[-2] < right:
            surface_points.extend((
                right,
                channel_top + 3 + surface_swell
                + math.sin((right - left) * 0.05 - phase) * 3.5,
            ))
            lower_points.extend((
                right,
                channel_top + 8 + lower_swell
                + math.sin((right - left) * 0.045 - phase - 1.4) * 1.8,
            ))
        self.prompt_shell.coords(
            fill, *surface_points, right, channel_bottom, left, channel_bottom
        )
        self.prompt_shell.itemconfigure(
            fill,
            fill=river_fill,
        )
        self.prompt_shell.coords(surface, *surface_points)
        self.prompt_shell.itemconfigure(
            surface,
            fill=river_edge,
            width=2,
        )
        self.prompt_shell.coords(lower_current, *lower_points)
        self.prompt_shell.itemconfigure(
            lower_current,
            fill=self._blend_color(colors["card"], colors["accent"], 0.48),
            width=1,
        )
        for index, highlight in enumerate(highlights):
            segment_width = max(28, span * 0.09)
            cycle_width = span + segment_width
            start_x = left + ((phase * (22 + index * 3) + index * span / len(highlights)) % cycle_width) - segment_width
            end_x = min(right, start_x + segment_width)
            start_x = max(left, start_x)
            y = (
                channel_top + 4 + surface_swell
                + math.sin((start_x - left) * 0.05 - phase) * 3.5
            )
            self.prompt_shell.coords(highlight, start_x, y, end_x, y + 0.5)
        for index, decoration in enumerate(self._composer_river_decorations):
            x = left + ((phase * (9 + index * 2) + index * span / 3) % max(1, span - 7))
            y = channel_top + 6 + math.sin(phase + index * 1.8) * 2
            radius = 2 if index == 1 else 1.5
            self.prompt_shell.coords(
                decoration, x, y - radius, x + radius * 2, y + radius
            )
        self._update_composer_river_objects(left, span, channel_top, phase, colors)

    def _update_composer_river_objects(self, left, span, channel_top, phase, colors):
        objects = self._composer_river_objects
        if not objects:
            return

        def position(speed, offset, object_width=0):
            available = max(1, span + object_width)
            return left - object_width + ((phase * speed + offset) % available)

        hull, mast, sail = objects["boat"]
        boat_x = position(13, span * 0.12, 27)
        self.prompt_shell.coords(
            hull, boat_x, channel_top + 9, boat_x + 7, channel_top + 14,
            boat_x + 22, channel_top + 14, boat_x + 27, channel_top + 9,
        )
        self.prompt_shell.coords(
            mast, boat_x + 13, channel_top + 9, boat_x + 13, channel_top + 1
        )
        self.prompt_shell.coords(
            sail, boat_x + 14, channel_top + 2, boat_x + 14, channel_top + 8,
            boat_x + 20, channel_top + 8,
        )
        logs = objects["logs"]
        log_x = position(7, span * 0.66, 15)
        self.prompt_shell.coords(
            logs[0], log_x, channel_top + 10, log_x + 15, channel_top + 10
        )
        self.prompt_shell.coords(
            logs[1], log_x + 2, channel_top + 13, log_x + 15, channel_top + 13
        )

        fish_body, fish_tail, fish_eye = objects["fish"]
        fish_x = position(19, span * 0.42, 12)
        fish_y = channel_top + 7 + math.sin(phase * 0.8) * 2
        self.prompt_shell.coords(
            fish_body, fish_x, fish_y, fish_x + 12, fish_y + 6
        )
        self.prompt_shell.coords(
            fish_tail, fish_x + 1, fish_y + 3, fish_x - 4, fish_y - 1,
            fish_x - 4, fish_y + 7,
        )
        self.prompt_shell.coords(
            fish_eye, fish_x + 9, fish_y + 1, fish_x + 10.5, fish_y + 2.5
        )

        pad_xs = []
        pad_offset = phase * 5
        pad_spacing = min(44, max(28, span * 0.18))
        pad_travel = max(1, span - pad_spacing - 13)
        pad_start = left + (pad_offset % pad_travel)
        for index, (pad, notch) in enumerate(objects["lily_pads"]):
            pad_x = pad_start + index * pad_spacing
            pad_xs.append(pad_x)
            pad_y = channel_top + 12 + math.sin(phase * 0.55 + index) * 1.2
            self.prompt_shell.coords(
                pad, pad_x, pad_y, pad_x + 13, pad_y + 5
            )
            self.prompt_shell.coords(
                notch, pad_x + 6.5, pad_y + 2,
                pad_x + 11, pad_y,
            )

        frog_progress = (phase * 0.38) % 2
        source_index = 0 if frog_progress < 1 else 1
        destination_index = 1 - source_index
        jump_progress = frog_progress % 1
        eased_jump = jump_progress * jump_progress * (3 - 2 * jump_progress)
        frog_x = (
            pad_xs[source_index] + 3
            + (pad_xs[destination_index] - pad_xs[source_index]) * eased_jump
        )
        pad_y = channel_top + 12 + math.sin(phase * 0.55 + source_index) * 1.2
        frog_y = pad_y - 7 - math.sin(jump_progress * math.pi) * 7
        frog_body, eye_left, eye_right, leg_left, leg_right = objects["frog"]
        self.prompt_shell.coords(
            frog_body, frog_x, frog_y, frog_x + 8, frog_y + 6
        )
        self.prompt_shell.coords(
            eye_left, frog_x + 1, frog_y - 2, frog_x + 3, frog_y + 1
        )
        self.prompt_shell.coords(
            eye_right, frog_x + 5, frog_y - 2, frog_x + 7, frog_y + 1
        )
        self.prompt_shell.coords(
            leg_left, frog_x + 1, frog_y + 4, frog_x - 1, frog_y + 7
        )
        self.prompt_shell.coords(
            leg_right, frog_x + 6, frog_y + 4, frog_x + 8, frog_y + 7
        )
        self.prompt_shell.itemconfigure(
            objects["boat"][0], fill=self._blend_color(colors["card"], colors["accent"], 0.72)
        )
        self.prompt_shell.itemconfigure(
            objects["boat"][1], fill=self._blend_color(colors["card"], colors["accent"], 0.72)
        )
        self.prompt_shell.itemconfigure(objects["boat"][2], fill=colors["accent"])
        self.prompt_shell.itemconfigure(
            objects["fish"][0], fill=colors["accent"]
        )
        self.prompt_shell.itemconfigure(
            objects["frog"][0], fill=colors["accent"]
        )

    def _animate_composer_river(self):
        self._composer_river_phase += 0.16 * self._animation_delta
        if getattr(self, "prompt_shell", None) is not None:
            try:
                self._update_composer_river()
                self._animate_prompt_bubbles()
            except tk.TclError:
                return
        self._schedule_animation(
            self._animate_composer_river,
            ANIMATION_INTERVAL_MS,
            reference_delay=70,
        )

    def _on_prompt_modified(self, _event=None):
        if not self.prompt.edit_modified():
            return
        self.prompt.edit_modified(False)
        if self.prompt_placeholder_active:
            return
        text_length = len(self.prompt.get("1.0", "end-1c"))
        changed = abs(text_length - self._prompt_text_length)
        self._prompt_text_length = text_length
        if changed == 0:
            return

        self._release_prompt_bubbles(min(3, max(1, changed // 3 + 1)))

    def _release_prompt_bubbles(self, count):
        self._release_text_bubbles(
            self.prompt, self._prompt_bubble_particles, count
        )

    def _on_grammar_input_modified(self, _event=None):
        if not self.grammar_input.edit_modified():
            return
        self.grammar_input.edit_modified(False)
        if self.grammar_input_placeholder_active:
            return
        text_length = len(self.grammar_input.get("1.0", "end-1c"))
        changed = abs(text_length - self._grammar_text_length)
        self._grammar_text_length = text_length
        if changed:
            self._release_text_bubbles(
                self.grammar_input,
                self._grammar_bubble_particles,
                min(3, max(1, changed // 3 + 1)),
            )

    def _release_text_bubbles(self, text_widget, particles, count):
        colors = self._theme_colors()
        bubble_color = self._blend_color(colors["card"], colors["accent"], 0.78)
        for index in range(count):
            radius = random.choice((3, 4, 5))
            x = radius + 1 + random.randint(
                0, max(0, text_widget.winfo_width() - radius * 2 - 2)
            )
            y = radius + 1 + random.randint(
                0, max(0, text_widget.winfo_height() - radius * 2 - 2)
            )
            particle = tk.Canvas(
                text_widget, width=radius * 2 + 2, height=radius * 2 + 2,
                bg=colors["card"], highlightthickness=0, bd=0, takefocus=0,
            )
            bubble_item = particle.create_oval(
                1, 1, radius * 2 + 1, radius * 2 + 1,
                outline=bubble_color, width=1,
            )
            particle.place(x=x - radius, y=y - radius)
            particle.bind("<Button-1>", lambda _event, target=text_widget: target.focus_set())
            particles.append({
                "widget": particle,
                "item": bubble_item,
                "x": x,
                "y": y,
                "radius": radius,
                "speed": 0.8 + index * 0.2,
                "phase": index * 1.7 + self._composer_river_phase,
                "age": 0,
                "lifetime": 24,
                "color": bubble_color,
            })

    def _clear_prompt_bubbles(self):
        for particle in self._prompt_bubble_particles:
            widget = particle.get("widget")
            if widget is not None:
                try:
                    widget.destroy()
                except tk.TclError:
                    pass
        self._prompt_bubble_particles.clear()

    def _animate_prompt_bubbles(self):
        colors = self._theme_colors()
        alive = []
        for particle in self._prompt_bubble_particles:
            particle["age"] += self._animation_delta
            particle["y"] -= particle["speed"] * self._animation_delta
            widget = particle["widget"]
            if particle["age"] >= particle["lifetime"] or not widget.winfo_exists():
                if widget.winfo_exists():
                    widget.destroy()
                continue
            progress = particle["age"] / particle["lifetime"]
            drift = math.sin(self._composer_river_phase + particle["phase"]) * 1.4
            x = particle["x"] + drift
            y = particle["y"]
            radius = particle["radius"]
            widget.place_configure(x=round(x - radius), y=round(y - radius))
            widget.itemconfigure(
                particle["item"],
                outline=self._blend_color(particle["color"], colors["card"], progress),
                width=1 if progress < 0.75 else 0,
            )
            alive.append(particle)
        self._prompt_bubble_particles = alive
        if self._composer_bubble_emission_active and self.prompt.winfo_exists():
            self._composer_bubble_emit_ticks += self._animation_delta
            if self._composer_bubble_emit_ticks >= 7:
                self._composer_bubble_emit_ticks -= 7
                self._release_prompt_bubbles(1)
        grammar_alive = []
        for particle in self._grammar_bubble_particles:
            particle["age"] += self._animation_delta
            particle["y"] -= particle["speed"] * self._animation_delta
            widget = particle["widget"]
            if particle["age"] >= particle["lifetime"] or not widget.winfo_exists():
                if widget.winfo_exists():
                    widget.destroy()
                continue
            progress = particle["age"] / particle["lifetime"]
            drift = math.sin(self._composer_river_phase + particle["phase"]) * 1.4
            x = particle["x"] + drift
            y = particle["y"]
            radius = particle["radius"]
            widget.place_configure(x=round(x - radius), y=round(y - radius))
            widget.itemconfigure(
                particle["item"],
                outline=self._blend_color(particle["color"], colors["card"], progress),
                width=1 if progress < 0.75 else 0,
            )
            grammar_alive.append(particle)
        self._grammar_bubble_particles = grammar_alive

    def _send_button_hover(self, _event=None):
        if str(self.send_button.cget("state")) != "disabled":
            self.send_button.itemconfigure("send-circle", fill=self._theme_colors()["hover"])

    def _send_button_unhover(self, _event=None):
        self.send_button.itemconfigure("send-circle", fill=self._theme_colors()["accent"])

    def _clear_prompt_placeholder(self, _event=None):
        if self.prompt_placeholder_active:
            self.prompt.delete("1.0", "end")
            self.prompt.configure(fg=self._theme_colors()["text"])
            self.prompt_placeholder_active = False
            self._prompt_text_length = 0
            self.prompt.edit_modified(False)

    def _restore_prompt_placeholder(self, _event=None):
        if not self.prompt.get("1.0", "end-1c").strip():
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", self._t("composer_hint"))
            self.prompt.configure(fg=self._theme_colors()["muted"])
            self.prompt_placeholder_active = True
            self._prompt_text_length = 0
            self.prompt.edit_modified(False)

    def _theme_colors(self):
        palette = INTERFACE_PRESETS[self.accent_preset][DEFAULT_THEME]
        colors = {
            **palette,
            "text": "#ececec", "muted": "#a1a1a1",
            "assistant_bubble": palette["card"],
            "system_bubble": palette["main"],
            "hover": palette["border"],
            "success": "#55c89a", "warning": "#e5b567",
            "sidebar_divider": palette["border"],
        }
        colors["accent"] = self.accent_color
        return colors

    def _apply_theme(self):
        colors = self._theme_colors()
        self.root.configure(bg=colors["main"])
        for widget in (
            self.shell, self.main_panel, self.chat_page, self.organizer_page,
            self.grammar_page,
            self.header, self.chat, self.chat_inner, self.ocean_surface,
            self.prompt_area, self.prompt_shell, self.quick_actions,
            self.grammar_header, self.grammar_content,
        ):
            widget.configure(bg=colors["main"])
        for widget in (
            self.organizer_page, self.organizer_header,
            self.organizer_content, self.organize_controls, self.organize_options,
            self.organize_options_row, self.organizer_actions,
        ):
            widget.configure(bg="#000000")
        self._background_mode = None
        self._background_size = None
        self._draw_chat_background()
        self._draw_ocean_surface()
        self.sidebar_separator.configure(bg=colors["sidebar_divider"])
        for widget in (self.sidebar, self.brand_label, self.chat_tab_button, self.organizer_tab_button):
            widget.configure(bg=colors["sidebar"])
        self.sidebar_river.configure(bg=colors["sidebar"])
        self._draw_sidebar_river()
        self._draw_organizer_bubbles()
        self._draw_grammar_bubbles()
        self.organizer_title.configure(bg="#000000", fg="#ffffff")
        self.organizer_subtitle.configure(bg="#000000", fg="#a1a1a1")
        for widget in (
            self.organize_method_label, self.organize_sort_label,
        ):
            widget.configure(bg="#000000", fg=colors["muted"])
        self.style.configure(
            "Organizer.TCombobox",
            fieldbackground="#111111", background="#111111",
            foreground="#ffffff", arrowcolor="#ffffff",
        )
        self.organize_method_box.configure(style="Organizer.TCombobox")
        self.organize_sort_box.configure(style="Organizer.TCombobox")
        self.folders_heading.configure(bg="#000000", fg="#a1a1a1")
        self.folder_count_label.configure(bg="#000000", fg="#a1a1a1")
        self.folder_list.configure(
            bg="#000000", fg="#ffffff",
            selectbackground="#303030", selectforeground="#ffffff",
            highlightbackground="#303030", highlightcolor="#303030",
        )
        self.grammar_title.configure(bg=colors["main"], fg=colors["text"])
        for widget in (
            self.grammar_subtitle, self.grammar_input_label,
            self.grammar_result_label,
        ):
            widget.configure(bg=colors["main"], fg=colors["muted"])
        self.grammar_feedback.configure(
            bg=colors["main"], fg=colors["muted"],
        )
        self.grammar_input.configure(
            bg=colors["card"], fg=colors["text"],
            insertbackground=colors["text"],
        )
        self.grammar_output.configure(bg=colors["main"], fg=colors["text"])
        for widget in (self.brand_label, self.title_label):
            widget.configure(fg=colors["text"])
        for widget in (self.organizer_subtitle, self.folders_heading, self.folder_count_label):
            widget.configure(fg=colors["muted"])
        for button in (
            self.new_chat_button, self.add_folder_button,
            self.remove_folder_button, self.settings_button,
            self.organize_preview_button, self.organize_apply_button,
            self.review_duplicates_button,
            self.chat_tab_button, self.organizer_tab_button,
            self.grammar_tab_button, self.grammar_check_button,
        ):
            self._style_rounded_button(button)
        self.sidebar_separator.configure(cursor="sb_h_double_arrow")
        for button, _key in self.quick_action_buttons:
            self._style_quick_action(button)
        self.prompt.configure(bg=colors["card"], fg=colors["text"], insertbackground=colors["text"])
        if self.prompt_placeholder_active:
            self.prompt.configure(fg=colors["muted"])
        self.send_button.configure(bg=colors["card"])
        self.send_button.itemconfigure("send-circle", fill=colors["accent"], outline="")
        self.style.configure(
            "Vertical.TScrollbar",
            background=colors["sidebar"],
            troughcolor=colors["main"],
            bordercolor=colors["main"],
            arrowcolor=colors["muted"],
        )
        self.title_label.configure(bg=colors["main"])
        self._redraw_prompt_shell()
        self._refresh_language()
        self._set_button_hover(self.new_chat_button, False)
        self._set_button_hover(self.add_folder_button, False)
        self._set_button_hover(self.remove_folder_button, False)
        self._set_button_hover(self.settings_button, False)
        self._show_page(self._active_page)
        self._draw_chat_background()
        self._render_chat_items()

    def _show_page(self, page):
        pages = {
            "chat": self.chat_page,
            "organizer": self.organizer_page,
            "grammar": self.grammar_page,
        }
        if page not in pages:
            raise ValueError(f"Unknown page: {page}")
        self.chat_page.pack_forget()
        self.organizer_page.pack_forget()
        self.grammar_page.pack_forget()
        pages[page].pack(fill="both", expand=True)
        self._active_page = page
        for tab, tab_page in (
            (self.chat_tab_button, "chat"),
            (self.organizer_tab_button, "organizer"),
            (self.grammar_tab_button, "grammar"),
        ):
            tab.button_variant = "sidebar-active" if page == tab_page else "sidebar"
            self._style_rounded_button(tab)
        if page == "chat":
            self.chat.after_idle(lambda: self.chat.yview_moveto(1.0))

    def _clear_grammar_placeholder(self, _event=None):
        if self.grammar_input_placeholder_active:
            self.grammar_input.delete("1.0", "end")
            self.grammar_input_placeholder_active = False

    def _restore_grammar_placeholder(self, _event=None):
        if not self.grammar_input.get("1.0", "end-1c").strip():
            self.grammar_input.delete("1.0", "end")
            self.grammar_input.insert("1.0", self._t("grammar_placeholder"))
            self.grammar_input_placeholder_active = True

    def _run_grammar_check(self):
        if self.grammar_input_placeholder_active:
            source = ""
        else:
            source = self.grammar_input.get("1.0", "end-1c")
        self.grammar_output.configure(state="normal")
        self.grammar_output.delete("1.0", "end")
        self.grammar_feedback.configure(text="")
        try:
            result = check_grammar_locally(source)
        except ValueError:
            self.grammar_output.insert("1.0", self._t("grammar_empty"))
        else:
            self.grammar_output.insert("1.0", result["corrected"])
            if result["issues"]:
                self.grammar_feedback.configure(
                    text=self._t("grammar_issues")
                    + "\n• " + "\n• ".join(result["issues"])
                )
            else:
                self.grammar_feedback.configure(
                    text=self._t("grammar_no_issues")
                )
        self.grammar_output.configure(state="disabled")

    def _draw_organizer_bubbles(self, _event=None):
        self._organizer_bubbles = []
        for canvas in getattr(self, "_organizer_bubble_canvases", ()):
            self._organizer_bubbles.extend(
                self._create_bubbles(canvas, "#000000", 18)
            )

    def _draw_grammar_bubbles(self, _event=None):
        background = self._theme_colors()["main"]
        self._grammar_bubbles = []
        for canvas in getattr(self, "_grammar_bubble_canvases", ()):
            canvas.configure(bg=background)
            self._grammar_bubbles.extend(
                self._create_bubbles(canvas, background, 18)
            )

    def _create_bubble_rails(self, parent, background, redraw):
        rails = []
        for side in ("left", "right"):
            canvas = tk.Canvas(
                parent, width=48, bg=background, highlightthickness=0,
                bd=0, takefocus=0,
            )
            if side == "left":
                canvas.place(x=0, y=0, width=48, relheight=1)
            else:
                canvas.place(relx=1, x=0, y=0, width=48, relheight=1, anchor="ne")
            canvas.bind("<Configure>", redraw)
            rails.append(canvas)
        return rails

    def _create_bubbles(self, canvas, background, count):
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.delete("organizer-bubble")
        accent = self._theme_colors()["accent"]
        color = self._blend_color(background, accent, 0.9)
        bubble_fill = self._blend_color(background, accent, 0.28)
        bubbles = []
        for index in range(count):
            radius = 5 + index % 4
            x = max(2, (width - radius * 2) / 2 + math.sin(index * 2.1) * 8)
            y = height - ((index * 67 + 24) % max(1, height))
            outer = canvas.create_oval(
                x, y, x + radius * 2, y + radius * 2,
                fill=bubble_fill, outline=color, width=2,
                tags=("organizer-bubble",),
            )
            inset = max(1, radius - 2)
            inner = canvas.create_oval(
                x + radius - inset, y + radius - inset,
                x + radius + inset, y + radius + inset,
                outline=self._blend_color(background, "#ffffff", 0.75),
                width=1, tags=("organizer-bubble",),
            )
            bubbles.append({
                "canvas": canvas, "items": (outer, inner),
                "x": x, "y": y, "radius": radius,
                "speed": 0.45 + (index % 4) * 0.12, "phase": index * 0.8,
            })
        return bubbles

    def _animate_organizer_bubbles(self):
        self._organizer_bubble_phase += 0.12 * self._animation_delta
        for bubble in self._organizer_bubbles + self._grammar_bubbles:
            canvas = bubble["canvas"]
            if not canvas.winfo_exists():
                continue
            height = max(1, canvas.winfo_height())
            bubble["y"] -= bubble["speed"] * self._animation_delta
            if bubble["y"] < -16:
                bubble["y"] = height + bubble["radius"] * 2
            x = bubble["x"] + math.sin(
                self._organizer_bubble_phase + bubble["phase"]
            ) * 2
            y = bubble["y"]
            radius = bubble["radius"]
            outer, inner = bubble["items"]
            canvas.coords(outer, x, y, x + radius * 2, y + radius * 2)
            inset = max(1, radius - 2)
            canvas.coords(
                inner, x + radius - inset, y + radius - inset,
                x + radius + inset, y + radius + inset,
            )
        self._schedule_animation(
            self._animate_organizer_bubbles,
            ANIMATION_INTERVAL_MS,
            reference_delay=80,
        )

    def new_chat(self):
        if self.busy:
            return
        self.research_session_id = None
        self.organization_plan = None
        self._has_user_messages = False
        self._composer_bubble_emission_active = False
        self._composer_bubble_emit_ticks = 0
        self.chat_items.clear()
        self._clear_chat_current()
        self._chat_current_particles.clear()
        self._draw_chat_background()
        self.title_label.configure(text=self._t("new_conversation"))
        self._render_chat_items()
        self._append(self._t("ready"), role="system")

    def open_settings(self):
        if getattr(self, "settings_window", None) and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        colors = self._theme_colors()
        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title(self._t("settings_title"))
        window.geometry("390x310")
        window.resizable(False, False)
        window.configure(bg=colors["main"])
        window.transient(self.root)

        body = tk.Frame(window, bg=colors["main"], padx=24, pady=22)
        body.pack(fill="both", expand=True)
        title = tk.Label(
            body, text=self._t("settings_title"), bg=colors["main"],
            fg=colors["text"], font=("Segoe UI", 17, "bold"), anchor="w"
        )
        title.pack(fill="x")
        subtitle = tk.Label(
            body, text=self._t("settings_subtitle"), bg=colors["main"],
            fg=colors["muted"], font=("Segoe UI", 9), anchor="w"
        )
        subtitle.pack(fill="x", pady=(3, 20))

        theme_label = tk.Label(
            body, text=self._t("dark_theme"), bg=colors["main"],
            fg=colors["text"], font=("Segoe UI", 10, "bold"), anchor="w"
        )
        theme_label.pack(fill="x", pady=(0, 9))

        accent_row = tk.Frame(body, bg=colors["main"])
        accent_row.pack(fill="x", pady=(0, 22))
        accent_label = tk.Label(
            accent_row, text=self._t("accent_color"), bg=colors["main"],
            fg=colors["text"], font=("Segoe UI", 9), anchor="w"
        )
        accent_label.pack(side="left")
        self.accent_preview = tk.Canvas(
            accent_row, width=76, height=24, bg=colors["main"],
            highlightthickness=0, bd=0
        )
        self.accent_preview.pack(side="right", padx=(8, 0), pady=2)
        self.accent_preset_var = tk.StringVar(
            value=self._t(f"preset_{self.accent_preset}")
        )
        self.accent_preset_box = ttk.Combobox(
            accent_row,
            textvariable=self.accent_preset_var,
            values=[self._t(f"preset_{key}") for key in INTERFACE_PRESETS],
            state="readonly",
            width=14,
        )
        self.accent_preset_box.pack(side="right")
        self.accent_preset_box.bind("<<ComboboxSelected>>", self._settings_accent_changed)

        language_row = tk.Frame(body, bg=colors["main"])
        language_row.pack(fill="x", pady=(0, 22))
        language_label = tk.Label(
            language_row, text=self._t("language"), bg=colors["main"],
            fg=colors["text"], font=("Segoe UI", 9), anchor="w"
        )
        language_label.pack(side="left")
        self.language_var = tk.StringVar(value="हिन्दी" if self.language == "hi" else "English")
        self.language_box = ttk.Combobox(
            language_row, textvariable=self.language_var,
            values=("English", "हिन्दी"), state="readonly", width=16
        )
        self.language_box.pack(side="right")
        self.language_box.bind("<<ComboboxSelected>>", self._settings_language_changed)

        done_button = self._create_rounded_button(
            body, self._t("close"), window.destroy,
            variant="accent", height=38, padding=20, bold=True,
        )
        done_button.pack(anchor="e")
        window.protocol("WM_DELETE_WINDOW", window.destroy)

        self.settings_labels = {
            "title": title, "subtitle": subtitle, "theme": theme_label,
            "accent": accent_label,
            "language": language_label, "done": done_button,
        }
        self._style_settings_window()

    def _style_settings_window(self):
        if not getattr(self, "settings_window", None) or not self.settings_window.winfo_exists():
            return
        colors = self._theme_colors()
        self.settings_window.configure(bg=colors["main"])
        for child in self.settings_window.winfo_children():
            child.configure(bg=colors["main"])
        for key in ("title", "theme"):
            self.settings_labels[key].configure(fg=colors["text"])
        for key in ("subtitle", "accent", "language"):
            self.settings_labels[key].configure(fg=colors["muted"] if key == "subtitle" else colors["text"])
        self._style_rounded_button(self.settings_labels["done"])
        self.accent_preview.configure(bg=colors["main"])
        self.accent_preview.delete("all")
        self.accent_preview.create_rectangle(1, 2, 24, 22, fill=colors["main"], outline=colors["border"])
        self.accent_preview.create_rectangle(26, 2, 49, 22, fill=colors["sidebar"], outline=colors["border"])
        self.accent_preview.create_rectangle(51, 2, 75, 22, fill=colors["accent"], outline="")
        self.style.configure(
            "Settings.TCombobox",
            fieldbackground=colors["card"], background=colors["card"],
            foreground=colors["text"], arrowcolor=colors["text"],
        )
        self.accent_preset_box.configure(style="Settings.TCombobox")
        self.language_box.configure(style="Settings.TCombobox")

    def _settings_language_changed(self, _event=None):
        self.language = "hi" if self.language_var.get() == "हिन्दी" else "en"
        self._refresh_language()
        self._save_settings()

    def _settings_accent_changed(self, _event=None):
        selected = self.accent_preset_var.get()
        self.accent_preset = next(
            key for key in INTERFACE_PRESETS
            if self._t(f"preset_{key}") == selected
        )
        self._apply_theme()
        self._style_settings_window()
        self._save_settings()

    def _refresh_language(self):
        self._set_rounded_button_text(self.new_chat_button, self._t("new_chat"))
        self._set_rounded_button_text(self.chat_tab_button, self._t("chat_tab"))
        self._set_rounded_button_text(
            self.organizer_tab_button, self._t("organizer_tab")
        )
        self._set_rounded_button_text(
            self.grammar_tab_button, self._t("grammar_tab")
        )
        self.organizer_title.configure(text=self._t("organizer_title"))
        self.organizer_subtitle.configure(text=self._t("organizer_subtitle"))
        self.organize_method_label.configure(text=self._t("organize_method"))
        self.organize_method_box.configure(values=self._organization_method_labels())
        self.organize_sort_label.configure(text=self._t("organize_sort"))
        self.organize_sort_box.configure(values=self._organization_sort_labels())
        self._set_rounded_button_text(
            self.review_duplicates_button, self._t("organize_review_duplicates")
        )
        self.organize_method_var.set(
            self._t({
                "type": "organize_type",
                "name": "organize_name",
                "size": "organize_size",
                "modified": "organize_modified",
            }[self.organization_method])
        )
        self.organize_sort_var.set(self._t(f"organize_sort_{self.organization_sort_by}"))
        self._set_rounded_button_text(
            self.organize_preview_button, self._t("organize_preview")
        )
        self._set_rounded_button_text(
            self.organize_apply_button, self._t("organize_apply")
        )
        self.folders_heading.configure(text=self._t("folders"))
        count = len(self.folders)
        count_key = "folder_count_one" if count == 1 else "folder_count"
        self.folder_count_label.configure(text=self._t(count_key).format(count=count))
        self._set_rounded_button_text(self.add_folder_button, self._t("add_folder"))
        self._set_rounded_button_text(self.remove_folder_button, self._t("remove_folder"))
        self.grammar_title.configure(text=self._t("grammar_title"))
        self.grammar_subtitle.configure(text=self._t("grammar_subtitle"))
        self.grammar_input_label.configure(text=self._t("grammar_input"))
        self.grammar_result_label.configure(text=self._t("grammar_result"))
        self.grammar_feedback.configure(bg=self._theme_colors()["main"])
        self._set_rounded_button_text(
            self.grammar_check_button, self._t("grammar_check")
        )
        if self.grammar_input_placeholder_active:
            self.grammar_input.delete("1.0", "end")
            self.grammar_input.insert("1.0", self._t("grammar_placeholder"))
        self._set_rounded_button_text(self.settings_button, self._t("settings"))
        for button, key in self.quick_action_buttons:
            self._style_quick_action(button)
        if self.prompt_placeholder_active:
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", self._t("composer_hint"))
        if self.title_label.cget("text") in {"New conversation", "नई बातचीत"}:
            self.title_label.configure(text=self._t("new_conversation"))
        if getattr(self, "settings_labels", None) and self.settings_window.winfo_exists():
            labels = self.settings_labels
            self.settings_window.title(self._t("settings_title"))
            labels["title"].configure(text=self._t("settings_title"))
            labels["subtitle"].configure(text=self._t("settings_subtitle"))
            labels["theme"].configure(text=self._t("dark_theme"))
            labels["accent"].configure(text=self._t("accent_color"))
            labels["language"].configure(text=self._t("language"))
            self._set_rounded_button_text(labels["done"], self._t("close"))
            self.language_var.set("हिन्दी" if self.language == "hi" else "English")
            self.accent_preset_var.set(self._t(f"preset_{self.accent_preset}"))
            self.accent_preset_box.configure(
                values=[self._t(f"preset_{key}") for key in INTERFACE_PRESETS]
            )

    def _update_chat_scrollregion(self, _event=None):
        self._draw_chat_background()
        self.chat.configure(scrollregion=self.chat.bbox("all"))

    def _resize_chat_inner(self, event):
        self.chat.itemconfigure(self.chat_window, width=event.width)
        self._draw_chat_background()
        if self.chat_render_width != event.width and self.chat_items:
            self.chat_render_width = event.width
            self.chat.after_idle(self._render_chat_items)

    def _draw_chat_background(self, _event=None):
        if not hasattr(self, "chat"):
            return
        width = max(1, self.chat.winfo_width())
        height = max(1, self.chat.winfo_height())
        mode = "after-message" if self._has_user_messages else "welcome"
        size = (width, height)
        if self._background_mode == mode and self._background_size == size:
            return

        self._background_mode = mode
        self._background_size = size
        self.chat.delete("chat-bubble-overlay")
        self._background_bubbles.clear()
        colors = self._theme_colors()
        count = 32
        tint = 0.36 if mode == "welcome" else 0.24
        for index in range(count):
            radius = 2 + index % 3
            margin = radius + 4
            x = margin + (width - margin * 2) * index / max(1, count - 1) - radius
            y = height - ((index * 83 + 40) % max(1, height - 20))
            fill = self._blend_color(colors["main"], colors["accent"], tint)
            outer = self.chat.create_oval(
                x, y, x + radius * 2, y + radius * 2,
                outline=fill, width=1, tags=("chat-bubble-overlay",),
            )
            inner_radius = max(1, radius - 2)
            inner = self.chat.create_oval(
                x + radius - inner_radius, y + radius - inner_radius,
                x + radius + inner_radius, y + radius + inner_radius,
                outline=fill, width=1, tags=("chat-bubble-overlay",),
            )
            self._background_bubbles.append({
                "items": (outer, inner),
                "x": x,
                "y": y,
                "radius": radius,
                "speed": (0.45 + (index % 4) * 0.12) if mode == "welcome" else (0.8 + (index % 4) * 0.18),
                "phase": index * 0.8,
            })
        self._refresh_message_bubbles(mode, colors)
        self.chat.tag_raise("chat-bubble-overlay")

    def _refresh_message_bubbles(self, mode, colors):
        count = 12
        tint = 0.36 if mode == "welcome" else 0.24
        speed = 0.45 if mode == "welcome" else 0.8
        fill = self._blend_color(colors["main"], colors["accent"], tint)
        for item in self.chat_items:
            self._draw_message_bubbles(item, count, fill, speed)

    @staticmethod
    def _draw_message_bubbles(item, count, fill, speed):
        bubble = item.get("message_canvas")
        if bubble is None:
            return
        try:
            if not bubble.winfo_exists():
                item["message_canvas"] = None
                item["message_bubbles"] = []
                return
            bubble.delete("message-bubble-overlay")
        except tk.TclError:
            item["message_canvas"] = None
            item["message_bubbles"] = []
            return
        animation_bubbles = []
        width = max(1, int(bubble.cget("width")))
        height = max(1, int(bubble.cget("height")))
        for index in range(count):
            radius = 2 + index % 2
            x = width * (index + 1) / (count + 1) - radius
            y = height * (index + 1) / (count + 1) - radius
            outer = bubble.create_oval(
                x, y, x + radius * 2, y + radius * 2,
                outline=fill, width=1, tags=("message-bubble-overlay",),
            )
            inset = max(1, radius - 2)
            inner = bubble.create_oval(
                x + radius - inset, y + radius - inset,
                x + radius + inset, y + radius + inset,
                outline=fill, width=1, tags=("message-bubble-overlay",),
            )
            animation_bubbles.append({
                "items": (outer, inner),
                "x": x,
                "y": y,
                "radius": radius,
                "speed": speed + index * 0.12,
                "phase": index * 0.8,
            })
        bubble.tag_raise("message-bubble-overlay")
        item["message_bubbles"] = animation_bubbles

    def _schedule_animation(self, callback, delay, reference_delay=None):
        after_id = None

        def run():
            self._animation_after_ids.discard(after_id)
            try:
                if self.root.winfo_exists():
                    if reference_delay is None:
                        callback()
                    else:
                        now = time.perf_counter()
                        previous = self._animation_last_run.get(
                            callback, now - reference_delay / 1000
                        )
                        self._animation_last_run[callback] = now
                        previous_delta = self._animation_delta
                        self._animation_delta = min(
                            2.5,
                            max(0.0, (now - previous) / (reference_delay / 1000)),
                        )
                        try:
                            callback()
                        finally:
                            self._animation_delta = previous_delta
            except tk.TclError:
                return

        after_id = self.root.after(delay, run)
        self._animation_after_ids.add(after_id)
        return after_id

    def _stop_animations(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        for after_id in tuple(self._animation_after_ids):
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self._animation_after_ids.clear()

    @staticmethod
    def _blend_color(start, end, amount):
        start_rgb = tuple(int(start[index:index + 2], 16) for index in (1, 3, 5))
        end_rgb = tuple(int(end[index:index + 2], 16) for index in (1, 3, 5))
        blended = tuple(
            round(first + (last - first) * amount)
            for first, last in zip(start_rgb, end_rgb)
        )
        return "#{:02x}{:02x}{:02x}".format(*blended)

    def _animate_chat_background(self):
        self._background_phase += 0.12 * self._animation_delta
        height = max(1, self.chat.winfo_height())
        top = self.chat.canvasy(0)
        for bubble in self._background_bubbles:
            bubble["y"] -= bubble["speed"] * self._animation_delta
            if bubble["y"] < -16:
                bubble["y"] = height + bubble["radius"] * 2
            drift_x = math.sin(self._background_phase + bubble["phase"]) * 2
            x = bubble["x"] + drift_x
            y = top + bubble["y"]
            radius = bubble["radius"]
            outer, inner = bubble["items"]
            try:
                self.chat.coords(outer, x, y, x + radius * 2, y + radius * 2)
                inset = max(1, radius - 2)
                self.chat.coords(
                    inner, x + radius - inset, y + radius - inset,
                    x + radius + inset, y + radius + inset,
                )
            except tk.TclError:
                break
        for item in self.chat_items:
            bubble_canvas = item.get("message_canvas")
            if bubble_canvas is None:
                continue
            canvas_height = max(1, int(bubble_canvas.cget("height")))
            for bubble in item.get("message_bubbles", ()):
                bubble["y"] -= bubble["speed"] * self._animation_delta
                if bubble["y"] < -bubble["radius"] * 2:
                    bubble["y"] = canvas_height
                drift_x = math.sin(self._background_phase + bubble["phase"]) * 1.5
                x = bubble["x"] + drift_x
                y = bubble["y"]
                radius = bubble["radius"]
                outer, inner = bubble["items"]
                try:
                    bubble_canvas.coords(
                        outer, x, y, x + radius * 2, y + radius * 2
                    )
                    inset = max(1, radius - 2)
                    bubble_canvas.coords(
                        inner, x + radius - inset, y + radius - inset,
                        x + radius + inset, y + radius + inset,
                    )
                except tk.TclError:
                    break
        active_splashes = []
        for particle in self._chat_splash_particles:
            particle["age"] += self._animation_delta
            if particle["age"] >= particle["lifetime"]:
                self.chat.delete(particle["item"])
                continue
            progress = particle["age"] / particle["lifetime"]
            if particle.get("ripple"):
                radius = particle["radius"] + progress * 24
                self.chat.coords(
                    particle["item"],
                    particle["x"] - radius,
                    particle["y"] - radius * 0.55,
                    particle["x"] + radius,
                    particle["y"] + radius * 0.55,
                )
            elif particle.get("droplet"):
                particle["x"] += particle["vx"] * self._animation_delta
                particle["y"] += particle["vy"] * self._animation_delta
                particle["vy"] += particle["gravity"] * self._animation_delta
                radius_x = particle["radius_x"] * (1 - progress * 0.25)
                radius_y = particle["radius_y"] * (1 - progress * 0.25)
                self.chat.coords(
                    particle["item"],
                    particle["x"] - radius_x,
                    particle["y"] - radius_y,
                    particle["x"] + radius_x,
                    particle["y"] + radius_y,
                )
            else:
                particle["x"] += particle["vx"] * self._animation_delta
                particle["y"] += particle["vy"] * self._animation_delta
                particle["vy"] += particle["gravity"] * self._animation_delta
                self.chat.coords(
                    particle["item"],
                    particle["x"], particle["y"],
                    particle["x"] + particle["length"] * particle["dx"],
                    particle["y"] + particle["length"] * particle["dy"],
                )
            self.chat.itemconfigure(
                particle["item"],
                fill=self._blend_color(
                    particle["color"], self._theme_colors()["main"], progress
                ),
                width=max(0.5, particle["width"] * (1 - progress)),
            )
            active_splashes.append(particle)
        self._chat_splash_particles = active_splashes
        active_currents = []
        for current in self._chat_current_particles:
            current["age"] += self._animation_delta
            if current["age"] >= current["lifetime"]:
                current["canvas"].delete(current["item"])
                continue
            progress = current["age"] / current["lifetime"]
            base_y = current["start_y"] - current["age"] * current["speed"]
            points = []
            for point_index in range(7):
                points.extend((
                    current["x"] + math.sin(
                        self._background_phase + current["phase"] + point_index * 0.75
                    ) * current["amplitude"],
                    base_y - point_index * current["segment"],
                ))
            current["canvas"].coords(current["item"], *points)
            current["canvas"].itemconfigure(
                current["item"],
                fill=self._blend_color(
                    current["color"], self._theme_colors()["main"], progress
                ),
                width=max(0.6, current["width"] * (1 - progress)),
            )
            active_currents.append(current)
        expired_canvases = {
            current["canvas"]
            for current in self._chat_current_particles
            if current not in active_currents and "canvas" in current
        }
        for canvas in expired_canvases:
            try:
                if canvas.winfo_exists() and not canvas.find_withtag("chat-current"):
                    canvas.destroy()
            except tk.TclError:
                pass
        self._chat_current_particles = active_currents
        self.chat.tag_raise("chat-bubble-overlay")
        self.chat.tag_raise("message-splash")
        self._schedule_animation(
            self._animate_chat_background,
            ANIMATION_INTERVAL_MS,
            reference_delay=70,
        )

    def _release_upward_current(self, item):
        bubble = item.get("message_canvas")
        row = item.get("widget")
        if bubble is None:
            return
        try:
            if not bubble.winfo_exists() or row is None or not row.winfo_exists():
                return
            bubble_height = bubble.winfo_height()
        except tk.TclError:
            return

        side = "right" if item["role"] == "user" else "left"
        current_canvas = tk.Canvas(
            row, width=48, height=bubble_height + 12,
            bg=self._theme_colors()["main"], highlightthickness=0, bd=0,
            takefocus=0,
        )
        current_canvas.pack(
            side=side, padx=(0, 8) if side == "right" else (8, 0),
            pady=2,
        )
        colors = self._theme_colors()
        for index in range(5):
            x = 7 + index * 8
            start_y = bubble_height + 28 + index * 5
            color = self._blend_color(colors["main"], colors["accent"], 0.96)
            coords = []
            for point_index in range(7):
                coords.extend((x, start_y - point_index * 13))
            line = current_canvas.create_line(
                *coords, smooth=True, splinesteps=12,
                fill=color, width=2.8 if index < 2 else 2,
                capstyle="round", tags=("chat-current",),
            )
            self._chat_current_particles.append({
                "canvas": current_canvas,
                "item": line,
                "x": x,
                "start_y": start_y,
                "age": 0,
                "lifetime": 54,
                "speed": 4.2 + index * 0.25,
                "segment": 13,
                "amplitude": 3 + index * 0.5,
                "phase": index * 1.3,
                "width": 2.8 if index < 2 else 2,
                "color": color,
            })

    def _clear_chat_current(self):
        canvases = {
            particle["canvas"]
            for particle in self._chat_current_particles
            if "canvas" in particle
        }
        for canvas in canvases:
            try:
                if canvas.winfo_exists():
                    canvas.destroy()
            except tk.TclError:
                pass

    def _draw_ocean_surface(self, _event=None):
        if not hasattr(self, "ocean_surface"):
            return
        width = max(1, self.ocean_surface.winfo_width())
        height = max(1, self.ocean_surface.winfo_height())
        if not self._surface_wave_items or self._surface_size != (width, height):
            self.ocean_surface.delete("all")
            self._surface_size = (width, height)
            self._surface_wave_items = [
                self.ocean_surface.create_polygon(
                    0, height, 0, height, width, height,
                    fill=self._theme_colors()["selection"],
                    outline="", smooth=True, splinesteps=16,
                    tags=("ocean-wave",),
                ),
                self.ocean_surface.create_line(
                    0, 0, width, 0, smooth=True, splinesteps=16,
                    fill=self._theme_colors()["accent"], width=2,
                    tags=("ocean-wave",),
                ),
                self.ocean_surface.create_line(
                    0, 0, width, 0, smooth=True, splinesteps=16,
                    fill=self._theme_colors()["border"], width=1.5,
                    tags=("ocean-wave",),
                ),
            ]
        self._update_ocean_surface()

    def _update_ocean_surface(self):
        if not self._surface_wave_items or not self.ocean_surface.winfo_exists():
            return
        width = max(1, self.ocean_surface.winfo_width())
        height = max(1, self.ocean_surface.winfo_height())
        colors = self._theme_colors()
        self._surface_phase += 0.12 * self._animation_delta
        fill_points = []
        wave_points = [[], []]
        sample_x = list(range(0, width + 1, 4))
        if sample_x[-1] < width:
            sample_x.append(width)
        front_amplitude = max(3.0, height * 0.07)
        back_amplitude = max(2.5, height * 0.055)
        fill_amplitude = max(4.0, height * 0.1)
        front_swell = math.sin(self._surface_phase) * height * 0.06
        back_swell = math.sin(self._surface_phase + 1.5) * height * 0.045
        fill_swell = math.sin(self._surface_phase + 0.25) * height * 0.075
        for x in sample_x:
            for index, (amplitude, phase, frequency) in enumerate(
                ((front_amplitude, 0, 0.018), (back_amplitude, 1.7, 0.021))
            ):
                swell = front_swell if index == 0 else back_swell
                y = height * (0.38 + index * 0.22) + swell + math.sin(
                    x * frequency + self._surface_phase + phase
                ) * amplitude
                wave_points[index].extend((x, y))
            y = (
                height * 0.54 + fill_swell
                + math.sin(x * 0.018 + self._surface_phase) * fill_amplitude
                + math.sin(x * 0.039 - self._surface_phase * 0.7)
                * fill_amplitude * 0.2
            )
            fill_points.extend((x, y))
        fill_points.extend((width, height, 0, height))
        fill, front_wave, back_wave = self._surface_wave_items
        self.ocean_surface.coords(fill, *fill_points)
        self.ocean_surface.coords(front_wave, *wave_points[0])
        self.ocean_surface.coords(back_wave, *wave_points[1])
        self.ocean_surface.itemconfigure(
            fill, fill=self._blend_color(colors["selection"], colors["accent"], 0.2)
        )
        self.ocean_surface.itemconfigure(front_wave, fill=colors["accent"])
        self.ocean_surface.itemconfigure(back_wave, fill=colors["border"])
        self.ocean_surface.tag_raise(back_wave)
        self.ocean_surface.tag_raise(front_wave)

    def _animate_ocean_surface(self):
        self._update_ocean_surface()
        self._schedule_animation(
            self._animate_ocean_surface,
            ANIMATION_INTERVAL_MS,
            reference_delay=80,
        )

    def _draw_sidebar_river(self, _event=None):
        if not hasattr(self, "sidebar_river"):
            return
        width = max(1, self.sidebar_river.winfo_width())
        height = max(1, self.sidebar_river.winfo_height())
        self._sidebar_river_size = (width, height)
        self.sidebar_river.delete("all")
        colors = self._theme_colors()
        bank = self._blend_color(colors["sidebar"], colors["accent"], 0.09)
        stream = self._blend_color(colors["sidebar"], colors["accent"], 0.24)
        self.sidebar_river.create_rectangle(
            0, 0, width, height, fill=bank, outline="", tags=("river-bank",)
        )
        water = self.sidebar_river.create_polygon(
            width * 0.12, 0, width * 0.88, 0,
            width * 0.88, height, width * 0.12, height,
            fill=stream, outline="", tags=("sidebar-river-water",)
        )
        current = [
            self.sidebar_river.create_line(
                width * (0.34 + index * 0.16), 0,
                width * (0.34 + index * 0.16), height,
                fill=self._blend_color(colors["sidebar"], colors["accent"], 0.66 - index * 0.1),
                width=1.5 if index == 0 else 1,
                smooth=True, splinesteps=12, tags=("sidebar-river-current",)
            )
            for index in range(3)
        ]
        bubbles = []
        for index in range(16):
            radius = 1 + index % 2
            x = width / 2 + (index % 2 * 2 - 1) * width * 0.16
            bubble = self.sidebar_river.create_oval(
                x, index * 42, x + radius * 2, index * 42 + radius * 2,
                outline=self._blend_color(colors["sidebar"], colors["accent"], 0.52),
                width=1, tags=("sidebar-river-bubble",)
            )
            bubbles.append((bubble, radius, index))
        boats = []
        for _ in range(3):
            hull = self.sidebar_river.create_polygon(
                width / 2 - 10, 0, width / 2 - 7, 9,
                width / 2 + 7, 9, width / 2 + 10, 0,
                fill=self._blend_color(colors["sidebar"], colors["accent"], 0.8),
                outline="", tags=("sidebar-river-object",),
            )
            mast = self.sidebar_river.create_line(
                width / 2, 1, width / 2, -8,
                fill=colors["text"], width=1, tags=("sidebar-river-object",),
            )
            sail = self.sidebar_river.create_polygon(
                width / 2 + 1, -7, width / 2 + 1, 0, width / 2 + 8, 0,
                fill=colors["accent"], outline="", tags=("sidebar-river-object",),
            )
            boats.append((hull, mast, sail))
        logs = []
        for _ in range(3):
            logs.append((
                self.sidebar_river.create_line(
                    0, 0, 13, 0, fill=self._blend_color(
                        colors["sidebar"], colors["accent"], 0.72
                    ), width=4, capstyle="round", tags=("sidebar-river-object",),
                ),
                self.sidebar_river.create_line(
                    0, 0, 11, 0, fill=self._blend_color(
                        colors["sidebar"], colors["accent"], 0.72
                    ), width=3, capstyle="round", tags=("sidebar-river-object",),
                ),
            ))
        fishes = []
        for _ in range(4):
            body = self.sidebar_river.create_oval(
                0, 0, 12, 7, fill=colors["accent"], outline="",
                tags=("sidebar-river-object",),
            )
            tail = self.sidebar_river.create_polygon(
                0, 0, -5, -4, -5, 4, fill=colors["accent"], outline="",
                tags=("sidebar-river-object",),
            )
            eye = self.sidebar_river.create_oval(
                0, 0, 2, 2, fill=colors["sidebar"], outline="",
                tags=("sidebar-river-object",),
            )
            fishes.append((body, tail, eye))
        lily_pads = []
        for _ in range(2):
            pad = self.sidebar_river.create_oval(
                0, 0, 14, 7, fill=self._blend_color(
                    colors["sidebar"], colors["accent"], 0.64
                ), outline="", tags=("sidebar-river-object",),
            )
            notch = self.sidebar_river.create_line(
                7, 3, 12, 0, fill=stream, width=1,
                tags=("sidebar-river-object",),
            )
            lily_pads.append((pad, notch))
        frogs = []
        for _ in range(2):
            frog = self.sidebar_river.create_oval(
                0, 0, 8, 6, fill=colors["accent"], outline="",
                tags=("sidebar-river-object",),
            )
            frog_eyes = [
                self.sidebar_river.create_oval(
                    0, 0, 2, 3, fill=colors["text"], outline="",
                    tags=("sidebar-river-object",),
                )
                for _ in range(2)
            ]
            frog_legs = [
                self.sidebar_river.create_line(
                    0, 0, 2, 3, fill=colors["accent"], width=1.5,
                    capstyle="round", tags=("sidebar-river-object",),
                )
                for _ in range(2)
            ]
            frogs.append((frog, *frog_eyes, *frog_legs))
        self._sidebar_river_objects = {
            "currents": current,
            "boats": boats,
            "logs": logs,
            "fishes": fishes,
            "lily_pads": lily_pads,
            "frogs": frogs,
        }
        self._sidebar_river_items = [water, *current, *bubbles]
        self._update_sidebar_river()

    def _update_sidebar_river(self):
        if not self._sidebar_river_items or not self.sidebar_river.winfo_exists():
            return
        width, height = self._sidebar_river_size
        colors = self._theme_colors()
        self.sidebar_river.itemconfigure(
            "river-bank",
            fill=self._blend_color(colors["sidebar"], colors["accent"], 0.09),
        )
        self.sidebar_river.itemconfigure(
            "sidebar-river-water",
            fill=self._blend_color(colors["sidebar"], colors["accent"], 0.24),
        )
        self.sidebar_river.itemconfigure(
            "sidebar-river-current",
            fill=self._blend_color(colors["sidebar"], colors["accent"], 0.72),
        )
        phase = self._sidebar_river_phase
        left_points = []
        right_points = []
        for y in range(0, height + 7, 6):
            center = width / 2 + math.sin(y * 0.025 - phase) * width * 0.08
            left_points.extend((center - width * 0.3, y))
            right_points.append((center + width * 0.3, y))
        water = self._sidebar_river_items[0]
        self.sidebar_river.coords(
            water, *left_points,
            *(coordinate for point in reversed(right_points) for coordinate in point),
        )
        for index, current in enumerate(self._sidebar_river_objects["currents"]):
            current_points = []
            offset = phase * (9 + index * 3) + index * height / 3
            for y in range(0, height + 9, 8):
                flow_y = (y + offset) % max(1, height)
                center = width / 2 + math.sin(flow_y * 0.025 - phase) * width * 0.08
                x = center + (index - 1) * width * 0.16
                current_points.extend((x, y))
            self.sidebar_river.coords(current, *current_points)
        for bubble, radius, index in self._sidebar_river_items[4:]:
            y = (height - ((phase * (8 + index * 1.4) + index * height / 5) % max(1, height + radius * 2)))
            x = width / 2 + math.sin(phase + index) * width * 0.2 - radius
            self.sidebar_river.coords(
                bubble, x, y, x + radius * 2, y + radius * 2
            )
        objects = self._sidebar_river_objects

        def flow_y(speed, offset, extent):
            return (phase * speed + offset) % max(1, height + extent) - extent

        for index, (hull, mast, sail) in enumerate(objects["boats"]):
            boat_y = flow_y(14 + index * 1.5, height * (0.14 + index * 0.3), 12)
            boat_x = width / 2 + math.sin(phase * 0.7 + index) * 5
            self.sidebar_river.coords(
                hull, boat_x - 10, boat_y, boat_x - 7, boat_y + 9,
                boat_x + 7, boat_y + 9, boat_x + 10, boat_y,
            )
            self.sidebar_river.coords(mast, boat_x, boat_y + 1, boat_x, boat_y - 8)
            self.sidebar_river.coords(
                sail, boat_x + 1, boat_y - 7, boat_x + 1, boat_y,
                boat_x + 8, boat_y,
            )
        for index, (log_a, log_b) in enumerate(objects["logs"]):
            log_y = flow_y(9 + index * 1.2, height * (0.24 + index * 0.27), 6)
            log_x = width / 2 + math.sin(phase + index * 2) * 12
            self.sidebar_river.coords(
                log_a, log_x - 7, log_y, log_x + 6, log_y
            )
            self.sidebar_river.coords(
                log_b, log_x - 5, log_y + 3, log_x + 6, log_y + 3
            )
        for index, (fish_body, fish_tail, fish_eye) in enumerate(objects["fishes"]):
            fish_y = flow_y(18 + index * 2, height * (0.1 + index * 0.22), 10)
            fish_x = width / 2 + math.sin(phase * 0.8 + index * 1.7) * 12
            self.sidebar_river.coords(
                fish_body, fish_x - 6, fish_y - 3, fish_x + 6, fish_y + 4
            )
            self.sidebar_river.coords(
                fish_tail, fish_x - 5, fish_y, fish_x - 10, fish_y - 4,
                fish_x - 10, fish_y + 4,
            )
            self.sidebar_river.coords(
                fish_eye, fish_x + 2, fish_y - 2, fish_x + 4, fish_y
            )
        pad_y = height * 0.58
        pad_xs = (8, width - 22)
        for (pad, notch), pad_x in zip(objects["lily_pads"], pad_xs):
            self.sidebar_river.coords(
                pad, pad_x, pad_y, pad_x + 14, pad_y + 7
            )
            self.sidebar_river.coords(
                notch, pad_x + 7, pad_y + 3, pad_x + 12, pad_y
            )
        for index, (frog, eye_a, eye_b, leg_a, leg_b) in enumerate(objects["frogs"]):
            jump = (phase * 0.24 + index) % 2
            source = 0 if jump < 1 else 1
            progress = jump % 1
            eased = progress * progress * (3 - 2 * progress)
            frog_x = pad_xs[source] + 3 + (pad_xs[1 - source] - pad_xs[source]) * eased
            frog_y = pad_y - 6 - math.sin(progress * math.pi) * 11
            self.sidebar_river.coords(frog, frog_x, frog_y, frog_x + 8, frog_y + 6)
            self.sidebar_river.coords(
                eye_a, frog_x + 1, frog_y - 2, frog_x + 3, frog_y + 1
            )
            self.sidebar_river.coords(
                eye_b, frog_x + 5, frog_y - 2, frog_x + 7, frog_y + 1
            )
            self.sidebar_river.coords(
                leg_a, frog_x + 1, frog_y + 4, frog_x - 1, frog_y + 7
            )
            self.sidebar_river.coords(
                leg_b, frog_x + 6, frog_y + 4, frog_x + 8, frog_y + 7
            )

    def _animate_sidebar_river(self):
        self._sidebar_river_phase += 0.09 * self._animation_delta
        if getattr(self, "sidebar_river", None) is not None:
            try:
                self._update_sidebar_river()
            except tk.TclError:
                return
        self._schedule_animation(
            self._animate_sidebar_river,
            ANIMATION_INTERVAL_MS,
            reference_delay=80,
        )

    def _send_on_shortcut(self, _event):
        self.send_message()
        return "break"

    def _send_on_return(self, event):
        if event.state & 0x0001:
            return None
        self.send_message()
        return "break"

    def _fill_prompt(self, value):
        self._clear_prompt_placeholder()
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", value)
        self.prompt.focus_set()
        self.prompt.mark_set("insert", "end-1c")

    def _on_chat_mousewheel(self, event):
        if self.chat.winfo_exists():
            self.chat.yview_scroll(int(-event.delta / 120), "units")

    def _render_chat_items(self):
        self._stop_research_indicators()
        self.chat.delete("message-splash")
        self._chat_splash_particles.clear()
        self._clear_chat_current()
        self._chat_current_particles.clear()
        for child in self.chat_inner.winfo_children():
            child.destroy()
        colors = self._theme_colors()
        for item in self.chat_items:
            self._create_chat_bubble(item, colors, animate=False)
        self.chat.after_idle(lambda: self.chat.yview_moveto(1.0))

    def _create_chat_bubble(self, item, colors=None, animate=True):
        colors = colors or self._theme_colors()
        item["send_bubbles"] = []
        item["send_slashes"] = []
        item["send_splash"] = []
        role = item["role"]
        text = item["text"]
        if role == "user" and text.startswith("You: "):
            text = text[5:]
        elif role == "assistant" and text.startswith("Assistant: "):
            text = text[11:]
        background = {
            "user": colors["user_bubble"],
            "assistant": colors["assistant_bubble"],
            "thinking": colors["system_bubble"],
            "system": colors["system_bubble"],
        }.get(role, colors["assistant_bubble"])
        foreground = colors["muted"] if role in {"system", "thinking"} else colors["text"]

        row = tk.Frame(self.chat_inner, bg=colors["main"])
        row.pack(fill="x", pady=(5, 10))
        bubble_font = self.chat_font
        max_text_width = max(220, min(680, self.chat.winfo_width() - 150))
        lines = []
        for paragraph in text.splitlines() or [""]:
            words = paragraph.split()
            if not words:
                lines.append("")
                continue
            line = ""
            for word in words:
                candidate = f"{line} {word}".strip()
                if line and bubble_font.measure(candidate) > max_text_width:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
        display_text = "\n".join(lines)
        text_width = max((bubble_font.measure(line) for line in lines), default=40)
        bubble_width = min(max_text_width + 40, max(120, text_width + 40))
        label_text = {"user": "", "assistant": "HADAL", "thinking": "HADAL", "system": "INFO"}.get(role, "")
        bubble_height = max(
            48,
            len(lines) * 21 + (34 if role == "user" else 48)
            + (14 if role == "thinking" else 0),
        )
        bubble = tk.Canvas(
            row, width=bubble_width, height=bubble_height, bg=colors["main"],
            highlightthickness=0, bd=0
        )
        if role == "user":
            bubble.pack(side="right", padx=(72, 8))
        elif role == "system":
            bubble.pack(anchor="center")
        else:
            bubble.pack(side="left", padx=(8, 72))
        initial_offset = 8 if role == "user" else -8
        shape_id = self._draw_rounded_rectangle(
            bubble, 2, 2, bubble_width - 2, bubble_height - 2,
            radius=min(28, bubble_height // 2) if role == "user" else 24,
            fill=background, tags=("message-animation", "message-shape"),
        )
        if label_text:
            bubble.create_text(
                20, 11, anchor="nw", text=label_text, fill=colors["muted"],
                font=("Segoe UI", 9, "bold"), tags=("message-animation", "message-label"),
            )
        bubble.create_text(
            20, 17 if role == "user" else 31, anchor="nw", text=display_text, fill=foreground,
            font=bubble_font, width=bubble_width - 40, justify="left",
            tags=("message-animation", "message-text"),
        )
        if animate:
            bubble.move("message-animation", 0, initial_offset)
        item["widget"] = row
        if animate:
            self._animate_message_bubble(bubble, colors, background, foreground, initial_offset)
        if role == "thinking" and text == self._t("research_status"):
            self._start_research_indicator(bubble, bubble_width, bubble_height, colors)
        item["message_canvas"] = bubble
        mode = "after-message" if self._has_user_messages else "welcome"
        count = 7
        tint = 0.36 if mode == "welcome" else 0.24
        speed = 0.45 if mode == "welcome" else 0.8
        fill = self._blend_color(colors["main"], colors["accent"], tint)
        self._draw_message_bubbles(item, count, fill, speed)
        if animate and role in {"user", "assistant"}:
            self._schedule_animation(
                lambda target=item, target_bubble=bubble, target_colors=colors:
                    self._release_message_splash(
                        target, target_bubble, target_colors
                    ),
                70,
            )
        return bubble

    def _release_message_splash(self, item, bubble, colors):
        if item.get("message_canvas") is not bubble:
            return
        try:
            if not bubble.winfo_exists():
                return
            width = max(1, bubble.winfo_width())
            height = max(1, bubble.winfo_height())
            screen_x = bubble.winfo_rootx() - self.chat.winfo_rootx()
            screen_y = bubble.winfo_rooty() - self.chat.winfo_rooty()
            left = self.chat.canvasx(screen_x)
            top = self.chat.canvasy(screen_y)
        except tk.TclError:
            return
        center_x = left + width / 2
        center_y = top + height * 0.58
        base_color = self._blend_color(colors["accent"], "#ffffff", 0.04)
        particles = []
        for index in range(20):
            angle = math.pi * 2 * index / 20 + random.uniform(-0.08, 0.08)
            direction_x = math.cos(angle)
            direction_y = math.sin(angle)
            edge_scale = min(
                (width / 2) / max(0.01, abs(direction_x)),
                (height / 2) / max(0.01, abs(direction_y)),
            )
            start_x = center_x + direction_x * (edge_scale + 8)
            start_y = center_y + direction_y * (edge_scale + 8)
            length = random.uniform(10, 19)
            line_width = random.choice((2.5, 3, 4))
            splash = self.chat.create_line(
                start_x, start_y,
                start_x + direction_x * length,
                start_y + direction_y * length,
                fill=base_color, width=line_width,
                capstyle="round", tags=("message-splash",),
            )
            particles.append({
                "item": splash,
                "x": start_x,
                "y": start_y,
                "vx": direction_x * random.uniform(2.1, 4.1),
                "vy": direction_y * random.uniform(1.7, 3.6) - 0.8,
                "gravity": 0.16,
                "dx": direction_x,
                "dy": direction_y,
                "length": length,
                "width": line_width,
                "age": 0,
                "lifetime": random.randint(30, 40),
                "color": base_color,
            })
        for index in range(14):
            angle = math.pi * 2 * (index + 0.5) / 14 + random.uniform(-0.12, 0.12)
            direction_x = math.cos(angle)
            direction_y = math.sin(angle)
            edge_scale = min(
                (width / 2) / max(0.01, abs(direction_x)),
                (height / 2) / max(0.01, abs(direction_y)),
            )
            start_x = center_x + direction_x * (edge_scale + 12)
            start_y = center_y + direction_y * (edge_scale + 12)
            radius_x = random.uniform(3, 5)
            radius_y = random.uniform(4, 7)
            droplet = self.chat.create_oval(
                start_x - radius_x, start_y - radius_y,
                start_x + radius_x, start_y + radius_y,
                fill=base_color, outline="#ffffff", width=1,
                tags=("message-splash",),
            )
            particles.append({
                "item": droplet,
                "x": start_x,
                "y": start_y,
                "vx": direction_x * random.uniform(2.3, 4.4),
                "vy": direction_y * random.uniform(1.8, 3.8) - 0.8,
                "gravity": 0.18,
                "radius_x": radius_x,
                "radius_y": radius_y,
                "width": 1,
                "age": 0,
                "lifetime": random.randint(32, 42),
                "color": base_color,
                "droplet": True,
            })
        ripple_color = self._blend_color(
            colors["accent"], "#ffffff", 0.08
        )
        for index in range(3):
            radius = 6
            ripple = self.chat.create_arc(
                center_x - radius, top + height + 8 - radius * 0.45,
                center_x + radius, top + height + 8 + radius * 0.45,
                start=0, extent=180, style="arc", outline=ripple_color,
                width=4 if index == 0 else 2.5,
                tags=("message-splash",),
            )
            particles.append({
                "item": ripple,
                "x": center_x,
                "y": top + height + 8,
                "radius": radius,
                "age": 0,
                "lifetime": 36 + index * 5,
                "width": 4 if index == 0 else 2.5,
                "color": ripple_color,
                "ripple": True,
            })
        item["send_splash"] = particles
        self._chat_splash_particles.extend(particles)
        self.chat.tag_raise("message-splash")

    def _animate_message_bubble(self, bubble, colors, background, foreground, offset):
        steps = 10
        step = 0
        bubble.itemconfigure("message-shape", fill=self._blend_color(colors["main"], background, 0.12))
        bubble.itemconfigure("message-text", fill=self._blend_color(colors["main"], foreground, 0.12))
        bubble.itemconfigure("message-label", fill=colors["main"])

        def animate():
            nonlocal step
            if not bubble.winfo_exists():
                return
            step += 1
            progress = min(1, step / steps)
            eased = 1 - (1 - progress) ** 2
            previous_progress = (step - 1) / steps
            previous_eased = 1 - (1 - previous_progress) ** 2
            bubble.move(
                "message-animation",
                0,
                offset * ((1 - eased) - (1 - previous_eased)),
            )
            bubble.itemconfigure(
                "message-shape", fill=self._blend_color(
                    colors["main"], background, min(1, 0.12 + eased * 0.88)
                )
            )
            bubble.itemconfigure(
                "message-text", fill=self._blend_color(
                    colors["main"], foreground, min(1, 0.12 + eased * 0.88)
                )
            )
            bubble.itemconfigure(
                "message-label", fill=self._blend_color(colors["main"], colors["muted"], eased)
            )
            if step < steps:
                self._schedule_animation(animate, 18)

        self._schedule_animation(animate, 18)

    def _start_research_indicator(self, bubble, width, height, colors):
        dots = []
        for index in range(3):
            x = width - 30 + index * 8
            dots.append(bubble.create_oval(
                x, height - 13, x + 4, height - 9,
                fill=colors["accent"], outline="",
                tags=("research-indicator",),
            ))
        bubble.indicator_step = 0
        self._animate_research_indicator(bubble, dots, colors)

    def _animate_research_indicator(self, bubble, dots, colors):
        if not bubble.winfo_exists():
            return
        bubble.indicator_step = (bubble.indicator_step + 1) % 12
        active_dot = bubble.indicator_step // 4
        for index, dot in enumerate(dots):
            size = 6 if index == active_dot else 3
            center_x = bubble.winfo_width() - 28 + index * 8
            baseline = bubble.winfo_height() - 11
            lift = 3 if index == active_dot else 0
            bubble.coords(dot, center_x - size / 2, baseline - size / 2 - lift,
                          center_x + size / 2, baseline + size / 2 - lift)
            bubble.itemconfigure(dot, fill=colors["accent"] if index == active_dot else colors["border"])
        self._schedule_animation(
            lambda: self._animate_research_indicator(bubble, dots, colors), 100
        )

    def _stop_research_indicators(self):
        for item in self.chat_items:
            widget = item.get("widget")
            if not widget:
                continue
            for child in widget.winfo_children():
                if isinstance(child, tk.Canvas):
                    child.delete("research-indicator")

    @staticmethod
    def _draw_rounded_rectangle(canvas, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        canvas.create_polygon(points, smooth=True, splinesteps=20, **kwargs)

    def _load_api_key(self):
        if self.tinyfish_api_key:
            self._set_connection_status("TinyFish key found", "success")
            self._append(
                "TinyFish API key found. Research mode is ready; selected folders are only used for local file actions.\n",
                role="system",
            )
        else:
            self._set_connection_status("TinyFish key needed", "missing")
            self._append(self._t("tinyfish_key_error"), role="system")

    def _append(self, text, role="system"):
        message = text.rstrip("\n")
        item = {"role": role, "text": message, "widget": None, "send_bubbles": []}
        self.chat_items.append(item)
        if role == "user" and not self._has_user_messages:
            self._has_user_messages = True
            self._composer_bubble_emission_active = True
            self._background_mode = None
            self._draw_chat_background()
        if role == "user" and self.title_label.cget("text") in {"New conversation", "नई बातचीत"}:
            title = (message[5:] if message.startswith("You: ") else message).replace("\n", " ").strip()
            self.title_label.configure(text=title[:36] + ("…" if len(title) > 36 else ""))
        self._create_chat_bubble(item)
        self.chat.after_idle(lambda: self.chat.yview_moveto(1.0))

    def _set_connection_status(self, text, color):
        if color == "success":
            self.connection_status = "connected"
        elif color == "unavailable":
            self.connection_status = "unavailable"
        else:
            self.connection_status = "missing"

    def add_folder(self):
        chosen = filedialog.askdirectory(title="Choose a folder the assistant may access", mustexist=True)
        if not chosen:
            return
        try:
            path = safe_root(chosen)
        except Exception as exc:
            messagebox.showerror("Folder not available", str(exc))
            return

        # Avoid duplicates and redundant nested roots; users can still add any separate folder.
        if path in self.folders:
            messagebox.showinfo("Already added", "That folder is already selected.")
            return
        self.folders.append(path)
        self._refresh_folders()
        self._append(f"Added folder {len(self.folders)}: {path}\n", role="system")

    def remove_folder(self):
        selection = self.folder_list.curselection()
        if not selection:
            messagebox.showinfo("Select a folder", "Choose a folder in the list first.")
            return
        idx = selection[0]
        removed = self.folders.pop(idx)
        self.organization_plan = None
        self._refresh_folders()
        self._append(f"Removed selected folder: {removed}\nThe previous organization plan was cleared.\n", role="system")

    def _refresh_folders(self):
        self.folder_list.delete(0, "end")
        for index, folder in enumerate(self.folders, start=1):
            self.folder_list.insert("end", f"{index}. {folder}")
        count = len(self.folders)
        self.folder_count_label.configure(
            text=f"{count} folder{'s' if count != 1 else ''} connected"
        )

    def _organization_method_labels(self):
        return [
            self._t("organize_type"),
            self._t("organize_name"),
            self._t("organize_size"),
            self._t("organize_modified"),
        ]

    def _organization_sort_labels(self):
        return [
            self._t("organize_sort_name"),
            self._t("organize_sort_modified_newest"),
            self._t("organize_sort_modified_oldest"),
            self._t("organize_sort_size_largest"),
            self._t("organize_sort_size_smallest"),
        ]

    def _organization_method_changed(self, _event=None):
        selected = self.organize_method_var.get()
        methods = {
            self._t("organize_type"): "type",
            self._t("organize_name"): "name",
            self._t("organize_size"): "size",
            self._t("organize_modified"): "modified",
        }
        if selected not in methods:
            raise ValueError("Choose a supported organization method.")
        method = methods[selected]
        if method != self.organization_method and self.organization_plan:
            self.organization_plan = None
            self._append(
                "The organization method changed; the previous preview was cleared. Preview again before applying.\n",
                role="system",
            )
        self.organization_method = method

    def _organization_folder_changed(self, _event=None):
        if not self.organization_plan:
            return
        selection = self.folder_list.curselection()
        selected_root_index = selection[0] + 1 if selection else None
        if selected_root_index != self.organization_plan.get("root_index"):
            self.organization_plan = None
            self._append(
                "The selected folder changed; the previous preview was cleared. Preview again before applying.\n",
                role="system",
            )

    def _organization_sort_method(self):
        methods = {
            self._t("organize_sort_name"): "name",
            self._t("organize_sort_modified_newest"): "modified_newest",
            self._t("organize_sort_modified_oldest"): "modified_oldest",
            self._t("organize_sort_size_largest"): "size_largest",
            self._t("organize_sort_size_smallest"): "size_smallest",
        }
        try:
            return methods[self.organize_sort_var.get()]
        except KeyError as exc:
            raise ValueError("Choose a supported sort order.") from exc

    def _current_organization_options(self):
        return {"sort_by": self._organization_sort_method()}

    def _organization_options_changed(self, *_args):
        sort_by = self._organization_sort_method()
        self.organization_sort_by = sort_by
        self.organization_options = self._current_organization_options()
        if self.organization_plan:
            self.organization_plan = None
            self._append(
                "Organization options changed; the previous preview was cleared. Preview again before applying.\n",
                role="system",
            )

    def _preview_selected_organization(self):
        selection = self.folder_list.curselection()
        if not selection:
            messagebox.showinfo(
                self._t("organizer_title"), self._t("organize_select_folder")
            )
            return
        try:
            result = self._tool_preview(
                selection[0] + 1,
                self.organization_method,
                **self._current_organization_options(),
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror(self._t("organizer_title"), str(exc))
            return

        display_result = dict(result)
        if len(result["moves"]) > 100:
            display_result["moves"] = result["moves"][:100]
            display_result["note"] += " Showing the first 100 moves."
        self._append(
            self._t("organize_preview_title")
            + ":\n"
            + json.dumps(display_result, ensure_ascii=False, indent=2)
            + "\n",
            role="system",
        )
        self._show_page("chat")

    def _review_selected_duplicates(self):
        selection = self.folder_list.curselection()
        if not selection:
            messagebox.showinfo(
                self._t("organizer_title"), self._t("organize_select_folder")
            )
            return
        try:
            result = self._tool_review_duplicates(selection[0] + 1)
        except (OSError, ValueError) as exc:
            messagebox.showerror(self._t("organize_duplicate_title"), str(exc))
            return

        display_result = dict(result)
        if len(result["groups"]) > 100:
            display_result["groups"] = result["groups"][:100]
            display_result["note"] += " Showing the first 100 groups."
        self._append(
            self._t("organize_duplicate_title")
            + ":\n"
            + json.dumps(display_result, ensure_ascii=False, indent=2)
            + "\n",
            role="system",
        )
        self._show_page("chat")

    def _apply_organization_preview(self):
        result = self._tool_apply()
        self._append(
            "Organization result:\n"
            + json.dumps(result, ensure_ascii=False, indent=2)
            + "\n",
            role="system",
        )
        self._show_page("chat")

    def _root_by_index(self, index):
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("Folder index must be an integer.")
        if index < 1 or index > len(self.folders):
            raise ValueError("That folder index is not in the current selected-folder list.")
        root = safe_root(str(self.folders[index - 1]))
        return root

    def _tool_list_files(self, root_index, subfolder):
        root = self._root_by_index(root_index)
        folder = safe_child(root, subfolder, must_exist=True)
        if not folder.is_dir():
            raise ValueError("That path is not a folder.")
        entries = []
        for item in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            try:
                if item.is_symlink():
                    continue
                if not is_inside(item.resolve(strict=True), root):
                    continue
                suffix = "/" if item.is_dir() else ""
                entries.append(item.name + suffix)
            except (OSError, RuntimeError):
                continue
            if len(entries) >= 500:
                break
        rel = "." if folder == root else str(folder.relative_to(root))
        return {"root_index": root_index, "folder": rel, "entries": entries, "truncated": len(entries) >= 500}

    def _tool_search(self, query):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Search text cannot be empty.")
        matches = []
        q = query.casefold()
        for index, stored_root in enumerate(self.folders, start=1):
            try:
                root = safe_root(str(stored_root))
            except (OSError, ValueError):
                continue
            for current, dirs, files in os.walk(root, followlinks=False, onerror=lambda _e: None):
                current_path = Path(current)
                dirs[:] = [
                    name for name in dirs
                    if not (current_path / name).is_symlink()
                    and self._is_safe_resolved(current_path / name, root)
                ]
                for name in files:
                    p = current_path / name
                    if p.is_symlink() or q not in name.casefold():
                        continue
                    try:
                        resolved = p.resolve(strict=True)
                        if not is_inside(resolved, root):
                            continue
                        rel = str(p.relative_to(root))
                        matches.append({"root_index": index, "path": rel})
                    except (OSError, RuntimeError):
                        continue
                    if len(matches) >= 500:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    @staticmethod
    def _is_safe_resolved(path, root):
        try:
            return is_inside(path.resolve(strict=True), root)
        except (OSError, RuntimeError):
            return False

    def _tool_preview(
        self, root_index, method="type", sort_by="name",
    ):
        root = self._root_by_index(root_index)
        plan, skipped = build_organization_preview(root, method, sort_by)
        for item in plan:
            item["root_index"] = root_index
        self.organization_plan = {
            "root_index": root_index,
            "root": root,
            "method": method,
            "moves": plan,
            "sort_by": sort_by,
        }
        return {
            "root_index": root_index,
            "folder_name": root.name or str(root),
            "method": method,
            "sort_by": sort_by,
            "moves": [{"source": x["source_name"], "destination": f'{x["destination_folder"]}/{x["destination_name"]}'} for x in plan],
            "count": len(plan),
            "skipped": skipped,
            "note": "Preview only; no files have been changed. Only files directly inside this selected folder are considered. Sort order applies to this preview and its confirmation; Windows controls folder display order."
        }

    def _tool_review_duplicates(self, root_index):
        root = self._root_by_index(root_index)
        groups = {}
        scanned_count = 0
        unreadable_count = 0
        walk_errors = []
        truncated = False

        def record_walk_error(error):
            if len(walk_errors) < 20:
                walk_errors.append(str(error))

        for current, dirs, files in os.walk(
            root, followlinks=False, onerror=record_walk_error
        ):
            current_path = Path(current)
            dirs[:] = [
                name for name in dirs
                if not (current_path / name).is_symlink()
                and self._is_safe_resolved(current_path / name, root)
            ]
            for name in files:
                path = current_path / name
                if path.is_symlink():
                    continue
                try:
                    if not is_inside(path.resolve(strict=True), root):
                        continue
                    stat_result = path.stat()
                except (OSError, RuntimeError):
                    unreadable_count += 1
                    continue
                scanned_count += 1
                key = (name.casefold(), stat_result.st_size)
                groups.setdefault(key, []).append(
                    str(path.relative_to(root))
                )
                if scanned_count >= MAX_DUPLICATE_SCAN_FILES:
                    truncated = True
                    break
            if truncated:
                break

        duplicate_groups = [
            {
                "filename": key[0],
                "size_bytes": key[1],
                "paths": sorted(paths, key=str.casefold),
            }
            for key, paths in groups.items()
            if len(paths) > 1
        ]
        duplicate_groups.sort(
            key=lambda item: (item["filename"], item["size_bytes"])
        )
        return {
            "root_index": root_index,
            "scanned_files": scanned_count,
            "unreadable_files": unreadable_count,
            "scan_errors": walk_errors,
            "groups": duplicate_groups,
            "truncated": truncated,
            "note": self._t("organize_duplicate_note"),
        }

    def _tool_apply(self):
        saved = self.organization_plan
        if not saved:
            return {"status": "No current preview. Preview an organization plan first."}
        root = safe_root(str(saved["root"]))
        if root != saved["root"]:
            self.organization_plan = None
            return {"status": "Selected folder changed; preview again."}
        moves = saved["moves"]
        if not moves:
            self.organization_plan = None
            return {"status": "Nothing needs to be moved."}

        lines = []
        for item in moves[:100]:
            lines.append(f'{item["source_name"]}  ->  {item["destination_folder"]}/{item["destination_name"]}')
        if len(moves) > 100:
            lines.append(f"... and {len(moves) - 100} more move(s)")
        message = (
            f"Organize by {saved['method']} and move {len(moves)} file(s)?\n\n{root}\n\n"
            + "\n".join(lines)
            + "\n\nNo files will be overwritten. Continue?"
        )
        approved = messagebox.askyesno("Confirm file moves", message, icon="warning")
        if not approved:
            self.organization_plan = None
            return {"status": "Cancelled by user. No files were moved."}

        moved, skipped = [], []
        for item in moves:
            try:
                # Only simple filenames and generated organization folders are accepted.
                source_name = item["source_name"]
                destination_folder = item["destination_folder"]
                if Path(source_name).name != source_name or source_name in {".", ".."}:
                    raise ValueError("Unsafe source filename.")
                if (
                    Path(destination_folder).name != destination_folder
                    or destination_folder in {"", ".", ".."}
                    or "/" in destination_folder
                    or "\\" in destination_folder
                ):
                    raise ValueError("Unsafe destination folder.")
                source = root / source_name
                if source.is_symlink() or not source.is_file() or not is_inside(source.resolve(strict=True), root):
                    skipped.append({"source": source_name, "reason": "Source is missing or unsafe."})
                    continue

                target_folder = root / destination_folder
                if target_folder.exists() and (target_folder.is_symlink() or not target_folder.is_dir()):
                    skipped.append({"source": source_name, "reason": "Destination folder is not a safe directory."})
                    continue
                target_folder.mkdir(exist_ok=True)
                target_folder = target_folder.resolve(strict=True)
                if not is_inside(target_folder, root):
                    skipped.append({"source": source_name, "reason": "Destination escaped selected folder."})
                    continue

                target = target_folder / source_name
                if target.exists() or target.is_symlink():
                    skipped.append({"source": source_name, "reason": "Destination already exists."})
                    continue
                # Source is a direct child of root; target is inside a child folder of root.
                source.rename(target)
                moved.append({
                    "source": source_name,
                    "destination": f"{destination_folder}/{source_name}",
                })
            except Exception as exc:
                skipped.append({"source": item.get("source_name", "?"), "reason": str(exc)})

        self.organization_plan = None
        return {"status": "Organization finished.", "moved": moved, "skipped": skipped}

    def _run_tool(self, name, args):
        if name == "list_files":
            return self._tool_list_files(args.get("root_index"), args.get("subfolder", ""))
        if name == "search_filenames":
            return self._tool_search(args.get("query", ""))
        if name == "preview_organization":
            return self._tool_preview(
                args.get("root_index"),
                args.get("method", self.organization_method),
                args.get("sort_by", "name"),
            )
        if name == "review_duplicates":
            return self._tool_review_duplicates(args.get("root_index"))
        if name == "apply_organization":
            return self._tool_apply()
        raise ValueError("Unsupported tool requested.")

    def send_message(self):
        if self.busy:
            return
        if self.prompt_placeholder_active:
            return
        text = self.prompt.get("1.0", "end").strip()
        if not text:
            return
        self.prompt.delete("1.0", "end")
        self._append(f"You: {text}\n", role="user")
        sent_item = self.chat_items[-1]
        self._schedule_animation(
            lambda target=sent_item: self._release_upward_current(target), 90
        )

        if not self.tinyfish_api_key and not self._is_local_command(text):
            messagebox.showerror(
                "TinyFish API key missing",
                "Create a .env file beside the app with TINYFISH_API_KEY=your_tinyfish_key_here, then restart HADAL."
            )
            return

        self.busy = True
        self.send_button.configure(state="disabled")
        self._append(self._t("research_status") + "\n", role="thinking")

        threading.Thread(target=self._request_worker, args=(text,), daemon=True).start()

    def _request_worker(self, user_text):
        try:
            local_result = self._run_local_command(user_text)
            if local_result is not None:
                answer = "Local file action:\n" + json.dumps(local_result, ensure_ascii=False, indent=2)
            else:
                report = run_tinyfish_research(
                    user_text,
                    self.tinyfish_api_key,
                    language=self.language,
                    session_id=self.research_session_id,
                    on_delta=lambda text: self.root.after(
                        0, self._show_research_progress, text
                    ),
                )
                self.research_session_id = report["session_id"]
                answer = format_research_report(report["result"])
            self.root.after(0, self._finish_request, answer, None)
        except Exception as exc:
            self.root.after(0, self._finish_request, None, str(exc))

    def _show_research_progress(self, text):
        for item in reversed(self.chat_items):
            if item["role"] == "thinking":
                item["text"] = f"Assistant: {text}" if text else self._t("research_status")
                self._render_chat_items()
                return

    def _run_local_command(self, user_text):
        match = re.fullmatch(r"\s*list files in folder\s+(\d+)\s*", user_text, re.IGNORECASE)
        if match:
            return self._tool_list_files(int(match.group(1)), "")

        match = re.fullmatch(r"\s*search filenames for(?:\s+(.*))?\s*", user_text, re.IGNORECASE)
        if match:
            return self._tool_search(match.group(1))

        match = re.fullmatch(
            r"\s*preview organization for folder\s+(\d+)"
            r"(?:\s+by\s+(type|name|size|modified))?\s*",
            user_text,
            re.IGNORECASE,
        )
        if match:
            return self._tool_preview(
                int(match.group(1)),
                (match.group(2) or self.organization_method).casefold(),
                **self.organization_options,
            )

        match = re.fullmatch(
            r"\s*review duplicates in folder\s+(\d+)\s*",
            user_text,
            re.IGNORECASE,
        )
        if match:
            return self._tool_review_duplicates(int(match.group(1)))

        if re.fullmatch(
            r"\s*apply (?:the )?(?:previewed )?organization\s*", user_text, re.IGNORECASE
        ):
            return self._run_tool_threadsafe("apply_organization", {})
        return None

    @staticmethod
    def _is_local_command(user_text):
        return any((
            re.fullmatch(r"\s*list files in folder\s+\d+\s*", user_text, re.IGNORECASE),
            re.fullmatch(r"\s*search filenames for(?:\s+.*)?\s*", user_text, re.IGNORECASE),
            re.fullmatch(
                r"\s*preview organization for folder\s+\d+"
                r"(?:\s+by\s+(?:type|name|size|modified))?\s*",
                user_text,
                re.IGNORECASE,
            ),
            re.fullmatch(
                r"\s*review duplicates in folder\s+\d+\s*",
                user_text,
                re.IGNORECASE,
            ),
            re.fullmatch(
                r"\s*apply (?:the )?(?:previewed )?organization\s*", user_text, re.IGNORECASE
            ),
        ))

    def _run_tool_threadsafe(self, name, args):
        if name != "apply_organization":
            # Folder state is read by the worker; GUI mutations are disabled only for Send,
            # so protect list state with the app's normal simple single-user interaction model.
            return self._run_tool(name, args)

        event = threading.Event()
        result_box = {}

        def ask_on_gui_thread():
            try:
                result_box["result"] = self._tool_apply()
            except Exception as exc:
                result_box["result"] = {"error": str(exc)}
            finally:
                event.set()

        self.root.after(0, ask_on_gui_thread)
        if not event.wait(timeout=600):
            return {"status": "Confirmation timed out; no move was performed."}
        return result_box.get("result", {"error": "No confirmation result."})

    def _finish_request(self, answer, error):
        # Remove the temporary "Thinking…" line without clearing earlier conversation.
        self._remove_thinking_line()
        if error:
            self._append(f"Error: {error}\n", role="system")
        else:
            self._append(f"Assistant: {answer}\n", role="assistant")
        self.busy = False
        self.send_button.configure(state="normal")

    def _remove_thinking_line(self):
        for index in range(len(self.chat_items) - 1, -1, -1):
            if self.chat_items[index]["role"] == "thinking":
                self.chat_items.pop(index)
                self._render_chat_items()
                break


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    LocalFileAIApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
