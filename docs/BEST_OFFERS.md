# Eksperymentalny model i wyświetlanie ofert

Projekt rozdziela dwa kroki: `src.build_deals` przygotowuje model i ranking, a `best_offers.py` wyświetla zapisane wyniki. Model przewiduje **ceny ofertowe**. Oferty z dużą różnicą względem jego przewidywania są kandydatami do sprawdzenia. Taki wynik nie potwierdza oszczędności ani wartości transakcyjnej.

## Przygotuj model i ranking

W terminalu otwartym w głównym folderze projektu, ze środowiskiem Python projektu, uruchom:

```powershell
python -m src.build_deals
```

To ręcznie uruchamiany eksperyment na rzeczywistych, wcześniej zgromadzonych ofertach OLX i Otodom dotyczących sprzedaży mieszkań w Warszawie. Program odczytuje historię cen oraz ostatni katalog z Supabase, korzystając z `DATABASE_URL` w lokalnym `.env`. Odczyt ma wspólny, spójny stan bazy. Nie dopisuje rekordów do Supabase i nie odwiedza stron portali. Nie używa danych demonstracyjnych ani pilotażu jednej inwestycji Bemovo.

Po udanym przebiegu powstają lokalne pliki:

| Plik | Zawartość |
| --- | --- |
| `data/observations.csv` | Odczytana historia cen obu obsługiwanych źródeł |
| `data/catalog_latest.csv` | Odczytany ostatni katalog, także rekordy z brakami |
| `data/catalog_snapshot.json` | Opis tego odczytu katalogu |
| `data/catalog_latest.json` | Taki sam aktualny opis katalogu, pod wcześniejszą nazwą pliku |
| `data/training.csv` | Wybrane najwcześniejsze obserwacje po kontroli jakości |
| `data/latest.csv` | Najnowsze, dopuszczone do przewidywania rekordy z katalogu |
| `data/scored_listings.csv` | Przewidywania modelu i informacja o podziale danych |
| `data/deals.csv` | Ograniczona lista kandydatów przechodzących filtry wyświetlacza |
| `data/build_deals_report.json` | Raport przygotowania danych i rankingu |
| `models/model.joblib` | Wytrenowany XGBoost, przygotowanie cech i metadane |
| `models/metrics.json` | Wyniki walidacji i testu oraz porównanie z prostym modelem bazowym |

Domyślnie budujemy model z limitem 500 drzew i zapisujemy do 50 kandydatów, przy progu 15% oraz maksymalnym wieku odczytu 7 dni. Przykład z podanymi ustawieniami:

```powershell
python -m src.build_deals --top 50 --discount-threshold 15 --max-age-days 7 --n-estimators 500
```

Pliki wynikowe w domyślnych folderach zastępują wcześniejsze lokalne wyniki udanego przebiegu. Błąd przygotowania danych lub treningu nie powinien zastępować działającego kompletu plików. Dane i model pozostają ignorowane przez Git. Ten krok nie włącza automatycznego treningu ani nie zmienia harmonogramu pobierania.

Model i ranking nie odświeżają się samodzielnie raz dziennie. Harmonogram zbierania aktualizuje Supabase, a ręczne polecenie budowania tworzy nowy lokalny zestaw wyników. Samo uruchomienie `best_offers.py` ponownie sprawdza wiek zapisanych rekordów, ale nie pobiera nowszych cen.

### Bez połączenia z bazą

Możesz użyć wcześniej zapisanej historii i katalogu. Oba pliki są wymagane; sam spis ostatnich cen nie zastępuje historii. Program odrzuca zbieżność ścieżek wejść i wyników, dlatego podaj osobne foldery wyników:

```powershell
python -m src.build_deals --observations data/observations.csv --catalog data/catalog_latest.csv --data-dir data/experimental --models-dir models/experimental
python best_offers.py --input data/experimental/deals.csv --top 5
```

### Jak przygotowujemy dane

