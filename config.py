# -*- coding: utf-8 -*-

# Predefiniowane role i ich instrukcje systemowe
import json
import os

# --- Wczytywanie pliku .env (jeśli istnieje) ---
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ[_k.strip()] = _v.strip().strip('"').strip("'")
    except Exception:
        pass


DEFAULT_ROLE_PRESETS = {
    "Ekspert/Krytyk": (
        "Twoje imię to Krytyk. Jesteś cynicznym, sceptycznym i niezwykle merytorycznym oponentem. Twoim zadaniem jest bezlitosne punktowanie błędów w argumentacji rozmówcy, wskazywanie słabych punktów, antywzorców lub luk logicznych w dostarczonym temacie bądź kodzie. PISZ WYŁĄCZNIE PO POLSKU. "
    ),
    "Programista": (
        "Twoje imię to Deweloper. Jesteś dumnym, pragmatycznym i niezwykle sprawnym inżynierem oraz realistą. Twoim zadaniem jest odpieranie ataków Krytyka poprzez podawanie gotowych, zoptymalizowanych rozwiązań, kontrargumentów lub czystego, zrefaktoryzowanego kodu w blokach markdown, jeśli temat dotyczy programowania. PISZ WYŁĄCZNIE PO POLSKU."
    ),
    "Własny": ""
}

def load_role_presets():
    roles_path = os.path.join(os.path.dirname(__file__), "roles.json")
    presets = DEFAULT_ROLE_PRESETS.copy()
    if os.path.exists(roles_path):
        try:
            with open(roles_path, "r", encoding="utf-8") as f:
                custom_presets = json.load(f)
                if "Własny" not in custom_presets:
                    custom_presets["Własny"] = ""
                presets.update(custom_presets)
        except Exception:
            pass
    return presets

def save_role_presets(presets):
    roles_path = os.path.join(os.path.dirname(__file__), "roles.json")
    to_save = {}
    for name, prompt in presets.items():
        if name != "Własny":
            to_save[name] = prompt
    try:
        with open(roles_path, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

ROLE_PRESETS = load_role_presets()

# --- Ustawienia API DeepSeek ---
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# Lista aktualnych modeli dostępnych w systemie (stan na 2026)
AVAILABLE_MODELS = [
    "deepseek-chat",
    "deepseek-v4",
    "gemini-2.5-flash", 
    "gemini-2.5-pro", 
    "llama3.1", 
    "llama3"
]

# Domyślny temat początkowy debaty
DEFAULT_INITIAL_TOPIC = "Czy sztuczna inteligencja całkowicie zastąpi programistów w ciągu najbliższych 5 lat?"

# Stałe UI
APP_TITLE = "Multi-Agent AI Debate Arena"
WINDOW_WIDTH = 1100
WINDOW_HEIGHT = 880
WINDOW_MIN_WIDTH = 950
WINDOW_MIN_HEIGHT = 850

# Kolory dla logów debaty (w formacie HEX dla CustomTkinter)
COLOR_AGENT_A = "#3B82F6"  # Niebieski
COLOR_AGENT_B = "#10B981"  # Szmaragdowy
COLOR_SYSTEM = "#9CA3AF"   # Szary
COLOR_ERROR = "#EF4444"    # Czerwony

# Domyślny plik autozapisu kontekstu rozmowy
DEFAULT_CONTEXT_FILE = "ostatni_kontekst.txt"

# Brak limitów znaków/linii wczytywanych z plików kontekstowych
MAX_TOTAL_CONTEXT_CHARS = 100000  # Maksymalny twardy limit znaków wczytanych z plików projektu do kontekstu

# Biała lista rozszerzeń plików, które aplikacja może czytać
ALLOWED_EXTENSIONS = [
    '.py', '.txt', '.md', '.json', '.js', '.ts', '.jsx', '.tsx', 
    '.html', '.css', '.yaml', '.yml', '.swift', '.cpp', '.h', '.rs',
    '.cs', '.asmdef'
]

# Czarna lista folderów, które należy BEZWZGLĘDNIE ignorować przy skanowaniu dysku
DENIED_FOLDERS = [
    '.git', '.vscode', '__pycache__', 'node_modules', 'target', 
    'build', 'dist', 'venv', '.venv', 'env', '.idea', '.terraform'
]

# --- Zaawansowane Ustawienia Lektora (TTS) ---
ENABLE_TTS_BY_DEFAULT = True  # Domyślny stan włączenia lektora
TTS_VOLUME = 0.8              # Głośność lektora (0.0 do 1.0)
TTS_LANG = "pl"               # Język syntezy mowy
TTS_SLOW_BY_DEFAULT = False   # Domyślna prędkość lektora (True = wolno, False = normalnie)

# Parametry czyszczenia cache LRU i słownik akronimów
TTS_CACHE_MAX_SIZE_MB = 100
TTS_CACHE_MAX_AGE_DAYS = 30
TTS_CACHE_CLEANUP_INTERVAL_SECONDS = 3600
TTS_CACHE_CLEANUP_THRESHOLD_PERCENT = 90

# Mapowanie akronimów i skrótów dla lektora polskojęzycznego
TTS_ACRONYM_MAP = {
    "AI": "sztuczna inteligencja",
    "SQL": "es-ku-el",
    "API": "a-pe-i",
    "HTTP": "ha-te-te-pe",
    "JSON": "dżej-son",
    "YAML": "jamel",
    "DevOps": "dew-ops",
    "Git": "git",
    "UI": "u-i",
    "UX": "u-iks"
}
