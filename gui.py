# -*- coding: utf-8 -*-
import os
import sys
import queue
import base64
import json
import threading
import tempfile
import re
import time
import collections
import hashlib
import shutil
import subprocess
import requests.exceptions
import customtkinter as ctk
from tkinter import messagebox
from config import *
from debate_manager import DebateManager

# Zaawansowane czyszczenie formatowania Markdown i gTTS
from gtts import gTTS
import markdown

from config import (
    ENABLE_TTS_BY_DEFAULT, TTS_VOLUME, TTS_LANG, TTS_CACHE_MAX_SIZE_MB, 
    TTS_CACHE_MAX_AGE_DAYS, TTS_CACHE_CLEANUP_INTERVAL_SECONDS, 
    TTS_CACHE_CLEANUP_THRESHOLD_PERCENT, TTS_ACRONYM_MAP
)

# Definicja ścieżki cache
TTS_CACHE_DIR = os.path.join(tempfile.gettempdir(), "debate_tts_cache")
TTS_CACHE_INDEX_FILE = os.path.join(TTS_CACHE_DIR, "tts_cache_index.json")

# Lekka i bezpieczna obsługa pygame.mixer z automatycznym fallbackiem do natywnego afplay na macOS
try:
    import pygame.mixer
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False
    from types import ModuleType
    
    class DummyMixer:
        def __init__(self):
            self._proc = None
            self._volume = 1.0

        def init(self):
            pass

        def get_init(self):
            return True

        def quit(self):
            self.stop()

        def set_num_channels(self, num):
            pass

        def Channel(self, num):
            return self

        class Sound:
            def __init__(self, file_path):
                self.file_path = file_path

        def play(self, sound):
            self.stop()
            import subprocess
            try:
                if os.name == 'posix':
                    self._proc = subprocess.Popen(["afplay", "-v", str(self._volume), sound.file_path])
                else:
                    self._proc = subprocess.Popen(["ffplay", "-nodisp", "-autoexit", sound.file_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except:
                pass

        def stop(self):
            if self._proc:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=0.2)
                except:
                    try:
                        self._proc.kill()
                    except:
                        pass
                self._proc = None

        def get_busy(self):
            if self._proc:
                if self._proc.poll() is None:
                    return True
                else:
                    self._proc = None
            return False

        def set_volume(self, volume):
            self._volume = volume

    pygame_mixer_shim = DummyMixer()
    
    # Tworzymy dummy module pygame
    pygame_module = ModuleType("pygame")
    # Tworzymy dummy module pygame.mixer
    pygame_mixer_module = ModuleType("pygame.mixer")
    
    pygame_mixer_module.init = pygame_mixer_shim.init
    pygame_mixer_module.get_init = pygame_mixer_shim.get_init
    pygame_mixer_module.quit = pygame_mixer_shim.quit
    pygame_mixer_module.set_num_channels = pygame_mixer_shim.set_num_channels
    pygame_mixer_module.Channel = pygame_mixer_shim.Channel
    pygame_mixer_module.Sound = pygame_mixer_shim.Sound
    pygame_mixer_module.stop = pygame_mixer_shim.stop
    
    # Wiążemy atrybut mixer w module pygame
    pygame_module.mixer = pygame_mixer_module
    
    # Rejestrujemy oba w sys.modules
    sys.modules["pygame"] = pygame_module
    sys.modules["pygame.mixer"] = pygame_mixer_module
    import pygame.mixer

# Ustawienie motywu graficznego (ciemny motyw macOS)
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

def clean_text_for_tts(text: str, acronym_map: dict) -> str:
    # 1. Zamiana akronimów
    for acronym, replacement in acronym_map.items():
        text = re.sub(r'\b' + re.escape(acronym) + r'\b', replacement, text, flags=re.IGNORECASE)

    # 2. Usuwanie formatowania Markdown i bloków kodu
    text = re.sub(r'```.*?```', ' [blok kodu] ', text, flags=re.DOTALL)
    text = re.sub(r'`[^`]+`', ' [fragment kodu] ', text)
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
    text = re.sub(r'^[ \t]*#+\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,2}(.*?)\*{1,2}', r'\1', text) 
    text = re.sub(r'_{1,2}(.*?)_{1,2}', r'\1', text) 

    # 3. Normalizacja spacji
    text = re.sub(r'\s+', ' ', text).strip()

    # 4. Obsługa krytycznego edge-case (pusty tekst)
    if not text:
        return "[cisza]"
    return text

class LogWindow(ctk.CTkToplevel):
    """
    Osobne okno dedykowane wyłącznie do wyświetlania logów i podglądu debaty.
    Odporne na krasze dzięki ukrywaniu okna zamiast jego niszczenia przy zamknięciu.
    """
    def __init__(self, master, *args, **kwargs):
        super().__init__(master, *args, **kwargs)
        self.title("Podgląd Debaty i Logi w Czasie Rzeczywistym")
        self.geometry("800x600")
        self.minsize(400, 300)
        
        # Konfiguracja skalowania siatki
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_columnconfigure(2, weight=0)
        
        # Panel górny w oknie logów
        self.log_label = ctk.CTkLabel(self, text="Logi i podgląd debaty w czasie rzeczywistym:", font=("Helvetica Neue", 12, "bold"))
        self.log_label.grid(row=0, column=0, padx=15, pady=(10, 2), sticky="w")
        
        # Ramka wyboru docelowej liczby rund
        self.rounds_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.rounds_frame.grid(row=0, column=1, padx=10, pady=(10, 2), sticky="e")
        
        self.rounds_label = ctk.CTkLabel(self.rounds_frame, text="Docelowa liczba rund:", font=("Helvetica Neue", 11, "bold"))
        self.rounds_label.pack(side="left", padx=5)
        
        self.rounds_dropdown = ctk.CTkOptionMenu(
            self.rounds_frame,
            values=["Bez limitu", "3", "5", "10", "15", "20", "30", "50"],
            width=110,
            height=22,
            font=("Helvetica Neue", 11)
        )
        self.rounds_dropdown.pack(side="left", padx=5)
        self.rounds_dropdown.set("Bez limitu")
        
        # Checkbox do automatycznego przewijania
        self.autoscroll_var = ctk.BooleanVar(value=True)
        self.autoscroll_cb = ctk.CTkCheckBox(
            self, 
            text="Automatyczne przewijanie", 
            variable=self.autoscroll_var, 
            font=("Helvetica Neue", 11)
        )
        self.autoscroll_cb.grid(row=0, column=2, padx=15, pady=(10, 2), sticky="e")
        
        # Przeniesiony widget tekstowy logów z okna głównego
        self.debate_log = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Helvetica Neue", size=12), wrap="word")
        self.debate_log.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=10, pady=10)
        self.debate_log.configure(state="disabled") # Domyślnie tylko do odczytu
        
        # Konfiguracja tagów formatujących bezpiecznie
        self.update_idletasks()
        try:
            self.debate_log._textbox.tag_config("agent_a", foreground=COLOR_AGENT_A, font=("Helvetica Neue", 12, "bold"))
            self.debate_log._textbox.tag_config("agent_b", foreground=COLOR_AGENT_B, font=("Helvetica Neue", 12, "bold"))
            self.debate_log._textbox.tag_config("system", foreground=COLOR_SYSTEM, font=("Helvetica Neue", 11, "italic"))
            self.debate_log._textbox.tag_config("error", foreground=COLOR_ERROR, font=("Helvetica Neue", 11, "bold"))
            self.debate_log._textbox.tag_config("normal", font=("Helvetica Neue", 12))
        except AttributeError:
            pass

        # Przechwycenie protokołu zamknięcia okna [X] - zamiast niszczyć, ukrywamy okno!
        self.protocol("WM_DELETE_WINDOW", self.hide_window)

    def hide_window(self):
        """Ukrywa okno, zachowując obiekt i dane w pamięci."""
        self.withdraw()

    def show_case(self):
        """Wyświetla okno na froncie screenu."""
        self.deiconify()
        self.focus()