Najpierw wybieramy **najwcześniejszą surową obserwację każdej pary źródło–ID**, a dopiero potem sprawdzamy jej jakość. Jeśli pierwszy rekord jest niepoprawny lub niejednoznaczny, nie zastępujemy go późniejszą, wygodniejszą ceną. Zachowujemy rzeczywiste czasy obserwacji; nie udajemy dłuższej historii przez dopisanie dat.

Wybór danych do nauki wymaga przejścia reguł audytu, ale samo `pass` nie dowodzi reprezentatywności zbioru. Stałe progi kontrolne ograniczają próbkę modelu; metryki dotyczą tej próbki, a nie wszystkich ofert Warszawy. Model jest oceniany na podziale czasowym na trening, walidację i test, z oddzielnymi identyfikatorami źródło–ID. Przetwarzanie cech uczy się tylko na treningu. Do raportu trafia także wynik prostej prognozy opartej na medianie cen treningowych; model może okazać się od niej gorszy.

Program wymaga technicznego minimum 30 poprawnych, unikalnych ofert z co najmniej trzech dni pierwszych obserwacji UTC oraz minimum dwóch ofert w każdej części podziału. Spełnienie tego minimum pozwala uruchomić ocenę, ale nie oznacza wystarczających danych do wiarygodnej oceny rynku.

Do rankingu źródłem ostatniego stanu jest **katalog `listing_catalog`, obejmujący również wpisy z brakami**, a nie ostatni rekord z tabeli cen, który akurat ma wszystkie pola. Jeśli ostatni wpis katalogu nie ma ceny, ma niepoprawne cechy albo jest za stary, nie wracamy do wcześniejszej atrakcyjnej ceny. Braki, niejednoznaczne czasy i konflikty są odnotowywane w raportach. Najnowsze dopuszczone rekordy otrzymują predykcję, a lista kandydatów przechodzi kontrolę jakości, świeżości i pochodzenia danych opisaną poniżej. Końcowy `deals.csv` zawiera wyłącznie części `test` lub `unseen`; pomijamy również walidację, która sterowała zatrzymaniem uczenia.

### Jak czytać pierwszy wynik

**Model i ranking są eksperymentalne.** Wiersze przewidywań oraz kandydatów mają oznaczenie `ranking_status=EXPERIMENTAL`. Krótka historia, ograniczony zestaw cech i zmieniający się zakres pobierania ograniczają wnioski z metryk. Nie dopisujemy brakujących lat budowy, współrzędnych ani opisu stanu mieszkania jako potwierdzonych faktów; przygotowanie cech może uzupełniać brakujące wartości wyłącznie na potrzeby modelu.

Rozdzielenie źródło–ID nie rozdziela pewnie fizycznych mieszkań. Ten sam lokal może być obecny na dwóch portalach lub wrócić z nowym ID i trafić do innej części danych. Podobne pary z audytu nie są potwierdzonymi grupami nieruchomości. Dlatego wyniku testu nie przedstawiamy jako dowodu gotowości produkcyjnej ani skuteczności inwestycyjnej.

### Pierwszy rzeczywisty model — 15 października 2026

Odczyt Supabase obejmował **80 682 obserwacje cen** i **27 470 wpisów katalogu** OLX i Otodom. Po wyborze pierwszych obserwacji oraz kontroli jakości do oceny pozostało **25 142 unikalnych par źródło–ID**. Dni pierwszych obserwacji obejmowały 7–15 października, czyli dziewięć dni UTC.

| Część danych | Oferty | Dni pierwszych obserwacji UTC |
| --- | ---: | --- |
| Trening | 22 427 | 7 października |
| Walidacja sterująca zatrzymaniem uczenia | 43 | 8 października |
| Test | 2672 | 9–15 października |

Na tym konkretnym teście wynik wyniósł:

| Metryka | XGBoost | Mediana cen treningowych |
| --- | ---: | ---: |
| MAE | 153 174,13 PLN | 320 416,67 PLN |
| RMSE | 266 205,54 PLN | 576 730,52 PLN |
| R² | 0,7765 | −0,0491 |

