# -*- coding: utf-8 -*-
import threading
import logging
import queue
import time
import os
import json
import urllib.request
import urllib.error
import re
import gc
import pickle
import tempfile
import socket
from queue import Empty
from typing import Tuple, Dict, List, Callable, Optional
from datetime import datetime, timezone
from pathlib import Path
from openai import OpenAI
from config import ALLOWED_EXTENSIONS, DENIED_FOLDERS, MAX_TOTAL_CONTEXT_CHARS

logger = logging.getLogger(__name__)

# Precompiled regex patterns to detect and neutralize prompt injection attempts
_INJECTION_PATTERNS = [
    r"zapomnij\s+o\s+poprzednich",
    r"zapomnij\s+poprzednie",
    r"ignore\s+previous\s+instructions",
    r"ignore\s+previous\s+prompt",
    r"ignore\s+prompt",
    r"zapomnij\s+instrukcj",
    r"zresetuj\s+instrukcj",
    r"you\s+must\s+now\s+act\s+as",
    r"teraz\s+będziesz\s+działać\s+jako",
    r"forget\s+all\s+previous",
    r"system\s+override"
]
_INJECTION_REGEX = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


class APIMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.response_times = []
        self.success_count = 0
        self.failure_count = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def record_success(self, duration: float, input_tok: int = 0, output_tok: int = 0):
        with self.lock:
            self.response_times.append(duration)
            self.success_count += 1
            self.input_tokens += input_tok
            self.output_tokens += output_tok

    def record_failure(self):
        with self.lock:
            self.failure_count += 1

    def get_metrics_report(self) -> str:
        with self.lock:
            total_reqs = self.success_count + self.failure_count
            avg_time = sum(self.response_times) / len(self.response_times) if self.response_times else 0.0
            max_time = max(self.response_times) if self.response_times else 0.0
            return (
                f"\n Gauges --- METRYKI WYDAJNOŚCI API ---\n"
                f"Liczba zapytań: {total_reqs} (Sukcesy: {self.success_count}, Błędy: {self.failure_count})\n"
                f"Średni czas odpowiedzi: {avg_time:.2f}s (Maksymalny: {max_time:.2f}s)\n"
                f"Szacowane zużycie tokenów: Wejściowe: {self.input_tokens}, Wyjściowe: {self.output_tokens}, Razem: {self.input_tokens + self.output_tokens}\n"
                f"-----------------------------------"
            )

class RateLimiter:
    def __init__(self, capacity: float = 3.0, refill_rate: float = 0.5):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.lock = threading.Lock()
        self.last_update = time.time()

    def acquire(self, tokens: float = 1.0) -> bool:
        with self.lock:
            now = time.time()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def wait_and_acquire(self, tokens: float = 1.0):
        while True:
            if self.acquire(tokens):
                return
            time.sleep(0.1)

class CircuitBreaker:
    def __init__(self, threshold: int = 3, timeout: float = 30.0, max_timeout: float = 60.0):
        self.threshold = threshold
        self.timeout = timeout
        self.max_timeout = max_timeout
        self._lock = threading.Lock()
        self._state = "CLOSED"
        self._failures = 0
        self._last_failure = None
        self._current_timeout = timeout
        
    def call(self, func, *args, **kwargs):
        with self._lock:
            if self._state == "OPEN":
                if self._last_failure:
                    elapsed = (datetime.now() - self._last_failure).total_seconds()
                    if elapsed >= self._current_timeout:
                        self._state = "HALF_OPEN"
                    else:
                        remaining = self._current_timeout - elapsed
                        raise RuntimeError(f"Circuit OPEN, retry in {remaining:.0f}s")
            current_state = self._state
        try:
            result = func(*args, **kwargs)
            with self._lock:
                self._failures = 0
                self._last_failure = None
                if current_state == "HALF_OPEN":
                    self._state = "CLOSED"
                    self._current_timeout = max(self.timeout, self._current_timeout * 0.5)
            return result
        except Exception as e:
            with self._lock:
                self._failures += 1
                self._last_failure = datetime.now()
                if self._failures >= self.threshold:
                    self._state = "OPEN"
                    self._current_timeout = min(self._current_timeout * 1.5, self.max_timeout)
            raise

