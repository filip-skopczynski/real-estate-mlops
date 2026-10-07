# Codzienne obserwacje OLX i Otodom

Dodaliśmy osobne uruchomienie, które odczytuje kolejne publiczne strony sprzedaży mieszkań w Warszawie, usuwa powtórzenia w obrębie źródła i może dopisywać ceny do Supabase. Domyślnie sprawdza najwyżej **5 stron OLX i 5 stron Otodom**, z limitem **500 poprawnych rekordów na źródło**. Zapis do bazy wymaga `--save-db`. Harmonogram jest przygotowany w kodzie, ale nie został włączony w ustawieniach GitHuba.

To szersza próbka niż wcześniejsze podglądy jednej strony. Zakres pozostaje ograniczony do mieszkań na sprzedaż w Warszawie, a nie całych portali, wynajmu czy innych miast.

## 1. Sprawdź lokalny wynik

Uruchom z katalogu głównego projektu, w terminalu VS Code:

```powershell
.\.venv\Scripts\python.exe -m src.daily_listings
```

To zapisze `data/daily_listings.csv` oraz `data/daily_collection_audit.json`. Pierwszy plik zawiera odczytane ceny i cechy; drugi opisuje pobranie każdego źródła, zakres i powód zakończenia. Ten tryb nie łączy się z bazą. Dane i raporty są ignorowane przez Git.

Limity możesz podać wprost:

```powershell
.\.venv\Scripts\python.exe -m src.daily_listings --max-pages-olx 5 --max-pages-otodom 5 --max-listings 500 --delay 2
```

Program zachowuje co najmniej dwie sekundy przerwy między żądaniami. Sprawdza robots.txt i kończy pobieranie danego źródła na blokadzie 403/429, stronie weryfikacji lub niezgodnej strukturze. Nie korzysta z logowania, proxy, wewnętrznych API ani stron szczegółów ofert.

Możesz też sprawdzić obydwa źródła bez internetu na fikcyjnych ofertach:

```powershell
.\.venv\Scripts\python.exe -m src.daily_listings --offline-olx tests/fixtures/olx_search.html --offline-otodom tests/fixtures/otodom_search.html --max-pages-olx 1 --max-pages-otodom 1
```

Wcześniejsze polecenia `src.fetch_olx` i `src.fetch_otodom` nadal oznaczają osobny podgląd jednej strony. Przechodzenie po kolejnych stronach jest dostępne w nowym uruchomieniu `src.daily_listings`.

## 2. Zrozum, co znaczy „nowa oferta”

W tym projekcie **nowa oferta oznacza identyfikator, który pierwszy raz zapisujemy w naszej bazie**. Stara oferta, którą pierwszy raz znajdziemy dziś, też jest dla nas nowa. Data obserwacji jest czasem pobrania; nie zastępuje daty publikacji. Odświeżenie lub płatne promowanie ogłoszenia nie tworzy nowego identyfikatora.

| Licznik zapisu | Znaczenie |
| --- | --- |
| `new_listings` | Pierwszy zapis pary źródło + identyfikator |
| `existing_listings` | Oferta była już w naszej bazie |
| `observations_inserted` | Dopisane obserwacje cen, także dla znanych ofert z niezmienioną ceną |

Tabela `listings` przechowuje ostatnią znaną cenę oraz `first_seen_at` i `last_seen_at`. `listing_observations` zachowuje historię. Jedna nieruchomość wystawiona na obu portalach może nadal mieć dwa różne rekordy; rozpoznawanie takich duplikatów nie jest jeszcze zaimplementowane.

## 3. Dopiero potem zapisz do Supabase

Uzupełnij `DATABASE_URL` w swoim lokalnym `.env`, a po sprawdzeniu CSV i raportu uruchom:

```powershell
.\.venv\Scripts\python.exe -m src.daily_listings --save-db
```

Poprawne obserwacje zapisują się w transakcji osobno dla każdego źródła. Awaria jednego źródła nie usuwa danych z drugiego. Program raportuje błąd lub częściowe pobranie i kończy się niezerowym kodem, gdy nie udało się wykonać zaplanowanych żądań; osiągnięcie ustawionego limitu jest zwykłym zakończeniem ograniczonej próbki. Raport i zachowane poprawne rekordy pokazują, co udało się odczytać.

Ten przepływ nie zmienia tabel dostępności, nie oznacza zniknięć jako sprzedaży i nie uruchamia treningu. Nie ma pełnego spisu aktualnych ogłoszeń, na podstawie którego można byłoby bezpiecznie wnioskować o brakujących mieszkaniach.

## 4. Sprawdź ręczne uruchomienie na GitHubie

Po umieszczeniu nowego kodu na gałęzi `main` otwórz repozytorium → **Actions → Daily OLX and Otodom observations → Run workflow**. Wybierz `main` i pozostaw **save_db** wyłączone. Nie potrzebujesz wtedy sekretu bazy.

