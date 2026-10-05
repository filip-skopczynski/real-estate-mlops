# Pierwsze uruchomienie i nauka projektu

Ten przewodnik pokazuje pierwsze uruchomienie projektu po pobraniu lub sklonowaniu repozytorium. Repozytorium nie zawiera `.env` ani danych dostępu. Najpierw uruchom lokalne demo; połączenie z własnym projektem Supabase skonfigurujesz w dalszej części. Źródło prawdziwych ofert i harmonogram GitHub wymagają osobnej konfiguracji.

## 1. Otwórz właściwy folder

W VS Code wybierz **File → Open Folder** i wskaż `real-estate-mlops`. Wybierz **Terminal → New Terminal**. Komendy niżej wpisuj w terminalu; skrypty korzystają z plików i parametrów, więc nie pytają o metraż przez `input()` tak jak wcześniejsza `wycena.py`.

## 2. Przygotuj Python

Użyj Pythona 3.12. Jeśli `py -3.12` go nie znajduje, zainstaluj tę wersję Pythona i otwórz ponownie terminal. Następnie:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

`.venv` to oddzielne biblioteki tego projektu. Dzięki pełnej ścieżce do Pythona nie musisz aktywować środowiska. W notebooku wybierz ten sam interpreter.

## 3. Uruchom dane demonstracyjne

```powershell
.\.venv\Scripts\python.exe -m tests.demo_data
.\.venv\Scripts\python.exe -m src.preprocess --input data/demo_observations.csv
.\.venv\Scripts\python.exe -m src.train
```

Pierwsza komenda tworzy fikcyjne oferty. Druga sprawdza dane i tworzy dwa pliki: najwcześniejsze obserwacje do nauki i najnowsze do szukania kandydatów. Trzecia uczy model, sprawdza jego błędy, zapisuje go oraz tworzy `data/deals.csv`.

W `models/metrics.json` porównaj `xgboost.mae_pln` z `baseline_median.mae_pln`. MAE opisuje przeciętny błąd bezwzględny w złotych. Model porównujemy z prostym punktem odniesienia, żeby ustalić, czy uczenie wnosi coś użytecznego. Demo służy sprawdzeniu programu; nie ocenia rynku Warszawy.

## 4. Poznaj pliki w kolejności przepływu danych

| Plik | Za co odpowiada |
| --- | --- |
| `src/fetch_data.py` | Pobiera HTML i zamienia dane oferty na uporządkowany rekord |
| `src/database.py` | Łączy się z bazą oraz zapisuje bieżące oferty i historię |
| `src/preprocess.py` | Czyści dane, usuwa powtórzenia i przygotowuje CSV |
| `src/train.py` | Rozdziela dane, uczy XGBoost, mierzy błędy i ocenia najnowsze oferty |
| `notebooks/01_eda.ipynb` | Pomaga zrozumieć rozkłady danych oraz brakujące wartości |
| `.github/workflows/scraper_pipeline.yml` | Uruchamia testy i później codzienny przepływ na GitHub |
| `.env.example` | Pokazuje ustawienia; prawdziwe wartości trzymasz w lokalnym `.env` |

Najpierw zrozum pojedynczy rekord oferty, potem zapis w bazie. Na końcu przejdziemy przez trening. To ułatwi Ci wyjaśnienie projektu na rozmowie rekrutacyjnej.

## 5. Podłącz prawdziwe dane

Potrzebujemy wskazanego portalu i adresu wyszukiwania **sprzedaży mieszkań w Warszawie**. Obecny parser czyta JSON-LD (dane strukturalne w HTML). Konkretny portal może wymagać innego adaptera; nie należy uruchamiać harmonogramu, dopóki nie sprawdzimy poprawności jego rekordów.

Otwórz swój projekt Supabase → **Connect** → sekcja **Direct** → **Session pooler** → **URI**. Użyj skopiowanego adresu z portem `5432` i `sslmode=require`. Session pooler obsługuje IPv4. Kod sam zamienia standardowy początek `postgresql://` lub `postgres://` na sterownik SQLAlchemy. [Dokumentacja połączeń Supabase](https://supabase.com/docs/guides/database/connecting-to-postgres)

Jeśli nie masz `.env`, skopiuj `.env.example`. Jeśli plik już istnieje, edytuj go zamiast nadpisywać. Ustaw w nim `DATABASE_URL` oraz sprawdzony `LISTINGS_URL`. Hasło wpisz lokalnie; znaki specjalne w haśle wymagają kodowania dla URI. Przykład zawiera wyłącznie pola do zastąpienia:

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres.PROJECT_REF:YOUR_PASSWORD@POOLER_HOST:5432/postgres?sslmode=require
```

Ustawienia wybrane dla tego projektu to: **Data API wyłączone**, **Automatically expose new tables wyłączone**, **Automatic RLS zaznaczone**. Potwierdź je w panelu Supabase; ich stanu na serwerze projekt jeszcze nie zweryfikował. Program używa połączenia PostgreSQL i nie potrzebuje kluczy API Supabase. [Zabezpieczenia API Supabase](https://supabase.com/docs/guides/api/securing-your-api)

Program odczytuje `DATABASE_URL`. Opcjonalne `SUPABASE_DB_PASSWORD` służy lokalnym narzędziom konfiguracyjnym i samo nie ustawia połączenia. Hasła nie wklejaj do rozmowy ani repozytorium. Następnie wykonaj:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
# Najpierw uzupełnij .env lokalnie.
.\.venv\Scripts\python.exe -m src.database init
.\.venv\Scripts\python.exe -m src.database check
.\.venv\Scripts\python.exe -m src.fetch_data --no-db --output data/source_check.csv
```

Sprawdź cenę całkowitą w PLN, metraż, pokoje, miasto i adresy ofert. Potem usuń `--no-db`, przygotuj dane i uruchom trening. Jeśli masz tylko jeden dzień obserwacji, domyślny trening czasowy zgłosi brak historii. Opcja `--split group` pozwala na pierwszy eksperyment, ale nie udaje sprawdzenia modelu na przyszłych danych.

## 6. Dopiero wtedy uruchom GitHub

Utwórz repozytorium z gałęzią `main` i wgraj zawartość tego folderu do jego katalogu głównego. W ustawieniach Actions dodaj sekret `DATABASE_URL` i zmienną `LISTINGS_URL`. Po sprawdzeniu źródła ustaw `PIPELINE_ENABLED=true`. Pozostałe nazwy i kroki są opisane w README.

Codzienny harmonogram aktualizuje obserwacje w bazie. Gdy historia będzie wystarczająca, tworzy model i raport do pobrania z Actions. To codzienne odświeżanie danych; zbieranie w każdej sekundzie wymagałoby innej architektury.
