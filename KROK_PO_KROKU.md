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
| `src/fetch_bemovo.py` | Pobiera i łączy ceny, cechy oraz pełny spis mieszkań Bemovo; zapisuje CSV i raport |
| `src/bemovo_prices.py` | Czyta cennik z dane.gov.pl i sprawdza dewelopera, adres oraz daty ważności |
| `src/bemovo_features.py` | Czyta metraż, pokoje, piętro i dostępność z danych publicznej strony Bemovo |
| `src/fetch_data.py` | Ogólny parser JSON-LD, przydatny do innych źródeł po ich sprawdzeniu |
| `src/database.py` | Łączy się z bazą oraz zapisuje ostatnie znane ceny i ich historię |
| `src/availability.py` | Zapisuje pełny spis mieszkań, bieżące statusy i historię dostępności razem z cenami |
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

Otwórz `data/bemovo.csv`: każdy wiersz to dostępne mieszkanie z ceną. W `data/bemovo_audit.json` są adresy źródeł, data cennika, czas pobrania, wyniki kontroli oraz `inventory` — pełny spis mieszkań ze statusami. Pierwsza zweryfikowana próba z **5 października 2026** dała **120 dostępnych mieszkań i zero rozbieżności cen**. Strona zawierała też trzy sprzedane mieszkania oraz dwa sprzedane lokale usługowe. Lokale usługowe pomijamy w spisie mieszkań. Te liczby dotyczą tej konkretnej daty; nowe pobranie może dać inne wyniki.

Cena dotyczy mieszkania brutto, bez parkingu i dodatków. Metrażu nie wyliczamy z ceny. Sprzedane i zarezerwowane mieszkania nie trafiają do CSV z dostępnymi ofertami, ale ich statusy trafiają do raportu. Brakujących współrzędnych i roku budowy nie wymyślamy. Szczegóły źródła opisuje [docs/SOURCE_BEMOVO.md](docs/SOURCE_BEMOVO.md).

Cennik państwowy musi mieć datę zgodną z dniem pobrania w Warszawie. Dla pobrania 6 października cennik z 5 października jest za stary: zwykłe uruchomienie zatrzyma się bez zmiany zapisanych danych. Żeby mimo opóźnionego cennika sprawdzić dostępność, uruchom:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo --availability-only
```

Ten tryb pobiera jedną publiczną stronę HTML i sprawdza pełny spis mieszkań. Zapisuje statusy do `data/bemovo_availability.csv` i raport do `data/bemovo_availability_audit.json`. Nie pobiera cennika państwowego ani nie porównuje cen. Zachowuje wcześniejszy `data/bemovo.csv` i raport cen, a historii cen nie dopisuje. Czas w nowym raporcie oznacza rzeczywiste sprawdzenie dostępności.

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

Parametr `--save-db` zapisuje ceny dostępnych mieszkań oraz statusy całego sprawdzonego spisu w jednej transakcji. Albo zapiszą się wszystkie te dane, albo żadne. Raport dostaje też `database_availability_counts`, czyli liczby statusów po zapisie. `src.database init` dodaje brakujące tabele; nie zmienia ani nie usuwa dotychczasowych tabel cen.

Po skonfigurowaniu bazy możesz zapisać również samą dostępność:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo --availability-only --save-db
```

Ten zapis obejmuje statusy i potwierdzenie pełnego pobrania w jednej transakcji. Ostatnie znane ceny i ich czasy obserwacji pozostają takie jak wcześniej. W `inventory_snapshots` pole `prices_complete` rozróżnia pełne pobranie cen i statusów (`true`) od pobrania samych statusów (`false`). Pełny spis mieszkań jest wymagany w obu trybach.

W bazie rozdzielamy dwa rodzaje informacji:

| Tabele | Co zawierają |
| --- | --- |
| `listings`, `listing_observations` | Ostatnie znane ceny ofertowe i historię cen |
| `inventory_snapshots` | Potwierdzenie udanego pobrania pełnego spisu z czasem obserwacji |
| `listing_availability`, `listing_availability_observations` | Ostatnie statusy mieszkań i historię statusów |

Status `available` oznacza dostępne, `reserved` zarezerwowane, a `sold` sprzedane według źródła. `missing` oznacza, że wcześniej znanego mieszkania zabrakło w następnym **pełnym** spisie tej samej inwestycji. Nie oznacza potwierdzonej sprzedaży. Gdy mieszkanie wróci jako dostępne, status znów będzie `available`. Nie wyciągamy takich wniosków z przerwanego lub niepełnego pobrania.

Sprawdzenie z **6 października 2026** zapisało w Supabase 120 mieszkań dostępnych i 3 sprzedane. Cennik z dane.gov.pl był nadal z 5 października, więc użyliśmy trybu `--availability-only`: historia cen i jej daty pozostały bez zmian.

Nieprawidłowy lub niepełny spis nie zmienia zapisanych cen ani statusów. Powtórzenie identycznego spisu z tym samym czasem UTC niczego nie dopisuje. Inna zawartość pod tym samym czasem albo nowe pobranie starsze od ostatniego zostają odrzucone. Pełny spis zawierający wyłącznie mieszkania sprzedane lub zarezerwowane jest poprawny, nawet jeśli CSV dostępnych ofert będzie pusty.

Historia cen pozostaje po sprzedaży. Nadal są to dawne **ceny ofertowe**, nie ceny zakończonych transakcji. Jeśli mieszkanie widzimy pierwszy raz jako sprzedane, zapisujemy jego status bez wymyślania ceny. Kolejne pobranie cen ma nowy rzeczywisty czas obserwacji, nawet jeśli cena się nie zmieniła. Tryb samej dostępności dopisuje wyłącznie obserwacje statusów.

Przy przygotowaniu najnowszych kandydatów i wycenie program pomija śledzone mieszkania sprzedane, zarezerwowane i brakujące. Do nauki modelu zachowuje ich wcześniejsze obserwacje cen. Ogólny adapter `src.fetch_data` bez statusów nadal nie potrafi oznaczyć znikającej oferty jako niedostępnej; filtr obejmuje źródła z potwierdzonym śledzeniem dostępności.

Na tym etapie nie trenujemy jeszcze modelu rynku Warszawy: jeden dzień i jedna inwestycja nie wystarczą do wiarygodnego sprawdzenia jakości. Śledzenie dostępności jest już zaimplementowane. Następne kroki to kolejne inwestycje, rzeczywista historia pobrań i połączenie adaptera z codziennym harmonogramem.

## 6. Dopiero wtedy uruchom GitHub

Repozytorium jest już opublikowane: [filip-skopczynski/real-estate-mlops](https://github.com/filip-skopczynski/real-estate-mlops). Testy uruchamiają się po zmianach kodu. **Na razie nie ustawiaj `PIPELINE_ENABLED=true`** — zadanie produkcyjne korzysta z ogólnego parsera JSON-LD, a pilotaż Bemovo uruchamiamy ręcznie. Najpierw połączymy harmonogram z adapterem i atomowym zapisem cen oraz dostępności. Pozostałe ustawienia są opisane w README.

Po podłączeniu źródła codzienny harmonogram będzie aktualizował obserwacje w bazie. Gdy zbierzemy odpowiednio różnorodne dane i historię, przejdziemy do modelu i oceny jego błędów. To codzienne odświeżanie danych; zbieranie w każdej sekundzie wymagałoby innej architektury.
