#!/bin/bash

# Nazwa folderu środowiska wirtualnego i pliku skryptu
VENV_DIR=".venv"
SCRIPT_NAME="main.py"

echo "🐍 Inicjalizacja skryptu Python..."

# 1. Sprawdzenie, czy środowisko wirtualne istnieje
if [ -d "$VENV_DIR" ]; then
    echo "📦 Aktywacja środowiska wirtualnego..."
    source "$VENV_DIR/bin/activate"
else
    echo "❌ Błąd: Nie znaleziono folderu środowiska wirtualnego '$VENV_DIR'!"
    echo "Upewnij się, że .venv znajduje się w tym samym folderze co ten skrypt."
    exit 1
fi

# 2. Sprawdzenie, czy plik main.py istnieje
if [ -f "$SCRIPT_NAME" ]; then
    echo "🚀 Uruchamianie $SCRIPT_NAME..."
    echo "------------------------------------------------"
    python "$SCRIPT_NAME"
    echo "------------------------------------------------"
else
    echo "❌ Błąd: Nie znaleziono pliku $SCRIPT_NAME!"
    exit 1
fi

# 3. Dezaktywacja środowiska po zakończeniu działania programu
deactivate
echo "✅ Skrypt Python zakończył działanie, środowisko wirtualne zamknięte."
