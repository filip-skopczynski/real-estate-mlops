# Kontrola jakości danych i podobnych ogłoszeń

Ten etap pomaga sprawdzić zebrany katalog przed dalszą analizą. Program zachowuje wszystkie wiersze wejściowe i dopisuje oznaczenia do osobnej kopii. Raport pozwala znaleźć braki, podejrzane wartości oraz pary ogłoszeń, które warto porównać ręcznie.

## Wynik na eksporcie z 8 października 2026

Audyt objął **24 606 wierszy** z `catalog_latest.csv`. Wszystkie pary źródło–ID są unikalne; w tym eksporcie nie ma powtórzeń tego klucza.

| Wynik reguł | Wiersze |
| --- | ---: |
| `pass` | 22 470 |
| `incomplete` | 2004 |
| `review` | 132 |

Brakuje **1260 cen** i **753 dokładnych liczb pokoi**. Te braki i flagi mogą występować razem, dlatego ich sumy nie odpowiadają liczbie wierszy o pojedynczym statusie. Wszystkie metraże są podane, ale reguły wskazały **4 powyżej 500 m²** oraz **1 poniżej 10 m²**. **10 cen poniżej 100 000 PLN**, **32 powyżej 10 mln PLN** oraz odstępstwa ceny za m² wymagają przeglądu. Są to sygnały do sprawdzenia, nie rozstrzygnięcia o błędach lub okazjach.

Program znalazł **5201 podobnych par OLX–Otodom**, wykonując **49 198 porównań**. Nie osiągnął limitu par ani porównań. Są to pary do ręcznej kontroli, a nie liczba potwierdzonych duplikatów ani mieszkań przeznaczonych do usunięcia. Program nie tworzy z tych par grup nieruchomości.

Rok budowy pozostaje nieznany dla wszystkich **24 606 wpisów**, dzielnica dla **158**, a współrzędne dla **21 007**. To ograniczenie zebranych cech, którego nie naprawia samo odrzucenie skrajnych cen. Ostatnie czasy obserwacji obejmują dwa dni UTC; eksport ostatniego stanu nie zastępuje historii potrzebnej do oceny modelu na przyszłych danych.

Raport lokalny zapisuje sumę SHA-256 wejścia, zastosowane progi i czas audytu, aby wynik można było przypisać do konkretnej wersji CSV. Dane źródłowe, baza i dotychczasowy model pozostają bez zmian.

## Uruchomienie

W terminalu otwartym w głównym folderze projektu wykonaj:

```powershell
.\.venv\Scripts\python.exe -m src.quality --input data/catalog_latest.csv --output-dir data/quality
```

Przy aktywnym środowisku możesz użyć krótszego polecenia:

```powershell
python -m src.quality --input data/catalog_latest.csv --output-dir data/quality
```

Wejściem jest lokalny CSV. Nie potrzebujesz `.env` ani połączenia z Supabase. To polecenie nie pobiera ofert z internetu, nie zapisuje zmian w bazie, nie zmienia harmonogramu i nie trenuje modelu.

`data/catalog_latest.csv` to wcześniej przygotowany eksport całej tabeli katalogu. Nazwa pliku nie gwarantuje aktualności: sam raport jakości nie odświeża eksportu. Sprawdź jego opis w `data/catalog_latest.json` oraz rzeczywiste czasy `observed_at`. Starszy czas oznacza wcześniejsze sprawdzenie ogłoszenia; zapisany rekord nie potwierdza dzisiejszej dostępności.

## Co powstaje

| Plik w `data/quality` | Do czego służy |
| --- | --- |
| `annotated_catalog.csv` | Wszystkie wejściowe wiersze i ich wartości, uzupełnione o `quality_flags`, `quality_status` i `audit_price_per_m2` |
| `quality_report.json` | Raport do dalszego przetwarzania: liczniki, zastosowane reguły i informacja o ograniczeniach wyszukiwania podobnych ofert |
| `quality_report.md` | Raport do przeczytania: podsumowanie jakości i wyników kontroli |
| `duplicate_candidates.csv` | Pary podobnych ogłoszeń z różnych portali, przeznaczone do ręcznego sprawdzenia |

Oryginalny CSV pozostaje bez zmian. Wiersze z brakującymi lub podejrzanymi wartościami nadal są widoczne w pliku z oznaczeniami. Dane i raporty w `data/` pozostają lokalne, zgodnie z `.gitignore`.

`audit_price_per_m2` to cena całkowita podzielona przez dodatni metraż, gdy oba pola dają się poprawnie odczytać. Pomaga zauważyć pomyłki jednostek lub nietypowe ogłoszenia. Ta kolumna zawiera cenę, czyli wartość przewidywaną przez model; nie należy przekazywać jej jako cechy do modelu przewidującego cenę całkowitą.

