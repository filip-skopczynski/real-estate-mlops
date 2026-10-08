# Spis Warszawy, codzienne odkrywanie i odświeżanie cen

Zbieramy publiczne ogłoszenia **sprzedaży mieszkań w Warszawie** z OLX i Otodom. Pierwsze uruchomienie buduje możliwie szeroki spis. Następne codzienne uruchomienia odkrywają ogłoszenia i stopniowo sprawdzają dalsze wyniki. Okresowy szerszy przegląd odświeża także ceny starszych ofert. Postęp zapisujemy w PostgreSQL, aby przerwany przegląd mógł zostać wznowiony w następnym przebiegu.

**Stan na 8 października 2026:** Supabase przechowuje **24 606 rozpoznanych par źródło–ID: 4413 OLX i 20 193 Otodom**. Pobranie startowe, poprawiony przegląd Otodom i pierwsze codzienne zadanie zostały sprawdzone przez odczyt bazy. To liczby ogłoszeń rozróżnianych według portalu i identyfikatora. To samo mieszkanie może mieć kilka ogłoszeń; spis nie potwierdza pełnego pokrycia rynku ani dzisiejszej dostępności każdego wpisu.

## Zweryfikowane wyniki — stan na 8 października 2026

| Przebieg | ID OLX odczytane w przebiegu | ID Otodom odczytane w przebiegu | Łącznie |
| --- | --- | --- | --- |
| [Pierwszy bootstrap](https://github.com/filip-skopczynski/real-estate-mlops/actions/runs/37675646093) | 4399 | 10 451 | 14 850 |
| [Poprawiony refresh Otodom](https://github.com/filip-skopczynski/real-estate-mlops/actions/runs/37681124097) | Nie pobierano | 20 153 | 20 153 |
| [Codzienne zadanie 8 października](https://github.com/filip-skopczynski/real-estate-mlops/actions/runs/37727995096) | 1638 | 1075 | 2713 |

Pierwszy bootstrap poprzedzał obsługę mieszkań z `relatedAds` w kartach inwestycji Otodom. Poprawiony `refresh` zakończył przejście **565 stron przy 577 żądaniach**, odkrył **9711 dodatkowych ID** i zwiększył zapisany katalog Otodom do **20 162 ID**. Zachował **1260 ogłoszeń bez ceny**, bez tworzenia wymyślonych wartości.

Odczyt kontrolny potwierdził obecność wszystkich **20 153 ID** z tego refreshu. Ostatnie pola katalogowe **19 109** wpisów zgadzały się z pobraniem; pozostałe **1044 ID** miały już nowszy odczyt z następnego codziennego zadania. Wszystkie **18 893 rekordy z ceną i metrażem** w CSV refreshu miały dokładnie zgodne pola w historii cen. Nowsza obserwacja nie usuwa poprzedniej historii.

Codzienne zadanie z 8 października zakończyło się poprawnie w trybie `daily`: OLX odczytał **55 stron / 57 żądań**, a Otodom **60 stron / 62 żądania**. Odkryło **45 ID nowych dla bazy: 14 OLX i 31 Otodom**. Wszystkie **2657 rekordów z ceną** w CSV tego przebiegu sprawdziliśmy przez odczyt bazy. Harmonogram celuje w 06:15 Warszawy; ten przebieg zaczął się około 06:35, co pokazuje możliwe opóźnienie GitHuba.

Po tym zadaniu stan katalogu wynosi **24 606 ID**. Brak ceny dotyczy **1260 wpisów**, wszystkich z Otodom; braków metrażu jest **0**. Dokładna liczba pokoi pozostaje nieznana dla **753 wpisów: 640 OLX i 113 Otodom**. Zachowujemy te braki zgodnie ze źródłem. Żaden z tych przebiegów nie uruchamiał treningu modelu.

Wcześniejsza [próba pięciu stron](https://github.com/filip-skopczynski/real-estate-mlops/actions/runs/37640796024) zapisała 355 obserwacji: 218 OLX i 137 Otodom. To historyczny test pierwszego przepływu, a nie obecny rozmiar spisu.

## Dlaczego pięć stron nie wystarczało

Stare `src.daily_listings` codziennie zaczyna od stron 1–5. Nie przechodzi następnego dnia automatycznie do 6–10. Dalsze ogłoszenia mogły więc nigdy nie trafić do bazy, a ich ceny nie były odświeżane. To polecenie pozostaje przydatne jako ograniczony test dawnego przepływu.

Nowe polecenie `src.catalog_pipeline` rozdziela trzy tryby:

| Tryb | Zastosowanie |
| --- | --- |
| `bootstrap` | Pobranie startowe; szerokie przejście dostępnych wyników z możliwością wznowienia |
| `daily` | Odkrywanie nowych identyfikatorów oraz sprawdzanie dalszych wyników |
| `refresh` | Szerszy przegląd znanych i nieznanych ogłoszeń, obejmujący również starsze ceny |

Przy zapisie do bazy brak postępu źródła powoduje rozpoczęcie pobrania startowego. Gdy rozpoczęty `bootstrap` pozostaje nieukończony, `daily` najpierw sprawdza najnowsze wyniki, a następnie wznawia dalszy spis. Po ukończeniu spisu codzienne zadanie sprawdza do 30 początkowych stron i do 30 stron rotującego przeglądu dalszych wyników, z budżetem do 60 żądań na każdą fazę. Szerszy `refresh` wykorzystuje pełny skonfigurowany budżet. Zapisany punkt wznowienia jest częścią stanu konkretnego źródła i trybu.

Osiągnięcie budżetu nie oznacza zakończenia spisu. Raport rozróżnia koniec zaplanowanego przejścia od przerwania przez limit albo błąd. Ukończony szeroki przegląd może rozpocząć nowy cykl; nieukończony kontynuuje zachowany postęp.

## Uruchomienie lokalne

W katalogu głównym projektu, w terminalu VS Code:

```powershell
# Zapisz lokalny CSV i raport; bez połączenia z Supabase:
.\.venv\Scripts\python.exe -m src.catalog_pipeline --mode bootstrap
# Po skonfigurowaniu własnego .env zapisz spis, ceny i postęp:
.\.venv\Scripts\python.exe -m src.catalog_pipeline --mode bootstrap --save-db
# Codzienne odkrywanie oraz okresowy szerszy przegląd:
.\.venv\Scripts\python.exe -m src.catalog_pipeline --mode daily --save-db
.\.venv\Scripts\python.exe -m src.catalog_pipeline --mode refresh --save-db
```

Wyniki zapisują się w ignorowanych przez Git plikach `data/catalog.csv` i `data/catalog_audit.json`. Parametry `--output` i `--report` pozwalają wybrać inne pliki. `--source olx` albo `--source otodom` uruchamia pojedyncze źródło; domyślne `both` obejmuje obydwa.

`catalog.csv` zawiera unikalne pary źródło–ID faktycznie odczytane podczas danego przebiegu. Przebieg tylko Otodom nie zawiera danych OLX. Pełny zapisany katalog znajduje się w tabeli `listing_catalog`; może obejmować także identyfikatory, których nie spotkaliśmy w ostatnim odświeżeniu. Ostatni znany stan i czas odczytu nie potwierdzają bieżącej dostępności mieszkania.

W lokalnym wdrożeniu przygotowaliśmy osobny odczyt całej tabeli jako `data/catalog_latest.csv` i raport `data/catalog_latest.json`. Eksport z **8 października 2026** obejmuje **24 606 ID**; wszystkie wyeksportowane wiersze sprawdziliśmy ponownie względem bazy. Zachowuje rzeczywisty ostatni czas obserwacji każdej pary źródło–ID. Raport opisuje zakres i stan przeglądów. Te pliki przygotowuje jednorazowe lokalne narzędzie; są ignorowane przez Git i nie są automatycznymi wynikami polecenia `src.catalog_pipeline`. Kolejne pobranie odnawia swój `catalog.csv`, nie ten osobny eksport.

Bez `--save-db` program nie zapisuje trwałego punktu wznowienia. Jest to lokalna próba, której zakres zależy od ustawionego budżetu. Zapis do Supabase wymaga `DATABASE_URL` w prywatnym `.env` lub zmiennej środowiska. Nie wpisuj pełnego adresu z hasłem do kodu, dokumentacji ani zwykłych Variables na GitHubie.

Domyślne limity na pojedyncze źródło:

| Ustawienie | Wartość | Znaczenie |
| --- | --- | --- |
| `CATALOG_MAX_PAGES` / `--max-pages` | `1000` | Maksymalna liczba stron przechodzenia |
| `CATALOG_MAX_LISTINGS` / `--max-listings` | `50000` | Maksymalna liczba rozpoznanych ogłoszeń |
| `CATALOG_MAX_REQUESTS` / `--max-requests` | `700` | Budżet żądań sieciowych |
| `REQUEST_DELAY_SECONDS` / `--delay` | `2` | Minimalna przerwa w sekundach |

Sprawdzamy robots.txt, stosujemy ograniczenia wielkości odpowiedzi i czasów oczekiwania. Blokada 403/429, strona weryfikacji albo niezgodna struktura zatrzymują dane źródło. Nie korzystamy z kont, proxy, wewnętrznych API ani stron szczegółów ogłoszeń. Stan w bazie ogranicza nakładanie się pobrań, a workflow dodatkowo dopuszcza tylko jedno zbieranie portalowe naraz.

## Co zapisujemy

Tabela `listing_catalog` przechowuje identyfikator i link również przy brakującej cenie, metrażu albo dokładnej liczbie pokoi, wraz z `first_seen_at` i `last_seen_at`. OLX opisuje `four` jako „4 i więcej”; dokładna liczba pozostaje wtedy pusta. Nie wpisujemy umownie czterech. Nieznane pola oraz daty publikacji pozostają puste, gdy format źródła ich nie potwierdza. Karty inwestycji i reklamowe kopie HPR nadal nie są osobnymi mieszkaniami.

Otodom może umieszczać osobne mieszkania w `relatedAds` jednej karty inwestycji. Katalog rozwija jeden poziom takich grup i sprawdza każde mieszkanie tym samym walidatorem co zwykłą ofertę: własne ID i adres, sprzedaż mieszkania, Warszawa oraz własne cechy. Nie przepisuje ceny, metrażu ani lokalizacji inwestycji do jej mieszkań. Na publicznej stronie 275 z 7 października 2026 potwierdziliśmy 36 takich mieszkań; cztery widoczne linki miały ceny i metraże zgodne z odczytem. Sama karta inwestycji pozostaje pominięta. Raport rozróżnia karty nadrzędne, rozwinięte mieszkania i odrzucone rekordy.

Tabela `collection_progress` zachowuje punkt wznowienia i stan ukończenia dla źródła oraz trybu. Czasowe zajęcie przeglądu przez jeden proces ogranicza równoczesny zapis postępu. Nowe tabele są dodawane bez usuwania istniejących danych; w PostgreSQL mają włączone RLS.

Istniejące `listings` i `listing_observations` obejmują rekordy ze zweryfikowaną ceną całkowitą w PLN i metrażem; liczba pokoi może pozostawać pusta. Rekord z brakującymi cechami jest użyteczny w spisie, ale wymaga dalszej oceny przed wykorzystaniem w modelu. Pipeline nie uruchamia treningu ani nie wycenia mieszkań.

**„Nowa oferta” oznacza identyfikator po raz pierwszy zapisany w naszej bazie.** Ogłoszenie opublikowane miesiąc temu może dziś zostać przez nas odkryte. To nie dowód nowej publikacji ani nowego mieszkania na rynku. Zmiana ceny znanego ID tworzy obserwację i zachowuje historię. Czas obserwacji jest rzeczywistym czasem pobrania; daty pierwszej publikacji nie potwierdziliśmy w obu formatach, więc jej osobne pole pozostaje puste. Jedno mieszkanie zamieszczone na obu portalach może mieć dwa ID; łączenie takich nieruchomości wymaga osobnego etapu.

Cena z wcześniejszego pobrania zachowuje swój wcześniejszy czas. Dzisiejsze odkrywanie nowych ofert nie oznacza, że sprawdziliśmy dziś cenę każdego starego ogłoszenia. Szerszy `refresh` aktualizuje tę część danych, którą rzeczywiście ponownie odczyta.

Nie zmieniamy tabel dostępności Bemovo. Brak oferty w wynikach portalu nie oznacza sprzedaży ani wycofania; przesunięcia stron, limity i zakończenie budżetu mogą powodować pominięcia.

## GitHub Actions

Workflow **Warsaw OLX and Otodom catalogue** znajduje się w `.github/workflows/daily_portals.yml`. Testy uruchamiają się najpierw z odizolowanym PostgreSQL 16. Dane produkcyjne nie są bazą testową.

W **Actions → Warsaw OLX and Otodom catalogue → Run workflow** wybierz `main`, tryb `bootstrap`, `daily` albo `refresh` i ustaw **save_db**. Domyślnie ręczne uruchomienie ma `daily` i wyłączony zapis, co pozwala obejrzeć CSV i raport. Pliki są dostępne jako **Artifacts** przez 14 dni. Ręczny `bootstrap` z zapisem uruchamia pobranie startowe; ponowienie może kontynuować zachowany postęp.

Pole **source** pozwala ręcznie wybrać `both`, `olx` lub `otodom`, np. odświeżyć pojedynczy portal po poprawie jego parsera. Uruchomienia z harmonogramu zawsze obejmują oba źródła.

| Ustawienie GitHuba | Zastosowanie |
| --- | --- |
| Secret `DATABASE_URL` | Prywatne połączenie Supabase Session pooler z SSL |
| Variable `PORTAL_COLLECTION_ENABLED=true` | Włącza planowane pobrania z zapisem do bazy |
| Variables `CATALOG_MAX_PAGES`, `CATALOG_MAX_LISTINGS`, `CATALOG_MAX_REQUESTS` | Zastępują domyślne budżety w tabeli powyżej |
| Variable `REQUEST_DELAY_SECONDS` | Zwiększa przerwę między żądaniami |

Harmonogram wyzwala zadanie około **06:15 czasu Warszawy**, także gdy Twój komputer jest wyłączony. W niedzielę wybieramy `refresh`, a w pozostałe dni `daily`; dzień określamy w strefie `Europe/Warsaw`. GitHub obsługuje strefę IANA i zmianę czasu letniego. Workflow korzysta z gałęzi `main`. [Składnia harmonogramu GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)

To planowana godzina startu. GitHub może opóźnić lub pominąć przebieg, a publiczne harmonogramy wyłącza po 60 dniach bez aktywności w repozytorium. Sprawdzaj raport i historię Actions. `PORTAL_COLLECTION_ENABLED=false` zatrzymuje kolejne planowane pobrania. [Ograniczenia schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

Stare `PIPELINE_ENABLED` pozostaw wyłączone: obsługuje ogólny parser JSON-LD i trening. Zmienne `OLX_MAX_PAGES`, `OTODOM_MAX_PAGES` oraz `PORTAL_MAX_LISTINGS` dotyczą dawnego `src.daily_listings`; nowy harmonogram korzysta z `CATALOG_*`.

## Jak oceniać pokrycie

W sprawdzeniu z 7 października 2026 OLX podał 4437 wyników widocznych, ale tylko 1000 pozycji i 25 stron udostępnionych przez wyszukiwanie. Otodom podał 20 252 wyniki i 563 strony. To liczby z jednej chwili i określonych filtrów; zmieniają się wraz z ofertami.

Spis przechodzi wyniki mieszczące się w budżecie i raportuje ograniczenia źródła. OLX w `bootstrap` i `refresh` dzieli wyszukiwania przekraczające pułap na mniejsze zakresy cen. Filtry `search[filter_float_price:from]` i `search[filter_float_price:to]` zostały sprawdzone na publicznych wynikach; próbny przedział 900 000–1 000 000 PLN zmniejszył wyszukiwanie z pułapem do 443 wyników i 12 stron. Kolejne podziały zachowują nakładanie granic, a identyfikatory usuwają powtórzenia. Górny otwarty zakres może być rozszerzany. Nierozwiązane przedziały, w tym zbyt wiele ofert o tej samej cenie, trafiają do raportu; ceny ułamkowe nie są zaokrąglane do rozłącznych luk.

Podział cenowy nie gwarantuje dotarcia do każdej oferty bez ceny. Zmiany kolejności podczas pobierania również mogą powodować pominięcia. Raport nie obiecuje stuprocentowego pokrycia Warszawy, nawet gdy kolejka zaplanowanych stron została ukończona.

Weryfikacja z 7 października 2026 potwierdziła publiczne sortowania: OLX `search[order]=created_at:desc`, Otodom `by=LATEST&direction=DESC`, odpowiadające zastosowanym parametrom strony i widocznemu wyborowi najnowszych wyników. Każda strona musi potwierdzać oczekiwany kontekst. To nadal nie daje gwarancji wykrycia wszystkich nowych publikacji. `daily` odczytuje cały skonfigurowany początkowy zakres; nie kończy po spotkaniu pierwszego znanego ID. Promowanie, odświeżanie i przesuwanie ofert wymagają okresowego szerszego przeglądu. `createdTime` w OLX oraz `dateCreated` i `pushedUpAt` w Otodom nie stanowią zweryfikowanej daty pierwszej publikacji; `published_at` pozostaje puste dla obu źródeł.

Dostępność publicznego HTML i robots.txt nie ustalają uprawnień do dowolnego ponownego wykorzystania danych. Otodom publikuje `search=yes, ai-input=no, ai-train=no`. Warunki wykorzystania danych do modelu pozostają do ustalenia. Szczegóły opisują [OLX](SOURCE_OLX.md), [Otodom](SOURCE_OTODOM.md) i [ustalenia dostępu](OLX_OTODOM.md).