MAE oznacza średnią bezwzględną różnicę między ceną ofertową a przewidywaniem. Wynik jest lepszy od mediany w tym eksperymencie, ale nie ustanawia błędu wyceny dowolnego mieszkania. Około 89% całej kohorty pochodzi z pierwszego dnia, a walidacja liczy tylko 43 oferty. Nierówny podział, niepotwierdzona tożsamość fizycznych mieszkań i braki cech ograniczają ocenę.

Oferty nie zostały w pełni sklasyfikowane według tytułu prawnego. Dane uczące mogą więc zawierać ceny udziałów lub innych praw, których nie należy utożsamiać z ceną całego mieszkania. Dodatkowy filtr adresów ofert dotyczy kandydatów do wyświetlenia; nie zmienia utrwalonego modelu ani metryk pierwszego testu.

### Ponowny wybór kandydatów — 16 października 2026

Zapisane przewidywania obejmują **24 065** ostatnich rekordów dopuszczonych do oceny. Po dodaniu filtra wskaźników udziałów i partycypacji **654** oferty spełniały warunki kandydatów; **5** wcześniej dopuszczonych ID pominięto na podstawie słów w URL. Do `data/deals.csv` zapisaliśmy **50 najwyżej sklasyfikowanych kandydatów**. Wyświetlacz domyślnie pokazuje pierwszych pięciu, a `--top 50` pozwala obejrzeć cały zapisany wynik.

Ten ponowny wybór odbył się **16 października o 00:05 czasu Warszawy**, czyli 15 października o 22:05 UTC. Model, metryki i odczyt danych nadal pochodzą z **15 października**. Nie trenowaliśmy ponownie modelu, nie dostrajaliśmy go do testu i nie zmienialiśmy dat obserwacji źródłowych.

Sprawdzenie zapisanych plików potwierdziło zgodność sum kontrolnych, odtworzenie wszystkich 50 przewidywań przez wczytany model oraz zgodność cen, URL i czasów obserwacji z katalogiem. Kandydaci pochodzą wyłącznie z części `test` lub `unseen`, bez danych treningowych i walidacyjnych. To kontrola zgodności zapisów i obliczeń; nie potwierdza tożsamości fizycznych mieszkań, ich tytułu prawnego ani aktualnej dostępności.

## Wyświetl zapisane wyniki

Otwórz plik `best_offers.py` w głównym folderze projektu. Możesz uruchomić go przyciskiem Run w VS Code, wybierając środowisko Python projektu. W terminalu otwartym w tym folderze działa też:

```powershell
python best_offers.py
```

Program wyświetla do pięciu kandydatów: cenę z ogłoszenia, przewidywaną cenę ofertową, różnicę procentową, dzielnicę, metraż, pokoje, czas ostatniego odczytu i pełny link do źródła. Pierwszy wynik ma największą procentową różnicę względem modelu spośród ofert spełniających filtry.

Żeby pokazać tylko pierwszy wynik:

```powershell
python best_offers.py --top 1
```

Odpowiednik uruchomienia przez moduł:

```powershell
python -m src.show_deals --top 5
```

## Skąd pochodzą wyniki

Domyślne wejście to `data/deals.csv`, przygotowane przez `src.build_deals` po treningu, ocenie i filtrowaniu kandydatów. Możesz wybrać inny plik rankingu:

```powershell
python best_offers.py --input data/deals.csv --top 10
```

Jeśli `deals.csv` nie istnieje, wyświetlacz wyjaśnia, którego pliku brakuje, i kończy działanie kodem 2. Katalog pobranych ofert oraz wynik audytu jakości nie zastępują rankingu. Wyświetlacz nie generuje przykładowych „okazji” ani nie uruchamia treningu samodzielnie. Model i ranking przygotowujesz oddzielnym poleceniem opisanym na początku instrukcji.