## Brak informacji i sygnał do sprawdzenia

Brak ceny, metrażu lub dokładnej liczby pokoi oznacza niekompletny rekord. Sam brak nie dowodzi, że ogłoszenie jest błędne. Przykładowo OLX „4 i więcej pokoi” daje dolną granicę liczby pokoi, ale nie pozwala zapisać dokładnie `4`. W raporcie zachowujemy tę niepewność.

Osobnego sprawdzenia wymagają m.in. wartości niedające się odczytać jako poprawne liczby, wartości niedodatnie, nieprawidłowy czas obserwacji, powtórzona para `source`–`listing_id` oraz wartości wykraczające poza progi kontrolne. Flagi mogą współistnieć: rekord może mieć jednocześnie brakującą cenę i inną wartość wymagającą sprawdzenia.

`quality_flags` wyjaśnia wykryte problemy, a `quality_status` zbiera wynik kontroli w jednym polu. Przejście tych reguł oznacza tylko przejście tego audytu. Nie potwierdza prawdziwości oferty, dostępności mieszkania ani gotowości rekordu do treningu.

| `quality_status` | Znaczenie |
| --- | --- |
| `pass` | Wiersz przeszedł zastosowane reguły i ma cenę, metraż oraz dokładną liczbę pokoi |
| `incomplete` | Brakuje przynajmniej jednego z tych trzech pól, bez wykrytego problemu wymagającego sprawdzenia |
| `review` | Wykryto niepoprawną lub podejrzaną wartość, problem czasu albo tożsamości rekordu; wiersz może dodatkowo mieć braki |

Najważniejsze oznaczenia:

| Flaga | Co oznacza |
| --- | --- |
| `missing_price`, `missing_area`, `missing_exact_rooms` | Brak ceny, metrażu lub dokładnej liczby pokoi |
| `invalid_price`, `invalid_area`, `invalid_rooms` | Wartość jest podana, ale nie spełnia wymagań poprawnego pola liczbowego |
| `rooms_below_minimum` | Dokładna liczba pokoi przeczy zapisanej dolnej granicy |
| `invalid_source`, `invalid_listing_id`, `not_warsaw` | Problem źródła, identyfikatora lub miasta |
| `invalid_observed_at`, `unverified_timestamp_timezone` | Niepoprawny czas obserwacji albo brak potwierdzonej strefy czasowej |
| `repeated_identity` | Para źródło–ID występuje w wejściu więcej niż raz |
| `conflicting_identity_timestamp` | Rekordy tej samej tożsamości i czasu mają sprzeczne dane |
| `price_below_review_min`, `price_above_review_max` | Cena przekracza dolny lub górny próg kontrolny |
| `area_below_review_min`, `area_above_review_max` | Metraż przekracza dolny lub górny próg kontrolny |
| `price_per_m2_below_review_min`, `price_per_m2_above_review_max` | Cena za m² przekracza dolny lub górny próg kontrolny |

Niepoprawne podane liczby w polach opcjonalnych, np. `floor`, `build_year`, `rooms_min`, współrzędnych lub odległości, otrzymują odpowiednią flagę `invalid_FIELD`. Brak roku budowy, współrzędnych lub dzielnicy jest wykazywany jako brak pokrycia cech. Sam taki brak nie nadaje statusu `incomplete`; ten status dotyczy ceny, metrażu i dokładnych pokoi.

### Progi kontrolne

| Pole | Przedział kontrolny, z uwzględnieniem obu granic | Co sprawdzamy poza przedziałem |
| --- | --- | --- |
| Cena całkowita | 100 000–10 000 000 PLN | Czy cena dotyczy całego mieszkania, czy poprawnie odczytano kwotę i walutę |
| Metraż | 10–500 m² | Czy odczytano powierzchnię mieszkania i właściwą jednostkę |
| Cena za m² | 3000–50 000 PLN/m² | Czy cena całkowita oraz metraż opisują ten sam przedmiot sprzedaży |

To stałe, umowne reguły przeglądu danych. Nie wyznaczają aktualnych cen rynkowych Warszawy, wartości mieszkania ani granic zbioru treningowego. Poprawna oferta może przekraczać te progi. Program ją oznacza i zachowuje, aby można było zweryfikować źródło oraz przyczynę odstępstwa.

## Jak szukamy podobnych ogłoszeń

Powtórzenie tego samego `source`–`listing_id` wymaga rozróżnienia między powtórzoną kopią a kolejną obserwacją historii. Flaga `repeated_identity` sama nie oznacza uszkodzonych danych. Ten sam lokal wystawiony na OLX i Otodom może mieć dwa różne identyfikatory; do jego wykrycia potrzebne jest porównanie cech.

