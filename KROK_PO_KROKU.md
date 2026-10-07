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
| `src/fetch_olx.py` | Podgląd jednej strony OLX oraz osobna funkcja do ograniczonego pobierania kolejnych stron |
| `src/olx.py` | Wyciąga i sprawdza cechy mieszkań z pobranego HTML OLX; sam nie łączy się z internetem |
| `src/fetch_otodom.py` | Podgląd jednej strony Otodom oraz osobna funkcja do ograniczonego pobierania kolejnych stron |
| `src/otodom.py` | Wyciąga i sprawdza cechy mieszkań z pobranego HTML Otodom; sam nie łączy się z internetem |
| `src/database.py` | Łączy się z bazą oraz zapisuje ostatnie znane ceny i ich historię |
| `src/daily_listings.py` | Łączy ograniczone pobrania OLX i Otodom, zapisuje raport i opcjonalnie historię cen w bazie |
| `src/availability.py` | Zapisuje pełny spis mieszkań, bieżące statusy i historię dostępności razem z cenami |
| `src/preprocess.py` | Czyści dane, usuwa powtórzenia i przygotowuje CSV |
| `src/train.py` | Rozdziela dane, uczy XGBoost, mierzy błędy i ocenia najnowsze oferty |
| `notebooks/01_eda.ipynb` | Pomaga zrozumieć rozkłady danych oraz brakujące wartości |
| `.github/workflows/scraper_pipeline.yml` | Uruchamia testy i później codzienny przepływ na GitHub |
| `.github/workflows/daily_portals.yml` | Osobny harmonogram obserwacji OLX i Otodom, włączany zmienną GitHuba |
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

