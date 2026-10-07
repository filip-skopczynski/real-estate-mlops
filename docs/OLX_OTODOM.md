# OLX i Otodom: przygotowanie źródeł danych

Projekt rozwijamy bez agenta językowego i bez integracji OpenAI API. Pobieranie
wykonuje kod w Pythonie, zapis obsługuje PostgreSQL, a model wyceny to XGBoost.

## Co zostało sprawdzone 7 października 2026

Wykonaliśmy mały odczyt publicznych stron, bez logowania i bez dostępu do
Supabase. Sprawdzenie korzystało ze stałego profilu HTTP `chrome120`, z
identyfikatorem `WarsawRealEstatePortfolio/0.1`. Uwzględniało robots.txt,
opóźnienie między żądaniami, timeout i limit rozmiaru odpowiedzi. Nie pobierało
stron szczegółów, nie wywoływało wewnętrznych API i nie zapisywało surowych stron.

| Źródło | Wynik odczytu | Wynik ogólnego parsera JSON-LD |
| --- | --- | --- |
| OLX: mieszkania na sprzedaż w Warszawie | HTTP 200 | 0 kompletnych rekordów |
| Otodom: mieszkania na sprzedaż w Warszawie, filtr `apartament` | Przekierowanie 301 do aktualnego adresu, następnie HTTP 200 | 0 kompletnych rekordów |

OLX zawiera dane JSON-LD, ale nie w formacie kompletnych mieszkań obsługiwanym
przez `src.fetch_data`. Otodom udostępnił blok `__NEXT_DATA__`; ogólny parser JSON-LD nie
obsługuje tego formatu. Te wyniki sprawdzają kompatybilność jednego pobrania,
nie działanie przyszłych codziennych uruchomień ani prawa do wykorzystania danych.
Otodom w tym teście obejmował wskazany filtr, nie cały katalog Warszawy.

**Samo wpisanie adresu portalu do `LISTINGS_URL` nie uruchomi sprawnej integracji.**
OLX i Otodom otrzymały następnie osobne parsery lokalnych próbek, opisane niżej.
Są oddzielnymi podglądami, bez zapisu do bazy, treningu czy harmonogramu.

## 1. Ustal dostęp i zakres wykorzystania danych