Przed porównaniem program wybiera najnowszy wiersz dla danego źródła i ID, korzystając z poprawnego czasu obserwacji zawierającego strefę czasową. Przy niejednoznacznym remisie najnowszych obserwacji wyłącza daną tożsamość z porównania. Nie zastępuje brakującego czasu dzisiejszą datą.

Porównanie obejmuje obsługiwane źródła OLX i Otodom, poprawne identyfikatory, Warszawę oraz dodatnie ceny i metraże. Dzielnica jest porównywana w całości po ujednoliceniu wielkości liter i odstępów; nie zgadujemy dzielnic na podstawie podobnych nazw. Kandydaci powstają z surowych danych według tych reguł, niezależnie od `quality_status`. Podejrzana cena oznaczona do przeglądu może więc nadal wystąpić w parze.

Para trafia do listy kandydatów, jeśli spełnia reguły:

1. Ogłoszenia pochodzą z różnych źródeł.
2. Mają znaną i zgodną dzielnicę oraz dokładną liczbę pokoi. Brak dzielnicy lub dokładnych pokoi wyłącza porównanie.
3. Różnica metrażu wynosi najwyżej większą z wartości: **0,1 m²** lub **0,5% mniejszego metrażu**.
4. Różnica ceny wynosi najwyżej **1% mniejszej ceny**.
5. Jeśli oba piętra są znane, muszą się zgadzać. Brak piętra dopuszcza parę, ale osłabia dowody podobieństwa.

Przykład fikcyjny: dla mieszkań o powierzchniach 50 i 50,2 m² dopuszczalna różnica wynosi 0,25 m². Dla cen 900 000 i 905 000 PLN dopuszczalna różnica wynosi 9000 PLN. Przy zgodnej dzielnicy, pokojach i znanych piętrach taka para spełnia reguły podobieństwa. Nadal może opisywać dwa różne mieszkania.

Program porównuje oferty w grupach o zgodnej dzielnicy i pokojach. Ogranicza pracę do **1 000 000 porównań** i domyślnie wynik do **10 000 par**. Parametrem `--max-pairs` można obniżyć limit par. Raport wskazuje, gdy któryś limit skrócił wyszukiwanie. W takim przypadku lista obejmuje znalezioną część kandydatów. Nawet bez osiągnięcia limitu brak pary nie dowodzi, że mieszkanie występuje tylko raz.

## Jak interpretować kandydatów

`duplicate_candidates.csv` jest kolejką do sprawdzenia. Program nie scala ogłoszeń, nie usuwa rekordów i nie wybiera automatycznie „prawidłowej” oferty.

W jednej inwestycji deweloperskiej dwa mieszkania mogą mieć taki sam metraż, liczbę pokoi, piętro i cenę. Z kolei jeden lokal może pojawić się na dwóch portalach z różnymi cenami albo inaczej zaokrągloną powierzchnią. Reguły mogą więc wskazać różne mieszkania jako podobne i pominąć prawdziwe powtórzenia.

Katalog nie zawiera obecnie adresów, tytułów ani zdjęć potrzebnych do dokładniejszego rozpoznania nieruchomości. Sam zestaw cech liczbowych i dzielnica nie wystarczają do potwierdzenia tożsamości mieszkania. Zmiana ID po ponownym wystawieniu w obrębie jednego portalu również pozostaje poza tym porównaniem między źródłami.

## Co robimy po raporcie

Najpierw przeczytaj `quality_report.md`, potem obejrzyj oznaczone wiersze i przykładowe pary w `duplicate_candidates.csv`. Dla pary sprawdź oba odnośniki do ofert, dostępne cechy oraz czasy obserwacji. Zachowaj rozróżnienie między potwierdzonym powtórzeniem a samym podobieństwem.

Kolejny etap to zbieranie rzeczywistej historii, ręczna weryfikacja wybranych par i zaplanowanie podziału danych do oceny modelu. Ta heurystyka nie gwarantuje, że to samo mieszkanie nie znajdzie się po obu stronach podziału trening/test. Nie należy tworzyć grup do takiego podziału na podstawie podobieństwa ceny: wykorzystywałoby to przewidywany cel modelu. Potwierdzone niezależnie grupy nieruchomości oraz czas obserwacji trzeba uwzględnić osobno.

Obecne `src.preprocess` i `src.train` działają na dotychczasowych zasadach. Raport jakości nie zmienia ich wejścia ani filtrów, a trening na danych z portali pozostaje wyłączony.