class DebateApp(ctk.CTk):
    """
    Główna klasa aplikacji GUI dziedzicząca po ctk.CTk.
    Zapewnia układ elementów w ciemnym motywie dla macOS.
    """
    def __init__(self):
        super().__init__()
        
        # Flagi stanu kontekstu debaty
        self.has_history = False
        self.added_prompts_list = []
        self.context_folder_path = ""

        from config import TTS_SLOW_BY_DEFAULT

        # Flagi i synchronizacja TTS
        self.tts_enabled = ctk.BooleanVar(value=ENABLE_TTS_BY_DEFAULT)
        self.tts_slow = ctk.BooleanVar(value=TTS_SLOW_BY_DEFAULT) # Nowa flaga prędkości (True=wolno, False=normalnie)
        
        # Powiązanie zmiany przełącznika ON/OFF z natychmiastowym uciszeniem lektora, jeśli użytkownik go wyłączy
        self.tts_enabled.trace_add("write", lambda *args: self.stop_tts_and_clear_queue() if not self.tts_enabled.get() else None)

        self.tts_playing_event = threading.Event()
        self._cache_cleanup_stop_event = threading.Event()
        self._playback_stop_event = threading.Event()
        self._playback_queue = queue.Queue()
        self.tts_queue = queue.Queue() # Dodatkowa kolejka zadań
        self._cache_index = collections.OrderedDict()
        self._lock = threading.Lock()
        self._afplay_process = None
        self._cleanup_thread = None
        self._playback_thread = None
        self._is_generating_tts = False

        # Define audio_player helper interface
        class AudioPlayerInterface:
            def __init__(self, app):
                self.app = app
            def is_ready(self):
                return True
            def is_playing(self):
                return self.app.tts_playing_event.is_set()
            def play_file(self, file_path, on_finish_callback=None):
                def play_task():
                    self.app._actual_play_audio_internal(file_path)
                    if on_finish_callback:
                        on_finish_callback()
                threading.Thread(target=play_task, daemon=True).start()
                
        self.audio_player = AudioPlayerInterface(self)

        # Bezpieczna inicjalizacja miksera audio Pygame
        try:
            pygame.mixer.init()
            pygame.mixer.set_num_channels(8)
            self.tts_channel = pygame.mixer.Channel(0)
            self.tts_channel.set_volume(TTS_VOLUME)
            self.has_pygame = True
        except Exception as e:
            logging.getLogger("DebateApp").warning(f"[TTS] Pygame mixer niedostępny, używam systemowego fallbacku. Błąd: {e}")
            self.has_pygame = False

        # Inicjalizacja katalogu cache i wątków demonicznych
        os.makedirs(TTS_CACHE_DIR, exist_ok=True)
        self._load_cache_index()
        
        self._cleanup_thread = threading.Thread(target=self._periodic_cache_cleanup, daemon=True)
        self._cleanup_thread.start()
        
        self._playback_thread = threading.Thread(target=self._playback_worker, daemon=True)
        self._playback_thread.start()
        
        # Konfiguracja okna głównego
        self.title(APP_TITLE)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        
        # Inicjalizacja kolejki komunikacyjnej i zarządcy debaty
        self.message_queue = queue.Queue()
        self.debate_manager = DebateManager(self.message_queue)
        self.is_running = False
        
        # Klucze API przechowywane wyłącznie w pamięci RAM (w zmiennych obiektu)
        raw_key = os.getenv("GEMINI_API_KEY", "")
        self.gemini_api_key = raw_key.strip().strip('"').strip("'") if raw_key else ""
        
        raw_ds_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.deepseek_api_key = raw_ds_key.strip().strip('"').strip("'") if raw_ds_key else ""
        
        # Konfiguracja rozciągania wierszy/kolumn okna głównego
        self.grid_columnconfigure(0, weight=1, uniform="agents")
        self.grid_columnconfigure(1, weight=1, uniform="agents")
        
        self.grid_rowconfigure(0, weight=0)  # API panel
        self.grid_rowconfigure(1, weight=1)  # Agent panels (teraz mogą się skalować)
        self.grid_rowconfigure(2, weight=0)  # Control panel
        
        # Tworzenie widgetów interfejsu
        self._create_widgets()
        self.log_window = LogWindow(self)
        self.log_window.withdraw()
        
        # Wpisanie klucza ze zmiennej środowiskowej do pola tekstowego (jeśli istnieje)
        if self.gemini_api_key:
            self.api_entry.insert(0, self.gemini_api_key)
        if self.deepseek_api_key:
            self.ds_api_entry.insert(0, self.deepseek_api_key)
        
        # Aktualizacja statusu przy wpisywaniu klucza API
        self.api_entry.bind("<KeyRelease>", lambda _: self._update_ui_state())
        self.ds_api_entry.bind("<KeyRelease>", lambda _: self._update_ui_state())
        
        # Obsługa zamknięcia okna ze sprzątaniem audio
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Rozpoczęcie okresowego sprawdzania kolejki wiadomości z wątku debaty
        self._check_queue()
        
        # Rozpoczęcie okresowej aktualizacji stanu interfejsu (Ollama/Gemini)
        self._periodic_ui_update()
        
        # Inicjalizacja pętli obsługi kolejki lektora (TTS)
        self._tts_loop_after_id = None
        self._check_tts_queue_loop()
        
    def _create_widgets(self):
        # 1. Górny panel konfiguracji API (Row 0)
        self.api_frame = ctk.CTkFrame(self)
        self.api_frame.grid(row=0, column=0, columnspan=2, padx=15, pady=(15, 5), sticky="ew")
        self.api_frame.grid_columnconfigure(1, weight=1)
        
        # Row 0: Gemini API Key
        self.api_label = ctk.CTkLabel(self.api_frame, text="Gemini API Key:", font=("Helvetica Neue", 12, "bold"))
        self.api_label.grid(row=0, column=0, padx=(15, 5), pady=(12, 6), sticky="w")
        
        self.api_entry = ctk.CTkEntry(self.api_frame, placeholder_text="Wprowadź swój Gemini API Key...", show="*")
        self.api_entry.grid(row=0, column=1, padx=10, pady=(12, 6), sticky="ew")
        
        # Row 1: DeepSeek API Key
        self.ds_api_label = ctk.CTkLabel(self.api_frame, text="DeepSeek API Key:", font=("Helvetica Neue", 12, "bold"))
        self.ds_api_label.grid(row=1, column=0, padx=(15, 5), pady=(6, 12), sticky="w")
        
        self.ds_api_entry = ctk.CTkEntry(self.api_frame, placeholder_text="Wprowadź swój DeepSeek API Key...", show="*")
        self.ds_api_entry.grid(row=1, column=1, padx=10, pady=(6, 12), sticky="ew")
        
        self.show_api_var = ctk.BooleanVar(value=False)
        self.show_api_cb = ctk.CTkCheckBox(
            self.api_frame, 
            text="Pokaż klucze", 
            variable=self.show_api_var, 
            command=self._toggle_api_visibility,
            font=("Helvetica Neue", 11)
        )
        self.show_api_cb.grid(row=0, column=2, rowspan=2, padx=(5, 15), pady=12, sticky="w")
        
        # 2. Panel Agenta A - Lewa strona (Row 1, Column 0)
        self.agent_a_frame = ctk.CTkFrame(self)
        self.agent_a_frame.grid(row=1, column=0, padx=(15, 7), pady=5, sticky="nsew")
        self.agent_a_frame.grid_columnconfigure(1, weight=1)
        self.agent_a_frame.grid_rowconfigure(4, weight=1)
        self._setup_agent_panel(self.agent_a_frame, "🤖 AGENT A (Lewy)", "A")
        
        # 3. Panel Agenta B - Prawa strona (Row 1, Column 1)
        self.agent_b_frame = ctk.CTkFrame(self)
        self.agent_b_frame.grid(row=1, column=1, padx=(7, 15), pady=5, sticky="nsew")
        self.agent_b_frame.grid_columnconfigure(1, weight=1)
        self.agent_b_frame.grid_rowconfigure(4, weight=1)
        self._setup_agent_panel(self.agent_b_frame, "🤖 AGENT B (Prawy)", "B")
        
        # 4. Panel Kontrolny - Środek/Dół (Row 2)
        self.control_frame = ctk.CTkFrame(self)
        self.control_frame.grid(row=2, column=0, columnspan=2, padx=15, pady=5, sticky="ew")
        self.control_frame.grid_columnconfigure(0, weight=1)
        self.control_frame.grid_columnconfigure(1, weight=1)
        self.control_frame.grid_columnconfigure(2, weight=1)
        
        self.topic_label = ctk.CTkLabel(self.control_frame, text="Temat początkowy (Initial Prompt):", font=("Helvetica Neue", 12, "bold"))
        self.topic_label.grid(row=0, column=0, columnspan=2, padx=15, pady=(10, 2), sticky="w")
        
        self.btn_edit_topic = ctk.CTkButton(
            self.control_frame, 
            text="Edytuj w oknie ↗", 
            width=95, 
            height=20, 
            font=("Helvetica Neue", 10, "bold"),
            command=self._open_topic_editor
        )
        self.btn_edit_topic.grid(row=0, column=2, padx=(5, 15), pady=(10, 2), sticky="e")
        
        self.topic_text = ctk.CTkTextbox(self.control_frame, height=65, font=("Helvetica Neue", 12))
        self.topic_text.grid(row=1, column=0, columnspan=3, padx=15, pady=(0, 10), sticky="ew")
        self.topic_text.insert("1.0", DEFAULT_INITIAL_TOPIC)
        
        # Wybór folderu z kontekstem (Row 2)
        self.folder_frame = ctk.CTkFrame(self.control_frame, fg_color="transparent")
        self.folder_frame.grid(row=2, column=0, columnspan=3, padx=15, pady=5, sticky="ew")
        self.folder_frame.grid_columnconfigure(1, weight=1)
        
        self.btn_select_folder = ctk.CTkButton(
            self.folder_frame,
            text="📂 Wybierz folder z projektem/plikami",
            font=("Helvetica Neue", 12, "bold"),
            command=self._select_folder
        )
        self.btn_select_folder.grid(row=0, column=0, padx=(0, 10), pady=5, sticky="w")
        
        self.lbl_folder_path = ctk.CTkLabel(
            self.folder_frame,
            text="Brak wybranego folderu - debata ogólna",
            font=("Helvetica Neue", 11)
        )
        self.lbl_folder_path.grid(row=0, column=1, pady=5, sticky="w")
        
        # Przyciski sterujące debatą (Row 3)
        self.btn_start = ctk.CTkButton(
            self.control_frame, 
            text="URUCHOM DEBATĘ", 
            fg_color=COLOR_AGENT_B, 
            hover_color="#059669", 
            font=("Helvetica Neue", 13, "bold"),
            command=self._start_debate
        )
        self.btn_start.grid(row=3, column=0, padx=(15, 5), pady=(10, 5), sticky="ew")
        
        self.btn_stop = ctk.CTkButton(
            self.control_frame, 
            text="ZATRZYMAJ", 
            fg_color=COLOR_ERROR, 
            hover_color="#DC2626", 
            font=("Helvetica Neue", 13, "bold"),
            state="disabled",
            command=self._stop_debate
        )
        self.btn_stop.grid(row=3, column=1, padx=5, pady=(10, 5), sticky="ew")

        self.btn_new = ctk.CTkButton(
            self.control_frame, 
            text="NOWA DEBATA", 
            fg_color="#4B5563", # Gray
            hover_color="#374151", 
            font=("Helvetica Neue", 13, "bold"),
            command=self._new_debate
        )
        self.btn_new.grid(row=3, column=2, padx=(5, 15), pady=(10, 5), sticky="ew")

        # Przyciski zapisu (Row 4)
        self.btn_save_debate_txt = ctk.CTkButton(
            self.control_frame,
            text="ZAPISZ DEBATĘ (TXT)",
            fg_color="#10B981", # Emerald
            hover_color="#059669",
            font=("Helvetica Neue", 11, "bold"),
            command=self._save_debate_to_txt
        )
        self.btn_save_debate_txt.grid(row=4, column=0, columnspan=2, padx=(15, 5), pady=(5, 15), sticky="ew")

        self.show_logs_button = ctk.CTkButton(
            self.control_frame, 
            text="📋 OTWÓRZ OKNO LOGÓW", 
            fg_color="#4F46E5", # Indigo
            hover_color="#4338CA", 
            font=("Helvetica Neue", 11, "bold"),
            command=lambda: self.log_window.show_case()
        )
        self.show_logs_button.grid(row=4, column=2, padx=(5, 15), pady=(5, 15), sticky="ew")

        # Status Label (Row 5)
        self.status_label = ctk.CTkLabel(self.control_frame, text="Status: Gotowy do debaty.", font=("Helvetica Neue", 11, "italic"))
        self.status_label.grid(row=5, column=0, columnspan=3, padx=15, pady=(2, 2), sticky="w")

        # Kontrolki lektora (TTS) w panelu sterowania (Row 6)
        self.tts_control_frame = ctk.CTkFrame(self.control_frame, fg_color="transparent")
        self.tts_control_frame.grid(row=6, column=0, columnspan=3, padx=15, pady=(2, 8), sticky="w")

        self.tts_checkbox = ctk.CTkCheckBox(self.tts_control_frame, text="Włącz lektora", variable=self.tts_enabled, onvalue=True, offvalue=False)
        self.tts_checkbox.pack(side="left", padx=(0, 20))

        self.tts_speed_switch = ctk.CTkSwitch(self.tts_control_frame, text="Spowolnij mowę (gTTS Slow)", variable=self.tts_slow, onvalue=True, offvalue=False)
        self.tts_speed_switch.pack(side="left")

    def _setup_agent_panel(self, frame: ctk.CTkFrame, title: str, suffix: str):
        """
        Pomocnicza metoda budująca panel konfiguracyjny Agenta.
        """
        # Nagłówek panelu
        header_lbl = ctk.CTkLabel(frame, text=title, font=("Helvetica Neue", 13, "bold"))
        header_lbl.grid(row=0, column=0, columnspan=2, padx=15, pady=(12, 8), sticky="w")
        
        # Wybór modelu
        model_lbl = ctk.CTkLabel(frame, text="Model:", font=("Helvetica Neue", 12))
        model_lbl.grid(row=1, column=0, padx=(15, 5), pady=6, sticky="w")
        
        model_dropdown = ctk.CTkOptionMenu(
            frame, 
            values=AVAILABLE_MODELS,
            command=lambda _: self._update_ui_state()
        )
        model_dropdown.set("llama3.1")
        model_dropdown.grid(row=1, column=1, padx=(5, 15), pady=6, sticky="ew")
        setattr(self, f"model_{suffix}", model_dropdown)
        
        # Wybór roli ("Gema")
        role_lbl = ctk.CTkLabel(frame, text="Rola:", font=("Helvetica Neue", 12))
        role_lbl.grid(row=2, column=0, padx=(15, 5), pady=6, sticky="w")
        
        role_subframe = ctk.CTkFrame(frame, fg_color="transparent")
        role_subframe.grid(row=2, column=1, padx=(5, 15), pady=6, sticky="ew")
        role_subframe.grid_columnconfigure(0, weight=1)
        
        role_dropdown = ctk.CTkOptionMenu(
            role_subframe, 
            values=list(ROLE_PRESETS.keys()),
            command=lambda val: self._on_role_change(val, suffix)
        )
        role_dropdown.grid(row=0, column=0, sticky="ew")
        setattr(self, f"role_{suffix}", role_dropdown)
        
        manage_roles_btn = ctk.CTkButton(
            role_subframe,
            text="⚙️",
            width=32,
            height=28,
            font=("Helvetica Neue", 14),
            command=self._open_role_manager
        )
        manage_roles_btn.grid(row=0, column=1, padx=(5, 0), sticky="e")
        setattr(self, f"manage_roles_btn_{suffix}", manage_roles_btn)
        
        # System Prompt
        prompt_lbl = ctk.CTkLabel(frame, text="System Prompt (Instrukcja):", font=("Helvetica Neue", 12))
        prompt_lbl.grid(row=3, column=0, padx=15, pady=(10, 2), sticky="w")
        
        edit_btn = ctk.CTkButton(
            frame, 
            text="Edytuj w oknie ↗", 
            width=95, 
            height=20, 
            font=("Helvetica Neue", 10, "bold"),
            command=lambda s=suffix: self._open_prompt_editor(s)
        )
        edit_btn.grid(row=3, column=1, padx=(5, 15), pady=(10, 2), sticky="e")
        setattr(self, f"edit_btn_{suffix}", edit_btn)
        
        prompt_text = ctk.CTkTextbox(frame, height=120, font=("Helvetica Neue", 11), wrap="word")
        prompt_text.grid(row=4, column=0, columnspan=2, padx=15, pady=(0, 12), sticky="nsew")
        setattr(self, f"prompt_{suffix}", prompt_text)
        
        # Domyślny startowy stan dropdownów i textboxów
        if suffix == "A":
            role_dropdown.set("Ekspert/Krytyk")
        else:
            role_dropdown.set("Programista")
            
        self._on_role_change(role_dropdown.get(), suffix)

    def _open_prompt_editor(self, suffix: str):
        """
        Otwiera nowe okno modalne z dużym edytorem tekstu dla System Promptu.
        """
        editor_window = ctk.CTkToplevel(self)
        editor_window.title(f"Edytor System Prompt - Agent {suffix}")
        editor_window.geometry("700x500")
        editor_window.minsize(500, 350)
        
        # Centrowanie okna względem okna głównego
        editor_window.transient(self)
        editor_window.grab_set()
        
        editor_window.grid_columnconfigure(0, weight=1)
        editor_window.grid_rowconfigure(1, weight=1)
        
        lbl = ctk.CTkLabel(
            editor_window, 
            text=f"Edycja System Promptu dla Agenta {suffix} (Zapisanie przełączy rolę na 'Własny'):", 
            font=("Helvetica Neue", 12, "bold")
        )
        lbl.grid(row=0, column=0, padx=20, pady=(15, 5), sticky="w")
        
        txt_editor = ctk.CTkTextbox(editor_window, font=("Helvetica Neue", 12), wrap="word")
        txt_editor.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        
        # Wpisanie aktualnego promptu do edytora
        current_prompt = getattr(self, f"prompt_{suffix}").get("1.0", "end-1c")
        txt_editor.insert("1.0", current_prompt)
        txt_editor.focus()
        
        # Przyciski dolne
        btn_frame = ctk.CTkFrame(editor_window, fg_color="transparent")
        btn_frame.grid(row=2, column=0, padx=20, pady=(5, 15), sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)
        
        def save():
            new_text = txt_editor.get("1.0", "end-1c").strip()
            
            # Przełączenie roli na "Własny"
            role_dropdown = getattr(self, f"role_{suffix}")
            role_dropdown.set("Własny")
            
            prompt_text = getattr(self, f"prompt_{suffix}")
            prompt_text.configure(state="normal")
            prompt_text.delete("1.0", "end")
            prompt_text.insert("1.0", new_text)
            
            editor_window.destroy()
            
        btn_save = ctk.CTkButton(
            btn_frame, 
            text="ZAPISZ", 
            fg_color=COLOR_AGENT_B, 
            hover_color="#059669", 
            font=("Helvetica Neue", 12, "bold"),
            command=save
        )
        btn_save.grid(row=0, column=0, padx=(0, 10), pady=10, sticky="ew")
        
        btn_cancel = ctk.CTkButton(
            btn_frame, 
            text="ANULUJ", 
            fg_color=COLOR_ERROR, 
            hover_color="#DC2626", 
            font=("Helvetica Neue", 12, "bold"),
            command=editor_window.destroy
        )
        btn_cancel.grid(row=0, column=1, padx=(10, 0), pady=10, sticky="ew")

    def _open_topic_editor(self):
        """
        Otwiera nowe okno modalne z dużym edytorem tekstu dla Tematu Początkowego.
        """
        editor_window = ctk.CTkToplevel(self)
        editor_window.title("Edytor Tematu Początkowego")
        editor_window.geometry("700x400")
        editor_window.minsize(500, 300)
        
        # Centrowanie okna modalnego względem okna głównego
        editor_window.transient(self)
        editor_window.grab_set()
        
        editor_window.grid_columnconfigure(0, weight=1)
        editor_window.grid_rowconfigure(1, weight=1)
        
        lbl = ctk.CTkLabel(
            editor_window, 
            text="Edycja Tematu Początkowego (Initial Prompt):", 
            font=("Helvetica Neue", 12, "bold")
        )
        lbl.grid(row=0, column=0, padx=20, pady=(15, 5), sticky="w")
        
        txt_editor = ctk.CTkTextbox(editor_window, font=("Helvetica Neue", 12), wrap="word")
        txt_editor.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        
        # Wczytanie obecnego tekstu z pola głównego
        current_text = self.topic_text.get("1.0", "end-1c")
        txt_editor.insert("1.0", current_text)
        txt_editor.focus()
        
        # Przyciski dolne
        btn_frame = ctk.CTkFrame(editor_window, fg_color="transparent")
        btn_frame.grid(row=2, column=0, padx=20, pady=(5, 15), sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)
        
        def save():
            new_text = txt_editor.get("1.0", "end-1c").strip()
            self.topic_text.configure(state="normal")
            self.topic_text.delete("1.0", "end")
            self.topic_text.insert("1.0", new_text)
            editor_window.destroy()
            
        btn_save = ctk.CTkButton(
            btn_frame, 
            text="ZAPISZ", 
            fg_color=COLOR_AGENT_B, 
            hover_color="#059669", 
            font=("Helvetica Neue", 12, "bold"),
            command=save
        )
        btn_save.grid(row=0, column=0, padx=(0, 10), pady=10, sticky="ew")
        
        btn_cancel = ctk.CTkButton(
            btn_frame, 
            text="ANULUJ", 
            fg_color=COLOR_ERROR, 
            hover_color="#DC2626", 
            font=("Helvetica Neue", 12, "bold"),
            command=editor_window.destroy
        )
        btn_cancel.grid(row=0, column=1, padx=(10, 0), pady=10, sticky="ew")

    def _open_role_manager(self):
        """
        Otwiera modalne okno zarządzania listą ról.
        """
        manager_window = ctk.CTkToplevel(self)
        manager_window.title("Zarządzanie listą ról")
        manager_window.geometry("550x450")
        manager_window.minsize(500, 400)
        
        manager_window.transient(self)
        manager_window.grab_set()
        
        manager_window.grid_columnconfigure(0, weight=1)
        manager_window.grid_rowconfigure(2, weight=1)
        
        # 1. Wybór roli do edycji
        top_frame = ctk.CTkFrame(manager_window, fg_color="transparent")
        top_frame.grid(row=0, column=0, padx=20, pady=(15, 5), sticky="ew")
        top_frame.grid_columnconfigure(1, weight=1)
        
        lbl_select = ctk.CTkLabel(top_frame, text="Wybierz rolę do edycji:", font=("Helvetica Neue", 12, "bold"))
        lbl_select.grid(row=0, column=0, padx=(0, 10), pady=5, sticky="w")
        
        selected_role_var = ctk.StringVar(value="Ekspert/Krytyk")
        
        role_selector = ctk.CTkOptionMenu(
            top_frame,
            values=list(ROLE_PRESETS.keys()),
            variable=selected_role_var,
            command=lambda val: on_select_role(val)
        )
        role_selector.grid(row=0, column=1, pady=5, sticky="ew")
        
        # Przycisk tworzenia nowej roli
        btn_new_role = ctk.CTkButton(
            top_frame,
            text="+ Nowa rola",
            width=90,
            command=lambda: prepare_new_role()
        )
        btn_new_role.grid(row=0, column=2, padx=(10, 0), pady=5, sticky="e")
        
        # 2. Nazwa roli
        name_frame = ctk.CTkFrame(manager_window, fg_color="transparent")
        name_frame.grid(row=1, column=0, padx=20, pady=5, sticky="ew")
        name_frame.grid_columnconfigure(1, weight=1)
        
        lbl_name = ctk.CTkLabel(name_frame, text="Nazwa roli:", font=("Helvetica Neue", 12))
        lbl_name.grid(row=0, column=0, padx=(0, 10), pady=5, sticky="w")
        
        name_entry = ctk.CTkEntry(name_frame, font=("Helvetica Neue", 12))
        name_entry.grid(row=0, column=1, pady=5, sticky="ew")
        
        # 3. System prompt roli
        prompt_frame = ctk.CTkFrame(manager_window, fg_color="transparent")
        prompt_frame.grid(row=2, column=0, padx=20, pady=5, sticky="nsew")
        prompt_frame.grid_columnconfigure(0, weight=1)
        prompt_frame.grid_rowconfigure(1, weight=1)
        
        lbl_prompt = ctk.CTkLabel(prompt_frame, text="System Prompt (Instrukcja):", font=("Helvetica Neue", 12))
        lbl_prompt.grid(row=0, column=0, pady=(5, 2), sticky="w")
        
        prompt_editor = ctk.CTkTextbox(prompt_frame, font=("Helvetica Neue", 11), wrap="word")
        prompt_editor.grid(row=1, column=0, pady=2, sticky="nsew")
        
        # 4. Przyciski dolne
        btn_frame = ctk.CTkFrame(manager_window, fg_color="transparent")
        btn_frame.grid(row=3, column=0, padx=20, pady=(10, 15), sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)
        btn_frame.grid_columnconfigure(2, weight=1)
        
        def on_select_role(role_name):
            name_entry.configure(state="normal")
            name_entry.delete(0, "end")
            name_entry.insert(0, role_name)
            
            prompt_editor.delete("1.0", "end")
            prompt_editor.insert("1.0", ROLE_PRESETS.get(role_name, ""))
            
            # Blokowanie ról systemowych przed usuwaniem i zmianą nazwy
            if role_name in ["Ekspert/Krytyk", "Programista", "Własny"]:
                name_entry.configure(state="disabled")
                btn_delete.configure(state="disabled")
            else:
                name_entry.configure(state="normal")
                btn_delete.configure(state="normal")
                
        def prepare_new_role():
            name_entry.configure(state="normal")
            name_entry.delete(0, "end")
            name_entry.insert(0, "Własna Rola")
            prompt_editor.delete("1.0", "end")
            prompt_editor.insert("1.0", "Twoje imię to... ")
            btn_delete.configure(state="disabled")
            
        def save():
            new_name = name_entry.get().strip()
            new_prompt = prompt_editor.get("1.0", "end-1c").strip()
            
            if not new_name:
                messagebox.showerror("Błąd", "Nazwa roli nie może być pusta!", parent=manager_window)
                return
                
            selected_old = selected_role_var.get()
            
            # Walidacja przed nadpisaniem wbudowanej roli w sposób nieautoryzowany
            if selected_old in ["Ekspert/Krytyk", "Programista", "Własny"] and new_name != selected_old:
                messagebox.showerror("Błąd", "Nie możesz zmienić nazwy roli wbudowanej!", parent=manager_window)
                return
            
            import config
            presets = config.load_role_presets()
            
            # Jeśli zmieniono nazwę własnej roli, usuń stary klucz
            if selected_old not in ["Ekspert/Krytyk", "Programista", "Własny"] and new_name != selected_old:
                presets.pop(selected_old, None)
                
            presets[new_name] = new_prompt
            config.save_role_presets(presets)
            
            # Zaktualizuj dropdowny i globalny stan w GUI
            self._update_role_dropdowns()
            
            # Odśwież menu wyboru w oknie menedżera
            role_selector.configure(values=list(ROLE_PRESETS.keys()))
            selected_role_var.set(new_name)
            on_select_role(new_name)
            
            messagebox.showinfo("Sukces", f"Pomyślnie zapisano rolę '{new_name}'!", parent=manager_window)
            
        def delete():
            role_to_del = selected_role_var.get()
            if role_to_del in ["Ekspert/Krytyk", "Programista", "Własny"]:
                messagebox.showerror("Błąd", "Nie można usunąć roli wbudowanej!", parent=manager_window)
                return
                
            if not messagebox.askyesno("Potwierdzenie", f"Czy na pewno chcesz usunąć rolę '{role_to_del}'?", parent=manager_window):
                return
                
            import config
            presets = config.load_role_presets()
            presets.pop(role_to_del, None)
            config.save_role_presets(presets)
            
            self._update_role_dropdowns()
            
            role_selector.configure(values=list(ROLE_PRESETS.keys()))
            selected_role_var.set("Ekspert/Krytyk")
            on_select_role("Ekspert/Krytyk")
            
            messagebox.showinfo("Sukces", f"Pomyślnie usunięto rolę '{role_to_del}'!", parent=manager_window)

        btn_save = ctk.CTkButton(
            btn_frame, 
            text="ZAPISZ", 
            fg_color=COLOR_AGENT_B, 
            hover_color="#059669", 
            font=("Helvetica Neue", 12, "bold"),
            command=save
        )
        btn_save.grid(row=0, column=0, padx=(0, 5), pady=10, sticky="ew")
        
        btn_delete = ctk.CTkButton(
            btn_frame, 
            text="USUŃ ROLĘ", 
            fg_color=COLOR_ERROR, 
            hover_color="#DC2626", 
            font=("Helvetica Neue", 12, "bold"),
            command=delete
        )
        btn_delete.grid(row=0, column=1, padx=5, pady=10, sticky="ew")
        
        btn_cancel = ctk.CTkButton(
            btn_frame, 
            text="ZAMKNIJ", 
            fg_color="#6B7280", 
            hover_color="#4B5563", 
            font=("Helvetica Neue", 12, "bold"),
            command=manager_window.destroy
        )
        btn_cancel.grid(row=0, column=2, padx=(5, 0), pady=10, sticky="ew")
        
        # Uruchomienie inicjalnego załadowania
        on_select_role("Ekspert/Krytyk")

    def _update_role_dropdowns(self):
        """
        Aktualizuje listę ról w głównym oknie oraz synchronizuje stan dropdownów.
        """
        import config
        updated = config.load_role_presets()
        ROLE_PRESETS.clear()
        ROLE_PRESETS.update(updated)
        
        val_a = self.role_A.get()
        val_b = self.role_B.get()
        
        roles_list = list(ROLE_PRESETS.keys())
        
        self.role_A.configure(values=roles_list)
        self.role_B.configure(values=roles_list)
        
        if val_a in ROLE_PRESETS:
            self.role_A.set(val_a)
        else:
            self.role_A.set("Ekspert/Krytyk")
            self._on_role_change("Ekspert/Krytyk", "A")
            
        if val_b in ROLE_PRESETS:
            self.role_B.set(val_b)
        else:
            self.role_B.set("Programista")
            self._on_role_change("Programista", "B")

    def _on_role_change(self, selected_role: str, suffix: str):
        prompt_text = getattr(self, f"prompt_{suffix}")
        prompt_text.configure(state="normal")
        prompt_text.delete("1.0", "end")
        
        preset_prompt = ROLE_PRESETS.get(selected_role, "")
        prompt_text.insert("1.0", preset_prompt)
        
        if selected_role == "Własny":
            prompt_text.configure(state="normal")
        else:
            prompt_text.configure(state="disabled")

    def _toggle_api_visibility(self):
        show_char = "" if self.show_api_var.get() else "*"
        self.api_entry.configure(show=show_char)
        if hasattr(self, "ds_api_entry"):
            self.ds_api_entry.configure(show=show_char)

    def _start_debate(self):
        model_a = self.model_A.get()
        model_b = self.model_B.get()
        
        # Pobieramy klucz Gemini z GUI, a jeśli pusty to z RAM lub środowiska
        api_key = self.api_entry.get().strip()
        if not api_key:
            api_key = self.gemini_api_key
        if not api_key:
            api_key = os.getenv("GEMINI_API_KEY", "")
            
        if api_key:
            api_key = api_key.strip().strip('"').strip("'")
            
        self.gemini_api_key = api_key

        # Pobieramy klucz DeepSeek z GUI, a jeśli pusty to z RAM lub środowiska
        ds_api_key = self.ds_api_entry.get().strip()
        if not ds_api_key:
            ds_api_key = self.deepseek_api_key
        if not ds_api_key:
            ds_api_key = os.getenv("DEEPSEEK_API_KEY", "")
            
        if ds_api_key:
            ds_api_key = ds_api_key.strip().strip('"').strip("'")
            
        self.deepseek_api_key = ds_api_key
        
        # Zapisujemy w os.environ, aby był dostępny dla debate_manager
        if self.deepseek_api_key:
            os.environ["DEEPSEEK_API_KEY"] = self.deepseek_api_key
        else:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        
        # Walidacja klucza dla modeli chmurowych Gemini i DeepSeek
        if model_a.startswith("gemini-") or model_b.startswith("gemini-"):
            if not api_key:
                messagebox.showerror(
                    "Błąd konfiguracji", 
                    "Wybrałeś model chmurowy Gemini, ale pole Gemini API Key jest puste! Wprowadź klucz API, aby kontynuować."
                )
                return

        if "deepseek" in model_a.lower() or "deepseek" in model_b.lower():
            if not self.deepseek_api_key or self.deepseek_api_key.strip() in ("", "your_deepseek_api_key_here"):
                messagebox.showerror(
                    "Błąd konfiguracji",
                    "Wybrałeś model chmurowy DeepSeek, ale klucz DEEPSEEK_API_KEY nie został podany w GUI ani w pliku .env!"
                )
                return
            
        topic = self.topic_text.get("1.0", "end-1c").strip()
        if not topic:
            messagebox.showerror("Błąd konfiguracji", "Proszę wprowadzić temat początkowy debaty!")
            return
            
        system_a = self.prompt_A.get("1.0", "end-1c").strip()
        system_b = self.prompt_B.get("1.0", "end-1c").strip()
        
        if not system_a or not system_b:
            messagebox.showerror("Błąd konfiguracji", "System Prompt nie może być pusty!")
            return
            
        # Odczyt opóźnienia
        delay = 3.0
            
        # Odczyt docelowej liczby rund
        rounds_val = self.log_window.rounds_dropdown.get()
        if rounds_val == "Bez limitu":
            max_rounds = None
        else:
            try:
                max_rounds = int(rounds_val)
            except ValueError:
                max_rounds = None
                
        if not getattr(self, "has_history", False):
            self.debate_manager.reset()
            self.added_prompts_list = []
            
        # Skanowanie folderu i wstrzykiwanie kontekstu jako prefix na początek instrukcji systemowych obu agentów
        context_text = ""
        if self.context_folder_path:
            self.append_log("system", "Skanowanie wybranego folderu projektu...")
            context_text = DebateManager._load_folder_context(self.context_folder_path, self.message_queue)
            
        if context_text:
            system_a = f"KONTEKST Z LOKALNEGO PROJEKTU UŻYTKOWNIKA:\n{context_text}\n\n{system_a}\n\nANALIZUJESZ KOD PROJEKTU. Skup się wyłącznie na znajdowaniu błędów w tym kodzie i pisaniu refaktoryzacji."
            system_b = f"KONTEKST Z LOKALNEGO PROJEKTU UŻYTKOWNIKA:\n{context_text}\n\n{system_b}\n\nANALIZUJESZ KOD PROJEKTU. Skup się wyłącznie na znajdowaniu błędów w tym kodzie i pisaniu refaktoryzacji."
        else:
            system_a = f"{system_a}\n\nPROWADZISZ LUŹNĄ DEBATĘ PUBLICYSTYCZNĄ. Masz całkowity zakaz pisania, generowania lub wymyślania jakichkolwiek sztucznych bloków kodu źródłowego. Skup się wyłącznie na argumentacji słownej."
            system_b = f"{system_b}\n\nPROWADZISZ LUŹNĄ DEBATĘ PUBLICYSTYCZNĄ. Masz całkowity zakaz pisania, generowania lub wymyślania jakichkolwiek sztucznych bloków kodu źródłowego. Skup się wyłącznie na argumentacji słownej."
            
        self._set_ui_state("running")
        
        # Czyścimy logi tylko gdy zaczynamy całkiem nową debatę
        if not getattr(self, "has_history", False):
            self.log_window.debate_log.configure(state="normal")
            self.log_window.debate_log.delete("1.0", "end")
            self.log_window.debate_log.configure(state="disabled")
            
        self.is_running = True
        self.has_history = True
        self.log_window.show_case()  # Automatyczne pokazanie okna po kliknięciu 'URUCHOM DEBATĘ'
        self.debate_manager.start_debate(
            api_key=self.gemini_api_key, model_a=model_a, system_a=system_a,
            model_b=model_b, system_b=system_b, initial_topic=topic,
            delay=delay, max_rounds=max_rounds
        )

    def _stop_debate(self):
        self.append_log("system", "Debata została natychmiast zatrzymana przez użytkownika. Trwa wygaszanie procesów w tle...")
        self.debate_manager.stop_debate()
        self.is_running = False
        self._set_ui_state("idle")

    def _set_ui_state(self, state: str):
        if state == "running":
            self.api_entry.configure(state="disabled")
            self.show_api_cb.configure(state="disabled")
            self.model_A.configure(state="disabled")
            self.role_A.configure(state="disabled")
            self.prompt_A.configure(state="disabled")
            self.model_B.configure(state="disabled")
            self.role_B.configure(state="disabled")
            self.prompt_B.configure(state="disabled")
            self.topic_text.configure(state="disabled")
            self.edit_btn_A.configure(state="disabled")
            self.edit_btn_B.configure(state="disabled")
            self.manage_roles_btn_A.configure(state="disabled")
            self.manage_roles_btn_B.configure(state="disabled")
            self.btn_edit_topic.configure(state="disabled")
            self.log_window.rounds_dropdown.configure(state="disabled")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.btn_new.configure(state="disabled")
            self.btn_save_debate_txt.configure(state="disabled")
            self.btn_select_folder.configure(state="disabled")
        else:
            self.api_entry.configure(state="normal")
            self.show_api_cb.configure(state="normal")
            self.model_A.configure(state="normal")
            self.role_A.configure(state="normal")
            if self.role_A.get() == "Własny":
                self.prompt_A.configure(state="normal")
            self.model_B.configure(state="normal")
            self.role_B.configure(state="normal")
            if self.role_B.get() == "Własny":
                self.prompt_B.configure(state="normal")
            self.topic_text.configure(state="normal")
            self.edit_btn_A.configure(state="normal")
            self.edit_btn_B.configure(state="normal")
            self.manage_roles_btn_A.configure(state="normal")
            self.manage_roles_btn_B.configure(state="normal")
            self.btn_edit_topic.configure(state="normal")
            self.log_window.rounds_dropdown.configure(state="normal")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.btn_new.configure(state="normal")
            self.btn_save_debate_txt.configure(state="normal")
            self.btn_select_folder.configure(state="normal")
        self._update_ui_state()

    def _update_ui_state(self):
        if not self.debate_manager:
            self.btn_start.configure(state="disabled")
            self.update_status("Błąd: Menedżer debaty niedostępny.")
            return

        selected_model_a = self.model_A.get()
        selected_model_b = self.model_B.get()

        is_ollama_needed = "llama" in selected_model_a.lower() or "llama" in selected_model_b.lower()
        is_gemini_needed = "gemini" in selected_model_a.lower() or "gemini" in selected_model_b.lower()
        is_deepseek_needed = "deepseek" in selected_model_a.lower() or "deepseek" in selected_model_b.lower()

        can_start = True
        status_parts = []

        if is_ollama_needed and not self.debate_manager.ollama_client:
            can_start = False
            status_parts.append("Ollama offline")

        raw_env_key = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")
        raw_entry_key = self.api_entry.get().strip().strip('"').strip("'")
        gemini_api_key_available = bool(raw_env_key) or bool(raw_entry_key)
        if is_gemini_needed and not gemini_api_key_available:
            can_start = False
            status_parts.append("Brak klucza Gemini")

        if is_deepseek_needed:
            raw_ds_env_key = os.getenv("DEEPSEEK_API_KEY", "").strip().strip('"').strip("'")
            raw_ds_entry_key = self.ds_api_entry.get().strip().strip('"').strip("'")
            ds_api_key_available = (raw_ds_env_key and raw_ds_env_key != "your_deepseek_api_key_here") or (raw_ds_entry_key and raw_ds_entry_key != "your_deepseek_api_key_here")
            if not ds_api_key_available:
                can_start = False
                status_parts.append("Brak klucza DeepSeek")

        self.btn_start.configure(state="normal" if can_start and not self.debate_manager.running else "disabled")

        if status_parts:
            self.update_status("Błąd: " + ", ".join(status_parts))
        elif self.debate_manager.running:
            self.update_status("Status: Debata w toku...")
        else:
            self.update_status("Status: Gotowy do debaty.")

    def update_status(self, text: str):
        if hasattr(self, "status_label"):
            self.status_label.configure(text=text)
            if "Błąd" in text or "offline" in text or "Brak" in text:
                self.status_label.configure(text_color="#EF4444")
            elif "Gotowy" in text:
                self.status_label.configure(text_color="#10B981")
            else:
                self.status_label.configure(text_color="#F59E0B")

    def _periodic_ui_update(self):
        self._update_ui_state()
        self.after(2000, self._periodic_ui_update)

    def append_log(self, log_type: str, text: str, round_num: int = 1):
        """
        Dopisuje wpis do pola logów w bezpieczny sposób.
        """
        # Sprawdzamy autoscroll
        try:
            was_at_bottom = self.log_window.debate_log._textbox.yview()[1] >= 0.95
        except AttributeError:
            was_at_bottom = True

        self.log_window.debate_log.configure(state="normal")
        try:
            if log_type == "agent_a":
                header = f"\n🤖 AGENT A (Runda {round_num}):\n"
                self.log_window.debate_log._textbox.insert("end", header, "agent_a")
                self.log_window.debate_log._textbox.insert("end", f"{text}\n", "normal")
            elif log_type == "agent_b":
                header = f"\n🤖 AGENT B (Runda {round_num}):\n"
                self.log_window.debate_log._textbox.insert("end", header, "agent_b")
                self.log_window.debate_log._textbox.insert("end", f"{text}\n", "normal")
            elif log_type == "system":
                self.log_window.debate_log._textbox.insert("end", f"ℹ️ [SYSTEM] {text}\n", "system")
            elif log_type == "error":
                self.log_window.debate_log._textbox.insert("end", f"❌ [BŁĄD] {text}\n", "error")
        except AttributeError:
            self.log_window.debate_log.insert("end", f"\n[{log_type.upper()}] {text}\n")
            
        # Przewijamy na dół tylko jeśli autoscroll jest aktywny oraz użytkownik był na samym dole
        if self.log_window.autoscroll_var.get() and was_at_bottom:
            self.log_window.debate_log.see("end")
            
        self.log_window.debate_log.configure(state="disabled")

    def _check_queue(self):
        try:
            while True:
                msg = self.message_queue.get_nowait()
                msg_type = msg.get("type")
                
                if msg_type == "finished":
                    self.is_running = False
                    self._set_ui_state("idle")
                elif msg_type in ["agent_a", "agent_b"]:
                    self.append_log(msg_type, msg.get("text"), round_num=msg.get("round"))
                    self._speak_agent_message(msg.get("text"))
                elif msg_type in ["system", "error"]:
                    self.append_log(msg_type, msg.get("text"))
                elif msg_type == "tts_error":
                    self.append_log("error", f"[LEKTOR] {msg.get('content')}")
                    if sys.platform == "darwin":
                        self.append_log("system", "Wykryto problem z połączeniem/limitem gTTS. Lektor automatycznie przełącza się na lokalny syntezator mowy macOS.")
                    
                self.message_queue.task_done()
        except queue.Empty:
            pass
        finally:
            self.after(100, self._check_queue)

    def _new_debate(self):
        """
        Resetuje historię debaty w GUI oraz w DebateManager, przygotowując do nowego tematu.
        """
        if self.is_running:
            messagebox.showwarning("Debata trwa", "Proszę zatrzymać debatę przed rozpoczęciem nowej!")
            return
            
        if messagebox.askyesno("Nowa debata", "Czy chcesz wyczyścić historię i rozpocząć nową debatę?"):
            self.debate_manager.reset()
            self.added_prompts_list = []
            self.has_history = False
            self.context_folder_path = ""
            self.lbl_folder_path.configure(text="Brak wybranego folderu - debata ogólna")
            
            # Czyszczenie logów
            self.log_window.debate_log.configure(state="normal")
            self.log_window.debate_log.delete("1.0", "end")
            self.log_window.debate_log.configure(state="disabled")
            
            self.append_log("system", "Rozpoczęto nową sesję debaty. Wpisz temat początkowy i kliknij Uruchom.")

    def _select_folder(self):
        """
        Otwiera dialog wyboru katalogu i aktualizuje ścieżkę kontekstową.
        """
        from tkinter import filedialog
        folder = filedialog.askdirectory()
        if folder:
            self.context_folder_path = folder
            self.lbl_folder_path.configure(text=folder)



    def _generate_summary_text(self, api_key, topic, history_a, model_name):
        """
        Wywołuje lokalną Ollamę lub Gemini API w celu wygenerowania zwięzłego podsumowania debaty.
        """
        try:
            lines = []
            for i, msg in enumerate(history_a):
                role = msg.get("role", "")
                text = msg.get("content", "")
                if i == 0:
                    lines.append(f"Temat: {text}")
                elif role == "assistant":
                    lines.append(f"Agent A: {text[:300]}")
                elif role == "user" and i > 1:
                    lines.append(f"Agent B: {text[:300]}")
            
            conversation_text = "\n".join(lines)
            prompt = (
                "Jesteś obserwatorem debaty między dwoma inteligentnymi agentami AI.\n"
                f"Temat początkowy debaty: {topic}\n\n"
                "Oto przebieg debaty:\n"
                f"{conversation_text}\n\n"
                "Napisz zwięzłe podsumowanie tej debaty (maksymalnie 4 zdania) w języku polskim. "
                "Wskaż główne poruszane kwestie oraz osiągnięty postęp w dyskusji."
            )
            
            # Jeśli brak klucza API lub wybrano model lokalny, generujemy podsumowanie przez Ollamę
            clean_api_key = api_key.strip().strip('"').strip("'") if api_key else ""
            if "deepseek" in model_name.lower():
                from openai import OpenAI
                deepseek_key = os.getenv("DEEPSEEK_API_KEY")
                if not deepseek_key:
                    return "Błąd: Brak klucza DEEPSEEK_API_KEY dla podsumowania DeepSeek."
                client = OpenAI(base_url="https://api.deepseek.com/v1", api_key=deepseek_key)
                response = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7
                )
                return response.choices[0].message.content.strip()
            elif not clean_api_key or "llama" in model_name.lower():
                from openai import OpenAI
                client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
                response = client.chat.completions.create(
                    model=model_name if "llama" in model_name.lower() else "llama3.1",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7
                )
                return response.choices[0].message.content.strip()
            else:
                from google import genai
                import os
                # Tymczasowo usuwamy GOOGLE_APPLICATION_CREDENTIALS dla zapobieżenia konfliktom
                old_adc = os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
                try:
                    client = genai.Client(api_key=clean_api_key)
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt
                    )
                    return response.text.strip()
                finally:
                    if old_adc:
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = old_adc
        except Exception as e:
            return f"Błąd podczas automatycznego generowania podsumowania: {str(e)}"

    def _save_debate_to_txt(self):
        """
        Zapisuje pełny tekst debaty z okna logów do pliku .txt w folderze programu.
        """
        try:
            logs_text = self.log_window.debate_log.get("1.0", "end-1c").strip()
            if not logs_text:
                messagebox.showwarning("Pusta debata", "Brak treści debaty do zapisania!")
                return
                
            # Folder programu
            app_dir = os.path.dirname(os.path.abspath(__file__))
            
            # Tworzymy nazwę pliku z aktualnym znacznikiem czasu
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            file_name = f"debata_{timestamp}.txt"
            file_path = os.path.join(app_dir, file_name)
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(logs_text)
                
            self.append_log("system", f"Zapisano tekst debaty do pliku: {file_name}")
            messagebox.showinfo("Zapisano debatę", f"Pomyślnie zapisano tekst debaty w folderze programu pod nazwą:\n{file_name}")
        except Exception as e:
            messagebox.showerror("Błąd zapisu", f"Nie udało się zapisać pliku: {str(e)}")

    def _save_context(self, auto=False):
        """
        Zapisuje stan debaty do pliku tekstowego (.txt) z ukrytą sekcją JSON.
        """
        api_key = self.gemini_api_key
        model_a = self.model_A.get()
        role_a = self.role_A.get()
        prompt_a = self.prompt_A.get("1.0", "end-1c").strip()
        model_b = self.model_B.get()
        role_b = self.role_B.get()
        prompt_b = self.prompt_B.get("1.0", "end-1c").strip()
        topic = self.topic_text.get("1.0", "end-1c").strip()
        
        delay_val = "3s (zalecane)"
        debate_state = self.debate_manager.get_state()
        history_a = debate_state.get("history_a_serialized", [])
        
        if auto:
            file_path = DEFAULT_CONTEXT_FILE
        else:
            from tkinter import filedialog
            file_path = filedialog.asksaveasfilename(
                defaultextension=".txt",
                filetypes=[("Pliki tekstowe", "*.txt"), ("Wszystkie pliki", "*.*")],
                initialfile="kontekst_debaty.txt",
                title="Zapisz kontekst debaty"
            )
            if not file_path:
                return
                
        summary = "Brak dotychczasowej historii debaty."
        if len(history_a) > 0:
            if not auto:
                self.append_log("system", "Generowanie automatycznego podsumowania debaty za pomocą AI...")
            summary = self._generate_summary_text(api_key, topic, history_a, model_a)
            
        # Zapisujemy stan BEZ api_key (bezpieczeństwo, klucz tylko w RAM)
        state_data = {
            "model_a": model_a,
            "role_a": role_a,
            "prompt_a": prompt_a,
            "model_b": model_b,
            "role_b": role_b,
            "prompt_b": prompt_b,
            "topic": topic,
            "added_prompts": self.added_prompts_list,
            "delay_val": delay_val,
            "rounds_val": self.rounds_dropdown.get(),
            "context_folder_path": getattr(self, "context_folder_path", ""),
            "debate_state": debate_state
        }
        
        try:
            json_str = json.dumps(state_data, ensure_ascii=False)
            b64_str = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("========================================================================\n")
                f.write("             KONTEKST ROZMOWY - MULTI-AGENT AI DEBATE\n")
                f.write("========================================================================\n\n")
                f.write(f"TEMAT POCZĄTKOWY DEBATY:\n{topic}\n\n")
                
                if self.added_prompts_list:
                    f.write("DODANE PROMPTY / INTERWENCJE UŻYTKOWNIKA:\n")
                    for p in self.added_prompts_list:
                        f.write(f"- {p}\n")
                    f.write("\n")
                    
                f.write("------------------------------------------------------------------------\n")
                f.write(f"PODSUMOWANIE DEBATY (Generowane automatycznie przez AI):\n{summary}\n\n")
                
                f.write("------------------------------------------------------------------------\n")
                f.write("SKRÓCONY ZAPIS ROZMOWY:\n")
                
                for i, msg in enumerate(history_a):
                    role = msg.get("role", "")
                    text = msg.get("content", "")
                    if role == "system":
                        continue
                    elif i == 1:
                        continue  # pomijamy temat początkowy
                    elif role == "assistant":
                        f.write(f"🤖 AGENT A: {text[:150]}...\n\n")
                    elif role == "user":
                        f.write(f"🤖 AGENT B: {text[:150]}...\n\n")
                        
                f.write("========================================================================\n")
                f.write("!!! PONIŻSZE DANE SĄ UŻYWANE PRZEZ APLIKACJĘ DO PRZYWRÓCENIA STANU !!!\n")
                f.write("!!! NIE MODYFIKUJ PONIŻSZEJ SEKCJI !!!\n")
                f.write("=== DATA (JSON) ===\n")
                f.write(f"{b64_str}\n")
                f.write("========================================================================\n")
                
            if not auto:
                self.append_log("system", f"Pomyślnie zapisano kontekst debaty do pliku: {file_path}")
        except Exception as e:
            if not auto:
                messagebox.showerror("Błąd zapisu", f"Nie udało się zapisać kontekstu: {str(e)}")

    def _load_context(self):
        """
        Wczytuje stan debaty z wybranego pliku .txt i przywraca konfigurację oraz historię.
        """
        if self.is_running:
            messagebox.showwarning("Debata trwa", "Zatrzymaj debatę przed wczytaniem kontekstu!")
            return
            
        from tkinter import filedialog
        file_path = filedialog.askopenfilename(
            filetypes=[("Pliki tekstowe", "*.txt"), ("Wszystkie pliki", "*.*")],
            title="Wczytaj kontekst debaty"
        )
        if not file_path:
            return
            
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            if "=== DATA (JSON) ===" not in content:
                messagebox.showerror("Błąd formatu", "Wybrany plik nie zawiera sekcji danych maszynowych aplikacji!")
                return
                
            parts = content.split("=== DATA (JSON) ===")
            data_part = parts[1].split("===")[0].strip()
            
            # Dekodujemy Base64 z powrotem do stanu JSON
            json_bytes = base64.b64decode(data_part)
            state_data = json.loads(json_bytes.decode('utf-8'))
            
            # Odtwarzanie pól konfiguracyjnych w GUI (klucz API ładujemy wyłącznie z RAM / os.environ, nie z pliku)
            self.api_entry.delete(0, "end")
            if self.gemini_api_key:
                self.api_entry.insert(0, self.gemini_api_key)
            
            self.ds_api_entry.delete(0, "end")
            if self.deepseek_api_key:
                self.ds_api_entry.insert(0, self.deepseek_api_key)
            
            self.model_A.set(state_data.get("model_a", AVAILABLE_MODELS[0]))
            self.role_A.set(state_data.get("role_a", "Własny"))
            
            self.prompt_A.configure(state="normal")
            self.prompt_A.delete("1.0", "end")
            self.prompt_A.insert("1.0", state_data.get("prompt_a", ""))
            if state_data.get("role_a") != "Własny":
                self.prompt_A.configure(state="disabled")
                
            self.model_B.set(state_data.get("model_b", AVAILABLE_MODELS[0]))
            self.role_B.set(state_data.get("role_b", "Własny"))
            
            self.prompt_B.configure(state="normal")
            self.prompt_B.delete("1.0", "end")
            self.prompt_B.insert("1.0", state_data.get("prompt_b", ""))
            if state_data.get("role_b") != "Własny":
                self.prompt_B.configure(state="disabled")
                
            self.topic_text.configure(state="normal")
            self.topic_text.delete("1.0", "end")
            self.topic_text.insert("1.0", state_data.get("topic", ""))
            
            self.rounds_dropdown.set(state_data.get("rounds_val", "Bez limitu"))
            
            # Wczytywanie ścieżki folderu kontekstowego
            self.context_folder_path = state_data.get("context_folder_path", "")
            if self.context_folder_path:
                self.lbl_folder_path.configure(text=self.context_folder_path)
            else:
                self.lbl_folder_path.configure(text="Brak wybranego folderu - debata ogólna")
            
            # Wczytywanie listy interwencji promptowych
            self.added_prompts_list = state_data.get("added_prompts", [])
            
            # Przywrócenie stanu w DebateManager
            debate_state = state_data.get("debate_state", {})
            self.debate_manager.load_state(debate_state)
            
            # Odtworzenie logów w oknie logów
            self.log_window.debate_log.configure(state="normal")
            self.log_window.debate_log.delete("1.0", "end")
            self.log_window.debate_log.configure(state="disabled")
            
            self.append_log("system", f"Pomyślnie wczytano kontekst z pliku: {file_path}")
            
            # Wypisujemy w logach historyczny przebieg rozmowy w formacie słowników OpenAI
            history_a = debate_state.get("history_a_serialized", [])
            for i, msg in enumerate(history_a):
                role = msg.get("role", "")
                text = msg.get("content", "")
                if role == "system":
                    self.append_log("system", f"Załadowano instrukcje systemowe dla agentów.")
                elif i == 1:
                    self.append_log("system", f"Temat początkowy: {text}")
                elif role == "assistant":
                    round_num = i // 2
                    self.append_log("agent_a", text, round_num=round_num)
                elif role == "user" and i > 1:
                    round_num = (i - 1) // 2
                    self.append_log("agent_b", text, round_num=round_num)
                    
            self.has_history = True
            
        except Exception as e:
            messagebox.showerror("Błąd wczytywania", f"Nie udało się poprawnie wczytać pliku: {str(e)}")

    def _load_cache_index(self):
        with self._lock:
            if os.path.exists(TTS_CACHE_INDEX_FILE):
                try:
                    with open(TTS_CACHE_INDEX_FILE, 'r', encoding='utf-8') as f:
                        self._cache_index = collections.OrderedDict(json.load(f))
                    keys_to_remove = [k for k in self._cache_index if not os.path.exists(os.path.join(TTS_CACHE_DIR, k + ".mp3"))]
                    for k in keys_to_remove:
                        del self._cache_index[k]
                except Exception:
                    self._cache_index = collections.OrderedDict()
            else:
                self._cache_index = collections.OrderedDict()

    def _save_cache_index_unlocked(self):
        try:
            with open(TTS_CACHE_INDEX_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(self._cache_index.items()), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.getLogger("DebateApp").error(f"[TTS] Błąd zapisu indeksu: {e}")

    def _clean_text_for_tts(self, text: str) -> str:
        return clean_text_for_tts(text, TTS_ACRONYM_MAP)

    def _get_tts_audio_path(self, text: str, lang: str = TTS_LANG, slow: bool = False) -> str | None:
        # KLUCZOWE: Dodanie flagi slow do skrótu SHA-256 gwarantuje brak konfliktów w cache dla różnych prędkości
        text_hash = hashlib.sha256(f"{text}-{lang}-{slow}".encode('utf-8')).hexdigest()
        cache_path = os.path.join(TTS_CACHE_DIR, f"{text_hash}.mp3")

        with self._lock:
            if text_hash in self._cache_index:
                item = self._cache_index.pop(text_hash)
                item['timestamp'] = time.time()
                self._cache_index[text_hash] = item
                self._save_cache_index_unlocked()
                if os.path.exists(cache_path) and os.path.getsize(cache_path) >= 500:
                    return cache_path
                else:
                    try: os.remove(cache_path)
                    except: pass
                    if text_hash in self._cache_index: del self._cache_index[text_hash]
                    self._save_cache_index_unlocked()

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_audio_file:
                temp_path = temp_audio_file.name
                # Przekazanie parametru slow bezpośrednio do silnika gTTS
                tts = gTTS(text=text, lang=lang, slow=slow)
                tts.save(temp_path)

            with self._lock:
                shutil.move(temp_path, cache_path)
                self._cache_index[text_hash] = {'timestamp': time.time(), 'size': os.path.getsize(cache_path)}
                self._save_cache_index_unlocked()
                
            current_size = sum(item['size'] for item in self._cache_index.values()) / (1024 * 1024)
            if current_size > (TTS_CACHE_MAX_SIZE_MB * (TTS_CACHE_CLEANUP_THRESHOLD_PERCENT / 100)):
                threading.Thread(target=self._clean_tts_cache, daemon=True).start()
            return cache_path
        except requests.exceptions.ConnectionError:
            self.message_queue.put({"type": "tts_error", "content": "Brak sieci dla lektora."})
            return None
        except Exception as e:
            self.message_queue.put({"type": "tts_error", "content": f"Błąd gTTS: {e}"})
            return None

    def _playback_worker(self):
        while not self._playback_stop_event.is_set():
            item = self._playback_queue.get()
            if item is None: break
            self._actual_play_audio_internal(item)
            self._playback_queue.task_done()

    def _actual_play_audio_internal(self, item):
        if not self.tts_enabled.get(): return
        self.tts_playing_event.set()
        self.message_queue.put({"type": "tts_status", "status": "playing"})

        # Obsługa local 'say' command fallback na macOS
        if isinstance(item, dict) and item.get("type") == "say":
            text = item.get("text", "")
            try:
                if sys.platform == "darwin":
                    cmd = ["say", "-v", "Zosia", text]
                    self._afplay_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    while self._afplay_process.poll() is None and self.tts_playing_event.is_set():
                        time.sleep(0.1)
                    if not self.tts_playing_event.is_set() and self._afplay_process.poll() is None:
                        self._afplay_process.terminate()
                        self._afplay_process.wait(timeout=1)
                    self._afplay_process = None
            except Exception as e:
                logging.getLogger("DebateApp").error(f"[TTS] Błąd fallback say: {e}")
            finally:
                self.tts_playing_event.clear()
                self.message_queue.put({"type": "tts_status", "status": "stopped"})
            return

        audio_file_path = item
        try:
            if self.has_pygame and pygame.mixer.get_init():
                sound = pygame.mixer.Sound(audio_file_path)
                self.tts_channel.play(sound)
                while self.tts_channel.get_busy() and self.tts_playing_event.is_set():
                    time.sleep(0.1)
                if not self.tts_playing_event.is_set():
                    self.tts_channel.stop()
            elif sys.platform == "darwin":
                cmd = ["afplay", audio_file_path]
                self._afplay_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                while self._afplay_process.poll() is None and self.tts_playing_event.is_set():
                    time.sleep(0.1)
                if not self.tts_playing_event.is_set() and self._afplay_process.poll() is None:
                    self._afplay_process.terminate()
                    self._afplay_process.wait(timeout=1)
                self._afplay_process = None
        except Exception as e:
            try: os.remove(audio_file_path)
            except: pass
        finally:
            self.tts_playing_event.clear()
            self.message_queue.put({"type": "tts_status", "status": "stopped"})

    def stop_tts(self):
        self.tts_playing_event.clear()
        try:
            while True:
                self._playback_queue.get_nowait()
                self._playback_queue.task_done()
        except queue.Empty:
            pass
        if self.has_pygame and pygame.mixer.get_init():
            pygame.mixer.stop()
        elif sys.platform == "darwin" and self._afplay_process and self._afplay_process.poll() is None:
            self._afplay_process.terminate()
            self._afplay_process = None

    def _periodic_cache_cleanup(self):
        while not self._cache_cleanup_stop_event.is_set():
            self._clean_tts_cache()
            self._cache_cleanup_stop_event.wait(TTS_CACHE_CLEANUP_INTERVAL_SECONDS)

    def _clean_tts_cache(self):
        now = time.time()
        keys_to_remove = []
        with self._lock:
            for file in os.listdir(TTS_CACHE_DIR):
                if file.endswith(".mp3"):
                    file_key = file.split(".")[0]
                    if file_key not in self._cache_index:
                        try: os.remove(os.path.join(TTS_CACHE_DIR, file))
                        except: pass
            current_size = sum(item['size'] for item in self._cache_index.values())
            for key, item in list(self._cache_index.items()):
                if (now - item['timestamp']) > TTS_CACHE_MAX_AGE_DAYS * 24 * 3600:
                    try:
                        os.remove(os.path.join(TTS_CACHE_DIR, key + ".mp3"))
                        current_size -= item['size']
                        keys_to_remove.append(key)
                    except: pass
            for key in keys_to_remove: del self._cache_index[key]
            max_size_bytes = TTS_CACHE_MAX_SIZE_MB * 1024 * 1024
            while current_size > max_size_bytes and self._cache_index:
                key, item = self._cache_index.popitem(last=False)
                try:
                    os.remove(os.path.join(TTS_CACHE_DIR, key + ".mp3"))
                    current_size -= item['size']
                except: pass
            self._save_cache_index_unlocked()

    def on_closing(self):
        try:
            if self.has_history:
                self._save_context(auto=True)
        except Exception as e:
            logging.getLogger("DebateApp").error(f"Błąd autozapisu przy zamykaniu: {e}")
            
        if self.is_running:
            if not messagebox.askokcancel("Wyjście", "Debata wciąż trwa. Czy na pewno chcesz przerwać i zamknąć aplikację?"):
                return
            self.debate_manager.stop_debate()

        self._cache_cleanup_stop_event.set()
        self.stop_tts()
        self._playback_stop_event.set()
        self._playback_queue.put(None)
        if self.has_pygame and pygame.mixer.get_init():
            pygame.mixer.quit()
        try:
            self.log_window.destroy()
        except:
            pass
        self.destroy()

    def _speak_agent_message(self, text: str, on_finish_callback=None):
        if not self.tts_enabled.get() or not self.audio_player.is_ready(): 
            if on_finish_callback: on_finish_callback()
            return
            
        # Pobieramy aktualny stan prędkości z GUI i pakujemy do kolejki zadań
        self.tts_queue.put({
            'type': 'generate_and_play', 
            'text': text, 
            'slow': self.tts_slow.get(), # Dynamiczne przekazanie parametru prędkości
            'original_callback': on_finish_callback 
        })
        self._check_tts_queue_loop()

    def _check_tts_queue_loop(self):
        # Zapobiegamy wielokrotnemu nakładaniu się pętli after
        if hasattr(self, "_tts_loop_after_id") and self._tts_loop_after_id:
            try:
                self.after_cancel(self._tts_loop_after_id)
            except Exception:
                pass
            self._tts_loop_after_id = None

        if not self.audio_player.is_playing() and not self._is_generating_tts and not self.tts_queue.empty():
            task = self.tts_queue.get_nowait()
            if task['type'] == 'generate_and_play':
                text_to_speak = task['text']
                is_slow = task.get('slow', False) # Odczytanie spakowanej prędkości
                
                on_tts_playback_finished = lambda: self.after(0, self._tts_playback_finished, task.get('original_callback'))
                
                self._is_generating_tts = True 
                threading.Thread(
                    target=self._generate_tts_and_add_to_player, 
                    args=(text_to_speak, is_slow, on_tts_playback_finished), # Przekazanie is_slow do workera
                    daemon=True
                ).start()
        
        self._tts_loop_after_id = self.after(100, self._check_tts_queue_loop)

    def _generate_tts_and_add_to_player(self, text_to_speak: str, is_slow: bool, on_playback_finish_callback=None):
        if not self.tts_enabled.get():
            self._is_generating_tts = False
            if on_playback_finish_callback: self.after(0, on_playback_finish_callback)
            return

        # Przekazujemy parametr is_slow do wyszukiwania ścieżki w cache
        audio_path = self._get_tts_audio_path(text_to_speak, slow=is_slow)
        if audio_path:
            self.audio_player.play_file(audio_path, on_finish_callback=on_playback_finish_callback)
        else:
            # Fallback dla macOS
            if sys.platform == "darwin":
                self.audio_player.play_file({"type": "say", "text": text_to_speak, "slow": is_slow}, on_finish_callback=on_playback_finish_callback)
            else:
                self._is_generating_tts = False
                if on_playback_finish_callback: self.after(0, on_playback_finish_callback)

    def _tts_playback_finished(self, original_callback=None):
        self._is_generating_tts = False 
        if original_callback:
            def run_callback_in_thread():
                try:
                    original_callback() 
                except Exception as e:
                    logging.getLogger("DebateApp").error(f"Błąd w oryginalnym callbacku po zakończeniu TTS: {e}", exc_info=True)
            
            threading.Thread(target=run_callback_in_thread, daemon=True).start()
        
        self._check_tts_queue_loop()

    def stop_tts_and_clear_queue(self):
        self.stop_tts()
        try:
            while not self.tts_queue.empty():
                self.tts_queue.get_nowait()
                self.tts_queue.task_done()
        except Exception:
            pass
        self._is_generating_tts = False