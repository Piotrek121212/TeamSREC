# TeamSREC — Automatyczny Rejestrator Spotkań Microsoft Teams i Organizator Kursów (.ics)

**TeamSREC** to minimalistyczna aplikacja w ciemnym motywie działająca w zasobniku systemowym Windows (**System Tray**), która automatycznie wykrywa aktywne spotkania w **Microsoft Teams**, nagrywa dźwięk ze spotkania (**WASAPI Loopback** ze słuchawek/głośników) oraz **Twój mikrofon**, łączy je w jeden plik `.mp3` przez FFmpeg i porządkuje nagrania w folderach według **Przedmiotu** oraz **Daty** (`RRRR-MM-DD`).

---

## Szybki Start

1. **Uruchomienie aplikacji:**
   Kliknij dwukrotnie skrót **`TeamSREC`** (`TeamSREC.lnk`) w folderze `C:\Users\polom\Documents\TeamsRec` (lub na Pulpicie).
2. **Działanie w tle (System Tray):**
   - Zamknięcie okna krzyżykiem (`X`) lub przyciskiem **`⬇ Do paska (Tray)`** chowa aplikację do rozwijanego zasobnika systemowego Windows (obok zegara).
   - Kliknięcie **lewym przyciskiem myszy** w ikonę mikrofonu w zasobniku (lub ponowne kliknięcie skrótu `TeamSREC`) przywraca okno na ekran.
   - Wbudowana blokada **Single-Instance Lock** gwarantuje, że w tle działa zawsze tylko jedna instancja programu.

---

## Najważniejsze funkcje

### 1. Inteligentna detekcja spotkań Microsoft Teams
- Program odróżnia zwykłe przeglądanie czatu, kalendarza lub kanałów w Teams od **faktycznego udziału w spotkaniu** (sprawdza aktywność strumienia mikrofonu w systemie Windows, wyskakujące okno spotkania oraz aktywne strumienie mediów RTP/UDP).
- Nagrywanie rozpoczyna się automatycznie po dołączeniu do rozmowy/wykładu (z 6-sekundowym buforem stabilności) i kończy po opuszczeniu spotkania (z 20-sekundowym buforem na wypadek chwilowego zerwania połączenia).

### 2. Kalendarz zajęć `.ics` (z obsługą kursów co 2 tygodnie)
- Wczytuje plan zajęć z pliku `.ics` (np. wyeksportowanego z USOS) i domyślnie filtruje wyłącznie **zajęcia zdalne** (`Budynek sal wirtualnych` / `Sala: W_0RBI` / `Teams` / `Online`).
- Automatycznie rozpoznaje zajęcia odbywające się **co tydzień**, **co 2 tygodnie** oraz **pojedyncze terminy**, dzięki czemu w „wolne tygodnie” nie przypisuje omyłkowo nieodbywających się kursów.
- Na kafelkach zajęć w widoku tygodniowym od razu widać stan nagrań:
  - **`✔ MAM NAGRANIE`** (zielony) — nagranie `.mp3` istnieje w folderze przedmiotu (można je od razu odtworzyć przyciskiem **`▶ Odtwórz`**),
  - **`✖ BRAK NAGRANIA`** (czerwony) — termin zajęć już minął, ale nie ma z niego nagrania,
  - **`🕒 W PLANIE`** (niebieski) — nadchodzące zajęcia,
  - **`🚫 ODWOŁANE`** (szary) — zajęcia oznaczone jako odwołane.

### 3. Wklejanie i wczytywanie własnych plików `.ics`
Kliknij przycisk **`📅 Wczytaj / Wklej kalendarz .ics`** na dolnym pasku, aby:
- Wkleić skopiowany plik `.ics` bezpośrednio ze schowka Windows (`Ctrl+C` na pliku w Eksploratorze -> **`📋 Wklej ze schowka`**),
- Wkleić surową zawartość tekstową `BEGIN:VCALENDAR ... END:VCALENDAR` lub link URL do kalendarza,
- Wybrać plik `.ics` z dysku przyciskiem **`📂 Wybierz plik z dysku...`**.

### 4. Zastępstwa i korekty kursów
- **`🔁 Zastępstwo / Edytuj`** (na karcie zajęć): pozwala zmienić przedmiot przypisany do konkretnego terminu, dodać notatkę lub oznaczyć zajęcia jako odwołane. Jeśli dla tych zajęć istnieje już nagrany plik `.mp3`, program automatycznie przeniesie go do folderu nowego kursu.
- **Zmiana przedmiotu w locie (na górnym pasku)**: pozwala przełączyć docelowy przedmiot w trakcie trwającego nagrywania.
- **`➕ Dodaj zajęcia`**: pozwala dopisać do kalendarza dodatkowe zajęcia lub odrabianie w dowolnym terminie.

---

## Struktura plików projektu (`C:\Users\polom\Documents\TeamsRec`)

```text
C:\Users\polom\Documents\TeamsRec\
├── TeamSREC.lnk          # Główny skrót uruchamiający aplikację (z ikoną mikrofonu)
├── teamsrec.ico          # Ikona aplikacji i zasobnika systemowego
├── gui_app.py            # Aplikacja okienkowa (System Tray + Kalendarz + Sterowanie)
├── main.py               # Punkt wejścia oraz narzędzia wiersza poleceń (CLI)
├── teams_detector.py     # Moduł wykrywania aktywnych spotkań Microsoft Teams
├── audio_recorder.py     # Rejestrator WASAPI Loopback + Mikrofon oraz mikser FFmpeg MP3
├── course_organizer.py   # Parser .ics, obsługa zastępstw i organizacja folderów
├── config.json           # Konfiguracja kursów, ścieżek, zastępstw i ustawień audio
├── README.md             # Dokumentacja programu
└── recordings\           # Katalog zapisywanych nagrań MP3 (<Przedmiot>/<RRRR-MM-DD>/)
```

---

## Przydatne komendy terminalowe (opcjonalne)

```powershell
cd C:\Users\polom\Documents\TeamsRec

# Uruchomienie aplikacji okienkowej:
python gui_app.py

# Uruchomienie aplikacji od razu schowanej w zasobniku systemowym (System Tray):
pythonw.exe gui_app.py --minimized

# Sprawdzenie statusu urządzeń audio WASAPI, FFmpeg i najbliższych zajęć zdalnych:
python main.py --status

# Wyświetlenie tabeli wszystkich zajęć zdalnych z pliku .ics:
python main.py --remote-schedule

# Krótki 4-sekundowy test nagrywania i miksowania do MP3:
python main.py --test-audio --duration 4
```
