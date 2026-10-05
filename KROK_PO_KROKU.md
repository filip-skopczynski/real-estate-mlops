# Pierwsze uruchomienie i nauka projektu

Ten przewodnik pokazuje pierwsze uruchomienie projektu po pobraniu lub sklonowaniu repozytorium. Repozytorium nie zawiera `.env` ani danych dostępu. Najpierw uruchom lokalne demo; połączenie z własnym projektem Supabase skonfigurujesz w dalszej części. Mamy też sprawdzone źródło prawdziwych mieszkań: pilotaż inwestycji Bemovo. Codzienny harmonogram nie jest jeszcze z nim połączony.

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
| `src/fetch_bemovo.py` | Pobiera i łączy rzeczywiste ceny oraz cechy mieszkań Bemovo; zapisuje CSV i raport |
| `src/bemovo_prices.py` | Czyta cennik z dane.gov.pl i sprawdza dewelopera, adres oraz daty ważności |
| `src/bemovo_features.py` | Czyta metraż, pokoje, piętro i dostępność z danych publicznej strony Bemovo |
| `src/fetch_data.py` | Ogólny parser JSON-LD, przydatny do innych źródeł po ich sprawdzeniu |
| `src/database.py` | Łączy się z bazą oraz zapisuje bieżące oferty i historię |
| `src/preprocess.py` | Czyści dane, usuwa powtórzenia i przygotowuje CSV |
| `src/train.py` | Rozdziela dane, uczy XGBoost, mierzy błędy i ocenia najnowsze oferty |
| `notebooks/01_eda.ipynb` | Pomaga zrozumieć rozkłady danych oraz brakujące wartości |
| `.github/workflows/scraper_pipeline.yml` | Uruchamia testy i później codzienny przepływ na GitHub |
| `.env.example` | Pokazuje ustawienia; prawdziwe wartości trzymasz w lokalnym `.env` |

Najpierw zrozum pojedynczy rekord oferty, potem zapis w bazie. Na końcu przejdziemy przez trening. To ułatwi Ci wyjaśnienie projektu na rozmowie rekrutacyjnej.

## 5. Podłącz prawdziwe dane

Zaczynamy od mieszkań deweloperskich w inwestycji **Bemovo w Warszawie**. Cennik pochodzi z [dane.gov.pl](https://dane.gov.pl/pl/dataset/39940), a metraż, liczba pokoi, piętro i dostępność z [publicznej strony inwestycji](https://bemovo.pl/pl/). Łączymy je po numerze mieszkania. Program sprawdza również, czy obie strony podają zgodną cenę.

Najpierw pobierz pliki lokalnie — bez połączenia z bazą:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo
```

Otwórz `data/bemovo.csv`: każdy wiersz to dostępne mieszkanie. W `data/bemovo_audit.json` są adresy źródeł, data cennika, czas pobrania i wyniki kontroli. Pierwsza zweryfikowana próba z 5 października 2026 dała **120 mieszkań i zero rozbieżności cen**. Liczba może zmieniać się z dostępnością lokali.

Cena dotyczy mieszkania brutto, bez parkingu i dodatków. Metrażu nie wyliczamy z ceny. Lokale sprzedane i usługowe nie trafiają do wyniku. Brakujących współrzędnych i roku budowy nie wymyślamy. Szczegóły źródła opisuje [docs/SOURCE_BEMOVO.md](docs/SOURCE_BEMOVO.md).

Otwórz swój projekt Supabase → **Connect** → sekcja **Direct** → **Session pooler** → **URI**. Użyj skopiowanego adresu z portem `5432` i `sslmode=require`. Session pooler obsługuje IPv4. Kod sam zamienia standardowy początek `postgresql://` lub `postgres://` na sterownik SQLAlchemy. [Dokumentacja połączeń Supabase](https://supabase.com/docs/guides/database/connecting-to-postgres)

Jeśli nie masz `.env`, skopiuj `.env.example`. Jeśli plik już istnieje, edytuj go zamiast nadpisywać. Ustaw w nim `DATABASE_URL`. Pilotaż Bemovo ma adresy źródeł w swoim adapterze i nie potrzebuje `LISTINGS_URL`. Hasło wpisz lokalnie; znaki specjalne w haśle wymagają kodowania dla URI. Przykład zawiera wyłącznie pola do zastąpienia:

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
.\.venv\Scripts\python.exe -m src.fetch_bemovo --save-db
```

Parametr `--save-db` zapisuje sprawdzone mieszkania również w Supabase. `listings` pokazuje ostatnią zapisaną wersję lokalu, a `listing_observations` historię pobrań. Kolejne pobranie ma nowy rzeczywisty czas obserwacji, nawet jeśli cena się nie zmieniła.

Na tym etapie nie trenujemy jeszcze modelu rynku Warszawy: jeden dzień i jedna inwestycja nie wystarczą do wiarygodnego sprawdzenia jakości. Następny krok to kolejne inwestycje, historia i oznaczanie mieszkań, które przestały być dostępne. Obecna baza nie oznacza automatycznie dawnych ofert jako nieaktywnych.

## 6. Dopiero wtedy uruchom GitHub

Repozytorium jest już opublikowane: [filip-skopczynski/real-estate-mlops](https://github.com/filip-skopczynski/real-estate-mlops). Testy uruchamiają się po zmianach kodu. **Na razie nie ustawiaj `PIPELINE_ENABLED=true`** — zadanie produkcyjne korzysta z ogólnego parsera JSON-LD. Najpierw połączymy je z adapterem Bemovo i obsłużymy dostępność mieszkań. Pozostałe ustawienia są opisane w README.

Po podłączeniu źródła codzienny harmonogram będzie aktualizował obserwacje w bazie. Gdy zbierzemy odpowiednio różnorodne dane i historię, przejdziemy do modelu i oceny jego błędów. To codzienne odświeżanie danych; zbieranie w każdej sekundzie wymagałoby innej architektury.