class ModelValidator:
    _cache = {}
    _cache_lock = threading.Lock()
    CACHE_TTL = 30
    
    @classmethod
    def validate(cls, model_name: str) -> Tuple[bool, str]:
        with cls._cache_lock:
            if model_name in cls._cache:
                cached = cls._cache[model_name]
                age = (datetime.now() - cached['timestamp']).total_seconds()
                if age < cls.CACHE_TTL:
                    return cached['available'], cached['details']
        available, details = cls._check_api(model_name)
        with cls._cache_lock:
            cls._cache[model_name] = {'available': available, 'details': details, 'timestamp': datetime.now()}
        return available, details
    
    @classmethod
    def _check_api(cls, model_name: str) -> Tuple[bool, str]:
        model_lower = model_name.lower()
        if "deepseek" in model_lower:
            if os.getenv("DEEPSEEK_API_KEY"):
                return True, "Model chmurowy DeepSeek (Klucz API obecny)"
            return False, "Brak klucza DEEPSEEK_API_KEY w pliku .env!"
            
        if "gemini" in model_lower:
            if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
                return True, "Model chmurowy Gemini (Klucz API obecny)"
            return False, "Brak klucza GEMINI_API_KEY w pliku .env!"
            
        # Poniżej zostaw istniejący kod dla Ollamy (urllib.request do /api/tags)
        try:
            req = urllib.request.Request("http://localhost:11434/api/tags", method='GET')
            with urllib.request.urlopen(req, timeout=2) as response:
                data = json.loads(response.read().decode())
                models = [m['name'] for m in data.get('models', [])]
                if model_name in models: return True, "Model dostępny"
                try:
                    show_req = urllib.request.Request("http://localhost:11434/api/show", data=json.dumps({"name": model_name}).encode(), headers={'Content-Type': 'application/json'})
                    with urllib.request.urlopen(show_req, timeout=2): return True, "Model istnieje, ale niezaładowany"
                except urllib.error.HTTPError as e:
                    if e.code == 404: return False, f"Model {model_name} nie istnieje"
                    return False, f"Błąd API: {e.code}"
        except ConnectionRefusedError: return False, "Serwer Ollama nie odpowiada"
        except Exception as e: return False, f"Błąd połączenia: {e}"