Standardowe [Partner API OLX](https://developer.olx.pl/articles/faq), punkt 6,
pozwala zarządzać własnymi ogłoszeniami na autoryzowanym koncie. Nie daje
odczytu cudzych ogłoszeń. [RE API dla Otodom](https://developer.olxgroup.com/docs/overview)
obsługuje publikację i zarządzanie ofertami klientów integracji. W tej
dokumentacji nie potwierdzono publicznego eksportu całego katalogu ofert.
Sama rejestracja w portalu deweloperskim nie potwierdza takiego dostępu.

Przed podłączeniem ofert do zbioru treningowego ustal z operatorem:

- Czy można pobierać oferty innych użytkowników dla Warszawy, raz dziennie?
- Jaki kanał jest dostępny: uzgodnione API, plik danych czy odczyt publicznych stron?
- Czy można przechowywać historię cen i używać danych do trenowania modelu regresji?
- Czy dostęp jest bezpłatny i jakie ma limity oraz warunki publikacji wyników?

[Kontakt OLX](https://developer.olx.pl/contact) i
[dokumentacja kontaktu RE API](https://developer.olxgroup.com/docs/contact-and-support)
pomagają ustalić właściwy zakres integracji. Żaden wniosek ani wiadomość nie
zostały wysłane w ramach tego sprawdzenia.

robots.txt określa preferencje odczytu przez roboty; nie zastępuje warunków
wykorzystania danych. Odczytane [robots.txt Otodom](https://www.otodom.pl/robots.txt)
zawierało również `ai-input=no` i `ai-train=no`. Ich zakres dla planowanego
wykorzystania wymaga wyjaśnienia z operatorem. Nie traktujemy dopuszczenia strony
wyszukiwania w robots.txt jako zgody na trening modelu.

## 2. Lokalne parsery

### OLX

`src.fetch_olx` służy do lokalnego podglądu jednej publicznej strony wyszukiwania
mieszkań na sprzedaż w Warszawie. Nie loguje się, nie korzysta z API ani proxy,
nie odwiedza stron szczegółów i nie przechodzi do kolejnych stron wyników.
Zapisuje identyfikator, link, cenę całego mieszkania w PLN, metraż, liczbę pokoi,
lokalizację i czas obserwacji. Brakujących cech nie wymyśla. Dane kontaktowe
sprzedawców nie są potrzebne i nie należą do eksportu.

Potwierdzony format to dane `window.__PRERENDERED_STATE__` w publicznym HTML,
ze spisem `listing.listing.ads`. Parser odczytuje JSON bez wykonywania
JavaScriptu. Sprawdza kategorię sprzedaży, Warszawę i wymagane cenę całkowitą,
metraż oraz pokoje. Wartość `four` oznacza **„4 i więcej”**, dlatego parser
zachowuje tylko dokładne liczby 1, 2 i 3, a grupę 4+ pomija. Ta próbka nie
reprezentuje rozkładu liczby pokoi w całym katalogu. Nie odgadujemy wartości
z opisu ani przy pomocy LLM. Niepoprawny pojedynczy rekord jest pomijany;
brak poprawnych rekordów kończy się błędem.

Eksport ma obejmować poprawne oferty sprzedaży z ceną całkowitą i wymaganymi
cechami. To próbka pierwszej strony, bez potwierdzenia kompletności katalogu.
Zniknięcie ogłoszenia nie potwierdza sprzedaży; niepełne pobranie nie potwierdza
zniknięcia. Rozróżnianie odświeżeń i duplikatów między portalami pozostaje
kolejnym etapem. Zakres parsera opisuje [SOURCE_OLX.md](SOURCE_OLX.md).

### Otodom

`src.fetch_otodom` obsługuje osobny podgląd jednej publicznej strony mieszkań
na sprzedaż w Warszawie. Odczytuje JSON `__NEXT_DATA__`, ścieżkę
`props.pageProps.data.searchAds.items`, bez wykonywania JavaScriptu i bez API.
Sprawdza kontekst `FLAT` / `SELL` oraz Warszawę na stronie i w lokalizacji oferty.
Cena całkowita w PLN, powierzchnia i dokładna obsługiwana liczba pokoi są
wymagane. Karty inwestycji (`INVESTMENT`) i rekordy z ukrytą ceną są pomijane.
Cena i metraż muszą mieć rzeczywisty typ liczbowy w danych JSON. Potwierdzone
wartości pokoi `ONE`, `TWO`, `THREE`, `FOUR` oznaczają dokładnie 1–4; `FOUR`
sprawdziliśmy na widocznej karcie „4 pokoje”. Inne wartości są pomijane.
Opublikowane dzielnica i piętro są opcjonalne; niepotwierdzone współrzędne,
odległość i rok budowy pozostają puste. Szczegóły opisuje
[SOURCE_OTODOM.md](SOURCE_OTODOM.md).

## 3. Sprawdź mały eksport lokalnie

Uruchom z katalogu projektu, po przygotowaniu środowiska zgodnie z README:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_olx --max-listings 20 --delay 2
# Wariant bez sieci z fikcyjnymi ofertami:
.\.venv\Scripts\python.exe -m src.fetch_olx --html tests/fixtures/olx_search.html --output data/olx_offline_preview.csv --audit data/olx_offline_audit.json
# Analogiczny, niezależny podgląd Otodom:
.\.venv\Scripts\python.exe -m src.fetch_otodom --max-listings 20 --delay 2
.\.venv\Scripts\python.exe -m src.fetch_otodom --html tests/fixtures/otodom_search.html --output data/otodom_offline_preview.csv --audit data/otodom_offline_audit.json
```

Program zapisuje `data/olx_preview.csv` i `data/olx_preview_audit.json` w
przypadku OLX, a `data/otodom_preview.csv` i `data/otodom_preview_audit.json`
w przypadku Otodom. Oba podglądy zapisują pliki w ignorowanym katalogu `data/`,
bez połączenia z bazą. Nie wymagają `.env` ani kluczy
API. Porównaj cenę, powierzchnię, pokoje i link z widoczną ofertą. Domyślny limit
wynosi 20 rekordów, maksymalny 100. Opóźnienie między żądaniami wynosi co najmniej
dwie sekundy. `--html` służy do odczytu zapisanego HTML bez sieci; `--output` i
`--audit` zmieniają lokalne ścieżki plików.

Odczyt online uwzględnia robots.txt i limity czasu oraz rozmiaru odpowiedzi.
HTTP 403/429 zatrzymuje pobieranie; nie jest traktowane jako pusty katalog ani
powód do zmiany zapisanej dostępności. Próbka nie trafia do Supabase, treningu
ani harmonogramu. Warunki dalszego wykorzystania danych pozostają do ustalenia.
Linki do ofert Otodom obecne w danych OLX nie są odwiedzane.

## 4. Połącz zapis i harmonogram

Zapis do Supabase jest już podłączony przez `src.daily_listings`. Hasła są w
lokalnym `.env` i prywatnym sekrecie GitHub Actions `DATABASE_URL`. Limity stron
są osobne dla każdego adaptera. Nowy workflow `daily_portals.yml` został
włączony 7 października 2026 na 06:15 czasu Warszawy po sprawdzeniu ręcznego
pobrania, zapisu i odczytu 355 obserwacji. Raport opisuje zakres i wynik każdego
źródła. Stare `PIPELINE_ENABLED` pozostaje wyłączone; steruje ogólnym parserem
JSON-LD i treningiem. Szczegóły są w [DAILY_COLLECTION.md](DAILY_COLLECTION.md).

[Standardowa maszyna GitHub Actions w publicznym repozytorium](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#standard-github-hosted-runners-for-public-repositories)
i [Supabase Free](https://supabase.com/pricing) pozwalają rozpocząć bez stałych
opłat, w granicach limitów usług. Nie ustalono
jeszcze bezpłatnego dostępu do danych tych portali. Jednorazowy udany odczyt
strony nie rozstrzyga tej kwestii.

## Co działa już teraz

Adapter Bemovo opisany w [SOURCE_BEMOVO.md](SOURCE_BEMOVO.md) obsługuje sprawdzone
ceny i dostępność. OLX i Otodom mają osobne lokalne podglądy jednej strony oraz
codzienny, ograniczony przepływ obserwacji z zapisem do Supabase. Pobieranie
portalowe nie uruchamia treningu i nie rozstrzyga sprzedaży brakujących ofert.
