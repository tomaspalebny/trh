# 📘 Tržní Aréna – Dokumentace

## Přehled aplikace

Tržní Aréna je real-time webová simulace trhu pro výuku Mikroekonomie 1. Studenti hrají role kupujících a prodávajících, zadávají cenové nabídky a systém je automaticky páruje. Hra běží na FastAPI s WebSocket komunikací, podporuje paralelní semináře (místnosti) a nevyžaduje žádnou instalaci na straně studenta – stačí prohlížeč.

---

## Architektura

```
┌─────────────┐     WebSocket (wss://)     ┌──────────────────┐
│  Student     │ ◄──────────────────────► │                  │
│  (prohlížeč) │                           │  FastAPI server  │
└─────────────┘                           │                  │
                                           │  GameState       │
┌─────────────┐     WebSocket (wss://)     │   per room       │
│  Lektor      │ ◄──────────────────────► │                  │
│  (prohlížeč) │                           └──────────────────┘
└─────────────┘
```

- **Frontend:** Vanilla HTML/CSS/JS, responsivní design (mobil i PC)
- **Backend:** Python, FastAPI, WebSocket pro obousměrnou real-time komunikaci
- **Stav hry:** V paměti serveru, oddělený per místnost (room)
- **Vizualizace:** Chart.js v admin panelu (graf konvergence cen)

---

## Systém místností (rooms)

Každý seminář má vlastní kód (např. `S01`, `PATOK3`). Místnost se vytvoří automaticky při prvním přístupu. Každá místnost obsahuje zcela nezávislý `GameState` – vlastní hráče, kola, obchody, šoky i žebříček.

| Cesta | Účel |
|---|---|
| `/` | Úvodní stránka – zadání kódu semináře a jména |
| `/play/{room_id}?name=Jméno` | Herní obrazovka studenta |
| `/admin/{room_id}` | Lektorský panel pro daný seminář |

---

## Přidělování rolí a hodnot

Při vstupu do místnosti je každý hráč **střídavě** přiřazen jako kupující nebo prodávající (sudý = kupující, lichý = prodávající).

- **Kupující** dostane náhodnou **WTP (Willingness To Pay)** z rozsahu **30–100 Kč**
  - WTP = maximální cena, kterou je ochoten zaplatit
- **Prodávající** dostane náhodné **MC (Marginal Cost)** z rozsahu **10–70 Kč**
  - MC = minimální cena, pod kterou nesmí prodávat

Tyto hodnoty jsou **soukromé** – každý student vidí jen tu svou. Různé hodnoty mezi hráči vytvářejí klesající křivku poptávky a rostoucí křivku nabídky.

---

## Fáze hry

Hra probíhá v **5 kolech**, každé kolo má 3 fáze:

```
lobby → trading → results → trading → results → ... → final
```

1. **Lobby (čekárna):** Studenti se připojují, obchodování je zablokované.
2. **Trading (obchodování):** Lektor spustí kolo. Studenti zadávají cenové nabídky. Systém páruje obchody v reálném čase. Každý hráč může v jednom kole obchodovat **maximálně jednou** (má jednu jednotku zboží).
3. **Results (výsledky):** Lektor ukončí kolo. Zobrazí se statistiky – počet obchodů, průměrná cena, srovnání s rovnováhou.

Mezi koly může lektor zavést **tržní šoky** (daň, cenová podlaha, cenový strop).

---

## Mechanismus párování obchodů (matching)

### Princip: Cenová kompatibilita (nikoliv čas)

Párování funguje na principu **oboustranné aukce (double auction)**. Systém **nepáruje podle času zadání**, ale podle **cenové kompatibility** – konkrétně najde nejlepší cenový match mezi aktivními nabídkami a poptávkami.

### Algoritmus krok za krokem

1. Student (kupující) zadá **bid** = cena, kterou nabízí za zboží.
2. Student (prodávající) zadá **offer** = cena, za kterou chce prodat.
3. Po každém novém bidu/offeru se spustí matching:

```
a) Seřaď všechny bidy SESTUPNĚ (nejvyšší nabídka první)
b) Seřaď všechny offery VZESTUPNĚ (nejnižší nabídka první)
c) Pro každý nespárovaný bid:
   - Najdi první nespárovaný offer, kde:
     bid_cena ≥ offer_cena + daň
   - Pokud existuje → obchod se uskuteční
```

### Výpočet obchodní ceny

Obchodní cena je **průměr bidu a offeru**:

```
obchodní_cena = (bid_cena + offer_cena) / 2
```

**Příklad:**
- Kupující zadá bid: 65 Kč
- Prodávající zadá offer: 45 Kč
- Podmínka: 65 ≥ 45 ✅
- Obchodní cena: (65 + 45) / 2 = **55 Kč**

### Proč průměr?

Průměr bidu a offeru simuluje **vyjednávání** – ani kupující, ani prodávající nezíská celý přebytek. Obě strany „ustoupí" na půl cesty. To je standardní mechanismus v experimentální ekonomii (Vernon Smith, Nobelova cena 2002).

### Validační pravidla

Obchod se uskuteční **pouze pokud**:

| Pravidlo | Popis |
|---|---|
| `bid ≥ offer + daň` | Kupující nabízí dost na pokrytí ceny i daně |
| `offer ≥ price_floor` | Prodávající neprodává pod cenovou podlahou |
| `bid ≤ price_ceiling` | Kupující nenabízí nad cenovým stropem |
| `obchodní_cena ∈ [floor, ceiling]` | Výsledná cena spadá do regulovaného pásma |
| Hráč ještě neobchodoval v tomto kole | Jeden obchod na kolo na hráče |

---

## Výpočet zisku

Po každém obchodu se oběma stranám připočte zisk:

