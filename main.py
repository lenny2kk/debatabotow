# -*- coding: utf-8 -*-
import os
import warnings
import logging

# Wyciszenie ostrzeżeń systemowych w konsoli
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
os.environ["TK_SILENCE_DEPRECATION"] = "1"

# Inicjalizacja profesjonalnego systemu logowania
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

try:
    import setuptools
except ImportError:
    pass

from gui import DebateApp

if __name__ == "__main__":
    app = DebateApp()
    app.mainloop()