Wymagane kolumny rankingu to `source`, `listing_id`, `url`, `city`, `price_pln`, `area_m2`, `rooms`, `observed_at`, `predicted_price_pln`, `is_deal`, `seen_in_training` i `partition`. Istniejące `src.train.score_listings()` zapisuje te pola. Dodatkowe cechy, np. dzielnica i piętro, są zachowywane. Różnicę procentową viewer oblicza ponownie z ceny i predykcji.

## Jak wybieramy widoczne oferty

Program sprawdza najnowszy odczyt każdej pary źródło–ID. Nie wraca do wcześniejszej niższej ceny, jeśli nowszy rekord nie spełnia wymagań. Niejednoznaczne remisy i niepewne daty wyłączają tożsamość z tego widoku.

Widoczne rekordy muszą przejść aktualny audyt jakości, mieć dodatnią, skończoną predykcję, poprawny link OLX lub Otodom oraz potwierdzony czas ze strefą czasową. Wykluczamy rekordy oznaczone jako użyte w treningu i rekordy z nieznanym albo sprzecznym opisem podziału danych. Domyślnie czas odczytu ma najwyżej 7 dni, a różnica względem modelu wynosi co najmniej 15%:

```text
(przewidywana cena ofertowa − cena ogłoszenia) / przewidywana cena ofertowa
```

Parametry można podać jawnie:

```powershell
python best_offers.py --top 5 --max-age-days 2 --discount-threshold 20
```

`--discount-threshold 20` oznacza próg 20%. `--top` przyjmuje wartości 1–50. Brak wyniku jest poprawnym rezultatem, jeśli żadna oferta nie spełnia filtrów.

Filtry dotyczą wyłącznie wierszy obecnych w CSV. Obniżenie progu w wyświetlaczu nie przywraca ofert odrzuconych wcześniej podczas generowania `deals.csv`.

### Sygnały udziałów i partycypacji w adresach ofert

Pomijamy kandydatów, gdy słowa w adresie oferty sugerują sprzedaż udziału w mieszkaniu, partycypację TBS, odstępne albo cesję najmu. Samo wskazanie udziału w gruncie, drodze lub ogrodzie nie wystarcza temu filtrowi do wykluczenia mieszkania. To ostrożny filtr zakresu oparty na samym URL, bez pełnego czytania i klasyfikacji ogłoszenia. Nie potwierdza tytułu prawnego; może też pominąć poprawną ofertę lub nie zauważyć problemu w niewyróżniającym się adresie. Taki filtr nie naprawia klasyfikacji danych użytych wcześniej do nauki modelu.

Przykładem zauważonym podczas kontroli był [adres ogłoszenia wskazujący udział 1/2 w mieszkaniu](https://www.olx.pl/d/oferta/udzial-1-2-w-mieszkaniu-po-generalnym-remoncie-CID3-ID1cFPfc.html). Takie sformułowanie wyjaśnia wykluczenie kandydata; nie stanowi pełnej oceny prawnej oferty. Link może z czasem przestać być dostępny.

Raport wyboru kandydatów podaje `excluded_counts.nonstandard_rights_indicated` oraz szczegóły `rights_filter`. W jego `flag_counts` znajdują się wskaźniki `partial_ownership_indicated` i `housing_participation_indicated`. To osobne oznaczenia wyboru kandydatów, a nie zmiana `quality_status` ani predykcji modelu.

To ranking różnic względem modelu ceny ofertowej. Jakość modelu, cechy pominięte w danych oraz niepotwierdzona tożsamość mieszkań między portalami wpływają na jego interpretację. Czas odczytu nie potwierdza bieżącej dostępności; przed oceną kandydata sprawdź oryginalny link. Wyświetlacz nie łączy duplikatów i nie rozstrzyga o wartości transakcyjnej mieszkania.

Sam wyświetlacz czyta lokalny CSV. Zachowuje plik wejściowy, bazę i model; nie otwiera stron ofert automatycznie i nie wymaga `.env`.