GitHub najpierw uruchomi testy z odizolowaną bazą PostgreSQL 16, a następnie pobierze ograniczoną próbkę. Zakończony przebieg udostępni CSV i raport w sekcji **Artifacts** przez 14 dni. Komputer może być wyłączony; zadanie wykonuje maszyna GitHuba. Pliki z ofertami nie są dopisywane do repozytorium.

## 5. Dodaj połączenie z bazą jako sekret

W repozytorium wybierz **Settings → Secrets and variables → Actions → Secrets → New repository secret**. Nazwa: `DATABASE_URL`. Wartość: Twój pełny adres Supabase Session pooler z hasłem i `sslmode=require`, wklejony prywatnie w GitHubie. Nie wpisuj go do kodu, README ani zwykłych Variables.

Teraz wykonaj jeszcze jedno ręczne uruchomienie z zaznaczonym **save_db** i sprawdź liczniki w raporcie oraz dane w Supabase. Zapis ręczny działa także przy wyłączonym harmonogramie.

## 6. Włącz raz dziennie

Po sprawdzeniu pobierania, zapisu i warunków korzystania ze źródeł otwórz **Settings → Secrets and variables → Actions → Variables → New repository variable**. Dodaj `PORTAL_COLLECTION_ENABLED` o wartości `true`.

| Zmienna GitHuba | Wartość domyślna | Co ogranicza |
| --- | --- | --- |
| `OLX_MAX_PAGES` | `5` | Liczbę publicznych stron OLX |
| `OTODOM_MAX_PAGES` | `5` | Liczbę publicznych stron Otodom |
| `PORTAL_MAX_LISTINGS` | `500` | Rekordy na pojedyncze źródło |
| `REQUEST_DELAY_SECONDS` | `2` | Minimalną przerwę w sekundach |

Nowy workflow jest ustawiony na **06:15 czasu Warszawy**, z `timezone: Europe/Warsaw`, i zapisuje obserwacje do bazy. GitHub obsługuje strefy IANA oraz zmianę czasu letniego. Harmonogram działa z gałęzi domyślnej; dodatkowo ten projekt wymaga `main`. [Dokumentacja składni GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)

Godzina jest planowanym wyzwoleniem, a nie gwarancją ukończenia o 06:15. GitHub może opóźnić lub pominąć uruchomienie, a publiczny harmonogram wyłącza po 60 dniach bez aktywności w repozytorium. Sprawdzaj historię Actions. Wyłączenie zmiennej `PORTAL_COLLECTION_ENABLED` zatrzymuje kolejne pobrania z harmonogramu. [Zdarzenie schedule w GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

Pozostaw stare `PIPELINE_ENABLED` wyłączone: steruje innym zadaniem, z ogólnym parserem JSON-LD i treningiem. Nowy workflow portalowy nie korzysta z `LISTINGS_URL`, `MAX_PAGES` ani `MAX_LISTINGS` tego starego zadania.

## Dlaczego to nie oznacza całego serwisu

W zapisanym sprawdzeniu z 7 października 2026 OLX podał 4437 wyników widocznych, ale tylko 1000 pozycji i 25 stron dostępnych w metadanych wyszukiwania. Otodom podał 20 252 wyniki i 563 strony. Liczby zależą od chwili i filtrów. Domyślnych pięć stron jest ograniczoną próbką, a nie pełnym zbiorem Warszawy. Samo zwiększenie limitu nie usuwa pułapu wyszukiwania OLX.

Nie potwierdziliśmy sortowania według najnowszej publikacji: Otodom wskazuje `DEFAULT/DESC`, a wyniki zawierają także stare odświeżone ogłoszenia. Promowanie i nowe wpisy przesuwają strony podczas pobierania. Dlatego program sprawdza kolejne skonfigurowane strony i usuwa powtórzenia; nie przerywa po pierwszym znanym identyfikatorze. Nadal może coś przeoczyć.

Parser OLX pomija „4 i więcej pokoi”, ponieważ nie znamy dokładnej liczby. Otodom obsługuje zweryfikowane 1–4 pokoje, a nieznane wartości, inwestycje, ukryte ceny i reklamowe kopie HPR pomija. Próbka obejmuje wyłącznie rekordy zgodne z kontraktem parserów. Raport nie może być traktowany jako dowód pełnego pokrycia rynku ani sprzedaży brakujących ofert.

Dostępność publicznego HTML i robots.txt nie ustalają uprawnień do dowolnego ponownego wykorzystania danych. Warunki automatycznej zbiórki i treningu nadal wymagają ustalenia; Otodom publikuje `ai-input=no, ai-train=no`. Nowy workflow zbiera obserwacje i nie uruchamia modelu. Szczegóły opisują [OLX](SOURCE_OLX.md), [Otodom](SOURCE_OTODOM.md) oraz [ustalenia dostępu](OLX_OTODOM.md).
