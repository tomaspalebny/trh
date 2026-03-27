# 🏛️ Tržní Aréna – Deploy na Render.com

## Krok za krokem

### 1. Vytvořte GitHub repozitář
1. Jděte na github.com → New repository → název: `trzni-arena`
2. Nahrajte do něj tyto soubory:
   - `main.py`
   - `requirements.txt`
   - `render.yaml`

### 2. Deploy na Render
1. Jděte na [render.com](https://render.com) a přihlaste se (lze přes GitHub)
2. Klikněte **New → Web Service**
3. Připojte svůj GitHub repozitář `trzni-arena`
4. Render automaticky detekuje `render.yaml` a nastaví vše
5. Klikněte **Create Web Service**
6. Počkejte 2-3 minuty na build

### 3. Hotovo!
- **Studenti:** `https://trzni-arena.onrender.com`
- **Lektor:** `https://trzni-arena.onrender.com/admin`

## Tipy
- Před hodinou otevřete URL, aby se služba "probudila" (free tier usíná po 15 min)
- Sdílejte studentům odkaz přes QR kód (např. qr-code-generator.com)
- Pro reset hry mezi seminárními skupinami použijte tlačítko 🔄 v admin panelu

## Alternativní ruční deploy (bez render.yaml)
Na Render zvolte:
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Environment:** Python 3
