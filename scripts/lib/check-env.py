"""Report only capability names, never configuration values or secrets."""

from pathlib import Path
import shutil
import sys

from dotenv import dotenv_values

from stage1.config import validate_env

values = dotenv_values(Path(sys.argv[1]), interpolate=False)
errors = 0
for warning in validate_env({key: str(value or "") for key, value in values.items()}):
    print(f"⚠️  {warning}")
if (values.get("ROUGH_CUT_API_TOKEN") or "").strip():
    print("✅ API token настроен")
else:
    print("❌ ROUGH_CUT_API_TOKEN пуст; запустите scripts/setup-local")
    errors += 1
provider = (values.get("TTS_PROVIDER") or "openrouter").strip().lower()
if values.get("OPENROUTER_API_KEY"):
    print("✅ LLM-анализ доступен (OpenRouter)")
else:
    print("ℹ️ Без OPENROUTER_API_KEY доступна работа без LLM")
if provider == "openrouter":
    key = "OPENROUTER_API_KEY"
    if values.get(key):
        print("✅ Выбранный облачный TTS настроен")
    else:
        print(f"⚠️ Для выбранного TTS нужен {key}")
elif provider == "macos":
    connector = (values.get("MACOS_CONNECTOR_URL") or "").strip()
    if connector:
        print("ℹ️ Выбран macOS TTS через connector; проверьте его доступность")
    elif sys.argv[2] == "Darwin":
        for command in ("say", "afconvert"):
            if shutil.which(command):
                print(f"✅ Локальный TTS: {command} найден")
            else:
                print(f"❌ Локальный TTS: нет {command}")
                errors += 1
    else:
        print("❌ macOS TTS на этой платформе требует MACOS_CONNECTOR_URL")
        errors += 1
sys.exit(1 if errors else 0)