- **Kupující:** `zisk = WTP - obchodní_cena - (daň / 2)`
- **Prodávající:** `zisk = obchodní_cena - MC - (daň / 2)`

Daň je rozdělena rovnoměrně mezi obě strany (50:50). Celkový zisk hráče se sčítá přes všechna kola.

**Příklad (bez daně):**
- Kupující s WTP = 80 nakoupí za 55 → zisk = 80 - 55 = **25 Kč**
- Prodávající s MC = 30 prodá za 55 → zisk = 55 - 30 = **25 Kč**

**Příklad (s daní 10 Kč):**
- Kupující: 80 - 55 - 5 = **20 Kč**
- Prodávající: 55 - 30 - 5 = **20 Kč**

---

## Výpočet rovnovážné ceny (P*) a množství (Q*)

Server průběžně počítá teoretickou rovnováhu na základě aktuálních WTP a MC všech hráčů:

```
1. Seřaď WTP všech kupujících SESTUPNĚ → křivka poptávky
2. Seřaď MC všech prodávajících VZESTUPNĚ → křivka nabídky
3. Procházej páry (WTP[i], MC[i]):
   - Dokud WTP[i] ≥ MC[i] + daň → zvyš Q* o 1
   - Jakmile WTP[i] < MC[i] + daň → zastav
4. P* = průměr posledního platného páru: (WTP[Q*] + MC[Q*] + daň) / 2
```

**Příklad** (4 kupující, 4 prodávající):

| Pořadí | WTP (sestupně) | MC (vzestupně) | WTP ≥ MC? |
|---|---|---|---|
| 1 | 90 | 15 | ✅ obchod |
| 2 | 75 | 35 | ✅ obchod |
| 3 | 55 | 50 | ✅ obchod |
| 4 | 40 | 60 | ❌ stop |

→ Q* = 3 obchody, P* = (55 + 50) / 2 = **52.5 Kč**

Tato rovnovážná cena se zobrazuje v admin panelu a v grafu konvergence, aby lektor mohl demonstrovat, jak se tržní ceny přibližují teoretické rovnováze.

---

## Tržní šoky

Lektor může mezi koly (nebo i během obchodování) zavést:

### Daň (tax)
- Jednotková daň v Kč na každou transakci
- Dělí se 50:50 mezi kupujícího a prodávajícího
- Posouvá efektivní nabídkovou křivku nahoru → snižuje Q*, mění P*
- **Pedagogický cíl:** daňová incidence, deadweight loss

### Cenová podlaha (price floor)
- Minimální povolená cena obchodu
- Pokud je nad rovnováhou → vzniká přebytek (excess supply)
- **Pedagogický cíl:** minimální mzda, regulace cen zemědělských produktů

### Cenový strop (price ceiling)
- Maximální povolená cena obchodu
- Pokud je pod rovnováhou → vzniká nedostatek (excess demand)
- **Pedagogický cíl:** regulované nájemné, cenové stropy na energie

---

## WebSocket komunikace

Veškerá komunikace probíhá přes WebSocket (full-duplex). Protokol je JSON.

### Zprávy klient → server

| Akce | Odesílatel | Popis |
|---|---|---|
| `{"action": "bid", "price": 55}` | Student | Zadání cenové nabídky |
| `{"action": "start_round"}` | Lektor | Spuštění nového kola |
| `{"action": "end_round"}` | Lektor | Ukončení kola |
| `{"action": "set_tax", "value": 10}` | Lektor | Nastavení daně |
| `{"action": "set_floor", "value": 40}` | Lektor | Nastavení cenové podlahy |
| `{"action": "set_ceiling", "value": 60}` | Lektor | Nastavení cenového stropu |
| `{"action": "reset"}` | Lektor | Reset celé hry v místnosti |

### Zprávy server → klient (broadcast)

| Typ | Popis |
|---|---|
| `welcome` | Potvrzení připojení + přidělená role a hodnota |
| `state` | Kompletní stav hry (kolo, hráči, žebříček, obchody...) |
| `trade` | Nově spárovaný obchod |
| `round_start` / `round_end` | Změna fáze |
| `shock` | Oznámení o tržním šoku |
| `error` | Chybová zpráva (jen danému hráči) |
| `reset` | Hra byla resetována |

---

## Pokrytá témata Mikroekonomie 1

| Kolo | Doporučený šok | Téma |
|---|---|---|
| 1–2 | Žádný | Nabídka, poptávka, tržní rovnováha, konvergence cen |
| 3 | Daň 15 Kč | Daňová incidence, kdo nese daň, deadweight loss |
| 4 | Cenový strop (pod P*) | Nedostatek, regulace nájemného |
| 5 | Cenová podlaha (nad P*) | Přebytek, minimální mzda |

---

## Technické požadavky

- **Server:** Python 3.11+, FastAPI, uvicorn, websockets
- **Klient:** Moderní prohlížeč (Chrome, Firefox, Safari, Edge)
- **Hosting:** Render.com (free tier), Railway, nebo vlastní VPS
- **Databáze:** Není potřeba – stav je v paměti (reset při restartu serveru)

---

## Omezení aktuální verze

- Stav hry je v paměti → restart serveru = ztráta dat
- Není autentizace lektora (kdokoliv může otevřít `/admin/{room}`)
- Jeden obchod na hráče na kolo (jedna jednotka zboží)
- WTP a MC jsou náhodné → nelze předem nastavit konkrétní křivky

## Možná rozšíření

- Export výsledků do CSV
- Autentizace lektora (heslo pro admin panel)
- Konfigurovatelné rozsahy WTP/MC
- Více jednotek na hráče (multi-unit auction)
- AI boti pro doplnění počtu hráčů
- Vizualizace S/D křivek v reálném čase