Repozytorium jest już opublikowane: [filip-skopczynski/real-estate-mlops](https://github.com/filip-skopczynski/real-estate-mlops). Testy uruchamiają się po zmianach kodu. **Na razie nie ustawiaj `PIPELINE_ENABLED=true`** — stare zadanie produkcyjne korzysta z ogólnego parsera JSON-LD. Nie uruchamia adapterów Bemovo, OLX ani Otodom. Dla obserwacji portali dodaliśmy osobny harmonogram, opisany w punkcie 8; nie jest jeszcze włączony w ustawieniach GitHuba. Pozostałe ustawienia są opisane w README.

Po podłączeniu źródła codzienny harmonogram będzie aktualizował obserwacje w bazie. Gdy zbierzemy odpowiednio różnorodne dane i historię, przejdziemy do modelu i oceny jego błędów. To codzienne odświeżanie danych; zbieranie w każdej sekundzie wymagałoby innej architektury.

## 7. OLX i Otodom

Wybraliśmy wersję bez agenta językowego i bez OpenAI API. Pobieranie i przetwarzanie wykonuje Python, a wycenę XGBoost.

### Podgląd OLX

Pierwsze sprawdzenie publicznych stron z 7 października 2026 zakończyło się poprawnymi odpowiedziami HTTP, ale ogólny parser JSON-LD nie odczytał kompletnych rekordów mieszkań. Dodaliśmy osobny parser OLX do lokalnego podglądu jednej strony z mieszkaniami na sprzedaż w Warszawie:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_olx --max-listings 20 --delay 2
# Przykład bez sieci, na fikcyjnych ofertach z testów:
.\.venv\Scripts\python.exe -m src.fetch_olx --html tests/fixtures/olx_search.html --output data/olx_offline_preview.csv --audit data/olx_offline_audit.json
```

Otwórz `data/olx_preview.csv` i porównaj cenę, metraż, pokoje oraz link z ofertą. W `data/olx_preview_audit.json` znajdziesz raport pobrania i odczytu. Do tego podglądu nie konfigurujesz `.env` ani Supabase. Program nie dopisuje obserwacji do bazy i nie uruchamia modelu.

Pierwsze pobranie na żywo 7 października 2026 zapisało lokalnie 20 ofert. Niezależnie porównaliśmy ceny i metraż 43 rekordów ze starszej próbki HTML z widocznymi kartami tej samej strony — wszystkie się zgadzały. To sprawdza poprawność odczytu, a nie jakość modelu ani prawdziwość informacji sprzedającego.

Domyślny limit to 20 rekordów, maksymalny 100. Opóźnienie musi wynosić co najmniej dwie sekundy. Program czyta jedną publiczną stronę HTML, bez logowania, API, proxy, stron szczegółów i przechodzenia do kolejnych stron wyników. Uwzględnia robots.txt; odpowiedź 403/429 zatrzymuje pobieranie. Parametr `--html` pozwala odczytać zapisany HTML bez sieci, a `--output` i `--audit` zmienić nazwy lokalnych plików.

Parser czyta dane ofert osadzone w HTML jako `__PRERENDERED_STATE__`, bez wykonywania JavaScriptu. Sprawdza kategorię sprzedaży, Warszawę, cenę całkowitą w PLN, metraż i pokoje. Pojedyncze niepoprawne rekordy pomija; brak poprawnych ofert kończy się błędem. OLX zapisuje `four` jako **„4 i więcej”**, więc parser zachowuje tylko pewne liczby: 1, 2 i 3 pokoje. Oferty 4+ pomija, co przesuwa próbkę w stronę mieszkań z mniejszą liczbą pokoi. Nie odgadujemy dokładnej liczby z opisu ani przy pomocy LLM. Linków do Otodom nie odwiedza.

To próbka, więc nie opisuje całego katalogu Warszawy. Brak ogłoszenia w próbce nie oznacza sprzedaży ani niedostępności. Warunki wykorzystania danych do treningu modelu nadal wymagają ustalenia. Szczegóły opisują [źródło OLX](docs/SOURCE_OLX.md) i [instrukcja OLX i Otodom](docs/OLX_OTODOM.md).

### Podgląd Otodom

Analogicznie uruchom osobny podgląd jednej strony z mieszkaniami na sprzedaż w Warszawie:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_otodom --max-listings 20 --delay 2
# Przykład bez sieci, na fikcyjnych ofertach z testów:
.\.venv\Scripts\python.exe -m src.fetch_otodom --html tests/fixtures/otodom_search.html --output data/otodom_offline_preview.csv --audit data/otodom_offline_audit.json
```

Otwórz `data/otodom_preview.csv` i raport `data/otodom_preview_audit.json`. Parser odczytuje JSON `__NEXT_DATA__` osadzony w publicznym HTML. Sprawdza, czy strona i konkretne oferty dotyczą sprzedaży mieszkań w Warszawie. Cena całkowita w PLN, metraż oraz dokładna liczba pokoi są wymagane. Cena i metraż muszą być liczbami w źródle, bez odgadywania wartości z tekstu. Karty inwestycji i oferty z ukrytą ceną pomija. Nie odtwarza metrażu z ceny za metr ani brakujących cech z opisów.

Pierwsze pobranie na żywo 7 października 2026 zapisało 18 unikalnych ofert. W osobnym odczycie wcześniejszego HTML porównaliśmy ceny, metraż i liczbę pokoi 17 mieszkań z widocznymi kartami — wszystkie dane się zgadzały. Parser pomija także reklamowe kopie HPR, aby nie traktować innej prezentacji tego samego ogłoszenia jako nowego mieszkania.

Obsługiwane wartości pokoi to `ONE`, `TWO`, `THREE` i `FOUR`, czyli dokładnie 1–4. Otodomowe `FOUR` sprawdziliśmy na karcie opisanej jako „4 pokoje”. Nieznane wartości są pomijane. Piętro od parteru do dziesiątego zachowujemy jako liczbę; „powyżej dziesiątego” i poddasze pozostają puste. W tym formacie nie zapisujemy współrzędnych, odległości ani roku budowy. Granice Warszawy obecne w danych strony nie są lokalizacją konkretnego mieszkania.

Także tutaj limit wynosi domyślnie 20 rekordów, maksymalnie 100, a opóźnienie co najmniej dwie sekundy. Nie potrzebujesz konta, API, `.env` ani Supabase. Program nie wykonuje JavaScriptu, nie korzysta z proxy i nie pobiera stron szczegółów ani kolejnych stron wyników. Sprawdza robots.txt i zatrzymuje się na 403/429. To lokalny CSV i raport, bez zapisu do bazy, modelu czy harmonogramu.

Próbka Otodom również nie jest pełnym katalogiem i nie pozwala uznać zniknięcia oferty za sprzedaż. robots.txt zawiera `search=yes, ai-input=no, ai-train=no`; nie ustaliliśmy uprawnień do treningu modelu na tych ofertach. Dokładny zakres parsera opisuje [źródło Otodom](docs/SOURCE_OTODOM.md). Powyższe polecenia są nadal podglądami jednej strony. Nowe pobieranie wielu stron uruchomisz osobnym poleceniem poniżej.

## 8. Codzienne obserwacje OLX i Otodom

Najpierw uruchom lokalnie bez zapisu do bazy:

```powershell
.\.venv\Scripts\python.exe -m src.daily_listings
```

Otwórz `data/daily_listings.csv` i `data/daily_collection_audit.json`. Domyślnie program sprawdza do pięciu publicznych stron każdego portalu i zapisuje do 500 poprawnych rekordów na źródło. To Warszawa i sprzedaż mieszkań, a nie cały serwis. OLX ogranicza także samo wyszukiwanie; domyślna kolejność ofert nie jest potwierdzona jako kolejność nowych publikacji. Raport pokazuje zakres pobrania i przerwane źródła.

Po sprawdzeniu próbki i konfiguracji swojego `.env` możesz dopisać historię do Supabase:

```powershell
.\.venv\Scripts\python.exe -m src.daily_listings --save-db
```

„Nowa oferta” w raporcie to pierwszy zapis identyfikatora w naszej bazie. Stare ogłoszenie pierwszy raz zauważone dziś też jest dla nas nowe. Znane identyfikatory aktualizują ostatnią cenę i dostają kolejną obserwację; ich wcześniejsza historia pozostaje w bazie. Program nie oznacza brakujących ofert jako sprzedanych i nie uruchamia modelu. Duplikaty tego samego mieszkania pomiędzy portalami wymagają późniejszego rozwiązania.

W GitHubie nowy workflow **Daily OLX and Otodom observations** najpierw uruchom ręcznie z `save_db` wyłączonym i obejrzyj pliki w **Artifacts**. Po dodaniu sekretu `DATABASE_URL` sprawdź ręczny zapis. Następnie zmienna `PORTAL_COLLECTION_ENABLED=true` pozwoli wykonywać kolekcję codziennie około **06:15 czasu Warszawy**, również przy wyłączonym komputerze. Harmonogram jest przygotowany w kodzie; nie został jeszcze aktywowany. Stare `PIPELINE_ENABLED` pozostaw wyłączone.

Dokładne kroki, ustawienia limitów, znaczenie liczników i ograniczenia znajdziesz w [instrukcji codziennego pobierania](docs/DAILY_COLLECTION.md). Warunki korzystania ze źródeł oraz treningu nadal wymagają ustalenia; ten harmonogram zbiera wyłącznie obserwacje.