class SafeAutosaver:
    MAX_SAVES = 20
    MAX_AGE_SECONDS = 3600  
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    
    def __init__(self, prefix: str = "debate_autosave_"):
        self.prefix = prefix
        self._temp_dir = Path(tempfile.gettempdir())
        
    def save(self, state: dict) -> Optional[Path]:
        try:
            data = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
            if len(data) > self.MAX_FILE_SIZE:
                logger.error(f"Stan za duży: {len(data)} bajtów")
                return None
                
            # Użycie bezpiecznego deskryptora pliku tempfile
            fd, temp_path = tempfile.mkstemp(
                prefix=self.prefix,
                suffix='.tmp',
                dir=self._temp_dir
            )
            
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(data)
                    
                # KRYTYCZNA POPRAWKA: os.replace gwarantuje atomowość operacji na poziomie OS
                final_path = self._generate_final_path()
                os.replace(temp_path, final_path)
            except Exception as write_error:
                try: os.unlink(temp_path)
                except OSError: pass
                raise write_error
                
            self._cleanup()
            return final_path
        except Exception as e:
            logger.error(f"Autosave failed: {e}")
            return None
            
    def _generate_final_path(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        return self._temp_dir / f"{self.prefix}{timestamp}.pkl"

    def _cleanup(self):
        try:
            now = time_module.time()
            cutoff = now - self.MAX_AGE_SECONDS
            saves = list(self._temp_dir.glob(f"{self.prefix}*.pkl"))
            saves.sort(key=lambda p: p.stat().st_mtime)
            
            for save in saves:
                if save.stat().st_mtime < cutoff:
                    save.unlink(missing_ok=True)
                    
            saves = list(self._temp_dir.glob(f"{self.prefix}*.pkl"))
            saves.sort(key=lambda p: p.stat().st_mtime)
            if len(saves) > self.MAX_SAVES:
                for old_save in saves[:-self.MAX_SAVES]:
                    old_save.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Błąd czyszczenia cache autozapisów: {e}")


class DebateManager:
    """
    Zarządza przebiegiem debaty w osobnym wątku, wyposażony w monitoring,
    zabezpieczenia limitów tokenów i zapytań, oraz mechanizmy odpornościowe.
    """
    def __init__(self, message_queue: queue.Queue):
        self.message_queue = message_queue
        self.stop_event = threading.Event()
        self.thread = None
        self.running = False
        
        # Inicjalizacja metryk i limiterów
        self.metrics = APIMetrics()
        self.rate_limiter = RateLimiter(capacity=3.0, refill_rate=0.5)
        self.circuit_breaker = CircuitBreaker()
        self.model_validator = ModelValidator()
        self.API_TIMEOUT = 30
        
        # Singleton klientów LLM
        self._init_llm_clients()
        
        # Historia konwersacji
        self.history_a = []
        self.history_b = []
        self.last_prompt = None
        
        # Inicjalizacja autozapisu
        self.autosaver = SafeAutosaver()

    def _init_llm_clients(self):
        """Inicjalizacja singletonów klientów API chmurowych i lokalnych."""
        # Klient Ollama (standard OpenAI)
        self.client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
        
        # Klient DeepSeek
        self.deepseek_client = None
        ds_key = os.getenv("DEEPSEEK_API_KEY")
        if ds_key and ds_key.strip() not in ("", "your_deepseek_api_key_here"):
            try:
                self.deepseek_client = OpenAI(
                    base_url="https://api.deepseek.com/v1", 
                    api_key=ds_key.strip()
                )
                logger.info("✅ Klient chmurowy DeepSeek zainicjalizowany pomyślnie")
            except Exception as e:
                logger.error(f"❌ Błąd inicjalizacji DeepSeek: {e}")
                
        # Klient Gemini
        self.gemini_client = None
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key and gemini_key.strip() not in ("", "your_gemini_api_key_here"):
            try:
                import google.generativeai as genai
                genai.configure(api_key=gemini_key)
                self.gemini_client = genai
                logger.info("[API] Klient Gemini zainicjalizowany pomyślnie.")
            except Exception as e:
                logger.error(f"[API] Błąd inicjalizacji klienta Gemini: {e}")

    @property
    def ollama_client(self):
        try:
            with socket.create_connection(("localhost", 11434), timeout=0.1):
                return self.client
        except:
            return None

    def start_debate(self, api_key: str, model_a: str, system_a: str, model_b: str, system_b: str, initial_topic: str, delay: float = 2.0, max_rounds: int = None):
        if self.running:
            return

        # Re-konfiguracja w locie w oparciu o wejściowy API key chmury (jeśli podano w GUI)
        if api_key and api_key.strip():
            clean_key = api_key.strip().strip('"').strip("'")
            os.environ["GEMINI_API_KEY"] = clean_key
            os.environ["GOOGLE_API_KEY"] = clean_key
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            try:
                import google.generativeai as genai
                genai.configure(api_key=clean_key)
                self.gemini_client = genai
            except Exception as e:
                logger.error(f"Błąd rekonfiguracji klienta Gemini: {e}")

        # Re-konfiguracja DeepSeek w locie ze środowiska
        ds_key = os.getenv("DEEPSEEK_API_KEY")
        if ds_key and ds_key.strip() not in ("", "your_deepseek_api_key_here"):
            try:
                self.deepseek_client = OpenAI(
                    base_url="https://api.deepseek.com/v1", 
                    api_key=ds_key.strip()
                )
            except Exception as e:
                logger.error(f"Błąd rekonfiguracji klienta DeepSeek: {e}")

        # Walidacja modeli przed startem
        for model_name, label in [(model_a, "Agent A"), (model_b, "Agent B")]:
            if any(x in model_name.lower() for x in ["ollama", "llama", "gemini", "deepseek"]):
                available, details = self.model_validator.validate(model_name)
                if not available:
                    self.message_queue.put({
                        "type": "error", 
                        "text": f"❌ {label}: Model {model_name} niedostępny: {details}"
                    })
                    self.message_queue.put({"type": "finished"})
                    return
                self.message_queue.put({
                    "type": "system",
                    "text": f"✓ {label}: {details}"
                })
        
        if "deepseek" in model_a.lower() or "deepseek" in model_b.lower():
            if not ds_key or ds_key.strip() in ("", "your_deepseek_api_key_here"):
                self.message_queue.put({"type": "error", "text": "Błąd: Brak klucza DEEPSEEK_API_KEY w pliku .env lub GUI!", "message": "Błąd: Brak klucza DEEPSEEK_API_KEY w pliku .env lub GUI!"})
                self.message_queue.put({"type": "finished"})
                self.running = False
                return
        
        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(
            target=self._run_debate,
            args=(api_key, model_a, system_a, model_b, system_b, initial_topic, delay, max_rounds),
            daemon=True
        )
        self.thread.start()

    def stop_debate(self):
        if not self.running:
            return
        self.stop_event.set()
        self.running = False

    def reset(self):
        self.history_a = []
        self.history_b = []
        self.last_prompt = None

    def inject_user_prompt(self, text: str):
        sanitized_text = self._sanitize_text(text)
        self.last_prompt = sanitized_text
        
        if self.history_a and self.history_a[-1]["role"] != "system":
            self.history_a[-1]["content"] = f"{self.history_a[-1]['content']}\n\n[INTERWENCJA UŻYTKOWNIKA]: {sanitized_text}"
        else:
            self.history_a.append({"role": "user", "content": sanitized_text})
            
        if self.history_b and self.history_b[-1]["role"] != "system":
            self.history_b[-1]["content"] = f"{self.history_b[-1]['content']}\n\n[INTERWENCJA UŻYTKOWNIKA]: {sanitized_text}"
        else:
            self.history_b.append({"role": "user", "content": sanitized_text})

    def get_state(self) -> dict:
        return {
            "history_a_serialized": self.history_a,
            "history_b_serialized": self.history_b,
            "last_prompt": self.last_prompt
        }

    def load_state(self, state_dict: dict):
        self.history_a = state_dict.get("history_a_serialized", [])
        self.history_b = state_dict.get("history_b_serialized", [])
        self.last_prompt = state_dict.get("last_prompt")

    def _sleep_with_check(self, duration: float):
        """Płynne czekanie przerybane natychmiast po ustawieniu stop_event."""
        self.stop_event.wait(timeout=duration)

    @classmethod
    def _sanitize_text(cls, text: str) -> str:
        """Sanityzacja tekstu w celu ochrony przed Prompt Injection."""
        if not text:
            return ""
        sanitized, count = _INJECTION_REGEX.subn("[USUNIĘTO ZŁOŚLIWY PROMPT]", text)
        if count > 0:
            logger.warning(f"Skaner wejść wykrył i usunął {count} potencjalnych prób prompt injection.")
        return sanitized

    def _enforce_token_limit(self):
        """Twardy bezpiecznik przycinający historię rozmowy do 20 najnowszych wymian (wpisów) poza system promptem."""
        for hist in [self.history_a, self.history_b]:
            if len(hist) > 21:  # System prompt (index 0) + 20 ostatnich wpisów
                system_msg = hist[0]
                truncated = hist[-20:]
                hist[:] = [system_msg] + truncated
        logger.info("[TOKEN LIMIT] Przycięto historię debaty w celu ochrony limitu tokenów.")

    def _auto_save_state(self):
        """Zapisuje stan debaty w bezpieczny sposób za pomocą SafeAutosaver."""
        try:
            state = self.get_state()
            final_path = self.autosaver.save(state)
            if final_path:
                logger.info(f"[AUTOSAVE] Zapisano stan debaty: {final_path}")
        except Exception as e:
            logger.error(f"[AUTOSAVE] Błąd podczas autozapisu stanu: {e}")

    def _call_with_retry(self, api_call_func, *args, **kwargs):
        """Wywołuje funkcję API z 3 próbami, exponential backoff i monitoringiem czasu."""
        retries = 3
        backoff = 2.0
        for attempt in range(retries):
            if self.stop_event.is_set():
                raise RuntimeError("Debata została zatrzymana podczas próby połączenia API.")
            try:
                start_time = time.time()
                res = api_call_func(*args, **kwargs)
                duration = time.time() - start_time
                return res, duration
            except Exception as e:
                logger.warning(f"Błąd API (próba {attempt+1}/{retries}): {e}")
                error_str = str(e)
                if "Insufficient Balance" in error_str or "402" in error_str or "401" in error_str:
                    self.metrics.record_failure()
                    raise
                if attempt == retries - 1:
                    self.metrics.record_failure()
                    raise
                # Exponential backoff czekający z możliwością natychmiastowego wybudzenia
                self.stop_event.wait(timeout=backoff ** attempt)

    def _convert_openai_to_gemini_messages(self, messages: list) -> list:
        gemini_messages = []
        last_role = None
        
        for msg in messages:
            role = msg["role"]
            content = msg["content"].strip()
            
            if not content or content == "[cisza]":
                continue
                
            # Mapowanie ról: system i user -> user, assistant -> model
            content = self._sanitize_text(content)
            
            gemini_role = "user" if role in ("user", "system") else "model"
            
            if gemini_role == last_role and gemini_messages:
                gemini_messages[-1]["parts"][0]["text"] += "\n\n" + content
            else:
                gemini_messages.append({"role": gemini_role, "parts": [{"text": content}]})
                last_role = gemini_role
                
        return gemini_messages

    def _prepare_messages_for_api(self, history_list: list) -> list:
        prepared_messages = []
        for msg in history_list:
            sanitized_content = self._sanitize_text(msg["content"])
            if prepared_messages and msg["role"] == "user" and prepared_messages[-1]["role"] == "user":
                prepared_messages[-1]["content"] += "\n\n" + sanitized_content
            elif prepared_messages and msg["role"] == "assistant" and prepared_messages[-1]["role"] == "assistant":
                prepared_messages[-1]["content"] += "\n\n" + sanitized_content
            else:
                new_msg = msg.copy()
                new_msg["content"] = sanitized_content
                prepared_messages.append(new_msg)
        return prepared_messages

    def _get_llm_response(self, model: str, messages: list, system_instruction: str = None) -> str:
        """Uniwersalna metoda pobierania odpowiedzi kierująca ruch, ze wsparciem limicenia i ponowień."""
        model_lower = model.lower()

        # Token bucket rate limiting (wymagamy 1 żeton na zapytanie API chmurowe)
        if "deepseek" in model_lower or "gemini" in model_lower:
            self.rate_limiter.wait_and_acquire(1.0)

        # CASE 1: DEEPSEEK V4 (Standard OpenAI Cloud)
        if "deepseek" in model_lower:
            if not self.deepseek_client:
                raise RuntimeError("Klient DeepSeek nie został zainicjalizowany. Sprawdź plik .env.")
            try:
                response, duration = self._call_with_retry(
                    self.deepseek_client.chat.completions.create,
                    model="deepseek-chat",  # Produkcyjna nazwa modelu dla V4/Pro
                    messages=messages,      # DeepSeek przyjmuje standardową historię OpenAI
                    timeout=self.API_TIMEOUT,
                    stream=False,
                    temperature=0.7
                )
                if response.choices and response.choices[0].message.content:
                    text = response.choices[0].message.content.strip()
                else:
                    text = "[DeepSeek: Pusta odpowiedź serwera]"

                # Odczyt zużycia tokenów
                input_tokens = 0
                output_tokens = 0
                usage = getattr(response, "usage", None)
                if usage:
                    input_tokens = getattr(usage, "prompt_tokens", 0)
                    output_tokens = getattr(usage, "completion_tokens", 0)
                else:
                    # Estymacja
                    input_tokens = sum(len(m["content"]) for m in messages) // 4
                    output_tokens = len(text) // 4
                    
                self.metrics.record_success(duration, input_tokens, output_tokens)
                return self._sanitize_text(text)
            except Exception as e:
                error_str = str(e)
                if "Insufficient Balance" in error_str or "402" in error_str:
                    raise RuntimeError("Brak środków na koncie API DeepSeek (402 Insufficient Balance)!")
                elif "401" in error_str:
                    raise RuntimeError("Nieprawidłowy klucz API DeepSeek (401 Unauthorized)!")
                else:
                    raise RuntimeError(f"Błąd API DeepSeek: {e}")

        # CASE 2: GEMINI 2.5 (Specyficzne Google SDK)
        elif "gemini" in model_lower:
            if not self.gemini_client:
                raise RuntimeError("Klient Gemini nie został zainicjowany. Sprawdź klucz API.")
            
            gemini_messages = self._convert_openai_to_gemini_messages(messages)
            chat_model = self.gemini_client.GenerativeModel(model_name=model, system_instruction=system_instruction)
            
            response, duration = self._call_with_retry(
                chat_model.generate_content,
                gemini_messages,
                request_options={"timeout": 30}
            )
            
            text = ""
            if response.candidates and response.candidates[0].content.parts:
                text = response.candidates[0].content.parts[0].text
            elif hasattr(response, 'text') and response.text:
                text = response.text
            else:
                text = "[Gemini zakończył proces bez zwrócenia tekstu]"

            # Odczyt zużycia tokenów
            input_tokens = 0
            output_tokens = 0
            usage = getattr(response, "usage_metadata", None)
            if usage:
                input_tokens = getattr(usage, "prompt_token_count", 0)
                output_tokens = getattr(usage, "candidates_token_count", 0)
            else:
                # Estymacja
                input_tokens = sum(len(m.get("parts", [{}])[0].get("text", "")) for m in gemini_messages) // 4
                output_tokens = len(text) // 4

            self.metrics.record_success(duration, input_tokens, output_tokens)
            return self._sanitize_text(text)

        # CASE 3: OLLAMA (Lokalny serwer hostowany)
        else:
            if not self.ollama_client:
                raise RuntimeError("Klient Ollama nie jest gotowy. Upewnij się, że serwer działa.")
            
            response, duration = self._call_with_retry(
                self.ollama_client.chat.completions.create,
                model=model,
                messages=messages,
                timeout=30
            )
            
            text = ""
            if response.choices and response.choices[0].message.content:
                text = response.choices[0].message.content
            else:
                text = "[Ollama zakończył proces bez zwrócenia tekstu]"
                
            input_tokens = 0
            output_tokens = 0
            usage = getattr(response, "usage", None)
            if usage:
                input_tokens = getattr(usage, "prompt_tokens", 0)
                output_tokens = getattr(usage, "completion_tokens", 0)
            else:
                input_tokens = sum(len(m["content"]) for m in messages) // 4
                output_tokens = len(text) // 4
                
            self.metrics.record_success(duration, input_tokens, output_tokens)
            return self._sanitize_text(text)

    def _send_message(self, model: str, history: list, system_instruction: str, api_key: str) -> str:
        messages_for_api = self._prepare_messages_for_api(history)
        model_lower = model.lower()
        
        # Inicjalizacja klienta DeepSeek w locie, jeśli wybrano DeepSeek i podano/zmieniono klucz
        if "deepseek" in model_lower:
            try:
                from openai import OpenAI as OpenAIClient
                ds_key = os.getenv("DEEPSEEK_API_KEY")
                if ds_key and ds_key.strip() not in ("", "your_deepseek_api_key_here"):
                    self.deepseek_client = OpenAIClient(base_url="https://api.deepseek.com/v1", api_key=ds_key.strip())
            except Exception as e:
                self.message_queue.put({"type": "error", "text": f"Błąd konfiguracji klienta DeepSeek ({model}): {str(e)}"})
                return None

        # Inicjalizacja klienta Gemini w locie, jeśli wybrano Gemini i podano klucz
        if "gemini" in model_lower:
            try:
                import google.generativeai as genai
                
                clean_api_key = api_key.strip().strip('"').strip("'") if api_key else ""
                os.environ["GEMINI_API_KEY"] = clean_api_key
                os.environ["GOOGLE_API_KEY"] = clean_api_key
                os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
                
                genai.configure(api_key=clean_api_key)
                self.gemini_client = genai
            except Exception as e:
                self.message_queue.put({"type": "error", "text": f"Błąd konfiguracji klienta Gemini ({model}): {str(e)}"})
                return None

        try:
            return self._get_llm_response(model, messages_for_api, system_instruction)
        except Exception as e:
            if "deepseek" in model_lower:
                self.message_queue.put({"type": "error", "text": f"Błąd API DeepSeek ({model}): {str(e)}"})
            elif "gemini" in model_lower:
                self.message_queue.put({"type": "error", "text": f"Błąd Gemini API ({model}): {str(e)}"})
            else:
                self.message_queue.put({"type": "error", "text": f"Lokalna Ollama zgłosiła błąd ({model}): {str(e)}"})
            return None

    def get_metrics_report(self) -> str:
        """Zwraca gotowy tekstowy raport wydajności debaty."""
        return self.metrics.get_metrics_report()

    @staticmethod
    def _load_folder_context(folder_path: str, message_queue: queue.Queue) -> str:
        context_parts = []
        total_chars_read = 0
        total_files_processed = 0
        
        priority_files = []
        other_files = []
        
        try:
            for root, dirs, files in os.walk(folder_path):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() not in [df.lower() for df in DENIED_FOLDERS]]
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        full_path = os.path.join(root, file)
                        rel_path = os.path.relpath(full_path, folder_path)
                        if any(pf in rel_path.lower() for pf in ['config.py', 'main.py', 'requirements.txt']):
                            priority_files.append((full_path, rel_path))
                        else:
                            other_files.append((full_path, rel_path))
            
            all_files_to_process = priority_files + other_files
            
            for full_path, rel_path in all_files_to_process:
                try:
                    file_size = os.path.getsize(full_path)
                    
                    if total_chars_read + file_size > MAX_TOTAL_CONTEXT_CHARS:
                        logger.warning(f"[CONTEXT] Osiągnięto twardy limit znaków ({MAX_TOTAL_CONTEXT_CHARS}). Przerywam skanowanie.")
                        message_queue.put({"type": "system", "text": f"⚠️ Osiągnięto limit kontekstu ({MAX_TOTAL_CONTEXT_CHARS} znaków). Pominięto resztę plików."})
                        break
                        
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    
                    if not content:
                        continue
                    
                    # Sanityzacja zawartości plików przed dodaniem do kontekstu
                    sanitized_content = DebateManager._sanitize_text(content)
                    
                    part = f"---\nPLIK: {rel_path}\nTREŚĆ:\n{sanitized_content}\n---\n"
                    context_parts.append(part)
                    total_chars_read += len(content)
                    total_files_processed += 1
                except Exception as e:
                    logger.error(f"Błąd odczytu pliku {full_path}: {e}")
        except Exception as e:
            message_queue.put({"type": "error", "text": f"❌ Błąd skanowania folderu: {str(e)}"})
        finally:
            # Jawne wyczyszczenie pamięci i Garbage Collection po przetworzeniu dużych plików
            gc.collect()
            
        if total_files_processed > 0:
            message_queue.put({"type": "system", "text": f"Pomyślnie wczytano {total_files_processed} plików ({total_chars_read} znaków kontekstu)."})
        return "".join(context_parts)

    def _run_debate(self, api_key: str, model_a: str, system_a: str, model_b: str, system_b: str, initial_topic: str, delay: float, max_rounds: int):
        try:
            self.message_queue.put({"type": "system", "text": "Inicjalizacja silników AI..."})
            
            # Przygotowanie struktur startowych, jeśli historia jest pusta
            if not self.history_a:
                self.history_a.append({"role": "system", "content": system_a})
                self.history_a.append({"role": "user", "content": initial_topic})
            if not self.history_b:
                self.history_b.append({"role": "system", "content": system_b})
                self.history_b.append({"role": "user", "content": initial_topic})

            # Obliczanie rund na podstawie wpisów typu 'assistant'
            count_a = sum(1 for msg in self.history_a if msg["role"] == "assistant")
            round_num = count_a + 1
            
            self.message_queue.put({"type": "system", "text": "Debata została uruchomiona!"})

            while not self.stop_event.is_set():
                if max_rounds is not None and round_num > max_rounds:
                    self.message_queue.put({"type": "system", "text": f"Osiągnięto limit rund ({max_rounds}). Zakończenie."})
                    break
                
                # Co 5 rund wywołujemy bezpiecznik tokenów
                if round_num % 5 == 0:
                    self._enforce_token_limit()

                # --- RUNDA AGENTA A ---
                self.message_queue.put({"type": "system", "text": f"Agent A (Runda {round_num}) analizuje..."})
                if self.stop_event.is_set(): break

                try:
                    text_a = self.circuit_breaker.call(
                        self._send_message, model_a, self.history_a, system_a, api_key
                    )
                except RuntimeError as e:
                    self.message_queue.put({"type": "error", "text": f"Circuit breaker dla Agenta A: {e}"})
                    break

                if text_a is None: break
                
                self.history_a.append({"role": "assistant", "content": text_a})
                
                if len(self.history_b) == 2:  # Na starcie w history_b mamy [system, user: topic]
                    self.history_b.append({"role": "assistant", "content": text_a})
                self.history_b.append({"role": "user", "content": text_a})

                self.message_queue.put({"type": "agent_a", "round": round_num, "text": text_a})
                if delay > 0: self._sleep_with_check(delay)

                # --- RUNDA AGENTA B ---
                self.message_queue.put({"type": "system", "text": f"Agent B (Runda {round_num}) analizuje..."})
                if self.stop_event.is_set(): break

                try:
                    text_b = self.circuit_breaker.call(
                        self._send_message, model_b, self.history_b, system_b, api_key
                    )
                except RuntimeError as e:
                    self.message_queue.put({"type": "error", "text": f"Circuit breaker dla Agenta B: {e}"})
                    break

                if text_b is None: break

                self.history_b.append({"role": "assistant", "content": text_b})
                self.history_a.append({"role": "user", "content": text_b})  # Odpowiedź B wraca jako prompt dla A

                self.message_queue.put({"type": "agent_b", "round": round_num, "text": text_b})
                
                # Zapisywanie stanu co rundę
                self._auto_save_state()
                
                if delay > 0: self._sleep_with_check(delay)

                round_num += 1
                
        except Exception as e:
            self.message_queue.put({"type": "error", "text": f"Błąd krytyczny wątku: {str(e)}", "message": f"Błąd krytyczny wątku: {str(e)}"})
        finally:
            self.running = False
            # Wysyłanie pełnego raportu z metrykami działania
            try:
                metrics_report = self.get_metrics_report()
                self.message_queue.put({"type": "system", "text": metrics_report})
            except Exception as e:
                logger.error(f"Nie udało się wysłać raportu metryk: {e}")
                
            self.message_queue.put({"type": "system", "text": "Debata zatrzymana."})
            self.message_queue.put({"type": "finished"})

    def generate_antigraviti_prompt(self, project_files: dict = None) -> str:
        """Automatyczny generator promptu implementacyjnego na podstawie decyzji z debaty."""
        if not project_files:
            project_files = self._load_project_files_for_prompt()
        changes = self._extract_changes_from_history()
        parts = [
            "=== ANTIGRAVITI IMPLEMENTATION ===",
            f"TIMESTAMP: {datetime.now().isoformat()}",
            "",
            "=== PROJECT FILES ==="
        ]
        for path, content in project_files.items():
            ext = os.path.splitext(path)[1]
            lang = {'.py': 'python', '.js': 'javascript', '.ts': 'typescript', '.json': 'json'}.get(ext, 'text')
            parts.extend([f"FILE: {path}", f"LANG: {lang}", content[:2000], "END_FILE"])
        parts.append("\n=== CHANGES ===")
        for i, change in enumerate(changes, 1):
            parts.extend([f"CHANGE_{i}:", json.dumps(change, indent=2, ensure_ascii=False), ""])
        parts.extend(["=== INSTRUCTIONS ===", "1. Implementuj zmiany w kolejności", "2. Oznacz zmiany komentarzem '// CHANGED'", "3. Waliduj składnię po każdej zmianie", "=== END ==="])
        return '\n'.join(parts)
    
    def _extract_changes_from_history(self) -> list:
        changes = []
        current_file = None
        for msg in self.history_a + self.history_b:
            content = msg.get('content', '')
            for line in content.split('\n'):
                line = line.strip()
                if line.startswith('FILE:'):
                    current_file = line.split('FILE:')[1].strip()
                elif '```' in line and current_file:
                    changes.append({'file': current_file, 'operation': 'MODIFY', 'code': line.strip('`').strip(), 'description': 'Implementation from debate'})
        return changes
    
    def _load_project_files_for_prompt(self) -> dict:
        files = {}
        total_size = 0
        max_size = 500 * 1024
        for path in Path('.').rglob('*.py'):
            if total_size >= max_size: break
            try:
                content = path.read_text(encoding='utf-8', errors='ignore')
                total_size += len(content.encode())
                files[str(path)] = content[:2000]
            except: pass
        return files
