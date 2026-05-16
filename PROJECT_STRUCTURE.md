# 📘 Tržní Aréna – Project Structure

## Cíl projektu
Real-time webová simulace trhu pro výuku Mikroekonomie 1. Studenti hrají role kupujících a prodávajících, zadávají cenové nabídky a systém je automaticky páruje v double auction. Demonstruje nabídku, poptávku, tržní rovnováhu, konvergenci cen, daňovou incidenci, cenové stropy a podlahy.

## Struktura adresářů
```
trh/
├── main.py              # Celá aplikace (591 řádků): FastAPI + GameState + HTML
├── doc.md               # Detailní dokumentace (257 řádků) – architektura, matching, WebSocket
├── README.md            # Deploy návod na Render.com
├── requirements.txt     # fastapi, uvicorn
└── render.yaml          # Deploy konfigurace pro Render.com
```

## Klíčové algoritmy / Logika

### Ekonomický model
- **Kupující**: WTP ~ Uniform(30, 100) – soukromá hodnota
- **Prodávající**: MC ~ Uniform(10, 70) – soukromé náklady
- **Role**: střídavě buyer/seller dle pořadí připojení
- **Každý hráč**: 1 jednotka na kolo (single-unit)

### Double Auction Matching (try_match)
1. Bids seřazeny **sestupně** (nejvyšší nabídka první)
2. Offers seřazeny **vzestupně** (nejnižší nabídka první)
3. Pro každý nespárovaný bid:
   - Najdi první nespárovaný offer kde: bid_price ≥ offer_price + tax
   - A zároveň: offer ≥ price_floor, bid ≤ price_ceiling
   - Obchodní cena = **(bid + offer) / 2** (průměr – simuluje vyjednávání)
   - Musí být v rozsahu [floor, ceiling]
4. Profit buyer = WTP − trade_price − tax/2
5. Profit seller = trade_price − MC − tax/2

### Rovnovážná cena (_calc_equilibrium)
1. Seřaď WTP sestupně (poptávková křivka)
2. Seřaď MC vzestupně (nabídková křivka)
3. Iteruj páry: dokud WTP[i] ≥ MC[i] + tax → Q*++
4. P* = (WTP[Q*−1] + MC[Q*−1] + tax) / 2

### Tržní šoky
- **Daň (tax)**: jednotková daň dělená 50:50, posouvá efektivní nabídku
- **Cenová podlaha (price_floor)**: minimum, pod kterým nelze obchodovat
- **Cenový strop (price_ceiling)**: maximum (999 = žádný)
- Lektor je může měnit mezi koly i za běhu

### GameState (in-memory per room)
- Fáze: `lobby` → `trading` → `results` → `trading` → ...
- 5 kol (výchozí)
- Paralelní rooms: každá s vlastním GameState

### Frontend
- Kupující vidí svou WTP, prodávající své MC
- Textové pole pro zadání ceny + Odeslat
- Real-time feed posledních obchodů
- Leaderboard podle kumulativního zisku

### Admin panel
- Šoky: set_tax, set_floor, set_ceiling, reset
- Zobrazení: teoretická P*, Q*, počet obchodů, průměrná cena
- Graf: vývoj průměrné ceny vs. teoretické rovnováhy (Chart.js)

### Datové toky
```
Student → WebSocket /ws/player/{room}/{name} → bid (price)
Server → try_match() → broadcast trade → broadcast_state()
Lektor → WebSocket /ws/admin/{room} → start_round / end_round / set_tax / set_floor / set_ceiling
Server → broadcast_state() → všichni připojení (stav, leaderboard, recent_trades, eq_price)
```

## Aktuální stav
✅ **Hotovo** – produkčně nasazeno. Nejpropracovanější dokumentace ze všech her. Podporuje paralelní semináře.

## Technické poznámky
- **Tech stack**: Python 3.11+, FastAPI, WebSocket, uvicorn
- **Deploy**: Render.com (render.yaml ready)
- **Bez DB**: Stav v paměti
- **Omezení**: 1 jednotka/hráč/kolo, WTP/MC náhodné (nelze přednastavit křivky)
- **Možná rozšíření**: Export CSV, auth lektora, multi-unit, AI boti, real-time S/D křivky
- **Dokumentace**: 257 řádků v doc.md – nejdetailnější z herní série
- **Původní verze**: Starší single-room verze v `Economics/Mikro-hra/main.py`